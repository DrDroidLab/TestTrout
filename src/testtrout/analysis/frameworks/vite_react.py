"""Screens for React SPAs built with Vite — the Lovable, v0, and Bolt output shape.

Routing in these applications is declared in JSX, most often as
``<Route path="/orders/:id" element={<OrderDetail />} />`` inside a
``<Routes>`` block. A minority use ``createBrowserRouter`` with an array of
route objects; both are handled.

The component reference matters as much as the path. Resolving
``<OrderDetail />`` back to the module that defines it is what lets the scanner
attribute data operations to the screen a user would have to visit to trigger
them.
"""

from __future__ import annotations

from typing import ClassVar

from testtrout.analysis.detect import ProjectContext
from testtrout.analysis.ids import IdAllocator
from testtrout.analysis.modules import resolve_specifier
from testtrout.analysis.parser import SourceFile, find_all, jsx_attributes, string_value
from testtrout.domain.surface import Endpoint, ScanWarning, Screen, ServerAction, SourceLocation

# Route props that carry a lazily-referenced component.
_ELEMENT_PROPS = frozenset({"element", "Component", "component", "lazy"})


class ViteReactAdapter:
    """Framework adapter for Vite + React Router applications."""

    id: ClassVar[str] = "vite-react"

    def matches(self, context: ProjectContext) -> bool:
        """Match Vite-style SPAs, which is also the fallback for bare React."""
        return context.info.framework == "vite-react"

    def screens(
        self,
        files: dict[str, SourceFile],
        context: ProjectContext,
        allocator: IdAllocator,
    ) -> tuple[list[Screen], list[ScanWarning]]:
        """Find every declared route across the project."""
        screens: list[Screen] = []
        warnings: list[ScanWarning] = []
        for file in files.values():
            if file.path.suffix not in {".tsx", ".jsx"}:
                continue
            bindings = _import_bindings(file, context)
            screens.extend(self._jsx_routes(file, bindings, allocator))
            screens.extend(self._object_routes(file, bindings, allocator))
        if not screens and any(f.endswith((".tsx", ".jsx")) for f in files):
            warnings.append(
                ScanWarning(
                    code="no_routes_found",
                    message=(
                        "No React Router routes were found. If this app routes some other way, "
                        "screens will be missing from the surface map — run 'qa probe' to "
                        "discover them from the running deployment instead."
                    ),
                )
            )
        return screens, warnings

    def endpoints(self, files, context, allocator) -> list[Endpoint]:  # type: ignore[no-untyped-def]
        """Vite SPAs have no first-party HTTP endpoints of their own."""
        return []

    def server_actions(self, files, context, allocator) -> list[ServerAction]:  # type: ignore[no-untyped-def]
        """Not applicable to this framework."""
        return []

    def _jsx_routes(
        self, file: SourceFile, bindings: dict[str, str], allocator: IdAllocator
    ) -> list[Screen]:
        """Parse ``<Route path=... element=... />`` declarations."""
        screens: list[Screen] = []
        for element in find_all(file.root, "jsx_self_closing_element", "jsx_opening_element"):
            name_node = element.child_by_field_name("name")
            if name_node is None or file.text(name_node) != "Route":
                continue
            attributes = jsx_attributes(element, file)
            if "path" not in attributes:
                continue
            path = _attribute_string(attributes["path"], file)
            if path is None:
                continue
            component = next(
                (
                    _component_name(attributes[prop], file)
                    for prop in _ELEMENT_PROPS
                    if prop in attributes
                ),
                None,
            )
            screens.append(
                _build_screen(
                    path, component, bindings, file, element.start_point[0] + 1, allocator
                )
            )
        return screens

    def _object_routes(
        self, file: SourceFile, bindings: dict[str, str], allocator: IdAllocator
    ) -> list[Screen]:
        """Parse ``createBrowserRouter([{ path, element }])`` route objects."""
        screens: list[Screen] = []
        for call in find_all(file.root, "call_expression"):
            function = call.child_by_field_name("function")
            if function is None or file.text(function) not in {
                "createBrowserRouter",
                "createHashRouter",
                "createMemoryRouter",
            }:
                continue
            for obj in find_all(call, "object"):
                pairs = {
                    _pair_key(p, file): p.child_by_field_name("value")
                    for p in obj.named_children
                    if p.type == "pair"
                }
                if "path" not in pairs:
                    continue
                object_path = string_value(pairs["path"], file)
                if object_path is None:
                    continue
                element = pairs.get("element") or pairs.get("Component")
                component = None
                if element is not None:
                    identifiers = find_all(element, "identifier")
                    component = file.text(identifiers[0]) if identifiers else None
                screens.append(
                    _build_screen(
                        object_path, component, bindings, file, obj.start_point[0] + 1, allocator
                    )
                )
        return screens


