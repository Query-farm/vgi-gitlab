"""Discovery metadata: catalog/schema docs, categories and tag helpers.

DuckDB, coding agents and the ``vgi-lint`` metadata checker all read these
tags. Every function and table carries ``object_tags(...)``: a title, a
category from its schema's category list, search keywords, and LLM- and
human-facing docs.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa

CATALOG_NAME = "gitlab"

SOURCE_URL = "https://github.com/Query-farm/vgi-gitlab"
GITLAB_API_DOCS = "https://docs.gitlab.com/api/rest/"

MAIN_CATEGORIES = [
    {"name": "projects", "title": "Groups and Projects", "description": "Groups, projects and their members."},
    {
        "name": "work_items",
        "title": "Issues and Merge Requests",
        "description": "Issues, merge requests, their notes, approvals and diff statistics.",
    },
    {
        "name": "repository",
        "title": "Repository",
        "description": "Commits, branches, tags, releases and contributors.",
    },
    {"name": "ci", "title": "CI/CD Pipelines", "description": "Pipelines, jobs and job logs."},
    {"name": "deployments", "title": "Deployments", "description": "Environments and deployments."},
]
UNIFIED_CATEGORIES = [
    {
        "name": "runs",
        "title": "Unified Runs",
        "description": "Pipelines and jobs in the cross-orchestrator runs / task_runs shape.",
    },
]
_ALL_CATEGORIES = {c["name"] for c in MAIN_CATEGORIES + UNIFIED_CATEGORIES}


def keywords_array(keywords: str) -> str:
    """Turn ``"a, b, c"`` into the JSON array string ``vgi.keywords`` requires."""
    return json.dumps([k.strip() for k in keywords.split(",") if k.strip()])


def object_tags(*, title: str, category: str, keywords: str, doc_llm: str, doc_md: str) -> dict[str, str]:
    """Build the standard per-object discovery tags."""
    if category not in _ALL_CATEGORIES:
        raise ValueError(f"unknown category {category!r}; add it to the schema categories")
    return {
        "vgi.title": title,
        "vgi.category": category,
        "vgi.keywords": keywords_array(keywords),
        "vgi.doc_llm": doc_llm,
        "vgi.doc_md": doc_md,
    }


def column(name: str, type_: pa.DataType, comment: str, *, nullable: bool = True) -> pa.Field[Any]:
    """Build an Arrow field whose ``comment`` shows up in DuckDB's ``DESCRIBE`` and agent tools."""
    return pa.field(name, type_, nullable=nullable, metadata={b"comment": comment.encode()})


_DUCKDB_TYPES = {
    pa.string(): "VARCHAR",
    pa.int64(): "BIGINT",
    pa.int32(): "INTEGER",
    pa.float64(): "DOUBLE",
    pa.bool_(): "BOOLEAN",
    pa.date32(): "DATE",
    pa.timestamp("us", tz="UTC"): "TIMESTAMP WITH TIME ZONE",
    pa.list_(pa.string()): "VARCHAR[]",
}


def duckdb_type(t: pa.DataType) -> str:
    """DuckDB spelling of an Arrow type used in this worker."""
    return _DUCKDB_TYPES[t]


def result_columns(schema: pa.Schema) -> str:
    """Build the ``vgi.result_columns_schema`` tag value for a table function's output."""
    return json.dumps(
        [
            {"name": f.name, "type": duckdb_type(f.type), "description": (f.metadata or {})[b"comment"].decode()}
            for f in schema
        ]
    )


def function_tags(
    schema: pa.Schema, *, title: str, category: str, keywords: str, doc_llm: str, doc_md: str
) -> dict[str, str]:
    """Discovery tags for a table function, including its result columns."""
    return {
        **object_tags(title=title, category=category, keywords=keywords, doc_llm=doc_llm, doc_md=doc_md),
        "domain": "devops",
        "vgi.result_columns_schema": result_columns(schema),
    }


CREDENTIALS_MD = (
    "Public projects on gitlab.com need no credentials. For private projects or a self-managed "
    "instance, create a secret:\n\n"
    "```sql\nCREATE SECRET gitlab (TYPE gitlab, TOKEN 'glpat-...', BASE_URL 'https://gitlab.example.com');\n```\n\n"
    "A token with the `read_api` scope is enough; the worker never writes to GitLab."
)

