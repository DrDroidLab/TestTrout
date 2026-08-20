"""Stable identifier allocation for surfaces.

Surface ids appear in approvals, coverage records, quarantine decisions, and
generated test filenames, so they must survive a re-scan of the same code.
That rules out anything derived from file position: adding an import at the top
of a file would otherwise renumber every surface below it.

Ids are therefore built from durable properties (table plus operation, route
path, policy name) and only fall back to a disambiguating suffix when two
surfaces are genuinely indistinguishable by those properties.
"""

from __future__ import annotations

import re
from collections import Counter

_UNSAFE = re.compile(r"[^a-zA-Z0-9_.:/@-]+")


def slug(value: str) -> str:
    """Normalise a fragment for use inside an id."""
    return _UNSAFE.sub("-", value.strip()).strip("-") or "unknown"


class IdAllocator:
    """Hands out unique ids, appending an ordinal only on genuine collision."""

    def __init__(self) -> None:
        self._counts: Counter[str] = Counter()

    def allocate(self, base: str, *qualifiers: str | None) -> str:
        """Build an id from ``base``, adding qualifiers only as needed.

        Qualifiers are tried in order, so the most meaningful disambiguator
        (usually the enclosing component) is preferred over a bare number.
        """
        candidate = slug(base)
        if self._counts[candidate] == 0:
            self._counts[candidate] += 1
            return candidate

        for qualifier in qualifiers:
            if not qualifier:
                continue
            qualified = f"{candidate}@{slug(qualifier)}"
            if self._counts[qualified] == 0:
                self._counts[qualified] += 1
                return qualified

        self._counts[candidate] += 1
        return f"{candidate}#{self._counts[candidate]}"
