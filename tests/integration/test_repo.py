import threading
from datetime import UTC, datetime

import pytest

from freight.domain.state import InvalidTransition, OrderState
from freight.email.outbox import OutboundEmail, OutboxMessage, Purpose
from freight.persistence import keys
from freight.persistence.repo import Put, Repo, StaleOrder, to_item
from freight.providers.cache import DynamoCache
from tests.integration.conftest import email_item

NOW = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)


def new_order(repo: Repo, order_id: str = "FC-AAAAAA") -> None:
    repo.create_order(
        order_id=order_id,
        customer_id="biergarten",
        customer_email="customer@example.com",
        customer_name="Biergarten Berlin GmbH",
        email_item=email_item(order_id),
        message_id=f"<m1-{order_id}@example.com>",
        subject="Anfrage",
        first_email_text="18 Paletten",
        now=NOW,
    )


def test_create_order_writes_meta_email_index_and_audit(repo: Repo) -> None:
    new_order(repo)
    meta = repo.get_meta("FC-AAAAAA")
    assert (meta.state, meta.version) == (OrderState.RECEIVED, 1)
    assert len(repo.emails("FC-AAAAAA")) == 1
    assert (
        repo.order_for_message_id("<M1-FC-AAAAAA@example.com>") == "FC-AAAAAA"
    )  # case-insensitive
    assert [e["kind"] for e in repo.events("FC-AAAAAA")] == ["ORDER_CREATED"]


def test_transition_bumps_version_and_audits(repo: Repo) -> None:
    new_order(repo)
    meta = repo.transition(
        repo.get_meta("FC-AAAAAA"),
        OrderState.EXTRACTING,
        reason="start",
        updates={"language": "de"},
    )
    assert (meta.state, meta.version, meta.language) == (OrderState.EXTRACTING, 2, "de")
    events = repo.events("FC-AAAAAA")
    assert events[-1]["detail"] == {"from": "RECEIVED", "to": "EXTRACTING", "reason": "start"}
    assert repo.orders_in_state(OrderState.EXTRACTING)[0].order_id == "FC-AAAAAA"


def test_illegal_transition_is_rejected_before_writing(repo: Repo) -> None:
    new_order(repo)
    with pytest.raises(InvalidTransition):
        repo.transition(repo.get_meta("FC-AAAAAA"), OrderState.BOOKING, reason="nope")
    assert repo.get_meta("FC-AAAAAA").version == 1


def test_stale_version_loses(repo: Repo) -> None:
    new_order(repo)
    stale = repo.get_meta("FC-AAAAAA")
    repo.transition(stale, OrderState.EXTRACTING, reason="first")
    with pytest.raises(StaleOrder):
        repo.transition(stale, OrderState.EXTRACTING, reason="second")


def test_wrong_customer_cannot_transition(repo: Repo) -> None:
    new_order(repo)
    other = repo.get_meta("FC-AAAAAA").model_copy(update={"customer_id": "someone-else"})
    with pytest.raises(StaleOrder):
        repo.transition(other, OrderState.EXTRACTING, reason="tenant check")


def test_retried_step_is_idempotent(repo: Repo) -> None:
    new_order(repo)
    meta = repo.get_meta("FC-AAAAAA")
    first = repo.transition(meta, OrderState.EXTRACTING, reason="x", step="extract:1")
    again = repo.transition(meta, OrderState.EXTRACTING, reason="x", step="extract:1")
    assert first.version == again.version == 2
    assert len(repo.events("FC-AAAAAA")) == 2  # created + one transition


def test_two_workers_race_exactly_one_wins(repo: Repo) -> None:
    """Follow-up question: two Lambdas pick up the same order at the same time."""
    new_order(repo)
    meta = repo.get_meta("FC-AAAAAA")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def worker(name: str) -> None:
        barrier.wait()
        try:
            repo.transition(meta, OrderState.EXTRACTING, reason=name)
            results.append("won")
        except StaleOrder:
            results.append("lost")

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["lost", "won"]
    assert repo.get_meta("FC-AAAAAA").version == 2


def outbox_msg(round_: int = 1) -> OutboxMessage:
    return OutboxMessage.create(
        order_id="FC-AAAAAA",
        customer_id="biergarten",
        purpose=Purpose.QUESTIONS,
        round_=round_,
        to=["customer@example.com"],
        email=OutboundEmail(subject="[FC-AAAAAA] Fragen", body_text="?"),
        now=NOW,
    )


