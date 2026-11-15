"""Issues, merge requests, their notes, approvals and diff statistics.

Column names follow vgi-github's ``issues`` / ``pulls`` / ``issue_comments``
where the concepts match: ``repo`` is the project path, ``number`` the
per-project iid, ``user_login`` the author, ``head_ref`` / ``base_ref`` the
source / target branch. ``state`` uses GitHub's vocabulary (``open`` /
``closed``; a merged merge request is ``closed`` with ``merged_at`` set), and
GitLab's own value is kept in ``gitlab_state``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg, SecretLookupEntry
from vgi.metadata import FunctionExample

from .client import GITLAB_SECRET_TYPE, encode_id
from .convert import TSTZ, get, names
from .meta import column, function_tags
from .scan import ApiScan, Listing, Lookup, Pushdown, ScanContext, range_params

LIST = pa.list_(pa.string())

GITHUB_STATE = {"opened": "open", "closed": "closed", "merged": "closed", "locked": "closed"}


def repo_of(obj: dict[str, Any], fallback: str | None) -> str | None:
    """Project path from ``references.full`` ('group/name#12' or 'group/name!3'), else ``fallback``."""
    full = get(obj, "references", "full")
    if isinstance(full, str):
        for sep in ("#", "!"):
            if sep in full:
                return full.rsplit(sep, 1)[0]
    return fallback


def _state_params(pd: Pushdown, *, merge_requests: bool) -> tuple[list[tuple[str, Any]], bool]:
    """Translate ``state`` / ``gitlab_state`` predicates into GitLab's ``state`` parameter.

    Args:
        pd: The pushed-down filters.
        merge_requests: Translating for merge requests (SQL 'closed' then includes merged).

    Returns:
        The parameters, and False when the predicates can match nothing.

    """
    raw = pd.one("gitlab_state")
    if raw:
        return [("state", raw)], True
    state = pd.values("state")
    if state is not None and len(set(state)) == 1:
        if state[0] == "open":
            return [("state", "opened")], True
        if state[0] == "closed" and not merge_requests:
            return [("state", "closed")], True
        if state[0] not in ("open", "closed"):
            return [], False
    return [], True


def _common_params(ctx: ScanContext, labels: str | None) -> list[tuple[str, Any]]:
    pd = ctx.pd
    q: list[tuple[str, Any]] = []
    author = pd.one("user_login")
    if author:
        q.append(("author_username", author))
    iids = pd.values("number")
    if iids:
        q.append(("iids[]", sorted({int(i) for i in iids})))
    milestone = pd.one("milestone")
    if milestone:
        q.append(("milestone", milestone))
    if labels:
        q.append(("labels", labels))
    q += range_params(pd, "created_at", "created_after", "created_before")
    q += range_params(pd, "updated_at", "updated_after", "updated_before")
    return q


# --------------------------------------------------------------------- issues

ISSUE_SCHEMA = pa.schema(
    [
        column("repo", pa.string(), "Project path 'group/name' the issue belongs to."),
        column("number", pa.int64(), "Issue iid within the project — what issue_notes() takes. Pushed down."),
        column("id", pa.int64(), "GitLab's instance-wide issue id.", nullable=False),
        column("title", pa.string(), "Issue title."),
        column("state", pa.string(), "'open' or 'closed' (GitHub vocabulary). Pushed down."),
        column("gitlab_state", pa.string(), "GitLab's own state: 'opened' or 'closed'. Pushed down."),
        column("user_login", pa.string(), "Username of the author. Pushed down (author_username)."),
        column("labels", LIST, "Labels applied; filter with the `labels` argument to push down."),
        column("assignees", LIST, "Usernames of the assignees."),
        column("milestone", pa.string(), "Milestone title. Pushed down."),
        column("comments", pa.int64(), "Number of user (non-system) notes."),
        column("reactions_plus_one", pa.int64(), "Thumbs-up (upvote) count."),
        column("downvotes", pa.int64(), "Thumbs-down count."),
        column("is_locked", pa.bool_(), "Whether the discussion is locked."),
        column("is_confidential", pa.bool_(), "Whether the issue is confidential. Pushed down."),
        column("issue_type", pa.string(), "'issue', 'incident', 'test_case' or 'task'."),
        column("weight", pa.int64(), "Issue weight, if set (Premium)."),
        column("due_date", pa.date32(), "Due date, if set."),
        column("closed_by_login", pa.string(), "Username of whoever closed the issue."),
        column("body", pa.string(), "Description as Markdown."),
        column("html_url", pa.string(), "Link to the issue in the GitLab UI."),
        column("created_at", TSTZ, "When the issue was opened. Ranges are pushed down."),
        column("updated_at", TSTZ, "When the issue last changed. Ranges are pushed down."),
        column("closed_at", TSTZ, "When the issue was closed; NULL while open."),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class IssuesArgs:
    """Arguments for ``issues(project, labels := NULL, search := NULL)``."""

    project: Annotated[str, Arg(0, doc="Project path 'group/name' (or id), or a group path for all its projects.")]
    labels: Annotated[
        str | None, Arg("labels", default=None, doc="Comma-separated labels; an issue must carry all of them.")
    ] = None
    search: Annotated[
        str | None, Arg("search", default=None, doc="Only issues whose title or description contains this text.")
    ] = None


class Issues(ApiScan[IssuesArgs]):
    """``issues(project)``: a project's (or group's) issues."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = ISSUE_SCHEMA

    class Meta:
        """Function metadata."""

        name = "issues"
        description = "Issues of a GitLab project or group, newest first, with filters pushed to the API"
        filter_pushdown = True
        auto_apply_filters = True
        projection_pushdown = True
        tags = function_tags(
            ISSUE_SCHEMA,
            title="Issues",
            category="work_items",
            keywords="issues, bugs, tickets, labels, milestones, triage, incidents",
            doc_llm=(
                "issues(project, labels := NULL, search := NULL) lists issues of a project ('group/name') or, given "
                "a group path, of every project in the group. Columns mirror vgi-github issues(): repo, number "
                "(iid), id, title, state ('open'/'closed'), gitlab_state ('opened'/'closed'), user_login (author), "
                "labels, assignees, milestone, comments, reactions_plus_one, downvotes, is_locked, is_confidential, "
                "issue_type, weight, due_date, closed_by_login, body, html_url, created_at, updated_at, closed_at. "
                "Pushed to the API: state / gitlab_state =, user_login =, number = / IN, milestone =, "
                "is_confidential =, created_at and updated_at ranges; labels via the labels argument "
                "(list_contains(labels, ...) works too but filters after fetching)."
            ),
            doc_md=(
                "## issues(project, labels := NULL, search := NULL)\n\n"
                "Issues of a project or group. State, author, iid, milestone and date filters are sent to GitLab."
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT number, title, user_login, created_at FROM gitlab.main.issues('gitlab-org/gitlab-runner', "
                    "labels := 'bug') WHERE state = 'open' ORDER BY created_at DESC LIMIT 20"
                ),
                description="The newest open bugs of a project",
            ),
            FunctionExample(
                sql=(
                    "SELECT date_trunc('month', closed_at) AS month, count(*) AS closed, "
                    "median(date_diff('day', created_at, closed_at)) AS median_days "
                    "FROM gitlab.main.issues('gitlab-org/cli') "
                    "WHERE state = 'closed' AND created_at >= TIMESTAMPTZ '2026-01-01' GROUP BY ALL ORDER BY month"
                ),
                description="Issues closed per month and how long they stayed open",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """Project issues, else group issues."""
        a: IssuesArgs = ctx.args
        state, possible = _state_params(ctx.pd, merge_requests=False)
        if not possible:
            return []
        q = [*state, *_common_params(ctx, a.labels)]
        conf = ctx.pd.one("is_confidential")
        if isinstance(conf, bool):
            q.append(("confidential", conf))
        if a.search:
            q.append(("search", a.search))
        pid = encode_id(a.project)
        return [
            Listing(
                paths=(f"projects/{pid}/issues", f"groups/{pid}/issues"),
                query=tuple(q),
                context=(("repo", a.project),),
            )
        ]

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map issue objects."""
        fallback = listing.ctx("repo") if ctx.path.startswith("projects/") else None
        return [
            {
                "repo": repo_of(i, fallback),
                "number": i.get("iid"),
                "id": i.get("id"),
                "title": i.get("title"),
                "state": GITHUB_STATE.get(i.get("state") or "", i.get("state")),
                "gitlab_state": i.get("state"),
                "user_login": get(i, "author", "username"),
                "labels": i.get("labels"),
                "assignees": names(i.get("assignees"), "username"),
                "milestone": get(i, "milestone", "title"),
                "comments": i.get("user_notes_count"),
                "reactions_plus_one": i.get("upvotes"),
                "downvotes": i.get("downvotes"),
                "is_locked": i.get("discussion_locked"),
                "is_confidential": i.get("confidential"),
                "issue_type": i.get("issue_type") or i.get("type"),
                "weight": i.get("weight"),
                "due_date": i.get("due_date"),
                "closed_by_login": get(i, "closed_by", "username"),
                "body": i.get("description"),
                "html_url": i.get("web_url"),
                "created_at": i.get("created_at"),
                "updated_at": i.get("updated_at"),
                "closed_at": i.get("closed_at"),
            }
            for i in items
        ]


# ------------------------------------------------------------- merge requests

MR_SCHEMA = pa.schema(
    [
        column("repo", pa.string(), "Project path 'group/name' the merge request targets."),
        column("number", pa.int64(), "Merge request iid within the project. Pushed down."),
        column("id", pa.int64(), "GitLab's instance-wide merge request id.", nullable=False),
        column("title", pa.string(), "Title."),
        column(
            "state",
            pa.string(),
            "'open' or 'closed' (GitHub vocabulary; merged counts as closed — test merged_at IS NOT NULL). "
            "Pushed down.",
        ),
        column("gitlab_state", pa.string(), "GitLab's state: 'opened', 'closed', 'merged' or 'locked'. Pushed down."),
        column("is_draft", pa.bool_(), "Whether the merge request is a draft. Pushed down."),
        column("user_login", pa.string(), "Username of the author. Pushed down."),
        column("head_ref", pa.string(), "Source branch. Pushed down."),
        column("head_sha", pa.string(), "Commit at the tip of the source branch."),
        column("base_ref", pa.string(), "Target branch. Pushed down."),
        column("source_project_id", pa.int64(), "Project the source branch lives in; differs for forks."),
        column("target_project_id", pa.int64(), "Project the merge request targets."),
        column("labels", LIST, "Labels applied; use the `labels` argument to push down."),
        column("assignees", LIST, "Usernames of the assignees."),
        column("requested_reviewers", LIST, "Usernames of the reviewers."),
        column("milestone", pa.string(), "Milestone title. Pushed down."),
        column("merge_commit_sha", pa.string(), "The merge commit (or squash commit) once merged."),
        column("merged_by_login", pa.string(), "Username of whoever merged it."),
        column("comments", pa.int64(), "Number of user (non-system) notes."),
        column("reactions_plus_one", pa.int64(), "Thumbs-up (upvote) count."),
        column("has_conflicts", pa.bool_(), "Whether the merge request has merge conflicts."),
        column("detailed_merge_status", pa.string(), "Mergeability, e.g. 'mergeable', 'ci_must_pass', 'conflict'."),
        column("is_squash", pa.bool_(), "Whether commits are squashed on merge."),
        column("body", pa.string(), "Description as Markdown."),
        column("html_url", pa.string(), "Link to the merge request in the GitLab UI."),
        column("created_at", TSTZ, "When it was opened. Ranges are pushed down."),
        column("updated_at", TSTZ, "When it last changed. Ranges are pushed down."),
        column("closed_at", TSTZ, "When it was closed without merging; NULL otherwise."),
        column("merged_at", TSTZ, "When it was merged; NULL if open or closed unmerged."),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class MergeRequestsArgs:
    """Arguments for ``merge_requests(project, labels := NULL, search := NULL)``."""

    project: Annotated[str, Arg(0, doc="Project path 'group/name' (or id), or a group path for all its projects.")]
    labels: Annotated[
        str | None, Arg("labels", default=None, doc="Comma-separated labels; a merge request must carry all of them.")
    ] = None
    search: Annotated[
        str | None, Arg("search", default=None, doc="Only merge requests whose title or description contains this.")
    ] = None


class MergeRequests(ApiScan[MergeRequestsArgs]):
    """``merge_requests(project)``: GitLab's pull requests."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = MR_SCHEMA

    class Meta:
        """Function metadata."""

        name = "merge_requests"
        description = "Merge requests of a GitLab project or group, with filters pushed to the API"
        filter_pushdown = True
        auto_apply_filters = True
        projection_pushdown = True
        tags = function_tags(
            MR_SCHEMA,
            title="Merge Requests",
            category="work_items",
            keywords="merge requests, pull requests, mr, code review, branches, merged, draft",
            doc_llm=(
                "merge_requests(project, labels := NULL, search := NULL) lists merge requests (GitLab's pull "
                "requests) of a project or, given a group path, of every project in it. Columns mirror vgi-github "
                "pulls(): repo, number (iid), id, title, state ('open'/'closed'; merged is closed), gitlab_state "
                "('opened'/'closed'/'merged'/'locked'), is_draft, user_login, head_ref (source branch), head_sha, "
                "base_ref (target branch), labels, assignees, requested_reviewers, milestone, merge_commit_sha, "
                "merged_by_login, comments, body, html_url, created_at, updated_at, closed_at, merged_at, plus "
                "GitLab extras. Pushed down: state / gitlab_state =, is_draft =, user_login =, head_ref =, "
                "base_ref =, number = / IN, milestone =, created_at and updated_at ranges."
            ),
            doc_md=(
                "## merge_requests(project, labels := NULL, search := NULL)\n\n"
                "Merge requests with GitHub-compatible columns. Use `gitlab_state = 'merged'` to fetch only merged."
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT count(*) AS merged, median(date_diff('hour', created_at, merged_at)) AS median_hours "
                    "FROM gitlab.main.merge_requests('gitlab-org/cli') WHERE gitlab_state = 'merged' "
                    "AND merged_at >= TIMESTAMPTZ '2026-10-01'"
                ),
                description="How quickly merge requests get merged",
            ),
            FunctionExample(
                sql=(
                    "SELECT number, title, user_login FROM gitlab.main.merge_requests('gitlab-org/gitlab-runner') "
                    "WHERE state = 'open' AND base_ref = 'main' AND NOT is_draft ORDER BY updated_at DESC LIMIT 10"
                ),
                description="Open, ready-for-review merge requests into main",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """Project merge requests, else group merge requests."""
        a: MergeRequestsArgs = ctx.args
        pd = ctx.pd
        state, possible = _state_params(pd, merge_requests=True)
        if not possible:
            return []
        q = [*state, *_common_params(ctx, a.labels)]
        for col, param in (("head_ref", "source_branch"), ("base_ref", "target_branch")):
            v = pd.one(col)
            if v:
                q.append((param, v))
        draft = pd.one("is_draft")
        if isinstance(draft, bool):
            q.append(("wip", "yes" if draft else "no"))
        if a.search:
            q.append(("search", a.search))
        pid = encode_id(a.project)
        return [
            Listing(
                paths=(f"projects/{pid}/merge_requests", f"groups/{pid}/merge_requests"),
                query=tuple(q),
                context=(("repo", a.project),),
            )
        ]

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map merge request objects."""
        fallback = listing.ctx("repo") if ctx.path.startswith("projects/") else None
        out = []
        for m in items:
            draft = m.get("draft")
            out.append(
                {
                    "repo": repo_of(m, fallback),
                    "number": m.get("iid"),
                    "id": m.get("id"),
                    "title": m.get("title"),
                    "state": GITHUB_STATE.get(m.get("state") or "", m.get("state")),
                    "gitlab_state": m.get("state"),
                    "is_draft": draft if draft is not None else m.get("work_in_progress"),
                    "user_login": get(m, "author", "username"),
                    "head_ref": m.get("source_branch"),
                    "head_sha": m.get("sha"),
                    "base_ref": m.get("target_branch"),
                    "source_project_id": m.get("source_project_id"),
                    "target_project_id": m.get("target_project_id"),
                    "labels": m.get("labels"),
                    "assignees": names(m.get("assignees"), "username"),
                    "requested_reviewers": names(m.get("reviewers"), "username"),
                    "milestone": get(m, "milestone", "title"),
                    "merge_commit_sha": m.get("merge_commit_sha") or m.get("squash_commit_sha"),
                    "merged_by_login": get(m, "merge_user", "username") or get(m, "merged_by", "username"),
                    "comments": m.get("user_notes_count"),
                    "reactions_plus_one": m.get("upvotes"),
                    "has_conflicts": m.get("has_conflicts"),
                    "detailed_merge_status": m.get("detailed_merge_status") or m.get("merge_status"),
                    "is_squash": m.get("squash"),
                    "body": m.get("description"),
                    "html_url": m.get("web_url"),
                    "created_at": m.get("created_at"),
                    "updated_at": m.get("updated_at"),
                    "closed_at": m.get("closed_at"),
                    "merged_at": m.get("merged_at"),
                }
            )
        return out


# ---------------------------------------------------------------------- notes

NOTE_SCHEMA = pa.schema(
    [
        column("repo", pa.string(), "Project path the note belongs to."),
        column("number", pa.int64(), "iid of the issue or merge request the note is on."),
        column("id", pa.int64(), "GitLab's note id.", nullable=False),
        column("user_login", pa.string(), "Username of the note's author."),
        column("body", pa.string(), "Note text as Markdown (system notes describe events, e.g. 'added 1 commit')."),
        column("is_system", pa.bool_(), "Whether GitLab generated the note (label changes, pushes, ...)."),
        column("is_internal", pa.bool_(), "Whether the note is internal (visible to project members only)."),
        column("is_resolvable", pa.bool_(), "Whether the note starts a resolvable thread."),
        column("is_resolved", pa.bool_(), "Whether its thread is resolved."),
        column("resolved_by_login", pa.string(), "Who resolved the thread."),
        column("created_at", TSTZ, "When the note was posted."),
        column("updated_at", TSTZ, "When the note was last edited."),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class NotesArgs:
    """Arguments for ``issue_notes`` / ``merge_request_notes(project, number)``."""

    project: Annotated[str, Arg(0, doc="Project path 'group/name' or id.")]
    number: Annotated[int, Arg(1, doc="The issue or merge request iid (the number column).", ge=1)]


def _note_rows(items: list[Any], listing: Listing) -> list[dict[str, Any]]:
    return [
        {
            "repo": listing.ctx("repo"),
            "number": listing.ctx("number"),
            "id": n.get("id"),
            "user_login": get(n, "author", "username"),
            "body": n.get("body"),
            "is_system": n.get("system"),
            "is_internal": n.get("internal", n.get("confidential")),
            "is_resolvable": n.get("resolvable"),
            "is_resolved": n.get("resolved"),
            "resolved_by_login": get(n, "resolved_by", "username"),
            "created_at": n.get("created_at"),
            "updated_at": n.get("updated_at"),
        }
        for n in items
    ]


def _notes_listing(ctx: ScanContext, kind: str) -> list[Listing]:
    a: NotesArgs = ctx.args
    return [
        Listing(
            paths=(f"projects/{encode_id(a.project)}/{kind}/{a.number}/notes",),
            query=(("order_by", "created_at"), ("sort", "asc")),
            context=(("repo", a.project), ("number", a.number)),
        )
    ]


_NOTES_DOC = (
    "Columns: repo, number, id, user_login, body, is_system (GitLab-generated event notes), is_internal, "
    "is_resolvable, is_resolved, resolved_by_login, created_at, updated_at — oldest first. Filter NOT is_system "
    "for the human conversation; use LATERAL to read the notes of many {kind}s."
)


class IssueNotes(Lookup[NotesArgs]):
    """``issue_notes(project, number)``: comments on an issue."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = NOTE_SCHEMA
    KEYS: ClassVar[tuple[str, ...]] = ("project", "number")

    class Meta:
        """Function metadata."""

        name = "issue_notes"
        description = "Notes (comments and system events) on one GitLab issue, oldest first"
        required_secrets = [SecretLookupEntry(secret_type=GITLAB_SECRET_TYPE)]
        projection_pushdown = True
        tags = function_tags(
            NOTE_SCHEMA,
            title="Issue Notes",
            category="work_items",
            keywords="issue comments, notes, discussion, conversation, issue thread",
            doc_llm="issue_notes(project, number) lists the notes on issue `number` (its iid). "
            + _NOTES_DOC.format(kind="issue"),
            doc_md="## issue_notes(project, number)\n\nThe conversation on an issue, like vgi-github `issue_comments`.",
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT user_login, created_at, body FROM gitlab.main.issue_notes('gitlab-org/cli', 1) "
                    "WHERE NOT is_system ORDER BY created_at"
                ),
                description="The human comments on an issue",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """``/projects/:id/issues/:iid/notes``."""
        return _notes_listing(ctx, "issues")

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map note objects."""
        return _note_rows(items, listing)


class MergeRequestNotes(Lookup[NotesArgs]):
    """``merge_request_notes(project, number)``: comments on a merge request."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = NOTE_SCHEMA
    KEYS: ClassVar[tuple[str, ...]] = ("project", "number")

    class Meta:
        """Function metadata."""

        name = "merge_request_notes"
        description = "Notes (review comments and system events) on one GitLab merge request, oldest first"
        required_secrets = [SecretLookupEntry(secret_type=GITLAB_SECRET_TYPE)]
        projection_pushdown = True
        tags = function_tags(
            NOTE_SCHEMA,
            title="Merge Request Notes",
            category="work_items",
            keywords="merge request comments, review comments, notes, code review, discussion threads",
            doc_llm="merge_request_notes(project, number) lists the notes on merge request `number` (its iid). "
            + _NOTES_DOC.format(kind="merge request"),
            doc_md="## merge_request_notes(project, number)\n\nThe review conversation on a merge request.",
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT user_login, count(*) AS comments FROM gitlab.main.merge_request_notes('gitlab-org/cli', 1) "
                    "WHERE NOT is_system GROUP BY user_login ORDER BY comments DESC"
                ),
                description="Who commented on a merge request",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """``/projects/:id/merge_requests/:iid/notes``."""
        return _notes_listing(ctx, "merge_requests")

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map note objects."""
        return _note_rows(items, listing)


# ------------------------------------------------------------------ approvals

APPROVAL_SCHEMA = pa.schema(
    [
        column("repo", pa.string(), "Project path."),
        column("number", pa.int64(), "Merge request iid.", nullable=False),
        column("is_approved", pa.bool_(), "Whether the merge request satisfies its approval rules."),
        column("approvals_required", pa.int64(), "Approvals the rules require (Premium; 0 on Free)."),
        column("approvals_left", pa.int64(), "Approvals still missing."),
        column("approved_by", LIST, "Usernames of everyone who approved."),
        column("user_has_approved", pa.bool_(), "Whether the token's user has approved."),
    ]
)


class MergeRequestApprovals(Lookup[NotesArgs]):
    """``merge_request_approvals(project, number)``: the approval state of one merge request."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = APPROVAL_SCHEMA
    KEYS: ClassVar[tuple[str, ...]] = ("project", "number")

    class Meta:
        """Function metadata."""

        name = "merge_request_approvals"
        description = "Approval state of one GitLab merge request: who approved and how many are still needed"
        required_secrets = [SecretLookupEntry(secret_type=GITLAB_SECRET_TYPE)]
        projection_pushdown = True
        tags = function_tags(
            APPROVAL_SCHEMA,
            title="Merge Request Approvals",
            category="work_items",
            keywords="approvals, approvers, code review, approved by, approval rules",
            doc_llm=(
                "merge_request_approvals(project, number) returns one row: repo, number, is_approved, "
                "approvals_required, approvals_left, approved_by (list of usernames), user_has_approved. Use LATERAL "
                "over merge_requests() to audit approvals across many merge requests."
            ),
            doc_md="## merge_request_approvals(project, number)\n\nWho approved a merge request.",
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT m.number, a.approved_by FROM gitlab.main.merge_requests('gitlab-org/cli') m, "
                    "LATERAL gitlab.main.merge_request_approvals(m.repo, m.number) a "
                    "WHERE m.gitlab_state = 'merged' AND len(a.approved_by) = 0"
                ),
                description="Merged merge requests that nobody approved",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """``/projects/:id/merge_requests/:iid/approvals``."""
        a: NotesArgs = ctx.args
        return [
            Listing(
                paths=(f"projects/{encode_id(a.project)}/merge_requests/{a.number}/approvals",),
                single=True,
                context=(("repo", a.project), ("number", a.number)),
            )
        ]

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map the approvals object."""
        return [
            {
                "repo": listing.ctx("repo"),
                "number": listing.ctx("number"),
                "is_approved": o.get("approved"),
                "approvals_required": o.get("approvals_required"),
                "approvals_left": o.get("approvals_left"),
                "approved_by": names(o.get("approved_by"), "user", "username") or [],
                "user_has_approved": o.get("user_has_approved"),
            }
            for o in items
        ]


# ----------------------------------------------------------------- diff stats

DIFF_SCHEMA = pa.schema(
    [
        column("repo", pa.string(), "Project path."),
        column("number", pa.int64(), "Merge request iid."),
        column("old_path", pa.string(), "File path before the change."),
        column("new_path", pa.string(), "File path after the change."),
        column("is_new_file", pa.bool_(), "Whether the file was added."),
        column("is_renamed_file", pa.bool_(), "Whether the file was renamed."),
        column("is_deleted_file", pa.bool_(), "Whether the file was deleted."),
        column("additions", pa.int64(), "Lines added (counted from the diff hunk)."),
        column("deletions", pa.int64(), "Lines removed (counted from the diff hunk)."),
        column("is_too_large", pa.bool_(), "GitLab omitted the diff text because it is too large (counts are NULL)."),
    ]
)


def diff_counts(diff: str | None) -> tuple[int | None, int | None]:
    """Count added and removed lines in a unified diff hunk (``+++``/``---`` headers excluded)."""
    if diff is None:
        return None, None
    adds = dels = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++ "):
            adds += 1
        elif line.startswith("-") and not line.startswith("--- "):
            dels += 1
    return adds, dels


class MergeRequestDiffStats(Lookup[NotesArgs]):
    """``merge_request_diff_stats(project, number)``: per-file line counts of a merge request."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = DIFF_SCHEMA
    KEYS: ClassVar[tuple[str, ...]] = ("project", "number")

    class Meta:
        """Function metadata."""

        name = "merge_request_diff_stats"
        description = "Files changed by one GitLab merge request with added and removed line counts"
        required_secrets = [SecretLookupEntry(secret_type=GITLAB_SECRET_TYPE)]
        projection_pushdown = True
        tags = function_tags(
            DIFF_SCHEMA,
            title="Merge Request Diff Stats",
            category="work_items",
            keywords="diff, changed files, lines added, lines removed, churn, merge request size",
            doc_llm=(
                "merge_request_diff_stats(project, number) lists the files a merge request changes: repo, number, "
                "old_path, new_path, is_new_file, is_renamed_file, is_deleted_file, additions, deletions, "
                "is_too_large. additions/deletions are counted from the diff text (NULL when GitLab omits a large "
                "diff). Sum them for the merge request's size."
            ),
            doc_md="## merge_request_diff_stats(project, number)\n\nChanged files and line counts of a merge request.",
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT count(*) AS files, sum(additions) AS added, sum(deletions) AS removed "
                    "FROM gitlab.main.merge_request_diff_stats('gitlab-org/cli', 1)"
                ),
                description="Size of a merge request",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """``/projects/:id/merge_requests/:iid/diffs`` (paginated, GitLab 15.7+)."""
        a: NotesArgs = ctx.args
        return [
            Listing(
                paths=(f"projects/{encode_id(a.project)}/merge_requests/{a.number}/diffs",),
                context=(("repo", a.project), ("number", a.number)),
            )
        ]

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map diff objects to counts."""
        out = []
        for d in items:
            too_large = bool(d.get("too_large")) or bool(d.get("collapsed") and not d.get("diff"))
            adds, dels = (None, None) if too_large else diff_counts(d.get("diff"))
            out.append(
                {
                    "repo": listing.ctx("repo"),
                    "number": listing.ctx("number"),
                    "old_path": d.get("old_path"),
                    "new_path": d.get("new_path"),
                    "is_new_file": d.get("new_file"),
                    "is_renamed_file": d.get("renamed_file"),
                    "is_deleted_file": d.get("deleted_file"),
                    "additions": adds,
                    "deletions": dels,
                    "is_too_large": too_large,
                }
            )
        return out
