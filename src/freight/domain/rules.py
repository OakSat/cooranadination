"""Business validation of an extracted request.

Pydantic guarantees the LLM output is well-*formed*; these rules check it is plausible. A
violation never leads to quoting or booking: it becomes a question to the customer (CLARIFY)
or a hand-off to a rep (HUMAN).
"""

from datetime import date, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from freight.domain.models import FieldStatus, ModePreference, ShipmentRequest
from freight.planning import catalogs
from freight.planning.cargo import catalog_weight_kg

REQUIRED_FIELDS = ("quantity", "packaging", "pickup", "delivery", "requested_delivery_date")
# A conflict in any of these must be clarified (spec: date, quantity, address, shipping terms).
CRITICAL_FIELDS = (
    "quantity",
    "packaging",
    "pickup",
    "delivery",
    "requested_delivery_date",
    "incoterms",
    "mode_preference",
)
MIN_TRANSIT_DAYS = {ModePreference.OCEAN: 21, ModePreference.AIR: 2}
HANDLING_BUFFER_DAYS = 3  # booking, export clearance, last-mile truck
WEIGHT_TOLERANCE = 0.15
REEFER_RANGE_C = (-30.0, 30.0)


class Action(StrEnum):
    CLARIFY = "clarify"  # ask the customer
    HUMAN = "human"  # route to a representative


class Violation(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    field: str
    action: Action
    message: str


def _earliest_feasible(mode: ModePreference, today: date) -> date:
    days = MIN_TRANSIT_DAYS.get(mode, MIN_TRANSIT_DAYS[ModePreference.AIR])
    return today + timedelta(days=days + HANDLING_BUFFER_DAYS)


def validate_request(req: ShipmentRequest, today: date) -> list[Violation]:
    out: list[Violation] = []

    for name in REQUIRED_FIELDS:
        if req.fields()[name].status is FieldStatus.MISSING:
            out.append(
                Violation(
                    code="missing", field=name, action=Action.CLARIFY, message=f"{name} is required"
                )
            )
    for name in CRITICAL_FIELDS:
        if req.fields()[name].status is FieldStatus.CONFLICTING:
            out.append(
                Violation(
                    code="conflict",
                    field=name,
                    action=Action.CLARIFY,
                    message=f"emails disagree on {name}",
                )
            )

    deliver_by = req.requested_delivery_date.value if req.requested_delivery_date.known else None
    if deliver_by is not None:
        mode = req.mode_preference.value or ModePreference.ANY
        if deliver_by <= today:
            out.append(
                Violation(
                    code="date_in_past",
                    field="requested_delivery_date",
                    action=Action.CLARIFY,
                    message="delivery date is not in the future",
                )
            )
        elif deliver_by < _earliest_feasible(mode, today):
            hint = " (air may still make it)" if mode is ModePreference.OCEAN else ""
            out.append(
                Violation(
                    code="date_infeasible",
                    field="requested_delivery_date",
                    action=Action.CLARIFY,
                    message=f"too soon for {mode.value} transit{hint}",
                )
            )

    stated = req.total_weight_kg.value if req.total_weight_kg.known else None
    implied = catalog_weight_kg(req)
    if stated is not None and implied is not None:
        deviation = abs(stated - implied) / implied
        if deviation > WEIGHT_TOLERANCE:
            out.append(
                Violation(
                    code="weight_mismatch",
                    field="total_weight_kg",
                    action=Action.CLARIFY,
                    message=f"stated {stated:.0f} kg vs ~{implied:.0f} kg implied by quantity",
                )
            )

    for name, address in (("pickup", req.pickup.value), ("delivery", req.delivery.value)):
        if address is not None and catalogs.resolve_location(address) is None:
            out.append(
                Violation(
                    code="unknown_location",
                    field=name,
                    action=Action.HUMAN,
                    message=f"{name} address does not resolve to a known gateway",
                )
            )

    temp = req.temperature_control.value if req.temperature_control.known else None
    if temp is not None and temp.required:
        lo, hi = REEFER_RANGE_C
        too_cold = temp.min_c is not None and temp.min_c < lo
        too_warm = temp.max_c is not None and temp.max_c > hi
        if too_cold or too_warm:
            out.append(
                Violation(
                    code="temperature_out_of_range",
                    field="temperature_control",
                    action=Action.HUMAN,
                    message="outside reefer capability",
                )
            )
    return out


def ready_for_planning(violations: list[Violation]) -> bool:
    return not violations
