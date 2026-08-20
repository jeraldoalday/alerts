"""`${VAR}` placeholder expansion for config values that carry secrets.

The config file is meant to be committed. Channels keep secrets out of it
with dedicated `*_env` fields, but that shape doesn't fit a value that is
part text and part secret -- an `Authorization: Bearer <token>` header is one
string with the secret in the middle of it.

So a monitor's header values may contain `${VAR}` placeholders, resolved
against the environment. Everything outside a placeholder is literal, and a
`$` that isn't the start of one is left exactly as written: passwords and
tokens contain `$` often enough that treating a bare one as syntax would
mangle a header nobody meant to templatise.

Expansion happens at check time rather than at load time, so the resolved
secret lives in the process that makes the request and never in the
database. `missing` is the load-time half of the same feature: it reports
which placeholders can't be resolved *now*, so a typo in a variable name is
a config error rather than an outage discovered at 3am.
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Mapping

# ${NAME}, where NAME is the shell-ish set of characters an environment
# variable is allowed to use. The bare $NAME form is deliberately not
# supported: it has no end delimiter, so "$TOKENabc" is ambiguous in a way
# the braced form never is.
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class MissingEnvVar(Exception):
    """Raised when a `${VAR}` placeholder names a variable that isn't set."""

    def __init__(self, names: List[str]):
        self.names = names
        joined = ", ".join(repr(name) for name in names)
        super().__init__(f"environment variable(s) not set: {joined}")


def placeholders(value: str) -> List[str]:
    """Every variable name referenced in a string, in order, without repeats."""
    seen: List[str] = []
    for name in _PLACEHOLDER.findall(value):
        if name not in seen:
            seen.append(name)
    return seen


def missing(value: str) -> List[str]:
    """The referenced variable names that aren't set in the environment.

    An empty string counts as unset, matching `_require_env` in the config
    module: a token set to "" is a misconfiguration that would otherwise
    surface as an authentication failure against the target.
    """
    return [name for name in placeholders(value) if not os.environ.get(name)]


def expand(value: str) -> str:
    """Substitute every `${VAR}` in a string, raising if any is unset.

    Raising rather than substituting an empty string on purpose: a header
    reading `Bearer ` would be sent, rejected by the target, and recorded as
    the service being down. Naming the variable that vanished is the
    difference between a five-minute fix and an incident spent reading
    someone else's access logs.
    """
    unresolved = missing(value)
    if unresolved:
        raise MissingEnvVar(unresolved)
    return _PLACEHOLDER.sub(lambda match: os.environ[match.group(1)], value)


def expand_mapping(values: Mapping[str, str]) -> Dict[str, str]:
    """Expand every value of a mapping, reporting all missing names at once.

    Collecting across the whole mapping matters for a monitor with several
    templated headers: fixing them one restart at a time, learning about the
    next missing variable only after setting the last, is a slow way to
    discover you have three.
    """
    unresolved: List[str] = []
    for value in values.values():
        for name in missing(value):
            if name not in unresolved:
                unresolved.append(name)
    if unresolved:
        raise MissingEnvVar(unresolved)
    return {key: expand(value) for key, value in values.items()}
