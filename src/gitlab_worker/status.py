"""``unified.unified_status(raw_status[, kind])``: the unified status mapping as a scalar.

The same mapping ``unified.runs`` / ``unified.task_runs`` apply, with no API
calls, so GitLab statuses already in a table (from ``main.pipelines`` or
``main.jobs``, or exported data) can be compared with other orchestrators.
"""

from __future__ import annotations

import json
from typing import Annotated

import pyarrow as pa
from vgi.arguments import ConstParam, Param, Returns
from vgi.metadata import FunctionExample
from vgi.scalar_function import ScalarFunction

from .meta import object_tags
from .unified import JOB_STATUS, PIPELINE_STATUS

KINDS = ("pipeline", "job")
_RAW_DOC = "A GitLab pipeline or job status, e.g. 'canceled' or 'manual'; NULL in, NULL out."
_KIND_DOC = "Which mapping to use: pipeline statuses or job statuses (they differ only for 'manual')."


def _map(raw: pa.StringArray, mapping: dict[str, str]) -> pa.StringArray:
    """Map every status; unknown non-NULL values become 'queued', the way the unified tables do."""
    out = pa.array(
        [None if v is None else mapping.get(v, "queued") for v in raw.to_pylist()],
        type=pa.string(),
    )
    return out


_EXEC = json.dumps(
    [
        {
            "description": "Normalize GitLab pipeline statuses",
            "sql": (
                "SELECT s AS raw_status, gitlab.unified.unified_status(s) AS status FROM (VALUES "
                "('canceled'), ('failed'), ('manual'), ('pending'), ('success')) t(s) ORDER BY s"
            ),
            "expected_result": [
                {"raw_status": "canceled", "status": "cancelled"},
                {"raw_status": "failed", "status": "failed"},
                {"raw_status": "manual", "status": "queued"},
                {"raw_status": "pending", "status": "queued"},
                {"raw_status": "success", "status": "success"},
            ],
        },
        {
            "description": "A manual job nobody started counts as skipped",
            "sql": "SELECT gitlab.unified.unified_status('manual', 'job') AS status",
            "expected_result": [{"status": "skipped"}],
        },
    ]
)


class UnifiedStatus(ScalarFunction):
    """``unified_status(raw_status)``: a GitLab pipeline status in the unified vocabulary."""

    class Meta:
        """Function metadata."""

        name = "unified_status"
        description = "Map a GitLab pipeline status onto queued/running/success/failed/cancelled/skipped"
        tags = {
            **object_tags(
                title="Unified Pipeline Status",
                category="runs",
                keywords="status mapping, normalize status, unified status, pipeline status, canceled",
                doc_llm=(
                    "unified_status(raw_status) maps a GitLab pipeline status to the cross-orchestrator "
                    "vocabulary used by unified.runs: created/waiting_for_resource/preparing/pending/scheduled/"
                    "manual -> queued, running/canceling -> running, success -> success, failed -> failed, "
                    "canceled -> cancelled, skipped -> skipped. No API call. Use unified_status(raw, 'job') for "
                    "job statuses (manual -> skipped)."
                ),
                doc_md="## unified_status(raw_status)\n\nGitLab pipeline status in the unified vocabulary.",
            ),
            "domain": "devops",
            "vgi.executable_examples": _EXEC,
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT gitlab.unified.unified_status(status) AS status, count(*) AS pipelines "
                    "FROM gitlab.main.pipelines('gitlab-org/cli') GROUP BY ALL ORDER BY pipelines DESC"
                ),
                description="Pipelines of a project by unified status",
            ),
        ]

    @classmethod
    def compute(
        cls, raw_status: Annotated[pa.StringArray, Param(doc=_RAW_DOC)]
    ) -> Annotated[pa.StringArray, Returns()]:
        """Map pipeline statuses."""
        return _map(raw_status, PIPELINE_STATUS)


class UnifiedStatusKind(ScalarFunction):
    """``unified_status(raw_status, kind)``: pipeline or job status in the unified vocabulary."""

    class Meta:
        """Function metadata."""

        name = "unified_status"
        description = "Map a GitLab pipeline or job status onto the unified run status vocabulary"
        tags = {
            **object_tags(
                title="Unified Pipeline or Job Status",
                category="runs",
                keywords="status mapping, job status, unified status, task status, manual job",
                doc_llm=(
                    "unified_status(raw_status, kind) maps a GitLab status with the mapping for kind 'pipeline' "
                    "(as unified.runs) or 'job' (as unified.task_runs). The two differ only for 'manual': a "
                    "pipeline waiting on a manual job is queued, a manual job nobody started is skipped."
                ),
                doc_md="## unified_status(raw_status, kind)\n\nPipeline or job status in the unified vocabulary.",
            ),
            "domain": "devops",
        }
        examples = [
            FunctionExample(
                sql=(
                    "SELECT name, gitlab.unified.unified_status(status, 'job') AS status "
                    "FROM gitlab.main.jobs('gitlab-org/gitlab-runner') ORDER BY id DESC LIMIT 20"
                ),
                description="Recent jobs with unified statuses",
            ),
        ]

    @classmethod
    def compute(
        cls,
        raw_status: Annotated[pa.StringArray, Param(doc=_RAW_DOC)],
        kind: Annotated[str, ConstParam(_KIND_DOC, choices=list(KINDS))],
    ) -> Annotated[pa.StringArray, Returns()]:
        """Map with the chosen mapping."""
        return _map(raw_status, JOB_STATUS if kind == "job" else PIPELINE_STATUS)
