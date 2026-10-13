"""Step Functions task handlers: freight-extract, -workflow, -quote, -book and -wait all point
here; the ``step`` field in the payload picks the function (see workflow/graph.py).

Exceptions propagate on purpose: their class name (StaleOrder, ProviderUnavailable) is the
error name Step Functions matches in Retry, and anything else is caught to Escalate.
"""

from collections.abc import Callable
from typing import Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

from freight.observability.logging import configure_logging
from freight.observability.metrics import powertools_metrics
from freight.workflow import steps
from freight.workflow.deps import Deps, aws_deps

logger = Logger(service="freight")
metrics = powertools_metrics()
configure_logging(logger)

Step = Callable[[Deps, dict[str, Any]], dict[str, Any]]

STEPS: dict[str, Step] = {
    "extract": lambda d, e: steps.extract_step(d, e["orderId"]),
    "plan": lambda d, e: steps.plan_step(d, e["orderId"]),
    "quote": lambda d, e: steps.quote_step(d, e["orderId"], int(e["round"]), e["provider"]),
    "rank": lambda d, e: steps.rank_step(d, e["orderId"], int(e["round"])),
    "send_quote": lambda d, e: steps.send_quote_step(d, e["orderId"]),
    "revalidate": lambda d, e: steps.revalidate_step(d, e["orderId"]),
    "book_main": lambda d, e: steps.book_main_step(d, e["orderId"]),
    "book_truck": lambda d, e: steps.book_truck_step(d, e["orderId"]),
    "complete": lambda d, e: steps.complete_step(d, e["orderId"]),
    "cancel": lambda d, e: steps.cancel_step(d, e["orderId"]),
    "escalate": lambda d, e: steps.escalate_step(d, e["orderId"], str(e.get("error", ""))),
    "resume_from_human": lambda d, e: steps.resume_from_human_step(d, e["orderId"], e["action"]),
    "wait": lambda d, e: steps.wait_for(d, e["orderId"], e["token"], e["waitingFor"]),
}


def dispatch(deps: Deps, event: dict[str, Any]) -> dict[str, Any]:
    return STEPS[event["step"]](deps, event)


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    logger.append_keys(order_id=event.get("orderId"), step=event.get("step"))
    result = dispatch(aws_deps(), event)
    logger.info("step done", extra={"result": result})
    return result
