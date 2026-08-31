"""The dependency graph and the traversals over it.

The estate these are written against is the one every install ends up
with a version of: a page in front of an API, the API in front of a
database and a cache, and something off to the side that needs nothing.
The cases worth pinning are the ones a plausible implementation gets
subtly wrong -- a chain that has to be followed more than one hop, a
diamond where two paths reach the same upstream, and a cycle, which must
be reported rather than looped over forever.
"""
from alerts.dependencies import (
    ARROW,
    DependencyGraph,
    build_graph,
    describe_chain,
    describe_cycle,
    find_cycle,
    graph_of,
    normalise_names,
)


def _estate() -> DependencyGraph:
    return build_graph(
        [
            ("web", ["api"]),
            ("api", ["db", "cache"]),
            ("checkout", ["api", "db"]),
            ("db", []),
            ("cache", []),
            ("marketing-site", []),
        ]
    )


class _Entry:
    """Stands in for a MonitorConfig or a Monitor row, which is all `graph_of` wants."""

    def __init__(self, name, depends_on=None):
        self.name = name
        self.depends_on = depends_on


def test_declared_dependencies_are_stripped_deduplicated_and_ordered():
    assert normalise_names([" db ", "cache", "db", ""]) == ("db", "cache")


def test_dependency_names_are_not_case_folded_the_way_tags_are():
    # A tag is a label shared by several monitors, so folding is a
    # convenience. A dependency names one specific row, and folding would
    # let `DB` resolve to a monitor called `db` that every other lookup in
    # the project treats as a different thing.
    assert normalise_names(["DB", "db"]) == ("DB", "db")


def test_direct_dependencies_keep_declaration_order():
    assert _estate().direct("api") == ("db", "cache")


def test_a_monitor_that_declares_nothing_has_no_dependencies():
    assert _estate().direct("marketing-site") == ()
    assert _estate().upstream("marketing-site") == []


def test_a_monitor_the_graph_has_never_heard_of_answers_empty():
    # Most monitors on most estates declare nothing, so the graph is built
    # from whatever did -- every lookup has to answer for a name that is
    # simply absent rather than raise on it.
    assert _estate().direct("nothing-like-this") == ()
    assert _estate().upstream("nothing-like-this") == []


def test_upstream_follows_the_chain_nearest_first():
    assert _estate().upstream("web") == ["api", "db", "cache"]


def test_a_diamond_reports_a_shared_upstream_once():
    # checkout needs the API and the database directly, and the API needs
    # the database too. The database is one dependency, not two.
    assert _estate().upstream("checkout") == ["api", "db", "cache"]


def test_dependents_are_the_monitors_naming_this_one_directly():
    assert _estate().dependents("db") == ["api", "checkout"]
    assert _estate().dependents("web") == []


def test_downstream_is_the_whole_blast_radius():
    # What an operator paged about the database wants: every service whose
    # own pages are about to be held back because of it.
    assert _estate().downstream("db") == ["api", "checkout", "web"]


def test_blocking_names_only_the_upstreams_that_are_down():
    graph = _estate()
    assert graph.blocking("web", {"db"}) == ["db"]
    assert graph.blocking("web", {"api", "db"}) == ["api", "db"]
    assert graph.blocking("web", set()) == []
    assert graph.blocking("marketing-site", {"db"}) == []


def test_a_monitor_is_blocked_when_anything_it_needs_is_down():
    graph = _estate()
    assert graph.is_blocked("web", {"cache"})
    assert not graph.is_blocked("web", {"marketing-site"})


def test_depth_counts_hops_to_the_furthest_thing_needed():
    graph = _estate()
    assert graph.depth("marketing-site") == 0
    assert graph.depth("api") == 1
    assert graph.depth("web") == 2


def test_order_puts_upstreams_before_the_monitors_that_need_them():
    order = _estate().order()
    assert order.index("db") < order.index("api") < order.index("web")
    assert order.index("cache") < order.index("api")


def test_order_is_stable_rather_than_dependent_on_insertion():
    forwards = build_graph([("web", ["api"]), ("api", ["db"]), ("db", [])])
    backwards = build_graph([("db", []), ("api", ["db"]), ("web", ["api"])])
    assert forwards.order() == backwards.order() == ["db", "api", "web"]


def test_declared_lists_only_the_monitors_that_depend_on_something():
    assert _estate().declared == ["api", "checkout", "web"]


def test_names_covers_upstreams_that_declare_nothing_themselves():
    graph = build_graph([("web", ["api"])])
    assert graph.names == ["api", "web"]


def test_a_graph_where_nothing_depends_on_anything_is_falsey():
    assert not build_graph([("web", []), ("api", [])])
    assert build_graph([("web", ["api"])])


def test_unknown_references_name_the_dependent_and_what_it_wanted():
    graph = build_graph([("web", ["api", "typo"]), ("api", ["db"])])
    assert graph.unknown_references({"web", "api", "db"}) == {"web": ["typo"]}


def test_a_monitor_naming_itself_is_found():
    assert build_graph([("web", ["web"])]).self_references() == ["web"]
    assert build_graph([("web", ["api"])]).self_references() == []


def test_a_cycle_is_reported_as_the_path_around_it():
    assert find_cycle({"a": ["b"], "b": ["c"], "c": ["a"]}) == ["a", "b", "c", "a"]


def test_a_cycle_is_found_from_a_start_that_is_not_part_of_it():
    # `entry` is walked first and reaches the loop without being in it, so
    # the reported path must be the loop itself rather than the walk that
    # got there.
    cycle = find_cycle({"entry": ["a"], "a": ["b"], "b": ["a"]})
    assert cycle == ["a", "b", "a"]


def test_a_self_dependency_is_a_cycle():
    assert find_cycle({"a": ["a"]}) == ["a", "a"]


def test_an_acyclic_graph_has_no_cycle():
    assert find_cycle({"web": ["api"], "api": ["db"], "db": []}) is None
    assert _estate().cycle() is None


def test_a_diamond_is_not_a_cycle():
    # Two paths reaching the same upstream is the shape a naive
    # "seen it before" check reports as a loop.
    assert find_cycle({"web": ["a", "b"], "a": ["db"], "b": ["db"], "db": []}) is None


def test_traversals_terminate_on_a_graph_that_contains_a_cycle():
    # Cycles are refused when a config is loaded, but a graph built out of
    # rows written by an older version has never been through that check,
    # and a monitoring tool spinning forever is the worse failure.
    graph = build_graph([("a", ["b"]), ("b", ["a"])])
    assert graph.upstream("a") == ["b"]
    assert graph.downstream("a") == ["b"]
    assert graph.order() == ["a", "b"]


def test_graph_of_reads_name_and_depends_on_off_anything_carrying_them():
    graph = graph_of([_Entry("web", ["api"]), _Entry("api", None)])
    assert graph.direct("web") == ("api",)
    assert graph.direct("api") == ()


def test_a_cycle_describes_itself_as_a_chain():
    assert describe_cycle(["a", "b", "a"]) == f"a{ARROW}b{ARROW}a"


def test_an_empty_chain_renders_as_a_dash():
    assert describe_chain([]) == "-"
    assert describe_chain(["db", "cache"]) == f"db{ARROW}cache"
