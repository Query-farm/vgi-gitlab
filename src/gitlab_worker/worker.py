"""Assemble the ``gitlab`` catalog and run the worker.

Run it:

    vgi-gitlab-worker                      # stdio: what DuckDB launches by default
    vgi-gitlab-worker --http --port 8000   # HTTP: for Docker / hosted deploys

Attach it from DuckDB, then (for private projects or self-managed GitLab) give it credentials:

    ATTACH 'gitlab' (TYPE vgi, LOCATION '/abs/path/to/.venv/bin/vgi-gitlab-worker');
    CREATE SECRET (TYPE gitlab, TOKEN 'glpat-...', BASE_URL 'https://gitlab.example.com');
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from vgi import Worker
from vgi.catalog import Catalog, Schema, Table
from vgi.catalog.attach_option import AttachOption
from vgi.catalog.constraints import ReferencedKey, constraint_record

from .ci import Deployments, Environments, Jobs, Pipelines
from .client import GITLAB_SECRET
from .joblog import JobLog, JobLogSections
from .meta import (
    CATALOG_DESCRIPTION,
    CATALOG_NAME,
    CATALOG_TAGS,
    MAIN_CATEGORIES,
    MAIN_DOC_LLM,
    MAIN_DOC_MD,
    SOURCE_URL,
    UNIFIED_CATEGORIES,
    UNIFIED_DOC_LLM,
    UNIFIED_DOC_MD,
    keywords_array,
    object_tags,
)
from .projects import Groups, Members, Project, Projects
from .repository import Branches, Commits, Contributors, Releases, Tags
from .status import UnifiedStatus, UnifiedStatusKind
from .unified import UnifiedRuns, UnifiedTaskRuns
from .work_items import (
    IssueNotes,
    Issues,
    MergeRequestApprovals,
    MergeRequestDiffStats,
    MergeRequestNotes,
    MergeRequests,
)

FUNCTIONS: list[type[Any]] = [
    Groups,
    Projects,
    Project,
    Members,
    Issues,
    IssueNotes,
    MergeRequests,
    MergeRequestNotes,
    MergeRequestApprovals,
    MergeRequestDiffStats,
    Commits,
    Branches,
    Tags,
    Releases,
    Contributors,
    Pipelines,
    Jobs,
    JobLog,
    JobLogSections,
    Environments,
    Deployments,
]

UNIFIED_FUNCTIONS: list[type[Any]] = [UnifiedStatus, UnifiedStatusKind]


def _stamp_example_queries(functions: list[type[Any]]) -> None:
    """Publish each function's examples as ``vgi.example_queries``, merged across same-named overloads."""
    by_name: dict[str, list[dict[str, str]]] = {}
    for fn in functions:
        bucket = by_name.setdefault(fn.Meta.name, [])
        for ex in fn.Meta.examples:
            entry = {"description": ex.description, "sql": ex.sql}
            if entry not in bucket:
                bucket.append(entry)
    for fn in functions:
        fn.Meta.tags = {**fn.Meta.tags, "vgi.example_queries": json.dumps(by_name[fn.Meta.name])}


_stamp_example_queries(FUNCTIONS)
_stamp_example_queries(UNIFIED_FUNCTIONS)

U = f"{CATALOG_NAME}.unified"

UNIFIED_DOCS: dict[str, dict[str, Any]] = {
    "runs": {
        "comment": "One row per GitLab pipeline in the cross-orchestrator runs shape.",
        "title": "Unified Runs",
        "keywords": "runs, pipelines, ci runs, cross orchestrator, union all, run status",
        "doc_llm": (
            "unified.runs: orchestrator ('gitlab'), run_id ('<project path>/<pipeline id>'), pipeline (project "
            "path), status (queued, running, success, failed, cancelled, skipped), raw_status (GitLab status), "
            "trigger (schedule, manual, api, event, other), queued_at, started_at, ended_at, duration_seconds, url. "
            "Needs a scope: WHERE pipeline = 'group/project' (or IN), WHERE run_id = ..., or the ATTACH options "
            "projects / group_path. status and lower bounds on queued_at/started_at/ended_at are pushed to the API."
        ),
        "doc_md": "## unified.runs\n\nGitLab pipelines as cross-orchestrator runs.",
        "examples": [
            (
                "Pipeline outcomes of one project",
                f"SELECT status, count(*) AS runs FROM {U}.runs WHERE pipeline = 'gitlab-org/cli' "
                "GROUP BY status ORDER BY runs DESC",
            ),
            (
                "Median run time per project for successful runs since November",
                f"SELECT pipeline, median(duration_seconds) AS median_s FROM {U}.runs "
                "WHERE pipeline IN ('gitlab-org/cli', 'gitlab-org/gitlab-runner') AND status = 'success' "
                "AND started_at >= TIMESTAMPTZ '2026-11-01' GROUP BY pipeline ORDER BY pipeline",
            ),
        ],
    },
    "task_runs": {
        "comment": "One row per GitLab CI job in the cross-orchestrator task_runs shape.",
        "title": "Unified Task Runs",
        "keywords": "task runs, jobs, ci jobs, cross orchestrator, union all",
        "doc_llm": (
            "unified.task_runs: orchestrator ('gitlab'), run_id ('<project path>/<pipeline id>', joins runs), "
            "task_run_id ('<project path>/<pipeline id>/<job id>'), task_name (job name), status, raw_status, "
            "attempt (always NULL: GitLab does not number retries; each retry is its own row), started_at, ended_at, "
            "duration_seconds, url. Needs a scope: WHERE run_id = ... (or IN), task_run_id = ..., or the ATTACH "
            "options projects / group_path. status is pushed to the API as scope[]."
        ),
        "doc_md": "## unified.task_runs\n\nGitLab jobs as cross-orchestrator task runs.",
        "examples": [
            (
                "Jobs of one pipeline",
                f"SELECT task_name, status, duration_seconds FROM {U}.task_runs "
                "WHERE run_id = 'gitlab-org/cli/1234567' ORDER BY task_name",
            ),
        ],
    },
}


