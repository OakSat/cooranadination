"""Is the account ready (Phase 0), and is the deployed demo healthy?

    uv run scripts/bootstrap_check.py              # everything
    uv run scripts/bootstrap_check.py --phase0     # prerequisites only (before cdk deploy)
    uv run scripts/bootstrap_check.py --sender you@gmail.com --customer you+bar@gmail.com \\
        --rep you+rep@gmail.com                    # SES identities, if not deployed yet

One real extraction runs on Bedrock (~$0.01). Exit code 0 only if every check passes.
"""

import argparse
import sys

import _common as c

BUDGET_NAME = "freight-demo-monthly"


def check_bedrock(model_id: str) -> bool:
    """One real extraction with the production prompt and schema, so a schema the model
    endpoint rejects shows up here and not halfway through a demo."""
    from datetime import date

    from freight.extraction import evaluation
    from freight.extraction.extractor import extract
    from freight.extraction.llm.bedrock import BedrockAnthropicClient

    case = next(
        x
        for x in evaluation.load_cases(c.ROOT / "tests/eval/golden.jsonl")
        if x["id"] == "de_complete"
    )
    try:
        result = extract(
            evaluation.thread_of(case),
            llm=BedrockAnthropicClient(model_id, c.REGION),
            today=date.fromisoformat(case["today"]),
        )
    except Exception as e:  # report any failure as a readable line
        c.bad(f"Bedrock extraction failed for {model_id}: {e}")
        return False
    qty = result.fresh.quantity.value
    good = qty is not None and qty.amount == 18
    shown = f"{qty.amount} {qty.unit.value}" if qty else "missing"
    (c.ok if good else c.bad)(
        f"Bedrock extraction OK (model {model_id}, quantity={shown}, "
        f"{result.latency_ms} ms, ${result.cost_usd:.4f})"
    )
    return good


def check_ses(addresses: list[str]) -> bool:
    import boto3

    ses = boto3.client("sesv2", region_name=c.REGION)
    verified = 0
    for a in addresses:
        try:
            status = ses.get_email_identity(EmailIdentity=a)["VerificationStatus"]
        except ses.exceptions.NotFoundException:
            status = "NOT_FOUND"
        if status == "SUCCESS":
            verified += 1
        else:
            c.bad(f"SES identity {a}: {status} (DEMO_SETUP_GUIDE.md B2)")
    sandbox = not ses.get_account().get("ProductionAccessEnabled", False)
    good = verified == len(addresses)
    (c.ok if good else c.bad)(
        f"SES identities verified: {verified}/{len(addresses)}, sandbox={sandbox}"
    )
    return good


def check_budget() -> bool:
    import boto3

    account = boto3.client("sts").get_caller_identity()["Account"]
    budgets = boto3.client("budgets", region_name=c.REGION)
    try:
        b = budgets.describe_budget(AccountId=account, BudgetName=BUDGET_NAME)["Budget"]
    except budgets.exceptions.NotFoundException:
        c.bad(f"Budget {BUDGET_NAME} not found (DEMO_SETUP_GUIDE.md A3)")
        return False
    c.ok(f"Budget {BUDGET_NAME} exists ({b['BudgetLimit']['Amount']} {b['BudgetLimit']['Unit']})")
    return True


def check_link_key() -> bool:
    import boto3

    ssm = boto3.client("ssm", region_name=c.REGION)
    try:
        ssm.get_parameter(Name=f"{c.PARAM_PREFIX}/link-hmac-key", WithDecryption=True)
    except ssm.exceptions.ParameterNotFound:
        c.bad(f"SSM {c.PARAM_PREFIX}/link-hmac-key missing: run scripts/seed.py")
        return False
    c.ok(f"SSM {c.PARAM_PREFIX}/link-hmac-key present")
    return True


def check_stacks() -> bool:
    import boto3

    cfn = boto3.client("cloudformation", region_name=c.REGION)
    found = []
    for stack in c.STACKS:
        status: str
        try:
            status = cfn.describe_stacks(StackName=stack)["Stacks"][0]["StackStatus"]
        except cfn.exceptions.ClientError:
            status = "MISSING"
        if status.endswith("_COMPLETE") and "ROLLBACK" not in status:
            found.append(stack)
        else:
            c.bad(f"stack {stack}: {status}")
    good = len(found) == len(c.STACKS)
    (c.ok if good else c.bad)(f"Stacks: {', '.join(found) or 'none'}")
    return good


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--phase0", action="store_true", help="prerequisites only")
    p.add_argument("--model", help="Bedrock model/inference profile id")
    p.add_argument("--sender")
    p.add_argument("--customer")
    p.add_argument("--rep")
    args = p.parse_args()

    if not c.aws_available():
        c.bad("no AWS credentials: run `aws sso login --profile freight` and export AWS_PROFILE")
        return 2
    out = c.outputs()
    addresses = [
        a
        for a in (
            args.sender or out.get("SenderEmail"),
            args.customer or out.get("CustomerEmail"),
            args.rep or out.get("RepEmail"),
        )
        if a
    ]
    model = args.model or "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    results = [check_bedrock(model), check_budget()]
    if addresses:
        results.append(check_ses(addresses))
    else:
        c.bad("no email addresses to check: pass --sender/--customer/--rep or deploy first")
        results.append(False)
    if not args.phase0:
        results += [check_stacks(), check_link_key()]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
