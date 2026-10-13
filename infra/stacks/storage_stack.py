"""FreightStorage: buckets, tables, the ingest queue, and the runtime switches in SSM.

Table schemas mirror ``freight.persistence.tables`` (tests check they agree).
"""

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_notifications as s3n
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from infra.stacks.common import PARAM_PREFIX, DemoConfig

S = ddb.AttributeType.STRING

SWITCHES = {
    "llm-enabled": "true",
    "email-mode": "ses",
    "chaos/provider-failure-rate": "0",
    "chaos/provider-latency-ms": "0",
    "chaos/price-drift-pct": "0",
    "chaos/provider-down": "none",
}


class StorageStack(Stack):
    def __init__(self, scope: Construct, cid: str, config: DemoConfig, **kw: object) -> None:
        super().__init__(scope, cid, **kw)  # type: ignore[arg-type]

        self.raw_bucket = s3.Bucket(
            self,
            "Raw",
            bucket_name=f"freight-raw-{self.account}-{self.region}",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(90))],
        )

        pk_sk = {
            "partition_key": ddb.Attribute(name="PK", type=S),
            "sort_key": ddb.Attribute(name="SK", type=S),
            "billing_mode": ddb.BillingMode.PAY_PER_REQUEST,
            "removal_policy": RemovalPolicy.DESTROY,
        }
        self.table = ddb.Table(
            self,
            "Freight",
            table_name="freight",
            stream=ddb.StreamViewType.NEW_IMAGE,
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            **pk_sk,  # type: ignore[arg-type]
        )
        self.table.add_global_secondary_index(
            index_name="GSI1",
            partition_key=ddb.Attribute(name="GSI1PK", type=S),
            sort_key=ddb.Attribute(name="GSI1SK", type=S),
        )
        self.table.add_global_secondary_index(
            index_name="GSI2",
            partition_key=ddb.Attribute(name="GSI2PK", type=S),
            sort_key=ddb.Attribute(name="GSI2SK", type=S),
        )
        self.cache = ddb.Table(
            self,
            "Cache",
            table_name="freight-cache",
            time_to_live_attribute="ttl",
            **pk_sk,  # type: ignore[arg-type]
        )
        self.cache.add_global_secondary_index(
            index_name="group-index", partition_key=ddb.Attribute(name="group", type=S)
        )
        self.idempotency = ddb.Table(
            self,
            "Idempotency",
            table_name="freight-idempotency",
            partition_key=ddb.Attribute(name="id", type=S),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expiration",
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.mock = ddb.Table(
            self,
            "Mock",
            table_name="freight-mock",
            time_to_live_attribute="ttl",
            **pk_sk,  # type: ignore[arg-type]
        )

        self.ingest_dlq = sqs.Queue(
            self,
            "IngestDlq",
            queue_name="freight-ingest-dlq",
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )
        self.ingest_queue = sqs.Queue(
            self,
            "IngestQueue",
            queue_name="freight-ingest",
            visibility_timeout=Duration.seconds(180),  # 6x the ingest Lambda timeout
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=self.ingest_dlq),
            enforce_ssl=True,
        )
        # Exactly the prefix SES would write to: simulated and real inbound share one path.
        self.raw_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.SqsDestination(self.ingest_queue),
            s3.NotificationKeyFilter(prefix="inbound/"),
        )

        for name, value in SWITCHES.items():
            ssm.StringParameter(
                self,
                f"Param-{name.replace('/', '-')}",
                parameter_name=f"{PARAM_PREFIX}/{name}",
                string_value=value,
            )

        CfnOutput(self, "RawBucketName", value=self.raw_bucket.bucket_name)
        CfnOutput(self, "SenderEmail", value=config.sender_email)
        CfnOutput(self, "CustomerEmail", value=config.customer_email)
        CfnOutput(self, "RepEmail", value=config.rep_email)
