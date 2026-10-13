"""Table definitions shared by tests (moto) and local tooling. CDK mirrors these in
``infra/stacks/storage_stack.py``; ``tests/integration/test_infra.py`` checks they agree."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

S = {"AttributeType": "S"}


def _attrs(*names: str) -> list[Any]:
    return [{"AttributeName": n, **S} for n in names]


def _gsi(name: str, pk: str, sk: str | None = None) -> dict[str, Any]:
    schema = [{"AttributeName": pk, "KeyType": "HASH"}]
    if sk:
        schema.append({"AttributeName": sk, "KeyType": "RANGE"})
    return {"IndexName": name, "KeySchema": schema, "Projection": {"ProjectionType": "ALL"}}


PK_SK = [{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}]

FREIGHT = {
    "KeySchema": PK_SK,
    "AttributeDefinitions": _attrs("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK"),
    "GlobalSecondaryIndexes": [_gsi("GSI1", "GSI1PK", "GSI1SK"), _gsi("GSI2", "GSI2PK", "GSI2SK")],
    "BillingMode": "PAY_PER_REQUEST",
    "StreamSpecification": {"StreamEnabled": True, "StreamViewType": "NEW_IMAGE"},
}
CACHE = {
    "KeySchema": PK_SK,
    "AttributeDefinitions": _attrs("PK", "SK", "group"),
    "GlobalSecondaryIndexes": [_gsi("group-index", "group")],
    "BillingMode": "PAY_PER_REQUEST",
}
IDEMPOTENCY = {
    "KeySchema": [{"AttributeName": "id", "KeyType": "HASH"}],
    "AttributeDefinitions": _attrs("id"),
    "BillingMode": "PAY_PER_REQUEST",
}
MOCK = {
    "KeySchema": PK_SK,
    "AttributeDefinitions": _attrs("PK", "SK"),
    "BillingMode": "PAY_PER_REQUEST",
}


def create_all(db: "DynamoDBClient", names: dict[str, str]) -> None:
    for kind, spec in (
        ("freight", FREIGHT),
        ("cache", CACHE),
        ("idempotency", IDEMPOTENCY),
        ("mock", MOCK),
    ):
        db.create_table(TableName=names[kind], **spec)
