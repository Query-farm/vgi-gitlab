#!/usr/bin/env python3
"""A dependency-free replay of the GitLab REST API v4, used by the tests.

GitLab CE is far too heavy to run in CI here, so the worker is tested against
hand-built responses in GitLab's documented shapes (``test/fixtures/gitlab_fixture.py``).
The server is deliberately strict, so the tests prove what the worker sends:

* **Unknown query parameters are rejected** with HTTP 400. Every route lists the
  parameters it accepts; a pushdown translation that invents a parameter fails.
* Accepted filter parameters are *applied* (equality, ``IN``-style ``x[]`` lists,
  timestamp ranges, label sets, substring search), so pushdown changes what
  comes back exactly the way GitLab would.
* Offset pagination (``page``/``per_page``) and keyset pagination
  (``pagination=keyset`` with an opaque ``cursor``) answer with a ``Link:
  <...>; rel="next"`` header. The links name a *different host*
  (``gitlab.fixture.invalid``), the way GitLab behind a proxy with a stale
  ``external_url`` does, so the worker must re-anchor them on its base URL.
* ``--token T``: every non-public route needs ``Authorization: Bearer T`` or
  ``PRIVATE-TOKEN: T`` (401 otherwise).
* ``--flaky``: the first request to every path gets ``429`` with
  ``Retry-After: 0`` and ``RateLimit-*`` headers, exercising the retry path.
* ``GET /__requests`` returns the request log (for unit tests); ``POST
  /__reset`` clears it.

Usage::

    python3 test/mock_api/server.py [--token T] [--flaky] [--port N]   # prints PORT:<n>
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "fixtures"))

from gitlab_fixture import ROUTES

LINK_HOST = "http://gitlab.fixture.invalid"
PAGINATION = {"per_page", "page"}
KEYSET = {"pagination", "cursor", "order_by", "sort"}

_log: list[dict[str, Any]] = []
_flaky_seen: set[str] = set()
_lock = threading.Lock()


def _ts(value: Any) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _get(obj: Any, dotted: str) -> Any:
    for key in dotted.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _match(item: dict[str, Any], spec: dict[str, Any], values: list[str]) -> bool:
    op = spec.get("op", "eq")
    fields = spec.get("field", "")
    value = values[-1]
    actual = _get(item, fields) if isinstance(fields, str) else None
    if op == "eq":
        if isinstance(actual, bool):
            return actual == (value.lower() in ("true", "yes", "1"))
        return str(actual) == value
    if op == "yesno":
        return bool(actual) == (value == "yes")
    if op == "in":
        return str(actual) in values
    if op in ("gte", "lte"):
        when, bound = _ts(actual), _ts(value)
        if when is None or bound is None:
            return False
        return when >= bound if op == "gte" else when <= bound
    if op == "contains":
        haystacks = [str(_get(item, f) or "") for f in (fields if isinstance(fields, list) else [fields])]
        return any(value.lower() in h.lower() for h in haystacks)
    if op == "anchored":
        name = str(actual)
        if value.startswith("^") and value.endswith("$"):
            return name == value[1:-1]
        if value.startswith("^"):
            return name.startswith(value[1:])
        return value in name
    if op == "labels":
        wanted = {v for v in value.split(",") if v}
        return wanted <= set(actual or [])
    if op == "path":
        return any(p == value or p.startswith(value.rstrip("/") + "/") for p in item.get("_paths", []))
    if op == "ref":
        return value in item.get("_refs", [])
    if op == "ignore":
        return True
    raise ValueError(f"unknown op {op}")


def _clean(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


class Handler(BaseHTTPRequestHandler):
    """Serve the fixture routes."""

    token: str | None = None
    flaky = False

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        """Keep stderr quiet."""

    def _send(self, status: int, body: Any, headers: dict[str, str] | None = None, *, text: bool = False) -> None:
        data = (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain" if text else "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        """``/__reset`` clears the request log."""
        if self.path == "/__reset":
            with _lock:
                _log.clear()
            self._send(200, {"ok": True})
            return
        self._send(405, {"message": "405 Method Not Allowed: the worker is read-only"})

    def do_GET(self) -> None:
        """Route a GET to its fixture."""
        parts = urlsplit(self.path)
        query = parse_qsl(parts.query, keep_blank_values=True)
        if parts.path == "/__requests":
            with _lock:
                self._send(200, list(_log))
            return
        with _lock:
            _log.append({"path": parts.path, "query": query})
        route = ROUTES.get(parts.path)
        if route is None:
            self._send(404, {"message": "404 Not Found"})
            return
        if not route.get("public") and self.token:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {self.token}" and self.headers.get("PRIVATE-TOKEN") != self.token:
                self._send(401, {"message": "401 Unauthorized"})
                return
        if self.flaky:
            with _lock:
                first = parts.path not in _flaky_seen
                _flaky_seen.add(parts.path)
            if first:
                reset = str(int(time.time()))
                self._send(
                    429,
                    {"message": "429 Too Many Requests"},
                    {"Retry-After": "0", "RateLimit-Remaining": "0", "RateLimit-Reset": reset},
                )
                return
        filters: dict[str, Any] = route.get("params", {})
        allowed = set(filters) | set(route.get("accept", []))
        is_list = "items" in route
        if is_list:
            allowed |= PAGINATION | (KEYSET if route.get("keyset") else set())
        grouped: dict[str, list[str]] = {}
        for k, v in query:
            grouped.setdefault(k, []).append(v)
        unknown = sorted(set(grouped) - allowed)
        if unknown:
            self._send(400, {"error": f"unknown query parameter(s): {', '.join(unknown)}"})
            return
        rl = {"RateLimit-Limit": "2000", "RateLimit-Remaining": "1999", "RateLimit-Reset": str(int(time.time()) + 60)}
        if "text" in route:
            self._send(200, route["text"], rl, text=True)
            return
        if "object" in route:
            self._send(200, _clean(route["object"]), rl)
            return
        items = [i for i in route["items"] if all(_match(i, filters[k], v) for k, v in grouped.items() if k in filters)]
        for k, v in grouped.items():
            strip = route.get("strip_unless", {}).get(k)
            if strip and v[-1] != "true":
                items = [{f: x for f, x in i.items() if f != strip} for i in items]
        retried = route.get("strip_retried_unless")
        if retried and grouped.get(retried, [""])[-1] != "true":
            items = [i for i in items if not i.get("_retried")]
        for field, param in route.get("strip_unless_absent", {}).items():
            if param not in grouped:
                items = [{f: x for f, x in i.items() if f != field} for i in items]
        per_page = int(grouped.get("per_page", ["20"])[-1])
        if grouped.get("pagination", [""])[-1] == "keyset":
            cursor = grouped.get("cursor", [""])[-1]
            start = int(base64.urlsafe_b64decode(cursor.encode()).decode()) if cursor else 0
        else:
            start = (int(grouped.get("page", ["1"])[-1]) - 1) * per_page
        page = items[start : start + per_page]
        headers = dict(rl)
        if start + per_page < len(items):
            nxt = [(k, v) for k, v in query if k not in ("page", "cursor")]
            if grouped.get("pagination", [""])[-1] == "keyset":
                nxt.append(("cursor", base64.urlsafe_b64encode(str(start + per_page).encode()).decode()))
            else:
                page_no = start // per_page + 2
                nxt.append(("page", str(page_no)))
                headers["X-Next-Page"] = str(page_no)
            headers["Link"] = f'<{LINK_HOST}{parts.path}?{urlencode(nxt)}>; rel="next"'
        if grouped.get("pagination", [""])[-1] != "keyset":
            headers["X-Total"] = str(len(items))
        self._send(200, _clean(page), headers)


def main() -> None:
    """Serve until killed, printing ``PORT:<n>`` on stdout once listening."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--token", default=None)
    parser.add_argument("--flaky", action="store_true")
    args = parser.parse_args()
    Handler.token = args.token
    Handler.flaky = args.flaky
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"PORT:{server.server_address[1]}", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
