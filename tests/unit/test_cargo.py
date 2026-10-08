import pytest

from freight.domain.models import (
    Dimensions,
    Packaging,
    PalletType,
    Quantity,
    QuantityUnit,
    ShipmentRequest,
)
from freight.planning.cargo import CargoDerivationError, derive_cargo
from tests.factories import complete_request, explicit


def test_pallets_of_bottles_use_catalog_weight() -> None:
    cargo = derive_cargo(complete_request())
    assert cargo.pallets == 18
    assert cargo.pallet_type is PalletType.US
    assert cargo.total_weight_kg == pytest.approx(18 * (84 * 10.4 + 25))
    assert not cargo.stackable


def test_kegs_are_palletized() -> None:
    req = complete_request(
        quantity=explicit(Quantity(amount=40, unit=QuantityUnit.KEGS)),
        packaging=explicit(Packaging.KEGS),
    )
    cargo = derive_cargo(req)
    assert cargo.pallets == 4  # 12 kegs per pallet
    assert cargo.total_weight_kg == pytest.approx(40 * 62.5 + 4 * 25)


def test_individual_units_convert_via_cases() -> None:
    req = complete_request(quantity=explicit(Quantity(amount=2400, unit=QuantityUnit.UNITS)))
    cargo = derive_cargo(req)
    assert cargo.pallets == 2  # 100 cases / 84 per pallet
    assert cargo.total_weight_kg == pytest.approx(100 * 10.4 + 2 * 25)


def test_stated_weight_and_dimensions_override_catalog() -> None:
    req = complete_request(
        total_weight_kg=explicit(15000.0),
        dimensions=explicit(Dimensions(length_cm=122, width_cm=102, height_cm=140, pieces=18)),
    )
    cargo = derive_cargo(req)
    assert cargo.total_weight_kg == 15000.0
    assert cargo.pallet_height_cm == 140


def test_requires_quantity_and_packaging() -> None:
    with pytest.raises(CargoDerivationError):
        derive_cargo(ShipmentRequest())
