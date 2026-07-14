from datetime import datetime, timezone

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

from relay.apns import APNsConfig, PushResult, build_payload, build_provider_token, send_push
from relay.models import Job, JobKind, Metric, Schedule, ScheduleType, TargetKind


@pytest.fixture(scope="module")
def ec_keypair_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def config(ec_keypair_pem) -> APNsConfig:
    return APNsConfig(
        team_id="TEAMID1234",
        key_id="KEYID56789",
        bundle_id="com.example.app",
        private_key_pem=ec_keypair_pem,
        use_sandbox=True,
    )


def fetch_job(**overrides) -> Job:
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


def reminder_job(**overrides) -> Job:
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


class TestBuildProviderToken:
    def test_token_has_correct_header_and_claims(self, config):
        now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
        token = build_provider_token(config, now=now)

        header = pyjwt.get_unverified_header(token)
        assert header["alg"] == "ES256"
        assert header["kid"] == "KEYID56789"

        # Decode without verifying signature to check claims shape (this test
        # exercises token *construction*, not real APNs verification).
        claims = pyjwt.decode(token, options={"verify_signature": False})
        assert claims["iss"] == "TEAMID1234"
        assert claims["iat"] == int(now.timestamp())

    def test_token_is_verifiable_with_the_public_key(self, config, ec_keypair_pem):
        private_key = serialization.load_pem_private_key(
            ec_keypair_pem.encode(), password=None
        )
        public_pem = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        token = build_provider_token(config)
        # Raises if the signature doesn't verify.
        pyjwt.decode(token, public_pem, algorithms=["ES256"])


class TestBuildPayload:
    def test_remote_fetch_payload_carries_extracted_value(self):
        job = fetch_job()
        payload = build_payload(job, extracted_value=42.5)
        assert payload["aps"] == {"content-available": 1}
        assert payload["kind"] == "remoteFetch"
        assert payload["value"] == 42.5
        assert "targetKind" not in payload

    def test_remote_fetch_payload_without_value_omits_it(self):
        job = fetch_job()
        payload = build_payload(job)
        assert "value" not in payload

    def test_reminder_auto_log_payload_carries_target_and_metric(self):
        job = reminder_job()
        payload = build_payload(job)
        assert payload["targetKind"] == "tracker"
        assert payload["targetID"] == "cat-1"
        assert payload["metric"] == "increment"
        assert "value" not in payload

    def test_payload_never_carries_tracker_names_values_or_notes(self):
        # Guard against regressions that would leak app-content fields into
        # the relay's payload — it must stay opaque routing data only.
        job = reminder_job()
        payload = build_payload(job)
        forbidden_keys = {"name", "note", "notes", "value", "label", "trackerName"}
        assert forbidden_keys.isdisjoint(payload.keys())


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "", headers: dict | None = None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class _FakeClient:
    def __init__(self, response: _FakeResponse):
        self.response = response
        self.calls = []

    def post(self, url, *, headers, content):
        self.calls.append({"url": url, "headers": headers, "content": content})
        return self.response


class TestSendPush:
    def test_successful_push_builds_correct_request(self, config):
        client = _FakeClient(_FakeResponse(200, headers={"apns-id": "abc-123"}))
        result = send_push("devtoken", {"aps": {"content-available": 1}}, config, client=client)

        assert result.ok
        assert result.status_code == 200
        assert result.apns_id == "abc-123"

        call = client.calls[0]
        assert call["url"] == "https://api.sandbox.push.apple.com/3/device/devtoken"
        assert call["headers"]["apns-topic"] == "com.example.app"
        assert call["headers"]["apns-push-type"] == "background"
        assert call["headers"]["apns-priority"] == "5"
        assert call["headers"]["authorization"].startswith("bearer ")

    def test_production_host_used_when_not_sandbox(self, ec_keypair_pem):
        config = APNsConfig(
            team_id="T",
            key_id="K",
            bundle_id="com.example.app",
            private_key_pem=ec_keypair_pem,
            use_sandbox=False,
        )
        client = _FakeClient(_FakeResponse(200))
        send_push("devtoken", {}, config, client=client)
        assert client.calls[0]["url"].startswith("https://api.push.apple.com/")

    def test_rejected_push_surfaces_reason(self, config):
        client = _FakeClient(_FakeResponse(410, text='{"reason": "Unregistered"}'))
        result = send_push("devtoken", {}, config, client=client)
        assert not result.ok
        assert result.reason == "Unregistered"
