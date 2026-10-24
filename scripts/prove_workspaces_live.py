"""Live proof on the tester for run 5's items 1-4.

1. two workspaces, isolated by `?ws=`, with their own settings
2. an over-committed book says "over-committed by", not a minus sign
3. a held card names its blocker with numbers
4. the cash figure moves after a match
"""
from __future__ import annotations

import json
import random
import re
import string
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8020"
SUF = "r6" + "".join(random.choices(string.ascii_lowercase + string.digits, k=4))


def _url(path: str, ws: str) -> str:
    return f"{BASE}{path}{'&' if '?' in path else '?'}ws={ws}"


def post(path: str, body: dict, ws: str = ""):
    url = _url(path, ws) if ws else BASE + path
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"body": e.read().decode()[:160]}
    except Exception as exc:                       # noqa: BLE001
        return 0, {"note": str(exc)[:120]}


def page(path: str, ws: str) -> str:
    with urllib.request.urlopen(_url(path, ws), timeout=90) as r:
        return r.read().decode()


def text(html: str) -> str:
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", body).strip()


A, B = f"{SUF}-a", f"{SUF}-b"
print(f"workspaces {A} and {B}\n")

print("[1] two books, one instance")
_st, _resp = post("/setup/customers", {"name": "Only In A"}, ws=A)
print("  wrote 'Only In A' into A ->", str(_resp.get("message"))[:60])
in_a = "Only In A" in text(page("/app?tab=setup", A))
in_b = "Only In A" in text(page("/app?tab=setup", B))
print(f"  visible in A: {in_a} | visible in B: {in_b}")
post("/setup/settings", {"budget_currency": "USD", "budget": "40000",
                         "reserve_floor": "5000", "auto_pay_limit": "9000"}, ws=A)
la = re.search(r"auto-pay limit[^0-9]*([0-9,]+)", text(page("/app?tab=home", A)))
lb = re.search(r"auto-pay limit[^0-9]*([0-9,]+)", text(page("/app?tab=home", B)))
print(f"  A auto-pay limit: {la.group(1) if la else '?'} | "
      f"B auto-pay limit: {lb.group(1) if lb else '?'}")
print(f"  settings independent: {bool(la and lb and la.group(1) != lb.group(1))}")

print("\n[2] and [3] a big bill in one book: over-committed, and named")
C = f"{SUF}-c"
post("/setup/customers", {"name": "Brightline C"}, ws=C)
post("/setup/suppliers", {
    "name": "Atlanta C", "country": "US", "currency": "USD",
    "payout_method": "LOCAL", "account_name": "Atlanta C",
    "account_number": "1234567890", "routing_value": "021000021",
    "routing_type": "aba", "bank_name": "Wells Fargo",
    "entity_type": "COMPANY", "city": "Atlanta", "street_address": "1 P",
    "state": "GA", "postcode": "30303", "bank_account_category": "Checking"}, ws=C)
import sqlite3
cpath = f"data/workspaces/{C}.sqlite3"
sid = sqlite3.connect(cpath).execute(
    "SELECT id FROM supplier WHERE name='Atlanta C'").fetchone()[0]
post("/setup/payables", {"supplier_id": sid, "number": "PO-BIG",
                         "currency": "USD", "amount": "5000"}, ws=C)
time.sleep(2)
post("/money-out/run", {}, ws=C)
time.sleep(2)
home_c = text(page("/app?tab=home", C))
print("  Home on an empty over-committed book:")
m = re.search(r"over-committed by[^.]{0,40}", home_c)
print("   ", m.group(0) if m else "(no over-committed line)")
out_c = text(page("/app?tab=money-out", C))
i = out_c.find("PO-BIG")
print("  the held card says:", out_c[i:i + 260] if i >= 0 else "(not found)")

print("\n[4] cash after an exact match")
acct = re.findall(r'<option value="([0-9a-f-]{36})"[^>]*>USD account',
                  page("/app?tab=money-in", C))
before = re.search(r"cash[^0-9]*([0-9][0-9,.]*)", home_c)
print("  cash on Home before:", before.group(1) if before else "(none)")
st, dep = post("/money-in/simulate", {
    "global_account_id": acct[0] if acct else "", "currency": "USD",
    "amount": "1200", "payer_name": "Brightline C", "reference": "no ref"}, ws=C)
print("  deposit:", str(dep.get("message"))[:110])
time.sleep(3)
after_home = text(page("/app?tab=home", C))
after = re.search(r"cash[^0-9]*([0-9][0-9,.]*)", after_home)
print("  cash on Home after :", after.group(1) if after else "(none)")

print("\n  workspace files:")
import pathlib
for p in sorted(pathlib.Path("data/workspaces").glob("*.sqlite3"))[-8:]:
    print("   ", p.name, p.stat().st_size, "bytes")
