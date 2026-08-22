"""Failure classification, reporter parsing, and the runner's guard clauses."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from testtrout.domain.config import (
    Config,
    Entrypoint,
    ExternalRule,
    IsolationStrategy,
    Permission,
    SubstitutionConfig,
    SupabaseConfig,
    TestUser,
)
from testtrout.domain.run import Classification, RunRecord, RunStatus, ScenarioResult
from testtrout.domain.scenario import Scenario, ScenarioIndex, ScenarioStatus, TestKind
from testtrout.runtime import environment, reporters
from testtrout.runtime.runner import apply_verdicts, run


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("could not sign in as owner: Invalid login credentials", Classification.AUTH_FAILURE),
        ("SUPABASE_URL is not set", Classification.ENVIRONMENT_FAILURE),
        ("connect ECONNREFUSED 127.0.0.1:5173", Classification.ENVIRONMENT_FAILURE),
        (
            "unmatched outbound request to api.stripe.com — no substitution contract",
            Classification.CONTRACT_MISMATCH,
        ),
        ("Test timeout of 30000ms exceeded", Classification.TIMEOUT),
        ("expected 4500 to be 5000", Classification.ASSERTION_FAILURE),
    ],
)
def test_failures_are_classified_from_their_message(message: str, expected: Classification):
    assert reporters.classify_message(message) is expected


def test_an_unrecognised_failure_defaults_to_a_product_signal():
    """Defaulting the other way would let real regressions hide.

    "Probably the environment" is a comfortable assumption and a dangerous one:
    a genuine regression that gets filed as an environment blip is a regression
    that ships.
    """
    assert reporters.classify_message("something odd") is Classification.ASSERTION_FAILURE


def test_only_assertion_failures_are_product_signals():
    """Everything else is about the harness, not the application."""
    assert Classification.ASSERTION_FAILURE.is_product_signal
    for other in (
        Classification.AUTH_FAILURE,
        Classification.ENVIRONMENT_FAILURE,
        Classification.CONTRACT_MISMATCH,
        Classification.TIMEOUT,
        Classification.FLAKE,
    ):
        assert not other.is_product_signal


def _record(*classifications: Classification) -> RunRecord:
    return RunRecord(
        id="r",
        started_at="now",
        results=[
            ScenarioResult(scenario_id=f"s{i}", classification=c)
            for i, c in enumerate(classifications)
        ],
    )


def test_an_environment_failure_never_reports_as_a_pass():
    """The single most important property of the status model."""
    record = _record(Classification.PASSED, Classification.ENVIRONMENT_FAILURE)
    assert record.status is RunStatus.INCONCLUSIVE


def test_inconclusive_outranks_failure():
    """If the environment fell over, the failures mean nothing either."""
    record = _record(Classification.ASSERTION_FAILURE, Classification.AUTH_FAILURE)
    assert record.status is RunStatus.INCONCLUSIVE


def test_an_assertion_failure_fails_the_run():
    assert _record(Classification.PASSED, Classification.ASSERTION_FAILURE).status is RunStatus.FAIL


def test_a_flake_warns_rather_than_fails():
    """A flake is a problem with the test, not evidence about the product."""
    assert _record(Classification.PASSED, Classification.FLAKE).status is RunStatus.WARNING


def test_a_contract_mismatch_warns_and_is_never_a_pass():
    """A mock matching nothing is how a suite reports green while testing nothing."""
    assert _record(Classification.CONTRACT_MISMATCH).status is RunStatus.WARNING


def test_an_empty_run_is_inconclusive_not_a_pass():
    assert RunRecord(id="r", started_at="now").status is RunStatus.INCONCLUSIVE


def test_playwright_report_is_parsed(tmp_path: Path):
    report = tmp_path / "playwright.json"
    report.write_text(
        json.dumps(
            {
                "suites": [
                    {
                        "specs": [
                            {
                                "title": "orders load",
                                "file": "tests/trout/browser/scenario_screen_orders.spec.ts",
                                "tests": [
                                    {
                                        "results": [
                                            {
                                                "status": "failed",
                                                "duration": 1500,
                                                "errors": [{"message": "expected 1 to be 2"}],
                                                "attachments": [
                                                    {"name": "trace", "path": "/tmp/trace.zip"}
                                                ],
                                            }
                                        ]
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    results = reporters.parse_playwright(report, tmp_path)
    assert len(results) == 1
    assert results[0].classification is Classification.ASSERTION_FAILURE
    assert results[0].duration_seconds == 1.5
    assert results[0].evidence.trace == "/tmp/trace.zip"


def test_vitest_report_is_parsed(tmp_path: Path):
    report = tmp_path / "vitest.json"
    report.write_text(
        json.dumps(
            {
                "testResults": [
                    {
                        "name": "/x/tests/trout/authz/scenario_authz_orders_own.test.ts",
                        "assertionResults": [
                            {"title": "a", "status": "passed", "duration": 200},
                            {
                                "title": "b",
                                "status": "failed",
                                "duration": 100,
                                "failureMessages": ["could not sign in as owner"],
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    results = reporters.parse_vitest(report)
    assert [r.classification for r in results] == [
        Classification.PASSED,
        Classification.AUTH_FAILURE,
    ]


def test_a_missing_report_yields_nothing_rather_than_raising(tmp_path: Path):
    """The runner must be able to tell "no report" from "no failures"."""
    assert reporters.parse_vitest(tmp_path / "absent.json") == []
    assert reporters.parse_playwright(tmp_path / "absent.json", tmp_path) == []


def test_environment_collects_every_missing_credential_at_once(monkeypatch):
    """Discovering three missing secrets one run at a time is miserable."""
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    monkeypatch.delenv("QA_OWNER_EMAIL", raising=False)
    config = Config(
        supabase=SupabaseConfig(url="https://x.supabase.co", anon_key="env:SUPABASE_ANON_KEY"),
        test_users=[TestUser(role="owner", email="env:QA_OWNER_EMAIL", password="env:P")],
    )
    result = environment.build(config, Entrypoint(name="e", url="http://x"))
    assert not result.complete
    assert len(result.missing) >= 2


def test_environment_passes_substituted_hosts_but_never_logs_values(monkeypatch):
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    config = Config(
        supabase=SupabaseConfig(url="https://x.supabase.co", anon_key="env:SUPABASE_ANON_KEY"),
        substitution=SubstitutionConfig(
            external=[ExternalRule(name="stripe", match="api.stripe.com")]
        ),
    )
    result = environment.build(config, Entrypoint(name="e", url="http://x"))
    assert result.variables["TROUT_SUBSTITUTE_HOSTS"] == "api.stripe.com"
    assert result.variables["TROUT_BASE_URL"] == "http://x"
    # names() must never expose values.
    assert "anon" not in " ".join(result.names())


def _scenario(scenario_id: str, emitted: str | None) -> Scenario:
    return Scenario(
        id=scenario_id,
        title="t",
        kind=TestKind.AUTHORIZATION,
        status=ScenarioStatus.APPROVED,
        emitted_to=emitted,
    )


def test_a_run_with_nothing_generated_says_what_to_do(tmp_path: Path):
    record = run(
        Config(),
        Entrypoint(name="e", url="http://x"),
        ScenarioIndex(scenarios=[_scenario("scenario:a", None)]),
        tmp_path,
        tmp_path / "runs",
    )
    assert record.status is RunStatus.INCONCLUSIVE
    assert any("trout build" in note for note in record.notes)


def test_a_missing_toolchain_is_inconclusive_not_a_failure(tmp_path: Path):
    """A suite that never ran has said nothing about the product."""
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    record = run(
        Config(),
        Entrypoint(name="e", url="http://x"),
        ScenarioIndex(scenarios=[_scenario("scenario:a", "tests/trout/authz/a.test.ts")]),
        tmp_path,
        tmp_path / "runs",
    )
    assert record.status is RunStatus.INCONCLUSIVE
    assert all(r.classification is Classification.INCONCLUSIVE for r in record.results)
    assert any("vitest is not installed" in note for note in record.notes)


def test_missing_credentials_stop_the_run_before_anything_executes(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"vitest": "1", "@supabase/supabase-js": "2"}}),
        encoding="utf-8",
    )
    config = Config(
        supabase=SupabaseConfig(url="https://x.supabase.co", anon_key="env:SUPABASE_ANON_KEY")
    )
    record = run(
        config,
        Entrypoint(name="e", url="http://x"),
        ScenarioIndex(scenarios=[_scenario("scenario:a", "tests/trout/authz/a.test.ts")]),
        tmp_path,
        tmp_path / "runs",
    )
    assert record.status is RunStatus.INCONCLUSIVE
    assert any("Credentials are missing" in note for note in record.notes)


def test_certification_promotes_only_a_clean_sweep():
    index = ScenarioIndex(scenarios=[_scenario("scenario:a", "x"), _scenario("scenario:b", "y")])
    changes = apply_verdicts(
        index,
        {"scenario:a": Classification.PASSED, "scenario:b": Classification.FLAKE},
    )
    assert changes["scenario:a"] is ScenarioStatus.CERTIFIED
    assert changes["scenario:b"] is ScenarioStatus.QUARANTINED


def test_a_failing_scenario_is_neither_certified_nor_quarantined():
    """It failed consistently, which is a result about the product, not the test."""
    index = ScenarioIndex(scenarios=[_scenario("scenario:a", "x")])
    changes = apply_verdicts(index, {"scenario:a": Classification.ASSERTION_FAILURE})
    assert changes == {}
    assert index.scenarios[0].status is ScenarioStatus.APPROVED


def test_scoped_seed_isolation_reports_its_own_caveat(tmp_path: Path):
    """Results that carry a caveat must say so, not be discovered later."""
    from testtrout.runtime import isolation

    config = Config(supabase=SupabaseConfig(isolation=IsolationStrategy.SCOPED_SEED))
    result = isolation.prepare(config, tmp_path)
    assert result.applied
    assert result.caveats and "reset" in result.caveats[0]


def test_branch_isolation_admits_it_is_unimplemented(tmp_path: Path):
    from testtrout.runtime import isolation

    config = Config(supabase=SupabaseConfig(isolation=IsolationStrategy.BRANCH))
    result = isolation.prepare(config, tmp_path)
    assert not result.applied
    assert "not implemented" in result.detail


def test_an_assertion_failure_is_not_mistaken_for_a_timeout():
    """Regression: `timeout` matched any stack frame from a file named *timeout*.

    A real assertion failure was reported as a timeout, which moves it out of
    the product-signal class and hides a genuine regression.
    """
    message = (
        "AssertionError: expected 4500 to be 5000 // Object.is equality\n"
        "    at /app/node_modules/.vite/deps/chunk-TIMEOUT.js:12:3\n"
        "    at withTimeout (/app/node_modules/vitest/dist/runner.js:88:1)"
    )
    assert reporters.classify_message(message) is Classification.ASSERTION_FAILURE


def test_a_real_timeout_is_still_recognised():
    assert reporters.classify_message("Test timeout of 30000ms exceeded") is Classification.TIMEOUT
    assert reporters.classify_message("Error: test timed out in 5000ms") is Classification.TIMEOUT


def test_classification_ignores_the_tail_of_a_stack_trace():
    """A long trace must not drag a clear message into the wrong class."""
    message = "expected true to be false\n" + "\n".join(
        f"    at econnrefused_helper_{i} (/x/y.js:{i}:1)" for i in range(50)
    )
    assert reporters.classify_message(message) is Classification.ASSERTION_FAILURE


def _endpoint_scenario(sid: str, method: str) -> Scenario:
    from testtrout.domain.scenario import Action, Step, Target

    return Scenario(
        id=sid,
        title=f"{method} /jobs",
        kind=TestKind.ENDPOINT,
        status=ScenarioStatus.APPROVED,
        emitted_to=f"tests/trout/endpoint/{sid}.test.ts",
        when=[Step(action=Action.REQUEST, target=Target(url="/jobs", method=method))],
    )


def test_a_scenario_knows_whether_it_changes_data():
    assert _endpoint_scenario("scenario:a", "POST").mutating is True
    assert _endpoint_scenario("scenario:b", "GET").mutating is False


def test_mutating_tests_are_refused_on_a_deployment_that_is_not_disposable(tmp_path: Path):
    """The guard that stops a suite POSTing to production.

    The prober's network block never covered generated tests: `npx vitest` would
    happily fire POST /auth/signup at a live deployment.
    """
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"vitest": "1", "@supabase/supabase-js": "2"}}),
        encoding="utf-8",
    )
    config = Config(supabase=SupabaseConfig(url="https://x.supabase.co", anon_key="env:ANON"))
    index = ScenarioIndex(
        scenarios=[
            _endpoint_scenario("scenario:write", "POST"),
            _endpoint_scenario("scenario:read", "GET"),
        ]
    )
    record = run(
        config,
        Entrypoint(name="production", url="https://app.example.com"),  # not disposable
        index,
        tmp_path,
        tmp_path / "runs",
    )

    skipped = [r for r in record.results if r.classification is Classification.SKIPPED]
    assert [r.scenario_id for r in skipped] == ["scenario:write"]
    assert any("not marked disposable" in note for note in record.notes)


def test_a_disposable_deployment_gets_the_write_flag(monkeypatch):
    """Generated tests check this themselves, so the guarantee survives `npx vitest`."""
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    config = Config(
        supabase=SupabaseConfig(url="https://x.supabase.co", anon_key="env:SUPABASE_ANON_KEY")
    )
    read_only = environment.build(config, Entrypoint(name="prod", url="https://x.dev"))
    assert "TROUT_ALLOW_WRITES" not in read_only.variables

    disposable = environment.build(
        config,
        Entrypoint(
            name="local",
            url="http://localhost:3000",
            disposable=True,
            allow=[Permission.READ, Permission.WRITE],
        ),
    )
    assert disposable.variables["TROUT_ALLOW_WRITES"] == "1"


def test_the_generated_helper_refuses_writes_without_the_flag():
    """Defence in depth: the check lives in the emitted code, not just the runner."""
    from testtrout.authoring.emitters.endpoint import SETUP_SOURCE

    assert "TROUT_ALLOW_WRITES" in SETUP_SOURCE
    assert "refusing to" in SETUP_SOURCE
    assert "guard(" in SETUP_SOURCE


def test_playwright_report_paths_resolve_from_the_config_directory(tmp_path: Path):
    """Playwright is the exception, and getting it wrong looked like a pass.

    `outputFile` and `outputDir` resolve against the directory holding the
    config, not the working directory. Written from the project root they put
    every report and screenshot under `tests/trout/.trout/`, where nothing
    looked for them — so a browser test that ran and genuinely failed came back
    as "the test did not run", which is inconclusive rather than a failure.
    """
    from testtrout.runtime import toolchain

    toolchain.write_configs(tmp_path, timeout_seconds=30, report_dir=tmp_path / ".trout/runs/x")
    config = (tmp_path / "tests/trout/playwright.config.ts").read_text(encoding="utf-8")

    assert "outputFile: '../../.trout/runs/x/playwright.json'" in config
    assert "outputDir: '../../.trout/runs/x/artifacts'" in config


def test_vitest_report_path_resolves_from_the_project_root(tmp_path: Path):
    """Vitest resolves reporter output against the working directory instead.

    The runner always invokes it from the project root, so this one stays
    root-relative — the same reason its `include` globs are written that way.
    """
    from testtrout.runtime import toolchain

    toolchain.write_configs(tmp_path, timeout_seconds=30, report_dir=tmp_path / ".trout/runs/x")
    config = (tmp_path / "tests/trout/vitest.config.ts").read_text(encoding="utf-8")

    assert "outputFile: '.trout/runs/x/vitest.json'" in config


def test_the_browser_helper_exports_everything_a_generated_test_imports(tmp_path: Path):
    """A missing export made every browser test fail before it started.

    `installSubstitutions` was imported by every generated spec and never
    written, so Playwright reported "No tests found" and the failure was
    classified as inconclusive rather than as the setup problem it was.
    """
    import re

    from testtrout.authoring.emitters.playwright import SETUP_PATH, PlaywrightEmitter

    scenario = Scenario(
        id="scenario:screen_home",
        title="/ loads",
        kind=TestKind.BROWSER_JOURNEY,
        status=ScenarioStatus.APPROVED,
    )
    emitted = PlaywrightEmitter().emit(scenario, Config())
    helper = emitted.shared[SETUP_PATH]

    imported = set(re.findall(r"import \{([^}]*)\} from '\.\./_browser'", emitted.content))
    names = {name.strip() for group in imported for name in group.split(",") if name.strip()}
    assert names, "the generated spec should import from the shared helper"

    exported = set(re.findall(r"export async function (\w+)", helper))
    assert names <= exported, f"imported but not exported: {sorted(names - exported)}"


def test_the_toolchain_is_read_from_the_app_root_not_the_repository_root(tmp_path: Path):
    """A monorepo keeps its frontend in a subdirectory.

    Reading dependencies from the repository root there finds no package.json
    at all, so every runner is reported missing and every generated test comes
    back "the toolchain is not installed" — a confusing way to say "I looked in
    the wrong directory".
    """
    import json

    from testtrout.runtime import toolchain

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(
        json.dumps({"devDependencies": {"vitest": "^2", "@playwright/test": "^1"}}),
        encoding="utf-8",
    )

    assert toolchain.app_root(tmp_path) == frontend

    chain = toolchain.detect(tmp_path)
    assert chain.has_vitest and chain.has_playwright


def test_generated_configs_land_beside_the_package_json(tmp_path: Path):
    """Otherwise the runner writes its config where npx will never look."""
    import json

    from testtrout.runtime import toolchain

    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text(json.dumps({}), encoding="utf-8")

    toolchain.write_configs(tmp_path, timeout_seconds=30, report_dir=tmp_path / "runs")

    assert (frontend / "tests/trout/playwright.config.ts").is_file()
    assert not (tmp_path / "tests/trout/playwright.config.ts").exists()


def test_authorization_specs_go_to_playwright_not_vitest(tmp_path: Path):
    """They import @playwright/test, so only Playwright can run them.

    Routing by directory sent `authz/` to Vitest, where they matched no include
    glob — generated, never executed, and silently absent from every report.
    """
    from testtrout.runtime import toolchain

    toolchain.write_configs(tmp_path, timeout_seconds=30, report_dir=tmp_path / "runs")
    playwright = (tmp_path / "tests/trout/playwright.config.ts").read_text(encoding="utf-8")
    vitest = (tmp_path / "tests/trout/vitest.config.ts").read_text(encoding="utf-8")

    assert "authz/**/*.spec.ts" in playwright
    assert "browser/**/*.spec.ts" in playwright
    assert "authz" not in vitest


def test_endpoint_tests_need_only_vitest():
    """Requiring @supabase/supabase-js blocked every HTTP test in an app that
    has never heard of Supabase — a leftover from when signing in meant calling
    it directly, rather than driving the app's own login form."""
    from testtrout.runtime.toolchain import Toolchain

    chain = Toolchain(root=Path("."), has_vitest=True, has_supabase_js=False)

    assert chain.can_run("node")
    assert not chain.can_run("browser")
