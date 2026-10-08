"""Order lifecycle: the business source of truth for which transitions are legal.

Step Functions decides *what runs next*; this table decides *what is allowed*. The repository
applies a transition only with a conditional write on (state, version), so a stray or duplicate
Lambda cannot move an order along an illegal edge -- in particular, nothing reaches BOOKING
without passing through CUSTOMER_APPROVED.
"""

from enum import StrEnum


class OrderState(StrEnum):
    RECEIVED = "RECEIVED"
    EXTRACTING = "EXTRACTING"
    AWAITING_CUSTOMER_INFO = "AWAITING_CUSTOMER_INFO"
    READY_FOR_PLANNING = "READY_FOR_PLANNING"
    QUOTING = "QUOTING"
    PENDING_REP_REVIEW = "PENDING_REP_REVIEW"
    QUOTE_SENT = "QUOTE_SENT"
    QUOTE_EXPIRED = "QUOTE_EXPIRED"
    CUSTOMER_APPROVED = "CUSTOMER_APPROVED"
    REVALIDATING = "REVALIDATING"
    BOOKING = "BOOKING"
    BOOKED = "BOOKED"
    LAST_MILE_BOOKED = "LAST_MILE_BOOKED"
    COMPLETED = "COMPLETED"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


S = OrderState

TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    S.RECEIVED: frozenset({S.EXTRACTING, S.NEEDS_HUMAN, S.CANCELLED}),
    S.EXTRACTING: frozenset(
        {S.AWAITING_CUSTOMER_INFO, S.READY_FOR_PLANNING, S.NEEDS_HUMAN, S.FAILED}
    ),
    S.AWAITING_CUSTOMER_INFO: frozenset({S.EXTRACTING, S.NEEDS_HUMAN, S.CANCELLED}),
    S.READY_FOR_PLANNING: frozenset({S.QUOTING, S.NEEDS_HUMAN, S.FAILED}),
    S.QUOTING: frozenset({S.PENDING_REP_REVIEW, S.QUOTE_SENT, S.NEEDS_HUMAN, S.FAILED}),
    # A rep edit creates a new extraction version and re-plans.
    S.PENDING_REP_REVIEW: frozenset({S.QUOTE_SENT, S.READY_FOR_PLANNING, S.CANCELLED}),
    # The customer may also come back with changed requirements instead of choosing.
    S.QUOTE_SENT: frozenset({S.CUSTOMER_APPROVED, S.QUOTE_EXPIRED, S.EXTRACTING, S.CANCELLED}),
    S.QUOTE_EXPIRED: frozenset({S.QUOTING, S.CANCELLED}),
    S.CUSTOMER_APPROVED: frozenset({S.REVALIDATING}),
    # Price or capacity moved since the quote: re-quote instead of booking.
    S.REVALIDATING: frozenset({S.BOOKING, S.QUOTE_EXPIRED, S.NEEDS_HUMAN}),
    S.BOOKING: frozenset({S.BOOKED, S.NEEDS_HUMAN, S.FAILED}),
    S.BOOKED: frozenset({S.LAST_MILE_BOOKED, S.NEEDS_HUMAN}),
    S.LAST_MILE_BOOKED: frozenset({S.COMPLETED}),
    S.NEEDS_HUMAN: frozenset(
        {S.EXTRACTING, S.READY_FOR_PLANNING, S.QUOTING, S.CANCELLED, S.FAILED}
    ),
    S.FAILED: frozenset({S.NEEDS_HUMAN}),
    S.COMPLETED: frozenset(),
    S.CANCELLED: frozenset(),
}

TERMINAL: frozenset[OrderState] = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)


class InvalidTransition(Exception):
    def __init__(self, current: OrderState, target: OrderState) -> None:
        super().__init__(f"illegal transition {current} -> {target}")
        self.current = current
        self.target = target


def can_transition(current: OrderState, target: OrderState) -> bool:
    return target in TRANSITIONS[current]


def assert_transition(current: OrderState, target: OrderState) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)
