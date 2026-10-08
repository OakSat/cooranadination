"""Air freight: chargeable weight, aircraft type and ULD fit.

Airlines bill the greater of actual and volumetric weight. Beer is dense, so it is almost always
billed on actual weight -- one reason ocean usually wins on price for this product.
"""

import math
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from freight.planning import catalogs
from freight.planning.cargo import CargoSpec
from freight.planning.catalogs import ULDSpec

VOLUMETRIC_DIVISOR_CM3_PER_KG = 6000  # IATA standard: 1 kg per 6,000 cm3
PASSENGER_MAX_PIECE_HEIGHT_CM = 160  # lower-deck contour on wide-body passenger aircraft


class Aircraft(StrEnum):
    PASSENGER = "passenger"
    FREIGHTER = "freighter"


def round_up_half_kg(kg: float) -> float:
    return math.ceil(kg * 2) / 2


def volumetric_weight_kg(length_cm: float, width_cm: float, height_cm: float, pieces: int) -> float:
    return length_cm * width_cm * height_cm * pieces / VOLUMETRIC_DIVISOR_CM3_PER_KG


def chargeable_weight_kg(actual_kg: float, volumetric_kg: float) -> float:
    return round_up_half_kg(max(actual_kg, volumetric_kg))


class ULDFit(BaseModel):
    model_config = ConfigDict(frozen=True)

    uld: str
    pallets_per_uld: int
    count: int


class AirPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    feasible: bool
    reason: str | None = None
    aircraft: Aircraft | None = None
    actual_weight_kg: float = 0.0
    volumetric_weight_kg: float = 0.0
    chargeable_weight_kg: float = 0.0
    billed_on: str | None = None  # "actual" | "volumetric"
    uld: ULDFit | None = None


def _pallets_on_base(uld: ULDSpec, pallet_l: float, pallet_w: float) -> int:
    straight = math.floor(uld.base_length_cm / pallet_l) * math.floor(uld.base_width_cm / pallet_w)
    rotated = math.floor(uld.base_length_cm / pallet_w) * math.floor(uld.base_width_cm / pallet_l)
    return max(straight, rotated)


def fit_uld(cargo: CargoSpec, uld: ULDSpec, aircraft: Aircraft) -> ULDFit | None:
    if cargo.pallet_height_cm > uld.max_height_cm[aircraft.value]:
        return None
    pallet_l, pallet_w = catalogs.pallet_footprint_cm(cargo.pallet_type)
    by_floor = _pallets_on_base(uld, pallet_l, pallet_w)
    by_weight = math.floor((uld.max_gross_kg - uld.tare_kg) / cargo.weight_per_pallet_kg)
    per_uld = min(by_floor, by_weight)
    if per_uld == 0:
        return None
    return ULDFit(uld=uld.code, pallets_per_uld=per_uld, count=math.ceil(cargo.pallets / per_uld))


def plan_air(cargo: CargoSpec) -> AirPlan:
    if cargo.requires_reefer:
        # Active temperature-controlled ULDs exist but are out of scope for the demo catalog.
        return AirPlan(feasible=False, reason="temperature-controlled air not supported")

    pallet_l, pallet_w = catalogs.pallet_footprint_cm(cargo.pallet_type)
    volumetric = volumetric_weight_kg(pallet_l, pallet_w, cargo.pallet_height_cm, cargo.pallets)
    actual = cargo.total_weight_kg
    aircraft = (
        Aircraft.PASSENGER
        if cargo.pallet_height_cm <= PASSENGER_MAX_PIECE_HEIGHT_CM
        else Aircraft.FREIGHTER
    )
    fits = [f for uld in catalogs.ulds() if (f := fit_uld(cargo, uld, aircraft))]
    if not fits:
        return AirPlan(feasible=False, reason="no ULD fits the pallet", aircraft=aircraft)
    tare = {u.code: u.tare_kg for u in catalogs.ulds()}
    best = min(fits, key=lambda f: (f.count, f.count * tare[f.uld]))
    return AirPlan(
        feasible=True,
        aircraft=aircraft,
        actual_weight_kg=round(actual, 1),
        volumetric_weight_kg=round(volumetric, 1),
        chargeable_weight_kg=chargeable_weight_kg(actual, volumetric),
        billed_on="actual" if actual >= volumetric else "volumetric",
        uld=best,
    )
