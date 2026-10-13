"""Mock freight providers behind one HTTP API: ``ocean-carrier``, ``air-carrier``, ``de-trucking``.

They behave like the real thing in the ways that matter for correctness:
- quotes expire (``valid_until``) and prices can move (chaos ``price-drift-pct``);
- a quote can be re-confirmed live (``GET /quotes/{id}``), which is what revalidation uses;
- bookings honour ``Idempotency-Key``: the same key always returns the same booking;
- chaos switches make them slow, flaky, or down (SSM ``/freight/demo/chaos/*``).

Routes: ``POST /mock/{provider}/quotes``, ``GET /mock/{provider}/quotes/{id}``,
``POST /mock/{provider}/bookings``.
"""

import json
import math
import random
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

PROVIDERS = ("ocean-carrier", "air-carrier", "de-trucking")
PRICE_TOLERANCE = Decimal("0.005")  # a booking at a price off by more than 0.5% is refused

OCEAN_BASE_USD = {"20GP": 2400, "40GP": 3100, "40HC": 3250, "40RF": 6500}
OCEAN_SERVICES = (
    # name, transit days, price factor, carrier
    ("FCL direct", 22, Decimal("1.20"), "Atlantic Express Line"),
    ("FCL via Algeciras", 30, Decimal("1.00"), "Pacifico Shipping"),
)
LCL_RATE_PER_RT = Decimal("95")
AIR_SERVICES = (
    # name, departs in days, transit days, USD per chargeable kg, route
    ("Express direct", 1, 2, Decimal("5.20"), "direct"),
    ("Standard direct", 2, 4, Decimal("3.60"), "direct"),
    ("Consolidated via MAD", 3, 6, Decimal("2.80"), "via MAD"),
)
FREIGHTER_SURCHARGE = Decimal("1.15")
# Rough road distances (km) from gateway to city, enough to make prices plausible.
ROAD_KM = {
    ("DEHAM", "berlin"): 290,
    ("DEHAM", "hamburg"): 15,
    ("DEHAM", "munich"): 780,
    ("DEHAM", "munchen"): 780,
    ("DEHAM", "frankfurt"): 490,
    ("DEBRV", "bremen"): 65,
    ("DEBRV", "cologne"): 330,
    ("DEBRV", "koln"): 330,
    ("DEHAM", "bremen"): 125,
    ("BER", "berlin"): 25,
    ("HAM", "hamburg"): 12,
    ("MUC", "munich"): 40,
    ("MUC", "munchen"): 40,
    ("FRA", "frankfurt"): 15,
    ("BRE", "bremen"): 8,
    ("CGN", "cologne"): 18,
    ("CGN", "koln"): 18,
}
PALLETS_PER_TRUCK = 33
TRUCK_PAYLOAD_KG = 24000


def money(x: Decimal | float | int) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


class MockStore(Protocol):
    def put(self, key: str, value: dict[str, Any], ttl_seconds: int | None = None) -> None: ...

    def get(self, key: str) -> dict[str, Any] | None: ...

    def put_if_absent(self, key: str, value: dict[str, Any]) -> dict[str, Any] | None:
        """Store ``value`` unless ``key`` exists; return the existing value, else None."""
        ...


class MemoryStore:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}

    def put(self, key: str, value: dict[str, Any], ttl_seconds: int | None = None) -> None:
        self.data[key] = value

    def get(self, key: str) -> dict[str, Any] | None:
        return self.data.get(key)

    def put_if_absent(self, key: str, value: dict[str, Any]) -> dict[str, Any] | None:
        if key in self.data:
            return self.data[key]
        self.data[key] = value
        return None


@dataclass
class Chaos:
    failure_rate: float = 0.0
    latency_ms: int = 0
    price_drift_pct: float = 0.0
    down: set[str] = field(default_factory=set)


@dataclass
class Response:
    status: int
    body: dict[str, Any]


Publisher = Callable[[str, dict[str, Any]], None]