def test_outbox_row_commits_with_the_transition(repo: Repo) -> None:
    new_order(repo)
    meta = repo.transition(repo.get_meta("FC-AAAAAA"), OrderState.EXTRACTING, reason="x")
    msg = outbox_msg()
    repo.transition(
        meta,
        OrderState.AWAITING_CUSTOMER_INFO,
        reason="ask",
        puts=[Put(item=Repo.outbox_item(msg))],
    )
    assert repo.get_outbox("FC-AAAAAA", msg.key)["status"] == "PENDING"  # type: ignore[index]


def test_failed_transition_writes_no_outbox(repo: Repo) -> None:
    new_order(repo)
    stale = repo.get_meta("FC-AAAAAA")
    meta = repo.transition(stale, OrderState.EXTRACTING, reason="x")
    repo.transition(meta, OrderState.AWAITING_CUSTOMER_INFO, reason="ask")
    with pytest.raises(StaleOrder):
        repo.transition(
            meta,
            OrderState.AWAITING_CUSTOMER_INFO,
            reason="duplicate",
            puts=[Put(item=Repo.outbox_item(outbox_msg()))],
        )
    assert repo.outbox("FC-AAAAAA") == []


def test_outbox_claim_is_exclusive(repo: Repo) -> None:
    new_order(repo)
    msg = outbox_msg()
    repo.db.put_item(TableName=repo.table, Item=to_item(Repo.outbox_item(msg)))
    assert repo.claim_outbox("FC-AAAAAA", msg.key)
    assert not repo.claim_outbox("FC-AAAAAA", msg.key)
    assert repo.mark_outbox_sent("FC-AAAAAA", msg.key, "ses-123")
    assert not repo.claim_outbox("FC-AAAAAA", msg.key)
    assert repo.get_outbox("FC-AAAAAA", msg.key)["ses_message_id"] == "ses-123"  # type: ignore[index]


def test_booking_is_write_once(repo: Repo) -> None:
    new_order(repo)
    assert repo.put_booking("FC-AAAAAA", "biergarten", "main", {"ref": "B1"})
    assert not repo.put_booking("FC-AAAAAA", "biergarten", "main", {"ref": "B2"})
    assert repo.get_booking("FC-AAAAAA", "main")["ref"] == "B1"  # type: ignore[index]


def test_wait_token_claimed_once_and_early_input_is_seen(repo: Repo) -> None:
    new_order(repo)
    meta = repo.transition(repo.get_meta("FC-AAAAAA"), OrderState.EXTRACTING, reason="x")
    repo.transition(meta, OrderState.AWAITING_CUSTOMER_INFO, reason="ask")
    # The reply lands before the workflow registered its token.
    early = repo.flag_pending_input("FC-AAAAAA")
    assert early.waiting_task_token is None
    after = repo.set_wait_token("FC-AAAAAA", "tok-1", "customer_info")
    assert after.pending_input  # the waiting step sees it and resumes itself
    assert repo.claim_wait_token("FC-AAAAAA", "tok-1")
    assert not repo.claim_wait_token("FC-AAAAAA", "tok-1")


def test_open_orders_by_customer(repo: Repo) -> None:
    new_order(repo, "FC-AAAAAA")
    new_order(repo, "FC-BBBBBB")
    meta = repo.get_meta("FC-BBBBBB")
    repo.transition(meta, OrderState.CANCELLED, reason="x")
    assert [o.order_id for o in repo.open_orders_for_customer("biergarten")] == ["FC-AAAAAA"]


def test_customer_lookup_by_address(repo: Repo) -> None:
    repo.put_customer("biergarten", "Biergarten Berlin GmbH", ["Customer@Example.com"])
    profile = repo.customer_for_address("customer@example.com")
    assert profile is not None and profile["name"] == "Biergarten Berlin GmbH"
    assert repo.customer_for_address("stranger@example.com") is None


def test_cache_ttl_and_group_invalidation(cache: DynamoCache) -> None:
    cache.put("LANE#A#B#ocean#x", {"price": 1}, ttl_seconds=60, group="LANE#A#B#ocean")
    cache.put("LANE#A#B#ocean#y", {"price": 2}, ttl_seconds=60, group="LANE#A#B#ocean")
    cache.put("expired", {"v": 1}, ttl_seconds=-1)
    assert cache.get("LANE#A#B#ocean#x") == {"price": 1}
    assert cache.get("expired") is None  # DynamoDB TTL deletion is lazy; reads check it
    assert cache.invalidate_group("LANE#A#B#ocean") == 2
    assert cache.get("LANE#A#B#ocean#y") is None


def test_keys_sort_chronologically() -> None:
    a = keys.event_sk(datetime(2026, 1, 1, tzinfo=UTC), "x")
    b = keys.event_sk(datetime(2026, 1, 2, tzinfo=UTC), "a")
    assert a < b
    assert keys.new_order_id().startswith("FC-")
