"""Unit tests. The SQL-level tests live in test/sql and run through DuckDB."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from gitlab_worker.ci import Deployments, Jobs, Pipelines, scope_values
from gitlab_worker.client import GitLabClient, GitLabConfig, GitLabError, NotFoundError, encode_id, next_link
from gitlab_worker.joblog import parse_trace
from gitlab_worker.scan import Pushdown, ScanContext, range_params
from gitlab_worker.unified import (
    PIPELINE_STATUS,
    UnifiedRuns,
    UnifiedTaskRuns,
    raw_statuses,
    run_status,
    split_id,
    task_status,
    trigger,
)
from gitlab_worker.work_items import Issues, MergeRequests, diff_counts, repo_of

from .conftest import TOKEN, Replay

UTC = dt.UTC


class FakePushdown(Pushdown):
    """Pushdown with hand-set constraints."""

    def __init__(self, values: dict[str, list[Any]] | None = None, bounds: dict[str, tuple[Any, Any]] | None = None):
        super().__init__(None)
        self._values = values or {}
        self._bounds = bounds or {}

    def values(self, column: str) -> list[Any] | None:
        return self._values.get(column)

    def bounds(self, column: str) -> tuple[Any, Any]:
        return self._bounds.get(column, (None, None))

    def columns(self) -> set[str]:
        return set(self._values) | set(self._bounds)


class Args:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


def ctx(args: Any = None, pd: Pushdown | None = None, scope: list[str] | None = None) -> ScanContext:
    return ScanContext(
        client=GitLabClient(GitLabConfig()),
        args=args,
        pd=pd or FakePushdown(),
        wanted=set(),
        scope=scope or [],
        params=None,  # type: ignore[arg-type]
    )


# ------------------------------------------------------------------ config


def test_config_defaults_to_anonymous_gitlab_com() -> None:
    cfg = GitLabConfig.from_secret(None)
    assert cfg.base_url == "https://gitlab.com"
    assert cfg.token is None


def test_config_strips_api_suffix_and_validates() -> None:
    cfg = GitLabConfig.from_secret({"BASE_URL": "https://gl.example.com/api/v4/", "token": "t", "per_page": 50})
    assert cfg.base_url == "https://gl.example.com"
    assert cfg.per_page == 50
    with pytest.raises(GitLabError, match="send_as"):
        GitLabConfig.from_secret({"send_as": "cookie"})
    with pytest.raises(GitLabError, match="base_url"):
        GitLabConfig.from_secret({"base_url": "gitlab.com"})
    with pytest.raises(GitLabError, match="per_page"):
        GitLabConfig.from_secret({"per_page": 500})


def test_token_never_in_repr() -> None:
    assert "secret-token" not in repr(GitLabConfig.from_secret({"token": "secret-token"}))


def test_encode_id() -> None:
    assert encode_id("gitlab-org/gitlab") == "gitlab-org%2Fgitlab"
    assert encode_id(278964) == "278964"
    with pytest.raises(GitLabError):
        encode_id(" / ")


def test_next_link() -> None:
    header = (
        '<https://gitlab.com/api/v4/projects?page=1>; rel="prev", '
        '<https://gitlab.com/api/v4/projects?id_after=42&pagination=keyset>; rel="next", '
        '<https://gitlab.com/api/v4/projects?page=9>; rel="last"'
    )
    assert next_link(header) == "https://gitlab.com/api/v4/projects?id_after=42&pagination=keyset"
    assert next_link('<https://x/y?page=1>; rel="first"') is None
    assert next_link(None) is None


def test_build_keeps_base_path_and_repeats_lists() -> None:
    client = GitLabClient(GitLabConfig(base_url="https://example.com/gitlab"))
    target = client.build("projects/1/jobs", [("scope[]", ["failed", "success"]), ("x", True), ("skip", None)])
    assert target == "/gitlab/api/v4/projects/1/jobs?scope%5B%5D=failed&scope%5B%5D=success&x=true"


# ------------------------------------------------------------------ client against the replay


def test_client_retries_429_and_reanchors_links(flaky_replay: Replay) -> None:
    client = GitLabClient(GitLabConfig(base_url=flaky_replay.url, token=TOKEN, per_page=1))
    resp = client.get(client.build("groups", [("pagination", "keyset"), ("order_by", "name"), ("per_page", 1)]))
    assert [g["full_path"] for g in resp.body] == ["acme"]
    # The replay's Link names gitlab.fixture.invalid; only the path and query are kept.
    assert resp.next_url is not None and resp.next_url.startswith("/api/v4/groups?")
    second = client.get(resp.next_url)
    assert [g["full_path"] for g in second.body] == ["acme/platform"]
    assert second.next_url is None


def test_client_errors(replay: Replay) -> None:
    anon = GitLabClient(GitLabConfig(base_url=replay.url))
    with pytest.raises(GitLabError, match=r"HTTP 401.*read_api"):
        anon.get(anon.build("projects/acme%2Fwidgets/issues"))
    authed = GitLabClient(GitLabConfig(base_url=replay.url, token=TOKEN, send_as="private"))
    with pytest.raises(NotFoundError):
        authed.get(authed.build("projects/nope%2Fnope/issues"))
    with pytest.raises(GitLabError, match=r"HTTP 400.*unknown query parameter"):
        authed.get(authed.build("projects/acme%2Fwidgets/issues", [("bogus", 1)]))


def test_listing_requests_only_documented_parameters(replay: Replay) -> None:
    """Every listing the pushdown builds is accepted by the strict replay."""
    replay.reset()
    client = GitLabClient(GitLabConfig(base_url=replay.url, token=TOKEN))
    pd = FakePushdown(
        values={"state": ["open"], "user_login": ["alice"], "number": [1, 2], "milestone": ["v1.2"]},
        bounds={"created_at": (dt.datetime(2026, 9, 1, tzinfo=UTC), None)},
    )
    c = ctx(Args(project="acme/widgets", labels="bug", search=None), pd)
    c.client = client
    (listing,) = Issues.listings(c)
    body = client.get(client.build(listing.paths[0], list(listing.query))).body
    assert [i["iid"] for i in body] == [1]
    (req,) = replay.requests()
    assert {k: v for k, v in req["query"] if k != "iids[]"} == {
        "state": "opened",
        "author_username": "alice",
        "milestone": "v1.2",
        "labels": "bug",
        "created_after": "2026-08-31T23:59:59Z",
    }
    assert [v for k, v in req["query"] if k == "iids[]"] == ["1", "2"]


# ------------------------------------------------------------------ pushdown translation


def test_issue_state_translation() -> None:
    args = Args(project="a/b", labels=None, search=None)
    (listing,) = Issues.listings(ctx(args, FakePushdown({"state": ["closed"]})))
    assert ("state", "closed") in listing.query
    assert Issues.listings(ctx(args, FakePushdown({"state": ["bogus"]}))) == []
    (listing,) = Issues.listings(ctx(args, FakePushdown({"state": ["open", "closed"]})))
    assert not [q for q in listing.query if q[0] == "state"]


def test_merge_request_closed_is_not_pushed() -> None:
    """SQL 'closed' includes merged merge requests; GitLab's state=closed does not."""
    args = Args(project="a/b", labels=None, search=None)
    (listing,) = MergeRequests.listings(ctx(args, FakePushdown({"state": ["closed"]})))
    assert not [q for q in listing.query if q[0] == "state"]
    (listing,) = MergeRequests.listings(ctx(args, FakePushdown({"gitlab_state": ["merged"], "is_draft": [False]})))
    assert ("state", "merged") in listing.query and ("wip", "no") in listing.query
    assert listing.paths == ("projects/a%2Fb/merge_requests", "groups/a%2Fb/merge_requests")


