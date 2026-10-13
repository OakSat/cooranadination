"""The order workflow, one function per Step Functions state.

Every step:
- reads what it needs from DynamoDB (the state machine passes only ids around);
- changes state only through ``repo.transition`` (conditional on state + version, with a step
  key so a Lambda retry of a step that already committed is a no-op);
- writes any email as an outbox row in the *same* transaction as the state change.

Waiting for a person (customer or rep) is a ``.waitForTaskToken`` task: ``wait_for`` stores
the token, and whoever answers calls ``resume``. Either side may come first; see ``resume``.
"""

import hashlib
import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from freight.domain.defaults import apply_defaults
from freight.domain.models import Priority, ShipmentRequest
from freight.domain.policy import needs_rereview, review_reasons
from freight.domain.rules import Action, validate_request
from freight.domain.state import OrderState, can_transition
from freight.domain.thread import Direction, ThreadMessage
from freight.email import compose
from freight.email.outbox import OutboundEmail, OutboxMessage, Purpose
from freight.extraction.extractor import extract
from freight.extraction.llm.port import LLMError
from freight.extraction.schema import Intent
from freight.persistence import keys
from freight.persistence.repo import OrderMeta, Put, Repo, StaleOrder
from freight.planning.alternatives import Label
from freight.planning.cargo import CargoDerivationError, derive_cargo
from freight.planning.quoting import (
    CargoPlan,
    PlanningError,
    QuotedOption,
    build_options,
    plan,
    provider_requests,
    rank,
)
from freight.providers.cache import quote_group
from freight.providers.client import ProviderRejected, ProviderUnavailable
from freight.workflow.deps import Deps
from freight.workflow.links import Choice, LinkSigner

log = logging.getLogger(__name__)

S = OrderState
MAX_CLARIFICATION_ROUNDS = 3
LINK_LIFETIME = timedelta(days=7)
PRICE_TOLERANCE = Decimal("0.005")
QUOTE_CACHE_MAX_SECONDS = 600
PROVIDERS = ("ocean-carrier", "air-carrier", "de-trucking")

# Who may answer which wait. A pending answer is only consumed by a wait for the same party.
CUSTOMER_WAITS = {"customer_info", "customer_choice"}
REP_WAITS = {"rep_review", "human"}


# --- helpers --------------------------------------------------------------------------------


def load_thread(repo: Repo, order_id: str) -> list[ThreadMessage]:
    return [
        ThreadMessage(
            email_id=e["emailId"],
            direction=Direction(e["direction"]),
            sent_at=e["sentAt"],
            subject=e.get("subject", ""),
            text=e.get("text", ""),
            attachments_text=[a["text"] for a in e.get("attachments", []) if a.get("text")],
        )
        for e in repo.emails(order_id)
    ]


def outbound(
    deps: Deps, meta: OrderMeta, purpose: Purpose, round_: int, email: OutboundEmail
) -> list[Put]:
    """The outbox row plus the outbound EMAIL row (so the extractor sees what we asked)."""
    now = deps.now()
    inbound = [e for e in deps.repo.emails(meta.order_id) if e["direction"] == "inbound"]
    message_ids = [e["messageId"] for e in inbound if e.get("messageId")]
    msg = OutboxMessage.create(
        order_id=meta.order_id,
        customer_id=meta.customer_id,
        purpose=purpose,
        round_=round_,
        to=[meta.customer_email],
        email=email,
        now=now,
        in_reply_to=message_ids[-1] if message_ids else None,
        references=message_ids[-5:],
    )
    email_row = {
        "PK": keys.order_pk(meta.order_id),
        "SK": keys.email_sk(now, f"out-{msg.key}"),
        "type": "email",
        "orderId": meta.order_id,
        "customerId": meta.customer_id,
        "emailId": f"out-{msg.key}",
        "direction": "outbound",
        "sentAt": now.isoformat(),
        "subject": email.subject,
        "text": email.body_text,
        "outboxKey": msg.key,
    }
    return [Put(item=Repo.outbox_item(msg)), Put(item=email_row)]


def _step(name: str, meta: OrderMeta, *parts: object) -> str:
    return ":".join([name, meta.order_id, *(str(p) for p in parts)])


