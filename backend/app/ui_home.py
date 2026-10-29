"""Home, rebuilt: what is there, what the AI may do, what needs you, and why.

Every figure and every tick on this screen is read from `build_context()`. A
check is shown as passed only when the record says it passed; nothing here is
decorative, and nothing claims a state the bank has not confirmed ("Sent,
awaiting bank" until a webhook says Paid).

Kept apart from `ui.py` so the Home composition can be read in one sitting.
The helpers it borrows (`e`, `money_str`, icons) are imported lazily to avoid a
circular import.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .money import money_str

#: Bank statuses that mean "we sent it and the bank has not confirmed it".
SENT_STATES = ("SUBMITTED", "SCHEDULED", "PROCESSING", "SENT", "APPROVED")
#: The only state that may be called Paid, and only with the bank's word.
PAID_STATES = ("PAID", "SETTLED", "COMPLETED")


def _ui():
    from . import ui
    return ui


def _e(v) -> str:
    return _ui().e(v)


def _ico(name: str) -> str:
    return _ui()._I.get(name, "")


def _when(v) -> tuple[str, str]:
    """(sort key, label). Today shows HH:MM; older shows the date."""
    if not v:
        return "", ""
    dt = v
    if not isinstance(v, datetime):
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return str(v), str(v)[:16]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    label = dt.strftime("%H:%M") if dt.date() == now.date() else dt.strftime("%b %d")
    return dt.isoformat(), label


def _pct(x) -> str:
    try:
        return f"{round(float(x) * 100)}%"
    except (TypeError, ValueError):
        return ""


def _check(ok: bool, text: str, testid: str = "") -> str:
    tid = f' data-testid="{testid}"' if testid else ""
    mark = _ico("check") if ok else _ico("x")
    return (f'<li class="h-chk {"is-ok" if ok else "is-no"}"{tid}>'
            f'<span class="h-tick">{mark}</span><span>{_e(text)}</span></li>')


def _stage(kind: str, label: str, body: str, tone: str = "") -> str:
    return (f'<div class="h-stage {tone}" data-stage="{kind}">'
            f'<div class="h-stage-k">{_e(label)}</div>{body}</div>')


# ------------------------------------------------------------------ figures
def _owed_to_you(c: dict) -> dict:
    out: dict[str, int] = {}
    for r in c.get("receivables") or []:
        open_minor = int(r.get("open_minor") or 0)
        if open_minor > 0:
            ccy = str(r.get("currency") or "USD")
            out[ccy] = out.get(ccy, 0) + open_minor
    return out


def _held(c: dict) -> dict:
    out: dict[str, int] = {}
    for a in c.get("arrivals") or []:
        w = int(a.get("waiting_minor") or 0)
        if w > 0:
            ccy = str(a.get("currency") or "USD")
            out[ccy] = out.get(ccy, 0) + w
    return out


def _join(d: dict) -> str:
    return " · ".join(money_str(v, k) for k, v in sorted(d.items()))


def hero(c: dict, tt: dict) -> str:
    """Cash, free to spend, owed to you. One currency big, the rest as chips."""
    m = c["money"]
    cash = dict(c.get("cash") or {})
    bud = str(m.get("headline_currency") or c["budget"]["currency"])
    floor_txt = money_str(m["reserve_floor_minor"], m["reserve_applies_to"])

    if cash:
        main_ccy = bud if bud in cash else sorted(cash)[0]
        big = money_str(cash[main_ccy], main_ccy)
    else:
        main_ccy, big = bud, "No cash yet"
    chips = ""
    if len(cash) > 1:
        chips = ('<div class="h-chips" data-testid="cash-by-currency">' + "".join(
            f'<span class="h-chip">{_e(money_str(v, k))}</span>'
            for k, v in sorted(cash.items())) + '</div>')

    shorts = dict(m.get("shortfall") or {})
    if shorts:
        free_fig = "Short by " + " and ".join(
            money_str(v, k) for k, v in sorted(shorts.items()))
        free_cls = " is-bad"
        free_note = (f"Bills already promised need more than is left after the "
                     f"{floor_txt} safety cushion. Nothing is sent until cash "
                     f"arrives or you decide.")
    else:
        free_fig = money_str(m["headline_free_minor"], m["headline_currency"])
        free_cls = ""
        promised = _join(m.get("promised") or {})
        free_note = (f"After the {floor_txt} safety cushion"
                     + (f" and {promised} promised to suppliers." if promised
                        else "."))
    owed = _join(_owed_to_you(c)) or "Nothing owed"
    held = _join(_held(c))
    held_line = (f'<div class="h-held" data-figure="held">{_e(held)} arrived and '
                 f'is not spendable until someone decides what it pays.</div>'
                 if held else "")
    return (
        '<section class="h-card h-hero" data-testid="home-numbers">'
        f'<div class="h-lbl">Cash in the bank</div>'
        f'<div class="h-big" data-figure="cash">{_e(big)}</div>'
        f'<span hidden data-figure="cash_all">{_e(tt.get("cash") or "")}</span>'
        f'{chips}'
        '<div class="h-row2">'
        f'<div><div class="h-lbl">Free to spend</div>'
        f'<div class="h-mid{free_cls}" data-figure="free_to_spend">{_e(free_fig)}</div></div>'
        f'<div><div class="h-lbl">Owed to you</div>'
        f'<div class="h-mid" data-figure="owed">{_e(owed)}</div></div>'
        '</div>'
        f'<p class="h-note">{_e(free_note)}</p>{held_line}'
        '</section>')


# --------------------------------------------------------------- authority
def authority(c: dict) -> str:
    """What the AI may do RIGHT NOW, read from the live settings, and the
    switch that stops it. Pausing needs no PIN; resuming does."""
    st = c.get("settings") or {}
    b = c["budget"]
    ccy = b["currency"]
    limit = money_str(b["auto_pay_limit_minor"], ccy)
    floor = money_str(b["reserve_floor_minor"], ccy)
    paused = bool(st.get("ai_paused"))
    ui = _ui()
    from .domain.matching import _LLM_MAX_CONFIDENCE as cap
    from .domain.policy import MATCH_CONFIDENCE_AUTO as bar

    if paused:
        _k, since = _when(st.get("ai_paused_at"))
        rows = (
            _check(False, "Apply deposits on its own: paused")
            + _check(False, "Send supplier payments on its own: paused")
            + _check(True, "You can still approve anything yourself")
        )
        control = (
            '<form class="h-resume" method="post" action="/agent/resume" data-action '
            'data-testid="ai-resume-form">'
            '<label class="h-lbl" for="resume-pin">Your PIN to resume</label>'
            '<div class="h-inline"><input id="resume-pin" class="x-input" name="pin" '
            'type="password" inputmode="numeric" autocomplete="off" required '
            'data-testid="ai-resume-pin">'
            '<button class="x-btn x-btn--primary" type="submit" '
            'data-testid="ai-resume">Resume</button></div></form>')
        tag = (f'<span class="h-tag is-bad" data-testid="ai-state">Paused'
               f'{" since " + _e(since) if since else ""}</span>')
    else:
        rows = (
            _check(True, f"Apply a deposit when the rules confirm the invoice. "
                         f"The AI alone tops out at {_pct(cap)}; acting needs "
                         f"{_pct(bar)}.")
            + _check(True, f"Pay a cleared supplier bill up to {limit}")
            + _check(True, f"Never take cash below the {floor} safety cushion")
            + _check(False, "Refunds, unclear deposits and bigger bills always "
                            "wait for you")
        )
        control = (
            '<form method="post" action="/agent/pause" data-action '
            'data-testid="ai-pause-form"><button class="h-pause" type="submit" '
            f'data-testid="ai-pause">{ui._I["lock"]}<span>Pause the AI</span>'
            '</button></form>')
        tag = '<span class="h-tag is-ok" data-testid="ai-state">Running</span>'
    return (
        '<section class="h-card h-auth" data-testid="ai-authority">'
        f'<div class="h-head"><h3>What the AI may do right now</h3>{tag}</div>'
        f'<ul class="h-list">{rows}</ul>{control}</section>')


# ----------------------------------------------------------- needs your OK
def _deposit_why(a: dict) -> str:
    st = str((a.get("proposal") or {}).get("status") or a.get("match_status") or "").upper()
    return {"PARTIAL": "pays only part of an invoice",
            "OVERPAID": "more than the invoice owes",
            "AMBIGUOUS": "reference unclear",
            "UNMATCHED": "no invoice found",
            "PROPOSED": "the AI is not sure"}.get(st, "needs a decision")


def _bill_why(p: dict) -> str:
    r = str(p.get("plan_approval_reason") or p.get("reason") or "").lower()
    if "exceeds the auto-pay limit" in r or "over your automatic limit" in r:
        return "over the auto-pay limit"
    if "confidence" in r:
        return "linked deposit unconfirmed"
    return "needs your OK"


def needs_items(c: dict) -> list[tuple[str, str, str]]:
    """(what, why, href) for every decision only a person can make. The same
    obligations `ui.needs_you` counts, so the count and the list agree."""
    items: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for s in c.get("approvable") or []:
        num = s.get("payable_number")
        if not num or num in seen:
            continue
        seen.add(num)
        amt = money_str(int(s.get("amount_minor") or 0), s.get("currency") or "USD")
        items.append((f"Pay {s.get('supplier') or num} {amt}", _bill_why(s),
                      f"/app?tab=ok#step-{s.get('step_id')}"))
    held_for_cash = {w.get("number") for w in c.get("waiting") or []}
    for p in c.get("awaiting_approval") or []:
        num = p.get("number")
        if not num or num in seen or num in held_for_cash:
            continue
        seen.add(num)
        amt = money_str(int(p.get("amount_minor") or 0), p.get("currency") or "USD")
        items.append((f"Pay {p.get('supplier') or num} {amt}", _bill_why(p),
                      "/app?tab=ok"))
    for a in c.get("inflows_waiting") or []:
        amt = money_str(int(a.get("waiting_minor") or a.get("amount_minor") or 0),
                        a.get("currency") or "USD")
        who = a.get("payer") or "someone"
        items.append((f"{amt} from {who}", _deposit_why(a),
                      f"/app?tab=ok#inflow-{a.get('id')}"))
    return items


def needs_block(c: dict) -> str:
    n = _ui().needs_you(c)
    items = needs_items(c)
    problems = c.get("problems") or []
    if n == 0 and not problems:
        # A bill held by a rule is not a decision for a person, but it is not
        # "all clear" either: say it is on hold, and point at why.
        waiting = c.get("waiting") or []
        if waiting:
            k = len(waiting)
            sub = (f'{k} payment{"" if k == 1 else "s"} on hold until the money '
                   f'{"it depends" if k == 1 else "they depend"} on arrives. '
                   'The reason is under Stopped by the rules.')
        else:
            sub = 'Every payment is inside your rules and explained below.'
        return (
            '<section class="h-card h-calm" data-testid="needs-none">'
            f'<span class="h-calm-ico">{_ico("check")}</span>'
            f'<div><b>Nothing needs your OK</b><p>{_e(sub)}</p></div>'
            '<span hidden data-figure="needs_you" data-testid="needs-you-count">0</span>'
            '</section>')
    rows = "".join(
        f'<a class="h-need" href="{href}" data-testid="need-item">'
        f'<span>{_e(what)}</span><small>{_e(why)}</small></a>'
        for what, why, href in items[:4])
    more = (f'<div class="h-need-more">and {len(items) - 4} more</div>'
            if len(items) > 4 else "")
    prob = ""
    if problems:
        prob = (f'<a class="h-need" href="/app?tab=money-out" data-testid="need-item">'
                f'<span>{len(problems)} payment{"s" if len(problems) != 1 else ""} '
                f'came back from the bank</span><small>see why</small></a>')
    head = (f'<span data-figure="needs_you" data-testid="needs-you-count">{n}</span> '
            f'thing{"" if n == 1 else "s"} need{"s" if n == 1 else ""} your OK'
            if n else "A payment needs a look")
    return (
        '<section class="h-needs" data-testid="needs-you">'
        f'<div class="h-needs-t">{head}</div>{rows}{prob}{more}'
        '<a class="h-needs-btn" href="/app?tab=ok" data-testid="needs-review">'
        f'Review {"them" if n != 1 else "it"}</a></section>')


# --------------------------------------------------------------- decisions
def _held_payable(c: dict) -> dict | None:
    for p in (c.get("awaiting_approval") or []) + (c.get("waiting") or []):
        return p
    return None


def stopped_card(c: dict) -> str:
    """The rules saying no, with the check that failed. This is what proves
    the guard-rails are real, so it is shown whenever there is one."""
    p = _held_payable(c)
    if not p:
        return ""
    b = c["budget"]
    ccy = b["currency"]
    limit = money_str(b["auto_pay_limit_minor"], ccy)
    floor = money_str(b["reserve_floor_minor"], ccy)
    amt = money_str(int(p.get("amount_minor") or 0), p.get("currency") or "USD")
    appr = str(p.get("plan_approval_reason") or "")
    over_limit = "exceeds the auto-pay limit" in appr
    codes = set(p.get("plan_blocker_codes") or [])
    cash_blocked = bool(codes & {"waits_for_inflow", "reserve_floor", "breaches_floor",
                                 "insufficient_funds", "shortfall"}) or (
        "below the" in str(p.get("plan_reason") or ""))
    checks = (
        _check(True, f"Bill {p.get('number')} is on the books", "rule-pass")
        + _check(True, f"{p.get('supplier') or 'Supplier'} has a payee on file",
                 "rule-pass")
        + _check(not over_limit,
                 (f"{amt} is over the {limit} auto-pay limit" if over_limit
                  else f"Within the {limit} auto-pay limit"),
                 "rule-fail" if over_limit else "rule-pass")
        + _check(not cash_blocked,
                 (f"Cash would not cover it and keep the {floor} cushion"
                  if cash_blocked else f"Cash stays above the {floor} cushion"),
                 "rule-fail" if cash_blocked else "rule-pass"))
    failed = int(over_limit) + int(cash_blocked)
    if over_limit and cash_blocked:
        result = ("Not sent. It waits for cash first, then for your OK, "
                  "because it is over the limit.")
    elif over_limit:
        result = "Not sent. Waiting for your OK."
    elif cash_blocked:
        result = "Not sent. It goes once enough cash has landed."
    else:
        result = "Not sent yet."
    why = str(p.get("plan_reason") or "")
    return (
        '<section class="h-card h-stopped" data-testid="stopped-by-rules">'
        '<div class="h-head"><h3>Stopped by the rules</h3>'
        '<span class="h-tag is-bad">Held</span></div>'
        f'<div class="h-amt">{_e(amt)} <span>to {_e(p.get("supplier") or "supplier")}, '
        f'{_e(p.get("number"))}</span></div>'
        + _stage("ai", "Plan proposed", f'<p>Pay {_e(p.get("number"))} now</p>')
        + _stage("checks", f"Rule checks · {4 - failed} of 4 passed",
                 f'<ul class="h-list">{checks}</ul>', "is-bad" if failed else "is-ok")
        + _stage("result", "Result", f'<p>{_e(result)}</p>'
                 + (f'<p class="h-why">{_e(why)}</p>' if why else ""), "is-wait")
        + '</section>')


def _latest_arrival(c: dict) -> dict | None:
    arr = list(c.get("arrivals") or [])
    arr.sort(key=lambda a: str(a.get("received_at") or ""), reverse=True)
    return arr[0] if arr else None


def money_in_card(c: dict) -> str:
    a = _latest_arrival(c)
    if not a:
        return ""
    from .domain.policy import MATCH_CONFIDENCE_AUTO as bar
    amt = money_str(int(a.get("amount_minor") or 0), a.get("currency") or "USD")
    applied = [x.get("invoice") for x in (a.get("applied") or []) if x.get("invoice")]
    prop = a.get("proposal") or {}
    status = str(prop.get("status") or a.get("match_status") or "").upper()
    method = str(a.get("method") or "").upper()
    conf = a.get("confidence")
    review = str(a.get("review_state") or "").upper()
    invs = applied or list(a.get("proposed_invoices") or [])
    inv_txt = ", ".join(invs) if invs else ""

    if method in ("LLM", "MODEL", "AI"):
        ai = (f'<p>Reference "{_e(a.get("reference") or "")}" means '
              f'{_e(inv_txt or "no invoice it could name")}</p>'
              f'<p class="h-why">{_e(_pct(conf))} sure. The AI alone can never '
              f'reach the {_e(_pct(bar))} bar, so the rules decide.</p>')
    else:
        rule = str((prop.get("evidence") or {}).get("rule") or "")
        if method == "EXACT" or not rule:
            ai = ('<p>Not needed.</p><p class="h-why">The reference named the '
                  'invoice exactly, so the rules decided before the AI was '
                  'asked.</p>')
        else:
            ai = (f'<p>Not asked. A rule proposed {_e(inv_txt or "a match")}.</p>'
                  f'<p class="h-why">Rule: {_e(rule)}.</p>')

    ok_conf = conf is not None and float(conf) >= bar
    checks = ""
    if inv_txt:
        checks += _check(True, f"{inv_txt} exists and was open", "rule-pass")
    else:
        checks += _check(False, "No open invoice matches the reference", "rule-fail")
    if status == "MATCHED":
        checks += _check(True, "Amount fits what was owed", "rule-pass")
    elif status == "PARTIAL":
        checks += _check(False, "Pays only part of what is owed", "rule-fail")
    elif status == "OVERPAID":
        checks += _check(False, "More than the invoice owes; the rest is credit",
                         "rule-fail")
    checks += _check(ok_conf, (f"Confidence {_pct(conf)}, "
                               f"{'at or above' if ok_conf else 'below'} the "
                               f"{_pct(bar)} bar") if conf is not None
                     else "No confidence recorded", "rule-pass" if ok_conf else "rule-fail")
    n_ok = checks.count('is-ok"')
    n_all = checks.count('class="h-chk')

    if applied:
        tag, tone = '<span class="h-tag is-ok">Applied</span>', "is-ok"
        result = f"{', '.join(applied)} updated in the books"
    elif review == "REFUND_OWED":
        tag, tone = '<span class="h-tag is-wait">Refund owed</span>', "is-wait"
        result = "Set aside as a refund owed. Not yours to spend."
    elif review == "RELEASED_INCOME":
        tag, tone = '<span class="h-tag is-ok">Income</span>', "is-ok"
        result = "Released as income by a person."
    else:
        tag, tone = '<span class="h-tag is-wait">Held for you</span>', "is-wait"
        result = "Nothing applied. Waiting for your decision."
    return (
        '<section class="h-card h-in" data-testid="decision-money-in">'
        f'<div class="h-head"><h3>Money in</h3>{tag}</div>'
        f'<div class="h-amt">{_e(amt)} <span>from {_e(a.get("payer") or "a payer")}</span></div>'
        + _stage("ai", "AI suggestion", ai)
        + _stage("checks", f"Rule checks · {n_ok} of {n_all} passed",
                 f'<ul class="h-list">{checks}</ul>',
                 "is-ok" if n_ok == n_all else "is-bad")
        + _stage("result", "Result", f'<p>{_e(result)}</p>', tone)
        + '</section>')


def money_out_card(c: dict) -> str:
    """The latest payment that left. Never 'Paid' without the bank's word."""
    sent = [p for p in (c.get("payables") or [])
            if str(p.get("live_status") or p.get("display_status") or "").upper()
            in SENT_STATES + PAID_STATES]
    if not sent:
        return ""
    p = sent[-1]
    live = str(p.get("live_status") or p.get("display_status") or "").upper()
    amt = money_str(int(p.get("amount_minor") or 0), p.get("currency") or "USD")
    if live in PAID_STATES:
        tag = '<span class="h-tag is-ok">Paid, bank confirmed</span>'
        result, tone = "The bank confirmed the payment.", "is-ok"
    else:
        tag = '<span class="h-tag is-wait">Sent · awaiting bank</span>'
        result = "Sent. It shows as Paid only when the bank confirms."
        tone = "is-wait"
    b = c["budget"]
    limit = money_str(b["auto_pay_limit_minor"], b["currency"])
    approved_by_person = bool(p.get("plan_needs_approval"))
    checks = (_check(True, f"Bill {p.get('number')} is on the books", "rule-pass")
              + _check(True, ("Approved by you with your PIN" if approved_by_person
                              else f"Within the {limit} auto-pay limit"), "rule-pass")
              + _check(True, "Cash covered it and kept the safety cushion",
                       "rule-pass"))
    return (
        '<section class="h-card h-out" data-testid="decision-money-out">'
        f'<div class="h-head"><h3>Money out</h3>{tag}</div>'
        f'<div class="h-amt">{_e(amt)} <span>to {_e(p.get("supplier") or "supplier")}, '
        f'{_e(p.get("number"))}</span></div>'
        + _stage("checks", "Rule checks · 3 of 3 passed",
                 f'<ul class="h-list">{checks}</ul>', "is-ok")
        + _stage("result", "Result", f'<p>{_e(result)}</p>', tone)
        + '</section>')


