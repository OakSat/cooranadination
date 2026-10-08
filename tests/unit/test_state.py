from collections import deque

import pytest

from freight.domain.state import (
    TERMINAL,
    TRANSITIONS,
    InvalidTransition,
    OrderState,
    assert_transition,
)


def reachable(start: OrderState, removed: frozenset[OrderState] = frozenset()) -> set[OrderState]:
    seen, queue = {start}, deque([start])
    while queue:
        for nxt in TRANSITIONS[queue.popleft()]:
            if nxt not in seen and nxt not in removed:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def test_every_state_has_an_entry() -> None:
    assert set(TRANSITIONS) == set(OrderState)


def test_terminal_states() -> None:
    assert {OrderState.COMPLETED, OrderState.CANCELLED} == TERMINAL


def test_happy_path_is_legal() -> None:
    path = [
        OrderState.RECEIVED,
        OrderState.EXTRACTING,
        OrderState.AWAITING_CUSTOMER_INFO,
        OrderState.EXTRACTING,
        OrderState.READY_FOR_PLANNING,
        OrderState.QUOTING,
        OrderState.PENDING_REP_REVIEW,
        OrderState.QUOTE_SENT,
        OrderState.CUSTOMER_APPROVED,
        OrderState.REVALIDATING,
        OrderState.BOOKING,
        OrderState.BOOKED,
        OrderState.LAST_MILE_BOOKED,
        OrderState.COMPLETED,
    ]
    for a, b in zip(path, path[1:], strict=False):
        assert_transition(a, b)


def test_cannot_skip_to_booking() -> None:
    with pytest.raises(InvalidTransition):
        assert_transition(OrderState.QUOTE_SENT, OrderState.BOOKING)


def test_booking_requires_customer_approval() -> None:
    # Safety invariant: no path from intake to BOOKING avoids CUSTOMER_APPROVED.
    without_approval = reachable(OrderState.RECEIVED, frozenset({OrderState.CUSTOMER_APPROVED}))
    assert OrderState.BOOKING not in without_approval


def test_every_state_can_reach_a_terminal_state() -> None:
    for state in OrderState:
        assert reachable(state) & TERMINAL, state
