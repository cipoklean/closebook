"""Run 9 item 2, against the TESTER: does the resolve form keep what is typed?

The tester reported that on an AMBIGUOUS sandbox deposit the release Note and
the PIN stay blank after typing, repeatedly, and five tests blocked on it. Run 8
made both fields visible and that did not stop it, so this drives the tester's
own steps on the LIVE tester and reads the fields at three moments:

  * immediately after typing   -- did the keystrokes land at all?
  * after 6s (past a poll)     -- did something clear them afterwards?
  * after the submit           -- did the release happen?

It also records console errors and any non-2xx response, because a silent error
is how a form "loses" input it never received.

Usage: .venv/bin/python scripts/prove_run9_resolve_live.py [book]
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

BASE = os.environ.get("TESTSPRITE_BASE_URL",
                      "https://professor-apr-girls-meanwhile.trycloudflare.com")
BOOK = sys.argv[1] if len(sys.argv) > 1 else "r9-live-1"
PIN = os.environ.get("TESTSPRITE_PIN") or "r9livepin"
SHOTS = Path("/tmp/proof/run9-live")
AMOUNT = "12000"
PAYER = "Apex Factors Ltd."
REFERENCE = "cash with no invoice"
NOTE = "no invoice will ever claim this"

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "e2e"))


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:                                   # pragma: no cover
        print(f"playwright unavailable: {exc}")
        return 2

    SHOTS.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        console: list[str] = []
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}")
                if m.type in ("error", "warning") else None)
        page.on("response", lambda r: console.append(f"HTTP {r.status} {r.url}")
                if r.status >= 400 else None)
        try:
            url = f"{BASE}/app?ws={BOOK}&tab=money-in"
            print(f"opening {url}")
            page.goto(url, wait_until="load")
            page.wait_for_timeout(1500)
            body = page.inner_text("body")
            if "Book:" not in body:
                problems.append("the tester did not render the book label")
                print(body[:300])

            # ---- 1. a real sandbox deposit, through the page's own control ----
            dep = page.query_selector("form[action^='/money-in/simulate']")
            if dep is None:
                problems.append("no sandbox deposit form on Money in")
            else:
                f = "form[action^='/money-in/simulate']"
                page.fill(f"{f} input[name=amount]", AMOUNT)
                page.fill(f"{f} input[name=payer_name]", PAYER)
                page.fill(f"{f} input[name=reference]", REFERENCE)
                page.click(f"{f} button[type=submit]")
                print("deposit submitted")
                page.wait_for_timeout(6000)

            # ---- 2. the resolve card, and the tester's own sequence ----------
            page.goto(f"{BASE}/app?ws={BOOK}&tab=money-in", wait_until="load")
            page.wait_for_timeout(1200)
            forms = page.eval_on_selector_all(
                'form[data-action][action*="/resolve"]',
                "fs => fs.map(f => f.getAttribute('action'))")
            print(f"resolve forms on Money in: {len(forms)}")
            if not forms:
                problems.append("no resolve form for the ambiguous deposit")
                page.screenshot(path=str(SHOTS / "01-no-card.png"), full_page=True)
            else:
                action = forms[0]
                fid = f'form[action="{action}"]'
                page.select_option(f"{fid} select[name=what]", "income")
                page.click(f"{fid} input[name=note]")
                page.keyboard.type(NOTE)
                now_note = page.input_value(f"{fid} input[name=note]")
                page.click(f"{fid} input[name=pin]")
                page.keyboard.type(PIN)
                now_pin = page.input_value(f"{fid} input[name=pin]")
                # NEVER PRINT THE PIN: report only whether it landed.
                print(f"immediately after typing: note={now_note!r} "
                      f"pin_landed={now_pin == PIN}")
                if now_note != NOTE:
                    problems.append("the note did not receive the keystrokes")
                if now_pin != PIN:
                    problems.append("the PIN did not receive the keystrokes")

                page.wait_for_timeout(6000)                      # past a poll
                later_note = page.input_value(f"{fid} input[name=note]")
                later_pin = page.input_value(f"{fid} input[name=pin]")
                print(f"after 6s: note={later_note!r} "
                      f"pin_still_there={later_pin == PIN}")
                if later_note != NOTE:
                    problems.append("something cleared the note after typing")
                if later_pin != PIN:
                    problems.append("something cleared the PIN after typing")

                page.screenshot(path=str(SHOTS / "02-typed.png"), full_page=True)
                page.click(f"{fid} button[type=submit]")
                page.wait_for_timeout(3500)
                after = " ".join(page.inner_text("body").split())
                page.screenshot(path=str(SHOTS / "03-after-submit.png"),
                                full_page=True)
                print("reply: " + after[:220])
                if "released" not in after.lower():
                    problems.append("the release did not report success")
                time.sleep(0.2)
        finally:
            ctx.close()
            b.close()

    print("\nconsole/network complaints:")
    for line in console[-12:]:
        print("   ", line)
    print("\nRESULT:", "PASS" if not problems else "FAIL")
    for p_ in problems:
        print("  -", p_)
    print(f"screenshots in {SHOTS}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