def to_human(deps: Deps, meta: OrderMeta, reason: str, notify_customer: bool = False) -> OrderMeta:
    """Hand the order to a rep. Optionally tell the customer someone is looking at it."""
    puts: list[Put] = []
    if notify_customer:
        email = compose.compose_handoff(
            lang=meta.language, ref=meta.order_id, name=meta.customer_name
        )
        puts = outbound(deps, meta, Purpose.HANDOFF, meta.version, email)
    return deps.repo.transition(
        meta,
        S.NEEDS_HUMAN,
        reason=reason,
        step=_step("human", meta, meta.version),
        puts=puts,
        now=deps.now(),
    )


def _latest_request(deps: Deps, order_id: str) -> ShipmentRequest:
    request = deps.repo.latest_extraction(order_id)
    if request is None:
        raise RuntimeError(f"{order_id} has no extraction")
    return request


# --- waits ----------------------------------------------------------------------------------


def wait_for(deps: Deps, order_id: str, token: str, waiting_for: str) -> dict[str, Any]:
    """``.waitForTaskToken`` task body: park the token, or resume at once if the answer
    already arrived while no step was waiting."""
    meta = deps.repo.set_wait_token(order_id, token, waiting_for)
    decision = meta.pending_decision or {}
    party_ok = (decision.get("party") == "customer" and waiting_for in CUSTOMER_WAITS) or (
        decision.get("party") == "rep" and waiting_for in REP_WAITS
    )
    if meta.pending_input and party_ok and deps.repo.claim_wait_token(order_id, token):
        deps.workflow.send_success(token, decision)
        return {"resumed": "immediately"}
    return {"waiting": waiting_for}


def resume(deps: Deps, order_id: str, decision: dict[str, Any]) -> bool:
    """Deliver a customer/rep answer. Returns True if a waiting step was resumed now.

    The flag is written first and returned with the token in one atomic update, so either we
    see the token (and resume), or the waiting step registers later and sees the flag.
    Claiming the token is conditional, so a token is used at most once.
    """
    meta = deps.repo.flag_pending_input(order_id, decision)
    token = meta.waiting_task_token
    party = decision.get("party")
    expected = CUSTOMER_WAITS if party == "customer" else REP_WAITS
    if token and meta.waiting_for in expected and deps.repo.claim_wait_token(order_id, token):
        if deps.workflow.send_success(token, decision):
            return True
        deps.repo.record_event(order_id, meta.customer_id, "LATE_INPUT", decision)
    return False


# --- extraction -----------------------------------------------------------------------------


