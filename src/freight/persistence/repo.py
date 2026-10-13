"""DynamoDB repository: the only code that writes order state.

Correctness rests on three DynamoDB features:
- **Conditional transitions.** ``transition`` updates META only if ``state`` and ``version`` are
  what the caller read, and the edge is legal in ``domain/state.py``. Two workers racing on the
  same order: exactly one wins, the other gets ``StaleOrder``.
- **One transaction per business step.** The state change, its audit EVENT, and the rows it
  causes (outbox email, quote options, extraction snapshot) commit together or not at all.
- **Deterministic keys + attribute_not_exists** for things that must exist at most once
  (outbox rows, quote rounds, bookings, the Message-ID index).

Tenant isolation: every item carries ``customerId`` and transitions are conditioned on it.
"""

import json
import secrets
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError
from pydantic import BaseModel, ConfigDict, Field

from freight.domain.models import ShipmentRequest
from freight.domain.state import TERMINAL, OrderState, assert_transition
from freight.email.outbox import OutboxMessage, OutboxStatus
from freight.email.threading import OpenOrder
from freight.persistence import keys

if TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBClient

_ser = TypeSerializer()
_de = TypeDeserializer()


class StaleOrder(Exception):
    """Someone else moved the order first (state/version/customer condition failed)."""


class AlreadyExists(Exception):
    """A write-once item (outbox row, booking, quote round, Message-ID) is already there."""


class OrderNotFound(Exception):
    pass


def to_item(data: dict[str, Any]) -> dict[str, Any]:
    # DynamoDB has no float type: round-trip through JSON so every number becomes a Decimal.
    plain = json.loads(json.dumps(data, default=str), parse_float=Decimal)
    return {k: _ser.serialize(v) for k, v in plain.items() if v is not None}


def from_item(item: dict[str, Any]) -> dict[str, Any]:
    return {k: _plain(_de.deserialize(v)) for k, v in item.items()}


def _plain(v: Any) -> Any:
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, set)):
        return [_plain(x) for x in v]
    return v


def _now() -> datetime:
    return datetime.now(UTC)


class OrderMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str
    customer_id: str
    customer_email: str
    customer_name: str = ""
    state: OrderState
    version: int
    created_at: datetime
    updated_at: datetime
    language: str = "en"
    extraction_version: int = 0
    quote_round: int = 0
    asked_fields: list[str] = Field(default_factory=list)
    waiting_task_token: str | None = None
    waiting_for: str | None = None
    pending_input: bool = False
    pending_decision: dict[str, Any] | None = None
    price_update: bool = False
    review_reasons: list[str] = Field(default_factory=list)
    reviewed_extraction: int = 0
    reviewed_max_price: str | None = None
    chosen_option: str | None = None
    injection_suspected: bool = False
    last_step: str | None = None
    subject: str = ""
    first_email_text: str = ""

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> "OrderMeta":
        d = from_item(item)
        return cls(
            order_id=d["orderId"],
            customer_id=d["customerId"],
            customer_email=d.get("customerEmail", ""),
            customer_name=d.get("customerName", ""),
            state=OrderState(d["state"]),
            version=d["version"],
            created_at=d["createdAt"],
            updated_at=d["updatedAt"],
            language=d.get("language", "en"),
            extraction_version=d.get("extractionVersion", 0),
            quote_round=d.get("quoteRound", 0),
            asked_fields=d.get("askedFields", []),
            waiting_task_token=d.get("waitingTaskToken"),
            waiting_for=d.get("waitingFor"),
            pending_input=d.get("pendingInput", False),
            pending_decision=d.get("pendingDecision"),
            price_update=d.get("priceUpdate", False),
            review_reasons=d.get("reviewReasons", []),
            reviewed_extraction=d.get("reviewedExtraction", 0),
            reviewed_max_price=d.get("reviewedMaxPrice"),
            chosen_option=d.get("chosenOption"),
            injection_suspected=d.get("injectionSuspected", False),
            last_step=d.get("lastStep"),
            subject=d.get("subject", ""),
            first_email_text=d.get("firstEmailText", ""),
        )