def _table(name: str, scan: type[Any], key: str, fk: bool = False) -> Table:
    """Build a ``unified`` catalog table over ``scan`` with NOT NULL, primary key (and FK to runs) records."""
    doc = UNIFIED_DOCS[name]
    schema = ["unified"]
    constraints = [
        constraint_record(
            schema,
            name,
            f"{name}_{f.name}_not_null",
            "NOT_NULL",
            [[f.name]],
            enforcement="ENFORCED",
            validation="ALL_ROWS",
        )
        for f in scan.FIXED_SCHEMA
        if not f.nullable
    ]
    constraints.append(
        constraint_record(
            schema, name, f"{name}_pk", "PRIMARY_KEY", [[key]], enforcement="ENFORCED", validation="ALL_ROWS"
        )
    )
    if fk:
        constraints.append(
            constraint_record(
                schema,
                name,
                f"{name}_runs_fk",
                "FOREIGN_KEY",
                [["run_id"]],
                enforcement="ENFORCED",
                validation="ALL_ROWS",
                references=ReferencedKey(
                    schema_path=schema, table="runs", columns=[["run_id"]], constraint_id="runs_pk"
                ),
            )
        )
    return Table(
        name=name,
        function=scan,
        comment=doc["comment"],
        constraints=tuple(constraints),
        column_comments={f.name: (f.metadata or {})[b"comment"].decode() for f in scan.FIXED_SCHEMA},
        tags={
            **object_tags(
                title=doc["title"],
                category="runs",
                keywords=doc["keywords"],
                doc_llm=doc["doc_llm"],
                doc_md=doc["doc_md"],
            ),
            "domain": "devops",
            "vgi.example_queries": json.dumps([{"description": d, "sql": s} for d, s in doc["examples"]]),
        },
    )


UNIFIED_TABLES = [
    _table("runs", UnifiedRuns, "run_id"),
    _table("task_runs", UnifiedTaskRuns, "task_run_id", fk=True),
]


def _schema_examples() -> str:
    return json.dumps([{"description": e.description, "sql": e.sql} for fn in FUNCTIONS for e in fn.Meta.examples[:1]])


class AttachOptions:
    """ATTACH options: the default project scope of the ``unified`` tables."""

    projects: Annotated[
        str | None,
        AttachOption(
            desc="Comma-separated project paths that gitlab.unified.runs / task_runs cover when a query names none, "
            "e.g. 'gitlab-org/cli,gitlab-org/gitlab-runner'."
        ),
    ] = None
    group_path: Annotated[
        str | None,
        AttachOption(
            desc="Group path whose (non-archived, subgroup-inclusive) projects the unified tables cover when a query "
            "names none, e.g. 'gitlab-org/ci-cd'."
        ),
    ] = None


class GitlabWorker(Worker):
    """The worker process: the ``gitlab`` catalog with ``main`` and ``unified`` schemas."""

    AttachOptions = AttachOptions

    catalog = Catalog(
        name=CATALOG_NAME,
        comment=CATALOG_DESCRIPTION,
        source_url=SOURCE_URL,
        tags=CATALOG_TAGS,
        schemas=[
            Schema(
                path=["main"],
                comment="GitLab REST resources as table functions: projects, issues, merge requests, CI and more.",
                functions=FUNCTIONS,
                tags={
                    "vgi.title": "GitLab Resources",
                    "domain": "devops",
                    "vgi.keywords": keywords_array(
                        "projects, issues, merge requests, commits, releases, pipelines, jobs, job logs, deployments"
                    ),
                    "vgi.doc_llm": MAIN_DOC_LLM,
                    "vgi.doc_md": MAIN_DOC_MD,
                    "vgi.categories": json.dumps(MAIN_CATEGORIES),
                    "vgi.example_queries": _schema_examples(),
                },
            ),
            Schema(
                path=["unified"],
                comment="GitLab pipelines and jobs in the cross-orchestrator runs / task_runs shape.",
                tables=UNIFIED_TABLES,
                functions=UNIFIED_FUNCTIONS,
                tags={
                    "vgi.title": "Unified Orchestrator Runs",
                    "domain": "devops",
                    "vgi.keywords": keywords_array("unified, runs, task runs, cross orchestrator, union all, ci"),
                    "vgi.doc_llm": UNIFIED_DOC_LLM,
                    "vgi.doc_md": UNIFIED_DOC_MD,
                    "vgi.categories": json.dumps(UNIFIED_CATEGORIES),
                    "vgi.example_queries": json.dumps(
                        [{"description": d, "sql": s} for t in UNIFIED_DOCS.values() for d, s in t["examples"][:1]]
                        + [{"description": e.description, "sql": e.sql} for e in UnifiedStatus.Meta.examples]
                    ),
                },
            ),
        ],
    )

    secret_types = [GITLAB_SECRET]


def main() -> None:
    """Console-script entry point: parse ``--http`` / ``--unix`` / logging flags and serve."""
    GitlabWorker.main()


if __name__ == "__main__":
    main()
