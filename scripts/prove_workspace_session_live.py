"""Live proof, on the tester, that a book stays a book for a whole session.

Run it against the tester instance (flag on):

    python scripts/prove_workspace_session_live.py https://<tester-host>
    python scripts/prove_workspace_session_live.py http://127.0.0.1:8020

WHAT IT PROVES, in order:

  1. `?ws=<suffix>` answers with a session cookie, and the page NAMES the book.
  2. A request carrying ONLY the cookie (no `?ws=` anywhere) is still that book:
     tabs, `/state/version`, and the landing page's link back to `/app`.
  3. A real browser clicks three tabs and every screenshot still says
     `Book: <suffix>` — the click-through, saved under /tmp/proof/.

Read-only: it writes nothing, presses no money control, and never touches the
shared book. The suffix is random per run so two runs cannot collide.
"""
from __future__ import annotations

import json
import random
import re
import string
import sys
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8020").rstrip("/")
SUFFIX = "live-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
OUT = Path("/tmp/proof") / "workspace-session"
OUT.mkdir(parents=True, exist_ok=True)

TABS = ("home", "money-in", "money-out")


def fetch(path: str, cookie: str = "") -> tuple[int, str, str, str]:
    """(status, body, set-cookie, x-closebook-workspace)."""
    req = urllib.request.Request(BASE + path)
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return (r.status, r.read().decode(), r.headers.get("set-cookie", ""),
                    r.headers.get("x-closebook-workspace", ""))
    except urllib.error.HTTPError as e:
        return (e.code, e.read().decode()[:200], e.headers.get("set-cookie", ""),
                e.headers.get("x-closebook-workspace", ""))


def text(html: str) -> str:
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", body).strip()


ok = True


def check(label: str, passed: bool, detail: str = "") -> None:
    global ok
    ok = ok and passed
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}"
          + (f"  {detail}" if detail else ""))


print(f"tester : {BASE}")
print(f"book   : {SUFFIX}\n")

print("[1] the way IN sets the cookie and the page names the book")
status, body, set_cookie, header = fetch(f"/app?ws={SUFFIX}")
cookie = ""
if set_cookie:
    jar = SimpleCookie()
    jar.load(set_cookie)
    for key, morsel in jar.items():
        cookie += f"{key}={morsel.value}"
check("HTTP 200", status == 200, f"got {status}")
check("a session cookie names the book", f"closebook_ws={SUFFIX}" in set_cookie,
      set_cookie.strip())
check("cookie is path-wide, SameSite=Lax, no Max-Age",
      "Path=/" in set_cookie and "SameSite=Lax" in set_cookie
      and "Max-Age" not in set_cookie)
check("the page says which book it is", f"Book: {SUFFIX}" in body)
check("the workspace header is set", header == SUFFIX, f"got {header!r}")

print("\n[2] everything AFTER the first request still resolves to that book")
status, body, _sc, header = fetch("/app?tab=home", cookie)
check("a page with no ?ws= is the same book", f"Book: {SUFFIX}" in body,
      f"header={header!r}")
for tab in TABS:
    _s, html, _c, _h = fetch(f"/app?tab={tab}", cookie)
    check(f"tab {tab} carries the book on its links",
          f'href="/app?tab={tab}&ws={SUFFIX}"' in html)
check("the state polls carry the book",
      f'fetch("/state/version?ws={SUFFIX}"' in body)
status, body_v, _c, header = fetch("/state/version", cookie)
check("/state/version resolves from the cookie", header == SUFFIX, f"got {header!r}")
status, landing, _c, header = fetch("/", cookie)
check("the landing page links back INTO the book",
      f'href="/app?ws={SUFFIX}"' in landing)
check("the shared book is not named by the landing page", "Book:" not in text(landing))

print("\n[3] a real browser clicks three tabs and stays put")
try:
    from playwright.sync_api import sync_playwright
except ImportError:                                     # pragma: no cover
    print("  playwright is not installed; skipping the browser half")
    sync_playwright = None

if sync_playwright is not None:
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:                        # noqa: BLE001
            print(f"  chromium is not installed ({exc}); skipping")
            browser = None
        if browser is not None:
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            errors: list[str] = []
            page.on("console", lambda m: errors.append(m.text)
                    if m.type == "error" else None)
            page.goto(f"{BASE}/app?ws={SUFFIX}", wait_until="load", timeout=60_000)
            shot = OUT / "00-first-open.png"
            page.screenshot(path=str(shot), full_page=True)
            for i, tab in enumerate(TABS, start=1):
                page.click(f'[data-testid="tab-{tab}"]')
                page.wait_for_load_state("load")
                label = page.inner_text('[data-testid="active-book"]').strip()
                shot = OUT / f"{i:02d}-{tab}.png"
                page.screenshot(path=str(shot), full_page=True)
                check(f"clicked {tab}: still says Book: {SUFFIX}",
                      label == f"Book: {SUFFIX}", f"page says {label!r}")
                print(f"        screenshot {shot}")
            page.goto(f"{BASE}/app?tab=setup", wait_until="load", timeout=60_000)
            label = page.inner_text('[data-testid="active-book"]').strip()
            check("a fresh navigation with no ?ws= is still the book",
                  label == f"Book: {SUFFIX}", f"page says {label!r}")
            check("no console errors while clicking", not errors, str(errors[:3]))
            browser.close()

print("\nRESULT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
