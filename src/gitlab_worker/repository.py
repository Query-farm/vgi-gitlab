"""Commits, branches, tags, releases and contributors.

Column names follow vgi-github's ``commits`` / ``releases`` / ``contributors``
where the concepts match.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg
from vgi.metadata import FunctionExample

from .client import encode_id
from .convert import TSTZ, get, names
from .meta import column, function_tags
from .scan import ApiScan, Listing, ScanContext, iso

LIST = pa.list_(pa.string())


@dataclass(slots=True, frozen=True, kw_only=True)
class ProjectOnlyArgs:
    """Arguments for functions that take just a project."""

    project: Annotated[str, Arg(0, doc="Project path 'group/name' or numeric id, e.g. 'gitlab-org/cli'.")]


# -------------------------------------------------------------------- commits

COMMIT_SCHEMA = pa.schema(
    [
        column("repo", pa.string(), "Project path the commit was listed from."),
        column("sha", pa.string(), "Full commit hash.", nullable=False),
        column("short_sha", pa.string(), "Abbreviated hash, as GitLab shows it."),
        column("title", pa.string(), "First line of the commit message."),
        column("message", pa.string(), "Full commit message."),
        column("author_name", pa.string(), "Author name from the git metadata. Pushed down (author)."),
        column("author_email", pa.string(), "Author email from the git metadata."),
        column("author_date", TSTZ, "When the change was originally authored."),
        column("committer_name", pa.string(), "Committer name from the git metadata."),
        column("committer_email", pa.string(), "Committer email from the git metadata."),
        column("committer_date", TSTZ, "When the commit was committed. Ranges are pushed down (since/until)."),
        column("parent_shas", LIST, "Parent hashes; more than one means a merge commit."),
        column("additions", pa.int64(), "Lines added (only with with_stats := true)."),
        column("deletions", pa.int64(), "Lines removed (only with with_stats := true)."),
        column("html_url", pa.string(), "Link to the commit in the GitLab UI."),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class CommitsArgs:
    """Arguments for ``commits(project, ref := NULL, since := NULL, until := NULL, path := NULL, ...)``."""

    project: Annotated[str, Arg(0, doc="Project path 'group/name' or numeric id.")]
    ref: Annotated[
        str | None,
        Arg(
            "ref",
            default=None,
            doc="Git ref whose history to list, e.g. 'main' or 'v1.2.0'; NULL means the default branch.",
        ),
    ] = None
    since: Annotated[
        str | None, Arg("since", default=None, doc="Only commits at or after this ISO 8601 instant, e.g. '2026-01-01'.")
    ] = None
    until: Annotated[str | None, Arg("until", default=None, doc="Only commits at or before this ISO 8601 instant.")] = (
        None
    )
    path: Annotated[str | None, Arg("path", default=None, doc="Only commits touching this file or directory.")] = None
    all_refs: Annotated[bool, Arg("all_refs", default=False, doc="List commits reachable from every ref.")] = False
    with_stats: Annotated[
        bool, Arg("with_stats", default=False, doc="Fill additions/deletions (GitLab computes them; slower).")
    ] = False


class Commits(ApiScan[CommitsArgs]):
    """``commits(project)``: commit history."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = COMMIT_SCHEMA

    class Meta:
        """Function metadata."""

        name = "commits"
        description = "Commit history of a GitLab project, newest first"
        filter_pushdown = True
        auto_apply_filters = True
        projection_pushdown = True
        tags = function_tags(
            COMMIT_SCHEMA,
            title="Commits",
            category="repository",
            keywords="commits, git history, log, authors, changes, churn",
            doc_llm=(
                "commits(project, ref := NULL, since := NULL, until := NULL, path := NULL, all_refs := false, "
                "with_stats := false) lists commits newest first. Columns mirror vgi-github commits(): repo, sha, "
                "short_sha, title, message, author_name, author_email, author_date, committer_name, committer_email, "
                "committer_date, parent_shas, additions, deletions (with_stats only), html_url. A committer_date "
                "range in WHERE becomes since/until, and author_name = becomes the author filter."
            ),
            doc_md=(
                "## commits(project, ref := NULL, since := NULL, until := NULL, path := NULL)\n\n"
                "Commit history of a branch, tag or path. `committer_date` ranges are sent to GitLab."
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT author_name, count(*) AS commits FROM gitlab.main.commits('gitlab-org/cli') "
                    "WHERE committer_date >= TIMESTAMPTZ '2026-10-01' GROUP BY author_name ORDER BY commits DESC "
                    "LIMIT 10"
                ),
                description="Most active authors since October",
            ),
            FunctionExample(
                sql=(
                    "SELECT sha, title, additions + deletions AS churn FROM gitlab.main.commits('gitlab-org/cli', "
                    "path := 'README.md', with_stats := true) ORDER BY committer_date DESC LIMIT 5"
                ),
                description="Recent changes to one file and their size",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """``/repository/commits`` with ref/since/until/path/author."""
        a: CommitsArgs = ctx.args
        q: list[tuple[str, Any]] = []
        if a.ref:
            q.append(("ref_name", a.ref))
        lo, hi = ctx.pd.bounds("committer_date")
        since = a.since or iso(lo)
        until = a.until or iso(hi)
        if since:
            q.append(("since", since))
        if until:
            q.append(("until", until))
        if a.path:
            q.append(("path", a.path))
        author = ctx.pd.one("author_name")
        if author:
            q.append(("author", author))
        if a.all_refs:
            q.append(("all", True))
        if a.with_stats:
            q.append(("with_stats", True))
        return [
            Listing(
                paths=(f"projects/{encode_id(a.project)}/repository/commits",),
                query=tuple(q),
                context=(("repo", a.project),),
            )
        ]

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map commit objects."""
        return [
            {
                "repo": listing.ctx("repo"),
                "sha": c.get("id"),
                "short_sha": c.get("short_id"),
                "title": c.get("title"),
                "message": c.get("message"),
                "author_name": c.get("author_name"),
                "author_email": c.get("author_email"),
                "author_date": c.get("authored_date"),
                "committer_name": c.get("committer_name"),
                "committer_email": c.get("committer_email"),
                "committer_date": c.get("committed_date"),
                "parent_shas": c.get("parent_ids"),
                "additions": get(c, "stats", "additions"),
                "deletions": get(c, "stats", "deletions"),
                "html_url": c.get("web_url"),
            }
            for c in items
        ]


# ------------------------------------------------------------ branches & tags

BRANCH_SCHEMA = pa.schema(
    [
        column("repo", pa.string(), "Project path."),
        column("name", pa.string(), "Branch name. `name =` is pushed down.", nullable=False),
        column("commit_sha", pa.string(), "Commit at the tip of the branch."),
        column("commit_title", pa.string(), "Title of that commit."),
        column("commit_date", TSTZ, "When that commit was committed."),
        column("is_default", pa.bool_(), "Whether this is the default branch."),
        column("is_protected", pa.bool_(), "Whether the branch is protected."),
        column("is_merged", pa.bool_(), "Whether the branch is merged into the default branch."),
        column("developers_can_push", pa.bool_(), "Whether developers may push to it."),
        column("html_url", pa.string(), "Link to the branch in the GitLab UI."),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class SearchArgs:
    """Arguments for ``branches`` / ``tags(project, search := NULL)``."""

    project: Annotated[str, Arg(0, doc="Project path 'group/name' or numeric id.")]
    search: Annotated[
        str | None,
        Arg("search", default=None, doc="Only names containing this text; '^x' means starts with, 'x$' ends with."),
    ] = None


def _name_search(ctx: ScanContext, search: str | None) -> list[tuple[str, Any]]:
    name = ctx.pd.one("name")
    if name:
        return [("search", f"^{name}$")]
    return [("search", search)] if search else []


class Branches(ApiScan[SearchArgs]):
    """``branches(project)``."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = BRANCH_SCHEMA

    class Meta:
        """Function metadata."""

        name = "branches"
        description = "Branches of a GitLab project with their tip commit and protection state"
        filter_pushdown = True
        auto_apply_filters = True
        projection_pushdown = True
        tags = function_tags(
            BRANCH_SCHEMA,
            title="Branches",
            category="repository",
            keywords="branches, protected branches, stale branches, merged branches, git refs",
            doc_llm=(
                "branches(project, search := NULL) lists branches: repo, name, commit_sha, commit_title, commit_date, "
                "is_default, is_protected, is_merged, developers_can_push, html_url. `name =` becomes an anchored "
                "search. Find stale branches by commit_date."
            ),
            doc_md="## branches(project, search := NULL)\n\nBranches and their tip commits.",
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT name, commit_date FROM gitlab.main.branches('gitlab-org/cli') "
                    "WHERE is_merged AND NOT is_protected ORDER BY commit_date LIMIT 20"
                ),
                description="Merged branches that could be deleted",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """``/repository/branches``."""
        a: SearchArgs = ctx.args
        return [
            Listing(
                paths=(f"projects/{encode_id(a.project)}/repository/branches",),
                query=tuple(_name_search(ctx, a.search)),
                context=(("repo", a.project),),
            )
        ]

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map branch objects."""
        return [
            {
                "repo": listing.ctx("repo"),
                "name": b.get("name"),
                "commit_sha": get(b, "commit", "id"),
                "commit_title": get(b, "commit", "title"),
                "commit_date": get(b, "commit", "committed_date"),
                "is_default": b.get("default"),
                "is_protected": b.get("protected"),
                "is_merged": b.get("merged"),
                "developers_can_push": b.get("developers_can_push"),
                "html_url": b.get("web_url"),
            }
            for b in items
        ]


TAG_SCHEMA = pa.schema(
    [
        column("repo", pa.string(), "Project path."),
        column("name", pa.string(), "Tag name, e.g. 'v1.2.0'. `name =` is pushed down.", nullable=False),
        column("message", pa.string(), "Annotation message (annotated tags only)."),
        column("target", pa.string(), "Object the tag points at (the tag object for annotated tags)."),
        column("commit_sha", pa.string(), "The tagged commit."),
        column("commit_date", TSTZ, "When the tagged commit was committed."),
        column("is_protected", pa.bool_(), "Whether the tag is protected."),
        column("has_release", pa.bool_(), "Whether a release is attached to the tag."),
        column("created_at", TSTZ, "When an annotated tag was created (NULL for lightweight tags)."),
    ]
)


class Tags(ApiScan[SearchArgs]):
    """``tags(project)``."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = TAG_SCHEMA

    class Meta:
        """Function metadata."""

        name = "tags"
        description = "Git tags of a GitLab project, newest first"
        filter_pushdown = True
        auto_apply_filters = True
        projection_pushdown = True
        tags = function_tags(
            TAG_SCHEMA,
            title="Tags",
            category="repository",
            keywords="tags, git tags, versions, release tags",
            doc_llm=(
                "tags(project, search := NULL) lists git tags newest first: repo, name, message, target, commit_sha, "
                "commit_date, is_protected, has_release, created_at. `name =` becomes an anchored search."
            ),
            doc_md="## tags(project, search := NULL)\n\nGit tags of a project.",
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT name, commit_date FROM gitlab.main.tags('gitlab-org/cli', search := '^v1.') "
                    "WHERE NOT has_release ORDER BY commit_date DESC"
                ),
                description="v1 tags without a release",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """``/repository/tags``."""
        a: SearchArgs = ctx.args
        return [
            Listing(
                paths=(f"projects/{encode_id(a.project)}/repository/tags",),
                query=tuple(_name_search(ctx, a.search)),
                context=(("repo", a.project),),
            )
        ]

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map tag objects."""
        return [
            {
                "repo": listing.ctx("repo"),
                "name": t.get("name"),
                "message": t.get("message") or None,
                "target": t.get("target"),
                "commit_sha": get(t, "commit", "id"),
                "commit_date": get(t, "commit", "committed_date"),
                "is_protected": t.get("protected"),
                "has_release": t.get("release") is not None,
                "created_at": t.get("created_at"),
            }
            for t in items
        ]


# ------------------------------------------------------------------- releases

RELEASE_SCHEMA = pa.schema(
    [
        column("repo", pa.string(), "Project path."),
        column("tag_name", pa.string(), "Tag the release points at, e.g. 'v1.2.0'.", nullable=False),
        column("name", pa.string(), "Release title."),
        column("is_upcoming", pa.bool_(), "Whether released_at is in the future (an upcoming release)."),
        column("author_login", pa.string(), "Username of the account that created the release."),
        column("commit_sha", pa.string(), "The tagged commit."),
        column("assets_count", pa.int64(), "Number of assets (links and source archives)."),
        column("milestones", LIST, "Titles of the milestones associated with the release."),
        column("body", pa.string(), "Release notes as Markdown."),
        column("html_url", pa.string(), "Link to the release in the GitLab UI."),
        column("created_at", TSTZ, "When the release was created."),
        column("published_at", TSTZ, "When the release is or was released (GitLab released_at)."),
    ]
)


class Releases(ApiScan[ProjectOnlyArgs]):
    """``releases(project)``."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = RELEASE_SCHEMA

    class Meta:
        """Function metadata."""

        name = "releases"
        description = "Releases of a GitLab project, newest first"
        projection_pushdown = True
        tags = function_tags(
            RELEASE_SCHEMA,
            title="Releases",
            category="repository",
            keywords="releases, versions, changelog, release notes, shipping cadence",
            doc_llm=(
                "releases(project) lists releases newest first. Columns mirror vgi-github releases(): repo, tag_name, "
                "name, is_upcoming, author_login, commit_sha, assets_count, milestones, body, html_url, created_at, "
                "published_at (GitLab's released_at)."
            ),
            doc_md="## releases(project)\n\nReleases and their notes.",
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT tag_name, published_at FROM gitlab.main.releases('gitlab-org/cli') "
                    "WHERE NOT is_upcoming ORDER BY published_at DESC LIMIT 10"
                ),
                description="The ten latest releases",
            ),
            FunctionExample(
                sql=(
                    "SELECT date_trunc('quarter', published_at) AS quarter, count(*) AS releases "
                    "FROM gitlab.main.releases('gitlab-org/cli') GROUP BY ALL ORDER BY quarter"
                ),
                description="Release cadence per quarter",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """``/projects/:id/releases``."""
        a: ProjectOnlyArgs = ctx.args
        return [Listing(paths=(f"projects/{encode_id(a.project)}/releases",), context=(("repo", a.project),))]

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map release objects."""
        return [
            {
                "repo": listing.ctx("repo"),
                "tag_name": r.get("tag_name"),
                "name": r.get("name"),
                "is_upcoming": r.get("upcoming_release"),
                "author_login": get(r, "author", "username"),
                "commit_sha": get(r, "commit", "id"),
                "assets_count": get(r, "assets", "count"),
                "milestones": names(r.get("milestones"), "title"),
                "body": r.get("description"),
                "html_url": get(r, "_links", "self"),
                "created_at": r.get("created_at"),
                "published_at": r.get("released_at"),
            }
            for r in items
        ]


# --------------------------------------------------------------- contributors

CONTRIBUTOR_SCHEMA = pa.schema(
    [
        column("repo", pa.string(), "Project path."),
        column("name", pa.string(), "Contributor name from the git metadata."),
        column("email", pa.string(), "Contributor email from the git metadata."),
        column("contributions", pa.int64(), "Commits to the default branch by this name/email."),
        column("additions", pa.int64(), "Lines added (GitLab reports 0 on recent versions)."),
        column("deletions", pa.int64(), "Lines removed (GitLab reports 0 on recent versions)."),
    ]
)


class Contributors(ApiScan[ProjectOnlyArgs]):
    """``contributors(project)``."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = CONTRIBUTOR_SCHEMA

    class Meta:
        """Function metadata."""

        name = "contributors"
        description = "Contributors to a GitLab project's default branch, ranked by commits"
        projection_pushdown = True
        tags = function_tags(
            CONTRIBUTOR_SCHEMA,
            title="Contributors",
            category="repository",
            keywords="contributors, authors, committers, commit counts, community",
            doc_llm=(
                "contributors(project) ranks contributors by commits to the default branch: repo, name, email, "
                "contributions (commit count, the vgi-github column name), additions, deletions. GitLab groups by "
                "git name/email, not by account, so there is no login."
            ),
            doc_md="## contributors(project)\n\nContributors ranked by commits.",
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT name, contributions FROM gitlab.main.contributors('gitlab-org/cli') "
                    "ORDER BY contributions DESC LIMIT 10"
                ),
                description="Top ten contributors",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """``/repository/contributors`` ordered by commits."""
        a: ProjectOnlyArgs = ctx.args
        return [
            Listing(
                paths=(f"projects/{encode_id(a.project)}/repository/contributors",),
                query=(("order_by", "commits"), ("sort", "desc")),
                context=(("repo", a.project),),
            )
        ]

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map contributor objects."""
        return [
            {
                "repo": listing.ctx("repo"),
                "name": c.get("name"),
                "email": c.get("email"),
                "contributions": c.get("commits"),
                "additions": c.get("additions"),
                "deletions": c.get("deletions"),
            }
            for c in items
        ]
