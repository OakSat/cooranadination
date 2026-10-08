import pytest
from pydantic import ValidationError

from freight.domain.models import Extracted, FieldStatus, ShipmentRequest
from tests.factories import complete_request


def test_new_request_is_all_missing() -> None:
    req = ShipmentRequest()
    assert set(req.with_status(FieldStatus.MISSING)) == set(ShipmentRequest.model_fields)


def test_missing_field_cannot_carry_value() -> None:
    with pytest.raises(ValidationError):
        Extracted[int](value=3, status=FieldStatus.MISSING, confidence=0.0)


def test_explicit_field_needs_value() -> None:
    with pytest.raises(ValidationError):
        Extracted[int](status=FieldStatus.EXPLICIT, confidence=0.9)


def test_conflicting_needs_two_candidates() -> None:
    with pytest.raises(ValidationError):
        Extracted[int](status=FieldStatus.CONFLICTING, confidence=0.5, candidates=[1])
    ok = Extracted[int](status=FieldStatus.CONFLICTING, confidence=0.5, candidates=[1, 2])
    assert not ok.known


def test_confidence_bounds() -> None:
    with pytest.raises(ValidationError):
        Extracted[int](value=1, status=FieldStatus.INFERRED, confidence=1.5)


def test_extra_keys_rejected() -> None:
    # Untrusted LLM output with unexpected keys must fail loudly, not be silently dropped.
    data = complete_request().model_dump(mode="json")
    data["book_now"] = True
    with pytest.raises(ValidationError):
        ShipmentRequest.model_validate(data)


def test_json_round_trip() -> None:
    req = complete_request()
    assert ShipmentRequest.model_validate_json(req.model_dump_json()) == req


def test_json_schema_is_exportable() -> None:
    schema = ShipmentRequest.model_json_schema()
    assert schema["additionalProperties"] is False
    assert "quantity" in schema["properties"]
