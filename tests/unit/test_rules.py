from datetime import date

from freight.domain.models import (
    Address,
    Extracted,
    FieldStatus,
    ModePreference,
    Quantity,
    QuantityUnit,
    TemperatureRequirement,
)
from freight.domain.rules import Action, ready_for_planning, validate_request
from tests.factories import complete_request, explicit

TODAY = date(2026, 10, 8)


def codes(violations: list) -> set[tuple[str, str]]:  # type: ignore[type-arg]
    return {(v.code, v.field) for v in violations}


def test_complete_request_is_ready() -> None:
    violations = validate_request(complete_request(), TODAY)
    assert violations == []
    assert ready_for_planning(violations)


def test_missing_required_fields_are_clarified() -> None:
    req = complete_request(
        requested_delivery_date=Extracted(status=FieldStatus.MISSING, confidence=0.0)
    )
    violations = validate_request(req, TODAY)
    assert codes(violations) == {("missing", "requested_delivery_date")}
    assert violations[0].action is Action.CLARIFY


def test_conflicting_quantity_needs_clarification() -> None:
    req = complete_request(
        quantity=Extracted(
            status=FieldStatus.CONFLICTING,
            confidence=0.4,
            candidates=[
                Quantity(amount=18, unit=QuantityUnit.PALLETS),
                Quantity(amount=20, unit=QuantityUnit.PALLETS),
            ],
        )
    )
    assert ("conflict", "quantity") in codes(validate_request(req, TODAY))


def test_date_in_past() -> None:
    req = complete_request(requested_delivery_date=explicit(date(2026, 9, 1)))
    assert ("date_in_past", "requested_delivery_date") in codes(validate_request(req, TODAY))


def test_date_too_soon_for_ocean() -> None:
    req = complete_request(
        requested_delivery_date=explicit(date(2026, 10, 20)),
        mode_preference=explicit(ModePreference.OCEAN),
    )
    violations = validate_request(req, TODAY)
    assert ("date_infeasible", "requested_delivery_date") in codes(violations)
    assert "air may still make it" in violations[0].message


def test_same_date_is_fine_for_air() -> None:
    req = complete_request(
        requested_delivery_date=explicit(date(2026, 10, 20)),
        mode_preference=explicit(ModePreference.AIR),
    )
    assert validate_request(req, TODAY) == []


def test_valid_json_but_wrong_weight() -> None:
    # 18 pallets of bottles weigh ~16 t; a stated 5 t is well-formed but implausible.
    req = complete_request(total_weight_kg=explicit(5000.0))
    assert ("weight_mismatch", "total_weight_kg") in codes(validate_request(req, TODAY))


def test_weight_within_tolerance_passes() -> None:
    req = complete_request(total_weight_kg=explicit(17000.0))
    assert validate_request(req, TODAY) == []


def test_unknown_location_goes_to_human() -> None:
    req = complete_request(delivery=explicit(Address(city="Atlantis", country="DE")))
    violations = validate_request(req, TODAY)
    assert codes(violations) == {("unknown_location", "delivery")}
    assert violations[0].action is Action.HUMAN


def test_city_with_umlaut_resolves() -> None:
    req = complete_request(delivery=explicit(Address(city="München", country="DE")))
    assert validate_request(req, TODAY) == []


def test_extreme_temperature_goes_to_human() -> None:
    req = complete_request(
        temperature_control=explicit(TemperatureRequirement(required=True, min_c=-40, max_c=-35))
    )
    assert ("temperature_out_of_range", "temperature_control") in codes(
        validate_request(req, TODAY)
    )
