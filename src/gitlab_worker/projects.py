"""Groups, projects and members.

Column names follow vgi-github's ``repos`` / ``repo`` where the concepts match
(``full_name``, ``owner_login``, ``html_url``, ``stargazers_count``,
``forks_count``, ``is_archived``, ...), so the two combine with ``UNION ALL BY
NAME``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg, SecretLookupEntry
from vgi.metadata import FunctionExample

from .client import GITLAB_SECRET_TYPE, encode_id
from .convert import TSTZ, get
from .meta import column, function_tags
from .scan import ApiScan, Listing, Lookup, ScanContext, range_params

LIST = pa.list_(pa.string())

# --------------------------------------------------------------------- groups

GROUP_SCHEMA = pa.schema(
    [
        column("id", pa.int64(), "GitLab's numeric group id.", nullable=False),
        column(
            "full_path", pa.string(), "Path including parent groups, e.g. 'gitlab-org/ci-cd'; what projects() takes."
        ),
        column("path", pa.string(), "The group's own URL slug."),
        column("name", pa.string(), "Display name."),
        column("full_name", pa.string(), "Display name including parent groups, e.g. 'GitLab.org / CI-CD'."),
        column("description", pa.string(), "Group description."),
        column("visibility", pa.string(), "'public', 'internal' or 'private'."),
        column("parent_id", pa.int64(), "Id of the parent group; NULL for a top-level group."),
        column("html_url", pa.string(), "Link to the group in the GitLab UI."),
        column("created_at", TSTZ, "When the group was created."),
    ]
)


@dataclass(slots=True, frozen=True, kw_only=True)
class GroupsArgs:
    """Arguments for ``groups(parent := NULL, search := NULL, all_available := NULL)``."""

    parent: Annotated[
        str | None,
        Arg("parent", default=None, doc="List the descendant groups of this group path instead of all groups."),
    ] = None
    search: Annotated[
        str | None, Arg("search", default=None, doc="Only groups whose name or path contains this text.")
    ] = None
    all_available: Annotated[
        bool | None,
        Arg(
            "all_available",
            default=None,
            doc="true: every group you can see (default on self-managed for admins); false: only your memberships.",
        ),
    ] = None


class Groups(ApiScan[GroupsArgs]):
    """``groups()``: the groups you can see, or a group's descendants."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = GROUP_SCHEMA

    class Meta:
        """Function metadata."""

        name = "groups"
        description = "GitLab groups you can see, or the descendant groups of one group"
        filter_pushdown = True
        auto_apply_filters = True
        projection_pushdown = True
        tags = function_tags(
            GROUP_SCHEMA,
            title="Groups",
            category="projects",
            keywords="groups, subgroups, namespaces, organizations, teams",
            doc_llm=(
                "groups(parent := NULL, search := NULL, all_available := NULL) lists GitLab groups: id, full_path, "
                "path, name, full_name, description, visibility, parent_id, html_url, created_at. With `parent` it "
                "lists every descendant group of that group. `visibility =` and `parent_id IS NULL`-style top-level "
                "filters are sent to the API. Feed full_path to projects() to list a group's projects."
            ),
            doc_md=(
                "## groups(parent := NULL, search := NULL, all_available := NULL)\n\n"
                "Groups you can see (keyset-paginated), or with `parent` the descendant groups of one group."
            ),
        )
        examples = [
            FunctionExample(
                sql="SELECT full_path, visibility FROM gitlab.main.groups(parent := 'gitlab-org') ORDER BY full_path",
                description="Every subgroup of gitlab-org",
            ),
            FunctionExample(
                sql=(
                    "SELECT visibility, count(*) AS groups FROM gitlab.main.groups(search := 'data') "
                    "GROUP BY visibility ORDER BY groups DESC"
                ),
                description="Groups matching 'data', counted by visibility",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """List groups or descendant groups, with visibility / search / top-level pushdown."""
        a: GroupsArgs = ctx.args
        q: list[tuple[str, Any]] = []
        if a.search:
            q.append(("search", a.search))
        if a.all_available is not None:
            q.append(("all_available", a.all_available))
        vis = ctx.pd.one("visibility")
        if vis:
            q.append(("visibility", vis))
        if a.parent:
            return [Listing(paths=(f"groups/{encode_id(a.parent)}/descendant_groups",), query=tuple(q))]
        q += [("pagination", "keyset"), ("order_by", "name"), ("sort", "asc")]
        return [Listing(paths=("groups",), query=tuple(q))]

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map group objects."""
        return [{**g, "html_url": g.get("web_url")} for g in items]


# ------------------------------------------------------------------- projects

PROJECT_FIELDS = [
    column("full_name", pa.string(), "Project path 'group/subgroup/name' — the key every per-project function takes."),
    column("id", pa.int64(), "GitLab's numeric project id; survives renames and transfers.", nullable=False),
    column("owner_login", pa.string(), "Full path of the group or user namespace that owns the project."),
    column("owner_type", pa.string(), "'group' or 'user'."),
    column("name", pa.string(), "Project URL slug (the last path segment)."),
    column("display_name", pa.string(), "Project display name."),
    column("description", pa.string(), "Project description."),
    column("html_url", pa.string(), "Link to the project in the GitLab UI."),
    column("topics", LIST, "Topics applied to the project."),
    column("visibility", pa.string(), "'public', 'internal' or 'private'. Pushed down."),
    column("is_private", pa.bool_(), "Whether visibility is 'private'."),
    column("is_fork", pa.bool_(), "Whether the project is a fork."),
    column("forked_from", pa.string(), "Path of the project this one was forked from."),
    column("is_archived", pa.bool_(), "Whether the project is archived (read-only). Pushed down."),
    column("default_branch", pa.string(), "Default branch, e.g. 'main'."),
    column("stargazers_count", pa.int64(), "Number of stars."),
    column("forks_count", pa.int64(), "Number of forks."),
    column("open_issues_count", pa.int64(), "Open issues (NULL when issues are disabled or hidden from you)."),
    column("has_issues", pa.bool_(), "Whether the issue tracker is enabled."),
    column("has_wiki", pa.bool_(), "Whether the wiki is enabled."),
    column("has_merge_requests", pa.bool_(), "Whether merge requests are enabled."),
    column("is_empty_repo", pa.bool_(), "Whether the repository has no commits."),
    column("http_url_to_repo", pa.string(), "HTTPS clone URL."),
    column("created_at", TSTZ, "When the project was created."),
    column("updated_at", TSTZ, "When the project object last changed."),
    column(
        "last_activity_at",
        TSTZ,
        "Last activity (push, issue, merge request, ...); the activity signal. Ranges are pushed down.",
    ),
]
PROJECT_SCHEMA = pa.schema(PROJECT_FIELDS)
PROJECT_DETAIL_SCHEMA = pa.schema(
    [
        *PROJECT_FIELDS,
        column("license_key", pa.string(), "Detected license key, e.g. 'mit' or 'apache-2.0'."),
        column("license_name", pa.string(), "Detected license name."),
        column("commit_count", pa.int64(), "Commits on the default branch (needs Reporter access)."),
        column("repository_size_bytes", pa.int64(), "Repository size in bytes (needs Reporter access)."),
        column("storage_size_bytes", pa.int64(), "Total storage incl. LFS and artifacts (needs Reporter access)."),
    ]
)


def project_row(p: dict[str, Any]) -> dict[str, Any]:
    """One project object as a project row."""
    vis = p.get("visibility")
    forked = get(p, "forked_from_project", "path_with_namespace")
    return {
        "full_name": p.get("path_with_namespace"),
        "id": p.get("id"),
        "owner_login": get(p, "namespace", "full_path"),
        "owner_type": get(p, "namespace", "kind"),
        "name": p.get("path"),
        "display_name": p.get("name"),
        "description": p.get("description"),
        "html_url": p.get("web_url"),
        "topics": p.get("topics") or p.get("tag_list"),
        "visibility": vis,
        "is_private": None if vis is None else vis == "private",
        "is_fork": "forked_from_project" in p,
        "forked_from": forked,
        "is_archived": p.get("archived"),
        "default_branch": p.get("default_branch"),
        "stargazers_count": p.get("star_count"),
        "forks_count": p.get("forks_count"),
        "open_issues_count": p.get("open_issues_count"),
        "has_issues": p.get("issues_enabled"),
        "has_wiki": p.get("wiki_enabled"),
        "has_merge_requests": p.get("merge_requests_enabled"),
        "is_empty_repo": p.get("empty_repo"),
        "http_url_to_repo": p.get("http_url_to_repo"),
        "created_at": p.get("created_at"),
        "updated_at": p.get("updated_at"),
        "last_activity_at": p.get("last_activity_at"),
        "license_key": get(p, "license", "key"),
        "license_name": get(p, "license", "name"),
        "commit_count": get(p, "statistics", "commit_count"),
        "repository_size_bytes": get(p, "statistics", "repository_size"),
        "storage_size_bytes": get(p, "statistics", "storage_size"),
    }


@dataclass(slots=True, frozen=True, kw_only=True)
class ProjectsArgs:
    """Arguments for ``projects(namespace, ...)``; a NULL namespace lists your memberships."""

    namespace: Annotated[
        str | None,
        Arg(0, doc="Group path (e.g. 'gitlab-org') or username whose projects to list; NULL lists your memberships."),
    ]
    include_subgroups: Annotated[
        bool, Arg("include_subgroups", default=True, doc="For a group, also list projects in its subgroups.")
    ] = True
    search: Annotated[
        str | None, Arg("search", default=None, doc="Only projects whose name or path contains this text.")
    ] = None
    membership: Annotated[
        bool,
        Arg(
            "membership",
            default=True,
            doc="Without a namespace: true lists projects you are a member of; false lists every project you can "
            "see (all of gitlab.com — combine with search or a LIMIT).",
        ),
    ] = True


class Projects(ApiScan[ProjectsArgs]):
    """``projects(namespace)``: a group's or user's projects, or your memberships."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = PROJECT_SCHEMA

    class Meta:
        """Function metadata."""

        name = "projects"
        description = "Projects in a GitLab group or user namespace, or the projects you are a member of"
        filter_pushdown = True
        auto_apply_filters = True
        projection_pushdown = True
        tags = function_tags(
            PROJECT_SCHEMA,
            title="Projects",
            category="projects",
            keywords="projects, repositories, repos, group projects, namespace, archived, visibility",
            doc_llm=(
                "projects(namespace, include_subgroups := true, search := NULL, membership := true) lists "
                "projects (GitLab's repositories). namespace is a group path or username (positional); NULL lists "
                "the projects you are a member of (pass NULL). Columns follow vgi-github repos(): full_name "
                "('group/name'), id, "
                "owner_login, owner_type, name, display_name, description, html_url, topics, visibility, is_private, "
                "is_fork, forked_from, is_archived, default_branch, stargazers_count, forks_count, open_issues_count, "
                "has_issues, has_wiki, has_merge_requests, is_empty_repo, http_url_to_repo, created_at, updated_at, "
                "last_activity_at. Pushed down: visibility =, is_archived =, last_activity_at ranges."
            ),
            doc_md=(
                "## projects(namespace, include_subgroups := true, search := NULL, membership := true)\n\n"
                "Projects of a group (with subgroups) or user; `projects(NULL)` lists your memberships. "
                "`visibility`, `is_archived` and `last_activity_at` filters are sent to the API."
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT full_name, stargazers_count, last_activity_at FROM gitlab.main.projects('gitlab-org') "
                    "WHERE NOT is_archived AND last_activity_at >= TIMESTAMPTZ '2026-10-01' "
                    "ORDER BY stargazers_count DESC LIMIT 10"
                ),
                description="The most-starred active projects of gitlab-org",
            ),
            FunctionExample(
                sql=(
                    "SELECT visibility, count(*) AS projects FROM gitlab.main.projects(NULL) "
                    "GROUP BY visibility ORDER BY projects DESC"
                ),
                description="Your projects counted by visibility",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """Group projects, else user projects, else /projects; with pushdown."""
        a: ProjectsArgs = ctx.args
        q: list[tuple[str, Any]] = []
        if a.search:
            q.append(("search", a.search))
        vis = ctx.pd.one("visibility")
        if vis:
            q.append(("visibility", vis))
        archived = ctx.pd.one("is_archived")
        if isinstance(archived, bool):
            q.append(("archived", archived))
        q += range_params(ctx.pd, "last_activity_at", "last_activity_after", "last_activity_before")
        if a.namespace:
            ns = encode_id(a.namespace)
            group_q = [*q, ("include_subgroups", a.include_subgroups)]
            return [
                Listing(paths=(f"groups/{ns}/projects",), query=tuple(group_q), missing_ok=True),
                Listing(paths=(f"users/{ns}/projects",), query=tuple(q), missing_ok=True),
            ]
        q += [("membership", a.membership), ("pagination", "keyset"), ("order_by", "id"), ("sort", "asc")]
        return [Listing(paths=("projects",), query=tuple(q))]

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map project objects."""
        return [project_row(p) for p in items]


@dataclass(slots=True, frozen=True, kw_only=True)
class ProjectArgs:
    """Arguments for ``project(path)``."""

    path: Annotated[str, Arg(0, doc="Project path 'group/name' or numeric id, e.g. 'gitlab-org/gitlab'.")]


class Project(Lookup[ProjectArgs]):
    """``project(path)``: one project with license and statistics."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = PROJECT_DETAIL_SCHEMA
    KEYS: ClassVar[tuple[str, ...]] = ("path",)

    class Meta:
        """Function metadata."""

        name = "project"
        description = "One GitLab project with its license and repository statistics"
        required_secrets = [SecretLookupEntry(secret_type=GITLAB_SECRET_TYPE)]
        projection_pushdown = True
        tags = function_tags(
            PROJECT_DETAIL_SCHEMA,
            title="Project",
            category="projects",
            keywords="project, repository, repo, license, statistics, stars, forks",
            doc_llm=(
                "project(path) returns one row for a project ('group/name' or numeric id): every projects() column "
                "plus license_key, license_name, commit_count, repository_size_bytes and storage_size_bytes "
                "(statistics need Reporter access and are NULL otherwise). Use LATERAL to look up several."
            ),
            doc_md="## project(path)\n\nOne project, like vgi-github's `repo()`, plus license and statistics.",
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT full_name, stargazers_count, forks_count, license_key "
                    "FROM gitlab.main.project('gitlab-org/gitlab-runner')"
                ),
                description="Stars, forks and license of one project",
            ),
            FunctionExample(
                sql=(
                    "SELECT p.full_name, p.stargazers_count FROM (VALUES ('gitlab-org/gitlab'), ('gitlab-org/cli')) "
                    "t(path), LATERAL gitlab.main.project(t.path) p ORDER BY p.stargazers_count DESC"
                ),
                description="Compare projects side by side",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """``GET /projects/:id?license=true&statistics=true``."""
        a: ProjectArgs = ctx.args
        return [
            Listing(
                paths=(f"projects/{encode_id(a.path)}",),
                query=(("license", True), ("statistics", True)),
                single=True,
            )
        ]

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map the project object."""
        return [project_row(p) for p in items]


# -------------------------------------------------------------------- members

MEMBER_SCHEMA = pa.schema(
    [
        column("namespace", pa.string(), "The project or group path the membership was listed for."),
        column("source", pa.string(), "'project' or 'group': which kind of namespace the path resolved to."),
        column("id", pa.int64(), "Numeric user id.", nullable=False),
        column("login", pa.string(), "Username."),
        column("name", pa.string(), "Display name."),
        column("state", pa.string(), "'active', 'blocked', 'awaiting', ..."),
        column("access_level", pa.int32(), "10 guest, 15 planner, 20 reporter, 30 developer, 40 maintainer, 50 owner."),
        column("access_level_name", pa.string(), "access_level as a word, e.g. 'developer'."),
        column("expires_at", pa.date32(), "When the membership expires, if set."),
        column("created_at", TSTZ, "When the membership was created."),
        column("html_url", pa.string(), "Link to the user's profile."),
    ]
)

ACCESS_LEVELS = {
    0: "none",
    5: "minimal",
    10: "guest",
    15: "planner",
    20: "reporter",
    30: "developer",
    40: "maintainer",
    50: "owner",
}


@dataclass(slots=True, frozen=True, kw_only=True)
class MembersArgs:
    """Arguments for ``members(namespace, inherited := true)``."""

    namespace: Annotated[str, Arg(0, doc="Project path ('group/name') or group path ('group').")]
    inherited: Annotated[
        bool, Arg("inherited", default=True, doc="Include members inherited from ancestor groups (/members/all).")
    ] = True


class Members(ApiScan[MembersArgs]):
    """``members(namespace)``: a project's or group's members."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = MEMBER_SCHEMA

    class Meta:
        """Function metadata."""

        name = "members"
        description = "Members of a GitLab project or group with their access levels"
        filter_pushdown = True
        auto_apply_filters = True
        projection_pushdown = True
        tags = function_tags(
            MEMBER_SCHEMA,
            title="Members",
            category="projects",
            keywords="members, users, access level, maintainers, owners, permissions, team",
            doc_llm=(
                "members(namespace, inherited := true) lists the members of a project (tried first) or group: "
                "namespace, source ('project'/'group'), id, login (username), name, state, access_level (10 guest .. "
                "50 owner), access_level_name, expires_at, created_at, html_url. inherited := false lists only "
                "direct members. A `login =` filter is sent to the API as a search."
            ),
            doc_md="## members(namespace, inherited := true)\n\nProject or group members and their roles.",
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT login, access_level_name FROM gitlab.main.members('gitlab-org/gitlab-runner') "
                    "WHERE access_level >= 40 ORDER BY login"
                ),
                description="Maintainers and owners of a project",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """Project members, else group members."""
        a: MembersArgs = ctx.args
        ns = encode_id(a.namespace)
        suffix = "members/all" if a.inherited else "members"
        q: list[tuple[str, Any]] = []
        login = ctx.pd.one("login")
        if login:
            q.append(("query", login))
        return [
            Listing(
                paths=(f"projects/{ns}/{suffix}", f"groups/{ns}/{suffix}"),
                query=tuple(q),
                context=(("namespace", a.namespace),),
            )
        ]

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Map member objects."""
        source = "group" if ctx.path.startswith("groups/") else "project"
        out = []
        for m in items:
            level = m.get("access_level")
            out.append(
                {
                    "namespace": listing.ctx("namespace"),
                    "source": source,
                    "id": m.get("id"),
                    "login": m.get("username"),
                    "name": m.get("name"),
                    "state": m.get("state"),
                    "access_level": level,
                    "access_level_name": ACCESS_LEVELS.get(level) if isinstance(level, int) else None,
                    "expires_at": m.get("expires_at"),
                    "created_at": m.get("created_at"),
                    "html_url": m.get("web_url"),
                }
            )
        return out
