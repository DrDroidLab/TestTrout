"""Request classification and the write-blocking safety guard."""

from __future__ import annotations

import pytest

from testtrout.deployment.network import classify, should_block
from testtrout.domain.observation import CallKind

APP = "https://myapp.vercel.app"
SUPABASE = "https://abcdefgh.supabase.co"


def test_postgrest_read_resolves_table_and_operation():
    call = classify(f"{SUPABASE}/rest/v1/orders?select=id,total", "GET", APP, SUPABASE)
    assert call.kind is CallKind.SUPABASE_REST
    assert call.table == "orders"
    assert call.operation == "select"


@pytest.mark.parametrize(
    ("method", "operation"),
    [("POST", "insert"), ("PATCH", "update"), ("DELETE", "delete")],
)
def test_verbs_map_onto_operations(method: str, operation: str):
    call = classify(f"{SUPABASE}/rest/v1/orders", method, APP, SUPABASE)
    assert call.operation == operation


def test_rpc_path_is_recognised():
    call = classify(f"{SUPABASE}/rest/v1/rpc/recalc", "POST", APP, SUPABASE)
    assert call.operation == "rpc"
    assert call.table == "recalc"


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("/auth/v1/token", CallKind.SUPABASE_AUTH),
        ("/storage/v1/object/avatars/a.png", CallKind.SUPABASE_STORAGE),
        ("/functions/v1/send-mail", CallKind.SUPABASE_FUNCTION),
    ],
)
def test_supabase_subsystems_are_distinguished(path: str, kind: CallKind):
    assert classify(f"{SUPABASE}{path}", "POST", APP, SUPABASE).kind is kind


def test_own_origin_is_first_party_and_others_are_external():
    assert classify(f"{APP}/api/checkout", "POST", APP, SUPABASE).kind is CallKind.FIRST_PARTY
    assert classify("https://api.stripe.com/v1/pi", "POST", APP, SUPABASE).kind is CallKind.EXTERNAL


def test_reads_are_never_blocked():
    call = classify(f"{SUPABASE}/rest/v1/orders", "GET", APP, SUPABASE)
    assert should_block(call, writable=False) is False


def test_writes_are_blocked_on_a_non_disposable_deployment():
    """The guard that makes probing a production URL safe."""
    for method in ("POST", "PATCH", "PUT", "DELETE"):
        call = classify(f"{SUPABASE}/rest/v1/orders", method, APP, SUPABASE)
        assert should_block(call, writable=False) is True


def test_writes_are_allowed_once_a_deployment_is_disposable():
    call = classify(f"{SUPABASE}/rest/v1/orders", "DELETE", APP, SUPABASE)
    assert should_block(call, writable=True) is False


def test_sign_in_is_never_blocked():
    """Auth is a POST. Blocking it would defeat the entire probe."""
    call = classify(f"{SUPABASE}/auth/v1/token", "POST", APP, SUPABASE)
    assert should_block(call, writable=False) is False


def test_third_party_writes_are_blocked_too():
    """A test run must never be able to charge a card."""
    call = classify("https://api.stripe.com/v1/charges", "POST", APP, SUPABASE)
    assert should_block(call, writable=False) is True
