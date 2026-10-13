"""Inbound email: S3 object -> stored email -> new order execution, or input for a waiting one.

Duplicate deliveries are harmless at every layer: Powertools idempotency on the dedupe key (in
the handler), a deterministic order id and write-once email rows here, and a Step Functions
execution named after the order id.
"""

import contextlib
import hashlib
import logging
from typing import Any

from freight.domain.state import TERMINAL, OrderState
from freight.email.compose import detect_language
from freight.email.parse import ParsedEmail, parse_raw
from freight.email.threading import MatchRule, OpenOrder, resolve
from freight.persistence import keys
from freight.persistence.repo import AlreadyExists, OrderMeta, Repo, StaleOrder
from freight.workflow.deps import Deps
from freight.workflow.steps import resume

log = logging.getLogger(__name__)
S = OrderState

# Where a customer email simply wakes the workflow up (it will re-extract).
RESUMES_WORKFLOW = frozenset(
    {S.RECEIVED, S.EXTRACTING, S.AWAITING_CUSTOMER_INFO, S.READY_FOR_PLANNING, S.QUOTING}
)


def order_id_for(dedupe_key: str) -> str:
    """Deterministic: two workers ingesting the same email compute the same order id."""
    alphabet = keys.ORDER_ALPHABET
    n = int(hashlib.sha256(dedupe_key.encode()).hexdigest(), 16)
    chars = []
    for _ in range(6):
        n, r = divmod(n, len(alphabet))
        chars.append(alphabet[r])
    return "FC-" + "".join(chars)


class _Lookup:
    def __init__(self, repo: Repo, customer_id: str | None) -> None:
        self.repo = repo
        self.customer_id = customer_id

    def order_for_message_id(self, message_id: str) -> str | None:
        return self.repo.order_for_message_id(message_id)

    def order_exists(self, order_id: str) -> bool:
        return self.repo.order_exists(order_id)

    def open_orders_for_sender(self, address: str) -> list[OpenOrder]:
        if self.customer_id is None:
            return []
        return self.repo.open_orders_for_customer(self.customer_id)


def _customer(repo: Repo, email: ParsedEmail) -> tuple[str, str, bool]:
    """(customer_id, display name, known). Unknown senders still get an order, which the
    first-order policy sends to a rep before any quote goes out."""
    profile = repo.customer_for_address(email.from_addr)
    if profile:
        return profile["customerId"], profile.get("name", ""), True
    digest = hashlib.sha256(email.from_addr.encode()).hexdigest()[:12]
    return f"unknown-{digest}", email.from_name, False


def _email_item(
    deps: Deps, order_id: str, customer_id: str, email: ParsedEmail, s3_key: str
) -> dict[str, Any]:
    attachments = []
    for i, att in enumerate(email.attachments):
        att_key = f"attachments/{email.dedupe_key[:16]}/{i}-{att.filename}"
        deps.raw.put(att_key, att.data, att.content_type)
        attachments.append(
            {
                "filename": att.filename,
                "contentType": att.content_type,
                "size": att.size,
                "s3Key": att_key,
                "text": att.text,
            }
        )
    email_id = f"in-{email.dedupe_key[:16]}"
    return {
        "PK": keys.order_pk(order_id),
        "SK": keys.email_sk(email.sent_at, email_id),
        "type": "email",
        "orderId": order_id,
        "customerId": customer_id,
        "emailId": email_id,
        "direction": "inbound",
        "messageId": email.message_id,
        "from": email.from_addr,
        "subject": email.subject,
        "sentAt": email.sent_at.isoformat(),
        "text": email.new_text,
        "fullTextHash": hashlib.sha256(email.text.encode()).hexdigest(),
        "s3Key": s3_key,
        "dedupeKey": email.dedupe_key,
        "attachments": attachments,
    }


