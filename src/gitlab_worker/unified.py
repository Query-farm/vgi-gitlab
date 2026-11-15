"""The ``unified`` schema: pipelines and jobs in the cross-orchestrator shape.

The same two tables (same columns, types and status vocabulary) are served by
the Airflow, Dagster, Prefect, Temporal, Databricks, dbt and GitHub workers, so
``UNION ALL`` across them just works.

GitLab specifics:

* ``pipeline`` is the project path: a project's ``.gitlab-ci.yml`` is the
  pipeline definition.
* ``run_id`` is ``'<project path>/<pipeline id>'`` and ``task_run_id``
  ``'<project path>/<pipeline id>/<job id>'``, so both parse back into API
  paths when a query filters on them.
* Scope: a GitLab instance has millions of pipelines, so the tables read only
  the projects a query names (``pipeline =`` / ``IN``, ``run_id =`` /
  ``IN``, ``task_run_id =``) or, failing that, the ATTACH options
  ``projects`` ('a/b,c/d') and ``group_path`` (every project in the group).
* ``attempt`` is NULL: GitLab's REST API does not number retries; each retry
  is its own job (and row).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

import pyarrow as pa
from vgi.table_function import ProcessParams

from .ci import JOB_SCOPES, with_details
from .client import GitLabError, encode_id
from .convert import TSTZ, get, seconds_between, to_float
from .meta import column
from .scan import ApiScan, Listing, ScanContext, client_from, fan_out, iso

ORCHESTRATOR = "gitlab"
# unified.runs columns that only the per-pipeline detail request can fill.
UNIFIED_DETAIL_COLUMNS = frozenset({"started_at", "ended_at", "duration_seconds"})

PIPELINE_STATUS = {
    "created": "queued",
    "waiting_for_resource": "queued",
    "preparing": "queued",
    "pending": "queued",
    "scheduled": "queued",
    # A pipeline blocked on a manual job is waiting for a person.
    "manual": "queued",
    "running": "running",
    "canceling": "running",
    "success": "success",
    "failed": "failed",
    "canceled": "cancelled",
    "skipped": "skipped",
}
JOB_STATUS = {
    **PIPELINE_STATUS,
    # A manual job nobody started did not run.
    "manual": "skipped",
}
TRIGGERS = {
    "schedule": "schedule",
    "web": "manual",
    "webide": "manual",
    "chat": "manual",
    "api": "api",
    "trigger": "api",
    "push": "event",
    "merge_request_event": "event",
    "external_pull_request_event": "event",
    "pipeline": "event",
    "parent_pipeline": "event",
    "external": "event",
}


def run_status(raw: str | None) -> str:
    """Map a GitLab pipeline status onto the unified vocabulary."""
    return PIPELINE_STATUS.get(raw or "", "queued")


def task_status(raw: str | None) -> str:
    """Map a GitLab job status onto the unified vocabulary."""
    return JOB_STATUS.get(raw or "", "queued")


def trigger(source: str | None) -> str | None:
    """Map a pipeline source onto schedule, manual, api, event or other."""
    if source is None:
        return None
    return TRIGGERS.get(source, "other")


def raw_statuses(mapping: dict[str, str], wanted: list[Any] | None) -> tuple[list[str] | None, bool]:
    """GitLab statuses whose unified status is in ``wanted`` (None = no filter), and whether any exist."""
    if wanted is None:
        return None, True
    targets = {str(w) for w in wanted}
    raw = sorted(k for k, v in mapping.items() if v in targets)
    return raw, bool(raw)


def split_id(value: Any, parts: int) -> tuple[str, list[int]] | None:
    """Parse '<project path>/<n1>[/<n2>]' into (path, [n1, n2]); None if it is not one of ours."""
    text = str(value)
    pieces = text.rsplit("/", parts)
    if len(pieces) != parts + 1 or not pieces[0]:
        return None
    try:
        return pieces[0], [int(p) for p in pieces[1:]]
    except ValueError:
        return None


def scope_from_options(params: ProcessParams[Any]) -> list[str]:
    """Projects named by the ATTACH options ``projects`` and ``group_path`` (the group's projects are listed)."""
    try:
        options = params.attach_options
    except Exception:
        options = {}
    projects = [p.strip() for p in str(options.get("projects") or "").split(",") if p.strip()]
    group = str(options.get("group_path") or "").strip()
    if group:
        client = client_from(params)
        target: str | None = client.build(
            f"groups/{encode_id(group)}/projects",
            [("include_subgroups", True), ("archived", False), ("per_page", client.config.per_page)],
        )
        while target:
            resp = client.get(target)
            projects += [str(p["path_with_namespace"]) for p in resp.body or [] if p.get("path_with_namespace")]
            target = resp.next_url
    return list(dict.fromkeys(projects))


NO_SCOPE = (
    "gitlab.unified.{table} needs a project scope: filter on {hint}, or ATTACH with the `projects` "
    "('group/a,group/b') or `group_path` option"
)


@dataclass(slots=True, frozen=True, kw_only=True)
class NoArgs:
    """Catalog tables take no arguments."""


RUN_FIELDS = [
    column("orchestrator", pa.string(), "Always 'gitlab'.", nullable=False),
    column("run_id", pa.string(), "'<project path>/<pipeline id>'. `=` / IN is pushed down.", nullable=False),
    column("pipeline", pa.string(), "Project path, e.g. 'gitlab-org/cli'. `=` / IN scopes the scan."),
    column(
        "status", pa.string(), "queued, running, success, failed, cancelled or skipped. Pushed down.", nullable=False
    ),
    column("raw_status", pa.string(), "GitLab's pipeline status, e.g. 'canceled' or 'manual'."),
    column("trigger", pa.string(), "schedule, manual (web UI), api (API / trigger token), event (push, MR) or other."),
    column("queued_at", TSTZ, "When the pipeline was created."),
    column("started_at", TSTZ, "When its first job started."),
    column("ended_at", TSTZ, "When it finished."),
    column("duration_seconds", pa.float64(), "GitLab's running time, else ended_at - started_at, in seconds."),
    column("url", pa.string(), "Link to the pipeline in the GitLab UI."),
]

TASK_RUN_FIELDS = [
    column("orchestrator", pa.string(), "Always 'gitlab'.", nullable=False),
    column(
        "run_id", pa.string(), "'<project path>/<pipeline id>'; joins unified.runs.run_id. Pushed down.", nullable=False
    ),
    column("task_run_id", pa.string(), "'<project path>/<pipeline id>/<job id>'. Pushed down.", nullable=False),
    column("task_name", pa.string(), "Job name."),
    column(
        "status", pa.string(), "queued, running, success, failed, cancelled or skipped. Pushed down.", nullable=False
    ),
    column("raw_status", pa.string(), "GitLab's job status."),
    column("attempt", pa.int32(), "Always NULL: GitLab does not number retries (each retry is its own job)."),
    column("started_at", TSTZ, "When a runner started the job."),
    column("ended_at", TSTZ, "When the job finished."),
    column("duration_seconds", pa.float64(), "Running time in seconds."),
    column("url", pa.string(), "Link to the job in the GitLab UI."),
]


class UnifiedRuns(ApiScan[NoArgs]):
    """Rows for ``gitlab.unified.runs``."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(RUN_FIELDS)

    class Meta:
        """Table metadata."""

        name = "unified_runs_scan"
        description = "Scan behind the unified.runs table"
        filter_pushdown = True
        auto_apply_filters = True
        projection_pushdown = True

    @classmethod
    def resolve_scope(cls, params: ProcessParams[NoArgs]) -> list[str]:
        """Projects from the ATTACH options, unless the query names its own."""
        pd_filters = params.current_pushdown_filters
        if pd_filters is not None and (
            pd_filters.get_column_values("pipeline") is not None or pd_filters.get_column_values("run_id") is not None
        ):
            return []
        return scope_from_options(params)

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """run_id → single pipelines; pipeline / scope → pipeline listings with status and updated_after."""
        pd = ctx.pd
        statuses, possible = raw_statuses(PIPELINE_STATUS, pd.values("status"))
        if not possible:
            return []
        run_ids = pd.values("run_id")
        if run_ids is not None:
            out = []
            for r in dict.fromkeys(run_ids):
                parsed = split_id(r, 1)
                if parsed is not None:
                    path, (pid,) = parsed
                    out.append(
                        Listing(
                            paths=(f"projects/{encode_id(path)}/pipelines/{pid}",),
                            single=True,
                            missing_ok=True,
                            context=(("repo", path), ("detail", True)),
                        )
                    )
            return out
        projects = pd.values("pipeline")
        scope = [str(p) for p in projects] if projects is not None else ctx.scope
        if not scope:
            raise GitLabError(NO_SCOPE.format(table="runs", hint="`pipeline = 'group/project'` or `run_id`"))
        q: list[tuple[str, Any]] = []
        lows = [lo for lo, _ in (pd.bounds(c) for c in ("queued_at", "started_at", "ended_at")) if lo is not None]
        if lows:
            # Every one of these instants precedes the pipeline's last update.
            q.append(("updated_after", iso(max(lows))))
        return [
            Listing(paths=(f"projects/{encode_id(p)}/pipelines",), query=qq, context=(("repo", p),))
            for p in dict.fromkeys(scope)
            for qq in fan_out(q, "status", statuses)
        ]

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map pipelines to unified rows (fetching detail timing when selected)."""
        repo = str(listing.ctx("repo"))
        if not listing.ctx("detail"):
            items = with_details(ctx, items, repo, UNIFIED_DETAIL_COLUMNS)
        out = []
        for p in items:
            started, ended = p.get("started_at"), p.get("finished_at")
            duration = to_float(p.get("duration"))
            if duration is None:
                duration = seconds_between(started, ended)
            out.append(
                {
                    "orchestrator": ORCHESTRATOR,
                    "run_id": f"{repo}/{p.get('id')}",
                    "pipeline": repo,
                    "status": run_status(p.get("status")),
                    "raw_status": p.get("status"),
                    "trigger": trigger(p.get("source")),
                    "queued_at": p.get("created_at"),
                    "started_at": started,
                    "ended_at": ended,
                    "duration_seconds": duration,
                    "url": p.get("web_url"),
                }
            )
        return out


class UnifiedTaskRuns(ApiScan[NoArgs]):
    """Rows for ``gitlab.unified.task_runs``."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = pa.schema(TASK_RUN_FIELDS)

    class Meta:
        """Table metadata."""

        name = "unified_task_runs_scan"
        description = "Scan behind the unified.task_runs table"
        filter_pushdown = True
        auto_apply_filters = True
        projection_pushdown = True

    @classmethod
    def resolve_scope(cls, params: ProcessParams[NoArgs]) -> list[str]:
        """Projects from the ATTACH options, unless the query names runs itself."""
        pd_filters = params.current_pushdown_filters
        if pd_filters is not None and (
            pd_filters.get_column_values("run_id") is not None
            or pd_filters.get_column_values("task_run_id") is not None
        ):
            return []
        return scope_from_options(params)

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """run_id / task_run_id → pipeline jobs; scope → project jobs (keyset); status → scope[]."""
        pd = ctx.pd
        wanted = pd.values("status")
        scopes: list[str] | None = None
        if wanted is not None:
            raw, possible = raw_statuses(JOB_STATUS, wanted)
            if not possible:
                return []
            usable = [s for s in raw or [] if s in JOB_SCOPES]
            # Only push when every matching raw status is expressible as a scope[] value.
            scopes = usable if len(usable) == len(raw or []) else None
        scope_q: tuple[tuple[str, Any], ...] = (("scope[]", scopes),) if scopes else ()
        pipelines: list[tuple[str, int]] = []
        for r in pd.values("run_id") or []:
            parsed = split_id(r, 1)
            if parsed is not None:
                pipelines.append((parsed[0], parsed[1][0]))
        for t in pd.values("task_run_id") or []:
            parsed = split_id(t, 2)
            if parsed is not None:
                pipelines.append((parsed[0], parsed[1][0]))
        if pd.values("run_id") is not None or pd.values("task_run_id") is not None:
            return [
                Listing(
                    paths=(f"projects/{encode_id(path)}/pipelines/{pid}/jobs",),
                    query=(*scope_q, ("include_retried", True)),
                    missing_ok=True,
                    context=(("repo", path),),
                )
                for path, pid in dict.fromkeys(pipelines)
            ]
        if not ctx.scope:
            raise GitLabError(NO_SCOPE.format(table="task_runs", hint="`run_id = 'group/project/<pipeline id>'`"))
        keyset = (("pagination", "keyset"), ("order_by", "id"), ("sort", "desc"))
        return [
            Listing(paths=(f"projects/{encode_id(p)}/jobs",), query=(*scope_q, *keyset), context=(("repo", p),))
            for p in dict.fromkeys(ctx.scope)
        ]

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map jobs to unified rows."""
        repo = str(listing.ctx("repo"))
        out = []
        for j in items:
            pid = get(j, "pipeline", "id")
            duration = to_float(j.get("duration"))
            if duration is None:
                duration = seconds_between(j.get("started_at"), j.get("finished_at"))
            out.append(
                {
                    "orchestrator": ORCHESTRATOR,
                    "run_id": f"{repo}/{pid}",
                    "task_run_id": f"{repo}/{pid}/{j.get('id')}",
                    "task_name": j.get("name"),
                    "status": task_status(j.get("status")),
                    "raw_status": j.get("status"),
                    "attempt": None,
                    "started_at": j.get("started_at"),
                    "ended_at": j.get("finished_at"),
                    "duration_seconds": duration,
                    "url": j.get("web_url"),
                }
            )
        return out
