# Demo Setup Guide: AI Freight Coordination on AWS

This guide walks through standing up the demo, from an empty AWS account to running the end-to-end demo and tearing everything down. For each step it says **where to click in the AWS Console**, **what to set**, and **why**. When the CLI or CDK does a step, the guide also says how to confirm in the console that it worked, so you learn what each service is doing.

> **About console labels:** AWS renames console menus from time to time. If a label doesn't match exactly, use the search bar at the top of the console and type the service name. The settings themselves stay the same.

**Region for everything: US East (N. Virginia), `us-east-1`.** Set the region selector (top-right of the console) to it now and check it before every step. "I can't find my resource" is almost always a wrong region.

**Total time:** about 2–3 hours the first time, then about 10 minutes per redeploy.
**Total cost target:** under $50, expected $10–20 (see `PLAN.md` §5).

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
   - Name `freight-demo-monthly`, Period **Monthly**, Recurring, **Fixed** $50.
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
7. Write down the **model ID / inference profile ID**. In the model catalog, open Claude Haiku 4.5 and copy the ID; it looks like `anthropic.claude-haiku-4-5…` or the cross-region profile `us.anthropic.claude-haiku-4-5-20251001-v1:0`. It goes into the CDK context in Part D.
8. Optional: check **Bedrock pricing** (aws.amazon.com/bedrock/pricing) for Haiku 4.5 in us-east-1 and put the real numbers into `src/freight/extraction/llm/pricing.py`.

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

*Why there's no inbound setup here:* SES **receiving** needs a domain with an MX record. Instead, the demo drops `.eml` files into the same S3 prefix that SES would write to (Part E3), so the code path is identical.

### B3. Optional: check service quotas
Console search **Service Quotas** → **AWS services** → **Amazon Bedrock** → search "Claude Haiku 4.5" → look at *On-demand InvokeModel requests per minute*. The default is plenty for a demo; just know where to look if you see `ThrottlingException`.

---

## Part C: Local workstation (one time, about 20 minutes)

| Tool | Install (macOS) | Check |
|---|---|---|
| AWS CLI v2 | `brew install awscli` | `aws --version` |
| Node.js LTS (for the CDK CLI) | `brew install node` | `node -v` |
| AWS CDK CLI | `npm install -g aws-cdk` | `cdk --version` |
| uv (Python) | `brew install uv` | `uv --version` |
| Docker Desktop (bundles Lambda dependencies for arm64) | download from docker.com and **start it** | `docker ps` |

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
uv run pytest                          # everything green before you spend money
cd infra
uv run cdk synth                       # renders CloudFormation; cdk-nag findings print here
uv run cdk deploy --all \
  -c senderEmail=you@gmail.com \
  -c customerEmail=you+bar@gmail.com \
  -c repEmail=you+rep@gmail.com \
  -c bedrockModelId=<ID from B1 step 7> \
  -c alarmEmail=you@gmail.com