def test_range_params_widen_bounds() -> None:
    pd = FakePushdown(bounds={"updated_at": (dt.date(2026, 1, 1), dt.datetime(2026, 2, 1, 12, tzinfo=UTC))})
    assert range_params(pd, "updated_at", "updated_after", "updated_before") == [
        ("updated_after", "2025-12-31T23:59:59Z"),
        ("updated_before", "2026-02-01T12:00:01Z"),
    ]


def test_pipeline_status_fan_out_and_impossible_status() -> None:
    args = Args(project="a/b")
    listings = Pipelines.listings(ctx(args, FakePushdown({"status": ["failed", "canceled"], "ref": ["main"]})))
    assert [dict(lst.query)["status"] for lst in listings] == ["failed", "canceled"]
    assert all(("ref", "main") in lst.query for lst in listings)
    assert Pipelines.listings(ctx(args, FakePushdown({"status": ["cancelled"]}))) == []


def test_jobs_scope_and_pipeline_pushdown() -> None:
    assert scope_values(["failed"]) == (["failed"], True)
    assert scope_values(["preparing"]) == (None, True)
    assert scope_values([]) == ([], False)
    args = Args(project="a/b", pipeline_id=None, include_retried=False)
    (listing,) = Jobs.listings(ctx(args, FakePushdown({"pipeline_id": [7], "status": ["failed"]})))
    assert listing.paths == ("projects/a%2Fb/pipelines/7/jobs",)
    (listing,) = Jobs.listings(ctx(args))
    assert ("pagination", "keyset") in listing.query


def test_deployments_updated_range_needs_order_by() -> None:
    pd = FakePushdown(bounds={"updated_at": (dt.datetime(2026, 1, 1, tzinfo=UTC), None)})
    (listing,) = Deployments.listings(ctx(Args(project="a/b"), pd))
    assert ("order_by", "updated_at") in listing.query


# ------------------------------------------------------------------ unified


