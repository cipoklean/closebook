#!/usr/bin/env python
"""PROVE the two instances cannot contaminate each other.

Both instances share ONE Airwallex sandbox account, so each one sees the
other's deposits in `GET /deposits` and receives the other's transfer events.
This script demonstrates, against the LIVE pair, that:

  1. a deposit created by the tester's instance is refused by the operator's —
     even when the operator's instance is forced to go and look at the shared
     account, which is the dangerous path (a poll is exactly how it would have
     arrived); and
  2. a transfer webhook for a payment the manipulator never issued raises no
     incident.

In both cases the refusal is RECORDED (an `ignored_event` row), because a
decision with no record on it is not auditable.

It creates exactly one sandbox deposit and never writes to either book except
through the apps' own routes.

    python scripts/prove_isolation.py
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REAL = "http://127.0.0.1:8000"          # the operator's instance
TEST = "http://127.0.0.1:8020"          # the automated tester's instance
REAL_DB = ROOT / "data" / "closebook.sqlite3"
TEST_DB = ROOT / "data" / "testsprite.sqlite3"


# --------------------------------------------------------------------- helpers
def get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=60) as r:
        return json.loads(r.read().decode())


def post(base: str, path: str, data: dict):
    req = urllib.request.Request(
        base + path, data=urllib.parse.urlencode(data).encode(),
        headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read().decode()
            try:
                return r.status, json.loads(body)
            except ValueError:
                return r.status, body[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def snap(base: str, db: Path) -> dict:
    """Everything that would change if foreign money got in."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cur = con.cursor()

        def one(sql: str) -> int:
            try:
                return cur.execute(sql).fetchone()[0] or 0
            except sqlite3.OperationalError:
                return 0

        out = {
            "inflows": one("SELECT COUNT(*) FROM inflow"),
            "inflow_total": one("SELECT COALESCE(SUM(amount_minor),0) FROM inflow") / 100.0,
            "matches": one("SELECT COUNT(*) FROM match"),
            "applied": one("SELECT COALESCE(SUM(amount_applied_minor),0) FROM match") / 100.0,
            "receivables": one("SELECT COUNT(*) FROM invoice_receivable"),
            "payables": one("SELECT COUNT(*) FROM invoice_payable"),
            "ignored": one("SELECT COUNT(*) FROM ignored_event"),
        }
        out["suspense"] = out["inflow_total"] - out["applied"]
        try:
            out["ignored_recent"] = [
                {"kind": r[0], "external_id": r[1], "instance": r[2], "detail": r[3]}
                for r in cur.execute("SELECT kind, external_id, instance, detail "
                                     "FROM ignored_event ORDER BY id DESC LIMIT 3")]
        except sqlite3.OperationalError:
            out["ignored_recent"] = []
    finally:
        con.close()
    out["incidents"] = len(get(base, "/incidents"))
    return out


def show(tag: str, s: dict, before: dict | None = None) -> None:
    print(f"  {tag}")
    for k in ("inflows", "inflow_total", "matches", "applied", "suspense",
              "receivables", "payables", "incidents", "ignored"):
        v = s[k]
        if before is None:
            print(f"     {k:14s} {v:>12,.2f}" if isinstance(v, float)
                  else f"     {k:14s} {v:>12}")
        else:
            delta = v - before[k]
            mark = "" if delta == 0 else "   <-- CHANGED"
            print(f"     {k:14s} {v:>12,.2f}{mark}" if isinstance(v, float)
                  else f"     {k:14s} {v:>12}{mark}")


def _webhook_secret() -> tuple[str, bool]:
    """Mirror scripts/probe_webhook_recovery.py.

    With AWX_WEBHOOK_SECRET unset, sign with a THROWAWAY secret sent in the
    sandbox test-event header, so signature verification is genuinely exercised
    rather than skipped.
    """
    m = re.search(r"^AWX_WEBHOOK_SECRET=(.*)$", (ROOT / ".env").read_text(), re.M)
    configured = (m.group(1).strip() if m else "")
    if configured:
        return configured, False
    return "probe-" + uuid.uuid4().hex, True


