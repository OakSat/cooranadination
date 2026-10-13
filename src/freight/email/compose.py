"""Deterministic outbound emails in DE / ES / EN.

Questions are built from the rule violations, not written by the LLM, so we only ever ask for
what is actually missing or conflicting and never invent facts. Every subject carries the
order's reference token, which is threading rule 2.
"""

import re
from datetime import date, datetime
from decimal import Decimal
from functools import cache
from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined
from pydantic import BaseModel, ConfigDict

from freight.domain.models import Address, Quantity, ShipmentRequest
from freight.domain.rules import Action, Violation
from freight.email.outbox import OutboundEmail

Lang = str  # "de" | "es" | "en"
LANGS = ("de", "es", "en")

QUESTIONS: dict[str, dict[str, str]] = {
    "quantity": {
        "de": "Wie viele Paletten (oder Kisten bzw. Fässer) möchten Sie bestellen?",
        "es": "¿Cuántos palets (o cajas o barriles) desea pedir?",
        "en": "How many pallets (or cases or kegs) would you like to order?",
    },
    "packaging": {
        "de": "Möchten Sie Flaschen, Dosen oder Fässer?",
        "es": "¿Desea botellas, latas o barriles?",
        "en": "Would you like bottles, cans or kegs?",
    },
    "pickup": {
        "de": "Von welchem Standort soll die Ware abgeholt werden?",
        "es": "¿Desde qué ubicación debemos recoger la mercancía?",
        "en": "Where should the goods be picked up?",
    },
    "delivery": {
        "de": "An welche vollständige Adresse (Straße, PLZ, Stadt) sollen wir liefern?",
        "es": "¿A qué dirección completa (calle, código postal, ciudad) debemos entregar?",
        "en": "What is the full delivery address (street, postcode, city)?",
    },
    "requested_delivery_date": {
        "de": "Bis zu welchem Datum muss die Ware spätestens bei Ihnen sein?",
        "es": "¿Cuál es la fecha límite de entrega?",
        "en": "By which date do you need the delivery at the latest?",
    },
    "total_weight_kg": {
        "de": "Können Sie das Gesamtgewicht bestätigen?",
        "es": "¿Puede confirmar el peso total?",
        "en": "Could you confirm the total weight?",
    },
    "incoterms": {
        "de": "Welche Incoterms sollen gelten?",
        "es": "¿Qué Incoterms aplican?",
        "en": "Which Incoterms apply?",
    },
    "mode_preference": {
        "de": "Bevorzugen Sie Seefracht oder Luftfracht?",
        "es": "¿Prefiere transporte marítimo o aéreo?",
        "en": "Do you prefer ocean or air freight?",
    },
}

CONFLICT: dict[str, str] = {
    "de": "Sie haben für {field} unterschiedliche Angaben gemacht ({options}). Welche gilt?",
    "es": "Recibimos datos distintos para {field} ({options}). ¿Cuál es el correcto?",
    "en": "We received different values for {field} ({options}). Which one is correct?",
}

DATE_TOO_SOON: dict[str, str] = {
    "de": "Der gewünschte Liefertermin ist für {mode} zu knapp. Ist ein späteres Datum möglich?",
    "es": "La fecha de entrega es demasiado próxima para {mode}. ¿Es posible una fecha posterior?",
    "en": "The requested delivery date is too soon for {mode}. Could a later date work?",
}

DATE_PAST: dict[str, str] = {
    "de": "Das genannte Lieferdatum liegt in der Vergangenheit. Welches Datum meinen Sie?",
    "es": "La fecha de entrega indicada ya pasó. ¿Qué fecha quiso decir?",
    "en": "The delivery date you gave is in the past. Which date did you mean?",
}

WEIGHT_MISMATCH: dict[str, str] = {
    "de": "Das genannte Gewicht ({stated}) passt nicht zur Menge (ca. {implied}). Bitte prüfen.",
    "es": "El peso indicado ({stated}) no coincide con la cantidad (aprox. {implied}).",
    "en": "The stated weight ({stated}) does not match the quantity (about {implied}).",
}

