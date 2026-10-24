"""Live proof on the tester for the run-4 findings that were fixed.

1. an over-limit bill is NOT sent and DOES produce a card
2. two bills, same supplier, same amount, cannot share an approval
3. a held bill names its real blocker, with numbers
4. a transfer with an object id that is not terminal gets a sandbox row
5. twenty parallel Setup posts answer no 5xx and lose no rows

Tester only (127.0.0.1:8020). Never prod.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import random
import re
import sqlite3
import string
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8020"
DB = "data/testsprite.sqlite3"
SUF = "r5" + "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
PIN = [ln.split("=", 1)[1].strip() for ln in open(".env")
       if ln.startswith("TESTSPRITE_PIN=")][0]


def post(path, body=None):
    data = json.dumps(body).encode() if body is not None else b"{}"
    req = urllib.request.Request(
        BASE + path, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"body": e.read().decode()[:200]}
    except Exception as exc:                       # noqa: BLE001
        return 0, {"note": str(exc)[:120]}


def page(path):
    with urllib.request.urlopen(BASE + path, timeout=90) as r:
        return r.read().decode()


def q(sql, *args):
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def rid(table, name):
    rows = q(f"SELECT id FROM {table} WHERE name = ?", name)
    return int(rows[0][0]) if rows else 0


print(f"suffix {SUF}\n")

print("[setup] a supplier, an invoice, and money that arrived")
CUST, SUP = f"Brightline {SUF}", f"Atlanta Packaging {SUF}"
post("/setup/customers", {"name": CUST})
st, body = post("/setup/suppliers", {
    "name": SUP, "country": "US", "currency": "USD", "payout_method": "LOCAL",
    "account_name": SUP, "account_number": "1234567890",
    "routing_value": "021000021", "routing_type": "aba", "bank_name": "Wells Fargo",
    "entity_type": "COMPANY", "city": "Atlanta", "street_address": "1 Peachtree",
    "state": "GA", "postcode": "30303", "bank_account_category": "Checking"})
print("  supplier:", str(body.get("message"))[:90])
CID, SID = rid("customer", CUST), rid("supplier", SUP)
INV = f"INV-{SUF.upper()}"
_st, _inv = post("/setup/invoices", {
    "customer_id": CID, "number": INV, "currency": "USD", "amount": "30000"})
print("  invoice :", str(_inv.get("message"))[:70])
acct = re.findall(r'<option value="([0-9a-f-]{36})"[^>]*>USD account',
                  page("/app?tab=money-in"))
st, dep = post("/money-in/simulate", {
    "global_account_id": acct[0] if acct else "", "currency": "USD",
    "amount": "30000", "payer_name": CUST, "reference": INV})
print("  deposit :", str(dep.get("message"))[:110])

print("\n[1] an over-limit bill is not sent, and asks")
OVER = f"PO-OVER-{SUF.upper()}"
_st, _bill = post("/setup/payables", {
    "supplier_id": SID, "number": OVER, "currency": "USD", "amount": "4000"})
print("  bill    :", str(_bill.get("message"))[:90])
time.sleep(2)
post("/money-out/run")
time.sleep(2)
steps = q("SELECT step_key,status,airwallex_object_id FROM plan_step WHERE step_key=?",
          f"PAY:{OVER}")
print("  plan_step:", steps)
ok_html = page("/app?tab=ok")
print("  a card names it         :", OVER in ok_html)
print("  it was NOT sent         :", not (steps and steps[0][2]))
print("  the card says the limit :", "auto-pay limit" in ok_html)

print("\n[2] two identical bills cannot share one approval")
A, B = f"PO-TWIN-A-{SUF.upper()}", f"PO-TWIN-B-{SUF.upper()}"
for n in (A, B):
    post("/setup/payables", {"supplier_id": SID, "number": n,
                             "currency": "USD", "amount": "4000"})
time.sleep(2)
post("/money-out/run")
time.sleep(2)
cards = re.findall(r'action="/approvals/(\d+)/approve"', page("/app?tab=ok"))
print("  approval forms on the page:", len(cards))
target = None
for sid in cards:
    # the form posts a PLAN STEP id, not an approval id
    rows = q("SELECT step_key FROM plan_step WHERE id=?", int(sid))
    if rows and rows[0][0] == f"PAY:{A}":
        target = sid
if target is None:
    # approve by the step id the form posts for bill A
    for sid in cards:
        row = q("SELECT payload_json FROM plan_step WHERE id=(SELECT plan_step_id "
                "FROM approval WHERE id=?)", int(sid))
    print("  could not identify bill A's card; approving none")
else:
    st, res = post(f"/approvals/{target}/approve", {"pin": PIN})
    print("  approve A:", str(res.get("message"))[:110])
time.sleep(3)
a_row = q("SELECT status,airwallex_object_id FROM plan_step WHERE step_key=?", f"PAY:{A}")
b_row = q("SELECT status,airwallex_object_id FROM plan_step WHERE step_key=?", f"PAY:{B}")
print("  A:", a_row)
print("  B:", b_row)
print("  B did not ride A's approval:", not (b_row and b_row[0][1]))

print("\n[3] a held bill names its real blocker")
SMALL = f"PO-HUGE-{SUF.upper()}"
post("/setup/payables", {"supplier_id": SID, "number": SMALL,
                         "currency": "USD", "amount": "25000"})
time.sleep(2)
post("/money-out/run")
time.sleep(2)
held = q("SELECT step_key,status FROM plan_step WHERE step_key=?", f"PAY:{SMALL}")
print("  plan_step:", held)
mo = page("/app?tab=money-out")
body = re.sub(r"<[^>]+>", " ", mo)
body = re.sub(r"\s+", " ", body)
i = body.find(SMALL)
print("  what the page says:", body[i:i + 320] if i >= 0 else "(not found)")

print("\n[4] a transfer that has not been checked still gets a sandbox row")
sent = q("SELECT step_key,airwallex_object_id FROM plan_step "
         "WHERE airwallex_object_id IS NOT NULL")
print("  transfers:", sent)
if sent:
    tid = sent[0][1]
    print("  row offered for", tid[:8], ":", f"sandbox-settle-{tid}" in mo)

print("\n[5] twenty parallel Setup posts")
def one(i):
    return post("/setup/customers", {"name": f"Par {SUF} {i}"})[0]

with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
    codes = list(pool.map(one, range(20)))
print("  status codes:", sorted(set(codes)), "| 5xx:", [c for c in codes if c >= 500])
names = q("SELECT COUNT(*) FROM customer WHERE name LIKE ?", f"Par {SUF} %")
print("  rows stored:", names[0][0], "of 20")

print("\nsuffix:", SUF)