CATALOG_DESCRIPTION = (
    "GitLab (gitlab.com and self-managed) as read-only SQL: groups, projects, issues, merge requests, "
    "commits, releases, CI/CD pipelines, jobs, job logs and deployments, with GitHub-compatible column names."
)

CATALOG_DOC_LLM = (
    "Read-only GitLab REST API v4 access from DuckDB. Schema `main` has table functions that take a "
    "project path such as 'gitlab-org/gitlab' (or a numeric id) as their first argument: issues, "
    "merge_requests, merge_request_notes, merge_request_approvals, merge_request_diff_stats, issue_notes, "
    "commits, branches, tags, releases, contributors, pipelines, jobs, job_log, job_log_sections, "
    "environments, deployments, members; plus groups(), projects(namespace) and project(path). Column names "
    "follow the vgi-github worker where the concepts match (repo, number, user_login, html_url, head_ref, "
    "base_ref, merged_at, ...) and state is normalized to 'open'/'closed' (GitLab's own value is in "
    "gitlab_state), so `UNION ALL BY NAME` with GitHub data works. WHERE filters on state, author, labels, "
    "dates, status, ref and similar columns are sent to the API. Schema `unified` has the cross-orchestrator "
    "tables runs (one row per pipeline) and task_runs (one row per job); they need a project scope from a "
    "`pipeline =` / `run_id =` filter or the ATTACH options `projects` / `group_path`. Credentials: an optional "
    "`gitlab` secret (token, base_url); without one, public gitlab.com data is read anonymously."
)

CATALOG_DOC_MD = (
    "# GitLab\n\n"
    "Query GitLab (gitlab.com or self-managed) from DuckDB: groups, projects, issues, merge requests, "
    "commits, releases, CI/CD pipelines and jobs, job logs, environments and deployments. The worker only "
    "reads.\n\n"
    "Column names match the [vgi-github](https://github.com/Query-farm/vgi-github) worker where the concepts "
    "match, so GitHub and GitLab data combine with `UNION ALL BY NAME`.\n\n"
    "## Credentials\n\n" + CREDENTIALS_MD
)

MAIN_DOC_LLM = (
    "Table functions over GitLab's REST API v4. Pass a project as 'group/subgroup/name' or a numeric id. "
    "issues(project) and merge_requests(project) also accept a group path and then cover every project in it. "
    "Filters on state ('open'/'closed', or gitlab_state 'opened'/'closed'/'merged'/'locked'), user_login "
    "(author), number (iid), milestone, created_at/updated_at ranges, labels (named argument) and more are "
    "pushed to the API. pipelines(project) pushes status, ref, source, sha, user_login and updated_at ranges; "
    "jobs(project) pushes status and pipeline_id. job_log(project, job_id) returns the CI log one row per line "
    "with ANSI colours and section markers removed, the timestamp and stream (when GitLab recorded them) and "
    "the enclosing section; job_log_sections lists the collapsible sections with their durations."
)

MAIN_DOC_MD = (
    "## main\n\n"
    "GitLab resources as table functions: groups, projects and members; issues and merge requests with "
    "notes, approvals and diff statistics; commits, branches, tags, releases and contributors; CI/CD "
    "pipelines, jobs and job logs; environments and deployments.\n\n"
    "Simple `WHERE` predicates on state, author, dates, status, ref and similar columns are translated into "
    "GitLab API parameters, so only matching objects are fetched."
)

UNIFIED_DOC_LLM = (
    "The cross-orchestrator shape shared by the Query.Farm Airflow, Dagster, Prefect, Temporal, Databricks, "
    "dbt and GitHub workers: runs(orchestrator, run_id, pipeline, status, raw_status, trigger, queued_at, "
    "started_at, ended_at, duration_seconds, url) with one row per GitLab pipeline, and task_runs(orchestrator, "
    "run_id, task_run_id, task_name, status, raw_status, attempt, started_at, ended_at, duration_seconds, url) "
    "with one row per job. orchestrator is 'gitlab'; pipeline is the project path; run_id is "
    "'<project path>/<pipeline id>'; task_run_id is '<project path>/<pipeline id>/<job id>'. status is one of "
    "queued, running, success, failed, cancelled, skipped. The tables need a scope: filter runs on "
    "`pipeline = 'group/project'` (or IN), filter either table on `run_id = ...`, or ATTACH with the "
    "`projects` ('a/b,c/d') or `group_path` option."
)

