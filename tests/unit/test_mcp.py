"""The MCP server.

Thin by design — it calls the same library functions the CLI does — so these
tests check the contract an agent sees rather than re-testing the logic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mcp.server.mcpserver")

from testtrout.mcp.server import build_server

FIXTURE = Path(__file__).resolve().parents[2] / "examples" / "lovable-shop"


def _payload(result: object) -> dict:
    """Unwrap a CallToolResult into the JSON an agent would parse."""
    content = getattr(result, "content", None)
    assert content, f"no content in {result!r}"
    return json.loads(content[0].text)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A throwaway copy of the fixture app, so tests never touch the real one."""
    import shutil

    destination = tmp_path / "app"
    shutil.copytree(FIXTURE, destination)
    return destination


async def test_every_capability_is_exposed(project: Path):
    server = build_server(project)
    names = {t.name for t in await server.list_tools()}
    assert names == {
        "scan",
        "surfaces",
        "gaps",
        "intent",
        "probe",
        "propose",
        "scenarios",
        "approve",
        "generate",
        "doctor",
        "run",
        "certify",
        "report",
    }


async def test_state_is_available_as_resources(project: Path):
    """Bulk data lives in resources so tool results stay small.

    A tool result lands in an agent's context window; returning a whole surface
    map would crowd out the reasoning it was meant to support.
    """
    server = build_server(project)
    uris = {str(r.uri) for r in await server.list_resources()}
    assert uris == {"trout://surfaces", "trout://config", "trout://intent", "trout://scenarios"}


async def test_tools_report_a_missing_scan_with_a_fix(project: Path):
    """An error an agent can act on beats a stack trace."""
    server = build_server(project)
    result = _payload(await server.call_tool("gaps", {}))
    assert result["error"] == "no scan found"
    assert "scan" in result["fix"]


async def test_scan_then_gaps_works_end_to_end(project: Path):
    server = build_server(project)

    scanned = _payload(await server.call_tool("scan", {}))
    assert scanned["framework"] == "vite-react"
    assert scanned["counts"]["policies"] == 4

    gaps = _payload(await server.call_tool("gaps", {"kind": "authorization"}))
    assert gaps["gaps"]
    for gap in gaps["gaps"]:
        assert gap["reasons"], "every gap must explain its own rank"


async def test_authorization_gaps_are_blocked_without_two_test_users(project: Path):
    server = build_server(project)
    await server.call_tool("scan", {})
    gaps = _payload(await server.call_tool("gaps", {"kind": "authorization"}))
    assert all(not g["ready"] for g in gaps["gaps"])
    assert any(b["code"] == "needs_two_roles" for g in gaps["gaps"] for b in g["blockers"])


async def test_approve_refuses_a_scenario_with_open_questions(project: Path):
    """Approval is the user's decision, and a vacuous test is not a favour."""
    server = build_server(project)
    await server.call_tool("scan", {})

    from testtrout.authoring.store import save
    from testtrout.domain.gap import Gap, TestKind
    from testtrout.domain.scenario import Scenario

    scenario = Scenario(
        id="scenario:needs-answers",
        title="unfinished",
        kind=TestKind.AUTHORIZATION,
        open_questions=["which column scopes this table?"],
    )
    save(project / ".trout" / "scenarios", scenario)

    result = _payload(
        await server.call_tool("approve", {"scenario_ids": ["scenario:needs-answers"]})
    )
    assert result["changed"] == []
    assert result["refused"][0]["id"] == "scenario:needs-answers"
    assert Gap  # keep the import meaningful for readers


async def test_generate_without_approval_explains_what_to_do(project: Path):
    server = build_server(project)
    await server.call_tool("scan", {})
    result = _payload(await server.call_tool("generate", {}))
    assert "no approved scenarios" in result["error"]
    assert "approve" in result["fix"]


async def test_doctor_reports_whether_authorization_tests_are_possible(project: Path):
    server = build_server(project)
    result = _payload(await server.call_tool("doctor", {}))
    assert result["authorization_tests_possible"] is False
    assert result["test_users"] == []
