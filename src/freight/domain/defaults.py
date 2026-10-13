"""Facts the seller knows that the customer should not be asked for.

A bar ordering beer rarely says where Corona ships from. The default origin is filled in by
code, marked INFERRED with a ``system`` source so the audit trail and the rep can tell it apart
from anything the customer wrote.
"""

from freight.domain.models import Address, Extracted, FieldStatus, ShipmentRequest, Source
from freight.domain.policy import SYSTEM_SOURCE

DEFAULT_ORIGIN = Address(name="Grupo Modelo Brewery", city="Zacatecas", country="MX")


def apply_defaults(request: ShipmentRequest) -> ShipmentRequest:
    if request.pickup.status is not FieldStatus.MISSING:
        return request
    pickup = Extracted[Address](
        value=DEFAULT_ORIGIN,
        status=FieldStatus.INFERRED,
        confidence=1.0,
        sources=[Source(email_id=SYSTEM_SOURCE, quote="default origin: Zacatecas brewery")],
    )
    return request.model_copy(update={"pickup": pickup})
