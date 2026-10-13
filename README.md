# AI Freight Coordination

A system design exercise built out as a working, serverless AWS demo.

A bar in Germany emails Corona (Mexico) a beer order, in German, Spanish or English. The system:

1. Reads the email and extracts the shipment details with an LLM (Claude Haiku 4.5 on Amazon Bedrock).
2. Asks the customer, in their language, about anything missing or contradictory.
3. Plans the cargo with deterministic code: pallets, container and air-ULD fit, chargeable weight.
4. Gets quotes from freight providers (mocked) and offers up to three options: cheapest, fastest and balanced.
5. Sends sensitive quotes to a sales rep for review.
6. Books only after the customer explicitly approves an option, then confirms by email.

The spec is [`System Design Practice AI Freight Coordination.md`](System%20Design%20Practice%20AI%20Freight%20Coordination.md). It describes a manual process that takes 5–7 business days; the aim here is a few hours.

## Design in one paragraph

**The LLM is never the source of truth.** It handles language: reading emails and drafting questions. Everything else is plain, tested code:
- Every extracted value carries a status (explicit, inferred, missing or conflicting), a confidence, and verbatim quotes from the email. A value whose quote can't be found in the email is rejected.
- Prices and availability come from the providers.
- Fit, weight and business rules are computed in code.
- Each order is one Step Functions execution. It can wait days for the customer or the rep.
- Every state change is a conditional DynamoDB write, recorded in the same transaction as its audit event and any outgoing email. Duplicate emails, retried Lambdas and concurrent clicks therefore produce no duplicate quotes, emails or bookings.

## Architecture

```
.eml → S3 → SQS → λ ingest ──► Step Functions (one execution per order)
                                 Extract (Bedrock) → Validate → Ask customer ⟲
                                 → Plan → Quote (providers) → Rank 3 options
                                 → Rep review → Send quote → Customer choice (signed link)
                                 → Revalidate price → Book → Confirm
DynamoDB (orders, audit, outbox, cache) · SES (outbound, via a stream-driven outbox)
API Gateway + Cognito (rep dashboard, choice links, mock providers) · CloudWatch alarms · Budgets
```

The CDK app has four stacks: `FreightStorage`, `FreightApi`, `FreightWorkflow` and `FreightObservability`. There's no email domain. Inbound mail is simulated by writing `.eml` files to the S3 prefix that SES would write to, and outbound mail uses the SES sandbox.

## Repository map

| Path | What's there |
|---|---|
| `src/freight/domain/` | Models, legal state transitions, business rules, review policy. Pure code: no AWS, no LLM |
| `src/freight/planning/` | Cargo derivation, ocean and air fit, quoting, choosing the alternatives, catalogs |
| `src/freight/extraction/` | Prompt, LLM clients (Bedrock, Anthropic API, fake), the quote check, normalizing, merging, the injection guard, the eval |
| `src/freight/email/` | MIME parsing, thread matching, DE/ES/EN templates, outbox |
| `src/freight/workflow/` | The state machine graph, steps, rep and customer actions, signed links |
| `src/freight/persistence/` | DynamoDB single-table repo with conditional transitions |
| `src/freight/providers/` | Mock ocean, air and trucking providers with chaos switches; HTTP client; cache |
| `src/freight/handlers/` | Lambda entry points |
| `src/freight/dashboard/` | Rep review dashboard (vanilla JS, served by the API) |
| `infra/` | CDK app (Python) with cdk-nag checks |
| `scripts/` | Verify, setup check, seed, demo, chaos and KPI scripts, teardown |
| `tests/` | Unit tests, moto integration tests, in-process end-to-end runs, golden extraction set |

## Quick start (local, no AWS needed)

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest                                   # unit, integration, local end-to-end, CDK synth
uv run scripts/verify.py                        # per-phase "done when" checks
uv run scripts/eval_extraction.py --llm oracle  # self-test of the extraction eval
uv run python infra/app.py                      # synthesize the CDK app to infra/cdk.out
```

The local end-to-end tests drive the whole order flow in process: email in, questions, reply, rep review, quote, customer click, booking, confirmation. They also cover the race case from the spec's follow-up question: a duplicate email, a price change and two concurrent approval clicks.

## Deploying the demo

Follow [`DEMO_SETUP_GUIDE.md`](DEMO_SETUP_GUIDE.md), a click-by-click console and CLI runbook. You'll need an AWS account, the AWS CLI, and the CDK CLI (Node 22 or 24). In outline:

```bash
uv run scripts/bootstrap_check.py --phase0      # Bedrock access, budget, SES identities (~$0.01)
cd infra && cdk deploy --all -c senderEmail=… -c customerEmail=… -c repEmail=… -c alarmEmail=…
cd .. && uv run scripts/seed.py                 # link-signing key and demo customer
uv run scripts/demo_e2e.py                      # guided happy path
uv run scripts/chaos_race.py                    # the follow-up race, live
uv run scripts/verify.py --live --deployed      # everything, including the Bedrock eval (~$0.10)
scripts/teardown.sh                             # remove it all
```

The demo is designed to cost about $10–20 a month, with a hard ceiling of $50. It uses pay-per-use services only (no NAT, VPC or ElastiCache) and sets AWS Budgets alerts. Setting the SSM parameter `/freight/demo/llm-enabled` to `false` turns off Bedrock calls and sends orders to a human.

## Status

All phases are built and pass locally. The checks that remain need an AWS account: the Bedrock eval, the deployed demo and chaos runs, and three manual steps. Run `uv run scripts/verify.py` for per-phase status.

## Further reading

- [`docs/architecture.md`](docs/architecture.md) answers the spec's 14 design topics and the follow-up race question, with links into the code.
- [`PLAN.md`](PLAN.md) is the implementation plan: data model, state machine, phases and cost budget. §9 lists where the build differs from the plan.
- [`CLAUDE.md`](CLAUDE.md) has developer notes and commands.

Out of scope: customs, export and import documents, insurance, alcohol restrictions and Incoterms are captured from the email but not acted on.
