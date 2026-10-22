"""The pool-exhaustion outage: helpers that opened a session and never closed it.

WHAT HAPPENED
    `GET /state` began returning 500 after a 30.4-second wait while `/healthz`
    stayed instant. The traceback was:

        sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10
        reached, connection timed out, timeout 30.00

    Two read helpers, both on the dashboard's render path, did:

        s = session or db.session()        # and never closed it

    So every `/state` and `/app` request checked a connection out of the pool
    and never returned it. Fifteen requests later the pool was empty and every
    later request waited out the 30-second timeout and failed. The 30 seconds
    was never a slow query — it was the pool's own timeout, which is why it was
    suspiciously round.

    An automated tester found it by hammering the app, which is exactly the load
    a single operator never generates.

THE RULE
    A helper that MIGHT create a session must own closing it. `session_for()`
    makes that unconditional rather than remembered at each call site, and the
    engine no longer uses a bounded pool for file-backed SQLite, so a future
    leak cannot take the whole server down either.
"""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.db import get_default_db, make_engine, reset_default_db, session_for  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated():
    reset_default_db()
    yield
    reset_default_db()


@pytest.fixture
def ledger_of_connections(tmp_path):
    """Count connections checked out and never returned."""
    from sqlalchemy import event

    engine = make_engine(f"sqlite:///{tmp_path / 'conns.sqlite3'}")
    live: list[int] = []

    @event.listens_for(engine, "checkout")
    def _out(dbapi_conn, _rec, _proxy):          # noqa: ANN001
        live.append(id(dbapi_conn))

    @event.listens_for(engine, "checkin")
    def _in(dbapi_conn, _rec):                   # noqa: ANN001
        if id(dbapi_conn) in live:
            live.remove(id(dbapi_conn))

    return engine, live


# ---- the ownership rule ---------------------------------------------------
class TestSessionForOwnsWhatItOpens:
    def test_it_closes_a_session_it_opened(self, ledger_of_connections):
        engine, live = ledger_of_connections
        from app.db import DB

        db = DB(f"sqlite:///{engine.url.database}")
        before = len(live)
        with session_for(db) as s:
            s.execute(__import__("sqlalchemy").text("SELECT 1"))
        assert len(live) == before, "the session it opened must be closed"

    def test_it_does_not_close_a_callers_session(self, tmp_path):
        """The caller owns its session; closing it here would be the mirror-image
        bug — a helper silently invalidating someone else's unit of work."""
        import sqlalchemy as sa

        from app.db import DB

        db = DB(f"sqlite:///{tmp_path / 'caller.sqlite3'}")
        s = db.session()
        try:
            with session_for(db, s) as got:
                assert got is s
            s.execute(sa.text("SELECT 1"))       # still usable: NOT closed
        finally:
            s.close()

    def test_it_closes_on_an_exception_too(self, ledger_of_connections):
        engine, live = ledger_of_connections
        from app.db import DB

        db = DB(f"sqlite:///{engine.url.database}")
        before = len(live)
        with pytest.raises(ValueError):
            with session_for(db):
                raise ValueError("boom")
        assert len(live) == before, "a failing read must not leak either"


# ---- the two helpers that actually leaked ---------------------------------
class TestTheHelpersThatLeakedDoNot:
    def test_budget_block_returns_what_it_took(self, ledger_of_connections, monkeypatch):
        engine, live = ledger_of_connections
        from app import dashboard as D

        # The helpers import it INSIDE the function, so it resolves from the
        # app.db module at call time — patching the consumer would not stick.
        monkeypatch.setattr("app.db.get_default_db", lambda: _db_for(engine))
        before = len(live)
        for _ in range(5):
            D.budget_block()
        assert len(live) == before, (
            "budget_block leaked a connection per call — fifteen calls is the "
            "whole pool and then every request waits out a 30s timeout")

    def test_current_plan_returns_what_it_took(self, ledger_of_connections, monkeypatch):
        engine, live = ledger_of_connections
        from app.domain import treasury as T

        db = _db_for(engine)
        before = len(live)
        for _ in range(5):
            T.current_plan(db)
        assert len(live) == before, "current_plan leaked a connection per call"

    def test_a_render_uses_one_connection_not_three(self, ledger_of_connections,
                                                    monkeypatch):
        """`build_context` owns a session; the helpers must use it rather than
        opening their own, or one page view costs several connections and three
        different snapshots of the book."""
        engine, live = ledger_of_connections
        from app import dashboard as D

        db = _db_for(engine)
        monkeypatch.setattr("app.db.get_default_db", lambda: db)
        peak = {"n": 0}
        real = db.session

        def counting():
            s = real()
            peak["n"] = max(peak["n"], len(live))
            return s

        monkeypatch.setattr(db, "session", counting)
        D.build_context([])
        assert len(live) == 0
        assert peak["n"] <= 2, f"a render opened {peak['n']} connections at once"


