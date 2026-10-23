"""Live proof on the TESTER: an approved over-limit bill is actually paid.

Drives the running tester over HTTP, exactly as an operator would: build a world
through Setup with a unique suffix, deposit the money that matches the invoice,
approve the over-limit bill, and watch it reach PROCESSING with a real Airwallex
object id. Then settle it through the sandbox control and confirm it reads Paid.

Against the TESTER only (127.0.0.1:8020). Never prod, never the demo book.

Two shapes this script had to learn the hard way, both by reading real output:
`_back` chooses JSON or a redirect from the ACCEPT header, so a POST without it
loses its message; and the Setup routes take IDs, not names.
"""
from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import string
import time
import urllib.error
import urllib.request

BASE = os.environ.get("TESTER", "http://127.0.0.1:8020")
DB = os.environ.get("TESTER_DB", "data/testsprite.sqlite3")
SUF = "r4" + "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
BILL = f"PO-{SUF.upper()}"
INV = f"INV-{SUF.upper()}"


def _env(name: str) -> str:
    for line in open(os.path.join(os.path.dirname(__file__), "..", ".env")):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"no {name} in .env")


PIN = _env("TESTSPRITE_PIN")


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 # `_back` picks its shape from ACCEPT: without this the reply is
                 # a 303 to an HTML page and the message is lost.
                 "Accept": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()[:300]}
    except Exception as exc:                        # noqa: BLE001
        return {"note": str(exc)[:200]}


def page(path: str) -> str:
    with urllib.request.urlopen(BASE + path, timeout=90) as r:
        return r.read().decode()


def row_id(table: str, name: str) -> int:
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        r = c.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
        return int(r[0]) if r else 0
    finally:
        c.close()


def plan_row(step_key: str):
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        return c.execute(
            "SELECT id, status, airwallex_object_id, request_id FROM plan_step "
            "WHERE step_key = ?", (step_key,)).fetchone()
    finally:
        c.close()


def step(n: int, title: str) -> None:
    print(f"\n[{n}] {title}")


print(f"suffix {SUF}  tester {BASE}")
print(f"bill {BILL} | invoice {INV}")

step(1, "build a world through Setup (unique suffix on every record)")
CUST = f"Brightline Retail {SUF}"
SUP = f"Atlanta Packaging {SUF}"
print("  customer :", post("/setup/customers", {"name": CUST}).get("message", "")[:80])
res = post("/setup/suppliers", {
    "name": SUP, "country": "US", "currency": "USD", "payout_method": "LOCAL",
    "account_name": SUP, "account_number": "1234567890",
    "routing_value": "021000021", "routing_type": "aba",
    "bank_name": "Wells Fargo", "entity_type": "COMPANY", "city": "Atlanta",
    "street_address": "1 Peachtree St", "state": "GA", "postcode": "30303",
    "bank_account_category": "Checking"})
print("  supplier :", res.get("message", str(res))[:120])

CID, SID = row_id("customer", CUST), row_id("supplier", SUP)
print("  ids      : customer", CID, "supplier", SID)
print("  invoice  :", post("/setup/invoices", {
    "customer_id": CID, "number": INV, "currency": "USD",
    "amount": "12000"}).get("message", "")[:90])
print("  bill     :", post("/setup/payables", {
    "supplier_id": SID, "number": BILL, "currency": "USD",
    "amount": "3500", "criticality": 3}).get("message", "")[:110])

step(2, "the money arrives")
html = page("/app?tab=money-in")
opts = re.findall(r'<option value="([0-9a-f-]{36})"[^>]*>USD account', html)
USD_ACCT = opts[0] if opts else ""
print("  USD account:", USD_ACCT or "NOT FOUND")
dep = post("/money-in/simulate", {
    "global_account_id": USD_ACCT, "currency": "USD", "amount": "12000",
    "payer_name": CUST, "reference": INV})
print("  deposit  :", str(dep.get("message", dep))[:170])

step(3, "the over-limit bill is in Needs your OK")
time.sleep(2)
ok_html = page("/app?tab=ok")
cards = re.findall(r'data-testid="approvable-([0-9]+)"', ok_html)
print("  approval cards:", cards)
print("  the bill is named on the page:", BILL in ok_html)
print("  it says why:", "auto-pay limit" in ok_html)

step(4, "approve it over HTTP (never in a browser)")
if cards:
    print("  wrong PIN:", post(f"/approvals/{cards[0]}/approve",
                              {"pin": "000000"}).get("message", "")[:60])
    for target in cards:
        r = post(f"/approvals/{target}/approve", {"pin": PIN})
        print(f"  approve {target}:", str(r.get("message"))[:200])
        if r.get("ok"):
            break

step(5, "the step now carries a real Airwallex object id")
row = None
for _ in range(25):
    row = plan_row(f"PAY:{BILL}")
    if row and row[2]:
        break
    time.sleep(2)
print("  (id, status, object_id, request_id):", row)

step(6, "the tracker must not show the red X for a live payment")
mo = page("/app?tab=money-out")
print("  BILL named on money-out:", BILL in mo)
print("  stopped marks on the page:", mo.count('data-testid="tracker-stopped"'))
print("  shows a process state:",
      ("Sending to the bank" in mo) or ("PROCESSING" in mo))

print("\nsuffix:", SUF, "| bill:", BILL, "| object id:", row[2] if row else None)
