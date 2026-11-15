"""Hand-built GitLab REST API v4 responses for the replay server.

Every object follows the shape GitLab documents for its endpoint
(https://docs.gitlab.com/api/rest/), trimmed to the fields the worker reads
plus a few it ignores. Fields starting with ``_`` are fixture-only (used by the
server's filters, e.g. which paths a commit touched) and are stripped from
responses.

The data describes a small instance at https://gitlab.example.com:

* group ``acme`` (id 10) with subgroup ``acme/platform`` (id 11);
* projects ``acme/widgets`` (101, the busy one), ``acme/gadgets`` (102, archived),
  ``acme/platform/api`` (103), and ``alice/dotfiles`` (201, a user project);
* issues, merge requests, notes, approvals, diffs, commits, branches, tags,
  releases, contributors, pipelines, jobs, a job log, environments and
  deployments for ``acme/widgets``.

``ROUTES`` maps a raw (percent-encoded) request path to a route spec:
``items`` (a paginated list), ``object`` or ``text``; ``params`` (accepted
filter parameters and how they filter), ``accept`` (accepted, ignored
parameters), ``keyset`` (keyset pagination allowed) and ``public``.
"""

from __future__ import annotations

from typing import Any

WEB = "https://gitlab.example.com"
API = "/api/v4"
W = "acme%2Fwidgets"


def user(username: str, uid: int, name: str) -> dict[str, Any]:
    """A GitLab 'basic user' object."""
    return {
        "id": uid,
        "username": username,
        "name": name,
        "state": "active",
        "avatar_url": f"{WEB}/uploads/-/system/user/avatar/{uid}/avatar.png",
        "web_url": f"{WEB}/{username}",
    }


ALICE = user("alice", 1, "Alice Liddell")
BOB = user("bob", 2, "Bob Builder")
CAROL = user("carol", 3, "Carol Danvers")

# ----------------------------------------------------------------- namespaces


def group(gid: int, path: str, full_path: str, name: str, parent: int | None, vis: str) -> dict[str, Any]:
    """A group object (GET /groups)."""
    return {
        "id": gid,
        "web_url": f"{WEB}/groups/{full_path}",
        "name": name,
        "path": path,
        "description": f"The {name} group",
        "visibility": vis,
        "share_with_group_lock": False,
        "require_two_factor_authentication": False,
        "full_name": name if parent is None else f"Acme / {name}",
        "full_path": full_path,
        "parent_id": parent,
        "created_at": "2024-01-10T09:00:00.000Z",
    }


GROUPS = [
    group(10, "acme", "acme", "Acme", None, "public"),
    group(11, "platform", "acme/platform", "Platform", 10, "internal"),
]


def namespace(kind: str, full_path: str, nid: int) -> dict[str, Any]:
    """The ``namespace`` embedded in a project."""
    return {
        "id": nid,
        "name": full_path.split("/")[-1],
        "path": full_path.split("/")[-1],
        "kind": kind,
        "full_path": full_path,
        "web_url": f"{WEB}/{full_path}",
    }


