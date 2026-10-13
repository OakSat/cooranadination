"""Transactional outbox: an email is first a row, written in the same transaction as the state
change that caused it; a separate sender delivers it.

The key is deterministic in (order, purpose, round), so a retried step writes the *same* row
(and the conditional put fails harmlessly) instead of a second email.
"""

import hashlib
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Purpose(StrEnum):
    QUESTIONS = "questions"
    QUOTE = "quote"
    CONFIRMATION = "confirmation"
    HANDOFF = "handoff"  # "a representative will get back to you"


class OutboxStatus(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    SENT = "SENT"
    FAILED = "FAILED"


def outbox_key(order_id: str, purpose: Purpose, round_: int) -> str:
    return hashlib.sha256(f"{order_id}|{purpose.value}|{round_}".encode()).hexdigest()[:24]


class OutboundEmail(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject: str
    body_text: str


class OutboxMessage(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    order_id: str
    customer_id: str
    purpose: Purpose
    round: int
    to: list[str]
    subject: str
    body_text: str
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    status: OutboxStatus = OutboxStatus.PENDING
    ses_message_id: str | None = None
    created_at: datetime
    sent_at: datetime | None = None

    @classmethod
    def create(
        cls,
        *,
        order_id: str,
        customer_id: str,
        purpose: Purpose,
        round_: int,
        to: list[str],
        email: OutboundEmail,
        now: datetime,
        in_reply_to: str | None = None,
        references: list[str] | None = None,
    ) -> "OutboxMessage":
        return cls(
            key=outbox_key(order_id, purpose, round_),
            order_id=order_id,
            customer_id=customer_id,
            purpose=purpose,
            round=round_,
            to=to,
            subject=email.subject,
            body_text=email.body_text,
            in_reply_to=in_reply_to,
            references=references or [],
            created_at=now,
        )
