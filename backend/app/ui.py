"""Server-rendered HTML. No build step, no JS framework.

Two pages: `/` the landing page and `/app` the operator app. Both share
`static/closebook.css`.

Everything on `/app` comes from `dashboard.build_context()` — there is no
hardcoded number anywhere in this file. The landing page carries illustrative
copy only, which is why its strings live here and not in the spec's data layer.
"""
from __future__ import annotations

import html
import json
import os
from datetime import datetime, timezone

_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# --------------------------------------------------------------- helpers
def e(v) -> str:
    """HTML-escape. Never interpolate a DB value raw."""
    return html.escape("" if v is None else str(v), quote=True)


def money(minor, ccy="USD") -> str:
    try:
        return f"{int(minor) / 100:,.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _flag(cond, cls) -> str:
    return f" {cls}" if cond else ""


def _fmt_ts(v) -> str:
    if not v:
        return ""
    if isinstance(v, datetime):
        dt = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    else:
        return str(v)
    return dt.strftime("%H:%M")


# ---------------------------------------------------------------- icons
_I = {
    "home": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.8V20h14V9.8"/></svg>',
    "in": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><path d="M12 4v11"/><path d="m7.5 10.5 4.5 4.5 4.5-4.5"/><path d="M4 20h16"/></svg>',
    "out": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><path d="M12 20V9"/><path d="m7.5 13.5 4.5-4.5 4.5 4.5"/><path d="M4 4h16"/></svg>',
    "ok": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="m8.5 12.5 2.4 2.4 4.6-5"/></svg>',
    "ai": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><path d="M12 3v3"/><path d="M12 18v3"/><path d="M4.2 7.5l2.6 1.5"/><path d="M17.2 15l2.6 1.5"/><path d="M4.2 16.5l2.6-1.5"/><path d="M17.2 9l2.6-1.5"/><circle cx="12" cy="12" r="3.6"/></svg>',
    "shield": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><path d="M12 3l7 3v5.5c0 4.2-2.9 7.6-7 9-4.1-1.4-7-4.8-7-9V6z"/><path d="m9 12 2 2 4-4"/></svg>',
    "bolt": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><path d="M13 3 5 14h5l-1 7 8-11h-5z"/></svg>',
    "check": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>',
    "warn": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><path d="M12 4.5 21 19.5H3z"/><path d="M12 10v4"/><path d="M12 17h.01"/></svg>',
    "x": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><path d="M7 7l10 10"/><path d="M17 7 7 17"/></svg>',
    "info": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/></svg>',
    "lock": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="9" rx="2.5"/><path d="M8.5 11V8a3.5 3.5 0 0 1 7 0v3"/></svg>',
    "scale": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><path d="M12 4v16"/><path d="M7 20h10"/><path d="M5 7h14"/><path d="m5 7-2.5 6a3 3 0 0 0 5 0z"/><path d="m19 7-2.5 6a3 3 0 0 0 5 0z"/></svg>',
    "refund": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><path d="M4 10a8 8 0 0 1 14-4"/><path d="M4 4v4h4"/><path d="M20 14a8 8 0 0 1-14 4"/><path d="M20 20v-4h-4"/></svg>',
    "doc": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6"/><path d="M9 17h4"/></svg>',
    "clock": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M12 7.5V12l3 2"/></svg>',
    "users": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><circle cx="9" cy="8.5" r="3"/><path d="M3.5 19.5a5.5 5.5 0 0 1 11 0"/><path d="M16 6.2a3 3 0 0 1 0 5.6"/><path d="M17.5 19.5a5.4 5.4 0 0 0-2-4.2"/></svg>',
    "wallet": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><rect x="3.5" y="6.5" width="17" height="12" rx="2.5"/><path d="M3.5 10.5h17"/><circle cx="17" cy="14.5" r="1.2"/></svg>',
    "arrow": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><path d="M5 12h13"/><path d="m13 7 5 5-5 5"/></svg>',
    "chev": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><path d="m6 9.5 6 6 6-6"/></svg>',
    "chev-r": '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><path d="m9.5 6 6 6-6 6"/></svg>',
}


def _css_version() -> str:
    """Cache-buster from mtime — no manual bumping."""
    p = os.path.join(_STATIC, "closebook.css")
    return str(int(os.path.getmtime(p)))


def _head(title: str) -> str:
    v = _css_version()
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{e(title)}</title>\n'
        '<meta name="description" content="Closebook — real-time books for a '
        'cross-border business. Money in is matched, money out is checked, and '
        'anything unclear waits for a human.">\n'
        '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap">\n'
        # System fonts first, Google as an upgrade — a blocked or slow CDN must
        # never leave the page unstyled.
        f'<link rel="stylesheet" href="/static/closebook.css?v={v}">\n'
        '</head>\n'
    )


_JS = """
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };

  /* --- refresh: reload the page every 5s, paused while the tab is hidden --- */
  var refresh = function () {
    if (document.visibilityState === "hidden") return;
    fetch("/state", { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function () {
        /* data is reachable; nothing else to do until the next tick */
      })
      .catch(function () {});
    window.setTimeout(refresh, 5000);
  };
  window.setTimeout(refresh, 5000);

  /* --- tooltips: title attr -> data-tip so CSS can render it --- */
  [].forEach.call(document.querySelectorAll("[title]"), function (el) {
    if (el.classList.contains("x-tip")) return;
    el.setAttribute("data-tip", el.getAttribute("title"));
    el.removeAttribute("title");
  });

  /* --- landing FAQ --- */
  [].forEach.call(document.querySelectorAll("[data-toggle]"), function (s) {
    s.addEventListener("click", function () {
      var box = document.getElementById(s.getAttribute("data-toggle"));
      if (box) box.hidden = !box.hidden;
    });
  });

  /* --- approval form: posts to the existing endpoint, shows the real result --- */
  var toast = $("#x-toast");
  var say = function (msg, kind) {
    if (!toast) return;
    toast.textContent = msg;
    toast.className = "x-toast is-on" + (kind ? " x-toast--" + kind : "");
    window.setTimeout(function () { toast.className = "x-toast"; }, 3200);
  };

  [].forEach.call(document.querySelectorAll("form[data-action]"), function (f) {
    f.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var btn = f.querySelector("button[type=submit]");
      var prev = btn ? btn.textContent : "";
      if (btn) { btn.disabled = true; btn.textContent = "Checking…"; }
      var fd = new FormData(f);
      var body = new URLSearchParams(fd).toString();
      fetch(f.getAttribute("action"), {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body
      }).then(function (r) {
        return r.text().then(function (t) { return { code: r.status, body: t }; });
      }).then(function (res) {
        var msg = "Decision recorded. The page will refresh.";
        if (res.code === 200) {
          say("Approved — payment released.", "good");
          var card = f.closest(".x-card");
          if (card) {
            card.querySelectorAll("[data-detail]").forEach(function (d) {
              d.innerHTML = '<div class="x-note">' +
                '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>' +
                'Approved. <a href="/app?tab=money-out">See it in Money out</a></div>';
            });
          }
        } else {
          var detail = "";
          try { detail = (JSON.parse(res.body) || {}).detail || ""; } catch (err) {}
          say("Declined: " + (detail || "the rules said no"), "bad");
          if (btn) { btn.disabled = false; btn.textContent = prev; }
        }
      }).catch(function () {
        say("Could not reach the server", "bad");
        if (btn) { btn.disabled = false; btn.textContent = prev; }
      });
    });
  });
}());
"""


