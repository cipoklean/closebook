"""Sweep every page at 1280 and 390 and count em dashes in the VISIBLE text.

Runs against the tester. Counts what a person reads: tags stripped, entities
decoded, folded `<details>` opened first so their text counts too.

  python scripts/sweep_dashes.py
"""
from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8020"
EM, EN = "\u2014", "\u2013"
PAGES = [("/", f"{BASE}/")] + [
    (t, f"{BASE}/app?tab={t}") for t in
    ("home", "money-in", "money-out", "ok", "ai", "setup")
]

COUNT_JS = """(() => {
  document.querySelectorAll('details').forEach(d => { d.open = true; });
  const t = document.body.innerText;
  const em = (t.match(/\\u2014/g) || []).length;
  let enBad = 0;
  for (let i = 0; i < t.length; i++) {
    if (t[i] !== '\\u2013') continue;
    const a = t[i-1] || '', b = t[i+1] || '';
    if (!(/[0-9]/.test(a) && /[0-9]/.test(b))) enBad++;
  }
  return {em: em, en: enBad, chars: t.length};
})()"""


def main() -> int:
    bad = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, url in PAGES:
            for width, height in ((1280, 900), (390, 844)):
                page = browser.new_page(viewport={"width": width, "height": height})
                page.goto(url, wait_until="load", timeout=60000)
                got = page.evaluate(COUNT_JS)
                flag = "" if got["em"] == 0 and got["en"] == 0 else "   <-- DASH"
                bad += got["em"] + got["en"]
                print(f"  {name:<10} {width:>4}px  em={got['em']}  "
                      f"en-as-punctuation={got['en']}  chars={got['chars']}{flag}")
                page.close()
        browser.close()
    print(f"\n  total dashes found: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
