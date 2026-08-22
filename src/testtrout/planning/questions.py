"""Turning everything the tool could not determine into a queue of questions.

Scan warnings, blocked capabilities, unanswered scenario questions, and
inconclusive results are all the same act — the tool reaching a limit — arriving
through four different surfaces. This gathers them into one list a developer can
actually work through.

Ids are derived from what a question is *about* rather than when it was raised,
so rescanning does not reopen something already answered. A queue that regrows
on every scan is a queue nobody finishes.
"""

from __future__ import annotations

from testtrout.domain.config import Config
from testtrout.domain.gap import GapMap
from testtrout.domain.observation import ProbeResult
from testtrout.domain.question import Question, QuestionKind, QuestionLog
from testtrout.domain.requirements import Plan
from testtrout.domain.scenario import ScenarioIndex
from testtrout.domain.surface import ScanResult


def _slug(value: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:60] or "unknown"


def from_scan(scan: ScanResult) -> list[Question]:
    """Questions the scan raised about code it could not follow."""
    out: list[Question] = []

    for warning in scan.warnings:
        if warning.code == "unresolved_table":
            where = str(warning.location) if warning.location else "somewhere"
            out.append(
                Question(
                    id=f"q:table:{_slug(where)}",
                    kind=QuestionKind.UNRESOLVED_TARGET,
                    text=f"Which table does the database call at {where} touch?",
                    context="The table name is built at runtime, so the scan cannot follow it.",
                    unlocks="This operation can be tested once its table is known.",
                    subject=where,
                    source="scan",
                )
            )
        elif warning.code == "unresolved_endpoint":
            where = str(warning.location) if warning.location else "somewhere"
            out.append(
                Question(
                    id=f"q:endpoint:{_slug(where)}",
                    kind=QuestionKind.UNRESOLVED_TARGET,
                    text=f"Which endpoint does the request at {where} call?",
                    context="The path is built at runtime, so the scan cannot follow it.",
                    unlocks="This call can be tested once its path is known.",
                    subject=where,
                    source="scan",
                )
            )
        elif warning.code == "table_without_rls":
            out.append(
                Question(
                    id=f"q:rls:{_slug(warning.message[:40])}",
                    kind=QuestionKind.AMBIGUOUS_BEHAVIOUR,
                    text=warning.message.split(".")[0] + ". Is that intended?",
                    context=warning.message,
                    unlocks="If it is not intended, this is a finding rather than a test.",
                    choices=["Intended — the data is public", "Not intended — needs fixing"],
                    subject=warning.code,
                    source="scan",
                )
            )
    return out


def from_readiness(plan: Plan) -> list[Question]:
    """Questions about configuration that is blocking a capability."""
    return [
        Question(
            id=f"q:setup:{item.capability.value}",
            kind=QuestionKind.MISSING_CREDENTIAL,
            text=f"{item.missing[0]}",
            context=item.detail,
            unlocks=f"{item.capability.label.capitalize()} become possible.",
            subject=item.capability.value,
            source="setup",
        )
        for item in plan.blocked
        if item.missing
    ]


def from_probe(probe: ProbeResult) -> list[Question]:
    """Questions about what the deployment did that the code did not explain."""
    out: list[Question] = []
    for divergence in probe.divergences:
        if divergence.code == "undeclared_table":
            out.append(
                Question(
                    id=f"q:undeclared:{_slug(divergence.message[:40])}",
                    kind=QuestionKind.AMBIGUOUS_BEHAVIOUR,
                    text=divergence.message + " — what triggers it?",
                    context=divergence.detail or "",
                    unlocks="Knowing the trigger means this can be covered.",
                    subject=divergence.code,
                    source="probe",
                )
            )
        elif divergence.code == "no_login_form":
            out.append(
                Question(
                    id="q:login-form",
                    kind=QuestionKind.MISSING_CREDENTIAL,
                    text="Where do users sign in to this app?",
                    context=divergence.detail or divergence.message,
                    unlocks="Every test behind the login wall, which is usually most of them.",
                    subject="login",
                    source="probe",
                )
            )
        elif divergence.code == "unreachable_screen":
            out.append(
                Question(
                    id=f"q:unreachable:{_slug(divergence.message[:40])}",
                    kind=QuestionKind.AMBIGUOUS_BEHAVIOUR,
                    text=divergence.message + " — is it reachable another way?",
                    context=divergence.detail or "",
                    unlocks="A route that cannot be loaded cannot be tested.",
                    choices=["It needs a sign-in", "It needs specific data", "It is dead code"],
                    subject=divergence.surface_id or divergence.code,
                    source="probe",
                )
            )
    return out


