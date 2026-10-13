"""Convert the model's raw form into the domain ``ShipmentRequest``, in code.

Units are converted here (lb -> kg, in -> cm), never by the model. A value that fails domain
validation (a zero quantity, an unparseable date, a three-letter country) becomes MISSING with a
note, so it is asked again instead of silently trusted.
"""

from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from freight.domain.models import (
    Address,
    Dimensions,
    Extracted,
    FieldStatus,
    Quantity,
    Responsibilities,
    ShipmentRequest,
    Source,
    TemperatureRequirement,
)
from freight.extraction.schema import (
    LengthUnit,
    LLMAddress,
    LLMDimensions,
    LLMQuantity,
    LLMResponsibilities,
    LLMTemperature,
    LLMWeight,
    ShipmentLLMOutput,
    WeightUnit,
)

KG_PER = {WeightUnit.KG: 1.0, WeightUnit.LB: 0.45359237, WeightUnit.T: 1000.0}
CM_PER = {LengthUnit.CM: 1.0, LengthUnit.M: 100.0, LengthUnit.IN: 2.54}

# LLM field name -> ShipmentRequest field name (only weight differs: the model reports a unit).
DOMAIN_NAME = {"total_weight": "total_weight_kg"}


def weight_kg(w: LLMWeight) -> float:
    return round(w.amount * KG_PER[w.unit], 1)


def dimensions_cm(d: LLMDimensions) -> Dimensions:
    f = CM_PER[d.unit]
    return Dimensions(
        length_cm=round(d.length * f, 1),
        width_cm=round(d.width * f, 1),
        height_cm=round(d.height * f, 1),
        pieces=d.pieces,
    )


def _convert(value: Any) -> Any:
    match value:
        case LLMQuantity():
            return Quantity(amount=value.amount, unit=value.unit)
        case LLMWeight():
            return weight_kg(value)
        case LLMDimensions():
            return dimensions_cm(value)
        case LLMAddress():
            return Address(**value.model_dump())
        case LLMTemperature():
            return TemperatureRequirement(**value.model_dump())
        case LLMResponsibilities():
            return Responsibilities(**value.model_dump())
        case BaseModel():
            raise TypeError(f"unhandled LLM type {type(value).__name__}")
        case _:
            return value


def _to_extracted(field: Any, domain_name: str, notes: list[str]) -> Extracted[Any]:
    """Convert one field and validate it against its typed ``Extracted[T]`` on its own, so a bad
    value costs only that field."""
    adapter: TypeAdapter[Extracted[Any]] = TypeAdapter(
        ShipmentRequest.model_fields[domain_name].annotation
    )
    try:
        raw = _build(field)
        return adapter.validate_python(raw.model_dump(mode="json"))
    except (ValidationError, ValueError, TypeError) as e:
        notes.append(f"{domain_name}: dropped invalid value ({e.__class__.__name__})")
        return Extracted[Any](status=FieldStatus.MISSING, confidence=0.0)


def _build(field: Any) -> Extracted[Any]:
    status = FieldStatus(field.status)
    sources = [Source(email_id=s.email_id, quote=s.quote) for s in field.sources]
    confidence = max(0.0, min(1.0, field.confidence))
    if status is FieldStatus.MISSING:
        return Extracted[Any](status=status, confidence=0.0)
    if status is FieldStatus.CONFLICTING:
        candidates = list(dict.fromkeys(_convert(c) for c in field.candidates))
        if len(candidates) < 2:
            # The model said "conflict" but gave one distinct value: treat it as that value.
            if not candidates:
                raise ValueError("conflicting without candidates")
            return Extracted[Any](
                value=candidates[0],
                status=FieldStatus.INFERRED,
                confidence=min(confidence, 0.5),
                sources=sources,
            )
        return Extracted[Any](
            status=status, confidence=confidence, sources=sources, candidates=candidates
        )
    if field.value is None:
        raise ValueError(f"{status} without a value")
    return Extracted[Any](
        value=_convert(field.value), status=status, confidence=confidence, sources=sources
    )


def to_request(output: ShipmentLLMOutput) -> tuple[ShipmentRequest, list[str]]:
    notes: list[str] = []
    fields: dict[str, Extracted[Any]] = {}
    for llm_name in type(output).model_fields:
        if llm_name in ("language", "intent", "suspected_instructions"):
            continue
        domain_name = DOMAIN_NAME.get(llm_name, llm_name)
        fields[domain_name] = _to_extracted(getattr(output, llm_name), domain_name, notes)
    return ShipmentRequest(**fields), notes
