"""DynamoDB access layer for the job store.

Table design (single table, see dynamodb.tf):

    pk (partition key) = job id (UUID string)

    GSI "gsi-identity": pk = supportID, sk = deviceToken
        -- lets the registration API list/update/delete a device's jobs
           without a table scan.

    GSI "gsi-due": pk = "DUE" (a single constant bucket), sk = nextDueAt
        -- lets the cron runner Query for due jobs instead of Scan.
        Known v1 scaling limit: a single-partition GSI caps throughput at one
        partition's worth of RCU/WCU (DynamoDB best practice normally warns
        against this). Acceptable for the expected volume of a personal-scale
        anonymous relay; if job counts grow enough to matter, shard the GSI
        pk by e.g. `"DUE#" + hash(job_id) % N` and fan the runner's Query out
        across the N buckets, or move to one native EventBridge Scheduler
        schedule per job instead of a poll loop.

Every write recomputes and stores `nextDueAt` so the GSI stays consistent;
`scheduling.py` is the single source of truth for that math.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

import boto3
from boto3.dynamodb.conditions import Key

from .models import Job, ValidationError
from .scheduling import job_next_due_at

logger = logging.getLogger(__name__)

DUE_BUCKET = "DUE"
# A sentinel far in the future so exhausted `once` jobs (nextDueAt is None)
# still get a sort-key value and sort last / drop out of any bounded Query.
NO_FURTHER_RUN_SENTINEL = "9999-12-31T23:59:59Z"


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class JobNotFound(KeyError):
    pass


class JobStore:
    def __init__(self, table_name: str, *, resource: Optional[Any] = None) -> None:
        self._dynamodb = resource or boto3.resource("dynamodb")
        self._table = self._dynamodb.Table(table_name)

    def _item_for(self, job: Job) -> dict:
        item = job.to_dict()
        next_due = job_next_due_at(job)
        item["nextDueAt"] = _iso(next_due) if next_due else NO_FURTHER_RUN_SENTINEL
        item["duePartition"] = DUE_BUCKET
        return item

    def put(self, job: Job) -> Job:
        job.validate()
        self._table.put_item(Item=self._item_for(job))
        return job

    @staticmethod
    def _parse_items(items: List[dict]) -> List[Job]:
        """Parses each item independently, skipping (and logging) any that
        fail validation instead of letting one bad row abort the whole
        batch — the same "one job's failure never starves the rest"
        contract `handlers/run_due_jobs.py` already documents for the
        dispatch step, extended to cover the parse step too (the actual
        gap a stale/legacy-shape row exposed: a single unparseable item
        used to raise out of ``list_due``/``list_for_device`` entirely,
        which meant the *cron poller itself* never ran for anything that
        pass — not just skipping the bad job, silently starving every
        other due job as well).

        Deliberately does not delete the offending row — a parse failure
        could in principle be a bug in this code rather than bad data, so
        removing data automatically on that basis is the wrong default;
        it stays in the table (and keeps logging) until a human looks and
        decides.
        """
        jobs: List[Job] = []
        for item in items:
            try:
                jobs.append(Job.from_dict(item))
            except ValidationError as exc:
                logger.warning(
                    "skipping unparseable job item (id=%r): %s",
                    item.get("id"),
                    exc,
                )
        return jobs

    def get(self, job_id: str) -> Job:
        response = self._table.get_item(Key={"id": job_id})
        item = response.get("Item")
        if item is None:
            raise JobNotFound(job_id)
        return Job.from_dict(item)

    def delete(self, job_id: str) -> None:
        self._table.delete_item(Key={"id": job_id})

    def list_for_device(self, support_id: str, device_token: str) -> List[Job]:
        response = self._table.query(
            IndexName="gsi-identity",
            KeyConditionExpression=(
                Key("supportID").eq(support_id) & Key("deviceToken").eq(device_token)
            ),
        )
        return self._parse_items(response.get("Items", []))

    def list_due(self, *, now: datetime, limit: int = 200) -> List[Job]:
        """Query the gsi-due index for every job whose nextDueAt <= now."""
        response = self._table.query(
            IndexName="gsi-due",
            KeyConditionExpression=(
                Key("duePartition").eq(DUE_BUCKET) & Key("nextDueAt").lte(_iso(now))
            ),
            Limit=limit,
        )
        return self._parse_items(response.get("Items", []))

    def mark_ran(self, job: Job, *, ran_at: datetime) -> Job:
        """Record that ``job`` fired at ``ran_at`` and recompute nextDueAt.
        Returns the updated Job (with last_run_at set)."""
        updated = job.with_last_run_at(ran_at)
        self.put(updated)
        return updated

    def delete_exhausted(self, job: Job) -> None:
        """A fired `once` job has no further schedule; the runner deletes it
        after a successful send rather than leaving dead rows around."""
        self.delete(job.id)
