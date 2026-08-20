"""Screens, Route Handlers, and Server Actions for the Next.js App Router.

App Router routing is filesystem-based, so screens come from directory layout
rather than from parsing JSX. The two surfaces worth special attention are:

``route.ts`` handlers
    Ordinary HTTP endpoints, detected from their exported method names.

Server Actions
    Functions marked ``'use server'``. These are the easiest surface in the
    whole stack to miss, because they look like a local function call at the
    call site while actually being an unauthenticated-by-default RPC endpoint.
    They carry an endpoint's risk with a helper's visibility, so they are
    enumerated separately rather than folded in with endpoints.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from testtrout.analysis.detect import ProjectContext
from testtrout.analysis.ids import IdAllocator
from testtrout.analysis.parser import SourceFile, find_all
from testtrout.domain.surface import Endpoint, ScanWarning, Screen, ServerAction, SourceLocation

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


class NextAppRouterAdapter:
    """Framework adapter for Next.js applications using the App Router."""

    id: ClassVar[str] = "nextjs-app"

    def matches(self, context: ProjectContext) -> bool:
        """Match projects with an ``app/`` directory and a Next dependency."""
        return context.info.framework == "nextjs-app"

    def screens(
        self,
        files: dict[str, SourceFile],
        context: ProjectContext,
        allocator: IdAllocator,
    ) -> tuple[list[Screen], list[ScanWarning]]:
        """Derive routes from ``page.tsx`` file locations."""
        app_dir = _app_dir(context.root)
        if app_dir is None:
            return [], [
                ScanWarning(
                    code="no_app_dir",
                    message="Next.js project detected but no app/ directory was found.",
                )
            ]
        screens: list[Screen] = []
        for rel, file in sorted(files.items()):
            if file.path.name not in {"page.tsx", "page.ts", "page.jsx", "page.js"}:
                continue
            if not file.path.is_relative_to(app_dir):
                continue
            path = _route_path(file.path.parent.relative_to(app_dir))
            component = _default_export_name(file) or file.path.parent.name
            screens.append(
                Screen(
                    id=allocator.allocate(f"screen:{path}", component),
                    location=SourceLocation(file=rel, line=1, symbol=component),
                    path=path,
                    component=component,
                    params=[p.lstrip(":") for p in path.split("/") if p.startswith(":")],
                    layout=rel,
                )
            )
        return screens, []

    def endpoints(
        self,
        files: dict[str, SourceFile],
        context: ProjectContext,
        allocator: IdAllocator,
    ) -> list[Endpoint]:
        """Find ``route.ts`` handlers and the HTTP methods they export."""
        app_dir = _app_dir(context.root)
        if app_dir is None:
            return []
        endpoints: list[Endpoint] = []
        for rel, file in sorted(files.items()):
            if file.path.name not in {"route.ts", "route.tsx", "route.js"}:
                continue
            if not file.path.is_relative_to(app_dir):
                continue
            exported = _exported_names(file)
            methods = [m for m in HTTP_METHODS if m in exported]
            path = _route_path(file.path.parent.relative_to(app_dir))
            endpoints.append(
                Endpoint(
                    id=allocator.allocate(f"endpoint:{path}"),
                    location=SourceLocation(file=rel, line=1),
                    path=path,
                    methods=methods or ["GET"],
                    runtime=_declared_runtime(file),
                )
            )
        return endpoints

    def server_actions(
        self,
        files: dict[str, SourceFile],
        context: ProjectContext,
        allocator: IdAllocator,
    ) -> list[ServerAction]:
        """Find exported functions marked ``'use server'``."""
        actions: list[ServerAction] = []
        for rel, file in sorted(files.items()):
            head = file.source[:200].decode("utf-8", errors="replace")
            module_level = "'use server'" in head or '"use server"' in head
            source_text = file.source.decode("utf-8", errors="replace")
            if not module_level and "use server" not in source_text:
                continue
            for name in sorted(_exported_names(file)):
                if name in HTTP_METHODS:
                    continue
                actions.append(
                    ServerAction(
                        id=allocator.allocate(f"action:{name}", rel),
                        location=SourceLocation(file=rel, line=1, symbol=name),
                        name=name,
                        module=rel,
                    )
                )
        return actions


def _app_dir(root: Path) -> Path | None:
    """Locate the App Router directory, which may be at the root or under src/."""
    for candidate in (root / "app", root / "src" / "app"):
        if candidate.is_dir():
            return candidate
    return None


def _route_path(relative: Path) -> str:
    """Convert an App Router directory path into a URL pattern.

    Route groups ``(marketing)`` are removed because they do not appear in the
    URL. Dynamic segments become ``:param`` so that every framework reports
    paths in one notation.
    """
    segments: list[str] = []
    for part in relative.parts:
        if part in {".", ""} or (part.startswith("(") and part.endswith(")")):
            continue
        if part.startswith("@"):  # parallel route slot, not part of the URL
            continue
        if part.startswith("[") and part.endswith("]"):
            inner = part[1:-1]
            if inner.startswith("..."):
                segments.append(f":{inner[3:]}*")
            elif inner.startswith("[") and inner.endswith("]"):
                segments.append(f":{inner[1:-1]}?")
            else:
                segments.append(f":{inner}")
            continue
        segments.append(part)
    return "/" + "/".join(segments) if segments else "/"


def _exported_names(file: SourceFile) -> set[str]:
    """Names exported by a module, covering functions, consts, and re-exports."""
    names: set[str] = set()
    for node in find_all(file.root, "export_statement"):
        declaration = node.child_by_field_name("declaration")
        if declaration is not None:
            named = declaration.child_by_field_name("name")
            if named is not None:
                names.add(file.text(named))
            for declarator in find_all(declaration, "variable_declarator"):
                name = declarator.child_by_field_name("name")
                if name is not None and name.type == "identifier":
                    names.add(file.text(name))
        for specifier in find_all(node, "export_specifier"):
            alias = specifier.child_by_field_name("alias") or specifier.child_by_field_name("name")
            if alias is not None:
                names.add(file.text(alias))
    return names


def _default_export_name(file: SourceFile) -> str | None:
    """Name of the default-exported component, when it has one."""
    for node in find_all(file.root, "export_statement"):
        if "default" not in file.text(node)[:40]:
            continue
        declaration = node.child_by_field_name("declaration")
        if declaration is None:
            continue
        named = declaration.child_by_field_name("name")
        if named is not None:
            return file.text(named)
    return None


def _declared_runtime(file: SourceFile) -> str | None:
    """Read an ``export const runtime = 'edge'`` declaration, if present."""
    text = file.source.decode("utf-8", errors="replace")
    for runtime in ("edge", "nodejs"):
        if f"runtime = '{runtime}'" in text or f'runtime = "{runtime}"' in text:
            return runtime
    return None