def extract_step(deps: Deps, order_id: str) -> dict[str, str]:
    repo = deps.repo
    meta = repo.get_meta(order_id)
    if meta.state is not S.EXTRACTING:
        meta = repo.transition(
            meta,
            S.EXTRACTING,
            reason="new customer input",
            step=_step("extract", meta, meta.version),
            clear_pending=True,
            now=deps.now(),
        )

    if not deps.params.flag("llm-enabled", True):
        to_human(deps, meta, "LLM kill switch is off", notify_customer=True)
        return {"next": "human"}

    thread = load_thread(repo, order_id)
    previous = repo.latest_extraction(order_id)
    try:
        result = extract(
            thread,
            llm=deps.llm,
            today=deps.today(),
            previous=previous,
            asked=set(meta.asked_fields),
            cache=deps.cache,
        )
    except LLMError as e:
        log.warning("extraction failed for %s: %s", order_id, e)
        to_human(deps, meta, f"extraction failed: {e}", notify_customer=True)
        return {"next": "human"}

    m = deps.metrics
    m.add("LLMInputTokens", result.usage.input_tokens)
    m.add("LLMOutputTokens", result.usage.output_tokens)
    m.add("LLMCostUSD", result.cost_usd, "None")
    m.add("ExtractionLatency", result.latency_ms, "Milliseconds")

    request = apply_defaults(result.request)
    violations = validate_request(request, deps.today())
    version = meta.extraction_version + 1
    if version == 1:
        m.add("FieldsMissingAtFirstPass", sum(v.code == "missing" for v in violations))

    snapshot = Put(
        item=Repo.extraction_item(
            meta,
            version,
            request,
            {
                "source": "llm",
                "model": result.model_id,
                "promptVersion": result.prompt_version,
                "inputTokens": result.usage.input_tokens,
                "outputTokens": result.usage.output_tokens,
                "costUsd": result.cost_usd,
                "cached": result.cached,
                "changes": [c.model_dump(mode="json") for c in result.changes],
                "notes": result.notes,
                "downgraded": result.downgraded,
                "intent": result.intent.value,
                "injectionHits": result.injection_hits,
                "violations": [v.model_dump(mode="json") for v in violations],
            },
            deps.now(),
        )
    )
    updates: dict[str, Any] = {
        "extraction_version": version,
        "language": result.language,
        "injection_suspected": meta.injection_suspected or result.injection_suspected,
    }
    detail = {"extraction": version, "violations": [f"{v.code}:{v.field}" for v in violations]}

    human = [v for v in violations if v.action is Action.HUMAN]
    rounds_so_far = len(
        [o for o in repo.outbox(order_id) if o.get("purpose") == Purpose.QUESTIONS.value]
    )
    if result.intent is Intent.CANCEL or human or rounds_so_far >= MAX_CLARIFICATION_ROUNDS:
        why = (
            "customer asked to cancel"
            if result.intent is Intent.CANCEL
            else "; ".join(v.message for v in human) or "too many clarification rounds"
        )
        email = compose.compose_handoff(lang=result.language, ref=order_id, name=meta.customer_name)
        repo.transition(
            meta,
            S.NEEDS_HUMAN,
            reason=why,
            step=_step("extracted", meta, version),
            updates=updates,
            puts=[snapshot, *outbound(deps, meta, Purpose.HANDOFF, version, email)],
            detail=detail,
            now=deps.now(),
        )
        return {"next": "human"}

    if violations:
        email, asked = compose.compose_questions(
            violations, request, result.language, order_id, meta.customer_name
        )
        round_ = rounds_so_far + 1
        repo.transition(
            meta,
            S.AWAITING_CUSTOMER_INFO,
            reason="missing or conflicting details",
            step=_step("extracted", meta, version),
            updates={**updates, "asked_fields": sorted(asked)},
            puts=[snapshot, *outbound(deps, meta, Purpose.QUESTIONS, round_, email)],
            detail={**detail, "asked": sorted(asked)},
            now=deps.now(),
        )
        m.add("ClarificationRounds", 1)
        return {"next": "ask"}

    repo.transition(
        meta,
        S.READY_FOR_PLANNING,
        reason="request complete and plausible",
        step=_step("extracted", meta, version),
        updates={**updates, "asked_fields": []},
        puts=[snapshot],
        detail=detail,
        now=deps.now(),
    )
    return {"next": "plan"}


# --- planning & quoting ---------------------------------------------------------------------


def plan_step(deps: Deps, order_id: str) -> dict[str, Any]:
    repo = deps.repo
    meta = repo.get_meta(order_id)
    if meta.state is S.PENDING_REP_REVIEW:  # a rep edited the request
        meta = repo.transition(
            meta, S.READY_FOR_PLANNING, reason="rep edit", step=_step("replan", meta, meta.version)
        )
    request = _latest_request(deps, order_id)
    violations = validate_request(request, deps.today())
    try:
        if violations:
            raise PlanningError("; ".join(v.message for v in violations))
        cargo_plan = plan(derive_cargo(request), request, deps.today())
    except (PlanningError, CargoDerivationError) as e:
        if meta.state is S.QUOTE_EXPIRED:
            meta = repo.transition(meta, S.QUOTING, reason="requote", step=_step("q", meta, 0))
        to_human(deps, meta, f"planning failed: {e}")
        return {"next": "human"}

    round_ = meta.quote_round + 1
    if meta.state is not S.QUOTING:
        meta = repo.transition(
            meta,
            S.QUOTING,
            reason=cargo_plan.summary,
            step=_step("plan", meta, round_),
            updates={"quote_round": round_},
            puts=[
                Put(
                    item={
                        "PK": keys.order_pk(order_id),
                        "SK": keys.plan_sk(round_),
                        "type": "plan",
                        "orderId": order_id,
                        "customerId": meta.customer_id,
                        "plan": cargo_plan.model_dump(mode="json"),
                        "requests": provider_requests(cargo_plan),
                    }
                )
            ],
            detail={"round": round_},
            now=deps.now(),
        )
    log.info("plan %s round %s: %s", order_id, meta.quote_round, cargo_plan.summary)
    return {"next": "quote", "round": meta.quote_round, "summary": cargo_plan.summary}


