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


def dispatch_job(job: Job, *, config: APNsConfig, client: HTTPClient) -> bool:
    """Fire one job's push. Returns True on a confirmed-sent push."""
    extracted_value = None

    if job.kind == JobKind.REMOTE_FETCH:
        try:
            data = fetch_json(job.endpoint_url)  # SSRF-guarded inside fetch_json
            extracted_value = extract_number(data, job.extraction_path)
        except (SSRFBlocked, FetchError, ExtractionError) as exc:
            logger.warning("job %s remoteFetch failed: %s", job.id, exc)
            return False
    # automationFire: nothing to fetch, fall straight through to the push.

    payload = build_payload(job, extracted_value=extracted_value)
    try:
        result = send_push(job.device_token, payload, config, client=client)
    except Exception as exc:  # noqa: BLE001 - one bad job must not kill the batch
        logger.warning("job %s push failed to send: %s", job.id, exc)
        return False

    if not result.ok:
        logger.warning(
            "job %s push rejected by APNs: status=%s reason=%s",
            job.id,
            result.status_code,
            result.reason,
        )
        return False

    return True


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

    for job in due_jobs:
        # Defensive re-check: the GSI query already filtered on nextDueAt,
        # but recompute from the pure scheduling module so a stale/duplicate
        # index entry can never cause a double-fire.
        if not job_is_due(job, now=now):
            continue

        ok = dispatch_job(job, config=config, client=client)
        if ok:
            sent += 1
            updated = store.mark_ran(job, ran_at=now)
            if job_is_exhausted(updated):
                store.delete_exhausted(updated)
                retired += 1
        else:
            skipped += 1

    logger.info(
        "run_due_jobs: due=%d sent=%d skipped=%d retired=%d",
        len(due_jobs),
        sent,
        skipped,
        retired,
    )
    return {"due": len(due_jobs), "sent": sent, "skipped": skipped, "retired": retired}
