"""HTTP client for the GitLab REST API v4 (gitlab.com and self-managed).

Everything that touches the network lives here:

* **Configuration** comes from an optional DuckDB secret of type ``gitlab``
  (``token``, ``base_url``, ``send_as``, ``per_page``, ``timeout``). Without a
  secret the worker reads ``https://gitlab.com`` anonymously, which covers public
  projects. Nothing is read from the environment.
* **Authentication**: ``Authorization: Bearer <token>`` (personal, project,
  group and OAuth tokens), or ``PRIVATE-TOKEN`` / ``JOB-TOKEN`` headers when
  ``send_as`` says so.
* **Pagination**: GitLab's ``Link: <...>; rel="next"`` header, which it sends
  for both offset and keyset pagination. The next URL is returned to the caller
  as a relative ``path?query`` string so it can be stored in table-function
  state (it carries no credentials), and it is re-anchored on the configured
  base URL so a misconfigured ``external_url`` can never send the token to
  another host.
* **Rate limits**: 429 and 502/503/504 are retried with backoff honouring
  ``Retry-After`` and ``RateLimit-Reset``; when ``RateLimit-Remaining`` reaches 0
  the next request waits for the reset (capped).

Credentials are never written to table-function state or logs: every
``process`` call rebuilds the client from the secret the SDK hands it.
"""

from __future__ import annotations

import email.utils
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa
from vgi.catalog import SecretTypeSpec

log = logging.getLogger(__name__)

GITLAB_SECRET_TYPE = "gitlab"
DEFAULT_BASE_URL = "https://gitlab.com"
USER_AGENT = "vgi-gitlab-worker/0.1"
DEFAULT_PER_PAGE = 100
MAX_ATTEMPTS = 5
MAX_WAIT_SECONDS = 60.0
RETRY_STATUSES = frozenset({429, 502, 503, 504})
SEND_AS = ("bearer", "private", "job")
DOC_URL = "https://github.com/Query-farm/vgi-gitlab#credentials"


def _field(name: str, doc: str, type_: pa.DataType | None = None, *, redact: bool = False) -> Any:
    meta = {"vgi_doc": doc}
    if redact:
        meta["redact"] = "true"
    return pa.field(name, type_ or pa.string(), metadata=meta)


GITLAB_SECRET = SecretTypeSpec(
    name=GITLAB_SECRET_TYPE,
    description=(
        "Access to one GitLab instance's REST API v4: an access token and, for self-managed GitLab, "
        "the instance's base URL (default https://gitlab.com)."
    ),
    schema=pa.schema(
        [
            _field(
                "token",
                "Personal, project or group access token (read_api scope is enough), or an OAuth access token. "
                "Optional for public projects on gitlab.com.",
                redact=True,
            ),
            _field(
                "base_url",
                "Root URL of the GitLab instance without /api/v4, e.g. 'https://gitlab.example.com'. "
                "Default 'https://gitlab.com'.",
            ),
            _field(
                "send_as",
                "How the token is sent: 'bearer' (default; Authorization: Bearer), 'private' (PRIVATE-TOKEN "
                "header) or 'job' (JOB-TOKEN header, for CI job tokens).",
            ),
            _field("per_page", "Items requested per API page, 1 to 100 (default 100).", pa.int64()),
            _field("timeout", "Per-request timeout in seconds (default 30).", pa.float64()),
        ],
        metadata={"vgi_doc_url": DOC_URL},
    ),
)


class GitLabError(RuntimeError):
    """The GitLab API returned an error, or the secret is unusable."""


class NotFoundError(GitLabError):
    """HTTP 404 from the API (missing object, or no access to it)."""