def _quote_cache_key(provider: str, body: dict[str, Any]) -> tuple[str, str]:
    group = quote_group(provider, body)
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    return f"QUOTES#{group}#{digest}", group


def quote_step(deps: Deps, order_id: str, round_: int, provider: str) -> dict[str, Any]:
    """One provider, called from a Parallel branch. Never fails the order: on error it
    records zero quotes so ranking continues with whoever answered."""
    repo = deps.repo
    plan_row = repo.get_item(order_id, keys.plan_sk(round_))
    if plan_row is None:
        raise RuntimeError(f"no plan for {order_id} round {round_}")
    body = plan_row["requests"].get(provider)
    quotes: list[dict[str, Any]] = []
    error: str | None = None
    if body is not None:
        key, lane = _quote_cache_key(provider, body)
        # A re-quote after a price change must not be served the price that just failed.
        fresh_only = repo.get_meta(order_id).price_update
        cached = None if fresh_only else deps.cache.get(key)
        now = deps.now()
        if cached and all(
            datetime.fromisoformat(q["valid_until"]) > now + timedelta(minutes=5)
            for q in cached["quotes"]
        ):
            quotes = cached["quotes"]
        else:
            client = deps.provider_factory(provider)
            try:
                quotes = client.quote(body)
            except (ProviderUnavailable, ProviderRejected) as e:
                error = str(e)
                deps.metrics.add("ProviderErrors", 1)
                if client.breaker.just_opened:
                    deps.metrics.add("ProviderCircuitOpen", 1)
            if quotes:
                expiry = min(datetime.fromisoformat(q["valid_until"]) for q in quotes)
                ttl = min(int((expiry - now).total_seconds()), QUOTE_CACHE_MAX_SECONDS)
                if ttl > 0:
                    deps.cache.put(key, {"quotes": quotes}, ttl, group=lane)
    repo.put_item(
        {
            "PK": keys.order_pk(order_id),
            "SK": keys.provider_quotes_sk(round_, provider),
            "type": "provider_quotes",
            "orderId": order_id,
            "provider": provider,
            "quotes": quotes,
            "error": error,
        }
    )
    return {"provider": provider, "count": len(quotes), "error": error}


def _quote_item(
    order_id: str, customer_id: str, round_: int, q: QuotedOption, labels: list[Label]
) -> dict[str, Any]:
    o = q.option
    return {
        "PK": keys.order_pk(order_id),
        "SK": keys.quote_sk(round_, o.option_id),
        "type": "quote",
        "orderId": order_id,
        "customerId": customer_id,
        "round": round_,
        "optionId": o.option_id,
        "labels": [label.value for label in labels],
        "mode": o.mode.value,
        "service": o.service,
        "carrier": o.carrier,
        "provider": q.provider,
        "providerQuoteId": o.provider_quote_id,
        "price": str(o.price),
        "mainPrice": str(q.main_price),
        "lastMilePrice": str(q.last_mile_price) if q.last_mile_price is not None else None,
        "lastMileQuoteId": q.last_mile_quote_id,
        "gateway": q.gateway,
        "currency": o.currency,
        "departure": o.departure.isoformat(),
        "arrival": o.arrival.isoformat(),
        "validUntil": o.valid_until.isoformat(),
        "priceHash": hashlib.sha256(f"{o.provider_quote_id}|{o.price}".encode()).hexdigest()[:16],
    }


