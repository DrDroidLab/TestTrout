"""The single entry point for model access.

Callers do not construct providers, read API keys, or handle cassettes. They
load a prompt, build a request, and call :meth:`Gateway.complete`. Keeping that
boundary tight is what makes it possible to say honestly that static analysis
never touches a model — there is exactly one door, and it is here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from testtrout.domain.config import ModelConfig, ModelProvider, resolve_secret
from testtrout.llm.base import CompletionRequest, CompletionResponse, Provider
from testtrout.llm.cassettes import CassetteMissError, CassetteMode, CassetteStore, current_mode

PROMPTS_DIR = Path(__file__).parent / "prompts"


class GatewayError(RuntimeError):
    """A model call could not be completed."""


def _hint(exc: Exception) -> str:
    """Append a fix for provider errors whose message does not suggest one.

    These are configuration mistakes rather than bugs, and the raw provider
    message sends people looking in the wrong place.
    """
    message = str(exc).lower()
    if "temperature" in message:
        return (
            "\n  hint: reasoning models restrict this parameter. Remove "
            "`temperature` from `model:` in .trout/config.yaml to use the "
            "provider's default."
        )
    if "authentication" in message or "api key" in message or "401" in message:
        return (
            "\n  hint: check the key and the endpoint match. Moonshot's .ai and "
            ".cn endpoints are separate account namespaces, and a key for one "
            "is rejected by the other."
        )
    return ""


def load_prompt(name: str, **variables: Any) -> str:
    """Load a prompt template from ``llm/prompts/`` and substitute variables.

    Prompts are files, not string literals in Python. That is deliberate: it
    lets someone improve a prompt through a readable diff without touching
    code, which is where most outside contributions to a tool like this will
    come from.

    Raises:
        GatewayError: if the named prompt does not exist, or a placeholder has
            no matching variable.
    """
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise GatewayError(f"no prompt named {name!r} in {PROMPTS_DIR}")
    template = path.read_text(encoding="utf-8")
    try:
        return template.format(**variables)
    except KeyError as exc:
        raise GatewayError(f"prompt {name!r} needs a value for {exc}") from exc


def _build_provider(config: ModelConfig) -> Provider:
    """Construct the configured provider."""
    api_key = resolve_secret(config.api_key)
    base_url = resolve_secret(config.base_url)

    if config.provider is ModelProvider.ANTHROPIC:
        from testtrout.llm.providers.anthropic_provider import AnthropicProvider

        return AnthropicProvider(api_key=api_key, model=config.model, base_url=base_url)

    if config.provider is ModelProvider.KIMI:
        from testtrout.llm.providers.openai_compat import KimiProvider

        return KimiProvider(api_key=api_key, model=config.model, base_url=base_url)

    from testtrout.llm.providers.openai_compat import OpenAICompatibleProvider

    return OpenAICompatibleProvider(api_key=api_key, model=config.model, base_url=base_url)


class Gateway:
    """Provider-agnostic model access with recording and replay."""

    def __init__(self, config: ModelConfig, cache_dir: Path) -> None:
        self.config = config
        self.store = CassetteStore(cache_dir / "cassettes")
        self._provider: Provider | None = None

    @property
    def provider(self) -> Provider:
        """The configured provider, constructed lazily.

        Lazy construction means a missing API key is only an error when a model
        is actually needed — ``trout scan`` never trips over it.
        """
        if self._provider is None:
            self._provider = _build_provider(self.config)
        return self._provider

    def complete(
        self,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        effort: str | None = None,
    ) -> CompletionResponse:
        """Run one completion, replaying from cassette when possible.

        Args:
            system: System prompt, normally from :func:`load_prompt`.
            user: The user turn.
            schema: JSON Schema to constrain the response. Strongly preferred
                over asking for JSON in the prompt.
            max_tokens: Overrides the configured ceiling.
            effort: Reasoning effort for providers that support it. Interactive
                commands should ask for less than the provider default, which
                is often the slowest setting.

        Raises:
            GatewayError: if the provider fails, or if a cassette is required
                and missing.
        """
        request = CompletionRequest(
            system=system,
            user=user,
            schema=schema,
            max_tokens=max_tokens or self.config.max_tokens,
            effort=effort or self.config.effort,
            # Only forwarded by providers that accept it; current Anthropic
            # models reject the parameter outright.
            temperature=self.config.temperature,
        )

        mode = current_mode()
        model_name = self.config.model or "default"
        key = self.store.key(self.config.provider.value, model_name, request)

        if mode in {CassetteMode.AUTO, CassetteMode.REPLAY}:
            recorded = self.store.load(key)
            if recorded is not None:
                return recorded
            if mode is CassetteMode.REPLAY:
                raise CassetteMissError(
                    f"no cassette for {self.config.provider.value}/{model_name} ({key}). "
                    "Record it with TROUT_CASSETTE_MODE=auto and a valid API key."
                )

        try:
            response = self.provider.complete(request)
        except Exception as exc:
            raise GatewayError(f"{self.config.provider.value} request failed: {exc}") from exc

        if mode is not CassetteMode.OFF:
            self.store.save(key, self.config.provider.value, model_name, request, response)
        return response
