# Demo Setup Guide: AI Freight Coordination on AWS

This guide walks through standing up the demo, from an empty AWS account to running the end-to-end demo and tearing everything down. For each step it says **where to click in the AWS Console**, **what to set**, and **why**. When the CLI or CDK does a step, the guide also says how to confirm in the console that it worked, so you learn what each service is doing.

> **About console labels:** AWS renames console menus from time to time. If a label doesn't match exactly, use the search bar at the top of the console and type the service name. The settings themselves stay the same.

**Region for everything: US East (N. Virginia), `us-east-1`.** Set the region selector (top-right of the console) to it now and check it before every step. "I can't find my resource" is almost always a wrong region.

**Total time:** about 2–3 hours the first time, then about 10 minutes per redeploy.
**Total cost target:** under $50, expected $10–20 (see `PLAN.md` §5).

**Progress check at any point:** `uv run scripts/verify.py` runs every phase's "done when" check that works locally; add `--live` (Bedrock eval, ~$0.10) and `--deployed` (the checks in Parts D–E) once your account is ready.

---

## Part A: Account foundations (one time, about 30 minutes)

### A1. Secure the root user
1. Sign in as root → click your account name (top-right) → **Security credentials**.
2. **Multi-factor authentication (MFA)** → **Assign MFA device** → Authenticator app → scan the QR code → enter two codes.
3. Under **Access keys**, make sure there are **none** for root.

*Why:* the root user can close the account and change billing. You should only ever use it for billing settings.

### A2. Create an admin identity for daily work (IAM Identity Center)
1. Console search → **IAM Identity Center** → **Enable** (choose the organization instance if asked).
2. **Users** → **Add user** → username `alon-admin`, your email → **Next** → skip groups → **Add user**. Accept the invite email and set a password and MFA.
3. **Permission sets** → **Create permission set** → **Predefined** → `AdministratorAccess` → session duration 8h → **Create**.
4. **AWS accounts** → tick your account → **Assign users or groups** → pick `alon-admin` → pick `AdministratorAccess` → **Submit**.
5. Copy the **AWS access portal URL** from the Identity Center **Dashboard** (format `https://d-xxxxxxxxxx.awsapps.com/start`).

*Why:* this gives you short-lived credentials and no long-lived access keys on your laptop. (Simpler but less secure alternative: an IAM user with access keys. Avoid it if you can.)

### A3. Billing guardrails (do this before deploying anything)
1. As root (or after granting billing access to IAM): **Billing and Cost Management** → **Billing preferences**:
   - Turn on **Receive AWS Free Tier alerts** and enter your email.
   - Turn on **Receive CloudWatch billing alerts**.
2. **Budgets** → **Create budget** → **Use a template**? No: choose **Customize (advanced)** → **Cost budget** → Next.
   - Name `freight-demo-monthly` (the readiness check looks for this exact name), Period **Monthly**, Recurring, **Fixed** $50.
   - Scope: **All AWS services**.
   - **Add alert threshold** four times:
     | Threshold | Trigger |
     |---|---|
     | 20% ($10) | Actual |
     | 50% ($25) | Actual |
     | 80% ($40) | Actual |
     | 100% ($50) | **Forecasted** |
   - Email recipients: your address → **Create budget**.
