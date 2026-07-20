"""The Job model — the single data shape this whole relay exists to serve.

Mirrors the shape defined in the Datum app's infra plan doc (NOTIFICATION-SERVER-
INFRA.md §3) verbatim. This module is pure Python: no AWS SDK, no I/O. It only
knows how to validate itself and convert to/from plain dicts (which the API
handler treats as JSON and the store treats as a DynamoDB item).

Deliberately generic: nothing here references trackers, check-ins, categories,
or any other Datum-app concept beyond the bare `targetKind`/`metric` vocabulary
strings the job carries opaquely on the automation-fire path.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class ValidationError(ValueError):
    """Raised when a Job or Schedule fails validation."""


class JobKind(str, Enum):
    REMOTE_FETCH = "remoteFetch"
    AUTOMATION_FIRE = "automationFire"


class ScheduleType(str, Enum):
    ONCE = "once"
    HOURLY = "hourly"
    EVERY_N_HOURS = "everyNHours"
    DAILY_AT_HOUR = "dailyAtHour"


class TargetKind(str, Enum):
    TRACKER = "tracker"
    CHECK_IN = "checkIn"


class Metric(str, Enum):
    INCREMENT = "increment"
    PRESENCE_ON = "presenceOn"
    PRESENCE_OFF = "presenceOff"


def _parse_iso8601(value: str) -> datetime:
    # Accept a trailing "Z" the way most JSON clients emit it; Python's
    # fromisoformat wants "+00:00" instead.
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso8601(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Schedule:
    """The shared schedule shape used by both job kinds.

    - ``once``: fires a single time at ``at``.
    - ``hourly``: fires every hour, on the hour relative to first run.
    - ``everyNHours``: fires every ``interval_hours`` hours.
    - ``dailyAtHour``: fires once per day at ``hour`` (0-23, UTC).
    """

    type: ScheduleType
    at: Optional[datetime] = None
    interval_hours: Optional[int] = None
    hour: Optional[int] = None

    def validate(self) -> None:
        if self.type == ScheduleType.ONCE:
            if self.at is None:
                raise ValidationError("schedule.at is required for a 'once' schedule")
        elif self.type == ScheduleType.HOURLY:
            pass
        elif self.type == ScheduleType.EVERY_N_HOURS:
            if not self.interval_hours or self.interval_hours < 1:
                raise ValidationError(
                    "schedule.interval_hours must be a positive integer for "
                    "'everyNHours'"
                )
        elif self.type == ScheduleType.DAILY_AT_HOUR:
            if self.hour is None or not (0 <= self.hour <= 23):
                raise ValidationError(
                    "schedule.hour must be 0-23 for 'dailyAtHour'"
                )
        else:  # pragma: no cover - Enum already constrains this
            raise ValidationError(f"unknown schedule type {self.type!r}")

    def to_dict(self) -> dict:
        d: dict = {"type": self.type.value}
        if self.at is not None:
            d["at"] = _iso8601(self.at)
        if self.interval_hours is not None:
            d["intervalHours"] = self.interval_hours
        if self.hour is not None:
            d["hour"] = self.hour
        return d

    @staticmethod
    def from_dict(d: dict) -> "Schedule":
        try:
            schedule_type = ScheduleType(d["type"])
        except (KeyError, ValueError) as exc:
            raise ValidationError(f"invalid or missing schedule.type: {exc}") from exc
        at = _parse_iso8601(d["at"]) if d.get("at") else None
        # DynamoDB round-trips numbers as Decimal, not int -- normalize here
        # so callers never have to know whether a dict came from JSON or a
        # DynamoDB item.
        interval_hours = (
            int(d["intervalHours"]) if d.get("intervalHours") is not None else None
        )
        hour = int(d["hour"]) if d.get("hour") is not None else None
        schedule = Schedule(
            type=schedule_type, at=at, interval_hours=interval_hours, hour=hour
        )
        schedule.validate()
        return schedule


@dataclass(frozen=True)
class Job:
    id: str
    kind: JobKind
    schedule: Schedule
    support_id: str
    device_token: str

    # kind == remoteFetch only
    endpoint_url: Optional[str] = None
    extraction_path: Optional[str] = None

    # kind == automationFire only
    automation_id: Optional[str] = None
    target_kind: Optional[TargetKind] = None
    target_id: Optional[str] = None
    metric: Optional[Metric] = None

    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_run_at: Optional[datetime] = None

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())

    def validate(self) -> None:
        if not self.support_id:
            raise ValidationError("supportID is required")
        if not self.device_token:
            raise ValidationError("deviceToken is required")
        self.schedule.validate()

        if self.kind == JobKind.REMOTE_FETCH:
            if not self.endpoint_url:
                raise ValidationError("endpointURL is required for a remoteFetch job")
            if not self.endpoint_url.lower().startswith("https://"):
                raise ValidationError("endpointURL must be an https:// URL")
            if not self.extraction_path:
                raise ValidationError(
                    "extractionPath is required for a remoteFetch job"
                )
        elif self.kind == JobKind.AUTOMATION_FIRE:
            if self.target_kind is None:
                raise ValidationError(
                    "targetKind is required for an automationFire job"
                )
            if not self.target_id:
                raise ValidationError(
                    "targetID is required for an automationFire job"
                )
            if self.metric is None:
                raise ValidationError("metric is required for an automationFire job")
        else:  # pragma: no cover - Enum already constrains this
            raise ValidationError(f"unknown job kind {self.kind!r}")

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind.value,
            "schedule": self.schedule.to_dict(),
            "supportID": self.support_id,
            "deviceToken": self.device_token,
            "createdAt": _iso8601(self.created_at),
        }
        if self.last_run_at is not None:
            d["lastRunAt"] = _iso8601(self.last_run_at)
        if self.kind == JobKind.REMOTE_FETCH:
            d["endpointURL"] = self.endpoint_url
            d["extractionPath"] = self.extraction_path
        else:
            if self.automation_id is not None:
                d["automationID"] = self.automation_id
            d["targetKind"] = self.target_kind.value if self.target_kind else None
            d["targetID"] = self.target_id
            d["metric"] = self.metric.value if self.metric else None
        return d

    @staticmethod
    def from_dict(d: dict) -> "Job":
        try:
            kind = JobKind(d["kind"])
        except (KeyError, ValueError) as exc:
            raise ValidationError(f"invalid or missing kind: {exc}") from exc

        schedule = Schedule.from_dict(d["schedule"])
        created_at = (
            _parse_iso8601(d["createdAt"])
            if d.get("createdAt")
            else datetime.now(timezone.utc)
        )
        last_run_at = _parse_iso8601(d["lastRunAt"]) if d.get("lastRunAt") else None

        job = Job(
            id=d.get("id") or Job.new_id(),
            kind=kind,
            schedule=schedule,
            support_id=d.get("supportID", ""),
            device_token=d.get("deviceToken", ""),
            endpoint_url=d.get("endpointURL"),
            extraction_path=d.get("extractionPath"),
            automation_id=d.get("automationID"),
            target_kind=TargetKind(d["targetKind"]) if d.get("targetKind") else None,
            target_id=d.get("targetID"),
            metric=Metric(d["metric"]) if d.get("metric") else None,
            created_at=created_at,
            last_run_at=last_run_at,
        )
        job.validate()
        return job

    def with_last_run_at(self, when: datetime) -> "Job":
        from dataclasses import replace

        return replace(self, last_run_at=when)
