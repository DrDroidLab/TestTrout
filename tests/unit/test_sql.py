from pathlib import Path

from testtrout.analysis.ids import IdAllocator
from testtrout.analysis.sql import parse_migrations

MIGRATION = """
create table if not exists orders (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references profiles(id),
  total numeric(10,2) not null default 0,
  note text
);

alter table orders enable row level security;

create policy "Users manage own orders" on orders
  for all to authenticated
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);
"""


def _parse(tmp_path: Path, sql: str = MIGRATION):
    migrations = tmp_path / "supabase" / "migrations"
    migrations.mkdir(parents=True)
    (migrations / "0001_init.sql").write_text(sql, encoding="utf-8")
    return parse_migrations(migrations, tmp_path, IdAllocator())


def test_columns_constraints_and_references(tmp_path: Path):
    tables, _ = _parse(tmp_path)
    orders = next(t for t in tables if t.name == "orders")
    by_name = {c.name: c for c in orders.columns}

    assert by_name["id"].primary_key
    assert by_name["user_id"].references == "profiles.id"
    assert by_name["user_id"].nullable is False
    assert by_name["note"].nullable is True
    # numeric(10,2) must not be split on the comma inside the parentheses.
    assert by_name["total"].type == "numeric(10,2)"


def test_rls_enabled_is_tracked(tmp_path: Path):
    tables, _ = _parse(tmp_path)
    assert next(t for t in tables if t.name == "orders").rls_enabled is True


def test_policy_clauses_are_captured_verbatim(tmp_path: Path):
    """The USING expression is the test specification — it must survive intact."""
    _, policies = _parse(tmp_path)
    policy = policies[0]
    assert policy.name == "Users manage own orders"
    assert policy.table == "orders"
    assert policy.command == "ALL"
    assert policy.roles == ["authenticated"]
    assert policy.using == "auth.uid() = user_id"
    assert policy.with_check == "auth.uid() = user_id"


def test_owner_column_detection(tmp_path: Path):
    tables, _ = _parse(tmp_path)
    assert next(t for t in tables if t.name == "orders").owner_column == "user_id"


def test_owner_column_returns_none_rather_than_guessing(tmp_path: Path):
    tables, _ = _parse(tmp_path, "create table widgets (id uuid primary key, label text);")
    assert next(t for t in tables if t.name == "widgets").owner_column is None
