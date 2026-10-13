from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import pytest

from freight.domain.models import (
    Extracted,
    FieldStatus,
    ModePreference,
    Quantity,
    QuantityUnit,
)
from freight.domain.rules import validate_request
from freight.domain.state import OrderState
from freight.email.compose import (
    QuoteLine,
    compose_questions,
    compose_quote,
    detect_language,
)
from freight.email.outbox import Purpose, outbox_key
from freight.email.parse import ParsedEmail, parse_raw, split_reply
from freight.email.threading import MatchRule, OpenOrder, reference_tokens, resolve
from tests.factories import complete_request, explicit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "emails"
TODAY = datetime(2026, 10, 13).date()


def load(rel: str) -> ParsedEmail:
    return parse_raw((FIXTURES / rel).read_bytes())


# --- parse ----------------------------------------------------------------------------------


def test_parse_plain_fixture() -> None:
    e = load("de/01_missing_date_pallets.eml")
    assert e.message_id == "<de01.jonas@biergarten-berlin.example>"
    assert e.from_addr == "customer@example.com"
    assert "Flaschen" in e.new_text
    assert not e.looks_like_reply


def test_reply_quoted_history_is_stripped() -> None:
    e = load("de/02_reply_changed_subject.eml")
    assert "18 Paletten" in e.new_text
    assert "Wie viele Paletten" not in e.new_text
    assert e.looks_like_reply  # quoted history, even though the subject changed


def test_html_only_body_becomes_text() -> None:
    e = load("en/01_complete_air.eml")
    # The plain part is a stub, so the parser prefers it; the HTML part is still readable.
    assert e.text
    raw = EmailMessage()
    raw["From"] = "a@example.com"
    raw["Subject"] = "x"
    raw.set_content("<p>6 pallets</p><script>evil()</script>", subtype="html")
    assert parse_raw(bytes(raw)).text == "6 pallets"


def test_csv_attachment_text_is_extracted() -> None:
    e = load("de/03_conflict_attachment.eml")
    assert len(e.attachments) == 1
    assert "24 Paletten" in e.attachments[0].text


@pytest.mark.parametrize(
    "separator",
    [
        "On Tue, 13 Oct 2026 at 10:00, Corona <x@y.com> wrote:",
        "El mar, 13 oct 2026 a las 10:00, Corona escribió:",
        "-----Original Message-----",
        "Von: Corona Export\nGesendet: Dienstag, 13. Oktober 2026",
    ],
)
def test_split_reply_separators(separator: str) -> None:
    assert split_reply(f"20 pallets please\n\n{separator}\nold text 18 pallets") == (
        "20 pallets please"
    )


def test_dedupe_key_is_stable_and_falls_back_without_message_id() -> None:
    raw = (FIXTURES / "de/01_missing_date_pallets.eml").read_bytes()
    assert parse_raw(raw).dedupe_key == parse_raw(raw).dedupe_key
    m = EmailMessage()
    m["From"] = "a@example.com"
    m["Date"] = "Tue, 13 Oct 2026 08:00:00 +0000"
    m.set_content("hello")
    n = EmailMessage()
    n["From"] = "a@example.com"
    n["Date"] = "Tue, 13 Oct 2026 08:00:00 +0000"
    n.set_content("hello  ")
    assert parse_raw(bytes(m)).dedupe_key == parse_raw(bytes(n)).dedupe_key


# --- threading ------------------------------------------------------------------------------


class Lookup:
    def __init__(
        self,
        messages: dict[str, str] | None = None,
        orders: set[str] | None = None,
        open_orders: list[OpenOrder] | None = None,
    ) -> None:
        self.messages = messages or {}
        self.orders = orders or set()
        self.open = open_orders or []

    def order_for_message_id(self, message_id: str) -> str | None:
        return self.messages.get(message_id)

    def order_exists(self, order_id: str) -> bool:
        return order_id in self.orders

    def open_orders_for_sender(self, address: str) -> list[OpenOrder]:
        return self.open


def make(
    subject: str = "Hallo",
    body: str = "Text",
    in_reply_to: str | None = None,
) -> ParsedEmail:
    m = EmailMessage()
    m["From"] = "customer@example.com"
    m["Subject"] = subject
    m["Message-ID"] = "<new@example.com>"
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
    m.set_content(body)
    return parse_raw(bytes(m))


def open_order(oid: str, state: OrderState, context: str = "") -> OpenOrder:
    return OpenOrder(
        order_id=oid, state=state, created_at=datetime(2026, 10, 1, tzinfo=UTC), context=context
    )


def test_in_reply_to_wins() -> None:
    m = resolve(
        make(in_reply_to="<ses-1@email.amazonses.com>", subject="[FC-BBBBBB] x"),
        Lookup(messages={"<ses-1@email.amazonses.com>": "FC-AAAAAA"}, orders={"FC-BBBBBB"}),
    )
    assert (m.rule, m.order_id) == (MatchRule.IN_REPLY_TO, "FC-AAAAAA")


def test_reference_token_in_changed_subject_body() -> None:
    m = resolve(
        make(subject="Neue Frage", body="Zu FC-7Q2K9D: 18 Paletten"), Lookup(orders={"FC-7Q2K9D"})
    )
    assert (m.rule, m.order_id) == (MatchRule.REFERENCE_TOKEN, "FC-7Q2K9D")


def test_unknown_token_is_ignored() -> None:
    assert reference_tokens("re: [fc-zzzzzz]") == ["FC-ZZZZZZ"]
    m = resolve(make(subject="[FC-ZZZZZZ] hi"), Lookup())
    assert m.rule is MatchRule.NEW


