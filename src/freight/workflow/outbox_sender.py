"""Deliver outbox rows through SES. Triggered by the DynamoDB stream on new PENDING rows.

PENDING -> SENDING is a conditional claim, so a duplicate stream record or a retry never sends
twice. The remaining gap: if SES accepted the email and the Lambda died before writing SENT,
the row stays SENDING and is *not* resent (at-most-once beats a duplicate quote). The
OUTBOX_STUCK check in ``report_kpis.py`` lists such rows for a rep.
"""

import logging
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from botocore.exceptions import ClientError

from freight.workflow.deps import Deps

log = logging.getLogger(__name__)
SIMULATOR = "success@simulator.amazonses.com"


def build_message(deps: Deps, row: dict[str, object], to: list[str]) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr(("Corona Export Logistics", deps.sender_email))
    msg["To"] = ", ".join(to)
    msg["Subject"] = str(row["subject"])
    msg["Message-ID"] = make_msgid(domain="freight.local")
    if row.get("in_reply_to"):
        msg["In-Reply-To"] = str(row["in_reply_to"])
    refs = row.get("references") or []
    if isinstance(refs, list) and refs:
        msg["References"] = " ".join(str(r) for r in refs)
    msg.set_content(str(row["body_text"]))
    return msg


def send_outbox(deps: Deps, order_id: str, key: str) -> str:
    repo = deps.repo
    row = repo.get_outbox(order_id, key)
    if row is None or row.get("status") != "PENDING":
        return "skipped"
    if not repo.claim_outbox(order_id, key):
        return "skipped"  # another invocation owns it

    mode = deps.params.get("email-mode", "ses")
    to = [SIMULATOR] if mode == "simulator" else list(row["to"])
    msg = build_message(deps, row, to)
    try:
        message_id = f"log-{key}" if mode == "log" else deps.mailer.send(msg)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("MessageRejected", "MailFromDomainNotVerifiedException", "BadRequestException"):
            repo.mark_outbox_failed(order_id, key, code)
            repo.record_event(order_id, row["customerId"], "EMAIL_REJECTED", {"code": code})
            return "failed"
        repo.release_outbox(order_id, key)  # transient: let the stream retry
        raise
    except Exception:
        repo.release_outbox(order_id, key)
        raise

    repo.mark_outbox_sent(order_id, key, message_id)
    # SES rewrites Message-ID to <id@email.amazonses.com>; replies reference that one.
    repo.index_message(f"<{message_id}@email.amazonses.com>", order_id, row["customerId"])
    repo.index_message(str(msg["Message-ID"]), order_id, row["customerId"])
    repo.record_event(
        order_id,
        row["customerId"],
        "EMAIL_SENT",
        {
            "purpose": row["purpose"],
            "round": row["round"],
            "sesMessageId": message_id,
            "mode": mode,
        },
    )
    return "sent"
