"""Shared machinery for the API-backed table functions and tables.

Every scan is a ``TableFunctionGenerator`` subclass of :class:`ApiScan`. A
subclass declares ``FIXED_SCHEMA`` and implements:

* ``listings(ctx)`` turns the call's arguments and the pushed-down SQL
  predicates into one or more API listings (path alternatives + query
  parameters). ``IN`` lists fan out into one listing per value, timestamp ranges
  become ``*_after`` / ``*_before`` parameters, and so on.
* ``rows(ctx, items, listing)`` maps one page of API objects to column values.

The cursor (:class:`ScanState`) is ``(listing index, path alternative, next
page URL)`` plus an optional resolved project scope. GitLab returns the next
page as a ``Link`` header for both offset and keyset pagination; the relative
URL is stored as-is, so a scan resumes on any worker process (HTTP transport)
and holds no credentials. ``auto_apply_filters`` re-applies every predicate to
each batch, so a translation only has to *narrow* what the API returns (a
superset is fine), never be exact.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, cast

import pyarrow as pa
from vgi.arguments import Secret
from vgi.invocation import BindResponse
from vgi.table_filter_pushdown import PushdownFilters
from vgi.table_function import (
    BindParams,
    OutputCollector,
    ProcessParams,
    TableFunctionGenerator,
    init_single_worker,
)
from vgi.table_in_out_function import RowTransformFunction
from vgi_rpc import ArrowSerializableDataclass

from .client import GITLAB_SECRET_TYPE, GitLabClient, GitLabConfig, NotFoundError
from .convert import batch_from_rows

log = logging.getLogger(__name__)

DETAIL_THREADS = 4


@dataclass(kw_only=True)
class ScanState(ArrowSerializableDataclass):
    """Resumable cursor: which listing, which path alternative, and the next page's URL."""

    listing: int = 0
    alt: int = 0
    next_url: str = ""
    scope: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Listing:
    """One API collection (or single object) to read.

    Attributes:
        paths: Path alternatives under ``/api/v4``; the first one that does not
            404 is used (e.g. a project, else a group with the same path).
        query: Query parameters (lists repeat the key).
        context: Values stamped on every row from this listing (e.g. ``repo``).
        single: The response is one object, not a list.
        text: The response is plain text (job logs).
        missing_ok: Every alternative 404ing yields no rows instead of an error.
        paginate: Send ``per_page`` and follow ``Link: rel=next``.

    """

    paths: tuple[str, ...]
    query: tuple[tuple[str, Any], ...] = ()
    context: tuple[tuple[str, Any], ...] = ()
    single: bool = False
    text: bool = False
    missing_ok: bool = False
    paginate: bool = True

    def ctx(self, key: str) -> Any:
        """Look up a context value."""
        return dict(self.context).get(key)


def secret_from(params: ProcessParams[Any]) -> dict[str, Any] | None:
    """Return the first resolved ``gitlab`` secret, if any."""
    for secret in params.secrets.of_type(GITLAB_SECRET_TYPE):
        return dict(secret)
    return None


def client_from(params: ProcessParams[Any]) -> GitLabClient:
    """Build a client from the secret DuckDB resolved for this call (anonymous gitlab.com without one)."""
    return GitLabClient(GitLabConfig.from_secret(secret_from(params)))


# ------------------------------------------------------------------------ pushdown


class Pushdown:
    """Read-only helpers over the predicates DuckDB pushed into a scan."""

    def __init__(self, filters: PushdownFilters | None) -> None:
        """Wrap the (possibly absent) pushdown filters."""
        self.filters = filters

    def values(self, column: str) -> list[Any] | None:
        """Exact values a column is constrained to (``=`` or ``IN``), else None."""
        if not self.filters:
            return None
        arr = self.filters.get_column_values(column)
        if arr is None:
            return None
        return [v for v in arr.to_pylist() if v is not None]

    def columns(self) -> set[str]:
        """Names of every column a pushed-down predicate references."""
        if not self.filters:
            return set()
        out: set[str] = set()
        stack = list(self.filters.filters)
        while stack:
            f = stack.pop()
            name = getattr(f, "column_name", None)
            if name:
                out.add(str(name))
            stack.extend(getattr(f, "children", None) or [])
        return out

    def one(self, column: str) -> Any | None:
        """Return the single value a column must equal, else None."""
        vals = self.values(column)
        return vals[0] if vals and len(vals) == 1 else None

    def bounds(self, column: str) -> tuple[Any, Any]:
        """``(low, high)`` inclusive-or-wider bounds for a column; either may be None."""
        if not self.filters:
            return None, None
        b = self.filters.get_column_bounds(column)
        if b is None:
            return None, None
        lo = b.min_value.as_py() if b.min_value is not None else None
        hi = b.max_value.as_py() if b.max_value is not None else None
        return lo, hi


def iso(value: Any) -> str | None:
    """Format a timestamp/date filter constant as ISO 8601 UTC, the way GitLab parses it."""
    if isinstance(value, dt.datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=dt.UTC)
        return aware.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, dt.date):
        return f"{value.isoformat()}T00:00:00Z"
    return None


