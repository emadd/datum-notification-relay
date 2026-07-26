from datetime import datetime, timezone

import boto3
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from moto import mock_aws

from relay.apns import APNsConfig
from relay.handlers.run_due_jobs import DispatchOutcome, dispatch_job
from relay.models import Job, JobKind, Metric, Schedule, ScheduleType, TargetKind
from relay.store import JobNotFound, JobStore

TABLE_NAME = "test-run-due-jobs"
UTC = timezone.utc

# A real (test-only) EC keypair, generated once for this module -- send_push
# builds a real ES256 JWT via pyjwt even when the HTTP client is faked, and
# apns.py caches the signed provider token in a module-level singleton
# (`_token_cache`) shared across the whole test process. A syntactically
# bogus PEM string would work by accident if some earlier-run test file
# happened to warm that cache first, and fail if it didn't -- order-dependent
# either way. Using a real keypair here sidesteps that entirely.
_EC_KEYPAIR_PEM = (
    ec.generate_private_key(ec.SECP256R1())
    .private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    .decode()
)


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


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


@pytest.fixture
def handler_module(monkeypatch, store):
    """Reload run_due_jobs against the mocked table, with APNs config/HTTP
    client wiring short-circuited so no real Secrets Manager or network call
    is ever made -- each test supplies its own fake HTTP client via
    `wire_fake_client`.
    """
    monkeypatch.setenv("JOBS_TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    import importlib

    from relay.handlers import run_due_jobs

    importlib.reload(run_due_jobs)

    # handle() looks up JobStore(table_name) itself (not the `store` fixture's
    # instance), but against the same mocked moto backend/table, so writes
    # through either handle are visible via `store` too.
    fake_config = APNsConfig(
        team_id="TEAMID1234",
        key_id="KEYID56789",
        bundle_id="com.example.app",
        private_key_pem=_EC_KEYPAIR_PEM,
        use_sandbox=True,
    )
    monkeypatch.setattr(run_due_jobs, "_load_apns_config", lambda: fake_config)

    def wire_fake_client(client):
        monkeypatch.setattr(run_due_jobs, "_get_http_client", lambda: client)

    run_due_jobs.wire_fake_client = wire_fake_client  # type: ignore[attr-defined]
    yield run_due_jobs


def automation_job(**overrides) -> Job:
    # Deliberately always `automationFire`, never `remoteFetch`, for every
    # helper-built job in this file: `remoteFetch` jobs make a *real*
    # network call inside dispatch_job (relay.fetch.fetch_json is explicit
    # in its own module docstring that it is "not covered by the unit test
    # suite" and does real I/O) before ever reaching the injectable
    # HTTPClient. Using `automationFire` throughout keeps every test here
    # fully offline and deterministic while still exercising the exact
    # send_push/dispatch_job/handle() logic this fix touches -- the kind
    # branch is orthogonal to the push-outcome branch under test.
    defaults = dict(
        id=Job.new_id(),
        kind=JobKind.AUTOMATION_FIRE,
        schedule=Schedule(type=ScheduleType.ONCE, at=datetime(2026, 1, 1, tzinfo=UTC)),
        support_id="DTM-AAAA-BBBB-CCCC",
        device_token="deadbeef",
        target_kind=TargetKind.TRACKER,
        target_id="cat-1",
        metric=Metric.INCREMENT,
        created_at=datetime(2025, 12, 31, tzinfo=UTC),
    )
    defaults.update(overrides)
    return Job(**defaults)


# ---------------------------------------------------------------------------
# Fake APNs HTTP client/response -- deliberately duplicated from
# tests/test_apns.py's _FakeClient/_FakeResponse (per-suite disposable test
# doubles, not shared state) rather than imported.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "", headers: dict | None = None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class _FakeClient:
    """A minimal fake APNs HTTP client. For dispatch_job-level tests a single
    canned response is enough. For handle()-level tests with multiple jobs in
    one batch, `responses_by_token` maps the device token embedded in the
    request URL (.../3/device/<token>) to either a canned response or an
    Exception instance to raise -- letting each job in a batch get an
    independent, deterministic outcome regardless of query ordering.
    """

    def __init__(
        self,
        response: "_FakeResponse | None" = None,
        *,
        responses_by_token: "dict[str, object] | None" = None,
    ):
        self.response = response
        self.responses_by_token = responses_by_token or {}
        self.calls: list[dict] = []

    def post(self, url, *, headers, content):
        self.calls.append({"url": url, "headers": headers, "content": content})
        if self.responses_by_token:
            token = url.rsplit("/", 1)[-1]
            outcome = self.responses_by_token[token]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return self.response


def _dummy_config() -> APNsConfig:
    return APNsConfig(
        team_id="TEAMID1234",
        key_id="KEYID56789",
        bundle_id="com.example.app",
        private_key_pem=_EC_KEYPAIR_PEM,
        use_sandbox=True,
    )


# ---------------------------------------------------------------------------
# dispatch_job-level scenarios (no DynamoDB needed)
# ---------------------------------------------------------------------------


class TestDispatchJob:
    def test_successful_push_returns_sent(self):
        job = automation_job()
        client = _FakeClient(_FakeResponse(200, headers={"apns-id": "abc-123"}))

        outcome = dispatch_job(job, config=_dummy_config(), client=client)

        assert outcome == DispatchOutcome.SENT

    def test_ordinary_failure_returns_failed_not_dead_token(self):
        job = automation_job()
        client = _FakeClient(_FakeResponse(500, text="Internal Server Error"))

        outcome = dispatch_job(job, config=_dummy_config(), client=client)

        assert outcome == DispatchOutcome.FAILED
        assert outcome != DispatchOutcome.DEAD_TOKEN

    def test_410_unregistered_returns_dead_token(self):
        # Mirrors test_apns.py's own test_rejected_push_surfaces_reason
        # fixture shape exactly -- this is the flagship case for the fix.
        job = automation_job()
        client = _FakeClient(_FakeResponse(410, text='{"reason": "Unregistered"}'))

        outcome = dispatch_job(job, config=_dummy_config(), client=client)

        assert outcome == DispatchOutcome.DEAD_TOKEN


# ---------------------------------------------------------------------------
# handle()-level scenarios (real handle() entry point, mocked DynamoDB)
# ---------------------------------------------------------------------------


class TestHandleRecurringSuccess:
    def test_due_recurring_job_survives_with_advanced_next_due_at(
        self, handler_module, store
    ):
        job = automation_job(schedule=Schedule(type=ScheduleType.HOURLY))
        store.put(job)
        original_item = store._table.get_item(Key={"id": job.id})["Item"]

        handler_module.wire_fake_client(_FakeClient(_FakeResponse(200)))
        result = handler_module.handle({})

        assert result["sent"] == 1
        assert result["skipped"] == 0
        assert result["retired"] == 0
        assert result["dead_token_deleted"] == 0

        updated = store.get(job.id)
        assert updated.id == job.id
        assert updated.last_run_at is not None

        updated_item = store._table.get_item(Key={"id": job.id})["Item"]
        assert updated_item["nextDueAt"] > original_item["nextDueAt"]


class TestHandleOnceSuccess:
    def test_due_once_job_is_deleted_after_successful_send(self, handler_module, store):
        job = automation_job(
            schedule=Schedule(type=ScheduleType.ONCE, at=datetime(2026, 1, 1, tzinfo=UTC))
        )
        store.put(job)

        handler_module.wire_fake_client(_FakeClient(_FakeResponse(200)))
        result = handler_module.handle({})

        assert result["sent"] == 1
        assert result["retired"] == 1
        assert result["dead_token_deleted"] == 0

        with pytest.raises(JobNotFound):
            store.get(job.id)


class TestHandleNotYetDue:
    def test_not_due_job_is_left_completely_alone(self, handler_module, store):
        job = automation_job(
            schedule=Schedule(
                # Deliberately far in the future so this stays "not due" no
                # matter when this suite is re-run.
                type=ScheduleType.ONCE,
                at=datetime(2099, 1, 1, tzinfo=UTC),
            )
        )
        store.put(job)

        # A client that records every call it receives -- the real assertion
        # below (`client.calls == []`) is what proves dispatch_job/send_push
        # was never even invoked for this job, i.e. the defensive job_is_due
        # re-check inside handle() actually short-circuits before dispatch.
        client = _FakeClient()
        handler_module.wire_fake_client(client)

        result = handler_module.handle({})

        assert result["sent"] == 0
        assert result["skipped"] == 0
        assert result["dead_token_deleted"] == 0
        assert client.calls == []

        still_there = store.get(job.id)
        assert still_there.id == job.id


class TestHandleOrdinaryFailureIsNoOp:
    def test_500_failure_leaves_job_byte_identical(self, handler_module, store):
        job = automation_job(schedule=Schedule(type=ScheduleType.HOURLY))
        store.put(job)
        before_item = store._table.get_item(Key={"id": job.id})["Item"]

        handler_module.wire_fake_client(
            _FakeClient(_FakeResponse(500, text="Internal Server Error"))
        )
        result = handler_module.handle({})

        assert result["skipped"] == 1
        assert result["sent"] == 0
        assert result["dead_token_deleted"] == 0

        after_item = store._table.get_item(Key={"id": job.id})["Item"]
        assert after_item == before_item


class TestHandleDeadToken:
    def test_410_unregistered_job_is_deleted_end_to_end(self, handler_module, store):
        job = automation_job(schedule=Schedule(type=ScheduleType.HOURLY))
        store.put(job)

        handler_module.wire_fake_client(
            _FakeClient(_FakeResponse(410, text='{"reason": "Unregistered"}'))
        )
        result = handler_module.handle({})

        assert result["dead_token_deleted"] == 1
        assert result["sent"] == 0
        assert result["skipped"] == 0
        assert result["retired"] == 0

        with pytest.raises(JobNotFound):
            store.get(job.id)


class TestHandleBatchIndependence:
    def test_one_failing_job_does_not_starve_the_other(self, handler_module, store):
        # Docstring contract: "A single job's failure ... never aborts the
        # batch". failing_job's send raises (simulating a network-level
        # error); succeeding_job should still be fully processed regardless.
        failing_job = automation_job(
            device_token="token-that-blows-up",
            schedule=Schedule(type=ScheduleType.HOURLY),
        )
        succeeding_job = automation_job(
            device_token="token-that-works",
            schedule=Schedule(type=ScheduleType.HOURLY),
        )
        store.put(failing_job)
        store.put(succeeding_job)

        client = _FakeClient(
            responses_by_token={
                "token-that-blows-up": ConnectionError("simulated network failure"),
                "token-that-works": _FakeResponse(200),
            }
        )
        handler_module.wire_fake_client(client)

        result = handler_module.handle({})

        assert result["sent"] == 1
        assert result["skipped"] == 1
        assert result["dead_token_deleted"] == 0

        updated_succeeding = store.get(succeeding_job.id)
        assert updated_succeeding.last_run_at is not None

        updated_failing = store.get(failing_job.id)
        assert updated_failing.last_run_at is None
