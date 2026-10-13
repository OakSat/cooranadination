"""SQS (S3 ObjectCreated under inbound/) -> ingest each email once.

Idempotency is layered: Powertools idempotency on the email's dedupe key makes a duplicate
S3/SQS/SES delivery a no-op within the record's lifetime, and the deterministic order id and
write-once rows below it hold even if that record has expired.
"""

import json
from typing import Any
from urllib.parse import unquote_plus

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.batch import (
    BatchProcessor,
    EventType,
    process_partial_response,
)
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord
from aws_lambda_powertools.utilities.idempotency import (
    DynamoDBPersistenceLayer,
    IdempotencyConfig,
    idempotent_function,
)
from aws_lambda_powertools.utilities.typing import LambdaContext

from freight.config import settings
from freight.email.parse import parse_raw
from freight.observability.logging import configure_logging
from freight.observability.metrics import powertools_metrics
from freight.workflow.deps import aws_deps
from freight.workflow.ingest import ingest_raw

logger = Logger(service="freight")
metrics = powertools_metrics()
configure_logging(logger)
processor = BatchProcessor(event_type=EventType.SQS)
persistence = DynamoDBPersistenceLayer(table_name=settings().idempotency_table)
idempotency = IdempotencyConfig(
    event_key_jmespath="dedupe_key",
    expires_after_seconds=7 * 24 * 3600,
    raise_on_no_idempotency_key=True,
)


@idempotent_function(  # type: ignore[untyped-decorator]
    data_keyword_argument="email", persistence_store=persistence, config=idempotency
)
def process_email(email: dict[str, Any]) -> dict[str, Any]:
    deps = aws_deps()
    raw = deps.raw.get(email["s3_key"])
    return ingest_raw(deps, raw, email["s3_key"])


def record_handler(record: SQSRecord) -> None:
    body = json.loads(record.body)
    if body.get("Event") == "s3:TestEvent":
        return
    for s3_record in body.get("Records", []):
        key = unquote_plus(s3_record["s3"]["object"]["key"])
        if not key.startswith("inbound/"):
            continue
        raw = aws_deps().raw.get(key)
        dedupe_key = parse_raw(raw).dedupe_key
        result = process_email(email={"dedupe_key": dedupe_key, "s3_key": key})
        logger.info("ingested", extra={"s3_key": key, "result": result})


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> Any:
    idempotency.register_lambda_context(context)
    return process_partial_response(
        event=event, record_handler=record_handler, processor=processor, context=context
    )