FIELD_LABEL: dict[str, dict[str, str]] = {
    "quantity": {"de": "die Menge", "es": "la cantidad", "en": "the quantity"},
    "packaging": {"de": "die Verpackung", "es": "el envase", "en": "the packaging"},
    "pickup": {"de": "den Abholort", "es": "el lugar de recogida", "en": "the pickup location"},
    "delivery": {"de": "die Lieferadresse", "es": "la dirección", "en": "the delivery address"},
    "requested_delivery_date": {"de": "den Liefertermin", "es": "la fecha", "en": "the date"},
    "incoterms": {"de": "die Incoterms", "es": "los Incoterms", "en": "the Incoterms"},
    "mode_preference": {"de": "die Versandart", "es": "el modo", "en": "the shipping mode"},
}

UNIT_NAME: dict[str, dict[str, str]] = {
    "pallets": {"de": "Paletten", "es": "palets", "en": "pallets"},
    "cases": {"de": "Kisten", "es": "cajas", "en": "cases"},
    "kegs": {"de": "Fässer", "es": "barriles", "en": "kegs"},
    "units": {"de": "Einheiten", "es": "unidades", "en": "units"},
    "bottles": {"de": "Flaschen", "es": "botellas", "en": "bottles"},
    "cans": {"de": "Dosen", "es": "latas", "en": "cans"},
}

MODE_NAME: dict[str, dict[str, str]] = {
    "ocean": {"de": "Seefracht", "es": "transporte marítimo", "en": "ocean freight"},
    "air": {"de": "Luftfracht", "es": "transporte aéreo", "en": "air freight"},
    "any": {"de": "den Versand", "es": "el envío", "en": "shipping"},
}

LABEL_NAME: dict[str, dict[str, str]] = {
    "cheapest": {"de": "Günstigste", "es": "Más económica", "en": "Cheapest"},
    "fastest": {"de": "Schnellste", "es": "Más rápida", "en": "Fastest"},
    "balanced": {"de": "Ausgewogen", "es": "Equilibrada", "en": "Balanced"},
}

SUBJECTS: dict[str, dict[str, str]] = {
    "questions": {
        "de": "Rückfragen zu Ihrer Anfrage",
        "es": "Preguntas sobre su solicitud",
        "en": "Questions about your request",
    },
    "quote": {"de": "Ihre Versandoptionen", "es": "Sus opciones de envío", "en": "Your options"},
    "price_update": {
        "de": "Aktualisierte Versandoptionen",
        "es": "Opciones de envío actualizadas",
        "en": "Updated shipping options",
    },
    "confirmation": {
        "de": "Buchungsbestätigung",
        "es": "Confirmación de reserva",
        "en": "Booking confirmation",
    },
    "handoff": {
        "de": "Ihre Anfrage wird bearbeitet",
        "es": "Estamos revisando su solicitud",
        "en": "We are reviewing your request",
    },
}

_LANG_HINTS = {
    "de": {"und", "wir", "bitte", "die", "der", "nicht", "ich", "paletten", "lieferung", "ist"},
    "es": {"y", "de", "para", "los", "las", "por", "necesitamos", "palets", "entrega", "gracias"},
    "en": {"the", "and", "we", "please", "to", "of", "need", "pallets", "delivery", "thanks"},
}


def detect_language(text: str, default: Lang = "en") -> Lang:
    words = re.findall(r"[a-záéíóúñäöüß]+", text.lower())
    scores = {lang: sum(w in hints for w in words) for lang, hints in _LANG_HINTS.items()}
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else default


@cache
def _env() -> Environment:
    return Environment(
        loader=PackageLoader("freight.email", "templates"),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,  # plain-text emails
    )


def _lang(lang: str) -> Lang:
    return lang if lang in LANGS else "en"


def subject(purpose: str, lang: Lang, ref: str) -> str:
    return f"[{ref}] {SUBJECTS[purpose][_lang(lang)]}"


def format_date(d: date, lang: Lang) -> str:
    fmt = {"de": "%d.%m.%Y", "es": "%d/%m/%Y"}.get(lang, "%Y-%m-%d")
    return d.strftime(fmt)


def format_money(amount: Decimal, currency: str, lang: Lang) -> str:
    whole = f"{amount:,.0f}"
    if lang in ("de", "es"):
        whole = whole.replace(",", ".")
    return f"{whole} {currency}" if lang in ("de", "es") else f"{currency} {whole}"


def format_value(value: Any, lang: Lang) -> str:
    match value:
        case Quantity():
            return f"{value.amount} {UNIT_NAME[value.unit.value][lang]}"
        case Address():
            return ", ".join(p for p in (value.street, value.postal_code, value.city) if p)
        case date():
            return format_date(value, lang)
        case str() if value in UNIT_NAME:
            return UNIT_NAME[value][lang]
        case str() if value in MODE_NAME:
            return MODE_NAME[value][lang]
        case _:
            return str(value)


