import pytest
from hypothesis import given
from hypothesis import strategies as st

from freight.domain.models import PalletType, TemperatureRequirement
from freight.planning.air import (
    Aircraft,
    chargeable_weight_kg,
    plan_air,
    volumetric_weight_kg,
)
from freight.planning.cargo import CargoSpec, derive_cargo
from tests.factories import complete_request


def test_volumetric_weight_uses_6000_divisor() -> None:
    assert volumetric_weight_kg(120, 80, 150, 1) == pytest.approx(240.0)


def test_chargeable_rounds_up_to_half_kg() -> None:
    assert chargeable_weight_kg(10.1, 3) == 10.5
    assert chargeable_weight_kg(10.5, 3) == 10.5
    assert chargeable_weight_kg(100, 311.06) == 311.5


def test_beer_is_billed_on_actual_weight() -> None:
    plan = plan_air(derive_cargo(complete_request()))
    assert plan.feasible
    assert plan.aircraft is Aircraft.PASSENGER  # 155 cm fits the lower deck
    assert plan.billed_on == "actual"
    assert plan.chargeable_weight_kg == pytest.approx(16175.0)
    assert plan.uld is not None
    assert (plan.uld.uld, plan.uld.pallets_per_uld, plan.uld.count) == ("PMC", 6, 3)


def test_light_bulky_cargo_is_billed_on_volume() -> None:
    cargo = CargoSpec(
        pallets=1,
        pallet_type=PalletType.US,
        pallet_height_cm=150,
        total_weight_kg=100,
        stackable=False,
    )
    plan = plan_air(cargo)
    assert plan.billed_on == "volumetric"
    assert plan.chargeable_weight_kg == 311.5


def test_tall_pallets_need_a_freighter() -> None:
    cargo = CargoSpec(
        pallets=4,
        pallet_type=PalletType.US,
        pallet_height_cm=200,
        total_weight_kg=3000,
        stackable=False,
    )
    plan = plan_air(cargo)
    assert plan.aircraft is Aircraft.FREIGHTER
    assert plan.uld is not None and plan.uld.uld in {"PMC", "PAG"}


def test_temperature_controlled_air_not_supported() -> None:
    cargo = CargoSpec(
        pallets=1,
        pallet_type=PalletType.US,
        pallet_height_cm=100,
        total_weight_kg=500,
        stackable=False,
        temperature=TemperatureRequirement(required=True, min_c=2, max_c=8),
    )
    assert not plan_air(cargo).feasible


@given(actual=st.floats(0.1, 1e5), vol=st.floats(0.1, 1e5))
def test_chargeable_is_at_least_both(actual: float, vol: float) -> None:
    c = chargeable_weight_kg(actual, vol)
    assert c >= actual and c >= vol and c - max(actual, vol) < 0.5
