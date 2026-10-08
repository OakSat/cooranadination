"""Pick up to three customer-facing alternatives: cheapest, fastest, balanced.

Input options are priced, scheduled offers that came back from providers (the source of truth
for price and availability). This module only filters and ranks; it never invents a number.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from freight.domain.models import Priority, ShippingMode


class Label(StrEnum):
    CHEAPEST = "cheapest"
    FASTEST = "fastest"
    BALANCED = "balanced"


class ShippingOption(BaseModel):
    model_config = ConfigDict(frozen=True)

    option_id: str
    mode: ShippingMode
    service: str  # e.g. "FCL 1x40GP" or "Air standard, PMC x3, via MAD"
    carrier: str
    provider_quote_id: str
    price: Decimal = Field(gt=0)
    currency: str = "USD"
    departure: date
    arrival: date
    valid_until: datetime

    @property
    def transit_days(self) -> int:
        return (self.arrival - self.departure).days


class Alternative(BaseModel):
    model_config = ConfigDict(frozen=True)

    option: ShippingOption
    labels: list[Label]


# Weight of price vs. transit time in the balanced score, by customer priority.
PRICE_WEIGHT: dict[Priority, float] = {
    Priority.CHEAPEST: 0.7,
    Priority.BALANCED: 0.5,
    Priority.FASTEST: 0.3,
}


def _normalized(value: float, lo: float, hi: float) -> float:
    return 0.0 if hi == lo else (value - lo) / (hi - lo)


def select_alternatives(
    options: list[ShippingOption],
    priority: Priority = Priority.BALANCED,
    deliver_by: date | None = None,
    now: datetime | None = None,
) -> list[Alternative]:
    """Return at most three distinct options, each tagged with the label(s) it earned.

    Options that are expired or arrive after ``deliver_by`` are dropped. The customer's
    priority decides which alternative is listed first.
    """
    usable = [
        o
        for o in options
        if (now is None or o.valid_until > now) and (deliver_by is None or o.arrival <= deliver_by)
    ]
    if not usable:
        return []

    cheapest = min(usable, key=lambda o: (o.price, o.arrival))
    fastest = min(usable, key=lambda o: (o.arrival, o.price))

    prices = [float(o.price) for o in usable]
    arrivals = [o.arrival.toordinal() for o in usable]
    w = PRICE_WEIGHT[priority]

    def score(o: ShippingOption) -> float:
        p = _normalized(float(o.price), min(prices), max(prices))
        t = _normalized(o.arrival.toordinal(), min(arrivals), max(arrivals))
        return w * p + (1 - w) * t

    picked: dict[str, list[Label]] = {}
    picked.setdefault(cheapest.option_id, []).append(Label.CHEAPEST)
    picked.setdefault(fastest.option_id, []).append(Label.FASTEST)
    # Balanced must add something new when possible: prefer an option not already picked.
    rest = [o for o in usable if o.option_id not in picked] or usable
    balanced = min(rest, key=lambda o: (score(o), o.price))
    picked.setdefault(balanced.option_id, []).append(Label.BALANCED)

    by_id = {o.option_id: o for o in usable}
    alternatives = [Alternative(option=by_id[i], labels=labels) for i, labels in picked.items()]

    first = {
        Priority.CHEAPEST: Label.CHEAPEST,
        Priority.FASTEST: Label.FASTEST,
        Priority.BALANCED: Label.BALANCED,
    }[priority]
    alternatives.sort(key=lambda a: (first not in a.labels, a.option.price))
    return alternatives
