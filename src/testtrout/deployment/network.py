"""Classifying requests seen in the browser.

The point is to answer one question per request: *is this the application
talking to its own database, to itself, or to somebody we will have to
substitute during a test run?* Everything downstream — the substitution
boundary, RLS-denial detection, reconciliation against the static scan —
depends on getting that classification right.
"""

from __future__ import annotations

from urllib.parse import urlparse

from testtrout.domain.observation import CallKind, NetworkCall

# PostgREST maps HTTP verbs onto operations. PATCH is an update; a POST with a
# `Prefer: resolution=merge-duplicates` header is an upsert, but the verb alone
# is enough for reconciliation and does not require reading headers.
_VERB_TO_OPERATION = {
    "GET": "select",
    "HEAD": "select",
    "POST": "insert",
    "PATCH": "update",
    "PUT": "update",
    "DELETE": "delete",
}

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Hosts that carry no application meaning. Filtering them keeps the external
# host list — which becomes the substitution boundary — readable.
_NOISE_HOSTS = frozenset(
    {
        "fonts.googleapis.com",
        "fonts.gstatic.com",
        "cdn.jsdelivr.net",
        "unpkg.com",
    }
)


def classify(url: str, method: str, app_origin: str, supabase_url: str | None) -> NetworkCall:
    """Classify one request without recording its body.

    Args:
        url: Full request URL.
        method: HTTP method.
        app_origin: The deployment's own origin, to separate first-party calls.
        supabase_url: The project's Supabase URL, when configured.
    """
    parsed = urlparse(url)
    host = parsed.netloc
    path = parsed.path

    kind = CallKind.EXTERNAL
    table: str | None = None
    operation: str | None = None

    if supabase_url and host == urlparse(supabase_url).netloc:
        if path.startswith("/rest/v1/"):
            kind = CallKind.SUPABASE_REST
            table = _table_from_path(path)
            operation = (
                "rpc" if path.startswith("/rest/v1/rpc/") else _VERB_TO_OPERATION.get(method)
            )
        elif path.startswith("/auth/v1/"):
            kind = CallKind.SUPABASE_AUTH
        elif path.startswith("/storage/v1/"):
            kind = CallKind.SUPABASE_STORAGE
        elif path.startswith("/realtime/v1/"):
            kind = CallKind.SUPABASE_REALTIME
        elif path.startswith("/functions/v1/"):
            kind = CallKind.SUPABASE_FUNCTION
        else:
            kind = CallKind.SUPABASE_REST
    elif host == urlparse(app_origin).netloc:
        kind = CallKind.FIRST_PARTY

    return NetworkCall(
        method=method, url=url, host=host, kind=kind, table=table, operation=operation
    )


def _table_from_path(path: str) -> str | None:
    """Pull the table (or function) name out of a PostgREST path."""
    remainder = path.removeprefix("/rest/v1/")
    if remainder.startswith("rpc/"):
        remainder = remainder.removeprefix("rpc/")
    name = remainder.split("?")[0].strip("/")
    return name or None


def is_noise(host: str) -> bool:
    """Whether a host is a CDN or font provider rather than a real dependency."""
    return host in _NOISE_HOSTS


def should_block(call: NetworkCall, writable: bool) -> bool:
    """Whether the prober must refuse to let this request through.

    This is the guard that makes probing a production URL safe. Loading a page
    read-only is not by itself safe — the application may fire a write on
    mount, log an analytics event, or record a visit. So writes are blocked at
    the network layer unless the entrypoint is explicitly disposable, rather
    than trusting that navigation alone is harmless.

    First-party navigation and asset loads are never blocked; only requests
    that would change state on the backend or at a third party.
    """
    if writable:
        return False
    if call.method not in MUTATING_METHODS:
        return False
    # Signing in is a POST, and blocking it would defeat the entire probe.
    if call.kind is CallKind.SUPABASE_AUTH:
        return False
    return call.kind in {
        CallKind.SUPABASE_REST,
        CallKind.SUPABASE_STORAGE,
        CallKind.SUPABASE_FUNCTION,
        CallKind.EXTERNAL,
        CallKind.FIRST_PARTY,
    }
