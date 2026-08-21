"""The question queue.

The single most useful thing a testing tool can say is "I do not know, and here
is what I need". These check that the queue stays worth reading.
"""

from __future__ import annotations

from testtrout.domain.gap import Blocker, Gap, GapMap, TestKind
from testtrout.domain.observation import Divergence, ProbeResult
from testtrout.domain.question import Question, QuestionKind, QuestionLog, QuestionStatus
from testtrout.domain.requirements import Capability, Plan, Readiness
from testtrout.domain.scenario import Scenario, ScenarioIndex
from testtrout.domain.surface import ProjectInfo, ScanResult, ScanWarning, SourceLocation
from testtrout.planning import questions as planner


def _question(qid: str = "q:x", kind: QuestionKind = QuestionKind.AMBIGUOUS_BEHAVIOUR) -> Question:
    return Question(id=qid, kind=kind, text="why?", unlocks="a test")


def test_answering_records_the_decision():
    q = _question()
    q.resolve("because of X")
    assert q.status is QuestionStatus.ANSWERED
    assert q.answer == "because of X"
    assert not q.open


def test_a_rescan_does_not_reopen_an_answered_question():
    """A queue that regrows every scan is a queue nobody finishes."""
    log = QuestionLog()
    assert log.add(_question("q:same")) is True
    log.get("q:same").resolve("answered")

    assert log.add(_question("q:same")) is False
    assert log.open_questions() == []


def test_a_dismissed_question_is_not_raised_again():
    log = QuestionLog()
    log.add(_question("q:same"))
    log.get("q:same").dismiss()

    assert log.add(_question("q:same")) is False
    assert log.open_questions() == []


def test_blocking_questions_sort_first():
    log = QuestionLog()
    log.add(_question("q:soft", QuestionKind.AMBIGUOUS_BEHAVIOUR))
    log.add(_question("q:hard", QuestionKind.MISSING_CREDENTIAL))
    assert [q.id for q in log.open_questions()] == ["q:hard", "q:soft"]


def test_only_setup_and_unresolved_code_count_as_blocking():
    """A badge that says "blocks work" on everything tells the reader nothing."""
    assert QuestionKind.MISSING_CREDENTIAL.blocks_work
    assert QuestionKind.UNRESOLVED_TARGET.blocks_work
    assert not QuestionKind.AMBIGUOUS_BEHAVIOUR.blocks_work
    assert not QuestionKind.FAILURE_UNCLEAR.blocks_work


def test_every_kind_has_a_plain_label():
    for kind in QuestionKind:
        assert kind.label
        assert kind.label != kind.value  # a plain phrase, not the enum name


def test_an_answer_is_retrievable_by_subject():
    """This is how an answer changes behaviour: the next build reads it back."""
    log = QuestionLog()
    question = _question("q:1")
    question.subject = "scenario:x"
    log.add(question)
    question.resolve("it is public")

    assert log.answer_for("scenario:x") == "it is public"
    assert log.answer_for("scenario:other") is None


def test_scan_warnings_become_questions():
    scan = ScanResult(
        project=ProjectInfo(root=".", framework="vite-react"),
        warnings=[
            ScanWarning(
                code="unresolved_table",
                message="computed table name",
                location=SourceLocation(file="src/x.ts", line=9),
            )
        ],
    )
    raised = planner.from_scan(scan)
    assert len(raised) == 1
    assert raised[0].kind is QuestionKind.UNRESOLVED_TARGET
    assert "src/x.ts:9" in raised[0].text


def test_blocked_capabilities_become_questions():
    plan = Plan(
        readiness=[
            Readiness(
                capability=Capability.AUTHORIZATION_TESTS,
                ready=False,
                missing=["a second test account"],
                detail="proves isolation",
            )
        ]
    )
    raised = planner.from_readiness(plan)
    assert raised[0].kind is QuestionKind.MISSING_CREDENTIAL
    assert "Authorization tests" in raised[0].unlocks


def test_probe_divergences_become_questions():
    probe = ProbeResult(
        entrypoint="e",
        base_url="http://x",
        divergences=[
            Divergence(code="no_login_form", message="none found", detail="looked at /login")
        ],
    )
    raised = planner.from_probe(probe)
    assert raised[0].id == "q:login-form"
    assert "sign in" in raised[0].text


def test_scenario_questions_are_carried_through():
    index = ScenarioIndex(
        scenarios=[
            Scenario(
                id="scenario:a",
                title="t",
                kind=TestKind.ENDPOINT,
                open_questions=["should this 401?"],
            )
        ]
    )
    raised = planner.from_scenarios(index)
    assert raised[0].text == "should this 401?"
    assert raised[0].subject == "scenario:a"


def test_one_blocker_raises_one_question_however_many_gaps_it_blocks():
    """Twenty copies of the same question is noise, not thoroughness."""
    gaps = GapMap(
        gaps=[
            Gap(
                id=f"gap:{i}",
                kind=TestKind.AUTHORIZATION,
                title="t",
                blockers=[Blocker(code="needs_two_roles", message="add a second account")],
            )
            for i in range(5)
        ]
    )
    assert len(planner.from_gaps(gaps)) == 1


def test_every_question_says_what_answering_unlocks():
    """ "What is audit_log for?" is a chore; saying why makes it a decision."""
    scan = ScanResult(
        project=ProjectInfo(root=".", framework="vite-react"),
        warnings=[
            ScanWarning(
                code="unresolved_table", message="x", location=SourceLocation(file="a.ts", line=1)
            )
        ],
    )
    log = QuestionLog()
    planner.collect(log, scan=scan)
    for question in log.questions:
        assert question.unlocks, f"{question.id} does not say what it unlocks"
