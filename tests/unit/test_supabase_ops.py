from pathlib import Path

from testtrout.analysis.ids import IdAllocator
from testtrout.analysis.parser import parse_file
from testtrout.analysis.supabase_ops import extract
from testtrout.domain.surface import Operation

CLIENT_IMPORT = 'import { supabase } from "@/integrations/supabase/client";\n'


def _extract(tmp_path: Path, body: str):
    path = tmp_path / "component.tsx"
    path.write_text(CLIENT_IMPORT + body, encoding="utf-8")
    file = parse_file(path, tmp_path)
    assert file is not None
    return extract(file, IdAllocator())


def test_select_resolves_table_and_columns(tmp_path: Path):
    ops, _ = _extract(tmp_path, 'const r = supabase.from("orders").select("id, total");')
    assert len(ops) == 1
    assert ops[0].table == "orders"
    assert ops[0].operation is Operation.SELECT
    assert ops[0].columns == ["id", "total"]


def test_nested_embed_is_not_split_on_its_inner_comma(tmp_path: Path):
    """`customer:profiles(name, email)` is one column, not three."""
    ops, _ = _extract(
        tmp_path, 'supabase.from("orders").select("id, customer:profiles(name, email), total");'
    )
    assert ops[0].columns == ["id", "customer:profiles(name, email)", "total"]


def test_write_operation_beats_select_in_the_same_chain(tmp_path: Path):
    """`.insert(...).select()` is an insert — the write is what matters."""
    ops, _ = _extract(tmp_path, 'supabase.from("orders").insert({ total: 1 }).select();')
    assert ops[0].operation is Operation.INSERT
    assert ops[0].columns == ["total"]


def test_non_literal_table_is_reported_not_guessed(tmp_path: Path):
    """A computed table name must produce a warning, never an invented name."""
    ops, warnings = _extract(tmp_path, 'supabase.from(tableName).select("*");')
    assert ops[0].table is None
    assert any(w.code == "unresolved_table" for w in warnings)


def test_rpc_and_storage_are_classified_separately(tmp_path: Path):
    ops, _ = _extract(
        tmp_path,
        'supabase.rpc("recalc", { id });\nsupabase.storage.from("avatars").upload("a.png", f);\n',
    )
    kinds = {op.operation for op in ops}
    assert Operation.RPC in kinds
    assert Operation.STORAGE in kinds


def test_a_file_with_no_client_import_yields_nothing(tmp_path: Path):
    """Some other fluent API named `supabase` must not be misread as one."""
    path = tmp_path / "other.tsx"
    path.write_text('const x = notSupabase.from("orders").select("*");', encoding="utf-8")
    file = parse_file(path, tmp_path)
    assert file is not None
    ops, _ = extract(file, IdAllocator())
    assert ops == []
