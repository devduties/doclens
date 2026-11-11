import json
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import boto3
import pytest
from moto import mock_aws

from lambdas.status.handler import lambda_handler


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("JOBS_TABLE", "doclens-jobs")
    monkeypatch.setenv("RESULTS_BUCKET", "doclens-results")
    with mock_aws():
        yield boto3.resource("dynamodb").create_table(
            TableName="doclens-jobs",
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )


def get_status(job_id):
    response = lambda_handler({"pathParameters": {"job_id": job_id}}, None)
    assert response["headers"]["Cache-Control"] == "no-store"
    return response["statusCode"], json.loads(response["body"])


@pytest.mark.parametrize("job_id", [None, 123, "", "invalid", "00000000-0000-1000-8000-000000000000"])
def test_invalid_job_id(table, job_id):
    assert get_status(job_id) == (400, {"error": "invalid job_id"})


def test_missing_path_parameters(table):
    response = lambda_handler({"pathParameters": None}, None)
    assert response["statusCode"] == 400


def test_missing_job(table):
    assert get_status(str(uuid4())) == (404, {"error": "not found"})


def test_dynamodb_failure(table, monkeypatch):
    monkeypatch.setenv("JOBS_TABLE", "missing-table")
    assert get_status(str(uuid4())) == (503, {"error": "service unavailable"})


@pytest.mark.parametrize(("status", "has_key"), [
    ("PENDING", False),
    ("PROCESSING", False),
    ("FAILED", True),
    ("DONE", False),
    ("DONE", True),
])
def test_status_returns_public_fields_and_signs_completed_result(table, status, has_key):
    job_id = str(uuid4())
    public = {
        "job_id": job_id,
        "status": status,
        "tone": "executive",
        "created_at": "2026-11-11T00:00:00+00:00",
        "updated_at": "2026-11-11T00:01:00+00:00",
    }
    item = {**public, "error": "internal error", "s3_key": "uploads/private.txt", "retry_count": 1}
    if has_key:
        item["result_key"] = f"summaries/{job_id}.txt"
    table.put_item(Item=item)

    status_code, body = get_status(job_id.upper())
    assert status_code == 200
    if status == "DONE" and has_key:
        url = urlsplit(body.pop("result_url"))
        assert url.scheme == "https"
        assert url.netloc.startswith("doclens-results.")
        assert url.path == f"/summaries/{job_id}.txt"
        query = parse_qs(url.query)
        assert query["X-Amz-Expires"] == ["3600"]
        assert query["X-Amz-Signature"]
    assert body == public
