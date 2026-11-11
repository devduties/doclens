import json
import logging
import os
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


logger = logging.getLogger(__name__)
PUBLIC_FIELDS = ("job_id", "status", "tone", "created_at", "updated_at")


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": json.dumps(body),
    }


def lambda_handler(event, context):
    raw_job_id = (event.get("pathParameters") or {}).get("job_id")
    if not isinstance(raw_job_id, str):
        return _response(400, {"error": "invalid job_id"})
    try:
        job_id = UUID(raw_job_id)
    except ValueError:
        return _response(400, {"error": "invalid job_id"})
    if job_id.version != 4:
        return _response(400, {"error": "invalid job_id"})

    try:
        table = boto3.resource("dynamodb").Table(os.environ["JOBS_TABLE"])
        item = table.get_item(
            Key={"job_id": str(job_id)},
            ConsistentRead=True,
            ProjectionExpression="job_id, #status, tone, created_at, updated_at, result_key",
            ExpressionAttributeNames={"#status": "status"},
        ).get("Item")
        if item is None:
            return _response(404, {"error": "not found"})

        body = {field: item[field] for field in PUBLIC_FIELDS}
        if item["status"] == "DONE" and item.get("result_key"):
            s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
            body["result_url"] = s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": os.environ["RESULTS_BUCKET"], "Key": item["result_key"]},
                ExpiresIn=3600,
            )
    except (ClientError, BotoCoreError):
        logger.exception("Could not read status for job %s", job_id)
        return _response(503, {"error": "service unavailable"})

    return _response(200, body)