def rank_step(deps: Deps, order_id: str, round_: int) -> dict[str, str]:
    repo = deps.repo
    meta = repo.get_meta(order_id)
    if meta.state is not S.QUOTING:
        return {"next": "review" if meta.state is S.PENDING_REP_REVIEW else "customer"}
    plan_row = repo.get_item(order_id, keys.plan_sk(round_))
    assert plan_row is not None
    cargo_plan = CargoPlan.model_validate(plan_row["plan"])
    quotes = {
        p: (repo.get_item(order_id, keys.provider_quotes_sk(round_, p)) or {}).get("quotes", [])
        for p in PROVIDERS
    }
    request = _latest_request(deps, order_id)
    priority = request.priority.value if request.priority.known and request.priority.value else None
    ranked = rank(
        build_options(cargo_plan, quotes),
        priority or Priority.BALANCED,
        request.requested_delivery_date.value if request.requested_delivery_date.known else None,
        deps.now(),
    )
    if not ranked:
        to_human(deps, meta, "no provider returned a usable option")
        return {"next": "human"}

    quote_puts = [
        Put(item=_quote_item(order_id, meta.customer_id, round_, q, alt.labels))
        for q, alt in ranked
    ]
    completed = [m for m in repo.orders_for_customer(meta.customer_id) if m.state is S.COMPLETED]
    max_price = max(q.option.price for q, _ in ranked)
    if meta.reviewed_extraction == meta.extraction_version and meta.reviewed_max_price:
        # A rep already approved this exact request; this is a re-quote (price change).
        reasons = needs_rereview(max_price, Decimal(meta.reviewed_max_price))
    else:
        reasons = review_reasons(
            request,
            max_quote_usd=max_price,
            first_order=not completed,
            injection_suspected=meta.injection_suspected,
        )
    deps.metrics.add("HumanReviewRate", 1 if reasons else 0)
    if reasons:
        repo.transition(
            meta,
            S.PENDING_REP_REVIEW,
            reason="; ".join(reasons),
            step=_step("ranked", meta, round_),
            updates={"review_reasons": reasons},
            puts=quote_puts,
            detail={"round": round_, "options": len(ranked)},
            now=deps.now(),
        )
        return {"next": "review"}

    _send_quote(deps, meta, round_, quote_puts, [i.item for i in quote_puts])
    return {"next": "customer"}


def _quote_email(
    deps: Deps, meta: OrderMeta, round_: int, items: list[dict[str, Any]]
) -> OutboundEmail:
    request = _latest_request(deps, meta.order_id)
    signer = LinkSigner(deps.link_key())
    expires = deps.now() + LINK_LIFETIME
    lines = [
        compose.QuoteLine(
            labels=i["labels"],
            mode=i["mode"],
            service=i["service"],
            carrier=i["carrier"],
            price=Decimal(i["mainPrice"]),
            last_mile=Decimal(i["lastMilePrice"]) if i.get("lastMilePrice") else None,
            currency=i["currency"],
            departure=date.fromisoformat(i["departure"]),
            arrival=date.fromisoformat(i["arrival"]),
            link=f"{deps.api_base_url.rstrip('/')}/choose?t="
            + signer.sign(Choice(meta.order_id, round_, i["optionId"], expires)),
        )
        for i in items
    ]
    lang = meta.language
    qty = request.quantity.value
    packaging = request.packaging.value
    cargo = " ".join(
        p
        for p in (
            compose.format_value(qty, lang) if qty else "",
            compose.UNIT_NAME[packaging.value][lang] if packaging else "",
        )
        if p
    )
    destination = request.delivery.value.city if request.delivery.value else ""
    return compose.compose_quote(
        lines,
        lang=lang,
        ref=meta.order_id,
        cargo=cargo,
        destination=destination,
        valid_until=min(datetime.fromisoformat(i["validUntil"]) for i in items),
        name=meta.customer_name,
        price_update=meta.price_update,
    )


def _send_quote(
    deps: Deps,
    meta: OrderMeta,
    round_: int,
    extra: list[Put],
    items: list[dict[str, Any]],
    updates: dict[str, Any] | None = None,
) -> OrderMeta:
    email = _quote_email(deps, meta, round_, items)
    meta = deps.repo.transition(
        meta,
        S.QUOTE_SENT,
        reason="quote sent to customer",
        step=_step("quote_sent", meta, round_),
        updates={"price_update": False, **(updates or {})},
        puts=[*extra, *outbound(deps, meta, Purpose.QUOTE, round_, email)],
        detail={"round": round_},
        now=deps.now(),
    )
    if round_ == 1:
        minutes = (deps.now() - meta.created_at).total_seconds() / 60
        deps.metrics.add("TimeToFirstQuote", minutes, "None")
    return meta


def send_quote_step(deps: Deps, order_id: str) -> dict[str, str]:
    """After a rep approved: PENDING_REP_REVIEW -> QUOTE_SENT with the quote email."""
    meta = deps.repo.get_meta(order_id)
    if meta.state is S.QUOTE_SENT:
        return {"next": "customer"}
    items = deps.repo.quote_options(order_id, meta.quote_round)
    reviewed = {
        "reviewed_extraction": meta.extraction_version,
        "reviewed_max_price": str(max(Decimal(i["price"]) for i in items)),
    }
    _send_quote(deps, meta, meta.quote_round, [], items, reviewed)
    return {"next": "customer"}


