import json
from unittest.mock import Mock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from lambdas.worker.handler import SYSTEM_PROMPTS, lambda_handler


MODEL = "us.anthropic.claude-sonnet-4-6"
SOURCE = "uploads/document.txt"


def record(job_id="job-1", receive_count=1, tone="executive"):
    return {
        "messageId": job_id,
        "attributes": {"ApproximateReceiveCount": str(receive_count)},
        "body": json.dumps({"job_id": job_id, "s3_key": SOURCE, "tone": tone}),
    }


def run(*records):
    return lambda_handler({"Records": records}, None)["batchItemFailures"]


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("JOBS_TABLE", "doclens-jobs")
    monkeypatch.setenv("UPLOAD_BUCKET", "doclens-uploads")
    monkeypatch.setenv("RESULTS_BUCKET", "doclens-results")
    monkeypatch.setenv("BEDROCK_MODEL_ID", MODEL)
    with mock_aws():
        table = boto3.resource("dynamodb").create_table(
            TableName="doclens-jobs",
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table.put_item(Item={"job_id": "job-1", "status": "PENDING"})
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="doclens-uploads")
        s3.create_bucket(Bucket="doclens-results")
        s3.put_object(Bucket="doclens-uploads", Key=SOURCE, Body=b"Document text")
        client = boto3.client
        with patch("lambdas.worker.handler.boto3.client") as make_client:
            bedrock = Mock()
            bedrock.converse.return_value = {
                "output": {"message": {"content": [{"text": "Summary"}]}}
            }
            make_client.side_effect = lambda service, **kwargs: (
                bedrock if service == "bedrock-runtime" else client(service, **kwargs)
            )
            yield table, s3, bedrock


@pytest.mark.parametrize("tone", SYSTEM_PROMPTS)
def test_worker_summarizes_once(aws, tone):
    table, s3, bedrock = aws
    assert run(record(tone=tone)) == []
    assert run(record(receive_count=2, tone=tone)) == []
    bedrock.converse.assert_called_once_with(
        modelId=MODEL,
        system=[{"text": SYSTEM_PROMPTS[tone]}],
        messages=[{"role": "user", "content": [{"text": "Document text"}]}],
        inferenceConfig={"maxTokens": 1024, "temperature": 0.3},
    )
    item = table.get_item(Key={"job_id": "job-1"})["Item"]
    assert item["status"] == "DONE"
    assert item["retry_count"] == 0
    result = s3.get_object(Bucket="doclens-results", Key=item["result_key"])
    assert result["Body"].read() == b"Summary"


@pytest.mark.parametrize("code", ["ValidationException", "AccessDeniedException"])
def test_permanent_bedrock_failure(aws, code):
    table, _, bedrock = aws
    bedrock.converse.side_effect = ClientError({"Error": {"Code": code}}, "Converse")
    assert run(record()) == []
    assert run(record(receive_count=2)) == []
    bedrock.converse.assert_called_once()
    item = table.get_item(Key={"job_id": "job-1"})["Item"]
    assert item["status"] == "FAILED"
    assert item["error"] == code


def test_stale_completion_does_not_overwrite_newer_attempt(aws):
    table, _, bedrock = aws
    key = {"job_id": "job-1"}
    response = bedrock.converse.return_value

    def newer_attempt_claims_job(**kwargs):
        table.update_item(
            Key=key,
            UpdateExpression="SET retry_count = :retry",
            ExpressionAttributeValues={":retry": 1},
        )
        return response

    bedrock.converse.side_effect = newer_attempt_claims_job

    assert run(record()) == []
    bedrock.converse.assert_called_once()
    item = table.get_item(Key=key)["Item"]
    assert item["status"] == "PROCESSING"
    assert item["retry_count"] == 1
    assert "result_key" not in item


@pytest.mark.parametrize("code", [
    "ThrottlingException", "ModelTimeoutException", "ServiceUnavailableException",
])
def test_retryable_bedrock_failure(aws, code):
    table, _, bedrock = aws
    bedrock.converse.side_effect = ClientError({"Error": {"Code": code}}, "Converse")
    assert run(record(), record("missing-job")) == [{"itemIdentifier": "job-1"}]
    assert table.get_item(Key={"job_id": "job-1"})["Item"]["status"] == "PROCESSING"
    bedrock.converse.side_effect = None
    assert run(record(receive_count=2)) == []
    item = table.get_item(Key={"job_id": "job-1"})["Item"]
    assert item["status"] == "DONE"
    assert item["retry_count"] == 1


def test_unexpected_s3_failure_retries_message(aws):
    _, s3, bedrock = aws
    s3.delete_object(Bucket="doclens-uploads", Key=SOURCE)
    assert run(record()) == [{"itemIdentifier": "job-1"}]
    bedrock.converse.assert_not_called()
