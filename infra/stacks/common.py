"""Shared Lambda settings: arm64, Python 3.12, 7-day logs, X-Ray, one shared code bundle."""

from dataclasses import dataclass

from aws_cdk import Duration, RemovalPolicy, Stack
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct

PARAM_PREFIX = "/freight/demo"
STATE_MACHINE_NAME = "freight-order"


@dataclass(frozen=True)
class DemoConfig:
    sender_email: str
    customer_email: str
    rep_email: str
    alarm_email: str
    bedrock_model_id: str
    # Reserved concurrency for the Bedrock caller (cost guard). 0 = no reservation, needed on
    # new accounts whose Lambda concurrency limit is 10 (AWS keeps 10 unreserved).
    llm_concurrency: int = 5


def state_machine_arn(stack: Stack) -> str:
    """Built by name so stacks that resume executions don't depend on the workflow stack."""
    return f"arn:aws:states:{stack.region}:{stack.account}:stateMachine:{STATE_MACHINE_NAME}"


def function(
    scope: Construct,
    name: str,
    handler: str,
    code: lambda_.Code,
    env: dict[str, str],
    *,
    memory: int = 256,
    timeout: int = 30,
    reserved: int | None = None,
) -> lambda_.Function:
    log_group = logs.LogGroup(
        scope,
        f"{name}-logs",
        log_group_name=f"/aws/lambda/freight-{name}",
        retention=logs.RetentionDays.ONE_WEEK,
        removal_policy=RemovalPolicy.DESTROY,
    )
    return lambda_.Function(
        scope,
        name,
        function_name=f"freight-{name}",
        runtime=lambda_.Runtime.PYTHON_3_12,
        architecture=lambda_.Architecture.ARM_64,
        handler=handler,
        code=code,
        memory_size=memory,
        timeout=Duration.seconds(timeout),
        reserved_concurrent_executions=reserved,
        environment={
            "POWERTOOLS_SERVICE_NAME": "freight",
            "POWERTOOLS_METRICS_NAMESPACE": "Freight",
            "POWERTOOLS_LOG_LEVEL": "INFO",
            "PARAM_PREFIX": PARAM_PREFIX,
            **env,
        },
        tracing=lambda_.Tracing.ACTIVE,
        log_group=log_group,
    )
