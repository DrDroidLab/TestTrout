"""The API behind the conversation and its sidebar."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
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
    worker = Worker(database)
    while (job := worker.queue.claim()) is not None:
        worker.execute(job)


def test_the_page_is_served(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "TestTrout" in response.text


def test_adding_a_project_queues_the_first_look(tmp_path: Path, database: Database):
    other = tmp_path / "second"
    shutil.copytree(FIXTURE, other)
    (other / ".git").mkdir()
    api = TestClient(create_app(database=database))

    response = api.post("/api/repos", json={"source": "local", "path": str(other)})
    assert response.status_code == 200
    assert [j["kind"] for j in api.get("/api/jobs").json()["jobs"]] == ["understand"]


def test_a_deployment_url_lands_before_the_first_look(tmp_path: Path, database: Database):
    """Otherwise the first look can only read code, and is wasted."""
    other = tmp_path / "third"
    shutil.copytree(FIXTURE, other)
    (other / ".git").mkdir()
    api = TestClient(create_app(database=database))

    created = api.post(
        "/api/repos",
        json={"source": "local", "path": str(other), "deployment_url": "https://example.test"},
    ).json()

    config = api.get(f"/api/repos/{created['id']}/config").json()["config"]
    assert [e["url"] for e in config["entrypoints"]] == ["https://example.test"]
    assert config["entrypoints"][0]["disposable"] is False


def test_the_sidebar_lists_four_artifacts_from_the_start(client: TestClient):
    """Even before anything exists, so the shape of the work is visible."""
    body = client.get("/api/projects/1/artifacts").json()

    assert [a["kind"] for a in body["artifacts"]] == ["map", "facts", "plan", "suite"]
    assert all(a["label"] and a["icon"] for a in body["artifacts"])
    assert not any(a["ready"] for a in body["artifacts"])


def test_looking_fills_in_the_map_and_the_plan(client: TestClient, database: Database):
    assert client.post("/api/repos/1/jobs", json={"kind": "understand"}).status_code == 200
    drain(database)

    job = client.get("/api/jobs").json()["jobs"][0]
    assert job["state"] == "done"
    assert job["result"]["pages"] > 0

    project_map = client.get("/api/projects/1/map").json()
    assert project_map["ready"] is True
    assert project_map["pages"]
    assert project_map["storage"]["tables"]

    ready = {
        a["kind"] for a in client.get("/api/projects/1/artifacts").json()["artifacts"] if a["ready"]
    }
    assert {"map", "facts", "plan"} <= ready


def test_the_form_asks_only_for_concrete_values(client: TestClient, database: Database):
    client.post("/api/repos/1/jobs", json={"kind": "understand"})
    drain(database)

    outstanding = client.get("/api/projects/1/facts").json()["outstanding"]
    assert outstanding
    for fact in outstanding:
        assert fact["kind"] in {"url", "account", "secret", "sample", "command"}
        assert fact["why"]


def test_saving_a_partial_answer_is_accepted_and_re_derives(client: TestClient, database: Database):
    """Partial input is the normal case, not an error state."""
    client.post("/api/repos/1/jobs", json={"kind": "understand"})
    drain(database)

    response = client.post("/api/projects/1/facts", json={"deployment_url": "https://example.test"})
    assert response.status_code == 200
    assert response.json()["applied"] == ["deployment_url"]

    assert "deployment_url" not in [
        f["id"] for f in client.get("/api/projects/1/facts").json()["outstanding"]
    ]


def test_a_value_that_is_not_a_url_is_refused_with_a_reason(client: TestClient):
    response = client.post("/api/projects/1/facts", json={"deployment_url": "my-app"})

    assert response.status_code == 400
    assert "https://" in response.json()["detail"]


def test_the_plan_says_what_each_blocked_item_needs(client: TestClient, database: Database):
    client.post("/api/repos/1/jobs", json={"kind": "understand"})
    drain(database)

    plan = client.get("/api/projects/1/plan").json()
    assert plan["counts"]["ready"] == 0  # nothing observed without a deployment
    assert plan["waiting"]
    assert all(item["needs_labels"] for item in plan["waiting"])


def test_the_suite_is_empty_until_something_is_built(client: TestClient):
    assert client.get("/api/projects/1/suite").json()["tests"] == []
