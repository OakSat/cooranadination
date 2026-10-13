"""Business and cost metrics (CloudWatch EMF, namespace ``Freight``).

About ten metrics, no high-cardinality dimensions, so the CloudWatch bill stays at a few
dollars a month (PLAN.md §2.11).
"""

from aws_lambda_powertools import Metrics
from aws_lambda_powertools.metrics import MetricUnit

NAMESPACE = "Freight"
METRIC_NAMES = (
    "LLMInputTokens",
    "LLMOutputTokens",
    "LLMCostUSD",
    "ExtractionLatency",
    "FieldsMissingAtFirstPass",
    "ClarificationRounds",
    "ProviderErrors",
    "ProviderCircuitOpen",
    "TimeToFirstQuote",
    "TimeToBooking",
    "HumanReviewRate",
    "DuplicateEvents",
    "RepEdits",
)

_metrics = Metrics(namespace=NAMESPACE, service="freight")


def powertools_metrics() -> Metrics:
    """The singleton the Lambda handlers flush with ``@log_metrics``."""
    return _metrics


class PowertoolsMetrics:
    def add(self, name: str, value: float, unit: str = "Count") -> None:
        _metrics.add_metric(name=name, unit=MetricUnit(unit), value=value)