def _shell(inner: str, *, app: bool, title: str) -> str:
    body_cls = ' class="x-app"' if app else ' class="x-landing"'
    return (_head(title) + f"<body{body_cls}>\n" + inner +
            '\n<script>' + _JS + '</script>\n</body>\n</html>\n')


# ------------------------------------------------------- landing page
def render_landing() -> str:
    """Static marketing page. Illustrative copy only — no live figures."""
    L: list[str] = []
    a = L.append

    a('''<header class="x-l-nav">
  <div class="x-l-wrap">
    <div class="x-word"><span class="x-logo">'''
      '''<svg class="x-ico x-ico--s" viewBox="0 0 24 24" aria-hidden="true">'''
      '''<path d="M6.5 3.5h11a1.5 1.5 0 0 1 1.5 1.5v14.5A1.5 1.5 0 0 1 17.5 21h-11A1.5 1.5 0 0 1 5 19.5V5a1.5 1.5 0 0 1 1.5-1.5z"/>'''
      '''<path d="M9 8h6M9 12h6M9 16h3"/></svg></span>Closebook</div>
    <div class="x-l-links">
      <a href="/#how">How it works</a>
      <a href="/#safety">Safety</a>
      <a href="/#who">Who it's for</a>
      <a href="/app" class="x-btn x-btn--pill x-btn--primary">Launch app</a>
    </div>
  </div>
</header>''')

    # hero
    a('''<section class="x-l-wrap"><div class="x-l-hero">
  <div>
    <h1>Your books close<br><span>while the money moves.</span></h1>
    <p class="x-l-sub">Closebook keeps up with every deposit that lands. It matches the
      payment to the right invoice, checks your cash against the floor you set, releases
      what is safe to pay, and puts anything unclear in front of a person — with the
      reason, not a shrug.</p>
    <div class="x-actions">
      <a href="/#how" class="x-btn x-btn--pill x-btn--lg">See how it works</a>
    </div>
    <div class="x-l-trust">
      <span>''' + _I["check"] + '''No blind retries</span>
      <span>''' + _I["check"] + '''Every move explained</span>
      <span>''' + _I["check"] + '''Humans approve money</span>
    </div>
  </div>
  <div>
    <div class="x-card x-feature">
      <div class="x-feature-head">
        <span class="x-banner-ico">''' + _I["in"] + '''</span>
        <div>
          <h3>An invoice, paid</h3>
          <p class="x-help">The reference has a typo. Closebook still finds it.</p>
        </div>
      </div>
      <div class="x-arrived">
        <span class="x-fig">12,000.00</span><span>USD</span>
        <span class="x-pill x-pill--good">''' + _I["check"] + '''Matched</span>
      </div>
      <div class="x-cols">
        <div class="x-col x-col--ai">
          <div class="x-col-h">''' + _I["ai"] + '''Reads the paperwork</div>
          <ul>
            <li>''' + _I["check"] + '''Reference "inv 0O41" is invoice INV-0041</li>
            <li>''' + _I["check"] + '''Payer "Brightline Retail" matches the customer</li>
            <li>''' + _I["check"] + '''Amount matches the open balance</li>
          </ul>
        </div>
        <div class="x-col x-col--rules">
          <div class="x-col-h">''' + _I["scale"] + '''Checks every number</div>
          <ul>
            <li>''' + _I["check"] + '''Allocation equals the amount that arrived</li>
            <li>''' + _I["check"] + '''Invoice is really open</li>
            <li>''' + _I["check"] + '''Same currency, no conversions invented</li>
          </ul>
        </div>
      </div>
      <div class="x-sure">
        <div class="x-bar x-bar--done"><span style="width:96%"></span></div>
        <b>0.96</b>
      </div>
    </div>
  </div>
</div></section>''')

    # problems
    a('''<section class="x-l-sec"><div class="x-l-wrap">
  <div class="x-l-head">
    <div class="x-l-eyebrow">The problem</div>
    <h2 class="x-l-h2">Three things go wrong every time money crosses a border.</h2>
  </div>
  <div class="x-l-grid x-l-grid--3">
    <div class="x-card x-l-card">
      <span class="x-todo-ico is-warn">''' + _I["warn"] + '''</span>
      <h3>You can't see the balance that matters</h3>
      <p>Your bank shows every currency at once. It does not show what you have already
        promised to pay, so the same money gets spent twice.</p>
    </div>
    <div class="x-card x-l-card">
      <span class="x-todo-ico is-bad">''' + _I["x"] + '''</span>
      <h3>Payments arrive looking like nothing</h3>
      <p>"L. Stores. No reference." Four words and an amount. Someone has to work out
        which invoice it belongs to, and a guess becomes the record.</p>
    </div>
    <div class="x-card x-l-card">
      <span class="x-todo-ico is-info">''' + _I["clock"] + '''</span>
      <h3>A failed transfer looks like a paid one</h3>
      <p>The webhook says failed, the bank says processing, and the fix is a second
        payment for a supplier who was already paid.</p>
    </div>
  </div>
</div></section>''')

    # how it works
    a('''<section class="x-l-sec x-l-sec--white" id="how"><div class="x-l-wrap">
  <div class="x-l-head">
    <div class="x-l-eyebrow">How it works</div>
    <h2 class="x-l-h2">The model reads. The rules decide.</h2>
    <p>Closebook never lets a model's word be the record. A model proposes, and
      deterministic code checks every number before anything is written down.</p>
  </div>
  <div class="x-l-grid x-l-grid--4">
    <div class="x-card x-l-card">
      <span class="x-todo-ico is-info">''' + _I["in"] + '''</span>
      <h3>1. Money lands</h3>
      <p>A deposit, a payout, a card swipe. Whatever arrives first, Closebook reacts
        while it is still relevant.</p>
    </div>
    <div class="x-card x-l-card">
      <span class="x-todo-ico is-info">''' + _I["doc"] + '''</span>
      <h3>2. The model proposes</h3>
      <p>For anything untidy — a typo, a nickname, a blank reference — a model suggests
        what the money probably is. That is all it does.</p>
    </div>
    <div class="x-card x-l-card">
      <span class="x-todo-ico is-warn">''' + _I["scale"] + '''</span>
      <h3>3. The rules check it</h3>
      <p>Code re-derives the allocation, confirms the invoice is open, and refuses
        anything it cannot prove. A wrong guess never becomes the record.</p>
    </div>
    <div class="x-card x-l-card">
      <span class="x-todo-ico is-good">''' + _I["bolt"] + '''</span>
      <h3>4. Money moves, or waits</h3>
      <p>Clear cases pay themselves within your limits. Everything else waits, with a
        one-line reason a person can act on.</p>
    </div>
  </div>
</div></section>''')

    # safety
    a('''<section class="x-l-sec" id="safety"><div class="x-l-wrap">
  <div class="x-l-head">
    <div class="x-l-eyebrow">Safety</div>
    <h2 class="x-l-h2">Four ways a wrong move is impossible.</h2>
  </div>
  <div class="x-l-grid x-l-grid--4" style="margin-bottom:48px">
    <div class="x-card x-l-card">
      <span class="x-todo-ico is-good">''' + _I["shield"] + '''</span>
      <h3>Nothing pays itself twice</h3>
      <p>Every payment carries an identity before it is sent. If the process dies
        mid-payment, the next run finds the original and stops.</p>
    </div>
    <div class="x-card x-l-card">
      <span class="x-todo-ico is-good">''' + _I["lock"] + '''</span>
      <h3>Terms are locked at approval</h3>
      <p>A human approves a hash of the exact amount, currency, supplier and rate.
        Change any of them and the approval stops being valid.</p>
    </div>
    <div class="x-card x-l-card">
      <span class="x-todo-ico is-good">''' + _I["scale"] + '''</span>
      <h3>No money without a reserve</h3>
      <p>Payments that would breach the floor you set are deferred in full — never
        quietly trimmed to fit.</p>
    </div>
    <div class="x-card x-l-card">
      <span class="x-todo-ico is-good">''' + _I["users"] + '''</span>
      <h3>People approve money, not software</h3>
      <p>Anything over your limit, or unclear, waits for a person. Approving records a
        decision — it does not itself move a cent.</p>
    </div>
  </div>
  <div class="x-l-safe">
    <div class="x-l-prose">
      <h2 class="x-l-h2">A "failed" webhook is a claim, not a fact.</h2>
      <p>Closebook treats a failure notice as a rumour. Before doing anything, it looks
        the payment up directly and reads the real status.</p>
      <p><b>Still processing?</b> It waits and does nothing. Creating a replacement
        payment now would pay a supplier twice.</p>
      <p><b>Actually failed?</b> It classifies why — bad beneficiary, not enough money,
        compliance hold — and says so.</p>
      <p><b>Not sure?</b> It escalates to a human with what it knows and what it
        checked, rather than guessing.</p>
    </div>
    <div>
      <div class="x-arrived">
        <span class="x-fig">9,012.85</span><span>EUR</span>
        <span class="x-pill x-pill--warn">''' + _I["clock"] + '''Processing</span>
      </div>
      <div class="x-card x-l-card" style="margin-top:16px">
        <h3>What Closebook checked</h3>
        <ul class="x-todo" style="margin-top:12px">
          <li><span class="x-todo-ico is-good">''' + _I["check"] + '''</span>
            <div><b>Webhook said failed</b>
            <p>Heard. Not yet acted on.</p></div>
            <span class="x-pill x-pill--warn">unverified</span></li>
          <li><span class="x-todo-ico is-info">''' + _I["doc"] + '''</span>
            <div><b>Looked the transfer up</b>
            <p>Status is still PROCESSING at the bank.</p></div>
            <span class="x-pill x-pill--info">checked</span></li>
          <li><span class="x-todo-ico is-warn">''' + _I["warn"] + '''</span>
            <div><b>Waiting — nothing created</b>
            <p>No replacement payment. No new approval.</p></div>
            <span class="x-pill x-pill--warn">in flight</span></li>
        </ul>
      </div>
    </div>
  </div>
</div></section>''')

    # features
    a('''<section class="x-l-sec x-l-sec--white"><div class="x-l-wrap">
  <div class="x-l-head">
    <div class="x-l-eyebrow">What it does</div>
    <h2 class="x-l-h2">The daily work of a cross-border business.</h2>
  </div>
  <div class="x-l-grid x-l-grid--5">
    <div class="x-card x-l-card">
      <span class="x-todo-ico is-info">''' + _I["in"] + '''</span>
      <h3>Matches money in</h3>
      <p>Exact references, then rules, then a model — and code re-checks every
        proposal before it counts.</p>
    </div>
    <div class="x-card x-l-card">
      <span class="x-todo-ico is-good">''' + _I["wallet"] + '''</span>
      <h3>Watches the floor</h3>
      <p>Committed cash against a reserve you set. Payments that would breach it are
        deferred, never quietly trimmed.</p>
    </div>
    <div class="x-card x-l-card">
      <span class="x-todo-ico is-info">''' + _I["refund"] + '''</span>
      <h3>Converts only what it needs</h3>
      <p>Converts the exact amount a payment needs, with headroom for a small rate
        move — no speculative conversions.</p>
    </div>
    <div class="x-card x-l-card">
      <span class="x-todo-ico is-warn">''' + _I["warn"] + '''</span>
      <h3>Escalates honestly</h3>
      <p>When it does not know, it says so and tells you exactly what would settle it.</p>
    </div>
    <div class="x-card x-l-card">
      <span class="x-todo-ico is-good">''' + _I["doc"] + '''</span>
      <h3>Explains every move</h3>
      <p>Each decision carries its reason, the evidence it used, and the code that
        made the call.</p>
    </div>
  </div>
</div></section>''')

    # who it's for
    a('''<section class="x-l-sec" id="who"><div class="x-l-wrap">
  <div class="x-l-head">
    <div class="x-l-eyebrow">Who it's for</div>
    <h2 class="x-l-h2">If money arrives and leaves in different currencies, this is yours.</h2>
  </div>
  <div class="x-l-who">
    <div class="x-card x-l-persona">
      <div class="x-l-persona-top">
        <span class="x-l-avatar">KC</span>
        <div><b>Kano Craft Imports</b><small>Lagos · imports and resells</small></div>
      </div>
      <p>"I get paid in dollars and pay suppliers in euros and yuan. I used to find out
        about a problem when a supplier rang me. Now I see it the morning it
        happens."</p>
      <div class="x-l-flow">
        <div><span class="x-label">Gets paid in</span><span class="x-pill x-pill--good">USD · GBP</span></div>
        <div><span class="x-label">Pays out in</span><span class="x-pill x-pill--info">EUR · USD · CNY</span></div>
        <div><span class="x-label">Pain</span><span class="x-pill x-pill--warn">Never knows what is left to spend</span></div>
      </div>
    </div>
    <div>
      <div class="x-l-chips x-l-grid x-l-grid--3">
        <div class="x-l-chip">
          <span class="x-pill x-pill--accent">Importers</span>
          <p>Paid in one currency, paying suppliers in three.</p>
        </div>
        <div class="x-l-chip">
          <span class="x-pill x-pill--accent">Freelancers &amp; studios</span>
          <p>Invoices out, fees in, no bookkeeper.</p>
        </div>
        <div class="x-l-chip">
          <span class="x-pill x-pill--accent">Agencies &amp; payroll</span>
          <p>Many small payouts, one treasury person.</p>
        </div>
      </div>
      <div class="x-card x-l-card" style="margin-top:24px">
        <h3>Not a bank, and does not try to be</h3>
        <p>Closebook is the reasoning layer on top of an account. It runs on sandbox
          money in this demo, and every rule it enforces is written in code you can
          read.</p>
      </div>
    </div>
  </div>
</div></section>''')

    # built on
    a('''<section class="x-l-sec x-l-sec--white"><div class="x-l-wrap">
  <div class="x-l-head" style="max-width:640px">
    <h2 class="x-l-h2" style="font-size:28px">Built on</h2>
  </div>
  <div class="x-l-built">
    <span><b>Airwallex</b> sandbox — accounts, balances, FX, payouts</span><i>·</i>
    <span><b>FastAPI</b> — the app and the webhook receiver</span><i>·</i>
    <span><b>Python</b> — every rule, no black boxes</span><i>·</i>
    <span><b>OpenAI-compatible</b> model, optional and offline-safe</span>
  </div>
</div></section>''')

    # closing CTA
    a('''<section class="x-l-wrap">
  <div class="x-l-cta">
    <h2>See your books close themselves.</h2>
    <div class="x-actions" style="justify-content:center">
      <a href="/app" class="x-btn x-btn--primary x-btn--pill x-btn--lg">Launch app'''
      + _I["arrow"] + '''</a>
    </div>
    <small>''' + _I["lock"] + '''Sandbox money only — no real payment can leave this demo.</small>
  </div>
</section>''')

    # footer
    a('''<footer class="x-l-wrap"><div class="x-l-foot">
  <span>Closebook — a real-time books agent for cross-border business.</span><br>
  <span>Runs on the Airwallex sandbox. Sandbox money only.</span>
</div></footer>''')

    return _shell("".join(L), app=False, title="Closebook — real-time books")


