"""The state of one test, joined from everything that has an opinion about it.

A scenario file says what a test asserts, a run record says what happened last
time, and the question log says what is unanswered. Those three live in three
places, and until they are joined the interface can show a list of tests with
no indication of which ones are actually protecting anything.

So this is the join, done once here rather than three times in a browser.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from testtrout.domain.question import Question
from testtrout.domain.run import Classification, RunRecord, ScenarioResult
from testtrout.domain.scenario import Scenario, ScenarioIndex, ScenarioStatus
from testtrout.domain.surface import Criticality


class TestState(StrEnum):
    """What a test is doing for you right now.

    Deliberately not the same as scenario status. Status is a decision about
    whether a test belongs in the suite; this is whether it is currently
    protecting anything, which is the question someone looking at a list wants
    answered.
    """

    PASSING = "passing"
    """Ran against the deployment and passed. The only state that protects."""
    FAILING = "failing"
    """The product did not do what the test asserts. The only product signal."""
    NEEDS_INFO = "needs_info"
    """Waiting on an answer. Not a defect — a question."""
    BLOCKED = "blocked"
    """Ran, but something other than the product stopped it: toolchain,
    credentials, an unreachable deployment. Says nothing either way."""
    UNTRIED = "untried"
    """Written but never run."""

    @property
    def label(self) -> str:
        """Words rather than jargon."""
        return {
            "passing": "passing",
            "failing": "failing",
            "needs_info": "needs you",
            "blocked": "could not run",
            "untried": "not run yet",
        }[self.value]

    @property
    def needs_attention(self) -> bool:
        """Whether this test is waiting on a person."""
        return self is not TestState.PASSING


class TestView(BaseModel):
    """One test, and everything flagged against it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    kind: str
    criticality: Criticality
    status: ScenarioStatus
    state: TestState
    detail: str = Field(default="", description="Why it is in this state, in one line.")
    classification: str | None = None
    questions: list[str] = Field(default_factory=list)
    reproduce: str = ""
    emitted_to: str | None = None
    surfaces: list[str] = Field(default_factory=list)

    @property
    def flagged(self) -> bool:
        """Whether this row should draw the eye."""
        return self.state.needs_attention


def latest_results(runs: list[RunRecord]) -> dict[str, ScenarioResult]:
    """The most recent result for each scenario, across runs newest-first.

    A scenario that ran three runs ago and has not run since still has a last
    known result, and hiding it would make the list look emptier than it is.
    """
    seen: dict[str, ScenarioResult] = {}
    for record in runs:
        for result in record.results:
            seen.setdefault(result.scenario_id, result)
    return seen


def build(
    index: ScenarioIndex,
    results: dict[str, ScenarioResult],
    questions: list[Question],
) -> list[TestView]:
    """Join scenarios with their last result and their open questions.

    Ordered so that whatever needs a person comes first: a list sorted by id is
    tidy and useless, and the reason to open this tab is to find what to do.
    """
    by_subject: dict[str, list[Question]] = {}
    for question in questions:
        if question.subject:
            by_subject.setdefault(question.subject, []).append(question)

    views = [_view(scenario, results.get(scenario.id), by_subject) for scenario in index.scenarios]
    order = {
        TestState.FAILING: 0,
        TestState.NEEDS_INFO: 1,
        TestState.BLOCKED: 2,
        TestState.UNTRIED: 3,
        TestState.PASSING: 4,
    }
    return sorted(views, key=lambda v: (order[v.state], v.criticality.rank, v.title))


def _view(
    scenario: Scenario,
    result: ScenarioResult | None,
    by_subject: dict[str, list[Question]],
) -> TestView:
    """Decide what one test is doing, and say why in one line."""
    open_questions = [
        question.text for question in by_subject.get(scenario.id, []) if question.open
    ]
    # The scenario's own open_questions are the authoritative copy: the log
    # dedupes across rescans and may not carry one raised moments ago.
    for text in scenario.open_questions:
        if text not in open_questions:
            open_questions.append(text)

    state, detail = _classify(scenario, result, open_questions)
    return TestView(
        id=scenario.id,
        title=scenario.title,
        kind=scenario.kind.value,
        criticality=scenario.criticality,
        status=scenario.status,
        state=state,
        detail=detail,
        classification=result.classification.value if result else None,
        questions=open_questions,
        reproduce=(result.evidence.reproduce if result and result.evidence else "") or "",
        emitted_to=scenario.emitted_to,
        surfaces=scenario.surfaces,
    )


def _classify(
    scenario: Scenario, result: ScenarioResult | None, open_questions: list[str]
) -> tuple[TestState, str]:
    """Rank the reasons a test is not protecting anything.

    Order matters. A failure the product caused outranks everything, because it
    is the only thing here that might be a regression. An unanswered question
    outranks a stale pass, because the pass was for a test built on a guess.
    """
    if result is not None and result.classification.is_product_signal:
        return TestState.FAILING, result.message or "the product did not do what this asserts"

    if open_questions:
        return TestState.NEEDS_INFO, open_questions[0]

    if result is None:
        # Certification only ever happens by passing, so a certified scenario
        # whose run record has been cleaned up still tells the truth.
        if scenario.status is ScenarioStatus.CERTIFIED:
            return TestState.PASSING, ""
        return (
            TestState.UNTRIED,
            "written but not run yet"
            if scenario.emitted_to
            else "no test code has been generated for this yet",
        )

    if result.passed:
        return TestState.PASSING, ""

    if result.classification is Classification.SKIPPED:
        return TestState.BLOCKED, result.message or "skipped"
    return TestState.BLOCKED, result.message or f"{result.classification.value.replace('_', ' ')}"


__all__ = ["TestState", "TestView", "build", "latest_results"]