def _db_for(engine):
    from app.db import DB

    return DB(str(engine.url))


# ---- the engine shape -----------------------------------------------------
class TestTheEngineCannotBeTakenDownByALeak:
    def test_file_sqlite_uses_no_bounded_pool(self, tmp_path):
        from sqlalchemy.pool import NullPool

        engine = make_engine(f"sqlite:///{tmp_path / 'p.sqlite3'}")
        assert isinstance(engine.pool, NullPool), (
            "a bounded pool turns ONE leaked session into a dead server: every "
            "later request waits out the pool timeout and returns a 500")

    def test_in_memory_still_pins_one_connection(self):
        from sqlalchemy.pool import StaticPool

        assert isinstance(make_engine("sqlite:///:memory:").pool, StaticPool), \
            "every new ':memory:' connection is a BRAND NEW empty database"

    def test_every_connection_gets_wal_and_a_busy_timeout(self, tmp_path):
        engine = make_engine(f"sqlite:///{tmp_path / 'wal.sqlite3'}")
        with engine.connect() as c:
            assert c.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"
            assert c.exec_driver_sql("PRAGMA busy_timeout").scalar() >= 1000
        # and on a SECOND connection, because a PRAGMA is per connection
        with engine.connect() as c:
            assert c.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"
            assert c.exec_driver_sql("PRAGMA busy_timeout").scalar() >= 1000

    def test_wal_survives_a_reader_during_a_write(self, tmp_path):
        """WAL's whole promise: a page render is not blocked by a writer."""
        import sqlalchemy as sa

        engine = make_engine(f"sqlite:///{tmp_path / 'c.sqlite3'}")
        with engine.begin() as c:
            c.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
            c.exec_driver_sql("INSERT INTO t (v) VALUES (1)")

        writer = engine.connect()
        trans = writer.begin()
        writer.exec_driver_sql("UPDATE t SET v = 2")
        try:
            with engine.connect() as reader:
                n = reader.exec_driver_sql("SELECT COUNT(*) FROM t").scalar()
            assert n == 1, "a reader must not be blocked by an open writer (WAL)"
        finally:
            trans.rollback()
            writer.close()


class TestNoConnectionIsHeldAcrossANetworkCall:
    def test_state_opens_nothing_that_outlives_it(self, ledger_of_connections,
                                                  monkeypatch):
        """Nothing may be checked out when the response is built — a session
        held open across an await or an API call blocks every writer behind it."""
        from fastapi.testclient import TestClient

        import app.main as M

        engine = ledger_of_connections[0]
        live = ledger_of_connections[1]
        monkeypatch.setattr(M, "_database", lambda: _db_for(engine))
        client = TestClient(M.app)
        for _ in range(10):
            assert client.get("/state").status_code == 200
        assert len(live) == 0, f"{len(live)} connection(s) still checked out"