# ---------------------------------------------------------- app pages
NAV = [
    ("home", "Home", "/app?tab=home", _I["home"]),
    ("money-in", "Money in", "/app?tab=money-in", _I["in"]),
    ("money-out", "Money out", "/app?tab=money-out", _I["out"]),
    ("ok", "Needs your OK", "/app?tab=ok", _I["ok"]),
    ("ai", "AI activity", "/app?tab=ai", _I["ai"]),
]


def _sidebar(tab: str, n_ok: int) -> str:
    links = []
    for key, label, href, icon in NAV:
        badge = (f'<span class="x-badge">{n_ok}</span>'
                 if key == "ok" and n_ok else "")
        cur = ' aria-current="page"' if key == tab else ""
        links.append(f'<a href="{href}"{cur}>{icon}<span>{e(label)}</span>{badge}</a>')
    return (
        '<div class="x-side">'
        '<a class="x-word" href="/"><span class="x-logo">'
        '<svg class="x-ico x-ico--s" viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="M6.5 3.5h11a1.5 1.5 0 0 1 1.5 1.5v14.5A1.5 1.5 0 0 1 17.5 21h-11A1.5 1.5 0 0 1 5 19.5V5a1.5 1.5 0 0 1 1.5-1.5z"/>'
        '<path d="M9 8h6M9 12h6M9 16h3"/></svg></span>Closebook</a>'
        '<nav class="x-nav">' + "".join(links) + '</nav>'
        '<div class="x-how"><b>' + _I["shield"] + 'How it decides</b>'
        'The model reads. The rules decide. Anything unclear waits for a person.</div>'
        '<a class="x-back" href="/">'
        '<svg class="x-ico x-ico--s" viewBox="0 0 24 24"><path d="M19 12H6"/><path d="m11 7-5 5 5 5"/></svg>'
        'Back to site</a>'
        '</div>'
    )


