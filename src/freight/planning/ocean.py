"""Ocean freight: container fit and LCL vs FCL.

Key lesson encoded here: floor space and payload are *independent* limits. A container can be
"full" by weight while most of its volume is empty (dense cargo like beer), or "full" by floor
slots while there is headroom above non-stackable pallets. Free volume never means more cargo fits.
"""

import math
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from freight.planning import catalogs
from freight.planning.cargo import CargoSpec
from freight.planning.catalogs import ContainerSpec

STACKING_CLEARANCE_CM = 5
LCL_MAX_CBM = 15.0  # above this, consolidators rarely offer LCL and FCL is nearly always cheaper


class Binding(StrEnum):
    WEIGHT = "weight"
    FLOOR = "floor"


class ContainerFit(BaseModel):
    model_config = ConfigDict(frozen=True)

    container_type: str
    feasible: bool
    reason: str | None = None
    count: int = 0
    layers: int = 0
    positions_per_container: int = 0
    weight_utilization: float = 0.0
    floor_utilization: float = 0.0
    volume_utilization: float = 0.0
    binding: Binding | None = None


def fit(cargo: CargoSpec, spec: ContainerSpec) -> ContainerFit:
    """How many containers of ``spec`` the cargo needs, and which limit drives that number."""
    if cargo.requires_reefer and not spec.reefer:
        return ContainerFit(container_type=spec.type, feasible=False, reason="needs reefer")
    if spec.reefer and not cargo.requires_reefer:
        return ContainerFit(container_type=spec.type, feasible=False, reason="reefer not needed")
    if cargo.pallet_height_cm > spec.door_height_cm:
        return ContainerFit(container_type=spec.type, feasible=False, reason="pallet too tall")
    if cargo.weight_per_pallet_kg > spec.max_payload_kg:
        return ContainerFit(container_type=spec.type, feasible=False, reason="pallet too heavy")

    stack_two = 2 * cargo.pallet_height_cm + STACKING_CLEARANCE_CM <= spec.internal_height_cm
    layers = 2 if cargo.stackable and stack_two else 1
    positions = spec.floor_slots[cargo.pallet_type] * layers

    # Pallets are indivisible, so weight is checked per container, not just in aggregate.
    pallets_by_weight = math.floor(spec.max_payload_kg / cargo.weight_per_pallet_kg)
    per_container = min(positions, pallets_by_weight)
    count = math.ceil(cargo.pallets / per_container)

    weight_util = cargo.total_weight_kg / (count * spec.max_payload_kg)
    floor_util = cargo.pallets / (count * positions)
    return ContainerFit(
        container_type=spec.type,
        feasible=True,
        count=count,
        layers=layers,
        positions_per_container=positions,
        weight_utilization=round(weight_util, 4),
        floor_utilization=round(floor_util, 4),
        volume_utilization=round(cargo.cbm / (count * spec.cbm), 4),
        binding=Binding.WEIGHT if pallets_by_weight < positions else Binding.FLOOR,
    )


def fcl_options(cargo: CargoSpec) -> list[ContainerFit]:
    """Feasible FCL plans, best first by estimated relative cost, then fewer boxes."""
    cost = {spec.type: spec.relative_cost for spec in catalogs.containers()}
    feasible = [f for spec in catalogs.containers() if (f := fit(cargo, spec)).feasible]
    return sorted(feasible, key=lambda f: (f.count * cost[f.container_type], f.count))


def recommend_fcl(cargo: CargoSpec) -> ContainerFit | None:
    options = fcl_options(cargo)
    return options[0] if options else None


def revenue_tons(cbm: float, weight_kg: float) -> float:
    """LCL is billed per W/M: the greater of cubic metres and metric tonnes."""
    return max(cbm, weight_kg / 1000)


LoadType = Literal["LCL", "FCL"]


class LoadDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    load_type: LoadType
    lcl_cost: float | None
    fcl_cost: float
    reason: str


def lcl_vs_fcl(cargo: CargoSpec, lcl_rate_per_rt: float | None, fcl_total: float) -> LoadDecision:
    """Compare provider prices. Rates come from providers at runtime; this only does the math."""
    if cargo.requires_reefer or lcl_rate_per_rt is None or cargo.cbm > LCL_MAX_CBM:
        return LoadDecision(
            load_type="FCL", lcl_cost=None, fcl_cost=fcl_total, reason="LCL not applicable"
        )
    lcl_cost = round(revenue_tons(cargo.cbm, cargo.total_weight_kg) * lcl_rate_per_rt, 2)
    if lcl_cost < fcl_total:
        return LoadDecision(
            load_type="LCL", lcl_cost=lcl_cost, fcl_cost=fcl_total, reason="LCL cheaper"
        )
    return LoadDecision(
        load_type="FCL", lcl_cost=lcl_cost, fcl_cost=fcl_total, reason="FCL cheaper or equal"
    )