# ---- /state must never be a raw 500 ---------------------------------------
class TestStateAlwaysAnswersWithSomethingReadable:
    def _client(self, monkeypatch, engine=None):
        from fastapi.testclient import TestClient

        import app.main as M

        if engine is not None:
            monkeypatch.setattr(M, "_database", lambda: _db_for(engine))
        return TestClient(M.app, raise_server_exceptions=False)

    def test_a_healthy_state_says_so(self, tmp_path, monkeypatch):
        client = self._client(monkeypatch, make_engine(f"sqlite:///{tmp_path/'s.sqlite3'}"))
        r = client.get("/state")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["receivables"] == [] and body["payables"] == []

    def test_a_failing_read_is_a_json_reason_not_a_500(self, tmp_path, monkeypatch):
        """The outage rendered as a bare 500, so the operator could not tell
        whether the books were safe. A reason and a plain statement that nothing
        changed is the minimum a failure owes the person reading it."""
        client = self._client(monkeypatch, make_engine(f"sqlite:///{tmp_path/'f.sqlite3'}"))

        class Boom:
            def session(self):
                raise RuntimeError("disk I/O error")

        monkeypatch.setattr("app.main._database", lambda: Boom())
        r = client.get("/state")
        assert r.status_code == 200, "never a raw 500"
        body = r.json()
        assert body["ok"] is False
        assert "RuntimeError" in body["error"]
        assert "Nothing was changed" in body["error"]
        assert isinstance(body["receivables"], list), \
            "the shape must stay stable so the page can still render"

    def test_the_page_shows_the_reason(self, tmp_path, monkeypatch):
        client = self._client(monkeypatch, make_engine(f"sqlite:///{tmp_path/'p.sqlite3'}"))
        monkeypatch.setattr("app.dashboard.build_context",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("the database file is locked")))
        html = client.get("/app").text
        assert "could not read" in html.lower()
        assert "the database file is locked" in html, \
            "the sentence a bot reported was useless precisely because it " \
            "withheld this"
        assert "Nothing was changed" in html

    def test_it_does_no_network_work_inline(self, tmp_path, monkeypatch):
        """It is polled every few seconds by every page, so a third party's
        latency must not be reachable from here."""
        import app.airwallex.client as C
        import app.airwallex.simulation as SIM
        import app.domain.intake as INTAKE

        def forbidden(*a, **k):
            raise AssertionError("/state must not call out to the network")

        # Every door to the outside world that this route could possibly use.
        monkeypatch.setattr(C.AirwallexClient, "__init__", forbidden)
        monkeypatch.setattr(SIM.Simulation, "__init__", forbidden)
        monkeypatch.setattr(INTAKE, "matcher_from_settings", forbidden)
        monkeypatch.setattr(INTAKE, "poll_deposits", forbidden)
        client = self._client(monkeypatch, make_engine(f"sqlite:///{tmp_path/'n.sqlite3'}"))
        assert client.get("/state").status_code == 200


# ---- concurrency ----------------------------------------------------------
class TestConcurrentRequestsAllSucceed:
    def test_thirty_parallel_state_reads(self, tmp_path, monkeypatch):
        """What the automated tester did to it, in miniature: a burst of reads
        that used to exhaust the pool, wait 30s, and 500."""
        from fastapi.testclient import TestClient

        import app.main as M

        engine = make_engine(f"sqlite:///{tmp_path/'burst.sqlite3'}")
        monkeypatch.setattr(M, "_database", lambda: _db_for(engine))
        client = TestClient(M.app)

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=30) as pool:
            codes = [f.result().status_code
                     for f in [pool.submit(client.get, "/state") for _ in range(30)]]
        elapsed = time.monotonic() - started

        assert codes == [200] * 30, f"got {sorted(set(codes))}"
        assert elapsed < 5, f"30 reads took {elapsed:.1f}s"

    def test_a_burst_does_not_exhaust_anything(self, tmp_path, monkeypatch):
        """With no bounded pool there is nothing to exhaust — the failure was
        structural, not a matter of tuning a number."""
        from sqlalchemy.pool import NullPool
        from fastapi.testclient import TestClient

        import app.main as M

        engine = make_engine(f"sqlite:///{tmp_path/'ex.sqlite3'}")
        assert isinstance(engine.pool, NullPool)
        monkeypatch.setattr(M, "_database", lambda: _db_for(engine))
        client = TestClient(M.app)
        for _ in range(60):
            assert client.get("/state").status_code == 200
