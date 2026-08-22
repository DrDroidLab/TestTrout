"""Extracting an application's calls to its own backend.

Not every app keeps its data in Supabase. Plenty have an ordinary HTTP API, and
for those the ``fetch`` call sites *are* the backend surface — the equivalent of
a ``supabase.from(...)`` chain. Without this, such an app scans to a list of
screens that reach nothing, every one of them scored low, and the gap map has
almost nothing to say.

Two shapes are recognised:

**Direct calls** — ``fetch('/api/jobs', { method: 'POST' })``.

**Calls through a wrapper** — the near-universal ``src/lib/api.ts`` pattern,
where one function adds the base URL and an auth header and everything else
calls ``api('/jobs', { method: 'POST' })``. Finding the wrapper first and then
its callers is what turns two literal ``fetch`` sites into the twenty real
endpoints an app actually uses.

Paths that are not literal are recorded as unresolved rather than guessed, in
keeping with the rest of the scanner.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tree_sitter import Node

from testtrout.analysis.ids import IdAllocator
from testtrout.analysis.parser import (
    SourceFile,
    enclosing_name,
    find_all,
    object_keys,
    string_value,
)
from testtrout.domain.location import SourceLocation
from testtrout.domain.surface import Endpoint, ScanWarning

# A wrapper module is one that calls fetch and exports something callable.
_FETCH = re.compile(r"\bfetch\s*\(")
_METHOD = re.compile(r"['\"](GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)['\"]", re.IGNORECASE)

# Template segments become path parameters: `/jobs/${id}` -> `/jobs/:param`.
_TEMPLATE_SUBSTITUTION = re.compile(r"\$\{[^}]*\}")


@dataclass(frozen=True)
class HttpCall:
    """One call to the application's own backend."""

    path: str
    method: str
    location: SourceLocation
    via: str
    """How it was found: 'fetch' or the wrapper's name."""


def wrapper_names(files: dict[str, SourceFile]) -> dict[str, str]:
    """Find exported functions that actually call ``fetch``.

    An app with a client wrapper has two literal ``fetch`` sites and twenty real
    endpoints; resolving the wrapper is the difference between finding two and
    finding twenty.

    Only exports whose *own body* calls fetch qualify. A first attempt accepted
    every export of a fetch-containing module, which meant helpers living in the
    same file — a ``sessionKey(slug)`` next to an ``api(path)`` — were scanned as
    HTTP calls and reported as unresolvable endpoints.
    """
    found: dict[str, str] = {}
    for rel, file in sorted(files.items()):
        text = file.source.decode("utf-8", errors="replace")
        if not _FETCH.search(text):
            continue

        for node in find_all(file.root, "export_statement"):
            declaration = node.child_by_field_name("declaration")
            if declaration is None:
                continue
            for name_node, body in _exported_functions(declaration, file):
                if _calls_fetch(body, file):
                    found[file.text(name_node)] = rel
    return found


# `${API_URL}${path}` — the shape a client wrapper's fetch target almost always
# takes. The first interpolation is the base; everything after it is the path.
_TEMPLATE_BASE = re.compile(r"^\$\{([A-Za-z_$][\w.$]*)\}")

# An environment variable read, however the framework spells it.
_ENV_READ = re.compile(r"(?:process\.env|import\.meta\.env)\.([A-Z][A-Z0-9_]*)")


def api_base(files: dict[str, SourceFile]) -> str:
    """The environment variable holding the API's base URL, if there is one.

    An application whose endpoints live on a separate backend calls them
    through a base that is configured, not written down. Without this the
    endpoint paths look absolute and every request goes to the frontend's own
    origin, where they all return 404 — which reads as "these endpoints are
    public" or "your app is broken", and is neither.

    Returns the variable name — never a value. A URL in the source is a
    development default and lying about production.
    """
    for _rel, file in sorted(files.items()):
        text = file.source.decode("utf-8", errors="replace")
        if not _FETCH.search(text):
            continue
        for call in find_all(file.root, "call_expression"):
            if file.text(call.child_by_field_name("function")) != "fetch":
                continue
            arguments = call.child_by_field_name("arguments")
            target = arguments.named_child(0) if arguments else None
            if target is None or target.type != "template_string":
                continue
            match = _TEMPLATE_BASE.match(file.text(target).strip("`"))
            if match is None:
                continue
            # The base is an identifier; find what it was assigned from.
            variable = match.group(1)
            found = _ENV_READ.search(_assignment_of(variable, text) or "")
            if found:
                return found.group(1)
    return ""


def _assignment_of(name: str, text: str) -> str | None:
    """The right-hand side of ``const <name> = ...``, up to the statement end."""
    match = re.search(rf"\b(?:const|let|var)\s+{re.escape(name)}\s*=", text)
    if match is None:
        return None
    return text[match.end() : match.end() + 300]


