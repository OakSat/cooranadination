"""In-process interpreter for ``freight.workflow.graph.STATES``, plus a fully wired local world.

It mirrors what the rendered ASL does: Lambda tasks go through the same ``dispatch`` as the
deployed handlers, Retry on the same error names, Catch to Escalate, waits park a token until
``send_success``. Outbox rows are delivered by calling the same sender the DynamoDB stream
triggers.
"""

import contextlib
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from freight.config import StaticParams
from freight.extraction.llm.fake import FakeLLMClient
from freight.handlers.workflow import dispatch
from freight.persistence.repo import Repo
from freight.providers.cache import DynamoCache, event_group
from freight.providers.client import CircuitBreaker, ProviderClient, in_process_transport
from freight.providers.mock_app import Chaos, MemoryStore, MockProviders
from freight.workflow.deps import Deps
from freight.workflow.graph import RETRY_ERRORS, STATES, End, QuoteFanout, Task, Wait
from freight.workflow.ingest import ingest_raw
from freight.workflow.outbox_sender import send_outbox
from freight.workflow.ports import MemoryMailer, MemoryMetrics, MemoryPublisher, MemoryRawStore

MAX_STEPS = 200


def resolve_path(ctx: dict[str, Any], path: str) -> Any:
    value: Any = ctx
    for part in path.removeprefix("$.").split("."):
        value = value[part]
    return value


@dataclass
class Execution:
    order_id: str
    ctx: dict[str, Any]
    status: str = "RUNNING"
    history: list[str] = field(default_factory=list)


class LocalStateMachine:
    """Implements the ``Workflow`` port; runs executions when ``drain`` is called."""

    def __init__(self) -> None:
        self.deps: Deps | None = None
        self.executions: dict[str, Execution] = {}
        self.tokens: dict[str, tuple[str, str]] = {}  # token -> (order id, wait state)
        self.used_tokens: set[str] = set()
        self.queue: deque[tuple[str, str, dict[str, Any] | None]] = deque()
        self._n = 0

    # Workflow port ---------------------------------------------------------------------------
    def start(self, order_id: str, payload: dict[str, Any]) -> bool:
        if order_id in self.executions:
            return False
        self.executions[order_id] = Execution(order_id, dict(payload))
        route = payload.get("route")
        self.queue.append((order_id, "WaitForHuman" if route == "human" else "Extract", None))
        return True

    def send_success(self, token: str, output: dict[str, Any]) -> bool:
        if token in self.used_tokens or token not in self.tokens:
            return False
        self.used_tokens.add(token)
        order_id, wait_state = self.tokens.pop(token)
        self.queue.append((order_id, wait_state, output))
        return True

    # interpreter -----------------------------------------------------------------------------
    def drain(self) -> None:
        while self.queue:
            order_id, state, decision = self.queue.popleft()
            ex = self.executions[order_id]
            if decision is not None:  # a wait was answered
                ex.ctx["decision"] = decision
                wait = STATES[state]
                assert isinstance(wait, Wait)
                state = wait.routes.get(decision.get("decision", ""), "UnknownRoute")
            self._run(ex, state)

    def _call(self, ex: Execution, payload: dict[str, Any]) -> dict[str, Any]:
        assert self.deps is not None
        for attempt in range(4):
            try:
                return dispatch(self.deps, payload)
            except Exception as e:
                if type(e).__name__ not in RETRY_ERRORS or attempt == 3:
                    raise
        raise AssertionError("unreachable")

    def _run(self, ex: Execution, state: str) -> None:
        for _ in range(MAX_STEPS):
            if state == "UnknownRoute":
                ex.ctx["error"] = {"Error": "UnknownRoute"}
                state = "Escalate"
            ex.history.append(state)
            st = STATES[state]
            if isinstance(st, End):
                ex.status = "SUCCEEDED" if st.succeed else "FAILED"
                return
            if isinstance(st, Wait):
                self._n += 1
                token = f"token-{self._n}"
                self.tokens[token] = (ex.order_id, state)
                self._call(
                    ex,
                    {
                        "orderId": ex.order_id,
                        "step": "wait",
                        "waitingFor": st.waiting_for,
                        "token": token,
                    },
                )
                ex.status = f"WAITING:{state}"
                return
            if isinstance(st, QuoteFanout):
                for provider in st.providers:
                    # A failing branch is caught to Pass {count: 0} in the ASL.
                    with contextlib.suppress(Exception):
                        self._call(
                            ex,
                            {
                                "orderId": ex.order_id,
                                "step": "quote",
                                "provider": provider,
                                "round": resolve_path(ex.ctx, "$.plan.body.round"),
                            },
                        )
                state = st.next
                continue
            assert isinstance(st, Task)
            payload = {"orderId": ex.order_id, "step": st.step}
            payload.update({k: resolve_path(ex.ctx, p) for k, p in st.params.items()})
            try:
                result = self._call(ex, payload)
            except Exception as e:
                if state == "Escalate":
                    ex.status = "FAILED"
                    return
                ex.ctx["error"] = {"Error": type(e).__name__, "Cause": str(e)}
                state = "Escalate"
                continue
            key = st.result_path.removeprefix("$.")
            ex.ctx[key] = {"next": result["next"], "body": result}
            state = st.routes.get(result["next"], "UnknownRoute")
        raise AssertionError(f"{ex.order_id} did not settle: {ex.history[-20:]}")


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