def project(pid: int, path_with_ns: str, ns: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """A project object (GET /projects)."""
    path = path_with_ns.split("/")[-1]
    base = {
        "id": pid,
        "description": f"{path} project",
        "name": path.title(),
        "name_with_namespace": path_with_ns.replace("/", " / "),
        "path": path,
        "path_with_namespace": path_with_ns,
        "created_at": "2024-02-01T10:00:00.000Z",
        "updated_at": "2026-11-10T08:00:00.000Z",
        "default_branch": "main",
        "topics": [],
        "tag_list": [],
        "ssh_url_to_repo": f"git@gitlab.example.com:{path_with_ns}.git",
        "http_url_to_repo": f"{WEB}/{path_with_ns}.git",
        "web_url": f"{WEB}/{path_with_ns}",
        "star_count": 0,
        "forks_count": 0,
        "last_activity_at": "2026-11-10T08:00:00.000Z",
        "namespace": ns,
        "visibility": "public",
        "archived": False,
        "empty_repo": False,
        "issues_enabled": True,
        "merge_requests_enabled": True,
        "wiki_enabled": True,
        "open_issues_count": 0,
    }
    base.update(extra)
    return base


ACME_NS = namespace("group", "acme", 10)
PLATFORM_NS = namespace("group", "acme/platform", 11)
ALICE_NS = namespace("user", "alice", 1)

WIDGETS = project(
    101,
    "acme/widgets",
    ACME_NS,
    star_count=42,
    forks_count=7,
    open_issues_count=3,
    topics=["widgets", "ruby"],
    last_activity_at="2026-11-14T16:30:00.000Z",
)
GADGETS = project(
    102,
    "acme/gadgets",
    ACME_NS,
    star_count=5,
    archived=True,
    visibility="internal",
    last_activity_at="2025-03-01T12:00:00.000Z",
)
API_PROJECT = project(
    103,
    "acme/platform/api",
    PLATFORM_NS,
    star_count=12,
    visibility="private",
    wiki_enabled=False,
    last_activity_at="2026-11-12T11:00:00.000Z",
)
DOTFILES = project(
    201,
    "alice/dotfiles",
    ALICE_NS,
    star_count=1,
    forked_from_project={"id": 999, "path_with_namespace": "upstream/dotfiles", "name": "dotfiles"},
    last_activity_at="2026-06-01T00:00:00.000Z",
)
WIDGETS_DETAIL = {
    **WIDGETS,
    "license": {
        "key": "mit",
        "name": "MIT License",
        "nickname": None,
        "html_url": "https://opensource.org/licenses/MIT",
        "source_url": None,
    },
    "statistics": {
        "commit_count": 1234,
        "storage_size": 9_876_543,
        "repository_size": 5_432_100,
        "wiki_size": 0,
        "lfs_objects_size": 0,
        "job_artifacts_size": 4_444_443,
    },
}

PROJECT_FILTERS = {
    "search": {"field": ["name", "path", "description"], "op": "contains"},
    "visibility": {"field": "visibility"},
    "archived": {"field": "archived"},
    "last_activity_after": {"field": "last_activity_at", "op": "gte"},
    "last_activity_before": {"field": "last_activity_at", "op": "lte"},
}

# --------------------------------------------------------------------- issues


def issue(
    iid: int,
    title: str,
    state: str,
    author: dict[str, Any],
    labels: list[str],
    created: str,
    updated: str,
    closed: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A project issue object."""
    base = {
        "id": 5000 + iid,
        "iid": iid,
        "project_id": 101,
        "title": title,
        "description": f"Details of {title.lower()}.",
        "state": state,
        "created_at": created,
        "updated_at": updated,
        "closed_at": closed,
        "closed_by": BOB if closed else None,
        "labels": labels,
        "milestone": {"id": 1, "iid": 1, "title": "v1.2", "state": "active"} if iid in (1, 2) else None,
        "assignees": [BOB] if iid in (1, 3) else [],
        "author": author,
        "type": "ISSUE",
        "issue_type": "incident" if iid == 5 else "issue",
        "user_notes_count": iid,
        "upvotes": 6 - iid,
        "downvotes": 0,
        "due_date": "2026-12-01" if iid == 1 else None,
        "confidential": iid == 4,
        "discussion_locked": None,
        "weight": None,
        "web_url": f"{WEB}/acme/widgets/-/issues/{iid}",
        "references": {"short": f"#{iid}", "relative": f"#{iid}", "full": f"acme/widgets#{iid}"},
    }
    base.update(extra)
    return base


ISSUES = [
    issue(
        5,
        "Outage on checkout",
        "opened",
        CAROL,
        ["bug", "incident"],
        "2026-11-14T09:00:00.000Z",
        "2026-11-14T10:00:00.000Z",
    ),
    issue(4, "Security review", "opened", ALICE, ["security"], "2026-11-01T12:00:00.000Z", "2026-11-02T12:00:00.000Z"),
    issue(3, "Add dark mode", "opened", BOB, ["feature"], "2026-10-15T08:00:00.000Z", "2026-11-05T08:00:00.000Z"),
    issue(
        2,
        "Widget crashes on resize",
        "closed",
        ALICE,
        ["bug"],
        "2026-10-01T08:00:00.000Z",
        "2026-10-03T08:00:00.000Z",
        "2026-10-03T08:00:00.000Z",
    ),
    issue(
        1,
        "Wrong total in cart",
        "opened",
        ALICE,
        ["bug", "frontend"],
        "2026-09-20T08:00:00.000Z",
        "2026-11-12T08:00:00.000Z",
    ),
]
GROUP_ISSUES = [
    *ISSUES,
    issue(
        1,
        "API returns 500",
        "opened",
        BOB,
        ["bug"],
        "2026-11-13T08:00:00.000Z",
        "2026-11-13T09:00:00.000Z",
        id=6001,
        project_id=103,
        web_url=f"{WEB}/acme/platform/api/-/issues/1",
        references={"short": "#1", "relative": "api#1", "full": "acme/platform/api#1"},
    ),
]

WORK_FILTERS = {
    "state": {"field": "state"},
    "labels": {"field": "labels", "op": "labels"},
    "author_username": {"field": "author.username"},
    "milestone": {"field": "milestone.title"},
    "iids[]": {"field": "iid", "op": "in"},
    "created_after": {"field": "created_at", "op": "gte"},
    "created_before": {"field": "created_at", "op": "lte"},
    "updated_after": {"field": "updated_at", "op": "gte"},
    "updated_before": {"field": "updated_at", "op": "lte"},
    "search": {"field": ["title", "description"], "op": "contains"},
}
ISSUE_FILTERS = {**WORK_FILTERS, "confidential": {"field": "confidential"}}

# ------------------------------------------------------------- merge requests


def mr(
    iid: int,
    title: str,
    state: str,
    author: dict[str, Any],
    source: str,
    created: str,
    updated: str,
    merged: str | None = None,
    closed: str | None = None,
    draft: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """A merge request object (GET /projects/:id/merge_requests)."""
    base = {
        "id": 7000 + iid,
        "iid": iid,
        "project_id": 101,
        "title": ("Draft: " if draft else "") + title,
        "description": f"This MR {title.lower()}.",
        "state": state,
        "created_at": created,
        "updated_at": updated,
        "merged_by": BOB if merged else None,
        "merge_user": BOB if merged else None,
        "merged_at": merged,
        "closed_by": ALICE if closed else None,
        "closed_at": closed,
        "target_branch": "main",
        "source_branch": source,
        "user_notes_count": 2 if iid == 1 else 0,
        "upvotes": 1,
        "downvotes": 0,
        "author": author,
        "assignees": [author],
        "reviewers": [CAROL] if iid in (1, 2) else [],
        "source_project_id": 101,
        "target_project_id": 101,
        "labels": ["backend"] if iid % 2 else ["frontend"],
        "draft": draft,
        "work_in_progress": draft,
        "milestone": None,
        "merge_when_pipeline_succeeds": False,
        "merge_status": "can_be_merged",
        "detailed_merge_status": "mergeable" if state == "opened" else "not_open",
        "sha": f"{iid:02d}" + "a" * 38,
        "merge_commit_sha": ("m" * 39 + str(iid)) if merged else None,
        "squash_commit_sha": None,
        "squash": iid == 4,
        "has_conflicts": False,
        "web_url": f"{WEB}/acme/widgets/-/merge_requests/{iid}",
        "references": {"short": f"!{iid}", "relative": f"!{iid}", "full": f"acme/widgets!{iid}"},
    }
    base.update(extra)
    return base


MRS = [
    mr(
        4,
        "Speed up search",
        "merged",
        CAROL,
        "perf/search",
        "2026-11-10T08:00:00.000Z",
        "2026-11-12T08:00:00.000Z",
        merged="2026-11-12T08:00:00.000Z",
    ),
    mr(
        3,
        "Remove legacy API",
        "closed",
        BOB,
        "chore/legacy",
        "2026-10-20T08:00:00.000Z",
        "2026-10-25T08:00:00.000Z",
        closed="2026-10-25T08:00:00.000Z",
    ),
    mr(
        2,
        "Dark mode",
        "opened",
        BOB,
        "feature/dark-mode",
        "2026-11-05T08:00:00.000Z",
        "2026-11-13T08:00:00.000Z",
        draft=True,
    ),
    mr(
        1,
        "Fix cart total",
        "merged",
        ALICE,
        "fix/cart-total",
        "2026-11-01T08:00:00.000Z",
        "2026-11-03T08:00:00.000Z",
        merged="2026-11-02T20:00:00.000Z",
    ),
]
MR_FILTERS = {
    **WORK_FILTERS,
    "source_branch": {"field": "source_branch"},
    "target_branch": {"field": "target_branch"},
    "wip": {"field": "draft", "op": "yesno"},
}


def note(
    nid: int,
    author: dict[str, Any],
    body: str,
    created: str,
    *,
    system: bool = False,
    noteable: str = "MergeRequest",
    **extra: Any,
) -> dict[str, Any]:
    """A note object."""
    base = {
        "id": nid,
        "type": "DiffNote" if extra.get("resolvable") else None,
        "body": body,
        "author": author,
        "created_at": created,
        "updated_at": created,
        "system": system,
        "noteable_id": 7001,
        "noteable_type": noteable,
        "resolvable": False,
        "confidential": False,
        "internal": False,
        "noteable_iid": 1,
    }
    base.update(extra)
    return base


MR_NOTES = [
    note(
        301,
        CAROL,
        "Can we add a test for the rounding?",
        "2026-11-01T10:00:00.000Z",
        resolvable=True,
        resolved=True,
        resolved_by=ALICE,
    ),
    note(302, ALICE, "added 1 commit", "2026-11-01T12:00:00.000Z", system=True),
    note(303, ALICE, "Test added, thanks!", "2026-11-01T12:05:00.000Z"),
]
ISSUE_NOTES = [
    note(401, BOB, "Reproduced on Firefox.", "2026-09-21T08:00:00.000Z", noteable="Issue"),
    note(402, ALICE, "changed the description", "2026-09-21T09:00:00.000Z", system=True, noteable="Issue"),
]
APPROVALS = {
    "id": 7001,
    "iid": 1,
    "project_id": 101,
    "title": "Fix cart total",
    "state": "merged",
    "approved": True,
    "approvals_required": 1,
    "approvals_left": 0,
    "user_has_approved": False,
    "user_can_approve": False,
    "approved_by": [{"user": CAROL}],
}
DIFFS = [
    {
        "old_path": "app/cart.rb",
        "new_path": "app/cart.rb",
        "a_mode": "100644",
        "b_mode": "100644",
        "diff": "@@ -10,7 +10,8 @@ class Cart\n   def total\n-    items.sum(&:price)\n+    items.sum(&:price).round(2)\n"
        "+    # rounded to cents\n   end\n",
        "new_file": False,
        "renamed_file": False,
        "deleted_file": False,
        "generated_file": False,
    },
    {
        "old_path": "spec/cart_spec.rb",
        "new_path": "spec/cart_spec.rb",
        "a_mode": "0",
        "b_mode": "100644",
        "diff": "@@ -0,0 +1,3 @@\n+describe Cart do\n+  it { expect(total).to eq 1.1 }\n+end\n",
        "new_file": True,
        "renamed_file": False,
        "deleted_file": False,
        "generated_file": False,
    },
    {
        "old_path": "vendor/big.min.js",
        "new_path": "vendor/big.min.js",
        "a_mode": "100644",
        "b_mode": "100644",
        "diff": "",
        "new_file": False,
        "renamed_file": False,
        "deleted_file": False,
        "too_large": True,
        "collapsed": True,
    },
]

# --------------------------------------------------------------------- repository


def commit(
    sha_char: str,
    title: str,
    author: str,
    email: str,
    when: str,
    paths: list[str],
    parents: list[str],
    refs: list[str],
    adds: int,
    dels: int,
) -> dict[str, Any]:
    """A commit object (GET /repository/commits)."""
    sha = sha_char * 40
    return {
        "id": sha,
        "short_id": sha[:8],
        "created_at": when,
        "parent_ids": [p * 40 for p in parents],
        "title": title,
        "message": f"{title}\n\nSigned-off-by: {author} <{email}>\n",
        "author_name": author,
        "author_email": email,
        "authored_date": when,
        "committer_name": author,
        "committer_email": email,
        "committed_date": when,
        "trailers": {},
        "web_url": f"{WEB}/acme/widgets/-/commit/{sha}",
        "stats": {"additions": adds, "deletions": dels, "total": adds + dels},
        "_paths": paths,
        "_refs": refs,
    }


COMMITS = [
    commit(
        "e",
        "Merge branch 'perf/search'",
        "Bob Builder",
        "bob@example.com",
        "2026-11-12T08:00:00.000Z",
        ["app/search.rb"],
        ["d", "c"],
        ["main"],
        30,
        12,
    ),
    commit(
        "d",
        "Speed up search",
        "Carol Danvers",
        "carol@example.com",
        "2026-11-11T15:00:00.000Z",
        ["app/search.rb"],
        ["b"],
        ["main", "perf/search"],
        30,
        12,
    ),
    commit(
        "c",
        "Fix cart total",
        "Alice Liddell",
        "alice@example.com",
        "2026-11-02T19:00:00.000Z",
        ["app/cart.rb", "spec/cart_spec.rb"],
        ["b"],
        ["main"],
        5,
        1,
    ),
    commit(
        "b",
        "Add README",
        "Alice Liddell",
        "alice@example.com",
        "2026-10-01T09:00:00.000Z",
        ["README.md"],
        ["a"],
        ["main", "perf/search"],
        40,
        0,
    ),
    commit(
        "a",
        "Initial commit",
        "Alice Liddell",
        "alice@example.com",
        "2026-09-01T09:00:00.000Z",
        ["app/cart.rb", "app/search.rb"],
        [],
        ["main", "perf/search"],
        200,
        0,
    ),
]


def branch(
    name: str, sha_char: str, when: str, *, default: bool = False, protected: bool = False, merged: bool = False
) -> dict[str, Any]:
    """A branch object."""
    return {
        "name": name,
        "merged": merged,
        "protected": protected,
        "default": default,
        "developers_can_push": not protected,
        "developers_can_merge": not protected,
        "can_push": True,
        "web_url": f"{WEB}/acme/widgets/-/tree/{name}",
        "commit": {"id": sha_char * 40, "short_id": sha_char * 8, "title": f"tip of {name}", "committed_date": when},
    }


BRANCHES = [
    branch("feature/dark-mode", "f", "2026-11-13T08:00:00.000Z"),
    branch("fix/cart-total", "c", "2026-11-02T19:00:00.000Z", merged=True),
    branch("main", "e", "2026-11-12T08:00:00.000Z", default=True, protected=True),
]
TAGS = [
    {
        "name": "v1.1.0",
        "message": "Release 1.1.0",
        "target": "t" * 40,
        "protected": True,
        "created_at": "2026-11-12T09:00:00.000Z",
        "commit": {"id": "e" * 40, "committed_date": "2026-11-12T08:00:00.000Z"},
        "release": {"tag_name": "v1.1.0", "description": "Faster search"},
    },
    {
        "name": "v1.0.1",
        "message": "",
        "target": "c" * 40,
        "protected": False,
        "created_at": None,
        "commit": {"id": "c" * 40, "committed_date": "2026-11-02T19:00:00.000Z"},
        "release": None,
    },
    {
        "name": "v1.0.0",
        "message": "First release",
        "target": "u" * 40,
        "protected": True,
        "created_at": "2026-10-01T10:00:00.000Z",
        "commit": {"id": "b" * 40, "committed_date": "2026-10-01T09:00:00.000Z"},
        "release": {"tag_name": "v1.0.0", "description": "First!"},
    },
]
RELEASES = [
    {
        "tag_name": "v2.0.0",
        "name": "Widgets 2.0",
        "description": "Coming soon",
        "created_at": "2026-11-14T00:00:00.000Z",
        "released_at": "2027-01-15T00:00:00.000Z",
        "upcoming_release": True,
        "author": ALICE,
        "commit": {"id": "e" * 40},
        "milestones": [{"id": 2, "title": "v2.0"}],
        "assets": {"count": 4, "sources": [], "links": []},
        "_links": {"self": f"{WEB}/acme/widgets/-/releases/v2.0.0"},
    },
    {
        "tag_name": "v1.1.0",
        "name": "Widgets 1.1",
        "description": "Faster search",
        "created_at": "2026-11-12T09:00:00.000Z",
        "released_at": "2026-11-12T09:00:00.000Z",
        "upcoming_release": False,
        "author": BOB,
        "commit": {"id": "e" * 40},
        "milestones": [],
        "assets": {"count": 5},
        "_links": {"self": f"{WEB}/acme/widgets/-/releases/v1.1.0"},
    },
    {
        "tag_name": "v1.0.0",
        "name": "Widgets 1.0",
        "description": "First!",
        "created_at": "2026-10-01T10:00:00.000Z",
        "released_at": "2026-10-01T10:00:00.000Z",
        "upcoming_release": False,
        "author": ALICE,
        "commit": {"id": "b" * 40},
        "milestones": [{"id": 1, "title": "v1.0"}],
        "assets": {"count": 4},
        "_links": {"self": f"{WEB}/acme/widgets/-/releases/v1.0.0"},
    },
]
CONTRIBUTORS = [
    {"name": "Alice Liddell", "email": "alice@example.com", "commits": 3, "additions": 0, "deletions": 0},
    {"name": "Bob Builder", "email": "bob@example.com", "commits": 1, "additions": 0, "deletions": 0},
    {"name": "Carol Danvers", "email": "carol@example.com", "commits": 1, "additions": 0, "deletions": 0},
]

# --------------------------------------------------------------------------- CI


def pipeline(
    pid: int,
    iid: int,
    status: str,
    source: str,
    ref: str,
    sha: str,
    created: str,
    updated: str,
    project_path: str = "acme/widgets",
    project_id: int = 101,
) -> dict[str, Any]:
    """A pipeline list item."""
    return {
        "id": pid,
        "iid": iid,
        "project_id": project_id,
        "sha": sha * 40,
        "ref": ref,
        "status": status,
        "source": source,
        "created_at": created,
        "updated_at": updated,
        "web_url": f"{WEB}/{project_path}/-/pipelines/{pid}",
    }


PIPELINES = [
    pipeline(5006, 6, "manual", "web", "main", "e", "2026-11-14T12:00:00.000Z", "2026-11-14T12:01:00.000Z"),
    pipeline(
        5005,
        5,
        "running",
        "merge_request_event",
        "feature/dark-mode",
        "f",
        "2026-11-13T08:00:00.000Z",
        "2026-11-13T08:05:00.000Z",
    ),
    pipeline(5004, 4, "success", "schedule", "main", "e", "2026-11-12T02:00:00.000Z", "2026-11-12T02:10:00.000Z"),
    pipeline(5003, 3, "canceled", "push", "perf/search", "d", "2026-11-11T15:01:00.000Z", "2026-11-11T15:03:00.000Z"),
    pipeline(5002, 2, "failed", "push", "main", "c", "2026-11-02T19:01:00.000Z", "2026-11-02T19:20:00.000Z"),
    pipeline(5001, 1, "success", "api", "main", "b", "2026-10-01T09:01:00.000Z", "2026-10-01T09:11:00.000Z"),
]
PIPELINE_TIMES = {
    5006: (None, None, None, 60.0),
    5005: ("2026-11-13T08:01:00.000Z", None, None, 60.0),
    5004: ("2026-11-12T02:00:30.000Z", "2026-11-12T02:10:00.000Z", 570, 30.0),
    5003: ("2026-11-11T15:01:10.000Z", "2026-11-11T15:03:00.000Z", 110, 10.0),
    5002: ("2026-11-02T19:02:00.000Z", "2026-11-02T19:20:00.000Z", 1080, 60.0),
    5001: ("2026-10-01T09:01:05.000Z", "2026-10-01T09:11:00.000Z", 595, 5.0),
}
TRIGGERERS = {5006: ALICE, 5005: BOB, 5004: ALICE, 5003: CAROL, 5002: ALICE, 5001: BOB}


def pipeline_detail(p: dict[str, Any]) -> dict[str, Any]:
    """The single-pipeline object (GET /pipelines/:id)."""
    started, finished, duration, queued = PIPELINE_TIMES[p["id"]]
    return {
        **p,
        "before_sha": "0" * 40,
        "tag": False,
        "yaml_errors": None,
        "user": TRIGGERERS[p["id"]],
        "started_at": started,
        "finished_at": finished,
        "committed_at": p["created_at"],
        "duration": duration,
        "queued_duration": queued,
        "coverage": "87.5" if p["status"] == "success" else None,
        "name": "Nightly" if p["source"] == "schedule" else None,
        "detailed_status": {"icon": f"status_{p['status']}", "text": p["status"], "group": p["status"]},
    }


PIPELINE_FILTERS = {
    "status": {"field": "status"},
    "ref": {"field": "ref"},
    "sha": {"field": "sha"},
    "source": {"field": "source"},
    "username": {"field": "_user"},
    "updated_after": {"field": "updated_at", "op": "gte"},
    "updated_before": {"field": "updated_at", "op": "lte"},
}
for _p in PIPELINES:
    _p["_user"] = TRIGGERERS[_p["id"]]["username"]

GADGETS_PIPELINES = [
    pipeline(
        6002,
        2,
        "success",
        "push",
        "main",
        "9",
        "2025-02-28T10:00:00.000Z",
        "2025-02-28T10:05:00.000Z",
        "acme/gadgets",
        102,
    ),
    pipeline(
        6001,
        1,
        "failed",
        "push",
        "main",
        "8",
        "2025-02-27T10:00:00.000Z",
        "2025-02-27T10:05:00.000Z",
        "acme/gadgets",
        102,
    ),
]


def job(
    jid: int,
    name: str,
    stage: str,
    status: str,
    pipeline_id: int,
    pipeline_status: str,
    ref: str,
    created: str,
    started: str | None,
    finished: str | None,
    duration: float | None,
    queued: float | None,
    failure: str | None = None,
    allow_failure: bool = False,
    project_path: str = "acme/widgets",
) -> dict[str, Any]:
    """A job object (GET /pipelines/:id/jobs, GET /projects/:id/jobs)."""
    return {
        "id": jid,
        "status": status,
        "stage": stage,
        "name": name,
        "ref": ref,
        "tag": False,
        "coverage": None,
        "allow_failure": allow_failure,
        "created_at": created,
        "started_at": started,
        "finished_at": finished,
        "erased_at": None,
        "duration": duration,
        "queued_duration": queued,
        "user": ALICE,
        "commit": {"id": "c" * 40, "short_id": "cccccccc", "title": "Fix cart total"},
        "pipeline": {"id": pipeline_id, "project_id": 101, "ref": ref, "sha": "c" * 40, "status": pipeline_status},
        "web_url": f"{WEB}/{project_path}/-/jobs/{jid}",
        "project": {"ci_job_token_scope_enabled": False},
        "artifacts": [],
        "runner": {
            "id": 77,
            "description": "shared-runner-1",
            "active": True,
            "is_shared": True,
            "runner_type": "instance_type",
            "name": "gitlab-runner",
            "online": True,
            "status": "online",
        },
        "tag_list": ["docker", "linux"],
        "failure_reason": failure,
        "archived": False,
    }


JOBS_5002 = [
    job(
        9004, "deploy", "deploy", "skipped", 5002, "failed", "main", "2026-11-02T19:01:00.000Z", None, None, None, None
    ),
    job(
        9003,
        "rspec",
        "test",
        "failed",
        5002,
        "failed",
        "main",
        "2026-11-02T19:01:00.000Z",
        "2026-11-02T19:05:00.000Z",
        "2026-11-02T19:20:00.000Z",
        900.0,
        120.0,
        failure="script_failure",
    ),
    job(
        9002,
        "lint",
        "test",
        "success",
        5002,
        "failed",
        "main",
        "2026-11-02T19:01:00.000Z",
        "2026-11-02T19:04:00.000Z",
        "2026-11-02T19:05:00.000Z",
        60.0,
        30.0,
    ),
    job(
        9001,
        "build",
        "build",
        "success",
        5002,
        "failed",
        "main",
        "2026-11-02T19:01:00.000Z",
        "2026-11-02T19:02:00.000Z",
        "2026-11-02T19:04:00.000Z",
        120.0,
        60.0,
    ),
]
RETRIED_5002 = [
    job(
        9000,
        "rspec",
        "test",
        "failed",
        5002,
        "failed",
        "main",
        "2026-11-02T19:01:00.000Z",
        "2026-11-02T19:04:30.000Z",
        "2026-11-02T19:04:50.000Z",
        20.0,
        90.0,
        failure="runner_system_failure",
    ),
]
JOBS_5004 = [
    job(
        9011,
        "build",
        "build",
        "success",
        5004,
        "success",
        "main",
        "2026-11-12T02:00:00.000Z",
        "2026-11-12T02:00:30.000Z",
        "2026-11-12T02:03:00.000Z",
        150.0,
        30.0,
    ),
    job(
        9012,
        "rspec",
        "test",
        "success",
        5004,
        "success",
        "main",
        "2026-11-12T02:00:00.000Z",
        "2026-11-12T02:03:10.000Z",
        "2026-11-12T02:10:00.000Z",
        410.0,
        10.0,
    ),
]
PROJECT_JOBS = sorted([*JOBS_5002, *RETRIED_5002, *JOBS_5004], key=lambda j: -j["id"])
JOB_FILTERS = {"scope[]": {"field": "status", "op": "in"}}
for _j in RETRIED_5002:
    _j["_retried"] = True

JOB_TRACE = (
    "\x1b[0KRunning with gitlab-runner 17.5.0 (abcdef12)\x1b[0;m\n"
    "\x1b[0K  on shared-runner-1 xyz, system ID: s_0123\x1b[0;m\n"
    'section_start:1730574300:prepare_executor\r\x1b[0K\x1b[0K\x1b[36;1mPreparing the "docker" executor\x1b[0;m\x1b[0;m\n'
    "\x1b[0KUsing Docker executor with image ruby:3.3 ...\x1b[0;m\n"
    "section_end:1730574304:prepare_executor\r\x1b[0K\n"
    "section_start:1730574304:get_sources[collapsed=true]\r\x1b[0K\x1b[0K\x1b[36;1mGetting source from Git repository\x1b[0;m\n"
    "\x1b[32;1mFetching changes with git depth set to 20...\x1b[0;m\n"
    "Receiving objects:  50% (5/10)\rReceiving objects: 100% (10/10), done.\n"
    "section_end:1730574310:get_sources\r\x1b[0K\n"
    'section_start:1730574310:step_script\r\x1b[0K\x1b[0K\x1b[36;1mExecuting "step_script" stage of the job script\x1b[0;m\n'
    "\x1b[32;1m$ bundle exec rspec\x1b[0;m\n"
    "..F.\n"
    "\x1b[31mERROR: Cart#total rounds to cents (FAILED - 1)\x1b[0m\n"
    "\x1b[31mERROR: expected 1.1, got 1.0999999\x1b[0m\n"
    "4 examples, 1 failure\n"
    "section_end:1730574400:step_script\r\x1b[0K\n"
    "section_start:1730574400:cleanup_file_variables\r\x1b[0K\x1b[0K\x1b[36;1mCleaning up project directory and file based variables\x1b[0;m\n"
    "section_end:1730574401:cleanup_file_variables\r\x1b[0K\n"
    "\x1b[31;1mERROR: Job failed: exit code 1\n"
    "\x1b[0;m\n"
)
# The same log as written by a runner with timestamped logs (FF_TIMESTAMPS, GitLab 17.1+).
JOB_TRACE_TIMESTAMPED = (
    "2026-11-12T02:00:30.100000Z 00O \x1b[0KRunning with gitlab-runner 17.5.0 (abcdef12)\x1b[0;m\n"
    '2026-11-12T02:00:30.200000Z 00O section_start:1762912830:step_script\r\x1b[0K\x1b[36;1mExecuting "step_script" stage\x1b[0;m\n'
    "2026-11-12T02:00:31.000000Z 00O \x1b[32;1m$ make build\x1b[0;m\n"
    "2026-11-12T02:00:40.000000Z 01E warning: deprecated fla\n"
    "2026-11-12T02:00:40.000500Z 01E+ g --old\n"
    "2026-11-12T02:02:59.000000Z 00O build ok\n"
    "2026-11-12T02:03:00.000000Z 00O section_end:1762912980:step_script\r\x1b[0K\n"
    "2026-11-12T02:03:00.100000Z 00O \x1b[32;1mJob succeeded\x1b[0;m\n"
)

ENVIRONMENTS = [
    {
        "id": 31,
        "name": "production",
        "slug": "production",
        "external_url": "https://widgets.example.com",
        "state": "available",
        "tier": "production",
        "created_at": "2024-03-01T00:00:00.000Z",
        "updated_at": "2026-11-12T03:00:00.000Z",
        "auto_stop_at": None,
    },
    {
        "id": 32,
        "name": "staging",
        "slug": "staging",
        "external_url": "https://staging.widgets.example.com",
        "state": "available",
        "tier": "staging",
        "created_at": "2024-03-01T00:00:00.000Z",
        "updated_at": "2026-11-12T02:30:00.000Z",
        "auto_stop_at": None,
    },
    {
        "id": 33,
        "name": "review/dark-mode",
        "slug": "review-dark-m-abc123",
        "external_url": None,
        "state": "stopped",
        "tier": "development",
        "created_at": "2026-11-05T09:00:00.000Z",
        "updated_at": "2026-11-13T09:00:00.000Z",
        "auto_stop_at": "2026-11-20T09:00:00.000Z",
    },
]


def deployment(
    did: int,
    iid: int,
    env: dict[str, Any],
    status: str,
    created: str,
    updated: str,
    job_id: int,
    pipeline_id: int,
    finished: str | None,
) -> dict[str, Any]:
    """A deployment object."""
    return {
        "id": did,
        "iid": iid,
        "ref": "main",
        "sha": "e" * 40,
        "created_at": created,
        "updated_at": updated,
        "finished_at": finished,
        "status": status,
        "user": BOB,
        "environment": {"id": env["id"], "name": env["name"], "external_url": env["external_url"]},
        "deployable": {
            "id": job_id,
            "status": status,
            "stage": "deploy",
            "name": f"deploy:{env['name']}",
            "ref": "main",
            "finished_at": finished,
            "pipeline": {"id": pipeline_id, "sha": "e" * 40, "ref": "main", "status": "success"},
        },
    }


DEPLOYMENTS = [
    deployment(
        43,
        3,
        ENVIRONMENTS[0],
        "success",
        "2026-11-12T02:50:00.000Z",
        "2026-11-12T03:00:00.000Z",
        9014,
        5004,
        "2026-11-12T03:00:00.000Z",
    ),
    deployment(
        42,
        2,
        ENVIRONMENTS[1],
        "success",
        "2026-11-12T02:20:00.000Z",
        "2026-11-12T02:30:00.000Z",
        9013,
        5004,
        "2026-11-12T02:30:00.000Z",
    ),
    deployment(
        41,
        1,
        ENVIRONMENTS[0],
        "failed",
        "2026-10-01T09:20:00.000Z",
        "2026-10-01T09:25:00.000Z",
        8999,
        5001,
        "2026-10-01T09:25:00.000Z",
    ),
]
DEPLOYMENT_FILTERS = {
    "status": {"field": "status"},
    "environment": {"field": "environment.name"},
    "updated_after": {"field": "updated_at", "op": "gte"},
    "updated_before": {"field": "updated_at", "op": "lte"},
}


def member(u: dict[str, Any], level: int, created: str, expires: str | None = None) -> dict[str, Any]:
    """A member object."""
    return {
        **u,
        "access_level": level,
        "created_at": created,
        "expires_at": expires,
        "created_by": ALICE,
        "membership_state": "active",
    }


PROJECT_MEMBERS = [
    member(ALICE, 50, "2024-02-01T10:00:00.000Z"),
    member(BOB, 40, "2024-02-02T10:00:00.000Z"),
    member(CAROL, 30, "2025-05-01T10:00:00.000Z", expires="2027-01-01"),
]
GROUP_MEMBERS = PROJECT_MEMBERS[:2]
MEMBER_FILTERS = {"query": {"field": ["username", "name"], "op": "contains"}}

# ------------------------------------------------------------------------ routes

ROUTES: dict[str, dict[str, Any]] = {
    # namespaces
    f"{API}/groups": {
        "items": GROUPS,
        "keyset": True,
        "params": {
            "search": {"field": ["name", "path"], "op": "contains"},
            "visibility": {"field": "visibility"},
            "all_available": {"op": "ignore"},
        },
    },
    f"{API}/groups/acme/descendant_groups": {
        "items": GROUPS[1:],
        "params": {
            "search": {"field": ["name", "path"], "op": "contains"},
            "visibility": {"field": "visibility"},
            "all_available": {"op": "ignore"},
        },
    },
    f"{API}/projects": {
        "items": [WIDGETS, GADGETS, API_PROJECT, DOTFILES],
        "keyset": True,
        "params": {**PROJECT_FILTERS, "membership": {"op": "ignore"}},
    },
    f"{API}/groups/acme/projects": {
        "items": [WIDGETS, GADGETS, API_PROJECT],
        "params": {**PROJECT_FILTERS, "include_subgroups": {"op": "ignore"}},
    },
    f"{API}/users/alice/projects": {"items": [DOTFILES], "params": PROJECT_FILTERS},
    f"{API}/projects/{W}": {"object": WIDGETS_DETAIL, "accept": ["license", "statistics"], "public": True},
    f"{API}/projects/{W}/members/all": {"items": PROJECT_MEMBERS, "params": MEMBER_FILTERS},
    f"{API}/projects/{W}/members": {"items": PROJECT_MEMBERS[:1], "params": MEMBER_FILTERS},
    f"{API}/groups/acme/members/all": {"items": GROUP_MEMBERS, "params": MEMBER_FILTERS},
    # work items
    f"{API}/projects/{W}/issues": {"items": ISSUES, "params": ISSUE_FILTERS, "accept": ["order_by", "sort"]},
    f"{API}/groups/acme/issues": {"items": GROUP_ISSUES, "params": ISSUE_FILTERS},
    f"{API}/projects/{W}/issues/1/notes": {"items": ISSUE_NOTES, "accept": ["order_by", "sort"]},
    f"{API}/projects/{W}/merge_requests": {"items": MRS, "params": MR_FILTERS},
    f"{API}/groups/acme/merge_requests": {"items": MRS, "params": MR_FILTERS},
    f"{API}/projects/{W}/merge_requests/1/notes": {"items": MR_NOTES, "accept": ["order_by", "sort"]},
    f"{API}/projects/{W}/merge_requests/1/approvals": {"object": APPROVALS},
    f"{API}/projects/{W}/merge_requests/1/diffs": {"items": DIFFS},
    # repository
    f"{API}/projects/{W}/repository/commits": {
        "items": COMMITS,
        "params": {
            "ref_name": {"op": "ref"},
            "since": {"field": "committed_date", "op": "gte"},
            "until": {"field": "committed_date", "op": "lte"},
            "path": {"op": "path"},
            "author": {"field": ["author_name", "author_email"], "op": "contains"},
            "all": {"op": "ignore"},
            "with_stats": {"op": "ignore"},
        },
        "strip_unless_absent": {"stats": "with_stats"},
    },
    f"{API}/projects/{W}/repository/branches": {
        "items": BRANCHES,
        "params": {"search": {"field": "name", "op": "anchored"}},
    },
    f"{API}/projects/{W}/repository/tags": {
        "items": TAGS,
        "params": {"search": {"field": "name", "op": "anchored"}},
        "accept": ["order_by", "sort"],
    },
    f"{API}/projects/{W}/releases": {"items": RELEASES, "accept": ["order_by", "sort"]},
    f"{API}/projects/{W}/repository/contributors": {"items": CONTRIBUTORS, "accept": ["order_by", "sort"]},
    # CI
    f"{API}/projects/{W}/pipelines": {"items": PIPELINES, "params": PIPELINE_FILTERS, "accept": ["order_by", "sort"]},
    f"{API}/projects/acme%2Fgadgets/pipelines": {"items": GADGETS_PIPELINES, "params": PIPELINE_FILTERS},
    f"{API}/projects/acme%2Fplatform%2Fapi/pipelines": {"items": [], "params": PIPELINE_FILTERS},
    **{f"{API}/projects/{W}/pipelines/{p['id']}": {"object": pipeline_detail(p)} for p in PIPELINES},
    **{
        f"{API}/projects/acme%2Fgadgets/pipelines/{p['id']}": {
            "object": {
                **p,
                "user": ALICE,
                "started_at": p["created_at"],
                "finished_at": p["updated_at"],
                "duration": 300,
                "queued_duration": 0,
            }
        }
        for p in GADGETS_PIPELINES
    },
    f"{API}/projects/{W}/pipelines/5002/jobs": {
        "items": JOBS_5002 + RETRIED_5002,
        "params": {**JOB_FILTERS, "include_retried": {"op": "ignore"}},
        "strip_retried_unless": "include_retried",
    },
    f"{API}/projects/{W}/pipelines/5004/jobs": {
        "items": JOBS_5004,
        "params": {**JOB_FILTERS, "include_retried": {"op": "ignore"}},
    },
    f"{API}/projects/{W}/jobs": {"items": PROJECT_JOBS, "params": JOB_FILTERS, "keyset": True},
    f"{API}/projects/acme%2Fgadgets/jobs": {"items": [], "params": JOB_FILTERS, "keyset": True},
    f"{API}/projects/{W}/jobs/9003/trace": {"text": JOB_TRACE},
    f"{API}/projects/{W}/jobs/9011/trace": {"text": JOB_TRACE_TIMESTAMPED},
    # deployments
    f"{API}/projects/{W}/environments": {
        "items": ENVIRONMENTS,
        "params": {
            "name": {"field": "name"},
            "search": {"field": "name", "op": "contains"},
            "states": {"field": "state"},
        },
    },
    f"{API}/projects/{W}/deployments": {
        "items": DEPLOYMENTS,
        "params": DEPLOYMENT_FILTERS,
        "accept": ["order_by", "sort"],
    },
}