# --- customer approval, revalidation, booking -----------------------------------------------


def revalidate_step(deps: Deps, order_id: str) -> dict[str, str]:
    """Never book on a cached or stale price: re-confirm the chosen offer live."""
    repo = deps.repo
    meta = repo.get_meta(order_id)
    if meta.state is S.CUSTOMER_APPROVED:
        meta = repo.transition(
            meta, S.REVALIDATING, reason="re-confirming price", step=_step("reval", meta, 0)
        )
    if meta.state is S.BOOKING:
        return {"next": "book"}
    option = repo.get_item(order_id, keys.quote_sk(meta.quote_round, meta.chosen_option or ""))
    if option is None:
        to_human(deps, meta, "chosen option not found")
        return {"next": "human"}

    live = deps.provider_factory(option["provider"]).confirm(option["providerQuoteId"])
    quoted = Decimal(option["mainPrice"])
    current = Decimal(live["price"])
    moved = abs(current - quoted) > quoted * PRICE_TOLERANCE
    if live.get("expired") or not live.get("available", True) or moved:
        why = "quote expired at provider" if live.get("expired") else "price changed"
        repo.transition(
            meta,
            S.QUOTE_EXPIRED,
            reason=why,
            step=_step("reval_failed", meta, meta.quote_round),
            updates={"price_update": True},
            detail={
                "kind": "PRICE_CHANGED_REVALIDATION_FAILED",
                "quoted": str(quoted),
                "current": str(current),
                "option": option["optionId"],
            },
            now=deps.now(),
        )
        repo.record_event(
            order_id,
            meta.customer_id,
            "PRICE_CHANGED_REVALIDATION_FAILED",
            {"quoted": str(quoted), "current": str(current), "option": option["optionId"]},
        )
        return {"next": "requote"}
    repo.transition(
        meta,
        S.BOOKING,
        reason=f"price confirmed {current} {option['currency']}",
        step=_step("reval_ok", meta, meta.quote_round),
        now=deps.now(),
    )
    return {"next": "book"}


def booking_key(order_id: str, round_: int, option_id: str, leg: str) -> str:
    return hashlib.sha256(f"{order_id}|{round_}|{option_id}|{leg}".encode()).hexdigest()[:32]


def book_main_step(deps: Deps, order_id: str) -> dict[str, str]:
    repo = deps.repo
    meta = repo.get_meta(order_id)
    if meta.state is not S.BOOKING:
        return {"next": "truck" if meta.state is S.BOOKED else "done"}
    option = repo.get_item(order_id, keys.quote_sk(meta.quote_round, meta.chosen_option or ""))
    assert option is not None
    key = booking_key(order_id, meta.quote_round, option["optionId"], "main")
    existing = repo.get_booking(order_id, "main")
    if existing is None:
        try:
            booking = deps.provider_factory(option["provider"]).book(
                {"quote_id": option["providerQuoteId"], "expected_price": option["mainPrice"]},
                idempotency_key=key,
            )
        except ProviderRejected as e:
            to_human(deps, meta, f"provider refused booking: {e.body.get('error')}")
            return {"next": "human"}
        repo.put_booking(
            order_id,
            meta.customer_id,
            "main",
            {**booking, "idempotencyKey": key, "provider": option["provider"]},
        )
    repo.transition(meta, S.BOOKED, reason="main leg booked", step=_step("booked", meta, 0))
    return {"next": "truck"}


def book_truck_step(deps: Deps, order_id: str) -> dict[str, str]:
    repo = deps.repo
    meta = repo.get_meta(order_id)
    if meta.state is not S.BOOKED:
        return {"next": "complete"}
    option = repo.get_item(order_id, keys.quote_sk(meta.quote_round, meta.chosen_option or ""))
    main = repo.get_booking(order_id, "main")
    assert option is not None and main is not None
    key = booking_key(order_id, meta.quote_round, option["optionId"], "truck")
    if repo.get_booking(order_id, "truck") is None:
        plan_row = repo.get_item(order_id, keys.plan_sk(meta.quote_round))
        assert plan_row is not None
        body = dict(plan_row["requests"]["de-trucking"])
        body["origins"] = [option["gateway"]]
        client = deps.provider_factory("de-trucking")
        try:
            # The truck leg is re-quoted live for the real arrival date, then booked.
            truck_quote = client.quote(body)[0]
            booking = client.book(
                {
                    "quote_id": truck_quote["quote_id"],
                    "expected_price": truck_quote["price"],
                    "pickup_date": main.get("arrival") or option["arrival"],
                },
                idempotency_key=key,
            )
        except (ProviderRejected, IndexError) as e:
            to_human(deps, meta, f"truck booking failed: {e}")
            return {"next": "human"}
        repo.put_booking(order_id, meta.customer_id, "truck", {**booking, "idempotencyKey": key})
    repo.transition(meta, S.LAST_MILE_BOOKED, reason="truck booked", step=_step("truck", meta, 0))
    return {"next": "complete"}


