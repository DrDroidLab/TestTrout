"""The scan orchestrator: repository in, :class:`ScanResult` out.

This is the entry point behind ``trout scan``. It is deliberately the only place
that knows the *order* things happen in; each extractor stays independent and
testable on its own.

Determinism is a hard requirement. Files are visited in sorted order, ids are
allocated from durable properties, and no step consults a model or the network.
The same commit must always produce a byte-identical result, because the
golden-file tests and the stability of surface ids both depend on it.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

from testtrout.analysis import (
    criticality,
    externals,
    http_calls,
    requirements,
    supabase_ops,
)
from testtrout.analysis.detect import ProjectContext, detect_project, find_app_root
from testtrout.analysis.frameworks.base import FrameworkAdapter
from testtrout.analysis.ids import IdAllocator
from testtrout.analysis.modules import DEFAULT_MAX_DEPTH, build_graph
from testtrout.analysis.parser import SourceFile, parse_file
from testtrout.domain.surface import ScanResult, ScanWarning

ENTRY_POINT_GROUP = "testtrout.frameworks"


def load_adapters() -> list[FrameworkAdapter]:
    """Load every registered framework adapter.

    Third-party adapters are discovered through the ``testtrout.frameworks``
    entry point group, so support for another framework can ship as a separate
    package. A broken adapter is skipped rather than crashing the scan.
    """
    adapters: list[FrameworkAdapter] = []
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            adapters.append(entry.load()())
        except Exception:
            continue
    return adapters


def _select_adapter(context: ProjectContext) -> FrameworkAdapter | None:
    """Pick the adapter that claims this project."""
    return next((a for a in load_adapters() if a.matches(context)), None)


def _parse_all(context: ProjectContext) -> tuple[dict[str, SourceFile], list[ScanWarning]]:
    """Parse every first-party source file, recording anything unreadable."""
    files: dict[str, SourceFile] = {}
    warnings: list[ScanWarning] = []
    for path in context.source_files():
        parsed = parse_file(path, context.root)
        if parsed is None:
            warnings.append(
                ScanWarning(
                    code="unreadable_file",
                    message=f"Could not read {path.relative_to(context.root).as_posix()}",
                )
            )
            continue
        files[parsed.rel] = parsed
    return files, warnings


def _attribute_reachability(
    result: ScanResult, files: dict[str, SourceFile], context: ProjectContext, max_depth: int
) -> None:
    """Fill in ``Screen.reaches`` using the module import graph.

    A screen "reaches" a data operation when the operation's file is within
    ``max_depth`` import hops of the screen's component module. This is an
    over-approximation — importing a module does not prove the call runs — but
    it is the right bias here: a false positive costs one unnecessary proposed
    test, while a false negative leaves a user-triggerable operation with no
    test at all.
    """
    graph = build_graph(files, context.root, context.aliases)
    by_file: dict[str, list[str]] = {}
    # Both kinds of backend call count. An app with its own HTTP API has no
    # Supabase call sites, and without this every one of its screens would
    # reach nothing and score low.
    for operation in result.data_operations:
        by_file.setdefault(operation.location.file, []).append(operation.id)
    for endpoint in result.endpoints:
        by_file.setdefault(endpoint.location.file, []).append(endpoint.id)

    for screen in result.screens:
        module = screen.layout or screen.location.file
        reachable = graph.reachable(module, max_depth=max_depth)
        reached: list[str] = []
        for candidate in sorted(reachable):
            reached.extend(by_file.get(candidate, []))
        screen.reaches = sorted(set(reached))


def scan(root: Path, max_depth: int = DEFAULT_MAX_DEPTH) -> ScanResult:
    """Analyse a repository and return its complete surface map.

    Args:
        root: Repository root. Must exist.
        max_depth: How many import hops to follow when attributing data
            operations to screens.

    Returns:
        A fully scored :class:`ScanResult`. Never raises for ordinary
        malformed input — problems are reported as warnings so that a partial
        map is still useful.
    """
    root = root.resolve()

    # A repository whose frontend lives in a subdirectory would otherwise scan
    # to nothing: no package.json at the root means no framework, no screens.
    app_root = find_app_root(root)
    context = detect_project(app_root or root)
    if app_root is not None:
        context.info.detected_from.append(f"app found in {app_root.relative_to(root).as_posix()}/")
    files, warnings = _parse_all(context)
    allocator = IdAllocator()

    result = ScanResult(project=context.info, warnings=warnings)

    adapter = _select_adapter(context)
    if adapter is None:
        result.warnings.append(
            ScanWarning(
                code="no_framework_adapter",
                message=(
                    f"No adapter matched framework {context.info.framework!r}. "
                    "Screens and endpoints will be missing; data operations and "
                    "policies are still scanned."
                ),
            )
        )
    else:
        screens, screen_warnings = adapter.screens(files, context, allocator)
        result.screens = screens
        result.warnings.extend(screen_warnings)
        result.endpoints = adapter.endpoints(files, context, allocator)
        result.server_actions = adapter.server_actions(files, context, allocator)

    # An app with its own HTTP backend has no Supabase call sites; its `fetch`
    # calls are the backend surface. Additive, so a Next.js app keeps the route
    # handlers its adapter found and gains the calls its client makes.
    api_calls, api_warnings = http_calls.discover(files, allocator)
    result.endpoints.extend(api_calls)
    result.warnings.extend(api_warnings)

    seen_vendors: set[str] = set()
    for _, file in sorted(files.items()):
        operations, op_warnings = supabase_ops.extract(file, allocator)
        result.data_operations.extend(operations)
        result.warnings.extend(op_warnings)
        result.externals.extend(externals.extract(file, allocator, seen_vendors))

    if context.info.backend == "supabase":
        from testtrout.analysis.sql import parse_migrations

        tables, policies = parse_migrations(root / "supabase" / "migrations", root, allocator)
        result.tables = tables
        result.policies = policies
        _warn_unprotected_tables(result)

    result.requirements = requirements.discover(result, files)
    _attribute_reachability(result, files, context, max_depth)
    return criticality.apply(result)


def _warn_unprotected_tables(result: ScanResult) -> None:
    """Flag tables that are written to but have no row-level security.

    In this stack the database is reachable from the browser with the anon key,
    so a table without RLS is readable and writable by anyone who opens the
    developer console. That is worth saying loudly during a scan, before any
    test is written.
    """
    written = {op.table for op in result.data_operations if op.table and op.operation.writes}
    protected = {t.name for t in result.tables if t.rls_enabled}
    for table in sorted(written - protected):
        if not any(t.name == table for t in result.tables):
            continue
        result.warnings.append(
            ScanWarning(
                code="table_without_rls",
                message=(
                    f"Table {table!r} is written to from client code but has no row-level "
                    "security enabled. With the anon key exposed in the browser, this data "
                    "is reachable by anyone."
                ),
            )
        )