def questions_for(
    violations: list[Violation], request: ShipmentRequest, lang: Lang
) -> tuple[list[str], set[str]]:
    """Customer-facing questions plus the set of fields they ask about (stored as ``asked``)."""
    lang = _lang(lang)
    questions: list[str] = []
    asked: set[str] = set()
    for v in violations:
        if v.action is not Action.CLARIFY or v.field in asked:
            continue
        field = request.fields()[v.field]
        if v.code == "missing" and v.field in QUESTIONS:
            questions.append(QUESTIONS[v.field][lang])
        elif v.code == "conflict":
            options = " / ".join(format_value(c, lang) for c in field.candidates)
            label = FIELD_LABEL.get(v.field, {}).get(lang, v.field)
            questions.append(CONFLICT[lang].format(field=label, options=options))
        elif v.code == "date_in_past":
            questions.append(DATE_PAST[lang])
        elif v.code == "date_infeasible":
            mode = request.mode_preference.value.value if request.mode_preference.value else "any"
            questions.append(DATE_TOO_SOON[lang].format(mode=MODE_NAME[mode][lang]))
        elif v.code == "weight_mismatch":
            stated, implied = re.findall(r"[\d.]+ kg", v.message)[:2] or ["?", "?"]
            questions.append(WEIGHT_MISMATCH[lang].format(stated=stated, implied=implied))
        else:
            continue
        asked.add(v.field)
    return questions, asked


def compose_questions(
    violations: list[Violation], request: ShipmentRequest, lang: Lang, ref: str, name: str = ""
) -> tuple[OutboundEmail, set[str]]:
    lang = _lang(lang)
    questions, asked = questions_for(violations, request, lang)
    body = (
        _env()
        .get_template(f"{lang}/questions.txt.j2")
        .render(questions=questions, ref=ref, name=name)
    )
    return OutboundEmail(subject=subject("questions", lang, ref), body_text=body), asked


class QuoteLine(BaseModel):
    model_config = ConfigDict(frozen=True)

    labels: list[str]
    mode: str
    service: str
    carrier: str
    price: Decimal
    last_mile: Decimal | None
    currency: str
    departure: date
    arrival: date
    link: str


def compose_quote(
    lines: list[QuoteLine],
    *,
    lang: Lang,
    ref: str,
    cargo: str,
    destination: str,
    valid_until: datetime,
    name: str = "",
    price_update: bool = False,
) -> OutboundEmail:
    lang = _lang(lang)
    options = [
        {
            "labels": " / ".join(LABEL_NAME[label][lang] for label in line.labels),
            "mode": MODE_NAME[line.mode][lang],
            "service": line.service,
            "carrier": line.carrier,
            "departure": format_date(line.departure, lang),
            "arrival": format_date(line.arrival, lang),
            "transit_days": (line.arrival - line.departure).days,
            "price": format_money(line.price + (line.last_mile or 0), line.currency, lang),
            "last_mile": format_money(line.last_mile, line.currency, lang)
            if line.last_mile
            else None,
            "link": line.link,
        }
        for line in lines
    ]
    body = (
        _env()
        .get_template(f"{lang}/quote.txt.j2")
        .render(
            options=options,
            ref=ref,
            name=name,
            cargo=cargo,
            destination=destination,
            valid_until=valid_until.strftime("%Y-%m-%d %H:%M UTC"),
            price_update=price_update,
        )
    )
    purpose = "price_update" if price_update else "quote"
    return OutboundEmail(subject=subject(purpose, lang, ref), body_text=body)


def compose_confirmation(
    *, lang: Lang, ref: str, main: dict[str, Any], truck: dict[str, Any], total: str, name: str = ""
) -> OutboundEmail:
    lang = _lang(lang)
    body = (
        _env()
        .get_template(f"{lang}/confirmation.txt.j2")
        .render(ref=ref, main=main, truck=truck, total=total, name=name)
    )
    return OutboundEmail(subject=subject("confirmation", lang, ref), body_text=body)


def compose_handoff(*, lang: Lang, ref: str, name: str = "") -> OutboundEmail:
    lang = _lang(lang)
    body = _env().get_template(f"{lang}/handoff.txt.j2").render(ref=ref, name=name)
    return OutboundEmail(subject=subject("handoff", lang, ref), body_text=body)