@dataclass(frozen=True, slots=True)
class GitLabConfig:
    """Connection settings parsed from a ``gitlab`` secret (or the anonymous gitlab.com default)."""

    base_url: str = DEFAULT_BASE_URL
    token: str | None = field(default=None, repr=False)
    send_as: str = "bearer"
    per_page: int = DEFAULT_PER_PAGE
    timeout: float = 30.0

    @classmethod
    def from_secret(cls, secret: Mapping[str, Any] | None) -> GitLabConfig:
        """Build and validate a config from a resolved secret's key/value pairs.

        Args:
            secret: Secret fields (values may be Arrow scalars), or None for anonymous gitlab.com.

        Returns:
            The parsed configuration.

        Raises:
            GitLabError: A field has an invalid value.

        """
        values: dict[str, Any] = {}
        for key, value in (secret or {}).items():
            plain = value.as_py() if hasattr(value, "as_py") else value
            if plain is not None and plain != "":
                values[str(key).lower()] = plain
        base_url = str(values.get("base_url", DEFAULT_BASE_URL)).rstrip("/")
        if base_url.endswith("/api/v4"):
            base_url = base_url[: -len("/api/v4")]
        if not base_url.startswith(("http://", "https://")):
            raise GitLabError(f"gitlab secret: base_url must start with http:// or https://, got {base_url!r}")
        send_as = str(values.get("send_as", "bearer")).lower()
        if send_as not in SEND_AS:
            raise GitLabError(f"gitlab secret: send_as must be one of {', '.join(SEND_AS)}, got {send_as!r}")
        per_page = int(values.get("per_page", DEFAULT_PER_PAGE))
        if not 1 <= per_page <= 100:
            raise GitLabError(f"gitlab secret: per_page must be between 1 and 100, got {per_page}")
        timeout = float(values.get("timeout", 30.0))
        if timeout <= 0:
            raise GitLabError("gitlab secret: timeout must be positive")
        token = values.get("token")
        return cls(
            base_url=base_url,
            token=str(token) if token else None,
            send_as=send_as,
            per_page=per_page,
            timeout=timeout,
        )


def encode_id(project_or_group: str | int) -> str:
    """URL-encode a project/group path ('group/sub/name') or pass a numeric id through."""
    text = str(project_or_group).strip().strip("/")
    if not text:
        raise GitLabError("a project or group path is required, e.g. 'gitlab-org/gitlab'")
    return urllib.parse.quote(text, safe="")


def next_link(link_header: str | None) -> str | None:
    """Return the ``rel="next"`` URL of an RFC 8288 ``Link`` header, if any."""
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.strip()
        if not section.startswith("<") or ">" not in section:
            continue
        url, _, params = section[1:].partition(">")
        rels = [p.strip() for p in params.split(";") if p.strip().startswith("rel=")]
        for rel in rels:
            if "next" in rel.split("=", 1)[1].strip("\"'").split():
                return url
    return None


def _wait_seconds(headers: Mapping[str, str], attempt: int) -> float:
    """Seconds to wait before retrying, from ``Retry-After`` / ``RateLimit-Reset`` or backoff."""
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            return float(min(max(float(retry_after), 0.0), MAX_WAIT_SECONDS))
        except ValueError:
            when = email.utils.parsedate_to_datetime(retry_after)
            if when is not None:
                return min(max(float(when.timestamp()) - time.time(), 0.0), MAX_WAIT_SECONDS)
    reset = headers.get("RateLimit-Reset")
    if reset and reset.isdigit():
        return float(min(max(int(reset) - time.time(), 0.0), MAX_WAIT_SECONDS))
    return float(min(0.5 * 2**attempt, MAX_WAIT_SECONDS))


@dataclass(slots=True)
class Response:
    """A decoded API response: body plus the pagination pointer."""

    body: Any
    next_url: str | None
    headers: dict[str, str]


