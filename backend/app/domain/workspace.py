"""The workspace the operator builds by hand — create it, read it, wipe it.

Everything here is the DATA the app runs on. There is no scenario, no seed, no
demo fixture: a customer exists because somebody typed one, and a bill exists
because somebody owes it. The vendored example data lives only in `tests/`.

Two rules this module exists to keep:

1. **Nothing is invented.** Every list returned here is a query. If the
   workspace is empty, the lists are empty and the UI says so.
2. **Refusals are plain.** A duplicate invoice number, a bad due date or a
   missing supplier comes back as `WorkspaceRefused` with a sentence an
   operator can act on, never as a stack trace and never as a silent no-op.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models import (
    AgentRun, AgentTrace, Approval, BalanceSnapshot, Customer, Inflow,
    InvoicePayable, InvoiceReceivable, LedgerEntry, Match, PayStatus, PayoutMethod,
    PlanStep, RecStatus, Supplier, WebhookEvent,
)
from ..money import money_str, to_minor, to_major_str


class WorkspaceRefused(ValueError):
    """Something the workspace will not accept, with the reason in plain words."""


# ---------------------------------------------------------------- customers
def create_customer(session, *, name: str, email: str = "", country: str = "") -> Customer:
    name = (name or "").strip()
    if not name:
        raise WorkspaceRefused("The customer needs a name.")
    row = Customer(name=name, email=(email or "").strip() or None,
                   country=(country or "").strip().upper() or None)
    session.add(row)
    session.flush()
    return row


def create_invoice(session, *, customer_id: int, number: str, currency: str,
                   amount: str, due_date: str = "") -> InvoiceReceivable:
    number = (number or "").strip()
    if not number:
        raise WorkspaceRefused("The invoice needs a number — it is how a payment "
                               "finds this invoice.")
    ccy = (currency or "").strip().upper()
    if len(ccy) != 3 or not ccy.isalpha():
        raise WorkspaceRefused("Currency must be a 3-letter code, e.g. USD.")
    if session.query(InvoiceReceivable).filter_by(number=number).first():
        raise WorkspaceRefused(f"Invoice {number} already exists. Numbers have to "
                               f"be unique — a payment matches on the number.")
    if session.get(Customer, customer_id) is None:
        raise WorkspaceRefused("Pick a customer for this invoice.")
    try:
        amount_minor = to_minor(amount, ccy)
    except Exception:
        raise WorkspaceRefused(f"Amount has to be a number like 12000. "
                               f"Got {amount!r}.") from None
    if amount_minor <= 0:
        raise WorkspaceRefused("Amount has to be more than zero.")
    row = InvoiceReceivable(
        number=number, customer_id=customer_id, currency=ccy,
        amount_minor=amount_minor, amount_paid_minor=0,
        status=RecStatus.OPEN, due_date=_parse_date(due_date),
    )
    session.add(row)
    session.flush()
    return row


# ----------------------------------------------------------------- suppliers
def create_supplier(session, *, name: str, country: str, currency: str,
                    payout_method: str = "LOCAL") -> Supplier:
    """The supplier ROW only.

    Deliberately separate from beneficiary creation: the row has to exist first
    so that a failed Airwallex call has somewhere to be recorded. A supplier
    whose beneficiary failed is a visible, retryable row — not a missing one.
    """
    name = (name or "").strip()
    if not name:
        raise WorkspaceRefused("The supplier needs a name.")
    ccy = (currency or "").strip().upper()
    if len(ccy) != 3 or not ccy.isalpha():
        raise WorkspaceRefused("Currency must be a 3-letter code, e.g. USD.")
    try:
        method = PayoutMethod((payout_method or "LOCAL").strip().upper())
    except ValueError:
        raise WorkspaceRefused("Payout method has to be LOCAL or SWIFT.") from None
    row = Supplier(name=name, country=(country or "").strip().upper() or None,
                   currency=ccy, payout_method=method)
    session.add(row)
    session.flush()
    return row


def record_beneficiary(session, supplier_id: int, *, request: dict,
                       beneficiary_id: Optional[str], status: str,
                       error: Optional[str] = None) -> Supplier:
    """Store the outcome of a real Airwallex beneficiary call."""
    sup = session.get(Supplier, supplier_id)
    if sup is None:
        raise WorkspaceRefused("That supplier no longer exists.")
    sup.beneficiary_request_json = json.dumps(request, sort_keys=True)
    sup.airwallex_beneficiary_id = beneficiary_id
    sup.beneficiary_status = status
    sup.beneficiary_error = error
    session.flush()
    return sup


# ------------------------------------------------------------------ payables
def create_payable(session, *, supplier_id: int, currency: str, amount: str,
                   number: str = "", depends_on: str = "",
                   criticality: int = 3, due_date: str = "") -> InvoicePayable:
    number = (number or "").strip()
    if not number:
        raise WorkspaceRefused("The bill needs a reference, e.g. PO-7001. It is "
                               "half of the payment's identity.")
    ccy = (currency or "").strip().upper()
    if len(ccy) != 3 or not ccy.isalpha():
        raise WorkspaceRefused("Currency must be a 3-letter code, e.g. USD.")
    sup = session.get(Supplier, supplier_id)
    if sup is None:
        raise WorkspaceRefused("Pick a supplier for this bill.")
    if not sup.airwallex_beneficiary_id:
        raise WorkspaceRefused(
            f"{sup.name} has no Airwallex beneficiary yet, so nothing could be "
            f"paid. Create a beneficiary for them first, on the Suppliers card."
        )
    if session.query(InvoicePayable).filter_by(number=number).first():
        raise WorkspaceRefused(f"Bill {number} already exists.")
    try:
        amount_minor = to_minor(amount, ccy)
    except Exception:
        raise WorkspaceRefused(f"Amount has to be a number like 1800. "
                               f"Got {amount!r}.") from None
    if amount_minor <= 0:
        raise WorkspaceRefused("Amount has to be more than zero.")

    dependency_id = None
    dep_label = ""
    if (depends_on or "").strip():
        dep_label = depends_on.strip()
        rec = (session.query(InvoiceReceivable)
               .filter_by(number=dep_label).first())
        if rec is None:
            raise WorkspaceRefused(
                f"There is no invoice called {dep_label}, so this bill cannot "
                f"depend on it. Create the invoice first, or leave the field "
                f"blank.")
        dependency_id = rec.id

    try:
        crit = int(criticality)
    except Exception:
        crit = 3
    crit = min(5, max(1, crit))

    row = InvoicePayable(
        number=number, supplier_id=supplier_id, currency=ccy,
        amount_minor=amount_minor, status=PayStatus.HELD,
        depends_on_receivable_id=dependency_id, due_date=_parse_date(due_date),
        criticality=crit,
    )
    session.add(row)
    session.flush()
    return row


def _parse_date(value: str) -> Optional[datetime]:
    """'' -> None. A bad date is refused rather than silently dropped, because a
    bill with no due date sorts differently in the planner."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise WorkspaceRefused(f"Due date has to look like 2026-11-30. Got {value!r}.")


