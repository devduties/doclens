from pathlib import Path

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from constructs import Construct


class DocLensStack(Stack):
    """The single stack for DocLens infrastructure."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.upload_bucket = s3.Bucket(
            self,
            "UploadBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        self.results_bucket = s3.Bucket(
            self,
            "ResultsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        self.jobs_table = dynamodb.Table(
            self,
            "JobsTable",
            table_name="doclens-jobs",
            partition_key=dynamodb.Attribute(
                name="job_id",
                type=dynamodb.AttributeType.STRING,
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.jobs_queue = sqs.Queue(
            self,
            "JobsQueue",
            encryption=sqs.QueueEncryption.KMS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
        )

        submit_logs = logs.LogGroup(
            self,
            "SubmitLogs",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        submit = lambda_.Function(
            self,
            "SubmitFunction",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(
                str(Path(__file__).resolve().parents[1] / "lambdas" / "submit"),
                exclude=["__pycache__", "*.pyc"],
            ),
            log_group=submit_logs,
            environment={
                "JOBS_TABLE": self.jobs_table.table_name,
                "JOBS_QUEUE_URL": self.jobs_queue.queue_url,
            },
        )
        submit.apply_removal_policy(RemovalPolicy.DESTROY)
        self.jobs_table.grant(submit, "dynamodb:PutItem")
        self.jobs_queue.grant_send_messages(submit)

        self.api = apigateway.RestApi(self, "DocLensApi", cloud_watch_role=False)
        self.api.apply_removal_policy(RemovalPolicy.DESTROY)
        self.jobs_resource = self.api.root.add_resource("jobs")
        self.jobs_resource.add_method(
            "POST",
            apigateway.LambdaIntegration(submit),
            authorization_type=apigateway.AuthorizationType.NONE,
        )
