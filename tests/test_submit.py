import base64
import json
from datetime import datetime, timedelta
from uuid import UUID
from unittest.mock import Mock, patch

import boto3
import pytest
from moto import mock_aws

from lambdas.submit.handler import lambda_handler
from lambdas.status.handler import lambda_handler as status_handler
from lambdas.worker.handler import lambda_handler as worker_handler


REQUEST = {"s3_key": "uploads/document.txt", "tone": "executive"}


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        table = boto3.resource("dynamodb").create_table(
            TableName="doclens-jobs",
            KeySchema=[{"AttributeName": "job_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "job_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        sqs = boto3.client("sqs")
        queue_url = sqs.create_queue(QueueName="jobs")["QueueUrl"]
        monkeypatch.setenv("JOBS_TABLE", table.name)
        monkeypatch.setenv("JOBS_QUEUE_URL", queue_url)
        yield table, sqs, queue_url


@pytest.mark.parametrize("tone", ["executive", "bullet_points", "eli5"])
def test_submit(aws, tone):
    table, sqs, queue_url = aws
    request = {**REQUEST, "tone": tone}
    encoded = base64.b64encode(json.dumps(request).encode()).decode()
    result = lambda_handler({"body": encoded, "isBase64Encoded": True}, None)
    assert result["statusCode"] == 202
    body = json.loads(result["body"])
    job_id = body["job_id"]
    assert UUID(job_id).version == 4
    assert body == {"job_id": job_id, "status": "PENDING"}

    item = table.get_item(Key={"job_id": job_id})["Item"]
    assert item == {**request, **body, "created_at": item["created_at"], "updated_at": item["created_at"]}
    assert datetime.fromisoformat(item["created_at"]).utcoffset() == timedelta(0)
    message, = sqs.receive_message(QueueUrl=queue_url)["Messages"]
    assert json.loads(message["Body"]) == {**request, "job_id": job_id}


@pytest.mark.parametrize("event", [
    {},
    {"body": "{"},
    {"body": "[]"},
    {"body": "!", "isBase64Encoded": True},
    *({"body": json.dumps(request)} for request in [
        {"tone": "executive"},
        {"s3_key": "uploads/document.txt"},
        {**REQUEST, "s3_key": ""},
        {**REQUEST, "s3_key": 42},
        {**REQUEST, "s3_key": "document.txt"},
        {**REQUEST, "s3_key": "uploads/" + "a" * 1017},
        {**REQUEST, "tone": "formal"},
        {**REQUEST, "tone": []},
        {**REQUEST, "extra": True},
    ]),
])
def test_invalid_request(aws, event):
    table, sqs, queue_url = aws
    result = lambda_handler(event, None)
    assert result["statusCode"] == 400
    assert json.loads(result["body"])["error"]
    assert table.scan()["Count"] == 0
    assert "Messages" not in sqs.receive_message(QueueUrl=queue_url)


@pytest.mark.parametrize("service", ["dynamodb", "sqs"])
def test_aws_failure(aws, monkeypatch, service):
    table, sqs, queue_url = aws
    if service == "dynamodb":
        monkeypatch.setenv("JOBS_TABLE", "missing-table")
    else:
        sqs.delete_queue(QueueUrl=queue_url)

    result = lambda_handler({"body": json.dumps(REQUEST)}, None)
    assert result["statusCode"] == 503
    assert json.loads(result["body"]) == {"error": "service unavailable"}
    if service == "dynamodb":
        assert "Messages" not in sqs.receive_message(QueueUrl=queue_url)
    else:
        item, = table.scan()["Items"]
        assert item["status"] == "PENDING"


def test_submit_worker_status_flow(aws, monkeypatch):
    _, sqs, queue_url = aws
    monkeypatch.setenv("UPLOAD_BUCKET", "doclens-uploads")
    monkeypatch.setenv("RESULTS_BUCKET", "doclens-results")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    s3 = boto3.client("s3")
    s3.create_bucket(Bucket="doclens-uploads")
    s3.create_bucket(Bucket="doclens-results")
    s3.put_object(Bucket="doclens-uploads", Key=REQUEST["s3_key"], Body=b"Original text")

    submitted = lambda_handler({"body": json.dumps(REQUEST)}, None)
    assert submitted["statusCode"] == 202
    job_id = json.loads(submitted["body"])["job_id"]
    status_event = {"pathParameters": {"job_id": job_id}}
    assert json.loads(status_handler(status_event, None)["body"])["status"] == "PENDING"

    message, = sqs.receive_message(
        QueueUrl=queue_url, MessageSystemAttributeNames=["ApproximateReceiveCount"],
    )["Messages"]
    record = {
        "messageId": message["MessageId"],
        "body": message["Body"],
        "attributes": message["Attributes"],
    }
    bedrock = Mock()
    bedrock.converse.return_value = {"output": {"message": {"content": [{"text": "Summary"}]}}}
    real_client = boto3.client
    with patch("lambdas.worker.handler.boto3.client") as make_client:
        make_client.side_effect = lambda service, **kwargs: (
            bedrock if service == "bedrock-runtime" else real_client(service, **kwargs)
        )
        assert worker_handler({"Records": [record]}, None) == {"batchItemFailures": []}

    completed = status_handler(status_event, None)
    assert completed["statusCode"] == 200
    body = json.loads(completed["body"])
    assert body["job_id"] == job_id and body["status"] == "DONE"
    assert f"/summaries/{job_id}.txt?" in body["result_url"]
    result = s3.get_object(Bucket="doclens-results", Key=f"summaries/{job_id}.txt")
    assert result["Body"].read() == b"Summary"
