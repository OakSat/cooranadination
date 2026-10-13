"""DynamoDB stream -> deliver new PENDING outbox rows (the event-source filter in CDK only
passes INSERTs of OUTBOX# items, so this Lambda is not invoked for anything else)."""

from typing import Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.batch import (
    BatchProcessor,
    EventType,
    process_partial_response,
)
from aws_lambda_powertools.utilities.data_classes.dynamo_db_stream_event import (
    DynamoDBRecord,
)
from aws_lambda_powertools.utilities.typing import LambdaContext

from freight.observability.logging import configure_logging
from freight.observability.metrics import powertools_metrics
from freight.workflow.deps import aws_deps
from freight.workflow.outbox_sender import send_outbox

logger = Logger(service="freight")
metrics = powertools_metrics()
configure_logging(logger)
processor = BatchProcessor(event_type=EventType.DynamoDBStreams)


def record_handler(record: DynamoDBRecord) -> None:
    image = record.dynamodb.new_image if record.dynamodb else None
    if not image or not str(image.get("SK", "")).startswith("OUTBOX#"):
        return
    order_id, key = str(image["orderId"]), str(image["key"])
    outcome = send_outbox(aws_deps(), order_id, key)
    logger.info("outbox", extra={"order_id": order_id, "key": key, "outcome": outcome})


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> Any:
    return process_partial_response(
        event=event, record_handler=record_handler, processor=processor, context=context
    )
