"""Screenshots of the tester at 1280 and 390, with the folds open.

The browser tool's screenshot daemon wedges on this box (`Page.captureScreenshot`
times out after 5s regardless of the timeout passed), so this drives the
Playwright chromium that is already installed for the e2e suite.
"""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8020"
BILL = sys.argv[1] if len(sys.argv) > 1 else "PO-R44W941"
OUT = "/tmp/proof/r4"

PAGES = [
    ("money-out", f"{BASE}/app?tab=money-out"),
    ("home", f"{BASE}/app?tab=home"),
]


with sync_playwright() as p:
    browser = p.chromium.launch()
    for label, url in PAGES:
        for width, height, tag in ((1280, 900, "1280"), (390, 844, "390")):
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(url, wait_until="load", timeout=60000)
            # The facts live inside folds; a screenshot of a closed fold shows
            # nothing, which is how a "verified" claim gets made about a state
            # nobody looked at.
            page.evaluate(
                "document.querySelectorAll('details').forEach(d => d.open = true)")
            page.wait_for_timeout(1200)
            path = f"{OUT}/{label}-{tag}.png"
            page.screenshot(path=path, full_page=True)
            body = page.inner_text("body")
            # The pill INSIDE the "On the bank" fold, not any pill on the page.
            state = page.evaluate("""(() => {
              const ls = [...document.querySelectorAll('.x-label')];
              const lab = ls.find(e => e.textContent.trim() === 'On the bank');
              if (!lab) return '(none)';
              const pill = lab.parentElement.querySelector('.x-pill');
              return pill ? pill.textContent.trim() : '(no pill)';
            })()""")
            print(f"{label}-{tag}: {path} | bill named: {BILL in body} | "
                  f"On the bank: {state}")
            page.close()
    browser.close()
