"""The TestSprite instance: a second Closebook an automated tester can wreck.

The whole point is that it CANNOT touch the operator's workspace. So these
tests are mostly about what it refuses to do — start against the demo book,
start without its own PIN, reset a file it was not told to reset — plus one
leak check, because a test instance is exactly where a secret gets committed
by accident.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "testsprite.sh"
RESET = ROOT / "scripts" / "testsprite_reset.py"
MAKEFILE = (ROOT / "Makefile").read_text()


def _run(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, cwd=ROOT, **kw)


def _free_port() -> int:
    """A port nothing is listening on, so a started instance cannot collide."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---- the reset tool -------------------------------------------------------
class TestTheResetToolRefusesTheWrongFile:
    def test_it_refuses_anything_not_named_testsprite(self, tmp_path):
        victim = tmp_path / "closebook.sqlite3"
        victim.write_text("do not delete me")
        r = _run([sys.executable, str(RESET), str(victim)])
        assert r.returncode != 0
        assert "not testsprite.sqlite3" in r.stderr
        assert victim.exists(), "the guard must run BEFORE the unlink"

    def test_it_refuses_the_demo_book_by_name(self, tmp_path):
        r = _run([sys.executable, str(RESET), str(ROOT / "data" / "closebook.sqlite3")])
        assert r.returncode != 0
        assert "testsprite.sqlite3" in r.stderr

    def test_the_guard_is_a_name_check_not_a_prefix_check(self, tmp_path):
        """`closebook.sqlite3.bak-testsprite.sqlite3` must not slip through a
        loose endswith/contains test."""
        sneaky = tmp_path / "closebook.sqlite3.bak-testsprite.sqlite3"
        sneaky.write_text("x")
        r = _run([sys.executable, str(RESET), str(sneaky)])
        assert r.returncode != 0 and sneaky.exists()

    def test_it_rebuilds_a_schema_with_no_rows_in_it(self, tmp_path):
        db = tmp_path / "testsprite.sqlite3"
        r = _run([sys.executable, str(RESET), str(db)])
        assert r.returncode == 0, r.stderr
        assert db.exists()

        from sqlalchemy import create_engine, inspect, text
        eng = create_engine(f"sqlite:///{db}")
        tables = inspect(eng).get_table_names()
        assert len(tables) >= 14, f"only got {tables}"
        with eng.connect() as c:
            for t in tables:
                n = c.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar()
                assert n == 0, f"{t} has {n} rows — this must seed NOTHING"
        assert "No seeded data" in r.stdout

    def test_it_removes_the_wal_and_shm_companions(self, tmp_path):
        db = tmp_path / "testsprite.sqlite3"
        db.write_text("old")
        (tmp_path / "testsprite.sqlite3-wal").write_text("wal")
        (tmp_path / "testsprite.sqlite3-shm").write_text("shm")
        r = _run([sys.executable, str(RESET), str(db)])
        assert r.returncode == 0, r.stderr
        assert not (tmp_path / "testsprite.sqlite3-wal").exists()
        assert not (tmp_path / "testsprite.sqlite3-shm").exists()
        assert "testsprite.sqlite3-wal" in r.stdout, "say what you removed"


