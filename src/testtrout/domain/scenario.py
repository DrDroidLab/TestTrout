"""Scenario specifications — the durable form of a test.

A scenario is stored as language-agnostic YAML and *compiled* into runnable
test code. That separation is the most important structural decision in the
authoring layer, and it buys three things:

*Regeneration.* Emitted code can be thrown away and rebuilt. Fixing a selector
strategy improves every test at once instead of requiring a sweep.

*Review.* A developer approving a scenario reads given/when/then in their
product's vocabulary, not a Playwright file. Approving code you have to parse
is how unreviewed tests get merged.

*Retargeting.* The same specification can emit a Playwright test today and
something else later without redoing the analysis that produced it.

Every assertion carries its provenance. An assertion backed by a row-level
security policy is evidence; one a model inferred is a suggestion, and the
distinction survives all the way into the generated file as a comment.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from testtrout.domain.observation import SelectorCandidate
from testtrout.domain.provenance import Provenance
from testtrout.domain.surface import Criticality


class TestKind(StrEnum):
    """What kind of test this is.

    Two of these are what the tool builds a baseline from — a browser journey
    and an endpoint check. The other two remain because the emitters for them
    exist and are useful once a person supplies database access, which most
    will not.
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


class ScenarioStatus(StrEnum):
    """Where a scenario sits in its lifecycle.

    Only ``certified`` scenarios belong in a regression baseline. A scenario
    that has passed once has not demonstrated it is deterministic, and an
    intermittently-failing test in a blocking suite is how teams learn to
    ignore the suite.
    """

    DRAFT = "draft"
    """Proposed, awaiting human review."""
    APPROVED = "approved"
    """A person accepted it. Eligible for code generation."""
    CERTIFIED = "certified"
    """Passed N consecutive runs against a known-good deployment."""
    QUARANTINED = "quarantined"
    """Non-deterministic. Excluded from blocking, flagged for repair."""
    REJECTED = "rejected"
    """A person declined it. Kept so it is not proposed again."""


class Action(StrEnum):
    """One thing a scenario does."""

    SIGN_IN = "sign_in"
    NAVIGATE = "navigate"
    CLICK = "click"
    FILL = "fill"
    WAIT_FOR = "wait_for"
    REQUEST = "request"
    """A direct HTTP call, for endpoint scenarios."""
    QUERY = "query"
    """A direct database call, for authorization scenarios."""


class AssertionKind(StrEnum):
    """What a scenario checks."""

    VISIBLE = "visible"
    TEXT = "text"
    URL = "url"
    STATUS = "status"
    ROW_COUNT = "row_count"
    DENIED = "denied"
    """The backend refused. The core of an authorization test."""
    NO_CONSOLE_ERRORS = "no_console_errors"


class Target(BaseModel):
    """What a step or assertion acts on.

    A selector is stored as a *candidate*, not as a raw string, so the emitter
    decides how to express it. That is what allows the selector strategy to be
    improved centrally rather than baked into hundreds of generated files.
    """

    model_config = ConfigDict(extra="forbid")

    selector: SelectorCandidate | None = None
    url: str | None = None
    table: str | None = None
    method: str | None = None
    description: str = ""


class Step(BaseModel):
    """One action in the ``when`` block."""

    model_config = ConfigDict(extra="forbid")

    action: Action
    target: Target = Field(default_factory=Target)
    value: str | None = Field(default=None, description="Text to type, or a role to sign in as.")
    description: str = Field(default="", description="Plain language, for the developer.")


class Assertion(BaseModel):
    """One check in the ``then`` block.

    ``source`` is not decoration. It is what lets a failing test explain why
    anyone expected the behaviour it just failed to see, and it is what the
    policy engine reads to decide whether a failure may block a merge.
    """

    model_config = ConfigDict(extra="forbid")

    kind: AssertionKind
    target: Target = Field(default_factory=Target)
    expected: str | None = None
    provenance: Provenance = Provenance.INFERRED
    source: str = Field(default="", description="The evidence behind this expectation.")
    description: str = ""

    @property
    def is_evidence(self) -> bool:
        """Whether this assertion may justify blocking a merge."""
        return self.provenance.is_evidence


