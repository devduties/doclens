import base64
import json
import logging
import os
from datetime import UTC, datetime
from uuid import uuid4

import boto3
from botocore.exceptions import BotoCoreError, ClientError


logger = logging.getLogger(__name__)
TONES = ("executive", "bullet_points", "eli5")


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def lambda_handler(event, context):
    body = event.get("body")
    if not isinstance(body, str):
        return _response(400, {"error": "body must be a JSON object"})

    try:
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body, validate=True).decode("utf-8")
        request = json.loads(body)
    except (ValueError, RecursionError):
        return _response(400, {"error": "body must be a JSON object"})

    if not isinstance(request, dict):
        return _response(400, {"error": "body must be a JSON object"})
    if request.keys() - {"s3_key", "tone"}:
        return _response(400, {"error": "unexpected fields"})

    s3_key = request.get("s3_key")
    if not isinstance(s3_key, str) or len(s3_key) > 1024 or not s3_key.startswith("uploads/"):
        return _response(400, {"error": "s3_key must start with uploads/ and be at most 1024 characters"})
    try:
        s3_key.encode("utf-8")
    except UnicodeEncodeError:
        return _response(400, {"error": "s3_key must be valid UTF-8"})
    tone = request.get("tone")
    if tone not in TONES:
        return _response(400, {"error": "invalid tone"})

    job_id = str(uuid4())
    now = datetime.now(UTC).isoformat()
    message = {"job_id": job_id, "s3_key": s3_key, "tone": tone}
    item = {**message, "status": "PENDING", "created_at": now, "updated_at": now}
    try:
        table = boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"])
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(job_id)")
        boto3.client("sqs").send_message(
            QueueUrl=os.environ["JOBS_QUEUE_URL"],
            MessageBody=json.dumps(message),
        )
    except (ClientError, BotoCoreError):
        logger.exception("Could not submit job %s", job_id)
        return _response(503, {"error": "service unavailable"})

    return _response(202, {"job_id": job_id, "status": "PENDING"})
