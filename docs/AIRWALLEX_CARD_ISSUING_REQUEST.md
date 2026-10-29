# Card issuing on the sandbox: status, evidence, and the open question

**Status: still blocked. Dev Help's diagnosis was applied and did not resolve
it, and their stated cause was disproved by direct test.**

Last updated after the Dev Help reply. Evidence is reproducible with
`scripts/probe_card_create.py` and `scripts/probe_card_fields.py`.

---

## What Dev Help told us

> The cardholder is `DELEGATE`, but `is_personalized: true` is being sent.
> Personalized cards require an `INDIVIDUAL` cardholder.

## What we changed, as instructed

`backend/app/airwallex/issuing.py`, in `create_card`:

| Field | Before | Now |
|---|---|---|
| `is_personalized` | `true` | **`false`** |
| `issue_to` | `"ORGANISATION"` | **removed entirely** |
| `program` | `{"purpose": "COMMERCIAL"}` | unchanged (already correct) |
| cardholder | `DELEGATE` | unchanged (spend controls attach to it) |
| `/issuing/programs`, `/issuing/balances` | called | **never called** |

Balances come from `GET /api/v1/balances/current` (already the case in
`airwallex/balances.py`).

## Result: unchanged

```
HTTP 400
{"code":"invalid_request","message":"Invalid issuance details","source":""}
```

Cardholder creation still succeeds (`type: DELEGATE`, `status: READY`), so it is
specific to card creation.

## Why the cardholder type is not the cause

The claim was tested directly: an `INDIVIDUAL` cardholder was created
successfully — it needs `individual.name`, then
`individual.express_consent_obtained: "yes"`, then `individual.address.country`
— and cards against it fail **identically**:

| cardholder | program | is_personalized | result |
|---|---|---|---|
| DELEGATE | CONSUMER | true | `invalid_argument` CONSUMER program is not allowed for this account |
| DELEGATE | CONSUMER | false | `invalid_argument` is_personalized must be true for consumer card |
| DELEGATE | COMMERCIAL | true | `invalid_request` Invalid issuance details |
| DELEGATE | COMMERCIAL | false | `invalid_request` Invalid issuance details |
| INDIVIDUAL | CONSUMER | true | `invalid_argument` CONSUMER program is not allowed for this account |
| INDIVIDUAL | COMMERCIAL | false | `invalid_request` Invalid issuance details |
| INDIVIDUAL | COMMERCIAL | true | `invalid_request` Invalid issuance details |

Both cardholder types fail the same way, so the variable Dev Help named does not
change the outcome.

## What the payload is not

The body is parsed field by field — removing any required field returns a
**named** error, which is why `source: ""` on the generic failure is informative:

| change | error |
|---|---|
| remove `created_by` | `invalid_argument` created_by is mandatory (`source: created_by`) |
| remove `program` | `invalid_argument` program is mandatory |
| remove `is_personalized` | `invalid_argument` is_personalized is mandatory |
| remove `authorization_controls` | `invalid_argument` authorization_controls is mandatory |
| remove `limits` | `field_required` limits field is required |
| `form_factor: PHYSICAL` | `invalid_request` PHYSICAL form factor is not supported for company card |

So the request is well-formed and reaching the issuing logic. The generic error
is not a field complaint.

## The pattern that points at the account

Every *management* endpoint on this account is gated:

- `GET /api/v1/issuing/balances` → `unauthorized: Admin permissions required`
- `GET /api/v1/issuing/programs` → `unauthorized: Admin permissions required`
- `GET /api/v1/issuing/cards/create/schema` → `unauthorized: Admin permissions
  required`

Cardholder creation is permitted; card issuance and issuing administration are
not. That is an entitlement boundary, not a payload fault.

## The open question for Airwallex

Both of these would unblock it, and only Airwallex can answer which applies:

1. Does this sandbox business need **issuing Admin permissions** (or issuing
   enabled) on the account, and can that be granted to a sandbox business?
2. Or does card issuance require a **sandbox business created with issuing
   enabled from the start**?

Please include the exact account so it can be looked up:

- **Sandbox email:** `<YOUR_SANDBOX_EMAIL>`
- **Client ID:** `<YOUR_CLIENT_ID>`

## Impact on the demo

