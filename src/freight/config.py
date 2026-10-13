"""Runtime configuration.

Static settings come from Lambda environment variables (set by CDK). Runtime switches (the LLM
kill switch, email mode, chaos knobs) come from SSM Parameter Store under ``PARAM_PREFIX`` and
are re-read every few seconds, so flipping one in the console takes effect without a deploy.
"""

import os
import time
from dataclasses import dataclass
from functools import cache
from typing import Any

PARAM_TTL_SECONDS = 5.0


@dataclass(frozen=True)
class Settings:
    table: str
    cache_table: str
    idempotency_table: str
    mock_table: str
    raw_bucket: str
    state_machine_arn: str
    api_base_url: str
    sender_email: str
    rep_email: str
    bedrock_model_id: str
    llm_provider: str
    region: str
    param_prefix: str
    event_bus: str

    @property
    def mock_base_url(self) -> str:
        return f"{self.api_base_url.rstrip('/')}/mock"


@cache
def settings() -> Settings:
    env = os.environ.get
    return Settings(
        table=env("TABLE_NAME", "freight"),
        cache_table=env("CACHE_TABLE", "freight-cache"),
        idempotency_table=env("IDEMPOTENCY_TABLE", "freight-idempotency"),
        mock_table=env("MOCK_TABLE", "freight-mock"),
        raw_bucket=env("RAW_BUCKET", ""),
        state_machine_arn=env("STATE_MACHINE_ARN", ""),
        api_base_url=env("API_BASE_URL", "http://localhost"),
        sender_email=env("SENDER_EMAIL", ""),
        rep_email=env("REP_EMAIL", ""),
        bedrock_model_id=env("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
        llm_provider=env("LLM_PROVIDER", "bedrock"),
        region=env("AWS_REGION", "us-east-1"),
        param_prefix=env("PARAM_PREFIX", "/freight/demo"),
        event_bus=env("EVENT_BUS", "default"),
    )


class Params:
    """SSM parameters with a short in-memory cache. Missing parameters return the default."""

    def __init__(self, prefix: str, client: Any = None) -> None:
        self.prefix = prefix.rstrip("/")
        self._client = client
        self._cache: dict[str, tuple[float, str | None]] = {}

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3

            self._client = boto3.client("ssm")
        return self._client

    def get(self, name: str, default: str = "", decrypt: bool = False) -> str:
        now = time.monotonic()
        hit = self._cache.get(name)
        if hit and now - hit[0] < PARAM_TTL_SECONDS:
            return default if hit[1] is None else hit[1]
        try:
            r = self.client.get_parameter(Name=f"{self.prefix}/{name}", WithDecryption=decrypt)
            value: str | None = r["Parameter"]["Value"]
        except self.client.exceptions.ParameterNotFound:
            value = None
        self._cache[name] = (now, value)
        return default if value is None else value

    def flag(self, name: str, default: bool) -> bool:
        return self.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")

    def number(self, name: str, default: float) -> float:
        try:
            return float(self.get(name, str(default)))
        except ValueError:
            return default


class StaticParams(Params):
    """Fixed values, for tests and local runs."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        super().__init__("/local")
        self.values = values or {}

    def get(self, name: str, default: str = "", decrypt: bool = False) -> str:
        return self.values.get(name, default)
