"""A module import graph, used to decide which screens reach which data.

Knowing that ``/orders`` renders ``OrdersPage`` is only half a surface map. The
useful question is which Supabase operations a user can trigger by visiting
``/orders`` — and those calls usually live several components deep, not in the
route file. This module resolves local imports so that reachability can be
computed by a bounded walk.

Only first-party modules are resolved. Following into ``node_modules`` would
explode the graph and add nothing: a regression in a third-party package is not
something this tool can generate a meaningful test for.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from testtrout.analysis.parser import SourceFile, find_all

_INDEX_NAMES = ("index.ts", "index.tsx", "index.js", "index.jsx")
_SUFFIXES = (".ts", ".tsx", ".js", ".jsx")

# How far to follow imports when attributing data operations to a screen.
# Three hops covers page -> section -> component, which is where the calls in
# these codebases actually live. Deeper walks mostly add shared UI primitives
# and blur the attribution rather than improving it.
DEFAULT_MAX_DEPTH = 3


@dataclass
class ModuleGraph:
    """Directed graph of first-party module imports, keyed by relative path."""

    edges: dict[str, set[str]] = field(default_factory=dict)

    def add(self, source: str, target: str) -> None:
        """Record that ``source`` imports ``target``."""
        self.edges.setdefault(source, set()).add(target)

    def reachable(self, start: str, max_depth: int = DEFAULT_MAX_DEPTH) -> set[str]:
        """Modules reachable from ``start`` within ``max_depth`` hops, inclusive."""
        seen = {start}
        queue: deque[tuple[str, int]] = deque([(start, 0)])
        while queue:
            module, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for target in self.edges.get(module, ()):
                if target not in seen:
                    seen.add(target)
                    queue.append((target, depth + 1))
        return seen


def import_specifiers(file: SourceFile) -> list[str]:
    """Every module specifier imported by a file, including dynamic imports."""
    out: list[str] = []
    for node in find_all(file.root, "import_statement", "export_statement"):
        source = node.child_by_field_name("source")
        if source is not None:
            text = file.text(source)
            out.append(text[1:-1] if len(text) >= 2 else text)
    for call in find_all(file.root, "call_expression"):
        function = call.child_by_field_name("function")
        if function is None or file.text(function) != "import":
            continue
        arguments = call.child_by_field_name("arguments")
        if arguments and arguments.named_children:
            text = file.text(arguments.named_children[0])
            if text[:1] in {'"', "'"}:
                out.append(text[1:-1])
    return out


def resolve_specifier(
    specifier: str, importer: Path, root: Path, aliases: dict[str, list[str]]
) -> str | None:
    """Resolve an import specifier to a repository-relative module path.

    Handles relative imports and tsconfig-style aliases, and appends the
    extension that the TypeScript resolver would infer. Returns ``None`` for
    bare package imports, which are deliberately out of scope.
    """
    candidates: list[Path] = []

    if specifier.startswith("."):
        candidates.append((importer.parent / specifier).resolve())
    else:
        for pattern, targets in aliases.items():
            prefix = pattern.removesuffix("*")
            if not specifier.startswith(prefix):
                continue
            remainder = specifier[len(prefix) :]
            candidates.extend((root / target.removesuffix("*") / remainder) for target in targets)

    for candidate in candidates:
        for resolved in _existing_variants(candidate):
            try:
                return resolved.relative_to(root).as_posix()
            except ValueError:
                continue
    return None


def _existing_variants(candidate: Path) -> list[Path]:
    """Paths TypeScript would try for a given specifier, in resolution order."""
    if candidate.is_file():
        return [candidate]
    found = [
        candidate.with_suffix(suffix)
        for suffix in _SUFFIXES
        if candidate.with_suffix(suffix).is_file()
    ]
    found.extend(candidate / name for name in _INDEX_NAMES if (candidate / name).is_file())
    return found


def build_graph(
    files: dict[str, SourceFile], root: Path, aliases: dict[str, list[str]]
) -> ModuleGraph:
    """Build the import graph for a set of parsed files."""
    graph = ModuleGraph()
    for rel, file in files.items():
        for specifier in import_specifiers(file):
            target = resolve_specifier(specifier, file.path, root, aliases)
            if target is not None and target != rel:
                graph.add(rel, target)
    return graph
