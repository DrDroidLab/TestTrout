"""What the tool can test, and what each blocked item is waiting for.

The predecessor to this module ranked every missing test by criticality and
marked each one ready or blocked against a list of reasons. It was accurate and
almost useless: forty rows, most of them blocked on something abstract, ordered
by a score nobody had asked for.

What a person actually needs to see is two lists. **This is testable now.**
**This is not, and here is the one concrete thing it needs.** Nothing here is
ranked by importance, because importance is product knowledge and the tool does
not have it — the order is simply what can be done first.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CandidateKind(StrEnum):
    """The two things this tool knows how to test.

    Deliberately short. A data-layer assertion needs database credentials most
    teams will not hand over, and a unit test is not this tool's business.
    """

    PAGE = "page"
    """A screen loads and shows what it showed at baseline."""
    ENDPOINT = "endpoint"
    """An HTTP endpoint answers the way it answered at baseline."""

    @property
    def label(self) -> str:
        """Words, not identifiers."""
        return {"page": "browser", "endpoint": "API"}[self.value]


class Candidate(BaseModel):
    """One thing that could become a test.

    ``observed`` is the whole design in one field. A candidate the deployment
    has actually answered can be turned into a baseline assertion immediately,
    because the baseline *is* what was observed. One that has not been reached
    cannot, and says which fact would let it be.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: CandidateKind
    title: str
    target: str = Field(description="The path or route this exercises.")
    observed: bool = Field(
        default=False, description="Whether the deployment has actually answered here."
    )
    detail: str = Field(default="", description="What was observed, or why it was not.")
    needs: list[str] = Field(default_factory=list, description="Fact ids that would unlock this.")
    surfaces: list[str] = Field(default_factory=list)
    behind_login: bool = False

    @property
    def ready(self) -> bool:
        """Whether a test can be written and proven right now."""
        return self.observed and not self.needs


class TestPlan(BaseModel):
    """Everything the tool could test, split by whether it can yet."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    candidates: list[Candidate] = Field(default_factory=list)

    @property
    def ready(self) -> list[Candidate]:
        """What can be turned into a proven test right now."""
        return [c for c in self.candidates if c.ready]

    @property
    def waiting(self) -> list[Candidate]:
        """What needs a fact first, most-shared blocker first."""
        return sorted(
            (c for c in self.candidates if not c.ready),
            key=lambda c: (len(c.needs), c.title),
        )

    def counts(self) -> dict[str, int]:
        """A one-line summary for a chat message."""
        return {
            "ready": len(self.ready),
            "waiting": len(self.waiting),
            "pages": sum(1 for c in self.candidates if c.kind is CandidateKind.PAGE),
            "endpoints": sum(1 for c in self.candidates if c.kind is CandidateKind.ENDPOINT),
        }


__all__ = ["Candidate", "CandidateKind", "TestPlan"]
