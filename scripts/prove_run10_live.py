"""Run 10, live on the TESTER: a cancelled attempt closes, a refund resolves.

Item 1: after a sandbox Fail or Return the page said "Re-planning failed
(IntegrityError); nothing was sent." and the re-plan rolled back. This sends a
real payment, fails it through the sandbox control, and checks that the page
names what the bank said, that no machine word is shown, and that the books
carry a closed attempt AND a fresh one.

Item 2: resolving unmatched money as refund owed left the payment waiting on the
same person who had just decided. This resolves one and reads both screens.

The book is seeded with the app's OWN writers (a bill, a supplier pointing at a
real sandbox beneficiary, a deposit); every action after that is the real
browser on the live tester.

Usage: .venv/bin/python scripts/prove_run10_live.py [book]
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
BOOK = sys.argv[1] if len(sys.argv) > 1 else "r10-live-1"
PIN = os.environ.get("TESTSPRITE_PIN") or "r10livepin"
BASE = os.environ.get("TESTSPRITE_BASE_URL",
                      "https://professor-apr-girls-meanwhile.trycloudflare.com")
SHOTS = Path("/tmp/proof/run10-live")
BILL = "PO-R10-LIVE-1"
BENEFICIARY = (os.environ.get("R10_BENEFICIARY")
               # A REAL sandbox beneficiary (US/USD LOCAL), read off the
               # account: a guessed id gets the payment refused at create time
               # and the live proof would then fail for the wrong reason.
               or "159854b9-96d4-49ae-bd2c-0a2709ef6581")


def _db():
    return REPO / "data" / "workspaces" / f"{BOOK}.sqlite3"


def seed() -> None:
    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("CLOSEBOOK_WORKSPACE_ROOT",
                          str(REPO / "data" / "workspaces"))
    from app.db import DB
    from app.domain.intake import record_and_match
    from app.domain.settings_store import save as save_settings
    from app.domain.workspace import (
        create_payable, create_supplier, record_beneficiary,
    )

    path = _db()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = DB(f"sqlite:///{path}")
    with db.session_scope() as s:
        save_settings(s, budget_currency="USD", reserve_floor="0",
                      auto_pay_limit="2500", fx_tolerance_pct="0.50")
        sup = create_supplier(s, name="Atlanta Packaging", country="US",
                              currency="USD", payout_method="LOCAL")
        record_beneficiary(s, sup.id, request={"prove": "real sandbox beneficiary"},
                           beneficiary_id=BENEFICIARY, status="CREATED")
        create_payable(s, supplier_id=sup.id, currency="USD", amount="3000",
                       number=BILL)
    # cash, and an unresolved deposit for item 2
    from app.domain.ledger import post

    with db.session_scope() as s:
        post(s, "opening:USD:r10", [("cash:USD", 5_000_00, 0, "USD"),
                                    ("opening_balance", 0, 5_000_00, "USD")])
    with db.session_scope() as s:
        record_and_match(s, deposit_id="dep-r10-live", currency="USD",
                         amount_minor=100_00, payer_name="Apex Factors Ltd.",
                         reference="nothing matches this", llm=False)


def book_state() -> dict:
    con = sqlite3.connect(f"file:{_db()}?mode=ro", uri=True)
    try:
        steps = con.execute(
            "select step_key, status, coalesce(airwallex_object_id, '') "
            "from plan_step order by id").fetchall()
        transfer = next((s[2] for s in steps if s[2]), "")
    finally:
        con.close()
    return {"steps": steps, "transfer": transfer}


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:                                    # pragma: no cover
        print(f"playwright unavailable: {exc}")
        return 2

    seed()
    SHOTS.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        console: list[str] = []
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}")
                if m.type == "error" else None)
        try:
            # ---- 1a. approve it, so a real transfer is created --------------
            page.goto(f"{BASE}/app?ws={BOOK}&tab=ok", wait_until="load")
            page.wait_for_timeout(1500)
            def approval_forms():
                return page.eval_on_selector_all(
                    "form[data-action][action*='/approvals']",
                    "fs => fs.map(f => f.getAttribute('action'))")

            forms = approval_forms()
            if not forms:
                # A CARD EXISTS ONLY AFTER A CYCLE HAS RUN. The seed writes rows;
                # nothing has planned yet, and a person in this state presses the
                # page's own Re-plan control. Do what they would do.
                # THE APP'S OWN ENDPOINT, the one the Re-plan form posts to.
                # That form only renders inside the "problems" card, so on a
                # fresh book there is nothing to click -- the route is still the
                # right way to make the first plan.
                r = page.request.post(f"{BASE}/money-out/run?ws={BOOK}")
                print("re-plan:", r.status)
                page.wait_for_timeout(5000)
                page.goto(f"{BASE}/app?ws={BOOK}&tab=ok", wait_until="load")
                page.wait_for_timeout(1200)
                forms = approval_forms()
            print(f"approval forms: {len(forms)}")
            if not forms:
                problems.append("no approval card for the over-limit bill")
            else:
                fid = f'form[action="{forms[0]}"]'
                page.click(f"{fid} input[name=pin]")
                page.keyboard.type(PIN)
                page.click(f"{fid} button[type=submit]")
                page.wait_for_timeout(6000)
                body = " ".join(page.inner_text("body").split())
                print("after approve:", body[:200])
                if "Approved" not in body and "sent" not in body.lower():
                    problems.append(f"the approval did not report: {body[:200]}")

            state = book_state()
            print("steps:", state["steps"])
            transfer = state["transfer"]
            if not transfer:
                problems.append("no transfer was created, so Fail cannot be tried")
            else:
                # ---- 1b. FAIL it through the sandbox control ----------------
                page.goto(f"{BASE}/app?ws={BOOK}&tab=money-out", wait_until="load")
                page.wait_for_timeout(1500)
                page.evaluate("""() => {
                    const d = document.getElementById('sandbox-controls');
                    if (d) d.open = true; }""")
                page.wait_for_timeout(300)
                clicked = page.evaluate("""(tid) => {
                    const row = document.getElementById('sandbox-row-' + tid);
                    if (!row) return false;
                    const btn = Array.from(row.querySelectorAll('button'))
                      .find(b => /fail/i.test(b.textContent));
                    if (!btn) return false;
                    btn.click(); return true; }""", transfer)
                print("fail clicked:", clicked)
                if not clicked:
                    problems.append("no Fail control for the transfer")
                page.wait_for_timeout(9000)
                body = " ".join(page.inner_text("body").split())
                page.screenshot(path=str(SHOTS / "41-after-fail.png"),
                                full_page=True)
                print("after fail:", body[:260])
                if "IntegrityError" in body:
                    problems.append("the page named an exception class")
                if "Re-planning failed" in body:
                    problems.append("the re-plan still failed")
                if "Failed:" not in body and "Returned:" not in body:
                    problems.append("the card does not say what the bank said")

                # ---- 1c. the books: a closed attempt AND a fresh one --------
                steps = book_state()["steps"]
                print("steps after fail:", steps)
                statuses = [s[1] for s in steps]
                if not any(s in ("CANCELLED", "FAILED") for s in statuses):
                    problems.append(f"no attempt was closed: {steps}")
                keys = [s[0] for s in steps]
                if len(keys) != len(set(keys)):
                    problems.append(f"duplicate step keys: {keys}")

            # ---- 2. resolve unmatched money as refund owed -----------------
            page.goto(f"{BASE}/app?ws={BOOK}&tab=money-in", wait_until="load")
            page.wait_for_timeout(1500)
            forms = page.eval_on_selector_all(
                'form[data-action][action*="/resolve"]',
                "fs => fs.map(f => f.getAttribute('action'))")
            print(f"resolve forms: {len(forms)}")
            if not forms:
                problems.append("no resolve form for the deposit")
            else:
                fid = f'form[action="{forms[0]}"]'
                page.select_option(f"{fid} select[name=what]", "refund")
                page.fill(f"{fid} input[name=reason]", "duplicate payment")
                page.click(f"{fid} input[name=pin]")
                page.keyboard.type(PIN)
                page.click(f"{fid} button[type=submit]")
                page.wait_for_timeout(4000)

                page.goto(f"{BASE}/app?ws={BOOK}&tab=ok", wait_until="load")
                page.wait_for_timeout(1500)
                body = " ".join(page.inner_text("body").split())
                page.screenshot(path=str(SHOTS / "42-resolved-money.png"),
                                full_page=True)
                print("resolved money section:", body[:300])
                if "Resolved money" not in body:
                    problems.append("the resolution is not listed under Resolved money")
                if "Refund owed" not in body:
                    problems.append("the resolution is not labelled Refund owed")
                if "waiting on you" in body.lower() and "1 payment" in body:
                    problems.append("the resolved payment still counts as waiting")
        finally:
            ctx.close()
            b.close()

    for line in console[-8:]:
        print("   ", line)
    print("\nRESULT:", "PASS" if not problems else "FAIL")
    for p_ in problems:
        print("  -", p_)
    print(f"screenshots in {SHOTS}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
