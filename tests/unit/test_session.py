"""The pipeline end to end, against a real deployment.

These are the tests that would have caught the emitter bugs: a suite that only
exercises the planners can be entirely green while every generated test throws
before its first assertion.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fake_app import FakeApp
from testtrout.app import facts as fact_writer
from testtrout.app import session as pipeline
from testtrout.app.session import Session
from testtrout.store import QaPaths

FIXTURE = Path(__file__).resolve().parents[2] / "examples" / "lovable-shop"
TOOLCHAIN = Path(__file__).resolve().parents[1] / ".toolchain"


@pytest.fixture
def project(tmp_path: Path) -> QaPaths:
    destination = tmp_path / "app"
    shutil.copytree(FIXTURE, destination, ignore=shutil.ignore_patterns("node_modules"))
    (destination / ".git").mkdir()
    # Symlinked from a shared install rather than copied: the browser
    # toolchain is hundreds of megabytes. It lives outside the fixtures so the
    # examples stay exactly as authored — a scan of them is a golden test.
    installed = TOOLCHAIN / "node_modules"
    if installed.is_dir():
        (destination / "node_modules").symlink_to(installed, target_is_directory=True)
        # The runners must also be *declared*, because that is what the
        # toolchain check reads. Written into the copy, never the fixture.
        import json

        manifest = destination / "package.json"
        package = json.loads(manifest.read_text(encoding="utf-8"))
        package.setdefault("devDependencies", {}).update({"@playwright/test": "^1", "vitest": "^2"})
        manifest.write_text(json.dumps(package, indent=2), encoding="utf-8")
    return QaPaths(root=destination)


def test_one_fact_turns_nothing_into_something(project: QaPaths):
    """The shape of the whole product: a URL is all it takes to start."""
    session = Session(paths=project)

    _, before = pipeline.refresh(session)
    assert before.ready == []

    with FakeApp() as app:
        fact_writer.apply(project, {"deployment_url": app.url})
        sheet, after = pipeline.refresh(session, rescan=False)

    assert after.ready
    assert "deployment_url" not in [f.id for f in sheet.outstanding]


def test_what_is_asked_for_shrinks_as_answers_arrive(project: QaPaths):
    session = Session(paths=project)
    pipeline.refresh(session)
    first = len(session.facts.outstanding)

    with FakeApp() as app:
        fact_writer.apply(project, {"deployment_url": app.url})
        pipeline.refresh(session, rescan=False)

    assert len(session.facts.outstanding) < first


def test_a_secret_never_reaches_committed_configuration(project: QaPaths):
    """The one rule about storage that is not negotiable."""
    with FakeApp() as app:
        fact_writer.apply(
            project,
            {
                "deployment_url": app.url,
                "account_primary": {"email": "a@b.test", "password": "hunter2"},
            },
        )

    committed = project.config.read_text(encoding="utf-8")
    assert "hunter2" not in committed
    assert "a@b.test" not in committed
    assert "env:TROUT_OWNER_PASSWORD" in committed
    assert "hunter2" in (project.root / ".env").read_text(encoding="utf-8")


@pytest.mark.slow
def test_the_baseline_is_written_proven_and_catches_a_change(project: QaPaths):
    """The end-to-end property. Needs a browser, so it is marked slow."""
    import fake_app

    session = Session(paths=project)
    with FakeApp() as app:
        fact_writer.apply(project, {"deployment_url": app.url})
        pipeline.refresh(session)
        outcome = pipeline.build(session, limit=3)

    if not outcome["kept"]:
        pytest.skip("no browser toolchain in this project")
    assert outcome["held"] == 0

    from testtrout.authoring.store import load_all
    from testtrout.domain.config import Config
    from testtrout.runtime.runner import run as execute
    from testtrout.store import read_model

    original = fake_app.ROUTES["/login"]
    fake_app.ROUTES["/login"] = ("Renamed", original[1], original[2])
    try:
        index, _ = load_all(project.scenarios)
        with FakeApp() as app:
            fact_writer.apply(project, {"deployment_url": app.url})
            config = read_model(project.config, Config)
            record = execute(
                config, config.entrypoint(), index, project.root, report_dir=project.runs / "x"
            )
    finally:
        fake_app.ROUTES["/login"] = original

    changed = [r for r in record.results if r.classification.is_product_signal]
    assert len(changed) == 1
    assert "/login" in (changed[0].title or "")
