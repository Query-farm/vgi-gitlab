# Changelog

## 0.1.2 (2026-11-16)

- Require vgi-python `>=0.52.2,<0.53`; uv.lock re-locked.
- Move to Python 3.14: `requires-python >=3.14`, CI, the Docker image and lint tooling target 3.14.

## 0.1.1

- Catalog and schema docs are overviews instead of object lists, and schemas have their own one-line comments (vgi-lint 2.8: VGI173, VGI102).

## 0.1.0

First release: GitLab REST API v4 (gitlab.com and self-managed) as read-only SQL.

- `main`: groups, projects, project, members, issues, issue_notes, merge_requests,
  merge_request_notes, merge_request_approvals, merge_request_diff_stats, commits, branches,
  tags, releases, contributors, pipelines, jobs, job_log, job_log_sections, environments,
  deployments — vgi-github-compatible column names, filter pushdown, keyset pagination.
- `unified`: runs / task_runs in the cross-orchestrator shape, unified_status scalar.
- `gitlab` secret (token, base_url, send_as, per_page, timeout); ATTACH options
  `projects` / `group_path` scope the unified tables.

Targets: vgi-python 0.51 · VGI protocol 3.3 · Haybarn 2.0.0rc11 · vgi-lint-check 2.x (unpinned)
