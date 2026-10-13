"""Side-effect ports used by workflow steps, with AWS adapters.

Steps depend on these small interfaces, not on boto3 directly, so the whole order flow can run
in-process in tests (tests/integration/test_order_flow.py) with the same step code.
"""

import contextlib
import json
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Protocol

from botocore.exceptions import ClientError


class Workflow(Protocol):
    def start(self, order_id: str, payload: dict[str, Any]) -> bool:
        """Start the order's execution (name = order id). False if it already exists."""
        ...

    def send_success(self, token: str, output: dict[str, Any]) -> bool:
        """Resume a waiting step. False if the token is no longer valid (late or duplicate)."""
        ...


class RawStore(Protocol):
    def get(self, key: str) -> bytes: ...

    def put(self, key: str, data: bytes, content_type: str) -> None: ...


class Mailer(Protocol):
    def send(self, msg: EmailMessage) -> str:
        """Send a complete MIME message. Returns the provider's message id."""
        ...


class Metrics(Protocol):
    def add(self, name: str, value: float, unit: str = "Count") -> None: ...


class Publisher(Protocol):
    def publish(self, detail_type: str, detail: dict[str, Any]) -> None: ...


# --- AWS adapters ---------------------------------------------------------------------------


class StepFunctions:
    def __init__(self, state_machine_arn: str, client: Any = None) -> None:
        import boto3

        self.arn = state_machine_arn
        self.sfn = client or boto3.client("stepfunctions")

    def start(self, order_id: str, payload: dict[str, Any]) -> bool:
        try:
            self.sfn.start_execution(
                stateMachineArn=self.arn, name=order_id, input=json.dumps(payload)
            )
        except self.sfn.exceptions.ExecutionAlreadyExists:
            return False
        return True

    def send_success(self, token: str, output: dict[str, Any]) -> bool:
        try:
            self.sfn.send_task_success(taskToken=token, output=json.dumps(output))
        except (self.sfn.exceptions.TaskTimedOut, self.sfn.exceptions.InvalidToken):
            return False
        except self.sfn.exceptions.TaskDoesNotExist:
            return False
        return True


class S3RawStore:
    def __init__(self, bucket: str, client: Any = None) -> None:
        import boto3

        self.bucket = bucket
        self.s3 = client or boto3.client("s3")

    def get(self, key: str) -> bytes:
        data: bytes = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        return data

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)


class SesMailer:
    def __init__(self, client: Any = None) -> None:
        import boto3

        self.ses = client or boto3.client("sesv2")

    def send(self, msg: EmailMessage) -> str:
        r = self.ses.send_email(Content={"Raw": {"Data": bytes(msg)}})
        message_id: str = r["MessageId"]
        return message_id


class EventBridgePublisher:
    SOURCE = "freight.app"

    def __init__(self, bus: str = "default", client: Any = None) -> None:
        import boto3

        self.bus = bus
        self.events = client or boto3.client("events")

    def publish(self, detail_type: str, detail: dict[str, Any]) -> None:
        # Side channel only (cache invalidation, metrics): never fail an order step on it.
        with contextlib.suppress(ClientError):
            self.events.put_events(
                Entries=[
                    {
                        "Source": self.SOURCE,
                        "DetailType": detail_type,
                        "Detail": json.dumps(detail, default=str),
                        "EventBusName": self.bus,
                    }
                ]
            )


# --- in-memory doubles (tests, local runs) --------------------------------------------------


@dataclass
class MemoryWorkflow:
    started: dict[str, dict[str, Any]] = field(default_factory=dict)
    resumed: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    consumed: set[str] = field(default_factory=set)

    def start(self, order_id: str, payload: dict[str, Any]) -> bool:
        if order_id in self.started:
            return False
        self.started[order_id] = payload
        return True

    def send_success(self, token: str, output: dict[str, Any]) -> bool:
        if token in self.consumed:
            return False
        self.consumed.add(token)
        self.resumed.append((token, output))
        return True


@dataclass
class MemoryRawStore:
    objects: dict[str, bytes] = field(default_factory=dict)

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data


@dataclass
class MemoryMailer:
    sent: list[EmailMessage] = field(default_factory=list)

    def send(self, msg: EmailMessage) -> str:
        self.sent.append(msg)
        return f"local-{len(self.sent):04d}"


@dataclass
class MemoryMetrics:
    points: list[tuple[str, float, str]] = field(default_factory=list)

    def add(self, name: str, value: float, unit: str = "Count") -> None:
        self.points.append((name, value, unit))

    def total(self, name: str) -> float:
        return sum(v for n, v, _ in self.points if n == name)


@dataclass
class MemoryPublisher:
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def publish(self, detail_type: str, detail: dict[str, Any]) -> None:
        self.events.append((detail_type, detail))
