"""Credential discovery and capability readiness."""

from __future__ import annotations

from pathlib import Path

from testtrout.analysis.parser import parse_file
from testtrout.analysis.requirements import from_source, implied
from testtrout.app import settings
from testtrout.domain.config import Config, Entrypoint, SupabaseConfig, TestUser
from testtrout.domain.requirements import Capability, RequirementKind
from testtrout.domain.surface import Policy, ProjectInfo, ScanResult, SourceLocation
from testtrout.planning.readiness import assess, kinds_possible
from testtrout.store import QaPaths


def _files(tmp_path: Path, source: str) -> dict:
    path = tmp_path / "client.ts"
    path.write_text(source, encoding="utf-8")
    parsed = parse_file(path, tmp_path)
    assert parsed is not None
    return {parsed.rel: parsed}


def test_vite_env_vars_are_discovered(tmp_path: Path):
    found = from_source(
        _files(
            tmp_path,
            """
        const u = import.meta.env.VITE_SUPABASE_URL;
        const k = import.meta.env.VITE_SUPABASE_ANON_KEY;
    """,
        )
    )
    by_name = {r.name: r for r in found}
    assert by_name["VITE_SUPABASE_URL"].kind is RequirementKind.SUPABASE_URL
    assert by_name["VITE_SUPABASE_ANON_KEY"].kind is RequirementKind.SUPABASE_ANON_KEY


def test_framework_prefixes_do_not_change_classification(tmp_path: Path):
    """The same requirement wears three prefixes across three frameworks."""
    found = from_source(
        _files(
            tmp_path,
            """
        process.env.NEXT_PUBLIC_SUPABASE_URL;
        process.env.REACT_APP_SUPABASE_URL;
        import.meta.env.VITE_SUPABASE_URL;
    """,
        )
    )
    assert {r.kind for r in found} == {RequirementKind.SUPABASE_URL}


def test_a_service_key_is_not_mistaken_for_an_anon_key(tmp_path: Path):
    """Ordering matters: the looser anon rule must not claim it first."""
    found = from_source(_files(tmp_path, "process.env.SUPABASE_SERVICE_ROLE_KEY;"))
    assert found[0].kind is RequirementKind.SUPABASE_SERVICE_KEY


def test_bracket_access_is_found_too(tmp_path: Path):
    found = from_source(_files(tmp_path, "process.env['STRIPE_SECRET_KEY'];"))
    assert found[0].name == "STRIPE_SECRET_KEY"
    assert found[0].kind is RequirementKind.THIRD_PARTY_KEY
    # Intercepted during runs, so a real key is rarely needed.
    assert found[0].optional is True


def test_every_reference_records_where_it_was_found(tmp_path: Path):
    found = from_source(_files(tmp_path, "\n\nconst u = import.meta.env.VITE_SUPABASE_URL;"))
    assert found[0].locations[0].line == 3


def test_test_accounts_are_implied_by_policies():
    """TestTrout's own needs never appear in the app's source."""
    scan = ScanResult(
        project=ProjectInfo(root=".", framework="vite-react"),
        policies=[
            Policy(
                id="p",
                location=SourceLocation(file="m.sql", line=1),
                name="n",
                table="orders",
                command="ALL",
            )
        ],
    )
    kinds = {r.kind for r in implied(scan)}
    assert RequirementKind.TEST_USER in kinds
    assert RequirementKind.DEPLOYMENT_URL in kinds


# --------------------------------------------------------------- readiness


def test_nothing_configured_means_nothing_is_ready():
    plan = assess(Config())
    assert plan.available == []
    assert all(r.next_step for r in plan.blocked)


def test_a_url_alone_unlocks_api_tests():
    """A partial set gives a partial suite, not an error."""
    config = Config(entrypoints=[Entrypoint(name="p", url="https://x.dev")])
    plan = assess(config)
    assert plan.can(Capability.API_TESTS)
    assert not plan.can(Capability.AUTHORIZATION_TESTS)
    assert "endpoint" in kinds_possible(plan)
    assert "authorization" not in kinds_possible(plan)


def test_authorization_needs_two_accounts(monkeypatch):
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    one = Config(
        supabase=SupabaseConfig(url="https://x.supabase.co", anon_key="env:SUPABASE_ANON_KEY"),
        test_users=[TestUser(role="owner", email="env:A", password="env:B")],
    )
    plan = assess(one)
    assert not plan.can(Capability.AUTHORIZATION_TESTS)
    blocked = next(r for r in plan.blocked if r.capability is Capability.AUTHORIZATION_TESTS)
    assert "second test account" in " ".join(blocked.missing)


def test_every_blocked_capability_names_one_concrete_thing():
    """ "Configure it properly" is not an actionable message."""
    for item in assess(Config()).blocked:
        assert item.missing, f"{item.capability} is blocked with nothing to do about it"
        assert len(item.missing[0]) > 15


# ---------------------------------------------------------------- settings


def test_a_literal_secret_is_refused_by_configuration(tmp_path: Path):
    paths = QaPaths(root=tmp_path)
    try:
        settings.apply(paths, {"supabase": {"anon_key_var": "eyJhbGci.actual.secret"}})
    except settings.SettingsError as exc:
        assert "environment variable name" in str(exc)
    else:
        raise AssertionError("a literal secret was accepted into committed config")


def test_secrets_are_written_to_env_and_gitignored(tmp_path: Path):
    paths = QaPaths(root=tmp_path)
    written = settings.set_secrets(paths, {"SUPABASE_ANON_KEY": "value-here"})

    assert written == ["SUPABASE_ANON_KEY"]
    assert "SUPABASE_ANON_KEY=value-here" in (tmp_path / ".env").read_text()
    assert ".env" in (tmp_path / ".gitignore").read_text()


def test_saving_secrets_preserves_unrelated_env_lines(tmp_path: Path):
    """A developer's own variables must survive a save from the interface."""
    (tmp_path / ".env").write_text("MY_OWN=keep-me\nSUPABASE_ANON_KEY=old\n", encoding="utf-8")
    settings.set_secrets(QaPaths(root=tmp_path), {"SUPABASE_ANON_KEY": "new"})

    text = (tmp_path / ".env").read_text()
    assert "MY_OWN=keep-me" in text
    assert "SUPABASE_ANON_KEY=new" in text
    assert "old" not in text


def test_an_empty_value_clears_a_secret(tmp_path: Path):
    (tmp_path / ".env").write_text("SUPABASE_ANON_KEY=old\n", encoding="utf-8")
    settings.set_secrets(QaPaths(root=tmp_path), {"SUPABASE_ANON_KEY": ""})
    assert "SUPABASE_ANON_KEY" not in (tmp_path / ".env").read_text()


def test_a_partial_patch_leaves_other_sections_alone(tmp_path: Path):
    """Editing deployments must not silently reset the model provider."""
    paths = QaPaths(root=tmp_path)
    settings.apply(paths, {"model": {"provider": "kimi"}})
    settings.apply(paths, {"entrypoints": [{"name": "p", "url": "https://x.dev"}]})

    config = settings.view(paths).config
    assert config.model.provider.value == "kimi"
    assert config.entrypoints[0].url == "https://x.dev"