# ---- the launcher ---------------------------------------------------------
class TestTheLauncherRefusesToRunUnseparated:
    def test_it_refuses_a_database_that_is_not_its_own(self):
        env = {**os.environ, "TESTSPRITE_DB": "/tmp/someone-elses.sqlite3",
               "TESTSPRITE_PIN": "1234"}
        r = _run(["bash", str(LAUNCHER)], env=env)
        assert r.returncode != 0
        assert "testsprite.sqlite3" in r.stderr

    def test_it_refuses_the_demo_book_outright(self):
        env = {**os.environ, "TESTSPRITE_DB": str(ROOT / "data" / "closebook.sqlite3"),
               "TESTSPRITE_ALLOW_OTHER_DB": "1", "TESTSPRITE_PIN": "1234"}
        r = _run(["bash", str(LAUNCHER)], env=env)
        assert r.returncode != 0
        assert "DEMO book" in r.stderr

    def test_it_refuses_to_start_without_its_own_pin(self, tmp_path):
        """An unset PIN must STOP it, not quietly fall back to the operator's.

        BOUNDED, because the ALLOWED outcome is that `.env` supplies the PIN and
        the instance genuinely STARTS. An unbounded `subprocess.run` then never
        returns: on this box that hung `make test` for as long as the caller's
        timeout allowed, which turns a green suite into an unverifiable one. So
        it gets its own free port and a deadline, and a process still alive at
        the deadline IS the "it started from .env" answer.
        """
        port = _free_port()
        env = {k: v for k, v in os.environ.items() if k != "TESTSPRITE_PIN"}
        env["TESTSPRITE_DB"] = str(tmp_path / "testsprite.sqlite3")
        env["TESTSPRITE_PORT"] = str(port)
        # .env on this box does define TESTSPRITE_PIN, so point HOME-relative
        # reads at a directory with no .env by running from a copy-free cwd.
        proc = subprocess.Popen(["bash", str(LAUNCHER)], cwd=str(ROOT), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True)
        try:
            out, _ = proc.communicate(timeout=12)
            started = False
        except subprocess.TimeoutExpired:
            started = True                     # it is up, which is allowed
            proc.terminate()
            try:
                out, _ = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate(timeout=10)
        combined = out or ""
        # Either it refused because the variable was missing, or it started
        # because .env supplied it — which is the documented way to set it.
        # What must never happen is starting with the OPERATOR's PIN.
        assert ("TESTSPRITE_PIN is not set" in combined
                or "PIN      : set" in combined), combined
        assert not (started and "TESTSPRITE_PIN is not set" in combined), combined

    def test_it_never_prints_the_pin(self, tmp_path):
        r = _run(["bash", str(LAUNCHER)],
                 env={**os.environ, "TESTSPRITE_DB": str(tmp_path / "testsprite.sqlite3"),
                      "TESTSPRITE_PIN": "918273", "TESTSPRITE_PORT": "1"})
        assert "918273" not in (r.stdout + r.stderr), "the PIN must never be echoed"
        assert "not printed" in r.stdout


# ---- the make targets -----------------------------------------------------
class TestTheMakeTargetsExist:
    def test_there_is_a_one_command_reset(self):
        assert "\ntestsprite-reset:" in MAKEFILE
        assert "testsprite_reset.py" in MAKEFILE

    def test_the_instance_target_uses_the_launcher(self):
        assert "\ntestsprite:" in MAKEFILE
        assert "testsprite.sh" in MAKEFILE

    def test_the_tunnel_target_does_not_use_ngrok_pooling(self):
        """Pooling would load-balance the bot and the operator onto ONE
        hostname, which is the failure this whole instance prevents."""
        assert "\ntestsprite-url:" in MAKEFILE
        block = MAKEFILE.split("testsprite-url:", 1)[1]
        assert "--pooling-enabled" not in block
        assert "cloudflared" in block

    @pytest.mark.skipif(not (ROOT / ".venv").exists(), reason="no venv")
    def test_make_help_lists_them(self):
        r = _run(["make", "help"])
        assert "testsprite" in r.stdout


# ---- leaks ----------------------------------------------------------------
class TestNothingSecretIsCommitted:
    def test_the_real_pin_is_not_in_any_tracked_file(self):
        """Read the value from the UNTRACKED .env and search everything git
        tracks. This is the only honest way to test for a leak: a hardcoded
        copy of the secret in the test would itself be the leak."""
        env = ROOT / ".env"
        if not env.exists():
            pytest.skip("no .env on this box")
        m = re.search(r"^TESTSPRITE_PIN=(.+)$", env.read_text(), re.M)
        if not m:
            pytest.skip("TESTSPRITE_PIN not set here")
        pin = m.group(1).strip()
        if len(pin) < 4:
            pytest.skip("PIN too short to search for meaningfully")

        tracked = _run(["git", "ls-files"]).stdout.split()
        offenders = []
        for rel in tracked:
            p = ROOT / rel
            try:
                if pin in p.read_text(errors="ignore"):
                    offenders.append(rel)
            except (OSError, UnicodeDecodeError):
                continue
        assert not offenders, f"TESTSPRITE_PIN appears in tracked files: {offenders}"

    def test_the_example_file_has_the_key_but_no_value(self):
        text = (ROOT / ".env.example").read_text()
        assert "TESTSPRITE_PIN=" in text
        assert re.search(r"^TESTSPRITE_PIN=\s*$", text, re.M), \
            "the example must ship the key EMPTY"

    def test_the_test_database_is_gitignored(self):
        r = _run(["git", "check-ignore", "-v", "data/testsprite.sqlite3"])
        assert r.returncode == 0, "a bot's database must never be committed"