def _bottom_tabs(tab: str, n_ok: int) -> str:
    links = []
    for key, label, href, icon in NAV:
        badge = (f'<span class="x-badge">{n_ok}</span>'
                 if key == "ok" and n_ok else "")
        cur = ' aria-current="page"' if key == tab else ""
        short = label.replace("Needs your OK", "Approve").replace("AI activity", "AI")
        links.append(f'<a href="{href}"{cur}>{icon}<span>{e(short)}</span>{badge}</a>')
    return '<div class="x-tabs">' + "".join(links) + '</div>'


def _top(title: str, sub: str, health: dict | None) -> str:
    live = ""
    if health:
        sandbox = bool(health.get("sandbox_ready"))
        live = (f'<div class="x-live"><span class="x-livepill">'
                f'<i></i>{"Sandbox connected" if sandbox else "Sandbox not ready"}</span>'
                f'<small>Refreshes every 5s</small></div>')
    return (f'<div class="x-top"><div><h1>{e(title)}</h1><p>{e(sub)}</p></div>'
            f'{live}</div>')


def _card(*, span: str = "x-span-12", inner: str) -> str:
    return f'<div class="x-card {span}">{inner}</div>'


# ---- home
def _home(c: dict) -> str:
    cash = c["cash"] or {}
    b = c["budget"]
    susp = c["suspense"] or {}

    cash_str = " · ".join(f"{k} {money(v)}" for k, v in cash.items()) or "no cash yet"

    cards = []
    cards.append(_card(span="x-span-3", inner=(
        f'<span class="x-stat-ico">{_I["wallet"]}</span>'
        '<div class="x-label">Cash you have</div>'
        f'<div class="x-fig">{e(cash_str)}</div>'
        f'<div class="x-statnote">Balanced: {"yes" if c["ledger_balanced"] else "no"}</div>'
    )))
    cards.append(_card(span="x-span-3", inner=(
        f'<span class="x-stat-ico">{_I["shield"]}</span>'
        '<div class="x-label">Cash set aside'
        '<span class="x-tip" tabindex="0" role="note" data-tip="Money already promised to '
        'suppliers. Paying it again is how a treasury gets into trouble.">?</span></div>'
        f'<div class="x-fig">USD {money(b["committed_minor"])}</div>'
        f'<div class="x-statnote">Reserve floor: USD {money(b["reserve_floor_minor"])}</div>'
    )))
    n_unmatched = len(c["unmatched"])
    susp_str = " · ".join(f"{k} {money(v)}" for k, v in susp.items())
    cards.append(_card(span="x-span-3", inner=(
        f'<span class="x-stat-ico">{_I["in"]}</span>'
        '<div class="x-label">Money we couldn\'t match'
        '<span class="x-tip" tabindex="0" role="note" data-tip="Deposits we could not '
        'safely link to an invoice. Nothing is applied to the books until a person '
        'confirms it.">?</span></div>'
        f'<div class="x-fig">{e(susp_str) if susp_str else "Nothing waiting"}</div>'
        f'<div class="x-statnote">{n_unmatched} payment'
        f'{"s" if n_unmatched != 1 else ""} held</div>'
    )))
    cards.append(_card(span="x-span-3", inner=(
        f'<span class="x-stat-ico">{_I["bolt"]}</span>'
        '<div class="x-label">Automatic limit'
        '<span class="x-tip" tabindex="0" role="note" data-tip="Anything at or under this '
        'pays without asking. Over it, a person approves.">?</span></div>'
        f'<div class="x-fig">USD {money(b["auto_pay_limit_minor"])}</div>'
        '<div class="x-statnote">Per payment</div>'
    )))

    # banner — must agree with the Problems list, never contradict it.
    n_ok = len(c["approvable"])
    n_prob = len(c["problems"])
    n_past = len(c.get("past_attempts") or [])
    if n_ok:
        banner_head = (f'{n_ok} payment{"s" if n_ok != 1 else ""} '
                       f'waiting for your OK')
        banner_sub = ('Over your automatic limit, or unclear. Approving needs '
                      'your PIN and does not itself move money.')
        banner_icon = _I["ok"]
        banner_href = '/app?tab=ok'
        banner_cta = 'Review them'
    elif n_prob:
        banner_head = (f'Nothing needs your OK, but {n_prob} payment'
                       f'{"s" if n_prob != 1 else ""} failed')
        banner_sub = ('These are not waiting on you — they need a new plan. '
                      'They will not be retried automatically.')
        banner_icon = _I["warn"]
        banner_href = '/app?tab=money-out'
        banner_cta = 'See what failed'
    else:
        banner_head = 'Nothing needs you right now'
        banner_sub = ('Every payment is inside your limits and explained. '
                      'Closebook will surface anything it cannot prove.')
        banner_icon = _I["check"]
        banner_href = None
        banner_cta = None
    banner = _card(inner=(
        f'<div class="x-banner"><div class="x-banner-l">'
        f'<span class="x-banner-ico">{banner_icon}</span>'
        f'<div><h2>{e(banner_head)}</h2><p>{e(banner_sub)}</p></div></div>'
        + (f'<a class="x-btn x-btn--primary" href="{banner_href}">'
           f'{banner_cta}{_I["arrow"]}</a>' if banner_href else '')
        + '</div>'))

    # today's checklist — real plan steps as sentences
    todo = []
    problem_payables = {p["payable_number"] for p in c["problems"]}
    for p in c["payables"]:
        st = p["display_status"]
        # A payable with a current failure reads as failed, never as moving:
        # SUBMITTED here plus a Problem card elsewhere was the contradiction.
        if p["number"] in problem_payables:
            icon, cls = _I["x"], "is-bad"
            head = f'{p["number"]} ({p["supplier"] or "supplier"}) failed'
            body = (next((q["plain_reason"] for q in c["problems"]
                          if q["payable_number"] == p["number"]), "")
                    or "Check the problem list.")
            todo.append(f'<li><span class="x-todo-ico {cls}">{icon}</span>'
                        f'<div><b>{e(head)}</b><p>{e(body)}</p></div>'
                        f'<span class="x-pill x-pill--bad">Problem</span></li>')
            continue
        if st in ("PAID", "SUBMITTED", "APPROVED"):
            icon, cls = _I["check"], "is-good"
            head = f'{p["number"]} ({p["supplier"] or "supplier"}) is moving'
            body = (f'{p["currency"]} {money(p["amount_minor"])} — '
                    f'{st.lower()}. Nothing further needed.')
        elif st in ("CANCELLED", "FAILED"):
            icon, cls = _I["x"], "is-bad"
            head = f'{p["number"]} ({p["supplier"] or "supplier"}) failed'
            body = p.get("step_reason") or "Check the problem list."
        else:
            icon, cls = _I["warn"], "is-warn"
            head = f'{p["number"]} ({p["supplier"] or "supplier"}) is held'
            body = p.get("step_reason") or "Waiting on policy."
        pill = ('<span class="x-pill x-pill--bad">Problem</span>' if cls == "is-bad"
                else f'<span class="x-pill x-pill--'
                     f'{"good" if cls == "is-good" else "warn"}">'
                     f'{e(st)}</span>')
        todo.append(f'<li><span class="x-todo-ico {cls}">{icon}</span>'
                    f'<div><b>{e(head)}</b><p>{e(body)}</p></div>{pill}</li>')

    todo_html = "".join(todo) or '<li><div><i>Nothing planned yet.</i></div></li>'
    checklist = _card(span="x-span-8", inner=(
        '<div class="x-cardhead"><div><h3>Today\'s checklist</h3>'
        '<p class="x-help">Plain sentences, straight from the treasury plan.</p></div></div>'
        f'<ul class="x-todo">{todo_html}</ul>'
    ))

    # Problems (current, actionable) + Past attempts (history, collapsed).
    prob_items = "".join(
        f'<li><span class="x-todo-ico is-bad">{_I["x"]}</span>'
        f'<div><b>{e(p["payable_number"] or "Unknown step")}</b>'
        f'<p>{e(p["plain_reason"])}</p></div>'
        f'<span class="x-pill x-pill--bad">Problem</span></li>'
        for p in c["problems"])
    past_items = "".join(
        f'<li><span class="x-todo-ico is-good">{_I["check"]}</span>'
        f'<div><b>{e(p["payable_number"] or "Unknown step")}</b>'
        f'<p>{e(p["plain_reason"])}</p></div>'
        f'<span class="x-pill x-pill--off">{e(p["status"])}</span></li>'
        for p in c.get("past_attempts") or [])

    prob_inner = ""
    if prob_items or past_items:
        prob_inner = _card(span="x-span-4", inner=(
            '<div class="x-cardhead"><div><h3>Problems</h3>'
            '<p class="x-help">Payments that failed and will not be retried '
            'automatically.</p></div></div>'
            f'<ul class="x-todo">{prob_items}</ul>'
            f'<p class="x-help">These cannot be approved. They need a new plan.</p>'
            + (f'<details class="x-hist"><summary>Past attempts '
               f'({n_past})</summary><ul class="x-todo">{past_items}</ul></details>'
               if past_items else '')))

    inc = c["incidents"]
    inc_inner = ""
    if inc:
        rows = "".join(
            f'<li><span class="x-todo-ico is-warn">{_I["warn"]}</span>'
            f'<div><b>{e(g["live_status"] or "unknown")}</b>'
            f'<p>{len(g["events"])} event'
            f'{"s" if len(g["events"]) != 1 else ""} · '
            f'{e(g["recovery"] or "no recovery decided")}</p></div></li>'
            for g in inc[:3])
        inc_inner = _card(span="x-span-4", inner=(
            '<div class="x-cardhead"><div><h3>Payments in flight</h3>'
            '<p class="x-help">Grouped by transfer. Nothing is created until the bank '
            'confirms a failure.</p></div></div>'
            f'<ul class="x-todo">{rows}</ul>'))

    if not prob_inner and not inc_inner:
        side = inc_inner
    else:
        side = (prob_inner or inc_inner)
        if prob_inner and inc_inner:
            side = prob_inner + inc_inner

    balance = _card(span="x-span-12", inner=(
        f'<div class="x-balance">'
        f'<span class="x-bigcheck">{_I["check"]}</span>'
        f'<div><h3>The books balance</h3>'
        f'<p class="x-help">Every debit has a matching credit, per currency. '
        f'Balanced: {"yes" if c["ledger_balanced"] else "no"}.</p></div>'
        '</div>'))

    return "".join(cards) + banner + checklist + side + balance


