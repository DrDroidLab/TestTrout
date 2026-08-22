"""Concrete things the tool needs from a person, and nothing else.

The rule that shapes this module: **never ask what the product should do.**
That is the user's product knowledge, and asking for it — "what is the correct
outcome of insert on payments?" — produced a queue of questions nobody could
answer quickly, each of which held a test hostage.

What the tool genuinely cannot discover is much smaller and entirely concrete:
where the thing is deployed, an account to sign in with, a real id to put in a
URL. Those are facts. A person can answer one in seconds, and every one of them
unlocks work immediately.

Everything else is read from the code or observed from the deployment.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class FactKind(StrEnum):
    """What sort of answer a fact takes.

    Drives how it is asked for — a text box, a password field, a pair — and
    whether the value may be stored in committed configuration or must go to
    the gitignored ``.env``.
    """

    URL = "url"
    """A web address. Committed."""
    ACCOUNT = "account"
    """An email and password pair. Secret."""
    SECRET = "secret"
    """A single opaque value, such as a bypass token. Secret."""
    SAMPLE = "sample"
    """A real value for a URL parameter, e.g. a job id that exists. Committed."""
    COMMAND = "command"
    """Something to run, not something to type. Committed as a note only."""

    @property
    def secret(self) -> bool:
        """Whether the value must never reach committed configuration."""
        return self in {FactKind.ACCOUNT, FactKind.SECRET}


class Fact(BaseModel):
    """One thing the tool needs, stated so it can be answered in seconds.

    ``why`` is not decoration. A form of unexplained boxes gets abandoned; a
    box that says what it turns on gets filled in.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: FactKind
    label: str = Field(description="The question, as a short phrase. Not a sentence.")
    why: str = Field(default="", description="What it unlocks. Concrete, and counted.")
    placeholder: str = ""
    evidence: str = Field(
        default="", description="Where this came from — a file and line, or what the probe saw."
    )
    env_var: str = Field(default="", description="Where a secret is stored. Never its value.")
    known: bool = Field(default=False, description="Whether a value is already on file.")
    blocks: int = Field(default=0, description="How many candidate tests are waiting on it.")

    @property
    def required(self) -> bool:
        """Whether anything at all is waiting on this.

        A fact nothing is blocked on is still worth offering — it may improve
        coverage — but it is never presented as something to do first.
        """
        return self.blocks > 0


class FactSheet(BaseModel):
    """Everything the tool would like to know, in one place.

    Presented as a single optional form rather than a queue. A person can fill
    in one field or all of them, and whatever arrives is used — a partial
    answer produces a partial suite, which is worth far more than nothing.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    facts: list[Fact] = Field(default_factory=list)

    @property
    def outstanding(self) -> list[Fact]:
        """What is still unanswered, most blocking first."""
        return sorted(
            (f for f in self.facts if not f.known),
            key=lambda f: (-f.blocks, f.label),
        )

    @property
    def answered(self) -> list[Fact]:
        """What is already on file."""
        return [f for f in self.facts if f.known]

    def get(self, fact_id: str) -> Fact | None:
        """One fact by id."""
        return next((f for f in self.facts if f.id == fact_id), None)


__all__ = ["Fact", "FactKind", "FactSheet"]
