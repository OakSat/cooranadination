"""Everything a workflow step needs, built once per Lambda container."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import cache

from freight.config import Params, settings
from freight.extraction.llm.port import LLMClient
from freight.persistence.repo import Repo
from freight.providers.cache import DynamoCache
from freight.providers.client import CircuitBreaker, ProviderClient
from freight.workflow.ports import Mailer, Metrics, Publisher, RawStore, Workflow


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Deps:
    repo: Repo
    cache: DynamoCache
    params: Params
    workflow: Workflow
    raw: RawStore
    mailer: Mailer
    metrics: Metrics
    publisher: Publisher
    llm_factory: Callable[[], LLMClient]
    provider_factory: Callable[[str], ProviderClient]
    link_key: Callable[[], bytes]
    api_base_url: str
    sender_email: str
    rep_email: str = ""
    now: Callable[[], datetime] = field(default=utcnow)

    def today(self) -> date:
        return self.now().date()

    @property
    def llm(self) -> LLMClient:
        return self.llm_factory()


@cache
def aws_deps() -> Deps:
    """Production wiring from environment variables (set by CDK)."""
    from freight.extraction.llm.bedrock import client_from_config
    from freight.observability.metrics import PowertoolsMetrics
    from freight.workflow.ports import (
        EventBridgePublisher,
        S3RawStore,
        SesMailer,
        StepFunctions,
    )

    s = settings()
    params = Params(s.param_prefix)
    cache_ = DynamoCache(s.cache_table)

    @cache
    def llm() -> LLMClient:
        return client_from_config(s.llm_provider, s.bedrock_model_id, s.region)

    breakers: dict[str, CircuitBreaker] = {}

    def provider(name: str) -> ProviderClient:
        breaker = breakers.setdefault(name, CircuitBreaker(name, cache_))
        return ProviderClient(name, s.mock_base_url, breaker)

    def link_key() -> bytes:
        return params.get("link-hmac-key", decrypt=True).encode()

    return Deps(
        repo=Repo(s.table),
        cache=cache_,
        params=params,
        workflow=StepFunctions(s.state_machine_arn),
        raw=S3RawStore(s.raw_bucket),
        mailer=SesMailer(),
        metrics=PowertoolsMetrics(),
        publisher=EventBridgePublisher(s.event_bus),
        llm_factory=llm,
        provider_factory=provider,
        link_key=link_key,
        api_base_url=s.api_base_url,
        sender_email=s.sender_email,
        rep_email=s.rep_email,
    )