# ------------------------------------------------------------------- reading
def snapshot(session) -> dict:
    """Everything the Setup screens show, read fresh from the database.

    No defaults, no sample rows: an empty workspace renders as an empty
    workspace, which is the only way an operator can trust what they see.
    """
    customers = session.query(Customer).order_by(Customer.name).all()
    invoices = (session.query(InvoiceReceivable)
                .order_by(InvoiceReceivable.number).all())
    suppliers = session.query(Supplier).order_by(Supplier.name).all()
    payables = session.query(InvoicePayable).order_by(InvoicePayable.number).all()

    inv_by_id = {r.id: r for r in invoices}

    return {
        "customers": [
            {"id": c.id, "name": c.name, "email": c.email or "",
             "country": c.country or "",
             "open_count": sum(1 for i in invoices if i.customer_id == c.id)}
            for c in customers
        ],
        "invoices": [
            {
                "id": i.id, "number": i.number,
                "customer": i.customer.name if i.customer else "",
                "customer_id": i.customer_id,
                "currency": i.currency,
                "amount_minor": i.amount_minor,
                "amount": money_str(i.amount_minor, i.currency),
                "amount_paid_minor": i.amount_paid_minor,
                "paid": money_str(i.amount_paid_minor, i.currency),
                "outstanding_minor": max(0, i.amount_minor - i.amount_paid_minor),
                "status": i.status.value if i.status else "",
                "due_date": i.due_date.strftime("%Y-%m-%d") if i.due_date else "",
            }
            for i in invoices
        ],
        "suppliers": [
            {
                "id": s.id, "name": s.name, "country": s.country or "",
                "currency": s.currency or "",
                "payout_method": s.payout_method.value if s.payout_method else "LOCAL",
                "beneficiary_id": s.airwallex_beneficiary_id or "",
                "beneficiary_status": s.beneficiary_status or "",
                "beneficiary_error": s.beneficiary_error or "",
                "beneficiary_request": _loads(s.beneficiary_request_json),
            }
            for s in suppliers
        ],
        "payables": [
            {
                "id": p.id, "number": p.number,
                "supplier": p.supplier.name if p.supplier else "",
                "supplier_id": p.supplier_id,
                "currency": p.currency,
                "amount_minor": p.amount_minor,
                "amount": money_str(p.amount_minor, p.currency),
                "status": p.status.value if p.status else "",
                "criticality": p.criticality,
                "payout_method": (p.supplier.payout_method.value
                                  if p.supplier and p.supplier.payout_method else ""),
                "depends_on": (inv_by_id[p.depends_on_receivable_id].number
                               if p.depends_on_receivable_id in inv_by_id else ""),
                "due_date": p.due_date.strftime("%Y-%m-%d") if p.due_date else "",
            }
            for p in payables
        ],
        "counts": {
            "customers": len(customers), "invoices": len(invoices),
            "suppliers": len(suppliers), "payables": len(payables),
        },
    }


