"""Shared fixtures.

The fixture applications under ``examples/`` are the only realistic input this
suite sees — the tool is written in Python and analyses TypeScript, so there is
nothing to dogfood. Keep them realistic.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def lovable_shop() -> Path:
    """A Lovable-style React + Vite + Supabase application."""
    return REPO_ROOT / "examples" / "lovable-shop"


@pytest.fixture(scope="session")
def scanned(lovable_shop: Path):
    """Scan result for the fixture app, computed once per session."""
    from testtrout.analysis.scanner import scan

    return scan(lovable_shop)
