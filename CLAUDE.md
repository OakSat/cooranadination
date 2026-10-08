# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

A system design exercise built out as a working AWS demo. `System Design Practice AI Freight Coordination.md` is the spec. A Corona customer in Germany emails a beer order; the system extracts shipment details with an LLM, asks the customer about anything missing, generates up to three shipping alternatives (cheapest / fastest / balanced), and books only after explicit approval.

- `PLAN.md` is the approved implementation plan: architecture, data model, state machine, phases, cost budget, and a traceability map from spec topics to code. Read it before adding a new component.
- `DEMO_SETUP_GUIDE.md` is the click-by-click AWS console runbook. Keep it in sync when infra, manual steps, or console-visible resource names change.

Status: Phase 1 is done (deterministic domain core). Extraction, email, persistence, orchestration (CDK), providers, and the dashboard are not built yet. See PLAN.md §4.

## Commands

```bash
uv sync                                   # install (Python >= 3.12; uv-managed .venv)
uv run pytest                             # all tests
uv run pytest tests/unit/test_ocean.py::test_free_volume_does_not_mean_more_cargo_fits
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # strict, pydantic plugin, checks src/
```

## Architecture (what spans multiple files)

- **The LLM is never the source of truth.** `src/freight/domain/` and `src/freight/planning/` are pure, deterministic code: no AWS, no LLM. Prices and availability come from providers; container and ULD fit, chargeable weight, cargo derivation and rule checks are computed here. Keep LLM calls out of these packages.
- **`Extracted[T]`** (`domain/models.py`) wraps every email-derived value with `status` (explicit / inferred / missing / conflicting), `confidence`, and `sources` (verbatim quotes). Models are `extra="forbid"` and frozen because they validate untrusted LLM output. `ShipmentRequest` fields default to MISSING.
- **The flow of a request:** `ShipmentRequest` → `domain/rules.validate_request` (business plausibility; violations become CLARIFY or HUMAN, never a booking) → `planning/cargo.derive_cargo` (pallets and weight from `catalogs/products.json`) → `planning/ocean` and `planning/air` → provider quotes → `planning/alternatives.select_alternatives`.
- **`domain/state.py`** holds the legal order transitions. Step Functions (to be built) runs the process; this table plus a conditional write on `(state, version)` is the business truth. A test enforces that BOOKING is unreachable without CUSTOMER_APPROVED. Preserve that invariant when you add transitions.
- **Catalogs:** `planning/catalogs/*.json` (containers, ULDs, products, locations) are loaded once per process via `functools.cache`. Values are demo approximations. `relative_cost` is only a ranking proxy.
- **Ocean fit lesson encoded in `ocean.fit`:** floor positions and payload are separate limits, and weight is checked per container because pallets are indivisible. Dense beer cargo is often weight-bound, so free volume doesn't mean more cargo fits.

## Constraints from the spec and plan

- Everything must be idempotent: duplicate SES/SQS events, repeated Lambda invocations, late replies, concurrent workers. No duplicate quotes, emails, or bookings.
- No booking without explicit customer approval. Sensitive quotes go through rep review first.
- The demo budget is under $50: pay-per-use serverless only. No NAT gateway, VPC, ElastiCache, or Secrets Manager (use SSM SecureString). Region `us-east-1`. LLM is Claude Haiku 4.5 on Bedrock behind a swappable client interface.
- There's no email domain: inbound mail is simulated by writing `.eml` files to the S3 prefix SES would use, and outbound uses the SES sandbox.
- Customs, export/import documents, insurance, alcohol restrictions and Incoterms are captured but not acted on (out of scope).
