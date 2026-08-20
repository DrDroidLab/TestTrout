"""Executing the generated suite and classifying what happened.

Tests are executed by the project's own toolchain via subprocess. What this
adds is everything a bare `npx playwright test` cannot do: putting the database
into a known state first, refusing to proceed when credentials are missing
rather than producing a wall of confusing auth errors, and turning "3 failed"
into three classified results that say which of them are actually about the
product.

The ordering of the guard clauses matters. Missing credentials and an
unrunnable toolchain are reported *before* anything executes, because a suite
that fails for those reasons has said nothing about the application and
reporting it as a failure would be a lie.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from testtrout.domain.config import Config, Entrypoint
from testtrout.domain.run import Classification, RunRecord, ScenarioResult
from testtrout.domain.scenario import Scenario, ScenarioIndex, ScenarioStatus
from testtrout.runtime import environment, isolation, reporters, toolchain

# Generous: a browser suite against a cold serverless deployment can be slow,
# and killing it produces a misleading timeout rather than a real result.
SUITE_TIMEOUT_SECONDS = 1800


@dataclass
class Suite:
    """One runner invocation."""

    kind: str
    config: str
    report: str

    @property
    def label(self) -> str:
        """Human-readable name."""
        return "browser" if self.kind == "browser" else "node"


SUITES = (
    Suite(kind="node", config="tests/trout/vitest.config.ts", report="vitest.json"),
    Suite(kind="browser", config="tests/trout/playwright.config.ts", report="playwright.json"),
)


def run(
    config: Config,
    entrypoint: Entrypoint,
    scenarios: ScenarioIndex,
    root: Path,
    report_dir: Path,
    scenario_id: str | None = None,
    only: list[str] | None = None,
    reset: bool = True,
) -> RunRecord:
    """Execute the suite once and return a classified record.

    Args:
        config: Repository configuration.
        entrypoint: Which deployment to run against.
        scenarios: The scenario index; only those with generated code run.
        root: Project root, and the working directory for the test runners.
        report_dir: Where runner JSON reports and artifacts are written.
        scenario_id: Run only this scenario. Used by certification and by the
            reproduction command attached to every failure.
        only: Restrict the run to these scenario ids. Used by change-based
            selection, which decides the set deterministically upstream.
        reset: Whether to apply the configured isolation strategy first.
    """
    started = datetime.now(UTC)
    record = RunRecord(
        id=started.strftime("%Y%m%dT%H%M%SZ"),
        started_at=started.isoformat(),
        entrypoint=entrypoint.name,
        base_url=entrypoint.url,
        isolation=config.supabase.isolation.value,
    )

    generated = [s for s in scenarios.scenarios if s.emitted_to]
    if only is not None:
        generated = [s for s in generated if s.id in set(only)]
    if scenario_id:
        generated = [s for s in generated if s.id == scenario_id]
    if not generated:
        record.notes.append(
            "No generated tests to run. Approve scenarios with `qa approve`, then `qa generate`."
        )
        record.finished_at = datetime.now(UTC).isoformat()
        return record

    chain = toolchain.detect(root)
    kinds_needed = {
        "browser" if s.emitted_to and "browser/" in s.emitted_to else "node" for s in generated
    }
    unrunnable = [k for k in kinds_needed if not chain.can_run(k)]
    if unrunnable:
        record.notes.extend(chain.problems)
        record.results = [
            _inconclusive(s, "the project's test toolchain is not installed") for s in generated
        ]
        record.finished_at = datetime.now(UTC).isoformat()
        return record

    env = environment.build(config, entrypoint)
    if not env.complete:
        record.notes.append(
            "Credentials are missing, so nothing was executed. A run that fails for this "
            "reason says nothing about the product."
        )
        record.notes.extend(f"missing: {item}" for item in env.missing)
        record.results = [_inconclusive(s, "missing credentials") for s in generated]
        record.finished_at = datetime.now(UTC).isoformat()
        return record

    if reset:
        isolated = isolation.prepare(config, root)
        record.notes.extend(isolated.caveats)
        record.isolation = (
            isolated.strategy.value
            if isolated.applied
            else f"{isolated.strategy.value} (not applied)"
        )

    report_dir.mkdir(parents=True, exist_ok=True)
    record.notes.extend(
        f"wrote {written}"
        for written in toolchain.write_configs(root, config.run.timeout_seconds, report_dir)
    )

    results: list[ScenarioResult] = []
    for suite in SUITES:
        if suite.kind not in kinds_needed:
            continue
        results.extend(
            _run_suite(suite, chain, root, report_dir, env.merged(), scenario_id, record)
        )

    record.results = _attach_metadata(results, generated, entrypoint)
    record.finished_at = datetime.now(UTC).isoformat()
    return record


def _run_suite(
    suite: Suite,
    chain: toolchain.Toolchain,
    root: Path,
    report_dir: Path,
    env: dict[str, str],
    scenario_id: str | None,
    record: RunRecord,
) -> list[ScenarioResult]:
    """Invoke one test runner and parse its report."""
    report = report_dir / suite.report
    report.unlink(missing_ok=True)

    if suite.kind == "browser":
        command = [*chain.runner, "playwright", "test", "--config", suite.config]
        if scenario_id:
            command += ["-g", scenario_id.replace("scenario:", "")]
    else:
        command = [*chain.runner, "vitest", "run", "--config", suite.config]
        if scenario_id:
            command += ["-t", scenario_id.replace("scenario:", "")]

    try:
        completed = subprocess.run(
            command,
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=SUITE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        record.notes.append(f"the {suite.label} suite exceeded {SUITE_TIMEOUT_SECONDS}s")
        return []
    except OSError as exc:
        record.notes.append(f"could not start the {suite.label} runner: {exc}")
        return []

    parsed = (
        reporters.parse_playwright(report, report_dir / "artifacts")
        if suite.kind == "browser"
        else reporters.parse_vitest(report)
    )

    if not parsed and completed.returncode != 0:
        # The runner failed before producing a report — a config error, a
        # missing binary, a syntax error in a generated file. That is an
        # environment problem, not a product one, and must not be silently lost.
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-12:]
        record.notes.append(
            f"the {suite.label} runner exited {completed.returncode} without a report: "
            + " ".join(tail)[:400]
        )
    return parsed


def _attach_metadata(
    results: list[ScenarioResult], scenarios: list[Scenario], entrypoint: Entrypoint
) -> list[ScenarioResult]:
    """Fill in titles and reproduction commands.

    A failure the developer cannot reproduce in one command is a failure they
    will not act on, so this is not cosmetic.
    """
    by_id = {s.id: s for s in scenarios}
    for result in results:
        scenario = by_id.get(result.scenario_id) or by_id.get(f"scenario:{result.scenario_id}")
        if scenario is not None:
            result.scenario_id = scenario.id
            result.title = result.title or scenario.title
        result.evidence.reproduce = (
            f"qa run --scenario {result.scenario_id} --env {entrypoint.name}"
        )
    return results


def _inconclusive(scenario: Scenario, reason: str) -> ScenarioResult:
    """A result for a scenario that never ran."""
    return ScenarioResult(
        scenario_id=scenario.id,
        title=scenario.title,
        classification=Classification.INCONCLUSIVE,
        message=reason,
    )


def certify(
    config: Config,
    entrypoint: Entrypoint,
    scenarios: ScenarioIndex,
    root: Path,
    report_dir: Path,
    runs: int | None = None,
) -> tuple[dict[str, Classification], list[RunRecord]]:
    """Run approved scenarios repeatedly to prove they are deterministic.

    A scenario that has passed once has not shown it is stable, and an
    intermittently-failing test in a blocking suite is how a team learns to
    ignore the suite. So certification requires N consecutive identical passes,
    and anything inconsistent is quarantined rather than admitted.

    Returns:
        A verdict per scenario, and the individual run records behind it.
    """
    attempts = runs or config.run.certification_runs
    records: list[RunRecord] = []
    observed: dict[str, list[Classification]] = {}

    for index in range(attempts):
        record = run(
            config,
            entrypoint,
            scenarios,
            root,
            report_dir / f"certify-{index}",
            # Reset only before the first pass: certification is measuring
            # whether a scenario is stable, and resetting between passes would
            # hide order-dependence, which is exactly the flakiness sought.
            reset=index == 0,
        )
        records.append(record)
        for result in record.results:
            observed.setdefault(result.scenario_id, []).append(result.classification)

    verdicts: dict[str, Classification] = {}
    for scenario_id, seen in observed.items():
        if len(set(seen)) > 1:
            verdicts[scenario_id] = Classification.FLAKE
        elif seen and seen[0] is Classification.PASSED:
            verdicts[scenario_id] = Classification.PASSED
        else:
            verdicts[scenario_id] = seen[0] if seen else Classification.INCONCLUSIVE
    return verdicts, records


def apply_verdicts(
    scenarios: ScenarioIndex, verdicts: dict[str, Classification]
) -> dict[str, ScenarioStatus]:
    """Promote or quarantine scenarios based on certification.

    Only a clean sweep promotes. Anything else is quarantined with the reason
    intact, because a scenario nobody trusts is worse than one that is honestly
    labelled untrustworthy.
    """
    changes: dict[str, ScenarioStatus] = {}
    for scenario in scenarios.scenarios:
        verdict = verdicts.get(scenario.id)
        if verdict is None:
            continue
        if verdict is Classification.PASSED:
            scenario.status = ScenarioStatus.CERTIFIED
        elif verdict is Classification.FLAKE:
            scenario.status = ScenarioStatus.QUARANTINED
        else:
            continue
        changes[scenario.id] = scenario.status
    return changes
