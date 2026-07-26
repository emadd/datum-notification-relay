"""Scheduled Lambda: invoked periodically by an EventBridge rule (every 5
minutes by default, see eventbridge.tf). Queries the job store for everything
due, dispatches each job by kind, sends the push, and advances/retires the
job's schedule.

Kind branch is deliberately shallow, per NOTIFICATION-SERVER-INFRA.md §3:

- ``remoteFetch``: SSRF-guarded HTTPS GET, extract a value via the JSON
  dot-path picker, push it silently.
- ``automationFire``: no fetch at all — just push the opaque
  {targetKind, targetID, metric} the job already carries. This kind never
  touches ``relay.ssrf`` or ``relay.fetch``.

A single job's failure (a dead endpoint, a malformed extraction path, an APNs
rejection) never aborts the batch — each job is handled independently and
errors are logged, not raised, so one bad job can't starve every other job of
its scheduled push.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import boto3

from ..apns import APNsConfig, HTTPClient, build_payload, make_httpx_client, send_push
from ..extraction import ExtractionError, extract_number
from ..fetch import FetchError, fetch_json
from ..models import Job, JobKind
from ..scheduling import job_is_due, job_is_exhausted
from ..ssrf import SSRFBlocked
from ..store import JobStore

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_apns_config: Optional[APNsConfig] = None
_http_client: Optional[HTTPClient] = None


class DispatchOutcome(str, Enum):
    """The three genuinely-different results a single job's dispatch can
    have. ``FAILED`` and ``DEAD_TOKEN`` used to collapse into the same
    ``False`` return value; they need different handling in ``handle()`` so
    they're now distinguished explicitly:

    - ``SENT``: the push was delivered (HTTP 200 from APNs).
    - ``FAILED``: the push did not succeed for a reason that might resolve
      itself (offline device, transient 5xx, a malformed job, an exception
      raised while fetching/extracting). Leave the job alone -- it's
      retried on the next cron tick.
    - ``DEAD_TOKEN``: APNs rejected the push with a 410 / reason
      "Unregistered" -- Apple's documented, permanent "this device token
      will never work again" signal. The job should be deleted outright
      rather than retried forever.
    """

    SENT = "sent"
    FAILED = "failed"
    DEAD_TOKEN = "dead_token"


def _load_apns_config() -> APNsConfig:
    global _apns_config
    if _apns_config is not None:
        return _apns_config

    secret_arn = os.environ["APNS_SECRET_ARN"]
    secrets = boto3.client("secretsmanager")
    secret_value = secrets.get_secret_value(SecretId=secret_arn)["SecretString"]

    import json

    secret = json.loads(secret_value)
    _apns_config = APNsConfig(
        team_id=os.environ["APNS_TEAM_ID"],
        key_id=os.environ["APNS_KEY_ID"],
        bundle_id=os.environ["APNS_BUNDLE_ID"],
        private_key_pem=secret["privateKeyPem"],
        use_sandbox=os.environ.get("APNS_USE_SANDBOX", "false").lower() == "true",
    )
    return _apns_config


def _get_http_client() -> HTTPClient:
    global _http_client
    if _http_client is None:
        _http_client = make_httpx_client()
    return _http_client


def dispatch_job(job: Job, *, config: APNsConfig, client: HTTPClient) -> DispatchOutcome:
    """Fire one job's push. Returns a :class:`DispatchOutcome` -- SENT on a
    confirmed-sent push, DEAD_TOKEN when APNs has permanently rejected the
    device token (410 / reason "Unregistered"), FAILED for every other
    non-success (transient errors, malformed jobs, exceptions)."""
    extracted_value = None

    if job.kind == JobKind.REMOTE_FETCH:
        try:
            data = fetch_json(job.endpoint_url)  # SSRF-guarded inside fetch_json
            extracted_value = extract_number(data, job.extraction_path)
        except (SSRFBlocked, FetchError, ExtractionError) as exc:
            logger.warning("job %s remoteFetch failed: %s", job.id, exc)
            return DispatchOutcome.FAILED
    # automationFire: nothing to fetch, fall straight through to the push.

    payload = build_payload(job, extracted_value=extracted_value)
    try:
        result = send_push(job.device_token, payload, config, client=client)
    except Exception as exc:  # noqa: BLE001 - one bad job must not kill the batch
        logger.warning("job %s push failed to send: %s", job.id, exc)
        return DispatchOutcome.FAILED

    if not result.ok:
        # Per Apple's APNs contract, HTTP 410 with reason "Unregistered"
        # means the device token is permanently invalid -- it will never
        # succeed again. Either signal alone is authoritative for that
        # case; check both since we only need one to be sure.
        if result.status_code == 410 or result.reason == "Unregistered":
            logger.warning(
                "job %s device token permanently invalid (APNs 410/Unregistered): "
                "status=%s reason=%s",
                job.id,
                result.status_code,
                result.reason,
            )
            return DispatchOutcome.DEAD_TOKEN

        logger.warning(
            "job %s push rejected by APNs: status=%s reason=%s",
            job.id,
            result.status_code,
            result.reason,
        )
        return DispatchOutcome.FAILED

    return DispatchOutcome.SENT


def handle(event: dict, context: Any = None) -> dict:  # noqa: ARG001
    now = datetime.now(timezone.utc)
    table_name = os.environ["JOBS_TABLE_NAME"]
    store = JobStore(table_name)
    config = _load_apns_config()
    client = _get_http_client()

    due_jobs = store.list_due(now=now)
    sent = 0
    skipped = 0
    retired = 0
    dead_token_deleted = 0

    for job in due_jobs:
        # Defensive re-check: the GSI query already filtered on nextDueAt,
        # but recompute from the pure scheduling module so a stale/duplicate
        # index entry can never cause a double-fire.
        if not job_is_due(job, now=now):
            continue

        outcome = dispatch_job(job, config=config, client=client)
        if outcome == DispatchOutcome.SENT:
            sent += 1
            updated = store.mark_ran(job, ran_at=now)
            if job_is_exhausted(updated):
                store.delete_exhausted(updated)
                retired += 1
        elif outcome == DispatchOutcome.DEAD_TOKEN:
            # The push never succeeded, so there's nothing to record via
            # mark_ran -- just remove the row for this permanently-dead
            # device token via the same delete path a fired 'once' job
            # uses, straight from the original (un-mutated) job object.
            store.delete_exhausted(job)
            dead_token_deleted += 1
        else:
            skipped += 1

    logger.info(
        "run_due_jobs: due=%d sent=%d skipped=%d retired=%d dead_token_deleted=%d",
        len(due_jobs),
        sent,
        skipped,
        retired,
        dead_token_deleted,
    )
    return {
        "due": len(due_jobs),
        "sent": sent,
        "skipped": skipped,
        "retired": retired,
        "dead_token_deleted": dead_token_deleted,
    }
