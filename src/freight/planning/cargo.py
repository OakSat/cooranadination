"""Turn an extracted request into a physical cargo description, deterministically.

The LLM says "40 Fässer"; this module says how many pallets, how heavy, how tall -- from the
product catalog, never from the model.
"""

import math

from pydantic import BaseModel, ConfigDict, Field

from freight.domain.models import (
    PalletType,
    QuantityUnit,
    ShipmentRequest,
    TemperatureRequirement,
)
from freight.planning import catalogs


class CargoDerivationError(ValueError):
    pass


class CargoSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    pallets: int = Field(gt=0)
    pallet_type: PalletType
    pallet_height_cm: float = Field(gt=0)
    total_weight_kg: float = Field(gt=0)
    stackable: bool
    temperature: TemperatureRequirement | None = None

    @property
    def requires_reefer(self) -> bool:
        return self.temperature is not None and self.temperature.required

    @property
    def weight_per_pallet_kg(self) -> float:
        return self.total_weight_kg / self.pallets

    @property
    def cbm(self) -> float:
        length, width = catalogs.pallet_footprint_cm(self.pallet_type)
        return self.pallets * length * width * self.pallet_height_cm / 1e6


def catalog_weight_kg(request: ShipmentRequest) -> float | None:
    """Weight implied by quantity x catalog unit weight (used to cross-check stated weight)."""
    if not (request.quantity.value and request.packaging.value):
        return None
    spec = catalogs.product(request.packaging.value)
    pallets = _pallet_count(request)
    cases = _case_count(request, spec.units_per_pallet)
    return cases * spec.unit_weight_kg + pallets * catalogs.pallet_tare_kg()


def _case_count(request: ShipmentRequest, units_per_pallet: int) -> float:
    qty = request.quantity.value
    assert qty is not None
    match qty.unit:
        case QuantityUnit.PALLETS:
            return qty.amount * units_per_pallet
        case QuantityUnit.UNITS:
            return qty.amount / catalogs.units_per_case()
        case QuantityUnit.CASES | QuantityUnit.KEGS:
            return qty.amount


def _pallet_count(request: ShipmentRequest) -> int:
    qty, packaging = request.quantity.value, request.packaging.value
    assert qty is not None and packaging is not None
    if qty.unit is QuantityUnit.PALLETS:
        return qty.amount
    spec = catalogs.product(packaging)
    return math.ceil(_case_count(request, spec.units_per_pallet) / spec.units_per_pallet)


def derive_cargo(request: ShipmentRequest) -> CargoSpec:
    if not (request.quantity.known and request.packaging.known):
        raise CargoDerivationError("quantity and packaging are required to derive cargo")
    packaging = request.packaging.value
    assert packaging is not None
    spec = catalogs.product(packaging)

    stated_weight = request.total_weight_kg.value if request.total_weight_kg.known else None
    weight = stated_weight or catalog_weight_kg(request)
    assert weight is not None

    height = spec.pallet_height_cm
    dims = request.dimensions.value if request.dimensions.known else None
    if dims is not None:
        height = dims.height_cm

    temperature = request.temperature_control.value if request.temperature_control.known else None
    return CargoSpec(
        pallets=_pallet_count(request),
        pallet_type=spec.pallet_type,
        pallet_height_cm=height,
        total_weight_kg=weight,
        stackable=spec.stackable,
        temperature=temperature,
    )