def post_event(payload: dict) -> int:
    secret, via_header = _webhook_secret()
    raw = json.dumps(payload).encode()
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "x-timestamp": ts,
        "x-signature": hmac.new(secret.encode(), ts.encode() + raw,
                                hashlib.sha256).hexdigest(),
    }
    if via_header:
        headers["client-secret-key"] = secret
    req = urllib.request.Request(REAL + "/webhooks/airwallex", data=raw,
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


# ----------------------------------------------------------------- the deposit
def prove_deposits() -> bool:
    print("=" * 78)
    print("PART 1 — a deposit the TESTER created cannot reach the operator's book")
    print("=" * 78)
    real_before = snap(REAL, REAL_DB)
    test_before = snap(TEST, TEST_DB)
    show("the OPERATOR's instance  (:8000, data/closebook.sqlite3)", real_before)
    print()
    show("the TestSprite instance  (:8020, data/testsprite.sqlite3)", test_before)

    print()
    print("STEP 1 — simulate a deposit ON THE TESTER'S instance, via its own route")
    status, body = post(TEST, "/money-in/simulate", {
        "amount": "4400.00", "currency": "USD",
        "payer_name": "Apex Factors Ltd",
        "reference": "for the lumen stores account, ref 5522",
    })
    print(f"  POST /money-in/simulate -> HTTP {status}")
    if isinstance(body, dict):
        print(f"  {str(body.get('ok') or body.get('err'))[:250]}")

    test_after = snap(TEST, TEST_DB)
    print()
    show("the tester's instance after its own deposit", test_after, test_before)
    recorded = test_after["inflows"] == test_before["inflows"] + 1
    print(f"  {'PASS' if recorded else 'FAIL'}  the tester recorded its OWN deposit "
          f"(otherwise the rule refuses everything)")

    print()
    print("STEP 2 — force the OPERATOR's instance to look at the SHARED account")
    print("  This is the dangerous path: GET /deposits returns every deposit in")
    print("  the account to whoever asks, the tester's included.")
    status, body = post(REAL, "/money-in/poll", {})
    print(f"  POST /money-in/poll -> HTTP {status}")
    print(f"  {json.dumps(body)[:240]}")

    real_after = snap(REAL, REAL_DB)
    print()
    show("the OPERATOR's instance after being made to look", real_after, real_before)

    print()
    ok = recorded
    for label in ("inflows", "inflow_total", "suspense", "matches", "applied",
                  "incidents", "receivables", "payables"):
        same = real_before[label] == real_after[label]
        ok = ok and same
        print(f"  {'PASS' if same else 'FAIL'}  operator's {label}: "
              f"{real_before[label]} -> {real_after[label]}")
    grew = real_after["ignored"] > real_before["ignored"]
    ok = ok and grew
    print(f"  {'PASS' if grew else 'FAIL'}  refusal RECORDED, not dropped: "
          f"ignored {real_before['ignored']} -> {real_after['ignored']}")
    for r in real_after["ignored_recent"]:
        if r["kind"] == "deposit":
            print(f"        {r['kind']:9s} {r['external_id'][:24]:26s} {r['detail'][:52]}")
    return ok


# ---------------------------------------------------------------- the transfer
def prove_transfers() -> bool:
    print()
    print("=" * 78)
    print("PART 2 — another instance's TRANSFER event raises no incident")
    print("=" * 78)
    before_inc = len(get(REAL, "/incidents"))
    before_ign = snap(REAL, REAL_DB)["ignored"]
    foreign = "req-issued-by-the-TEST-instance-" + uuid.uuid4().hex[:8]
    print(f"  a request_id this instance never minted: {foreign}")
    print(f"  incidents before: {before_inc}    ignored rows before: {before_ign}")

    # A `data`-nested payload exactly as Airwallex documents it: the `data`
    # object IS the `GET /transfers/{id}` body.
    status = post_event({
        "id": "evt-foreign-" + uuid.uuid4().hex[:8],
        "name": "payout.transfer.failed",
        "created_at": "2026-10-22T12:00:00+0000",
        "data": {
            "id": "tr-from-the-tester",
            "status": "CANCELLED",
            "request_id": foreign,
            "failure_type": "INVALID_BENEFICIARY",
            "transfer_currency": "USD",
            "transfer_amount": 1234.00,
        },
    })
    print(f"  POST /webhooks/airwallex -> HTTP {status}")

    # The receiver answers 200 FAST on purpose and decides in a background
    # task, so give it a moment before reading the outcome.
    after = before_ign
    for _ in range(20):
        time.sleep(0.5)
        after = snap(REAL, REAL_DB)["ignored"]
        if after > before_ign:
            break

    inc_after = len(get(REAL, "/incidents"))
    print()
    ok = inc_after == before_inc
    print(f"  {'PASS' if ok else 'FAIL'}  no incident raised: {before_inc} -> {inc_after}")
    grew = after > before_ign
    ok = ok and grew
    print(f"  {'PASS' if grew else 'FAIL'}  and it was recorded: {before_ign} -> {after}")
    for r in snap(REAL, REAL_DB)["ignored_recent"]:
        if r["kind"] == "transfer":
            print(f"        {r['kind']:9s} {r['external_id'][:34]:36s} {r['detail'][:50]}")
    return ok


def main() -> int:
    print("Two instances, ONE Airwallex sandbox account.")
    print("Neither may act on what the other created.\n")
    ok = prove_deposits()
    ok = prove_transfers() and ok
    print()
    print("=" * 78)
    print(f"RESULT: {'PASS' if ok else 'FAIL'}")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
