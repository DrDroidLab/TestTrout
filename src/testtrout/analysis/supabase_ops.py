"""Extraction of Supabase client calls — the characteristic surface of this stack.

In a Lovable or v0 application, most of what a backend would normally do is
expressed as ``supabase.from('table')...`` chains inside React components.
Those call sites *are* the API, so enumerating them accurately is the
difference between a useful surface map and an empty one.

Resolution is deliberately conservative. A table name that comes from a
variable is left unresolved and reported as a warning rather than guessed,
because a guessed table produces a confidently wrong test, while an honest gap
produces a question the developer can answer in a second.
"""

from __future__ import annotations

from dataclasses import dataclass

from testtrout.analysis.ids import IdAllocator
from testtrout.analysis.parser import (
    Chain,
    SourceFile,
    enclosing_name,
    object_keys,
    resolve_chains,
    string_value,
)
from testtrout.domain.surface import DataOperation, Operation, ScanWarning, SourceLocation

# Package and module hints that mark an identifier as a Supabase client.
_CLIENT_PACKAGE = "@supabase/supabase-js"
_CLIENT_MODULE_HINT = "supabase"

# PostgREST filter and modifier methods worth recording. They describe how a
# query is scoped, which is what a generated test has to reproduce.
_FILTERS = frozenset(
    {
        "eq",
        "neq",
        "gt",
        "gte",
        "lt",
        "lte",
        "like",
        "ilike",
        "is",
        "in",
        "contains",
        "containedBy",
        "overlaps",
        "textSearch",
        "match",
        "not",
        "or",
        "filter",
        "order",
        "limit",
        "range",
        "single",
        "maybeSingle",
        "csv",
        "abortSignal",
    }
)

_WRITE_METHODS: dict[str, Operation] = {
    "insert": Operation.INSERT,
    "update": Operation.UPDATE,
    "upsert": Operation.UPSERT,
    "delete": Operation.DELETE,
}


@dataclass
class _Extraction:
    """Intermediate result before a chain becomes a domain object."""

    operation: Operation
    table: str | None = None
    columns: list[str] | None = None
    function: str | None = None
    bucket: str | None = None


def client_bindings(file: SourceFile) -> set[str]:
    """Identifier names in this file that refer to a Supabase client.

    Covers the three shapes seen in practice: a direct import from the SDK, an
    import from a local client module (Lovable emits
    ``@/integrations/supabase/client``), and a local ``createClient`` call.
    """
    from testtrout.analysis.parser import find_all

    bindings: set[str] = set()

    for node in find_all(file.root, "import_statement"):
        source = file.text(node.child_by_field_name("source")).strip("\"'")
        if source != _CLIENT_PACKAGE and _CLIENT_MODULE_HINT not in source.lower():
            continue
        for clause in find_all(node, "import_clause", "named_imports", "import_specifier"):
            for identifier in find_all(clause, "identifier"):
                bindings.add(file.text(identifier))

    # `const supabase = createClient(url, key)` — including re-exported wrappers.
    for declarator in find_all(file.root, "variable_declarator"):
        value = declarator.child_by_field_name("value")
        name = declarator.child_by_field_name("name")
        if value is None or name is None or name.type != "identifier":
            continue
        text = file.text(value)
        if (
            "createClient(" in text
            or "createBrowserClient(" in text
            or "createServerClient(" in text
        ):
            bindings.add(file.text(name))

    return bindings


def _split_columns(raw: str) -> list[str]:
    """Split a PostgREST ``select`` string into top-level column names.

    Nested embeds such as ``id, customer:profiles(name)`` must not be split on
    the comma inside the parentheses, so this tracks depth rather than using
    ``str.split``.
    """
    columns: list[str] = []
    depth = 0
    current: list[str] = []
    for char in raw:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            columns.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    columns.append("".join(current).strip())
    return [c for c in columns if c]


