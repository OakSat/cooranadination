"""What a customer click or a rep button does.

Customer approval is the one transition made outside a workflow step, and on purpose: the
approval is the customer's act, so it is recorded atomically (QUOTE_SENT -> CUSTOMER_APPROVED,
conditional on state + version) at the moment of the click, with the option and its validity
checked in the same request. Everything after it runs in the workflow.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import TypeAdapter, ValidationError

from freight.domain.models import Extracted, FieldStatus, ShipmentRequest, Source
from freight.domain.state import OrderState
from freight.persistence import keys
from freight.persistence.repo import OrderNotFound, StaleOrder
from freight.workflow.deps import Deps
from freight.workflow.links import BadLink, Choice, LinkSigner
from freight.workflow.steps import resume

S = OrderState


class ChoiceOutcome(StrEnum):
    APPROVED = "approved"
    ALREADY_APPROVED = "already_approved"
    EXPIRED_REQUOTING = "expired_requoting"
    REPLACED = "replaced"  # a newer quote round exists
    NOT_OPEN = "not_open"  # the order is no longer waiting for a choice
    INVALID = "invalid"


def _verify(deps: Deps, token: str) -> Choice | None:
    try:
        return LinkSigner(deps.link_key()).verify(token, deps.now())
    except (BadLink, ValueError, KeyError):
        return None


def describe_choice(deps: Deps, token: str) -> dict[str, Any] | None:
    """Data for the GET confirmation page. Changes nothing."""
    choice = _verify(deps, token)
    if choice is None:
        return None
    option = deps.repo.get_item(choice.order_id, keys.quote_sk(choice.round, choice.option_id))
    if option is None:
        return None
    return {"orderId": choice.order_id, **option}


def confirm_choice(deps: Deps, token: str) -> tuple[ChoiceOutcome, dict[str, Any]]:
    choice = _verify(deps, token)
    if choice is None:
        return ChoiceOutcome.INVALID, {}
    repo = deps.repo
    try:
        meta = repo.get_meta(choice.order_id)
    except OrderNotFound:
        return ChoiceOutcome.INVALID, {}
    option = repo.get_item(choice.order_id, keys.quote_sk(choice.round, choice.option_id))
    if option is None:
        return ChoiceOutcome.INVALID, {}
    info = {"orderId": meta.order_id, "option": option}

    if meta.chosen_option == choice.option_id and meta.state not in (S.QUOTE_SENT,):
        return ChoiceOutcome.ALREADY_APPROVED, info
    if choice.round != meta.quote_round:
        return ChoiceOutcome.REPLACED, info
    if meta.state is not S.QUOTE_SENT:
        return ChoiceOutcome.NOT_OPEN, info

    try:
        if deps.now() >= datetime.fromisoformat(option["validUntil"]):
            repo.transition(
                meta,
                S.QUOTE_EXPIRED,
                reason="customer chose after the quote expired",
                step=f"expired:{meta.order_id}:{meta.quote_round}",
                updates={"price_update": True},
                detail={"option": choice.option_id},
                actor="customer",
                now=deps.now(),
            )
            resume(deps, meta.order_id, {"party": "customer", "decision": "requote"})
            return ChoiceOutcome.EXPIRED_REQUOTING, info
        repo.transition(
            meta,
            S.CUSTOMER_APPROVED,
            reason=f"customer chose {option['service']}",
            step=f"approve:{meta.order_id}:{meta.quote_round}:{choice.option_id}",
            updates={"chosen_option": choice.option_id},
            detail={"option": choice.option_id, "price": option["price"]},
            actor="customer",
            now=deps.now(),
        )
    except StaleOrder:
        # Two clicks raced (or two different options): whoever committed first wins.
        current = repo.get_meta(meta.order_id)
        if current.chosen_option == choice.option_id:
            return ChoiceOutcome.ALREADY_APPROVED, info
        return ChoiceOutcome.NOT_OPEN, info
    resume(deps, meta.order_id, {"party": "customer", "decision": "approved"})
    return ChoiceOutcome.APPROVED, info


# --- rep actions ----------------------------------------------------------------------------


class RepError(ValueError):
    pass


def _require(deps: Deps, order_id: str, *states: OrderState) -> Any:
    meta = deps.repo.get_meta(order_id)
    if meta.state not in states:
        raise RepError(f"order is {meta.state}, expected one of {[s.value for s in states]}")
    return meta


def rep_decide(deps: Deps, order_id: str, actor: str, decision: str, note: str = "") -> bool:
    """approve | reject for a quote under review."""
    if decision not in ("approve", "reject"):
        raise RepError(f"unknown decision {decision}")
    meta = _require(deps, order_id, S.PENDING_REP_REVIEW)
    deps.repo.record_event(
        order_id, meta.customer_id, f"REP_{decision.upper()}", {"note": note}, actor=actor
    )
    return resume(deps, order_id, {"party": "rep", "decision": decision, "actor": actor})


def rep_resolve(deps: Deps, order_id: str, actor: str, action: str) -> bool:
    """For NEEDS_HUMAN: retry_extraction | replan | cancel."""
    if action not in ("retry_extraction", "replan", "cancel"):
        raise RepError(f"unknown action {action}")
    meta = _require(deps, order_id, S.NEEDS_HUMAN)
    deps.repo.record_event(order_id, meta.customer_id, "REP_RESOLVE", {"action": action}, actor)
    return resume(deps, order_id, {"party": "rep", "decision": action, "actor": actor})


def rep_reextract(deps: Deps, order_id: str, actor: str) -> bool:
    """The customer answered a quote in free text; the rep decides it changes the request."""
    meta = _require(deps, order_id, S.QUOTE_SENT)
    deps.repo.record_event(order_id, meta.customer_id, "REP_REEXTRACT", {}, actor)
    return resume(deps, order_id, {"party": "customer", "decision": "reply", "actor": actor})


def rep_edit(deps: Deps, order_id: str, actor: str, field: str, value: Any) -> ShipmentRequest:
    """Correct one field. It becomes EXPLICIT with the rep as its source, as a new extraction
    version; the rep then approves (re-plan) or resolves the order."""
    meta = _require(deps, order_id, S.PENDING_REP_REVIEW, S.NEEDS_HUMAN)
    if field not in ShipmentRequest.model_fields:
        raise RepError(f"unknown field {field}")
    current = deps.repo.latest_extraction(order_id) or ShipmentRequest()
    adapter: TypeAdapter[Extracted[Any]] = TypeAdapter(
        ShipmentRequest.model_fields[field].annotation
    )
    try:
        edited = adapter.validate_python(
            {
                "value": value,
                "status": FieldStatus.EXPLICIT,
                "confidence": 1.0,
                "sources": [Source(email_id=f"rep:{actor}", quote="set by representative")],
            }
        )
    except ValidationError as e:
        raise RepError(f"invalid value for {field}: {e.errors()[0]['msg']}") from e
    request = current.model_copy(update={field: edited})
    before = current.fields()[field].model_dump(mode="json")
    deps.repo.record_edit(
        meta,
        request,
        actor=actor,
        detail={"field": field, "before": before.get("value"), "after": value},
        now=deps.now(),
    )
    deps.metrics.add("RepEdits", 1)
    return request


def rep_replan(deps: Deps, order_id: str, actor: str) -> bool:
    """After edits during review: re-plan and re-quote."""
    meta = _require(deps, order_id, S.PENDING_REP_REVIEW)
    deps.repo.record_event(order_id, meta.customer_id, "REP_REPLAN", {}, actor)
    return resume(deps, order_id, {"party": "rep", "decision": "edit", "actor": actor})
