"""FreightWorkflow: the per-order Step Functions state machine and the Lambdas around it.

The state machine definition is rendered from ``freight.workflow.graph`` (the same graph the
local end-to-end tests interpret).
"""

import json

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sqs as sqs
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

from freight.workflow.graph import asl
from infra.stacks.api_stack import TASK_RESPONSE, ApiStack
from infra.stacks.common import (
    PARAM_PREFIX,
    STATE_MACHINE_NAME,
    DemoConfig,
    function,
    state_machine_arn,
)
from infra.stacks.storage_stack import StorageStack

WORKFLOW_HANDLER = "freight.handlers.workflow.handler"


class WorkflowStack(Stack):
    def __init__(
        self,
        scope: Construct,
        cid: str,
        storage: StorageStack,
        api: ApiStack,
        code: lambda_.Code,
        config: DemoConfig,
        **kw: object,
    ) -> None:
        super().__init__(scope, cid, **kw)  # type: ignore[arg-type]
        sm_arn = state_machine_arn(self)
        env = {
            "TABLE_NAME": storage.table.table_name,
            "CACHE_TABLE": storage.cache.table_name,
            "IDEMPOTENCY_TABLE": storage.idempotency.table_name,
            "RAW_BUCKET": storage.raw_bucket.bucket_name,
            "STATE_MACHINE_ARN": sm_arn,
            "API_BASE_URL": api.api_url,
            "SENDER_EMAIL": config.sender_email,
            "REP_EMAIL": config.rep_email,
            "BEDROCK_MODEL_ID": config.bedrock_model_id,
            "LLM_PROVIDER": "bedrock",
        }

        fns = {
            "ingest": function(
                self,
                "ingest",
                "freight.handlers.ingest.handler",
                code,
                env,
                timeout=30,
            ),
            "extract": function(
                self,
                "extract",
                WORKFLOW_HANDLER,
                code,
                env,
                memory=512,
                timeout=90,
                reserved=config.llm_concurrency or None,  # cost guard on the Bedrock caller
            ),
            "workflow": function(self, "workflow", WORKFLOW_HANDLER, code, env),
            "quote": function(self, "quote", WORKFLOW_HANDLER, code, env, timeout=60),
            "book": function(self, "book", WORKFLOW_HANDLER, code, env, timeout=60),
            "wait": function(self, "wait", WORKFLOW_HANDLER, code, env, timeout=15),
            "send-outbox": function(
                self,
                "send-outbox",
                "freight.handlers.send_outbox.handler",
                code,
                env,
            ),
            "cache-invalidate": function(
                self, "cache-invalidate", "freight.handlers.cache_invalidate.handler", code, env
            ),
        }
        self.functions = fns

        # --- permissions: each function gets only what it touches -----------------------
        params_arn = f"arn:aws:ssm:{self.region}:{self.account}:parameter{PARAM_PREFIX}/*"
        for fn in fns.values():
            fn.add_to_role_policy(
                iam.PolicyStatement(actions=["ssm:GetParameter"], resources=[params_arn])
            )
            storage.table.grant_read_write_data(fn)
        for name in ("extract", "quote", "book", "workflow", "cache-invalidate"):
            storage.cache.grant_read_write_data(fns[name])
        storage.idempotency.grant_read_write_data(fns["ingest"])
        storage.raw_bucket.grant_read(fns["ingest"], "inbound/*")
        storage.raw_bucket.grant_put(fns["ingest"], "attachments/*")
        fns["extract"].add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
                    f"arn:aws:bedrock:*:{self.account}:inference-profile/*anthropic.claude-*",
                ],
            )
        )
        fns["send-outbox"].add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendEmail", "ses:SendRawEmail"],
                resources=[f"arn:aws:ses:{self.region}:{self.account}:identity/*"],
            )
        )
        for name in ("ingest", "wait", "extract", "workflow", "book"):
            fns[name].add_to_role_policy(
                iam.PolicyStatement(actions=TASK_RESPONSE, resources=[sm_arn])
            )
        fns["ingest"].add_to_role_policy(
            iam.PolicyStatement(actions=["states:StartExecution"], resources=[sm_arn])
        )

        # --- triggers ---------------------------------------------------------------------
        fns["ingest"].add_event_source(
            sources.SqsEventSource(
                storage.ingest_queue, batch_size=5, report_batch_item_failures=True
            )
        )
        self.outbox_dlq = sqs.Queue(
            self,
            "OutboxDlq",
            queue_name="freight-outbox-dlq",
            retention_period=Duration.days(14),
            enforce_ssl=True,
        )
        fns["send-outbox"].add_event_source(
            sources.DynamoEventSource(
                storage.table,
                starting_position=lambda_.StartingPosition.LATEST,
                batch_size=10,
                retry_attempts=3,
                bisect_batch_on_error=True,
                report_batch_item_failures=True,
                on_failure=sources.SqsDlq(self.outbox_dlq),
                filters=[
                    lambda_.FilterCriteria.filter(
                        {
                            "eventName": lambda_.FilterRule.is_equal("INSERT"),
                            "dynamodb": {
                                "NewImage": {"SK": {"S": lambda_.FilterRule.begins_with("OUTBOX#")}}
                            },
                        }
                    )
                ],
            )
        )
        events.Rule(
            self,
            "PriceChanged",
            rule_name="freight-price-changed",
            event_pattern=events.EventPattern(source=["freight.app"], detail_type=["PriceChanged"]),
            targets=[targets.LambdaFunction(fns["cache-invalidate"])],
        )

        # --- state machine ----------------------------------------------------------------
        definition = asl(
            {
                "extract": fns["extract"].function_arn,
                "workflow": fns["workflow"].function_arn,
                "quote": fns["quote"].function_arn,
                "book": fns["book"].function_arn,
                "wait": fns["wait"].function_arn,
            }
        )
        log_group = logs.LogGroup(
            self,
            "StateMachineLogs",
            log_group_name="/aws/vendedlogs/states/freight-order",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.state_machine = sfn.StateMachine(
            self,
            "Order",
            state_machine_name=STATE_MACHINE_NAME,
            state_machine_type=sfn.StateMachineType.STANDARD,
            definition_body=sfn.DefinitionBody.from_string(json.dumps(definition)),
            tracing_enabled=True,
            logs=sfn.LogOptions(
                destination=log_group, level=sfn.LogLevel.ALL, include_execution_data=False
            ),
        )
        for name in ("extract", "workflow", "quote", "book", "wait"):
            fns[name].grant_invoke(self.state_machine)

        CfnOutput(self, "StateMachineArn", value=self.state_machine.state_machine_arn)
        CfnOutput(self, "OutboxDlqName", value=self.outbox_dlq.queue_name)
