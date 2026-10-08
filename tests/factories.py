from datetime import date
from typing import Any

from freight.domain.models import (
    Address,
    Extracted,
    FieldStatus,
    Packaging,
    Quantity,
    QuantityUnit,
    ShipmentRequest,
    Source,
)


def explicit(value: Any, quote: str = "from email", email_id: str = "e1") -> Extracted[Any]:
    return Extracted(
        value=value,
        status=FieldStatus.EXPLICIT,
        confidence=0.95,
        sources=[Source(email_id=email_id, quote=quote)],
    )


BERLIN = Address(name="Biergarten Berlin GmbH", city="Berlin", postal_code="10115", country="DE")
ZACATECAS = Address(name="Grupo Modelo Brewery", city="Zacatecas", country="MX")


def complete_request(**overrides: Any) -> ShipmentRequest:
    fields: dict[str, Any] = {
        "quantity": explicit(Quantity(amount=18, unit=QuantityUnit.PALLETS), "18 Paletten"),
        "packaging": explicit(Packaging.BOTTLES, "Flaschen"),
        "pickup": explicit(ZACATECAS, "ab Brauerei Zacatecas"),
        "delivery": explicit(BERLIN, "Biergarten Berlin, 10115 Berlin"),
        "requested_delivery_date": explicit(date(2026, 12, 15), "bis 15.12.2026"),
    }
    fields.update(overrides)
    return ShipmentRequest(**fields)
