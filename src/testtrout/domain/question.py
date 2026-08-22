"""Questions the tool needs answered to do better work.

The single most useful thing a testing tool can say is "I do not know, and here
is exactly what I need from you". Today that admission is scattered: a scenario
carries ``open_questions``, a scan emits warnings, a run reports an
inconclusive result. Each is the same act — the tool reaching a limit — and
each is easy to miss.

Gathering them into one queue changes the interaction. Instead of reading three
different surfaces to work out why coverage is thin, a developer sees a short
list of things only they can answer, works through it, and the tests improve.

Two rules keep the queue worth reading:

*A question must be answerable in under a minute.* If it needs investigation it
is a task, not a question, and belongs in the findings.

*A question must say what changes when it is answered.* "What is the audit_log
table for?" is a chore. "What is audit_log for — I cannot tell whether it needs
a test" is a decision.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


def _now() -> str:
    return datetime.now(UTC).isoformat()


class QuestionKind(StrEnum):
    """Why the tool is asking."""

    MISSING_CREDENTIAL = "missing_credential"
    """Something is not configured, and a capability is blocked on it."""

    AMBIGUOUS_BEHAVIOUR = "ambiguous_behaviour"
    """The code shows *that* something happens but not what should happen."""

    UNRESOLVED_TARGET = "unresolved_target"
    """A computed path or table name the scan could not follow."""

    TEST_UNCERTAIN = "test_uncertain"
    """A drafted test could not be validated against the deployment."""

    FAILURE_UNCLEAR = "failure_unclear"
    """A test failed in a way that could be the product or the test."""

    UNCOVERED_AREA = "uncovered_area"
    """Something changed or exists with nothing protecting it."""

    @property
    def label(self) -> str:
        """A short description of what kind of question this is.

        Plainer than a badge saying "blocks work" on everything. A first
        attempt marked almost every kind as blocking, which put twenty
        questions in one bucket and told the reader nothing about where to
        start.
        """
        return {
            "missing_credential": "setup",
            "ambiguous_behaviour": "unclear behaviour",
            "unresolved_target": "unresolved code",
            "test_uncertain": "test unproven",
            "failure_unclear": "failure unclear",
            "uncovered_area": "not covered",
        }[self.value]

    @property
    def blocks_work(self) -> bool:
        """Whether this stops tests being written at all.

        Narrow on purpose. Configuration and code the scan could not follow
        block drafting outright; an ambiguity about intended behaviour only
        makes the resulting test weaker, which is a different and lesser
        problem.
        """
        return self in {
            QuestionKind.MISSING_CREDENTIAL,
            QuestionKind.UNRESOLVED_TARGET,
        }


class QuestionStatus(StrEnum):
    """Where a question is in its life."""

    OPEN = "open"
    ANSWERED = "answered"
    DISMISSED = "dismissed"
    """The developer decided it does not matter. Kept so it is not re-asked."""


class Question(BaseModel):
    """One thing the tool needs from a person."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: QuestionKind
    text: str = Field(description="The question, in one sentence, answerable quickly.")
    context: str = Field(default="", description="What prompted it, and what it affects.")
    unlocks: str = Field(
        default="", description="What becomes possible once answered. Never left empty."
    )
    choices: list[str] = Field(
        default_factory=list,
        description="Suggested answers. A question with choices is faster to answer than one "
        "with an empty box, and easier to act on.",
    )
    subject: str | None = Field(
        default=None, description="The surface, test, or run this concerns."
    )
    source: str = Field(default="", description="Which action raised it.")
    status: QuestionStatus = QuestionStatus.OPEN
    answer: str | None = None
    created_at: str = Field(default_factory=_now)
    answered_at: str | None = None

    @property
    def open(self) -> bool:
        """Whether this still needs a person."""
        return self.status is QuestionStatus.OPEN

    def resolve(self, answer: str) -> None:
        """Record an answer."""
        self.answer = answer
        self.status = QuestionStatus.ANSWERED
        self.answered_at = _now()

    def dismiss(self) -> None:
        """Mark as not worth answering, so it is not raised again."""
        self.status = QuestionStatus.DISMISSED
        self.answered_at = _now()


class QuestionLog(BaseModel):
    """Every question for one project, stored in ``.trout/questions.yaml``.

    Committed, because an answer is a decision about the product and belongs
    with the code — the next person to read the suite benefits from knowing why
    a test asserts what it does.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    questions: list[Question] = Field(default_factory=list)

    def open_questions(self) -> list[Question]:
        """Unanswered questions, most consequential first."""
        return sorted(
            (q for q in self.questions if q.open),
            key=lambda q: (not q.kind.blocks_work, q.created_at),
        )

    def get(self, question_id: str) -> Question | None:
        """One question by id."""
        return next((q for q in self.questions if q.id == question_id), None)

    def answer_for(self, subject: str) -> str | None:
        """The answer recorded for a subject, if any.

        This is how an answer changes behaviour: the next build reads it back
        instead of asking again.
        """
        for question in self.questions:
            if question.subject == subject and question.answer:
                return question.answer
        return None

    def add(self, question: Question) -> bool:
        """Record a question unless it is already known.

        Deduplicated by id, so a rescan does not reopen something the developer
        has already answered or dismissed. A queue that regrows every scan is a
        queue nobody works through.

        Also deduplicated by kind and text together, because two sources can
        arrive at the same sentence honestly: browser tests and authorization
        tests are blocked by the same missing runner, and asking twice for one
        install command makes the queue look like busywork. Kind is part of the
        key so that two genuinely different questions which happen to share
        wording are not silently collapsed into one.
        """
        duplicate = any(
            q.id == question.id or (q.kind is question.kind and q.text == question.text)
            for q in self.questions
        )
        if duplicate:
            return False
        self.questions.append(question)
        return True

    @property
    def counts(self) -> dict[str, int]:
        """Question counts by status."""
        return {
            status.value: sum(1 for q in self.questions if q.status is status)
            for status in QuestionStatus
            if any(q.status is status for q in self.questions)
        }
