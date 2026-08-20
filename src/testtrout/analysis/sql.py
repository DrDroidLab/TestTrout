"""Schema and row-level security extraction from SQL migrations.

This is a pragmatic extractor, not a SQL parser. It recognises the statements
that matter for test generation — ``CREATE TABLE``, ``CREATE POLICY``, and
``ENABLE ROW LEVEL SECURITY`` — and ignores everything else. Supabase
migrations are machine-generated or hand-written in a narrow dialect, so the
coverage is good in practice, and anything unrecognised simply does not appear
rather than causing a failure.

Policies matter disproportionately here. "A member cannot read another tenant's
orders" is a complete test specification already written down in the migration,
and it is the failure class that hurts these applications most, so parsing it
accurately is worth more than parsing the rest of the schema perfectly.
"""

from __future__ import annotations

import re
from pathlib import Path

from testtrout.analysis.ids import IdAllocator
from testtrout.domain.surface import Column, Policy, SourceLocation, Table

_CREATE_TABLE = re.compile(
    r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?P<name>[\w.\"]+)\s*\(",
    re.IGNORECASE,
)
_CREATE_POLICY = re.compile(
    r"create\s+policy\s+(?P<name>\"[^\"]+\"|'[^']+'|[\w]+)\s+on\s+(?P<table>[\w.\"]+)",
    re.IGNORECASE,
)
_ENABLE_RLS = re.compile(
    r"alter\s+table\s+(?P<name>[\w.\"]+)\s+enable\s+row\s+level\s+security",
    re.IGNORECASE,
)
_FOR_COMMAND = re.compile(r"\bfor\s+(all|select|insert|update|delete)\b", re.IGNORECASE)
_TO_ROLES = re.compile(r"\bto\s+(?P<roles>[\w\s,\"]+?)(?=\s+(?:using|with|as)\b|;)", re.IGNORECASE)
_AS_KIND = re.compile(r"\bas\s+(permissive|restrictive)\b", re.IGNORECASE)

_COLUMN_CONSTRAINT_WORDS = frozenset(
    {"primary", "unique", "check", "foreign", "constraint", "exclude"}
)


def _unquote(value: str) -> str:
    """Strip quoting and any schema prefix from an identifier."""
    cleaned = value.strip().strip('"').strip("'")
    return cleaned.split(".")[-1].strip('"')


def _line_of(text: str, index: int) -> int:
    """1-indexed line number for a character offset."""
    return text.count("\n", 0, index) + 1


def _balanced(text: str, open_index: int) -> tuple[str, int]:
    """Return the contents of a parenthesised group and the index after it.

    Quote-aware, so a parenthesis inside a string literal does not unbalance
    the scan.
    """
    depth = 0
    quote: str | None = None
    for index in range(open_index, len(text)):
        char = text[index]
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : index], index + 1
    return text[open_index + 1 :], len(text)


def _split_top_level(body: str) -> list[str]:
    """Split a ``CREATE TABLE`` body on commas outside parentheses and quotes."""
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    for char in body:
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if current:
        parts.append("".join(current).strip())
    return [p for p in parts if p]


def _parse_column(definition: str) -> Column | None:
    """Parse one column definition, skipping table-level constraints."""
    tokens = definition.split()
    if not tokens or tokens[0].lower() in _COLUMN_CONSTRAINT_WORDS:
        return None

    name = _unquote(tokens[0])
    rest = " ".join(tokens[1:])
    lowered = rest.lower()

    type_tokens: list[str] = []
    for token in tokens[1:]:
        if token.lower() in {
            "not",
            "null",
            "default",
            "primary",
            "references",
            "unique",
            "check",
            "generated",
        }:
            break
        type_tokens.append(token)

    references = None
    match = re.search(r"references\s+([\w.\"]+)\s*\(\s*([\w\"]+)\s*\)", rest, re.IGNORECASE)
    if match:
        references = f"{_unquote(match.group(1))}.{_unquote(match.group(2))}"
    else:
        match = re.search(r"references\s+([\w.\"]+)", rest, re.IGNORECASE)
        if match:
            references = f"{_unquote(match.group(1))}.id"

    default = None
    match = re.search(
        r"default\s+(.+?)(?=\s+(?:not\s+null|references|check|unique)\b|$)", rest, re.IGNORECASE
    )
    if match:
        default = match.group(1).strip()

    return Column(
        name=name,
        type=" ".join(type_tokens) or "unknown",
        nullable="not null" not in lowered and "primary key" not in lowered,
        default=default,
        primary_key="primary key" in lowered,
        references=references,
    )


def parse_migrations(
    migrations_dir: Path, root: Path, allocator: IdAllocator
) -> tuple[list[Table], list[Policy]]:
    """Parse every ``.sql`` file in a migrations directory.

    Files are processed in sorted order so that later migrations override
    earlier ones, mirroring how they are actually applied.
    """
    if not migrations_dir.is_dir():
        return [], []

    tables: dict[str, Table] = {}
    policies: list[Policy] = []

    for path in sorted(migrations_dir.rglob("*.sql")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.name

        for match in _CREATE_TABLE.finditer(text):
            name = _unquote(match.group("name"))
            body, _ = _balanced(text, match.end() - 1)
            columns = [c for c in (_parse_column(d) for d in _split_top_level(body)) if c]
            existing = tables.get(name)
            tables[name] = Table(
                name=name,
                columns=columns or (existing.columns if existing else []),
                rls_enabled=existing.rls_enabled if existing else False,
                location=SourceLocation(file=rel, line=_line_of(text, match.start())),
            )

        for match in _ENABLE_RLS.finditer(text):
            name = _unquote(match.group("name"))
            table = tables.get(name) or Table(name=name)
            tables[name] = table.model_copy(update={"rls_enabled": True})

        for match in _CREATE_POLICY.finditer(text):
            policies.append(_parse_policy(text, match, rel, allocator))

    return sorted(tables.values(), key=lambda t: t.name), policies


def _parse_policy(text: str, match: re.Match[str], rel: str, allocator: IdAllocator) -> Policy:
    """Parse a single ``CREATE POLICY`` statement starting at ``match``."""
    name = _unquote(match.group("name"))
    table = _unquote(match.group("table"))

    # The statement runs to the next unquoted semicolon.
    end = text.find(";", match.end())
    body = text[match.end() : end if end != -1 else len(text)]

    command_match = _FOR_COMMAND.search(body)
    command = command_match.group(1).upper() if command_match else "ALL"

    roles_match = _TO_ROLES.search(body)
    roles = (
        [_unquote(r) for r in roles_match.group("roles").split(",") if r.strip()]
        if roles_match
        else []
    )

    kind_match = _AS_KIND.search(body)
    permissive = kind_match is None or kind_match.group(1).lower() == "permissive"

    using = _clause(body, "using")
    with_check = _clause(body, "with check")

    return Policy(
        id=allocator.allocate(f"policy:{table}.{name}"),
        location=SourceLocation(file=rel, line=_line_of(text, match.start())),
        name=name,
        table=table,
        command=command,
        roles=roles,
        permissive=permissive,
        using=using,
        with_check=with_check,
    )


def _clause(body: str, keyword: str) -> str | None:
    """Extract a ``USING (...)`` or ``WITH CHECK (...)`` expression verbatim."""
    match = re.search(rf"\b{keyword}\s*\(", body, re.IGNORECASE)
    if not match:
        return None
    contents, _ = _balanced(body, match.end() - 1)
    return " ".join(contents.split())
