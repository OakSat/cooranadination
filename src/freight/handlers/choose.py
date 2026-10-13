"""Customer choice links. GET shows a confirmation page; only the POST approves.

Mail security scanners open links in emails. If the GET approved, a scanner could book a
shipment the customer never chose; the extra click on a form keeps approval explicit.
"""

from html import escape
from typing import Any
from urllib.parse import parse_qs

from aws_lambda_powertools import Logger
from aws_lambda_powertools.utilities.typing import LambdaContext

from freight.observability.logging import configure_logging
from freight.observability.metrics import powertools_metrics
from freight.workflow.actions import ChoiceOutcome, confirm_choice, describe_choice
from freight.workflow.deps import aws_deps

logger = Logger(service="freight")
metrics = powertools_metrics()
configure_logging(logger)

MESSAGES = {
    ChoiceOutcome.APPROVED: "Thank you! Your choice is confirmed. We are booking it now and "
    "will email you the booking confirmation.",
    ChoiceOutcome.ALREADY_APPROVED: "This option is already confirmed. No action needed.",
    ChoiceOutcome.EXPIRED_REQUOTING: "This price has expired. We are getting fresh prices and "
    "will email you updated options shortly.",
    ChoiceOutcome.REPLACED: "This offer was replaced by a newer one. Please use the links in "
    "our latest email.",
    ChoiceOutcome.NOT_OPEN: "This order is no longer waiting for a choice. Please contact us "
    "if you think this is wrong.",
    ChoiceOutcome.INVALID: "This link is invalid or has expired.",
}

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Confirm shipping option</title>
<style>
:root{{--bg:#fafaf7;--fg:#1d1d1b;--muted:#5d5d58;--card:#fff;--line:#e3e2dc;--accent:#1f6f50}}
@media (prefers-color-scheme: dark){{:root{{--bg:#151514;--fg:#ecebe6;--muted:#a3a29b;
--card:#1f1f1d;--line:#34332f;--accent:#5fc49a}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}}
main{{max-width:560px;margin:48px auto;padding:0 16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:24px}}
dl{{display:grid;grid-template-columns:auto 1fr;gap:6px 16px}}dt{{color:var(--muted)}}
button{{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:12px 20px;
font-size:16px;cursor:pointer}}</style></head><body><main><div class="card">{content}</div>
</main></body></html>"""


def _html(status: int, content: str) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "text/html; charset=utf-8", "cache-control": "no-store"},
        "body": PAGE.format(content=content),
    }


def confirmation_page(token: str) -> dict[str, Any]:
    option = describe_choice(aws_deps(), token)
    if option is None:
        return _html(400, f"<p>{MESSAGES[ChoiceOutcome.INVALID]}</p>")
    rows = [
        ("Order", option["orderId"]),
        ("Service", option["service"]),
        ("Carrier", option["carrier"]),
        ("Departure", option["departure"]),
        ("Arrival", option["arrival"]),
        ("Total price", f"{option['price']} {option['currency']}"),
        ("Price valid until", option["validUntil"][:16].replace("T", " ") + " UTC"),
    ]
    dl = "".join(f"<dt>{escape(k)}</dt><dd>{escape(str(v))}</dd>" for k, v in rows)
    return _html(
        200,
        "<h1>Confirm this shipping option</h1>"
        f"<dl>{dl}</dl>"
        '<form method="post"><input type="hidden" name="t" value="'
        f'{escape(token)}"><button type="submit">Confirm and book</button></form>',
    )


def handle_post(body: str) -> dict[str, Any]:
    token = parse_qs(body).get("t", [""])[0]
    outcome, info = confirm_choice(aws_deps(), token)
    logger.info("choice", extra={"outcome": outcome.value, "order_id": info.get("orderId")})
    status = 200 if outcome is not ChoiceOutcome.INVALID else 400
    return _html(
        status,
        f"<h1>{escape(outcome.value.replace('_', ' ').title())}</h1>"
        f"<p>{escape(MESSAGES[outcome])}</p>",
    )


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    method = event["requestContext"]["http"]["method"]
    if method == "POST":
        body = event.get("body") or ""
        if event.get("isBase64Encoded"):
            import base64

            body = base64.b64decode(body).decode()
        return handle_post(body)
    token = (event.get("queryStringParameters") or {}).get("t", "")
    return confirmation_page(token)
