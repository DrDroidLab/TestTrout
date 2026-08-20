"""Golden-file tests: the scan output for the fixture apps is committed.

The point is review, not assertion. Any change in extraction shows up as a diff
in a pull request, where a human decides whether it is an improvement or a
regression. That only works because the scan is deterministic — see
``docs/adr/0003-deterministic-core.md``.

Accept an intentional change with::

    QA_UPDATE_GOLDEN=1 pytest tests/golden

and then read the diff before committing it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from testtrout.analysis.scanner import scan
from testtrout.store.yaml_io import dumps

GOLDEN_DIR = Path(__file__).parent / "expected"
EXAMPLES = ["lovable-shop"]


def _normalise(text: str, root: Path) -> str:
    """Strip the absolute project root so goldens are machine-independent."""
    return text.replace(str(root), "<root>")


@pytest.mark.parametrize("example", EXAMPLES)
def test_scan_matches_golden(example: str) -> None:
    root = Path(__file__).resolve().parents[2] / "examples" / example
    actual = _normalise(dumps(scan(root)), root)
    golden = GOLDEN_DIR / f"{example}.yaml"

    if os.environ.get("QA_UPDATE_GOLDEN"):
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(actual, encoding="utf-8")
        pytest.skip(f"updated golden for {example}")

    assert golden.is_file(), (
        f"no golden for {example}. Create it with QA_UPDATE_GOLDEN=1 pytest tests/golden"
    )
    assert actual == golden.read_text(encoding="utf-8")


@pytest.mark.parametrize("example", EXAMPLES)
def test_scan_is_deterministic(example: str) -> None:
    """Two scans of the same tree must be byte-identical.

    Everything downstream depends on this: stable surface ids, meaningful
    goldens, and coverage records that survive a re-scan.
    """
    root = Path(__file__).resolve().parents[2] / "examples" / example
    assert dumps(scan(root)) == dumps(scan(root))
