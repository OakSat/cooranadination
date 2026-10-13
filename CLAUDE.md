# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

A system design exercise built out as a working AWS demo. `System Design Practice AI Freight Coordination.md` is the spec. A Corona customer in Germany emails a beer order; the system extracts shipment details with an LLM, asks the customer about anything missing, generates up to three shipping alternatives (cheapest / fastest / balanced), and books only after explicit approval.

- `PLAN.md` is the approved implementation plan: architecture, data model, state machine, phases, cost budget, and a traceability map from spec topics to code. §9 lists where the build differs from it. Read it before adding a new component.
- `DEMO_SETUP_GUIDE.md` is the click-by-click AWS console runbook. Keep it in sync when infra, manual steps, scripts, or console-visible resource names change (`scripts/verify.py` checks it names every script and stack).
- `docs/architecture.md` answers the spec's 14 topics and the follow-up, linking to code. Keep the links valid (verify checks them).

Status: all phases are built and pass locally. What remains needs an AWS account: the Phase 0 checks, the golden-set eval on Bedrock, and the deployed demo and chaos runs. `uv run scripts/verify.py` shows per-phase status.

## Commands

```bash
uv sync                                   # install (Python >= 3.12; dev + infra groups)
uv run pytest                             # all tests (unit, moto integration, local end-to-end, cdk synth)
uv run pytest tests/unit/test_ocean.py::test_free_volume_does_not_mean_more_cargo_fits
uv run ruff check . && uv run ruff format --check .
uv run mypy                               # strict, pydantic plugin, checks src/
uv run scripts/verify.py                  # each phase's "done when"; --live (Bedrock), --deployed (AWS)
uv run scripts/eval_extraction.py --llm oracle   # eval harness self-test; without --llm: Bedrock, ~$0.10
cd infra && cdk synth                     # needs the CDK CLI; `uv run python infra/app.py` also synthesizes
```

## Architecture (what spans multiple files)

- **The LLM is never the source of truth.** `src/freight/domain/` and `src/freight/planning/` are pure, deterministic code: no AWS, no LLM. Prices and availability come from providers; container and ULD fit, chargeable weight, cargo derivation and rule checks are computed here. Keep LLM calls out of these packages.
- **`Extracted[T]`** (`domain/models.py`) wraps every email-derived value with `status` (explicit / inferred / missing / conflicting), `confidence`, and `sources` (verbatim quotes). Models are `extra="forbid"` and frozen because they validate untrusted LLM output. `ShipmentRequest` fields default to MISSING. Code-filled values use source `system`, rep edits `rep:<email>`.
- **Extraction pipeline** (`extraction/extractor.py`): prompt → LLM port (`llm/port.py`; Bedrock, first-party API, or fake) → `provenance.verify` (quotes must exist in a customer email) → `normalize.to_request` (units converted in code, each field validated alone) → `merge.merge` (never forget a known value; a different value is a conflict unless it answers our question).
- **The flow of a request:** `ShipmentRequest` → `domain/rules.validate_request` (violations become CLARIFY or HUMAN, never a booking) → `planning/cargo.derive_cargo` → `planning/quoting.plan` (ocean + air plans, provider requests) → provider quotes → `quoting.build_options` + `alternatives.select_alternatives`.
- **The workflow** is defined once in `workflow/graph.py`. CDK renders it to ASL; `tests/integration/local_sfn.py` interprets the same graph in-process. Each state calls a function in `workflow/steps.py` through `handlers/workflow.py:dispatch`. Steps read state from DynamoDB (only ids travel through Step Functions) and change it only through `Repo.transition`.
- **`domain/state.py`** holds the legal order transitions. `Repo.transition` applies one with a conditional write on `(state, version, customerId)`, in the same transaction as its audit `EVENT#` and any outbox, quote or extraction rows. A `step` key makes a retried step a no-op. Tests enforce that BOOKING is unreachable without CUSTOMER_APPROVED, both in the transition table and in the graph. Preserve that invariant when you add transitions or states.
- **Waiting for people:** `.waitForTaskToken` states call `steps.wait_for`, which parks the token on META. Answers go through `steps.resume` (ingest, rep actions, the choose link). Either side may come first: the answer is flagged on META and the token is claimed exactly once. Customer approval is the one transition made outside a step (`workflow/actions.confirm_choice`), because it is the customer's act.
- **Email out = outbox row.** Never send email directly: add `steps.outbound(...)` puts to a transition. The DynamoDB stream triggers `workflow/outbox_sender.py`, which claims the row (`PENDING → SENDING`) before calling SES.
- **Catalogs:** `planning/catalogs/*.json` (containers, ULDs, products, locations) are loaded once per process via `functools.cache`. Values are demo approximations. `relative_cost` is only a ranking proxy.
- **Ocean fit lesson encoded in `ocean.fit`:** floor positions and payload are separate limits, and weight is checked per container because pallets are indivisible. Dense beer cargo is often weight-bound, so free volume doesn't mean more cargo fits.

## Testing notes

- `tests/integration/conftest.py` serializes moto's DynamoDB calls: moto implements transactions by snapshot and restore, which lets concurrent transactions undo each other. Real DynamoDB is atomic per request.
- End-to-end tests script the LLM from the golden labels (`evaluation.oracle_output`). New fixture emails need matching golden cases.
- Lambda code is bundled by `infra/build.py` (uv, Linux arm64 wheels, no Docker); the build is cached by a hash of `uv.lock` and `src/`.

## Constraints from the spec and plan

- Everything must be idempotent: duplicate SES/SQS events, repeated Lambda invocations, late replies, concurrent workers. No duplicate quotes, emails, or bookings.
- No booking without explicit customer approval. Sensitive quotes go through rep review first (`domain/policy.py`).
- The demo budget is under $50: pay-per-use serverless only. No NAT gateway, VPC, ElastiCache, or Secrets Manager (use SSM SecureString). Region `us-east-1`. LLM is Claude Haiku 4.5 on Bedrock behind a swappable client interface.
- There's no email domain: inbound mail is simulated by writing `.eml` files to the S3 prefix SES would use, and outbound uses the SES sandbox.
- Customs, export/import documents, insurance, alcohol restrictions and Incoterms are captured but not acted on (out of scope).
- cdk-nag runs on every synth; a new finding must be fixed or suppressed with a reason in `infra/app.py`.
