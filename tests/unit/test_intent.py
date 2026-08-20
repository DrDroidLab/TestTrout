"""Intent capture. The model is faked; what is tested is the validation around it."""

from __future__ import annotations

import json
from pathlib import Path

from testtrout.domain.config import ModelConfig, ModelProvider
from testtrout.domain.intent import Provenance
from testtrout.domain.surface import (
    Criticality,
    DataOperation,
    Operation,
    ProjectInfo,
    ScanResult,
    Screen,
    SourceLocation,
)
from testtrout.llm.base import CompletionRequest, CompletionResponse
from testtrout.llm.gateway import Gateway
from testtrout.planning import intent as planner

LOCATION = SourceLocation(file="src/x.tsx", line=1)

SCAN = ScanResult(
    project=ProjectInfo(root=".", framework="vite-react", backend="supabase"),
    screens=[Screen(id="screen:orders", location=LOCATION, path="/orders", component="Orders")],
    data_operations=[
        DataOperation(
            id="data:orders.select", location=LOCATION, table="orders", operation=Operation.SELECT
        )
    ],
)


def _gateway(tmp_path: Path, payload: dict[str, object]) -> Gateway:
    """A gateway pre-loaded with one recorded response, so no network is used."""
    config = ModelConfig(provider=ModelProvider.KIMI, model="kimi-k3")
    gateway = Gateway(config, tmp_path)
    request = CompletionRequest(
        system=planner.load_prompt("draft_intent"),
        user=planner._context_block(SCAN, None),
        schema=planner.INTENT_SCHEMA,
        max_tokens=config.max_tokens,
        effort=planner.DEFAULT_EFFORT,
    )
    gateway.store.save(
        gateway.store.key("kimi", "kimi-k3", request),
        "kimi",
        "kimi-k3",
        request,
        CompletionResponse(text=json.dumps(payload), model="kimi-k3", provider="kimi"),
    )
    return gateway


def test_invented_surface_ids_are_dropped_and_reported(tmp_path: Path):
    """A model that invents an id would attach a journey to nothing.

    Silently keeping it would produce a journey that appears to cover a
    surface and covers none, which is exactly the false confidence this tool
    exists to prevent.
    """
    gateway = _gateway(
        tmp_path,
        {
            "summary": "A shop",
            "audience": "customers",
            "journeys": [
                {
                    "name": "Browse orders",
                    "description": "",
                    "steps": ["open /orders"],
                    "criticality": "high",
                    "roles": ["owner"],
                    "surface_ids": ["screen:orders", "screen:does-not-exist"],
                    "consequence": "nobody sees their orders",
                }
            ],
            "never_break": [],
            "open_questions": [],
        },
    )
    captured, warnings = planner.draft(gateway, SCAN)

    assert captured.journeys[0].surfaces == ["screen:orders"]
    assert any("do not exist" in w for w in warnings)


def test_drafted_intent_is_marked_inferred(tmp_path: Path):
    """Inferred intent must never be mistaken for something the developer said."""
    gateway = _gateway(
        tmp_path,
        {
            "summary": "s",
            "audience": "a",
            "journeys": [
                {
                    "name": "J",
                    "description": "",
                    "steps": [],
                    "criticality": "high",
                    "roles": [],
                    "surface_ids": [],
                    "consequence": "",
                }
            ],
            "never_break": [],
            "open_questions": [],
        },
    )
    captured, _ = planner.draft(gateway, SCAN)
    assert captured.journeys[0].provenance is Provenance.INFERRED
    assert not captured.journeys[0].provenance.is_evidence


def test_open_questions_survive_and_start_unanswered(tmp_path: Path):
    gateway = _gateway(
        tmp_path,
        {
            "summary": "s",
            "audience": "a",
            "journeys": [],
            "never_break": [],
            "open_questions": [
                {
                    "question": "What is audit_log for?",
                    "context": "no writes found",
                    "surface_id": None,
                }
            ],
        },
    )
    captured, warnings = planner.draft(gateway, SCAN)
    assert len(captured.unanswered) == 1
    assert captured.open_questions[0].question == "What is audit_log for?"
    assert any("no journeys" in w for w in warnings)


def test_an_unusable_response_degrades_rather_than_crashes(tmp_path: Path):
    gateway = _gateway(tmp_path, {})
    captured, _ = planner.draft(gateway, SCAN)
    assert captured.journeys == []


def test_missing_criticality_defaults_high_not_low(tmp_path: Path):
    """Guessing low would quietly bury a surface. Guessing high only costs noise."""
    gateway = _gateway(
        tmp_path,
        {
            "summary": "s",
            "audience": "a",
            "journeys": [
                {
                    "name": "J",
                    "description": "",
                    "steps": [],
                    "criticality": "nonsense",
                    "roles": [],
                    "surface_ids": [],
                    "consequence": "",
                }
            ],
            "never_break": [],
            "open_questions": [],
        },
    )
    captured, _ = planner.draft(gateway, SCAN)
    assert captured.journeys[0].criticality is Criticality.HIGH


def test_context_block_lists_surfaces_by_id():
    """The model can only reference ids it was shown."""
    block = planner._context_block(SCAN, None)
    assert "screen:orders" in block
    assert "data:orders.select" in block