class MockProviders:
    def __init__(
        self,
        store: MockStore,
        chaos: Callable[[], Chaos],
        publish: Publisher | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        validity_minutes: int = 60,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store = store
        self.chaos = chaos
        self.publish = publish or (lambda _t, _d: None)
        self.now = now
        self.validity = timedelta(minutes=validity_minutes)
        self.rng = rng or random.Random()
        self.sleep = sleep

    # --- routing ----------------------------------------------------------------------------

    def handle(
        self, method: str, path: str, headers: dict[str, str], body: dict[str, Any]
    ) -> Response:
        parts = [p for p in path.split("/") if p]
        if parts[:1] == ["mock"]:
            parts = parts[1:]
        if not parts or parts[0] not in PROVIDERS:
            return Response(404, {"error": "unknown provider"})
        provider, rest = parts[0], parts[1:]

        chaos = self.chaos()
        if chaos.latency_ms:
            self.sleep(chaos.latency_ms / 1000)
        if provider in chaos.down:
            return Response(503, {"error": f"{provider} is down"})
        if chaos.failure_rate and self.rng.random() < chaos.failure_rate:
            return Response(503, {"error": "transient failure"})

        lowered = {k.lower(): v for k, v in headers.items()}
        try:
            if method == "POST" and rest == ["quotes"]:
                return self._quote(provider, body, chaos)
            if method == "GET" and len(rest) == 2 and rest[0] == "quotes":
                return self._confirm(provider, rest[1], chaos)
            if method == "POST" and rest == ["bookings"]:
                return self._book(provider, body, lowered.get("idempotency-key", ""), chaos)
        except (KeyError, ValueError, TypeError) as e:
            return Response(400, {"error": f"bad request: {e}"})
        return Response(404, {"error": "no such route"})

    # --- quotes -----------------------------------------------------------------------------

    def _drift(self, chaos: Chaos) -> Decimal:
        return Decimal(1) + Decimal(str(chaos.price_drift_pct)) / 100

    def _issue(
        self, provider: str, lane: str, base: Decimal, chaos: Chaos, offer: dict[str, Any]
    ) -> dict[str, Any]:
        now = self.now()
        quote_id = f"{provider[:3].upper()}-{secrets.token_hex(5)}"
        price = money(base * self._drift(chaos))
        valid_until = now + self.validity
        quote = {
            **offer,
            "quote_id": quote_id,
            "provider": provider,
            "lane": lane,
            "base_price": str(money(base)),
            "price": str(price),
            "currency": "USD",
            "valid_until": valid_until.isoformat(),
            "issued_at": now.isoformat(),
        }
        ttl = int(self.validity.total_seconds()) + 86400
        self.store.put(f"QUOTE#{quote_id}", quote, ttl)
        self._note_price(provider, lane, offer.get("service", ""), price)
        return quote

    def _note_price(self, provider: str, lane: str, service: str, price: Decimal) -> None:
        key = f"LASTPRICE#{provider}#{lane}#{service}"
        last = self.store.get(key)
        self.store.put(key, {"price": str(price)})
        if last is not None and Decimal(last["price"]) != price:
            self.publish(
                "PriceChanged",
                {
                    "provider": provider,
                    "lane": lane,
                    "service": service,
                    "old": last["price"],
                    "new": str(price),
                },
            )

    def _quote(self, provider: str, body: dict[str, Any], chaos: Chaos) -> Response:
        ready = date.fromisoformat(body["ready_date"])
        if provider == "ocean-carrier":
            quotes = self._ocean(body, ready, chaos)
        elif provider == "air-carrier":
            quotes = self._air(body, ready, chaos)
        else:
            quotes = self._truck(body, chaos)
        return Response(200, {"quotes": quotes})

    @staticmethod
    def _next_weekday(d: date, weekday: int) -> date:
        return d + timedelta(days=(weekday - d.weekday()) % 7)

    def _ocean(self, body: dict[str, Any], ready: date, chaos: Chaos) -> list[dict[str, Any]]:
        lane = f"{body['origin_port']}-{body['destination_port']}"
        out: list[dict[str, Any]] = []
        for plan in body.get("fcl_plans", []):
            base = sum(OCEAN_BASE_USD[c["type"]] * c["count"] for c in plan["containers"])
            boxes = " + ".join(f"{c['count']}x{c['type']}" for c in plan["containers"])
            for name, transit, factor, carrier in OCEAN_SERVICES:
                departure = self._next_weekday(ready, 3)  # weekly Thursday sailing
                out.append(
                    self._issue(
                        "ocean-carrier",
                        lane,
                        Decimal(base) * factor,
                        chaos,
                        {
                            "service": f"{name}, {boxes}",
                            "carrier": carrier,
                            "load": "FCL",
                            "containers": plan["containers"],
                            "departure": departure.isoformat(),
                            "arrival": (departure + timedelta(days=transit)).isoformat(),
                        },
                    )
                )
        if body.get("lcl_revenue_tons"):
            departure = self._next_weekday(ready, 0)
            rt = Decimal(str(body["lcl_revenue_tons"]))
            out.append(
                self._issue(
                    "ocean-carrier",
                    lane,
                    rt * LCL_RATE_PER_RT,
                    chaos,
                    {
                        "service": f"LCL consolidated, {rt} RT",
                        "carrier": "Pacifico Consolidators",
                        "load": "LCL",
                        "departure": departure.isoformat(),
                        "arrival": (departure + timedelta(days=35)).isoformat(),
                    },
                )
            )
        return out

    def _air(self, body: dict[str, Any], ready: date, chaos: Chaos) -> list[dict[str, Any]]:
        lane = f"{body['origin_airport']}-{body['destination_airport']}"
        kg = Decimal(str(body["chargeable_weight_kg"]))
        freighter = body.get("aircraft") == "freighter"
        uld = body.get("uld", {})
        out = []
        for name, dep_in, transit, rate, route in AIR_SERVICES:
            if freighter and route == "via MAD":
                continue  # the connecting leg is passenger-only in this mock
            departure = ready + timedelta(days=dep_in)
            price = kg * rate * (FREIGHTER_SURCHARGE if freighter else 1)
            out.append(
                self._issue(
                    "air-carrier",
                    lane,
                    price,
                    chaos,
                    {
                        "service": f"{name}, {uld.get('count', '?')}x{uld.get('code', 'ULD')}, "
                        f"{'freighter' if freighter else 'passenger'}",
                        "carrier": "Aerocarga" if freighter else "Lufthansa Cargo (mock)",
                        "route": route,
                        "departure": departure.isoformat(),
                        "arrival": (departure + timedelta(days=transit)).isoformat(),
                    },
                )
            )
        return out

    def _truck(self, body: dict[str, Any], chaos: Chaos) -> list[dict[str, Any]]:
        city = body["destination_city"].lower()
        trucks = max(
            math.ceil(body["pallets"] / PALLETS_PER_TRUCK),
            math.ceil(body["weight_kg"] / TRUCK_PAYLOAD_KG),
        )
        out = []
        for origin in body["origins"]:
            km = ROAD_KM.get((origin, city), 400)
            per_truck = Decimal(250) + Decimal("1.9") * km
            if body.get("reefer"):
                per_truck *= Decimal("1.4")
            out.append(
                self._issue(
                    "de-trucking",
                    f"{origin}-{city}",
                    per_truck * trucks,
                    chaos,
                    {
                        "service": f"{trucks} truck(s) {origin} -> {body['destination_city']}",
                        "carrier": "Spedition Nord (mock)",
                        "origin": origin,
                        "trucks": trucks,
                        "transit_days": 1 if km < 500 else 2,
                    },
                )
            )
        return out

    # --- confirm & book ---------------------------------------------------------------------

    def _current(self, quote: dict[str, Any], chaos: Chaos) -> Decimal:
        return money(Decimal(quote["base_price"]) * self._drift(chaos))

    def _confirm(self, provider: str, quote_id: str, chaos: Chaos) -> Response:
        quote = self.store.get(f"QUOTE#{quote_id}")
        if quote is None or quote["provider"] != provider:
            return Response(404, {"error": "unknown quote"})
        price = self._current(quote, chaos)
        self._note_price(provider, quote["lane"], quote.get("service", ""), price)
        expired = self.now() >= datetime.fromisoformat(quote["valid_until"])
        return Response(
            200,
            {
                "quote_id": quote_id,
                "price": str(price),
                "quoted_price": quote["price"],
                "currency": "USD",
                "available": not expired,
                "expired": expired,
                "valid_until": quote["valid_until"],
            },
        )

    def _book(self, provider: str, body: dict[str, Any], key: str, chaos: Chaos) -> Response:
        if not key:
            return Response(400, {"error": "Idempotency-Key header required"})
        previous = self.store.get(f"BOOKING#{provider}#{key}")
        if previous is not None:
            return Response(200, {**previous, "replayed": True})

        quote = self.store.get(f"QUOTE#{body['quote_id']}")
        if quote is None or quote["provider"] != provider:
            return Response(404, {"error": "unknown quote"})
        if self.now() >= datetime.fromisoformat(quote["valid_until"]):
            return Response(409, {"error": "quote_expired"})
        price = self._current(quote, chaos)
        expected = Decimal(str(body["expected_price"]))
        if abs(price - expected) > expected * PRICE_TOLERANCE:
            return Response(409, {"error": "price_changed", "price": str(price)})

        booking: dict[str, Any] = {
            "booking_ref": f"{provider[:2].upper()}B-{secrets.token_hex(4).upper()}",
            "quote_id": body["quote_id"],
            "price": str(price),
            "currency": "USD",
            "service": quote.get("service", ""),
            "carrier": quote.get("carrier", ""),
            "booked_at": self.now().isoformat(),
            **{k: quote[k] for k in ("departure", "arrival") if k in quote},
        }
        if provider == "de-trucking" and body.get("pickup_date"):
            pickup = date.fromisoformat(body["pickup_date"])
            booking["delivery"] = (pickup + timedelta(days=quote["transit_days"])).isoformat()
        existing = self.store.put_if_absent(f"BOOKING#{provider}#{key}", booking)
        if existing is not None:  # lost a race with a concurrent identical request
            return Response(200, {**existing, "replayed": True})
        return Response(201, booking)


# --- Lambda wiring ----------------------------------------------------------------------------


class DynamoMockStore:
    def __init__(self, table: str) -> None:
        import boto3

        self.table = boto3.resource("dynamodb").Table(table)

    def put(self, key: str, value: dict[str, Any], ttl_seconds: int | None = None) -> None:
        item: dict[str, Any] = {"PK": key, "SK": "V", "value": json.dumps(value)}
        if ttl_seconds:
            item["ttl"] = int(time.time()) + ttl_seconds
        self.table.put_item(Item=item)

    def get(self, key: str) -> dict[str, Any] | None:
        item = self.table.get_item(Key={"PK": key, "SK": "V"}, ConsistentRead=True).get("Item")
        return json.loads(str(item["value"])) if item else None

    def put_if_absent(self, key: str, value: dict[str, Any]) -> dict[str, Any] | None:
        from botocore.exceptions import ClientError

        try:
            self.table.put_item(
                Item={"PK": key, "SK": "V", "value": json.dumps(value)},
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return self.get(key)
            raise
        return None
