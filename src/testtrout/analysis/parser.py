"""tree-sitter parsing and AST helpers for TypeScript and TSX.

Why hand-written traversal instead of tree-sitter queries
---------------------------------------------------------
The query API has changed shape across several recent tree-sitter releases,
and the extraction this scanner needs — unwinding a method chain like
``supabase.from("x").select("y").eq(...)`` into an ordered list of steps — is
awkward to express as a query and easy to express as a walk. Explicit
traversal is more verbose but stable across versions and precise about what it
matches. See ``docs/adr/0002-tree-sitter.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser, Tree

# Node types that introduce a nameable scope. Used to attribute a call site to
# the component or function that contains it.
_FUNCTION_NODES = {
    "function_declaration",
    "function_expression",
    "arrow_function",
    "method_definition",
    "generator_function_declaration",
}


@lru_cache(maxsize=2)
def _language(tsx: bool) -> Language:
    """Load and cache a tree-sitter language.

    Loading is not free, and a scan parses hundreds of files, so both variants
    are cached for the process lifetime.
    """
    return Language(tstypescript.language_tsx() if tsx else tstypescript.language_typescript())


@dataclass(frozen=True)
class SourceFile:
    """A parsed source file plus everything needed to read text back out.

    The raw ``bytes`` are kept because tree-sitter reports byte offsets, and
    slicing the original buffer is the only way to recover exact source text
    for a node without re-deriving encodings.
    """

    path: Path
    rel: str
    source: bytes
    tree: Tree

    @property
    def root(self) -> Node:
        """The root ``program`` node."""
        return self.tree.root_node

    def text(self, node: Node | None) -> str:
        """Exact source text for a node, or an empty string for ``None``."""
        if node is None:
            return ""
        return self.source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    def line_of(self, node: Node) -> int:
        """1-indexed start line of a node."""
        return node.start_point[0] + 1

    def end_line_of(self, node: Node) -> int:
        """1-indexed end line of a node."""
        return node.end_point[0] + 1


def parse_file(path: Path, root: Path) -> SourceFile | None:
    """Parse one TypeScript or TSX file.

    Returns ``None`` for unreadable files rather than raising: a single
    malformed file in a large repository should degrade the scan, not abort it.
    The caller records a warning so the blind spot stays visible.
    """
    try:
        source = path.read_bytes()
    except OSError:
        return None
    parser = Parser(_language(tsx=path.suffix in {".tsx", ".jsx"}))
    tree = parser.parse(source)
    return SourceFile(
        path=path,
        rel=path.relative_to(root).as_posix(),
        source=source,
        tree=tree,
    )


def walk(node: Node) -> list[Node]:
    """Every node in the subtree, depth-first, including ``node`` itself."""
    out: list[Node] = []
    stack = [node]
    while stack:
        current = stack.pop()
        out.append(current)
        stack.extend(reversed(current.children))
    return out


def find_all(node: Node, *types: str) -> list[Node]:
    """All descendants of the given types, in document order."""
    wanted = set(types)
    return [n for n in walk(node) if n.type in wanted]


def enclosing_name(node: Node, file: SourceFile) -> str | None:
    """Name of the function or component containing ``node``.

    Walks up to the nearest function-like node and tries, in order: a
    declaration name, the variable it is assigned to, or the object property it
    defines. Returns ``None`` when the function is genuinely anonymous rather
    than inventing a label.
    """
    current: Node | None = node
    while current is not None:
        if current.type in _FUNCTION_NODES:
            named = current.child_by_field_name("name")
            if named is not None:
                return file.text(named)
            parent = current.parent
            if parent is not None and parent.type == "variable_declarator":
                return file.text(parent.child_by_field_name("name"))
            if parent is not None and parent.type == "pair":
                return file.text(parent.child_by_field_name("key"))
        current = current.parent
    return None


@dataclass(frozen=True)
class ChainStep:
    """One ``.method(args)`` link in a fluent call chain."""

    method: str
    args: tuple[Node, ...]
    node: Node
    """The ``call_expression`` for this step, for location reporting."""


@dataclass(frozen=True)
class Chain:
    """A resolved method chain, e.g. ``supabase.from("t").select("*").eq(...)``.

    ``base`` is the identifier the chain starts from, which is what lets the
    scanner tell a Supabase client apart from any other fluent API.
    ``properties`` holds non-call property accesses between the base and the
    first call — this is how ``supabase.auth.signIn()`` and
    ``supabase.storage.from()`` are distinguished from a plain table query.
    """

    base: str
    properties: tuple[str, ...]
    steps: tuple[ChainStep, ...]
    node: Node

    def step(self, *names: str) -> ChainStep | None:
        """First step whose method matches any of ``names``."""
        wanted = set(names)
        return next((s for s in self.steps if s.method in wanted), None)

    def has(self, *names: str) -> bool:
        """Whether any step matches."""
        return self.step(*names) is not None

    @property
    def methods(self) -> list[str]:
        """Method names in call order."""
        return [s.method for s in self.steps]


def _unwind(call: Node) -> tuple[Node | None, list[ChainStep], list[str]]:
    """Walk a call chain from the outermost call back to its base identifier."""
    steps: list[ChainStep] = []
    properties: list[str] = []
    current: Node | None = call

    while current is not None:
        if current.type == "call_expression":
            function = current.child_by_field_name("function")
            arguments = current.child_by_field_name("arguments")
            args = tuple(a for a in (arguments.named_children if arguments else []))
            if function is None:
                break
            if function.type == "member_expression":
                prop = function.child_by_field_name("property")
                steps.append(
                    ChainStep(
                        method=prop.text.decode() if prop is not None and prop.text else "",
                        args=args,
                        node=current,
                    )
                )
                current = function.child_by_field_name("object")
                continue
            # A bare call like `createClient(...)` terminates the chain.
            current = function
            continue
        if current.type == "member_expression":
            prop = current.child_by_field_name("property")
            if prop is not None and prop.text:
                properties.append(prop.text.decode())
            current = current.child_by_field_name("object")
            continue
        if current.type in {"await_expression", "parenthesized_expression", "non_null_expression"}:
            current = current.named_children[0] if current.named_children else None
            continue
        break

    steps.reverse()
    properties.reverse()
    return current, steps, properties


def resolve_chains(root: Node, file: SourceFile) -> list[Chain]:
    """Find every maximal method chain in a subtree.

    Only outermost calls are returned. Without that filter,
    ``a.from("x").select("y")`` would yield two overlapping chains and every
    call site would be double-counted.
    """
    chains: list[Chain] = []
    for call in find_all(root, "call_expression"):
        parent = call.parent
        # Skip inner links: this call is the object of a member access that is
        # itself being called, so a larger chain already covers it.
        #
        # Compare by `.id`, not `is`. tree-sitter constructs a fresh Node
        # wrapper on every access, so identity comparison silently never
        # matches and every link in a chain gets reported as its own chain.
        if parent is not None and parent.type == "member_expression":
            obj = parent.child_by_field_name("object")
            if obj is not None and obj.id == call.id:
                continue
        base_node, steps, properties = _unwind(call)
        if base_node is None or not steps:
            continue
        if base_node.type != "identifier":
            continue
        chains.append(
            Chain(
                base=file.text(base_node),
                properties=tuple(properties),
                steps=tuple(steps),
                node=call,
            )
        )
    return chains


def string_value(node: Node | None, file: SourceFile) -> str | None:
    """Literal value of a string node, or ``None`` if it is not a literal.

    Returning ``None`` for template literals and variables is deliberate: a
    guessed table name produces a wrong test, while an unresolved one produces
    an honest warning the developer can answer.
    """
    if node is None:
        return None
    if node.type == "string":
        raw = file.text(node)
        return raw[1:-1] if len(raw) >= 2 else ""
    if node.type == "template_string" and not find_all(node, "template_substitution"):
        raw = file.text(node)
        return raw[1:-1] if len(raw) >= 2 else ""
    return None


def object_keys(node: Node | None, file: SourceFile) -> list[str]:
    """Keys of an object literal, used to infer written columns.

    Handles the array-of-objects form that ``.insert([{...}])`` accepts.
    """
    if node is None:
        return []
    if node.type == "array":
        keys: list[str] = []
        for child in node.named_children:
            keys.extend(object_keys(child, file))
        return list(dict.fromkeys(keys))
    if node.type != "object":
        return []
    out: list[str] = []
    for pair in node.named_children:
        if pair.type != "pair":
            continue
        key = pair.child_by_field_name("key")
        if key is None:
            continue
        text = file.text(key)
        out.append(text[1:-1] if text[:1] in {'"', "'"} else text)
    return out


def jsx_attributes(element: Node, file: SourceFile) -> dict[str, Node | None]:
    """Map a JSX element's attribute names to their value nodes.

    ``jsx_attribute`` exposes no ``name`` or ``value`` field in the TSX
    grammar, so this reads positionally: the first child is the
    ``property_identifier``, and the value is the last named child when one is
    present. A bare boolean attribute maps to ``None``.
    """
    attributes: dict[str, Node | None] = {}
    for attribute in element.named_children:
        if attribute.type != "jsx_attribute":
            continue
        children = attribute.named_children
        if not children:
            continue
        name = file.text(children[0])
        attributes[name] = children[1] if len(children) > 1 else None
    return attributes
