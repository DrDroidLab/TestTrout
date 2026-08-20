"""Source locations.

Its own module because both :mod:`testtrout.domain.surface` and
:mod:`testtrout.domain.requirements` need it, and having either import the
other creates a cycle. A primitive shared by two peers belongs beneath both.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class SourceLocation(BaseModel):
    """A file and line range, used to trace every finding back to real code."""

    model_config = ConfigDict(frozen=True)

    file: str = Field(description="Repository-relative POSIX path.")
    line: int = Field(ge=1, description="1-indexed start line.")
    end_line: int | None = Field(default=None, ge=1)
    symbol: str | None = Field(
        default=None, description="Enclosing function or component name, when resolvable."
    )

    def __str__(self) -> str:
        """Render as ``file:line``, which most terminals make clickable."""
        return f"{self.file}:{self.line}"
