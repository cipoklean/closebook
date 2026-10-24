"""Live proof, on the tester, of the run-6 money-out chain end to end.

WHY THIS EXISTS SEPARATELY FROM THE E2E. `tests/e2e/test_run6_money_out.py` is
provider-free and stops where the local decision chain stops: PROCESSING, settle,
fail and return are AIRWALLEX's states, and a stub bank would prove the stub. So
this drives the same chain against the tester, which has real sandbox
credentials, and takes a screenshot at each step.

  1. a fresh book, a supplier with a REAL beneficiary, a bill under the limit
  2. a REAL sandbox deposit, which lands unattributed
  3. released as income with the PIN, so the money becomes spendable
  4. the executor sends the bill, and the transfer reaches PROCESSING
  5. the Sandbox settle control, with free to spend UNCHANGED across it
  6. an over-limit bill, PIN approved, also reaching PROCESSING
  7. another payment driven to FAILED, which cancels at the bank

Run it against a running tester:

    .venv/bin/python scripts/prove_run6_money_out_live.py [base-url]

It moves SANDBOX money only. Its book is deleted by removing
data/workspaces/*.sqlite3 afterwards.
"""
from __future__ import annotations

import json
import random
import re
import sqlite3
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8020").rstrip("/")
SUFFIX = "r6-live-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
OUT = Path("/tmp/proof/run6-live")
OUT.mkdir(parents=True, exist_ok=True)
REPO = Path(__file__).resolve().parents[1]
BOOK_DB = REPO / "data" / "workspaces" / f"{SUFFIX}.sqlite3"

BANK = "Pennine Supplies"
BILL_UNDER = "PO-R6-L1"      # USD 1,800.00, under the 2,500 limit
BILL_OVER = "PO-R6-L3"       # USD 3,000.00, over it
BILL_FAIL = "PO-R6-L4"       # USD 2,000.00, driven to a failure

PIN = ""
if (REPO / ".env").exists():
    m = re.search(r"^TESTSPRITE_PIN=(.*)$", (REPO / ".env").read_text(), re.M)
    PIN = (m.group(1).strip() if m else "")
if not PIN:
    print("refused: TESTSPRITE_PIN is not set in .env, so nothing can be approved")
    raise SystemExit(1)

_jar: dict[str, str] = {}
_failures: list[str] = []


def _url(path: str) -> str:
    sep = "&" if "?" in path else "?"
    return f"{BASE}{path}{sep}ws={SUFFIX}"


def fetch(path: str, *, method: str = "GET", body: dict | None = None):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = urllib.parse.urlencode(body).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if _jar:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in _jar.items())
    req = urllib.request.Request(_url(path), data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read().decode()
            for cookie in r.headers.get_all("set-cookie") or []:
                name, _, rest = cookie.partition("=")
                _jar[name.strip()] = rest.split(";")[0]
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode()[:300]}
    try:
        return 200, json.loads(raw or "{}")
    except ValueError:
        return 200, {"html": raw}