```
CDK shows an IAM changes table and asks `Do you wish to deploy these changes (y/n)?`. Read it: this is exactly the set of permissions each Lambda gets. Answer `y`.

When it finishes, the **Outputs** are printed. Save them:
`ApiUrl`, `DashboardUrl`, `CognitoUserPoolId`, `CognitoHostedUiUrl`, `RawBucketName`, `StateMachineArn`, `MockProviderUrl`.

### D2. Post-deploy manual steps
**1. Confirm the alarm email subscription.** AWS SNS sends "AWS Notification – Subscription Confirmation" to `alarmEmail` → click **Confirm subscription**.

**2. Create the HMAC secret for customer approval links** (CloudFormation can't create SecureString parameters, so this one is manual):
- Console: **Systems Manager** → **Parameter Store** → **Create parameter**
  - Name `/freight/demo/link-hmac-key`, Tier **Standard** (free), Type **SecureString**, KMS key **alias/aws/ssm** (AWS-managed, free)
  - Value: the output of `openssl rand -hex 32` → **Create parameter**.
- Or run `uv run scripts/seed.py`, which creates it if it's missing.

**3. Seed demo data** (customer profile, catalogs check):
```bash
uv run scripts/seed.py
```

**4. Create the rep user in Cognito:**
- **Amazon Cognito** → **User pools** → `freight-reps` → **Users** → **Create user**
  - Invitation: **Send an email invitation**, username = rep email, **Generate a password** → **Create user**.
- Open `CognitoHostedUiUrl` (or `DashboardUrl`, which redirects to it) → sign in with the temporary password → set a new one.

**5. Run the readiness check:**
```bash
uv run scripts/bootstrap_check.py
# ✔ Bedrock invoke OK (model …, $0.0003)
# ✔ SES identities verified: 3/3, sandbox=true
# ✔ SSM /freight/demo/link-hmac-key present
# ✔ Budget freight-demo-monthly exists
# ✔ Stacks: FreightStorage, FreightWorkflow, FreightApi, FreightDashboard, FreightObservability
```

### D3. Console tour: what CDK built, and where to look

Spend 20 minutes here once. It's how you learn what each service does in this design.

| Service → console path | What you should see | What to look at / learn |
|---|---|---|
| **CloudFormation → Stacks** | 5 `Freight*` stacks + `CDKToolkit` | **Resources** tab of each; **Template** tab is what CDK generated |
| **S3 → Buckets** | `freight-raw-…` (emails, attachments), `freight-archive-…` (LLM prompts/responses), `freight-dashboard-…` | **Properties**: Default encryption SSE-S3, **Permissions**: Block all public access ON (except the dashboard website bucket if using S3 website hosting), **Event notifications**: `inbound/` → SQS |
| **SQS → Queues** | `freight-ingest`, `freight-ingest-dlq`, `freight-outbox`, `freight-outbox-dlq` | `freight-ingest` → **Dead-letter queue** tab (maxReceiveCount 3); **Lambda triggers** tab → ingest function |
| **Lambda → Functions** | `freight-ingest`, `-extract`, `-plan`, `-quote`, `-revalidate`, `-book`, `-send-outbox`, `-rep-api`, `-choose`, `-mock-provider`, `-cache-invalidate` | **Configuration → General**: arm64, memory; **→ Concurrency**: reserved concurrency (cost guard); **→ Environment variables**: table names, `EMAIL_MODE`, model ID; **→ Permissions**: the execution role is least-privilege |
| **Step Functions → State machines** | `freight-order` (Type: **Standard**) | **Definition** tab → graph: Extract → Choice → AskCustomer (wait for callback) → Plan → Parallel quotes → RepReview (callback) → SendQuote → CustomerChoice (callback) → Revalidate → Book → Truck → Done. Note the **Retry/Catch** on each task |
| **DynamoDB → Tables** | `freight`, `freight-cache`, `freight-idempotency` | `freight` → **Indexes** (GSI1, GSI2); `freight-cache` → **Additional settings → Time to Live** enabled on `ttl`; capacity mode **On-demand** |
| **EventBridge → Rules** (default bus) | `freight-price-changed → cache-invalidate`, `freight-state-changed → metrics` | **Event pattern** of each rule |
| **API Gateway → APIs** | `freight-http-api` (HTTP) | **Routes** (`/rep/*` JWT-protected, `/choose`, `/mock/*`), **Authorization** → Cognito JWT authorizer, **Stages** → `$default` URL = `ApiUrl` |
| **Cognito → User pools** | `freight-reps` | **App integration** → hosted UI domain + app client callback URL = dashboard |
| **Systems Manager → Parameter Store** | `/freight/demo/llm-enabled=true`, `/freight/demo/chaos/*`, `/freight/demo/link-hmac-key` | These are your runtime switches (Part F) |
| **CloudWatch → Dashboards** | `Freight` | LLM tokens and cost, extraction latency, time-to-quote, DLQ depth, failed executions |
| **CloudWatch → Alarms** | 5 alarms, state OK | DLQ>0, SFN failed, Lambda errors, LLM cost/day>$2, provider circuit open |
| **CloudWatch → Log groups** | `/aws/lambda/freight-*` (retention 7 days) | **Retention** column says 1 week (cost control) |
| **SES → Account dashboard** | sends counter | **Sending statistics** after the demo |

---

## Part E: Run the demo (about 15 minutes per run)

### E1. Automated happy path
```bash
uv run scripts/demo_e2e.py            # interactive: pauses at each step and tells you where to look
```

### E2. The same thing by hand, walking through the console (good for presenting)

**Step 1: A customer email arrives (simulated inbound).**
- Option A (CLI): `uv run scripts/send_inbound.py tests/fixtures/emails/de/01_missing_date_pallets.eml`
- Option B (console, the most visual):
  **S3** → `freight-raw-…` → open the `inbound/` folder → **Upload** → **Add files** → choose the `.eml` → **Upload**.
  *This is exactly what SES would do on a real inbound email.*
- Look at:
  - **SQS** → `freight-ingest` → **Monitoring**: a message in flight.
  - **Step Functions** → `freight-order` → **Executions**: a new execution **named after the order ID** (e.g. `FC-7Q2K9D`). Open it → **Graph view** shows it running Extract and pausing at **AskCustomer** (blue, waiting for callback).
  - **DynamoDB** → `freight` → **Explore table items** → **Query**: PK = `ORDER#FC-7Q2K9D`. You'll see `META` (state `AWAITING_CUSTOMER_INFO`), `EMAIL#…`, `EXTRACTION#1` (open it: each field has `status`, `confidence`, and `sources[].quote`), `OUTBOX#…` (status `SENT`), and several `EVENT#…` audit rows.
  - **Your inbox** (customer alias): a German email asking *only* for the delivery date and pallet count, with `[FC-7Q2K9D]` in the subject.

**Step 2: The customer replies, with a changed subject.**
- `uv run scripts/send_inbound.py tests/fixtures/emails/de/02_reply_changed_subject.eml --order FC-7Q2K9D`
  (the script injects the correct `In-Reply-To` header from the SES Message-ID stored in DynamoDB)
- Or use the dashboard → **Simulate customer reply** → paste the text → Send.
- Look at:
  - **Step Functions graph**: AskCustomer turns green, then Extract runs again, then Plan, then the Parallel quotes.
  - **DynamoDB**: `EXTRACTION#2`. Fields answered earlier keep their `sources` from email 1, and the new ones point to email 2.

**Step 3: Planning and quotes.**
- **CloudWatch → Log groups** → `/aws/lambda/freight-plan` → latest stream. A structured log shows the decision, e.g. "18 US pallets of bottles, 16,175 kg, not stackable → 20' GP needs 2 boxes (10 floor slots) → 1×40' GP selected, floor-bound (volume about 51%); air: 3×PMC, billed on actual weight".
- The execution pauses at **RepReview**, because this is the customer's first order.

**Step 4: The rep reviews.**
- Open `DashboardUrl` → sign in → **Review queue** → open the order. The extracted fields show their **highlighted source quotes**, the rule results, and the 3 options.
- Click **Approve & send**. The execution continues and the quote email arrives in the customer inbox.

**Step 5: The customer picks an option.**
- In the quote email, click **"Balanced: Ocean 40' GP, ~24 days, $X"** → the confirmation page → **Confirm**.
- **Step Functions**: CustomerChoice → Revalidate (a live re-quote) → Book ocean → Book truck → **Succeeded**.
- **DynamoDB**: `BOOKING#ocean` and `BOOKING#truck`, each with its idempotency key; `META.state = COMPLETED`.
- The confirmation email arrives.

**Step 6: The evidence.**
- **CloudWatch → Dashboards → Freight**: time-to-first-quote in minutes, and LLM cost for this order (≈ $0.02).
- Optional, **CloudWatch → Logs Insights**: choose the `/aws/lambda/freight-*` groups and run
  ```
  fields @timestamp, order_id, message, state_from, state_to
  | filter order_id = "FC-7Q2K9D" | sort @timestamp asc
  ```
- **X-Ray** (CloudWatch → **X-Ray traces → Trace map**): the call graph across Lambda, Bedrock, DynamoDB, and SES.

### E3. Chaos demo: the follow-up question, live
```bash
uv run scripts/chaos_race.py --order <a new order ID at QUOTE_SENT>
```
The script:
1. Sets **Parameter Store** `/freight/demo/chaos/price-drift-pct` to `12`. You can also do this in the console: open the parameter → **Edit** → value → **Save changes**.
2. Re-uploads the *same* `.eml` twice concurrently. That's the duplicate SES event.
3. Clicks the customer's choice link after the price moved.

Then show:
- **Step Functions**: still **one** execution for the order. The duplicate start was rejected because the execution name already existed.
- **DynamoDB** `freight-idempotency`: one record for the dedupe key. `freight`: one `QUOTE#1`, one `OUTBOX#quote…` with `SENT`, an `EVENT#…` row for `PRICE_CHANGED_REVALIDATION_FAILED`, and then `QUOTE#2` with the new price sent to the customer.
- **Your inbox**: exactly one original quote, plus one "price updated" email. No duplicates.
- Reset: `uv run scripts/chaos_race.py --reset`, or set the parameter back to `0`.

### E4. Other switches (Parameter Store, `/freight/demo/…`)
| Parameter | Effect |
|---|---|
| `llm-enabled` = `false` | **Kill switch**: new orders go to the NEEDS_HUMAN queue with no Bedrock calls |
| `chaos/provider-failure-rate` = `0.5` | 50% of mock provider calls fail, which shows retries, the circuit breaker, and degradation to fewer options |
| `chaos/provider-latency-ms` = `3000` | Slow provider, which shows timeouts |
| `email-mode` = `simulator` | All outbound mail goes to `success@simulator.amazonses.com` (no inbox noise) |

---

## Part F: Between sessions and monitoring cost
- Leaving the stacks deployed costs roughly $0 per day while idle. The only steady costs are custom metrics and alarms, about $3–5 per month in total.
- **Cost Explorer** → filter **Tag: project = freight-demo**. To make that work, first go to **Billing → Cost allocation tags** → find `project` → **Activate**. It only appears after resources exist, and data shows up after about 24h.
- **Billing → Bills**: check charges by service. Expect Bedrock and CloudWatch to be the top two.
- If you get a Budget alert, set `llm-enabled=false` first, then investigate in Cost Explorer (Group by: Service, then Usage type).

---

## Part G: Teardown (about 10 minutes)
```bash
cd infra && uv run cdk destroy --all       # answers y per stack
```
Then confirm in the console:
1. **CloudFormation → Stacks**: no `Freight*` stacks (filter "Deleted" to see them gone).
2. **S3**: no `freight-*` buckets. Demo buckets use `autoDeleteObjects`. If one remains, **Empty** it, then **Delete** it.
3. **DynamoDB**: no `freight*` tables.
4. **CloudWatch → Log groups**: delete any leftover `/aws/lambda/freight-*`. These appear if a function logged before its log group was managed by CDK.
5. **Systems Manager → Parameter Store**: delete `/freight/demo/link-hmac-key`. It was created manually, so CDK won't remove it.
6. **SES → Identities**: optionally delete the 3 verified addresses.
7. **Bedrock**: nothing to delete. Model access costs nothing.
8. **Optional:** delete the **CDKToolkit** stack. Empty its `cdk-…-assets-…` bucket first. Keep it if you'll redeploy.
9. **Keep the Budget**: it's free (the first 2 budgets are free).
10. The next day, check **Billing → Bills**: no new charges accruing.

---

## Troubleshooting

| Symptom | Cause → fix |
|---|---|
| `AccessDeniedException … model` from Bedrock | Model access isn't granted, or the wrong region or model ID → redo B1, and check the `bedrockModelId` context matches the console ID exactly |
| `ValidationException: Invocation of model ID … with on-demand throughput isn't supported` | Use the **inference profile** ID (`us.anthropic…`) instead of the bare model ID |
| `MessageRejected: Email address is not verified` | Sandbox: the recipient isn't verified → verify it in B2, or set `email-mode=simulator` |
| Emails never arrive | Check spam. **SES → Account dashboard** sending stats. The `freight-send-outbox` logs. The `OUTBOX#` item status |
| `cdk deploy`: `This stack uses assets… bootstrap` | Run C2 for this account and region |
| `cdk deploy` hangs at bundling / Docker error | Start Docker Desktop. On Apple Silicon, arm64 bundling is native |
| `ExpiredToken` / `The SSO session … has expired` | `aws sso login --profile freight` |
| Execution stuck at a "waiting for callback" state | Expected: it's waiting for the customer or rep. Check `META.currentTaskToken`. Late replies are logged as `LATE_REPLY` events |
| Messages in `freight-ingest-dlq` | Open **SQS → queue → Send and receive messages → Poll for messages** to see the body. After fixing, **Start DLQ redrive** |
| Step Functions execution **Failed** | Open the execution → the red state → **Error/Cause**. After fixing, use **Redrive** on the execution page |
| Dashboard login loop | Cognito app client callback URL must equal `DashboardUrl` exactly (trailing slash matters) |

---

## Appendix 1: Real inbound email with your own domain (optional, after the demo)
This takes the system from "simulated inbound" to real SES receiving with **no code change**: only CDK context `-c inboundMode=ses -c domain=example.com`.

1. Get a domain. Use **Route 53 → Registered domains → Register** (about $3–15 per year), or bring one from another registrar.
2. **SES → Identities → Create identity → Domain** → `example.com` → **Easy DKIM**, RSA 2048 → **Create**. Add the 3 DKIM CNAME records it shows. If the domain is on Route 53, click **Publish DNS records to Route 53**. Wait for the status to become **Verified**.
3. Add an **MX record** on the domain or a subdomain (recommended: `orders.example.com`) with the value `10 inbound-smtp.us-east-1.amazonaws.com`.
4. Redeploy with `inboundMode=ses`. CDK then creates:
   - an **SES receipt rule set** (`freight-inbound`), set as active, with a rule for recipient `orders.example.com`, action **S3** → bucket `freight-raw-…`, prefix `inbound/`.
   - a bucket policy allowing `ses.amazonaws.com` to `PutObject` (scoped to your account ID).
   You can see it in **SES → Configuration → Email receiving → Rule sets**. Check **Active**.
5. Optional: add a DMARC TXT record `_dmarc.example.com "v=DMARC1; p=none;"` and switch the sender to `quotes@example.com` to fix the Gmail "via amazonses" and spam issue.
6. Optional: request SES **production access** (**Account dashboard → Request production access**) only if you'll email unverified addresses. Not needed for the demo.
7. Plus-addressing (`orders+FC7Q2K9D@orders.example.com`) now works as an extra threading signal (`PLAN.md` §2.1).

## Appendix 2: What runs where (quick mental model)
```
You (or SES) ──.eml──► S3 raw/inbound ──event──► SQS ──► λ ingest ──► Step Functions (1 per order)
                                                                     │  λ extract ──► Bedrock (Haiku 4.5)
                                                                     │  λ plan / quote ──► mock providers (API GW + λ)
Dashboard (Cognito) ──► API GW ──► λ rep-api ──SendTaskSuccess──────►│  wait for rep
Customer link ──────► API GW ──► λ choose ──SendTaskSuccess─────────►│  wait for customer
                                                                     │  λ book (idempotency key)
DynamoDB (state, audit, outbox, cache, idempotency) ◄── every λ    SES (outbound via outbox λ) ──► your inbox
```
