"""A readable account of what a project is and how to test it.

The scan produces an inventory: forty surfaces, each with an id and a source
location. That is the right thing for a machine to rank and a poor thing for a
person to read. Nobody opens a testing tool wanting a list of call sites; they
want to know what their product does and which parts are worth protecting.

So this is the inventory turned back into product language. Three groupings,
because they are the three ways these applications actually break:

**Pages** — what a user can navigate to.
**APIs** — what the application asks its backend for.
**Transactions** — a page together with the state-changing calls it can
trigger. This is the interesting one: a single endpoint is rarely a feature,
but "create a job and attach an assignment" is, and it is where regressions
that matter tend to live.

Derived deterministically. A model may improve the wording, but the structure
is read out of the code.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from testtrout.domain.surface import Criticality


class PageSummary(BaseModel):
    """One screen a user can reach."""

    model_config = ConfigDict(extra="forbid")

    path: str
    name: str = Field(description="Component or inferred name.")
    criticality: Criticality = Criticality.MEDIUM
    reads: int = 0
    writes: int = 0
    behind_login: bool = False
    surface_ids: list[str] = Field(default_factory=list)
    covered: bool = Field(default=False, description="An accepted test asserts on it.")
    how_to_test: str = ""


class ApiSummary(BaseModel):
    """One endpoint the application calls."""

    model_config = ConfigDict(extra="forbid")

    path: str
    methods: list[str] = Field(default_factory=list)
    criticality: Criticality = Criticality.MEDIUM
    used_by: list[str] = Field(default_factory=list, description="Pages that call it.")
    changes_data: bool = False
    surface_ids: list[str] = Field(default_factory=list)
    covered: bool = Field(default=False, description="An accepted test asserts on it.")
    how_to_test: str = ""


class TransactionSummary(BaseModel):
    """A page together with the state it can change.

    The unit a person recognises as a feature. A single endpoint is rarely
    worth naming; "create a job and attach an assignment" is, and it is where
    the regressions that hurt tend to be.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    page: str
    steps: list[str] = Field(default_factory=list)
    criticality: Criticality = Criticality.HIGH
    surface_ids: list[str] = Field(default_factory=list)
    covered: bool = Field(default=False, description="An accepted test asserts on it.")
    how_to_test: str = ""


class CoverageEstimate(BaseModel):
    """How much of the product has a test, by the groupings a person thinks in."""

    model_config = ConfigDict(extra="forbid")

    pages_total: int = 0
    pages_covered: int = 0
    apis_total: int = 0
    apis_covered: int = 0
    transactions_total: int = 0
    transactions_covered: int = 0

    def _percent(self, covered: int, total: int) -> int:
        return round(100 * covered / total) if total else 0

    @property
    def pages_percent(self) -> int:
        """Share of pages with a test."""
        return self._percent(self.pages_covered, self.pages_total)

    @property
    def apis_percent(self) -> int:
        """Share of endpoints with a test."""
        return self._percent(self.apis_covered, self.apis_total)

    @property
    def transactions_percent(self) -> int:
        """Share of transactions with a test. The number that matters most."""
        return self._percent(self.transactions_covered, self.transactions_total)

    @property
    def overall_percent(self) -> int:
        """Everything, weighted equally by count."""
        return self._percent(
            self.pages_covered + self.apis_covered + self.transactions_covered,
            self.pages_total + self.apis_total + self.transactions_total,
        )


class ProjectOverview(BaseModel):
    """What this project is, and what testing it currently looks like."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    summary: str = Field(default="", description="One or two sentences, plain language.")
    stack: str = ""
    pages: list[PageSummary] = Field(default_factory=list)
    apis: list[ApiSummary] = Field(default_factory=list)
    transactions: list[TransactionSummary] = Field(default_factory=list)
    coverage: CoverageEstimate = Field(default_factory=CoverageEstimate)

    @property
    def total_surfaces(self) -> int:
        """Everything worth testing."""
        return len(self.pages) + len(self.apis) + len(self.transactions)


class ScanDelta(BaseModel):
    """What changed since the last scan, measured against the existing suite.

    The answer to "I scanned again — now what?". Without it a rescan produces
    the same forty items and no sense of progress.
    """

    model_config = ConfigDict(extra="forbid")

    new_areas: list[str] = Field(default_factory=list)
    newly_covered: list[str] = Field(default_factory=list)
    still_missing: list[str] = Field(default_factory=list)
    gone: list[str] = Field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        """Whether anything moved since last time."""
        return bool(self.new_areas or self.newly_covered or self.gone)
