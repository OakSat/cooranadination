"""The Lambda entry points, invoked with API Gateway / SQS / EventBridge-shaped events (moto)."""

import importlib
import json
from collections.abc import Iterator
from typing import Any

import boto3
import pytest

from tests.integration.conftest import TABLES

MODULES = (
    "workflow",
    "ingest",
    "send_outbox",
    "rep_api",
    "choose",
    "mock_provider",
    "cache_invalidate",
)


class Context:
    function_name = "freight-test"
    memory_limit_in_mb = 256
    invoked_function_arn = "arn:aws:lambda:us-east-1:123456789012:function:freight-test"
    aws_request_id = "req-1"

    @staticmethod
    def get_remaining_time_in_millis() -> int:
        return 30_000


@pytest.fixture
def env(aws: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TABLE_NAME", TABLES["freight"])
    monkeypatch.setenv("CACHE_TABLE", TABLES["cache"])
    monkeypatch.setenv("IDEMPOTENCY_TABLE", TABLES["idempotency"])
    monkeypatch.setenv("MOCK_TABLE", TABLES["mock"])
    monkeypatch.setenv("RAW_BUCKET", "raw")
    monkeypatch.setenv("API_BASE_URL", "https://api.example")
    monkeypatch.setenv("POWERTOOLS_METRICS_NAMESPACE", "Freight")
    boto3.client("s3").create_bucket(Bucket="raw")
    ssm = boto3.client("ssm")
    ssm.put_parameter(Name="/freight/demo/link-hmac-key", Value="k" * 32, Type="SecureString")
    ssm.put_parameter(Name="/freight/demo/chaos/price-drift-pct", Value="0", Type="String")
    from freight.config import settings
    from freight.workflow.deps import aws_deps

    settings.cache_clear()
    aws_deps.cache_clear()
    yield
    settings.cache_clear()
    aws_deps.cache_clear()


def http(
    method: str,
    path: str,
    body: Any = None,
    query: dict[str, str] | None = None,
    claims: dict[str, str] | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "version": "2.0",
        "routeKey": f"{method} {path}",
        "rawPath": path,
        "rawQueryString": "&".join(f"{k}={v}" for k, v in (query or {}).items()),
        "queryStringParameters": query,
        "headers": {"content-type": "application/json"},
        "requestContext": {
            "http": {"method": method, "path": path, "sourceIp": "1.2.3.4", "userAgent": "t"},
            "requestId": "r",
            "stage": "$default",
        },
        "isBase64Encoded": False,
        "body": json.dumps(body) if isinstance(body, dict) else body,
    }
    if claims:
        event["requestContext"]["authorizer"] = {"jwt": {"claims": claims, "scopes": None}}
    return event


@pytest.mark.parametrize("module", MODULES)
def test_handler_modules_import(env: None, module: str) -> None:
    mod = importlib.import_module(f"freight.handlers.{module}")
    assert callable(mod.handler)


def test_rep_queue_and_dashboard(env: None) -> None:
    from freight.handlers import rep_api

    page = rep_api.handler(http("GET", "/dashboard"), Context())
    assert page["statusCode"] == 200 and "Freight Rep Desk" in page["body"]
    queue = rep_api.handler(
        http("GET", "/rep/queue", claims={"email": "rep@example.com"}), Context()
    )
    assert queue["statusCode"] == 200
    assert set(json.loads(queue["body"])) == {"review", "human", "quoted", "asking", "completed"}
    missing = rep_api.handler(http("GET", "/rep/orders/FC-NOPE00"), Context())
    assert missing["statusCode"] == 404


def test_rep_simulate_writes_to_inbound(env: None) -> None:
    from freight.handlers import rep_api

    r = rep_api.handler(
        http("POST", "/rep/simulate", {"subject": "Hi", "text": "18 pallets"}), Context()
    )
    key = json.loads(r["body"])["s3Key"]
    assert key.startswith("inbound/simulated/")
    raw = boto3.client("s3").get_object(Bucket="raw", Key=key)["Body"].read()
    assert b"18 pallets" in raw


def test_choose_rejects_bad_tokens(env: None) -> None:
    from freight.handlers import choose

    page = choose.handler(http("GET", "/choose", query={"t": "forged.token"}), Context())
    assert page["statusCode"] == 400
    post = choose.handler(http("POST", "/choose", body="t=forged.token"), Context())
    assert post["statusCode"] == 400 and "invalid" in post["body"].lower()


def test_mock_provider_quotes_over_http(env: None) -> None:
    from freight.handlers import mock_provider

    mock_provider.app.cache_clear()
    body = {
        "origins": ["DEHAM"],
        "destination_city": "Berlin",
        "pallets": 18,
        "weight_kg": 16175,
        "ready_date": "2026-10-16",
    }
    r = mock_provider.handler(http("POST", "/mock/de-trucking/quotes", body), Context())
    assert r["statusCode"] == 200
    assert json.loads(r["body"])["quotes"][0]["origin"] == "DEHAM"


def test_cache_invalidate_on_price_changed(env: None) -> None:
    from freight.handlers import cache_invalidate
    from freight.providers.cache import DynamoCache

    cache = DynamoCache(TABLES["cache"])
    cache.put(
        "QUOTES#ocean-carrier#MXZLO-DEHAM#x", {"q": 1}, 600, group="ocean-carrier#MXZLO-DEHAM"
    )
    event = {"detail": {"provider": "ocean-carrier", "lane": "MXZLO-DEHAM"}}
    assert cache_invalidate.handler(event, Context()) == {"deleted": 1}
