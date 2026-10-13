from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from freight.domain.models import ModePreference, Priority, ShippingMode
from freight.planning.alternatives import Label
from freight.planning.cargo import derive_cargo
from freight.planning.quoting import build_options, plan, provider_requests, rank
from freight.providers.client import (
    FAILURES_TO_OPEN,
    CircuitBreaker,
    ProviderClient,
    ProviderRejected,
    ProviderUnavailable,
    in_process_transport,
)
from freight.providers.mock_app import Chaos, MemoryStore, MockProviders
from tests.factories import complete_request, explicit

TODAY = date(2026, 10, 13)
NOW = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class World:
    """Mock providers + clients wired in-process."""

    def __init__(self) -> None:
        self.chaos = Chaos()
        self.clock = Clock()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.app = MockProviders(
            MemoryStore(),
            lambda: self.chaos,
            publish=lambda t, d: self.events.append((t, d)),
            now=self.clock,
            sleep=lambda _s: None,
        )
        self.transport = in_process_transport(self.app)

    def client(self, provider: str, breaker: CircuitBreaker | None = None) -> ProviderClient:
        return ProviderClient(
            provider, "http://mock/mock", breaker, self.transport, backoff_seconds=0
        )


@pytest.fixture
def world() -> World:
    return World()


def demo_plan(**overrides: Any):  # type: ignore[no-untyped-def]
    req = complete_request(**overrides)
    return plan(derive_cargo(req), req, TODAY)


def quote_all(world: World, p: Any) -> dict[str, list[dict[str, Any]]]:
    quotes: dict[str, list[dict[str, Any]]] = {}
    for provider, body in provider_requests(p).items():
        try:
            quotes[provider] = world.client(provider).quote(body)
        except ProviderUnavailable:
            quotes[provider] = []
    return quotes


def test_demo_cargo_plan_matches_the_runbook() -> None:
    p = demo_plan()
    assert p.fcl[0].container_type == "40GP" and p.fcl[0].count == 1
    assert "floor bound" in p.summary
    assert p.air is not None and p.air.billed_on == "actual"
    assert (p.destination_port, p.destination_airport) == ("DEHAM", "BER")


def test_requests_only_for_wanted_modes() -> None:
    ocean_only = demo_plan(mode_preference=explicit(ModePreference.OCEAN))
    reqs = provider_requests(ocean_only)
    assert set(reqs) == {"ocean-carrier", "de-trucking"}
    assert reqs["de-trucking"]["origins"] == ["DEHAM"]
    assert "lcl_revenue_tons" not in reqs["ocean-carrier"]  # 35 m3 is too big for LCL


def test_three_ranked_alternatives_with_truck_leg(world: World) -> None:
    p = demo_plan()
    options = build_options(p, quote_all(world, p))
    ranked = rank(options, Priority.BALANCED, date(2026, 12, 15), NOW)
    assert len(ranked) == 3
    labels = {label for _, alt in ranked for label in alt.labels}
    assert labels == {Label.CHEAPEST, Label.FASTEST, Label.BALANCED}
    for quoted, _ in ranked:
        assert quoted.last_mile_price is not None
        assert quoted.option.price == quoted.main_price + quoted.last_mile_price
    cheapest = next(q for q, a in ranked if Label.CHEAPEST in a.labels)
    fastest = next(q for q, a in ranked if Label.FASTEST in a.labels)
    assert cheapest.option.mode is ShippingMode.OCEAN
    assert fastest.option.mode is ShippingMode.AIR


def test_dead_provider_degrades_to_fewer_options(world: World) -> None:
    world.chaos.down = {"air-carrier"}
    p = demo_plan()
    quotes = quote_all(world, p)
    assert quotes["air-carrier"] == []
    ranked = rank(build_options(p, quotes), Priority.BALANCED, date(2026, 12, 15), NOW)
    assert ranked and all(q.option.mode is ShippingMode.OCEAN for q, _ in ranked)


def test_breaker_opens_after_repeated_failures(world: World) -> None:
    world.chaos.down = {"air-carrier"}
    breaker = CircuitBreaker("air-carrier")
    client = world.client("air-carrier", breaker)
    body = provider_requests(demo_plan())["air-carrier"]
    for _ in range(FAILURES_TO_OPEN):
        with pytest.raises(ProviderUnavailable):
            client.quote(body)
    assert breaker.just_opened
    world.chaos.down = set()  # recovered, but the circuit is still open
    with pytest.raises(ProviderUnavailable, match="circuit open"):
        client.quote(body)


def test_transient_failures_are_retried(world: World) -> None:
    calls = {"n": 0}
    real = world.app.handle

    def flaky(*args: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            from freight.providers.mock_app import Response

            return Response(503, {"error": "blip"})
        return real(*args)

    world.app.handle = flaky  # type: ignore[method-assign]
    quotes = world.client("ocean-carrier").quote(provider_requests(demo_plan())["ocean-carrier"])
    assert quotes and calls["n"] == 2


def test_booking_is_idempotent_per_key(world: World) -> None:
    client = world.client("ocean-carrier")
    q = client.quote(provider_requests(demo_plan())["ocean-carrier"])[0]
    body = {"quote_id": q["quote_id"], "expected_price": q["price"]}
    first = client.book(body, "key-1")
    again = client.book(body, "key-1")
    assert first["booking_ref"] == again["booking_ref"]
    assert again["replayed"] is True
    assert client.book(body, "key-2")["booking_ref"] != first["booking_ref"]


def test_price_drift_is_caught_before_booking(world: World) -> None:
    client = world.client("ocean-carrier")
    q = client.quote(provider_requests(demo_plan())["ocean-carrier"])[0]
    world.chaos.price_drift_pct = 12
    live = client.confirm(q["quote_id"])
    assert float(live["price"]) == pytest.approx(float(q["price"]) * 1.12, rel=0.001)
    assert world.events and world.events[-1][0] == "PriceChanged"
    with pytest.raises(ProviderRejected) as e:
        client.book({"quote_id": q["quote_id"], "expected_price": q["price"]}, "k")
    assert e.value.body["error"] == "price_changed"


def test_expired_quote_cannot_be_booked(world: World) -> None:
    client = world.client("ocean-carrier")
    q = client.quote(provider_requests(demo_plan())["ocean-carrier"])[0]
    world.clock.now = NOW + timedelta(hours=2)
    assert client.confirm(q["quote_id"])["expired"] is True
    with pytest.raises(ProviderRejected, match="quote_expired"):
        client.book({"quote_id": q["quote_id"], "expected_price": q["price"]}, "k")


def test_truck_booking_returns_delivery_date(world: World) -> None:
    client = world.client("de-trucking")
    q = client.quote(provider_requests(demo_plan())["de-trucking"])[0]
    booking = client.book(
        {"quote_id": q["quote_id"], "expected_price": q["price"], "pickup_date": "2026-11-14"},
        "truck-key",
    )
    assert booking["delivery"] == "2026-11-15"
