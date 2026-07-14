"""Pure due-job scheduling math. No AWS SDK, no I/O, no wall-clock reads unless
the caller passes one in — every function here takes `now` explicitly so it is
trivially unit-testable and deterministic.

Semantics mirror the Datum app's `RemoteFetchSchedule` conceptually (see
NOTIFICATION-SERVER-INFRA.md §3), without importing any app code:

- ``once``: due exactly once, at ``schedule.at``, and never again afterward.
- ``hourly``: due every hour, anchored to the job's first run (or creation, if
  it has never run).
- ``everyNHours``: due every ``interval_hours`` hours, same anchoring.
- ``dailyAtHour``: due once per UTC calendar day, at ``schedule.hour``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import Job, Schedule, ScheduleType


def next_due_at(
    schedule: Schedule,
    *,
    anchor: datetime,
    last_run_at: Optional[datetime],
) -> Optional[datetime]:
    """Compute the next instant this schedule is due, or ``None`` if it will
    never fire again (a ``once`` schedule that has already run).

    ``anchor`` is a stable reference point for recurring schedules that have
    never run (typically the job's ``created_at``) — it decouples "when was
    this job created" from "when did the scheduler last look at it", so a
    freshly created hourly job is due immediately rather than waiting a full
    interval before its first fire.
    """
    if schedule.type == ScheduleType.ONCE:
        assert schedule.at is not None  # validated at construction
        return schedule.at if last_run_at is None else None

    if schedule.type == ScheduleType.HOURLY:
        interval = timedelta(hours=1)
        base = last_run_at or anchor
        return base + interval

    if schedule.type == ScheduleType.EVERY_N_HOURS:
        assert schedule.interval_hours is not None
        interval = timedelta(hours=schedule.interval_hours)
        base = last_run_at or anchor
        return base + interval

    if schedule.type == ScheduleType.DAILY_AT_HOUR:
        assert schedule.hour is not None
        base = last_run_at or (anchor - timedelta(days=1))
        candidate = base.replace(
            hour=schedule.hour, minute=0, second=0, microsecond=0
        )
        while candidate <= base:
            candidate += timedelta(days=1)
        return candidate

    raise ValueError(f"unknown schedule type {schedule.type!r}")  # pragma: no cover


def is_due(
    schedule: Schedule,
    *,
    anchor: datetime,
    last_run_at: Optional[datetime],
    now: datetime,
) -> bool:
    """True if the schedule's next fire instant is at or before ``now``."""
    due_at = next_due_at(schedule, anchor=anchor, last_run_at=last_run_at)
    return due_at is not None and due_at <= now


def job_is_due(job: Job, *, now: Optional[datetime] = None) -> bool:
    """Convenience wrapper: is this Job due right now?"""
    now = now or datetime.now(timezone.utc)
    return is_due(
        job.schedule, anchor=job.created_at, last_run_at=job.last_run_at, now=now
    )


def job_next_due_at(job: Job) -> Optional[datetime]:
    return next_due_at(
        job.schedule, anchor=job.created_at, last_run_at=job.last_run_at
    )


def job_is_exhausted(job: Job) -> bool:
    """True for a ``once`` job that has already fired — the runner (and the
    store's cleanup pass) can delete these rather than re-check them forever.
    """
    return job.schedule.type == ScheduleType.ONCE and job.last_run_at is not None