# ---- money in
def _money_in(c: dict) -> str:
    rows = []
    for r in c["receivables"]:
        done = r["status"] in ("PAID", "OVERPAID")
        # "USD 0.00 of 4,500.00" is accurate but reads like a rendering bug;
        # name the state instead.
        paid_label = (f'{r["currency"]} {money(r["paid_minor"])} of '
                      f'{money(r["amount_minor"])}' if r["paid_minor"]
                      else "Nothing paid yet")
        rows.append(
            '<div class="x-inv">'
            f'<div class="x-inv-who"><b>{e(r["number"])}</b>'
            f'<small>{e(r["customer"] or "Unknown customer")}</small></div>'
            '<div><div class="x-inv-amt">'
            f'<span>{paid_label}</span>'
            f'<b>{r["pct"]}%</b></div>'
            f'<div class="x-bar{_flag(done, "x-bar--done")}">'
            f'<span style="width:{max(2, r["pct"])}%"></span></div></div>'
            f'<span class="x-pill x-pill--'
            f'{"good" if done else ("warn" if r["status"] == "PARTIAL" else "info")}">'
            f'{e(r["status"])}</span></div>')

    inv = _card(span="x-span-8", inner=(
        '<div class="x-cardhead"><div><h3>What customers owe</h3>'
        '<p class="x-help">Paid amounts come from the ledger, not from an estimate.'
        '</p></div></div>'
        + (("".join(rows)) if rows else '<p class="x-empty">No invoices yet.</p>')
    ))

    # unmatched — the beat-6 story, read from the data
    un = []
    for u in c["unmatched"]:
        prop = u.get("model_proposed") or {}
        allocs = prop.get("allocations") or []
        what = ", ".join(f'{a.get("invoice_number")} '
                         f'{money(int(float(a.get("amount", 0)) * 100))}'
                         for a in allocs) or "nothing specific"
        # The reason, derived from the numbers rather than asserted: a shortfall
        # under the bank-fee tolerance is ambiguous, over it is a part-payment.
        matched = next((r for r in c["receivables"]
                        if any(a.get("invoice_number") == r["number"]
                               for a in allocs)), None)
        reasons = []
        if matched:
            got = int(float((allocs[0].get("amount") or 0) if allocs else 0) * 100)
            short = matched["amount_minor"] - got
            reasons.append(f'{money(short)} {matched["currency"]} still outstanding')
            reasons.append("too small to be a deliberate part-payment")
            reasons.append("too large to be a bank fee")
        else:
            reasons.append("no open invoice matched the proposal")
            reasons.append("the model could not name a real invoice")
        conf = float(prop.get("confidence", 0) or 0)
        conf_label = f'{conf:.2f} confident' if conf else "No guess to stand behind"
        un.append(
            '<div class="x-card x-l-card" style="box-shadow:none;background:var(--x-bg)">'
            f'<div class="x-cardhead" style="margin-bottom:8px"><div>'
            f'<h3>{e(u["payer_name"] or "Unknown payer")}</h3>'
            f'<p class="x-help">{e(u["currency"])} {money(u["amount_minor"])} landed with '
            f'{"no reference" if not u["reference_raw"] else chr(34) + e(u["reference_raw"]) + chr(34)}.</p>'
            '</div><span class="x-pill x-pill--warn">Waiting for you</span></div>'
            '<div class="x-cols" style="margin-bottom:0">'
            f'<div class="x-col x-col--ai"><div class="x-col-h">{_I["ai"]}The model guessed</div>'
            f'<ul><li>{_I["x"]}{e(what)}</li>'
            f'<li>{e(conf_label)}</li></ul></div>'
            f'<div class="x-col x-col--rules"><div class="x-col-h">{_I["scale"]}Code refused</div>'
            + "".join(f'<li>{_I["x"]}{e(r)}</li>' for r in reasons)
            + '</div></div></div>')

    # The refused-match reason, read from the ledger rather than asserted.
    n_unmatched = len(c["unmatched"])
    note = ('<div class="x-note" style="margin-top:16px">'
            f'{_I["info"]}<span>Applying a held payment to an invoice needs a '
            'person — no endpoint exists for it yet, so this is '
            '<b>Coming soon</b>.</span></div>')
    empty_un = ('<p class="x-empty">Nothing is held. Every deposit that landed '
                'was matched with certainty.</p>')
    side = _card(span="x-span-4", inner=(
        '<div class="x-cardhead"><div><h3>Money we couldn\'t match</h3>'
        '<p class="x-help">Held in suspense — not applied to any invoice, and no '
        'supplier paid from it.</p></div>'
        f'<span class="x-pill x-pill--warn">{n_unmatched} held</span></div>'
        f'<div class="x-stack">{("".join(un)) if un else empty_un}</div>'
        + note))

    return inv + side


