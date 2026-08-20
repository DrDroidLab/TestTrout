"""Minimal ``.env`` loading.

Credentials are referenced from ``.trout/config.yaml`` as ``env:VAR`` and must
never be written into it, because that file is committed. That leaves the
question of where the values actually live during local development, and the
answer developers already expect is a gitignored ``.env`` beside the project.

Deliberately tiny and dependency-free. Existing environment variables always
win, so an explicit ``export`` or a CI secret overrides the file rather than
being silently replaced by it.
"""

from __future__ import annotations

import os
from pathlib import Path


def load(root: Path, filename: str = ".env") -> list[str]:
    """Load ``root/.env`` into ``os.environ`` without overwriting existing values.

    Returns:
        The names of the variables that were set, so ``qa doctor`` can show
        where a value came from. Never returns values.
    """
    path = root / filename
    if not path.is_file():
        return []

    applied: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        name, separator, value = line.partition("=")
        if not separator:
            continue
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if name and name not in os.environ:
            os.environ[name] = value
            applied.append(name)
    return applied
