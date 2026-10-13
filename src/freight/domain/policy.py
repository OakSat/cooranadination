"""When must a human see a quote before the customer does? (PLAN.md §2.8)

Any one reason is enough. The reasons are stored with the order so the rep sees *why* it was
routed to them.
"""

from decimal import Decimal

from freight.domain.models import Extracted, FieldStatus, ShipmentRequest
from freight.domain.rules import CRITICAL_FIELDS

REVIEW_ABOVE_USD = Decimal("25000")
# A re-quote of a request a rep already approved goes straight to the customer unless prices
# rose more than this above what the rep saw.
REREVIEW_PRICE_INCREASE = Decimal("0.15")
MIN_CONFIDENCE = 0.8
SYSTEM_SOURCE = "system"  # Source.email_id of values filled by code (e.g. default origin)


def _system_filled(f: Extracted[object]) -> bool:
    return bool(f.sources) and all(s.email_id == SYSTEM_SOURCE for s in f.sources)


def review_reasons(
    request: ShipmentRequest,
    *,
    max_quote_usd: Decimal,
    first_order: bool,
    injection_suspected: bool,
) -> list[str]:
    reasons: list[str] = []
    if max_quote_usd > REVIEW_ABOVE_USD:
        reasons.append(f"quote above {REVIEW_ABOVE_USD} USD")
    temp = request.temperature_control.value if request.temperature_control.known else None
    if temp is not None and temp.required:
        reasons.append("temperature-controlled (reefer) shipment")
    for name, f in request.fields().items():
        if not f.known or _system_filled(f):
            continue
        if f.confidence < MIN_CONFIDENCE:
            reasons.append(f"low confidence on {name} ({f.confidence:.2f})")
        elif name in CRITICAL_FIELDS and f.status is FieldStatus.INFERRED:
            reasons.append(f"{name} was inferred, not stated")
    if first_order:
        reasons.append("customer's first order")
    if injection_suspected:
        reasons.append("possible prompt injection in the email")
    return reasons


def needs_rereview(max_quote_usd: Decimal, reviewed_max_usd: Decimal) -> list[str]:
    if max_quote_usd > reviewed_max_usd * (1 + REREVIEW_PRICE_INCREASE):
        return [f"prices rose more than {REREVIEW_PRICE_INCREASE:.0%} since the rep approved"]
    return []
