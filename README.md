<p align="center">
  <img src="https://query.farm/media-kit/products/vgi/vgi-logo.png" alt="Vector Gateway Interface (VGI)" width="320">
</p>

# Query GitLab Projects, Merge Requests and CI Pipelines in DuckDB

> **vgi-gitlab** · a [Query.Farm](https://query.farm) VGI worker

[![CI](https://github.com/Query-farm/vgi-gitlab/actions/workflows/ci.yml/badge.svg)](https://github.com/Query-farm/vgi-gitlab/actions/workflows/ci.yml)
[![vgi worker: query.farm](https://query.farm/media-kit/shields/vgi-worker.svg)](https://query.farm)

Ask GitLab questions in SQL and get tables back: groups and projects, issues and merge
requests (with notes, approvals and diff sizes), commits, branches, tags, releases,
contributors, CI/CD pipelines and jobs, job logs split into clean lines and timed sections,
environments and deployments. It works with **gitlab.com and self-managed GitLab** and only
ever reads.

Column names follow the [vgi-github](https://github.com/Query-farm/vgi-github) worker where the
concepts match (`repo`, `number`, `user_login`, `head_ref`, `base_ref`, `merged_at`,
`html_url`, ...), and `state` uses GitHub's `open`/`closed`, so GitHub and GitLab data combine
with `UNION ALL BY NAME`. Pipelines and jobs are also served in the cross-orchestrator
`unified.runs` / `unified.task_runs` shape shared with the Airflow, Dagster, Prefect, Temporal,
Databricks and dbt workers.

vgi-gitlab 0.2.0 speaks VGI protocol 3.4, so it needs Haybarn 2.0.1 or newer (Haybarn 2.0.0 loads a 3.3 `vgi` extension that cannot attach it).

## Get started

You need [DuckDB](https://duckdb.org) (or [Haybarn](https://query.farm)) and
[uv](https://docs.astral.sh/uv/). In DuckDB:

```sql
INSTALL vgi FROM community;   -- once per machine
LOAD vgi;

ATTACH 'gitlab' (TYPE vgi,
  LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-gitlab@v0.2.0 vgi-gitlab-worker');

SELECT full_name, stargazers_count, forks_count, license_key
FROM gitlab.main.project('gitlab-org/gitlab-runner');
```

Public projects on gitlab.com need no credentials, except for job listings and job logs, which
GitLab only serves to authenticated users. For those, private projects, or a self-managed
instance, create a secret (a token with the `read_api` scope is enough):

```sql
CREATE SECRET gitlab (TYPE gitlab, TOKEN 'glpat-...');                                   -- gitlab.com
CREATE SECRET gitlab (TYPE gitlab, TOKEN 'glpat-...', BASE_URL 'https://gitlab.example.com');  -- self-managed
```

## What you can ask

```sql
-- The newest open bugs
SELECT number, title, user_login, created_at
FROM gitlab.main.issues('gitlab-org/cli', labels := 'bug')
WHERE state = 'open' ORDER BY created_at DESC LIMIT 20;

-- How quickly merge requests get merged
SELECT count(*) AS merged, median(date_diff('hour', created_at, merged_at)) AS median_hours
FROM gitlab.main.merge_requests('gitlab-org/cli')
WHERE gitlab_state = 'merged' AND updated_at >= TIMESTAMPTZ '2026-10-01';

-- Which branches fail CI most this month
SELECT ref, count(*) AS pipelines, count(*) FILTER (WHERE status = 'failed') AS failed
FROM gitlab.main.pipelines('gitlab-org/cli')
WHERE updated_at >= TIMESTAMPTZ '2026-11-01'
GROUP BY ref ORDER BY failed DESC;

-- The error lines of every failed job in a pipeline
SELECT j.name, l.line_number, l.message
FROM gitlab.main.jobs('gitlab-org/cli', pipeline_id := 1234567) j,
     LATERAL gitlab.main.job_log(j.repo, j.id) l
WHERE j.status = 'failed' AND l.message ILIKE '%error%';

-- Where a job spends its time
SELECT section, duration_seconds
FROM gitlab.main.job_log_sections('gitlab-org/cli', 9876543) ORDER BY duration_seconds DESC;

-- Weekly production deployment frequency (DORA)
SELECT date_trunc('week', created_at) AS week, count(*) AS deploys
FROM gitlab.main.deployments('gitlab-org/cli')
WHERE environment = 'production' AND status = 'success' GROUP BY ALL ORDER BY week;

-- GitHub and GitLab pull/merge requests side by side (with the vgi-github worker attached)
SELECT 'github' AS forge, repo, number, title, merged_at FROM github.main.pulls('duckdb/duckdb')
UNION ALL BY NAME
SELECT 'gitlab' AS forge, repo, number, title, merged_at FROM gitlab.main.merge_requests('gitlab-org/cli');
```

## What's available

Projects are written `'group/subgroup/name'` (or their numeric id); groups by their full path.

### `gitlab.main`

| Function | What you get | Pushed to the API |
|---|---|---|
| `groups(parent := NULL, search := NULL, all_available := NULL)` | Groups you can see, or a group's descendants | `visibility =` |
| `projects(namespace, include_subgroups := true, search := NULL, membership := true)` | A group's or user's projects; `projects(NULL)` = your memberships | `visibility =`, `is_archived =`, `last_activity_at` ranges |
| `project(path)` | One project plus license and statistics | — (lookup) |
| `members(namespace, inherited := true)` | Project (or group) members and access levels | `login =` (as a search) |
| `issues(project, labels := NULL, search := NULL)` | Issues of a project, or of every project in a group | `state` / `gitlab_state =`, `user_login =`, `number =`/`IN`, `milestone =`, `is_confidential =`, `created_at` / `updated_at` ranges |
| `issue_notes(project, number)` | The conversation on an issue | — (lookup) |
| `merge_requests(project, labels := NULL, search := NULL)` | Merge requests of a project or group | as issues, plus `head_ref =`, `base_ref =`, `is_draft =` |
| `merge_request_notes(project, number)` | Review comments and system notes | — (lookup) |
| `merge_request_approvals(project, number)` | Who approved, how many approvals are left | — (lookup) |
| `merge_request_diff_stats(project, number)` | Files changed with added/removed lines | — (lookup) |
| `commits(project, ref := NULL, since := NULL, until := NULL, path := NULL, all_refs := false, with_stats := false)` | Commit history | `committer_date` ranges (since/until), `author_name =` |
| `branches(project, search := NULL)` | Branches with tip commit, protection, merged state | `name =` |
| `tags(project, search := NULL)` | Git tags | `name =` |
| `releases(project)` | Releases | — |
| `contributors(project)` | Contributors ranked by commits | — |
| `pipelines(project)` | CI/CD pipelines; timing columns fetched only when used | `status =`/`IN`, `ref =`, `sha =`, `source =`, `user_login =`, `updated_at` ranges |
| `jobs(project, pipeline_id := NULL, include_retried := false)` | Jobs of a pipeline or the whole project (keyset-paginated) | `status =`/`IN` (as `scope[]`), `pipeline_id =`/`IN` |
| `job_log(project, job_id)` | A job's log, one clean row per line | — (lookup) |
| `job_log_sections(project, job_id)` | The log's collapsible sections and their durations | — (lookup) |
| `environments(project, search := NULL)` | Environments | `name =`, `state =` |
| `deployments(project)` | Deployments | `environment =`, `status =`/`IN`, `updated_at` ranges |

`DESCRIBE gitlab.main.issues('gitlab-org/cli')` documents every column.

**Lookups compose under `LATERAL`.** `project`, `issue_notes`, `merge_request_notes`,
`merge_request_approvals`, `merge_request_diff_stats`, `job_log` and `job_log_sections` take
columns as well as literals, and a key that does not exist (404) yields no rows rather than an
error. The listing functions stream page by page instead, so a `LIMIT` stops paging early; they
are the driving side of a query.

### Job logs

`job_log` reads `GET /projects/:id/jobs/:job_id/trace` and cleans it the way the GitLab UI
renders it: ANSI colour codes are removed; `section_start:` / `section_end:` markers are removed
and become the `section` / `section_path` columns (and `job_log_sections` rows with start, end
and duration); carriage-return progress output keeps only the final text; and runners writing
**timestamped logs** (GitLab 17.1+, `FF_TIMESTAMPS`) get `timestamp` and `stream`
(stdout/stderr) columns, with `+` continuation fragments joined into one line.

### `gitlab.unified`

| Object | What you get |
|---|---|
| `runs` | One row per pipeline: `orchestrator` ('gitlab'), `run_id` ('<project>/<pipeline id>'), `pipeline` (project path), `status`, `raw_status`, `trigger`, `queued_at`, `started_at`, `ended_at`, `duration_seconds`, `url` |
| `task_runs` | One row per job: `orchestrator`, `run_id`, `task_run_id` ('<project>/<pipeline id>/<job id>'), `task_name`, `status`, `raw_status`, `attempt`, `started_at`, `ended_at`, `duration_seconds`, `url` |
| `unified_status(raw_status[, kind])` | The status mapping as a scalar, no API calls |

`status` is one of `queued`, `running`, `success`, `failed`, `cancelled`, `skipped`
(`canceled` → `cancelled`; `created`/`pending`/`preparing`/`scheduled`/`waiting_for_resource` →
`queued`; a pipeline blocked on a manual job is `queued`, a manual job nobody started is
`skipped`). `trigger` maps the pipeline source: `schedule`; `web`/`webide`/`chat` → `manual`;
`api`/`trigger` → `api`; push, merge request and upstream-pipeline sources → `event`; anything
else `other`. `attempt` is NULL: GitLab does not number retries, and each retry is its own job.

A GitLab instance has millions of pipelines, so these tables need a **scope**: filter on
`pipeline = 'group/project'` (or `IN`), on `run_id` / `task_run_id`, or set it at ATTACH time:

```sql
ATTACH 'gitlab' (TYPE vgi, LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-gitlab@v0.2.0 vgi-gitlab-worker',
                 projects 'gitlab-org/cli,gitlab-org/gitlab-runner');   -- or: group_path 'gitlab-org/ci-cd'

SELECT r.pipeline, t.status, count(*) AS jobs
FROM gitlab.unified.task_runs t JOIN gitlab.unified.runs r USING (run_id)
GROUP BY ALL ORDER BY ALL;
```

`status` and lower bounds on `queued_at` / `started_at` / `ended_at` are sent to the API
(`status`, `updated_after`); `run_id =` becomes a single `GET /pipelines/:id`.

## Credentials

| Secret field | Meaning |
|---|---|
| `token` | Personal, project, group or OAuth access token (`read_api` is enough). Redacted. Optional for public gitlab.com data. |
| `base_url` | Instance root, default `https://gitlab.com` (a trailing `/api/v4` is accepted). |
| `send_as` | `bearer` (default, `Authorization: Bearer`), `private` (`PRIVATE-TOKEN`) or `job` (`JOB-TOKEN`, for CI job tokens). |
| `per_page` | Page size, 1–100 (default 100). |
| `timeout` | Per-request timeout in seconds (default 30). |

The token is read from the secret on every call. It never enters table-function state (which
travels with the request over the HTTP transport) or logs, and pagination links from the server
are re-anchored on `base_url` so the token is never sent to another host.

## How paging and rate limits work

GitLab answers every listing with a `Link: <...>; rel="next"` header. The worker stores that
relative URL as the scan's cursor, so a scan resumes on any worker process (HTTP transport) and
fetches one page per batch. **Keyset pagination** is used where GitLab supports it
(`projects(NULL)`, `groups()`, `jobs(project)` and `unified.task_runs`), so deep scans stay fast.
429 and 502/503/504 responses are retried with backoff that honours `Retry-After` and
`RateLimit-Reset`, and when `RateLimit-Remaining` reaches 0 the next request waits for the reset
(at most 60 s).

Every pushed-down predicate is also re-checked by the worker, so a translation only ever narrows
what GitLab returns. Predicates without an API equivalent (`LIKE`, list membership, ...) are
applied after fetching.

## Not covered

- **Vulnerabilities / security dashboards**: Ultimate-only and GraphQL-shaped; left out.
- **Writes**: none. The worker never creates, edits or deletes anything on GitLab.

## Testing

```bash
make test-unit                 # pytest: parsing, pushdown translation, client against the replay
make test-sql                  # test/sql/*.test through DuckDB (stdio)
make test-sql TRANSPORT=http   # …over HTTP (also: launch)
make check                     # everything CI runs
```

GitLab CE is far too heavy to run in CI, so the tests use **hand-built fixtures in GitLab's
documented response shapes** (`test/fixtures/gitlab_fixture.py`), served by a strict replay
(`test/mock_api/server.py`). The replay rejects any query parameter a route does not document,
applies the ones it does (so pushdown changes the answer exactly as on GitLab), requires the
fixture token, answers the first request to every path with a 429, and names a different host in
its `Link` headers. The SQL suite points the worker at it with `CREATE SECRET (TYPE gitlab,
TOKEN 'glpat-fixture', BASE_URL '<replay url>')`.

**Optional manual check against gitlab.com** (public data, no token, not run in CI):

```sql
SELECT full_name, stargazers_count FROM gitlab.main.project('gitlab-org/gitlab-runner');
SELECT tag_name, published_at FROM gitlab.main.releases('gitlab-org/cli') ORDER BY published_at DESC LIMIT 5;
SELECT status, count(*) FROM gitlab.unified.runs WHERE pipeline = 'gitlab-org/cli' GROUP BY status;
```

## Metadata quality

`make lint-explain` runs [vgi-lint](https://github.com/Query-farm/vgi-lint-check) with the
settings in `vgi-lint.toml` (100/100, `fail_on = "info"`). Every example reads a live GitLab, so
the execution rules are off (`[execution] enabled = false` locally, `execute: "false"` in CI);
the SQL suite is the behavioural gate.

## Deploying

The same worker serves stdio (DuckDB starts it locally) or HTTP:

```bash
docker build -t vgi-gitlab .
docker run -p 8000:8000 vgi-gitlab       # health: GET /health
```

```sql
ATTACH 'gitlab' (TYPE vgi, LOCATION 'https://workers.example.com/gitlab');
CREATE SECRET (TYPE gitlab, TOKEN 'glpat-...');   -- each user's own token, sent per query
```

Put hosted workers behind TLS and authentication; see
[authentication](https://query.farm/vgi/authentication).

## Support

Open an issue on this repository. For VGI itself, see [query.farm](https://query.farm).

## Authorship & License

Copyright 2026 Query Farm LLC - https://query.farm

Written by [Query.Farm](https://query.farm) and released under the
[BSD Zero Clause License](LICENSE) (0BSD). GitLab is a trademark of GitLab Inc.; this worker is
not affiliated with or endorsed by GitLab.
