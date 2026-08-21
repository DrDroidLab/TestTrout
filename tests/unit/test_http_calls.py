"""Discovering an app's calls to its own HTTP backend.

Not every application keeps its data in Supabase. For one with an ordinary API,
the fetch call sites *are* the backend surface — without them the scan reports a
list of screens that reach nothing, all scored low.
"""

from __future__ import annotations

from pathlib import Path

from testtrout.analysis.http_calls import discover, wrapper_names
from testtrout.analysis.ids import IdAllocator
from testtrout.analysis.parser import parse_file

CLIENT = """
export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';
export const sessionKey = (slug: string) => `session_${slug}`;

export async function api<T>(path: string, opts: Options = {}): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, { method: opts.method ?? 'GET' });
  return res.json();
}
"""


def _files(tmp_path: Path, sources: dict[str, str]) -> dict:
    out = {}
    for name, text in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        parsed = parse_file(path, tmp_path)
        assert parsed is not None
        out[parsed.rel] = parsed
    return out


def test_only_exports_that_call_fetch_count_as_wrappers(tmp_path: Path):
    """Regression: every export of the module used to qualify.

    A `sessionKey(slug)` living beside `api(path)` was scanned as an HTTP call
    and reported as an unresolvable endpoint.
    """
    names = wrapper_names(_files(tmp_path, {"lib/api.ts": CLIENT}))
    assert "api" in names
    assert "sessionKey" not in names
    assert "API_URL" not in names


def test_calls_through_the_wrapper_are_found(tmp_path: Path):
    files = _files(
        tmp_path,
        {
            "lib/api.ts": CLIENT,
            "page.tsx": "const jobs = await api('/jobs');",
        },
    )
    endpoints, warnings = discover(files, IdAllocator())
    assert [(e.methods[0], e.path) for e in endpoints] == [("GET", "/jobs")]
    assert warnings == []


def test_a_generic_call_is_found(tmp_path: Path):
    """`await api<Job>('/jobs')` parses with the callee wrapped in an await.

    A naive read of the callee misses every generic call, which in a typed
    client is most of them.
    """
    files = _files(
        tmp_path,
        {
            "lib/api.ts": CLIENT,
            "page.tsx": "const job = await api<Job>('/jobs/1', { method: 'PATCH' });",
        },
    )
    endpoints, _ = discover(files, IdAllocator())
    assert [(e.methods[0], e.path) for e in endpoints] == [("PATCH", "/jobs/1")]


def test_template_paths_become_route_patterns(tmp_path: Path):
    files = _files(
        tmp_path,
        {
            "lib/api.ts": CLIENT,
            "page.tsx": "await api(`/jobs/${id}/sessions`);",
        },
    )
    endpoints, _ = discover(files, IdAllocator())
    assert endpoints[0].path == "/jobs/:param/sessions"


def test_a_body_without_a_method_is_a_post(tmp_path: Path):
    files = _files(
        tmp_path,
        {
            "lib/api.ts": CLIENT,
            "page.tsx": "await api('/jobs', { body: { title: 'x' } });",
        },
    )
    assert discover(files, IdAllocator())[0][0].methods == ["POST"]


def test_the_wrappers_own_fetch_is_not_an_endpoint(tmp_path: Path):
    """`fetch(`${BASE}${path}`)` inside the client is plumbing, not a route."""
    endpoints, _ = discover(_files(tmp_path, {"lib/api.ts": CLIENT}), IdAllocator())
    assert endpoints == []


def test_a_direct_fetch_is_found(tmp_path: Path):
    files = _files(tmp_path, {"page.tsx": "await fetch('/api/health');"})
    endpoints, _ = discover(files, IdAllocator())
    assert [(e.methods[0], e.path) for e in endpoints] == [("GET", "/api/health")]


def test_an_absolute_url_to_another_service_is_ignored(tmp_path: Path):
    files = _files(tmp_path, {"page.tsx": "await fetch('https://api.stripe.com/v1/x');"})
    assert discover(files, IdAllocator())[0] == []


def test_a_computed_path_is_reported_not_guessed(tmp_path: Path):
    files = _files(
        tmp_path,
        {
            "lib/api.ts": CLIENT,
            "page.tsx": "await api(buildPath(id));",
        },
    )
    endpoints, warnings = discover(files, IdAllocator())
    assert endpoints == []
    assert any(w.code == "unresolved_endpoint" for w in warnings)
