"""The DynamoDB cache layer (``freight-cache`` table, TTL attribute ``ttl``).

What lives here (PLAN.md §2.7): LLM results for identical threads, short-lived provider quotes
keyed by lane, estimated transit times, and circuit-breaker state. Static catalogs live in
Lambda memory instead (planning/catalogs, functools.cache).

DynamoDB deletes expired items lazily (up to ~48h late), so reads check ``ttl`` themselves.
Nothing read from here is ever used to book: revalidation always asks the provider live.
"""

import time
from typing import TYPE_CHECKING, Any

import boto3
from botocore.exceptions import ClientError

from freight.persistence.repo import from_item, to_item

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient


def quote_group(provider: str, body: dict[str, Any]) -> str:
    """Invalidation group for a provider request. Matches ``event_group`` for PriceChanged."""
    if provider == "de-trucking":
        return f"{provider}#{str(body['destination_city']).lower()}"
    origin = body.get("origin_port") or body.get("origin_airport")
    destination = body.get("destination_port") or body.get("destination_airport")
    return f"{provider}#{origin}-{destination}"


def event_group(provider: str, lane: str) -> str:
    """Group for a PriceChanged event's ``lane`` (``ORIGIN-DEST``, trucking: ``GATEWAY-city``)."""
    if provider == "de-trucking":
        return f"{provider}#{lane.split('-', 1)[-1].lower()}"
    return f"{provider}#{lane}"


class DynamoCache:
    def __init__(self, table: str, client: "DynamoDBClient | None" = None) -> None:
        self.table = table
        self.db: DynamoDBClient = client or boto3.client("dynamodb")

    def get(self, key: str) -> dict[str, Any] | None:
        r = self.db.get_item(TableName=self.table, Key=to_item({"PK": key, "SK": "V"}))
        if "Item" not in r:
            return None
        item = from_item(r["Item"])
        if item.get("ttl", 0) <= time.time():
            return None
        value: dict[str, Any] = item["value"]
        return value

    def put(
        self, key: str, value: dict[str, Any], ttl_seconds: int, group: str | None = None
    ) -> None:
        item: dict[str, Any] = {
            "PK": key,
            "SK": "V",
            "value": value,
            "ttl": int(time.time()) + ttl_seconds,
        }
        if group:
            item["group"] = group  # lets invalidation find every key of one lane
        self.db.put_item(TableName=self.table, Item=to_item(item))

    def delete(self, key: str) -> None:
        self.db.delete_item(TableName=self.table, Key=to_item({"PK": key, "SK": "V"}))

    def invalidate_group(self, group: str) -> int:
        """Delete every key tagged with ``group`` (GSI ``group-index``). Returns the count."""
        deleted = 0
        kwargs: dict[str, Any] = {
            "TableName": self.table,
            "IndexName": "group-index",
            "KeyConditionExpression": "#g = :g",
            "ExpressionAttributeNames": {"#g": "group"},
            "ExpressionAttributeValues": to_item({":g": group}),
        }
        while True:
            try:
                r = self.db.query(**kwargs)
            except ClientError:
                return deleted
            for item in r.get("Items", []):
                self.db.delete_item(TableName=self.table, Key={"PK": item["PK"], "SK": item["SK"]})
                deleted += 1
            if "LastEvaluatedKey" not in r:
                return deleted
            kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]
