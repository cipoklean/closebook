"""Live proof of F1, F2, F4, F5 on the TESTER, fresh ?ws= books.

  F1  cross-customer subset-sum must NOT auto-apply (ambiguous).
  F2  /dev/* refuses without/with wrong PIN; settings refuse without PIN;
       simulated deposit capped at USD 50,000.
  F4  a recognized payer overpaying their single open invoice gets a CAPPED
       proposal with credit, and approve gives OVERPAID + credit.
  F5  the approve flash reports the applied sum (1,000) and the credit
       (200), never the deposit amount (1,200).

Setup rows are written with the app's OWN writers into the book's SQLite file
(the only bearer of the workspace); every action after that is the live web
API on the tester. The PIN is read from /etc/closebook/closebook.env and never
printed.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
BASE = os.environ.get("TESTSPRITE_BASE_URL",
                      "https://professor-apr-girls-meanwhile.trycloudflare.com")

def _pin() -> str:
    env_file = Path("/etc/closebook/closebook.env")
    try:
        text = env_file.read_text()
    except PermissionError:
        text = subprocess.run(
            ["sudo", "-n", "cat", str(env_file)],
            capture_output=True, text=True).stdout
    m = re.search(r"^\s*TESTSPRITE_PIN=(.+)$", text, re.MULTILINE)
    if m:
        return m.group(1).strip().strip("\"'")
    return os.environ.get("TESTSPRITE_PIN", "")

PIN = _pin()

def seed_book(book: str) -> None:
    """A clean book with the app's own writers."""
    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("CLOSEBOOK_WORKSPACE_ROOT", str(REPO / "data" / "workspaces"))
    from app.db import DB
    from app.domain.settings_store import save as save_settings
    from app.domain.workspace import create_customer, create_invoice

    path = REPO / "data" / "workspaces" / f"{book}.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = DB(f"sqlite:///{path}")
    with db.session_scope() as s:
        save_settings(s, budget_currency="USD", reserve_floor="0",
                      auto_pay_limit="2500", fx_tolerance_pct="0.50")
        cust = create_customer(s, name="Brightline Retail")
        create_invoice(s, customer_id=cust.id, number="INV-1001", currency="USD",
                       amount="12000.00")
    return db


def seed_customers_and_invoices(book: str, rows) -> None:
    """Seed customers + invoices straight through the app writers (returns id)."""
    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("CLOSEBOOK_WORKSPACE_ROOT", str(REPO / "data" / "workspaces"))
    from app.db import DB
    from app.domain.workspace import create_customer, create_invoice

    path = REPO / "data" / "workspaces" / f"{book}.sqlite3"
    db = DB(f"sqlite:///{path}")
    with db.session_scope() as s:
        for name, inv_number, amount_major in rows:
            cust = create_customer(s, name=name)
            create_invoice(s, customer_id=cust.id, number=inv_number,
                           currency="USD", amount=amount_major)


def curl(book: str, method: str, path: str, body=None) -> dict:
    jar = f"/tmp/cb_live_{book}.jar"
    # The ?ws= suffix sets the closebook_ws cookie; nothing else routes into
    # this book. Prime it once before the first action of the session.
    if not os.path.exists(jar):
        subprocess.run(["curl", "-s", "-m", "30", "-c", jar,
                        f"{BASE}/app?ws={urllib.parse.quote(book)}"],
                       capture_output=True)
    args = ["curl", "-s", "-m", "30", "-c", jar, "-b", jar,
            "-X", method, "-H", "Accept: application/json"]
    if body is not None:
        args += ["-H", "Content-Type: application/json",
                 "--data", json_dumps(body)]
    args.append(f"{BASE}{path}")
    out = subprocess.run(args, capture_output=True, text=True).stdout
    try:
        return json_loads(out)
    except Exception:
        return {"_raw": out[:300]}


import json  # noqa: E402
json_dumps = json.dumps
json_loads = json.loads


def assert_ok(cond, label, detail=""):
    if not cond:
        print(f"  FAIL {label} {detail}")
        sys.exit(1)
    print(f"  ok  {label}")


def db_rows(book: str, table: str):
    sys.path.insert(0, str(BACKEND))
    path = REPO / "data" / "workspaces" / f"{book}.sqlite3"
    import sqlite3
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    rows = c.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
    c.close()
    return rows


