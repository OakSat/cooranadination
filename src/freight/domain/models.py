"""Core domain models.

Every value that comes out of an email is wrapped in ``Extracted[T]`` so the system always
knows *how* it learned the value (explicit / inferred / missing / conflicting), how sure it is,
and *where* in which email it came from. Downstream code never sees a bare LLM value.
"""

from collections.abc import Callable
from datetime import date
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FieldStatus(StrEnum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"
    MISSING = "missing"
    CONFLICTING = "conflicting"


class Packaging(StrEnum):
    BOTTLES = "bottles"
    CANS = "cans"
    KEGS = "kegs"


class QuantityUnit(StrEnum):
    PALLETS = "pallets"
    CASES = "cases"
    KEGS = "kegs"
    UNITS = "units"  # individual bottles or cans


class PalletType(StrEnum):
    EUR = "EUR"  # 120 x 80 cm
    US = "US"  # 122 x 102 cm (48" x 40")


class ShippingMode(StrEnum):
    OCEAN = "ocean"
    AIR = "air"


class ModePreference(StrEnum):
    OCEAN = "ocean"
    AIR = "air"
    ANY = "any"


class Priority(StrEnum):
    CHEAPEST = "cheapest"
    FASTEST = "fastest"
    BALANCED = "balanced"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(Strict):
    email_id: str
    quote: str = Field(min_length=1, description="Verbatim span of the email the value came from")
    start: int | None = None
    end: int | None = None


class Extracted[T](Strict):
    value: T | None = None
    status: FieldStatus
    confidence: float = Field(ge=0.0, le=1.0)
    sources: list[Source] = Field(default_factory=list)
    candidates: list[T] = Field(default_factory=list)

    @model_validator(mode="after")
    def _status_matches_value(self) -> Self:
        if self.status is FieldStatus.MISSING and self.value is not None:
            raise ValueError("missing field must not carry a value")
        if self.status in (FieldStatus.EXPLICIT, FieldStatus.INFERRED) and self.value is None:
            raise ValueError(f"{self.status} field must carry a value")
        if self.status is FieldStatus.CONFLICTING and len(self.candidates) < 2:
            raise ValueError("conflicting field needs at least two candidates")
        return self

    @property
    def known(self) -> bool:
        return self.status in (FieldStatus.EXPLICIT, FieldStatus.INFERRED)


def missing(tp: Any) -> Callable[[], Any]:
    """default_factory for a field that has not been seen yet."""
    return lambda: Extracted[tp](status=FieldStatus.MISSING, confidence=0.0)


class Quantity(Strict):
    amount: int = Field(gt=0)
    unit: QuantityUnit


class Dimensions(Strict):
    length_cm: float = Field(gt=0)
    width_cm: float = Field(gt=0)
    height_cm: float = Field(gt=0)
    pieces: int = Field(default=1, gt=0)


class Address(Strict):
    name: str | None = None
    street: str | None = None
    city: str = Field(min_length=1)
    postal_code: str | None = None
    country: str = Field(min_length=2, max_length=2, description="ISO 3166-1 alpha-2")

    @field_validator("country")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


class TemperatureRequirement(Strict):
    required: bool
    min_c: float | None = None
    max_c: float | None = None

    @model_validator(mode="after")
    def _range(self) -> Self:
        if self.min_c is not None and self.max_c is not None and self.min_c > self.max_c:
            raise ValueError("min_c must be <= max_c")
        return self


Party = Literal["seller", "buyer"]


class Responsibilities(Strict):
    customs: Party | None = None
    insurance: Party | None = None
    local_transport: Party | None = None


class ShipmentRequest(Strict):
    quantity: Extracted[Quantity] = Field(default_factory=missing(Quantity))
    packaging: Extracted[Packaging] = Field(default_factory=missing(Packaging))
    total_weight_kg: Extracted[float] = Field(default_factory=missing(float))
    dimensions: Extracted[Dimensions] = Field(default_factory=missing(Dimensions))
    pickup: Extracted[Address] = Field(default_factory=missing(Address))
    delivery: Extracted[Address] = Field(default_factory=missing(Address))
    requested_delivery_date: Extracted[date] = Field(default_factory=missing(date))
    mode_preference: Extracted[ModePreference] = Field(default_factory=missing(ModePreference))
    priority: Extracted[Priority] = Field(default_factory=missing(Priority))
    temperature_control: Extracted[TemperatureRequirement] = Field(
        default_factory=missing(TemperatureRequirement)
    )
    # Captured for the audit trail; acting on them is out of scope for now.
    incoterms: Extracted[str] = Field(default_factory=missing(str))
    responsibilities: Extracted[Responsibilities] = Field(default_factory=missing(Responsibilities))

    def fields(self) -> dict[str, Extracted[Any]]:
        return {name: getattr(self, name) for name in type(self).model_fields}

    def with_status(self, status: FieldStatus) -> list[str]:
        return [name for name, f in self.fields().items() if f.status is status]