def _exported_functions(declaration: Node, file: SourceFile) -> list[tuple[Node, Node]]:
    """Name and body of each function an export statement declares."""
    out: list[tuple[Node, Node]] = []

    named = declaration.child_by_field_name("name")
    if named is not None and declaration.type in {
        "function_declaration",
        "generator_function_declaration",
    }:
        out.append((named, declaration))

    for declarator in find_all(declaration, "variable_declarator"):
        name = declarator.child_by_field_name("name")
        value = declarator.child_by_field_name("value")
        if name is not None and name.type == "identifier" and value is not None:
            out.append((name, value))
    return out


def _calls_fetch(body: Node, file: SourceFile) -> bool:
    """Whether a function body issues a fetch call itself."""
    return any(
        file.text(call.child_by_field_name("function")) == "fetch"
        for call in find_all(body, "call_expression")
        if call.child_by_field_name("function") is not None
    )


def _callee_name(function: Node, file: SourceFile) -> str:
    """The name being called, seeing through wrappers the grammar adds.

    ``await api<Job>('/jobs')`` parses with the callee as an ``await_expression``
    whose text is ``await api`` — so a naive read misses every generic call,
    which in a typed client is most of them.
    """
    node: Node | None = function
    while node is not None and node.type in {
        "await_expression",
        "parenthesized_expression",
        "non_null_expression",
    }:
        node = node.named_children[0] if node.named_children else None
    return file.text(node) if node is not None else ""


def _normalise(path: str) -> str:
    """Turn a template path into a route pattern."""
    cleaned = _TEMPLATE_SUBSTITUTION.sub(":param", path.strip())
    if not cleaned.startswith("/"):
        cleaned = "/" + cleaned
    return cleaned


def _method_from(arguments: list, file: SourceFile) -> str:  # type: ignore[type-arg]
    """Read the HTTP method from an options object, defaulting sensibly.

    A body without an explicit method means POST in every client wrapper this
    pattern describes, and in ``fetch`` itself the default is GET.
    """
    for argument in arguments[1:]:
        text = file.text(argument)
        match = _METHOD.search(text)
        if match:
            return match.group(1).upper()
        keys = object_keys(argument, file)
        if "body" in keys or "form" in keys:
            return "POST"
    return "GET"


def extract(
    file: SourceFile, wrappers: dict[str, str], allocator: IdAllocator
) -> tuple[list[Endpoint], list[ScanWarning]]:
    """Find calls to the application's own backend in one file."""
    endpoints: list[Endpoint] = []
    warnings: list[ScanWarning] = []

    is_wrapper_module = file.rel in set(wrappers.values())

    for call in find_all(file.root, "call_expression"):
        function = call.child_by_field_name("function")
        if function is None:
            continue
        name = _callee_name(function, file)
        via = "fetch" if name == "fetch" else (name if name in wrappers else None)
        if via is None:
            continue
        # Inside the client wrapper itself, `fetch(`${BASE}${path}`)` is plumbing:
        # the real endpoints are at its call sites, which are handled elsewhere.
        if is_wrapper_module and name == "fetch":
            continue

        arguments = call.child_by_field_name("arguments")
        args = list(arguments.named_children) if arguments else []
        if not args:
            continue

        location = SourceLocation(
            file=file.rel,
            line=file.line_of(call),
            symbol=enclosing_name(call, file),
        )
        raw = string_value(args[0], file)
        if raw is None:
            text = file.text(args[0])
            if "${" in text:
                raw = text.strip("`")
            else:
                warnings.append(
                    ScanWarning(
                        code="unresolved_endpoint",
                        message=(
                            "An HTTP call with a computed path. The scan cannot tell which "
                            "endpoint this reaches, so it will not be covered by generated tests."
                        ),
                        location=location,
                    )
                )
                continue

        # A wrapper that prefixes a base URL means the literal is already a path.
        path = _normalise(raw)
        if path.startswith("/http") or "://" in path:
            continue  # an absolute URL to somebody else's service

        method = _method_from(args, file)
        endpoints.append(
            Endpoint(
                id=allocator.allocate(f"api:{method}:{path}", location.symbol, file.rel),
                location=location,
                path=path,
                methods=[method],
            )
        )

    return endpoints, warnings


def discover(
    files: dict[str, SourceFile], allocator: IdAllocator
) -> tuple[list[Endpoint], list[ScanWarning]]:
    """Every call the application makes to its own backend."""
    wrappers = wrapper_names(files)
    endpoints: list[Endpoint] = []
    warnings: list[ScanWarning] = []
    for _, file in sorted(files.items()):
        found, issues = extract(file, wrappers, allocator)
        endpoints.extend(found)
        warnings.extend(issues)
    return endpoints, warnings
