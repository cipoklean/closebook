"""Server-rendered HTML. No build step, no JS framework.

Two pages: `/` the landing page and `/app` the operator app. Both share
`static/closebook.css`.

Everything on `/app` comes from `dashboard.build_context()` — there is no
hardcoded number anywhere in this file. The landing page carries illustrative
copy only, which is why its strings live here and not in the spec's data layer.
"""
from __future__ import annotations

import re

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
    # DEDUPED. The planner can record the same sentence twice (cash and the
    # floor describe the same shortfall in the same words), and two identical
    # paragraphs on a card read as two problems.
    items = []
    for _x in (p.get("plan_blockers") or []):
        if _x and _x not in items:
            items.append(_x)
    if not items:
        fallback = p.get("plan_reason") or p.get("step_reason")
        items = [fallback] if fallback else []
    if not items:
        # A BARE "HELD BY POLICY" IS NOT A REASON. Run 5 had three bills showing
        # exactly that, and an operator cannot act on it. If the planner stored
        # no blocker, say what IS known and where the rule lives, rather than
        # asserting a policy nobody can see.
        return ('<p data-testid="blocker-unknown">Held. The plan did not record a '
                'reason for this one, so treat the figures on Money out as the '
                'evidence and re-plan before acting.</p>')
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


def pct(x) -> str:
    """A confidence as a person reads it: 0.89, never 0.8900000000000001."""
    try:
        return f"{float(x):.2f}"
    except (TypeError, ValueError):
        return ""


_FIRST_PERSON = re.compile(
    r"^\s*(?:i\s+(?:think|believe|reckon|d\s*guess)|i'?m|i'?ll|i'?ve|"
    r"my\s+(?:reading|guess|take)|in\s+my\s+(?:view|opinion))\b[\s,:-]*",
    re.I)


def _prose(text: object, limit: int = 120) -> str:
    """ONE line of the model's reasoning, and never in the first person.

    "I think this looks like INV-1001" is a sentence about the model. The
    receipt is about the CLAIM, so the same content is stated flat: "looks like
    INV-1001". The reader is comparing a claim with a verdict, and a narrator
    in the middle of that comparison is noise.
    """
    t = str(text or "").strip().replace("\n", " ")
    t = _FIRST_PERSON.sub("", t).strip()
    if t and t[0].isupper() and not t[:2].isupper():
        t = t[0].lower() + t[1:]
    t = " ".join(t.split())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def _rule_checks(model: dict, code: dict) -> list[tuple[str, bool, str]]:
    """THE CODE'S OWN CHECKS, as a pass/fail list, between claim and result.

    "The model proposes, the code decides" is the whole product and it was a
    sentence in a README. These are the checks that actually ran, in the words a
    person can follow, so the middle column is the argument rather than a claim
    about one. Only facts the record carries are shown: a check is never marked
    as passed because nobody looked.
    """
    from .domain.policy import MATCH_CONFIDENCE_AUTO

    out: list[tuple[str, bool, str]] = []
    conf = code.get("confidence")
    if conf not in (None, ""):
        ok = float(conf) >= MATCH_CONFIDENCE_AUTO
        out.append((f"Confidence {pct(conf)}: "
                    f"{'at or above' if ok else 'below'} the "
                    f"{pct(MATCH_CONFIDENCE_AUTO)} bar", ok,
                    "is-pass" if ok else "is-fail"))
    verdict = str(code.get("verdict") or "").strip()
    if verdict:
        applied = verdict.upper() == "MATCHED"
        out.append((f"Verdict: {verdict.lower()}"
                    + ("" if applied else " (a person decides this one)"),
                    applied, "is-pass" if applied else "is-fail"))
    rule = str(code.get("rule") or "").strip()
    if rule:
        out.append((f"Rule that fired: {rule}", True, "is-pass"))
    if not out:
        out.append(("No check recorded for this payment.", False, "is-fail"))
    return out


def _receipt(model: dict, code: dict) -> str:
    """Model said / Code decided -- side by side, because that is the product.

    The two columns are the whole argument in one component: the model reads
    what a person typed, and the code decides what it is allowed to do. Putting
    them next to each other makes the ORDER visible -- a claimed 0.90 rendered
    struck through beside a final 0.89 says more than any sentence about the
    model proposing and the code enforcing.

    WHERE THE CODE OVERRODE THE MODEL, the model's value is struck through
    rather than hidden. Hiding it would make the override invisible; showing it
    unstruck would misreport the number that is actually in force.

    WHEN THE MODEL WAS UNAVAILABLE the left column says so in plain words and
    the right column shows the escalation. An unavailable model is not an error
    state to be dressed up: it is the case the code was built to survive.
    """
    m = model or {}
    c = code or {}
    if m.get("unavailable"):
        left = (f'<div class="x-receipt-absent" data-testid="receipt-model-absent">'
                f'<b>Model did not answer</b>'
                f'<span>{e(m["unavailable"])}</span></div>')
    else:
        rows = []
        if m.get("invoice"):
            rows.append(("Invoice", e(m["invoice"]), False))
        if m.get("amount"):
            rows.append(("Amount", e(m["amount"]), False))
        if m.get("confidence") not in (None, ""):
            rows.append(("Claimed confidence", e(pct(m["confidence"])),
                         bool(m.get("struck"))))
        if m.get("reason"):
            rows.append(("Reasoning", e(m["reason"]), False))
        left = "".join(
            f'<div class="x-receipt-row"><span>{k}</span>'
            + (f'<s data-testid="model-overridden">{v}</s>' if struck else f'<b>{v}</b>')
            + '</div>' for k, v, struck in rows
        ) or '<div class="x-receipt-row"><span>No claim recorded.</span></div>'

    rows = []
    if c.get("verdict"):
        rows.append(("Verdict", e(c["verdict"])))
    if c.get("rule"):
        rows.append(("Rule that fired", e(c["rule"])))
    if c.get("confidence") not in (None, ""):
        rows.append(("Final confidence", e(pct(c["confidence"]))))
    if c.get("note"):
        rows.append(("", e(c["note"])))
    right = "".join(f'<div class="x-receipt-row"><span>{k}</span><b>{v}</b></div>'
                    for k, v in rows) or '<div class="x-receipt-row"><span>, </span></div>'

    checks = _rule_checks(m, c)
    middle = "".join(
        f'<div class="x-receipt-row {cls}" data-testid="rule-{ "pass" if ok else "fail"}">'
        f'<span class="x-tick">{_I["check"] if ok else _I["x"]}</span>'
        f'<b>{e(text)}</b></div>' for text, ok, cls in checks)

    return (f'<div class="x-receipt" data-testid="receipt">'
            f'<div class="x-receipt-col x-receipt-col--model">'
            f'<div class="x-receipt-head">AI suggestion</div>{left}</div>'
            f'<div class="x-receipt-col x-receipt-col--checks">'
            f'<div class="x-receipt-head">Rule checks</div>{middle}</div>'
            f'<div class="x-receipt-col x-receipt-col--code">'
            f'<div class="x-receipt-head">Result</div>{right}</div></div>')


def _waterfall_bar(ccy: str, w: dict) -> str:
    """ONE bar per currency, from cash down to free to spend.

    THE CURRENCY IS ON THE BAR. It used to exist only as `data-wf-ccy`, so with
    two bars side by side the reader had to guess which was which -- and a
    report of the GBP and USD bars got them the wrong way round, which is what
    a label that only machines can read buys you. The heading names it and the
    closing figure repeats it in the tile's own format ("USD -8,646.00 free to
    spend"), so the bar and the tile above it read as the same number.

    ZERO SEGMENTS ARE DROPPED. A subtraction of nothing is not a step, and a
    bar padded with 0.00 blocks buries the one term that matters. When nothing
    but cash is left the bar becomes a SENTENCE -- "GBP 4,800.00, nothing set
    aside, all free to spend" -- because there is no proportion to draw.

    Widths are a share of the CASH, so the segments and the remainder always
    add up to the whole, and a term that takes the total past zero is painted
    red where it happens rather than only at the end.
    """
    free = w["free_minor"]
    # A NEGATIVE FREE FIGURE IS NOT "MINUS 4,880 FREE". It is a business that
    # has committed more than it holds, which is a different sentence and a
    # different decision. Name it, so the operator reads the situation rather
    # than a minus sign.
    if free < 0:
        closing = f'over-committed by {money_str(-free, ccy)}'
    else:
        closing = f'{money_str(free, ccy)} free to spend'

    start = w["start_minor"] or 1

    # keep the start (cash) and any subtraction that actually takes something
    live = [st for st in w["steps"] if st["kind"] == "start" or st["minor"]]
    takes = [st for st in live if st["kind"] != "start"]

    if not takes:
        # THE CURRENCY IS IN THE SENTENCE, so there is no heading above it. It
        # read "GBP GBP 4,800.00, nothing set aside..." -- the same word twice,
        # because the heading and the sentence each named it.
        return (f'<div class="x-wf" data-wf-ccy="{e(ccy)}">'
                f'<p class="x-wf-simple" data-testid="wf-simple" '
                f'data-wf-ccy-label="{e(ccy)}">'
                f'{e(money_str(w["start_minor"], ccy))}, nothing set aside, '
                f'all free to spend</p></div>')

    segs = []
    for st in live:
        share = max(1, round(abs(st["delta_minor"]) * 100 / start)) if start else 0
        cls = ("x-wf-seg x-wf-seg--start" if st["kind"] == "start"
               else "x-wf-seg x-wf-seg--take")
        if st["overdrawn"]:
            cls += " is-overdrawn"
        sign = "+" if st["kind"] == "start" else "−"
        segs.append(
            f'<span class="{cls}" style="--w:{share}" '
            f'data-wf="{st["key"]}" title="{e(st["label"])}">'
            f'<b>{sign} {e(money(st["minor"]))}</b>'
            f'<small>{e(st["label"])}</small></span>')
    free_cls = "x-wf-seg x-wf-seg--free" + (" is-overdrawn" if free < 0 else "")
    # The closing line below says what this figure IS, so the segment states
    # the number and nothing else -- otherwise "free to spend" arrived twice in
    # two lines a few pixels apart.
    segs.append(
        f'<span class="{free_cls}" style="--w:1" data-wf="free" '
        f'title="free to spend">'
        f'<b>{e(money(free))}</b><small></small></span>')

    label = (f'{ccy}: cash {money_str(w["start_minor"], ccy)}, then '
             + ", ".join(f'less {st["label"]} {money_str(st["minor"], ccy)}'
                         for st in takes)
             + f", ending at {closing}")
    return (f'<div class="x-wf" data-wf-ccy="{e(ccy)}">'
            f'<div class="x-wf-bar" role="img" aria-label="{e(label)}">'
            f'{"".join(segs)}</div>'
            f'<div class="x-wf-end" data-testid="wf-end">{e(closing)}</div></div>')


def _waterfall(m: dict, cash: dict) -> str:
    """The bar for every currency the page already reports free cash in."""
    from .domain.policy import cash_waterfall

    owed = m.get("refunds_owed") or {}
    out = []
    for ccy in sorted(m["free"]):
        w = cash_waterfall(
            # `cash` lives on the context, not on the metrics dict -- the
            # metrics carry the BASES, the context carries the balances.
            cash_minor=cash.get(ccy, 0),
            in_flight_minor=m["in_flight"].get(ccy, 0),
            committed_this_run_minor=m["promised"].get(ccy, 0),
            suspense_minor=m["suspense"].get(ccy, 0),
            refunds_owed_minor=owed.get(ccy, 0),
            # The floor is a single currency's limit, the way the tile reads it.
            floor_minor=m["reserve_floor_minor"] if ccy == m["headline_currency"] else 0,
        )
        out.append(_waterfall_bar(ccy, w))
    return "".join(out) or '<p class="x-empty">Nothing to work out yet.</p>'


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
        '<meta name="description" content="Closebook: real-time books for a '
        'cross-border business. Money in is matched, money out is checked, and '
        'anything unclear waits for a human.">\n'
        # System fonts first, Google as an upgrade: a blocked or slow CDN must
        # never leave the page unstyled.
        f'<link rel="stylesheet" href="/static/closebook.css?v={v}">\n'
        '</head>\n'
    )


