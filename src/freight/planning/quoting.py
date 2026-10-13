"""From a cargo plan to provider requests, and from provider offers to ranked options.

Code decides *what to ask for* (which container plans, which ULDs, chargeable weight, which
gateways) from the deterministic planners; providers answer with prices and schedules; code
then normalizes, adds the German truck leg, and picks up to three alternatives.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from freight.domain.models import ModePreference, Priority, ShipmentRequest, ShippingMode
from freight.planning import catalogs
from freight.planning.air import AirPlan, plan_air
from freight.planning.alternatives import Alternative, ShippingOption, select_alternatives
from freight.planning.cargo import CargoSpec
from freight.planning.ocean import LCL_MAX_CBM, ContainerFit, fcl_options, revenue_tons

EXPORT_HANDLING_DAYS = 3  # brewery to port/airport, export clearance
FCL_PLANS_TO_QUOTE = 2


class PlanningError(ValueError):
    pass


class CargoPlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    cargo: CargoSpec
    origin_port: str
    origin_airport: str
    destination_port: str
    destination_airport: str
    destination_city: str
    destination_postal_code: str | None
    modes: list[ShippingMode]
    fcl: list[ContainerFit]
    air: AirPlan | None
    ready_date: date
    summary: str


def _modes(pref: ModePreference | None) -> list[ShippingMode]:
    if pref is ModePreference.OCEAN:
        return [ShippingMode.OCEAN]
    if pref is ModePreference.AIR:
        return [ShippingMode.AIR]
    return [ShippingMode.OCEAN, ShippingMode.AIR]


def describe(cargo: CargoSpec, fcl: list[ContainerFit], air: AirPlan | None) -> str:
    parts = [
        f"{cargo.pallets} {cargo.pallet_type.value} pallets, {cargo.total_weight_kg:,.0f} kg, "
        f"{'stackable' if cargo.stackable else 'not stackable'}, {cargo.cbm:.1f} m3"
    ]
    for f in fcl:
        parts.append(
            f"FCL {f.count}x{f.container_type} ({f.binding} bound, "
            f"weight {f.weight_utilization:.0%}, floor {f.floor_utilization:.0%}, "
            f"volume {f.volume_utilization:.0%})"
        )
    if air is not None:
        if air.feasible and air.uld is not None:
            parts.append(
                f"air {air.uld.count}x{air.uld.uld} on {air.aircraft}, chargeable "
                f"{air.chargeable_weight_kg:,.0f} kg (billed on {air.billed_on})"
            )
        else:
            parts.append(f"air not feasible: {air.reason}")
    return "; ".join(parts)


def plan(cargo: CargoSpec, request: ShipmentRequest, today: date) -> CargoPlan:
    pickup, delivery = request.pickup.value, request.delivery.value
    if pickup is None or delivery is None:
        raise PlanningError("pickup and delivery are required")
    origin = catalogs.resolve_location(pickup)
    dest = catalogs.resolve_location(delivery)
    if origin is None or dest is None:
        raise PlanningError("address does not resolve to a gateway")

    modes = _modes(request.mode_preference.value if request.mode_preference.known else None)
    fcl = fcl_options(cargo)[:FCL_PLANS_TO_QUOTE] if ShippingMode.OCEAN in modes else []
    air = plan_air(cargo) if ShippingMode.AIR in modes else None
    if not fcl and not (air and air.feasible):
        raise PlanningError("no feasible ocean or air plan for this cargo")
    return CargoPlan(
        cargo=cargo,
        origin_port=origin.port,
        origin_airport=origin.airport,
        destination_port=dest.port,
        destination_airport=dest.airport,
        destination_city=delivery.city,
        destination_postal_code=delivery.postal_code,
        modes=modes,
        fcl=fcl,
        air=air,
        ready_date=today + timedelta(days=EXPORT_HANDLING_DAYS),
        summary=describe(cargo, fcl, air),
    )


def provider_requests(p: CargoPlan) -> dict[str, dict[str, Any]]:
    """Request body per provider. A provider is absent when its mode is not wanted/feasible."""
    out: dict[str, dict[str, Any]] = {}
    origins: list[str] = []
    if p.fcl:
        cbm = p.cargo.cbm
        req: dict[str, Any] = {
            "origin_port": p.origin_port,
            "destination_port": p.destination_port,
            "ready_date": p.ready_date.isoformat(),
            "reefer": p.cargo.requires_reefer,
            "fcl_plans": [
                {"containers": [{"type": f.container_type, "count": f.count}]} for f in p.fcl
            ],
        }
        if not p.cargo.requires_reefer and cbm <= LCL_MAX_CBM:
            rt = revenue_tons(cbm, p.cargo.total_weight_kg)
            req["lcl_revenue_tons"] = round(rt, 2)
        out["ocean-carrier"] = req
        origins.append(p.destination_port)
    if p.air is not None and p.air.feasible and p.air.uld is not None:
        out["air-carrier"] = {
            "origin_airport": p.origin_airport,
            "destination_airport": p.destination_airport,
            "ready_date": p.ready_date.isoformat(),
            "chargeable_weight_kg": p.air.chargeable_weight_kg,
            "aircraft": p.air.aircraft.value if p.air.aircraft else "passenger",
            "uld": {"code": p.air.uld.uld, "count": p.air.uld.count},
        }
        origins.append(p.destination_airport)
    out["de-trucking"] = {
        "origins": origins,
        "destination_city": p.destination_city,
        "postal_code": p.destination_postal_code,
        "pallets": p.cargo.pallets,
        "weight_kg": p.cargo.total_weight_kg,
        "reefer": p.cargo.requires_reefer,
        "ready_date": p.ready_date.isoformat(),
    }
    return out


class QuotedOption(BaseModel):
    """One customer-facing option: main leg + German truck leg, priced by providers."""

    model_config = ConfigDict(frozen=True)

    option: ShippingOption  # price is the total the customer pays
    provider: str
    main_price: Decimal
    gateway: str
    last_mile_price: Decimal | None = None
    last_mile_quote_id: str | None = None
    last_mile_valid_until: datetime | None = None


def build_options(p: CargoPlan, quotes: dict[str, list[dict[str, Any]]]) -> list[QuotedOption]:
    trucks = {q["origin"]: q for q in quotes.get("de-trucking", [])}
    out: list[QuotedOption] = []
    for provider, mode, gateway in (
        ("ocean-carrier", ShippingMode.OCEAN, p.destination_port),
        ("air-carrier", ShippingMode.AIR, p.destination_airport),
    ):
        truck = trucks.get(gateway)
        for q in quotes.get(provider, []):
            main = Decimal(q["price"])
            last_mile = Decimal(truck["price"]) if truck else None
            valid = datetime.fromisoformat(q["valid_until"])
            truck_valid = datetime.fromisoformat(truck["valid_until"]) if truck else None
            arrival = date.fromisoformat(q["arrival"])
            if truck:
                arrival += timedelta(days=int(truck.get("transit_days", 1)))
            out.append(
                QuotedOption(
                    option=ShippingOption(
                        option_id=q["quote_id"],
                        mode=mode,
                        service=q["service"],
                        carrier=q["carrier"],
                        provider_quote_id=q["quote_id"],
                        price=main + (last_mile or 0),
                        currency=q.get("currency", "USD"),
                        departure=date.fromisoformat(q["departure"]),
                        arrival=arrival,
                        valid_until=min(valid, truck_valid) if truck_valid else valid,
                    ),
                    provider=provider,
                    main_price=main,
                    gateway=gateway,
                    last_mile_price=last_mile,
                    last_mile_quote_id=truck["quote_id"] if truck else None,
                    last_mile_valid_until=truck_valid,
                )
            )
    return out


def rank(
    options: list[QuotedOption],
    priority: Priority,
    deliver_by: date | None,
    now: datetime,
) -> list[tuple[QuotedOption, Alternative]]:
    by_id = {o.option.option_id: o for o in options}
    picked = select_alternatives([o.option for o in options], priority, deliver_by, now)
    return [(by_id[a.option.option_id], a) for a in picked]
