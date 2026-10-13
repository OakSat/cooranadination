"""Helpers shared by the scripts: stack outputs, settings for running app code against the
deployed stack from your laptop, and small polling/printing utilities."""

import os
import sys
import time
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REGION = "us-east-1"
STACKS = ("FreightStorage", "FreightApi", "FreightWorkflow", "FreightObservability")
PARAM_PREFIX = "/freight/demo"


def ok(msg: str) -> None:
    print(f"  ✔ {msg}")


def bad(msg: str) -> None:
    print(f"  ✘ {msg}")


def info(msg: str) -> None:
    print(f"  • {msg}")


def aws_available() -> bool:
    try:
        import boto3

        boto3.client("sts", region_name=REGION).get_caller_identity()
    except Exception:
        return False
    return True


@cache
def outputs() -> dict[str, str]:
    """All CloudFormation outputs of the Freight stacks, by output key."""
    import boto3

    cfn = boto3.client("cloudformation", region_name=REGION)
    out: dict[str, str] = {}
    for stack in STACKS:
        try:
            desc = cfn.describe_stacks(StackName=stack)["Stacks"][0]
        except cfn.exceptions.ClientError:
            continue
        for o in desc.get("Outputs", []):
            out[o["OutputKey"]] = o["OutputValue"]
    return out


def require_outputs(*keys: str) -> dict[str, str]:
    out = outputs()
    missing = [k for k in keys if k not in out]
    if missing:
        sys.exit(f"stack outputs missing: {missing}. Deploy first (DEMO_SETUP_GUIDE.md, Part D).")
    return out


def use_deployed_stack() -> None:
    """Point freight.config at the deployed resources so app code (repo, actions) can run here."""
    out = require_outputs("RawBucketName", "ApiUrl", "StateMachineArn")
    os.environ.update(
        AWS_REGION=REGION,
        AWS_DEFAULT_REGION=REGION,
        TABLE_NAME="freight",
        CACHE_TABLE="freight-cache",
        IDEMPOTENCY_TABLE="freight-idempotency",
        MOCK_TABLE="freight-mock",
        RAW_BUCKET=out["RawBucketName"],
        STATE_MACHINE_ARN=out["StateMachineArn"],
        API_BASE_URL=out["ApiUrl"],
        SENDER_EMAIL=out.get("SenderEmail", ""),
        PARAM_PREFIX=PARAM_PREFIX,
    )


def poll[T](
    fn: Callable[[], T | None], timeout: float = 180, every: float = 3, what: str = ""
) -> T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(every)
    raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}")


def wait_for_state(order_id: str, *states: str, timeout: float = 240) -> str:
    from freight.persistence.repo import OrderNotFound, Repo

    repo = Repo("freight")

    def check() -> str | None:
        try:
            state = repo.get_meta(order_id).state.value
        except OrderNotFound:
            return None
        return state if state in states else None

    return poll(check, timeout, what=f"{order_id} to reach {'/'.join(states)}")


def set_param(name: str, value: str) -> None:
    import boto3

    boto3.client("ssm", region_name=REGION).put_parameter(
        Name=f"{PARAM_PREFIX}/{name}", Value=value, Type="String", Overwrite=True
    )


def summary(order_id: str) -> dict[str, Any]:
    from freight.persistence.repo import Repo

    repo = Repo("freight")
    outbox = repo.outbox(order_id)
    return {
        "state": repo.get_meta(order_id).state.value,
        "quote_rounds": repo.get_meta(order_id).quote_round,
        "emails": sorted((o["purpose"], o["round"], o["status"]) for o in outbox),
        "bookings": sorted(b["kind"] for b in repo.bookings(order_id)),
        "events": [e["kind"] for e in repo.events(order_id)],
    }