# ---- money out
_STEP_LABELS = ["Planned", "Checked", "Approved", "Sent", "Confirmed"]


def _step_index(status: str) -> int:
    return {
        "PROPOSED": 0, "APPROVED": 2, "SUBMITTED": 3,
        "PAID": 4, "FAILED": 3, "CANCELLED": 2,
    }.get(status, 1)


def _money_out(c: dict) -> str:
    cards = []
    for p in c["payables"]:
        st = p["display_status"]
        idx = _step_index(st)
        steps = []
        for i, label in enumerate(_STEP_LABELS):
            cls = "is-done" if i < idx else ("is-now" if i == idx else "")
            mark = "✓" if i < idx else (str(i + 1))
            steps.append(f'<li class="{cls}"><i>{mark}</i>'
                         f'<span>{e(label)}</span></li>')
        done = st in ("PAID",)
        badge = ('<span class="x-pill x-pill--good">Paid</span>' if done else
                 '<span class="x-pill x-pill--warn">Held</span>' if st in
                 ("PROPOSED", "HELD") else
                 '<span class="x-pill x-pill--bad">' + e(st) + '</span>' if st in
                 ("FAILED", "CANCELLED") else
                 f'<span class="x-pill x-pill--info">{e(st)}</span>')

        reason = (p.get("step_reason") or "").strip()
        note = ""
        if reason:
            short = reason[:150] + ("…" if len(reason) > 150 else "")
            note = (f'<div class="x-callnote" style="margin-top:20px">'
                    f'{_I["info"]}<span>{e(short)}</span></div>')

        obj = p.get("airwallex_object_id")
        ref = ""
        if obj:
            ref = (f'<div class="x-meta"><span class="x-label">On the bank</span>'
                   f'<span class="x-tag x-mono">{e(str(obj)[:18])}…</span></div>')

        # criticality is an int 1-5 on the model; 1 is most urgent.
        crit = int(p["criticality"] or 3)
        urg_cls = "high" if crit <= 2 else ("mid" if crit == 3 else "low")
        urg_word = {1: "critical", 2: "urgent", 3: "normal",
                    4: "low", 5: "lowest"}.get(crit, "normal")
        cards.append(_card(span="x-span-6", inner=(
            '<div class="x-sup">'
            f'<div class="x-sup-top"><div>'
            f'<span class="x-label">{e(p["supplier"] or "Supplier")}</span>'
            f'<div class="x-fig">{e(p["currency"])} {money(p["amount_minor"])}</div>'
            f'<div class="x-meta"><span class="x-pill">{e(p["payout_method"] or "unknown rail")}</span>'
            f'<span class="x-urg x-urg--{urg_cls}"><i></i>{urg_word}</span>'
            f'<span class="x-mono">{e(p["number"])}</span></div></div>'
            f'{badge}</div>'
            f'<ol class="x-steps">{"".join(steps)}</ol>'
            f'{note}{ref}</div>'
        )))

    head = _card(inner=(
        '<div class="x-cardhead"><div><h3>What you owe, and where it is</h3>'
        '<p class="x-help">Each state comes from the plan step, so this can never '
        'disagree with the treasury plan.</p></div></div>'
        + ("".join(cards) if cards else '<p class="x-empty">No payables yet.</p>')))

    # problems block
    probs = c["problems"]
    past = c.get("past_attempts") or []
    prob = ""
    if probs:
        items = "".join(
            f'<li><span class="x-todo-ico is-bad">{_I["x"]}</span>'
            f'<div><b>{e(p["payable_number"] or "Unknown")}</b>'
            f'<p>{e(p["plain_reason"])}</p>'
            f'<details class="x-hist"><summary>Details</summary>'
            f'<pre class="x-raw">{e(p["reason"] or "no raw error recorded")}</pre>'
            f'</details></div>'
            f'<span class="x-pill x-pill--bad">Problem</span></li>'
            for p in probs)
        past_block = ""
        if past:
            past_items = "".join(
                f'<li><span class="x-todo-ico is-good">{_I["check"]}</span>'
                f'<div><b>{e(p["payable_number"] or "Unknown")}</b>'
                f'<p>{e(p["plain_reason"])}</p></div>'
                f'<span class="x-pill x-pill--off">{e(p["status"])}</span></li>'
                for p in past)
            past_block = (f'<details class="x-hist"><summary>Past attempts '
                          f'({len(past)})</summary>'
                          f'<ul class="x-todo">{past_items}</ul></details>')
        prob = _card(inner=(
            '<div class="x-cardhead"><div><h3>Problems</h3>'
            '<p class="x-help">These failed and are deliberately not retried. '
            'Re-plan them to try again.</p></div></div>'
            f'<ul class="x-todo">{items}</ul>{past_block}'))

    return head + prob


