"""APNs token-based (ES256 JWT) push sender.

Builds a provider authentication token per Apple's token-auth spec
(https://developer.apple.com/documentation/usernotifications/establishing-a-token-based-connection-to-apns)
and POSTs a silent (`content-available`) push over HTTP/2 to APNs.

Only the ``.p8`` key material is secret; Team ID and Key ID are safe to keep in
config (see NOTIFICATION-SERVER-INFRA.md §8). The key material itself is
never read from disk in this repo — the Lambda handler pulls it from Secrets
Manager at invocation time and passes it in as a string.

The JWT-building half (``build_provider_token``) is pure given a fixed `now`
and is unit-tested directly. The actual network POST (``send_push``) is not
unit-tested against a live APNs endpoint (out of scope per the task — "do NOT
attempt real APNs delivery testing"), but its request-construction is
exercised via an injectable HTTP client so the payload/headers shape is
covered.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Protocol

import jwt as pyjwt

from .models import Job, JobKind

APNS_PRODUCTION_HOST = "https://api.push.apple.com"
APNS_SANDBOX_HOST = "https://api.sandbox.push.apple.com"

# Provider tokens are valid up to 60 minutes per Apple's spec; refresh a bit
# early to avoid edge-of-window rejections.
TOKEN_MAX_AGE_SECONDS = 50 * 60


class APNsError(RuntimeError):
    pass


@dataclass(frozen=True)
class APNsConfig:
    team_id: str
    key_id: str
    bundle_id: str
    private_key_pem: str
    use_sandbox: bool = False

    @property
    def host(self) -> str:
        return APNS_SANDBOX_HOST if self.use_sandbox else APNS_PRODUCTION_HOST


def build_provider_token(config: APNsConfig, *, now: Optional[datetime] = None) -> str:
    """Build a fresh ES256 provider JWT. Pure given ``now``."""
    now = now or datetime.now(timezone.utc)
    headers = {"alg": "ES256", "kid": config.key_id}
    payload = {"iss": config.team_id, "iat": int(now.timestamp())}
    return pyjwt.encode(payload, config.private_key_pem, algorithm="ES256", headers=headers)


class _CachedToken:
    """Small helper so a warm Lambda execution environment reuses one
    provider token across invocations instead of re-signing every push
    (Apple rate-limits provider-token creation)."""

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._issued_at: float = 0.0

    def get(self, config: APNsConfig) -> str:
        now = time.time()
        if self._token is None or (now - self._issued_at) > TOKEN_MAX_AGE_SECONDS:
            self._token = build_provider_token(config)
            self._issued_at = now
        return self._token


_token_cache = _CachedToken()


def build_payload(job: Job, *, extracted_value: Optional[float] = None) -> dict:
    """Build the APNs JSON payload for a job firing. Every payload is a
    silent, `content-available` push — v1 never renders a user-facing alert
    (see NOTIFICATION-SERVER-INFRA.md §3). The payload carries only opaque
    routing data the device already knows how to interpret (a job id, a
    target kind/id, a metric name, or an extracted numeric value) — never
    tracker names, values in a human-readable sense, or notes.
    """
    aps: dict[str, Any] = {"content-available": 1}
    body: dict[str, Any] = {"aps": aps, "jobId": job.id, "kind": job.kind.value}

    if job.kind == JobKind.REMOTE_FETCH:
        if extracted_value is not None:
            body["value"] = extracted_value
    elif job.kind == JobKind.AUTOMATION_FIRE:
        body["targetKind"] = job.target_kind.value if job.target_kind else None
        body["targetID"] = job.target_id
        body["metric"] = job.metric.value if job.metric else None
        if job.automation_id:
            body["automationID"] = job.automation_id

    return body


class HTTPClient(Protocol):
    """The narrow surface `send_push` needs from an HTTP/2 client — lets
    tests inject a fake without pulling in a real network stack."""

    def post(self, url: str, *, headers: dict, content: bytes) -> "HTTPResponse":
        ...


class HTTPResponse(Protocol):
    status_code: int
    text: str


@dataclass(frozen=True)
class PushResult:
    apns_id: Optional[str]
    status_code: int
    reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status_code == 200


def send_push(
    device_token: str,
    payload: dict,
    config: APNsConfig,
    *,
    client: HTTPClient,
    priority: int = 5,  # silent pushes must use priority 5, per Apple's spec
    expiration: int = 0,
) -> PushResult:
    """POST one push to APNs via HTTP/2. ``client`` must implement
    :class:`HTTPClient` (an ``httpx.Client(http2=True)`` satisfies this).
    """
    token = _token_cache.get(config)
    url = f"{config.host}/3/device/{device_token}"
    headers = {
        "authorization": f"bearer {token}",
        "apns-topic": config.bundle_id,
        "apns-push-type": "background",
        "apns-priority": str(priority),
        "apns-expiration": str(expiration),
        "content-type": "application/json",
    }
    body = json.dumps(payload).encode("utf-8")

    response = client.post(url, headers=headers, content=body)
    apns_id = None
    if hasattr(response, "headers"):
        apns_id = response.headers.get("apns-id")  # type: ignore[attr-defined]

    reason = None
    if response.status_code != 200:
        try:
            reason = json.loads(response.text).get("reason")
        except Exception:  # pragma: no cover - best-effort diagnostics only
            reason = response.text

    return PushResult(apns_id=apns_id, status_code=response.status_code, reason=reason)


def make_httpx_client() -> "HTTPClient":  # pragma: no cover - thin factory
    import httpx

    return httpx.Client(http2=True, timeout=10.0)
