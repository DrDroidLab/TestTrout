"""Where a claim came from.

Every assertion this tool generates carries one of these. It is what makes a
failure report answerable at two in the morning: whoever reads it needs to know
whether the expectation came from a real observation or from something the tool
worked out on its own.

Lives in its own module because it outlived the intent capture it was written
for. A baseline suite has exactly one strong provenance — ``OBSERVED`` — and
the weaker ones remain only to describe assertions derived from code.
"""

from __future__ import annotations

from enum import StrEnum


class Provenance(StrEnum):
    """Where a claim came from. Ordered from strongest to weakest."""

    STATED = "stated"
    """A person said it, in their own words."""
    CONFIRMED = "confirmed"
    """The tool drafted it and a person accepted it."""
    OBSERVED = "observed"
    """Seen happening against a running deployment. The basis of a baseline."""
    DERIVED = "derived"
    """Follows deterministically from schema, policy, or code."""
    INFERRED = "inferred"
    """A guess. Never sufficient on its own to block anything."""

    @property
    def rank(self) -> int:
        """Sort key, strongest first."""
        return ["stated", "confirmed", "observed", "derived", "inferred"].index(self.value)

    @property
    def is_evidence(self) -> bool:
        """Whether this provenance can justify a blocking assertion."""
        return self is not Provenance.INFERRED


__all__ = ["Provenance"]
