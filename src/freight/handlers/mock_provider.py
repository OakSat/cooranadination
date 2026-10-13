"""API Gateway (``/mock/*``) -> the mock freight providers. Chaos knobs come from SSM."""

import json
import os
from functools import cache
from typing import Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

from freight.config import Params, settings
from freight.observability.logging import configure_logging
from freight.providers.mock_app import Chaos, DynamoMockStore, MockProviders
from freight.workflow.ports import EventBridgePublisher

logger = Logger(service="freight-mock")
configure_logging(logger)


@cache
def app() -> MockProviders:
    s = settings()
    params = Params(s.param_prefix)
    events = EventBridgePublisher(s.event_bus)

    def chaos() -> Chaos:
        down = params.get("chaos/provider-down", "")
        return Chaos(
            failure_rate=params.number("chaos/provider-failure-rate", 0.0),
            latency_ms=int(params.number("chaos/provider-latency-ms", 0)),
            price_drift_pct=params.number("chaos/price-drift-pct", 0.0),
            down={p.strip() for p in down.split(",") if p.strip() and p.strip() != "none"},
        )

    return MockProviders(
        DynamoMockStore(s.mock_table),
        chaos,
        publish=lambda t, d: events.publish(t, d),
        validity_minutes=int(os.environ.get("QUOTE_VALIDITY_MINUTES", "60")),
    )


@logger.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    method = event["requestContext"]["http"]["method"]
    body = json.loads(event["body"]) if event.get("body") else {}
    resp = app().handle(method, event["rawPath"], event.get("headers") or {}, body)
    logger.info("mock", extra={"path": event["rawPath"], "status": resp.status})
    return {
        "statusCode": resp.status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(resp.body),
    }
