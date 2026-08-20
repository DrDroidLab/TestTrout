"""Finding tests that already exist, so the tool does not propose duplicates.

This is a heuristic and says so. It looks for test files and checks whether
they mention a table name or a route path, which is enough to notice that
somebody already wrote a checkout test and not enough to know whether that test
is any good.

Being wrong in the safe direction matters here. A false negative costs one
redundant proposal that a human rejects in a second. A false positive means a
critical surface is silently marked protected when nothing protects it, which
is exactly the false confidence this tool exists to prevent. So a match is
reported as *possible* coverage, never as coverage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from testtrout.analysis.detect import IGNORED_DIRS
from testtrout.domain.surface import ScanResult

TEST_FILE_PATTERN = re.compile(r"\.(test|spec)\.[jt]sx?$")
TEST_DIRS = frozenset({"tests", "test", "__tests__", "e2e", "cypress", "playwright"})

# Long enough that an incidental match is unlikely. "id" or "user" would match
# half the codebase and mark everything covered.
MIN_TOKEN_LENGTH = 4


@dataclass
class ExistingTests:
    """Which surfaces are possibly already covered, and by what."""

    files: list[str] = field(default_factory=list)
    by_surface: dict[str, list[str]] = field(default_factory=dict)

    @property
    def found_any(self) -> bool:
        """Whether the project has any tests at all."""
        return bool(self.files)

    def covers(self, surface_id: str) -> bool:
        """Whether a surface has at least one possible test."""
        return bool(self.by_surface.get(surface_id))


def _test_files(root: Path) -> list[Path]:
    """Every file that looks like a test."""
    found: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in {".ts", ".tsx", ".js", ".jsx"}:
            continue
        parts = path.relative_to(root).parts
        if any(part in IGNORED_DIRS for part in parts):
            continue
        if TEST_FILE_PATTERN.search(path.name) or any(part in TEST_DIRS for part in parts[:-1]):
            found.append(path)
    return sorted(found)


def _mentions_table(text: str, table: str | None) -> bool:
    """Whether a test file references a table by name, as a quoted literal.

    Requiring quotes avoids matching a table name that happens to appear inside
    an unrelated identifier, and the length floor keeps short names like "id"
    from marking half the codebase as covered.
    """
    if not table or len(table) < MIN_TOKEN_LENGTH:
        return False
    return f"'{table}'" in text or f'"{table}"' in text


def detect(root: Path, scan: ScanResult) -> ExistingTests:
    """Find existing tests and guess which surfaces they touch."""
    result = ExistingTests()
    files = _test_files(root)
    if not files:
        return result

    contents: dict[str, str] = {}
    for path in files:
        rel = path.relative_to(root).as_posix()
        result.files.append(rel)
        try:
            contents[rel] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

    for rel, text in contents.items():
        for screen in scan.screens:
            # Match the literal route path; a bare "/" would match everything.
            if len(screen.path) > 1 and screen.path.split(":")[0].rstrip("/") in text:
                result.by_surface.setdefault(screen.id, []).append(rel)
        for operation in scan.data_operations:
            if _mentions_table(text, operation.table):
                result.by_surface.setdefault(operation.id, []).append(rel)
        for policy in scan.policies:
            if policy.name and policy.name in text:
                result.by_surface.setdefault(policy.id, []).append(rel)

    return result