def from_scenarios(index: ScenarioIndex) -> list[Question]:
    """Questions a drafted test could not answer for itself."""
    return [
        Question(
            id=f"q:test:{_slug(scenario.id)}:{position}",
            kind=QuestionKind.AMBIGUOUS_BEHAVIOUR,
            text=text,
            context=f"Drafted for: {scenario.title}",
            unlocks="This test can be approved once answered.",
            subject=scenario.id,
            source="build",
        )
        for scenario in index.scenarios
        for position, text in enumerate(scenario.open_questions)
    ]


def from_gaps(gaps: GapMap) -> list[Question]:
    """Questions about work that is ranked but blocked."""
    seen: set[str] = set()
    out: list[Question] = []
    for gap in gaps.gaps:
        for blocker in gap.blockers:
            if blocker.code in seen:
                continue
            seen.add(blocker.code)
            out.append(
                Question(
                    id=f"q:blocked:{blocker.code}",
                    kind=QuestionKind.MISSING_CREDENTIAL,
                    text=blocker.message,
                    context=f"Blocking {sum(1 for g in gaps.gaps if not g.ready)} test(s).",
                    unlocks="Those tests can be drafted.",
                    subject=blocker.code,
                    source="scan",
                )
            )
    return out


def from_api_address(
    scan: ScanResult | None, probe: ProbeResult | None, config: Config | None
) -> list[Question]:
    """Ask once where the API lives, when the evidence says it is not here.

    An app whose backend deploys separately has endpoint paths that mean
    nothing against the frontend's origin: every one of them returns 404. Asked
    per endpoint that is thirty identical questions; asked once it is a single
    setting, and answering it makes every endpoint testable at the same time.
    """
    if scan is None or config is None or not scan.endpoints:
        return []

    entrypoint = config.entrypoint()
    if entrypoint is None or entrypoint.api_url:
        return []

    # Prefer the deployment's own answer. Falling back to the code alone would
    # nag an app that happens to serve its API from the same origin.
    if probe is not None and probe.endpoints:
        answered = [e for e in probe.endpoints if e.status is not None]
        missing = [e for e in answered if e.status == 404]
        if not answered or len(missing) * 2 <= len(answered):
            return []
        evidence = f"{len(missing)} of {len(answered)} endpoints returned 404"
    elif scan.project.api_base_var:
        evidence = f"your code reads the API address from {scan.project.api_base_var}"
    else:
        return []

    variable = scan.project.api_base_var
    return [
        Question(
            id="q:api-address",
            kind=QuestionKind.MISSING_CREDENTIAL,
            text="Where is this app's API served from?",
            context=(
                f"{evidence} against {entrypoint.url}, so the backend is deployed "
                "somewhere else. Set it as the API URL for this deployment under Setup"
                + (f" — the same value as {variable}." if variable else ".")
            ),
            unlocks=f"All {len(scan.endpoints)} endpoint(s) become testable.",
            subject="api_address",
            source="scan",
        )
    ]


def collect(
    log: QuestionLog,
    scan: ScanResult | None = None,
    plan: Plan | None = None,
    probe: ProbeResult | None = None,
    gaps: GapMap | None = None,
    scenarios: ScenarioIndex | None = None,
    config: Config | None = None,
) -> int:
    """Gather questions from every source into the log.

    Returns how many were newly raised. Existing ones — answered or dismissed —
    are left alone, so working through the queue is progress that sticks.
    """
    raised = 0
    for question in (
        (from_scan(scan) if scan else [])
        + from_api_address(scan, probe, config)
        + (from_readiness(plan) if plan else [])
        + (from_probe(probe) if probe else [])
        + (from_gaps(gaps) if gaps else [])
        + (from_scenarios(scenarios) if scenarios else [])
    ):
        raised += int(log.add(question))
    return raised
