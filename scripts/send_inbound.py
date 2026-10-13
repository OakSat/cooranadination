"""Simulate an inbound email: upload an .eml to the raw bucket's inbound/ prefix, exactly where an
SES receipt rule would write it.

    uv run scripts/send_inbound.py tests/fixtures/emails/de/01_missing_date_pallets.eml
    uv run scripts/send_inbound.py tests/fixtures/emails/de/02_reply_changed_subject.eml \\
        --order FC-7Q2K9D          # reply to our last email in that order (sets In-Reply-To)
    uv run scripts/send_inbound.py ... --fresh   # new Message-ID, so it is not a duplicate

The fixture's placeholder sender is replaced by the deployed CustomerEmail.
"""

import argparse
import re
import sys
import uuid
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from email.utils import format_datetime, make_msgid
from pathlib import Path

import _common as c

PLACEHOLDER = "customer@example.com"


def prepare(path: Path, customer: str, sender: str, order: str | None, fresh: bool) -> bytes:
    msg = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    name = re.sub(r"\s*<.*>", "", str(msg["From"]))
    del msg["From"], msg["To"]
    msg["From"] = f"{name} <{customer}>"
    msg["To"] = sender or "orders@example.com"
    if fresh:
        del msg["Message-ID"], msg["Date"]
        msg["Message-ID"] = make_msgid(domain="simulated.freight")
        msg["Date"] = format_datetime(datetime.now(UTC))
    if order:
        c.use_deployed_stack()
        from freight.persistence.repo import Repo

        sent = [o for o in Repo("freight").outbox(order) if o.get("ses_message_id")]
        if not sent:
            sys.exit(f"{order} has no sent email to reply to yet")
        last = max(sent, key=lambda o: o["created_at"])
        reply_to = f"<{last['ses_message_id']}@email.amazonses.com>"
        del msg["In-Reply-To"], msg["References"]
        msg["In-Reply-To"] = reply_to
        msg["References"] = reply_to
    return bytes(msg)


def upload(raw: bytes, label: str) -> str:
    import boto3

    bucket = c.require_outputs("RawBucketName")["RawBucketName"]
    key = f"inbound/simulated/{datetime.now(UTC):%Y/%m/%d}/{uuid.uuid4().hex}-{label}.eml"
    boto3.client("s3", region_name=c.REGION).put_object(
        Bucket=bucket, Key=key, Body=raw, ContentType="message/rfc822"
    )
    return key


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("eml", type=Path)
    p.add_argument("--order", help="reply to this order's last outbound email")
    p.add_argument("--fresh", action="store_true", help="new Message-ID and Date")
    args = p.parse_args()
    out = c.require_outputs("CustomerEmail", "RawBucketName")
    raw = prepare(
        args.eml, out["CustomerEmail"], out.get("SenderEmail", ""), args.order, args.fresh
    )
    key = upload(raw, args.eml.stem)
    c.ok(f"uploaded s3://{out['RawBucketName']}/{key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
