# Engineering notes

The decisions and gotchas behind Closebook. This is the short version — the
reasoning, not the chronology. It contains no secrets, no API keys and no
credential values of any kind.

---

## The core principle

**The model proposes; deterministic code enforces.** Amounts, limits, approvals,
the reserve floor, idempotency and duplicate protection live in code. A model may
read a messy bank reference and explain a decision; it cannot authorise a
payment, choose an amount, or mark money as matched.

---

## Key decisions

**The agent has no payment tool.** It is given five tools
(`list_open_invoices`, `propose_match`, `explain_decision`, `request_approval`,
`read_plan`). There is deliberately no `pay_invoice`, `create_transfer` or
`convert_fx`. A model that cannot express a payment cannot make one — stronger
than a policy check that could be mis-ordered or bypassed. Constructing an agent
with a payment tool raises.

**Plan-step identity is `(payable, action)`, never the payable alone.** One
payable legitimately has both an FX step and a transfer step. Keying on the
payable alone made the transfer step adopt the *conversion's* object id, report
`ALREADY_DONE`, and never pay the supplier while the run looked successful. Three
sites carried the bug; only running a real end-to-end payout found it.

**A plan step is an identity, not a log line.** Appending one per run defeats the
idempotency key entirely — a payment you can ask for twice is not protected by an
idempotency key, because the key only helps if the same key is genuinely reused.

**A `SUBMITTED` step is unresolved, not done.** After a crash the only correct
action is a `request_id` lookup on the provider. Refusing it as "not submittable"
would strand every payment in flight when the process died. This shipped as a bug
and only a crash-recovery acceptance test caught it; every unit test stayed green.

**A failure webhook is a claim, not proof.** `payout.transfer.failed` triggers a
lookup. If the transfer is still `PROCESSING`, Closebook watches it and creates
nothing — a replacement could double-pay a transfer that has not settled. This
was caught by a live probe, not by tests.

**Confidence is enforced where allocations are written, not where they're
scored.** The matcher correctly scored a fuzzy match below the auto threshold, and
the ledger applied the allocations anyway — creating a refund obligation from a
guess. Scoring low does not stop a caller from applying the result.

**Capacity is not liquidity.** The Airwallex sandbox pre-funds 10M per currency,
so policy written against the raw balance never binds. Closebook tracks its own
committed cash in `settings/committed_cash.json` and treats the sandbox balance
as the ceiling of what it *could* move.

**One rate convention for the whole codebase:** `rates[CCY] = CCY per 1 USD`, so
USD value is `amount / rate`. Two modules had each been internally consistent
while being inverses of each other, valuing the same payout 30% apart. Every
isolated test passed.

**Approvals bind terms, not prose.** Every term an approver agrees to folds into
a SHA-256 hash, HMAC-signed separately so a leaked hash alone cannot forge one.
Re-checked at execution, including on retries, because a retry is when nobody
re-reads the numbers.

**An empty secret is a closed door, not an open one.** With
`AWX_WEBHOOK_SECRET` unset, every webhook is rejected. With `DEMO_APPROVER_PIN`
unset, the approval endpoint is closed. The secure state is the default state.

**Offline is a supported mode, not a degraded one.** With no LLM key, matching
runs fully deterministically and explanations return the deterministic reason
verbatim. The demo has never had a paid LLM dependency.

**The webhook secret is the only HMAC key.** Honouring the request's own
`client-secret-key` header as the key was an authentication bypass: an attacker
could supply both the key and a matching signature. It is now honoured only with
an explicit `AWX_ALLOW_TEST_EVENTS=true` **and** a constant-time match against
the configured secret.

**Claim, don't read.** Webhook processing claims each event with a conditional
`UPDATE ... WHERE processed_at IS NULL`. Reading then marking is check-then-act:
two background tasks both pass the check, and a duplicate `transfer.failed` is a
duplicate replacement payment.

---

## Gotchas

### Money and APIs

- `Decimal.normalize()` gives `1E+2` scientific notation, which JSON parsers
  reject. Use fixed-point formatting for API amounts.
- `Decimal("1.10")` and `Decimal("1.1")` hash differently as strings, voiding a
  valid approval over trailing zeros. Normalise before hashing.
- Never key a lookup on `LIKE` over a JSON/text column — `%` and `_` are
  wildcards. Parse and compare values.
