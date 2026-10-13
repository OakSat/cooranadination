"""The spec's follow-up question, live (DEMO_SETUP_GUIDE.md, E3).

A quote has been sent. Then, at the same time: the same SES event arrives again (twice), the
provider's price moves 12%, and the customer clicks the same option twice. Expected:
1 execution, no duplicate quote round or email, the stale price rejected at revalidation, a
price-update quote (round 2), and 0 bookings until the customer approves the new price.

    uv run scripts/chaos_race.py                  # makes a fresh order first (~2 min)
    uv run scripts/chaos_race.py --order FC-...   # an order at QUOTE_SENT
    uv run scripts/chaos_race.py --approve-new    # then approve round 2: exactly 1 booking set
    uv run scripts/chaos_race.py --reset          # price drift back to 0
"""

import argparse
import sys
import threading
import uuid

import _common as c
from demo_e2e import click, links, run_to_quote, sent_email


def executions_named(order_id: str) -> int:
    import boto3

    sfn = boto3.client("stepfunctions", region_name=c.REGION)
    arn = c.require_outputs("StateMachineArn")["StateMachineArn"]
    pages = sfn.get_paginator("list_executions").paginate(stateMachineArn=arn)
    return sum(e["name"] == order_id for page in pages for e in page["executions"])


def reupload_first_email(order_id: str) -> None:
    """The duplicate SES event: the very same bytes land in inbound/ again."""
    import boto3

    from freight.persistence.repo import Repo

    first = Repo("freight").emails(order_id)[0]
    s3 = boto3.client("s3", region_name=c.REGION)
    bucket = c.outputs()["RawBucketName"]
    raw = s3.get_object(Bucket=bucket, Key=first["s3Key"])["Body"].read()

    def put() -> None:
        key = f"inbound/simulated/duplicate-{uuid.uuid4().hex}.eml"
        s3.put_object(Bucket=bucket, Key=key, Body=raw, ContentType="message/rfc822")

    threads = [threading.Thread(target=put) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--order")
    p.add_argument("--approve-new", action="store_true")
    p.add_argument("--reset", action="store_true")
    p.add_argument("--drift", type=float, default=12)
    args = p.parse_args()
    if not c.aws_available():
        print("no AWS credentials (aws sso login --profile freight)")
        return 2
    c.use_deployed_stack()
    if args.reset:
        c.set_param("chaos/price-drift-pct", "0")
        c.ok("price drift reset to 0")
        return 0

    order_id = args.order or run_to_quote(interactive=False)
    c.wait_for_state(order_id, "QUOTE_SENT", timeout=10)
    quote = sent_email(order_id, "quote")
    url = next((u for line, u in links(quote).items() if "Ausgewogen" in line), None)
    url = url or next(iter(links(quote).values()))
    events_before = len(c.summary(order_id)["events"])

    print(f"\nRace on {order_id}:")
    c.set_param("chaos/price-drift-pct", str(args.drift))
    c.info(f"provider price drift set to {args.drift}%")
    reupload_first_email(order_id)
    c.info("same inbound email uploaded twice more (duplicate SES events)")
    outcomes: list[str] = []
    clickers = [threading.Thread(target=lambda: outcomes.append(click(url))) for _ in range(2)]
    for t in clickers:
        t.start()
    for t in clickers:
        t.join()
    c.info(f"customer clicked twice: {outcomes}")

    try:
        c.poll(
            lambda: c.summary(order_id)["quote_rounds"] >= 2 or None,
            180,
            what="the re-quote after revalidation",
        )
        c.wait_for_state(order_id, "QUOTE_SENT", "PENDING_REP_REVIEW", timeout=120)
    finally:
        c.set_param("chaos/price-drift-pct", "0")
    s = c.summary(order_id)
    new_events = s["events"][events_before:]
    per_key = [(p, r) for p, r, _ in s["emails"]]
    checks = {
        "one execution": executions_named(order_id) == 1,
        "duplicate email ignored (no extra round)": s["quote_rounds"] == 2,
        "one email per purpose and round": len(per_key) == len(set(per_key)),
        "stale price rejected": "PRICE_CHANGED_REVALIDATION_FAILED" in new_events,
        "nothing booked at the stale price": s["bookings"] == [],
    }
    if args.approve_new:
        round2 = sent_email(order_id, "quote", 2)
        new_url = next(iter(links(round2).values()))
        c.info(f"customer approves the new price: {click(new_url)}")
        c.wait_for_state(order_id, "COMPLETED", "NEEDS_HUMAN", timeout=300)
        s = c.summary(order_id)
        checks["exactly one booking set"] = s["bookings"] == ["main", "truck"]
    for name, good in checks.items():
        (c.ok if good else c.bad)(name)
    print(f"    emails={s['emails']}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
