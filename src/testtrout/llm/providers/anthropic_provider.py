"""Anthropic provider, using the official SDK.

Two details here are easy to get wrong and expensive to debug:

*No ``temperature``.* Current Claude models reject the parameter with a 400.
The gateway therefore treats temperature as opt-in per provider, and this one
never forwards it.

*Structured output goes through ``output_config``*, not a prompt instruction.
Asking a model to "reply with JSON only" and then parsing the result is how you
get intermittent failures at 2am; constraining the format at the API level is
not.
"""

from __future__ import annotations

from typing import Any, ClassVar

from testtrout.llm.base import CompletionRequest, CompletionResponse

# Claude Opus 5. Overridable per repository via .trout/config.yaml.
DEFAULT_MODEL = "claude-opus-5"


class AnthropicProvider:
    """Completion backed by the Anthropic Messages API."""

    name: ClassVar[str] = "anthropic"
    default_model: ClassVar[str] = DEFAULT_MODEL

    def __init__(
        self, api_key: str | None = None, model: str | None = None, base_url: str | None = None
    ) -> None:
        import anthropic

        # Passing None lets the SDK fall back to its own resolution chain
        # (ANTHROPIC_API_KEY, then an `ant auth login` profile), which is what a
        # user who has already authenticated expects.
        self._client = anthropic.Anthropic(api_key=api_key, base_url=base_url)
        self._model = model or DEFAULT_MODEL

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Run one completion, optionally constrained to a JSON schema."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.user}],
        }
        if request.schema is not None:
            kwargs["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": request.schema,
                }
            }

        message = self._client.messages.create(**kwargs)

        text = "".join(block.text for block in message.content if block.type == "text")
        return CompletionResponse(
            text=text,
            model=self._model,
            provider=self.name,
            input_tokens=getattr(message.usage, "input_tokens", None),
            output_tokens=getattr(message.usage, "output_tokens", None),
        )
