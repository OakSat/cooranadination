# System Design Practice: AI Freight Coordination

Oct 8, 2026 · @Alon

## Background

Corona, based in Mexico, sells beer to bars and business customers around the world. A bar in Germany wants to order a large shipment of beer, but freight coordination is currently handled through a manual email chain.

When an order request arrives, a representative in Mexico has to work out from the email:

- What quantity is requested?
- What are the cargo dimensions and weight?
- Is it bottles, cans, kegs, or pallets?
- What is the requested delivery date?
- What are the pickup and delivery addresses?
- Is ocean or air freight required?
- Does the customer prefer the lowest price or the fastest delivery?
- Is temperature control required?
- Who is responsible for customs, insurance, and local transport?
- What are the Incoterms?

If details are missing, the representative sends questions back to the bar. Because of the time difference, each round of questions and answers can add a full day to the process.

Once the details are in, the representative must:

1. Check whether ocean or air freight suits the order.
2. Choose how to consolidate the cargo and which container type to use.
3. Check sailing or flight schedules and capacity availability.
4. Get price quotes from freight forwarders.
5. Offer the customer several alternatives.
6. Get the customer's approval.
7. Book the international shipment.
8. Arrange a truck from the port or airport to the bar.

The whole process currently takes about five to seven business days. The company wants to cut it down to a few hours using an LLM and automation, without letting the model make risky decisions or place a wrong booking.

## The task

Design a backend system based on AWS and an LLM that receives the email thread, extracts the shipment details from it, identifies missing information, communicates with the customer, and generates suitable shipping alternatives.

The system must support the process flow below.

&#91;embedded content: request-to-booking flow · 6 steps, 1 decision\]

If any detail is missing, the system asks the customer and re-runs extraction on the reply; only a complete request moves on to alternatives, approval and booking.

## Functional requirements

The system must:

- Receive and send emails using Amazon SES. SES can store incoming emails in S3 and trigger a Lambda when they arrive (ref: AWS SES).
- Associate each email with the correct order and thread, even when the subject line changes or the customer replies without a clear identifier.
- Store the original email, the attachments, the extraction results, and the change history.
- Use an LLM to extract information from free text in German, Spanish, or English.
- Convert the model's output into a strict Pydantic model, including validation and JSON Schema.
- Distinguish between:
  - Information provided explicitly.
  - Information inferred by the model.
  - Missing information.
  - Conflicting information.
- Automatically draft and send targeted questions for the missing fields.
- Avoid asking again if the information already appeared in a previous email.
- Get prices, availability, transit times, and departure dates from external freight providers.
- Return up to three alternatives to the customer, for example:
  - The cheapest.
  - The fastest.
  - The balanced option.
- Allow a human representative to review and approve a quote before it is sent, in sensitive cases.
- Also coordinate the last mile by truck in Germany.
- Track the entire process as a state machine through to the final booking.

## Ocean freight logic

The system must decide between:

| Option | Meaning |
| --- | --- |
| LCL | Less than Container Load: partial cargo that shares a container with other shipments. |
| FCL | Full Container Load: a full container dedicated to the shipment. |
| 20' GP | A 20-foot general-purpose container. |
| 40' GP | A 40-foot general-purpose container. |
| 40' HC | A 40-foot high-cube container, for when cargo volume is the main constraint. |
| Reefer | A refrigerated container, if there is a temperature requirement that justifies it. |

Container selection must be based on volume, weight, number of pallets, loading constraints, cost, availability, cargo sensitivity, and whether LCL or FCL is preferable.

Do not assume that free volume necessarily means more cargo can be added: the container may hit its weight limit before it is full by volume.

## Air freight logic

The system must support:

- A direct flight vs. a route with a connection.
- Express service vs. standard or consolidated service.
- Passenger aircraft vs. freighter aircraft, depending on dimensions and weight.
- Calculation by actual weight vs. volumetric (dimensional) weight.
- Fit to a ULD of the container or pallet type.

A ULD (Unit Load Device) is an air cargo loading unit. It can be a dedicated container or a pallet with a net, and it locks directly into the aircraft's loading system (ref: IATA – Aircraft ULD).

## Business and safety constraints

- The LLM is not the source of truth for prices, availability, weight limits, or container fit.
- Deterministic calculations are done in code or with a rules engine.
- Every value the model extracts must include a confidence score and its source within the email.
- A conflict in date, quantity, address, or shipping terms requires clarification.
- No shipment may be booked without explicit approval.
- Support for customs, export and import documents, insurance, alcohol restrictions, and Incoterms. *(Out of scope for now.)*
- The system must handle duplicate emails, repeated Lambda events, and late-arriving replies.
- Operations must be idempotent; AWS recommends designing Lambda so that duplicate events do not cause additional side effects (ref: AWS Lambda Best Practices).

## Non-functional requirements

- Thousands of shipment requests per day.
- High availability and retry capability.
- A full audit trail for every decision and change.
- Encryption of data in transit and at rest.
- Protection against prompt injection inside an email or attachment.
- Separation between different customers' data.
- Monitoring of latency, failures, LLM costs, and extraction quality.
- The ability to swap the LLM provider.
- Resilience to temporary failure of a freight provider, SES, or the model.
- Prevention of duplicate emails and duplicate bookings.

## Topics to address in your design

1. Which AWS services would you choose, and what is each one's role?
2. How does an email travel from SES through Lambda to the point where the Shipment is saved?
3. Is Lambda enough, or do you need Step Functions, SQS, or EventBridge?
4. How would you design the data model and the state machine?
5. How would you use Pydantic to validate untrusted LLM output?
6. How would you store the source and confidence of each field?
7. How would you prevent double processing and duplicate replies?
8. What would you put in the cache?
   - The catalog of ports and airports.
   - Container types and ULDs.
   - Estimated transit times.
   - Short-lived provider results.
   - LLM results for the same content.
9. Which cache would you choose: Lambda memory, DynamoDB, ElastiCache, or combined layers?
10. How would you handle cache invalidation when price or availability changes?
11. How would you integrate human-in-the-loop?
12. How would you test the system without sending real bookings and emails?
13. What happens when the LLM returns JSON that is syntactically valid but wrong from a business standpoint?
14. How would you measure whether the system really cut the process from a week to a few hours?

## Follow-up question for the candidate

The system sent the customer a quote for ocean freight in a 40' HC. A few minutes later, the same SES event arrived again, and at the same time the price changed at the freight provider. Two different Lambdas started processing the same order.

How would you prevent:

- Creating two different quotes?
- Sending two emails?
- Saving a price that is no longer valid?
- A duplicate booking after the customer approves?
- Losing the response if one of the functions crashes midway?

This is the key deep-dive point of the question: not just "where do we put the LLM," but how to build a reliable workflow in which the LLM handles language while the deterministic system guarantees the correctness of the process.