def check(label: str, passed: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not passed:
        _failures.append(label)
    return passed


def rows(sql: str) -> list[tuple]:
    if not BOOK_DB.exists():
        return []
    con = sqlite3.connect(f"file:{BOOK_DB}?mode=ro", uri=True)
    try:
        return con.execute(sql).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


def transfer_of(bill: str) -> tuple[str, str]:
    """(airwallex_object_id, stored status) for a bill's newest transfer step.

    PARSED IN PYTHON, never matched with SQL `LIKE` on a JSON column: `%` and `_`
    are wildcards and the separator spacing has to match the writer exactly, so a
    text match can bind a step to someone else's transfer.
    """
    out: list[tuple[str, str]] = []
    for payload, obj, status in rows(
            "SELECT payload_json, airwallex_object_id, status FROM plan_step "
            "WHERE type = 'TRANSFER' ORDER BY id"):
        try:
            data = json.loads(payload or "{}")
        except ValueError:
            continue
        if data.get("payable_number") == bill and obj:
            out.append((str(obj), str(status)))
    return out[-1] if out else ("", "")


def step_id_of(bill: str) -> int:
    found = 0
    for step_id, payload in rows("SELECT id, payload_json FROM plan_step "
                                 "WHERE type = 'TRANSFER' ORDER BY id"):
        try:
            data = json.loads(payload or "{}")
        except ValueError:
            continue
        if data.get("payable_number") == bill:
            found = int(step_id)
    return found


# ------------------------------------------------------------------ browser
class _Page:
    """Playwright, for screenshots and for reading what the page says."""

    def __init__(self):
        from playwright.sync_api import sync_playwright

        self._p = sync_playwright().start()
        self.browser = self._p.chromium.launch(headless=True)
        self.ctx = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.page = self.ctx.new_page()

    def text(self, path: str) -> str:
        self.page.goto(_url(path), wait_until="load", timeout=90_000)
        self.page.wait_for_timeout(400)
        return " ".join(self.page.inner_text("body").split())

    def free_to_spend(self) -> str:
        text = self.text("/app?tab=home")
        m = re.search(r"Free to spend\s+(Short by\s+)?(USD [\d,]+\.\d\d)", text)
        return (m.group(0) if m else "(not found)")

    def bank_state(self, bill: str) -> str:
        """The bank pill for a bill's card, read off the rendered page.

        The card is `.x-sup` and the pill next to the bill number carries the
        LIVE status the app asked Airwallex for (`live_status`, refreshed on
        render), so this asserts what the bank says, not what we wrote down.
        """
        self.text("/app?tab=money-out")
        return self.page.evaluate("""(() => {
          const cards = [...document.querySelectorAll('.x-sup')];
          const card = cards.find(c => c.textContent.includes('%s'));
          if (!card) return '(card not found)';
          const top = card.querySelector('.x-sup-top') || card;
          const pills = [...top.querySelectorAll('.x-pill')].map(p => p.textContent.trim());
          return pills.join(' | ') || '(no pill)';
        })()""" % bill)

    def shot(self, name: str) -> None:
        path = OUT / f"{SUFFIX}-{name}.png"
        self.page.screenshot(path=str(path), full_page=True)
        print(f"        screenshot {path}")

    def wait_for_bank(self, bill: str, *wanted: str, seconds: int = 45) -> str:
        """Re-render until the bank pill reports one of `wanted`.

        The pill is filled by a live Airwallex lookup that runs after the render
        (`live_status`, refreshed in a BackgroundTask), so immediately after a
        send it reads UNCHECKED. That is a race with the lookup, not a wrong
        state, so poll rather than assert on the first frame.
        """
        deadline = time.time() + seconds
        seen = ""
        while time.time() < deadline:
            seen = self.bank_state(bill)
            if any(w.upper() in seen.upper() for w in wanted):
                return seen
            time.sleep(3)
        return seen

    def close(self) -> None:
        self.browser.close()
        self._p.stop()


print(f"tester : {BASE}")
print(f"book   : {SUFFIX}\n")

page = _Page()

# ---------------------------------------------------------------- 1. the book
print("[1] a fresh book, a supplier and a bill under the limit")
check("the book opens", fetch("/app")[0] == 200)
res = fetch("/setup/settings", method="POST", body={
    "budget_currency": "USD", "reserve_floor": "0", "auto_pay_limit": "2500"})[1]
check("policy set (floor 0, limit 2,500)", res.get("ok") is True,
      str(res.get("message"))[:60])
fetch("/setup/customers", method="POST", body={"name": "Brightline Retail"})
res = fetch("/setup/suppliers", method="POST", body={
    "name": BANK, "country": "US", "currency": "USD", "payout_method": "LOCAL",
    "entity_type": "COMPANY", "account_name": BANK, "account_number": "1234567890",
    "routing_value": "021000021", "bank_account_category": "Checking",
    "bank_name": "Wells Fargo", "street_address": "1 Peachtree St",
    "city": "Atlanta", "state": "GA", "postcode": "30303"})[1]
check("a REAL sandbox beneficiary is created", res.get("ok") is True,
      str(res.get("message"))[:110])

for bill, amount in ((BILL_UNDER, "1800"), (BILL_OVER, "3000"), (BILL_FAIL, "2000")):
    res = fetch("/setup/payables", method="POST", body={
        "supplier_id": "1", "number": bill, "currency": "USD",
        "amount": amount, "criticality": "3"})[1]
    check(f"{bill} ({amount}) is created", res.get("ok") is True,
          str(res.get("message"))[:70])
page.text("/app?tab=home")
page.shot("1-setup")

# ------------------------------------------------------------- 2. the deposit
print("\n[2] a REAL sandbox deposit, which lands unattributed")
res = fetch("/money-in/simulate", method="POST", body={
    "currency": "USD", "amount": "20000", "payer_name": "Brightline Retail",
    "reference": "cash with no invoice"})[1]
check("the sandbox created a USD 20,000.00 deposit", res.get("ok") is True,
      str(res.get("message"))[:180])
money_out = page.text("/app?tab=money-out")
check("the page says the money is awaiting a match, not that the account is empty",
      "waiting on a match" in money_out and "holds USD 0.00" not in money_out)
check("it names what the book actually holds", "USD 20,000.00" in money_out)
page.shot("2-held-names-the-truth")
before_free = page.free_to_spend()
print(f"        free to spend, before the release: {before_free}")

# ------------------------------------------------------- 3. release as income
print("\n[3] released as income with the PIN, so it becomes spendable")
inflow_id = rows("SELECT id FROM inflow ORDER BY id DESC LIMIT 1")
inflow_id = int(inflow_id[0][0]) if inflow_id else 1
res = fetch(f"/inflows/{inflow_id}/resolve", method="POST", body={
    "what": "income", "note": "no invoice will claim this", "pin": PIN})[1]
check("the release is accepted", res.get("ok") is True, str(res.get("message"))[:160])
time.sleep(3)
after_release = page.free_to_spend()
check("free to spend no longer says the money is unmatched",
      after_release != before_free, f"{before_free} -> {after_release}")
page.shot("3-released-as-income")

# ------------------------------------------------------------------ 4. send
print("\n[4] the executor sends the under-limit bill")
res = fetch("/money-out/run", method="POST", body={})[1]
print(f"        run: {str(res.get('message'))[:200]}")
time.sleep(10)
obj, stored = transfer_of(BILL_UNDER)
check("a real transfer was created for it", bool(obj), f"{obj[:8]}... status={stored}")
bank = page.wait_for_bank(BILL_UNDER, "PROCESSING")
check("the bank reports it PROCESSING (what Airwallex says, not our row)",
      "PROCESSING" in bank.upper(), bank)
page.shot("4-processing")

# ---------------------------------------------------------------- 5. settle
print("\n[5] the Sandbox settle control, and free to spend across it")
before_settle = page.free_to_spend()
if obj:
    res = fetch(f"/dev/transfers/{obj}/transition", method="POST",
                body={"what": "settle"})[1]
    print(f"        settle: {str(res.get('message'))[:200]}")
time.sleep(8)
after_settle = page.free_to_spend()
check("free to spend did NOT move when the payment settled",
      before_settle == after_settle, f"{before_settle} -> {after_settle}")
bank = page.wait_for_bank(BILL_UNDER, "PAID", "COMPLETED", "SUCCESS")
check("the bank now reports it settled", "PAID" in bank.upper(), bank)
page.shot("5-settled")

# ------------------------------------------------------- 6. the over-limit bill
print("\n[6] an over-limit bill, PIN approved, reaching PROCESSING")
res = fetch("/money-out/run", method="POST", body={})[1]
time.sleep(6)
time.sleep(2)
ok_page = page.text("/app?tab=ok")
check("the over-limit bill is waiting for a person",
      BILL_OVER in ok_page, ok_page[:200])
step = step_id_of(BILL_OVER)
res = fetch(f"/approvals/{step}/approve", method="POST",
            body={"pin": PIN})[1] if step else {"ok": False}
check("the PIN approval is recorded", res.get("ok") is True,
      str(res.get("message"))[:160])
time.sleep(12)
obj_over, stored_over = transfer_of(BILL_OVER)
check("an approved over-limit payment was sent", bool(obj_over),
      f"{obj_over[:8]}... status={stored_over}")
bank = page.wait_for_bank(BILL_OVER, "PROCESSING")
check("the bank reports it PROCESSING", "PROCESSING" in bank.upper(), bank)
page.shot("6-over-limit-approved-processing")

# ------------------------------------------------------------- 7. a failure
print("\n[7] another payment driven to FAILED, which cancels at the bank")
res = fetch("/money-out/run", method="POST", body={})[1]
time.sleep(8)
obj_fail, stored_fail = transfer_of(BILL_FAIL)
if not obj_fail:
    print("        no transfer for the failure bill yet: running once more")
    fetch("/money-out/run", method="POST", body={})
    time.sleep(10)
    obj_fail, stored_fail = transfer_of(BILL_FAIL)
check("a transfer exists for the failure bill", bool(obj_fail),
      f"{obj_fail[:8]}... status={stored_fail}")
if obj_fail:
    for attempt in range(1, 5):
        res = fetch(f"/dev/transfers/{obj_fail}/transition", method="POST",
                    body={"what": "fail"})[1]
        print(f"        fail (attempt {attempt}): {str(res.get('message'))[:160]}")
        if res.get("ok"):
            break
        time.sleep(15)
bank = page.wait_for_bank(BILL_FAIL, "CANCELLED", "FAILED", seconds=60)
check("the bank reports it CANCELLED", "CANCELLED" in bank.upper(), bank)
page.shot("7-failed-cancelled")

page.close()
print("\nRESULT:", "PASS" if not _failures else f"FAIL ({len(_failures)})")
for f in _failures:
    print("  -", f)
raise SystemExit(0 if not _failures else 1)
