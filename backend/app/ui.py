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

# Read for display only — the DECISION is made in domain/matching.py and
# recorded in agent_trace. Importing the thresholds here lets the page name the
# real numbers without re-deriving the verdict.
from .domain.matching import _LLM_MAX_CONFIDENCE as _LLM_CAP
from .domain.policy import MATCH_CONFIDENCE_AUTO, MATCH_CONFIDENCE_ESCALATE
# ONE display formatter for every amount, so no view can invent its own shape
# ("3200 GBP" vs "USD 2,500.00" both appeared on this page).
from .money import money_str

# --------------------------------------------------------------- helpers
def e(v) -> str:
    """HTML-escape. Never interpolate a DB value raw."""
    return html.escape("" if v is None else str(v), quote=True)


def money(minor, ccy="USD") -> str:
    try:
        return f"{int(minor) / 100:,.2f}"
    except (TypeError, ValueError):
        return "0.00"


def blockers_html(p: dict) -> str:
    """EVERY reason this payable is not being paid, one line each.

    ONE source for all three tabs. A hold consumed one reason and printed it in
    three slightly different places; a payment blocked by two rules would then
    tell the reader to clear the first, watch nothing happen, and have no idea
    why. `plan_blockers` carries the planner's own list (dependency, floor,
    capacity) and the dashboard appends the cash-basis floor reason computed by
    the same code the tiles use.
    """
    items = [x for x in (p.get("plan_blockers") or []) if x]
    if not items:
        fallback = p.get("plan_reason") or p.get("step_reason")
        items = [fallback] if fallback else []
    if not items:
        return '<p>Held by policy. Open Money out for the full rule.</p>'
    if len(items) == 1:
        return f'<p>{e(items[0])}</p>'
    return ('<ul class="x-blockers">'
            + "".join(f'<li>{e(x)}</li>' for x in items)
            + '</ul>')


def _sum_by_currency(rows) -> dict:
    """Add up `{currency, amount_minor}` rows into one figure per currency."""
    out: dict = {}
    for r in rows or []:
        ccy = str(r.get("currency") or "")
        out[ccy] = out.get(ccy, 0) + int(r.get("amount_minor") or 0)
    return {k: v for k, v in out.items() if k}


def _shown(text) -> str:
    """What a stored trace or rules line may SHOW. See plain_words.render."""
    from .domain.plain_words import render

    return render(text)[0]


def _raw_of(text) -> str:
    """The machine's own words from a stored line, or "" when it is prose."""
    from .domain.plain_words import render

    return render(text)[1]