Beat 4 (the card guardrail) is reported as **Closebook's policy verdict**, not a
live card-network decline, and is labelled as such in the beat output,
`seed/scenario.py`, and the README. No card exists, so nothing was authorised
against Airwallex and no decline was returned.

The per-transaction limit logic itself is fully unit-tested; what is missing is
a live authorisation to demonstrate it against.

---

# RESOLUTION (2026-10-29): issuing was a country problem, and it is solved

## What Airwallex confirmed

Card issuing depends on the **business country of the account**, not on the
request body. The sandbox that runs Closebook's books is registered in
**Nigeria**, which is not an issuing region. That is why every card create
returned the generic

```
[invalid_request] Invalid issuance details
```

with an **empty `source`**. The error named no field because the problem was
never a field. Every payload theory we chased (is_personalized, cardholder
type, program shape, issue_to) was a theory about the wrong layer.

## What fixed it

A second Airwallex sandbox, registered in **Hong Kong**, used for issuing only:

| | account | job |
|---|---|---|
| books | Nigeria sandbox (`AWX_CLIENT_ID` / `AWX_API_KEY`) | deposits, transfers, beneficiaries, webhooks |
| cards | Hong Kong sandbox (`AIRWALLEX_ISSUING_CLIENT_ID` / `AIRWALLEX_ISSUING_API_KEY`) | cardholders, cards, authorisations |

`airwallex/issuing.py::issuing_client()` builds the second client by copying
the settings and swapping those two credential fields, so auth, retries, rate
limiting and request ids have exactly one implementation. **With no issuing
credentials it returns `None`** and beat 4 keeps its policy-verdict caveat:
empty is a supported state, not a crash.

## Live proof (2026-10-29, HK sandbox)

```
cardholder f1f7a1dc-928d-41f9-9212-5d21d88938e5  type=DELEGATE  status=READY
card       c000fbf0-5792-4fcb-b330-cfafc0834827  VIRTUAL  status=ACTIVE
           limits USD 2,500.00 PER_TRANSACTION / USD 10,000.00 ALL_TIME
           (the per-transaction limit is the operator's auto-pay limit)

inside the limit  USD 2,500.00 -> process_result APPROVED
over the limit    USD 2,500.01 -> process_result DECLINED
                                   failure_reason LIMIT_EXCEEDED
```

That is a **real decline returned by the card**, not a staged failure. No
`transaction_failure_reason` was sent: asking Airwallex to fail a transaction
would only prove that Airwallex can be told to fail.

## Contract facts captured live (these cost attempts to find)

- `POST /issuing/cardholders/create` returns **`cardholder_id`**, not `id`.
- `POST /issuing/cards/create` returns **`card_id`** and `card_status`.
- The HK program is `{"purpose":"COMMERCIAL","type":"CREDIT","sub_type":"GOOD_FUNDS_CREDIT"}`,
  wallet-funded, **no `funding_source_id`**.
- A DELEGATE cardholder cannot hold a personalized card: `is_personalized: false`.
- Reusing a `request_id` is refused (`The provided ID is already associated with
  a previous request`) — the idempotency guard working, not a bug.
- The same email cannot create a second cardholder
  (`already associated with a cardholder`) — reuse it.
- `POST /simulation/issuing/create` takes **`transaction_amount`** and
  **`transaction_currency`** (not `amount`/`currency`), `merchant_info`
  (not `merchant_name`), and `single_phase: true` to authorise and clear at
  once. It answers with **`process_result`**: `APPROVED` or `DECLINED` plus
  `failure_reason`. It had no `transaction_type` field at all; sending one was
  a guess, and the response's `process_result` is the only reliable word.
- `card_number` **outranks** `card_id` when both are sent, so the old default
  test-card number would have authorised the wrong card. It is now omitted
  unless asked for.

## What beat 4 says now

`domain/cards.py::caveat_wanted()` keeps the caveat until a decision that is
**both live and a decline** is recorded in the workspace. A verdict produced by
our own arithmetic can never retire it, because a verdict is not evidence. On a
workspace where the probe has run, beat 4 reports the bank's amount and reason;
everywhere else it still says "policy verdict" and means it.

To record it on a workspace:

```bash
.venv/bin/python scripts/probe_issuing_hk.py <book>
```
