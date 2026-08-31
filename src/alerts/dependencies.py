"""Which monitors a monitor needs in order to be healthy, and what that means.

A gateway sitting in front of a database is not independently broken when
the database is down. It fails its checks, it opens an incident, and it
pages somebody -- about the database. On an estate of any size that is how
one outage turns into fifteen pages, not one of which names the thing that
actually broke, and how the page that does name it arrives fourteenth.

`depends_on` is the operator saying which monitors a service needs. When
one of them is down, alerts for the dependent are held back the same way a
maintenance window holds them back: checks keep running, results keep being
recorded, the incident is still opened -- only the page is suppressed, and
only for as long as something upstream is down to explain it.

This module is the graph and the questions asked of it, and nothing else.
It takes names and lists of names, so the same code answers for a config
that has never been synced (`alerts check`, config validation) and for the
rows in a live database (the scheduler, the dashboard, the reports).
Nothing here loads a config, opens a session, or knows what a monitor state
is: the caller decides which monitors count as down and passes their names
in.

Dependency names are compared exactly, with no folding of the kind tags
get. A tag is a label several monitors share, so `Prod` and `prod` being
the same thing is a convenience; a dependency names one specific monitor,
and folding would let `depends_on: [DB]` quietly resolve to a monitor
called `db` that every other lookup in this project treats as a different
row. An unknown name is refused at load time instead, which is the loud
version of the same catch.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Collection, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# How a chain of dependencies is written in an error message or a report.
ARROW = " -> "


def normalise_names(names: Iterable[str]) -> Tuple[str, ...]:
    """Fold a declared dependency list to the one that gets stored.

    Surrounding whitespace goes, blanks are dropped, and repeats collapse
    while declaration order is kept. Naming the same upstream twice is a
    typo rather than a stronger dependency, and the order is the one the
    operator wrote the services down in, which is the order a report reads
    best in.
    """
    folded: List[str] = []
    for raw in names:
        name = raw.strip()
        if name and name not in folded:
            folded.append(name)
    return tuple(folded)


@dataclass(frozen=True)
class DependencyGraph:
    """Every monitor's declared upstreams, and the questions asked of them.

    `edges` maps a monitor name to the monitors it depends on directly. A
    monitor with no dependencies may be absent entirely rather than mapped
    to an empty tuple -- the graph is built from whatever declared
    something, and every lookup below answers for a name it has never heard
    of, since most monitors on most estates declare nothing.

    Every traversal carries its own seen-set, so a graph that contains a
    cycle answers rather than hangs. Cycles are refused when a config is
    loaded, but a graph built out of database rows written by an older
    version has never been through that check, and a monitoring tool
    spinning forever is a worse failure than one reporting an odd-looking
    chain.
    """

    edges: Mapping[str, Tuple[str, ...]]

    @property
    def names(self) -> List[str]:
        """Every monitor the graph mentions, as a dependent or an upstream."""
        found = set(self.edges)
        for upstreams in self.edges.values():
            found.update(upstreams)
        return sorted(found)

    @property
    def declared(self) -> List[str]:
        """The monitors that declare at least one dependency, sorted."""
        return sorted(name for name, upstreams in self.edges.items() if upstreams)

    def __bool__(self) -> bool:
        """Whether anything declares a dependency at all.

        The common estate declares none, and every surface that reports
        dependencies wants to say nothing at all rather than render an
        empty section, so the emptiness check is worth having on the graph
        itself.
        """
        return any(self.edges.values())

    def direct(self, name: str) -> Tuple[str, ...]:
        """The monitors `name` depends on directly, in declaration order."""
        return tuple(self.edges.get(name, ()))

    def upstream(self, name: str) -> List[str]:
        """Everything `name` depends on, directly or through another monitor.

        Breadth-first, so the nearest dependencies come first: when a report
        has room to name one upstream, the one the operator declared is a
        better answer than something three hops away they may not know is
        involved.

        `name` itself never appears, even when a cycle leads back to it. A
        monitor is not its own dependency in any useful sense, and a report
        saying "api is blocked by api" describes the graph rather than the
        outage.
        """
        seen = {name}
        found: List[str] = []
        queue = deque(self.direct(name))
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            found.append(current)
            queue.extend(self.direct(current))
        return found

    def dependents(self, name: str) -> List[str]:
        """The monitors that name this one as a direct dependency, sorted."""
        return sorted(
            dependent
            for dependent, upstreams in self.edges.items()
            if name in upstreams
        )

    def downstream(self, name: str) -> List[str]:
        """Everything that depends on `name`, directly or transitively.

        The blast radius of one monitor being down: what an operator wants
        after being paged about a database is the list of services whose
        own pages are about to be held back because of it.
        """
        seen = {name}
        found: List[str] = []
        queue = deque(self.dependents(name))
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            found.append(current)
            queue.extend(self.dependents(current))
        return found

    def blocking(self, name: str, down: Collection[str]) -> List[str]:
        """Which of `name`'s upstreams are in the given down set, nearest first.

        The caller decides what "down" means and passes the names in. That
        keeps this module free of monitor states, and it is also the honest
        shape: the scheduler counts a monitor as down when its state says
        so, while a paused monitor is left out entirely -- its state is
        frozen at whatever it was when checking stopped, and blocking a live
        service's pages on a reading nobody is updating would suppress them
        forever.
        """
        return [upstream for upstream in self.upstream(name) if upstream in down]

    def is_blocked(self, name: str, down: Collection[str]) -> bool:
        """Whether anything `name` depends on is currently down."""
        return bool(self.blocking(name, down))

    def depth(self, name: str) -> int:
        """How many hops separate `name` from the furthest thing it needs.

        Zero for a monitor that depends on nothing. Used to indent a
        dependency report so the chain reads top-down, which is the only
        rendering of a graph that fits in a terminal column.
        """
        best = 0
        seen = {name}
        queue = deque((upstream, 1) for upstream in self.direct(name))
        while queue:
            current, distance = queue.popleft()
            best = max(best, distance)
            if current in seen:
                continue
            seen.add(current)
            queue.extend((further, distance + 1) for further in self.direct(current))
        return best

    def order(self) -> List[str]:
        """Every monitor, upstreams before the monitors that depend on them.

        Kahn's algorithm with alphabetical tie-breaking, so the order is
        stable across runs rather than dependent on dict insertion: a report
        whose rows move around between two runs that found the same estate
        is a report nobody trusts.

        Anything left over -- which is only possible inside a cycle -- is
        appended in sorted order rather than dropped or raised over. This
        is a rendering order, and a cycle that slipped past config
        validation should still show up on the page that would let someone
        see it.
        """
        remaining: Dict[str, set] = {
            name: set(self.direct(name)) for name in self.names
        }
        ordered: List[str] = []
        while True:
            ready = sorted(
                name for name, pending in remaining.items() if not pending
            )
            if not ready:
                break
            for name in ready:
                ordered.append(name)
                del remaining[name]
            for pending in remaining.values():
                pending.difference_update(ready)
        ordered.extend(sorted(remaining))
        return ordered

    def unknown_references(self, known: Collection[str]) -> Dict[str, List[str]]:
        """Dependencies naming monitors that don't exist, by dependent.

        A typo here fails in the direction that costs the most: the
        dependency looks configured, nothing is ever found to be down
        upstream, and the suppression the operator set up to keep one
        outage from paging fifteen services quietly never happens.
        """
        problems: Dict[str, List[str]] = {}
        for name, upstreams in self.edges.items():
            missing = [upstream for upstream in upstreams if upstream not in known]
            if missing:
                problems[name] = missing
        return problems

    def self_references(self) -> List[str]:
        """Monitors that declare themselves as their own dependency."""
        return sorted(name for name, upstreams in self.edges.items() if name in upstreams)

    def cycle(self) -> Optional[List[str]]:
        """One cycle in the graph, as the path around it, or None."""
        return find_cycle(self.edges)


def build_graph(entries: Iterable[Tuple[str, Sequence[str]]]) -> DependencyGraph:
    """Build a graph from (monitor name, declared dependencies) pairs."""
    return DependencyGraph(
        edges={name: normalise_names(upstreams) for name, upstreams in entries}
    )


def graph_of(monitors: Iterable[Any]) -> DependencyGraph:
    """Build a graph from anything carrying `name` and `depends_on`.

    Deliberately duck-typed rather than annotated against one class. A
    `MonitorConfig` and a `Monitor` row carry the same two attributes, and
    the whole point of this module is that the config-only commands and the
    database-backed ones ask their questions of the same graph -- typing it
    against either one would mean the other converting first, for nothing.
    """
    return build_graph((monitor.name, monitor.depends_on or ()) for monitor in monitors)


def find_cycle(edges: Mapping[str, Sequence[str]]) -> Optional[List[str]]:
    """One cycle in a dependency graph, as the path around it, or None.

    Returns the path closed on itself -- `["api", "db", "api"]` -- because
    the message an operator has to act on is the loop, not the fact that
    there is one. Iterative rather than recursive: the depth is bounded by
    the number of monitors, but a config file is operator-controlled input
    and a stack overflow is a poor way to report a typo.

    The first cycle found is the one reported. A config with two of them
    gets the second on the next run, which is the same way every other
    validation error in this project behaves.
    """
    visited: set = set()
    on_path: set = set()
    path: List[str] = []
    # Sorted, so a config with more than one cycle reports the same one on
    # every run rather than whichever the dict happened to yield first.
    for start in sorted(edges):
        if start in visited:
            continue
        stack: List[Tuple[str, int]] = [(start, 0)]
        while stack:
            name, index = stack[-1]
            if index == 0:
                if name in on_path:
                    # Close the loop at the point it was first entered, so
                    # the path is the cycle itself and not the walk that
                    # reached it.
                    return path[path.index(name):] + [name]
                if name in visited:
                    stack.pop()
                    continue
                visited.add(name)
                on_path.add(name)
                path.append(name)
            upstreams = tuple(edges.get(name, ()))
            if index < len(upstreams):
                stack[-1] = (name, index + 1)
                stack.append((upstreams[index], 0))
                continue
            stack.pop()
            on_path.discard(name)
            path.pop()
    return None


def describe_cycle(path: Sequence[str]) -> str:
    """A cycle as the one-line chain an error message quotes."""
    return ARROW.join(path)


def describe_chain(names: Sequence[str]) -> str:
    """A dependency chain as one line, or a dash when there is nothing to say."""
    return ARROW.join(names) if names else "-"