# Python attribute -> DynamoDB attribute, for META updates passed to ``transition``.
META_ATTR = {
    "language": "language",
    "extraction_version": "extractionVersion",
    "quote_round": "quoteRound",
    "asked_fields": "askedFields",
    "chosen_option": "chosenOption",
    "injection_suspected": "injectionSuspected",
    "customer_name": "customerName",
    "price_update": "priceUpdate",
    "review_reasons": "reviewReasons",
    "reviewed_extraction": "reviewedExtraction",
    "reviewed_max_price": "reviewedMaxPrice",
}


class Put(BaseModel):
    """An extra item written inside a transition's transaction."""

    item: dict[str, Any]
    write_once: bool = True  # attribute_not_exists(PK): an existing row cancels the transaction


class Repo:
    def __init__(
        self,
        table: str,
        client: "DynamoDBClient | None" = None,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.table = table
        self.db: DynamoDBClient = client or boto3.client("dynamodb")
        self.now = clock  # audit timestamps; injectable so tests have one consistent clock

    # --- reads ------------------------------------------------------------------------------

    def get_meta(self, order_id: str, *, consistent: bool = True) -> OrderMeta:
        r = self.db.get_item(
            TableName=self.table,
            Key=to_item({"PK": keys.order_pk(order_id), "SK": keys.META}),
            ConsistentRead=consistent,
        )
        if "Item" not in r:
            raise OrderNotFound(order_id)
        return OrderMeta.from_item(r["Item"])

    def order_exists(self, order_id: str) -> bool:
        try:
            self.get_meta(order_id, consistent=False)
        except OrderNotFound:
            return False
        return True

    def items(self, order_id: str, prefix: str = "") -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {
            "TableName": self.table,
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :p)",
            "ExpressionAttributeValues": to_item({":pk": keys.order_pk(order_id), ":p": prefix}),
            "ConsistentRead": True,
        }
        out: list[dict[str, Any]] = []
        while True:
            r = self.db.query(**kwargs)
            out.extend(from_item(i) for i in r.get("Items", []))
            if "LastEvaluatedKey" not in r:
                return out
            kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]

    def emails(self, order_id: str) -> list[dict[str, Any]]:
        return self.items(order_id, "EMAIL#")

    def events(self, order_id: str) -> list[dict[str, Any]]:
        return self.items(order_id, "EVENT#")

    def latest_extraction(self, order_id: str) -> ShipmentRequest | None:
        rows = self.items(order_id, "EXTRACTION#")
        if not rows:
            return None
        return ShipmentRequest.model_validate(rows[-1]["request"])

    def quote_options(self, order_id: str, round_: int) -> list[dict[str, Any]]:
        return self.items(order_id, keys.quote_round_prefix(round_))

    def bookings(self, order_id: str) -> list[dict[str, Any]]:
        return self.items(order_id, "BOOKING#")

    def outbox(self, order_id: str) -> list[dict[str, Any]]:
        return self.items(order_id, "OUTBOX#")

    def get_outbox(self, order_id: str, key: str) -> dict[str, Any] | None:
        r = self.db.get_item(
            TableName=self.table,
            Key=to_item({"PK": keys.order_pk(order_id), "SK": keys.outbox_sk(key)}),
            ConsistentRead=True,
        )
        return from_item(r["Item"]) if "Item" in r else None

    def order_for_message_id(self, message_id: str) -> str | None:
        r = self.db.get_item(
            TableName=self.table,
            Key=to_item({"PK": keys.message_pk(message_id), "SK": keys.THREAD}),
        )
        return from_item(r["Item"])["orderId"] if "Item" in r else None

    def customer_for_address(self, address: str) -> dict[str, Any] | None:
        r = self.db.get_item(
            TableName=self.table,
            Key=to_item({"PK": keys.address_pk(address), "SK": "CUSTOMER"}),
        )
        if "Item" not in r:
            return None
        customer_id = from_item(r["Item"])["customerId"]
        p = self.db.get_item(
            TableName=self.table,
            Key=to_item({"PK": keys.customer_pk(customer_id), "SK": keys.PROFILE}),
        )
        return from_item(p["Item"]) if "Item" in p else None

    def orders_for_customer(self, customer_id: str) -> list[OrderMeta]:
        r = self.db.query(
            TableName=self.table,
            IndexName="GSI1",
            KeyConditionExpression="GSI1PK = :c",
            ExpressionAttributeValues=to_item({":c": keys.customer_pk(customer_id)}),
        )
        return [OrderMeta.from_item(i) for i in r.get("Items", [])]

    def open_orders_for_customer(self, customer_id: str) -> list[OpenOrder]:
        return [
            OpenOrder(
                order_id=m.order_id,
                state=m.state,
                created_at=m.created_at,
                context=f"{m.subject}\n{m.first_email_text}",
            )
            for m in self.orders_for_customer(customer_id)
            if m.state not in TERMINAL
        ]

    def orders_in_state(self, state: OrderState, limit: int = 50) -> list[OrderMeta]:
        r = self.db.query(
            TableName=self.table,
            IndexName="GSI2",
            KeyConditionExpression="GSI2PK = :s",
            ExpressionAttributeValues=to_item({":s": f"STATE#{state.value}"}),
            ScanIndexForward=False,
            Limit=limit,
        )
        return [OrderMeta.from_item(i) for i in r.get("Items", [])]

    # --- writes -----------------------------------------------------------------------------

    def _event(
        self,
        order_id: str,
        customer_id: str,
        kind: str,
        now: datetime,
        detail: dict[str, Any],
        actor: str = "system",
    ) -> dict[str, Any]:
        return {
            "PK": keys.order_pk(order_id),
            "SK": keys.event_sk(now, secrets.token_hex(4)),
            "type": "event",
            "orderId": order_id,
            "customerId": customer_id,
            "kind": kind,
            "actor": actor,
            "at": now.isoformat(),
            "detail": detail,
        }

    def _transact(self, items: list[dict[str, Any]]) -> None:
        try:
            self.db.transact_write_items(TransactItems=items)  # type: ignore[arg-type]
        except ClientError as e:
            if e.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            reasons = [r.get("Code") for r in e.response.get("CancellationReasons", [])]
            if reasons and reasons[0] == "ConditionalCheckFailed":
                raise StaleOrder(str(reasons)) from e
            if "ConditionalCheckFailed" in reasons:
                raise AlreadyExists(str(reasons)) from e
            raise

    def _put(self, item: dict[str, Any], write_once: bool) -> dict[str, Any]:
        put: dict[str, Any] = {"TableName": self.table, "Item": to_item(item)}
        if write_once:
            put["ConditionExpression"] = "attribute_not_exists(PK)"
        return {"Put": put}

    def create_order(
        self,
        *,
        order_id: str,
        customer_id: str,
        customer_email: str,
        customer_name: str,
        email_item: dict[str, Any],
        message_id: str,
        subject: str,
        first_email_text: str,
        now: datetime | None = None,
        language: str = "en",
    ) -> None:
        now = now or self.now()
        meta = {
            "PK": keys.order_pk(order_id),
            "SK": keys.META,
            "type": "order",
            "orderId": order_id,
            "customerId": customer_id,
            "customerEmail": customer_email,
            "customerName": customer_name,
            "state": OrderState.RECEIVED.value,
            "version": 1,
            "createdAt": now.isoformat(),
            "updatedAt": now.isoformat(),
            "subject": subject,
            "firstEmailText": first_email_text[:2000],
            "language": language,
            **keys.gsi1(customer_id, now),
            **keys.gsi2(OrderState.RECEIVED.value, now),
        }
        items = [
            self._put(meta, write_once=True),
            self._put(email_item, write_once=True),
            self._put(self._event(order_id, customer_id, "ORDER_CREATED", now, {}), True),
        ]
        if message_id:
            items.append(self._put(self._thread_item(message_id, order_id, customer_id), True))
        self._transact(items)

    def _thread_item(self, message_id: str, order_id: str, customer_id: str) -> dict[str, Any]:
        return {
            "PK": keys.message_pk(message_id),
            "SK": keys.THREAD,
            "orderId": order_id,
            "customerId": customer_id,
        }

    def add_email(
        self,
        order_id: str,
        customer_id: str,
        email_item: dict[str, Any],
        message_id: str,
        event_kind: str,
        detail: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Attach an email to an existing order. Idempotent on the email's sort key: returns
        False (and writes nothing) if this email is already stored."""
        now = now or self.now()
        items = [
            self._put(email_item, write_once=True),
            self._put(self._event(order_id, customer_id, event_kind, now, detail or {}), True),
        ]
        if message_id:
            # Not write-once: the same Message-ID always maps to the same order.
            items.append(self._put(self._thread_item(message_id, order_id, customer_id), False))
        try:
            self._transact(items)
        except (StaleOrder, AlreadyExists):
            return False  # the email row exists: a duplicate delivery, nothing to do
        return True

    def index_message(self, message_id: str, order_id: str, customer_id: str) -> None:
        self.db.put_item(
            TableName=self.table, Item=to_item(self._thread_item(message_id, order_id, customer_id))
        )

    def transition(
        self,
        meta: OrderMeta,
        target: OrderState,
        *,
        reason: str,
        step: str | None = None,
        updates: dict[str, Any] | None = None,
        puts: Iterable[Put] = (),
        detail: dict[str, Any] | None = None,
        actor: str = "system",
        now: datetime | None = None,
        clear_pending: bool = False,
    ) -> OrderMeta:
        """Move ``meta`` to ``target`` if nobody else moved it first. Returns the new META.

        ``step`` makes a retried workflow step idempotent: if the transition already happened
        under the same step key, the current META is returned instead of raising.
        """
        assert_transition(meta.state, target)
        now = now or self.now()
        names = {"#s": "state", "#v": "version", "#u": "updatedAt", "#c": "customerId"}
        values: dict[str, Any] = {
            ":expected": meta.state.value,
            ":v": meta.version,
            ":c": meta.customer_id,
            ":target": target.value,
            ":one": 1,
            ":now": now.isoformat(),
            ":g2pk": f"STATE#{target.value}",
            ":step": step or f"{target.value}@{now.isoformat()}",
        }
        sets = ["#s = :target", "#v = #v + :one", "#u = :now", "GSI2PK = :g2pk", "GSI2SK = :now"]
        sets.append("lastStep = :step")
        for i, (attr, value) in enumerate((updates or {}).items()):
            names[f"#a{i}"] = META_ATTR[attr]
            values[f":a{i}"] = value
            sets.append(f"#a{i} = :a{i}")
        # The waiting token is left alone: a state change can be the very answer the waiting
        # step needs (customer approval), and tokens are claimed at most once anyway.
        expression = "SET " + ", ".join(sets)
        if clear_pending:
            # Re-extraction reads every email so far, so earlier "new email" signals are spent.
            expression += " REMOVE pendingInput, pendingDecision"
        update = {
            "Update": {
                "TableName": self.table,
                "Key": to_item({"PK": keys.order_pk(meta.order_id), "SK": keys.META}),
                "UpdateExpression": expression,
                "ConditionExpression": "#s = :expected AND #v = :v AND #c = :c",
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": to_item(values),
            }
        }
        event = self._event(
            meta.order_id,
            meta.customer_id,
            "STATE_CHANGED",
            now,
            {"from": meta.state.value, "to": target.value, "reason": reason, **(detail or {})},
            actor,
        )
        items = [update, self._put(event, True)]
        items.extend(self._put(p.item, p.write_once) for p in puts)
        try:
            self._transact(items)
        except StaleOrder:
            current = self.get_meta(meta.order_id)
            if step is not None and current.last_step == step:
                return current  # this exact step already committed (Lambda retry)
            raise
        return self.get_meta(meta.order_id)

    def record_event(
        self,
        order_id: str,
        customer_id: str,
        kind: str,
        detail: dict[str, Any] | None = None,
        actor: str = "system",
        now: datetime | None = None,
    ) -> None:
        item = self._event(order_id, customer_id, kind, now or self.now(), detail or {}, actor)
        self.db.put_item(TableName=self.table, Item=to_item(item))

    def record_edit(
        self,
        meta: OrderMeta,
        request: ShipmentRequest,
        *,
        actor: str,
        detail: dict[str, Any],
        now: datetime | None = None,
    ) -> OrderMeta:
        """A rep edit: a new extraction version, without a state change, guarded by version."""
        now = now or self.now()
        version = meta.extraction_version + 1
        update = {
            "Update": {
                "TableName": self.table,
                "Key": to_item({"PK": keys.order_pk(meta.order_id), "SK": keys.META}),
                "UpdateExpression": "SET #v = #v + :one, extractionVersion = :ev, #u = :now",
                "ConditionExpression": "#v = :v",
                "ExpressionAttributeNames": {"#v": "version", "#u": "updatedAt"},
                "ExpressionAttributeValues": to_item(
                    {":one": 1, ":ev": version, ":now": now.isoformat(), ":v": meta.version}
                ),
            }
        }
        snapshot = self.extraction_item(
            meta, version, request, {"source": "rep_edit", "actor": actor, **detail}, now
        )
        event = self._event(meta.order_id, meta.customer_id, "REP_EDIT", now, detail, actor)
        self._transact([update, self._put(snapshot, True), self._put(event, True)])
        return self.get_meta(meta.order_id)

    @staticmethod
    def extraction_item(
        meta: OrderMeta,
        version: int,
        request: ShipmentRequest,
        info: dict[str, Any],
        now: datetime,
    ) -> dict[str, Any]:
        return {
            "PK": keys.order_pk(meta.order_id),
            "SK": keys.extraction_sk(version),
            "type": "extraction",
            "orderId": meta.order_id,
            "customerId": meta.customer_id,
            "version": version,
            "at": now.isoformat(),
            "request": request.model_dump(mode="json"),
            **info,
        }

    def put_item(self, item: dict[str, Any]) -> None:
        """Plain overwrite, for rows owned by a single idempotent step (plans, provider quotes)."""
        self.db.put_item(TableName=self.table, Item=to_item(item))

    def get_item(self, order_id: str, sk: str) -> dict[str, Any] | None:
        r = self.db.get_item(
            TableName=self.table,
            Key=to_item({"PK": keys.order_pk(order_id), "SK": sk}),
            ConsistentRead=True,
        )
        return from_item(r["Item"]) if "Item" in r else None

    # --- task tokens (Step Functions callbacks) ---------------------------------------------

    def set_wait_token(self, order_id: str, token: str, waiting_for: str) -> OrderMeta:
        """Store the token of the step that is now waiting. Returns META *after* the write, so
        the caller can see an input that arrived before the token did (``pending_input``)."""
        r = self.db.update_item(
            TableName=self.table,
            Key=to_item({"PK": keys.order_pk(order_id), "SK": keys.META}),
            UpdateExpression="SET waitingTaskToken = :t, waitingFor = :w",
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues=to_item({":t": token, ":w": waiting_for}),
            ReturnValues="ALL_NEW",
        )
        return OrderMeta.from_item(r["Attributes"])

    def flag_pending_input(
        self, order_id: str, decision: dict[str, Any] | None = None
    ) -> OrderMeta:
        """Note that the customer/rep answered. Returns META after the write: if a token is
        there, the caller resumes the workflow; if not, the waiting step will see the flag."""
        r = self.db.update_item(
            TableName=self.table,
            Key=to_item({"PK": keys.order_pk(order_id), "SK": keys.META}),
            UpdateExpression="SET pendingInput = :t, pendingDecision = :d",
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeValues=to_item({":t": True, ":d": decision or {}}),
            ReturnValues="ALL_NEW",
        )
        return OrderMeta.from_item(r["Attributes"])

    def claim_wait_token(self, order_id: str, token: str) -> bool:
        """Take the token exactly once. Of all callers holding it, only one gets True."""
        try:
            self.db.update_item(
                TableName=self.table,
                Key=to_item({"PK": keys.order_pk(order_id), "SK": keys.META}),
                UpdateExpression="REMOVE waitingTaskToken, waitingFor, pendingInput, "
                "pendingDecision",
                ConditionExpression="waitingTaskToken = :t",
                ExpressionAttributeValues=to_item({":t": token}),
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    # --- outbox -----------------------------------------------------------------------------

    @staticmethod
    def outbox_item(msg: OutboxMessage) -> dict[str, Any]:
        return {
            "PK": keys.order_pk(msg.order_id),
            "SK": keys.outbox_sk(msg.key),
            "type": "outbox",
            "orderId": msg.order_id,
            "customerId": msg.customer_id,
            **msg.model_dump(mode="json"),
        }

    def claim_outbox(self, order_id: str, key: str, now: datetime | None = None) -> bool:
        """PENDING -> SENDING. A duplicate trigger loses this race and must not send."""
        return self._outbox_status(
            order_id, key, OutboxStatus.PENDING, OutboxStatus.SENDING, now or self.now()
        )

    def release_outbox(self, order_id: str, key: str) -> bool:
        return self._outbox_status(
            order_id, key, OutboxStatus.SENDING, OutboxStatus.PENDING, self.now()
        )

    def _outbox_status(
        self,
        order_id: str,
        key: str,
        expected: OutboxStatus,
        target: OutboxStatus,
        now: datetime,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        sets = ["#st = :target", "updatedAt = :now"]
        values: dict[str, Any] = {":expected": expected.value, ":target": target.value}
        values[":now"] = now.isoformat()
        for i, (k, v) in enumerate((extra or {}).items()):
            sets.append(f"{k} = :x{i}")
            values[f":x{i}"] = v
        try:
            self.db.update_item(
                TableName=self.table,
                Key=to_item({"PK": keys.order_pk(order_id), "SK": keys.outbox_sk(key)}),
                UpdateExpression="SET " + ", ".join(sets),
                ConditionExpression="#st = :expected",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues=to_item(values),
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def mark_outbox_sent(self, order_id: str, key: str, ses_message_id: str) -> bool:
        now = self.now()
        return self._outbox_status(
            order_id,
            key,
            OutboxStatus.SENDING,
            OutboxStatus.SENT,
            now,
            {"ses_message_id": ses_message_id, "sent_at": now.isoformat()},
        )

    def mark_outbox_failed(self, order_id: str, key: str, reason: str) -> bool:
        return self._outbox_status(
            order_id, key, OutboxStatus.SENDING, OutboxStatus.FAILED, self.now(), {"error": reason}
        )

    # --- bookings ---------------------------------------------------------------------------

    def put_booking(self, order_id: str, customer_id: str, kind: str, data: dict[str, Any]) -> bool:
        """Write-once booking row. False if it already exists (a retry of the same booking)."""
        item = {
            "PK": keys.order_pk(order_id),
            "SK": keys.booking_sk(kind),
            "type": "booking",
            "orderId": order_id,
            "customerId": customer_id,
            "kind": kind,
            **data,
        }
        try:
            self.db.put_item(
                TableName=self.table,
                Item=to_item(item),
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def get_booking(self, order_id: str, kind: str) -> dict[str, Any] | None:
        r = self.db.get_item(
            TableName=self.table,
            Key=to_item({"PK": keys.order_pk(order_id), "SK": keys.booking_sk(kind)}),
            ConsistentRead=True,
        )
        return from_item(r["Item"]) if "Item" in r else None

    # --- customers (seeded) -----------------------------------------------------------------

    def put_customer(self, customer_id: str, name: str, addresses: list[str], **extra: Any) -> None:
        profile = {
            "PK": keys.customer_pk(customer_id),
            "SK": keys.PROFILE,
            "type": "customer",
            "customerId": customer_id,
            "name": name,
            "addresses": [a.lower() for a in addresses],
            **extra,
        }
        self.db.put_item(TableName=self.table, Item=to_item(profile))
        for a in addresses:
            self.db.put_item(
                TableName=self.table,
                Item=to_item(
                    {"PK": keys.address_pk(a), "SK": "CUSTOMER", "customerId": customer_id}
                ),
            )
