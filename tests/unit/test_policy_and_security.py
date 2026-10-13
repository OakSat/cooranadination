from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from freight.domain.defaults import apply_defaults
from freight.domain.models import Extracted, FieldStatus, TemperatureRequirement
from freight.domain.policy import needs_rereview, review_reasons
from freight.observability.logging import redact
from freight.workflow.links import BadLink, Choice, LinkSigner
from tests.factories import complete_request, explicit

LOW = Decimal("8000")


def test_clean_repeat_order_needs_no_review() -> None:
    assert (
        review_reasons(
            complete_request(), max_quote_usd=LOW, first_order=False, injection_suspected=False
        )
        == []
    )


@pytest.mark.parametrize(
    "kwargs,overrides,expected",
    [
        ({"max_quote_usd": Decimal("90000")}, {}, "quote above"),
        ({"first_order": True}, {}, "first order"),
        ({"injection_suspected": True}, {}, "prompt injection"),
        (
            {},
            {"temperature_control": explicit(TemperatureRequirement(required=True, min_c=4))},
            "reefer",
        ),
        (
            {},
            {
                "requested_delivery_date": Extracted(
                    value=datetime(2026, 12, 1).date(),
                    status=FieldStatus.INFERRED,
                    confidence=0.9,
                )
            },
            "inferred",
        ),
    ],
)
def test_review_reasons(kwargs: dict, overrides: dict, expected: str) -> None:  # type: ignore[type-arg]
    args = {"max_quote_usd": LOW, "first_order": False, "injection_suspected": False, **kwargs}
    reasons = review_reasons(complete_request(**overrides), **args)
    assert any(expected in r for r in reasons), reasons


def test_default_origin_does_not_trigger_review() -> None:
    req = apply_defaults(
        complete_request(pickup=Extracted(status=FieldStatus.MISSING, confidence=0))
    )
    assert req.pickup.value is not None and req.pickup.value.city == "Zacatecas"
    assert (
        review_reasons(req, max_quote_usd=LOW, first_order=False, injection_suspected=False) == []
    )


def test_rereview_only_on_large_price_rise() -> None:
    assert needs_rereview(Decimal("112"), Decimal("100")) == []
    assert needs_rereview(Decimal("116"), Decimal("100"))


def test_redaction_masks_pii_but_not_dates() -> None:
    line = (
        "from jonas@biergarten.de iban DE89 3704 0044 0532 0130 00 "
        "phone +49 30 1234567 on 2026-10-13"
    )
    out = redact(line)
    assert "jonas@" not in out and "DE89" not in out and "+49" not in out
    assert "2026-10-13" in out


def test_choice_links_are_signed_and_expire() -> None:
    signer = LinkSigner(b"k" * 32)
    now = datetime(2026, 10, 13, tzinfo=UTC)
    token = signer.sign(Choice("FC-1", 1, "OCE-1", now + timedelta(days=7)))
    assert signer.verify(token, now).option_id == "OCE-1"
    payload, mac = token.split(".")
    with pytest.raises(BadLink):
        signer.verify(f"{payload}x.{mac}", now)  # tampered
    with pytest.raises(BadLink):
        LinkSigner(b"other-key-0123456789").verify(token, now)
    with pytest.raises(BadLink):
        signer.verify(token, now + timedelta(days=8))
