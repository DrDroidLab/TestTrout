"""Recording and replaying model calls.

Every request and response is written to ``.trout/.cache/cassettes/`` keyed by a
hash of the request. This buys three things that matter for an open-source
tool:

*Contributors need no API key.* The project's own tests replay recorded
cassettes, so ``pytest`` passes offline and CI does not need a secret.

*Runs are auditable.* When a proposed scenario looks wrong, the exact prompt
and the exact response are on disk next to it.

*Iteration is cheap.* Re-running a command after fixing downstream code does
not pay for the same completion twice.

Modes are chosen with ``TROUT_CASSETTE_MODE``: ``auto`` (default — replay a hit,
otherwise call and record), ``replay`` (never call; a miss is an error), or
``off``.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from enum import StrEnum
from pathlib import Path

from testtrout.llm.base import CompletionRequest, CompletionResponse


class CassetteMode(StrEnum):
    """How the cassette layer behaves for this process."""

    AUTO = "auto"
    """Replay when a recording exists, otherwise call the provider and record."""
    REPLAY = "replay"
    """Never call a provider. A miss raises, which is what CI wants."""
    OFF = "off"
    """Always call the provider and record nothing."""


class CassetteMissError(RuntimeError):
    """Raised in ``replay`` mode when no recording matches the request."""


def current_mode() -> CassetteMode:
    """Read the mode from the environment, defaulting to ``auto``."""
    raw = os.environ.get("TROUT_CASSETTE_MODE", CassetteMode.AUTO.value).lower()
    try:
        return CassetteMode(raw)
    except ValueError:
        return CassetteMode.AUTO


class CassetteStore:
    """Content-addressed storage for model calls."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def key(self, provider: str, model: str, request: CompletionRequest) -> str:
        """Stable hash identifying a request.

        The provider and model are part of the key because the same prompt
        produces materially different output across models, and replaying one
        model's response for another would quietly invalidate a test.
        """
        digest = hashlib.sha256()
        for part in (provider, model, *request.cache_key_parts()):
            digest.update(part.encode("utf-8"))
            digest.update(b"\x00")
        return digest.hexdigest()[:24]

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def load(self, key: str) -> CompletionResponse | None:
        """Return a recorded response, or ``None`` if absent or unreadable."""
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return CompletionResponse(**payload["response"])

    def save(
        self,
        key: str,
        provider: str,
        model: str,
        request: CompletionRequest,
        response: CompletionResponse,
    ) -> None:
        """Record a call.

        The request is stored alongside the response purely for human
        inspection — lookup uses the key. Storing it is what turns the cache
        directory into an audit log.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = {
            "provider": provider,
            "model": model,
            "request": asdict(request),
            "response": asdict(response),
        }
        self._path(key).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
