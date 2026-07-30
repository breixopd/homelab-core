"""Typed, payload-free controller job history views."""

from __future__ import annotations

from toolkit.controller.contracts import JobRecord, JobState, job_can_cancel
from toolkit.controller.read_models import JobSummaryView, JobsView

_ATTENTION_STATES = frozenset({JobState.PARTIAL_FAILURE, JobState.FAILED, JobState.CANCELLED})


def read_jobs_view(jobs: list[JobRecord]) -> JobsView:
    summaries = [
        JobSummaryView(
            job_id=job.job_id,
            kind=job.request.kind,
            state=job.state,
            created_at=job.created_at,
            updated_at=job.updated_at,
            can_cancel=job_can_cancel(job.request.kind, job.state),
            error_code=job.error.code if job.error else "",
        )
        for job in jobs
    ]
    return JobsView(
        jobs=summaries,
        queued=sum(job.state is JobState.QUEUED for job in jobs),
        running=sum(job.state in {JobState.RUNNING, JobState.CANCEL_REQUESTED} for job in jobs),
        attention=sum(job.state in _ATTENTION_STATES for job in jobs),
        succeeded=sum(job.state is JobState.SUCCEEDED for job in jobs),
    )
