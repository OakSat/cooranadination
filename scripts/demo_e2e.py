"""The demo, end to end, against the deployed stack (DEMO_SETUP_GUIDE.md, Part E).

    uv run scripts/demo_e2e.py                    # interactive: pauses, you approve and click
    uv run scripts/demo_e2e.py --non-interactive  # asserts the outcome; used by verify.py

Costs a few cents (one or two Bedrock calls; the rest is free tier). Non-interactive mode passes
when the order reaches COMPLETED with exactly 1 quote round, 3 emails (questions, quote,
confirmation) and 2 bookings (main leg + German truck).
"""

import argparse
import re
import sys
from email import policy
from email.parser import BytesParser
from typing import Any

import _common as c
from send_inbound import prepare, upload

FIXTURES = c.ROOT / "tests" / "fixtures" / "emails"
LINK = re.compile(r"(https://\S+/choose\?t=\S+)")


def pause(interactive: bool, text: str) -> None:
    if interactive:
        input(f"\n>>> {text}\n    press Enter to continue ")


def order_for(raw: bytes) -> str:
    from freight.persistence.repo import Repo

    message_id = str(BytesParser(policy=policy.default).parsebytes(raw)["Message-ID"])
    order_id: str = c.poll(
        lambda: Repo("freight").order_for_message_id(message_id), 120, what="the order to appear"
    )
    return order_id


def sent_email(order_id: str, purpose: str, round_: int = 1) -> dict[str, Any]:
    from freight.persistence.repo import Repo

    def find() -> dict[str, Any] | None:
        for o in Repo("freight").outbox(order_id):
            if o["purpose"] == purpose and o["round"] == round_ and o["status"] == "SENT":
                row: dict[str, Any] = o
                return row
        return None

    return c.poll(find, 120, what=f"the {purpose} email (round {round_})")


def links(email: dict[str, Any]) -> dict[str, str]:
    """Choice links by their option line ("1) Günstigste: ...")."""
    out: dict[str, str] = {}
    label = ""
    for line in email["body_text"].splitlines():
        if re.match(r"^\d\) ", line):
            label = line
        if m := LINK.search(line):
            out[label] = m.group(1)
    return out


def click(url: str) -> str:
    """What a customer does: open the page (GET), then confirm (POST)."""
    import httpx

    page = httpx.get(url, timeout=30)
    page.raise_for_status()
    base, token = url.split("?t=", 1)
    result = httpx.post(base, data={"t": token}, timeout=30)
    match = re.search(r"<h1>(.*?)</h1>", result.text)
    return match.group(1) if match else str(result.status_code)


def run_to_quote(interactive: bool) -> str:
    out = c.require_outputs("DashboardUrl")
    print("\n1) A German bar emails a request that is missing the date and the pallet count.")
    raw = prepare(
        FIXTURES / "de/01_missing_date_pallets.eml",
        out["CustomerEmail"],
        out.get("SenderEmail", ""),
        None,
        fresh=True,
    )
    upload(raw, "01_request")
    order_id = order_for(raw)
    c.ok(f"order {order_id} created; Step Functions execution named {order_id}")
    c.wait_for_state(order_id, "AWAITING_CUSTOMER_INFO", "NEEDS_HUMAN")
    questions = sent_email(order_id, "questions")
    c.ok(f"questions sent: {questions['subject']}")
    print("    " + questions["body_text"].replace("\n", "\n    "))
    pause(interactive, "Check the customer inbox for the German questions email.")

    print("\n2) The customer replies with a new subject; the reply answers both questions.")
    reply = prepare(
        FIXTURES / "de/02_reply_changed_subject.eml",
        out["CustomerEmail"],
        out.get("SenderEmail", ""),
        order_id,
        fresh=True,
    )
    upload(reply, "02_reply")
    state = c.wait_for_state(order_id, "PENDING_REP_REVIEW", "QUOTE_SENT", "NEEDS_HUMAN")
    if state == "NEEDS_HUMAN":
        raise SystemExit(f"{order_id} went to NEEDS_HUMAN; see its audit trail in the dashboard")

    if state == "PENDING_REP_REVIEW":
        print("\n3) First order for this customer, so a rep reviews the quote before it goes out.")
        if interactive:
            pause(interactive, f"Open {out['DashboardUrl']}, open {order_id}, Approve & send.")
        else:
            c.use_deployed_stack()
            from freight.workflow.actions import rep_decide
            from freight.workflow.deps import aws_deps

            rep_decide(aws_deps(), order_id, "demo_e2e", "approve", "automated demo")
            c.ok("approved as rep (automated)")
        c.wait_for_state(order_id, "QUOTE_SENT")
    return order_id


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--non-interactive", action="store_true")
    p.add_argument("--email-mode", choices=["ses", "simulator", "log"])
    args = p.parse_args()
    interactive = not args.non_interactive
    if not c.aws_available():
        print("no AWS credentials (aws sso login --profile freight)")
        return 2
    c.use_deployed_stack()
    if args.email_mode:
        c.set_param("email-mode", args.email_mode)
        c.info(f"email-mode = {args.email_mode}")

    order_id = run_to_quote(interactive)
    quote = sent_email(order_id, "quote")
    options = links(quote)
    c.ok(f"quote sent with {len(options)} options")
    for line in options:
        print(f"    {line}")

    print("\n4) The customer picks the balanced option.")
    balanced = next((u for line, u in options.items() if "Ausgewogen" in line), None)
    balanced = balanced or next(iter(options.values()))
    if interactive:
        pause(interactive, f"Click 'Ausgewogen' in the email (or open {balanced}) and confirm.")
    else:
        c.ok(f"customer clicked: {click(balanced)}")

    print("\n5) Revalidate live price, book the main leg, book the truck, confirm.")
    c.wait_for_state(order_id, "COMPLETED", "NEEDS_HUMAN", "QUOTE_SENT", timeout=300)
    s = c.summary(order_id)
    print(f"    state={s['state']} quote_rounds={s['quote_rounds']} bookings={s['bookings']}")
    print(f"    emails={s['emails']}")

    expected_emails = [("confirmation", 1, "SENT"), ("questions", 1, "SENT"), ("quote", 1, "SENT")]
    checks = {
        "completed": s["state"] == "COMPLETED",
        "one quote round": s["quote_rounds"] == 1,
        "three emails": s["emails"] == expected_emails,
        "two bookings": s["bookings"] == ["main", "truck"],
    }
    for name, good in checks.items():
        (c.ok if good else c.bad)(name)
    print(
        f"\nOrder {order_id}: see the audit trail in the dashboard and the Freight CloudWatch "
        "dashboard for time-to-quote."
    )
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
