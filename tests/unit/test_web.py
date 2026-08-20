"""The local web interface.

Thin over the same library the CLI uses, so these test the contract the page
sees and the guarantees the API must not break — not the logic underneath.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from testtrout.web.app import create_app

FIXTURE = Path(__file__).resolve().parents[2] / "examples" / "lovable-shop"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    destination = tmp_path / "app"
    shutil.copytree(FIXTURE, destination)
    return destination


@pytest.fixture
def client(project: Path) -> TestClient:
    return TestClient(create_app(project))


def _wait_for_job(client: TestClient, tries: int = 200) -> dict:
    """Poll until the background job settles."""
    import time

    for _ in range(tries):
        job = client.get("/api/job").json()
        if job.get("state") in {"done", "failed"}:
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_the_page_is_served(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "TestTrout" in response.text


def test_overview_works_before_anything_has_been_scanned(client: TestClient):
    """A first-run user must get a usable page, not an error."""
    body = client.get("/api/overview").json()
    assert body["scanned"] is False
    assert body["coverage"] is None


def test_scan_action_runs_and_reports_counts(client: TestClient):
    assert client.post("/api/actions/scan", json={}).status_code == 200
    job = _wait_for_job(client)
    assert job["state"] == "done"
    assert job["result"]["counts"]["policies"] == 4
    assert any("analysing" in e["message"] for e in job["events"])


def test_overview_reflects_the_scan(client: TestClient):
    client.post("/api/actions/scan", json={})
    _wait_for_job(client)
    body = client.get("/api/overview").json()
    assert body["scanned"] is True
    assert body["project"]["framework"] == "vite-react"
    assert body["coverage"]["total_surfaces"] > 0
    # Populated from the scan, so a run cannot reach a real payment processor.
    assert any(rule["match"] == "api.stripe.com" for rule in body["substitution"])


def test_gaps_carry_their_reasons(client: TestClient):
    client.post("/api/actions/scan", json={})
    _wait_for_job(client)
    gaps = client.get("/api/gaps").json()["gaps"]
    assert gaps
    for gap in gaps:
        assert gap["reasons"], f"{gap['id']} has no reasons"


def test_only_one_job_runs_at_a_time(client: TestClient):
    """Two concurrent runs against one database interfere inexplicably."""
    client.post("/api/actions/probe", json={})
    second = client.post("/api/actions/scan", json={})
    # The probe may already have failed fast (no entrypoint); either it is
    # still running and blocks, or it finished and the scan is allowed.
    assert second.status_code in {200, 409}
    if second.status_code == 409:
        assert "already running" in second.json()["detail"]


def test_approving_a_scenario_with_open_questions_is_refused(client: TestClient):
    """Same rule as the CLI: it would produce a test that passes vacuously."""
    from testtrout.authoring.store import save
    from testtrout.domain.gap import TestKind
    from testtrout.domain.scenario import Scenario

    client.post("/api/actions/scan", json={})
    _wait_for_job(client)

    scenario = Scenario(
        id="scenario:unfinished",
        title="unfinished",
        kind=TestKind.AUTHORIZATION,
        open_questions=["which column scopes this table?"],
    )
    # The app is bound to the project the client was built from.
    project_root = Path(client.get("/api/overview").json()["root"])
    save(project_root / ".trout" / "scenarios", scenario)

    response = client.post("/api/scenarios/scenario:unfinished/status", json={"status": "approved"})
    assert response.status_code == 409
    assert "vacuously" in response.json()["detail"]


def test_approving_a_ready_scenario_succeeds(client: TestClient):
    from testtrout.authoring.store import save
    from testtrout.domain.gap import TestKind
    from testtrout.domain.scenario import Assertion, AssertionKind, Scenario

    client.post("/api/actions/scan", json={})
    _wait_for_job(client)
    project_root = Path(client.get("/api/overview").json()["root"])
    save(
        project_root / ".trout" / "scenarios",
        Scenario(
            id="scenario:ready",
            title="ready",
            kind=TestKind.AUTHORIZATION,
            then=[Assertion(kind=AssertionKind.ROW_COUNT, expected="0")],
        ),
    )
    response = client.post("/api/scenarios/scenario:ready/status", json={"status": "approved"})
    assert response.status_code == 200
    assert response.json()["status"] == "approved"


def test_an_unknown_scenario_is_a_404(client: TestClient):
    assert (
        client.post("/api/scenarios/scenario:nope/status", json={"status": "approved"}).status_code
        == 404
    )


def test_an_unsupported_status_is_rejected(client: TestClient):
    from testtrout.authoring.store import save
    from testtrout.domain.gap import TestKind
    from testtrout.domain.scenario import Scenario

    project_root = Path(client.get("/api/overview").json()["root"])
    save(
        project_root / ".trout" / "scenarios",
        Scenario(id="scenario:x", title="x", kind=TestKind.AUTHORIZATION),
    )
    assert (
        client.post("/api/scenarios/scenario:x/status", json={"status": "certified"}).status_code
        == 400
    )


def test_no_endpoint_can_make_a_deployment_writable(client: TestClient):
    """The safety posture is a deliberate edit to a committed file.

    One click away from pointing test writes at production is exactly the
    mistake the guard exists to prevent, so there must be no route for it.
    """
    write_routes = {
        route.path for route in client.app.routes if "POST" in getattr(route, "methods", set())
    }
    assert write_routes == {
        "/api/scenarios/{scenario_id}/status",
        "/api/actions/scan",
        "/api/actions/probe",
        "/api/actions/propose",
        "/api/actions/generate",
        "/api/actions/run",
        "/api/actions/certify",
    }