def _build_screen(
    path: str,
    component: str | None,
    bindings: dict[str, str],
    file: SourceFile,
    line: int,
    allocator: IdAllocator,
) -> Screen:
    """Assemble a Screen, recording the module its component came from."""
    module = bindings.get(component or "", "")
    return Screen(
        id=allocator.allocate(f"screen:{path or '/'}", component),
        location=SourceLocation(file=file.rel, line=line, symbol=component),
        path=path or "/",
        component=component or "unknown",
        params=[p.lstrip(":") for p in path.split("/") if p.startswith(":")],
        layout=module or None,
    )


def _import_bindings(file: SourceFile, context: ProjectContext) -> dict[str, str]:
    """Map imported identifiers to the repository-relative module they come from."""
    bindings: dict[str, str] = {}
    for node in find_all(file.root, "import_statement"):
        source_node = node.child_by_field_name("source")
        if source_node is None:
            continue
        specifier = file.text(source_node).strip("\"'")
        target = resolve_specifier(specifier, file.path, context.root, context.aliases)
        if target is None:
            continue
        clause = next((c for c in node.named_children if c.type == "import_clause"), None)
        if clause is None:
            continue
        for identifier in find_all(clause, "identifier"):
            bindings[file.text(identifier)] = target
    # `const Orders = lazy(() => import('@/pages/Orders'))`
    for declarator in find_all(file.root, "variable_declarator"):
        name = declarator.child_by_field_name("name")
        value = declarator.child_by_field_name("value")
        if name is None or value is None or name.type != "identifier":
            continue
        for call in find_all(value, "call_expression"):
            function = call.child_by_field_name("function")
            if function is None or file.text(function) != "import":
                continue
            arguments = call.child_by_field_name("arguments")
            if not arguments or not arguments.named_children:
                continue
            lazy_specifier = string_value(arguments.named_children[0], file)
            if lazy_specifier is None:
                continue
            target = resolve_specifier(lazy_specifier, file.path, context.root, context.aliases)
            if target is not None:
                bindings[file.text(name)] = target
    return bindings


def _attribute_string(value, file: SourceFile) -> str | None:  # type: ignore[no-untyped-def]
    """Read a JSX attribute value when it is a plain string.

    Returns ``""`` for a bare attribute (an index route) and ``None`` when the
    value is a computed expression, which the caller treats as unresolvable
    rather than guessing at a route path.
    """
    if value is None:
        return ""
    if value.type == "string":
        text = file.text(value)
        return text[1:-1] if len(text) >= 2 else ""
    inner = find_all(value, "string")
    if inner:
        text = file.text(inner[0])
        return text[1:-1] if len(text) >= 2 else ""
    return None


def _component_name(value, file: SourceFile) -> str | None:  # type: ignore[no-untyped-def]
    """Read the component identifier out of an ``element={<Foo />}`` value."""
    if value is None:
        return None
    for node in find_all(value, "jsx_self_closing_element", "jsx_opening_element"):
        name = node.child_by_field_name("name")
        if name is not None:
            return file.text(name)
    identifiers = find_all(value, "identifier")
    return file.text(identifiers[0]) if identifiers else None


def _pair_key(pair, file: SourceFile) -> str:  # type: ignore[no-untyped-def]
    """Normalised key of an object literal pair."""
    key = pair.child_by_field_name("key")
    if key is None:
        return ""
    text = file.text(key)
    return text[1:-1] if text[:1] in {'"', "'"} else text
