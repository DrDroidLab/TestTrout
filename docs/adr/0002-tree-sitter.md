# 2. tree-sitter with hand-written traversal

**Status:** accepted

## Context

We need to extract routes, Supabase call chains, and exported symbols from
TypeScript and TSX. The options were the TypeScript compiler API (via a Node
subprocess), a regex pass, or tree-sitter.

## Decision

Use tree-sitter, and walk the tree by hand rather than using tree-sitter
queries.

## Consequences

tree-sitter parses TSX correctly, is fast enough to scan a large repository in
under a second, and needs no Node subprocess.

Queries were rejected for two reasons. The query API has changed shape across
several recent releases, so query strings are a version-compatibility liability
in a tool people install from PyPI. And the central extraction — unwinding
`supabase.from('t').select('c').eq(...)` into ordered steps — is awkward as a
query and straightforward as a walk.

Hand traversal is more verbose. That is the price, and the helpers in
`analysis/parser.py` absorb most of it.

**A trap worth knowing:** tree-sitter constructs a new `Node` wrapper on every
access, so `node_a is node_b` is always false even for the same underlying
node. Compare `.id`. This cost us a bug where every link of a method chain was
reported as its own chain.