def _classify(chain: Chain, file: SourceFile) -> _Extraction | None:
    """Map a resolved chain onto a Supabase operation, or ``None`` if it is not one."""
    if "auth" in chain.properties:
        return _Extraction(
            operation=Operation.AUTH, function=chain.methods[0] if chain.steps else None
        )

    if "storage" in chain.properties:
        bucket_step = chain.step("from")
        bucket = (
            string_value(bucket_step.args[0], file) if bucket_step and bucket_step.args else None
        )
        method = next((m for m in chain.methods if m != "from"), None)
        return _Extraction(operation=Operation.STORAGE, bucket=bucket, function=method)

    first = chain.methods[0] if chain.methods else ""

    if first == "rpc":
        step = chain.steps[0]
        return _Extraction(
            operation=Operation.RPC,
            function=string_value(step.args[0], file) if step.args else None,
        )

    if first in {"channel", "realtime"}:
        return _Extraction(operation=Operation.REALTIME)

    from_step = chain.step("from")
    if from_step is None:
        return None

    table = string_value(from_step.args[0], file) if from_step.args else None

    # A write anywhere in the chain wins over the trailing `.select()` that
    # PostgREST callers add to read back the affected rows.
    for method, operation in _WRITE_METHODS.items():
        write_step = chain.step(method)
        if write_step is None:
            continue
        written = object_keys(write_step.args[0], file) if write_step.args else []
        return _Extraction(operation=operation, table=table, columns=written)

    select_step = chain.step("select")
    selected: list[str] = []
    if select_step is not None and select_step.args:
        raw = string_value(select_step.args[0], file)
        if raw:
            selected = _split_columns(raw)
    return _Extraction(operation=Operation.SELECT, table=table, columns=selected)


def _identity(extraction: _Extraction) -> str:
    """The durable part of a data operation's id."""
    if extraction.operation is Operation.RPC:
        return f"rpc:{extraction.function or 'unknown'}"
    if extraction.operation is Operation.AUTH:
        return f"auth:{extraction.function or 'unknown'}"
    if extraction.operation is Operation.STORAGE:
        return f"storage:{extraction.bucket or 'unknown'}.{extraction.function or 'unknown'}"
    if extraction.operation is Operation.REALTIME:
        return "realtime:channel"
    return f"data:{extraction.table or 'unresolved'}.{extraction.operation.value}"


def extract(
    file: SourceFile, allocator: IdAllocator
) -> tuple[list[DataOperation], list[ScanWarning]]:
    """Extract every Supabase data operation from one file."""
    bindings = client_bindings(file)
    if not bindings:
        return [], []

    operations: list[DataOperation] = []
    warnings: list[ScanWarning] = []

    for chain in resolve_chains(file.root, file):
        if chain.base not in bindings:
            continue
        extraction = _classify(chain, file)
        if extraction is None:
            continue

        component = enclosing_name(chain.node, file)
        location = SourceLocation(
            file=file.rel,
            line=file.line_of(chain.node),
            end_line=file.end_line_of(chain.node),
            symbol=component,
        )

        if extraction.operation not in {Operation.AUTH, Operation.REALTIME} and not (
            extraction.table or extraction.function or extraction.bucket
        ):
            warnings.append(
                ScanWarning(
                    code="unresolved_table",
                    message=(
                        "Supabase call with a non-literal table name. The scan cannot tell "
                        "which table this touches, so it will not be covered by generated tests."
                    ),
                    location=location,
                )
            )

        filters = [
            f"{step.method}({string_value(step.args[0], file) or '...'})"
            if step.args
            else step.method
            for step in chain.steps
            if step.method in _FILTERS
        ]

        operations.append(
            DataOperation(
                id=allocator.allocate(_identity(extraction), component, file.rel),
                location=location,
                table=extraction.table,
                operation=extraction.operation,
                columns=extraction.columns or [],
                filters=filters,
                function=extraction.function,
                bucket=extraction.bucket,
                in_component=component,
            )
        )

    return operations, warnings
