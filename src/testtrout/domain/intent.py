"""What the developer says matters, structured.

Static analysis can rank a delete above a select, but it cannot know that the
checkout flow is the business and the settings page is not. That judgement only
exists in someone's head, and this is where it gets written down.

Two rules shape the design:

*Intent is claimed, not inferred.* Every journey records where it came from —
the developer said it, or the tool drafted it from the scan and a human
confirmed. A drafted-but-unconfirmed journey is explicitly weaker evidence, and
never silently promoted.

*Ambiguity becomes a question, not a guess.* When the tool cannot tell what a
table is for or why a screen exists, it records an open question rather than
inventing an answer. A developer answers it in seconds; a wrong guess
propagates into every test generated afterwards.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from testtrout.domain.surface import Criticality


class Provenance(StrEnum):
    """Where a claim came from. Ordered from strongest to weakest."""

    STATED = "stated"
    """The developer said it, in their own words."""
    CONFIRMED = "confirmed"
    """The tool drafted it and a human accepted it."""
    OBSERVED = "observed"
    """Seen happening against a running deployment."""
    DERIVED = "derived"
    """Follows deterministically from schema, policy, or code."""
    INFERRED = "inferred"
    """A model's guess. Never sufficient on its own to block anything."""

    @property
    def rank(self) -> int:
        """Sort key, strongest first."""
        return ["stated", "confirmed", "observed", "derived", "inferred"].index(self.value)

    @property
    def is_evidence(self) -> bool:
        """Whether this provenance can justify a blocking assertion."""
        return self is not Provenance.INFERRED


class Journey(BaseModel):
    """One thing a user does with the product, end to end.

    Journeys are the unit the developer thinks in ("a customer places an
    order"), as opposed to surfaces, which are the unit the code exposes. The
    mapping between them is what turns a ranked list of API calls into a test
    plan someone actually agrees with.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable slug, e.g. 'journey:place-order'.")
    name: str
    description: str = ""
    steps: list[str] = Field(
        default_factory=list, description="Plain-language steps, in the product's own words."
    )
    criticality: Criticality = Criticality.HIGH
    roles: list[str] = Field(
        default_factory=list, description="Which test-user roles perform this journey."
    )
    surfaces: list[str] = Field(
        default_factory=list, description="Ids of surfaces this journey touches."
    )
    provenance: Provenance = Provenance.INFERRED
    consequence: str = Field(
        default="",
        description="What happens to the business if this silently breaks. Drives ranking.",
    )


class OpenQuestion(BaseModel):
    """Something the tool could not determine and a human can answer quickly."""

    model_config = ConfigDict(extra="forbid")

    id: str
    question: str
    context: str = Field(default="", description="What prompted the question.")
    surface_id: str | None = None
    answer: str | None = Field(default=None, description="Set once answered; then it is evidence.")

    @property
    def answered(self) -> bool:
        """Whether a human has resolved this."""
        return bool(self.answer)


class ProductIntent(BaseModel):
    """The complete contents of ``.trout/intent.yaml``.

    Committed and hand-editable. A developer correcting this file directly is a
    supported workflow, not a fallback — it is usually faster than another round
    of conversation.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    summary: str = Field(default="", description="What this product is, in one or two sentences.")
    audience: str = Field(default="", description="Who uses it.")
    journeys: list[Journey] = Field(default_factory=list)
    never_break: list[str] = Field(
        default_factory=list,
        description="Blunt statements of what must always hold. Free text, by design.",
    )
    open_questions: list[OpenQuestion] = Field(default_factory=list)

    def journey(self, journey_id: str) -> Journey | None:
        """Look up one journey by id."""
        return next((j for j in self.journeys if j.id == journey_id), None)

    def criticality_for(self, surface_id: str) -> Criticality | None:
        """Highest criticality any journey assigns to a surface.

        This is how stated intent overrides the scanner's deterministic prior:
        a surface nobody mentioned keeps its computed score, and one that sits
        on a critical journey inherits that journey's weight.
        """
        levels = [j.criticality for j in self.journeys if surface_id in j.surfaces]
        return min(levels, key=lambda c: c.rank) if levels else None

    @property
    def unanswered(self) -> list[OpenQuestion]:
        """Questions still waiting on a human."""
        return [q for q in self.open_questions if not q.answered]
