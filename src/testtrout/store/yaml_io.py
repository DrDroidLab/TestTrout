"""YAML serialisation for domain models.

Two properties matter more than convenience here:

*Stable output.* Keys are written in model-declaration order rather than
alphabetically, and defaults are preserved, so re-running a scan produces a
diff only where something actually changed. A file that churns on every run is
a file nobody reviews.

*Readable output.* These files are meant to be opened, understood, and edited
by hand, so block style is forced and enums are written as their plain values.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)

_HEADER = (
    "# Managed by testtrout. Safe to edit by hand — the tool reads what you write.\n"
    "# Regenerate with `trout look`.\n"
)


class _BlockDumper(yaml.SafeDumper):
    """Dumper that keeps nested structures in readable block style."""

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        """Indent list items under their key, which reads better in review."""
        super().increase_indent(flow=flow, indentless=False)


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    """Use literal block style for multi-line strings instead of escapes."""
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_BlockDumper.add_representer(str, _represent_str)


def dumps(model: BaseModel, *, exclude_none: bool = True) -> str:
    """Serialise a model to a YAML string."""
    payload: dict[str, Any] = model.model_dump(mode="json", exclude_none=exclude_none)
    return yaml.dump(
        payload,
        Dumper=_BlockDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=100,
    )


def write_model(path: Path, model: BaseModel, *, header: bool = True) -> None:
    """Write a model to disk, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = dumps(model)
    path.write_text((_HEADER if header else "") + body, encoding="utf-8")


def read_model(path: Path, model_type: type[ModelT]) -> ModelT:
    """Read and validate a model from disk.

    Raises:
        FileNotFoundError: if the file does not exist. Callers are expected to
            check first and give a command-specific message.
        pydantic.ValidationError: if the file has drifted from the schema.
    """
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return model_type.model_validate(raw)