def range_params(pd: Pushdown, column: str, after: str, before: str) -> list[tuple[str, Any]]:
    """Translate a timestamp range on ``column`` into GitLab ``<x>_after`` / ``<x>_before`` parameters.

    GitLab's ``_after`` / ``_before`` are inclusive-or-exclusive depending on the
    endpoint; a strict SQL bound widened to the API's semantics is still trimmed
    exactly by DuckDB's re-check. The upper bound is widened by one second so an
    exclusive ``_before`` never drops a row the SQL predicate keeps.

    Args:
        pd: The pushed-down filters.
        column: SQL column name.
        after: Lower-bound API parameter.
        before: Upper-bound API parameter.

    Returns:
        Query parameters to add.

    """
    lo, hi = pd.bounds(column)
    out: list[tuple[str, Any]] = []
    if isinstance(lo, dt.date):
        low = lo if isinstance(lo, dt.datetime) else dt.datetime(lo.year, lo.month, lo.day, tzinfo=dt.UTC)
        out.append((after, iso(low - dt.timedelta(seconds=1))))
    if isinstance(hi, dt.date):
        high = hi if isinstance(hi, dt.datetime) else dt.datetime(hi.year, hi.month, hi.day, tzinfo=dt.UTC)
        out.append((before, iso(high + dt.timedelta(seconds=1))))
    return out


def fan_out(base: Sequence[tuple[str, Any]], key: str, values: list[Any] | None) -> list[tuple[tuple[str, Any], ...]]:
    """One query per value of ``key`` (an ``IN`` list), or just ``base`` when unconstrained."""
    if not values:
        return [tuple(base)]
    return [(*base, (key, v)) for v in dict.fromkeys(values)]


# --------------------------------------------------------------------------- base


@dataclass(slots=True)
class ScanContext:
    """Everything a scan's hooks need for one ``process`` call."""

    client: GitLabClient
    args: Any
    pd: Pushdown
    wanted: set[str]
    scope: list[str]
    params: ProcessParams[Any]
    path: str = ""


def parallel_map[T, R](fn: Callable[[T], R], items: Sequence[T]) -> list[R]:
    """Map ``fn`` over ``items`` on a small thread pool (detail lookups), preserving order."""
    if len(items) <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(max_workers=DETAIL_THREADS) as pool:
        return list(pool.map(fn, items))


@init_single_worker
class ApiScan[TArgs](TableFunctionGenerator[TArgs, ScanState]):
    """Base class for scans that page through one or more GitLab listings."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema([])

    class Meta:
        """Overridden by every concrete scan."""

        name = "api_scan"
        description = "Base class for GitLab API scans"

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Inherit the ``gitlab`` secret request declared by :meth:`on_bind`.

        The SDK skips generic intermediate bases (``ApiScan[TArgs]``) when it
        scans ``on_bind`` for ``Secret()`` parameters, and a subclass that does
        not redefine ``on_bind`` inherits the base's empty result. Re-declare it
        for every concrete scan so DuckDB resolves the secret for all of them.
        """
        super().__init_subclass__(**kwargs)
        if "on_bind" not in cls.__dict__:
            cls._secret_params = {GITLAB_SECRET_TYPE: Secret(GITLAB_SECRET_TYPE)}

    @classmethod
    def on_bind(
        cls,
        params: BindParams[Any],
        *,
        gitlab: Annotated[dict[str, pa.Scalar[Any]] | None, Secret(GITLAB_SECRET_TYPE)] = None,
    ) -> BindResponse:
        """Declare the fixed schema and ask DuckDB for the optional ``gitlab`` secret."""
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def initial_state(cls, params: ProcessParams[TArgs]) -> ScanState:
        """Start at the first listing; resolve the project scope once if the scan needs one."""
        return ScanState(scope=cls.resolve_scope(params))

    @classmethod
    def resolve_scope(cls, params: ProcessParams[TArgs]) -> list[str]:
        """Return the projects a scan covers when its arguments do not say (unified tables)."""
        return []

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """Return the listings to read for these arguments and pushed-down predicates."""
        raise NotImplementedError

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map one page of API items to rows (dicts keyed by column name)."""
        raise NotImplementedError

    @classmethod
    def process(cls, params: ProcessParams[TArgs], state: ScanState, out: OutputCollector) -> None:
        """Read one page of the current listing, emit it, and advance the cursor."""
        client = client_from(params)
        pushdown = Pushdown(params.current_pushdown_filters)
        ctx = ScanContext(
            client=client,
            args=params.args,
            pd=pushdown,
            wanted=set(params.output_schema.names) | pushdown.columns(),
            scope=list(state.scope),
            params=params,
        )
        listings = cls.listings(ctx)
        while state.listing < len(listings):
            listing = listings[state.listing]
            target = state.next_url
            if not target:
                query = list(listing.query)
                if listing.paginate and not listing.single and not listing.text:
                    query.append(("per_page", client.config.per_page))
                target = client.build(listing.paths[state.alt], query)
            try:
                resp = client.get(target, text=listing.text)
            except NotFoundError:
                if state.next_url:
                    raise
                if state.alt + 1 < len(listing.paths):
                    state.alt += 1
                    continue
                if not listing.missing_ok:
                    raise
                state.listing, state.alt, state.next_url = state.listing + 1, 0, ""
                continue
            used_alt = state.alt
            if listing.single or listing.text:
                items: list[Any] = [resp.body]
            else:
                items = list(resp.body or [])
            if resp.next_url and items and listing.paginate and not listing.single:
                state.next_url = resp.next_url
            else:
                state.listing, state.alt, state.next_url = state.listing + 1, 0, ""
            ctx.path = listing.paths[used_alt]
            rows = cls.rows(ctx, items, listing) if items else []
            if rows:
                out.emit(batch_from_rows(rows, params.output_schema))
                return
        out.finish()


# ------------------------------------------------------------------ lookups

#: A lookup walks at most this many pages per input row (100 objects each).
MAX_LOOKUP_PAGES = 100


def read_listing(
    ctx: ScanContext,
    listing: Listing,
    rows: Callable[[ScanContext, list[Any], Listing], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Read every page of one listing (bounded); a 404 on every alternative yields no rows."""
    client = ctx.client
    out: list[dict[str, Any]] = []
    for path in listing.paths:
        query = list(listing.query)
        if listing.paginate and not listing.single and not listing.text:
            query.append(("per_page", client.config.per_page))
        target: str | None = client.build(path, query)
        pages = 0
        try:
            while target and pages < MAX_LOOKUP_PAGES:
                resp = client.get(target, text=listing.text)
                pages += 1
                items = [resp.body] if listing.single or listing.text else list(resp.body or [])
                ctx.path = path
                if items:
                    out.extend(rows(ctx, items, listing))
                target = resp.next_url if listing.paginate and not listing.single and items else None
        except NotFoundError:
            continue
        return out
    return out


