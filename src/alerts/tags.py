"""The one definition of what a monitor tag is, and how two are compared.

Tags arrive from three places that don't know about each other -- the YAML
config, a `--tag` argument, and a `?tag=` query parameter -- and are compared
against a list stored in the database. Every one of those comparisons has to
agree, so the folding rule lives here rather than being spelled out again at
each boundary.

It sits in its own module because of who needs it. The config validator
normalises on the way in, the `Monitor` model compares on the way out, and
the dashboard normalises what a URL brought it; a rule owned by any one of
those would have the other two importing across a layer they otherwise
don't.
"""
from __future__ import annotations

from typing import Iterable, List

# Characters a tag may not contain. A comma is out because tags are rendered
# as a comma-joined list and read back as one, so a tag containing one would
# split into two nobody declared. Whitespace is out because a tag is picked
# out of a list by eye, where " prod" and "prod" are indistinguishable, and
# different everywhere else.
FORBIDDEN_CHARACTERS = (",", " ", "\t", "\n")


def normalise(value: str) -> str:
    """Fold a tag to the one spelling everything else compares against.

    Lowercased and stripped, so `Prod`, `prod`, and ` prod ` are the same
    tag. Case-sensitive tags would mean a monitor tagged `Prod` silently
    missing from a `--tag prod` report, which is the failure mode where an
    operator concludes a service is fine having never seen it.
    """
    return value.strip().lower()


def forbidden_characters(tag: str) -> List[str]:
    """Which forbidden characters a tag contains, for an error worth reading."""
    return [character for character in FORBIDDEN_CHARACTERS if character in tag]


def normalise_all(values: Iterable[str]) -> List[str]:
    """Fold a sequence of tags, dropping repeats and keeping declaration order.

    Order is preserved rather than sorted because it is how the operator
    grouped them in their head, and the status table shows it back to them.
    """
    folded: List[str] = []
    for value in values:
        tag = normalise(value)
        if tag not in folded:
            folded.append(tag)
    return folded
