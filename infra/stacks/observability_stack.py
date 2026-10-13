"""FreightObservability: five alarms to one email topic, and the ``Freight`` dashboard.

Kept small on purpose: about $0.10 per alarm and $3 per dashboard per month.
"""

from aws_cdk import Duration, Stack
from aws_cdk import aws_cloudwatch as cw
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from constructs import Construct

from infra.stacks.common import DemoConfig
from infra.stacks.storage_stack import StorageStack
from infra.stacks.workflow_stack import WorkflowStack

NS = "Freight"
DAILY_LLM_BUDGET_USD = 2


def metric(name: str, stat: str = "Sum", period: Duration | None = None) -> cw.Metric:
    return cw.Metric(
        namespace=NS,
        metric_name=name,
        dimensions_map={"service": "freight"},
        statistic=stat,
        period=period or Duration.minutes(5),
    )


class ObservabilityStack(Stack):
    def __init__(
        self,
        scope: Construct,
        cid: str,
        storage: StorageStack,
        workflow: WorkflowStack,
        config: DemoConfig,
        **kw: object,
    ) -> None:
        super().__init__(scope, cid, **kw)  # type: ignore[arg-type]
        topic = sns.Topic(self, "Alarms", topic_name="freight-alarms", enforce_ssl=True)
        topic.add_subscription(subs.EmailSubscription(config.alarm_email))
        action = cw_actions.SnsAction(topic)

        dlq_depth = cw.MathExpression(
            expression="ingest + outbox",
            using_metrics={
                "ingest": storage.ingest_dlq.metric_approximate_number_of_messages_visible(),
                "outbox": workflow.outbox_dlq.metric_approximate_number_of_messages_visible(),
            },
            label="Messages in DLQs",
            period=Duration.minutes(5),
        )
        lambda_errors = cw.Metric(
            namespace="AWS/Lambda",
            metric_name="Errors",
            statistic="Sum",
            period=Duration.minutes(5),
        )
        alarms = [
            cw.Alarm(
                self,
                "DlqNotEmpty",
                alarm_name="freight-dlq-not-empty",
                metric=dlq_depth,
                threshold=0,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
            ),
            cw.Alarm(
                self,
                "ExecutionsFailed",
                alarm_name="freight-executions-failed",
                metric=workflow.state_machine.metric_failed(period=Duration.minutes(5)),
                threshold=0,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            ),
            cw.Alarm(
                self,
                "LambdaErrors",
                alarm_name="freight-lambda-errors",
                metric=lambda_errors,
                threshold=3,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            ),
            cw.Alarm(
                self,
                "LlmCost",
                alarm_name="freight-llm-cost-per-day",
                metric=metric("LLMCostUSD", period=Duration.days(1)),
                threshold=DAILY_LLM_BUDGET_USD,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            ),
            cw.Alarm(
                self,
                "CircuitOpen",
                alarm_name="freight-provider-circuit-open",
                metric=metric("ProviderCircuitOpen"),
                threshold=0,
                evaluation_periods=1,
                comparison_operator=cw.ComparisonOperator.GREATER_THAN_THRESHOLD,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            ),
        ]
        for alarm in alarms:
            alarm.add_alarm_action(action)

        hour = Duration.hours(1)
        dash = cw.Dashboard(self, "Dashboard", dashboard_name="Freight")
        dash.add_widgets(
            cw.TextWidget(
                markdown="# Freight demo\nManual baseline: **5-7 business days** to book. "
                "Time-to-first-quote is in minutes, time-to-booking in hours; both include "
                "time spent waiting on the customer and the rep.",
                width=24,
                height=2,
            )
        )
        dash.add_widgets(
            cw.GraphWidget(
                title="Time to first quote (minutes)",
                left=[
                    metric("TimeToFirstQuote", "Average", hour),
                    metric("TimeToFirstQuote", "p90", hour),
                ],
            ),
            cw.GraphWidget(
                title="Time to booking (hours)",
                left=[
                    metric("TimeToBooking", "Average", hour),
                    metric("TimeToBooking", "p90", hour),
                ],
            ),
            cw.GraphWidget(
                title="Human review rate",
                left=[metric("HumanReviewRate", "Average", hour)],
                left_y_axis=cw.YAxisProps(min=0, max=1),
            ),
        )
        dash.add_widgets(
            cw.GraphWidget(
                title="LLM tokens",
                left=[
                    metric("LLMInputTokens", period=hour),
                    metric("LLMOutputTokens", period=hour),
                ],
            ),
            cw.SingleValueWidget(
                title="LLM cost today (USD)",
                metrics=[metric("LLMCostUSD", period=Duration.days(1))],
            ),
            cw.GraphWidget(
                title="Extraction latency (ms)",
                left=[metric("ExtractionLatency", "Average"), metric("ExtractionLatency", "p90")],
            ),
        )
        dash.add_widgets(
            cw.GraphWidget(
                title="Extraction quality signals",
                left=[
                    metric("FieldsMissingAtFirstPass", "Average", hour),
                    metric("ClarificationRounds", period=hour),
                    metric("RepEdits", period=hour),
                ],
            ),
            cw.GraphWidget(
                title="Providers",
                left=[metric("ProviderErrors"), metric("ProviderCircuitOpen")],
            ),
            cw.GraphWidget(
                title="Orders (Step Functions)",
                left=[
                    workflow.state_machine.metric_started(),
                    workflow.state_machine.metric_succeeded(),
                    workflow.state_machine.metric_failed(),
                ],
            ),
            cw.GraphWidget(title="Dead-letter queues", left=[dlq_depth]),
            cw.GraphWidget(title="Duplicate deliveries absorbed", left=[metric("DuplicateEvents")]),
        )