def ingest_raw(deps: Deps, raw: bytes, s3_key: str) -> dict[str, Any]:
    email = parse_raw(raw)
    repo = deps.repo
    if email.message_id and repo.order_for_message_id(email.message_id):
        # This exact email is already stored (SES retry, S3 re-upload, SQS redelivery).
        deps.metrics.add("DuplicateEvents", 1)
        return {"duplicate": True, "orderId": repo.order_for_message_id(email.message_id)}
    customer_id, name, known = _customer(repo, email)
    match = resolve(email, _Lookup(repo, customer_id if known else None))
    log.info("thread match for %s: %s %s", email.message_id, match.rule, match.order_id)

    if match.rule in (MatchRule.NEW, MatchRule.AMBIGUOUS):
        order_id = order_id_for(email.dedupe_key)
        route = "human" if match.rule is MatchRule.AMBIGUOUS else "extract"
        try:
            repo.create_order(
                order_id=order_id,
                customer_id=customer_id,
                customer_email=email.from_addr,
                customer_name=name,
                email_item=_email_item(deps, order_id, customer_id, email, s3_key),
                message_id=email.message_id,
                subject=email.subject,
                first_email_text=email.new_text,
                now=deps.now(),
                language=detect_language(email.new_text),
            )
        except (StaleOrder, AlreadyExists):
            deps.metrics.add("DuplicateEvents", 1)
            log.info("order %s already exists: duplicate delivery", order_id)
        if route == "human":
            meta = repo.get_meta(order_id)
            if meta.state is S.RECEIVED:
                repo.transition(
                    meta,
                    S.NEEDS_HUMAN,
                    reason=f"ambiguous thread, candidates {match.candidates}",
                    step=f"ambiguous:{order_id}",
                )
        started = deps.workflow.start(order_id, {"orderId": order_id, "route": route})
        if not started:
            deps.metrics.add("DuplicateEvents", 1)
        return {"orderId": order_id, "rule": match.rule.value, "started": started}

    assert match.order_id is not None
    order_id = match.order_id
    meta = repo.get_meta(order_id)
    if meta.customer_id != customer_id and known:
        # Tenant isolation: a known customer cannot write into someone else's order, even if
        # they quote its reference token.
        log.warning("customer %s referenced foreign order %s", customer_id, order_id)
        return ingest_as_new(deps, email, raw, s3_key, customer_id, name)
    return attach_to_order(deps, meta, email, s3_key, match.rule.value)


def ingest_as_new(
    deps: Deps, email: ParsedEmail, raw: bytes, s3_key: str, customer_id: str, name: str
) -> dict[str, Any]:
    order_id = order_id_for(email.dedupe_key)
    with contextlib.suppress(StaleOrder, AlreadyExists):  # already created by a duplicate
        deps.repo.create_order(
            order_id=order_id,
            customer_id=customer_id,
            customer_email=email.from_addr,
            customer_name=name,
            email_item=_email_item(deps, order_id, customer_id, email, s3_key),
            message_id="",
            subject=email.subject,
            first_email_text=email.new_text,
            now=deps.now(),
            language=detect_language(email.new_text),
        )
    deps.workflow.start(order_id, {"orderId": order_id, "route": "human"})
    return {"orderId": order_id, "rule": "foreign_reference"}


def attach_to_order(
    deps: Deps, meta: OrderMeta, email: ParsedEmail, s3_key: str, rule: str
) -> dict[str, Any]:
    repo = deps.repo
    state = meta.state
    late = state in TERMINAL or state in (
        S.CUSTOMER_APPROVED,
        S.REVALIDATING,
        S.BOOKING,
        S.BOOKED,
        S.LAST_MILE_BOOKED,
    )
    kind = "LATE_REPLY" if late else "CUSTOMER_EMAIL"
    stored = repo.add_email(
        meta.order_id,
        meta.customer_id,
        _email_item(deps, meta.order_id, meta.customer_id, email, s3_key),
        email.message_id,
        kind,
        {"rule": rule, "state": state.value, "subject": email.subject},
        now=deps.now(),
    )
    if not stored:
        deps.metrics.add("DuplicateEvents", 1)
        return {"orderId": meta.order_id, "rule": rule, "duplicate": True}
    resumed = False
    if state in RESUMES_WORKFLOW:
        resumed = resume(deps, meta.order_id, {"party": "customer", "decision": "reply"})
    # QUOTE_SENT: free text is not an approval. The rep sees the email and can re-extract.
    # PENDING_REP_REVIEW / NEEDS_HUMAN: the rep sees it with the order.
    return {"orderId": meta.order_id, "rule": rule, "event": kind, "resumed": resumed}
