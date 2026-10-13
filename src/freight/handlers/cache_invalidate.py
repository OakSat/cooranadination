"""EventBridge ``PriceChanged`` (from a provider) -> drop that lane's cached quotes.

TTL alone would serve a stale price for up to 10 minutes; this closes the gap for estimates.
Bookings never depend on it: revalidation always asks the provider live.
"""

from typing import Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

from freight.observability.logging import configure_logging
from freight.providers.cache import event_group
from freight.workflow.deps import aws_deps

logger = Logger(service="freight")
configure_logging(logger)


def invalidate(detail: dict[str, Any]) -> int:
    group = event_group(detail["provider"], detail["lane"])
    return aws_deps().cache.invalidate_group(group)


@logger.inject_lambda_context(log_event=False)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    deleted = invalidate(event["detail"])
    logger.info("cache invalidated", extra={"detail": event["detail"], "deleted": deleted})
    return {"deleted": deleted}