def main() -> None:
    if not PIN:
        print("no TESTSPRITE_PIN available; refusing")
        sys.exit(2)

    # ---------------- F1: cross-customer subset-sum is ambiguous ------------
    print("F1: cross-customer subset-sum must not auto-apply")
    book1 = f"f1-{int(time.time())}"
    seed_book(book1)
    # Two customers, one invoice each, USD 400 + USD 600.
    seed_customers_and_invoices(
        book1, [("Alpha", "INV-C1", "400"), ("Beta", "INV-C2", "600")])
    d = curl(book1, "POST", "/money-in/simulate",
             {"currency": "USD", "amount": "1000",
              "payer_name": "Mystery Payer", "reference": ""})
    assert_ok(not d.get("err"), "simulate deposit landed", str(d))
    time.sleep(2)  # give the poll/cycle a tick
    st = curl(book1, "GET", "/state")
    paid = [r for r in st.get("receivables", []) if r.get("paid_minor") or r.get("status") == "PAID"]
    assert_ok(not paid, "no invoice auto-applied across customers", str(paid))
    # The deposit's own answer is the authoritative one: it must have been
    # read BELOW the automatic threshold and nothing applied — not MATCHED
    # at 0.91 across the two customers' invoices.
    msg = (d.get("message") or "").lower()
    assert_ok("nothing has been applied" in msg
              and "below the automatic threshold" in msg,
              "inflow held for a human, nothing applied", msg[:300])

    # ---------------- F2: route gates + deposit cap ------------------------
    print("F2: dev/settings gates and the deposit cap")
    book2 = f"f2-{int(time.time())}"
    seed_book(book2)
    r = curl(book2, "POST", "/dev/transfers/x/transition", {"what": "settle"})
    assert_ok("PIN" in r.get("err", "") or "disabled" in r.get("err", "").lower(),
              "dev transition refuses without PIN", str(r))
    r = curl(book2, "POST", "/dev/transfers/x/transition",
             {"what": "settle", "pin": "000000"})
    assert_ok("PIN" in r.get("err", ""),
              "dev transition refuses wrong PIN", str(r))
    r = curl(book2, "POST", "/dev/transfers/x/transition",
             {"what": "settle", "pin": PIN})
    # Correct PIN: the gate lets it through to the sandbox action (which
    # refuses a bogus transfer id) — it is never refused ON the PIN.
    assert_ok("PIN is not right" not in r.get("err", ""),
              "dev transition with correct PIN passes the gate", str(r)[:200])
    r = curl(book2, "POST", "/setup/settings",
             {"budget_currency": "USD", "reserve_floor": "5000"})
    assert_ok("PIN" in r.get("err", ""), "settings refuse without PIN", str(r))
    r = curl(book2, "POST", "/money-in/simulate",
             {"currency": "USD", "amount": "60000",
              "payer_name": "X", "reference": ""})
    assert_ok("50,000" in r.get("err", ""), "deposit over 50k refused", str(r))

    # ---------------- F4 + F5: overpayer capped proposal, flash ------------
    print("F4/F5: recognized-payer overpayment is capped and the flash names it")
    book3 = f"f4-{int(time.time())}"
    seed_book(book3)
    seed_customers_and_invoices(book3, [("Lumen Stores", "INV-F1", "1000")])
    d = curl(book3, "POST", "/money-in/simulate",
             {"currency": "USD", "amount": "1200",
              "payer_name": "Lumen Stores", "reference": "INV-F1"})
    assert_ok(not d.get("err"), "F4 deposit landed", str(d))
    time.sleep(2)
    inflows = db_rows(book3, "inflow")
    inflow = next((i for i in inflows if i["amount_minor"] == 120_000), None)
    assert_ok(inflow is not None, "found the F4 inflow in the book",
              f"inflow count={len(inflows)}")
    # The proposal must be CAPPED: it allocates 1,000 (the open balance), not
    # the full 1,200 deposit, and records 200 as credit.
    prop = json_loads(inflow["proposed_json"])
    allocs = prop.get("allocations", [])
    alloc_sum = sum(a["amount_minor"] for a in allocs)
    assert_ok(alloc_sum == 100_000 and prop.get("credit_minor") == 20_000,
              "proposal capped at the open balance with credit",
              json_dumps(prop)[:300])
    # And the invoice itself has not moved yet (application happens on approve).
    invs = db_rows(book3, "invoice_receivable")
    inv_before = next(r for r in invs if r["number"] == "INV-F1")
    assert_ok(inv_before["amount_paid_minor"] == 0,
              "invoice untouched before approval", str(dict(inv_before)))
    fid = inflow["id"]
    a = curl(book3, "POST", f"/inflows/{fid}/approve", {"pin": PIN})
    msg = a.get("message") or a.get("ok") or json_dumps(a)
    if not isinstance(msg, str):
        msg = json_dumps(a)
    assert_ok("1,000.00 has been applied" in msg,
              "approve flash reports the applied sum", str(msg)[:300])
    assert_ok("200.00 held as credit" in msg,
              "approve flash names the credit", str(msg)[:300])
    assert_ok("1,200.00 has been applied" not in msg,
              "approve flash does not quote the deposit", str(msg)[:300])
    invs = db_rows(book3, "invoice_receivable")
    inv_after = next(r for r in invs if r["number"] == "INV-F1")
    assert_ok(inv_after["status"] == "OVERPAID",
              "invoice reads OVERPAID", str(dict(inv_after)))

    print("\nRESULT: PASS (F1, F2, F4, F5 verified live)")


if __name__ == "__main__":
    main()
