import json
import logging
import os
from contextlib import closing
from datetime import UTC, datetime

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.config import Config
from botocore.exceptions import ClientError


logger = logging.getLogger(__name__)
RETRYABLE_ERRORS = {
    "ThrottlingException",
    "ModelTimeoutException",
    "ServiceUnavailableException",
}
SYSTEM_PROMPTS = {
    "executive": (
        "You are a professional summariser. Write two to three sentences. "
        "Lead with the main conclusion. Use formal language. No bullet points."
    ),
    "bullet_points": (
        "You are a professional summariser. Return five to eight bullet points. "
        "Each bullet is one idea, one sentence. No introduction or conclusion. "
        "Start directly with the first bullet."
    ),
    "eli5": (
        "You are a professional summariser. Write for someone with no background "
        "in the topic. Use short sentences and everyday words. No jargon."
    ),
}


def lambda_handler(event, context):
    table = boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"])
    max_receive_count = int(os.environ["JOBS_MAX_RECEIVE_COUNT"])
    s3 = boto3.client("s3")
    bedrock = boto3.client(
        "bedrock-runtime",
        config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
    )
    failures = []
    for record in event["Records"]:
        try:
            _process_record(record, table, s3, bedrock, max_receive_count)
        except Exception:
            logger.exception("Worker could not process message %s", record["messageId"])
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def _process_record(record, table, s3, bedrock, max_receive_count):
    message = json.loads(record["body"])
    job_id = message["job_id"]
    retry_count = int(record["attributes"]["ApproximateReceiveCount"]) - 1
    status = Attr("status")
    retry = Attr("retry_count")
    try:
        table.update_item(
            Key={"job_id": job_id},
            ConditionExpression=status.eq("PENDING") | (
                status.eq("PROCESSING") & (retry.not_exists() | retry.lt(retry_count))
            ),
            UpdateExpression="SET #status = :processing, retry_count = :retry, updated_at = :now",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":processing": "PROCESSING",
                ":retry": retry_count,
                ":now": datetime.now(UTC).isoformat(),
            },
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return

    try:
        _summarize_job(message, job_id, retry_count, table, s3, bedrock)
    except Exception as exc:
        if retry_count + 1 >= max_receive_count:
            reason = exc.response["Error"]["Code"] if isinstance(exc, ClientError) else type(exc).__name__
            _finish_job(table, job_id, retry_count, "FAILED", "error", f"Retries exhausted: {reason}")
        raise


def _summarize_job(message, job_id, retry_count, table, s3, bedrock):
    source = s3.get_object(Bucket=os.environ["UPLOAD_BUCKET"], Key=message["s3_key"])
    with closing(source["Body"]) as body:
        document = body.read().decode("utf-8")

    try:
        response = bedrock.converse(
            modelId=os.environ["BEDROCK_MODEL_ID"],
            system=[{"text": SYSTEM_PROMPTS[message["tone"]]}],
            messages=[{"role": "user", "content": [{"text": document}]}],
            inferenceConfig={"maxTokens": 1024, "temperature": 0.3},
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in RETRYABLE_ERRORS:
            raise
        _finish_job(table, job_id, retry_count, "FAILED", "error", code)
        return

    summary = "\n".join(
        block["text"] for block in response["output"]["message"]["content"] if "text" in block
    ).strip()
    if not summary:
        _finish_job(table, job_id, retry_count, "FAILED", "error", "Bedrock returned no summary")
        return
    result_key = f"summaries/{job_id}.txt"
    s3.put_object(
        Bucket=os.environ["RESULTS_BUCKET"],
        Key=result_key,
        Body=summary.encode("utf-8"),
        ContentType="text/plain; charset=utf-8",
    )
    _finish_job(table, job_id, retry_count, "DONE", "result_key", result_key)


def _finish_job(table, job_id, retry_count, status, field, value):
    try:
        table.update_item(
            Key={"job_id": job_id},
            ConditionExpression=Attr("status").eq("PROCESSING") & Attr("retry_count").eq(retry_count),
            UpdateExpression="SET #status = :status, #field = :value, updated_at = :now",
            ExpressionAttributeNames={"#status": "status", "#field": field},
            ExpressionAttributeValues={
                ":status": status,
                ":value": value,
                ":now": datetime.now(UTC).isoformat(),
            },
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return
