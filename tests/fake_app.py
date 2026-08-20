"""A tiny stand-in for a Lovable-style app, used to test the prober.

Probing needs something real to load — a browser, a live HTTP server, actual
fetch calls. Pointing the tests at a deployed application would make them slow,
flaky, and dependent on someone else's uptime, so this serves the smallest
thing that exercises every path the prober cares about:

* several routes, one of which takes a parameter;
* PostgREST-shaped endpoints under ``/rest/v1/`` so requests classify as
  Supabase traffic;
* a read that returns rows (giving the prober an id to resolve ``/orders/:id``);
* a read that returns 403, standing in for a row-level security denial;
* a write endpoint, to prove the safety guard blocks it;
* ``data-testid`` attributes, so selector extraction has something to find.

Runs on an ephemeral port so tests never collide.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# Recorded so tests can assert that a blocked write never arrived.
WRITES_RECEIVED: list[str] = []

_PAGE = """<!doctype html>
<html><head><title>{title}</title></head>
<body>
  <h1 data-testid="page-heading">{title}</h1>
  <button data-testid="primary-action" aria-label="Primary action">Do the thing</button>
  <label for="q">Search</label><input id="q" type="text" />
  <script>
    (async () => {{
      for (const path of {fetches}) {{
        try {{ await fetch(path); }} catch (e) {{}}
      }}
      {writes}
    }})();
  </script>
</body></html>
"""

_WRITE_SNIPPET = """
      try {
        await fetch('/rest/v1/audit_log', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({action: 'page_view'}),
        });
      } catch (e) {}
"""

# path -> (title, reads it performs, whether it writes on load)
ROUTES: dict[str, tuple[str, list[str], bool]] = {
    "/": ("Home", [], False),
    "/login": ("Sign in", [], False),
    "/orders": ("Orders", ["/rest/v1/orders?select=id,total"], False),
    "/settings": ("Settings", ["/rest/v1/profiles?select=id,name"], False),
    # Writes on mount — the case that makes "just navigating is safe" false.
    "/checkout": ("Checkout", ["/rest/v1/orders?select=id"], True),
    # Returns 403, standing in for a policy denial.
    "/reports": ("Reports", ["/rest/v1/secrets?select=*"], False),
}


class _Handler(BaseHTTPRequestHandler):
    """Serves the fake application and its fake PostgREST."""

    def log_message(self, *args: object) -> None:
        """Silence the default stderr logging."""

    def do_GET(self) -> None:
        """Serve a route or a table read."""
        path = urlparse(self.path).path

        if path.startswith("/rest/v1/"):
            table = path.removeprefix("/rest/v1/").strip("/")
            if table == "secrets":
                self._json({"message": "permission denied for table secrets"}, status=403)
            elif table == "orders":
                self._json([{"id": "ord_1", "total": 4500}])
            else:
                self._json([{"id": "prof_1", "name": "Ada"}])
            return

        # A parameterised detail route: /orders/<id>
        if path.startswith("/orders/") and len(path.split("/")) == 3:
            self._html(_PAGE.format(title="Order detail", fetches="[]", writes=""))
            return

        route = ROUTES.get(path)
        if route is None:
            self._html("<h1>Not found</h1>", status=404)
            return
        title, fetches, writes = route
        self._html(
            _PAGE.format(
                title=title,
                fetches=json.dumps(fetches),
                writes=_WRITE_SNIPPET if writes else "",
            )
        )

    def do_POST(self) -> None:
        """Record a write. Should never be reached when the guard is working."""
        WRITES_RECEIVED.append(self.path)
        self._json({"ok": True}, status=201)

    def _html(self, body: str, status: int = 200) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, body: object, status: int = 200) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class FakeApp:
    """Context manager owning the server thread."""

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self) -> FakeApp:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        WRITES_RECEIVED.clear()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
