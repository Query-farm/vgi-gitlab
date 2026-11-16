r"""Job logs: ``job_log`` (one row per line) and ``job_log_sections``.

GitLab serves a job's log (``GET /projects/:id/jobs/:job_id/trace``) as the raw
terminal stream the runner captured. This module turns it into clean rows:

* **ANSI escapes** (colours, ``ESC[0K`` erase-line) are removed.
* **Collapsible sections** — ``section_start:<epoch>:<name>[options]\r`` and
  ``section_end:<epoch>:<name>\r`` markers — are removed from the text and
  parsed: every line carries the innermost open ``section`` and the full
  ``section_path``, and ``job_log_sections`` lists each section with its start,
  end and duration.
* **Timestamps**: with the runner's timestamped log format (GitLab 17.1+,
  ``FF_TIMESTAMPS``) each line starts ``<RFC 3339 time> <stream><O|E>[+] ``. The
  time and the stream (stdout/stderr) become columns, and ``+`` continuation
  fragments are joined onto the line they continue.
* **Carriage returns** inside a line (progress bars) keep only what a terminal
  would show: the text after the last ``\r``.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar

import pyarrow as pa
from vgi.arguments import Arg, SecretLookupEntry
from vgi.metadata import FunctionExample

from .client import GITLAB_SECRET_TYPE, encode_id
from .convert import TSTZ, to_timestamp
from .meta import column, function_tags
from .scan import Listing, Lookup, ScanContext

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[=>]")
MARKER = re.compile(r"(?:\x1b\[0K)?section_(start|end):(\d+):([A-Za-z0-9_.\-]+)(\[[^\]\r\n]*\])?\r?(?:\x1b\[0K)?")
TIMESTAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z) ([0-9a-f]{2})([OE])(\+?) ?")


@dataclass(slots=True)
class LogLine:
    """One cleaned log line."""

    line_number: int
    timestamp: dt.datetime | None
    stream: str | None
    section: str | None
    section_path: list[str]
    message: str
    is_section_header: bool


@dataclass(slots=True)
class Section:
    """One collapsible section."""

    name: str
    parent: str | None
    depth: int
    started_at: dt.datetime | None
    ended_at: dt.datetime | None = None
    header: str | None = None
    first_line: int | None = None
    last_line: int | None = None
    is_collapsed: bool = False
    options: dict[str, str] = field(default_factory=dict)


def _epoch(value: str) -> dt.datetime | None:
    try:
        return dt.datetime.fromtimestamp(int(value), tz=dt.UTC)
    except ValueError, OverflowError, OSError:
        return None


def _visible(text: str) -> str:
    """Return what a terminal shows: drop ANSI escapes and keep the text after the last carriage return."""
    text = text.rstrip("\r")
    if "\r" in text:
        text = text.rsplit("\r", 1)[1]
    return ANSI.sub("", text)


def parse_trace(trace: str) -> tuple[list[LogLine], list[Section]]:
    """Parse a raw GitLab job trace into clean lines and sections.

    Args:
        trace: The ``/trace`` response body.

    Returns:
        The log lines (marker-only lines dropped) and the sections in start order.

    """
    lines: list[LogLine] = []
    sections: list[Section] = []
    stack: list[Section] = []
    for raw in trace.split("\n"):
        stamp: dt.datetime | None = None
        stream: str | None = None
        continuation = False
        m = TIMESTAMP.match(raw)
        if m:
            stamp = to_timestamp(m.group(1))
            stream = "stdout" if m.group(3) == "O" else "stderr"
            continuation = m.group(4) == "+"
            raw = raw[m.end() :]
        header = False
        pieces: list[str] = []
        pos = 0
        for mk in MARKER.finditer(raw):
            pieces.append(raw[pos : mk.start()])
            pos = mk.end()
            kind, epoch, name, opts = mk.groups()
            when = _epoch(epoch)
            if kind == "start":
                options: dict[str, str] = {}
                for kv in (opts or "").strip("[]").split(","):
                    if kv:
                        key, _, value = kv.partition("=")
                        options[key.strip()] = value.strip()
                sec = Section(
                    name=name,
                    parent=stack[-1].name if stack else None,
                    depth=len(stack),
                    started_at=when,
                    is_collapsed=options.get("collapsed") == "true",
                    options=options,
                )
                stack.append(sec)
                sections.append(sec)
                header = True
            else:
                for i in range(len(stack) - 1, -1, -1):
                    if stack[i].name == name:
                        for closed in stack[i:]:
                            closed.ended_at = closed.ended_at or when
                        del stack[i:]
                        break
        pieces.append(raw[pos:])
        text = _visible("".join(pieces))
        had_marker = pos > 0
        if continuation and lines:
            lines[-1].message += text
            continue
        if had_marker and not text.strip() and not header:
            continue
        if had_marker and header and stack and stack[-1].header is None:
            stack[-1].header = text or None
        line_number = len(lines) + 1
        for sec in stack:
            if sec.first_line is None:
                sec.first_line = line_number
            sec.last_line = line_number
        lines.append(
            LogLine(
                line_number=line_number,
                timestamp=stamp,
                stream=stream,
                section=stack[-1].name if stack else None,
                section_path=[s.name for s in stack],
                message=text,
                is_section_header=had_marker and header,
            )
        )
    # A trailing newline produces one empty final fragment; drop it.
    if lines and lines[-1].message == "" and not lines[-1].is_section_header and lines[-1].timestamp is None:
        lines.pop()
    return lines, sections


@dataclass(slots=True, frozen=True, kw_only=True)
class JobLogArgs:
    """Arguments for ``job_log`` / ``job_log_sections(project, job_id)``."""

    project: Annotated[str, Arg(0, doc="Project path 'group/name' or numeric id.")]
    job_id: Annotated[int, Arg(1, doc="Job id (the id column of jobs()).", ge=1)]


def _trace_listing(ctx: ScanContext) -> list[Listing]:
    a: JobLogArgs = ctx.args
    return [
        Listing(
            paths=(f"projects/{encode_id(a.project)}/jobs/{a.job_id}/trace",),
            text=True,
            paginate=False,
            context=(("repo", a.project), ("job_id", a.job_id)),
        )
    ]


JOB_LOG_SCHEMA = pa.schema(
    [
        column("repo", pa.string(), "Project path."),
        column("job_id", pa.int64(), "Job id.", nullable=False),
        column(
            "line_number", pa.int64(), "1-based line number after cleaning (marker-only lines removed).", nullable=False
        ),
        column("timestamp", TSTZ, "When the runner captured the line (timestamped logs, GitLab 17.1+); else NULL."),
        column("stream", pa.string(), "'stdout' or 'stderr' (timestamped logs only); else NULL."),
        column("section", pa.string(), "Innermost collapsible section, e.g. 'step_script' or 'prepare_executor'."),
        column("section_path", pa.list_(pa.string()), "Every open section, outermost first."),
        column("message", pa.string(), "The line's text without ANSI escapes or section markers."),
        column("is_section_header", pa.bool_(), "Whether the line is a section's header line."),
    ]
)


class JobLog(Lookup[JobLogArgs]):
    """``job_log(project, job_id)``: a CI job's log, one row per line."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = JOB_LOG_SCHEMA
    KEYS: ClassVar[tuple[str, ...]] = ("project", "job_id")

    class Meta:
        """Function metadata."""

        name = "job_log"
        description = "A GitLab CI job's log as clean lines with timestamp, stream and section"
        required_secrets = [SecretLookupEntry(secret_type=GITLAB_SECRET_TYPE)]
        projection_pushdown = True
        tags = function_tags(
            JOB_LOG_SCHEMA,
            title="Job Log",
            category="ci",
            keywords="job log, trace, ci log, build log, errors, stack trace, debugging",
            doc_llm=(
                "job_log(project, job_id) reads a job's log and returns one row per line: repo, job_id, line_number, "
                "timestamp and stream (stdout/stderr; only for runners that write timestamped logs, else NULL), "
                "section (innermost collapsible section such as 'step_script', 'prepare_executor', "
                "'upload_artifacts_on_failure'), section_path, message (ANSI colours and section markers removed) and "
                "is_section_header. Filter message ILIKE '%error%' to find failures; the script output is in "
                "section 'step_script'."
            ),
            doc_md=(
                "## job_log(project, job_id)\n\n"
                "A CI job's log with ANSI colours and section markers removed, one row per line."
            ),
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT line_number, message FROM gitlab.main.job_log('gitlab-org/cli', 9876543) "
                    "WHERE message ILIKE '%error%' ORDER BY line_number"
                ),
                description="Error lines of a failed job",
            ),
            FunctionExample(
                sql=(
                    "SELECT j.name, l.message FROM gitlab.main.jobs('gitlab-org/cli', pipeline_id := 1234567) j, "
                    "LATERAL gitlab.main.job_log(j.repo, j.id) l WHERE j.status = 'failed' AND l.section = "
                    "'step_script' AND l.message LIKE '%FAIL%'"
                ),
                description="Failing test lines across a pipeline's failed jobs",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """``/projects/:id/jobs/:job_id/trace``."""
        return _trace_listing(ctx)

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Parse the trace into line rows."""
        lines, _ = parse_trace(str(items[0] or ""))
        repo, job = listing.ctx("repo"), listing.ctx("job_id")
        return [
            {
                "repo": repo,
                "job_id": job,
                "line_number": ln.line_number,
                "timestamp": ln.timestamp,
                "stream": ln.stream,
                "section": ln.section,
                "section_path": ln.section_path,
                "message": ln.message,
                "is_section_header": ln.is_section_header,
            }
            for ln in lines
        ]


SECTION_SCHEMA = pa.schema(
    [
        column("repo", pa.string(), "Project path."),
        column("job_id", pa.int64(), "Job id.", nullable=False),
        column("section", pa.string(), "Section name, e.g. 'step_script'.", nullable=False),
        column("parent", pa.string(), "Enclosing section, if nested."),
        column("depth", pa.int32(), "Nesting depth (0 = top level)."),
        column("header", pa.string(), "The section's header text, e.g. 'Executing \"step_script\" stage'."),
        column("started_at", TSTZ, "When the section started (from its marker)."),
        column("ended_at", TSTZ, "When it ended; NULL if the log stops inside it."),
        column("duration_seconds", pa.float64(), "ended_at - started_at in seconds (marker resolution: 1 s)."),
        column("first_line", pa.int64(), "First job_log line_number inside the section."),
        column("last_line", pa.int64(), "Last job_log line_number inside the section."),
        column("is_collapsed", pa.bool_(), "Whether the section is collapsed by default ([collapsed=true])."),
    ]
)


class JobLogSections(Lookup[JobLogArgs]):
    """``job_log_sections(project, job_id)``: the collapsible sections of a job's log."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = SECTION_SCHEMA
    KEYS: ClassVar[tuple[str, ...]] = ("project", "job_id")

    class Meta:
        """Function metadata."""

        name = "job_log_sections"
        description = "The collapsible sections of a GitLab CI job's log with their durations"
        required_secrets = [SecretLookupEntry(secret_type=GITLAB_SECRET_TYPE)]
        projection_pushdown = True
        tags = function_tags(
            SECTION_SCHEMA,
            title="Job Log Sections",
            category="ci",
            keywords="job log sections, step timing, ci profiling, slow steps, prepare executor",
            doc_llm=(
                "job_log_sections(project, job_id) lists the collapsible sections of a job's log in order: repo, "
                "job_id, section, parent, depth, header, started_at, ended_at, duration_seconds, first_line, "
                "last_line (job_log line numbers) and is_collapsed. Use it to see which step of a job (pulling the "
                "image, restoring cache, step_script, uploading artifacts) takes the time."
            ),
            doc_md="## job_log_sections(project, job_id)\n\nWhere a CI job spends its time, section by section.",
        )
        examples = [
            FunctionExample(
                sql=(
                    "SELECT section, duration_seconds FROM gitlab.main.job_log_sections('gitlab-org/cli', 9876543) "
                    "ORDER BY duration_seconds DESC"
                ),
                description="The slowest steps of a job",
            ),
        ]

    @classmethod
    def listings(cls, ctx: ScanContext) -> list[Listing]:
        """``/projects/:id/jobs/:job_id/trace``."""
        return _trace_listing(ctx)

    @classmethod
    def rows(cls, ctx: ScanContext, items: list[Any], listing: Listing) -> list[dict[str, Any]]:
        """Parse the trace into section rows."""
        _, sections = parse_trace(str(items[0] or ""))
        repo, job = listing.ctx("repo"), listing.ctx("job_id")
        return [
            {
                "repo": repo,
                "job_id": job,
                "section": s.name,
                "parent": s.parent,
                "depth": s.depth,
                "header": s.header,
                "started_at": s.started_at,
                "ended_at": s.ended_at,
                "duration_seconds": (s.ended_at - s.started_at).total_seconds()
                if s.started_at and s.ended_at
                else None,
                "first_line": s.first_line,
                "last_line": s.last_line,
                "is_collapsed": s.is_collapsed,
            }
            for s in sections
        ]
