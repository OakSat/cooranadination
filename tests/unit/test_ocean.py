import pytest
from hypothesis import given
from hypothesis import strategies as st

from freight.domain.models import PalletType, TemperatureRequirement
from freight.planning import catalogs
from freight.planning.cargo import CargoSpec, derive_cargo
from freight.planning.ocean import (
    Binding,
    fcl_options,
    fit,
    lcl_vs_fcl,
    recommend_fcl,
    revenue_tons,
)
from tests.factories import complete_request

SPECS = {s.type: s for s in catalogs.containers()}

BOTTLE_PALLET_KG = 84 * 10.4 + 25  # 898.6, 155 cm, not stackable
CAN_PALLET_KG = 120 * 9.0 + 25  # 1105, 115 cm, stackable


def cans(pallets: int) -> CargoSpec:
    return CargoSpec(
        pallets=pallets,
        pallet_type=PalletType.US,
        pallet_height_cm=115,
        total_weight_kg=pallets * CAN_PALLET_KG,
        stackable=True,
    )


def test_18_bottle_pallets_fit_one_40gp_floor_bound() -> None:
    cargo = derive_cargo(complete_request())
    best = recommend_fcl(cargo)
    assert best is not None
    assert (best.container_type, best.count, best.binding) == ("40GP", 1, Binding.FLOOR)
    # Non-stackable: plenty of headroom, but the floor is what limits.
    assert best.volume_utilization == pytest.approx(0.514, abs=0.01)
    assert fit(cargo, SPECS["20GP"]).count == 2


def test_weight_binds_before_volume_in_40gp() -> None:
    # 24 stackable pallets: 40 positions available, but only 24 x 1105 kg fit under payload.
    f = fit(cans(24), SPECS["40GP"])
    assert f.count == 1
    assert f.binding is Binding.WEIGHT
    assert f.floor_utilization == pytest.approx(0.6)
    assert f.weight_utilization > 0.99


def test_free_volume_does_not_mean_more_cargo_fits() -> None:
    # 40 can pallets would fit one 40GP by space (2 layers x 20 slots), but not by weight.
    f = fit(cans(40), SPECS["40GP"])
    assert f.positions_per_container == 40
    assert f.count == 2
    assert f.binding is Binding.WEIGHT
    assert f.volume_utilization < 0.5


def test_dense_cargo_prefers_two_20gp_over_two_40gp() -> None:
    best = recommend_fcl(cans(40))
    assert best is not None
    assert (best.container_type, best.count) == ("20GP", 2)


def test_reefer_only_when_required() -> None:
    cold = CargoSpec(
        pallets=10,
        pallet_type=PalletType.US,
        pallet_height_cm=155,
        total_weight_kg=10 * BOTTLE_PALLET_KG,
        stackable=False,
        temperature=TemperatureRequirement(required=True, min_c=2, max_c=8),
    )
    assert [f.container_type for f in fcl_options(cold)] == ["40RF"]
    assert "40RF" not in [f.container_type for f in fcl_options(cans(10))]


def test_too_tall_pallet_is_infeasible_in_gp_but_fits_hc() -> None:
    tall = CargoSpec(
        pallets=5,
        pallet_type=PalletType.US,
        pallet_height_cm=240,
        total_weight_kg=5000,
        stackable=False,
    )
    assert not fit(tall, SPECS["40GP"]).feasible
    assert fit(tall, SPECS["40HC"]).feasible


def test_revenue_tons() -> None:
    assert revenue_tons(cbm=2.0, weight_kg=3500) == 3.5
    assert revenue_tons(cbm=4.0, weight_kg=1000) == 4.0


def test_lcl_vs_fcl() -> None:
    small = cans(2)  # ~2.9 cbm, 2.21 t -> 2.9 W/M
    assert lcl_vs_fcl(small, lcl_rate_per_rt=120, fcl_total=2600).load_type == "LCL"
    assert lcl_vs_fcl(cans(12), lcl_rate_per_rt=120, fcl_total=1500).load_type == "FCL"
    assert lcl_vs_fcl(small, lcl_rate_per_rt=None, fcl_total=2600).load_type == "FCL"


@given(
    pallets=st.integers(1, 200),
    kg=st.floats(50, 2000),
    stackable=st.booleans(),
    height=st.floats(60, 220),
)
def test_fit_respects_both_limits(pallets: int, kg: float, stackable: bool, height: float) -> None:
    cargo = CargoSpec(
        pallets=pallets,
        pallet_type=PalletType.US,
        pallet_height_cm=height,
        total_weight_kg=pallets * kg,
        stackable=stackable,
    )
    for spec in catalogs.containers():
        f = fit(cargo, spec)
        if not f.feasible:
            continue
        per_container = -(-pallets // f.count)  # pallets in the fullest box
        assert per_container <= f.positions_per_container
        assert per_container * kg <= spec.max_payload_kg + 1e-6
        more = fit(
            cargo.model_copy(
                update={"pallets": pallets + 1, "total_weight_kg": (pallets + 1) * kg}
            ),
            spec,
        )
        assert more.count >= f.count
