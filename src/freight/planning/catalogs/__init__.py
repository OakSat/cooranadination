"""Static reference catalogs, loaded once per Lambda container (the in-memory cache layer).

They change only on deploy, so a deploy is the invalidation.
"""

import json
import unicodedata
from functools import cache
from importlib.resources import files
from typing import Any

from pydantic import BaseModel, ConfigDict

from freight.domain.models import Address, PalletType


class ContainerSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: str
    name: str
    internal_length_cm: float
    internal_width_cm: float
    internal_height_cm: float
    door_height_cm: float
    max_payload_kg: float
    floor_slots: dict[PalletType, int]
    reefer: bool
    relative_cost: float

    @property
    def cbm(self) -> float:
        return self.internal_length_cm * self.internal_width_cm * self.internal_height_cm / 1e6


class ULDSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    name: str
    base_length_cm: float
    base_width_cm: float
    max_height_cm: dict[str, float]  # aircraft type -> usable contour height
    max_gross_kg: float
    tare_kg: float


class ProductSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_unit: str
    unit_weight_kg: float
    units_per_pallet: int
    pallet_height_cm: float
    stackable: bool
    pallet_type: PalletType


class Location(BaseModel):
    model_config = ConfigDict(frozen=True)

    city: str
    country: str
    port: str
    airport: str


def _load(name: str) -> Any:
    return json.loads(files(__package__).joinpath(name).read_text(encoding="utf-8"))


@cache
def containers() -> tuple[ContainerSpec, ...]:
    return tuple(ContainerSpec(**c) for c in _load("containers.json")["containers"])


@cache
def ulds() -> tuple[ULDSpec, ...]:
    return tuple(ULDSpec(**u) for u in _load("ulds.json")["ulds"])


@cache
def _products_raw() -> dict[str, Any]:
    data: dict[str, Any] = _load("products.json")
    return data


def product(packaging: str) -> ProductSpec:
    return ProductSpec(**_products_raw()["products"][packaging])


def pallet_tare_kg() -> float:
    return float(_products_raw()["pallet_tare_kg"])


def units_per_case() -> int:
    return int(_products_raw()["units_per_case"])


def pallet_footprint_cm(pallet_type: PalletType) -> tuple[float, float]:
    length, width = _products_raw()["pallet_dims_cm"][pallet_type.value]
    return float(length), float(width)


def _normalize_city(city: str) -> str:
    decomposed = unicodedata.normalize("NFKD", city)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).strip().lower()


@cache
def _locations() -> dict[str, Any]:
    data: dict[str, Any] = _load("locations.json")
    return data


def resolve_location(address: Address) -> Location | None:
    entry = _locations()["cities"].get(_normalize_city(address.city))
    if entry is None or entry["country"] != address.country:
        return None
    return Location(city=address.city, **entry)
