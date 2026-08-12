"""A Stop must not be lost while a turn is still in its pre-flight.

`agent_runs.start()` is the last line of the chat_stream handler: upload, file
conversion, indexing and job provisioning all run inline before it. For a heavy
attachment that window is seconds to minutes, and a Stop clicked inside it found nothing in
_RUNS, returned {"stopped": false} as HTTP 200, and the handler launched the run
regardless.

Observed live (2026-08-12): Stop at 21:01:04, session DELETEd at 21:01:12, run
started anyway at 21:01:15 and billed 20 rounds of claude-sonnet-5 against a
session that no longer existed — with no way to cancel it, because the Stop
endpoint authorises against the session that had just been deleted.
"""

import asyncio
import time

import pytest

from src import agent_runs


@pytest.fixture(autouse=True)
def _clean_state():
    agent_runs._RUNS.clear()
    agent_runs._STOP_REQUESTS.clear()
    yield
    agent_runs._RUNS.clear()
    agent_runs._STOP_REQUESTS.clear()


async def _never_ends():
    """Stands in for a real agent turn: yields forever until cancelled."""
    while True:
        yield "data: {}\n\n"
        await asyncio.sleep(0.01)


class TestStopBeforeRunExists:
    def test_stop_with_no_run_is_recorded_not_dropped(self):
        assert agent_runs.stop("sess-1") is False          # nothing live to cancel
        assert "sess-1" in agent_runs._STOP_REQUESTS       # ...but remembered

    def test_run_stopped_during_setup_never_starts(self):
        async def _go():
            started_at = time.monotonic()
            await asyncio.sleep(0.01)
            agent_runs.stop("sess-1")                      # Stop lands mid-pre-flight
            await asyncio.sleep(0.01)
            # Handler finishes its setup and tries to launch the run.
            run = agent_runs.start("sess-1", _never_ends(), requested_at=started_at)
            await asyncio.sleep(0.05)
            return run

        run = asyncio.run(_go())
        assert run.status == "stopped"
        assert "data: [DONE]\n\n" in run.buffer

    def test_the_exact_live_timeline_does_not_start_a_run(self):
        # request begins -> Stop (+1s) -> DELETE (+9s) -> setup ends (+12s)
        async def _go():
            t0 = time.monotonic()
            agent_runs.stop("78bc71d5")                    # +1s in real time
            return agent_runs.start("78bc71d5", _never_ends(), requested_at=t0)

        run = asyncio.run(_go())
        assert run.status == "stopped", "the run that billed 20 rounds must not start"

    def test_tombstone_is_consumed_so_the_next_turn_runs(self):
        async def _go():
            t0 = time.monotonic()
            agent_runs.stop("sess-1")
            first = agent_runs.start("sess-1", _never_ends(), requested_at=t0)
            # A NEW turn the user starts afterwards must not inherit that stop.
            t1 = time.monotonic()
            second = agent_runs.start("sess-1", _never_ends(), requested_at=t1)
            await asyncio.sleep(0.02)
            status = second.status
            agent_runs.stop("sess-1")
            await asyncio.sleep(0.02)
            return first.status, status

        first_status, second_status = asyncio.run(_go())
        assert first_status == "stopped"
        assert second_status == "running", "a stale tombstone must not kill a later run"

    def test_a_stop_older_than_the_request_does_not_apply(self):
        async def _go():
            agent_runs.stop("sess-1")        # stop belongs to a PREVIOUS request
            await asyncio.sleep(0.02)
            t_new = time.monotonic()         # this request began after it
            run = agent_runs.start("sess-1", _never_ends(), requested_at=t_new)
            await asyncio.sleep(0.02)
            status = run.status
            agent_runs.stop("sess-1")
            await asyncio.sleep(0.02)
            return status

        assert asyncio.run(_go()) == "running"


class TestStopStillCancelsLiveRuns:
    def test_a_registered_run_is_still_cancelled(self):
        async def _go():
            run = agent_runs.start("sess-1", _never_ends(), requested_at=time.monotonic())
            await asyncio.sleep(0.05)
            assert run.status == "running"
            assert agent_runs.stop("sess-1") is True       # True == a live run died
            await asyncio.sleep(0.05)
            return run.status

        assert asyncio.run(_go()) == "stopped"


class TestDeleteCancelsTheRun:
    def test_delete_session_cancels_an_in_flight_run(self, monkeypatch):
        # The chokepoint fix: every delete path funnels through delete_session,
        # so none of them can forget to cancel.
        import core.session_manager as sm

        called = {}
        monkeypatch.setattr(agent_runs, "stop", lambda sid: called.setdefault("sid", sid) or True)

        mgr = sm.SessionManager.__new__(sm.SessionManager)
        try:
            mgr.delete_session("sess-to-delete")
        except Exception:
            pass  # DB teardown is irrelevant — we only assert the cancel fired first
        assert called.get("sid") == "sess-to-delete"
