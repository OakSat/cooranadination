"""Rep dashboard API (``/rep/*``, Cognito JWT) and the dashboard page itself (``/dashboard``).

The page is served by this Lambda instead of S3 + CloudFront: Cognito's hosted UI requires an
HTTPS callback URL, and API Gateway already gives us one.
"""

from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from importlib.resources import files
from typing import Any

from aws_lambda_powertools import Logger
from aws_lambda_powertools.event_handler import APIGatewayHttpResolver, Response
from aws_lambda_powertools.event_handler.exceptions import BadRequestError, NotFoundError
from aws_lambda_powertools.utilities.typing import LambdaContext

from freight.config import settings
from freight.domain.state import OrderState
from freight.observability.logging import configure_logging
from freight.observability.metrics import powertools_metrics
from freight.persistence.repo import OrderNotFound
from freight.workflow import actions
from freight.workflow.deps import Deps, aws_deps

logger = Logger(service="freight")
metrics = powertools_metrics()
configure_logging(logger)
app = APIGatewayHttpResolver()

QUEUES = {
    "review": OrderState.PENDING_REP_REVIEW,
    "human": OrderState.NEEDS_HUMAN,
    "quoted": OrderState.QUOTE_SENT,
    "asking": OrderState.AWAITING_CUSTOMER_INFO,
    "completed": OrderState.COMPLETED,
}


def _actor() -> str:
    claims = (
        app.current_event.request_context.authorizer.jwt_claim
        if app.current_event.request_context.authorizer
        else {}
    )
    return str(claims.get("email") or claims.get("username") or claims.get("sub") or "rep")


def _deps() -> Deps:
    return aws_deps()


def _static(name: str, content_type: str) -> Response[str]:
    body = files("freight.dashboard").joinpath(name).read_text("utf-8")
    if name == "config.js":
        body = body.replace("__API_BASE__", settings().api_base_url.rstrip("/"))
    return Response(200, content_type, body, headers={"cache-control": "no-store"})


@app.get("/dashboard")
def dashboard() -> Response[str]:
    return _static("index.html", "text/html; charset=utf-8")


@app.get("/dashboard/app.js")
def dashboard_js() -> Response[str]:
    return _static("app.js", "text/javascript; charset=utf-8")


@app.get("/dashboard/config.js")
def dashboard_config() -> Response[str]:
    import os

    s = settings()
    body = (
        "window.FREIGHT_CONFIG = "
        + __import__("json").dumps(
            {
                "apiBase": s.api_base_url.rstrip("/"),
                "cognitoDomain": os.environ.get("COGNITO_DOMAIN", ""),
                "clientId": os.environ.get("COGNITO_CLIENT_ID", ""),
            }
        )
        + ";"
    )
    return Response(200, "text/javascript", body, headers={"cache-control": "no-store"})


def order_view(deps: Deps, order_id: str) -> dict[str, Any]:
    repo = deps.repo
    meta = repo.get_meta(order_id)
    items = repo.items(order_id)
    by_type: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_type.setdefault(str(item.get("type", "other")), []).append(item)
    extractions = by_type.get("extraction", [])
    quotes = [q for q in by_type.get("quote", []) if q.get("round") == meta.quote_round]
    plans = by_type.get("plan", [])
    return {
        "meta": meta.model_dump(mode="json", exclude={"waiting_task_token"}),
        "emails": by_type.get("email", []),
        "extraction": extractions[-1] if extractions else None,
        "extractionCount": len(extractions),
        "plan": plans[-1]["plan"]["summary"] if plans else None,
        "quotes": quotes,
        "bookings": by_type.get("booking", []),
        "outbox": by_type.get("outbox", []),
        "events": by_type.get("event", []),
    }


@app.get("/rep/queue")
def queue() -> dict[str, Any]:
    repo = _deps().repo
    return {
        name: [
            {
                "orderId": m.order_id,
                "customer": m.customer_name or m.customer_email,
                "subject": m.subject,
                "state": m.state.value,
                "updatedAt": m.updated_at.isoformat(),
                "reasons": m.review_reasons,
            }
            for m in repo.orders_in_state(state)
        ]
        for name, state in QUEUES.items()
    }


@app.get("/rep/orders/<order_id>")
def get_order(order_id: str) -> dict[str, Any]:
    try:
        return order_view(_deps(), order_id)
    except OrderNotFound as e:
        raise NotFoundError(order_id) from e


def _body() -> dict[str, Any]:
    body = app.current_event.json_body
    if not isinstance(body, dict):
        raise BadRequestError("JSON object expected")
    return body


def _run(fn: Any, *args: Any) -> dict[str, Any]:
    try:
        result = fn(*args)
    except actions.RepError as e:
        raise BadRequestError(str(e)) from e
    except OrderNotFound as e:
        raise NotFoundError(str(e)) from e
    return {"ok": True, "resumed": result if isinstance(result, bool) else None}


@app.post("/rep/orders/<order_id>/decision")
def decision(order_id: str) -> dict[str, Any]:
    b = _body()
    return _run(
        actions.rep_decide, _deps(), order_id, _actor(), b.get("decision"), b.get("note", "")
    )


@app.post("/rep/orders/<order_id>/edit")
def edit(order_id: str) -> dict[str, Any]:
    b = _body()
    return _run(actions.rep_edit, _deps(), order_id, _actor(), b.get("field"), b.get("value"))


@app.post("/rep/orders/<order_id>/replan")
def replan(order_id: str) -> dict[str, Any]:
    return _run(actions.rep_replan, _deps(), order_id, _actor())


@app.post("/rep/orders/<order_id>/resolve")
def resolve(order_id: str) -> dict[str, Any]:
    return _run(actions.rep_resolve, _deps(), order_id, _actor(), _body().get("action"))


@app.post("/rep/orders/<order_id>/reextract")
def reextract(order_id: str) -> dict[str, Any]:
    return _run(actions.rep_reextract, _deps(), order_id, _actor())


def simulated_email(body: dict[str, Any], deps: Deps) -> tuple[str, bytes]:
    """Build the .eml SES would have stored, for the dashboard's "simulate email" box."""
    msg = EmailMessage()
    msg["From"] = body.get("from") or "customer@example.com"
    msg["To"] = deps.sender_email or "orders@example.com"
    msg["Subject"] = body.get("subject", "")
    msg["Date"] = format_datetime(deps.now())
    msg["Message-ID"] = make_msgid(domain="dashboard.simulated")
    if body.get("inReplyTo"):
        msg["In-Reply-To"] = body["inReplyTo"]
    msg.set_content(body.get("text", ""))
    key = f"inbound/simulated/{deps.now():%Y/%m/%d}/{msg['Message-ID'].strip('<>')}.eml"
    return key, bytes(msg)


@app.post("/rep/simulate")
def simulate() -> dict[str, Any]:
    deps = _deps()
    key, raw = simulated_email(_body(), deps)
    deps.raw.put(key, raw, "message/rfc822")
    return {"ok": True, "s3Key": key}


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return app.resolve(event, context)
