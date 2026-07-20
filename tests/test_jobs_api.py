import json
import os

import boto3
import pytest
from moto import mock_aws

TABLE_NAME = "test-jobs-api"


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
def handler_module(monkeypatch):
    monkeypatch.setenv("JOBS_TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        _create_table(resource)

        # Import (or reload) after the mock + table exist so the handler's
        # lazily-created global store binds against the mocked DynamoDB.
        import importlib

        from relay.handlers import jobs_api

        importlib.reload(jobs_api)
        yield jobs_api


def _event(method: str, path: str, *, path_params=None, body=None) -> dict:
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "pathParameters": path_params or {},
        "body": json.dumps(body) if body is not None else None,
    }


REMOTE_FETCH_BODY = {
    "kind": "remoteFetch",
    "schedule": {"type": "hourly"},
    "supportID": "DTM-AAAA-BBBB-CCCC",
    "deviceToken": "devtoken1",
    "endpointURL": "https://example.com/data.json",
    "extractionPath": "value",
}

AUTOMATION_BODY = {
    "kind": "automationFire",
    "schedule": {"type": "dailyAtHour", "hour": 9},
    "supportID": "DTM-AAAA-BBBB-CCCC",
    "deviceToken": "devtoken1",
    "targetKind": "tracker",
    "targetID": "cat-1",
    "metric": "increment",
}


class TestPutJob:
    def test_creates_a_job(self, handler_module):
        response = handler_module.handle(
            _event("PUT", "/jobs/job-1", path_params={"jobId": "job-1"}, body=REMOTE_FETCH_BODY)
        )
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["id"] == "job-1"
        assert body["kind"] == "remoteFetch"

    def test_invalid_job_returns_400(self, handler_module):
        bad_body = dict(REMOTE_FETCH_BODY)
        del bad_body["endpointURL"]
        response = handler_module.handle(
            _event("PUT", "/jobs/job-1", path_params={"jobId": "job-1"}, body=bad_body)
        )
        assert response["statusCode"] == 400

    def test_missing_job_id_returns_400(self, handler_module):
        response = handler_module.handle(
            _event("PUT", "/jobs/", path_params={}, body=REMOTE_FETCH_BODY)
        )
        assert response["statusCode"] == 400


class TestGetJob:
    def test_returns_the_job(self, handler_module):
        handler_module.handle(
            _event("PUT", "/jobs/job-1", path_params={"jobId": "job-1"}, body=AUTOMATION_BODY)
        )
        response = handler_module.handle(
            _event("GET", "/jobs/job-1", path_params={"jobId": "job-1"})
        )
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["metric"] == "increment"

    def test_missing_job_returns_404(self, handler_module):
        response = handler_module.handle(
            _event("GET", "/jobs/nope", path_params={"jobId": "nope"})
        )
        assert response["statusCode"] == 404


class TestDeleteJob:
    def test_deletes_the_job(self, handler_module):
        handler_module.handle(
            _event("PUT", "/jobs/job-1", path_params={"jobId": "job-1"}, body=REMOTE_FETCH_BODY)
        )
        delete_response = handler_module.handle(
            _event("DELETE", "/jobs/job-1", path_params={"jobId": "job-1"})
        )
        assert delete_response["statusCode"] == 204

        get_response = handler_module.handle(
            _event("GET", "/jobs/job-1", path_params={"jobId": "job-1"})
        )
        assert get_response["statusCode"] == 404


class TestListJobsForDevice:
    def test_lists_jobs_for_the_pair(self, handler_module):
        handler_module.handle(
            _event("PUT", "/jobs/job-1", path_params={"jobId": "job-1"}, body=REMOTE_FETCH_BODY)
        )
        handler_module.handle(
            _event("PUT", "/jobs/job-2", path_params={"jobId": "job-2"}, body=AUTOMATION_BODY)
        )
        response = handler_module.handle(
            _event(
                "GET",
                "/devices/DTM-AAAA-BBBB-CCCC/devtoken1/jobs",
                path_params={"supportID": "DTM-AAAA-BBBB-CCCC", "deviceToken": "devtoken1"},
            )
        )
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        ids = {job["id"] for job in body["jobs"]}
        assert ids == {"job-1", "job-2"}

    def test_missing_params_returns_400(self, handler_module):
        response = handler_module.handle(
            _event("GET", "/devices//jobs", path_params={})
        )
        assert response["statusCode"] == 400


class TestUnknownRoute:
    def test_returns_404(self, handler_module):
        response = handler_module.handle(_event("GET", "/unknown", path_params={}))
        assert response["statusCode"] == 404