class Fixture(BaseModel):
    """Data a scenario needs before it can run."""

    model_config = ConfigDict(extra="forbid")

    table: str
    description: str = ""
    owned_by_role: str | None = Field(
        default=None, description="Which test user owns this row, for authorization scenarios."
    )
    columns: dict[str, str] = Field(default_factory=dict)


class Scenario(BaseModel):
    """One test, stored as ``.trout/scenarios/<id>.yaml``.

    Committed and hand-editable. Editing the specification and regenerating is
    the supported way to change a test; editing generated code is not, because
    the next generation overwrites it.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    id: str
    title: str = Field(description="What this asserts, in one line, in the product's words.")
    kind: TestKind
    status: ScenarioStatus = ScenarioStatus.DRAFT
    criticality: Criticality = Criticality.MEDIUM
    provenance: Provenance = Provenance.INFERRED

    gap_id: str | None = None
    surfaces: list[str] = Field(default_factory=list)
    journey_id: str | None = None

    entrypoint: str | None = Field(default=None, description="Which deployment to run against.")
    role: str | None = Field(default=None, description="Test-user role performing the scenario.")
    other_role: str | None = Field(
        default=None, description="The second role, for authorization scenarios."
    )

    given: list[str] = Field(default_factory=list, description="Preconditions, plain language.")
    when: list[Step] = Field(default_factory=list)
    then: list[Assertion] = Field(default_factory=list)

    fixtures: list[Fixture] = Field(default_factory=list)
    substitute: list[str] = Field(
        default_factory=list, description="Third-party vendors to intercept during this scenario."
    )
    open_questions: list[str] = Field(
        default_factory=list,
        description="What the author could not determine. Blocks approval until answered.",
    )

    estimated_seconds: int = 0
    emitted_to: str | None = Field(default=None, description="Path of the generated test file.")

    @property
    def mutating(self) -> bool:
        """Whether running this can change state on the deployment.

        The deciding question for whether a test may run against a shared or
        production URL. Anything that issues a non-idempotent request, or drives
        the interface into clicking and typing, can leave a mark.
        """
        mutating_methods = {"POST", "PUT", "PATCH", "DELETE"}
        for step in self.when:
            if step.action in {Action.CLICK, Action.FILL}:
                return True
            if (step.target.method or "GET").upper() in mutating_methods:
                return True
        return any((a.target.method or "GET").upper() in mutating_methods for a in self.then)

    @property
    def ready_to_approve(self) -> bool:
        """Whether this can be accepted as-is.

        A scenario with unanswered questions or no assertions is not a test; it
        is a draft that would pass vacuously, which is worse than no test at
        all.
        """
        return not self.open_questions and bool(self.then)

    @property
    def blocking_eligible(self) -> bool:
        """Whether a failure here may block a merge.

        Requires certification *and* at least one assertion backed by real
        evidence. A certified test whose every expectation was inferred proves
        only that behaviour has not changed since a model guessed at it.
        """
        return self.status is ScenarioStatus.CERTIFIED and any(a.is_evidence for a in self.then)


class ScenarioIndex(BaseModel):
    """A collection of scenarios, for summary and filtering."""

    model_config = ConfigDict(extra="forbid")

    scenarios: list[Scenario] = Field(default_factory=list)

    def by_status(self, status: ScenarioStatus) -> list[Scenario]:
        """All scenarios in one state."""
        return [s for s in self.scenarios if s.status is status]

    def get(self, scenario_id: str) -> Scenario | None:
        """Look up one scenario by id."""
        return next((s for s in self.scenarios if s.id == scenario_id), None)

    @property
    def counts(self) -> dict[str, int]:
        """Scenario counts by status."""
        return {
            status.value: len(self.by_status(status))
            for status in ScenarioStatus
            if self.by_status(status)
        }
