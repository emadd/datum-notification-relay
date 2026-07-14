from datetime import datetime, timezone

import pytest

from relay.models import (
    Job,
    JobKind,
    Metric,
    Schedule,
    ScheduleType,
    TargetKind,
    ValidationError,
)


def make_fetch_job(**overrides) -> Job:
    defaults = dict(
        id="job-1",
        kind=JobKind.REMOTE_FETCH,
        schedule=Schedule(type=ScheduleType.HOURLY),
        support_id="DTM-AAAA-BBBB-CCCC",
        device_token="deadbeef",
        endpoint_url="https://example.com/data.json",
        extraction_path="value",
    )
    defaults.update(overrides)
    return Job(**defaults)


def make_reminder_job(**overrides) -> Job:
    defaults = dict(
        id="job-2",
        kind=JobKind.REMINDER_AUTO_LOG,
        schedule=Schedule(type=ScheduleType.DAILY_AT_HOUR, hour=9),
        support_id="DTM-AAAA-BBBB-CCCC",
        device_token="deadbeef",
        target_kind=TargetKind.TRACKER,
        target_id="cat-1",
        metric=Metric.INCREMENT,
    )
    defaults.update(overrides)
    return Job(**defaults)


class TestScheduleValidation:
    def test_once_requires_at(self):
        with pytest.raises(ValidationError):
            Schedule(type=ScheduleType.ONCE).validate()

    def test_once_with_at_is_valid(self):
        Schedule(type=ScheduleType.ONCE, at=datetime.now(timezone.utc)).validate()

    def test_hourly_is_valid_with_no_extra_fields(self):
        Schedule(type=ScheduleType.HOURLY).validate()

    def test_every_n_hours_requires_positive_interval(self):
        with pytest.raises(ValidationError):
            Schedule(type=ScheduleType.EVERY_N_HOURS, interval_hours=0).validate()
        with pytest.raises(ValidationError):
            Schedule(type=ScheduleType.EVERY_N_HOURS).validate()

    def test_every_n_hours_valid(self):
        Schedule(type=ScheduleType.EVERY_N_HOURS, interval_hours=6).validate()

    @pytest.mark.parametrize("hour", [-1, 24, None])
    def test_daily_at_hour_requires_valid_hour(self, hour):
        with pytest.raises(ValidationError):
            Schedule(type=ScheduleType.DAILY_AT_HOUR, hour=hour).validate()

    def test_daily_at_hour_valid(self):
        Schedule(type=ScheduleType.DAILY_AT_HOUR, hour=0).validate()
        Schedule(type=ScheduleType.DAILY_AT_HOUR, hour=23).validate()


class TestScheduleRoundTrip:
    def test_once_round_trips(self):
        at = datetime(2026, 7, 15, 9, 30, tzinfo=timezone.utc)
        s = Schedule(type=ScheduleType.ONCE, at=at)
        d = s.to_dict()
        assert d == {"type": "once", "at": "2026-07-15T09:30:00Z"}
        assert Schedule.from_dict(d) == s

    def test_every_n_hours_round_trips(self):
        s = Schedule(type=ScheduleType.EVERY_N_HOURS, interval_hours=4)
        d = s.to_dict()
        assert d == {"type": "everyNHours", "intervalHours": 4}
        assert Schedule.from_dict(d) == s

    def test_daily_at_hour_round_trips(self):
        s = Schedule(type=ScheduleType.DAILY_AT_HOUR, hour=14)
        d = s.to_dict()
        assert d == {"type": "dailyAtHour", "hour": 14}
        assert Schedule.from_dict(d) == s

    def test_unknown_type_rejected(self):
        with pytest.raises(ValidationError):
            Schedule.from_dict({"type": "everyFortnight"})


class TestJobValidation:
    def test_remote_fetch_requires_endpoint_and_path(self):
        with pytest.raises(ValidationError):
            make_fetch_job(endpoint_url=None).validate()
        with pytest.raises(ValidationError):
            make_fetch_job(extraction_path=None).validate()

    def test_remote_fetch_rejects_non_https(self):
        with pytest.raises(ValidationError):
            make_fetch_job(endpoint_url="http://example.com/data.json").validate()

    def test_remote_fetch_valid(self):
        make_fetch_job().validate()

    def test_reminder_auto_log_requires_target_and_metric(self):
        with pytest.raises(ValidationError):
            make_reminder_job(target_kind=None).validate()
        with pytest.raises(ValidationError):
            make_reminder_job(target_id=None).validate()
        with pytest.raises(ValidationError):
            make_reminder_job(metric=None).validate()

    def test_reminder_auto_log_valid(self):
        make_reminder_job().validate()

    def test_requires_support_id_and_device_token(self):
        with pytest.raises(ValidationError):
            make_fetch_job(support_id="").validate()
        with pytest.raises(ValidationError):
            make_fetch_job(device_token="").validate()


class TestJobRoundTrip:
    def test_remote_fetch_round_trips(self):
        job = make_fetch_job()
        d = job.to_dict()
        restored = Job.from_dict(d)
        assert restored.kind == JobKind.REMOTE_FETCH
        assert restored.endpoint_url == job.endpoint_url
        assert restored.extraction_path == job.extraction_path
        assert restored.target_kind is None
        assert restored.metric is None

    def test_reminder_auto_log_round_trips(self):
        job = make_reminder_job()
        d = job.to_dict()
        restored = Job.from_dict(d)
        assert restored.kind == JobKind.REMINDER_AUTO_LOG
        assert restored.target_kind == TargetKind.TRACKER
        assert restored.target_id == "cat-1"
        assert restored.metric == Metric.INCREMENT
        assert restored.endpoint_url is None

    def test_from_dict_mints_id_when_missing(self):
        d = make_reminder_job().to_dict()
        del d["id"]
        restored = Job.from_dict(d)
        assert restored.id  # non-empty, minted

    def test_with_last_run_at_is_immutable(self):
        job = make_fetch_job()
        when = datetime(2026, 7, 15, tzinfo=timezone.utc)
        updated = job.with_last_run_at(when)
        assert job.last_run_at is None
        assert updated.last_run_at == when
