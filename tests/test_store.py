from datetime import datetime, timedelta, timezone

import boto3
import pytest
from moto import mock_aws

from relay.models import Job, JobKind, Metric, Schedule, ScheduleType, TargetKind
from relay.store import JobNotFound, JobStore

TABLE_NAME = "test-jobs"
UTC = timezone.utc


def _create_table(dynamodb):
    return dynamodb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "id", "AttributeType": "S"},
            {"AttributeName": "supportID", "AttributeType": "S"},
            {"AttributeName": "deviceToken", "AttributeType": "S"},
            {"AttributeName": "duePartition", "AttributeType": "S"},
            {"AttributeName": "nextDueAt", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "gsi-identity",
                "KeySchema": [
                    {"AttributeName": "supportID", "KeyType": "HASH"},
                    {"AttributeName": "deviceToken", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "gsi-due",
                "KeySchema": [
                    {"AttributeName": "duePartition", "KeyType": "HASH"},
                    {"AttributeName": "nextDueAt", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def store():
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        _create_table(resource)
        yield JobStore(TABLE_NAME, resource=resource)


def fetch_job(**overrides) -> Job:
    defaults = dict(
        id=Job.new_id(),
        kind=JobKind.REMOTE_FETCH,
        schedule=Schedule(type=ScheduleType.HOURLY),
        support_id="DTM-AAAA-BBBB-CCCC",
        device_token="deadbeef",
        endpoint_url="https://example.com/data.json",
        extraction_path="value",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    defaults.update(overrides)
    return Job(**defaults)


def automation_job(**overrides) -> Job:
    defaults = dict(
        id=Job.new_id(),
        kind=JobKind.AUTOMATION_FIRE,
        schedule=Schedule(type=ScheduleType.ONCE, at=datetime(2026, 1, 2, tzinfo=UTC)),
        support_id="DTM-AAAA-BBBB-CCCC",
        device_token="deadbeef",
        target_kind=TargetKind.TRACKER,
        target_id="cat-1",
        metric=Metric.INCREMENT,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    defaults.update(overrides)
    return Job(**defaults)


class TestPutGetDelete:
    def test_put_then_get_round_trips(self, store):
        job = fetch_job()
        store.put(job)
        fetched = store.get(job.id)
        assert fetched.id == job.id
        assert fetched.endpoint_url == job.endpoint_url

    def test_get_missing_raises(self, store):
        with pytest.raises(JobNotFound):
            store.get("does-not-exist")

    def test_delete_removes_it(self, store):
        job = fetch_job()
        store.put(job)
        store.delete(job.id)
        with pytest.raises(JobNotFound):
            store.get(job.id)

    def test_put_rejects_invalid_job(self, store):
        job = fetch_job(endpoint_url=None)
        with pytest.raises(Exception):
            store.put(job)


class TestListForDevice:
    def test_lists_only_that_devices_jobs(self, store):
        mine1 = fetch_job(support_id="DTM-A", device_token="tokenA")
        mine2 = automation_job(support_id="DTM-A", device_token="tokenA")
        other = fetch_job(support_id="DTM-B", device_token="tokenB")
        for j in (mine1, mine2, other):
            store.put(j)

        jobs = store.list_for_device("DTM-A", "tokenA")
        ids = {j.id for j in jobs}
        assert ids == {mine1.id, mine2.id}


class TestListDue:
    def test_only_due_jobs_are_returned(self, store):
        now = datetime(2026, 1, 10, tzinfo=UTC)
        due_job = fetch_job(
            schedule=Schedule(type=ScheduleType.ONCE, at=datetime(2026, 1, 1, tzinfo=UTC))
        )
        not_due_job = fetch_job(
            schedule=Schedule(type=ScheduleType.ONCE, at=datetime(2026, 2, 1, tzinfo=UTC))
        )
        store.put(due_job)
        store.put(not_due_job)

        due = {j.id for j in store.list_due(now=now)}
        assert due_job.id in due
        assert not_due_job.id not in due

    def test_exhausted_once_job_drops_out_after_mark_ran(self, store):
        now = datetime(2026, 1, 10, tzinfo=UTC)
        job = fetch_job(
            schedule=Schedule(type=ScheduleType.ONCE, at=datetime(2026, 1, 1, tzinfo=UTC))
        )
        store.put(job)
        assert job.id in {j.id for j in store.list_due(now=now)}

        updated = store.mark_ran(job, ran_at=now)
        assert updated.id not in {j.id for j in store.list_due(now=now)}

    def test_recurring_job_becomes_due_again_after_interval(self, store):
        job = fetch_job(
            schedule=Schedule(type=ScheduleType.EVERY_N_HOURS, interval_hours=2),
            created_at=datetime(2026, 1, 1, 0, tzinfo=UTC),
        )
        store.put(job)

        now = datetime(2026, 1, 1, 0, 30, tzinfo=UTC)
        assert job.id not in {j.id for j in store.list_due(now=now)}

        ran_at = datetime(2026, 1, 1, 1, tzinfo=UTC)
        updated = store.mark_ran(job, ran_at=ran_at)

        still_not_due = datetime(2026, 1, 1, 2, 30, tzinfo=UTC)
        assert updated.id not in {j.id for j in store.list_due(now=still_not_due)}

        due_again = datetime(2026, 1, 1, 3, 0, tzinfo=UTC)
        assert updated.id in {j.id for j in store.list_due(now=due_again)}

    def test_delete_exhausted_removes_the_job(self, store):
        job = fetch_job(
            schedule=Schedule(type=ScheduleType.ONCE, at=datetime(2026, 1, 1, tzinfo=UTC))
        )
        store.put(job)
        updated = store.mark_ran(job, ran_at=datetime(2026, 1, 1, tzinfo=UTC))
        store.delete_exhausted(updated)
        with pytest.raises(JobNotFound):
            store.get(job.id)
