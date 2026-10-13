"""Did we get from "a week" to "hours"? (Topic 14)

Reads every order's audit trail and reports, for orders that got that far:
  received -> first quote sent, and received -> booked (p50 / p90),
  split into system time vs waiting on the customer vs waiting on a rep,
against the manual baseline of 5-7 business days. Also lists outbox rows stuck in SENDING
(the documented at-most-once gap in workflow/outbox_sender.py).

    uv run scripts/report_kpis.py
"""

import argparse
import statistics
import sys
from datetime import datetime, timedelta
from typing import Any

import _common as c

BASELINE = "5-7 business days"
WAITING = {
    "AWAITING_CUSTOMER_INFO": "customer",
    "QUOTE_SENT": "customer",
    "PENDING_REP_REVIEW": "rep",
    "NEEDS_HUMAN": "rep",
}


def all_orders() -> list[str]:
    import boto3

    db = boto3.client("dynamodb", region_name=c.REGION)
    ids: list[str] = []
    for page in db.get_paginator("scan").paginate(
        TableName="freight",
        FilterExpression="SK = :m",
        ExpressionAttributeValues={":m": {"S": "META"}},
        ProjectionExpression="orderId",
    ):
        ids += [i["orderId"]["S"] for i in page["Items"]]
    return ids


def timeline(events: list[dict[str, Any]]) -> list[tuple[datetime, str]]:
    changes = [e for e in events if e["kind"] == "STATE_CHANGED"]
    return sorted((datetime.fromisoformat(e["at"]), e["detail"]["to"]) for e in changes)


def split(
    created: datetime, points: list[tuple[datetime, str]], until: datetime
) -> dict[str, float]:
    """Hours spent in system / customer / rep buckets between creation and ``until``."""
    out = {"system": 0.0, "customer": 0.0, "rep": 0.0}
    prev_t, prev_state = created, "RECEIVED"
    for t, state in [*points, (until, "END")]:
        t = min(t, until)
        out[WAITING.get(prev_state, "system")] += (t - prev_t).total_seconds() / 3600
        prev_t, prev_state = t, state
        if t >= until:
            break
    return out


def pct(values: list[float], q: float) -> float:
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(q * 100) - 1]


def fmt(hours: float) -> str:
    return f"{hours * 60:.0f} min" if hours < 2 else f"{hours:.1f} h"


def main() -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    if not c.aws_available():
        print("no AWS credentials")
        return 2
    c.use_deployed_stack()
    from freight.persistence.repo import Repo

    repo = Repo("freight")
    to_quote: list[dict[str, float]] = []
    to_book: list[dict[str, float]] = []
    stuck: list[str] = []
    orders = all_orders()
    for order_id in orders:
        meta = repo.get_meta(order_id)
        points = timeline(repo.events(order_id))
        for target, bucket in (("QUOTE_SENT", to_quote), ("COMPLETED", to_book)):
            reached = next((t for t, s in points if s == target), None)
            if reached:
                parts = split(meta.created_at, points, reached)
                bucket.append({**parts, "total": sum(parts.values())})
        for o in repo.outbox(order_id):
            created = datetime.fromisoformat(o["created_at"])
            if o["status"] == "SENDING" and datetime.now(created.tzinfo) - created > timedelta(
                minutes=5
            ):
                stuck.append(f"{order_id} {o['purpose']} round {o['round']}")

    print(f"Orders: {len(orders)} (manual baseline: {BASELINE} to book)\n")
    for label, rows in (("received -> first quote", to_quote), ("received -> booked", to_book)):
        if not rows:
            print(f"{label}: no orders yet")
            continue
        totals = sorted(r["total"] for r in rows)
        print(f"{label} (n={len(rows)}): p50 {fmt(pct(totals, 0.5))}, p90 {fmt(pct(totals, 0.9))}")
        for part in ("system", "customer", "rep"):
            print(f"    {part:9s} p50 {fmt(statistics.median(r[part] for r in rows))}")
    if stuck:
        print("\nOutbox rows stuck in SENDING (check SES sending statistics, then resend by hand):")
        for s in stuck:
            print(f"    {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
