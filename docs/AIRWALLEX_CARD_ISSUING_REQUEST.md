# Draft: request to enable commercial card issuing on the sandbox

Send to **devhelp@airwallex.com**. Replace the two placeholders before sending.

---

**Subject:** Please enable commercial card issuing on my sandbox account

Hello,

I am building against the Airwallex sandbox for the Agentic Banking Hackathon
and would like to request that commercial card issuing be enabled on my sandbox
account.

Cardholder creation works — `POST /api/v1/issuing/cardholders/create` with
`type: DELEGATE` returns `status: READY`.

Card creation does not. `POST /api/v1/issuing/cards/create` returns:

```
HTTP 400
{"code":"invalid_request","message":"Invalid issuance details","source":""}
```

I have tried the documented commercial-card payload:

```json
{
  "cardholder_id": "<a READY cardholder id>",
  "form_factor": "VIRTUAL",
  "issue_to": "ORGANISATION",
  "created_by": "Closebook",
  "is_personalized": true,
  "program": {"purpose": "COMMERCIAL"},
  "authorization_controls": {
    "allowed_transaction_count": "MULTIPLE",
    "transaction_limits": {
      "currency": "USD",
      "limits": [
        {"interval": "PER_TRANSACTION", "amount": "7000.00"},
        {"interval": "ALL_TIME", "amount": "70000.00"}
      ]
    }
  }
}
```

Two other endpoints on the same account return `unauthorized: Admin permissions
required`, which suggests the restriction is account-level rather than a payload
problem:

- `GET /api/v1/issuing/balances`
- `GET /api/v1/issuing/programs`

Account details:

- **Sandbox email:** `<YOUR_SANDBOX_EMAIL>`
- **Client ID:** `<YOUR_CLIENT_ID>`

Could you let me know what is needed to enable card issuing, or whether the
account needs to be created with issuing enabled from the start?

Thank you,
David