@dataclass(slots=True)
class _RowArgs:
    """Positional (per-row) and named (bind-time) arguments of one lookup input row."""

    values: dict[str, Any]

    def __getattr__(self, name: str) -> Any:
        try:
            return self.values[name]
        except KeyError as err:
            raise AttributeError(name) from err


class Lookup[TArgs](RowTransformFunction[TArgs]):
    """Base class for keyed lookups that also work as the inner side of a ``LATERAL``.

    These are *blended* table functions: their positional arguments are per-row
    input columns, so ``f('a/b', 1)`` and ``FROM t, LATERAL f(t.repo, t.number)``
    are both served. Every input row's listing is read in full (bounded by
    ``MAX_LOOKUP_PAGES``) and emitted with ``parent_rows`` provenance. A key
    that names nothing (404), or a NULL key, yields no rows for that input row.
    Subclasses implement ``listings(ctx)`` and ``rows(ctx, items, listing)``
    exactly like :class:`ApiScan`, and list their positional fields in ``KEYS``.
    """

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema([])
    KEYS: ClassVar[tuple[str, ...]] = ()

    class Meta:
        """Overridden by every concrete lookup."""

        name = "lookup"
        description = "Base class for GitLab lookups"

    @classmethod
    def on_bind(cls, params: BindParams[Any]) -> BindResponse:
        """Declare the fixed output schema."""
        return BindResponse(output_schema=cls.FIXED_SCHEMA)

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """Return the listings to read for one input row."""
        raise NotImplementedError

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map one page of API items to rows."""
        raise NotImplementedError

    @classmethod
    def process(cls, params: ProcessParams[TArgs], state: None, batch: pa.RecordBatch, out: OutputCollector) -> None:
        """Look up every input row (repeated keys once) and emit one 1->N batch with provenance."""
        client = client_from(params)
        named = {
            f: getattr(params.args, f)
            for f in getattr(params.args, "__dataclass_fields__", {})
            if f not in cls.KEYS and hasattr(params.args, f)
        }
        keys = list(zip(*(batch.column(k).to_pylist() for k in cls.KEYS), strict=True))
        fetched: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        rows: list[dict[str, Any]] = []
        parents: list[int] = []
        for index, key in enumerate(keys):
            if any(part is None for part in key):
                continue
            if key not in fetched:
                ctx = ScanContext(
                    client=client,
                    args=_RowArgs({**named, **dict(zip(cls.KEYS, key, strict=True))}),
                    pd=Pushdown(None),
                    wanted=set(params.output_schema.names),
                    scope=[],
                    params=params,
                )
                found: list[dict[str, Any]] = []
                for listing in cls.listings(ctx):
                    found.extend(read_listing(ctx, listing, cls.rows))
                fetched[key] = found
            rows.extend(fetched[key])
            parents.extend([index] * len(fetched[key]))
        cast("Any", out).emit(batch_from_rows(rows, params.output_schema), parent_rows=parents)
