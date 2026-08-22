"""What each test is doing, joined from three places that each know a third.

The interface showed a list of tests with no indication of which ones were
protecting anything. These tests are about the join that fixes that, and
especially about its ordering: a failure the product caused must outrank
everything, because it is the only thing here that might be a regression.
"""

from __future__ import annotations

import pytest

from testtrout.domain.gap import TestKind
from testtrout.domain.question import Question, QuestionKind, QuestionStatus
from testtrout.domain.run import (
    Classification,
    Evidence,
    RunRecord,
    ScenarioResult,
)
from testtrout.domain.scenario import Scenario, ScenarioIndex, ScenarioStatus
from testtrout.planning import tests_view
from testtrout.planning.tests_view import TestState


def scenario(sid: str, **kwargs) -> Scenario:
    return Scenario(
        id=sid,
        title=kwargs.pop("title", sid),
        kind=TestKind.BROWSER_JOURNEY,
        status=kwargs.pop("status", ScenarioStatus.APPROVED),
        emitted_to=kwargs.pop("emitted_to", "tests/trout/browser/x.spec.ts"),
        **kwargs,
    )


def result(sid: str, classification: Classification, message: str = "") -> ScenarioResult:
    return ScenarioResult(
        scenario_id=sid,
        classification=classification,
        message=message,
        evidence=Evidence(reproduce="npx playwright test x"),
    )


def view_of(index: ScenarioIndex, results=None, questions=None):
    return tests_view.build(index, results or {}, questions or [])


def test_a_passing_test_says_nothing_more() -> None:
    """Nothing is flagged against a test that works."""
    index = ScenarioIndex(scenarios=[scenario("a")])
    (view,) = view_of(index, {"a": result("a", Classification.PASSED)})

    assert view.state is TestState.PASSING
    assert view.detail == ""
    assert not view.flagged


def test_an_assertion_failure_is_the_only_product_signal() -> None:
    index = ScenarioIndex(scenarios=[scenario("a")])
    (view,) = view_of(
        index, {"a": result("a", Classification.ASSERTION_FAILURE, "heading was not visible")}
    )

    assert view.state is TestState.FAILING
    assert view.detail == "heading was not visible"
    assert view.reproduce == "npx playwright test x"


@pytest.mark.parametrize(
    "classification",
    [
        Classification.ENVIRONMENT_FAILURE,
        Classification.AUTH_FAILURE,
        Classification.SKIPPED,
        Classification.INCONCLUSIVE,
    ],
)
def test_everything_else_could_not_run_rather_than_failed(classification) -> None:
    """Reporting a toolchain problem as a failure is how a suite loses credibility."""
    index = ScenarioIndex(scenarios=[scenario("a")])
    (view,) = view_of(index, {"a": result("a", classification, "playwright is not installed")})

    assert view.state is TestState.BLOCKED


def test_an_unanswered_question_outranks_a_stale_pass() -> None:
    """The pass was for a test built on a guess, so the guess comes first."""
    index = ScenarioIndex(scenarios=[scenario("a", open_questions=["What should this return?"])])
    (view,) = view_of(index, {"a": result("a", Classification.PASSED)})

    assert view.state is TestState.NEEDS_INFO
    assert view.detail == "What should this return?"


def test_a_failure_outranks_an_unanswered_question() -> None:
    """A possible regression is the one thing worth interrupting someone for."""
    index = ScenarioIndex(scenarios=[scenario("a", open_questions=["What should this return?"])])
    (view,) = view_of(index, {"a": result("a", Classification.ASSERTION_FAILURE, "wrong total")})

    assert view.state is TestState.FAILING
    assert view.detail == "wrong total"
    assert view.questions == ["What should this return?"]


def test_a_certified_test_with_no_surviving_record_still_passed() -> None:
    """Run records are not committed, so they go missing. Certification does not."""
    index = ScenarioIndex(scenarios=[scenario("a", status=ScenarioStatus.CERTIFIED)])
    (view,) = view_of(index)

    assert view.state is TestState.PASSING


def test_a_test_with_no_code_says_so() -> None:
    index = ScenarioIndex(scenarios=[scenario("a", emitted_to=None)])
    (view,) = view_of(index)

    assert view.state is TestState.UNTRIED
    assert "generated" in view.detail


def test_questions_from_the_log_are_attached_to_their_test() -> None:
    index = ScenarioIndex(scenarios=[scenario("a")])
    question = Question(id="q1", kind=QuestionKind.TEST_UNCERTAIN, text="Which table?", subject="a")
    (view,) = view_of(index, questions=[question])

    assert view.questions == ["Which table?"]
    assert view.state is TestState.NEEDS_INFO


def test_an_answered_question_stops_flagging_its_test() -> None:
    index = ScenarioIndex(scenarios=[scenario("a")])
    question = Question(
        id="q1",
        kind=QuestionKind.TEST_UNCERTAIN,
        text="Which table?",
        subject="a",
        status=QuestionStatus.ANSWERED,
    )
    (view,) = view_of(index, {"a": result("a", Classification.PASSED)}, [question])

    assert view.questions == []
    assert view.state is TestState.PASSING


def test_whatever_needs_a_person_comes_first() -> None:
    """A list sorted by id is tidy and useless. The reason to open it is to act."""
    index = ScenarioIndex(
        scenarios=[
            scenario("pass", title="pass"),
            scenario("ask", title="ask", open_questions=["?"]),
            scenario("fail", title="fail"),
            scenario("new", title="new", emitted_to=None),
        ]
    )
    views = view_of(
        index,
        {
            "pass": result("pass", Classification.PASSED),
            "fail": result("fail", Classification.ASSERTION_FAILURE, "no"),
        },
    )

    assert [v.id for v in views] == ["fail", "ask", "new", "pass"]


def test_the_newest_run_wins_but_an_older_one_is_better_than_none() -> None:
    """A scenario that has not run since is still known to have passed once."""
    old = RunRecord(
        id="r1",
        started_at="2026-01-01T00:00:00Z",
        results=[
            result("a", Classification.ASSERTION_FAILURE, "old"),
            result("b", Classification.PASSED),
        ],
    )
    new = RunRecord(
        id="r2",
        started_at="2026-01-02T00:00:00Z",
        results=[
            result("a", Classification.PASSED),
        ],
    )

    latest = tests_view.latest_results([new, old])

    assert latest["a"].classification is Classification.PASSED
    assert latest["b"].classification is Classification.PASSED
