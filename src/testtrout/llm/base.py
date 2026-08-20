"""Provider-agnostic types for model completion.

The interface is deliberately small: one text-in, text-or-JSON-out call. This
tool does not need streaming, multi-turn state, or tool use from the model — it
needs a proposal in a known shape. Keeping the surface narrow is what makes a
new provider a fifty-line file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol, runtime_checkable


@dataclass(frozen=True)
class CompletionRequest:
    """One model call.

    Attributes:
        system: System prompt. Loaded from ``llm/prompts/``, never inlined.
        user: The user turn.
        schema: JSON Schema the response must satisfy. When set, providers use
            their native structured-output mechanism, and the gateway returns a
            parsed object rather than text.
        max_tokens: Output ceiling.
        temperature: Only applied by providers that accept it. Current
            Anthropic models reject the parameter outright, so it is opt-in per
            provider rather than passed blindly.
    """

    system: str
    user: str
    schema: dict[str, Any] | None = None
    max_tokens: int = 8192
    effort: str | None = None
    """Reasoning effort. Only forwarded by providers that accept it."""
    temperature: float | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def cache_key_parts(self) -> tuple[str, ...]:
        """Stable components identifying this request for cassette lookup."""
        import json

        return (
            self.system,
            self.user,
            json.dumps(self.schema, sort_keys=True) if self.schema else "",
            str(self.max_tokens),
            self.effort or "",
        )


@dataclass(frozen=True)
class CompletionResponse:
    """The result of a model call."""

    text: str
    model: str
    provider: str
    input_tokens: int | None = None
    output_tokens: int | None = None

    def json(self) -> Any:
        """Parse the response as JSON.

        Raises:
            ValueError: if the response is not valid JSON. Callers should treat
                this as a provider failure and surface it rather than retrying
                silently — a malformed structured response usually means the
                schema was too complex, and that is worth seeing.
        """
        import json as _json

        try:
            return _json.loads(self.text)
        except _json.JSONDecodeError as exc:
            raise ValueError(f"provider {self.provider} returned non-JSON output: {exc}") from exc


@runtime_checkable
class Provider(Protocol):
    """A model backend.

    Implementations live in ``testtrout/llm/providers/``. Adding one means
    implementing this protocol and registering it in
    :data:`testtrout.llm.gateway.PROVIDERS`.
    """

    name: ClassVar[str]
    default_model: ClassVar[str]

    def complete(self, request: CompletionRequest) -> CompletionResponse:
        """Run one completion. May raise provider-specific exceptions."""
        ...
