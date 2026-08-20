"""Gaps: surfaces with nothing protecting them, ranked by what to do first.

The gap map is the tool's first genuinely opinionated output. A scan says what
exists; a gap map says *where to start*, which is the question a developer with
no tests and limited time actually has.

Ranking is deterministic and every rank carries its reasons. That matters more
here than anywhere else in the tool: a ranked list is a claim about priority,
and a claim about priority that cannot be interrogated is just an assertion of
taste.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from testtrout.domain.intent import Provenance
from testtrout.domain.surface import Criticality


class TestKind(StrEnum):
    """The kind of test that would close a gap.

    Kinds differ sharply in cost and reliability, so this drives both effort
    estimates and which emitter Phase 4 uses.
    """

    AUTHORIZATION = "authorization"
    """One user must not reach another's data. Generated from an RLS policy,
    which states the expectation directly — cheap to write and high value."""
    BROWSER_JOURNEY = "browser_journey"
    """A user-visible flow driven through the interface. The most faithful and
    the most expensive."""
    DATA_OPERATION = "data_operation"
    """A specific read or write asserted at the data layer."""
    ENDPOINT = "endpoint"
    """A first-party HTTP endpoint, Route Handler, or Server Action."""

    @property
    def typical_runtime_seconds(self) -> int:
        """Rough cost, used to build a suite that fits a time budget."""
        return {
            "authorization": 3,
            "data_operation": 4,
            "endpoint": 3,
            "browser_journey": 20,
        }[self.value]


class Blocker(BaseModel):
    """Something missing that prevents this test from being written yet.

    Reported rather than worked around. A gap that cannot be turned into a
    working test is worse than no gap at all, because it produces a scenario
    that fails for reasons unrelated to the product.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class Gap(BaseModel):
    """One unprotected behaviour, and what it would take to protect it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: TestKind
    title: str = Field(description="What the test would assert, in one line.")
    surfaces: list[str] = Field(default_factory=list, description="Surface ids this covers.")
    criticality: Criticality = Criticality.MEDIUM
    score: float = Field(default=0.0, description="Ranking score. Higher is more urgent.")
    reasons: list[str] = Field(
        default_factory=list, description="Why it ranks here. Every input, in plain language."
    )
    provenance: Provenance = Provenance.DERIVED
    journey_id: str | None = None
    blockers: list[Blocker] = Field(default_factory=list)
    estimated_seconds: int = 0

    @property
    def ready(self) -> bool:
        """Whether this could be authored right now."""
        return not self.blockers


class Coverage(BaseModel):
    """How much of the product currently has a test protecting it."""

    model_config = ConfigDict(extra="forbid")

    total_surfaces: int = 0
    covered_surfaces: int = 0
    critical_total: int = 0
    critical_covered: int = 0
    policies_total: int = 0
    policies_covered: int = 0

    @property
    def percent(self) -> int:
        """Overall surface coverage, rounded."""
        if not self.total_surfaces:
            return 0
        return round(100 * self.covered_surfaces / self.total_surfaces)

    @property
    def critical_percent(self) -> int:
        """Coverage restricted to critical surfaces — the number that matters."""
        if not self.critical_total:
            return 0
        return round(100 * self.critical_covered / self.critical_total)


class GapMap(BaseModel):
    """The complete output of ``trout gaps``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    coverage: Coverage = Field(default_factory=Coverage)
    gaps: list[Gap] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list, description="Caveats about the analysis itself.")

    def ranked(self, limit: int | None = None, ready_only: bool = False) -> list[Gap]:
        """Gaps in priority order."""
        items = [g for g in self.gaps if g.ready] if ready_only else list(self.gaps)
        items.sort(key=lambda g: (-g.score, g.criticality.rank, g.id))
        return items[:limit] if limit else items

    def budget(self, seconds: int) -> list[Gap]:
        """The most valuable gaps that fit in a time budget.

        Greedy by score rather than by value density, on purpose: a developer
        asking for a ten-minute suite wants the most important tests in it, not
        the largest number of tests.
        """
        chosen: list[Gap] = []
        spent = 0
        for gap in self.ranked(ready_only=True):
            if spent + gap.estimated_seconds > seconds:
                continue
            chosen.append(gap)
            spent += gap.estimated_seconds
        return chosen