LINK = re.compile(r"https?://\S+/choose\?t=(\S+)")


class LocalWorld:
    """Everything wired in-process: moto DynamoDB, fake LLM, mock providers, local SFN."""

    def __init__(self, tables: dict[str, str], responder: Any, start: datetime) -> None:
        self.clock = Clock(start)
        self.sfn = LocalStateMachine()
        self.chaos = Chaos()
        self.provider_events: list[tuple[str, dict[str, Any]]] = []
        self.providers = MockProviders(
            MemoryStore(),
            lambda: self.chaos,
            publish=self._on_provider_event,
            now=self.clock,
            sleep=lambda _s: None,
        )
        transport = in_process_transport(self.providers)
        self.cache = DynamoCache(tables["cache"])
        self.llm = FakeLLMClient(responder)
        self.mailer = MemoryMailer()
        self.metrics = MemoryMetrics()
        self.raw = MemoryRawStore()
        self.params = StaticParams({"email-mode": "ses", "llm-enabled": "true"})
        breakers: dict[str, CircuitBreaker] = {}
        self.deps = Deps(
            repo=Repo(tables["freight"], clock=self.clock),
            cache=self.cache,
            params=self.params,
            workflow=self.sfn,
            raw=self.raw,
            mailer=self.mailer,
            metrics=self.metrics,
            publisher=MemoryPublisher(),
            llm_factory=lambda: self.llm,
            provider_factory=lambda p: ProviderClient(
                p,
                "http://mock/mock",
                breakers.setdefault(p, CircuitBreaker(p)),
                transport,
                attempts=2,
                backoff_seconds=0,
            ),
            link_key=lambda: b"local-test-hmac-key-0123456789",
            api_base_url="https://api.example",
            sender_email="orders@example.com",
            now=self.clock,
        )
        self.sfn.deps = self.deps

    def _on_provider_event(self, detail_type: str, detail: dict[str, Any]) -> None:
        """What EventBridge + the cache-invalidate Lambda do in AWS."""
        self.provider_events.append((detail_type, detail))
        if detail_type == "PriceChanged":
            self.cache.invalidate_group(event_group(detail["provider"], detail["lane"]))

    @property
    def repo(self) -> Repo:
        return self.deps.repo

    def settle(self) -> None:
        """Run executions and deliver outbox rows until nothing moves."""
        for _ in range(20):
            self.sfn.drain()
            delivered = 0
            for order_id in list(self.sfn.executions):
                for row in self.repo.outbox(order_id):
                    if row["status"] == "PENDING":
                        delivered += send_outbox(self.deps, order_id, row["key"]) == "sent"
            if not delivered and not self.sfn.queue:
                return

    def receive(self, path: Path, **headers: str) -> dict[str, Any]:
        raw = path.read_bytes()
        for name, value in headers.items():
            raw = raw.replace(b"\nSubject:", f"\n{name}: {value}\nSubject:".encode(), 1)
        key = f"inbound/{path.name}"
        self.raw.put(key, raw, "message/rfc822")
        result = ingest_raw(self.deps, raw, key)
        self.settle()
        return result

    def links(self, email_index: int = -1) -> dict[str, str]:
        """Choice links in a sent email, keyed by their label line."""
        body = self.mailer.sent[email_index].get_content()
        out: dict[str, str] = {}
        label = ""
        for line in body.splitlines():
            if re.match(r"^\d\) ", line):
                label = line
            if m := LINK.search(line):
                out[label] = m.group(1)
        return out

    def order_state(self, order_id: str) -> str:
        return self.repo.get_meta(order_id).state.value


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)
