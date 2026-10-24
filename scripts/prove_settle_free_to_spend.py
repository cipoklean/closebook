"""Does settling a payment move free to spend?

Run 4 saw USD 22,300 become USD 12,300 when a USD 10,000 payment settled. The
money should have left the figure the moment it was COMMITTED (sent), not when
the bank confirmed, so settling should change nothing.

This mirrors the test on the live tester: fund, let a USD 1,800 bill auto-send,
read free to spend, settle, read again. It prints the INPUTS of the waterfall at
every read, because "the number moved" is not a cause and "in flight went from X
to Y" is.

  python scripts/prove_settle_free_to_spend.py
"""
from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import string
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

BASE = "http://127.0.0.1:8020"
DB = "data/testsprite.sqlite3"
# `build_context` reads the DEFAULT handle, so point it at the tester before any
# app module is imported. Reading a second handle would answer a different book.
os.environ["CLOSEBOOK_DATABASE_URL"] = f"sqlite:///{os.path.abspath(DB)}"
SUF = "fs" + "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
PIN = [ln.split("=", 1)[1].strip() for ln in open(".env")
       if ln.startswith("TESTSPRITE_PIN=")][0]


def post(path, body=None):
    data = json.dumps(body).encode() if body is not None else b"{}"
    req = urllib.request.Request(
        BASE + path, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"body": e.read().decode()[:200]}
    except Exception as exc:                       # noqa: BLE001
        return 0, {"note": str(exc)[:120]}


def page(p):
    with urllib.request.urlopen(BASE + p, timeout=90) as r:
        return r.read().decode()


def q(sql, *args):
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def inputs() -> dict:
    """The waterfall's own inputs, read from the app rather than recomputed here."""
    from app.dashboard import build_context

    # The page's OWN context, so what is printed here is what the tile shows.
    c = build_context([])
    m = c["money"]
    return {
        "cash": c["cash"],
        "in_flight": m["in_flight"],
        "promised": m["promised"],
        "suspense": m["suspense"],
        "refunds_owed": m["refunds_owed"],
        "floor_minor": m["reserve_floor_minor"],
        "free": m["free"],
    }


def show(tag: str) -> dict:
    got = inputs()
    print(f"  {tag}:")
    print(f"    {json.dumps(got, default=str)[:600]}")
    return got


print(f"suffix {SUF}")

print("\n[fund] a supplier, an invoice, and USD 30,000 in")
CUST, SUP = f"Brightline {SUF}", f"Atlanta {SUF}"
post("/setup/customers", {"name": CUST})
_st, sup = post("/setup/suppliers", {
    "name": SUP, "country": "US", "currency": "USD", "payout_method": "LOCAL",
    "account_name": SUP, "account_number": "1234567890",
    "routing_value": "021000021", "routing_type": "aba", "bank_name": "Wells Fargo",
    "entity_type": "COMPANY", "city": "Atlanta", "street_address": "1 Peachtree",
    "state": "GA", "postcode": "30303", "bank_account_category": "Checking"})
SID = int(q("SELECT id FROM supplier WHERE name = ?", SUP)[0][0])
CID = int(q("SELECT id FROM customer WHERE name = ?", CUST)[0][0])
INV = f"INV-{SUF.upper()}"
post("/setup/invoices", {"customer_id": CID, "number": INV, "currency": "USD",
                         "amount": "30000"})
acct = re.findall(r'<option value="([0-9a-f-]{36})"[^>]*>USD account',
                  page("/app?tab=money-in"))
post("/money-in/simulate", {"global_account_id": acct[0] if acct else "",
                            "currency": "USD", "amount": "30000",
                            "payer_name": CUST, "reference": INV})
time.sleep(2)

print("\n[a] a USD 1,800 bill, under the limit, so it sends itself")
BILL = f"PO-{SUF.upper()}"
post("/setup/payables", {"supplier_id": SID, "number": BILL, "currency": "USD",
                         "amount": "1800"})
time.sleep(2)
post("/money-out/run")
time.sleep(3)
step = q("SELECT status, airwallex_object_id FROM plan_step WHERE step_key=?",
         f"PAY:{BILL}")
print("  plan_step:", step)
if not step or not step[0][1]:
    print("  the bill did not send; nothing to settle")
    raise SystemExit(0)
TID = step[0][1]

print("\n[b] free to spend BEFORE settle")
before = show("before")

print("\n[c] settle it")
print("  settle ->", str(post(f"/dev/transfers/{TID}/transition",
                            {"what": "settle"})[1].get("message"))[:130])
time.sleep(3)

print("\n[d] free to spend AFTER settle")
after = show("after")

print("\n[e] the same reading while other payments and a cycle run")
import threading
stop = threading.Event()
reads: list = []


def sampler():
    while not stop.is_set():
        try:
            got = inputs()
            reads.append(("free=" + str(got["free"].get("USD")),
                          "cash=" + str(got["cash"].get("USD")),
                          "flight=" + str(got["in_flight"].get("USD"))))
        except Exception as exc:                   # noqa: BLE001
            reads.append(("ERR", str(exc)[:40], ""))
        time.sleep(0.6)


def churn():
    for i in range(3):
        n = f"PO-{SUF.upper()}-C{i}"
        post("/setup/payables", {"supplier_id": SID, "number": n,
                                 "currency": "USD", "amount": "600"})
        post("/money-out/run")
        time.sleep(1)


t = threading.Thread(target=sampler)
t.start()
churn()
stop.set()
t.join()
print("  reads (free, cash, in flight):")
for r in reads[:14]:
    print("   ", r)
print("  distinct free values:", sorted({r[0] for r in reads}))

print("\n[result]")
print(f"  before: {json.dumps(before, default=str)[:400]}")
print(f"  after : {json.dumps(after, default=str)[:400]}")