def test_unified_status_vocabulary() -> None:
    assert set(PIPELINE_STATUS.values()) <= {"queued", "running", "success", "failed", "cancelled", "skipped"}
    assert run_status("canceled") == "cancelled"
    assert run_status("manual") == "queued"
    assert task_status("manual") == "skipped"
    assert run_status(None) == "queued"
    assert trigger("schedule") == "schedule"
    assert trigger("web") == "manual"
    assert trigger("trigger") == "api"
    assert trigger("merge_request_event") == "event"
    assert trigger("ondemand_dast_scan") == "other"
    assert trigger(None) is None
    raw, ok = raw_statuses(PIPELINE_STATUS, ["cancelled"])
    assert ok and raw == ["canceled"]
    assert raw_statuses(PIPELINE_STATUS, ["nope"]) == ([], False)


def test_split_id() -> None:
    assert split_id("gitlab-org/sub/cli/123", 1) == ("gitlab-org/sub/cli", [123])
    assert split_id("a/b/1/2", 2) == ("a/b", [1, 2])
    assert split_id("a/b/x", 1) is None
    assert split_id("123", 1) is None


def test_unified_scope_required() -> None:
    with pytest.raises(GitLabError, match="needs a project scope"):
        UnifiedRuns.listings(ctx(Args()))
    with pytest.raises(GitLabError, match="needs a project scope"):
        UnifiedTaskRuns.listings(ctx(Args()))
    listings = UnifiedRuns.listings(ctx(Args(), scope=["a/b", "c/d"]))
    assert [lst.paths[0] for lst in listings] == ["projects/a%2Fb/pipelines", "projects/c%2Fd/pipelines"]
    (single,) = UnifiedRuns.listings(ctx(Args(), FakePushdown({"run_id": ["a/b/9"]})))
    assert single.single and single.paths == ("projects/a%2Fb/pipelines/9",)
    (jobs,) = UnifiedTaskRuns.listings(ctx(Args(), FakePushdown({"task_run_id": ["a/b/9/77"]})))
    assert jobs.paths == ("projects/a%2Fb/pipelines/9/jobs",)


def test_unified_started_at_bound_becomes_updated_after() -> None:
    pd = FakePushdown(
        {"pipeline": ["a/b"]},
        {
            "started_at": (dt.datetime(2026, 3, 1, tzinfo=UTC), None),
            "queued_at": (dt.datetime(2026, 2, 1, tzinfo=UTC), None),
        },
    )
    (listing,) = UnifiedRuns.listings(ctx(Args(), pd))
    assert ("updated_after", "2026-03-01T00:00:00Z") in listing.query


# ------------------------------------------------------------------ parsing


def test_diff_counts() -> None:
    assert diff_counts("@@ -1,2 +1,3 @@\n a\n-b\n+c\n+d\n") == (2, 1)
    assert diff_counts("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-x\n+y\n") == (1, 1)
    assert diff_counts(None) == (None, None)


def test_repo_of_references() -> None:
    assert repo_of({"references": {"full": "g/sub/p#12"}}, None) == "g/sub/p"
    assert repo_of({"references": {"full": "g/p!3"}}, None) == "g/p"
    assert repo_of({}, "fallback/x") == "fallback/x"


def test_parse_trace_sections_ansi_and_carriage_returns() -> None:
    trace = (
        "\x1b[0KRunning with gitlab-runner\x1b[0;m\n"
        "section_start:100:outer[collapsed=true]\r\x1b[0K\x1b[36;1mOuter\x1b[0;m\n"
        "section_start:101:inner\r\x1b[0KInner\n"
        "10%\r100%\n"
        "section_end:105:inner\r\x1b[0K\n"
        "after inner\n"
        "section_end:110:outer\r\x1b[0K\n"
        "done\n"
    )
    lines, sections = parse_trace(trace)
    assert [ln.message for ln in lines] == [
        "Running with gitlab-runner",
        "Outer",
        "Inner",
        "100%",
        "after inner",
        "done",
    ]
    assert [ln.section for ln in lines] == [None, "outer", "inner", "inner", "outer", None]
    assert lines[3].section_path == ["outer", "inner"]
    outer, inner = sections
    assert (outer.depth, inner.depth, inner.parent) == (0, 1, "outer")
    assert outer.is_collapsed and not inner.is_collapsed
    assert outer.ended_at is not None and outer.started_at is not None
    assert (outer.ended_at - outer.started_at).total_seconds() == 10
    assert (inner.first_line, inner.last_line) == (3, 4)
    assert (outer.header, inner.header) == ("Outer", "Inner")


def test_parse_trace_timestamped_with_continuations() -> None:
    trace = (
        "2026-01-01T00:00:00.000000Z 00O hello \n"
        "2026-01-01T00:00:00.500000Z 00O+ world\n"
        "2026-01-01T00:00:01.000000Z 01E oops\n"
    )
    lines, _ = parse_trace(trace)
    assert [(ln.message, ln.stream) for ln in lines] == [("hello world", "stdout"), ("oops", "stderr")]
    assert lines[0].timestamp == dt.datetime(2026, 1, 1, tzinfo=UTC)


def test_parse_trace_unclosed_section() -> None:
    lines, sections = parse_trace("section_start:1:step_script\r\x1b[0Kgo\nstill running")
    assert sections[0].ended_at is None
    assert lines[-1].section == "step_script"
