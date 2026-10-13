"""Which order does an inbound email belong to?

Rules in priority order (PLAN.md §2.1). Each one is weaker than the one above it, and when the
weakest signal is ambiguous the email goes to a human. We never guess.
1. In-Reply-To / References matches the Message-ID of an email we stored or sent.
2. A reference token like [FC-7Q2K9D] anywhere in the subject or body (every outbound email
   carries one).
3. The sender has open orders: exactly one -> match it, if the email reads like a reply or
   that order is waiting on the customer. Several -> word-overlap similarity with a clear
   winner, else AMBIGUOUS.
4. Otherwise it is a new order.
"""

import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from freight.domain.state import OrderState
from freight.email.parse import ParsedEmail

REFERENCE_TOKEN = re.compile(r"\bFC-([0-9A-Z]{6})\b")
WAITING_ON_CUSTOMER = frozenset({OrderState.AWAITING_CUSTOMER_INFO, OrderState.QUOTE_SENT})
MIN_SIMILARITY = 0.2
MIN_MARGIN = 0.1


class MatchRule(StrEnum):
    IN_REPLY_TO = "in_reply_to"
    REFERENCE_TOKEN = "reference_token"
    SENDER_SINGLE_OPEN = "sender_single_open"
    SENDER_SIMILARITY = "sender_similarity"
    AMBIGUOUS = "ambiguous"
    NEW = "new"


class OpenOrder(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str
    state: OrderState
    created_at: datetime
    context: str = ""  # subject and first email text, for similarity


class ThreadMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule: MatchRule
    order_id: str | None = None
    candidates: list[str] = Field(default_factory=list)


class ThreadLookup(Protocol):
    def order_for_message_id(self, message_id: str) -> str | None: ...

    def order_exists(self, order_id: str) -> bool: ...

    def open_orders_for_sender(self, address: str) -> list[OpenOrder]: ...


def reference_tokens(text: str) -> list[str]:
    return [f"FC-{m}" for m in REFERENCE_TOKEN.findall(text.upper())]


_WORD = re.compile(r"[a-z0-9]{3,}")
_STOP = frozenset(
    {
        *("the", "and", "for", "with", "our", "you", "your", "hello", "thanks"),
        *("bitte", "und", "der", "die", "das", "wir", "ihr", "hallo", "danke"),
        *("para", "los", "las", "con", "que", "una", "hola", "gracias"),
        *("corona", "beer", "bier", "cerveza"),
    }
)


def _words(text: str) -> set[str]:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return {w for w in _WORD.findall(folded) if w not in _STOP}


def similarity(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def resolve(email: ParsedEmail, lookup: ThreadLookup) -> ThreadMatch:
    for mid in [email.in_reply_to, *reversed(email.references)]:
        if mid and (order_id := lookup.order_for_message_id(mid)):
            return ThreadMatch(rule=MatchRule.IN_REPLY_TO, order_id=order_id)

    tokens = dict.fromkeys(reference_tokens(f"{email.subject}\n{email.text}"))
    known = [t for t in tokens if lookup.order_exists(t)]
    if len(known) == 1:
        return ThreadMatch(rule=MatchRule.REFERENCE_TOKEN, order_id=known[0])
    if len(known) > 1:
        return ThreadMatch(rule=MatchRule.AMBIGUOUS, candidates=known)

    open_orders = lookup.open_orders_for_sender(email.from_addr)
    if len(open_orders) == 1:
        only = open_orders[0]
        if email.looks_like_reply or only.state in WAITING_ON_CUSTOMER:
            return ThreadMatch(rule=MatchRule.SENDER_SINGLE_OPEN, order_id=only.order_id)
        return ThreadMatch(rule=MatchRule.NEW)
    if len(open_orders) > 1:
        if not email.looks_like_reply:
            return ThreadMatch(rule=MatchRule.NEW)
        text = f"{email.subject}\n{email.new_text}"
        scored = sorted(
            ((similarity(text, o.context), o.order_id) for o in open_orders), reverse=True
        )
        best, runner_up = scored[0][0], scored[1][0]
        if best >= MIN_SIMILARITY and best - runner_up >= MIN_MARGIN:
            return ThreadMatch(rule=MatchRule.SENDER_SIMILARITY, order_id=scored[0][1])
        return ThreadMatch(rule=MatchRule.AMBIGUOUS, candidates=[o.order_id for o in open_orders])

    return ThreadMatch(rule=MatchRule.NEW)
