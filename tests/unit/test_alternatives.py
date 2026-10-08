from datetime import date, datetime, timedelta
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from freight.domain.models import Priority, ShippingMode
from freight.planning.alternatives import Label, ShippingOption, select_alternatives

NOW = datetime(2026, 10, 8, 12, 0)
VALID = NOW + timedelta(minutes=15)


def opt(
    oid: str,
    price: int,
    transit: int,
    mode: ShippingMode = ShippingMode.OCEAN,
    valid_until: datetime = VALID,
) -> ShippingOption:
    dep = date(2026, 10, 12)
    return ShippingOption(
        option_id=oid,
        mode=mode,
        service=oid,
        carrier="mock",
        provider_quote_id=f"q-{oid}",
        price=Decimal(price),
        departure=dep,
        arrival=dep + timedelta(days=transit),
        valid_until=valid_until,
    )


OPTIONS = [
    opt("ocean-std", 4200, 30),
    opt("ocean-direct", 5100, 22),
    opt("air-consol", 31000, 6, ShippingMode.AIR),
    opt("air-express", 46000, 3, ShippingMode.AIR),
]


def test_cheapest_fastest_balanced() -> None:
    alts = select_alternatives(OPTIONS, Priority.BALANCED, now=NOW)
    labels = {a.option.option_id: a.labels for a in alts}
    assert labels["ocean-std"] == [Label.CHEAPEST]
    assert labels["air-express"] == [Label.FASTEST]
    assert labels["ocean-direct"] == [Label.BALANCED]
    assert alts[0].option.option_id == "ocean-direct"  # priority decides what is listed first


def test_priority_orders_the_list() -> None:
    alts = select_alternatives(OPTIONS, Priority.FASTEST, now=NOW)
    assert alts[0].option.option_id == "air-express"


def test_expired_and_late_options_are_dropped() -> None:
    stale = opt("stale", 100, 20, valid_until=NOW - timedelta(seconds=1))
    alts = select_alternatives([*OPTIONS, stale], deliver_by=date(2026, 11, 5), now=NOW)
    ids = {a.option.option_id for a in alts}
    assert "stale" not in ids
    assert "ocean-std" not in ids  # arrives 2026-11-11, after the deadline


def test_single_option_gets_all_labels() -> None:
    alts = select_alternatives([opt("only", 4000, 25)], now=NOW)
    assert len(alts) == 1
    assert set(alts[0].labels) == set(Label)


def test_nothing_usable() -> None:
    assert select_alternatives([], now=NOW) == []


@given(
    st.lists(st.tuples(st.integers(100, 50000), st.integers(1, 45)), min_size=1, max_size=12),
    st.sampled_from(list(Priority)),
)
def test_at_most_three_distinct_and_all_labels_assigned(
    raw: list[tuple[int, int]], priority: Priority
) -> None:
    options = [opt(f"o{i}", p, t) for i, (p, t) in enumerate(raw)]
    alts = select_alternatives(options, priority, now=NOW)
    ids = [a.option.option_id for a in alts]
    assert 1 <= len(alts) <= 3
    assert len(ids) == len(set(ids))
    assigned = [label for a in alts for label in a.labels]
    assert sorted(assigned) == sorted(Label)
    cheapest = next(a for a in alts if Label.CHEAPEST in a.labels)
    assert cheapest.option.price == min(o.price for o in options)
