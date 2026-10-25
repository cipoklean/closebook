"""Run 10's gap, closed live: the card says why a transfer came back.

Fails ONE transfer and RETURNS another through the sandbox controls on the live
tester, then reads the rendered page: "Failed: <reason>" and "Returned:
<reason>" on their cards, and the same words in the trail.

Both bills are seeded with the app's own writers (a real beneficiary, real
cash); every click after that is the real UI, and every status is Airwallex's.

Usage: .venv/bin/python scripts/prove_run11_reasons_live.py [book]
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
BOOK = sys.argv[1] if len(sys.argv) > 1 else "r11-live-1"
PIN = os.environ.get("TESTSPRITE_PIN") or "r11livepin"
BASE = os.environ.get("TESTSPRITE_BASE_URL",
                      "https://professor-apr-girls-meanwhile.trycloudflare.com")
SHOTS = Path("/tmp/proof/run11-live")
BILLS = ("PO-R11-FAIL-1", "PO-R11-RETURN-1")
BENEFICIARY = "159854b9-96d4-49ae-bd2c-0a2709ef6581"


def _db() -> Path:
    return REPO / "data" / "workspaces" / f"{BOOK}.sqlite3"


def seed() -> None:
    sys.path.insert(0, str(BACKEND))
    os.environ.setdefault("CLOSEBOOK_WORKSPACE_ROOT",
                          str(REPO / "data" / "workspaces"))
    from app.db import DB
    from app.domain.ledger import post
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
        record_beneficiary(s, sup.id, request={"prove": "real beneficiary"},
                           beneficiary_id=BENEFICIARY, status="CREATED")
        for number in BILLS:
            create_payable(s, supplier_id=sup.id, currency="USD",
                           amount="3000", number=number)
    with db.session_scope() as s:
        post(s, "opening:USD:r11", [("cash:USD", 9_000_00, 0, "USD"),
                                    ("opening_balance", 0, 9_000_00, "USD")])


def steps() -> list[tuple]:
    con = sqlite3.connect(f"file:{_db()}?mode=ro", uri=True)
    try:
        return con.execute(
            "select step_key, status, coalesce(airwallex_object_id, '') "
            "from plan_step order by id").fetchall()
    finally:
        con.close()


def transfer_for(number: str) -> str:
    con = sqlite3.connect(f"file:{_db()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select payload_json, coalesce(airwallex_object_id, '') "
            "from plan_step order by id").fetchall()
    finally:
        con.close()
    for payload, oid in rows:
        if number in (payload or "") and oid:
            return oid
    return ""


def approve_and_control(page, number: str, what: str) -> str:
    """Approve one bill, then drive the sandbox control for its transfer."""
    page.goto(f"{BASE}/app?ws={BOOK}&tab=ok", wait_until="load")
    page.wait_for_timeout(1200)
    # the approval card for THIS bill
    hit = page.evaluate("""(num) => {
        const f = Array.from(document.querySelectorAll("form[data-action][action*='/approvals']"))
          .find(x => (x.closest('.x-card, [data-card]') || x).textContent.includes(num));
        if (!f) return false;
        const pin = f.querySelector("input[name=pin]");
        if (pin) pin.value = "";
        f.querySelector("button[type=submit]").click();
        return true; }""", number)
    if not hit:
        return ""
    page.wait_for_timeout(400)
    # the PIN goes in with the keyboard, the way a person types it
    fid = page.evaluate("""(num) => {
        const f = Array.from(document.querySelectorAll("form[data-action][action*='/approvals']"))
          .find(x => (x.closest('.x-card, [data-card]') || x).textContent.includes(num));
        return f ? f.getAttribute('action') : null; }""", number)
    if fid:
        page.click(f'form[action="{fid}"] input[name=pin]')
        page.keyboard.type(PIN)
        page.click(f'form[action="{fid}"] button[type=submit]')
    page.wait_for_timeout(7000)

    oid = transfer_for(number)
    if not oid:
        return ""
    page.goto(f"{BASE}/app?ws={BOOK}&tab=money-out", wait_until="load")
    page.wait_for_timeout(1500)
    page.evaluate("() => { const d = document.getElementById('sandbox-controls'); "
                  "if (d) d.open = true; }")
    page.wait_for_timeout(400)
    page.evaluate("""([tid, what]) => {
        const row = document.getElementById('sandbox-row-' + tid);
        if (!row) return;
        const btn = Array.from(row.querySelectorAll('button'))
          .find(b => new RegExp(what, 'i').test(b.textContent));
        if (btn) btn.click(); }""", [oid, what])
    page.wait_for_timeout(11000)
    return oid


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
        ctx = b.new_context(viewport={"width": 1440, "height": 1100})
        page = ctx.new_page()
        try:
            # the first plan
            page.goto(f"{BASE}/app?ws={BOOK}&tab=ok", wait_until="load")
            page.wait_for_timeout(1200)
            page.request.post(f"{BASE}/money-out/run?ws={BOOK}")
            page.wait_for_timeout(4000)
            print("steps after re-plan:", steps())

            for number, what, want in ((BILLS[0], "fail", "Failed:"),
                                      (BILLS[1], "return", "Returned:")):
                oid = approve_and_control(page, number, what)
                print(f"{number} {what} -> transfer {oid[:8] if oid else '(none)'}")
                if not oid:
                    problems.append(f"{number}: nothing was sent, so {what} "
                                    f"could not be tried")
                    continue
                body = " ".join(page.inner_text("body").split())
                print("page says:", body[:240])
                if want not in body:
                    problems.append(f"{number}: the card does not say {want!r}")
                if "IntegrityError" in body or "Re-planning failed" in body:
                    problems.append(f"{number}: the re-plan failed on screen")
                shot = SHOTS / f"{what}-caption.png"
                page.screenshot(path=str(shot), full_page=True)

                # AND IN THE TRAIL (the collapsed history on the same card)
                trail = page.evaluate("""() => Array.from(
                    document.querySelectorAll("[data-testid='trail-reason']"))
                    .map(e => e.textContent.trim()).join(' | ')""")
                print("trail:", trail[:200] or "(none rendered yet)")
                if trail and want not in trail:
                    problems.append(f"{number}: the trail does not repeat it")
                if not trail:
                    # the detail is folded; open it and read again
                    page.evaluate("""() => document.querySelectorAll('details')
                        .forEach(d => d.open = true)""")
                    page.wait_for_timeout(400)
                    trail = page.evaluate("""() => Array.from(
                        document.querySelectorAll("[data-testid='trail-reason']"))
                        .map(e => e.textContent.trim()).join(' | ')""")
                    print("trail (opened):", trail[:200])
                # A MISSING TRAIL IS A FAILURE, not something to skip: the ask
                # was the reason on the card AND in the timeline.
                if not trail:
                    problems.append(f"{number}: no trail-reason rendered at all")
                elif want not in trail:
                    problems.append(f"{number}: the trail lacks {want!r}")
                    page.screenshot(path=str(SHOTS / f"{what}-timeline.png"),
                                    full_page=True)

            print("final steps:", steps())
        finally:
            ctx.close()
            b.close()

    print("\nRESULT:", "PASS" if not problems else "FAIL")
    for p_ in problems:
        print("  -", p_)
    print(f"screenshots in {SHOTS}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
