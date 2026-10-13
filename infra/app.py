"""CDK app: four stacks in us-east-1, checked by cdk-nag's AwsSolutions pack.

    cd infra && uv run cdk synth
    cd infra && uv run cdk deploy --all -c senderEmail=you@gmail.com -c customerEmail=... \\
        -c repEmail=... -c alarmEmail=... -c bedrockModelId=...

Every accepted cdk-nag finding is suppressed below with its reason; anything else fails synth.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

import aws_cdk as cdk  # noqa: E402
from aws_cdk import aws_lambda as lambda_  # noqa: E402
from cdk_nag import AwsSolutionsChecks, NagPackSuppression, NagSuppressions  # noqa: E402

from infra.build import build_lambda_bundle  # noqa: E402
from infra.stacks.api_stack import ApiStack  # noqa: E402
from infra.stacks.common import DemoConfig  # noqa: E402
from infra.stacks.observability_stack import ObservabilityStack  # noqa: E402
from infra.stacks.storage_stack import StorageStack  # noqa: E402
from infra.stacks.workflow_stack import WorkflowStack  # noqa: E402

DEFAULT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# Accepted findings, with the reason a reviewer would want to read.
SUPPRESSIONS = [
    ("AwsSolutions-IAM4", "AWSLambdaBasicExecutionRole only grants writing the function's logs."),
    (
        "AwsSolutions-IAM5",
        "Wildcards are scoped: table indexes (CDK grants), one S3 prefix, /freight/demo/* "
        "parameters, SES identities in this account, anthropic.claude-* Bedrock models, and "
        "X-Ray (which has no resource-level permissions).",
    ),
    ("AwsSolutions-L1", "Pinned to Python 3.12 to match the project and the bundled wheels."),
    ("AwsSolutions-S1", "Demo: S3 server access logs would cost more than the data they protect."),
    ("AwsSolutions-SQS3", "These queues are the dead-letter queues."),
    (
        "AwsSolutions-SNS2",
        "CloudWatch alarms cannot publish to a topic using the AWS-managed SNS key.",
    ),
    (
        "AwsSolutions-COG1",
        "Password policy is 12+ chars with digits and symbols; uppercase optional.",
    ),
    ("AwsSolutions-COG2", "Demo with one rep user; enable MFA before real use."),
    ("AwsSolutions-COG3", "Cognito threat protection is a paid feature; demo only."),
    ("AwsSolutions-COG8", "The Plus feature plan is paid; the Essentials plan is enough here."),
    (
        "AwsSolutions-DDB3",
        "Only the freight table holds business data (PITR on); cache, idempotency and mock "
        "tables are disposable by design.",
    ),
    (
        "AwsSolutions-APIG1",
        "HTTP API access logs not enabled; Lambda logs carry the request context.",
    ),
    (
        "AwsSolutions-APIG4",
        "Public by design: /choose uses signed, expiring tokens; /mock stands in for third-party "
        "providers; /dashboard serves only static files. /rep/* uses the Cognito JWT authorizer.",
    ),
]


def config(app: cdk.App) -> DemoConfig:
    def ctx(key: str, default: str) -> str:
        return str(app.node.try_get_context(key) or default)

    sender = ctx("senderEmail", "sender@example.com")
    return DemoConfig(
        sender_email=sender,
        customer_email=ctx("customerEmail", "customer@example.com"),
        rep_email=ctx("repEmail", "rep@example.com"),
        alarm_email=ctx("alarmEmail", sender),
        bedrock_model_id=ctx("bedrockModelId", DEFAULT_MODEL),
        llm_concurrency=int(ctx("llmConcurrency", "5")),
    )


def main() -> None:
    # The CDK CLI sets CDK_OUTDIR; `python app.py` (verify script, tests) writes to infra/cdk.out.
    app = cdk.App(outdir=os.environ.get("CDK_OUTDIR", str(ROOT / "infra" / "cdk.out")))
    env = cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region="us-east-1")
    cfg = config(app)
    bundle = str(build_lambda_bundle())

    def code() -> lambda_.Code:  # CDK wants one Code object per stack; same files, same hash
        return lambda_.Code.from_asset(bundle)

    storage = StorageStack(app, "FreightStorage", cfg, env=env)
    api = ApiStack(app, "FreightApi", storage, code(), cfg, env=env)
    workflow = WorkflowStack(app, "FreightWorkflow", storage, api, code(), cfg, env=env)
    observability = ObservabilityStack(app, "FreightObservability", storage, workflow, cfg, env=env)

    cdk.Tags.of(app).add("project", "freight-demo")
    cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
    for stack in (storage, api, workflow, observability):
        NagSuppressions.add_stack_suppressions(
            stack, [NagPackSuppression(id=i, reason=r) for i, r in SUPPRESSIONS], True
        )
    app.synth()


if __name__ == "__main__":
    main()