def test_single_open_order_waiting_on_customer_matches_without_identifier() -> None:
    lookup = Lookup(open_orders=[open_order("FC-AAAAAA", OrderState.AWAITING_CUSTOMER_INFO)])
    m = resolve(make(subject="Lieferung Dezember", body="18 Paletten"), lookup)
    assert (m.rule, m.order_id) == (MatchRule.SENDER_SINGLE_OPEN, "FC-AAAAAA")


def test_fresh_email_while_order_is_busy_is_a_new_order() -> None:
    lookup = Lookup(open_orders=[open_order("FC-AAAAAA", OrderState.BOOKING)])
    assert resolve(make(subject="Neue Bestellung"), lookup).rule is MatchRule.NEW


def test_several_open_orders_pick_clear_winner() -> None:
    lookup = Lookup(
        open_orders=[
            open_order("FC-AAAAAA", OrderState.QUOTE_SENT, "Fässer München Oktoberfest"),
            open_order("FC-BBBBBB", OrderState.QUOTE_SENT, "Dosen Hamburg Strandbar Elbe"),
        ]
    )
    m = resolve(make(subject="Re: Dosen", body="Für die Strandbar Elbe in Hamburg"), lookup)
    assert (m.rule, m.order_id) == (MatchRule.SENDER_SIMILARITY, "FC-BBBBBB")


def test_several_open_orders_without_winner_go_to_human() -> None:
    lookup = Lookup(
        open_orders=[
            open_order("FC-AAAAAA", OrderState.QUOTE_SENT, "Berlin Flaschen"),
            open_order("FC-BBBBBB", OrderState.QUOTE_SENT, "Berlin Flaschen"),
        ]
    )
    m = resolve(make(subject="Re: Anfrage", body="ok, passt"), lookup)
    assert m.rule is MatchRule.AMBIGUOUS
    assert m.candidates == ["FC-AAAAAA", "FC-BBBBBB"]


# --- compose --------------------------------------------------------------------------------


def missing() -> Extracted:  # type: ignore[type-arg]
    return Extracted(status=FieldStatus.MISSING, confidence=0.0)


@pytest.mark.parametrize("lang,word", [("de", "Paletten"), ("es", "palets"), ("en", "pallets")])
def test_questions_ask_only_for_missing_fields(lang: str, word: str) -> None:
    req = complete_request(quantity=missing(), requested_delivery_date=missing())
    email, asked = compose_questions(validate_request(req, TODAY), req, lang, "FC-7Q2K9D")
    assert asked == {"quantity", "requested_delivery_date"}
    assert email.subject.startswith("[FC-7Q2K9D]")
    assert word in email.body_text
    assert email.body_text.count("\n1. ") == 1 and "\n2. " in email.body_text
    assert "\n3. " not in email.body_text


def test_conflict_question_lists_both_candidates() -> None:
    req = complete_request(
        quantity=Extracted(
            status=FieldStatus.CONFLICTING,
            confidence=0.4,
            candidates=[
                Quantity(amount=20, unit=QuantityUnit.PALLETS),
                Quantity(amount=24, unit=QuantityUnit.PALLETS),
            ],
        )
    )
    email, asked = compose_questions(validate_request(req, TODAY), req, "de", "FC-1")
    assert "20 Paletten / 24 Paletten" in email.body_text
    assert asked == {"quantity"}


def test_infeasible_date_question_names_the_mode() -> None:
    req = complete_request(
        requested_delivery_date=explicit(datetime(2026, 10, 20).date()),
        mode_preference=explicit(ModePreference.OCEAN),
    )
    email, _ = compose_questions(validate_request(req, TODAY), req, "en", "FC-1")
    assert "too soon for ocean freight" in email.body_text


def test_quote_email_has_links_and_totals() -> None:
    from decimal import Decimal

    line = QuoteLine(
        labels=["cheapest", "balanced"],
        mode="ocean",
        service="FCL 1x40GP",
        carrier="Oceanic",
        price=Decimal("4200"),
        last_mile=Decimal("650"),
        currency="USD",
        departure=datetime(2026, 10, 20).date(),
        arrival=datetime(2026, 11, 14).date(),
        link="https://api.example/choose?t=abc",
    )
    email = compose_quote(
        [line],
        lang="de",
        ref="FC-1",
        cargo="18 Paletten Flaschen",
        destination="Berlin",
        valid_until=datetime(2026, 10, 13, 12, 0),
    )
    assert "Günstigste / Ausgewogen" in email.body_text
    assert "4.850 USD" in email.body_text
    assert "https://api.example/choose?t=abc" in email.body_text
    assert "25 Tage" in email.body_text


@pytest.mark.parametrize(
    "text,lang",
    [
        ("Wir brauchen 18 Paletten bitte", "de"),
        ("Necesitamos 10 palets para la entrega", "es"),
        ("We need 10 pallets delivered", "en"),
    ],
)
def test_detect_language(text: str, lang: str) -> None:
    assert detect_language(text) == lang


def test_outbox_key_is_deterministic_per_purpose_and_round() -> None:
    a = outbox_key("FC-1", Purpose.QUOTE, 1)
    assert a == outbox_key("FC-1", Purpose.QUOTE, 1)
    assert a != outbox_key("FC-1", Purpose.QUOTE, 2)
    assert a != outbox_key("FC-1", Purpose.QUESTIONS, 1)
