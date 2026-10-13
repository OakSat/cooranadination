"""Single-table key design for the ``freight`` table (PLAN.md §2.2).

Everything about one order lives under one partition, so a state change, its audit event, and
the outbox row it causes can be written in one transaction.
"""

import secrets
from datetime import datetime

ORDER_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O/1/I: read aloud over the phone


def new_order_id() -> str:
    return "FC-" + "".join(secrets.choice(ORDER_ALPHABET) for _ in range(6))


def order_pk(order_id: str) -> str:
    return f"ORDER#{order_id}"


META = "META"


def email_sk(sent_at: datetime, email_id: str) -> str:
    return f"EMAIL#{sent_at.isoformat()}#{email_id}"


def extraction_sk(version: int) -> str:
    return f"EXTRACTION#{version:04d}"


def quote_sk(round_: int, option_id: str) -> str:
    return f"QUOTE#{round_:03d}#OPT#{option_id}"


def quote_round_prefix(round_: int) -> str:
    return f"QUOTE#{round_:03d}#"


def plan_sk(round_: int) -> str:
    return f"PLAN#{round_:03d}"


def provider_quotes_sk(round_: int, provider: str) -> str:
    return f"PQ#{round_:03d}#{provider}"


def outbox_sk(key: str) -> str:
    return f"OUTBOX#{key}"


def booking_sk(kind: str) -> str:
    return f"BOOKING#{kind}"


def event_sk(at: datetime, seq: str) -> str:
    return f"EVENT#{at.isoformat()}#{seq}"


def message_pk(message_id: str) -> str:
    return f"MSG#{message_id.strip().lower()}"


THREAD = "THREAD"


def customer_pk(customer_id: str) -> str:
    return f"CUSTOMER#{customer_id}"


PROFILE = "PROFILE"


def address_pk(address: str) -> str:
    return f"ADDRESS#{address.strip().lower()}"


def gsi1(customer_id: str, created_at: datetime) -> dict[str, str]:
    return {"GSI1PK": customer_pk(customer_id), "GSI1SK": created_at.isoformat()}


def gsi2(state: str, updated_at: datetime) -> dict[str, str]:
    return {"GSI2PK": f"STATE#{state}", "GSI2SK": updated_at.isoformat()}
