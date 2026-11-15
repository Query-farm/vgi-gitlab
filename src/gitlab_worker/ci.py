"""CI/CD: pipelines, jobs, environments and deployments.

GitLab's pipeline *list* omits timing and the triggering user; those come from
``GET /pipelines/:id``. ``pipelines()`` fetches the detail only when the query
needs one of those columns (projection pushdown), on a small thread pool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.metadata import FunctionExample

from .client import GitLabClient, NotFoundError, encode_id
from .convert import TSTZ, get, to_float
from .meta import column, function_tags
from .scan import ApiScan, Listing, ScanContext, fan_out, parallel_map, range_params

LIST = pa.list_(pa.string())

PIPELINE_STATUSES = (
    "created",
    "waiting_for_resource",
    "preparing",
    "pending",
    "running",
    "success",
    "failed",
    "canceling",
    "canceled",
    "skipped",
    "manual",
    "scheduled",
)
JOB_SCOPES = (
    "created",
    "pending",
    "running",
    "failed",
    "success",
    "canceled",
    "skipped",
    "waiting_for_resource",
    "manual",
)

# --------------------------------------------------------------------- pipelines

PIPELINE_DETAIL_COLUMNS = frozenset(
    {
        "name",
        "before_sha",
        "is_tag",
        "user_login",
        "started_at",
        "finished_at",
        "committed_at",
        "duration_seconds",
        "queued_duration_seconds",
        "coverage",
        "yaml_errors",
    }
)

PIPELINE_SCHEMA = pa.schema(
    [
        column("repo", pa.string(), "Project path."),
        column("id", pa.int64(), "Instance-wide pipeline id — what jobs(pipeline_id := ...) takes.", nullable=False),
        column("iid", pa.int64(), "Per-project pipeline number."),
        column("project_id", pa.int64(), "Numeric project id."),
        column(
            "status",
            pa.string(),
            "GitLab status: success, failed, running, canceled, skipped, manual, ... Pushed down.",
        ),
        column(
            "source",
            pa.string(),
            "What triggered it: push, web, schedule, api, trigger, merge_request_event, pipeline, ... Pushed down.",
        ),
        column("ref", pa.string(), "Branch or tag the pipeline ran for. Pushed down."),
        column("sha", pa.string(), "Commit the pipeline ran for. Pushed down."),
        column("name", pa.string(), "Pipeline name (workflow:name), if set. Detail column."),
        column("before_sha", pa.string(), "Previous tip of the ref for push pipelines. Detail column."),
        column("is_tag", pa.bool_(), "Whether ref is a tag. Detail column."),
        column("user_login", pa.string(), "Who triggered it. Pushed down (username). Detail column."),
        column("html_url", pa.string(), "Link to the pipeline in the GitLab UI."),
        column("created_at", TSTZ, "When the pipeline was created (queued)."),
        column("updated_at", TSTZ, "When the pipeline last changed. Ranges are pushed down."),
        column("started_at", TSTZ, "When the first job started. Detail column."),
        column("finished_at", TSTZ, "When the pipeline finished. Detail column."),
        column("committed_at", TSTZ, "When the commit was committed. Detail column."),
        column("duration_seconds", pa.float64(), "Running time in seconds, excluding queueing. Detail column."),
        column("queued_duration_seconds", pa.float64(), "Seconds spent queued before starting. Detail column."),
        column("coverage", pa.float64(), "Test coverage percentage parsed from job logs. Detail column."),
        column("yaml_errors", pa.string(), "CI configuration errors, when the pipeline could not be created. Detail."),
    ]
)


def pipeline_detail(client: GitLabClient, project: str, pipeline_id: Any) -> dict[str, Any]:
    """``GET /projects/:id/pipelines/:pipeline_id`` (empty when it vanished meanwhile)."""
    try:
        body = client.get(client.build(f"projects/{encode_id(project)}/pipelines/{pipeline_id}")).body
    except NotFoundError:
        return {}
    return body if isinstance(body, dict) else {}


def pipeline_row(p: dict[str, Any], repo: str | None) -> dict[str, Any]:
    """One pipeline (list or detail object) as a pipelines() row."""
    return {
        "repo": repo,
        "id": p.get("id"),
        "iid": p.get("iid"),
        "project_id": p.get("project_id"),
        "status": p.get("status"),
        "source": p.get("source"),
        "ref": p.get("ref"),
        "sha": p.get("sha"),
        "name": p.get("name"),
        "before_sha": p.get("before_sha"),
        "is_tag": p.get("tag"),
        "user_login": get(p, "user", "username"),
        "html_url": p.get("web_url"),
        "created_at": p.get("created_at"),
        "updated_at": p.get("updated_at"),
        "started_at": p.get("started_at"),
        "finished_at": p.get("finished_at"),
        "committed_at": p.get("committed_at"),
        "duration_seconds": to_float(p.get("duration")),
        "queued_duration_seconds": to_float(p.get("queued_duration")),
        "coverage": to_float(p.get("coverage")),
        "yaml_errors": p.get("yaml_errors"),
    }


def pipeline_query(ctx: ScanContext, statuses: list[str] | None) -> list[tuple[tuple[str, Any], ...]]:
    """Pipeline list queries for the pushed-down ref/sha/source/user/updated_at (one per status value)."""
    pd = ctx.pd
    q: list[tuple[str, Any]] = []
    for col, param in (("ref", "ref"), ("sha", "sha"), ("source", "source"), ("user_login", "username")):
        v = pd.one(col)
        if v:
            q.append((param, v))
    q += range_params(pd, "updated_at", "updated_after", "updated_before")
    return fan_out(q, "status", statuses)


def with_details(
    ctx: ScanContext,
    items: list[dict[str, Any]],
    project: str,
    detail_columns: frozenset[str] = PIPELINE_DETAIL_COLUMNS,
) -> list[dict[str, Any]]:
    """Merge each list item with its detail object when one of ``detail_columns`` is wanted."""
    if not (ctx.wanted & detail_columns):
        return items
    details = parallel_map(lambda p: pipeline_detail(ctx.client, project, p.get("id")), items)
    return [{**p, **d} for p, d in zip(items, details, strict=True)]


@dataclass(slots=True, frozen=True, kw_only=True)
class ProjectArgs:
    """Arguments for functions that take just a project."""

    project: Annotated[str, Arg(0, doc="Project path 'group/name' or numeric id, e.g. 'gitlab-org/cli'.")]


class Pipelines(ApiScan[ProjectArgs]):
    """``pipelines(project)``: CI/CD pipelines, newest first."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = PIPELINE_SCHEMA

    class Meta:
        """Function metadata."""

        name = "pipelines"
        description = "CI/CD pipelines of a GitLab project, newest first, with status/ref/source filters pushed down"
        filter_pushdown = True
        auto_apply_filters = True
        projection_pushdown = True
        tags = function_tags(
            PIPELINE_SCHEMA,
            title="Pipelines",
            category="ci",
            keywords="pipelines, ci, cd, builds, workflow runs, ci health, flaky, duration",
            doc_llm=(
                "pipelines(project) lists CI/CD pipelines (GitLab's workflow runs) newest first: repo, id, iid, "
                "project_id, status, source, ref, sha, html_url, created_at, updated_at, plus detail columns name, "
                "before_sha, is_tag, user_login, started_at, finished_at, committed_at, duration_seconds, "
                "queued_duration_seconds, coverage, yaml_errors. Detail columns cost one extra request per pipeline, "
                "made only when the query selects or filters on them. Pushed to the API: status = / IN (GitLab "
                "values success, failed, running, canceled, skipped, manual, pending, created, ...), ref =, sha =, "
                "source =, user_login =, updated_at ranges. Use jobs(project, pipeline_id := id) for its jobs."
            ),
            doc_md=(
                "## pipelines(project)\n\n"
                "Pipelines with status, ref and source; timing columns are fetched only when the query uses them."
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT ref, count(*) AS pipelines, count(*) FILTER (WHERE status = 'failed') AS failed "
                    "FROM gitlab.main.pipelines('gitlab-org/cli') WHERE updated_at >= TIMESTAMPTZ '2026-11-01' "
                    "GROUP BY ref ORDER BY failed DESC"
                ),
                description="Failure counts per branch this month",
            ),
            FunctionExample(
                sql=(
                    "SELECT id, duration_seconds, queued_duration_seconds "
                    "FROM gitlab.main.pipelines('gitlab-org/gitlab-runner') "
                    "WHERE status = 'success' AND ref = 'main' ORDER BY id DESC LIMIT 20"
                ),
                description="How long recent successful main pipelines took",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """``/projects/:id/pipelines`` per pushed-down status."""
        a: ProjectArgs = ctx.args
        statuses = ctx.pd.values("status")
        if statuses is not None:
            statuses = [s for s in statuses if s in PIPELINE_STATUSES]
            if not statuses:
                return []
        path = f"projects/{encode_id(a.project)}/pipelines"
        return [Listing(paths=(path,), query=q, context=(("repo", a.project),)) for q in pipeline_query(ctx, statuses)]

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map pipelines, fetching details when needed."""
        repo = listing.ctx("repo")
        return [pipeline_row(p, repo) for p in with_details(ctx, items, repo)]


# ------------------------------------------------------------------------- jobs

JOB_SCHEMA = pa.schema(
    [
        column("repo", pa.string(), "Project path."),
        column("id", pa.int64(), "Instance-wide job id — what job_log() takes.", nullable=False),
        column("name", pa.string(), "Job name from .gitlab-ci.yml."),
        column("stage", pa.string(), "Stage the job belongs to."),
        column("status", pa.string(), "success, failed, running, pending, canceled, skipped, manual, ... Pushed down."),
        column("ref", pa.string(), "Branch or tag."),
        column("is_tag", pa.bool_(), "Whether ref is a tag."),
        column("allow_failure", pa.bool_(), "Whether a failure of this job does not fail the pipeline."),
        column("pipeline_id", pa.int64(), "The pipeline the job belongs to. Pushed down."),
        column("pipeline_status", pa.string(), "Status of that pipeline."),
        column("commit_sha", pa.string(), "Commit the job ran for."),
        column("user_login", pa.string(), "Who triggered the job."),
        column("runner_id", pa.int64(), "Runner that ran the job."),
        column("runner_description", pa.string(), "That runner's description."),
        column("runner_tags", LIST, "Runner tags the job requires."),
        column("failure_reason", pa.string(), "Why it failed, e.g. 'script_failure', 'runner_system_failure'."),
        column("coverage", pa.float64(), "Coverage percentage parsed from the log."),
        column("is_archived", pa.bool_(), "Whether the job is archived (cannot be retried)."),
        column("html_url", pa.string(), "Link to the job in the GitLab UI."),
        column("created_at", TSTZ, "When the job was created."),
        column("started_at", TSTZ, "When a runner started it."),
        column("finished_at", TSTZ, "When it finished."),
        column("erased_at", TSTZ, "When its log and artifacts were erased."),
        column("duration_seconds", pa.float64(), "Running time in seconds."),
        column("queued_duration_seconds", pa.float64(), "Seconds spent waiting for a runner."),
    ]
)


def job_row(j: dict[str, Any], repo: str | None) -> dict[str, Any]:
    """One job object as a jobs() row."""
    return {
        "repo": repo,
        "id": j.get("id"),
        "name": j.get("name"),
        "stage": j.get("stage"),
        "status": j.get("status"),
        "ref": j.get("ref"),
        "is_tag": j.get("tag"),
        "allow_failure": j.get("allow_failure"),
        "pipeline_id": get(j, "pipeline", "id"),
        "pipeline_status": get(j, "pipeline", "status"),
        "commit_sha": get(j, "commit", "id") or get(j, "pipeline", "sha"),
        "user_login": get(j, "user", "username"),
        "runner_id": get(j, "runner", "id"),
        "runner_description": get(j, "runner", "description"),
        "runner_tags": j.get("tag_list"),
        "failure_reason": j.get("failure_reason"),
        "coverage": to_float(j.get("coverage")),
        "is_archived": j.get("archived"),
        "html_url": j.get("web_url"),
        "created_at": j.get("created_at"),
        "started_at": j.get("started_at"),
        "finished_at": j.get("finished_at"),
        "erased_at": j.get("erased_at"),
        "duration_seconds": to_float(j.get("duration")),
        "queued_duration_seconds": to_float(j.get("queued_duration")),
    }


def job_listings(
    project: str, pipeline_ids: list[Any] | None, statuses: list[str] | None, include_retried: bool
) -> list[Listing]:
    """Pipeline-scoped job listings (one per pipeline id), else the keyset-paginated project listing."""
    pid = encode_id(project)
    scope: list[tuple[str, Any]] = [("scope[]", statuses)] if statuses else []
    ctx = (("repo", project),)
    if pipeline_ids:
        retried = [("include_retried", True)] if include_retried else []
        return [
            Listing(
                paths=(f"projects/{pid}/pipelines/{int(p)}/jobs",),
                query=(*scope, *retried),
                context=(*ctx, ("pipeline_id", int(p))),
                missing_ok=True,
            )
            for p in dict.fromkeys(pipeline_ids)
        ]
    keyset = (("pagination", "keyset"), ("order_by", "id"), ("sort", "desc"))
    return [Listing(paths=(f"projects/{pid}/jobs",), query=(*scope, *keyset), context=ctx)]


@dataclass(slots=True, frozen=True, kw_only=True)
class JobsArgs:
    """Arguments for ``jobs(project, pipeline_id := NULL, include_retried := false)``."""

    project: Annotated[str, Arg(0, doc="Project path 'group/name' or numeric id.")]
    pipeline_id: Annotated[
        int | None,
        Arg("pipeline_id", default=None, doc="Only the jobs of this pipeline (same as WHERE pipeline_id =)."),
    ] = None
    include_retried: Annotated[
        bool,
        Arg("include_retried", default=False, doc="For a pipeline, also list attempts that were retried."),
    ] = False


def scope_values(values: list[Any] | None) -> tuple[list[str] | None, bool]:
    """Statuses usable as ``scope[]`` (None = no pushdown), and False when nothing can match."""
    if values is None:
        return None, True
    usable = [str(v) for v in values if v in JOB_SCOPES]
    if len(usable) != len(values):
        # A status GitLab's scope[] does not know (e.g. 'preparing'): filter after fetching.
        return None, True
    return usable, bool(usable)


class Jobs(ApiScan[JobsArgs]):
    """``jobs(project)``: CI/CD jobs, newest first."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = JOB_SCHEMA

    class Meta:
        """Function metadata."""

        name = "jobs"
        description = "CI/CD jobs of a GitLab project or one pipeline, newest first"
        filter_pushdown = True
        auto_apply_filters = True
        projection_pushdown = True
        tags = function_tags(
            JOB_SCHEMA,
            title="Jobs",
            category="ci",
            keywords="jobs, ci jobs, builds, workflow jobs, runners, failures, flaky tests, queue time",
            doc_llm=(
                "jobs(project, pipeline_id := NULL, include_retried := false) lists CI/CD jobs (GitLab's workflow "
                "jobs): repo, id, name, stage, status, ref, is_tag, allow_failure, pipeline_id, pipeline_status, "
                "commit_sha, user_login, runner_id, runner_description, runner_tags, failure_reason, coverage, "
                "is_archived, html_url, created_at, started_at, finished_at, erased_at, duration_seconds, "
                "queued_duration_seconds. Pushed down: status = / IN (as scope[]) and pipeline_id = / IN. Without a "
                "pipeline the whole project's job history is read newest first (keyset pagination); add LIMIT. "
                "Read a job's log with job_log(project, id). GitLab requires a token for job listings, even on public "
                "projects."
            ),
            doc_md=(
                "## jobs(project, pipeline_id := NULL, include_retried := false)\n\n"
                "Jobs of one pipeline or of the whole project, with runner, timing and failure reason."
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT name, failure_reason, duration_seconds FROM gitlab.main.jobs('gitlab-org/cli', "
                    "pipeline_id := 1234567) WHERE status = 'failed' ORDER BY name"
                ),
                description="Failed jobs of one pipeline",
            ),
            FunctionExample(
                sql=(
                    "SELECT name, count(*) AS runs, avg(queued_duration_seconds) AS avg_queue_s "
                    "FROM (SELECT * FROM gitlab.main.jobs('gitlab-org/gitlab-runner') LIMIT 500) GROUP BY name "
                    "ORDER BY avg_queue_s DESC"
                ),
                description="Which jobs wait longest for a runner",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """Pipeline jobs for a pushed-down pipeline id, else project jobs."""
        a: JobsArgs = ctx.args
        statuses, possible = scope_values(ctx.pd.values("status"))
        if not possible:
            return []
        pipelines = [a.pipeline_id] if a.pipeline_id is not None else ctx.pd.values("pipeline_id")
        return job_listings(a.project, pipelines, statuses, a.include_retried)

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map job objects."""
        repo = listing.ctx("repo")
        return [job_row(j, repo) for j in items]


# ---------------------------------------------------------------- environments

ENVIRONMENT_SCHEMA = pa.schema(
    [
        column("repo", pa.string(), "Project path."),
        column("id", pa.int64(), "Environment id.", nullable=False),
        column("name", pa.string(), "Environment name, e.g. 'production' or 'review/feature-x'. Pushed down."),
        column("slug", pa.string(), "URL-safe name."),
        column("tier", pa.string(), "production, staging, testing, development or other."),
        column("state", pa.string(), "'available', 'stopping' or 'stopped'. Pushed down."),
        column("external_url", pa.string(), "Where the environment is reachable."),
        column("html_url", pa.string(), "Link to the environment in the GitLab UI."),
        column("created_at", TSTZ, "When the environment was created."),
        column("updated_at", TSTZ, "When it last changed."),
        column("auto_stop_at", TSTZ, "When it will be stopped automatically, if scheduled."),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class EnvironmentsArgs:
    """Arguments for ``environments(project, search := NULL)``."""

    project: Annotated[str, Arg(0, doc="Project path 'group/name' or numeric id.")]
    search: Annotated[
        str | None, Arg("search", default=None, doc="Only environments whose name contains this text.")
    ] = None


class Environments(ApiScan[EnvironmentsArgs]):
    """``environments(project)``."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ENVIRONMENT_SCHEMA

    class Meta:
        """Function metadata."""

        name = "environments"
        description = "Deployment environments of a GitLab project"
        filter_pushdown = True
        auto_apply_filters = True
        projection_pushdown = True
        tags = function_tags(
            ENVIRONMENT_SCHEMA,
            title="Environments",
            category="deployments",
            keywords="environments, production, staging, review apps, deploy targets",
            doc_llm=(
                "environments(project, search := NULL) lists deployment environments: repo, id, name, slug, tier, "
                "state, external_url, html_url, created_at, updated_at, auto_stop_at. `name =` and `state =` are sent "
                "to the API. Join deployments() on environment = name."
            ),
            doc_md="## environments(project, search := NULL)\n\nEnvironments and their state.",
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT name, tier, external_url FROM gitlab.main.environments('gitlab-org/cli') "
                    "WHERE state = 'available' ORDER BY name"
                ),
                description="Environments that are up",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """``/projects/:id/environments`` with name/state/search."""
        a: EnvironmentsArgs = ctx.args
        q: list[tuple[str, Any]] = []
        name = ctx.pd.one("name")
        if name:
            q.append(("name", name))
        elif a.search:
            q.append(("search", a.search))
        state = ctx.pd.one("state")
        if state:
            q.append(("states", state))
        return [
            Listing(
                paths=(f"projects/{encode_id(a.project)}/environments",), query=tuple(q), context=(("repo", a.project),)
            )
        ]

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map environment objects."""
        repo = listing.ctx("repo")
        return [
            {
                "repo": repo,
                "id": e.get("id"),
                "name": e.get("name"),
                "slug": e.get("slug"),
                "tier": e.get("tier"),
                "state": e.get("state"),
                "external_url": e.get("external_url"),
                "html_url": ctx.client.web_url(f"{repo}/-/environments/{e.get('id')}") if repo else None,
                "created_at": e.get("created_at"),
                "updated_at": e.get("updated_at"),
                "auto_stop_at": e.get("auto_stop_at"),
            }
            for e in items
        ]


# ----------------------------------------------------------------- deployments

DEPLOYMENT_STATUSES = ("created", "running", "success", "failed", "canceled", "skipped", "blocked")

DEPLOYMENT_SCHEMA = pa.schema(
    [
        column("repo", pa.string(), "Project path."),
        column("id", pa.int64(), "Deployment id.", nullable=False),
        column("iid", pa.int64(), "Per-project deployment number."),
        column("environment", pa.string(), "Environment name deployed to. Pushed down."),
        column("environment_id", pa.int64(), "Environment id."),
        column("status", pa.string(), "created, running, success, failed, canceled, skipped or blocked. Pushed down."),
        column("ref", pa.string(), "Branch or tag deployed."),
        column("sha", pa.string(), "Commit deployed."),
        column("user_login", pa.string(), "Who deployed."),
        column("job_id", pa.int64(), "The deploy job (deployable)."),
        column("job_name", pa.string(), "Its name."),
        column("pipeline_id", pa.int64(), "The pipeline the deploy job belongs to."),
        column("created_at", TSTZ, "When the deployment was created."),
        column("updated_at", TSTZ, "When it last changed. Ranges are pushed down."),
        column("finished_at", TSTZ, "When the deploy job finished."),
    ]
)


class Deployments(ApiScan[ProjectArgs]):
    """``deployments(project)``."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = DEPLOYMENT_SCHEMA

    class Meta:
        """Function metadata."""

        name = "deployments"
        description = "Deployments of a GitLab project to its environments, newest first"
        filter_pushdown = True
        auto_apply_filters = True
        projection_pushdown = True
        tags = function_tags(
            DEPLOYMENT_SCHEMA,
            title="Deployments",
            category="deployments",
            keywords="deployments, releases to production, deploy frequency, dora metrics, change failure rate",
            doc_llm=(
                "deployments(project) lists deployments newest first: repo, id, iid, environment, environment_id, "
                "status, ref, sha, user_login, job_id, job_name, pipeline_id, created_at, updated_at, finished_at. "
                "Pushed down: environment =, status = / IN, updated_at ranges. Good for DORA deployment frequency "
                "and change failure rate."
            ),
            doc_md="## deployments(project)\n\nDeployments per environment, for DORA-style metrics.",
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT date_trunc('week', created_at) AS week, count(*) AS deploys "
                    "FROM gitlab.main.deployments('gitlab-org/cli') WHERE environment = 'production' "
                    "AND status = 'success' GROUP BY ALL ORDER BY week"
                ),
                description="Weekly production deployment frequency",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """``/projects/:id/deployments`` with environment/status/updated range."""
        a: ProjectArgs = ctx.args
        q: list[tuple[str, Any]] = []
        env = ctx.pd.one("environment")
        if env:
            q.append(("environment", env))
        updated = range_params(ctx.pd, "updated_at", "updated_after", "updated_before")
        if updated:
            # GitLab only accepts updated_* together with order_by=updated_at.
            q += [*updated, ("order_by", "updated_at"), ("sort", "desc")]
        statuses = ctx.pd.values("status")
        if statuses is not None:
            statuses = [s for s in statuses if s in DEPLOYMENT_STATUSES]
            if not statuses:
                return []
        path = f"projects/{encode_id(a.project)}/deployments"
        return [
            Listing(paths=(path,), query=qq, context=(("repo", a.project),)) for qq in fan_out(q, "status", statuses)
        ]

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map deployment objects."""
        repo = listing.ctx("repo")
        return [
            {
                "repo": repo,
                "id": d.get("id"),
                "iid": d.get("iid"),
                "environment": get(d, "environment", "name"),
                "environment_id": get(d, "environment", "id"),
                "status": d.get("status"),
                "ref": d.get("ref"),
                "sha": d.get("sha"),
                "user_login": get(d, "user", "username"),
                "job_id": get(d, "deployable", "id"),
                "job_name": get(d, "deployable", "name"),
                "pipeline_id": get(d, "deployable", "pipeline", "id"),
                "created_at": d.get("created_at"),
                "updated_at": d.get("updated_at"),
                "finished_at": d.get("finished_at") or get(d, "deployable", "finished_at"),
            }
            for d in items
        ]
