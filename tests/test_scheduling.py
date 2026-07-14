from datetime import datetime, timedelta, timezone

import pytest

from relay.models import Job, JobKind, Metric, Schedule, ScheduleType, TargetKind
from relay.scheduling import (
    is_due,
    job_is_due,
    job_is_exhausted,
    job_next_due_at,
    next_due_at,
)

UTC = timezone.utc


def dt(*args, **kwargs) -> datetime:
    return datetime(*args, tzinfo=UTC, **kwargs)


class TestOnceSchedule:
    def test_due_when_at_has_passed_and_never_run(self):
        schedule = Schedule(type=ScheduleType.ONCE, at=dt(2026, 1, 1, 9))
        assert is_due(
            schedule, anchor=dt(2025, 1, 1), last_run_at=None, now=dt(2026, 1, 1, 10)
        )

    def test_not_due_before_at(self):
        schedule = Schedule(type=ScheduleType.ONCE, at=dt(2026, 1, 1, 9))
        assert not is_due(
            schedule, anchor=dt(2025, 1, 1), last_run_at=None, now=dt(2026, 1, 1, 8)
        )

    def test_due_exactly_at_boundary(self):
        schedule = Schedule(type=ScheduleType.ONCE, at=dt(2026, 1, 1, 9))
        assert is_due(
            schedule, anchor=dt(2025, 1, 1), last_run_at=None, now=dt(2026, 1, 1, 9)
        )

    def test_never_due_again_once_run(self):
        schedule = Schedule(type=ScheduleType.ONCE, at=dt(2026, 1, 1, 9))
        assert next_due_at(schedule, anchor=dt(2025, 1, 1), last_run_at=dt(2026, 1, 1, 9)) is None
        assert not is_due(
            schedule,
            anchor=dt(2025, 1, 1),
            last_run_at=dt(2026, 1, 1, 9),
            now=dt(2027, 1, 1),
        )


class TestHourlySchedule:
    def test_due_immediately_relative_to_anchor_when_never_run(self):
        schedule = Schedule(type=ScheduleType.HOURLY)
        anchor = dt(2026, 1, 1, 9)
        # Due at anchor + 1h, not immediately at anchor itself.
        assert next_due_at(schedule, anchor=anchor, last_run_at=None) == anchor + timedelta(hours=1)

    def test_due_one_hour_after_last_run(self):
        schedule = Schedule(type=ScheduleType.HOURLY)
        last_run = dt(2026, 1, 1, 9)
        assert next_due_at(schedule, anchor=dt(2025, 1, 1), last_run_at=last_run) == dt(2026, 1, 1, 10)

    def test_not_due_before_the_hour_elapses(self):
        schedule = Schedule(type=ScheduleType.HOURLY)
        last_run = dt(2026, 1, 1, 9)
        assert not is_due(
            schedule, anchor=dt(2025, 1, 1), last_run_at=last_run, now=dt(2026, 1, 1, 9, 59)
        )
        assert is_due(
            schedule, anchor=dt(2025, 1, 1), last_run_at=last_run, now=dt(2026, 1, 1, 10, 0)
        )


class TestEveryNHoursSchedule:
    def test_due_after_n_hours(self):
        schedule = Schedule(type=ScheduleType.EVERY_N_HOURS, interval_hours=6)
        last_run = dt(2026, 1, 1, 0)
        assert next_due_at(schedule, anchor=dt(2025, 1, 1), last_run_at=last_run) == dt(2026, 1, 1, 6)

    def test_not_due_before_n_hours(self):
        schedule = Schedule(type=ScheduleType.EVERY_N_HOURS, interval_hours=6)
        last_run = dt(2026, 1, 1, 0)
        assert not is_due(
            schedule, anchor=dt(2025, 1, 1), last_run_at=last_run, now=dt(2026, 1, 1, 5, 59)
        )


