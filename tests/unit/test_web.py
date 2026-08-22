"""The local web interface.

Thin over the same library the CLI uses, so these test the contract the page
sees and the guarantees the API must not break — not the logic underneath.

The API only *enqueues* work; a worker executes it. Tests that want the effect
drain the queue synchronously rather than starting a thread, which keeps them
deterministic.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from testtrout.app import Database, RepoRegistry
from testtrout.app.worker import Worker
from testtrout.web.app import create_app

FIXTURE = Path(__file__).resolve().parents[2] / "examples" / "lovable-shop"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    destination = tmp_path / "app"
    shutil.copytree(FIXTURE, destination)
    (destination / ".git").mkdir()
    return destination


@pytest.fixture
def database(tmp_path: Path) -> Database:
    return Database(tmp_path / "app.db")


@pytest.fixture
def client(project: Path, database: Database) -> TestClient:
    RepoRegistry(database).link_local(project)
    return TestClient(create_app(database=database))


def drain(database: Database) -> None:
    """Execute everything queued, synchronously."""
    worker = Worker(database)
    while (job := worker.queue.claim()) is not None:
        worker.execute(job)


def test_the_page_is_served(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "TestTrout" in response.text


def test_repos_are_listed(client: TestClient):
    body = client.get("/api/repos").json()
    assert len(body["repos"]) == 1
    assert body["repos"][0]["exists"] is True
    assert body["database"].endswith(".db")


def test_linking_a_repository_queues_a_scan(tmp_path: Path, database: Database):
    """A freshly linked repository is useless until scanned."""
    other = tmp_path / "second"
    shutil.copytree(FIXTURE, other)
    (other / ".git").mkdir()
    api = TestClient(create_app(database=database))

    response = api.post("/api/repos", json={"source": "local", "path": str(other)})
    assert response.status_code == 200

    jobs = api.get("/api/jobs").json()["jobs"]
    assert [j["kind"] for j in jobs] == ["scan"]


def test_a_repository_that_moved_is_a_404(client: TestClient, project: Path):
    shutil.rmtree(project)
    response = client.get("/api/repos/1/overview")
    assert response.status_code == 404
    assert "no longer exists" in response.json()["detail"]


def test_overview_works_before_anything_is_scanned(client: TestClient):
    body = client.get("/api/repos/1/overview").json()
    assert body["scanned"] is False
    assert body["coverage"] is None


def test_a_queued_scan_runs_and_updates_the_overview(client: TestClient, database: Database):
    assert client.post("/api/repos/1/jobs", json={"kind": "scan"}).status_code == 200
    drain(database)

    job = client.get("/api/jobs").json()["jobs"][0]
    assert job["state"] == "done"
    assert job["result"]["counts"]["policies"] == 4

    body = client.get("/api/repos/1/overview").json()
    assert body["scanned"] is True
    assert body["project"]["framework"] == "vite-react"
    # Populated by the scan so a run cannot reach a real payment processor.
    assert any(rule["match"] == "api.stripe.com" for rule in body["substitution"])


def test_gaps_carry_their_reasons(client: TestClient, database: Database):
    client.post("/api/repos/1/jobs", json={"kind": "scan"})
    drain(database)

    gaps = client.get("/api/repos/1/gaps").json()["gaps"]
    assert gaps
    for gap in gaps:
        assert gap["reasons"], f"{gap['id']} has no reasons"


def test_an_unknown_job_kind_is_rejected(client: TestClient):
    response = client.post("/api/repos/1/jobs", json={"kind": "nonsense"})
    assert response.status_code == 400
    assert "unknown job kind" in response.json()["detail"]


def test_approving_a_scenario_with_open_questions_is_refused(client: TestClient, project: Path):
    """Same rule as the CLI: it would produce a test that passes vacuously."""
    from testtrout.authoring.store import save
    from testtrout.domain.gap import TestKind
    from testtrout.domain.scenario import Scenario

    save(
        project / ".trout" / "scenarios",
        Scenario(
            id="scenario:unfinished",
            title="unfinished",
            kind=TestKind.AUTHORIZATION,
            open_questions=["which column scopes this table?"],
        ),
    )
    response = client.post(
        "/api/repos/1/scenarios/scenario:unfinished/status", json={"status": "approved"}
    )
    assert response.status_code == 409
    assert "vacuously" in response.json()["detail"]


def test_approving_a_ready_scenario_succeeds(client: TestClient, project: Path):
    from testtrout.authoring.store import save
    from testtrout.domain.gap import TestKind
    from testtrout.domain.scenario import Assertion, AssertionKind, Scenario

    save(
        project / ".trout" / "scenarios",
        Scenario(
            id="scenario:ready",
            title="ready",
            kind=TestKind.AUTHORIZATION,
            then=[Assertion(kind=AssertionKind.ROW_COUNT, expected="0")],
        ),
    )
    response = client.post(
        "/api/repos/1/scenarios/scenario:ready/status", json={"status": "approved"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "approved"


def test_an_unknown_scenario_is_a_404(client: TestClient):
    assert (
        client.post(
            "/api/repos/1/scenarios/scenario:nope/status", json={"status": "approved"}
        ).status_code
        == 404
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
        "/api/repos",
        "/api/repos/{repo_id}/jobs",
        "/api/repos/{repo_id}/scenarios/{scenario_id}/status",
        "/api/repos/{repo_id}/secrets",
        "/api/repos/{repo_id}/questions/{question_id}",
        "/api/jobs/{job_id}/cancel",
    }


def test_making_a_deployment_writable_requires_explicit_confirmation(client: TestClient):
    """The one setting that can destroy real data must never be incidental."""
    unconfirmed = client.put(
        "/api/repos/1/config",
        json={"entrypoints": [{"name": "prod", "url": "https://x.dev", "disposable": True}]},
    )
    assert unconfirmed.status_code == 400
    assert "Confirm that explicitly" in unconfirmed.json()["detail"]

    confirmed = client.put(
        "/api/repos/1/config",
        json={
            "entrypoints": [
                {
                    "name": "prod",
                    "url": "https://x.dev",
                    "disposable": True,
                    "confirm_disposable": True,
                }
            ]
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["config"]["entrypoints"][0]["disposable"] is True


def test_configuration_never_accepts_a_literal_secret(client: TestClient):
    """A password typed into a config field must be refused, not committed."""
    response = client.put(
        "/api/repos/1/config",
        json={"supabase": {"anon_key_var": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.secret"}},
    )
    assert response.status_code == 400
    assert "environment variable name" in response.json()["detail"]


def test_secrets_go_to_a_gitignored_env_file(client: TestClient, project: Path):
    """Writing credentials into a repo without ignoring them would be harmful."""
    response = client.post("/api/repos/1/secrets", json={"SUPABASE_ANON_KEY": "anon-value-here"})
    assert response.status_code == 200
    assert response.json()["written"] == ["SUPABASE_ANON_KEY"]

    env = (project / ".env").read_text()
    assert "SUPABASE_ANON_KEY=anon-value-here" in env
    assert ".env" in (project / ".gitignore").read_text()

    # Values are never read back out.
    view = client.get("/api/repos/1/config").json()
    assert view["env_present"].get("SUPABASE_ANON_KEY") is True
    assert "anon-value-here" not in str(view)


def test_readiness_degrades_rather_than_refusing(client: TestClient):
    """A partial set of credentials gives a partial suite, not an error."""
    client.put(
        "/api/repos/1/config",
        json={"entrypoints": [{"name": "p", "url": "https://x.dev"}]},
    )
    view = client.get("/api/repos/1/config").json()
    by_name = {r["capability"]: r for r in view["readiness"]}

    assert by_name["api_tests"]["ready"] is True
    assert by_name["authorization_tests"]["ready"] is False
    assert by_name["authorization_tests"]["next_step"]


def test_a_project_is_added_with_its_deployment_in_one_step(tmp_path: Path, database: Database):
    """The URL must be on file *before* the first scan is queued.

    A scan without a deployment can only read code. With one it can also check
    what the running system does, and that is the difference between a guess
    and evidence — so asking for the URL afterwards would waste the first scan.
    """
    other = tmp_path / "second"
    shutil.copytree(FIXTURE, other)
    (other / ".git").mkdir()
    api = TestClient(create_app(database=database))

    response = api.post(
        "/api/repos",
        json={"source": "local", "path": str(other), "deployment_url": "https://example.test"},
    )
    assert response.status_code == 200
    repo_id = response.json()["id"]

    config = api.get(f"/api/repos/{repo_id}/config").json()["config"]
    assert [e["url"] for e in config["entrypoints"]] == ["https://example.test"]
    # Read-only until the user says otherwise, however it was added.
    assert config["entrypoints"][0]["disposable"] is False
    assert [j["kind"] for j in api.get("/api/jobs").json()["jobs"]] == ["scan"]


def test_a_project_added_without_a_deployment_still_links(tmp_path: Path, database: Database):
    """A URL is worth asking for, not worth blocking on."""
    other = tmp_path / "third"
    shutil.copytree(FIXTURE, other)
    (other / ".git").mkdir()
    api = TestClient(create_app(database=database))

    response = api.post("/api/repos", json={"source": "local", "path": str(other)})
    assert response.status_code == 200
    config = api.get(f"/api/repos/{response.json()['id']}/config").json()["config"]
    assert config["entrypoints"] == []


def test_the_project_account_is_empty_before_a_scan(client: TestClient):
    assert client.get("/api/repos/1/project").json() == {"scanned": False}


def test_the_project_account_describes_the_product(client: TestClient, database: Database):
    assert client.post("/api/repos/1/jobs", json={"kind": "scan"}).status_code == 200
    drain(database)

    body = client.get("/api/repos/1/project").json()
    assert body["scanned"] is True
    assert body["summary"]
    assert body["pages"]
    assert all(page["how_to_test"] for page in body["pages"])

    coverage = body["coverage"]
    assert coverage["overall_percent"] == 0
    assert coverage["pages_total"] == len(body["pages"])


def test_a_scan_says_what_is_left_to_test(client: TestClient, database: Database):
    """The answer to "I scanned again — now what?"."""
    assert client.post("/api/repos/1/jobs", json={"kind": "scan"}).status_code == 200
    drain(database)

    body = client.get("/api/repos/1/project").json()
    missing = body["delta"]["still_missing"]
    assert len(missing) == len(body["pages"]) + len(body["apis"]) + len(body["transactions"])
    assert body["delta"]["gone"] == []