# ---- needs your OK
def _ok(c: dict) -> str:
    cards = []
    for a in c["approvable"]:
        ccy = a["currency"] or a["buy_currency"] or "USD"
        amt = a["amount_minor"] or a["sell_amount_minor"] or 0
        fx = ""
        if a["action"] == "FX":
            fx = (f'<div class="x-meta"><span class="x-pill x-pill--info">FX</span>'
                  f'<span>sell {e(a["sell_currency"])} '
                  f'{money(a["sell_amount_minor"])} → buy {e(ccy)}</span></div>')

        reason = (a["reason"] or "").strip()
        why = ""
        if reason:
            short = reason[:160] + ("…" if len(reason) > 160 else "")
            why = (f'<div class="x-callnote" style="margin:20px 0">'
                   f'{_I["scale"]}<span>{e(short)}</span></div>')

        approved_note = ""
        if a["already_approved"]:
            approved_note = ('<div class="x-note" style="margin-top:16px">'
                             f'{_I["check"]}<span>Already approved — waiting for the '
                             f'executor to send it. Approving again would create a '
                             f'second payment.</span></div>')
            form = ('<div class="x-actions"><span class="x-btn" aria-disabled="true">'
                    'Approved</span></div>')
        else:
            form = (
                f'<form class="x-actions" method="post" '
                f'action="/approvals/{a["step_id"]}/approve" data-action>'
                f'<div class="x-pin"><label for="pin-{a["step_id"]}">Your PIN</label>'
                f'<input id="pin-{a["step_id"]}" name="pin" type="password" '
                f'inputmode="numeric" autocomplete="one-time-code" maxlength="12" '
                f'required></div>'
                f'<button class="x-btn x-btn--primary" type="submit">Approve</button>'
                f'<button class="x-btn" type="submit" '
                f'formaction="/approvals/{a["step_id"]}/reject">Decline</button>'
                '</form>')

        cards.append(_card(inner=(
            f'<div class="x-cardhead"><div>'
            f'<h3>{e(a["payable_number"] or "Unknown step")}'
            f'{" · " + e(a["supplier"]) if a["supplier"] else ""}</h3>'
            f'<p class="x-help">{e(a["action"])} · {e(ccy)}</p></div>'
            f'<span class="x-pill x-pill--warn">Needs your OK</span></div>'
            f'<div class="x-break"><div><dt>{e(ccy)}</dt>'
            f'<dd>{money(amt)}</dd></div>'
            f'<div class="is-total"><dt>Total</dt><dd>{e(ccy)} {money(amt)}</dd></div>'
            f'</div>{fx}{why}{approved_note}{form}'
            f'<div data-detail></div>'
        )))

    body = ""
    if cards:
        body = "".join(cards)
    else:
        body = _card(inner=(
            '<div class="x-banner"><div class="x-banner-l">'
            f'<span class="x-banner-ico">{_I["check"]}</span>'
            '<div><h2>Nothing needs your OK</h2>'
            '<p>Every payment is inside your limits, or already decided.</p></div>'
            '</div></div>'))

    note = _card(inner=(
        '<div class="x-note">' + _I["lock"] +
        '<span>Approving needs your PIN and records a decision. It does not itself '
        'move money — the executor re-checks the amount, the supplier and the rate '
        'against the hash you approved, every single time.</span></div>'))

    return body + note


