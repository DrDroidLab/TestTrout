"""OpenAI and any OpenAI-compatible endpoint, including Moonshot Kimi.

One implementation covers three cases because the wire format is the same; only
the base URL and default model differ. That is also why this is the right place
to plug in a self-hosted or foundry-style deployment — set ``base_url`` and the
rest works unchanged.
"""

from __future__ import annotations

from typing import Any, ClassVar

from testtrout.llm.base import CompletionRequest, CompletionResponse

OPENAI_DEFAULT_MODEL = "gpt-4o"

# Moonshot's international endpoint. The .cn endpoint is a separate account
# namespace — a key issued for one is rejected by the other, which produces a
# confusing "Invalid Authentication" rather than a routing error. Override
# base_url in config for the .cn region, a self-hosted deployment, or a foundry.
KIMI_DEFAULT_BASE_URL = "https://api.moonshot.ai/v1"

# kimi-k3: 1M context, reasoning, tool support. The scenario proposer feeds it a
# whole surface map plus schema, so context length is the binding constraint.
KIMI_DEFAULT_MODEL = "kimi-k3"

# Checked in order. MOONSHOT_API_KEY is what Moonshot's own docs use; KIMI_API_KEY
# is accepted because the model, not the vendor, is what users think in terms of.
KIMI_KEY_ENV_VARS = ("MOONSHOT_API_KEY", "KIMI_API_KEY")


class OpenAICompatibleProvider:
    """Completion backed by any endpoint speaking the OpenAI chat format."""

    name: ClassVar[str] = "openai"
    default_model: ClassVar[str] = OPENAI_DEFAULT_MODEL

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        provider_name: str | None = None,
    ) -> None:
        from openai import OpenAI

        # None defers to the SDK's own environment lookup.
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model or self.default_model
        self._name = provider_name or self.name

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Run one completion, optionally constrained to a JSON schema."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
        }
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.effort is not None:
            # Moonshot's reasoning models default to "max", which is accurate
            # and slow. Endpoints that do not know the parameter ignore it.
            kwargs["reasoning_effort"] = request.effort
        if request.schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "strict": True,
                    "schema": request.schema,
                },
            }

        completion = self._client.chat.completions.create(**kwargs)
        choice = completion.choices[0]
        usage = completion.usage

        return CompletionResponse(
            text=choice.message.content or "",
            model=self._model,
            provider=self._name,
            input_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            output_tokens=getattr(usage, "completion_tokens", None) if usage else None,
        )


class KimiProvider(OpenAICompatibleProvider):
    """Moonshot Kimi, which speaks the OpenAI format at a different base URL."""

    name: ClassVar[str] = "kimi"
    default_model: ClassVar[str] = KIMI_DEFAULT_MODEL

    def __init__(
        self, api_key: str | None = None, model: str | None = None, base_url: str | None = None
    ) -> None:
        super().__init__(
            api_key=api_key or _kimi_key_from_env(),
            model=model or KIMI_DEFAULT_MODEL,
            base_url=base_url or KIMI_DEFAULT_BASE_URL,
            provider_name="kimi",
        )


def _kimi_key_from_env() -> str | None:
    """Find a Moonshot key in the environment.

    Without this the OpenAI SDK would fall back to ``OPENAI_API_KEY`` and send
    an OpenAI key to Moonshot, which fails with an authentication error that
    says nothing about the actual mistake.
    """
    import os

    return next((v for name in KIMI_KEY_ENV_VARS if (v := os.environ.get(name))), None)
