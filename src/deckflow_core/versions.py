"""A small version parser and range checker.

Deliberately not a full PEP 440 / semver implementation — core only needs to
answer one question: does the version found in the environment fall inside the
`compatible` range declared in the pin matrix?  Anything ambiguous is treated
as incompatible, so an unknown version can never silently satisfy a pin.
"""

from __future__ import annotations

import re

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?(?:[-+.]?([0-9A-Za-z.\-]+))?")
_TERM_RE = re.compile(r"^(>=|<=|==|>|<)\s*(.+)$")


def parse(text: str | None) -> tuple[tuple[int, int, int], tuple[str, ...]] | None:
    """Extract the first version-looking token from arbitrary CLI output."""
    if not text:
        return None
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    major, minor, patch, pre = match.groups()
    release = (int(major), int(minor), int(patch or 0))
    prerelease = tuple(pre.split(".")) if pre else ()
    return release, prerelease


def _compare(left: tuple[tuple[int, int, int], tuple[str, ...]],
             right: tuple[tuple[int, int, int], tuple[str, ...]]) -> int:
    if left[0] != right[0]:
        return -1 if left[0] < right[0] else 1
    # A release outranks any prerelease of the same numbers (1.0.0 > 1.0.0-rc.1).
    if not left[1] and right[1]:
        return 1
    if left[1] and not right[1]:
        return -1
    if left[1] == right[1]:
        return 0
    return -1 if left[1] < right[1] else 1


def satisfies(version: str | None, spec: str | None) -> bool:
    """True when `version` satisfies every whitespace-separated term of `spec`.

    An unparseable version or term returns False rather than passing: core
    prefers a managed install over trusting a version it cannot read.
    """
    if not spec:
        return True
    parsed = parse(version)
    if parsed is None:
        return False
    for term in spec.split():
        match = _TERM_RE.match(term.strip())
        if match is None:
            return False
        operator, bound_text = match.groups()
        bound = parse(bound_text)
        if bound is None:
            return False
        order = _compare(parsed, bound)
        if operator == ">=" and order < 0:
            return False
        if operator == ">" and order <= 0:
            return False
        if operator == "<=" and order > 0:
            return False
        if operator == "<" and order >= 0:
            return False
        if operator == "==" and order != 0:
            return False
    return True


def is_newer(candidate: str | None, current: str | None) -> bool:
    """True when `candidate` is strictly newer than `current`.

    Either side being unparseable returns False: `deckflow update` must not
    install something because a version string it could not read *might* be
    newer.
    """
    left, right = parse(candidate), parse(current)
    if left is None or right is None:
        return False
    return _compare(left, right) > 0


def normalize(version: str | None) -> str | None:
    """Render a parsed version back as a plain dotted string, or None."""
    parsed = parse(version)
    if parsed is None:
        return None
    release, prerelease = parsed
    text = ".".join(str(part) for part in release)
    return f"{text}-{'.'.join(prerelease)}" if prerelease else text
