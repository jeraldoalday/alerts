"""`${VAR}` expansion for config values that carry a secret inline."""
import pytest

from alerts.envsubst import MissingEnvVar, expand, expand_mapping, missing, placeholders


def test_a_string_with_no_placeholders_is_returned_unchanged():
    assert expand("application/json") == "application/json"


def test_a_placeholder_is_replaced_with_the_variable(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "s3cret")

    assert expand("Bearer ${API_TOKEN}") == "Bearer s3cret"


def test_several_placeholders_are_all_replaced(monkeypatch):
    monkeypatch.setenv("USER", "svc")
    monkeypatch.setenv("REALM", "prod")

    assert expand("${USER}@${REALM}") == "svc@prod"


def test_the_same_placeholder_twice_is_replaced_twice(monkeypatch):
    monkeypatch.setenv("X", "ab")

    assert expand("${X}-${X}") == "ab-ab"


def test_a_bare_dollar_is_left_alone():
    """Tokens contain '$' often enough that it can't be treated as syntax."""
    assert expand("p$ssw0rd$") == "p$ssw0rd$"


def test_the_unbraced_shell_form_is_not_expanded(monkeypatch):
    """$NAME has no end delimiter, so "$TOKENabc" would be ambiguous."""
    monkeypatch.setenv("TOKEN", "secret")

    assert expand("$TOKEN") == "$TOKEN"


def test_an_unset_variable_raises_naming_it(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)

    with pytest.raises(MissingEnvVar) as excinfo:
        expand("Bearer ${NOPE}")

    assert excinfo.value.names == ["NOPE"]
    assert "NOPE" in str(excinfo.value)


def test_a_variable_set_to_an_empty_string_counts_as_unset(monkeypatch):
    """An empty token is sent, rejected, and recorded as the service being down."""
    monkeypatch.setenv("EMPTY", "")

    with pytest.raises(MissingEnvVar):
        expand("Bearer ${EMPTY}")


def test_the_expanded_value_is_never_a_partial_substitution(monkeypatch):
    """All or nothing: no header goes out with one half resolved."""
    monkeypatch.setenv("SET", "ok")
    monkeypatch.delenv("UNSET", raising=False)

    with pytest.raises(MissingEnvVar):
        expand("${SET}/${UNSET}")


def test_placeholders_lists_names_in_order_without_repeats():
    assert placeholders("${B} ${A} ${B}") == ["B", "A"]


def test_placeholders_of_a_plain_string_is_empty():
    assert placeholders("text/plain") == []


def test_missing_reports_only_the_unset_names(monkeypatch):
    monkeypatch.setenv("HERE", "1")
    monkeypatch.delenv("GONE", raising=False)

    assert missing("${HERE} ${GONE}") == ["GONE"]


def test_missing_is_empty_when_everything_resolves(monkeypatch):
    monkeypatch.setenv("HERE", "1")

    assert missing("${HERE}") == []


def test_expand_mapping_expands_every_value(monkeypatch):
    monkeypatch.setenv("TOKEN", "abc")

    expanded = expand_mapping({"Authorization": "Bearer ${TOKEN}", "Accept": "application/json"})

    assert expanded == {"Authorization": "Bearer abc", "Accept": "application/json"}


def test_expand_mapping_leaves_the_keys_alone(monkeypatch):
    """Header names are not secrets and are not templatised."""
    monkeypatch.setenv("TOKEN", "abc")

    expanded = expand_mapping({"X-${TOKEN}": "v"})

    assert list(expanded) == ["X-${TOKEN}"]


def test_expand_mapping_reports_every_missing_name_at_once(monkeypatch):
    """So three missing variables aren't discovered one restart at a time."""
    monkeypatch.delenv("ONE", raising=False)
    monkeypatch.delenv("TWO", raising=False)

    with pytest.raises(MissingEnvVar) as excinfo:
        expand_mapping({"A": "${ONE}", "B": "${TWO}", "C": "literal"})

    assert excinfo.value.names == ["ONE", "TWO"]


def test_expand_mapping_of_an_empty_mapping_is_empty():
    assert expand_mapping({}) == {}
