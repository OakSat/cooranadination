"""End-to-end order flows, in-process: the demo happy path and the follow-up race.

Same step code, same state machine graph, same outbox sender as the deployed stack; only the
LLM (scripted from the golden labels), Step Functions, SES and the providers' transport are
local.
"""

import threading
from pathlib import Path
from typing import Any

import pytest

from freight.extraction import evaluation
from freight.workflow.actions import ChoiceOutcome, confirm_choice, rep_decide
from freight.workflow.steps import book_main_step, rank_step
from tests.integration.local_sfn import LocalWorld, utc

ROOT = Path(__file__).resolve().parents[1]
EMAILS = ROOT / "fixtures" / "emails"
CASES = {c["id"]: c for c in evaluation.load_cases(ROOT / "eval" / "golden.jsonl")}


def scripted_llm(prompt: str) -> dict[str, Any]:
    """Answer like a good model would, from the golden labels of the matching thread."""
    if "18 Paletten" in prompt:
        return evaluation.oracle_output(CASES["de_reply_changed_subject"])
    return evaluation.oracle_output(CASES["de_missing_date_pallets"])


@pytest.fixture
def world(aws: None) -> LocalWorld:
    from tests.integration.conftest import TABLES

    w = LocalWorld(TABLES, scripted_llm, utc(2026, 10, 13, 8, 5))
    w.repo.put_customer("biergarten", "Biergarten Berlin GmbH", ["customer@example.com"])
    return w


def to_quote_sent(world: LocalWorld) -> str:
    first = world.receive(EMAILS / "de/01_missing_date_pallets.eml")
    order_id = first["orderId"]
    world.clock.advance(hours=20)
    world.receive(EMAILS / "de/02_reply_changed_subject.eml")
    assert world.order_state(order_id) == "PENDING_REP_REVIEW"
    rep_decide(world.deps, order_id, "rep@example.com", "approve")
    world.settle()
    assert world.order_state(order_id) == "QUOTE_SENT"
    return order_id


def pick(world: LocalWorld, label: str) -> str:
    return next(t for line, t in world.links().items() if label in line)


def test_demo_happy_path(world: LocalWorld) -> None:
    first = world.receive(EMAILS / "de/01_missing_date_pallets.eml")
    order_id = first["orderId"]
    assert world.order_state(order_id) == "AWAITING_CUSTOMER_INFO"

    questions = world.mailer.sent[0]
    assert questions["Subject"].startswith(f"[{order_id}]")
    body = questions.get_content()
    assert "Wie viele Paletten" in body and "Bis zu welchem Datum" in body
    assert "Flaschen oder Dosen" not in body  # packaging was given: never asked
    assert sorted(world.repo.get_meta(order_id).asked_fields) == [
        "quantity",
        "requested_delivery_date",
    ]

    # Reply with a different subject and no reference token: matched by sender + open order.
    world.clock.advance(hours=20)
    reply = world.receive(EMAILS / "de/02_reply_changed_subject.eml")
    assert reply["rule"] == "sender_single_open" and reply["orderId"] == order_id
    meta = world.repo.get_meta(order_id)
    assert meta.state.value == "PENDING_REP_REVIEW"
    assert "customer's first order" in meta.review_reasons
    request = world.repo.latest_extraction(order_id)
    assert request is not None and request.quantity.value is not None
    assert request.quantity.value.amount == 18
    assert request.pickup.sources[0].email_id == "system"  # default origin, not asked

    plan = world.repo.get_item(order_id, "PLAN#001")
    assert plan is not None and "1x40GP (floor bound" in plan["plan"]["summary"]

    rep_decide(world.deps, order_id, "rep@example.com", "approve")
    world.settle()
    assert world.order_state(order_id) == "QUOTE_SENT"
    links = world.links()
    assert len(links) == 3

    world.clock.advance(minutes=20)
    outcome, _ = confirm_choice(world.deps, pick(world, "Ausgewogen"))
    assert outcome is ChoiceOutcome.APPROVED
    world.settle()

    assert world.order_state(order_id) == "COMPLETED"
    assert world.sfn.executions[order_id].status == "SUCCEEDED"
    purposes = [r["purpose"] for r in world.repo.outbox(order_id)]
    assert sorted(purposes) == ["confirmation", "questions", "quote"]
    assert len(world.mailer.sent) == 3
    assert {b["kind"] for b in world.repo.bookings(order_id)} == {"main", "truck"}
    assert "Buchungsnummer" in world.mailer.sent[-1].get_content()
    assert world.repo.get_meta(order_id).quote_round == 1
    assert world.metrics.total("TimeToFirstQuote") > 0


def test_reply_threads_by_in_reply_to(world: LocalWorld) -> None:
    order_id = world.receive(EMAILS / "de/01_missing_date_pallets.eml")["orderId"]
    ses_id = world.repo.outbox(order_id)[0]["ses_message_id"]
    reply = world.receive(
        EMAILS / "de/02_reply_changed_subject.eml",
        **{"In-Reply-To": f"<{ses_id}@email.amazonses.com>"},
    )
    assert reply["rule"] == "in_reply_to"


