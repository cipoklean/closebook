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
