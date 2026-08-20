from pathlib import Path

from testtrout.analysis.parser import parse_file, resolve_chains


def _parse(tmp_path: Path, source: str, name: str = "sample.tsx"):
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    parsed = parse_file(path, tmp_path)
    assert parsed is not None
    return parsed


def test_chain_is_reported_once_not_per_link(tmp_path: Path):
    """A fluent chain must yield exactly one Chain.

    Regression test for a real bug: tree-sitter rebuilds Node wrappers on every
    access, so an identity check for "is this call the object of an enclosing
    member expression" silently never matched, and every link of the chain was
    reported as its own call site.
    """
    file = _parse(tmp_path, 'const r = supabase.from("orders").select("id").eq("a", 1).limit(5);')
    chains = resolve_chains(file.root, file)
    supabase_chains = [c for c in chains if c.base == "supabase"]
    assert len(supabase_chains) == 1
    assert supabase_chains[0].methods == ["from", "select", "eq", "limit"]


def test_property_access_before_first_call_is_captured(tmp_path: Path):
    """`supabase.auth.signIn()` must be distinguishable from a table query."""
    file = _parse(tmp_path, "await supabase.auth.signInWithPassword({ email, password });")
    chain = next(c for c in resolve_chains(file.root, file) if c.base == "supabase")
    assert chain.properties == ("auth",)
    assert chain.methods == ["signInWithPassword"]


def test_storage_chain_keeps_both_property_and_bucket(tmp_path: Path):
    file = _parse(tmp_path, 'await supabase.storage.from("avatars").upload(path, file);')
    chain = next(c for c in resolve_chains(file.root, file) if c.base == "supabase")
    assert chain.properties == ("storage",)
    assert chain.methods == ["from", "upload"]


def test_await_and_parentheses_do_not_break_the_chain(tmp_path: Path):
    file = _parse(tmp_path, 'const { data } = await (supabase.from("t").select("*"));')
    chains = [c for c in resolve_chains(file.root, file) if c.base == "supabase"]
    assert len(chains) == 1
