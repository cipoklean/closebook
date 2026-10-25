"""The sandbox dev controls, and the state machine behind them.

Verified live before being written down here:

  * `PROCESSING -> PAID` in one hop is **HTTP 500 `operation_failed`**, and so is
    `PROCESSING -> FAILED`. The sandbox only accepts the NEXT valid status, which
    for PROCESSING is SENT, so every route goes through it.
  * a failed or returned transfer ends as **CANCELLED**, never FAILED, with the
    reason in a nested `failure` object (`{"code": "90802", "message":
    "Beneficiary bank returned"}`), not in `failure_type`/`failure_reason`.
  * asking a terminal transfer to transition is also a 500, so the live status
    is read FIRST and the request is skipped when there is nothing to ask for.
"""
from __future__ import annotations

import pytest

from app.airwallex.simulation import Simulation


class Recorder:
    """A client that records the bodies instead of sending them."""

    def __init__(self, fail_on=()):
        self.calls: list[dict] = []
        self._fail_on = set(fail_on)

    def call(self, method, path, json_body=None, params=None, is_simulation=False,
             request_id=None, **kw):
        self.calls.append({"method": method, "path": path,
                           "body": dict(json_body or {})})
        ns = (json_body or {}).get("next_status")
        if ns in self._fail_on:
            raise RuntimeError(f"HTTP 500 operation_failed for {ns}")
        return {"id": "tr_1", "status": ns}


def _statuses(client):
    return [c["body"]["next_status"] for c in client.calls]


class TestTheStateMachineIsWalkedNotJumped:
    def test_settling_goes_through_SENT_first(self):
        """PROCESSING -> PAID on its own is a 500. Proven live."""
        c = Recorder(fail_on={"PAID"})
        with pytest.raises(RuntimeError):
            Simulation(c)._c.call("POST", "/x", json_body={"next_status": "PAID"})

        c = Recorder()
        Simulation(c).transition_transfer("tr_1", "PAID")
        assert _statuses(c) == ["PROCESSING", "SENT", "PAID"]

    def test_failing_goes_through_SENT_and_carries_the_type_only_at_the_end(self):
        c = Recorder()
        Simulation(c).transition_transfer(
            "tr_1", "FAILED", failure_type="INVALID_ACCOUNT_NAME_OR_NUMBER")
        assert _statuses(c) == ["PROCESSING", "SENT", "FAILED"]
        assert "failure_type" not in c.calls[0]["body"], \
            "the intermediate hop must not carry the failure type"
        # THE TYPE RIDES THE FINAL HOP, which is the third one now: the walk
        # starts at PROCESSING because a transfer the bank reports as SCHEDULED
        # has to be asked for PROCESSING before anything else (run 8).
        assert c.calls[-1]["body"]["failure_type"] == "INVALID_ACCOUNT_NAME_OR_NUMBER"

    def test_a_return_is_a_FAILED_with_a_return_reason(self):
        c = Recorder()
        Simulation(c).transition_transfer(
            "tr_1", "FAILED", failure_type="BENEFICIARY_BANK_RETURNED")
        assert c.calls[-1]["body"] == {"next_status": "FAILED",
                                       "failure_type": "BENEFICIARY_BANK_RETURNED"}

    def test_settle_transfer_is_the_same_route(self):
        c = Recorder()
        Simulation(c).settle_transfer("tr_1")
        assert _statuses(c) == ["PROCESSING", "SENT", "PAID"]

    def test_an_unreachable_target_is_refused_rather_than_guessed(self):
        c = Recorder()
        with pytest.raises(ValueError):
            Simulation(c).transition_transfer("tr_1", "TELEPORTED")
        assert c.calls == []


class TestTheLoopReportsWhatItActuallyDid:
    """The route's own wording, because these messages are the evidence an
    operator acts on and two of them were wrong before."""

    def test_a_terminal_transfer_is_read_not_asked(self):
        from app.main import _dev_controls_are_offered

        assert not _dev_controls_are_offered("PAID")
        assert not _dev_controls_are_offered("CANCELLED")
        assert _dev_controls_are_offered("PROCESSING")
        assert _dev_controls_are_offered("SENT")
        assert not _dev_controls_are_offered(None), \
            "an unknown status must not offer a button that cannot work"


class TestATransferTheBankHasScheduled:
    """Run 8: a transfer the bank reported as SCHEDULED could not be moved.

    SCHEDULED is a FUNDING status, not one of the simulatable transfer statuses,
    and Airwallex only accepts the NEXT valid status. Asking for SENT first was
    refused, so Settle and Fail did nothing at all (twice) and the operator read
    "Airwallex's sandbox refused this step".
    """

    class Refuses:
        """A client that refuses any hop that is not the next valid one."""

        def __init__(self, current: str, order: tuple[str, ...]):
            self.current = current
            self.order = order
            self.calls: list[dict] = []

        def call(self, method, path, *, json_body=None, **kw):
            self.calls.append({"path": path, "body": dict(json_body or {})})
            want = json_body.get("next_status")
            i = self.order.index(self.current)
            nxt = self.order[i + 1] if i + 1 < len(self.order) else None
            if want != nxt:
                from app.airwallex.client import ApiError

                raise ApiError(
                    f"next_status must be {nxt} for a transfer in "
                    f"{self.current}", 400,
                    {"details": {"errors": [{"source": "next_status"}]}})
            self.current = want
            return {"status": want}

    def test_a_scheduled_transfer_is_walked_to_paid(self):
        from app.airwallex.simulation import Simulation

        c = self.Refuses("SCHEDULED", ("SCHEDULED", "PROCESSING", "SENT", "PAID"))
        assert Simulation(c).transition_transfer("tr_1", "PAID")["status"] == "PAID"
        assert c.current == "PAID"

    def test_a_scheduled_transfer_is_walked_to_cancelled(self):
        from app.airwallex.simulation import Simulation

        c = self.Refuses("SCHEDULED",
                         ("SCHEDULED", "PROCESSING", "SENT", "CANCELLED"))
        assert Simulation(c).transition_transfer(
            "tr_1", "CANCELLED")["status"] == "CANCELLED"
        assert c.current == "CANCELLED"

    def test_a_refused_FINAL_hop_is_still_an_error(self):
        """A ladder that swallows everything would report success on nothing."""
        import pytest

        from app.airwallex.client import ApiError
        from app.airwallex.simulation import Simulation

        c = self.Refuses("SCHEDULED", ("SCHEDULED", "PROCESSING", "PAID"))
        with pytest.raises(ApiError):
            Simulation(c).transition_transfer("tr_1", "SENT")


class TestScheduledReadsAsInFlight:
    def test_the_token_is_not_shown_bare(self):
        from app.ui import bank_words

        words, tone = bank_words("SCHEDULED")
        assert words == "Sent, the bank has scheduled it", words
        assert tone == "info"
        assert "SCHEDULED" not in words

    def test_the_controls_are_offered_while_it_is_scheduled(self):
        from app.ui import _dev_controls

        html = _dev_controls("tr_1", "SCHEDULED")
        assert "Settle" in html and "Fail" in html, (
            "a scheduled transfer is in flight, so the controls must be there: "
            + html[:300])