UNIFIED_DOC_MD = (
    "## unified\n\n"
    "GitLab pipelines and jobs as `runs` and `task_runs`, the same columns and status vocabulary as the "
    "other Query.Farm orchestrator workers, for `UNION ALL`.\n\n"
    "Scope the tables with `WHERE pipeline = 'group/project'`, `WHERE run_id = 'group/project/123'`, or "
    "ATTACH options:\n\n"
    "```sql\nATTACH 'gitlab' (TYPE vgi, LOCATION '...', projects 'gitlab-org/gitlab-runner,gitlab-org/cli');\n```"
)

AGENT_TEST_TASKS = [
    {
        "name": "open_bugs",
        "prompt": "Using the gitlab worker, list the numbers and titles of the open issues labelled 'bug' "
        "in the project acme/widgets.",
    },
    {
        "name": "merged_mrs",
        "prompt": "How many merge requests in acme/widgets have been merged, and what is the median "
        "number of hours from opening to merge?",
    },
    {"name": "failed_pipelines", "prompt": "Which pipelines of acme/widgets failed? Give their ids and refs."},
    {"name": "job_errors", "prompt": "Show the log lines of job 9003 in acme/widgets that mention 'ERROR'."},
    {
        "name": "runs_per_status",
        "prompt": "Count the CI runs of the project acme/widgets per unified status (queued, running, success, "
        "failed, cancelled, skipped).",
    },
    {
        "name": "group_projects",
        "prompt": "List the subgroups of the acme group, and the non-archived projects in acme with their star counts.",
    },
    {"name": "project_license", "prompt": "What license does acme/widgets use, and how many commits does it have?"},
    {"name": "maintainers", "prompt": "Who has maintainer or owner access to acme/widgets?"},
    {
        "name": "review_of_mr_1",
        "prompt": "For merge request 1 of acme/widgets: who approved it, how many lines did it add and remove, "
        "and who left human (non-system) comments on it?",
    },
    {"name": "issue_1_comments", "prompt": "What did people say on issue 1 of acme/widgets (excluding system notes)?"},
    {
        "name": "commit_authors",
        "prompt": "Count commits per author in acme/widgets since 2026-11-01, and list the contributors ranked "
        "by commits.",
    },
    {
        "name": "unreleased_tags",
        "prompt": "Which tags of acme/widgets have no release, which branches are merged, and what is the latest "
        "non-upcoming release?",
    },
    {
        "name": "slowest_job_step",
        "prompt": "In pipeline 5002 of acme/widgets, which job failed, and which section of its log took longest?",
    },
    {
        "name": "production_deploys",
        "prompt": "List the successful deployments of acme/widgets to production and the environments that are "
        "currently available.",
    },
    {
        "name": "pipeline_task_runs",
        "prompt": "Using the unified task runs, list the jobs of run acme/widgets/5002 with their unified status.",
    },
    {
        "name": "normalize_statuses",
        "prompt": "Map the GitLab pipeline statuses 'canceled', 'manual' and 'success' to the unified run status "
        "vocabulary.",
    },
]

CATALOG_TAGS = {
    "vgi.title": "GitLab DevOps Data",
    "vgi.subtitle": "GitLab projects, merge requests, issues, CI pipelines, jobs and logs as SQL tables",
    "vgi.doc_llm": CATALOG_DOC_LLM,
    "vgi.doc_md": CATALOG_DOC_MD,
    "vgi.source_url": SOURCE_URL,
    "vgi.icon_url": "https://query.farm/media-kit/products/vgi/vgi-logo.png",
    "vgi.license": "0BSD",
    "vgi.author": "Query.Farm",
    "vgi.copyright": "Copyright 2026 Query Farm LLC - https://query.farm",
    "vgi.keywords": keywords_array(
        "gitlab, merge requests, issues, ci/cd, pipelines, jobs, devops, git, self-managed gitlab, rest api"
    ),
    "vgi.support_contact": f"{SOURCE_URL}/issues",
    "vgi.support_policy_url": f"{SOURCE_URL}#support",
    "vgi.agent_test_tasks": json.dumps(AGENT_TEST_TASKS),
}
