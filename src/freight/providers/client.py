"""HTTP client for freight providers: timeouts, retries with jitter, and a circuit breaker.

A provider that keeps failing is skipped quickly (circuit open) instead of slowing every order;
the quote step then degrades to fewer options rather than failing (PLAN.md §2.7). Bookings are
retried with the *same* Idempotency-Key, so a retry can never book twice.
"""

import time
from typing import Any, Protocol

import httpx
from tenacity import (
    RetryError,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

FAILURES_TO_OPEN = 5
OPEN_SECONDS = 30
TIMEOUT = httpx.Timeout(5.0, connect=2.0)


class ProviderError(Exception):
    def __init__(self, provider: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.status = status


class ProviderUnavailable(ProviderError):
    """Circuit open, or retries exhausted on a transient failure."""


class ProviderRejected(ProviderError):
    """The provider understood and said no (expired quote, price changed, bad request)."""

    def __init__(self, provider: str, message: str, status: int, body: dict[str, Any]) -> None:
        super().__init__(provider, message, status)
        self.body = body


class BreakerStore(Protocol):
    def get(self, key: str) -> dict[str, Any] | None: ...

    def put(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None: ...


class CircuitBreaker:
    """Consecutive-failure breaker. State is shared across Lambda instances through the cache
    table, with a local copy so a hot instance does not pay a read per call."""

    def __init__(self, provider: str, store: BreakerStore | None = None) -> None:
        self.provider = provider
        self.store = store
        self.failures = 0
        self.open_until = 0.0
        self.just_opened = False

    @property
    def key(self) -> str:
        return f"BREAKER#{self.provider}"

    def _load(self) -> None:
        if self.store is not None and (state := self.store.get(self.key)):
            self.failures = int(state.get("failures", 0))
            self.open_until = float(state.get("open_until", 0))

    def _save(self) -> None:
        if self.store is not None:
            state = {"failures": self.failures, "open_until": self.open_until}
            self.store.put(self.key, state, ttl_seconds=3600)

    def allow(self) -> bool:
        self._load()
        return time.time() >= self.open_until

    def success(self) -> None:
        if self.failures or self.open_until:
            self.failures, self.open_until = 0, 0.0
            self._save()

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= FAILURES_TO_OPEN:
            self.open_until = time.time() + OPEN_SECONDS
            self.just_opened = True
        self._save()


def _transient(e: BaseException) -> bool:
    if isinstance(e, httpx.TransportError):
        return True
    return isinstance(e, ProviderError) and (e.status is None or e.status >= 500 or e.status == 429)


class ProviderClient:
    def __init__(
        self,
        provider: str,
        base_url: str,
        breaker: CircuitBreaker | None = None,
        transport: httpx.BaseTransport | None = None,
        attempts: int = 3,
        backoff_seconds: float = 0.2,
    ) -> None:
        self.provider = provider
        self.breaker = breaker or CircuitBreaker(provider)
        self.http = httpx.Client(
            base_url=f"{base_url.rstrip('/')}/{provider}", timeout=TIMEOUT, transport=transport
        )
        self.attempts = attempts
        self.backoff = backoff_seconds

    def _call(
        self, method: str, path: str, body: dict[str, Any] | None, headers: dict[str, str]
    ) -> dict[str, Any]:
        if not self.breaker.allow():
            raise ProviderUnavailable(self.provider, "circuit open")

        @retry(
            retry=retry_if_exception(_transient),
            stop=stop_after_attempt(self.attempts),
            wait=wait_exponential_jitter(multiplier=self.backoff, max=2.0, jitter=self.backoff),
            reraise=False,
        )
        def attempt() -> dict[str, Any]:
            r = self.http.request(method, path, json=body, headers=headers)
            data: dict[str, Any] = r.json() if r.content else {}
            if r.status_code >= 500 or r.status_code == 429:
                raise ProviderError(self.provider, data.get("error", "server error"), r.status_code)
            if r.status_code >= 400:
                raise ProviderRejected(
                    self.provider, data.get("error", "rejected"), r.status_code, data
                )
            return data

        try:
            result = attempt()
        except RetryError as e:
            self.breaker.failure()
            last = e.last_attempt.exception()
            raise ProviderUnavailable(self.provider, f"gave up: {last}") from e
        except ProviderRejected:
            self.breaker.success()  # the provider is healthy; it just said no
            raise
        self.breaker.success()
        return result

    def quote(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        quotes: list[dict[str, Any]] = self._call("POST", "/quotes", request, {})["quotes"]
        return quotes

    def confirm(self, quote_id: str) -> dict[str, Any]:
        return self._call("GET", f"/quotes/{quote_id}", None, {})

    def book(self, request: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self._call("POST", "/bookings", request, {"Idempotency-Key": idempotency_key})


def in_process_transport(app: Any) -> httpx.MockTransport:
    """Route client calls straight into a ``MockProviders`` instance (tests, local runs)."""
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        resp = app.handle(request.method, request.url.path, dict(request.headers), body)
        return httpx.Response(resp.status, json=resp.body)

    return httpx.MockTransport(handler)
