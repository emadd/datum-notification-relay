"""API Gateway (HTTP API, Lambda proxy integration) handler for job
registration.

Routes (see apigateway.tf):

    PUT    /jobs/{jobId}   -- create or replace a job (idempotent upsert)
    GET    /jobs/{jobId}   -- fetch one job
    DELETE /jobs/{jobId}   -- delete one job
    GET    /devices/{supportID}/{deviceToken}/jobs  -- list a device's jobs

Auth model is deliberately the app's own: "no auth beyond supportID +
deviceToken" (NOTIFICATION-SERVER-INFRA.md §1/§3). There is no bearer token,
no API key, no IAM auth on these routes — anyone who knows a job's id can
read/replace/delete it, and anyone who knows a (supportID, deviceToken) pair
can list its jobs. That is an intentional consequence of "no accounts, no
PII" — the supportID + deviceToken pair itself IS the credential, exactly
like the rest of Datum's anonymous-account model. Job ids are UUIDv4 (122
bits of entropy), so they are not guessable in practice even though they are
not treated as secret.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from ..models import Job, ValidationError
from ..store import JobNotFound, JobStore

_store: Optional[JobStore] = None


def _get_store() -> JobStore:
    global _store
    if _store is None:
        table_name = os.environ["JOBS_TABLE_NAME"]
        _store = JobStore(table_name)
    return _store


def _response(status: int, body: Any) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def _error(status: int, message: str) -> dict:
    return _response(status, {"error": message})


def handle(event: dict, context: Any = None) -> dict:  # noqa: ARG001 - context unused
    """API Gateway HTTP API (payload format 2.0) Lambda proxy entrypoint."""
    try:
        method = event["requestContext"]["http"]["method"]
        raw_path = event["requestContext"]["http"]["path"]
    except KeyError:
        return _error(400, "malformed request")

    path_params = event.get("pathParameters") or {}
    store = _get_store()

    try:
        if raw_path.startswith("/jobs/") or raw_path == "/jobs":
            job_id = path_params.get("jobId")
            if method == "PUT":
                return _put_job(store, job_id, event)
            if method == "GET":
                return _get_job(store, job_id)
            if method == "DELETE":
                return _delete_job(store, job_id)
            return _error(405, f"method {method} not allowed on /jobs/{{jobId}}")

        if raw_path.startswith("/devices/"):
            support_id = path_params.get("supportID")
            device_token = path_params.get("deviceToken")
            if method == "GET":
                return _list_jobs(store, support_id, device_token)
            return _error(405, f"method {method} not allowed on this route")

        return _error(404, f"no route for {raw_path}")

    except ValidationError as exc:
        return _error(400, str(exc))
    except JobNotFound:
        return _error(404, "job not found")
    except Exception as exc:  # pragma: no cover - last-resort guard
        return _error(500, f"internal error: {exc}")


def _put_job(store: JobStore, job_id: Optional[str], event: dict) -> dict:
    if not job_id:
        return _error(400, "jobId path parameter is required")
    body_raw = event.get("body") or "{}"
    try:
        body = json.loads(body_raw)
    except json.JSONDecodeError:
        return _error(400, "request body must be valid JSON")

    body["id"] = job_id  # the path is authoritative, not any id in the body
    job = Job.from_dict(body)
    store.put(job)
    return _response(200, job.to_dict())


def _get_job(store: JobStore, job_id: Optional[str]) -> dict:
    if not job_id:
        return _error(400, "jobId path parameter is required")
    job = store.get(job_id)
    return _response(200, job.to_dict())


def _delete_job(store: JobStore, job_id: Optional[str]) -> dict:
    if not job_id:
        return _error(400, "jobId path parameter is required")
    store.delete(job_id)
    return _response(204, "")


def _list_jobs(
    store: JobStore, support_id: Optional[str], device_token: Optional[str]
) -> dict:
    if not support_id or not device_token:
        return _error(400, "supportID and deviceToken path parameters are required")
    jobs = store.list_for_device(support_id, device_token)
    return _response(200, {"jobs": [job.to_dict() for job in jobs]})