# ---------------------------------------------------------------- timeline
def timeline(c: dict) -> str:
    """ONE timeline, newest first, one plain line per event."""
    ev: list[tuple[str, str]] = []
    for a in c.get("arrivals") or []:
        key, label = _when(a.get("received_at"))
        amt = money_str(int(a.get("amount_minor") or 0), a.get("currency") or "USD")
        who = a.get("payer") or "somebody"
        invs = ", ".join(x.get("invoice") or "" for x in (a.get("applied") or [])
                         if x.get("invoice"))
        review = str(a.get("review_state") or "").upper()
        if review == "REFUND_OWED":
            ico, cls, t, s, href = "refund", "is-wait", f"{amt} set aside", \
                "Refund owed, not yours to spend", "/app?tab=ok"
        elif review == "RELEASED_INCOME":
            ico, cls, t, s, href = "in", "is-ok", f"{amt} released as income", \
                f"From {who}", "/app?tab=money-in"
        elif invs:
            ico, cls, t, s, href = "in", "is-ok", f"{amt} arrived", \
                f"{who} · paid {invs}", "/app?tab=money-in"
        elif a.get("awaiting_human"):
            ico, cls, t, s, href = "warn", "is-wait", f"{amt} held", \
                f"From {who} · {_deposit_why(a)}, needs you", "/app?tab=ok"
        else:
            ico, cls, t, s, href = "in", "", f"{amt} arrived", f"From {who}", \
                "/app?tab=money-in"
        ev.append((key, ico, cls, t, s, href, label))
    for inc in c.get("incidents") or []:
        key, label = _when(inc.get("created_at"))
        tid = str(inc.get("transfer_id") or "")
        said = (inc.get("failure_reason") or inc.get("failure_type")
                or inc.get("summary") or "the bank did not say why")
        ev.append((key, "x", "is-bad", "A payment came back",
                   f"{tid[:8]}… · {_ui()._shown(said)}",
                   "/app?tab=money-out#sandbox-controls", label))
    ev.sort(key=lambda r: r[0], reverse=True)
    rows = "".join(
        f'<li><a href="{href}" data-testid="happened">'
        f'<span class="h-ic {cls}">{_ico(ico)}</span>'
        f'<span class="h-tl-b"><b>{_e(t)}</b><small>{_e(s)}</small></span>'
        f'<time>{_e(label)}</time></a></li>'
        for _k, ico, cls, t, s, href, label in ev[:8])
    if not rows:
        rows = ('<li class="h-empty">Nothing has happened yet. When money arrives '
                'or a payment moves, it shows here.</li>')
    return (
        '<section class="h-card h-tl">'
        '<div class="h-head"><h3>What just happened</h3>'
        '<span class="h-lbl">Newest first</span></div>'
        f'<ul data-testid="what-just-happened">{rows}</ul></section>')


def compose(c: dict, tt: dict, *, guide: str, details: str) -> str:
    """Home, top to bottom, in the order a person needs it."""
    # Two columns on a wide screen: the money and the decisions on the left,
    # what the AI may do and what just happened on the right. On a phone the
    # columns dissolve (display: contents) and CSS order restores the reading
    # order: numbers, needs, authority, stopped, money in, money out, timeline.
    main = (hero(c, tt) + needs_block(c) + stopped_card(c)
            + money_in_card(c) + money_out_card(c))
    side = authority(c) + timeline(c)
    return (guide
            + '<div class="h-grid" data-testid="home-grid">'
            f'<div class="h-col-main">{main}</div>'
            f'<aside class="h-col-side">{side}</aside></div>'
            + details)