def complete_step(deps: Deps, order_id: str) -> dict[str, str]:
    repo = deps.repo
    meta = repo.get_meta(order_id)
    if meta.state is S.COMPLETED:
        return {"next": "done"}
    main = repo.get_booking(order_id, "main") or {}
    truck = repo.get_booking(order_id, "truck") or {}
    lang = meta.language
    total = Decimal(main.get("price", "0")) + Decimal(truck.get("price", "0"))
    email = compose.compose_confirmation(
        lang=lang,
        ref=order_id,
        name=meta.customer_name,
        main={
            "service": main.get("service", ""),
            "carrier": main.get("carrier", ""),
            "ref": main.get("booking_ref", ""),
            "departure": main.get("departure", ""),
            "arrival": main.get("arrival", ""),
        },
        truck={
            "carrier": truck.get("carrier", ""),
            "ref": truck.get("booking_ref", ""),
            "delivery": truck.get("delivery", ""),
        },
        total=compose.format_money(total, main.get("currency", "USD"), lang),
    )
    meta = repo.transition(
        meta,
        S.COMPLETED,
        reason="booked end to end",
        step=_step("complete", meta, 0),
        puts=outbound(deps, meta, Purpose.CONFIRMATION, meta.quote_round, email),
        now=deps.now(),
    )
    hours = (deps.now() - meta.created_at).total_seconds() / 3600
    deps.metrics.add("TimeToBooking", hours, "None")
    return {"next": "done"}


# --- rep and failure paths ------------------------------------------------------------------


def cancel_step(deps: Deps, order_id: str, reason: str = "cancelled by rep") -> dict[str, str]:
    meta = deps.repo.get_meta(order_id)
    if meta.state is S.CANCELLED:
        return {"next": "done"}
    if meta.state is S.QUOTE_SENT:
        meta = deps.repo.transition(meta, S.QUOTE_EXPIRED, reason="no choice before timeout")
    deps.repo.transition(meta, S.CANCELLED, reason=reason, step=_step("cancel", meta, 0))
    return {"next": "done"}


def escalate_step(deps: Deps, order_id: str, error: str = "") -> dict[str, str]:
    """Catch-all target: park the order with a rep instead of losing it."""
    meta = deps.repo.get_meta(order_id)
    if meta.state is S.NEEDS_HUMAN:
        return {"next": "human"}
    reason = f"workflow error: {error}"[:500] if error else "waited too long"
    try:
        if can_transition(meta.state, S.NEEDS_HUMAN):
            to_human(deps, meta, reason)
        elif can_transition(meta.state, S.FAILED):
            failed = deps.repo.transition(meta, S.FAILED, reason=reason)
            deps.repo.transition(failed, S.NEEDS_HUMAN, reason="failure needs a rep")
        else:
            deps.repo.record_event(
                order_id, meta.customer_id, "ESCALATION_IMPOSSIBLE", {"error": error}
            )
            return {"next": "fail"}
    except StaleOrder:
        return escalate_step(deps, order_id, error)
    return {"next": "human"}


def resume_from_human_step(deps: Deps, order_id: str, action: str) -> dict[str, str]:
    """What the rep chose for a NEEDS_HUMAN order: re-extract, re-plan, or cancel."""
    meta = deps.repo.get_meta(order_id)
    if action == "cancel":
        return cancel_step(deps, order_id, "cancelled by rep")
    if action == "replan" and meta.state is S.NEEDS_HUMAN:
        deps.repo.transition(meta, S.READY_FOR_PLANNING, reason="rep: re-plan")
        return {"next": "plan"}
    return {"next": "extract"}