class TestDailyAtHourSchedule:
    def test_first_fire_uses_anchor_minus_a_day_as_base(self):
        schedule = Schedule(type=ScheduleType.DAILY_AT_HOUR, hour=9)
        anchor = dt(2026, 1, 1, 12)  # created mid-day, after 09:00
        due = next_due_at(schedule, anchor=anchor, last_run_at=None)
        # base = anchor - 1 day = Dec 31 12:00; candidate starts at Dec 31 09:00,
        # which is <= base, so it rolls forward to Jan 1 09:00.
        assert due == dt(2026, 1, 1, 9)

    def test_next_occurrence_is_next_day_after_running(self):
        schedule = Schedule(type=ScheduleType.DAILY_AT_HOUR, hour=9)
        last_run = dt(2026, 1, 1, 9)
        assert next_due_at(schedule, anchor=dt(2025, 1, 1), last_run_at=last_run) == dt(2026, 1, 2, 9)

    def test_not_due_until_the_hour_that_day(self):
        schedule = Schedule(type=ScheduleType.DAILY_AT_HOUR, hour=9)
        last_run = dt(2026, 1, 1, 9)
        assert not is_due(
            schedule, anchor=dt(2025, 1, 1), last_run_at=last_run, now=dt(2026, 1, 2, 8, 59)
        )
        assert is_due(
            schedule, anchor=dt(2025, 1, 1), last_run_at=last_run, now=dt(2026, 1, 2, 9, 0)
        )

    def test_late_check_still_fires_once_not_repeatedly(self):
        # If the runner is late (e.g. checked at 11:00 for a 09:00 job), the
        # next candidate after firing should be the following day, not later
        # today again.
        schedule = Schedule(type=ScheduleType.DAILY_AT_HOUR, hour=9)
        ran_at = dt(2026, 1, 1, 11)  # ran late, at 11:00 instead of 09:00
        due = next_due_at(schedule, anchor=dt(2025, 1, 1), last_run_at=ran_at)
        assert due == dt(2026, 1, 2, 9)


def _fetch_job(schedule: Schedule, *, created_at: datetime, last_run_at=None) -> Job:
    return Job(
        id="j",
        kind=JobKind.REMOTE_FETCH,
        schedule=schedule,
        support_id="s",
        device_token="d",
        endpoint_url="https://example.com/x",
        extraction_path="v",
        created_at=created_at,
        last_run_at=last_run_at,
    )


def _reminder_job(schedule: Schedule, *, created_at: datetime, last_run_at=None) -> Job:
    return Job(
        id="j",
        kind=JobKind.REMINDER_AUTO_LOG,
        schedule=schedule,
        support_id="s",
        device_token="d",
        target_kind=TargetKind.TRACKER,
        target_id="t",
        metric=Metric.INCREMENT,
        created_at=created_at,
        last_run_at=last_run_at,
    )


class TestJobConvenienceWrappers:
    def test_job_is_due_uses_created_at_as_anchor(self):
        schedule = Schedule(type=ScheduleType.ONCE, at=dt(2026, 1, 1, 9))
        job = _fetch_job(schedule, created_at=dt(2025, 1, 1))
        assert job_is_due(job, now=dt(2026, 1, 1, 9, 1))
        assert not job_is_due(job, now=dt(2026, 1, 1, 8, 59))

    def test_job_next_due_at(self):
        schedule = Schedule(type=ScheduleType.EVERY_N_HOURS, interval_hours=2)
        job = _fetch_job(schedule, created_at=dt(2026, 1, 1, 0), last_run_at=dt(2026, 1, 1, 4))
        assert job_next_due_at(job) == dt(2026, 1, 1, 6)

    def test_job_is_exhausted_only_for_fired_once_jobs(self):
        once = Schedule(type=ScheduleType.ONCE, at=dt(2026, 1, 1))
        recurring = Schedule(type=ScheduleType.HOURLY)

        never_run = _reminder_job(once, created_at=dt(2025, 1, 1))
        assert not job_is_exhausted(never_run)

        fired = _reminder_job(once, created_at=dt(2025, 1, 1), last_run_at=dt(2026, 1, 1))
        assert job_is_exhausted(fired)

        recurring_job = _reminder_job(recurring, created_at=dt(2025, 1, 1), last_run_at=dt(2026, 1, 1))
        assert not job_is_exhausted(recurring_job)
