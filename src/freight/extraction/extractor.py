"""Thread -> verified, merged ShipmentRequest.

Pipeline: build prompt -> LLM (or cache) -> provenance check -> unit normalization -> merge with
the previous version. The result carries everything the audit trail and metrics need.
"""

from datetime import date
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from freight.domain.models import ShipmentRequest
from freight.domain.thread import Direction, ThreadMessage
from freight.extraction import injection, provenance
from freight.extraction.llm.port import LLMClient, Usage
from freight.extraction.llm.pricing import cost_usd
from freight.extraction.merge import FieldChange, merge
from freight.extraction.normalize import to_request
from freight.extraction.prompt import PROMPT_VERSION, build_prompt, cache_key, system_prompt
from freight.extraction.schema import Intent, ShipmentLLMOutput

LLM_CACHE_TTL_SECONDS = 7 * 24 * 3600


class LLMCache(Protocol):
    def get(self, key: str) -> dict[str, Any] | None: ...

    def put(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None: ...


class NoCache:
    def get(self, key: str) -> dict[str, Any] | None:
        return None

    def put(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        return None


class ExtractionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    request: ShipmentRequest
    fresh: ShipmentRequest
    changes: list[FieldChange]
    language: str
    intent: Intent
    injection_suspected: bool
    injection_hits: list[str] = Field(default_factory=list)
    downgraded: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    model_id: str
    prompt_version: str = PROMPT_VERSION
    usage: Usage
    latency_ms: int
    cost_usd: float
    cached: bool


def latest_inbound_id(thread: list[ThreadMessage]) -> str:
    inbound = [m for m in thread if m.direction is Direction.INBOUND]
    if not inbound:
        raise ValueError("thread has no customer email")
    return max(inbound, key=lambda m: m.sent_at).email_id


def extract(
    thread: list[ThreadMessage],
    *,
    llm: LLMClient,
    today: date,
    previous: ShipmentRequest | None = None,
    asked: set[str] | None = None,
    cache: LLMCache | None = None,
) -> ExtractionResult:
    cache = cache or NoCache()
    prompt = build_prompt(thread, today)
    key = cache_key(llm.model_id, prompt)

    hit = cache.get(key)
    if hit is not None:
        output = ShipmentLLMOutput.model_validate(hit["output"])
        usage, latency_ms, cached = Usage(), 0, True
    else:
        result = llm.extract(system_prompt(), prompt, ShipmentLLMOutput)
        output, usage, latency_ms, cached = result.parsed, result.usage, result.latency_ms, False
        cache.put(key, {"output": output.model_dump(mode="json")}, LLM_CACHE_TTL_SECONDS)

    report = provenance.verify(output, thread)
    fresh, notes = to_request(output)
    merged, changes = merge(previous, fresh, asked or set(), latest_inbound_id(thread))
    hits = injection.suspicious_spans(thread)
    return ExtractionResult(
        request=merged,
        fresh=fresh,
        changes=changes,
        language=output.language,
        intent=output.intent,
        injection_suspected=output.suspected_instructions or bool(hits),
        injection_hits=hits,
        downgraded=report.downgraded,
        notes=notes,
        model_id=llm.model_id,
        usage=usage,
        latency_ms=latency_ms,
        cost_usd=0.0 if cached else cost_usd(llm.model_id, usage),
        cached=cached,
    )
