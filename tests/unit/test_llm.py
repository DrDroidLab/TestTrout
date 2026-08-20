"""Gateway behaviour. No network: every test runs against cassettes or fakes."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from testtrout.domain.config import ModelConfig, ModelProvider
from testtrout.llm.base import CompletionRequest, CompletionResponse
from testtrout.llm.cassettes import CassetteMissError, CassetteStore
from testtrout.llm.gateway import Gateway, GatewayError, load_prompt


def test_temperature_is_unset_by_default():
    """Reasoning models reject a caller-chosen temperature.

    Claude models return 400 for the parameter at all; Moonshot's kimi-k3
    accepts only 1. A sensible-looking 0.0 default breaks both, so the default
    must stay None.
    """
    assert ModelConfig().temperature is None


def test_kimi_defaults_to_the_international_endpoint():
    from testtrout.llm.providers.openai_compat import KIMI_DEFAULT_BASE_URL, KIMI_DEFAULT_MODEL

    assert KIMI_DEFAULT_BASE_URL == "https://api.moonshot.ai/v1"
    assert KIMI_DEFAULT_MODEL == "kimi-k3"


def _record(
    store: CassetteStore,
    provider: str,
    model: str,
    request: CompletionRequest,
    response: CompletionResponse,
) -> None:
    store.save(store.key(provider, model, request), provider, model, request, response)


def test_replay_returns_the_recording_without_a_provider(tmp_path: Path):
    """CI has no API key. If this breaks, contribution gets expensive."""
    config = ModelConfig(provider=ModelProvider.KIMI, model="kimi-k3")
    gateway = Gateway(config, tmp_path)
    request = CompletionRequest(system="s", user="u", max_tokens=8192)
    _record(
        gateway.store,
        "kimi",
        "kimi-k3",
        request,
        CompletionResponse(text='{"ok": true}', model="kimi-k3", provider="kimi"),
    )

    response = gateway.complete(system="s", user="u")
    assert response.json() == {"ok": True}


def test_replay_mode_fails_loudly_on_a_miss(tmp_path: Path, monkeypatch):
    """A miss must never fall through to a real call during a test run."""
    monkeypatch.setenv("TROUT_CASSETTE_MODE", "replay")
    gateway = Gateway(ModelConfig(provider=ModelProvider.KIMI), tmp_path)
    with pytest.raises(CassetteMissError):
        gateway.complete(system="s", user="never recorded")


def test_cassette_key_separates_models(tmp_path: Path):
    """Replaying one model's answer for another would quietly corrupt a test."""
    store = CassetteStore(tmp_path)
    request = CompletionRequest(system="s", user="u")
    assert store.key("kimi", "kimi-k3", request) != store.key("kimi", "kimi-k2.6", request)
    assert store.key("kimi", "kimi-k3", request) != store.key("openai", "kimi-k3", request)


def test_cassette_records_the_request_for_auditing(tmp_path: Path):
    """The cache doubles as an audit log — a proposal's exact prompt is on disk."""
    store = CassetteStore(tmp_path)
    request = CompletionRequest(system="sys", user="usr")
    response = CompletionResponse(text="hi", model="m", provider="p")
    key = store.key("p", "m", request)
    store.save(key, "p", "m", request, response)

    payload = json.loads((tmp_path / f"{key}.json").read_text())
    assert payload["request"] == asdict(request)


def test_prompts_load_from_files_not_string_literals():
    text = load_prompt(
        "propose_scenarios",
        project="p",
        kind="k",
        surface="s",
        schema="sc",
        observed="o",
        intent="i",
    )
    assert "regression" in text.lower()


def test_missing_prompt_is_an_explicit_error():
    with pytest.raises(GatewayError, match="no prompt named"):
        load_prompt("does_not_exist")


def test_prompt_with_a_missing_variable_names_it():
    with pytest.raises(GatewayError, match="needs a value"):
        load_prompt("propose_scenarios", project="p")
