"""Deploy check on the TESTER: Home, the pause, the resume, and the two counts.

Drives the real pages at /app?ws=deploycheck: Home loads, Pause the AI pauses
WITHOUT asking for a PIN, Resume refuses without the PIN and works with it, and
the Approve tab and Home agree about how many things need a person.

Usage: .venv/bin/python scripts/deploy_check_pause.py [base] [book]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BACKEND = REPO / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

BASE = (sys.argv[1].rstrip("/") if len(sys.argv) > 1
        else "http://127.0.0.1:8020")
BOOK = sys.argv[2] if len(sys.argv) > 2 else "deploycheck"
# THE PIN IS NEVER WRITTEN DOWN HERE. It comes from the environment, like every
# other credential, so this file can be committed and read.
PIN = os.environ.get("DEMO_APPROVER_PIN") or ""
SHOTS = Path("/tmp/proof/deploy-5e77d8f")


def _seed_ambiguous() -> None:
    """One deposit nobody can place, through the app's own intake.

    Without it both counts are zero and "the two tabs agree" proves nothing.
    """
    os.environ.setdefault("CLOSEBOOK_WORKSPACE_ROOT",
                          str(REPO / "data" / "workspaces"))
    from app import workspaces
    from app.domain.intake import record_and_match

    with workspaces.book(BOOK).session_scope() as s:
        record_and_match(s, deposit_id=f"dep-{BOOK}-1", currency="USD",
                         amount_minor=123_400, payer_name="Unknown Factor Ltd",
                         reference="no invoice here at all", llm=False)


def main() -> int:
    from playwright.sync_api import sync_playwright

    if not PIN:
        print("DEMO_APPROVER_PIN is not set, so Resume cannot be checked")
        return 2

    _seed_ambiguous()
    SHOTS.mkdir(parents=True, exist_ok=True)
    bad: list[str] = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        for label, width in (("desktop", 1440), ("phone", 390)):
            ctx = b.new_context(viewport={"width": width,
                                          "height": 1400 if width < 500 else 1100})
            page = ctx.new_page()
            try:
                page.goto(f"{BASE}/app?ws={BOOK}&tab=home", wait_until="load")
                page.wait_for_timeout(1800)
                if page.locator('[data-testid="home-numbers"]').count() != 1:
                    bad.append(f"{label}: Home did not load its numbers")
                page.screenshot(path=str(SHOTS / f"home-{label}.png"),
                                full_page=True)

                if label == "desktop":
                    # ---- the counts must agree BEFORE anything else happens
                    home_n = page.inner_text('[data-testid="needs-you-count"]').strip()
                    page.goto(f"{BASE}/app?ws={BOOK}&tab=ok", wait_until="load")
                    page.wait_for_timeout(1200)
                    ok_txt = " ".join(page.inner_text("body").split())
                    import re

                    m = re.search(r"(\d+)\s+things?\s+needs?\s+your OK", ok_txt)
                    print("home needs-you:", home_n, "| ok tab:",
                          (m.group(0) if m else "no count found"))
                    if not m:
                        bad.append("the Approve tab does not say how many things "
                                   "need an OK")
                    elif m.group(1) != home_n:
                        bad.append(f"Home says {home_n} but the Approve tab says "
                                   f"{m.group(0)}")

                    def state() -> str:
                        page.goto(f"{BASE}/app?ws={BOOK}&tab=home",
                                  wait_until="load")
                        page.wait_for_timeout(1500)
                        return page.inner_text('[data-testid="ai-state"]').strip()

                    def click_pause() -> None:
                        page.click('[data-testid="ai-pause"]')
                        page.wait_for_timeout(2200)

                    def click_resume(pin: str) -> None:
                        # RE-NAVIGATE: the page polls and swaps the DOM, so a
                        # locator captured before the swap is detached by the
                        # time it is filled. Fresh load, then fill.
                        page.goto(f"{BASE}/app?ws={BOOK}&tab=home",
                                  wait_until="load")
                        page.wait_for_timeout(1200)
                        page.fill('[data-testid="ai-resume-pin"]', pin)
                        page.click('[data-testid="ai-resume"]')
                        page.wait_for_timeout(2400)

                    # normalise: the check must not depend on how the last run
                    # left the switch
                    if "paused" in state().lower():
                        click_resume(PIN)
                    before = state()
                    print("ai state:", before)
                    if "running" not in before.lower():
                        bad.append(f"the AI is not running to begin with: {before!r}")

                    # ---- pause, with no PIN anywhere near it
                    click_pause()
                    paused = state()
                    print("after Pause:", paused)
                    if "paused" not in paused.lower():
                        bad.append(f"Pause did not pause (state reads {paused!r})")
                    page.screenshot(path=str(SHOTS / "home-paused.png"),
                                    full_page=True)

                    # ---- resume refuses without the PIN, works with it
                    click_resume("000000")
                    if "paused" not in state().lower():
                        bad.append("Resume with a wrong PIN was accepted")
                    click_resume(PIN)
                    resumed = state()
                    print("after the right PIN:", resumed)
                    if "paused" in resumed.lower():
                        bad.append(f"the right PIN did not resume (state {resumed!r})")
                    page.screenshot(path=str(SHOTS / "home-resumed.png"),
                                    full_page=True)
            finally:
                ctx.close()
        b.close()

    print("\nRESULT:", "PASS" if not bad else "FAIL")
    for b_ in bad:
        print("  -", b_)
    print(f"screenshots in {SHOTS}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
