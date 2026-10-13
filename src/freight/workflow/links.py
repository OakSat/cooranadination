"""Signed customer choice links: ``/choose?t=<payload>.<hmac>``.

The token names one option of one quote round of one order and expires. It needs no login and
no domain (the link points at API Gateway). The click only shows a confirmation page; the
approval is a POST, so mail scanners that pre-fetch links cannot approve anything.
"""

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime


class BadLink(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@dataclass(frozen=True)
class Choice:
    order_id: str
    round: int
    option_id: str
    expires: datetime


class LinkSigner:
    def __init__(self, key: bytes) -> None:
        if len(key) < 16:
            raise ValueError("link key too short")
        self.key = key

    def _mac(self, payload: str) -> str:
        return _b64(hmac.new(self.key, payload.encode(), hashlib.sha256).digest())

    def sign(self, choice: Choice) -> str:
        payload = _b64(
            json.dumps(
                {
                    "o": choice.order_id,
                    "r": choice.round,
                    "x": choice.option_id,
                    "e": int(choice.expires.timestamp()),
                },
                separators=(",", ":"),
            ).encode()
        )
        return f"{payload}.{self._mac(payload)}"

    def verify(self, token: str, now: datetime | None = None) -> Choice:
        try:
            payload, mac = token.split(".", 1)
        except ValueError as e:
            raise BadLink("malformed") from e
        if not hmac.compare_digest(mac, self._mac(payload)):
            raise BadLink("bad signature")
        data = json.loads(_unb64(payload))
        expires = datetime.fromtimestamp(data["e"], UTC)
        if (now or datetime.now(UTC)) >= expires:
            raise BadLink("expired")
        return Choice(order_id=data["o"], round=data["r"], option_id=data["x"], expires=expires)
