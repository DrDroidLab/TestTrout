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
        """Parse the response as JSON, tolerating the ways models wrap it.

        Even with a JSON schema requested, some endpoints return the object
        inside a ``` fence, and at least one does so only sometimes — which is
        worse than always, because it turns into an intermittent failure that
        looks like a bad prompt.

        Raises:
            ValueError: if nothing parseable is present. Callers should surface
                this rather than retry silently: a genuinely malformed
                structured response usually means the schema was too complex,
                and that is worth seeing.
        """
        import json as _json

        for candidate in _json_candidates(self.text):
            try:
                return _json.loads(candidate)
            except _json.JSONDecodeError:
                continue
        raise ValueError(
            f"provider {self.provider} returned no parseable JSON "
            f"({len(self.text)} chars, starts {self.text[:60]!r})"
        )


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


def _json_candidates(text: str) -> list[str]:
    """Progressively looser readings of a response, strictest first.

    1. The text as given — the common and correct case.
    2. The contents of a fenced code block, which several providers add even
       when a JSON schema was requested.
    3. The outermost balanced ``{...}``, for a model that wrote a sentence
       before or after the object.

    Ordering matters: a valid response must never be reinterpreted by a looser
    rule, so each fallback is only reached when the previous one failed to
    parse.
    """
    stripped = text.strip()
    candidates = [stripped]

    if stripped.startswith("```"):
        without_fence = stripped.split("\n", 1)[-1] if "\n" in stripped else ""
        candidates.append(without_fence.removesuffix("```").strip())

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])

    return [c for c in candidates if c]