def _loads(raw: Optional[str]):
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


# -------------------------------------------------------------- start fresh
#: Order matters only for readability; every table is emptied.
_WORKSPACE_TABLES = (
    PlanStep, Approval, Match, Inflow, LedgerEntry, InvoicePayable,
    InvoiceReceivable, Supplier, Customer, AgentTrace, AgentRun,
    WebhookEvent, BalanceSnapshot,
)


@dataclass
class Archive:
    path: str
    cleared: dict


def archive_and_reset(db, *, confirm: str, backup_dir: Path | None = None) -> Archive:
    """Archive the whole book to a file, then empty the workspace.

    The settings row is deliberately KEPT: the floor and the limit are the
    workspace's policy, not its data, and an operator who resets to test again
    should not have to re-enter the rules every time. The UI says so.

    What this canNOT undo: transfers already created at Airwallex. Resetting
    the book does not cancel them, and the confirmation says that too.
    """
    if (confirm or "").strip().upper() != "START FRESH":
        raise WorkspaceRefused(
            'Type START FRESH exactly to confirm. This clears every customer, '
            'invoice, supplier, bill, match and ledger entry.')

    engine = db.engine
    url = str(engine.url)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_dir = Path(backup_dir) if backup_dir else _default_archive_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)

    if url.startswith("sqlite"):
        src = engine.url.database
        if not src or src == ":memory:":
            raise WorkspaceRefused(
                "This workspace is in memory, so there is nothing to archive. "
                "Set CLOSEBOOK_DATABASE_URL to a file to use Start fresh.")
        dest = dest_dir / f"workspace-{stamp}.sqlite3"
        # Checkpoint first: with WAL, copying the .sqlite3 alone can miss
        # committed pages that still sit in the -wal file, so the "backup"
        # would be missing the most recent rows.
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA wal_checkpoint(FULL)")
        shutil.copy2(src, dest)
    else:
        # Not SQLite: dump the rows instead of pretending we can copy a file.
        dest = dest_dir / f"workspace-{stamp}.json"
        dest.write_text(json.dumps(dump_rows(db.session()), default=str,
                                   sort_keys=True, indent=2))

    cleared: dict[str, int] = {}
    with db.session_scope() as s:
        for model in _WORKSPACE_TABLES:
            n = s.query(model).delete(synchronize_session=False)
            cleared[model.__tablename__] = int(n or 0)
    return Archive(path=str(dest), cleared=cleared)


def dump_rows(session) -> dict:
    """Every workspace row as plain data, for a non-SQLite archive."""
    out: dict[str, list] = {}
    for model in _WORKSPACE_TABLES:
        out[model.__tablename__] = [
            {c.name: getattr(r, c.name) for c in model.__table__.columns}
            for r in session.query(model).all()
        ]
    return out


def _default_archive_dir() -> Path:
    from ..config import REPO_ROOT
    return Path(REPO_ROOT) / "data" / "archives"


__all__ = [
    "Archive", "WorkspaceRefused", "archive_and_reset", "create_customer",
    "create_invoice", "create_payable", "create_supplier", "dump_rows",
    "record_beneficiary", "snapshot",
]