class GitLabClient:
    """Minimal read-only REST v4 client: GET with auth, retries, rate limits and pagination."""

    def __init__(self, config: GitLabConfig) -> None:
        """Hold the connection settings."""
        self.config = config
        self._parsed_base = urllib.parse.urlsplit(config.base_url)
        self._throttle_until = 0.0

    @property
    def api_root(self) -> str:
        """``<base_url>/api/v4``."""
        return f"{self.config.base_url}/api/v4"

    def web_url(self, path: str) -> str:
        """Return a link into the GitLab web UI, ``<base_url>/<path>``."""
        return f"{self.config.base_url}/{path.lstrip('/')}"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        token = self.config.token
        if token:
            if self.config.send_as == "private":
                headers["PRIVATE-TOKEN"] = token
            elif self.config.send_as == "job":
                headers["JOB-TOKEN"] = token
            else:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    def build(self, path: str, query: Mapping[str, Any] | list[tuple[str, Any]] | None = None) -> str:
        """Relative request target ``/api/v4/<path>?<query>`` (lists repeat keys, booleans become true/false)."""
        pairs: list[tuple[str, str]] = []
        items = query.items() if isinstance(query, Mapping) else (query or [])
        for key, value in items:
            if value is None:
                continue
            for v in value if isinstance(value, list | tuple) else [value]:
                pairs.append((key, ("true" if v else "false") if isinstance(v, bool) else str(v)))
        prefix = self._parsed_base.path.rstrip("/")
        target = f"{prefix}/api/v4/{path.lstrip('/')}"
        return f"{target}?{urllib.parse.urlencode(pairs)}" if pairs else target

    def relative(self, absolute: str) -> str:
        """Strip scheme and host from a server-provided URL, keeping path and query."""
        parts = urllib.parse.urlsplit(absolute)
        return urllib.parse.urlunsplit(("", "", parts.path, parts.query, ""))

    def get(self, target: str, *, text: bool = False) -> Response:
        """GET a relative target (from :meth:`build` or a stored next link) on the configured host.

        Args:
            target: ``/api/v4/...?...`` (relative; always sent to the configured base URL).
            text: Return the body as text instead of decoding JSON.

        Returns:
            The decoded body and the relative URL of the next page, if any.

        Raises:
            NotFoundError: HTTP 404.
            GitLabError: Any other non-retryable error, or retries exhausted.

        """
        base = self._parsed_base
        url = urllib.parse.urlunsplit((base.scheme, base.netloc, "", "", "")) + target
        headers = self._headers()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            delay = self._throttle_until - time.time()
            if delay > 0:
                log.info("GitLab rate limit exhausted; waiting %.1fs for the reset", delay)
                time.sleep(min(delay, MAX_WAIT_SECONDS))
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout) as response:
                    resp_headers = dict(response.headers.items())
                    raw = response.read()
            except urllib.error.HTTPError as err:
                err_headers = dict((err.headers or {}).items())
                if err.code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                    wait = _wait_seconds(err_headers, attempt)
                    log.info("GitLab %s on %s; retrying in %.1fs", err.code, target.split("?")[0], wait)
                    time.sleep(wait)
                    continue
                detail = _error_detail(err)
                if err.code == 404:
                    raise NotFoundError(f"GitLab GET {target.split('?')[0]} failed with HTTP 404: {detail}") from err
                hint = ""
                if err.code in (401, 403):
                    hint = " (check the token and its read_api scope in the gitlab secret)"
                raise GitLabError(
                    f"GitLab GET {target.split('?')[0]} failed with HTTP {err.code}: {detail}{hint}"
                ) from err
            except urllib.error.URLError as err:
                if attempt == MAX_ATTEMPTS:
                    raise GitLabError(f"GitLab GET {url.split('?')[0]} failed: {err.reason}") from err
                time.sleep(min(0.5 * 2**attempt, MAX_WAIT_SECONDS))
                continue
            self._note_rate_limit(resp_headers)
            body: Any = raw.decode("utf-8", errors="replace") if text else (json.loads(raw) if raw else None)
            nxt = next_link(resp_headers.get("Link") or resp_headers.get("link"))
            if nxt is None and resp_headers.get("X-Next-Page"):
                # Offset pagination without a Link header (some proxies strip it).
                parts = urllib.parse.urlsplit(target)
                query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k != "page"]
                query.append(("page", resp_headers["X-Next-Page"]))
                nxt = f"{parts.path}?{urllib.parse.urlencode(query)}"
            return Response(body=body, next_url=self.relative(nxt) if nxt else None, headers=resp_headers)
        raise GitLabError(f"GitLab GET {target.split('?')[0]} failed after {MAX_ATTEMPTS} attempts")

    def _note_rate_limit(self, headers: Mapping[str, str]) -> None:
        remaining = headers.get("RateLimit-Remaining")
        reset = headers.get("RateLimit-Reset")
        if remaining is not None and remaining.strip() == "0" and reset and reset.isdigit():
            self._throttle_until = min(float(reset), time.time() + MAX_WAIT_SECONDS)


def _error_detail(err: urllib.error.HTTPError) -> str:
    """GitLab's ``message``/``error`` text from an error body, else the raw prefix."""
    try:
        raw = err.read(2000).decode(errors="replace")
    except OSError:
        return err.reason or ""
    try:
        decoded = json.loads(raw)
    except ValueError:
        return raw[:300]
    if isinstance(decoded, dict):
        msg = decoded.get("message") or decoded.get("error") or decoded
        return msg if isinstance(msg, str) else json.dumps(msg)[:300]
    return raw[:300]