_JS = """
(function () {
  "use strict";
  var stamp = function () {
    var u = document.querySelector("[data-updated]");
    if (u) u.textContent = "Updated " + new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
  };
  stamp();
  document.addEventListener("closebook:patched", stamp);
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
    note.textContent = "Done: this no longer needs you.";
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

    /* BOTH FIELDS STAY VISIBLE, AND THE LABELS SAY WHICH CHOICE NEEDS WHICH.
       This used to hide the note unless "release as income" was chosen and the
       reason unless "refund" was, on the theory that showing both invites the
       reader to fill the one that will be ignored. What it actually did was
       render the field as NOT THERE: run 8 typed a note into nothing, submitted,
       and the form could not be completed -- a 25-minute hang in one test. A
       label that names its choice is a cheaper guard than a hidden input. */
    var sync = function () {
      if (!sel) return;
      /* Kept as a no-op hook point: the fields are always on the page, and the
         server still refuses a refund without a reason and a release without a
         note, so nothing here decides anything. */
    };
    if (sel) { sel.addEventListener("change", sync); sync(); }

    f.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (busy) { say("Still working on the last one.", "bad"); return; }
      busy = true;

      var btn = f.querySelector("button[type=submit]");
      var prev = btn ? btn.textContent : "";
      if (btn) { btn.disabled = true; btn.textContent = "Working…"; }

      /* A WATCHDOG, because the abort path is not enough on its own. The
         request can hang before it is ever sent, the response can arrive after
         the abort, an exception inside a success handler can skip the reset --
         and each of those leaves the button reading "Working…" with no way
         back, which is worse than an error because it looks like progress. This
         fires whatever else happened. */
      var done = false;
      var finish = function (message, tone) {
        if (done) return;
        done = true;
        if (watchdog) window.clearTimeout(watchdog);
        if (timer) window.clearTimeout(timer);
        busy = false;
        if (btn) { btn.disabled = false; btn.textContent = prev; }
        if (message) say(message, tone || "bad");
      };
      var watchdog = window.setTimeout(function () {
        finish("That is taking too long, so it was stopped. Check the page "
               + "before trying again.", "bad");
      }, 30000);

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
        // `ok` IS A BOOLEAN and `message` is the sentence. Deciding on the
        // sentence is what produced "The server did not say what happened" on
        // an approval that had SUCCEEDED: a successful action reported as
        // unknown is worse than a failure reported as one, because it invites
        // the operator to do it again.
        //
        // EVERY PATH ENDS IN `finish`, so the button comes back whatever
        // happened: a parsed answer, an unparseable one, or a throw inside a
        // success handler. The watchdog behind it covers the rest.
        if (!res || typeof res.ok !== "boolean") {
          finish("The server did not say what happened. Reload to see the books.");
          return;
        }
        if (res.ok) {
          finish(res.message, "good");
          settled(f, res.message);
        } else {
          finish(res.err || res.message || "That did not work.", "bad");
          if (res.detail) showDetail(f, res.detail);
        }
      }).catch(function (err) {
        var aborted = err && err.name === "AbortError";
        /* Deliberately does NOT reuse the old mockup phrasing, which a test
           guards against: this is real copy and must read as its own thing. */
        finish(aborted
               ? "That took too long, so it was stopped. Check the page before trying again."
               : "Could not reach the server. The books are unchanged.");
      });
    });
  });
}());

/* THE ACCOUNT PICKER FILLS ITSELF IN. The page renders without waiting for
   Airwallex: if the global accounts are not cached yet the picker says
   "Loading accounts..." and this asks for them, once a second, for a short
   while. The server does the fresh read on THAT request, so the wait is on a
   control the user can see is waiting rather than on the whole page. */
(function () {
  var sel = document.querySelector("select[data-loading='1']");
  if (!sel) return;
  var tries = 0;
  function ask() {
    tries += 1;
    fetch("/money-in/accounts", { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res && res.ready && res.html) {
          sel.innerHTML = res.html;
          sel.removeAttribute("data-loading");
          sel.disabled = false;
          var hint = document.querySelector("[data-testid='accounts-loading']");
          if (hint) { hint.parentNode.removeChild(hint); }
          return;
        }
        if (tries < 20) { window.setTimeout(ask, 1000); }
        else { giveUp(); }
      })
      .catch(function () {
        // A FAILED FETCH MUST SETTLE TOO. This branch retried and then did
        // NOTHING on the last try, so a request that never succeeded left the
        // control disabled with "Loading accounts..." beside it for ever: a
        // spinner that cannot finish, which is what hung a Setup test.
        if (tries < 20) { window.setTimeout(ask, 1000); } else { giveUp(); }
      });
  }


  function giveUp() {
    sel.removeAttribute("data-loading");
    var dead = document.querySelector("[data-testid='accounts-loading']");
    if (dead) {
      dead.setAttribute("data-settled", "1");
      dead.textContent =
        "Could not reach Airwallex for the account list. The rest of this "
        + "page still works; reload to try the list again.";
    }
  }

  ask();
  }());

/* --- jump to a payment's row inside the fenced sandbox panel ----------------
   A plain anchor cannot open a collapsed <details>, so the link would land on
   a closed box and look broken. Open it first, then bring the row into view. */
(function () {
  [].forEach.call(document.querySelectorAll("[data-open-sandbox]"), function (a) {
    a.addEventListener("click", function (ev) {
      var panel = document.getElementById("sandbox-controls");
      if (!panel) return;
      ev.preventDefault();
      panel.open = true;
      var row = document.getElementById("sandbox-row-" + a.getAttribute("data-open-sandbox"));
      if (row && row.scrollIntoView) {
        row.scrollIntoView({ block: "center", behavior: "smooth" });
      }
      if (history.replaceState) {
        history.replaceState(null, "", a.getAttribute("href"));
      }
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


# ------------------------------------------------- carrying the book on URLs
#: Every internal URL a rendered page can navigate or post to. `src=` is
#: deliberately absent: assets are not book-scoped and must not be rewritten.
_WS_URL_RE = re.compile(
    r'(?P<name>href|action|formaction)="'
    r'(?P<path>/[^"?#]*)(?P<query>\?[^"#]*)?(?P<frag>#[^"]*)?"')
#: The page's own fetches. `fetch("/state/version")` and friends carried no
#: workspace at all, so a test's Home figures were patched from the SHARED book
#: while it was looking at its own.
_WS_FETCH_RE = re.compile(
    r'fetch\("(?P<path>/[^"?#]*)(?P<query>\?[^"#]*)?(?P<frag>#[^"]*)?"')

#: URLs that are not book state and must be left exactly as written.
_WS_SKIP = ("//", "/static")


def carry_workspace(html: str, ws: str) -> str:
    """Name the active book on every internal URL in a rendered page.

    WHY BOTH THIS AND A COOKIE. The cookie makes the book stick for a browser
    that keeps cookies; this makes it stick for one that does not, and it puts
    the book where an automated tester can read it off the page. Two independent
    mechanisms for one fact is deliberate here: the failure they guard against
    (a test's book silently becoming the shared book) is invisible on screen.

    Pure: same input, same output. `ws` is a validated suffix (letters, digits,
    dashes and underscores), so nothing needs escaping.
    """
    if not ws:
        return html

    def _add(m: re.Match) -> str:
        path, query, frag = m.group("path"), m.group("query") or "", m.group("frag") or ""
        if path.startswith(_WS_SKIP) or "ws=" in query:
            return m.group(0)
        name = m.group("name") if "name" in m.groupdict() else "fetch"
        sep = "&" if query else "?"
        tail = f'{query}{sep}ws={ws}{frag}"'
        return f'{name}="{path}{tail}' if name != "fetch" else f'fetch("{path}{tail}'

    out = _WS_URL_RE.sub(_add, html)
    return _WS_FETCH_RE.sub(_add, out)


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


def render_landing(stats: dict | None = None, ws: str = "") -> str:
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
         "A deposit arrives with whatever the payer typed: a name, a fragment "
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
         "the evidence attached: and nothing a person has not approved can "
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
         "rate: all hashed. Change any one of them and the approval is void."),
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
      <em>send it again</em>: and a replacement is treated as a new payment
      with a new approval, because that is what it is.</p>
    </div>
  </div>
</div></section>''')

    # ---- the one live proof ---------------------------------------------
    if pr["empty"]:
        proof_body = ('<p class="x-l-proofnote">This workspace is empty, so there '
                      'is nothing to show yet. Launch the app and build it: the '
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
, {pr["ledger_rows"]} ledger posting{"s" if pr["ledger_rows"] != 1 else ""}
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

    return carry_workspace(
        _shell("".join(L), app=False, title="Closebook: real-time books"), ws)


# ---------------------------------------------------------- app pages
NAV = [
    ("home", "Home", "/app?tab=home", _I["home"]),
    ("money-in", "Money in", "/app?tab=money-in", _I["in"]),
    ("money-out", "Money out", "/app?tab=money-out", _I["out"]),
    ("ok", "Needs your OK", "/app?tab=ok", _I["ok"]),
    ("ai", "AI decisions", "/app?tab=ai", _I["ai"]),
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
        short = label.replace("Needs your OK", "Approve").replace("AI decisions", "Decisions")
        links.append(f'<a href="{href}"{cur} data-testid="tab-mobile-{key}">'
                     f'{icon}<span>{e(short)}</span>{badge}</a>')
    return '<div class="x-tabs">' + "".join(links) + '</div>'


def _top(title: str, sub: str, health: dict | None) -> str:
    live = ""
    if health:
        sandbox = bool(health.get("sandbox_ready"))
        live = (f'<div class="x-live"><span class="x-livepill{"" if sandbox else " is-off"}">'
                f'<i></i>{"Live · sandbox" if sandbox else "Sandbox not ready"}</span>'
                f'<small data-updated>Updated {datetime.now(timezone.utc).strftime("%H:%M")} UTC</small></div>')
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
    # A bill over the limit that cash cannot cover yet is HELD, not waiting on
    # a person: the Approve tab lists it under "not waiting on you", so Home
    # must not count it as a decision (the two screens said opposite things).
    held_for_cash = {p.get("number") for p in c.get("waiting") or []}
    obligations |= {p["number"] for p in c.get("awaiting_approval") or []
                    if p["number"] not in held_for_cash}
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
        headline = f"Short by {bits} for supplier bills and the {floor_txt} safety cushion"
    else:
        headline = (f"{money_str(m['headline_free_minor'], m['headline_currency'])} "
                    f"for supplier bills and the {floor_txt} safety cushion")
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


def _start_here(c: dict) -> str:
    """THE FIRST TWO MINUTES, for somebody who has never seen this.

    Three steps, each one a real screen and a real route -- nothing pre-filled,
    nothing simulated on the operator's behalf. A step ticks itself off from the
    books, so the guide cannot claim progress that did not happen, and the guide
    disappears once a book has all three (it can be reopened from "How it
    works").
    """
    payables = c.get("payables") or []
    invoices = c.get("receivables") or []
    arrivals = c.get("arrivals") or []
    matched = [a for a in arrivals
               if (a.get("applied") or a.get("settled_match"))]

    steps = [
        ("bill", "Add a supplier bill", "What you owe, like a supplier invoice.",
         "/app?tab=setup#bill", "Add a supplier bill", bool(payables)),
        ("invoice", "Add a customer invoice", "What somebody owes you.",
         "/app?tab=setup#invoice", "Add an invoice", bool(invoices)),
        ("payment", "Receive a payment",
         "Simulate one landing in the sandbox, naming the invoice.",
         "/app?tab=money-in", "Receive a payment", bool(arrivals)),
    ]
    done = sum(1 for *_, ok in steps if ok)
    rows = []
    for i, (key, title, blurb, href, cta, ok) in enumerate(steps, start=1):
        tick = (_I["check"] if ok else f'<span class="x-step-num">{i}</span>')
        rows.append(
            f'<li class="x-step{" is-done" if ok else ""}" data-testid="step-{key}">'
            f'<span class="x-step-ico{" is-done" if ok else ""}">{tick}</span>'
            f'<div class="x-step-body"><b>{e(title)}</b>'
            f'<p>{e(blurb)}</p></div>'
            + ("" if ok else
               f'<a class="x-btn x-btn--primary" href="{href}" '
               f'data-testid="step-{key}-go">{e(cta)}</a>')
            + '</li>')

    # WHAT ACTUALLY HAPPENED, in the plainest words available.
    result = ""
    if matched:
        a = matched[-1]
        who = a.get("payer") or "a customer"
        invs = ", ".join(x.get("invoice") or "" for x in (a.get("applied") or [])
                         if x.get("invoice"))
        conf = a.get("confidence")
        sure = (f" ({round(float(conf) * 100)}% sure)" if conf is not None else "")
        sent = ""
        moved = [p for p in payables
                 if (p.get("display_status") or "").upper()
                 in ("APPROVED", "SUBMITTED", "SCHEDULED", "PROCESSING", "PAID")]
        if moved:
            sent = (f" Supplier bill {moved[0]['number']} was released.")
        result = (
            '<div class="x-note x-note--done" data-testid="guide-result">'
            f'{_I["ok"]}<span>{e(money(a.get("amount_minor") or 0, a.get("currency") or ""))} '
            f'arrived. The AI thinks it pays {e(invs or "an invoice")}{e(sure)}. '
            f'The rules checked it.{sent}</span></div>')

    return _card(span="x-span-12", inner=(
        '<div class="x-cardhead"><div><h3>Start here</h3>'
        '<p class="x-help">Three steps and the demo tells its own story. '
        'Everything runs on the Airwallex sandbox: no real money, nothing '
        'pre-filled.</p></div>'
        f'<span class="x-pill x-pill--accent" data-testid="guide-progress">'
        f'{done} of 3</span></div>'
        f'<ul class="x-steps-list" data-testid="start-here">'
        + "".join(rows) + '</ul>' + result
    ))


def _just_happened(c: dict) -> str:
    """ONE timeline, newest first, one plain sentence per event.

    Deposits and anything the bank pushed back, merged and ordered by their own
    timestamps. Each line links to the screen that owns it, so "what just
    happened" is a way in rather than a report.
    """
    events: list[tuple[str, str]] = []
    for a in c.get("arrivals") or []:
        when = a.get("received_at") or ""
        money_txt = money(a.get("amount_minor") or 0, a.get("currency") or "")
        who = a.get("payer") or "somebody"
        state = str(a.get("match_status") or "").upper()
        resolution = str(a.get("review_state") or "").upper()
        invs = ", ".join(x.get("invoice") or "" for x in (a.get("applied") or [])
                         if x.get("invoice"))
        if resolution == "REFUND_OWED":
            line = (f"{money_txt} was set aside as a refund owed. "
                    f"It is still in the bank and not yours to spend.")
            href = "/app?tab=ok"
        elif resolution == "RELEASED_INCOME":
            line = f"{money_txt} was released as income. It is yours to spend."
            href = "/app?tab=money-in"
        elif invs:
            line = f"{money_txt} arrived from {who} and paid {invs}."
            href = "/app?tab=money-in"
        elif state in ("AMBIGUOUS", "UNMATCHED", "PROPOSED"):
            line = f"{money_txt} arrived from {who} and nobody can tell what it " \
                   f"pays yet. It needs you."
            href = "/app?tab=ok"
        else:
            line = f"{money_txt} arrived from {who}."
            href = "/app?tab=money-in"
        events.append((when, f'<li><a href="{href}" data-testid="happened">'
                             f'<span class="x-when">{e(_fmt_ts(when))}</span>'
                             f'<span>{e(line)}</span></a></li>'))
    for inc in c.get("incidents") or []:
        when = str(inc.get("created_at") or "")
        tid = str(inc.get("transfer_id") or "")
        said = (inc.get("failure_reason") or inc.get("failure_type")
                or inc.get("summary") or "the bank did not say why")
        events.append((when, f'<li><a href="/app?tab=money-out#sandbox-controls" '
                             f'data-testid="happened"><span class="x-when">'
                             f'{e(_fmt_ts(when))}</span><span>A payment '
                             f'({e(tid[:8])}…) came back: {e(_shown(said))}'
                             f'</span></a></li>'))
    events.sort(key=lambda pair: pair[0], reverse=True)
    rows = "".join(html for _when, html in events[:8])
    if not rows:
        rows = ('<li class="x-empty-row">Nothing has happened yet. When money '
                'arrives or a payment moves, it appears here in one line.</li>')
    return _card(span="x-span-12", inner=(
        '<div class="x-cardhead"><div><h3>What just happened</h3>'
        '<p class="x-help">Newest first. Tap a line to open the screen behind '
        'it.</p></div></div>'
        f'<ul class="x-happened" data-testid="what-just-happened">{rows}</ul>'
    ))


def _bignums(c: dict, tt: dict, m: dict) -> str:
    """THE THREE NUMBERS a person needs: what is there, what is spare, what
    needs them. Everything the old Home gave equal weight to now lives one fold
    below, in the same order it always did."""
    floor_txt = money_str(m["reserve_floor_minor"], m["reserve_applies_to"])
    promised = tt["promised"]
    shorts = dict(m["shortfall"])
    if shorts:
        bits = " and ".join(money_str(v, ccy) for ccy, v in sorted(shorts.items()))
        free_fig = f"Short by {bits}"
        free_note = ("Supplier bills already promise more than is left after "
                     f"keeping back a {floor_txt} safety cushion.")
    else:
        free_fig = money_str(m["headline_free_minor"], m["headline_currency"])
        # ONE SOURCE for the sentence: the same string the folded tile uses,
        # so the two can never say different things about the same number.
        free_note = tt["free_to_spend"]
    n_you = needs_you(c)
    you_note = ("Everything is decided. Closebook is watching the books."
                if n_you == 0 else
                f"{'One thing' if n_you == 1 else str(n_you) + ' things'} only "
                "a person can decide.")
    n_inflight = len([ln for ln in m["lines"] if ln["where"] == "in_flight"])
    cash_txt = tt["cash"]
    return (
        '<div class="x-bignums" data-testid="home-numbers">'
        '<div class="x-big">'
        '<div class="x-label">Cash in the bank</div>'
        f'<div class="x-bigfig" data-figure="cash">{e(cash_txt)}</div>'
        '<p>Money actually in your Airwallex accounts. '
        f'{n_inflight} payment{"s" if n_inflight != 1 else ""} already sent '
        'and not yet settled are listed under Details.</p></div>'
        '<div class="x-big">'
        '<div class="x-label">Free to spend</div>'
        f'<div class="x-bigfig" data-figure="free_to_spend">{e(free_fig)}</div>'
        f'<p>{e(free_note)}</p></div>'
        f'<a class="x-big" href="/app?tab=ok">'
        '<div class="x-label">Needs you</div>'
        f'<div class="x-bigfig" data-figure="needs_you" '
        f'data-testid="needs-you-count">{n_you}</div>'
        f'<p>{e(you_note)}</p></a>'
        '</div>')


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

    # 1b. WHAT IS IN THE BANK AND NOT SPENDABLE YET. The money has arrived and
    # nobody has decided about it, so it is neither cash nor free to spend, and
    # an operator reading only those two figures concludes it never arrived.
    # Read from the SAME per-arrival figure the Money in tile sums, so the two
    # screens cannot disagree about what is held.
    held_rows = [a for a in (c.get("arrivals") or [])
                 if int(a.get("waiting_minor") or 0) > 0]
    held_by_ccy: dict[str, int] = {}
    for a in held_rows:
        ccy = str(a.get("currency") or "USD")
        held_by_ccy[ccy] = held_by_ccy.get(ccy, 0) + int(a.get("waiting_minor") or 0)
    held_str = " + ".join(money_str(v, k) for k, v in sorted(held_by_ccy.items()))
    cards.append(_card(span="x-span-3", inner=(
        f'<span class="x-stat-ico">{_I["clock"]}</span>'
        '<div class="x-label">Unmatched money</div>'
        f'<div class="x-fig" data-figure="held">'
        f'{e(held_str) if held_str else "Nothing held"}</div>'
        f'<div class="x-statnote">In the bank and not spendable: '
        f'{len(held_rows)} payment{"s" if len(held_rows) != 1 else ""} waiting '
        f'on a decision in <a href="/app?tab=money-in">Money in</a>.</div>'
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
        f'{n_inflight} sent to the bank and not settled yet: a sandbox '
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
            headline += (f": paying {who['payable_number']} "
                         f"({money_str(who['amount_minor'], who['currency'])}) "
                         f"would take cash below the {floor_txt} safety cushion")
        else:
            headline += (f" after promised payments and the {floor_txt} "
                         f"reserve floor")
        head_cls = ' style="color:var(--x-bad)"'
    else:
        headline = (f"{money_str(m['headline_free_minor'], hccy)} after promised "
                    f"supplier bills and the {floor_txt} safety cushion")
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
        + (f' − unmatched money {money(m["suspense"].get(ccy, 0))}'
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
        bits = "; ".join(f'{u["payable_number"]}, {u["note"]}'
                         for u in m["unfunded"])
        unfunded_note = (f'<div class="x-statnote" style="color:var(--x-bad)">'
                         f'Not costed here: {e(bits)}.</div>')
    cards.append(_card(span="x-span-3", inner=(
        f'<span class="x-stat-ico">{_I["bolt"]}</span>'
        '<div class="x-label">Free to spend</div>'
        f'<div class="x-fig"{head_cls} data-figure="free_to_spend">{e(headline)}</div>'
        + unfunded_note
        # THE WATERFALL REPLACES THE DETAILS TEXT. It is the same arithmetic,
        # drawn, and per currency -- so the headline above and the bar below
        # cannot disagree.
        + _waterfall(m, c["cash"])
        + (f'<p class="x-statnote">{len(m["lines"])} outgoings counted. '
           f'<a href="/app?tab=money-out">See them</a>.</p>'
           if m["lines"] else '<p class="x-statnote">Nothing outstanding.</p>')
    )))

    # ONE LIST, BOTH NUMBERS. The figure came from the book's SUSPENSE and the
    # count from the list of unmatched payments, so after a refund was recorded
    # the tile read "Nothing waiting" and "1 payment waiting on you" in the same
    # breath -- while Home also showed the GBP 100.00 under Refunds owed. Both
    # numbers are now taken from the payments that are genuinely still waiting,
    # and the money for a payment is suspense PLUS what we have recorded as owed
    # back, so the tile and Refunds owed cannot disagree either.
    waiting_rows = [a for a in c["unmatched"]
                    if int(a.get("waiting_minor") or 0) > 0]
    n_unmatched = len(waiting_rows)
    by_ccy: dict[str, int] = {}
    for a in waiting_rows:
        ccy = str(a.get("currency") or "USD")
        by_ccy[ccy] = by_ccy.get(ccy, 0) + int(a.get("waiting_minor") or 0)
    susp_str = " + ".join(money_str(v, k) for k, v in sorted(by_ccy.items()))
    cards.append(_card(span="x-span-3", inner=(
        f'<span class="x-stat-ico">{_I["in"]}</span>'
        '<div class="x-label">Unmatched money: not spendable</div>'
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
                      'your approval: re-plan and the ones that can go will '
                      'appear in Needs your OK.')
        banner_icon = _I["warn"]
        banner_href = '/app?tab=money-out'
        banner_cta = 'See what failed'
    elif n_wait:
        # Held, but NOT a human decision: quote the planner's own reason so the
        # header says what the payment is actually waiting for.
        first = c["waiting"][0]
        banner_head = (f'Nothing needs your OK, {n_wait} payment'
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
                body = (f'{p["currency"]} {money(p["amount_minor"])}: settled '
                        f'in sandbox.')
                pill = '<span class="x-pill x-pill--good">Paid</span>'
            else:
                head = (f'{p["number"]} ({p["supplier"] or "supplier"}) '
                        f'is moving')
                body = (f'{p["currency"]} {money(p["amount_minor"])} sandbox '
                        f'transfer sent'
                        + (f': the bank says {live}.' if live else '.'))
                # THE TOKEN NEVER REACHES THE SCREEN: "SUBMITTED" is true and
                # means nothing to a reader. chip() says it in words and
                # explains itself on hover or tap.
                label, why = plain_status(live or st)
                pill = (f'<span class="x-pill x-pill--info x-tip" '
                        f'title="{e(why)}" data-tip="{e(why)}">'
                        f'{e(label)}</span>')
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
            '<p class="x-help">Nothing here is sent until the reason below '
            'clears.</p>'
            '</div></div><ul class="x-todo">' + "".join(
                f'<li><span class="x-todo-ico is-warn">{_I["clock"]}</span>'
                f'<div id="held-{e(p["number"])}">'
                f'<b>{e(p["number"])} · {e(p["supplier"] or "supplier")}</b>'
                # THE reason, once, on the screen that shows the plan. Every
                # other mention links here rather than saying it again.
                f'{blockers_html(p)}</div>'
                f'<span class="x-pill x-pill--warn">'
                f'{e(policy_words(p.get("plan_policy_reason")))}</span></li>'
                for p in c["waiting"])
            + '</ul>'))

    inc = c["incidents"]
    inc_inner = ""
    if inc:
        rows = "".join(
            f'<li><span class="x-todo-ico is-warn">{_I["warn"]}</span>'
            f'<div><b>{e(bank_words(g.get("live_status"))[0])}</b>'
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

    # THE GUIDE STAYS UNTIL ALL THREE ARE DONE, not until the book stops being
    # empty: adding a bill is step one, and hiding the guide at step one is how
    # the second and third steps never get found (caught by driving the guide
    # with a real browser, not by reading it).
    all_three = bool(c.get("payables")) and bool(c.get("receivables")) \
        and bool(c.get("arrivals"))
    folded = ("".join(cards) + checklist + side + balance)
    details = (
        '<details class="x-details" data-testid="home-details">'
        '<summary><span class="x-label">Details</span>'
        '<span class="x-hint">Every other figure, one line each</span></summary>'
        f'{folded}</details>')
    guide = _start_here(c) if ((not all_three) or c.get("guide")) else ""
    from .ui_home import compose
    return compose(c, tt, guide=guide, details=details)



def _dev_controls_are_offered_for(live: str) -> bool:
    """The same question `_dev_controls` asks, asked once.

    Shared so the jump link and the row it points at cannot disagree: a link
    rendered for a payment the panel omits is a link to nothing.
    """
    try:
        from .main import _dev_controls_are_offered
        return bool(_dev_controls_are_offered(live))
    except Exception:                              # noqa: BLE001
        return False


def _pay_label(p: dict) -> str:
    """Which payment a sandbox row is about: "PO-7001 · Atlanta Packaging · USD 1,800".

    The number, who is being paid and how much, in that order -- the same three
    facts the payment card leads with, so a row in the panel and the card it
    came from read as the same thing.
    """
    bits = [str(p.get("number") or p.get("payable_number") or "payment"),
            str(p.get("supplier") or "supplier"),
            money_str(int(p.get("amount_minor") or 0), p.get("currency") or "USD")]
    return " · ".join(bits)


def _sandbox_link(transfer_id: str) -> str:
    """A jump from a payment card to its row in the fenced panel.

    A plain anchor cannot open a collapsed <details>, so this carries a hook the
    page's script uses to open the panel and scroll to the row -- otherwise the
    link lands on a closed box and appears to do nothing.
    """
    if not transfer_id:
        return ""
    ref = e(str(transfer_id))
    return (f'<a class="x-devjump" href="#sandbox-row-{ref}" '
            f'data-open-sandbox="{ref}" data-testid="sandbox-jump-{ref}">'
            f'Sandbox controls ↓</a>')


def _sandbox_panel(simulate: str = "", devs: str = "") -> str:
    """THE ONE FENCED PANEL holding every sandbox-only control on a screen.

    These buttons do not exist in a real deployment: one invents a deposit, the
    others ask Airwallex to move an in-flight transfer to its end state. They
    were spread across the pages, styled like the operator's own controls, which
    made the demo machinery look like part of the product.

    Dashed border and an explicit label, collapsed by default, and it renders
    NOTHING when there is nothing to put in it -- so a screen with nothing to
    simulate shows no trace of the sandbox at all.
    """
    body = (simulate or "") + (devs or "")
    if not body.strip():
        return ""
    # OPEN WHEN IT HOLDS THE DEPOSIT FORM. The fence exists to keep demo
    # machinery out of the operator's view, and the terminal-state buttons can
    # stay behind a summary line. The deposit form cannot: it is the one control
    # a tester (and the live demo) has to reach, and inside a COLLAPSED <details>
    # its fields are never rendered, so a real browser reports them as invisible
    # and types into nothing. Collapsed-by-default is what cost run 7 every
    # deposit test; `open` when there is something to type into, collapsed when
    # it only holds buttons that move a transfer to its end state.
    opened = " open" if simulate.strip() else ""
    return (f'<details class="x-sandbox x-span-12" id="sandbox-controls" '
            f'data-testid="sandbox-controls"{opened}>'
            '<summary><span class="x-sandbox-tag">sandbox only</span>'
            '<b>Sandbox controls</b>'
            '<span class="x-hint">Not part of the product. These move sandbox '
            'money so the demo can reach the interesting states.</span>'
            '</summary>'
            f'<div class="x-sandbox-body">{body}</div></details>')


def bank_words(live) -> tuple[str, str]:
    """(words, tone) for what the BANK says, with the placeholders spelled out.

    `live_status` is a machine value: "UNCHECKED" before a lookup lands, and
    "UNKNOWN" when one failed. Both were rendered verbatim next to "Sent" on a
    payment that had just been approved, so run 7 read a payment that was moving
    as one whose state nobody knew. One function, so every screen that shows a
    bank status says the same human thing, and no screen can print the token.
    """
    value = str(live or "").strip().upper()
    # ONLY the placeholders are re-worded. A real status is the BANK'S OWN WORD
    # and is passed through exactly as Airwallex reports it -- re-casing it here
    # would put "Processing" on a card where every other screen says PROCESSING.
    if value in ("UNCHECKED", ""):
        return "Checking with the bank…", "info"
    if value == "UNKNOWN":
        return "Could not reach the bank", "warn"
    tone = ("good" if value in ("PAID", "COMPLETED", "SUCCESS")
            else "bad" if value in ("CANCELLED", "FAILED", "RETURNED")
            else "info")
    if value == "SCHEDULED":
        # THE BANK'S "SCHEDULED" IS NOT A QUESTION, AND NOT A FINISH. It is a
        # FUNDING status: the money is on its way on the transfer's own date, so
        # a transfer sitting on it is IN FLIGHT and the money is committed. The
        # bare token read as "waiting for me to do something" (run 8 tried to
        # Fail it twice and to Settle it), and the word an operator needs is the
        # one that says the bank already has it. The token stays in the payload
        # for anyone reading the raw state; this is what the page says.
        return "Sent, the bank has scheduled it", "info"
    return value, tone


def _dev_controls(transfer_id: str, live: str, label: str = "") -> str:
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
    ref = html.escape(transfer_id, quote=True)
    action = f"/dev/transfers/{ref}/transition"
    buttons = ""
    for value, text, title in (
            ("settle", "Settle", "Airwallex reports PAID"),
            ("fail", "Fail", "beneficiary details rejected"),
            ("return", "Return", "the beneficiary bank sent it back")):
        buttons += (
            f'<form method="post" action="{action}" style="display:inline">'
            f'<input type="hidden" name="what" value="{value}">'
            # PER-TRANSFER TESTIDS. Several of these panels can be on one page,
            # and "the Settle button" is ambiguous the moment there is more than
            # one payment -- a test that has to guess which one it pressed is
            # not testing the transfer it thinks it is.
            f'<button class="x-btn x-btn--sm" type="submit" title="{e(title)}" '
            f'data-testid="sandbox-{value}-{ref}">{e(text)}</button></form>')
    # WHICH PAYMENT IS THIS ROW ABOUT? The panel collected every control on the
    # page, so without this a Settle button was a button with no subject.
    who = (f'<span class="x-devlabel" data-testid="sandbox-who-{ref}">'
           f'{e(label)}</span>' if label else "")
    return (f'<div class="x-meta x-devrow" id="sandbox-row-{ref}" '
            f'data-transfer="{ref}">{who}'
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
        '<option value="refund">Refund owed: record the liability</option>'
        '<option value="income">Release as unallocated income</option>'
        '<option value="reject">Not this invoice: leave it unmatched</option>')

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
            + f'<span><b>{e(head)}</b>, {e(why)}. Decided by '
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
        opts = account_options_html(accts)
        usable = sum(1 for a in accts if a.get("usable"))
        acct_field = (f'<div class="x-field x-span-4">'
                      f'<label for="f-global_account_id">Global account</label>'
                      f'<select class="x-select" id="f-global_account_id" '
                      f'name="global_account_id">{opts}</select>'
                      f'<span class="x-hint">From GET /global_accounts'
                      f'{", none are ACTIVE yet" if not usable else ""}. The '
                      f'deposit lands in the account currency.</span></div>')
    elif not ready:
        acct_field = ('<div class="x-field x-span-4">'
                      '<label>Global account</label>'
                      '<select class="x-select" disabled><option>none found'
                      '</option></select>'
                      '<span class="x-hint">No global account could be listed, '
                      'so a deposit cannot be simulated.</span></div>')
    else:
        # Sandbox is configured and the list is simply not here YET. Say that,
        # rather than "none found" -- which is a claim about the account setup
        # and would be wrong.
        acct_field = accounts_loading_field()

    body = (
        '<div class="x-cardhead"><div><h3>Simulate a customer payment</h3>'
        '<p class="x-help">Calls POST /api/v1/simulation/deposit/create on the '
        'Airwallex sandbox. It creates a real sandbox deposit: no real money, '
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
                 hint="Free text. Typos welcome: that is the point.")
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
        label += ", not active yet"
    return label


def account_options_html(accts: list) -> str:
    """The <option> list for the deposit picker. ONE builder.

    Shared by the page and by the tiny JSON endpoint the page falls back to, so
    the options that arrive late are byte-identical to the ones that would have
    been there on a warm cache.
    """
    out = []
    for a in accts or []:
        title = " · ".join(x for x in (a.get("country"), a.get("status")) if x)
        out.append(
            f'<option value="{e(a["id"])}"'
            + ("" if a.get("usable") else " disabled")
            + (f' title="{e(title)}"' if title else "")
            + '>'
            # "USD account …5888", not "USD · US · ACTIVE · 5888". The operator
            # is picking an ACCOUNT and the last four digits are what tells two
            # of them apart; country and status stay as a hover title.
            + _acct_label(a)
            + '</option>')
    return "".join(out)


def accounts_loading_field() -> str:
    """The picker, before the account list has arrived.

    THE PAGE MUST NOT WAIT FOR AIRWALLEX. It did: the refresh ran before the
    render, which put ~2.8s on the first Money in view after a restart. The
    cache is now warmed at startup and this is what renders if it is still not
    there -- the same control, saying what it is doing, filled in by the page
    once the background refresh lands.
    """
    return ('<div class="x-field x-span-4">'
            '<label for="f-global_account_id">Global account</label>'
            '<select class="x-select" id="f-global_account_id" '
            'name="global_account_id" data-loading="1" disabled>'
            '<option>Loading accounts…</option></select>'
            '<span class="x-hint" data-testid="accounts-loading">'
            'Fetching the global accounts from Airwallex. The form will fill '
            'in by itself.</span></div>')


def _money_in(c: dict) -> str:
    rows = []
    for r in c["receivables"]:
        done = r["status"] in ("PAID", "OVERPAID")
        # "USD 0.00 of 4,500.00" is accurate but reads like a rendering bug;
        # name the state instead.
        paid_label = (f'{r["currency"]} {money(r["paid_minor"])} of '
                      f'{money(r["amount_minor"])}' if r["paid_minor"]
                      else "Nothing paid yet")
        # WHAT IS STILL OWED, in words. A part-paid invoice showed as
        # "USD 800.00 of 1,200.00 · 66%", so the reader had to do the
        # subtraction to find the USD 400.00 they are chasing — on the tab whose
        # whole job is what came in and what it was for.
        owed = r.get("open_minor") or 0
        owed_line = (f'<div class="x-help" data-testid="still-to-collect">'
                     f'{r["currency"]} {money(owed)} still to collect</div>'
                     if (not done and r["paid_minor"] and owed > 0) else "")
        rows.append(
            '<div class="x-inv">'
            f'<div class="x-inv-who"><b>{e(r["number"])}</b>'
            f'<small>{e(r["customer"] or "Unknown customer")}</small></div>'
            '<div><div class="x-inv-amt">'
            f'<span>{paid_label}</span>'
            f'<b>{r["pct"]}%</b></div>'
            f'<div class="x-bar{_flag(done, "x-bar--done")}">'
            f'<span style="width:{max(2, r["pct"])}%"></span></div>'
            f'{owed_line}</div>'
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
                "No decision was recorded for this payment: re-run the demo "
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
            'person: it is on <b>Needs your OK</b>, where you can match it, '
            'record a refund owed, or release it as income. Until one of those '
            'is done this cash <b>is not spendable</b>: it is in the bank, and a '
            'bill in this book cannot be paid from it.</span></div>')
    empty_un = ('<p class="x-empty">Nothing is held. Every deposit that landed '
                'was matched with certainty.</p>')
    side = _card(span="x-span-4", inner=(
        '<div class="x-cardhead"><div><h3>Money we couldn\'t match</h3>'
        '<p class="x-help">Unmatched: not applied to any invoice, and no '
        'supplier paid from it.</p></div>'
        f'<span class="x-pill x-pill--warn">{n_unmatched} held</span></div>'
        f'<div class="x-stack">{("".join(un)) if un else empty_un}</div>'
        + note))

    # detail=True: this is the tab where a reader asks "why did it decide
    # that", so the rows carry their receipt. The Home summary stays short.
    return (_sandbox_panel(simulate=_simulate_card(c))
            + _timeline(c) + inv + side)


# ---- money out
_STEP_LABELS = ["Planned", "Checked", "Approved", "Sent", "Confirmed"]

#: Where a payment STOPPED, as opposed to where it currently is. A cancelled
#: transfer auto-cancels within seconds of failing, so the bank reports
#: CANCELLED for both; the step it reached is what tells the operator how far it
#: got, and a red X there is the difference between "it never left" and "the
#: money went and came back".
#: EXACTLY the three terminal states that mean "this did not complete".
#: REJECTED is deliberately NOT here: it is a human declining an approval, so
#: the payment never started and the tracker should still read as not-yet-sent
#: rather than as a failed transfer.
_STOPPED_STATUSES = {"FAILED", "CANCELLED", "RETURNED"}


def _tracker_legend(stopped: bool = False) -> str:
    """A tiny key under the tracker. Five steps with no key read as decoration."""
    bits = [("is-done", "done"), ("is-now", "current")]
    if stopped:
        bits.append(("is-stopped", "stopped here"))
    bits.append(("", "next"))
    return ('<div class="x-legend" data-testid="tracker-legend">'
            + "".join(f'<span class="{cls}"><i></i>{e(txt)}</span>'
                      for cls, txt in bits) + '</div>')


def _step_index(status: str) -> int:
    return {
        "PROPOSED": 0, "APPROVED": 2, "SUBMITTED": 3,
        "PAID": 4,
        # The step each one STOPPED at. A failed or returned transfer got out of
        # the door (Sent); a cancelled one never left (Approved). Without these
        # the X landed on "Checked", which says the opposite.
        "FAILED": 3, "RETURNED": 3, "CANCELLED": 2,
    }.get(status, 1)


def _money_out(c: dict) -> str:
    cards = []
    # Collected rather than rendered inline: the sandbox buttons belong in ONE
    # fenced place, not scattered down the payment list looking like the
    # operator's own controls.
    dev_targets: list = []
    for p in c["payables"]:
        st = p["display_status"]
        idx = _step_index(st)
        # A FAILED OR RETURNED PAYMENT KEEPS ITS RED X. It used to render as a
        # tick up to the step it reached, which reads as progress and hides that
        # the money stopped there.
        # ONLY A PAYMENT THAT ACTUALLY STOPPED GETS THE X. This also asked
        # whether an EARLIER attempt had failed, which painted a red cross on a
        # perfectly healthy PROCESSING transfer -- PO-7001 read "checked,
        # approved, sent, ✕ stopped here" while Airwallex was still moving the
        # money. A replaced payment's history is a fact about the past, not
        # about where this transfer is.
        stopped = st in _STOPPED_STATUSES
        steps = []
        for i, label in enumerate(_STEP_LABELS):
            if stopped and i == idx:
                cls, mark = "is-stopped", "✕"
            elif stopped and i > idx:
                cls, mark = "", str(i + 1)      # never reached: "next", not "done"
            else:
                cls = "is-done" if i < idx else ("is-now" if i == idx else "")
                mark = "✓" if i < idx else str(i + 1)
            steps.append(f'<li class="{cls}"><i>{mark}</i>'
                         f'<span>{e(label)}</span></li>')
        done = st == "PAID"
        live = p.get("live_status")
        # Read HERE rather than further down: the badge needs it to tell
        # "approved, not yet sent" from "approved and already at the bank".
        obj = p.get("airwallex_object_id")
        # 6. WHAT CAME BACK, AND WHY. A returned transfer arrives from the bank
        # as CANCELLED, which on its own reads as "someone stopped this". The
        # reason the bank gave is the whole point of the row.
        # A REASON BELONGS TO A FAILURE, NOT TO A PAYMENT. The incident store
        # keeps the reason a transfer failed for, and a transfer that later
        # SUCCEEDS still has that row behind it: pinning it to the card made a
        # paid payment read "account closed". So the reason is shown only while
        # the transfer is where that reason applies -- the bank says it ended
        # badly, or we do not know yet and our own last word was a bad one.
        back_why = (p.get("failure_reason") or p.get("return_reason")
                    or p.get("failure_type") or "")
        # ONLY A KNOWN-GOOD ANSWER BURIES A REASON. This used to clear it for
        # any non-terminal live status, including the placeholders: after a
        # restart the cache is empty, the card reads "Checking with the bank…",
        # and the reason the operator had already read disappeared with it.
        # "We do not know yet" is not evidence that the payment succeeded.
        if back_why and live in ("PAID", "COMPLETED", "SUCCESS"):
            back_why = ""
        # A CONCLUSION ABOUT A DIFFERENT ATTEMPT. When the current row has no
        # transfer of its own, the reason on file belongs to the attempt the
        # bank refused -- so the card says WHICH attempt it is talking about,
        # next to the status that is actually current.
        previous_why = ""
        if back_why and not p.get("airwallex_object_id"):
            previous_why, back_why = back_why, ""
        # The badge reports the BANK. A submitted transfer is not a paid one,
        # and in this project every transfer is sandbox money.
        if live in ("PAID", "COMPLETED", "SUCCESS"):
            badge = '<span class="x-pill x-pill--good">Paid</span>'
        elif live in ("UNCHECKED", "UNKNOWN"):
            # NEVER SHOW THE MACHINE'S PLACEHOLDER. `live_status` is "UNCHECKED"
            # until a lookup lands and "UNKNOWN" when one failed, and both were
            # printed verbatim on a SENT payment: run 7 read "UNCHECKED" beside
            # "Sent" and could not tell whether the money had moved. A person
            # needs to know the app is still finding out, or that it could not.
            waiting = live == "UNCHECKED"
            badge = (f'<span class="x-pill x-pill--info" '
                     f'data-testid="{"checking-with-bank" if waiting else "bank-unreachable"}">'
                     f'{"Checking with the bank…" if waiting else "Could not reach the bank"}</span>')
            # AND THE REASON WE ALREADY HAVE. After a restart the live-status
            # cache is empty, so every card reads "Checking with the bank…" --
            # and the caption a person had already read vanished behind it.
            # What the bank said is a stored fact, not a status.
            if back_why:
                badge += (f'<span class="x-why" data-testid="return-reason">'
                          f'{e(_shown(back_why))}</span>')
        elif live and live in ("CANCELLED", "FAILED", "RETURNED"):
            # THE REASON HAS TO SURVIVE THE BANK'S ANSWER. This used to be the
            # `elif live:` branch, so the moment the live status arrived the card
            # showed "CANCELLED" and nothing else: the reason a person needs --
            # rejected by the beneficiary bank, a wrong account number -- was
            # only rendered while the status was still OUR stored value, which in
            # the running app is never. The reason is the row. Run 8 asked for it
            # in the trail as well as on the badge, so it appears in both.
            badge = f'<span class="x-pill x-pill--bad">{e(live)}</span>'
            if back_why:
                badge += (f'<span class="x-why" data-testid="return-reason">'
                          f'{e(_shown(back_why))}</span>')
        elif live:
            badge = f'<span class="x-pill x-pill--info">{e(live)}</span>'
        elif st in ("CANCELLED", "FAILED"):
            badge = f'<span class="x-pill x-pill--bad">{e(st)}</span>'
            if back_why:
                badge += (f'<span class="x-why" data-testid="return-reason">'
                          f'{e(_shown(back_why))}</span>')
        elif st == "APPROVED" and not obj:
            # 5(b) APPROVED IS NOT A STATE THE BANK KNOWS. Between the approval
            # and the transfer having a reference there is a real wait, and the
            # card used to sit on "APPROVED" with an empty reference line --
            # which reads as a stuck payment rather than one in flight.
            badge = ('<span class="x-pill x-pill--info" '
                     'data-testid="sending-to-bank">Sending to the bank…</span>')
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
            # THE BLOCKER GOES ON THE CARD. It used to say "Held by policy" and
            # link away, so three bills in run 5 showed a verdict and no reason
            # at all. A person looking at a held payment needs the sentence with
            # the numbers in it, here, and the link is a bonus rather than the
            # whole answer.
            note = (f'<div class="x-callnote" style="margin-top:20px">'
                    f'{_I["info"]}<div>{blockers_html(p)}'
                    f'<p><a href="/app?tab=home#held-{e(p["number"])}">'
                    f'See it with the rest of the plan</a></p></div></div>')
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
        ref = ""
        if obj:
            live = p.get("live_status")
            live_txt, live_tone = bank_words(live)
            # The bank's own identifier is a 36-character UUID and the
            # environment is the same word on every card. Both are needed when
            # something goes wrong and neither is worth a line on every row, so
            # the status stays visible (it is the answer) and the reference and
            # the environment fold away behind it.
            ref = (f'<details class="x-hist x-hist--bank" data-testid="bank-ref">'
                   f'<summary><span class="x-label">On the bank</span>'
                   f'<span class="x-pill x-pill--{live_tone}"'
                   f'>{e(live_txt)}</span></summary>'
                   f'<div class="x-meta"><span class="x-label">Reference</span>'
                   f'<span class="x-tag x-mono">{e(str(obj))}</span></div>'
                   + (f'<div class="x-meta"><span class="x-label">The bank says'
                      f'</span><span class="x-tag" data-testid="trail-reason">'
                      f'{e(_shown(back_why))}</span></div>' if back_why else "")
                   + f'<div class="x-meta"><span class="x-label">Environment</span>'
                   f'<span class="x-tag">sandbox</span></div>'
                   f'</details>')
            # Registered for the one Sandbox controls panel at the foot of the
            # page, rather than rendered here among the operator's own buttons.
            # The label travels with it so the row can name its payment.
            dev_targets.append((str(obj), live, _pay_label(p)))

        if previous_why and not obj:
            # THE ATTEMPT THAT WENT TO THE BANK, FOLDED AWAY BUT PRESENT. A
            # replacement has no transfer of its own, so the block above never
            # runs for it and the trail said nothing at all about a payment the
            # operator watched come back. The reference is worth keeping too:
            # it is what a support call asks for.
            ref = (f'<details class="x-hist x-hist--bank" data-testid="bank-ref">'
                   f'<summary><span class="x-label">Previous attempt</span>'
                   f'<span class="x-pill x-pill--bad">'
                   f'{e(p.get("bank_attempt_status") or "ended")}</span></summary>'
                   f'<div class="x-meta"><span class="x-label">Reference</span>'
                   f'<span class="x-tag x-mono">{e(str(p.get("bank_attempt_id") or ""))}'
                   f'</span></div>'
                   f'<div class="x-meta"><span class="x-label">The bank says</span>'
                   f'<span class="x-tag" data-testid="trail-reason">'
                   f'{e(_shown(previous_why))}</span></div>'
                   f'</details>')
            # A jump to this payment's row in the panel. Only when the panel will
            # actually have a row for it, or the link goes nowhere.
            if _dev_controls_are_offered_for(live):
                ref += _sandbox_link(str(obj))

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
            # THE PREVIOUS ATTEMPT'S REASON, outside the bank block on purpose:
            # a replacement has no transfer of its own, so the `if obj` branch
            # that holds the rest of the bank detail never runs for it -- which
            # is exactly the card that needs this line.
            + (f'<div class="x-why" data-testid="previous-reason">'
               f'{e(_shown(previous_why))} '
               f'<em>(the previous attempt)</em></div>' if previous_why else "")
            + f'<ol class="x-steps">{"".join(steps)}</ol>'
            f'{_tracker_legend(stopped)}'
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

    devs = "".join(_dev_controls(tid, live, lab) for tid, live, lab in dev_targets)
    return head + prob + _sandbox_panel(devs=devs)


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
                             f'{_I["check"]}<span>Already approved: waiting for the '
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
        n_dep = len(c.get("inflows_waiting") or [])
        if n_dep:
            # NEVER "Nothing needs your OK" above deposits that do.
            head = "No payments need your OK"
            sub = (f"{n_dep} deposit{'s' if n_dep != 1 else ''} below "
                   f"need{'s' if n_dep == 1 else ''} your decision.")
        else:
            head = "Nothing needs your OK"
            sub = "Every payment is inside your limits, or already decided."
        body = _card(inner=(
            '<div class="x-banner"><div class="x-banner-l">'
            f'<span class="x-banner-ico">{_I["check"]}</span>'
            f'<div><h2>{head}</h2>'
            f'<p>{sub}</p></div>'
            '</div></div>'))

    note = _card(inner=(
        '<div class="x-note">' + _I["lock"] +
        '<span>Approving needs your PIN and records a decision. It does not itself '
        'move money: the executor re-checks the amount, the supplier and the rate '
        'against the hash you approved, every single time.</span></div>'))

    # Anything held, with the planner's exact rule. The header says nothing needs
    # the reader; this is the evidence for that claim, on the same screen.
    held = ""
    waits = c.get("waiting") or []
    if waits:
        held = _card(inner=(
            '<div class="x-cardhead" id="held"><div><h3>Held, and nothing to send yet, and not '
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
                f'{e(policy_words(p.get("plan_policy_reason")))}</span></li>'
                for p in waits)
            + '</ul>'))

    # A payment that arrived but was NOT matched with confidence is also
    # waiting on a human — and it is not a payment step, so the approvable list
    # never contained it. Shown here so "Needs your OK" means what it says.
    wait = c.get("inflows_waiting") or []
    # Every card that belongs after the main body is gathered into ONE string and
    # appended once. Carrying two hand-built returns meant the second one won by
    # accident and silently dropped a card that had been built correctly.
    # ALWAYS RENDERED, even with nothing to show: the component carries its own
    # empty state, so "nothing needs you" is a sentence rather than a missing
    # card. Guarding it on `wait` made that state unreachable -- the screen went
    # quiet exactly when it had the most reassuring thing to say.
    note_in = _timeline(c, only_needing_human=True)


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
        f'<span class="x-pill x-pill--{tone}" data-testid="deposit-state">'
        f'{e(_deposit_state(a))}</span>'
        f'<span class="x-hint">{e(method)} &middot; confidence {e(conf_txt)}'
        f'{e(applied)}</span></div>'
        f'<div class="x-mono" style="margin-top:6px">ref: {e(ref)}</div>')
    if detail and a.get("reason"):
        out += f'<div class="x-hint" style="margin-top:4px">{e(a["reason"])}</div>'
    rec = a.get("receipt") or {}
    if detail and rec:
        # MODEL SAID / CODE DECIDED, on the payment itself. Not a summary of the
        # two -- the two, side by side, so the reader can see which number the
        # system acted on and which one it only considered.
        out += _receipt(rec.get("model") or {}, rec.get("code") or {})
    if detail and a.get("deposit_id"):
        out += (f'<div class="x-hint">Airwallex deposit '
                f'<span class="x-mono">{e(a["deposit_id"])}</span></div>')
    return out + "</div>"


_RESOLUTION_WORDS = {
    "REFUND_OWED": "Refund owed",
    "RELEASED_INCOME": "Released as income",
    "APPROVED": "Matched",
}


def _deposit_state(a: dict) -> str:
    """What this payment IS now, not what the matcher first called it.

    THE BADGE WAS READING THE STALE FIELD. It showed `match_status`, which is
    the matcher's verdict from when the money arrived and never changes -- so a
    payment resolved as a refund owed still read PROPOSED, and a resolved
    payment looked like one still waiting. The resolution is a later, stronger
    statement about the same row and it wins.
    """
    resolved = (a.get("review_state") or "").upper()
    word = _RESOLUTION_WORDS.get(resolved)
    if word:
        return word
    st = str(a.get("match_status") or "")
    # A deposit is never "Not sent yet": that is a payment's word. A proposed
    # deposit is a suggestion waiting on a person.
    return "SUGGESTED" if st.upper() == "PROPOSED" else st


#: What a plan step's reason CODE means, in words. A code is a machine value:
#: `insufficient_balance` beside a payment told an operator nothing, and run 9
#: read it as the only explanation on a payment that was already approved and
#: was merely waiting for cash. The code stays in the payload for anyone reading
#: the raw state; the page says the thing a person needs to know.
_POLICY_WORDS = {
    "insufficient_balance": "waiting on cash",
    "reserve_floor": "waiting on the safety cushion",
    "dependency_not_met": "waiting on a customer to pay",
    "no_rate": "no rate for this currency yet",
    "unsupported_currency": "currency not supported yet",
    "needs_approval": "needs your OK",
    "over_auto_limit": "over the automatic limit",
    "budget_currency": "waiting on cash",
    "waits_for_inflow": "waiting on cash",
}


def policy_words(code) -> str:
    """The human words for a plan reason code, never the raw token."""
    value = str(code or "").strip()
    if not value:
        return "held"
    if value in _POLICY_WORDS:
        return _POLICY_WORDS[value]
    return value.replace("_", " ")


def _timeline_row(a: dict, *, needs_human: bool) -> str:
    """ONE deposit, collapsed, expanding into its receipt and its resolution.

    The three lists this replaces -- "What arrived", the model activity strip
    and the review queue -- each held a different FRAGMENT of the same payment,
    so a reader had to visit three places and hold the joins in their head. A
    deposit arrived once; it is listed once.
    """
    conf = a.get("confidence")
    tone = ("good" if a.get("settled_match")
            else ("bad" if (conf is not None and conf < 0.75) else "warn"))
    can_resolve = bool(a.get("resolvable"))
    return (
        f'<details class="x-tl" data-testid="timeline-row" '
        f'data-deposit="{a["id"]}" id="deposit-{a["id"]}"'
        + (" open" if needs_human else "") + '>'
        f'<summary><b>{e(money(a["amount_minor"], a["currency"]))}</b>'
        f'<span class="x-hint">{e(a["payer"] or "payer not given")}</span>'
        f'<span class="x-pill x-pill--{tone}" data-testid="deposit-state">'
        f'{e(_deposit_state(a))}</span>'
        + ('<span class="x-pill x-pill--warn" data-testid="needs-you">'
           'needs you</span>' if needs_human else '')
        + '</summary>'
        '<div class="x-tl-body">'
        + _arrival_row(a, detail=True)
        # THE RESOLUTION IS HERE TOO, next to the evidence for it. It used to be
        # a separate list, which meant the form and the facts it acts on were
        # never on screen together.
        + (_resolve_form(a) + _reask_form(a) if can_resolve else "")
        + _resolution_audit(a)
        + '</div></details>')


def _timeline(c: dict, *, only_needing_human: bool = False,
              span: str = "x-span-12") -> str:
    """Every deposit, once. Or only the ones a person has to decide.

    `only_needing_human` is the ONLY difference between the Money in timeline
    and Needs your OK, so the two cannot disagree about what a deposit is --
    which is what let a payment look resolved on one screen and open on the
    other.
    """
    rows = list(c.get("arrivals") or [])
    if only_needing_human:
        rows = [a for a in rows
                if a.get("awaiting_human") and (a.get("suspense_minor") or 0) > 0]
    if not rows:
        empty = ('<p class="x-empty">Nothing here needs a person. Every deposit '
                 'that landed was matched with certainty.</p>'
                 if only_needing_human else
                 '<p class="x-empty">No deposits yet. Money that arrives appears '
                 'here, with what the code decided about it.</p>')
        return _card(span=span, inner=empty)
    head = (
        '<div class="x-cardhead"><div><h3>'
        + ("Deposits needing you" if only_needing_human
           else "One timeline of every deposit")
        + '</h3><p class="x-help">'
        + ("Only the deposits a person has to decide. Each one opens on its "
           "receipt and its resolution."
           if only_needing_human else
           "What arrived, what the model said, what the code decided, and where "
           "the money went. Each row expands.")
        + '</p></div>'
        f'<span class="x-pill x-pill--warn">{len(rows)} to review</span>'
        '</div>' if only_needing_human else
        '<div class="x-cardhead"><div><h3>One timeline of every deposit</h3>'
        '<p class="x-help">What arrived, what the model said, what the code '
        'decided, and where the money went. Each row expands.</p></div>'
        f'<span class="x-pill x-pill--accent">{len(rows)} '
        f'deposit{"s" if len(rows) != 1 else ""}</span></div>')
    return _card(span=span, inner=(
        head + '<div class="x-timeline" data-testid="money-timeline">'
        # A ROW THAT HOLDS A DECISION OPENS ITSELF. `needs_human` used to be
        # `only_needing_human`, so on Money in every row was collapsed and the
        # resolve form was rendered INSIDE the closed body: the select and both
        # fields were in the DOM and unreachable, five tester tests timed out on
        # actionability, and a person saw a row with no hint that a decision was
        # waiting inside it. Needs your OK opened its rows, which is why the same
        # card worked there and why an empty-book sweep saw nothing.
        + "".join(_timeline_row(a, needs_human=(only_needing_human
                                               or bool(a.get("resolvable"))))
                  for a in rows)
        + '</div>'))


def _arrivals_card(c: dict, *, span: str = "x-span-12", detail: bool = True,
                   limit: int = 6, title: str = "What arrived",
                   sub: str = "") -> str:
    arr = (c.get("arrivals") or [])[:limit]
    body = "".join(_arrival_row(a, detail=detail) for a in arr) if arr else (
        '<p class="x-empty">Nothing has landed yet. Use the form above, or make '
        'a real transfer into your Airwallex Global Account.</p>')
    return _card(span=span, inner=(
        '<div class="x-cardhead"><div><h3>' + e(title) + '</h3>'
        '<p class="x-help">' + e(sub or "Each row is a real sandbox deposit, "
        "with the reference exactly as the bank gave it and the verdict the "
        "matcher recorded.") + '</p></div></div>' + body))


def _card_decisions_card(c: dict) -> str:
    """Card authorisations, in the same three columns as every other decision.

    A card is where Closebook's limits stop being a sentence: the bank refuses
    the purchase and the money does not move. Showing it the same way as a
    matching decision is the point -- suggestion, the checks that ran, result.
    """
    rows = []
    for d in (c.get("card_decisions") or [])[:6]:
        declined = str(d.get("result") or "").lower().startswith("declin")
        # a decision stored without prose still has to read as a sentence
        said = str(d.get("suggestion") or "").strip() or (
            "authorise " + money_str(int(d.get("amount_minor") or 0),
                                     str(d.get("currency") or "USD")))
        checks = "".join(
            f'<div class="x-receipt-row {"is-pass" if bool(x[1]) else "is-fail"}">'
            f'<span class="x-tick">{_I["check"] if bool(x[1]) else _I["x"]}</span>'
            f'<b>{e(str(x[0]))}</b></div>'
            for x in (d.get("checks") or []) if isinstance(x, (list, tuple)))
        bank = (f'<div class="x-receipt-row"><span class="x-hint">The bank said: '
                f'{e(str(d.get("bank_reason")))}</span></div>'
                if d.get("bank_reason") else "")
        provenance = ('<div class="x-receipt-row"><span class="x-hint">live from '
                      'the issuing sandbox</span></div>' if d.get("live") else
                      '<div class="x-receipt-row"><span class="x-hint">our own '
                      'check, no bank call</span></div>')
        rows.append(
            '<div class="x-receipt" data-testid="card-decision" style="margin-bottom:14px">'
            '<div class="x-receipt-col x-receipt-col--model">'
            '<div class="x-receipt-head">AI suggestion</div>'
            f'<div class="x-receipt-row"><b>{e(said)}</b></div>'
            '<div class="x-receipt-row"><span class="x-hint">card '
            f'{e(str(d.get("card_id") or "")[:12])}</span></div></div>'
            '<div class="x-receipt-col x-receipt-col--checks">'
            f'<div class="x-receipt-head">Rule checks</div>{checks}</div>'
            '<div class="x-receipt-col x-receipt-col--code">'
            '<div class="x-receipt-head">Result</div>'
            f'<div class="x-receipt-row {"is-fail" if declined else "is-pass"}">'
            f'<span class="x-tick">{_I["x"] if declined else _I["check"]}</span>'
            f'<b>{e(str(d.get("result") or ""))}</b></div>{bank}{provenance}'
            '</div></div>')
    if rows:
        tail = ('<p class="x-help" style="margin-top:10px">Both amounts were sent '
                'against the same card. The one over the limit was refused by the '
                "card itself, and the reason above is the bank's, not ours.</p>")
    else:
        tail = ('<p class="x-empty">No card has been authorised yet. A card is how '
                'a spend limit refuses a purchase instead of asking a model '
                'nicely: the bank declines and the money does not move. Nothing '
                'is issued until issuing credentials are configured.</p>')
    return _card(span="x-span-12", inner=(
        '<div class="x-cardhead"><div><h3>Card authorisations</h3>'
        '<p class="x-help">The limit lives on the card, so the refusal happens '
        'at Airwallex and not on this page.</p></div></div>'
        + "".join(rows) + tail))


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
    for t in c["agent_trace"]:
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
                "rules_raw": "", "receipt": ""}

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
                what = (f'{e(ins.get("payer") or "a payer")}, '
                        f'{money_str(amt, ins.get("currency") or "USD")}').strip()
            claim = float((prop or {}).get("confidence") or 0)

            if prop and prop.get("allocations"):
                says = " · ".join(
                    f'{a.get("invoice_number")} '
                    f'{money(int(float(a.get("amount") or 0) * 100))}'
                    for a in prop["allocations"])
                reply = (f'I think this is <b>{e(says)}</b>: about '
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
            # The same two-column receipt, built from the TRACE. The AI screen
            # is where a reader asks "what did it claim, and what was enforced",
            # so it gets the same component the payment does rather than a
            # differently-worded version of it.
            # THE TWO COLUMNS, from what this trace actually holds. A
            # propose_match trace carries the model's proposal under
            # `model_proposed`; the code's half is the CAP, which is the rule
            # that fired on every model reading: the claim is pinned below the
            # auto band before it can matter. Reading `outputs.evidence` here
            # found nothing, so nothing was ever struck through and the override
            # was invisible on the one screen built to show it.
            _mp = prop if isinstance(prop, dict) else {}
            _alloc = _mp.get("allocations") or []
            _inv = ", ".join(str(a.get("invoice_number") or a.get("invoice") or "")
                             for a in _alloc
                             if (a.get("invoice_number") or a.get("invoice")))
            _amt_major = 0
            for a in _alloc:
                try:
                    _amt_major += int(round(float(a.get("amount") or 0) * 100))
                except (TypeError, ValueError):
                    pass
            _claimed = _mp.get("confidence")
            try:
                _final = (min(float(_claimed), _LLM_CAP)
                          if _claimed is not None else None)
            except (TypeError, ValueError):
                _final = None
            if outs.get("model_error"):
                model_col = {"unavailable": _shown(str(outs["model_error"]))}
            else:
                model_col = {
                    "invoice": _inv,
                    "amount": (money(_amt_major, ins.get("currency") or "")
                               if _amt_major else ""),
                    "confidence": _claimed,
                    "reason": _prose(_mp.get("rationale") or ""),
                    # STRUCK WHEN THE CODE OVERRODE IT. 0.90 struck through
                    # beside a final 0.89 says the whole thing in one glance.
                    "struck": bool(_claimed is not None and _final is not None
                                   and float(_claimed) > float(_final)),
                }
            # THE CODE COLUMN COMES FROM THE STORED DECISION. What the code did
            # with this claim -- used it, capped it, or ignored it because a
            # deterministic tier won -- is a fact about the payment, not about
            # the model's trace. Deriving it from the claim alone reported the
            # MODEL's number as the final one whenever RULE had won.
            _decided = (t.get("decision") or {}).get("code") or {}
            code_col = {
                "verdict": (_decided.get("verdict")
                            or ("held for you"
                                if _final is not None and _final < 0.90
                                else "applied")),
                "rule": (_decided.get("rule")
                         or (f"capped at the {_LLM_CAP:.2f} auto band"
                             if model_col.get("struck") else "re-checked in code")),
                "confidence": (_decided.get("confidence")
                               if _decided.get("confidence") is not None
                               else _final),
                "note": _decided.get("note") or "",
            }
            item.update(what=_shown(what), ai=reply, rules=_shown(_rules),
                        rules_raw=_raw_of(_rules), conf=conf_label,
                        receipt=_receipt(model_col, code_col))
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
        # THE PROSE STAYS; THE NARRATOR GOES. The model's own answer is real
        # content -- a refusal is information, and deleting it to make room for
        # the receipt would hide the case where the model declined to guess. What
        # goes is the FIRST PERSON: "I think this looks like INV-1001" is a
        # sentence about the model, and the reader is comparing a claim with a
        # verdict. The receipt then states the same claim in columns.
        bubble = (f'<div class="x-msg x-msg--ai">'
                  f'<span class="x-av x-av--ai">{_I["ai"]}</span>'
                  f'<div><span class="x-bubble"><b class="x-who">The model</b>'
                  f'{_prose(it["ai"], 240)}</span></div></div>')
        feed.append(
            '<li>'
            + bubble
            + f'<div class="x-aifoot"><span class="x-pill x-pill--'
            f'{"accent" if good else "warn"}">{e(it["verdict"])}</span>{conf_pill}'
            f'{model_pill}<span>about {e(it["what"])}</span></div>'
            f'<div class="x-msg x-msg--rules"><span class="x-av x-av--rules">{_I["scale"]}</span>'
            f'<div><span class="x-bubble" data-testid="rules-bubble"><b class="x-who">The rules</b>'
            f'{e(it["rules"])}</span>'
            # THE MACHINE'S OWN WORDS, folded away. Every stored trace line is
            # translated on the way out, so a 429 recorded days ago reads as a
            # sentence today and its stack is one tap deep, not on the screen.
            + _detail_toggle(it.get("rules_raw") or "")
            + '</div></div>'
            + (it.get("receipt") or "")
            + '</li>')

    total = len(items)

    # THE COUNT IS THE STORE; THE LIST IS A WINDOW OF IT. Showing 24 rows

    # and calling that the number made a run of 26 read as 24, which is the

    # same class of lie as saying zero.

    shown = items[:24]

    feed = feed[:24]

    body = "".join(feed) if feed else (
        '<p class="x-empty" data-testid="ai-empty">No model calls yet. '
        'Record an inflow in <b>Money in</b> (simulate a customer payment, or '
        'poll Airwallex for one that landed) and the model\'s proposal and the '
        'code\'s verdict on it appear here side by side.</p>')

    if total > len(shown):

        body += (f'<p class="x-help" data-testid="ai-more">Showing the latest '

                 f'{len(shown)} of {total}.</p>')

    return _card_decisions_card(c) + matches + _card(span="x-span-12", inner=(
        '<div class="x-cardhead"><div><h3>What the model said, and what the rules did</h3>'
        '<p class="x-help">Straight from <span class="x-mono">agent_trace</span>. '
        'Nothing here is a decision: every row is one the code already made.</p></div>'
        '<span class="x-pill x-pill--accent" data-testid="ai-count">'
        + str(total) + (" call" if total == 1 else " calls")
        + '</span></div>'
        f'<ul class="x-feed">{body}</ul>'
    )) + _card(inner=(
        '<div class="x-note">' + _I["shield"] +
        '<span>The model can read invoices, explain a decision and ask for approval. '
        'It has no tool that can move money: that is a structural limit, not a '
        'setting.</span></div>'))


# ---- setup --------------------------------------------------------------
# Six screens, all of them WRITE THE BOOK and none of them move money. There is
# no transfer or conversion form anywhere on this page by design: money moves
# only through planner -> policy -> executor, and the way to keep that true is
# to give the UI nothing to call.
_CURRENCY_HINT = "3-letter code, e.g. USD"


def _known_good_helper() -> str:
    """The one beneficiary input we have PROVED works, on the form.

    Rendered from `beneficiary_form.KNOWN_GOOD_BENEFICIARY` rather than typed
    into the template, so the example and the value that was verified against
    the sandbox cannot drift apart.
    """
    from .domain.beneficiary_form import KNOWN_GOOD_BENEFICIARY as K

    return ('<div class="x-note" data-testid="beneficiary-example">'
            f'{_I["info"]}<span><b>A beneficiary input that works</b> (US, LOCAL, '
            f'USD): entity COMPANY, account {e(K["account_number"])}, routing '
            f'{e(K["routing_value"])} (aba), category {e(K["bank_account_category"])}, '
            f'and the FULL address: {e(K["street_address"])}, {e(K["city"])}, '
            f'{e(K["state"])}, {e(K["postcode"])}. Airwallex refuses a partial '
            f'address, so fill Street address, City, Postcode and State.</span>'
            f'</div>')


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

    if live in ("UNCHECKED", "UNKNOWN"):
        # A machine placeholder is not a state a person can act on, and printed
        # beside "Sent" it reads as a payment whose fate is unknown.
        words, tone = bank_words(live)
        return words, tone, (note or "Airwallex is being asked about this transfer.")
    if live:
        return live, ("bad" if live in ("FAILED", "CANCELLED", "RETURNED")
                      else "info"), (note or f"Airwallex reports {live}")
    if disp and disp not in ("HELD", "OPEN"):
        return disp, ("bad" if disp in ("FAILED", "CANCELLED") else "info"), note
    if action == "PAY":
        if p.get("plan_needs_approval"):
            return "NEEDS YOUR OK", "warn", (p.get("plan_approval_reason") or note)
        return "READY", "good", (note or "Within your limits: the planner will pay it.")
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
        return f'<span class="x-pill x-pill--{tone}">{e(label or key or ".")}</span>'
    tone = "info"
    if key in ("PAID", "MATCHED", "PAYABLE", "VERIFIED", "OK", "SUCCESS", "SETTLED"):
        tone = "good"
    elif key in ("OPEN", "HELD", "PENDING", "PARTIAL", "SUBMITTED", "PROCESSING",
                 "AWAITING_APPROVAL"):
        tone = "warn"
    elif key in ("OVERPAID", "FAILED", "CANCELLED", "REJECTED", "NO BENEFICIARY",
                 "INVALID", "PROBLEM"):
        tone = "bad"
    return f'<span class="x-pill x-pill--{tone}">{e(label or key or ".")}</span>'


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
            'Airwallex returns: so a plan you see on Home is a plan about your '
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
        + _field("reserve_floor", "Safety cushion",
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
    cust_rows = [[e(x["name"]), e(x["country"] or "."),
                  e(x["email"] or "."), str(x["open_count"])]
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
                         e(x["due_date"] or ".")])
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
        sup_rows.append([f'<strong>{e(x["name"])}</strong>', e(x["country"] or "."),
                         e(x["currency"] or "."), e(x["payout_method"]), state])
    out.append(_card(inner=(
        _cardhead("Suppliers", "A supplier is only payable once Airwallex has "
                               "given it a beneficiary. Validation runs first, "
                               "and any refusal is shown in Airwallex's own words.")
        # ONE KNOWN-GOOD INPUT, on the form a tester fills. This is not
        # decoration: a beneficiary is refused without the FULL address (Airwallex
        # answers with a schema dump naming beneficiary.address.postcode and
        # friends), and an operator with nothing to copy is stuck. The values come
        # from `beneficiary_form.KNOWN_GOOD_BENEFICIARY`, which is the input that
        # was verified by CREATING a real sandbox beneficiary.
        + _known_good_helper()
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
                 hint="LOCAL only. ABA for USD, sort code for GBP.")
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
                         e(x["payout_method"] or ".")])
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
        + _field("criticality", "Priority", span="x-span-2", value="3",
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
          'ledger entry is archived first: nothing is destroyed without a '
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
    "home": "Your money, what the AI did with it, and what needs you.",
    "money-in": "Money that has arrived. For each payment: who sent it, what it "
                "was for, and what Closebook did with it.",
    "money-out": "Supplier bills and where each payment has got to, from "
                 "planned to confirmed by the bank.",
    "ok": "The short list of things only a person can decide: unclear payments "
          "and payments over your limit.",
    "ai": "What the model suggested for each payment, and what the rules did "
          "with that suggestion.",
    "setup": "Add the customers, invoices, suppliers and bills Closebook works "
             "from. Everything here is real sandbox data.",
}


def needs_you_count(c: dict) -> int:
    """How many things are waiting on a person. ONE answer for the badge and Home."""
    return len(c.get("approvable") or [])


#: Machine states, in the words a person needs, with the reason behind them.
#: Nothing in the interface prints a raw status: HELD, AMBIGUOUS and SCHEDULED
#: are all true and all mean nothing to somebody reading the page for the first
#: time. The code keeps its own names; this is what the screen says.
_PLAIN_STATUS = {
    "HELD": ("Waiting", "Not sent yet. Something has to change first, or you "
                        "need to approve it."),
    "READY": ("Ready to pay", "Everything it needs is here."),
    "APPROVED": ("Approved", "A person approved it. It goes as soon as the "
                             "money is free to spend."),
    "PROPOSED": ("Not sent yet", "Planned, but nothing has been sent to the bank."),
    "SUGGESTED": ("Suggested", "A rule or the AI suggested an invoice. Nothing is "
                  "applied until a person decides."),
    "SUBMITTED": ("Sent, waiting for the bank", "Closebook has sent it and the "
                                                "bank has not finished with it."),
    "SCHEDULED": ("Sent, the bank has scheduled it",
                  "The bank has it and will move it on its own date."),
    "PROCESSING": ("Sent, waiting for the bank", "The bank is moving it."),
    "SENT": ("Sent, waiting for the bank", "The bank has it."),
    "PAID": ("Paid", "The bank confirmed it."),
    "CANCELLED": ("Cancelled", "It did not complete."),
    "FAILED": ("Failed", "The bank did not accept it."),
    "RETURNED": ("Returned", "The money came back."),
    "MATCHED": ("Matched", "Closebook is confident which invoice this pays."),
    "PARTIAL": ("Part paid", "It covers part of what the invoice asks for."),
    "OVERPAID": ("Overpaid", "It covers the invoice and more, so there is credit "
                             "to deal with."),
    "AMBIGUOUS": ("Unclear", "Closebook could not tell what this pays. It is "
                             "waiting for a person."),
    "UNMATCHED": ("Unclear", "Nobody has said what this pays yet."),
    "PROPOSED ": ("Unclear", ""),
    "REJECTED": ("Set aside", "A person said this is not a match."),
    "REFUND_OWED": ("Refund owed", "The money is still in the bank and is not "
                                   "yours to spend."),
    "RELEASED_INCOME": ("Released as income", "It is yours to spend now."),
    "OPEN": ("Unpaid", "Nothing has arrived against this yet."),
    "PAYABLE": ("Supplier bill", "Money you owe a supplier."),
    "RECEIVABLE": ("Customer invoice", "Money a customer owes you."),
    "LOCAL": ("Local transfer", "Paid inside the supplier's own country."),
    "SWIFT": ("International transfer", "Paid across borders, which carries a fee."),
}


def _plain_chips(html: str) -> str:
    """ONE GATE: no chip anywhere prints a machine token.

    Pills are made in a dozen places, and a raw status in any of them is the
    same bug: "HELD" and "AMBIGUOUS" are true and mean nothing to a reader who
    has not memorised the database. Rather than trust every call site to
    remember, the finished page is swept once. Only the CONTENTS of a pill are
    touched, and only when the text is a status this module knows, so a chip
    holding a count or a currency is left exactly as it was.
    """
    import re as _re

    # the CONTENTS may contain markup, so match lazily to the closing span and
    # read the text without it
    pattern = _re.compile(
        r'(<span class="x-pill[^"]*")([^>]*>)(.{1,120}?)(</span>)', _re.S)

    def swap(m: "_re.Match[str]") -> str:
        open_tag, close, text, end = m.groups()
        plain = _re.sub(r"<[^>]+>", "", text).strip()
        key = plain.upper()
        rest = ""
        if key not in _PLAIN_STATUS:
            # "MATCHED 89%" and friends: the machine word leads, the useful
            # part follows it, and only the word needs translating.
            first, _, tail = plain.partition(" ")
            if first.upper() in _PLAIN_STATUS:
                key, rest = first.upper(), tail
            else:
                return m.group(0)
        label, why = plain_status(key)
        if "title=" in open_tag or "data-tip=" in m.group(0):
            return m.group(0)
        # THE MACHINE WORD STAYS IN THE PAGE, in the explanation: an operator
        # debugging a payment still needs to find "PROCESSING" in the HTML, and
        # the person reading the screen still never sees it.
        tip = f"{why} (status: {key})"
        shown = f"{label} {rest}".strip()
        return (f'{open_tag} class="x-tip" title="{e(tip)}" '
                f'data-tip="{e(tip)}"{close}{e(shown)}{end}')

    return pattern.sub(swap, html)


def plain_status(status) -> tuple[str, str]:
    """(label, explanation) for a status. Never the raw token."""
    value = str(status or "").strip().upper()
    label, why = _PLAIN_STATUS.get(
        value, (value.replace("_", " ").title() if value else "Unclear",
                "Closebook has not recorded an explanation for this state."))
    return label, why


def chip(status, *, testid: str = "") -> str:
    """A status as a chip a person can read, with its explanation on tap."""
    label, why = plain_status(status)
    tone = ("good" if status in ("PAID", "MATCHED", "RELEASED_INCOME")
            else "bad" if status in ("FAILED", "CANCELLED", "REJECTED")
            else "warn" if status in ("HELD", "AMBIGUOUS", "UNMATCHED", "PROPOSED")
            else "info")
    tid = f' data-testid="{testid}"' if testid else ""
    tip = f"{why} (status: {str(status or '').strip().upper()})"
    return (f'<span class="x-pill x-pill--{tone} x-tip"{tid} '
            f'title="{e(tip)}" data-tip="{e(tip)}">{e(label)}</span>')


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


def render_app(ctx: dict, tab: str = "home", health: dict | None = None,
               ws: str = "", guide: bool = False) -> str:
    tab = tab if tab in _TABS else "home"
    if guide:
        ctx = {**ctx, "guide": True}
    n_ok = needs_you_count(ctx)
    screen = _flash(ctx) + _TABS[tab](ctx)
    # WHICH BOOK AM I IN, said quietly. A per-test book that cannot be named on
    # the page is a book a test has to infer from row counts, and the whole
    # failure being fixed here was a test NOT KNOWING it had been moved back to
    # the shared book. Rendered only when a book is actually active, so prod
    # shows nothing.
    book = (f'<span class="x-book" data-testid="active-book">Book: {e(ws)}</span>'
            if ws else "")
    inner = (
        _sidebar(tab, n_ok)
        + '<div class="x-body">'
        + _top(NAV[[n[0] for n in NAV].index(tab)][1], _SUBS[tab], health)
        + f'<main class="x-main"><div class="x-screen">{screen}</div></main>'
        + '<footer class="x-foot">' + book + '<span>Closebook: sandbox only. '
        'No real money moves here.</span></footer>'
        + '</div>'
        + _bottom_tabs(tab, n_ok)
        + '<div id="x-toast" class="x-toast" role="status" aria-live="polite"></div>'
        # The refresh OFFER, not a refresh. Hidden until a poll says the book
        # actually changed and the reader is not mid-task.
        + '<button id="x-new" class="x-new" hidden '
        + 'data-testid="new-activity" type="button">'
        + 'New activity: tap to refresh</button>'
    )
    return carry_workspace(
        _plain_chips(
            _shell(inner, app=True,
                   title=f"Closebook, {NAV[[n[0] for n in NAV].index(tab)][1]}")),
        ws)


__all__ = ["carry_workspace", "render_landing", "render_app", "money", "e"]
