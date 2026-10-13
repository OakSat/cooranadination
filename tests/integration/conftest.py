import os
import threading
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws
from moto.dynamodb.models import DynamoDBBackend

from freight.persistence.repo import Repo
from freight.persistence.tables import create_all
from freight.providers.cache import DynamoCache

# moto implements a transaction by snapshotting and restoring table state, so two concurrent
# transactions can undo each other's committed writes. Real DynamoDB is atomic per request;
# serialize moto's request handling to match it. Races between a read and a later conditional
# write (what the tests are about) are unaffected.
_MOTO_LOCK = threading.RLock()


def _atomic(method: Any) -> Any:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with _MOTO_LOCK:
            return method(*args, **kwargs)

    return wrapper


for _name in (
    "put_item",
    "update_item",
    "get_item",
    "delete_item",
    "query",
    "transact_write_items",
):
    setattr(DynamoDBBackend, _name, _atomic(getattr(DynamoDBBackend, _name)))

TABLES = {
    "freight": "freight",
    "cache": "freight-cache",
    "idempotency": "freight-idempotency",
    "mock": "freight-mock",
}


@pytest.fixture
def aws() -> Iterator[None]:
    os.environ.update(
        AWS_ACCESS_KEY_ID="testing",
        AWS_SECRET_ACCESS_KEY="testing",
        AWS_SESSION_TOKEN="testing",
        AWS_DEFAULT_REGION="us-east-1",
        AWS_REGION="us-east-1",
    )
    with mock_aws():
        create_all(boto3.client("dynamodb"), TABLES)
        yield


@pytest.fixture
def repo(aws: None) -> Repo:
    return Repo(TABLES["freight"])


@pytest.fixture
def cache(aws: None) -> DynamoCache:
    return DynamoCache(TABLES["cache"])


def email_item(order_id: str, n: int = 1, **extra: Any) -> dict[str, Any]:
    from datetime import UTC, datetime

    from freight.persistence import keys

    sent = datetime(2026, 10, 13, 8, n, tzinfo=UTC)
    return {
        "PK": keys.order_pk(order_id),
        "SK": keys.email_sk(sent, f"e{n}"),
        "type": "email",
        "emailId": f"e{n}",
        "direction": "inbound",
        "sentAt": sent.isoformat(),
        "subject": "Anfrage",
        "text": "18 Paletten",
        **extra,
    }