def test_follow_up_race(world: LocalWorld) -> None:
    """The spec's follow-up: duplicate SES event + price change + concurrent workers."""
    order_id = to_quote_sent(world)
    emails_before = len(world.mailer.sent)

    # 1. The same SES event arrives again (twice, concurrently).
    results: list[dict[str, Any]] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(world.receive(EMAILS / "de/01_missing_date_pallets.eml"))
        )
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r.get("duplicate") for r in results)
    assert len(world.sfn.executions) == 1
    assert len(world.mailer.sent) == emails_before

    # 2. Two workers rank the same round at once: still exactly one quote round.
    meta = world.repo.get_meta(order_id)
    rank_step(world.deps, order_id, meta.quote_round)
    assert len(world.repo.quote_options(order_id, 1)) == 3
    assert [r["purpose"] for r in world.repo.outbox(order_id)].count("quote") == 1

    # 3. The provider's price moves 12% before the customer clicks.
    world.chaos.price_drift_pct = 12
    world.clock.advance(minutes=10)
    token = pick(world, "Ausgewogen")
    clicks: list[ChoiceOutcome] = []
    click_threads = [
        threading.Thread(target=lambda: clicks.append(confirm_choice(world.deps, token)[0]))
        for _ in range(2)
    ]
    for t in click_threads:
        t.start()
    for t in click_threads:
        t.join()
    # The guarantee is one approval, whatever each click is told.
    assert ChoiceOutcome.APPROVED in clicks
    approvals = [
        e
        for e in world.repo.events(order_id)
        if e["kind"] == "STATE_CHANGED" and e["detail"]["to"] == "CUSTOMER_APPROVED"
    ]
    assert len(approvals) == 1
    world.settle()

    # Revalidation caught the stale price: nothing booked, a new round went to the customer
    # without a second rep review (the rep already reviewed this exact request).
    meta = world.repo.get_meta(order_id)
    assert meta.state.value == "QUOTE_SENT" and meta.quote_round == 2
    assert world.repo.bookings(order_id) == []
    kinds = [e["kind"] for e in world.repo.events(order_id)]
    assert "PRICE_CHANGED_REVALIDATION_FAILED" in kinds
    update = world.mailer.sent[-1].get_content()
    assert "Preis Ihrer gewählten Option hat sich" in update

    # An old link now points at a replaced round.
    assert confirm_choice(world.deps, token)[0] is ChoiceOutcome.REPLACED

    # 4. The customer approves the new price; booking runs twice (Lambda retry): one booking.
    outcome, _ = confirm_choice(world.deps, pick(world, "Ausgewogen"))
    assert outcome is ChoiceOutcome.APPROVED
    world.settle()
    assert world.order_state(order_id) == "COMPLETED"
    assert book_main_step(world.deps, order_id) == {"next": "done"}
    bookings = world.repo.bookings(order_id)
    assert sorted(b["kind"] for b in bookings) == ["main", "truck"]

    # Exactly one email per purpose and round.
    sent = [(r["purpose"], r["round"], r["status"]) for r in world.repo.outbox(order_id)]
    assert len(sent) == len(set(sent))
    assert all(status == "SENT" for *_, status in sent)
    assert len(world.sfn.executions) == 1


def test_expired_quote_is_requoted_not_booked(world: LocalWorld) -> None:
    order_id = to_quote_sent(world)
    token = pick(world, "Günstigste")
    world.clock.advance(hours=2)  # mock quotes are valid for 60 minutes
    outcome, _ = confirm_choice(world.deps, token)
    assert outcome is ChoiceOutcome.EXPIRED_REQUOTING
    world.settle()
    meta = world.repo.get_meta(order_id)
    assert meta.state.value == "QUOTE_SENT" and meta.quote_round == 2
    assert world.repo.bookings(order_id) == []


def test_dead_provider_still_quotes(world: LocalWorld) -> None:
    world.chaos.down = {"air-carrier"}
    order_id = to_quote_sent(world)
    modes = {q["mode"] for q in world.repo.quote_options(order_id, 1)}
    assert modes == {"ocean"}
    assert world.metrics.total("ProviderErrors") >= 1


def test_llm_kill_switch_routes_to_human(world: LocalWorld) -> None:
    world.params.values["llm-enabled"] = "false"
    order_id = world.receive(EMAILS / "de/01_missing_date_pallets.eml")["orderId"]
    assert world.order_state(order_id) == "NEEDS_HUMAN"
    assert world.llm.calls == []
    assert "Mitarbeiter" in world.mailer.sent[-1].get_content()  # handoff email in German


def test_unknown_sender_injection_goes_to_review(world: LocalWorld) -> None:
    def injected(prompt: str) -> dict[str, Any]:
        return evaluation.oracle_output(CASES["en_injection"])

    world.llm._responder = injected
    order_id = world.receive(EMAILS / "en/02_injection.eml")["orderId"]
    meta = world.repo.get_meta(order_id)
    assert meta.state.value == "PENDING_REP_REVIEW"
    assert meta.injection_suspected
    assert any("prompt injection" in r for r in meta.review_reasons)
    assert world.repo.bookings(order_id) == []


def test_rep_reject_cancels(world: LocalWorld) -> None:
    world.receive(EMAILS / "de/01_missing_date_pallets.eml")
    world.clock.advance(hours=1)
    order_id = world.receive(EMAILS / "de/02_reply_changed_subject.eml")["orderId"]
    rep_decide(world.deps, order_id, "rep@example.com", "reject")
    world.settle()
    assert world.order_state(order_id) == "CANCELLED"
    assert world.sfn.executions[order_id].status == "SUCCEEDED"