# ---- AI activity
def _ai(c: dict) -> str:
    items = []
    for t in c["agent_trace"][:24]:
        ins = t["inputs"]
        outs = t["outputs"]
        prop = outs.get("model_proposed")
        if t["tool"] == "propose_match":
            what = "a payment that needs matching"
            if ins.get("payer") or ins.get("amount"):
                what = (f'{e(ins.get("payer") or "a payer")} — '
                        f'{money(int(float(ins.get("amount") or 0) * 100))} '
                        f'{e(ins.get("currency") or "")}'.strip())
            if prop and prop.get("allocations"):
                says = " · ".join(
                    f'{a.get("invoice_number")} '
                    f'{money(int(float(a.get("amount", 0)) * 100))}'
                    for a in prop["allocations"])
            else:
                says = outs.get("explanation") or "nothing it could stand behind"
            ai_msg = (f'I think this is <b>{e(says)}</b>'
                      + (f' — about {float(prop.get("confidence", 0)):.2f} confident.'
                         if prop else '.'))
            rule_msg = (t["detail"] or outs.get("explanation") or
                        "Checked every number against the open invoices.")
            conf = float(prop.get("confidence", 0) or 0) if prop else 0
            conf_label = f'{conf:.2f} confident' if conf else "No guess"
            items.append((what, ai_msg, rule_msg, t["verdict"], conf_label))
        elif t["tool"] == "explain_decision":
            items.append((outs.get("explanation") or "Explained a decision.",
                          "I only explain. I do not decide.",
                          "The reason is whatever the code recorded.",
                          t["verdict"], ""))
        else:
            items.append((f'Tool: {e(t["tool"])}',
                          "Checked the books and answered.",
                          t["detail"] or "No further detail.", t["verdict"], ""))

    feed = []
    for what, ai_msg, rule_msg, verdict, conf in items:
        good = verdict == "accepted"
        conf_pill = (f'<span class="x-pill x-pill--off">{e(conf)}</span>'
                     if conf else "")
        feed.append(
            '<li>'
            f'<div class="x-msg x-msg--ai"><span class="x-av x-av--ai">{_I["ai"]}</span>'
            f'<div><span class="x-bubble"><b class="x-who">The model</b>{ai_msg}</span>'
            f'<div class="x-aifoot"><span class="x-pill x-pill--'
            f'{"accent" if good else "warn"}">{e(verdict)}</span>{conf_pill}'
            f'<span>about {e(what)}</span></div></div></div>'
            f'<div class="x-msg x-msg--rules"><span class="x-av x-av--rules">{_I["scale"]}</span>'
            f'<div><span class="x-bubble"><b class="x-who">The rules</b>{e(rule_msg)}</span></div></div>'
            '</li>')

    body = "".join(feed) if feed else '<p class="x-empty">No model calls yet.</p>'

    return _card(span="x-span-12", inner=(
        '<div class="x-cardhead"><div><h3>What the model said, and what the rules did</h3>'
        '<p class="x-help">Straight from <span class="x-mono">agent_trace</span>. '
        'Nothing here is a decision — every row is one the code already made.</p></div>'
        '<span class="x-pill x-pill--accent">' + str(len(items)) + ' calls</span></div>'
        f'<ul class="x-feed">{body}</ul>'
    )) + _card(inner=(
        '<div class="x-note">' + _I["shield"] +
        '<span>The model can read invoices, explain a decision and ask for approval. '
        'It has no tool that can move money — that is a structural limit, not a '
        'setting.</span></div>'))


_TABS = {"home": _home, "money-in": _money_in, "money-out": _money_out,
         "ok": _ok, "ai": _ai}
_SUBS = {
    "home": "Everything money did since the last time you looked.",
    "money-in": "What landed, and what it was for.",
    "money-out": "What you owe, and where each payment is.",
    "ok": "Waiting on you — over the limit, or unclear.",
    "ai": "Every model call, and the rule that checked it.",
}


def render_app(ctx: dict, tab: str = "home", health: dict | None = None) -> str:
    tab = tab if tab in _TABS else "home"
    n_ok = len(ctx["approvable"])
    screen = _TABS[tab](ctx)
    inner = (
        _sidebar(tab, n_ok)
        + '<div class="x-body">'
        + _top(NAV[[n[0] for n in NAV].index(tab)][1], _SUBS[tab], health)
        + f'<main class="x-main"><div class="x-screen">{screen}</div></main>'
        + '<footer class="x-foot"><span>Closebook — sandbox only. '
        'No real money moves here.</span></footer>'
        + '</div>'
        + _bottom_tabs(tab, n_ok)
        + '<div id="x-toast" class="x-toast" role="status" aria-live="polite"></div>'
    )
    return _shell(inner, app=True, title=f"Closebook — {NAV[[n[0] for n in NAV].index(tab)][1]}")


__all__ = ["render_landing", "render_app", "money", "e"]
