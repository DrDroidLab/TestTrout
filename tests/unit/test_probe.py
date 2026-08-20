"""Integration tests for the prober, against a hermetic fake application.

These drive a real browser. They are skipped when Playwright's Chromium is not
installed, so the rest of the suite still runs on a bare checkout.
"""

from __future__ import annotations

import pytest
from tests.fake_app import WRITES_RECEIVED, FakeApp

from testtrout.domain.config import (
    Config,
    Entrypoint,
    Permission,
    ProjectConfig,
    SupabaseConfig,
)
from testtrout.domain.observation import CallKind
from testtrout.domain.surface import (
    Criticality,
    ProjectInfo,
    ScanResult,
    Screen,
    SourceLocation,
)

pytest.importorskip("playwright.sync_api")

LOCATION = SourceLocation(file="src/App.tsx", line=1)


def _has_browser() -> bool:
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
    except Exception:
        return False
    return True


requires_browser = pytest.mark.skipif(
    not _has_browser(), reason="chromium not installed (run `playwright install chromium`)"
)


def _scan() -> ScanResult:
    """A minimal surface map matching the fake app's routes."""
    paths = ["/orders", "/settings", "/checkout", "/reports", "/orders/:id"]
    return ScanResult(
        project=ProjectInfo(root=".", framework="vite-react", backend="supabase"),
        screens=[
            Screen(
                id=f"screen:{path}",
                location=LOCATION,
                path=path,
                component="X",
                params=["id"] if ":id" in path else [],
                criticality=Criticality.HIGH,
            )
            for path in paths
        ],
    )


def _config(base_url: str, disposable: bool) -> Config:
    """Point both the app origin and Supabase at the fake server."""
    return Config(
        project=ProjectConfig(framework="vite-react", backend="supabase", auth="supabase"),
        entrypoints=[
            Entrypoint(
                name="fake",
                url=base_url,
                disposable=disposable,
                allow=[Permission.READ, Permission.WRITE] if disposable else [Permission.READ],
            )
        ],
        supabase=SupabaseConfig(url=base_url),
    )


@requires_browser
def test_probe_records_reachable_screens_and_their_queries():
    from testtrout.deployment.prober import probe

    with FakeApp() as app:
        result = probe(
            _scan(), _config(app.url, disposable=False), _config(app.url, False).entrypoints[0]
        )

    by_path = {s.path: s for s in result.screens}
    assert by_path["/orders"].reachable
    assert by_path["/orders"].title == "Orders"

    tables = {c.table for c in by_path["/orders"].calls if c.kind is CallKind.SUPABASE_REST}
    assert "orders" in tables


@requires_browser
def test_selectors_are_extracted_with_test_ids_preferred():
    from testtrout.deployment.prober import probe

    with FakeApp() as app:
        config = _config(app.url, disposable=False)
        result = probe(_scan(), config, config.entrypoints[0])

    screen = next(s for s in result.screens if s.path == "/orders")
    assert screen.selectors, "no selector candidates extracted"
    # test_id ranks first, so it must sort to the front.
    assert screen.selectors[0].strategy.value == "test_id"
    assert {c.value for c in screen.selectors if c.strategy.value == "test_id"} >= {
        "page-heading",
        "primary-action",
    }


@requires_browser
def test_writes_are_blocked_and_never_reach_a_non_disposable_deployment():
    """The safety guarantee, verified end to end rather than in isolation.

    /checkout writes on mount, which is exactly the case that makes "navigation
    is read-only" false.
    """
    from testtrout.deployment.prober import probe

    with FakeApp() as app:
        config = _config(app.url, disposable=False)
        result = probe(_scan(), config, config.entrypoints[0])
        assert WRITES_RECEIVED == [], "a write reached a non-disposable deployment"

    blocked = [c for c in result.all_calls() if c.blocked]
    assert blocked, "the write was never seen at all"
    assert all(c.method == "POST" for c in blocked)


@requires_browser
def test_writes_are_allowed_once_the_deployment_is_disposable():
    from testtrout.deployment.prober import probe

    with FakeApp() as app:
        config = _config(app.url, disposable=True)
        probe(_scan(), config, config.entrypoints[0])
        assert WRITES_RECEIVED, "the write was blocked even though the deployment is disposable"


@requires_browser
def test_route_parameters_are_resolved_from_a_harvested_row_id():
    """/orders/:id is unvisitable until a list screen yields a real id."""
    from testtrout.deployment.prober import probe

    with FakeApp() as app:
        config = _config(app.url, disposable=False)
        result = probe(_scan(), config, config.entrypoints[0])

    detail = next(s for s in result.screens if s.path == "/orders/:id")
    assert detail.reachable, detail.note
    assert detail.url.endswith("/orders/ord_1")


@requires_browser
def test_reconciliation_reports_policy_denials_and_undeclared_tables():
    from testtrout.deployment.prober import probe
    from testtrout.deployment.reconcile import reconcile

    scan = _scan()
    with FakeApp() as app:
        config = _config(app.url, disposable=False)
        result = probe(scan, config, config.entrypoints[0])

    codes = {d.code for d in reconcile(scan, result)}
    # /reports returns 403, standing in for a row-level security denial.
    assert "policy_denial" in codes
    # The scan declares no data operations at all, so every observed table is
    # unaccounted for.
    assert "undeclared_table" in codes
    assert "write_blocked" in codes