def _detail_toggle(raw: str, what: str = "Detail") -> str:
    """The raw text, folded away. Rendered only when there IS raw text."""
    if not raw:
        return ""
    return (f'<details class="x-hist"><summary>{e(what)}</summary>'
            f'<pre class="x-raw">{e(raw)}</pre></details>')


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

  /* --- one toast, used for every outcome -------------------------------- */
  var toast = $("#x-toast");
  var say = function (msg, kind) {
    if (!toast) return;
    toast.textContent = msg;
    toast.className = "x-toast is-on" + (kind ? " x-toast--" + kind : "");
    window.clearTimeout(say._t);
    say._t = window.setTimeout(function () { toast.className = "x-toast"; }, 8000);
  };

  /* --- is the reader typing? -------------------------------------------
     A refresh that replaces the page under someone's fingers loses what they
     typed. Any focused field, any field with a value, or a submit in flight
     stops the reload completely. */
  var busy = false;
  var readerIsTyping = function () {
    var a = document.activeElement;
    if (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return true;
    var fields = document.querySelectorAll("form input, form select, form textarea");
    for (var i = 0; i < fields.length; i++) {
      var f = fields[i];
      if (f.type === "password" && f.value) return true;
      if (f.value && f.type !== "hidden") return true;
    }
    return false;
  };

  /* --- new activity: OFFER a refresh, never TAKE one --------------------
     This page used to call window.location.reload() every 5 seconds. That
     rebuilds the whole DOM under whoever is reading it, so:
       * a visitor gets yanked back mid-scroll or mid-read;
       * any element a person, or an automated test, is about to click has
         become STALE between locating it and clicking it.
     So it no longer reloads on its own. It polls a cheap stamp and, if the
     book genuinely changed AND nobody is mid-task, shows a pill the reader can
     act on. An unrequested reload is never worth interrupting someone for. */
  var pill = $("#x-new");
  var stamp = null;
  var pending = false;

  var lastTouch = Date.now();
  ["pointerdown", "keydown", "wheel", "scroll", "input", "focusin", "touchstart"]
    .forEach(function (ev) {
      window.addEventListener(ev, function () { lastTouch = Date.now(); },
                            { capture: true, passive: true });
    });

  /* A form or a details panel being open counts as mid-task: refreshing would
     close it. `busy` covers a submit in flight. */
  var midTask = function () {
    if (busy || readerIsTyping()) return true;
    if (Date.now() - lastTouch < 10000) return true;
    if (document.querySelector("details[open]")) return true;
    if (document.querySelector("[aria-modal='true'], dialog[open], .x-modal")) return true;
    return false;
  };

  var poll = function () {
    if (document.visibilityState === "hidden") return;
    fetch("/state/version", { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.text() : null; })
      .then(function (v) {
        if (v === null || v === "") return;          // a failed poll changes nothing
        if (stamp === null) { stamp = v; return; }   // first tick is the baseline
        if (v !== stamp) { stamp = v; pending = true; }
        if (pending && pill && !midTask()) pill.hidden = false;
      })
      .catch(function () { /* the page is unchanged; say nothing */ });
  };
  window.setInterval(poll, 20000);
  poll();

  if (pill) {
    pill.addEventListener("click", function () { window.location.reload(); });
  }

  /* --- tooltips: title attr -> data-tip so CSS can render it ------------- */
  [].forEach.call(document.querySelectorAll("[title]"), function (el) {
    if (el.classList.contains("x-tip")) return;
    el.setAttribute("data-tip", el.getAttribute("title"));
    el.removeAttribute("title");
  });

  /* --- landing FAQ ------------------------------------------------------ */
  [].forEach.call(document.querySelectorAll("[data-toggle]"), function (s) {
    el_toggle(s);
  });
  function el_toggle(s) {
    s.addEventListener("click", function () {
      var box = document.getElementById(s.getAttribute("data-toggle"));
      if (box) box.hidden = !box.hidden;
    });
  }

  /* --- what a successful action does to the page -------------------------
     THE PAGE UPDATES ITSELF; it does not reload. A reload throws the reader
     back to the top of a page they were reading and makes every element a test
     has located stale, which is exactly the fault this replaced. So on success
     the card goes, the PIN is cleared, the counts and the Home figures are
     patched from the server's own numbers. */
  var patchTiles = function () {
    fetch("/state/tiles", { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (res) {
        if (!res || !res.ok) return;
        Object.keys(res.tiles || {}).forEach(function (k) {
          [].forEach.call(
            document.querySelectorAll('[data-figure="' + k + '"]'),
            function (el) { el.textContent = res.tiles[k]; });
        });
        var badges = document.querySelectorAll(
          '.x-nav .x-badge, .x-tabs .x-badge, [data-testid="needs-you-count"]');
        [].forEach.call(badges, function (b) {
          var n = res.needs_you;
          if (typeof n !== "number") return;
          if (n > 0) { b.textContent = String(n); }
          else { b.remove(); document.body.setAttribute("data-needs-you", "0"); }
        });
      })
      .catch(function () { /* the page keeps the numbers it has */ });
  };

  var settled = function (f, msg) {
    // Clear the PIN the moment it is spent: leaving it in a field on a card
    // that no longer needs it invites it to be typed somewhere it must not be.
    var p = f.querySelector("input[name=pin]");
    if (p) p.value = "";

    // The card goes, replaced by one line saying it is done. A card that stays
    // with its controls live looks like the action did not happen.
    var host = f.closest("[data-card]") || f.closest(".x-card");
    var note = document.createElement("div");
    note.className = "x-note x-note--done";
    note.setAttribute("data-testid", "done-note");
    note.textContent = "Done — this no longer needs you.";
    if (host && host.hasAttribute("data-card")) {
      host.innerHTML = "";
      host.appendChild(note);
      host.setAttribute("data-done", "1");
    } else {
      f.parentNode.insertBefore(note, f);
      f.remove();
    }
    patchTiles();
  };

  var showDetail = function (f, text) {
    // The machine's own words, kept for whoever has to report the problem, and
    // folded away so they help instead of shouting.
    var box = f.querySelector("[data-detail]");
    if (!box) return;
    box.innerHTML = "";
    var d = document.createElement("details");
    d.className = "x-hist";
    var sum = document.createElement("summary");
    sum.textContent = "Detail";
    var pre = document.createElement("pre");
    pre.className = "x-raw";
    pre.textContent = text;
    d.appendChild(sum); d.appendChild(pre);
    box.appendChild(d);
  };

  /* --- every form that acts on money ------------------------------------
     Posts, then reports the SERVER'S OWN SENTENCE. The endpoints answer JSON
     when the caller asks for it, because a fetch cannot see through a redirect:
     it follows it, lands on 200 HTML, and a client judging by status code calls
     a refusal a success. It also cannot hang: the request is aborted after 25s
     and the button always comes back. */
  [].forEach.call(document.querySelectorAll("form[data-action]"), function (f) {
    var pin = f.querySelector("input[name=pin]");
    var sel = f.querySelector("select[name=what]");
    var reason = f.querySelector("input[name=reason]");
    var note = f.querySelector("input[name=note]");

    /* Only the field the chosen action needs. A refund needs a reason; releasing
       money as income needs a note. Showing both for every choice invites the
       reader to fill in the one that will be ignored. */
    var sync = function () {
      if (!sel) return;
      var v = sel.value;
      if (reason) reason.closest(".x-field").hidden = (v !== "refund");
      if (note) note.closest(".x-field").hidden = (v !== "income");
    };
    if (sel) { sel.addEventListener("change", sync); sync(); }

    f.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (busy) { say("Still working on the last one.", "bad"); return; }
      busy = true;

      var btn = f.querySelector("button[type=submit]");
      var prev = btn ? btn.textContent : "";
      if (btn) { btn.disabled = true; btn.textContent = "Working…"; }

      var ctl = ("AbortController" in window) ? new AbortController() : null;
      var timer = ctl ? window.setTimeout(function () { ctl.abort(); }, 25000) : null;

      fetch(f.getAttribute("action"), {
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "Accept": "application/json"
        },
        body: new URLSearchParams(new FormData(f)).toString(),
        signal: ctl ? ctl.signal : undefined
      }).then(function (r) {
        return r.json().catch(function () { return {}; });
      }).then(function (res) {
        if (timer) window.clearTimeout(timer);
        // `ok` IS A BOOLEAN and `message` is the sentence. Deciding on the
        // sentence is what produced "The server did not say what happened" on
        // an approval that had SUCCEEDED — a successful action reported as
        // unknown is worse than a failure reported as one, because it invites
        // the operator to do it again.
        if (!res || typeof res.ok !== "boolean") {
          say("The server did not say what happened. Reload to see the books.",
              "bad");
          return;
        }
        if (res.ok) {
          say(res.message, "good");
          settled(f, res.message);
        } else {
          say(res.err || res.message || "That did not work.", "bad");
          if (res.detail) showDetail(f, res.detail);
        }
      }).catch(function (err) {
        if (timer) window.clearTimeout(timer);
        var aborted = err && err.name === "AbortError";
        /* Deliberately does NOT reuse the old mockup phrasing, which a test
           guards against: this is real copy and must read as its own thing. */
        say(aborted
            ? "That took too long, so it was stopped. Check the page before trying again."
            : "Could not reach the server. The books are unchanged.", "bad");
      }).then(function () {
        if (btn) { btn.disabled = false; btn.textContent = prev; }
        busy = false;
      });
    });
  });
}());
"""


#: The PUBLIC pages get this and only this. No reload, no polling: a landing
#: page that rebuilds itself under a reader is a page that loses them, and one
#: that rebuilds under a test is a page that fails its own test.
_LANDING_JS = """
(function () {
  "use strict";
  [].forEach.call(document.querySelectorAll("[title]"), function (el) {
    if (el.classList.contains("x-tip")) return;
    el.setAttribute("data-tip", el.getAttribute("title"));
    el.removeAttribute("title");
  });
  [].forEach.call(document.querySelectorAll("[data-toggle]"), function (s) {
    s.addEventListener("click", function () {
      var box = document.getElementById(s.getAttribute("data-toggle"));
      if (box) box.hidden = !box.hidden;
    });
  });
}());
"""


def _shell(inner: str, *, app: bool, title: str) -> str:
    """Wrap a page. THE TWO KINDS OF PAGE GET DIFFERENT SCRIPTS.

    `app` is not decoration: it decides whether the interactive script is
    included at all. The public pages used to receive the same script as the
    dashboard, which meant the landing page reloaded itself every five seconds.
    """
    body_cls = ' class="x-app"' if app else ' class="x-landing"'
    script = _JS if app else _LANDING_JS
    return (_head(title) + f"<body{body_cls}>\n" + inner +
            '\n<script>' + script + '</script>\n</body>\n</html>\n')


# ------------------------------------------------------- landing page
def landing_proof(stats: dict | None) -> dict:
    """Real figures for the landing page's one live proof.

    THE PAGE USED TO INVENT ITS EVIDENCE: a glowing quote from "Kano Craft
    Imports", a decorative "0.96", a "9,012.85 EUR Processing" chip. None of it
    was a fact about anything, and a reader who checked would find nothing
    behind any of it. A proof is only a proof if it comes from the running book.

    Everything here is counted from the same tables the app renders, and it is
    honest about being empty: a fresh workspace says so rather than showing a
    zero dressed up as a result.
    """
    s = dict(stats or {})
    deposits = int(s.get("deposits") or 0)
    by_rules = int(s.get("by_rules") or 0)
    by_model = int(s.get("by_model") or 0)
    return {
        "deposits": deposits,
        "by_rules": by_rules,
        "by_model": by_model,
        "invoices": int(s.get("invoices") or 0),
        "bills": int(s.get("bills") or 0),
        "balanced": bool(s.get("balanced")),
        "empty": deposits == 0,
        "ledger_rows": int(s.get("ledger_rows") or 0),
    }


def render_landing(stats: dict | None = None) -> str:
    """The marketing page. Short on purpose, and honest about its evidence.

    CUT DOWN. It carried six sections of tile grids — a problem list, a
    what-it-does grid, an audience grid, a testimonial and decorative figures —
    and the useful parts (how it works, safety, one real proof) were buried
    between them. A page that says one true thing four times is worse than one
    that says it once.
    """
    L: list[str] = []
    a = L.append
    pr = landing_proof(stats)

    a('''<header class="x-l-nav">
  <div class="x-l-wrap">
    <div class="x-word"><span class="x-logo">'''
      '''<svg class="x-ico x-ico--s" viewBox="0 0 24 24" aria-hidden="true">'''
      '''<path d="M6.5 3.5h11a1.5 1.5 0 0 1 1.5 1.5v14.5A1.5 1.5 0 0 1 17.5 21h-11A1.5 1.5 0 0 1 5 19.5V5a1.5 1.5 0 0 1 1.5-1.5z"/>'''
      '''<path d="M9 8h6M9 12h6M9 16h3"/></svg></span>Closebook</div>
    <div class="x-l-links">
      <a href="/#how" data-testid="how-it-works">How it works</a>
      <a href="/#safety" data-testid="safety">Safety</a>
    </div>
    <div class="x-l-cta">
      <a href="/app" class="x-btn x-btn--pill x-btn--primary"
         data-testid="launch-app">Launch app</a>
    </div>
  </div>
</header>''')

    # ---- hero ------------------------------------------------------------
    a('''<section class="x-l-wrap"><div class="x-l-hero">
  <div class="x-l-eyebrow">Real-time books for a cross-border business</div>
  <h1 class="x-l-h1">Every deposit matched.<br><em>Every payment checked.</em></h1>
  <p class="x-l-sub">Closebook keeps up with the money that lands, and decides what
  it can safely do with it. The model reads the messy parts. The rules hold the
  amounts, the limits and the reserve floor.</p>
  <div class="x-l-hero-cta">
    <a href="/app" class="x-btn x-btn--primary x-btn--pill x-btn--lg"
       data-testid="launch-app-hero">Launch app</a>
    <a href="/#how" class="x-btn x-btn--pill">See how it decides</a>
  </div>
</div></section>''')

    # ---- how it works ----------------------------------------------------
    a('''<section class="x-l-sec x-l-sec--white" id="how"><div class="x-l-wrap">
  <div class="x-l-head">
    <div class="x-l-eyebrow">How it works</div>
    <h2 class="x-l-h2">The model reads. The rules decide.</h2>
  </div>
  <div class="x-l-grid x-l-grid--3">''')
    for n, title, body in (
        ("01", "It reads the reference",
         "A deposit arrives with whatever the payer typed — a name, a fragment "
         "of an invoice number, a typo. The matcher tries the exact reading "
         "first, then a rule, then the model. Every proposal the model makes is "
         "re-checked in code before it can change anything."),
        ("02", "It decides what the money may do",
         "Matching an invoice is not permission to spend. Closebook holds a "
         "reserve floor, an automatic payment limit and a committed-cash "
         "position, and a payment that would breach any of them is deferred "
         "with the reason rather than quietly trimmed."),
        ("03", "It asks a person when unsure",
         "Anything below the confidence band is proposed, never applied. "
         "Overpayments, part-payments and unclear references go to a human with "
         "the evidence attached — and nothing a person has not approved can "
         "move."),
    ):
        a(f'''    <div class="x-l-card">
      <div class="x-l-num">{n}</div>
      <h3 class="x-l-h3">{title}</h3>
      <p>{body}</p>
    </div>''')
    a('''  </div>
</div></section>''')

    # ---- safety ----------------------------------------------------------
    a('''<section class="x-l-sec" id="safety"><div class="x-l-wrap">
  <div class="x-l-head">
    <div class="x-l-eyebrow">Safety</div>
    <h2 class="x-l-h2">Four ways a wrong move is impossible.</h2>
  </div>
  <div class="x-l-grid x-l-grid--4">''')
    for title, body in (
        ("Amounts are integers",
         "Money is held in minor units end to end, so a rounding error cannot "
         "become a payment that is a penny out."),
        ("Nothing is sent twice",
         "Every payment carries a reference recorded before it leaves. A crash "
         "mid-submission is resolved by looking the payment up, never by "
         "sending it again."),
        ("Approvals bind the terms",
         "An approval covers the amount, the currency, the supplier and the "
         "rate — all hashed. Change any one of them and the approval is void."),
        ("Nothing moves itself",
         "A model can propose. Only one module in the codebase is permitted to "
         "move money, and it re-checks every rule on the way out."),
    ):
        a(f'''    <div class="x-l-card">
      <h3 class="x-l-h3">{title}</h3>
      <p>{body}</p>
    </div>''')
    a('''  </div>
  <div class="x-l-safe">
    <div class="x-l-prose">
      <h2 class="x-l-h2">A &ldquo;failed&rdquo; webhook is a claim, not a fact.</h2>
      <p>When a bank says a transfer failed, Closebook does not act on the
      message. It goes and looks at the transfer first. A failure notice for a
      payment that is still in flight means <em>watch it</em>, not
      <em>send it again</em> — and a replacement is treated as a new payment
      with a new approval, because that is what it is.</p>
    </div>
  </div>
</div></section>''')

    # ---- the one live proof ---------------------------------------------
    if pr["empty"]:
        proof_body = ('<p class="x-l-proofnote">This workspace is empty, so there '
                      'is nothing to show yet. Launch the app and build it — the '
                      'figures here come from the same tables the app renders, '
                      'not from a fixture.</p>')
    else:
        proof_body = f'''<div class="x-l-stats">
      <div><span class="x-l-stat">{pr["deposits"]}</span>
        <small>deposit{"s" if pr["deposits"] != 1 else ""} recorded</small></div>
      <div><span class="x-l-stat">{pr["by_rules"]}</span>
        <small>settled by a rule alone</small></div>
      <div><span class="x-l-stat">{pr["by_model"]}</span>
        <small>read by the model, then re-checked</small></div>
      <div><span class="x-l-stat">{pr["invoices"]}</span>
        <small>invoice{"s" if pr["invoices"] != 1 else ""}</small></div>
      <div><span class="x-l-stat">{"yes" if pr["balanced"] else "no"}</span>
        <small>ledger balanced</small></div>
    </div>
    <p class="x-l-proofnote">Counted from this instance&rsquo;s own book just now
    — {pr["ledger_rows"]} ledger posting{"s" if pr["ledger_rows"] != 1 else ""}
    behind it. Nothing here is a sample.</p>'''
    a(f'''<section class="x-l-sec x-l-sec--white"><div class="x-l-wrap">
  <div class="x-l-head">
    <div class="x-l-eyebrow">Live from this book</div>
    <h2 class="x-l-h2">What the app has actually done.</h2>
  </div>
  <div class="x-l-proof">{proof_body}</div>
</div></section>''')

    # ---- footer ----------------------------------------------------------
    a('''<footer class="x-l-wrap x-l-foot">
  <div>
    <div class="x-word"><span class="x-logo">'''
      '''<svg class="x-ico x-ico--s" viewBox="0 0 24 24" aria-hidden="true">'''
      '''<path d="M6.5 3.5h11a1.5 1.5 0 0 1 1.5 1.5v14.5A1.5 1.5 0 0 1 17.5 21h-11A1.5 1.5 0 0 1 5 19.5V5a1.5 1.5 0 0 1 1.5-1.5z"/>'''
      '''<path d="M9 8h6M9 12h6M9 16h3"/></svg></span>Closebook</div>
    <p class="x-l-fine">Runs against the Airwallex sandbox. No real money moves.</p>
  </div>
  <div class="x-l-links">
    <a href="/#how">How it works</a>
    <a href="/#safety">Safety</a>
    <a href="/app">Launch app</a>
  </div>
</footer>''')

    return _shell("".join(L), app=False, title="Closebook — real-time books")


# ---------------------------------------------------------- app pages
NAV = [
    ("home", "Home", "/app?tab=home", _I["home"]),
    ("money-in", "Money in", "/app?tab=money-in", _I["in"]),
    ("money-out", "Money out", "/app?tab=money-out", _I["out"]),
    ("ok", "Needs your OK", "/app?tab=ok", _I["ok"]),
    ("ai", "AI activity", "/app?tab=ai", _I["ai"]),
    ("setup", "Setup", "/app?tab=setup", _I["out"]),
]


def _sidebar(tab: str, n_ok: int) -> str:
    links = []
    for key, label, href, icon in NAV:
        badge = (f'<span class="x-badge">{n_ok}</span>'
                 if key == "ok" and n_ok else "")
        cur = ' aria-current="page"' if key == tab else ""
        links.append(f'<a href="{href}"{cur} data-testid="tab-{key}">'
                     f'{icon}<span>{e(label)}</span>{badge}</a>')
    return (
        '<div class="x-side">'
        '<a class="x-word" href="/"><span class="x-logo">'
        '<svg class="x-ico x-ico--s" viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="M6.5 3.5h11a1.5 1.5 0 0 1 1.5 1.5v14.5A1.5 1.5 0 0 1 17.5 21h-11A1.5 1.5 0 0 1 5 19.5V5a1.5 1.5 0 0 1 1.5-1.5z"/>'
        '<path d="M9 8h6M9 12h6M9 16h3"/></svg></span>Closebook</a>'
        '<nav class="x-nav">' + "".join(links) + '</nav>'
        '<div class="x-how"><b>' + _I["shield"] + 'How it decides</b>'
        'The model reads. The rules decide. Anything unclear waits for a person.</div>'
        '<a class="x-back" href="/" data-testid="back-to-site">'
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
        links.append(f'<a href="{href}"{cur} data-testid="tab-mobile-{key}">'
                     f'{icon}<span>{e(short)}</span>{badge}</a>')
    return '<div class="x-tabs">' + "".join(links) + '</div>'


def _top(title: str, sub: str, health: dict | None) -> str:
    live = ""
    if health:
        sandbox = bool(health.get("sandbox_ready"))
        live = (f'<div class="x-live"><span class="x-livepill">'
                f'<i></i>{"Sandbox connected" if sandbox else "Sandbox not ready"}</span>'
                f'<small>Updates when the books change</small></div>')
    return (f'<div class="x-top"><div><h1>{e(title)}</h1><p>{e(sub)}</p></div>'
            f'{live}</div>')


def _card(*, span: str = "x-span-12", inner: str) -> str:
    return f'<div class="x-card {span}">{inner}</div>'


# ---- home
def needs_you(c: dict) -> int:
    """How many things are waiting on a human. ONE definition.

    Counted as distinct OBLIGATIONS — an approvable step and its payable are
    the same commitment, so summing them would double-count one decision —
    PLUS the deposits sitting in suspense waiting to be reviewed. It used to
    count only payments, so money waiting for a person was invisible in the
    one number the operator uses to decide whether to go and look.
    """
    obligations = {x.get("payable_number") for x in c["approvable"]}
    obligations |= {p["number"] for p in c.get("awaiting_approval") or []}
    obligations.discard(None)
    return len(obligations) + len(c.get("inflows_waiting") or [])


def tile_text(c: dict) -> dict:
    """What each Home figure SAYS. The render and the patch share this.

    An action that changes the books updates these figures in place; if it
    formatted them itself, the page and the server would drift apart the
    first time a format changed.
    """
    m = c["money"]
    b = c["budget"]
    bud_ccy = b["currency"]
    cash = c["cash"] or {}
    susp = c["suspense"] or {}
    owed = m.get("refunds_owed") or {}

    def join(d):
        return " · ".join(money_str(v, k) for k, v in sorted(d.items()))

    floor_txt = money_str(m["reserve_floor_minor"], m["reserve_applies_to"])
    shorts = dict(m["shortfall"])
    if shorts:
        bits = " and ".join(money_str(v, ccy) for ccy, v in sorted(shorts.items()))
        headline = f"Short by {bits} after promised payments and the {floor_txt} reserve floor"
    else:
        headline = (f"{money_str(m['headline_free_minor'], m['headline_currency'])} "
                    f"after promised payments and the {floor_txt} reserve floor")
    paid = m.get("paid_today") or []
    return {
        "cash": join(cash) or "no cash yet",
        "promised": join(m["promised"]) or "Nothing owed",
        "in_flight": join(m["in_flight"]) or "Nothing sent",
        "free_to_spend": headline,
        "unmatched": join(susp) or "Nothing waiting",
        "refunds_owed": join(owed),
        "paid_today": join(_sum_by_currency(paid)) if paid else "Nothing yet",
        "auto_pay_limit": money_str(b["auto_pay_limit_minor"], bud_ccy),
    }


def _home(c: dict) -> str:
    # ONE SOURCE for the tile text: the same function the patch endpoint uses,
    # so the screen and the server cannot drift apart on the same number.
    tt = tile_text(c)
    cash = c["cash"] or {}
    b = c["budget"]
    susp = c["suspense"] or {}

    cash_str = tt["cash"]

    cards = []
    m = c["money"]
    bud_ccy = b["currency"]

    # 1. Cash actually held, per currency.
    cards.append(_card(span="x-span-3", inner=(
        f'<span class="x-stat-ico">{_I["wallet"]}</span>'
        '<div class="x-label">Cash you have</div>'
        f'<div class="x-fig" data-figure="cash">{e(cash_str)}</div>'
        f'<div class="x-statnote">Ledger balance, per currency. Balanced: '
        f'{"yes" if c["ledger_balanced"] else "no"}.</div>'
        # Expected inflows are a FORECAST and never spendable. Say both, on
        # their own line: this figure cannot fund a payment, and a reader who
        # thinks otherwise will not understand why a payment was deferred.
        f'<div class="x-statnote">Forecast: {e(money_str(b["forecast_minor"], bud_ccy))} '
        f'invoiced and expected, not landed. Not spendable.</div>'
        f'<details class="x-hist"><summary>What is expected</summary><div>'
        + ("".join(
            f'<div>{e(ln.get("number") or "invoice")} · '
            f'{e(money_str(int(ln.get("usd_minor") or 0), bud_ccy))}</div>'
            for ln in (b.get("forecast_lines") or []))
           or "<div>Nothing invoiced and outstanding.</div>")
        + '</div></details>'
    )))

    # 2. What we owe, in the currency that will PAY for it, excluding anything
    # already sent. "GBP 3,200.00", never "3200 GBP" — the project had both
    # shapes on screen at once and they read as different quantities.
    promised_str = tt["promised"]
    cards.append(_card(span="x-span-3", inner=(
        f'<span class="x-stat-ico">{_I["shield"]}</span>'
        '<div class="x-label">Promised to suppliers</div>'
        f'<div class="x-fig" data-figure="promised">{e(promised_str) if promised_str else "Nothing owed"}</div>'
        '<div class="x-statnote">Unpaid, not yet sent, costed in the currency '
        'that buys them. A part-payment does not release one.</div>'
    )))

    # 3. Sent but not settled. This money is committed and gone from the
    # spendable balance, so it must be neither dropped nor still called
    # "promised" — counted exactly once, here.
    inflight_str = tt["in_flight"]
    n_inflight = len([ln for ln in m["lines"] if ln["where"] == "in_flight"])
    inflight_note = (
        f'{n_inflight} sent to the bank and not settled yet — a sandbox '
        'transfer is not a completed payment. Counted once, here, not again as '
        'promised.')
    cards.append(_card(span="x-span-3", inner=(
        f'<span class="x-stat-ico">{_I["clock"]}</span>'
        '<div class="x-label">In flight</div>'
        f'<div class="x-fig" data-figure="in_flight">{e(inflight_str) if inflight_str else "Nothing sent"}</div>'
        f'<div class="x-statnote">{inflight_note}</div>'
    )))

    # 4. Free to spend — ONE honest headline in the budget currency, with the
    # per-currency working behind Details. The old tile answered per currency
    # and so reported a EUR shortfall for a EUR bill that USD was always going
    # to buy.
    hccy = m["headline_currency"]
    floor_txt = money_str(m["reserve_floor_minor"], m["reserve_applies_to"])
    shorts = dict(m["shortfall"])
    if shorts:
        # EVERY short currency, not just the budget one. A shortfall in a
        # currency we cannot fund is still a shortfall, and a page that reported
        # only the budget currency would look comfortable while an unfundable
        # payment sat in the list.
        bits = " and ".join(money_str(v, ccy) for ccy, v in sorted(shorts.items()))
        headline = f"Short by {bits}"
        who = m["breaching"][0] if m["breaching"] else None
        if who:
            headline += (f" — paying {who['payable_number']} "
                         f"({money_str(who['amount_minor'], who['currency'])}) "
                         f"would take cash below the {floor_txt} reserve floor")
        else:
            headline += (f" after promised payments and the {floor_txt} "
                         f"reserve floor")
        head_cls = ' style="color:var(--x-bad)"'
    else:
        headline = (f"{money_str(m['headline_free_minor'], hccy)} after promised "
                    f"payments and the {floor_txt} reserve floor")
        head_cls = ""
    # Working, per currency, so the headline number is checkable.
    owed = m.get("refunds_owed") or {}
    # THE WHOLE ARITHMETIC, per currency, so every tile can be checked by hand
    # against the one above it. The operator's formula:
    #   cash - promised - in flight - unattributed - owed back - floor
    work = "".join(
        f'<div>{e(ccy)}: cash {money(c["cash"].get(ccy, 0))} '
        f'− promised {money(m["promised"].get(ccy, 0))} '
        + (f' − in flight {money(m["in_flight"].get(ccy, 0))}'
           if m["in_flight"].get(ccy) else '')
        + (f' − unmatched {money(m["suspense"].get(ccy, 0))}'
           if m["suspense"].get(ccy) else '')
        + (f' − refunds owed {money(owed.get(ccy, 0))}' if owed.get(ccy) else '')
        + (f' − floor {money(m["reserve_floor_minor"])}' if ccy == hccy else '')
        + f' = {money(m["free"].get(ccy, 0))}</div>'
        for ccy in sorted(m["free"]))
    # A bill we cannot price is NOT a shortfall in its own currency — it is a
    # bill we have not yet decided how to fund. Said plainly, with the reason
    # the plan gave, instead of a made-up number in the wrong currency.
    unfunded_note = ""
    if m["unfunded"]:
        bits = "; ".join(f'{u["payable_number"]} — {u["note"]}'
                         for u in m["unfunded"])
        unfunded_note = (f'<div class="x-statnote" style="color:var(--x-bad)">'
                         f'Not costed here: {e(bits)}.</div>')
    cards.append(_card(span="x-span-3", inner=(
        f'<span class="x-stat-ico">{_I["bolt"]}</span>'
        '<div class="x-label">Free to spend</div>'
        f'<div class="x-fig"{head_cls} data-figure="free_to_spend">{e(headline)}</div>'
        + unfunded_note
        + f'<details class="x-hist"><summary>Details</summary><div>{work}</div>'
          + ("".join(
              f'<div>{e(ln["payable_number"])} · {e(ln["where"].replace("_", " "))} · '
              f'{e(money_str(ln["amount_minor"], ln["currency"]))}'
              + (f' · {e(ln["note"])}' if ln["note"] else '')
              + '</div>' for ln in m["lines"]) or '<div>Nothing outstanding.</div>')
          + '</details>'
    )))

    n_unmatched = len(c["unmatched"])
    susp_str = tt["unmatched"]
    cards.append(_card(span="x-span-3", inner=(
        f'<span class="x-stat-ico">{_I["in"]}</span>'
        '<div class="x-label">Unmatched money — not spendable</div>'
        f'<div class="x-fig" data-figure="unmatched">{e(susp_str) if susp_str else "Nothing waiting"}</div>'
        f'<div class="x-statnote">It is in the bank and it is not in the figure '
        f'above. {n_unmatched} payment'
        f'{"s" if n_unmatched != 1 else ""} waiting on you.</div>'
    )))

    # 5. Money we have decided is owed back. Real cash in the bank, and still not
    # ours to spend — so it comes off the same total, and it is shown separately
    # so the subtraction is visible rather than implied.
    owed_str = tt["refunds_owed"]
    if owed_str:
        owed_rows = "".join(
            f'<div>{e(a.get("payer") or "unknown payer")} · '
            f'{e(money_str(a.get("amount_minor", 0), a.get("currency", "")))}'
            f'{" · " + e(a.get("resolution_reason") or "") if a.get("resolution_reason") else ""}'
            f'</div>'
            for a in (c.get("arrivals") or [])
            if (a.get("review_state") or "") == "REFUND_OWED")
        cards.append(_card(span="x-span-3", inner=(
            f'<span class="x-stat-ico">{_I["warn"]}</span>'
            '<div class="x-label">Refunds owed</div>'
            f'<div class="x-fig" data-figure="refunds_owed">{e(owed_str)}</div>'
            '<div class="x-statnote">Recorded as a liability. No transfer has '
            'been sent, and this money is not spendable.</div>'
            f'<details class="x-hist"><summary>Details</summary>'
            f'<div>{owed_rows or "No detail."}</div></details>'
        )))

    # 5. Paid today — settled payouts, read from the ledger. An FX conversion is
    # deliberately absent: it moved money between our own currencies and paid
    # nobody, so listing it as a payment would overstate what went out.
    paid = m.get("paid_today") or []
    if paid:
        paid_rows = "".join(
            f'<div>{e(p["payable_number"])} · '
            f'{e(money_str(p["amount_minor"], p["currency"]))}'
            f'{" · " + e(str(p["posted_at"])[:19]) if p.get("posted_at") else ""}</div>'
            for p in paid)
        # THE AMOUNT, not a count. The label says "Paid today" and a reader
        # reads a number as money; "2 settled" made them do the addition
        # themselves, and the addition was the interesting part.
        paid_total = tt["paid_today"]
        paid_body = (f'<div class="x-fig" data-figure="paid_today">{e(paid_total)}</div>'
                     f'<div class="x-statnote">{len(paid)} settled payout'
                     f'{"s" if len(paid) != 1 else ""} since midnight UTC, from the '
                     f'ledger. An FX conversion is not here: it moved money '
                     f'between our own currencies and paid nobody.</div>'
                     f'<details class="x-hist"><summary>Details</summary>'
                     f'<div>{paid_rows}</div></details>')
    else:
        paid_body = ('<div class="x-fig" data-figure="paid_today">Nothing yet</div>'
                     '<div class="x-statnote">Settled payouts appear here once '
                     'the bank confirms them and the books record the outflow.</div>')
    cards.append(_card(span="x-span-3", inner=(
        f'<span class="x-stat-ico">{_I["out"]}</span>'
        '<div class="x-label">Paid today</div>' + paid_body
    )))

    cards.append(_card(span="x-span-3", inner=(
        f'<span class="x-stat-ico">{_I["scale"]}</span>'
        '<div class="x-label">Automatic limit</div>'
        f'<div class="x-fig" data-figure="auto_pay_limit">{e(tt["auto_pay_limit"])}</div>'
        f'<div class="x-statnote">Per payment. Anything over it needs a person. '
        f'Reserve floor {e(floor_txt)}.</div>'
    )))

    # banner — must agree with the Problems list, the held payments AND the
    # payables awaiting approval, never contradict any of them. "Nothing needs
    # you" above a held payment is the contradiction this replaced.
    #
    # Counted as distinct OBLIGATIONS: an approvable step and its payable are the
    # same commitment, so summing them would double-count one decision.
    n_ok = needs_you(c)
    n_prob = len(c["problems"])
    n_wait = len(c.get("waiting") or [])
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
        banner_sub = ('Nothing is retried on its own, and a replacement needs '
                      'your approval — re-plan and the ones that can go will '
                      'appear in Needs your OK.')
        banner_icon = _I["warn"]
        banner_href = '/app?tab=money-out'
        banner_cta = 'See what failed'
    elif n_wait:
        # Held, but NOT a human decision: quote the planner's own reason so the
        # header says what the payment is actually waiting for.
        first = c["waiting"][0]
        banner_head = (f'Nothing needs your OK — {n_wait} payment'
                       f'{"s" if n_wait != 1 else ""} on hold')
        banner_sub = (f'{first["number"]}: {first.get("plan_reason") or "held"}'
                      if first.get("plan_reason") else
                      'Held by policy, not waiting on a human.')
        banner_icon = _I["clock"]
        banner_href = '/app?tab=ok'
        banner_cta = 'See why'
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
            # Never say "paid" for a transfer we have merely sent. The bank's
            # status is the only thing that settles whether money moved.
            live = p.get("live_status")
            if st == "PAID":
                head = f'{p["number"]} ({p["supplier"] or "supplier"}) is paid'
                body = (f'{p["currency"]} {money(p["amount_minor"])} — settled '
                        f'in sandbox.')
                pill = '<span class="x-pill x-pill--good">Paid</span>'
            else:
                head = (f'{p["number"]} ({p["supplier"] or "supplier"}) '
                        f'is moving')
                body = (f'{p["currency"]} {money(p["amount_minor"])} sandbox '
                        f'transfer sent'
                        + (f' — the bank says {live}.' if live else '.'))
                pill = (f'<span class="x-pill x-pill--info">{e(live or st)}'
                        f'</span>')
            todo.append(f'<li><span class="x-todo-ico {cls}">{icon}</span>'
                        f'<div><b>{e(head)}</b><p>{e(body)}</p></div>{pill}</li>')
            continue
        elif st in ("CANCELLED", "FAILED"):
            icon, cls = _I["x"], "is-bad"
            head = f'{p["number"]} ({p["supplier"] or "supplier"}) failed'
            # `body` is HTML from here on (blockers_html below), so this branch
            # must produce HTML too — the shared append no longer escapes it.
            body = f'<p>{e(p.get("step_reason") or "Check the problem list.")}</p>'
        else:
            icon, cls = _I["warn"], "is-warn"
            head = f'{p["number"]} ({p["supplier"] or "supplier"}) is held'
            # NEVER "Waiting on policy." — a held payment owes the reader the
            # specific rule and its numbers (which invoice, how much, what
            # limit), and the planner already computed exactly that. ALL of
            # them, not the first.
            body = blockers_html(p)
        pill = ('<span class="x-pill x-pill--bad">Problem</span>' if cls == "is-bad"
                else f'<span class="x-pill x-pill--'
                     f'{"good" if cls == "is-good" else "warn"}">'
                     f'{e(st)}</span>')
        todo.append(f'<li><span class="x-todo-ico {cls}">{icon}</span>'
                    f'<div><b>{e(head)}</b>{body}</div>{pill}</li>')

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
            '<p class="x-help">A payment to a supplier cannot be sent twice, so '
            'nothing here is retried on its own. A replacement is a NEW payment '
            'with a new reference, and it needs your approval.</p></div></div>'
            f'<ul class="x-todo">{prob_items}</ul>'
            '<p class="x-help">Re-plan and any payment that can go again will '
            'appear in <b>Needs your OK</b> for you to approve.</p>'
            f'<form class="x-actions" method="post" action="/money-out/run" '
            f'data-action><button class="x-btn" type="submit">Re-plan and ask '
            f'for my OK</button></form>'
            + (f'<details class="x-hist"><summary>Past attempts '
               f'({len(c.get("past_attempts") or [])})</summary>'
               f'<ul class="x-todo">{past_items}</ul></details>'
               if past_items else '')))

    # Held payments, with the planner's own rule and numbers. A header that says
    # nothing needs the reader, directly above a held payment, is the mismatch
    # this section exists to close.
    wait_inner = ""
    for w in c.get("waiting") or []:
        wait_inner = _card(span="x-span-4", inner=(
            '<div class="x-cardhead"><div><h3>On hold</h3>'
            '<p class="x-help">Held by policy — not waiting on a person.</p>'
            '</div></div><ul class="x-todo">' + "".join(
                f'<li><span class="x-todo-ico is-warn">{_I["clock"]}</span>'
                f'<div id="held-{e(p["number"])}">'
                f'<b>{e(p["number"])} · {e(p["supplier"] or "supplier")}</b>'
                # THE reason, once, on the screen that shows the plan. Every
                # other mention links here rather than saying it again.
                f'{blockers_html(p)}</div>'
                f'<span class="x-pill x-pill--warn">'
                f'{e(p.get("plan_policy_reason") or "held")}</span></li>'
                for p in c["waiting"])
            + '</ul>'))

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

    # Compose the right-hand column from whatever exists, held payments included
    # so the header's claim is visible on the page beneath it.
    side = "".join(x for x in (prob_inner, wait_inner, inc_inner) if x)

    balance = _card(span="x-span-12", inner=(
        f'<div class="x-balance">'
        f'<span class="x-bigcheck">{_I["check"]}</span>'
        f'<div><h3>The books balance</h3>'
        f'<p class="x-help">Every debit has a matching credit, per currency. '
        f'Balanced: {"yes" if c["ledger_balanced"] else "no"}.</p></div>'
        '</div>'))

    return "".join(cards) + banner + checklist + side + balance



def _dev_controls(transfer_id: str, live: str) -> str:
    """Sandbox-only buttons that move an in-flight transfer to its END state.

    This is the ONLY way to make a transfer finish in the sandbox, and it is
    deliberately not a shortcut around anything: the button asks Airwallex to
    transition the transfer, then the app reads the status back OFF Airwallex
    (never from the form), runs the incident logic on what it finds, and posts
    the settlement. Repeating it is harmless — settlement is keyed to the
    Airwallex object id, so a second PAID writes nothing.

    Offered only while the bank says the transfer is still moving. There is
    nothing to simulate once it is terminal, and a button that cannot work is
    worse than no button.
    """
    # ONE lifecycle definition, shared with the route that handles the press.
    # Two copies would drift, and the failure mode is a button the API refuses.
    from .main import _dev_controls_are_offered

    if not _dev_controls_are_offered(live):
        return ""
    action = f"/dev/transfers/{html.escape(transfer_id, quote=True)}/transition"
    buttons = ""
    for value, label, title in (
            ("settle", "Settle", "Airwallex reports PAID"),
            ("fail", "Fail", "beneficiary details rejected"),
            ("return", "Return", "the beneficiary bank sent it back")):
        buttons += (
            f'<form method="post" action="{action}" style="display:inline">'
            f'<input type="hidden" name="what" value="{value}">'
            f'<button class="x-btn x-btn--sm" type="submit" title="{e(title)}">'
            f'{e(label)}</button></form>')
    return ('<div class="x-meta"><span class="x-label">Sandbox dev control</span>'
            f'<span class="x-pills">{buttons}</span></div>')



def _operator_name() -> str:
    """Whose name goes on the audit row. From settings, with a safe default."""
    try:
        from .config import get_settings
        return (get_settings().operator_name or "").strip() or "david"
    except Exception:                              # noqa: BLE001
        return "david"


def _resolve_form(a: dict) -> str:
    """ONE form, ONE PIN, and only the fields the chosen action needs.

    Two separate forms meant two "Your PIN" boxes on the same card, which reads
    as "type your PIN twice" and invites the reader to fill the wrong one. There
    is one decision to make here, so there is one control: pick what happens to
    the money, type your PIN once.

    The Match option is offered ONLY when the stored proposal still fits. A
    proposal is a snapshot: three payments can each name the same invoice at its
    full value, and the second one is no longer true. Offering a button that can
    only fail — or that would overpay an invoice — is worse than not offering it,
    so the page says why instead.
    """
    if not a.get("resolvable"):
        return ""
    iid = a["id"]
    fit = a.get("proposal_fit") or {}
    invs = [str(x) for x in (a.get("proposed_invoices") or []) if x]

    options = ""
    if invs and fit.get("ok"):
        options += (f'<option value="match">Match it to '
                    f'{e(" and ".join(invs))}</option>')
    options += (
        '<option value="refund">Refund owed — record the liability</option>'
        '<option value="income">Release as unallocated income</option>'
        '<option value="reject">Not this invoice — leave it unmatched</option>')

    blocked = ""
    if not (invs and fit.get("ok")) and a.get("no_match_why"):
        blocked = (f'<div class="x-note" style="margin-top:14px">{_I["warn"]}'
                   f'<span>{e(a["no_match_why"])}</span></div>')

    # "Resolving as" used to default to the literal string "dashboard", so every
    # audit row named a page rather than a person.
    who = e(a.get("resolving_as") or _operator_name())
    return (
        blocked +
        f'<form class="x-actions" method="post" '
        f'action="/inflows/{iid}/resolve" data-action>'
        f'<div class="x-field"><label for="what-{iid}">What happens to this '
        f'money?</label><select id="what-{iid}" name="what">{options}</select>'
        f'</div>'
        f'<div class="x-field" data-for="refund"><label for="reason-{iid}">'
        f'Reason <span class="x-req">required for a refund</span></label>'
        f'<input id="reason-{iid}" name="reason" maxlength="200" '
        f'placeholder="duplicate payment" autocomplete="off"></div>'
        f'<div class="x-field" data-for="income"><label for="note-{iid}">'
        f'Note <span class="x-req">required to release as income</span></label>'
        f'<input id="note-{iid}" name="note" maxlength="200" '
        f'placeholder="no invoice will ever claim this" autocomplete="off"></div>'
        f'<div class="x-field"><label for="by-{iid}">Resolving as</label>'
        f'<input id="by-{iid}" name="by" maxlength="64" value="{who}" '
        f'autocomplete="off"></div>'
        f'<div class="x-pin"><label for="pin-{iid}">Your PIN</label>'
        f'<input id="pin-{iid}" name="pin" type="password" inputmode="numeric" '
        f'autocomplete="one-time-code" maxlength="12" required></div>'
        f'<button class="x-btn x-btn--primary" type="submit">Resolve</button>'
        f'</form>')


def _reask_form(a: dict) -> str:
    """Ask the model again. NO PIN — it changes no balance and sends nothing.

    Requiring a PIN to retry a failed READING would teach people to type their
    PIN at things that do not move money, which is how a PIN stops meaning
    anything.
    """
    if not a.get("resolvable"):
        return ""
    return (f'<form class="x-actions" method="post" '
            f'action="/inflows/{a["id"]}/reask" data-action>'
            f'<button class="x-btn" type="submit">Ask the model again</button>'
            f'</form>')


def _resolution_audit(a: dict) -> str:
    """What was decided about a resolved payment, and by whom.

    Shown, not buried: releasing someone else's money as our own income is the
    decision most likely to be questioned later, and "who decided this" is the
    first thing anyone asks.
    """
    state = (a.get("review_state") or "").upper()
    if state == "REFUND_OWED":
        head = "Recorded as refunds owed"
    elif state == "RELEASED_INCOME":
        head = "Released as unallocated income"
    else:
        return ""
    who = a.get("resolved_by") or "unrecorded"
    when = (a.get("resolved_at") or "")[:19].replace("T", " ")
    why = a.get("resolution_reason") or "no reason recorded"
    return ('<div class="x-note" style="margin-top:16px">' + _I["info"]
            + f'<span><b>{e(head)}</b> — {e(why)}. Decided by '
            + f'<b>{e(who)}</b>{(" on " + e(when)) if when else ""}.</span></div>')


# ---- money in
def _simulate_card(c: dict) -> str:
    """The one place money comes IN on purpose, and it is sandbox-only.

    Every value comes from code: the accounts are what `GET /global_accounts`
    returned (cached for a minute), the amount is what was typed, and the
    reference is passed through UNCUT — a messy reference is the whole point,
    and "tidying" it in the view would hide the case the matcher has to handle.
    """
    accts = c.get("money_in_accounts") or []
    ready = c.get("sandbox_ready")
    if accts:
        opts = "".join(
            f'<option value="{e(a["id"])}"'
            + ("" if a.get("usable") else " disabled")
            + (f' title="{e(" · ".join(x for x in (a.get("country"), a.get("status")) if x))}"'
               if (a.get("country") or a.get("status")) else "")
            + '>'
            # "USD account …5888", not "USD · US · ACTIVE · 5888". The
            # operator is picking an ACCOUNT, and the country and status are
            # not what tells two of them apart -- the last four digits are.
            # Both stay available as a hover title, so nothing is lost.
            + _acct_label(a)
            + '</option>'
            for a in accts)
        usable = sum(1 for a in accts if a.get("usable"))
        acct_field = (f'<div class="x-field x-span-4">'
                      f'<label for="f-global_account_id">Global account</label>'
                      f'<select class="x-select" id="f-global_account_id" '
                      f'name="global_account_id">{opts}</select>'
                      f'<span class="x-hint">From GET /global_accounts'
                      f'{" — none are ACTIVE yet" if not usable else ""}. The '
                      f'deposit lands in the account currency.</span></div>')
    else:
        acct_field = ('<div class="x-field x-span-4">'
                      '<label>Global account</label>'
                      '<select class="x-select" disabled><option>none found'
                      '</option></select>'
                      '<span class="x-hint">No global account could be listed, '
                      'so a deposit cannot be simulated.</span></div>')

    body = (
        '<div class="x-cardhead"><div><h3>Simulate a customer payment</h3>'
        '<p class="x-help">Calls POST /api/v1/simulation/deposit/create on the '
        'Airwallex sandbox. It creates a real sandbox deposit — no real money — '
        'and it comes back through the same webhook and matcher as any other '
        'payment.</p></div>'
        f'<span class="x-pill x-pill--{"info" if ready else "bad"}">'
        f'{"sandbox" if ready else "sandbox not ready"}</span></div>'
        + '<form class="x-form" method="post" action="/money-in/simulate">'
        + acct_field
        + _field("amount", "Amount", span="x-span-2", placeholder="12000",
                 required=True)
        + _field("payer_name", "Payer name", span="x-span-3",
                 placeholder="Brightline Retail",
                 hint="What the bank says, not what you wish it said")
        + _field("reference", "Reference", span="x-span-3",
                 placeholder="inv 0O41 brightline",
                 hint="Free text. Typos welcome — that is the point.")
        + '<div class="x-submit">'
          '<button class="x-btn x-btn--primary" type="submit"'
          + ("" if ready else " disabled")
          + '>Create the deposit</button>'
          '<button class="x-btn" type="submit" formaction="/money-in/poll" '
          'formnovalidate>Check Airwallex for new deposits</button>'
          '<span class="x-hint">The poll button is the fallback for a webhook '
          'that is late. Both paths use the same intake, so neither can post the '
          'cash twice.</span></div></form>')
    return _card(inner=body)


def _acct_label(a: dict) -> str:
    """The one line that tells two global accounts apart."""
    ccy = e(a.get("currency") or "unknown")
    # A missing number is not the digit "-". Airwallex returns literal "-" for
    # an account that has no number yet, and slicing its last four characters
    # produced "HKD account …-" -- a wrong number on the row where the number is
    # the ONLY thing distinguishing two accounts.
    num = str(a.get("account_number") or "")
    last4 = num[-4:] if sum(c.isdigit() for c in num) >= 4 else ""
    label = f"{ccy} account" + (f" …{e(last4)}" if last4 else "")
    if a.get("usable") is False:
        label += " — not active yet"
    return label


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
                         f'{money(int(float(a.get("amount") or 0) * 100))}'
                         for a in allocs) or "nothing specific"
        # The reasons come from the decision code actually recorded in
        # agent_trace — NOT recomputed here. A view that re-derives policy is a
        # second source of truth, and it drifts from the rule that decided.
        decided = u.get("decided") or {}
        evidence = decided.get("evidence") or {}
        conf = float(prop.get("confidence", 0) or 0)
        # The model's number is ITS CLAIM, not the decision. Code's band is
        # shown separately and always wins; a model asserting 0.99 about itself
        # is exactly the thing this page must not present as a verdict.
        claimed = float(evidence.get("model_claimed_confidence") or 0)
        if conf:
            conf_label = f'The model claims {conf:.2f}'
        elif claimed:
            conf_label = f'The model claims {claimed:.2f}'
        else:
            conf_label = "No guess to stand behind"
        band = decided.get("band")
        band_label = {
            "AUTO": "Code: automatic",
            "APPROVAL": "Code: held for approval",
            "ESCALATE": "Code: escalated to a human",
        }.get(band, "")

        # What is still owed on the invoice this payment touched.
        still_line = ""
        for r in c["receivables"]:
            if r["number"] in (a.get("invoice_number") for a in allocs) \
                    and r["open_minor"] > 0:
                still_line = (f'{money(r["open_minor"])} {r["currency"]} of '
                              f'{r["number"]} is still outstanding')
                break

        recorded = (decided.get("reason") or "").strip()
        reasons: list[str] = []
        if recorded:
            # The rule's own words, including the shortfall maths it computed.
            reasons.append(recorded[0].upper() + recorded[1:])
            if decided.get("band") == "ESCALATE":
                reasons.append(
                    f'Below the {MATCH_CONFIDENCE_ESCALATE:.2f} line where a '
                    f'person must decide.')
            elif decided.get("band") == "APPROVAL":
                reasons.append(
                    f'Held for approval: code placed this in the approval band, '
                    f'not the automatic one.')
        else:
            # No recorded decision (an inflow from before the trace existed).
            # Say that. Inventing a reason here would be the exact failure this
            # whole change exists to stop.
            reasons.append(
                "No decision was recorded for this payment — re-run the demo "
                "beats to see why it was held.")
        if not u.get("reference_raw"):
            reasons.append("It arrived with no reference at all.")
        if still_line:
            reasons.append(still_line + ".")

        un.append(
            '<div class="x-card x-l-card" style="box-shadow:none;background:var(--x-bg)">'
            f'<div class="x-cardhead" style="margin-bottom:8px"><div>'
            f'<h3>{e(u["payer_name"] or "Unknown payer")}</h3>'
            f'<p class="x-help">{e(u["currency"])} {money(u["amount_minor"])} landed with '
            f'{"no reference" if not u["reference_raw"] else chr(34) + e(u["reference_raw"]) + chr(34)}.</p>'
            '</div><span class="x-pill x-pill--warn">Waiting for you</span></div>'
            '<div class="x-cols" style="margin-bottom:0">'
            f'<div class="x-col x-col--ai"><div class="x-col-h">{_I["ai"]}The model proposed</div>'
            f'<ul><li>{_I["x"]}{e(what)}</li>'
            f'<li>{e(conf_label)}</li></ul></div>'
            f'<div class="x-col x-col--rules"><div class="x-col-h">{_I["scale"]}Code decided</div>'
            + (f'<li>{_I["scale"]}{e(band_label)}</li>' if band_label else "")
            + "".join(f'<li>{_I["x"]}{e(r)}</li>' for r in reasons)
            + '</div></div></div>')

    # The refused-match reason, read from the ledger rather than asserted.
    n_unmatched = len(c["unmatched"])
    # NO "COMING SOON". Resolving exists and is on the Needs your OK tab;
    # telling the operator a working feature is unbuilt sends them away from
    # the screen that would have answered them.
    note = ('<div class="x-note" style="margin-top:16px">'
            f'{_I["info"]}<span>Applying a held payment to an invoice needs a '
            'person — it is on <b>Needs your OK</b>, where you can match it, '
            'record a refund owed, or release it as income.</span></div>')
    empty_un = ('<p class="x-empty">Nothing is held. Every deposit that landed '
                'was matched with certainty.</p>')
    side = _card(span="x-span-4", inner=(
        '<div class="x-cardhead"><div><h3>Money we couldn\'t match</h3>'
        '<p class="x-help">Unmatched — not applied to any invoice, and no '
        'supplier paid from it.</p></div>'
        f'<span class="x-pill x-pill--warn">{n_unmatched} held</span></div>'
        f'<div class="x-stack">{("".join(un)) if un else empty_un}</div>'
        + note))

    return _simulate_card(c) + _arrivals_card(c) + inv + side


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
        done = st == "PAID"
        live = p.get("live_status")
        # The badge reports the BANK. A submitted transfer is not a paid one,
        # and in this project every transfer is sandbox money.
        if live in ("PAID", "COMPLETED", "SUCCESS"):
            badge = '<span class="x-pill x-pill--good">Paid</span>'
        elif live:
            badge = f'<span class="x-pill x-pill--info">{e(live)}</span>'
        elif st in ("CANCELLED", "FAILED"):
            badge = f'<span class="x-pill x-pill--bad">{e(st)}</span>'
        elif st in ("PROPOSED", "HELD"):
            badge = '<span class="x-pill x-pill--warn">Held</span>'
        else:
            badge = f'<span class="x-pill x-pill--info">{e(st)}</span>'

        # WHY it is where it is. A step reason is what actually happened; a
        # payable with no step yet has only the planner's rules — and those name
        # the invoice, the amount and the limit, so they are the useful half.
        # "Held" on its own tells a reader nothing they can act on. ALL the
        # rules, not the first.
        if p.get("plan_blockers"):
            # The rules are stated ONCE, on Home, next to the plan they
            # explain. Here the reader gets the verdict and a way to the why.
            note = (f'<div class="x-callnote" style="margin-top:20px">'
                    f'{_I["info"]}<div><p>Held by policy. '
                    f'<a href="/app?tab=home#held-{e(p["number"])}">'
                    f'Why it is held</a></p></div></div>')
        else:
            reason = (p.get("step_reason") or "").strip()
            if not reason:
                reason = (p.get("plan_reason") or "").strip()
            note = ""
            if reason:
                short = reason[:220] + ("…" if len(reason) > 220 else "")
                note = (f'<div class="x-callnote" style="margin-top:20px">'
                        f'{_I["info"]}<span>{e(short)}</span></div>')

        # Say what the BANK says. "SUBMITTED" is what we did; it is not a claim
        # that money moved, and never render it as "paid".
        obj = p.get("airwallex_object_id")
        ref = ""
        if obj:
            live = p.get("live_status") or "UNKNOWN"
            # The bank's own identifier is a 36-character UUID and the
            # environment is the same word on every card. Both are needed when
            # something goes wrong and neither is worth a line on every row, so
            # the status stays visible (it is the answer) and the reference and
            # the environment fold away behind it.
            ref = (f'<details class="x-hist x-hist--bank" data-testid="bank-ref">'
                   f'<summary><span class="x-label">On the bank</span>'
                   f'<span class="x-pill x-pill--'
                   f'{"good" if live in ("PAID", "COMPLETED", "SUCCESS") else ("warn" if live in ("PROCESSING", "PENDING", "SUBMITTED") else "info")}"'
                   f'>{e(live)}</span></summary>'
                   f'<div class="x-meta"><span class="x-label">Reference</span>'
                   f'<span class="x-tag x-mono">{e(str(obj))}</span></div>'
                   f'<div class="x-meta"><span class="x-label">Environment</span>'
                   f'<span class="x-tag">sandbox</span></div>'
                   f'</details>'
                   f'{_dev_controls(str(obj), live)}')

        # criticality is an int 1-5 on the model; 1 is most urgent.
        crit = int(p["criticality"] or 3)
        urg_cls = "high" if crit <= 2 else ("mid" if crit == 3 else "low")
        urg_word = {1: "critical", 2: "urgent", 3: "normal",
                    4: "low", 5: "lowest"}.get(crit, "normal")
        cards.append(_card(span="x-span-6", inner=(
            '<div class="x-sup">'
            f'<div class="x-sup-top"><div>'
            f'<span class="x-label">{e(p["supplier"] or "Supplier")}</span>'
            f'<div class="x-fig">{e(_bill_amount(p))}</div>'
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
            '<p class="x-help">A supplier cannot be paid twice, so nothing here '
            'is retried on its own. A replacement is a NEW payment with a new '
            'reference, and it needs your approval.</p></div></div>'
            f'<ul class="x-todo">{items}</ul>'
            '<p class="x-help">Re-plan and any payment that can go again will '
            'appear in <b>Needs your OK</b> for you to approve.</p>'
            '<form class="x-actions" method="post" action="/money-out/run" '
            'data-action><button class="x-btn" type="submit">Re-plan and ask '
            'for my OK</button></form>'
            f'{past_block}'))

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

    # Anything held, with the planner's exact rule. The header says nothing needs
    # the reader; this is the evidence for that claim, on the same screen.
    held = ""
    waits = c.get("waiting") or []
    if waits:
        held = _card(inner=(
            '<div class="x-cardhead" id="held"><div><h3>Held — and not '
            'waiting on you</h3>'
            '<p class="x-help">No approval can release these. They are waiting on '
            'something outside your control, and Closebook will re-plan '
            'automatically when it happens.</p></div>'
            f'<span class="x-pill x-pill--warn">{len(waits)} held</span></div>'
            '<ul class="x-todo">' + "".join(
                f'<li>'
                f'<span class="x-todo-ico is-warn">{_I["clock"]}</span>'
                f'<div><b>{e(p["number"])} · {e(p["supplier"] or "supplier")}</b>'
                f'<p class="x-help"><a href="/app?tab=home#held-{e(p["number"])}">'
                f'Why it is held</a></p></div>'
                f'<span class="x-pill x-pill--warn">'
                f'{e(p.get("plan_policy_reason") or "held")}</span></li>'
                for p in waits)
            + '</ul>'))

    # A payment that arrived but was NOT matched with confidence is also
    # waiting on a human — and it is not a payment step, so the approvable list
    # never contained it. Shown here so "Needs your OK" means what it says.
    wait = c.get("inflows_waiting") or []
    # Every card that belongs after the main body is gathered into ONE string and
    # appended once. Carrying two hand-built returns meant the second one won by
    # accident and silently dropped a card that had been built correctly.
    note_in = ""
    if wait:
        note_in = _card(inner=(
            '<div class="x-cardhead"><div><h3>Money in that needs a look</h3>'
            '<p class="x-help">The cash is already in the book and on the '
            'invoice, but the match was not confident enough to settle itself. '
            'Chase it, re-apply it, or refund it.</p></div>'
            f'<span class="x-pill x-pill--warn">{len(wait)} to review</span>'
            '</div>'
            + "".join(
                # `data-card` marks the UNIT a resolution finishes. Without it
                # the form vanished and the stale row stayed behind it.
                '<div class="x-waititem" data-card data-testid="review-item-'
                + str(a["id"]) + '">'
                + _arrival_row(a, detail=True)
                + _resolve_form(a)
                + _reask_form(a)
                + '</div>'
                for a in wait)))

    done = [a for a in (c.get("arrivals") or [])
            if (a.get("review_state") or "").upper()
            in ("REFUND_OWED", "RELEASED_INCOME")]
    if done:
        note_in += _card(inner=(
            '<div class="x-cardhead"><div><h3>Resolved money</h3>'
            '<p class="x-help">Where unmatched money went, and who decided. A refund '
            'owed is a liability we have not paid; released income is cash that '
            'is now ours to spend.</p></div>'
            f'<span class="x-pill x-pill--info">{len(done)} resolved</span>'
            '</div>'
            + "".join(_arrival_row(a, detail=True) + _resolution_audit(a)
                      for a in done)))

    return body + note_in + held + note


# ---- AI activity
def _arrival_row(a: dict, *, detail: bool = False) -> str:
    """One deposit as it arrived, and what the code decided about it.

    The RAW reference is shown exactly as the bank supplied it, spaces and typos
    included: it is the input the matcher had to work with, and hiding it makes
    the decision look arbitrary.
    """
    conf = a.get("confidence")
    tone = ("good" if a.get("settled_match")
            else ("bad" if (conf is not None and conf < 0.75) else "warn"))
    method = a.get("method") or "n/a"
    conf_txt = f"{conf:.2f}" if conf is not None else "n/a"
    ref = a["reference"] if a["has_reference"] else "(no reference given)"
    applied = (" -> " + ", ".join(x["invoice"] for x in a["applied"])
               if a["applied"] else "")
    out = (
        '<div style="border-bottom:1px solid var(--x-line);padding:10px 0">'
        '<div style="display:flex;gap:10px;align-items:baseline;flex-wrap:wrap">'
        f'<b>{e(money(a["amount_minor"], a["currency"]))}</b>'
        f'<span class="x-hint">{e(a["payer"] or "payer not given")}</span>'
        f'<span class="x-pill x-pill--{tone}">{e(a["match_status"])}</span>'
        f'<span class="x-hint">{e(method)} &middot; confidence {e(conf_txt)}'
        f'{e(applied)}</span></div>'
        f'<div class="x-mono" style="margin-top:6px">ref: {e(ref)}</div>')
    if detail and a.get("reason"):
        out += f'<div class="x-hint" style="margin-top:4px">{e(a["reason"])}</div>'
    if detail and a.get("deposit_id"):
        out += (f'<div class="x-hint">Airwallex deposit '
                f'<span class="x-mono">{e(a["deposit_id"])}</span></div>')
    return out + "</div>"


def _arrivals_card(c: dict, *, span: str = "x-span-12",
                   limit: int = 6, title: str = "What arrived",
                   sub: str = "") -> str:
    arr = (c.get("arrivals") or [])[:limit]
    body = "".join(_arrival_row(a) for a in arr) if arr else (
        '<p class="x-empty">Nothing has landed yet. Use the form above, or make '
        'a real transfer into your Airwallex Global Account.</p>')
    return _card(span=span, inner=(
        '<div class="x-cardhead"><div><h3>' + e(title) + '</h3>'
        '<p class="x-help">' + e(sub or "Each row is a real sandbox deposit, "
        "with the reference exactly as the bank gave it and the verdict the "
        "matcher recorded.") + '</p></div></div>' + body))


def _ai(c: dict) -> str:
    """Every matching decision, then every model call.

    The deterministic tiers are shown FIRST because they decide most matches and
    they are the part that can be trusted. A model is only consulted when the
    deterministic tiers stop short, so an empty model section is a fact about
    the run, not a gap in the page.
    """
    matches = _arrivals_card(
        c, limit=12, title="How the matcher decided",
        sub="Tier, confidence and the sentence the code recorded, read from the "
            "inflow itself. Deterministic tiers run first; the model is only "
            "asked when they stop short.")
    items = []
    for t in c["agent_trace"][:24]:
        ins = t["inputs"]
        outs = t["outputs"]
        prop = outs.get("model_proposed")
        is_model_call = t["tool"] == "propose_match"
        # WHICH model answered is not decoration: there is a primary and a
        # fallback, and "the model said X" is worth much less if you cannot tell
        # whether the one you configured is the one that replied. A trace that
        # never calls a model (the code's own decisions) must not claim one is
        # missing — that is a false alarm about the configuration.
        item = {"verdict": t["verdict"], "model": (str(ins.get("model") or "")
                                                   if is_model_call else ""),
                # The machine's own words, when there are any. Folded away on
                # screen rather than shouted.
                "rules_raw": ""}

        if is_model_call:
            # The amount is `amount_minor` on the trace. Reading `amount` (which
            # is not there) rendered every call as "0.00 USD" — a wrong number on
            # the page next to a right one.
            amt = int(ins.get("amount_minor") or 0)
            if not amt:
                amt = int(float(ins.get("amount") or 0) * 100)
            what = "a payment that needs matching"
            if ins.get("payer") or amt:
                # ONE display formatter for money across the whole app —
                # `money_str` renders "USD 4,400.00" everywhere else, so the
                # AI feed must not be the one screen that says "4,400.00 USD".
                what = (f'{e(ins.get("payer") or "a payer")} — '
                        f'{money_str(amt, ins.get("currency") or "USD")}').strip()
            claim = float((prop or {}).get("confidence") or 0)

            if prop and prop.get("allocations"):
                says = " · ".join(
                    f'{a.get("invoice_number")} '
                    f'{money(int(float(a.get("amount") or 0) * 100))}'
                    for a in prop["allocations"])
                reply = (f'I think this is <b>{e(says)}</b> — about '
                         f'{claim:.2f} confident.')
                # WHAT THE MODEL CLAIMED IS NOT WHAT THE SYSTEM USED. The claim
                # is capped below the AUTO band before it can matter, so showing
                # only the claim put 0.90 on the page next to a decision made at
                # 0.89 with nothing to explain the gap.
                conf_label = (f'claimed {claim:.2f} · capped to {_LLM_CAP:.2f} by code'
                              if claim > _LLM_CAP else f'{claim:.2f} confident')
            else:
                # A confidence number attached to "it could stand behind
                # nothing" reads as a CONFIDENT REFUSAL, which is not what
                # happened. Keep the number, drop the misreading.
                reply = "<b>I cannot stand behind any reading of this one.</b>"
                if claim:
                    reply += f' It put its own certainty at {claim:.2f}.'
                conf_label = f'claimed {claim:.2f}' if claim else "No guess"
            _rules = (t["detail"] or outs.get("explanation")
                      or "Checked every number against the open invoices.")
            item.update(what=_shown(what), ai=reply, rules=_shown(_rules),
                        rules_raw=_raw_of(_rules), conf=conf_label)
        elif t["tool"] == "explain_decision":
            item.update(what=_shown(outs.get("explanation")
                                    or "Explained a decision."),
                        ai="I only explain. I do not decide.",
                        rules="The reason is whatever the code recorded.",
                        rules_raw="", conf="")
        else:
            item.update(what=f'Tool: {e(t["tool"])}',
                        ai="Checked the books and answered.",
                        rules=_shown(t["detail"] or "No further detail."),
                        rules_raw=_raw_of(t["detail"]), conf="")
        items.append(item)

    feed = []
    for it in items:
        good = it["verdict"] == "accepted"
        conf_pill = (f'<span class="x-pill x-pill--plain">{e(it["conf"])}</span>'
                     if it["conf"] else "")
        if it["model"]:
            model_pill = (f'<span class="x-pill x-pill--plain" title="model used">'
                          f'{e(it["model"])}</span>')
        elif it["what"].startswith("Tool:") or it["ai"].startswith("I only explain"):
            model_pill = ""
        else:
            model_pill = ('<span class="x-pill x-pill--warn">no model configured'
                          '</span>')
        feed.append(
            '<li>'
            f'<div class="x-msg x-msg--ai"><span class="x-av x-av--ai">{_I["ai"]}</span>'
            f'<div><span class="x-bubble"><b class="x-who">The model</b>{it["ai"]}</span>'
            f'<div class="x-aifoot"><span class="x-pill x-pill--'
            f'{"accent" if good else "warn"}">{e(it["verdict"])}</span>{conf_pill}'
            f'{model_pill}<span>about {e(it["what"])}</span></div></div></div>'
            f'<div class="x-msg x-msg--rules"><span class="x-av x-av--rules">{_I["scale"]}</span>'
            f'<div><span class="x-bubble" data-testid="rules-bubble"><b class="x-who">The rules</b>'
            f'{e(it["rules"])}</span>'
            # THE MACHINE'S OWN WORDS, folded away. Every stored trace line is
            # translated on the way out, so a 429 recorded days ago reads as a
            # sentence today and its stack is one tap deep, not on the screen.
            + _detail_toggle(it.get("rules_raw") or "")
            + '</div></div>'
            '</li>')

    body = "".join(feed) if feed else '<p class="x-empty">No model calls yet.</p>'

    return matches + _card(span="x-span-12", inner=(
        '<div class="x-cardhead"><div><h3>What the model said, and what the rules did</h3>'
        '<p class="x-help">Straight from <span class="x-mono">agent_trace</span>. '
        'Nothing here is a decision — every row is one the code already made.</p></div>'
        '<span class="x-pill x-pill--accent" data-testid="ai-count">'
        + str(len(items)) + (" call" if len(items) == 1 else " calls")
        + '</span></div>'
        f'<ul class="x-feed">{body}</ul>'
    )) + _card(inner=(
        '<div class="x-note">' + _I["shield"] +
        '<span>The model can read invoices, explain a decision and ask for approval. '
        'It has no tool that can move money — that is a structural limit, not a '
        'setting.</span></div>'))


# ---- setup --------------------------------------------------------------
# Six screens, all of them WRITE THE BOOK and none of them move money. There is
# no transfer or conversion form anywhere on this page by design: money moves
# only through planner -> policy -> executor, and the way to keep that true is
# to give the UI nothing to call.
_CURRENCY_HINT = "3-letter code, e.g. USD"


def _field(name: str, label: str, *, span: str = "x-span-3", kind: str = "text",
           value: str = "", placeholder: str = "", hint: str = "",
           required: bool = False, step: str = "") -> str:
    req = " required" if required else ""
    extra = f' step="{e(step)}"' if step else ""
    hint_html = f'<span class="x-hint">{e(hint)}</span>' if hint else ""
    return (f'<div class="x-field {span}"><label for="f-{e(name)}">{e(label)}</label>'
            f'<input class="x-input" id="f-{e(name)}" name="{e(name)}" type="{e(kind)}"'
            f' value="{e(value)}" placeholder="{e(placeholder)}"{extra}{req}>'
            f'{hint_html}</div>')


def _select(name: str, label: str, options: list[tuple[str, str]], *,
            span: str = "x-span-3", hint: str = "") -> str:
    if not options:
        return (f'<div class="x-field {span}"><label for="f-{e(name)}">'
                f'{e(label)}</label><select class="x-select" id="f-{e(name)}" '
                f'name="{e(name)}" disabled>'
                f'<option value="">nothing to pick yet</option></select>'
                f'<span class="x-hint">{e(hint or "Create one above first.")}</span>'
                f'</div>')
    opts = "".join(f'<option value="{e(v)}">{e(t)}</option>' for v, t in options)
    hint_html = f'<span class="x-hint">{e(hint)}</span>' if hint else ""
    return (f'<div class="x-field {span}"><label for="f-{e(name)}">{e(label)}</label>'
            f'<select class="x-select" id="f-{e(name)}" name="{e(name)}">{opts}</select>'
            f'{hint_html}</div>')


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return '<p class="x-empty">Nothing here yet.</p>'
    head = "".join(f"<th>{e(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
                   for r in rows)
    return (f'<div class="x-table-wrap"><table class="x-table">'
            f'<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>')


def _cardhead(title: str, sub: str) -> str:
    return (f'<div class="x-cardhead"><div><h3>{e(title)}</h3>'
            f'<p>{e(sub)}</p></div></div>')


def _bill_amount(p: dict) -> str:
    """`EUR 9,000.00 (USD 10,346.00 at 1.1496)` when a conversion funds it.

    A EUR bill paid from USD is not a EUR obligation — we hold no EUR, so
    the bill is really "buy EUR 9,000 with USD 10,346". Showing only the EUR
    figure hid the conversion the plan had already decided to make, and the
    two screens then quoted different numbers for the same payment.
    """
    own = f'{p["currency"]} {money(p["amount_minor"])}'
    fund_ccy = p.get("funding_currency")
    fund_minor = int(p.get("funding_amount_minor") or 0)
    if not fund_ccy or fund_ccy == p["currency"] or fund_minor <= 0:
        return own
    rate = fund_minor / p["amount_minor"] if p.get("amount_minor") else 0
    return (f'{own} ({fund_ccy} {money(fund_minor)} at {rate:.4f} '
            f'{fund_ccy}/{p["currency"]})')


def _live_bill_state(p: dict | None, stored: str) -> tuple[str, str, str]:
    """The bill's REAL state, and where it came from.

    The payable row goes to PAID only when a payout SETTLES, so `payable.status`
    is stale by design for anything in flight — a bill can be HELD while its
    transfer is PROCESSING at the bank. Showing the stored column alone is
    therefore a lie the operator will notice immediately.

    Precedence, most authoritative first:
      1. what AIRWALLEX says about the object we sent (live_status);
      2. our newest plan step (display_status), which is what we last did;
      3. the planner's intent for it this run (plan_action).
    """
    if not p:
        return (stored or "HELD").upper(), "warn", ""
    live = (p.get("live_status") or "").upper()
    disp = (p.get("display_status") or "").upper()
    action = (p.get("plan_action") or "").upper()
    note = p.get("step_reason") or ""

    if live:
        return live, ("bad" if live in ("FAILED", "CANCELLED", "RETURNED")
                      else "info"), (note or f"Airwallex reports {live}")
    if disp and disp not in ("HELD", "OPEN"):
        return disp, ("bad" if disp in ("FAILED", "CANCELLED") else "info"), note
    if action == "PAY":
        if p.get("plan_needs_approval"):
            return "NEEDS YOUR OK", "warn", (p.get("plan_approval_reason") or note)
        return "READY", "good", (note or "Within your limits — the planner will pay it.")
    if action in ("DEFER", "ESCALATE", "CHASE"):
        blockers = p.get("plan_blockers") or []
        return "HELD", "warn", (blockers[0] if blockers else note)
    return (stored or "HELD").upper(), "warn", note


def _pill(status: str, label: str = "", tone: str = "") -> str:
    """A status pill, using the classes this stylesheet actually defines.

    (The first draft of the setup screen wrote `class="pill good"`, which is not
    in the stylesheet at all — the pill would have rendered as plain text and a
    warning state would have been invisible.)

    `tone` overrides the guess when the caller already knows which of
    Airwallex's states it is looking at.
    """
    key = (status or "").strip().upper()
    if tone:
        return f'<span class="x-pill x-pill--{tone}">{e(label or key or "—")}</span>'
    tone = "info"
    if key in ("PAID", "MATCHED", "PAYABLE", "VERIFIED", "OK", "SUCCESS", "SETTLED"):
        tone = "good"
    elif key in ("OPEN", "HELD", "PENDING", "PARTIAL", "SUBMITTED", "PROCESSING",
                 "AWAITING_APPROVAL"):
        tone = "warn"
    elif key in ("OVERPAID", "FAILED", "CANCELLED", "REJECTED", "NO BENEFICIARY",
                 "INVALID", "PROBLEM"):
        tone = "bad"
    return f'<span class="x-pill x-pill--{tone}">{e(label or key or "—")}</span>'


def _setup(c: dict) -> str:
    """The workspace builder. Everything shown was created here or at Airwallex."""
    st = c.get("settings") or {}
    ws = c.get("workspace") or {}
    counts = ws.get("counts") or {}
    out: list[str] = []

    if not any(counts.values()):
        out.append(_card(inner=(
            '<div class="x-cardhead"><div><h3>This workspace is empty</h3>'
            '<p>Closebook ships with no sample invoices, suppliers or balances. '
            'Everything it shows comes from what you create here plus what '
            'Airwallex returns — so a plan you see on Home is a plan about your '
            'own money.</p></div></div>'
            '<p class="x-empty">Start with Settings, then add a customer, an '
            'invoice, a supplier and a bill. Money in and money out come next.'
            '</p>')))

    # 1 ---- policy
    ccy = st.get("budget_currency") or "USD"
    out.append(_card(inner=(
        _cardhead("Settings", "The rules the planner obeys. Stored in the "
                              "workspace, not in a config file.")
        + f'<form class="x-form" method="post" action="/setup/settings">'
        + _field("budget_currency", "Budget currency", value=ccy,
                 placeholder="USD", hint="The floor and the limit are in this currency.")
        + _field("reserve_floor", "Reserve floor",
                 value=f'{st.get("reserve_floor_minor", 0) / 100:.2f}',
                 hint="Cash the planner will never spend.")
        + _field("auto_pay_limit", "Automatic limit",
                 value=f'{st.get("auto_pay_limit_minor", 0) / 100:.2f}',
                 hint="Above this, a payment needs your approval.")
        + _field("fx_tolerance_pct", "FX drift headroom (%)",
                 value=f'{st.get("fx_tolerance_pct", 0.5)}',
                 hint="Extra room a conversion leaves for a rate move.")
        + f'<div class="x-submit"><button class="x-btn x-btn--primary" '
          f'type="submit">Save settings</button>'
          f'<span class="x-hint">Now: keep {e(st.get("reserve_floor") or "?")} '
          f'back, approve anything over {e(st.get("auto_pay_limit") or "?")}.</span>'
          f'</div></form>')))

    # 2 ---- customers
    cust_rows = [[e(x["name"]), e(x["country"] or "—"),
                  e(x["email"] or "—"), str(x["open_count"])]
                 for x in ws.get("customers", [])]
    out.append(_card(inner=(
        _cardhead("Customers", "Who owes you money.")
        + '<form class="x-form" method="post" action="/setup/customers">'
        + _field("name", "Customer name", span="x-span-4",
                 placeholder="Brightline Trading", required=True)
        + _field("country", "Country", span="x-span-2", placeholder="NG",
                 hint="Optional")
        + _field("email", "Email", span="x-span-4", placeholder="optional")
        + '<div class="x-submit"><button class="x-btn x-btn--primary" '
          'type="submit">Add customer</button></div></form>'
        + _table(["Name", "Country", "Email", "Invoices"], cust_rows))))

    # 3 ---- invoices
    cust_opts = [(str(x["id"]), x["name"]) for x in ws.get("customers", [])]
    inv_rows = []
    for x in ws.get("invoices", []):
        pill = _pill(x["status"])
        inv_rows.append([f'<strong>{e(x["number"])}</strong>', e(x["customer"]),
                         e(x["amount"]), e(x["paid"]), pill,
                         e(x["due_date"] or "—")])
    out.append(_card(inner=(
        _cardhead("Invoices", "Money owed to you. Payments match against these "
                              "numbers, so a messy reference is the interesting case.")
        + '<form class="x-form" method="post" action="/setup/invoices">'
        + _select("customer_id", "Customer", cust_opts, span="x-span-4")
        + _field("number", "Invoice number", span="x-span-2",
                 placeholder="INV-0041", required=True)
        + _field("amount", "Amount", span="x-span-2", placeholder="12000",
                 required=True)
        + _field("currency", "Currency", span="x-span-2", placeholder="USD",
                 hint=_CURRENCY_HINT)
        + _field("due_date", "Due date", span="x-span-2", placeholder="2026-11-30",
                 hint="YYYY-MM-DD")
        + '<div class="x-submit"><button class="x-btn x-btn--primary" '
          'type="submit">Add invoice</button></div></form>'
        + _table(["Number", "Customer", "Amount", "Paid", "Status", "Due"],
                 inv_rows))))

    # 4 ---- suppliers
    sup_rows = []
    for x in ws.get("suppliers", []):
        if x["beneficiary_id"]:
            state = (_pill("PAYABLE")
                     + f'<div class="x-mono">{e(x["beneficiary_id"])}</div>')
        else:
            state = (_pill("NO BENEFICIARY")
                     + (f'<div class="x-hint">{e(x["beneficiary_error"])}</div>'
                        if x["beneficiary_error"] else ""))
        sup_rows.append([f'<strong>{e(x["name"])}</strong>', e(x["country"] or "—"),
                         e(x["currency"] or "—"), e(x["payout_method"]), state])
    out.append(_card(inner=(
        _cardhead("Suppliers", "A supplier is only payable once Airwallex has "
                               "given it a beneficiary. Validation runs first, "
                               "and any refusal is shown in Airwallex's own words.")
        + '<form class="x-form" method="post" action="/setup/suppliers">'
        + _field("name", "Supplier name", span="x-span-3",
                 placeholder="Atlanta Packaging", required=True)
        + _field("country", "Bank country", span="x-span-2", placeholder="US",
                 hint="2-letter code")
        + _field("currency", "Currency", span="x-span-2", placeholder="USD",
                 hint=_CURRENCY_HINT)
        + _select("payout_method", "Payout method",
                  [("LOCAL", "LOCAL"), ("SWIFT", "SWIFT")], span="x-span-2")
        + _field("entity_type", "Entity type", span="x-span-2",
                 value="COMPANY", hint="PERSONAL or COMPANY")
        + _field("account_name", "Account name", span="x-span-4",
                 hint="Defaults to the supplier name")
        + _field("account_number", "Account number", span="x-span-3",
                 hint="For SWIFT in an IBAN country, this is the IBAN.")
        + _field("routing_value", "Routing number", span="x-span-3",
                 hint="LOCAL only — ABA for USD, sort code for GBP.")
        + _field("bank_name", "Bank name", span="x-span-3")
        + _field("swift_code", "SWIFT / BIC", span="x-span-3",
                 hint="SWIFT only, e.g. INGBNL2A")
        + _field("iban", "IBAN", span="x-span-4",
                 hint="SWIFT only, if it is not the account number above.")
        + _field("city", "City", span="x-span-3")
        + _field("street_address", "Street address", span="x-span-5")
        + _field("postcode", "Postcode", span="x-span-2")
        + _field("state", "State", span="x-span-2", hint="US only")
        + _field("bank_account_category", "Account category", span="x-span-3",
                 value="Checking", hint='USD only: "Checking" or "Savings"')
        + '<div class="x-submit">'
          '<button class="x-btn x-btn--primary" type="submit">'
          'Validate and create beneficiary</button>'
          '<span class="x-hint">This creates a real beneficiary in the '
          'Airwallex sandbox. No money moves.</span></div></form>'
        + _table(["Supplier", "Country", "Currency", "Rail", "Airwallex"],
                 sup_rows))))

    # 5 ---- bills
    sup_opts = [(str(x["id"]), x["name"]) for x in ws.get("suppliers", [])]
    inv_nums = [x["number"] for x in ws.get("invoices", [])]
    # The REAL state of each bill: what Airwallex says, else our newest step,
    # else the planner's verdict. The stored column alone is stale by design —
    # a payable stays HELD until a payout SETTLES.
    live_by_number = {p["number"]: p for p in (c.get("payables") or [])
                      if p.get("number")}
    pay_rows = []
    for x in ws.get("payables", []):
        # FROM THE SAME SOURCE AS THE PLAN REASON. This read the workspace
        # dump's own column while the plan quoted the planner's answer, so a
        # bill could show a dependency on one screen and none on the next.
        live = live_by_number.get(x["number"]) or {}
        dep_on = live.get("depends_on") or x.get("depends_on") or ""
        dep = (f'<span class="x-pill x-pill--warn">after {e(dep_on)}</span>'
               if dep_on else '<span class="x-hint">none</span>')
        label, tone, note = _live_bill_state(live_by_number.get(x["number"]),
                                             x["status"])
        reason = f'<div class="x-hint">{e(note)}</div>' if note else ""
        pay_rows.append([f'<strong>{e(x["number"])}</strong>', e(x["supplier"]),
                         e(x["amount"]), _pill(label, tone=tone) + reason, dep,
                         e(x["payout_method"] or "—")])
    out.append(_card(inner=(
        _cardhead("Bills to pay", "What you owe. A bill only becomes payable "
                                  "once its supplier has a beneficiary.")
        + '<form class="x-form" method="post" action="/setup/payables">'
        + _select("supplier_id", "Supplier", sup_opts, span="x-span-3")
        + _field("number", "Your reference", span="x-span-2", placeholder="PO-7001",
                 required=True, hint="Half of the payment's identity")
        + _field("amount", "Amount", span="x-span-2", placeholder="1800",
                 required=True)
        + _field("currency", "Currency", span="x-span-2", placeholder="USD",
                 hint=_CURRENCY_HINT)
        + _field("criticality", "Priority", span="x-span-1", value="3",
                 kind="number", hint="1-5")
        + _field("due_date", "Due date", span="x-span-2", placeholder="2026-11-30")
        + _field("depends_on", "Pay only after invoice", span="x-span-3",
                 hint=("Optional. One of: " + ", ".join(inv_nums)) if inv_nums
                      else "Optional. No invoices exist yet.")
        + '<div class="x-submit"><button class="x-btn x-btn--primary" '
          'type="submit">Add bill</button></div></form>'
        + _table(["Reference", "Supplier", "Amount", "Status", "Depends on",
                  "Rail"], pay_rows))))

    # 6 ---- start fresh
    out.append(_card(inner=(
        _cardhead("Start fresh", "Archive this workspace to a file and empty it.")
        + '<p class="x-hint">Every customer, invoice, supplier, bill, match and '
          'ledger entry is archived first — nothing is destroyed without a '
          'copy. Your reserve floor and automatic limit are kept, because they '
          'are policy rather than data. <strong>Airwallex transfers that already '
          'exist are not cancelled</strong>: this empties the book, not the bank.'
          '</p>'
        + '<details><summary class="x-hint">Show the Start fresh form</summary>'
          "<form class=\"x-form\" method=\"post\" action=\"/workspace/reset\" "
          "onsubmit=\"return confirm('Archive and empty this workspace?')\">"
        + _field("confirm", "Type START FRESH to confirm", span="x-span-6",
                 placeholder="START FRESH", required=True)
        + '<div class="x-submit"><button class="x-btn" type="submit">'
          'Archive and start fresh</button></div></form></details>')))

    return "".join(out)


_TABS = {"home": _home, "money-in": _money_in, "money-out": _money_out,
         "ok": _ok, "ai": _ai, "setup": _setup}
_SUBS = {
    "home": "Everything money did since the last time you looked.",
    "money-in": "What landed, and what it was for.",
    "money-out": "What you owe, and where each payment is.",
    "ok": "Waiting on you — over the limit, or unclear.",
    "ai": "Every model call, and the rule that checked it.",
    "setup": "Build the workspace Closebook runs on.",
}


def _flash(c: dict) -> str:
    """The one-line result of an action, shown on WHATEVER tab it lands on.

    Deliberately not inside any one screen: a refusal from the Money-in form
    must be visible on the Money-in tab, and a screen that forgets to render it
    is a screen that silently swallows an error.
    """
    out = ""
    if c.get("flash_ok"):
        out += f'<div class="x-note x-note--ok">{e(c["flash_ok"])}</div>'
    if c.get("flash_err"):
        out += f'<div class="x-note x-note--err">{e(c["flash_err"])}</div>'
    return out


def render_app(ctx: dict, tab: str = "home", health: dict | None = None) -> str:
    tab = tab if tab in _TABS else "home"
    n_ok = len(ctx["approvable"])
    screen = _flash(ctx) + _TABS[tab](ctx)
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
        # The refresh OFFER, not a refresh. Hidden until a poll says the book
        # actually changed and the reader is not mid-task.
        + '<button id="x-new" class="x-new" hidden '
        + 'data-testid="new-activity" type="button">'
        + 'New activity — tap to refresh</button>'
    )
    return _shell(inner, app=True, title=f"Closebook — {NAV[[n[0] for n in NAV].index(tab)][1]}")


__all__ = ["render_landing", "render_app", "money", "e"]