3. **Cost Explorer** → **Launch Cost Explorer** (the first launch takes up to 24h to populate. That's fine.)

*Why:* Budgets only **alerts**, it doesn't stop spending. The code adds the hard stops: an LLM kill switch, reserved concurrency limits, and no always-on infrastructure.

---

## Part B: Service prerequisites (one time, about 20 minutes)

### B1. Amazon Bedrock: enable Claude Haiku 4.5
1. Region **us-east-1** → console search **Amazon Bedrock**.
2. Left nav → **Model access** (it may sit under *Bedrock configurations* near the bottom).
3. **Modify model access** (or **Enable specific models**) → under **Anthropic** tick **Claude Haiku 4.5** → **Next**.
4. Anthropic models ask for a one-time **use case form**: company name (personal is fine), use case "internal demo: extracting shipment details from emails", and no external end users. Submit, then **Submit** the access request.
5. Wait until the status shows **Access granted**. This usually takes minutes.
6. **Test it:** left nav → **Playgrounds → Chat / Text** → **Select model** → Anthropic → Claude Haiku 4.5 → type "Extract quantity from: 'We need 18 pallets of Corona Extra'" → **Run**.
7. Write down the **inference profile ID**. In the model catalog, open Claude Haiku 4.5 → **Cross-region inference**; it looks like `us.anthropic.claude-haiku-4-5-20251001-v1:0`. That is the default the code uses. If your console shows a different ID, pass it as `-c bedrockModelId=…` in Part D. (The code also accepts an `anthropic.`-prefixed Mantle model ID; see Troubleshooting.)
8. Optional: check **Bedrock pricing** (aws.amazon.com/bedrock/pricing) for Haiku 4.5 in us-east-1 and update `src/freight/extraction/llm/pricing.py` if it differs from $1 / $5 per million tokens.

*Why:* Bedrock calls fail with `AccessDeniedException` until model access is granted. This is the #1 setup error.

### B2. Amazon SES: verify email identities (sandbox mode)
New accounts are in the **SES sandbox**: you can only send **to and from verified addresses**, at most 200 per day and 1 per second. That's ideal for a demo because it's impossible to email a real stranger.

1. Console search **Amazon SES** → region **us-east-1**.
2. Left nav **Configuration → Identities** → **Create identity**.
3. **Identity type: Email address** → enter your sender address (e.g. your Gmail) → **Create identity**.
4. Open the verification email → click the link. The status becomes **Verified**.
5. Repeat for:
   - **customer** address: plays "Biergarten Berlin". Use a Gmail alias like `you+bar@gmail.com`; it works with plus-addressing.
   - **rep** address: plays the Corona representative, e.g. `you+rep@gmail.com`.

   > Gmail plus-aliases each need their own verification click, but they all land in your one inbox.
6. Left nav **Account dashboard**: confirm it says *Your account is in the sandbox*. **Do not request production access.** It isn't needed.
7. **Test:** open the sender identity → **Send test email** → Scenario **Custom** → To: the customer alias → **Send**. Check your inbox and the spam folder.

> **Gmail and deliverability:** mail sent "from" a Gmail address through SES may land in spam or show "via amazonses.com", because Gmail's DMARC policy doesn't authorize SES. For a demo this is acceptable: add a Gmail filter "from:(you@gmail.com) has the words 'FC-' → Never send to spam". The proper fix is your own domain (Appendix 1).

*Why there's no inbound setup here:* SES **receiving** needs a domain with an MX record. Instead, the demo drops `.eml` files into the same S3 prefix that SES would write to (Part E), so the code path is identical.

### B3. Optional: check service quotas
Console search **Service Quotas** → **AWS services** → **Amazon Bedrock** → search "Claude Haiku 4.5" → look at *On-demand InvokeModel requests per minute*. The default is plenty for a demo; just know where to look if you see `ThrottlingException`.

### B4. Phase 0 check
Once Part C1 below gives you CLI credentials, run:
```bash
uv run scripts/bootstrap_check.py --phase0 \
  --sender you@gmail.com --customer you+bar@gmail.com --rep you+rep@gmail.com
# ✔ Bedrock extraction OK (model us.anthropic.claude-haiku-4-5-20251001-v1:0, quantity=18 pallets, 2400 ms, $0.0080)
# ✔ Budget freight-demo-monthly exists (50.0 USD)
# ✔ SES identities verified: 3/3, sandbox=True
```

---

## Part C: Local workstation (one time, about 20 minutes)

| Tool | Install (macOS) | Check |
|---|---|---|
| AWS CLI v2 | `brew install awscli` | `aws --version` |
| Node.js 22 or 24 LTS (for the CDK CLI; Node 20 is end-of-life) | `brew install node@22` | `node -v` |
| AWS CDK CLI | `npm install -g aws-cdk` | `cdk --version` |
| uv (Python) | `brew install uv` | `uv --version` |

No Docker is needed: `infra/build.py` downloads Linux arm64 wheels with uv and bundles the Lambda code on your Mac.

### C1. Configure CLI credentials via SSO
```bash
aws configure sso --profile freight
#  SSO session name: freight
#  SSO start URL:    <the access portal URL from A2>
#  SSO region:       us-east-1
#  (browser opens → approve) → choose the account → AdministratorAccess
#  CLI default region: us-east-1   output: json
aws sts get-caller-identity --profile freight     # shows your account id
export AWS_PROFILE=freight                         # use it for the rest of this session
```
Next time the session expires, run `aws sso login --profile freight`.

### C2. Bootstrap CDK (once per account and region)
```bash
cdk bootstrap aws://<ACCOUNT_ID>/us-east-1
```
**Console check:** **CloudFormation** → **Stacks** → you should see **CDKToolkit** with status `CREATE_COMPLETE`. Click it → **Resources**: an S3 staging bucket, an ECR repo, and IAM roles that CDK uses to deploy. *Why:* CDK uploads Lambda code and assets there before CloudFormation deploys them.

---

## Part D: Deploy the application (about 15 minutes)

### D1. Configure and deploy
```bash
cd /Users/alonsha/dev/gitmine/cooranadination
uv sync
uv run scripts/verify.py               # every local check green before you spend money
cd infra
cdk synth                              # renders CloudFormation; cdk-nag must report nothing unresolved
cdk deploy --all \
  -c senderEmail=you@gmail.com \
  -c customerEmail=you+bar@gmail.com \
  -c repEmail=you+rep@gmail.com \
  -c alarmEmail=you@gmail.com \
  -c bedrockModelId=<ID from B1 step 7, optional>
```
`cdk.json` runs the app with `uv run`, so the CDK CLI uses the project's Python environment. CDK shows an IAM changes table per stack and asks `Do you wish to deploy these changes (y/n)?`. Read it: this is exactly the set of permissions each Lambda gets. Answer `y`.

Four stacks deploy in order: **FreightStorage**, **FreightApi**, **FreightWorkflow**, **FreightObservability**. When they finish, the **Outputs** are printed. You don't need to copy them: the scripts read them from CloudFormation. The ones you'll use by hand are `DashboardUrl`, `CognitoUserPoolId` and `ApiUrl`.

### D2. Post-deploy manual steps
**1. Confirm the alarm email subscription.** AWS SNS sends "AWS Notification – Subscription Confirmation" to `alarmEmail` → click **Confirm subscription**.

**2. Create the link-signing key and the demo customer:**
```bash
cd ..
uv run scripts/seed.py
# ✔ created /freight/demo/link-hmac-key (SecureString, AWS-managed key)
# ✔ customer profile Biergarten Berlin GmbH <you+bar@gmail.com>
```
CloudFormation can't create SecureString parameters, which is why this is a script. To do it in the console instead: **Systems Manager** → **Parameter Store** → **Create parameter** → Name `/freight/demo/link-hmac-key`, Tier **Standard**, Type **SecureString**, KMS key **alias/aws/ssm**, Value: the output of `openssl rand -hex 32`. You still need `seed.py` for the customer profile.

**3. Create the rep user in Cognito:**
- **Amazon Cognito** → **User pools** → `freight-reps` → **Users** → **Create user**
  - Invitation: **Send an email invitation**, User name: the rep email, Email: the rep email, tick **Mark email address as verified**, **Generate a password** → **Create user**.
- Open `DashboardUrl` → **Sign in** → use the temporary password from the invitation email → set a new one. You land on the **Freight Rep Desk**.

**4. Run the readiness check:**
```bash
uv run scripts/bootstrap_check.py
# ✔ Bedrock extraction OK (model …, quantity=18 pallets, … ms, $0.008)
# ✔ Budget freight-demo-monthly exists (50.0 USD)
# ✔ SES identities verified: 3/3, sandbox=True
# ✔ Stacks: FreightStorage, FreightApi, FreightWorkflow, FreightObservability
# ✔ SSM /freight/demo/link-hmac-key present
```

### D3. Console tour: what CDK built, and where to look

Spend 20 minutes here once. It's how you learn what each service does in this design.

| Service → console path | What you should see | What to look at / learn |
|---|---|---|
| **CloudFormation → Stacks** | 4 `Freight*` stacks + `CDKToolkit` | **Resources** tab of each; **Template** tab is what CDK generated |
| **S3 → Buckets** | `freight-raw-<account>-us-east-1` | **Properties**: Default encryption SSE-S3, lifecycle (90 days). **Permissions**: Block all public access ON, a bucket policy denying non-TLS requests. **Event notifications**: `inbound/` → SQS `freight-ingest` |
| **SQS → Queues** | `freight-ingest`, `freight-ingest-dlq`, `freight-outbox-dlq` | `freight-ingest` → **Dead-letter queue** tab (maxReceiveCount 3); **Lambda triggers** tab → `freight-ingest` |
| **Lambda → Functions** | `freight-ingest`, `-extract`, `-workflow`, `-quote`, `-book`, `-wait`, `-send-outbox`, `-rep-api`, `-choose`, `-mock-provider`, `-cache-invalidate` | **Configuration → General**: arm64, memory. **→ Concurrency** on `freight-extract`: reserved concurrency 5, the Bedrock cost guard (the only function that calls the model). **→ Environment variables**: table names, model ID. **→ Permissions**: only `extract` may call Bedrock, only `send-outbox` may send email. **→ Triggers** on `send-outbox`: the DynamoDB stream with a filter for new `OUTBOX#` rows |
| **Step Functions → State machines** | `freight-order` (Type: **Standard**) | **Definition** → graph: Route → Extract → WaitForCustomerInfo (callback) → Plan → Quote (Parallel, 3 providers) → Rank → WaitForRep (callback) → SendQuote → WaitForChoice (callback) → Revalidate → BookMain → BookTruck → Complete. Every task has Retry and a Catch to **Escalate** → WaitForHuman |
| **DynamoDB → Tables** | `freight`, `freight-cache`, `freight-idempotency`, `freight-mock` | `freight` → **Indexes** (GSI1, GSI2), **Exports and streams** (stream on), **Backups** (PITR on). `freight-cache` → **Additional settings → Time to Live** on `ttl`. All on-demand |
| **EventBridge → Rules** (default bus) | `freight-price-changed → freight-cache-invalidate` | **Event pattern**: source `freight.app`, detail-type `PriceChanged` |
| **API Gateway → APIs** | `freight-http-api` (HTTP) | **Routes**: `/dashboard*` (public static page), `/rep/{proxy+}` (JWT authorizer), `/choose` (public, signed tokens), `/mock/{proxy+}` (the mock providers). **Stages** → `$default` URL = `ApiUrl` |
| **Cognito → User pools** | `freight-reps` | **App integration** → domain `freight-<account>`; app client `freight-dashboard` with callback URL = `DashboardUrl` |
| **Systems Manager → Parameter Store** | `/freight/demo/llm-enabled`, `email-mode`, `chaos/*`, `link-hmac-key` | Runtime switches (Part E4), read every few seconds, so no redeploy |
| **CloudWatch → Dashboards** | `Freight` | Time to first quote and to booking, human review rate, LLM tokens and cost, extraction latency and quality signals, providers, orders, DLQs |
| **CloudWatch → Alarms** | 5 alarms, state OK (or Insufficient data) | DLQs not empty, executions failed, Lambda errors, LLM cost per day > $2, provider circuit open |
| **CloudWatch → Log groups** | `/aws/lambda/freight-*`, `/aws/vendedlogs/states/freight-order` | **Retention** column says 1 week (cost control) |
| **SES → Account dashboard** | sends counter | **Sending statistics** after the demo |

---

## Part E: Run the demo (about 15 minutes per run)

### E1. Automated happy path
```bash
uv run scripts/demo_e2e.py                    # interactive: pauses at each step and tells you where to look
uv run scripts/demo_e2e.py --non-interactive  # no pauses; asserts the result (used by verify.py --deployed)
```
The non-interactive run approves as the rep and clicks the customer link itself, then checks: COMPLETED, 1 quote round, 3 emails (questions, quote, confirmation), 2 bookings (ocean and truck). Add `--email-mode simulator` to keep it out of your inbox.

### E2. The same thing by hand, walking through the console (good for presenting)

**Step 1: A customer email arrives (simulated inbound).**
- Option A (CLI): `uv run scripts/send_inbound.py tests/fixtures/emails/de/01_missing_date_pallets.eml --fresh`
  (`--fresh` gives it a new Message-ID; without it, a second run is correctly ignored as a duplicate. The script also swaps the fixture's placeholder sender for your customer address.)
- Option B (dashboard): **Simulate email** → paste text → **Send**.
- Option C (console, the most visual): **S3** → `freight-raw-…` → `inbound/` → **Upload** an `.eml` whose `From:` is your customer address. *This is exactly what SES would do on a real inbound email.*
- Look at:
  - **SQS** → `freight-ingest` → **Monitoring**: a message in flight.
  - **Step Functions** → `freight-order` → **Executions**: a new execution **named after the order ID** (e.g. `FC-7Q2K9D`). Open it → **Graph view** shows Extract done and **WaitForCustomerInfo** waiting (blue).
  - **DynamoDB** → `freight` → **Explore table items** → **Query**: PK = `ORDER#FC-7Q2K9D`. You'll see `META` (state `AWAITING_CUSTOMER_INFO`), `EMAIL#…` rows (in and out), `EXTRACTION#0001` (open it: each field has `status`, `confidence`, and `sources[].quote`; `pickup` has the source `system`, the default brewery), `OUTBOX#…` (status `SENT`), and several `EVENT#…` audit rows.
  - **Your inbox** (customer alias): a German email asking *only* for the pallet count and the delivery date, with `[FC-7Q2K9D]` in the subject.

**Step 2: The customer replies, with a changed subject.**
- `uv run scripts/send_inbound.py tests/fixtures/emails/de/02_reply_changed_subject.eml --order FC-7Q2K9D --fresh`
  (`--order` adds the `In-Reply-To` header pointing at our SES message, as a mail client would. Without it, the reply is still matched because it's this customer's only order waiting on them.)
- Look at:
  - **Step Functions graph**: WaitForCustomerInfo turns green, Extract runs again, then Plan, the Parallel quotes, Rank, and it waits at **WaitForRep**.
  - **DynamoDB**: `EXTRACTION#0002`. Fields from email 1 keep their sources; quantity and date point at email 2.

**Step 3: Planning and quotes.**
- **CloudWatch → Log groups** → `/aws/lambda/freight-workflow` → the latest stream. A structured log shows the plan: "18 US pallets, 16,175 kg, not stackable, 34.7 m3; FCL 1x40GP (floor bound, weight 61%, floor 90%, volume 51%); …; air 3xPMC on passenger, chargeable 16,175 kg (billed on actual)".
- The order is waiting for a rep because it's this customer's first order and the air option is above $25k.

**Step 4: The rep reviews.**
- Open `DashboardUrl` → **Review** tab → open the order. The extracted fields show their **highlighted source quotes** in the emails, the review reasons, the plan, and the 3 options.
- Click **Approve & send quote**. The execution continues and the quote email arrives in the customer inbox.

**Step 5: The customer picks an option.**
- In the quote email, click the **"Ausgewogen" (balanced)** link → the confirmation page → **Confirm and book**.
- **Step Functions**: WaitForChoice → Revalidate (a live re-confirmation with the provider) → BookMain → BookTruck → Complete → **Succeeded**.
- **DynamoDB**: `BOOKING#main` and `BOOKING#truck`, each with its idempotency key; `META.state = COMPLETED`.
- The confirmation email arrives.

**Step 6: The evidence.**
- `uv run scripts/report_kpis.py`: time to first quote and to booking, split into system time, waiting on the customer and waiting on the rep, against the 5–7 day baseline.
- **CloudWatch → Dashboards → Freight**: time to first quote, LLM cost for the order (≈ $0.01–0.02).
- Optional, **CloudWatch → Logs Insights**: choose the `/aws/lambda/freight-*` groups and run
  ```
  fields @timestamp, order_id, step, message
  | filter order_id = "FC-7Q2K9D" | sort @timestamp asc
  ```
- **X-Ray** (CloudWatch → **X-Ray traces → Trace map**): the call graph across Lambda, Bedrock, DynamoDB and SES.

### E3. Chaos demo: the follow-up question, live
```bash
uv run scripts/chaos_race.py                  # creates a fresh order up to QUOTE_SENT first
uv run scripts/chaos_race.py --order FC-…     # or use an order that is at QUOTE_SENT
uv run scripts/chaos_race.py --approve-new    # also approve the new price at the end
```
The script:
1. Sets **Parameter Store** `/freight/demo/chaos/price-drift-pct` to `12`. You can do this in the console too: open the parameter → **Edit** → value → **Save changes**.
2. Uploads the order's *original* inbound email twice more, concurrently. These are the duplicate SES events.
3. Clicks the customer's choice link twice, concurrently, after the price moved.
4. Resets the drift to 0 and prints the checks.

Then show:
- **Step Functions**: still **one** execution for the order.
- **DynamoDB** `freight`: one `QUOTE#001#…` set, an `EVENT#…` row `PRICE_CHANGED_REVALIDATION_FAILED`, then `QUOTE#002#…` with the new prices and an `OUTBOX#…` for it. No `BOOKING#` rows yet.
- **Your inbox**: exactly one original quote, plus one "updated shipping options" email. No duplicates.
- With `--approve-new`: exactly one `BOOKING#main` and one `BOOKING#truck`.
- Reset by hand if a run was interrupted: `uv run scripts/chaos_race.py --reset`.

### E4. Other switches (Parameter Store, `/freight/demo/…`)
| Parameter | Effect |
|---|---|
| `llm-enabled` = `false` | **Kill switch**: new orders go to the NEEDS_HUMAN queue with no Bedrock calls, and the customer gets a "a representative will get back to you" email |
| `chaos/provider-down` = `air-carrier` | That provider answers 503. Orders still get ocean options; after 5 failures the circuit opens and the `freight-provider-circuit-open` alarm fires. Set back to `none` |
| `chaos/provider-failure-rate` = `0.5` | 50% of mock provider calls fail, which shows retries and the circuit breaker |
| `chaos/provider-latency-ms` = `3000` | Slow provider, which shows timeouts |
| `chaos/price-drift-pct` = `12` | Every provider price is 12% higher than its base; used by E3 |
| `email-mode` = `simulator` | All outbound mail goes to `success@simulator.amazonses.com` (no inbox noise). `log` = record only, send nothing |

---

## Part F: Between sessions and monitoring cost
- Leaving the stacks deployed costs roughly $0 per day while idle. The steady costs are custom metrics, alarms and the dashboard, about $3–5 per month in total.
- **Cost Explorer** → filter **Tag: project = freight-demo**. To make that work, first go to **Billing → Cost allocation tags** → find `project` → **Activate**. It only appears after resources exist, and data shows up after about 24h.
- **Billing → Bills**: check charges by service. Expect Bedrock and CloudWatch to be the top two.
- If you get a Budget alert, set `llm-enabled=false` first, then investigate in Cost Explorer (Group by: Service, then Usage type).

---

## Part G: Teardown (about 10 minutes)
```bash
scripts/teardown.sh        # asks you to type 'destroy', runs cdk destroy --all, deletes the link key
```
Then confirm in the console:
1. **CloudFormation → Stacks**: no `Freight*` stacks (filter "Deleted" to see them gone).
2. **S3**: no `freight-raw-…` bucket. It uses `autoDeleteObjects`. If it remains, **Empty** it, then **Delete** it.
3. **DynamoDB**: no `freight*` tables.
4. **CloudWatch → Log groups**: no `/aws/lambda/freight-*` (the script prints any leftovers). Delete stragglers by hand.
5. **Systems Manager → Parameter Store**: `/freight/demo/link-hmac-key` is gone (the script deletes it; CDK owned the others).
6. **Cognito**: `freight-reps` is gone with the FreightApi stack.
7. **SES → Identities**: optionally delete the 3 verified addresses.
8. **Bedrock**: nothing to delete. Model access costs nothing.
9. **Optional:** delete the **CDKToolkit** stack. Empty its `cdk-…-assets-…` bucket first. Keep it if you'll redeploy.
10. **Keep the Budget**: it's free (the first 2 budgets are free).
11. The next day, check **Billing → Bills**: no new charges accruing.

When you've done a full fresh deploy following this guide and torn it down, record it: `uv run scripts/verify.py --tick "fresh deploy"`.

---

## Troubleshooting

| Symptom | Cause → fix |
|---|---|
| `AccessDeniedException … model` from Bedrock | Model access isn't granted, or the wrong region or model ID → redo B1, and check the `bedrockModelId` context matches the console ID exactly |
| `ValidationException: Invocation of model ID … with on-demand throughput isn't supported` | Use the **inference profile** ID (`us.anthropic…`) instead of the bare model ID |
| Bedrock works in the check but `freight-extract` gets `AccessDenied` with an `anthropic.…` (Mantle) model ID | The Lambda policy covers the `bedrock:InvokeModel` path. Use the `us.anthropic…` inference profile (the default) |
| `MessageRejected: Email address is not verified` | Sandbox: the recipient isn't verified → verify it in B2, or set `email-mode=simulator`. The outbox row is marked `FAILED` and an `EMAIL_REJECTED` event is recorded |
| Emails never arrive | Check spam. **SES → Account dashboard** sending stats. The `freight-send-outbox` logs. The `OUTBOX#` item status |
| A second `send_inbound.py` run does nothing | The same Message-ID was already ingested, so it's a duplicate by design → add `--fresh` |
| A reply starts a new order | The customer has several open orders from earlier runs and the reply matched none clearly. Use `--order FC-…` (sets `In-Reply-To`), or cancel old orders in the dashboard |
| `cdk deploy`: `This stack uses assets… bootstrap` | Run C2 for this account and region |
| `cdk deploy`: `Specified ReservedConcurrentExecutions … decreases account's UnreservedConcurrentExecution below its minimum value of [10]` | New accounts can start with a Lambda concurrency limit of 10 (**Service Quotas → AWS Lambda → Concurrent executions**). Redeploy with `-c llmConcurrency=0` (no reservation; the daily LLM cost alarm and the kill switch remain) and request a quota increase to 1000 |
| `cdk synth` fails with a cdk-nag error | A new resource has an unreviewed finding. Fix it, or add a suppression with a reason in `infra/app.py` |
| `ExpiredToken` / `The SSO session … has expired` | `aws sso login --profile freight` |
| Execution waiting at a "callback" state | Expected: it's waiting for the customer or rep. `META.waitingFor` says who. Late replies are logged as `LATE_REPLY` events |
| Messages in `freight-ingest-dlq` or `freight-outbox-dlq` | Open **SQS → queue → Send and receive messages → Poll for messages** to see the body. After fixing, **Start DLQ redrive** (ingest), or re-insert the outbox row (outbox) |
| Order in NEEDS_HUMAN | Read the last `STATE_CHANGED` event's reason in the dashboard's audit trail, then *Re-run extraction*, *Re-plan* or *Cancel* |
| Step Functions execution **Failed** | Open the execution → the red state → **Error/Cause**. After fixing, use **Redrive** on the execution page |
| Dashboard login loop | The Cognito app client callback URL must equal `DashboardUrl` exactly. Redeploy FreightApi if the API URL changed |

---

## Appendix 1: Real inbound email with your own domain (optional, after the demo)
This takes the system from "simulated inbound" to real SES receiving with **no code change**: SES writes each email to the same `inbound/` prefix the demo uses. The receipt rule is set up in the console (it isn't in CDK).

1. Get a domain. Use **Route 53 → Registered domains → Register** (about $3–15 per year), or bring one from another registrar.
2. **SES → Identities → Create identity → Domain** → `example.com` → **Easy DKIM**, RSA 2048 → **Create**. Add the 3 DKIM CNAME records it shows. If the domain is on Route 53, click **Publish DNS records to Route 53**. Wait for the status to become **Verified**.
3. Add an **MX record** on the domain or a subdomain (recommended: `orders.example.com`) with the value `10 inbound-smtp.us-east-1.amazonaws.com`.
4. Let SES write to the raw bucket: **S3** → `freight-raw-…` → **Permissions → Bucket policy** → add a statement allowing principal `ses.amazonaws.com` to `s3:PutObject` on `arn:aws:s3:::freight-raw-…/inbound/*`, with the condition `aws:SourceAccount = <your account id>`.
5. **SES → Configuration → Email receiving → Create rule set** `freight-inbound` → **Set as active** → **Create rule**: recipient `orders.example.com`, action **Deliver to S3 bucket** → `freight-raw-…`, prefix `inbound/`.
6. Send an email to `anything@orders.example.com` from your customer address: it shows up as a new order exactly like a simulated one.
7. Optional: add a DMARC TXT record `_dmarc.example.com "v=DMARC1; p=none;"`, verify `quotes@example.com` and redeploy with `-c senderEmail=quotes@example.com` to fix the Gmail "via amazonses" and spam issue.
8. Optional: request SES **production access** (**Account dashboard → Request production access**) only if you'll email unverified addresses. Not needed for the demo.
9. Plus-addressing (`orders+FC7Q2K9D@orders.example.com`) would be a further threading signal (`PLAN.md` §2.1); the code doesn't use it yet.

> If you redeploy FreightStorage later, CDK rewrites the bucket policy and removes the statement from step 4. Re-add it after each deploy.

## Appendix 2: What runs where (quick mental model)
```
You (or SES) ──.eml──► S3 inbound/ ──event──► SQS ──► λ ingest ──► Step Functions (1 per order)
                                                                    │  λ extract ──► Bedrock (Haiku 4.5)
                                                                    │  λ workflow (plan, rank, send quote, complete)
                                                                    │  λ quote / λ book ──► mock providers (API GW + λ)
Dashboard (Cognito) ──► API GW ──► λ rep-api ──SendTaskSuccess─────►│  wait for rep
Customer link ──────► API GW ──► λ choose ──SendTaskSuccess────────►│  wait for customer
                                                                    │  λ wait (parks the task token)
DynamoDB freight ──stream (OUTBOX#)──► λ send-outbox ──► SES ──► your inbox
Provider price change ──► EventBridge ──► λ cache-invalidate ──► freight-cache
```