- An FX step's traded amount is `sell_amount_minor`; a payment's is
  `amount_minor`. Reading one field for both renders every conversion as 0.00.
- `x-timestamp` is a unix epoch for signature freshness; `created_at` is ISO for
  ordering. They are different fields with different formats.
- Airwallex error messages name the field you did *not* send. Read the details
  array, not the top-level message.
- `transfers/create` needs both `source_currency` and `transfer_currency`, plus a
  non-empty `reason`.
- `issuing/cards/create` needs `program` as an **object** with
  `purpose: COMMERCIAL` (a business account rejects `CONSUMER`), plus
  `issue_to: ORGANISATION`, `form_factor`, `is_personalized` and `created_by`.
  Do **not** send `primary_contact_details` — the docs still show it, but the
  API rejects it as unsupported in favour of `cardholder_id`.
- **Commercial card issuance is blocked for this sandbox account.** Cardholder
  creation returns `READY` fine, but card creation fails with
  `invalid_request: Invalid issuance details`, and `/issuing/balances` and
  `/issuing/programs` return "Admin permissions required" — an account-level
  entitlement, not a payload bug. Beat 4 therefore reports Closebook's policy
  verdict and says so, rather than claiming a card-network decline that never
  happened. The docs MCP example for a business card is stale; live errors beat
  documentation every time.

### Python and SQLAlchemy

- `datetime.utcnow()` is deprecated in 3.12+. Moving to `now(timezone.utc)`
  naively breaks every DB comparison, because SQLite stores naive values and
  mixing aware with naive raises. `app/timeutils.py` returns naive UTC derived
  from the aware call — one place to change if storage ever becomes aware.
- A relative import inside a function fails when the module is imported as a
  top-level script. Put imports at module level.
- A dict containing only an id is truthy — `if not payload` will not filter
  legacy rows. Test for the field you actually need.
- Each `db.session()` returns a different identity map, so mutating through a
  handle from another session silently does nothing. Use one session, or a
  conditional `UPDATE`.
- A session opened without `with` leaks a connection per request. Only shows up
  under load.

### Testing

- **Fixtures that stand in for persisted state must call the real writer.** Every
  fixture here hand-wrote the payload shape that was wrong in production, which
  is exactly why 161 tests missed the identity bug above. Hand-rolled fixtures
  agree with each other and disagree with reality.
- **A test can encode the bug it describes.** One asserted two rows for one
  payable — which *was* the duplicate-payout defect. When behaviour is wrong, the
  test protecting it is wrong too.
- **Assert on persisted state, not on the component that produced the
  proposal.** A beat that checked the matcher's verdict instead of the recorded
  `match_status` reported a false bug and sent a hunt for a defect the ledger had
  already prevented.
- **A safety guard must return, not just print.** One correctly refused a
  duplicate payout, then fell through to `rows[0]` — empty *precisely because* the
  payout already existed — and exited with `IndexError`. The correct behaviour was
  producing a crash.
- A state with no colour styling is an invisible warning. Test for the styled
  class, not just the string.
- Grepping source for `os.environ` also matches docstrings explaining why we
  avoid it. Walk the AST instead.

### Process

- `git add -A` will commit a `.env` backup. Back secrets up outside the repo and
  check `git status` before staging. If a secret lands in history, rewriting it
  removes the file but the credential must still be **rotated**.
- A stale server on the port serves old code and makes every live check lie.
  Check what is listening before trusting a result.
- Writing a file is not committing it. The only honest test of the user's path is
  a fresh clone: `make setup && make test && make seed`.
- A demo that is not idempotent corrupts its own arithmetic. Verify by running it
  twice and diffing the balances.

---

## Sandbox limitations found by probing

- Login returns `token`, not `access_token`; global-account lists are under
  `items`; balances are a bare list keyed `available_amount`.
- FX quotes require a `validity` enum (`HR_1`), not a duration string.
- Beneficiary `entity_type` is `PERSONAL` or `COMPANY`; `validate()` and
  `create()` take different shapes.
- **Virtual card issuance is blocked for this account** ("CONSUMER program is not
  allowed"). Beat 4 therefore reports Closebook's policy verdict and says so,
  rather than claiming a card-network decline that never happened.
