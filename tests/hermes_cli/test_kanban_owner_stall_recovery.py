"""Ficha t_718832e6 (patches 1/3 and 3/3): an owner event-loop stall mid-admission must not
abandon a durably committed admission, and the receipt poll must not hammer a stalled loop.

Root cause (CAUSA_RAIZ_t_ec056a29.md §4B): ``_submit()``'s catch-all swallowed every
GatewayClientError (it inherits ValueError), exited rc=1 with no retry, and the sweep booked
a crash against the card — two stalls hit ``failure_limit: 2`` and parked healthy cards as
``gave_up`` with nobody waking them. The owner side is idempotent (gateway/session_kanban.py:
a committed ``owner_admitted`` marker is reused and ``submit`` returns the ORIGINAL receipt for
a retry of the same request_id), so the submitter reconnects with backoff 2/4/8 s and keeps
waiting on the receipt until OWNER_WAIT_S.

Polling friction (patch 3/3): the receipt poll ran 5 rpc/s on a 30 s RPC budget; past a 30 s
owner stall every poll exploded in wait_for and resurfaced as "Gateway connection failed".
Now 1 s between polls with a 60 s rpc budget: 5x fewer rpc/s on a loop that already stalls.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest

from hermes_cli import gateway_client, kanban_worker_client
from hermes_cli.gateway_client import GatewayClientError


STALL_MSG = "Gateway connection failed; no local fallback"


def _rec_sleep():
    """asyncio.sleep stand-in: records the requested delays, never waits."""
    slept = []

    async def _sleep(seconds):
        slept.append(seconds)

    return slept, _sleep


class _FakeTime:
    """Monotonic clock stand-in: one fixed step per reading, so deadline tests stay fast."""

    def __init__(self, step):
        self.now = 0.0
        self.step = step

    def monotonic(self):
        self.now += self.step
        return self.now


def _patched_env(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)
    monkeypatch.setattr(kanban_worker_client, "admission_answer_budget", lambda: 10.0)
    from gateway import session_kanban
    monkeypatch.setattr(session_kanban, "worker_result",
                        lambda *_: {"exit_code": 0, "last_output": "card closed"})


def _owner_recovering(stall_connects, receipts, submitted, rpc_timeouts):
    """First ``stall_connects`` connections raise the stall literal inside kanban.run (the
    owner already committed the admission — resubmitting returns the original receipt), then
    connections answer normally and ``prompt.receipt`` walks the given status list."""
    state = {"connects": 0}

    @asynccontextmanager
    async def connect():
        state["connects"] += 1
        stalled = state["connects"] <= stall_connects

        class _Client:
            events = asyncio.Queue()

            async def rpc(self, method, **params):
                if method == "kanban.run":
                    if stalled:
                        # The stall hits AFTER the owner committed the admission: the count of
                        # successful submits must stay 1 (owner-side idempotency proven in
                        # gateway/session_kanban.py; the submitter must not double-submit).
                        raise GatewayClientError(STALL_MSG)
                    submitted.append(method)
                    return {"session_id": "s1",
                            "receipt": {"status": receipts[0], "admission_id": "a1"}}
                # prompt.receipt: consume one status per poll, terminal after the list.
                submitted.append(method)
                rpc_timeouts.append(params.get("_timeout"))
                status = receipts[min(len(receipts) - 1, submitted.count("prompt.receipt"))]
                return {"status": status, "admission_id": "a1"}

        yield _Client()

    return connect, state


@pytest.mark.asyncio
async def test_owner_stall_mid_admission_reconnects_and_waits_out_the_receipt(monkeypatch, tmp_path):
    """Patch 1/3 + 3/3: one stall at 2 s of backoff, then the SAME attempt is finished by
    polling the receipt to terminal — the card never books a failure."""
    _patched_env(monkeypatch, tmp_path)
    slept, sleep = _rec_sleep()
    monkeypatch.setattr(kanban_worker_client.asyncio, "sleep", sleep)
    submitted, rpc_timeouts = [], []
    connect, state = _owner_recovering(1, ["running", "running", "terminal"], submitted, rpc_timeouts)
    monkeypatch.setattr(gateway_client, "connect_gateway", connect)

    assert await kanban_worker_client.run({"task_id": "t1"}, str(tmp_path / "kanban.db")) == 0
    assert state["connects"] == 2, "the submitter reconnected instead of abandoning"
    assert slept[0] == 2.0, f"first backoff must be 2 s, got {slept}"
    assert submitted.count("kanban.run") == 1, "kanban.run was submitted once, not twice"
    assert submitted.count("prompt.receipt") == 2, "receipt polled until terminal"
    # Patch 3/3: every poll sleeps 1 s (was 0.2) and carries the 60 s rpc budget (was default 30).
    assert set(slept[1:]) == {kanban_worker_client._RECEIPT_POLL_S} == {1.0}
    assert set(rpc_timeouts) == {kanban_worker_client._RECEIPT_RPC_TIMEOUT_S} == {60.0}


@pytest.mark.asyncio
async def test_backoff_sequence_is_2_4_8_and_caps_then_deadline_books_the_loss(monkeypatch, tmp_path):
    """Persistent stall: sleeps follow 2/4/8 (capped), and PAST OWNER_WAIT_S the raise stands
    so main() can book it as infrastructure (test below) — bounded waiting, never a hang."""
    _patched_env(monkeypatch, tmp_path)
    slept, sleep = _rec_sleep()
    monkeypatch.setattr(kanban_worker_client.asyncio, "sleep", sleep)

    @asynccontextmanager
    async def connect():
        class _Client:
            events = asyncio.Queue()

            async def rpc(self, method, **params):
                raise GatewayClientError(STALL_MSG)
        yield _Client()

    monkeypatch.setattr(gateway_client, "connect_gateway", connect)
    # 180 s deadline read in 20 s steps: ~8 stall checks before the deadline bites.
    monkeypatch.setattr(kanban_worker_client, "time", _FakeTime(20.0))

    with pytest.raises(GatewayClientError, match="no local fallback"):
        await kanban_worker_client.run({"task_id": "t1"}, str(tmp_path / "kanban.db"))
    assert slept[:3] == [2.0, 4.0, 8.0], slept
    assert set(slept[3:]) <= {8.0}, "backoff caps at 8 s"
    assert len(slept) < 180 / 20, "waiting is bounded by OWNER_WAIT_S, not infinite"


@pytest.mark.asyncio
async def test_non_stall_client_error_still_abandons_at_once(monkeypatch, tmp_path):
    """Only the owner-stall literal earns reconnection; any other GatewayClientError keeps the
    old fail-fast (rc=1) path — a misbehaving card must not spin 180 s."""
    _patched_env(monkeypatch, tmp_path)
    slept, sleep = _rec_sleep()
    monkeypatch.setattr(kanban_worker_client.asyncio, "sleep", sleep)

    @asynccontextmanager
    async def connect():
        class _Client:
            events = asyncio.Queue()

            async def rpc(self, method, **params):
                raise GatewayClientError("stale_kanban_claim")
        yield _Client()

    monkeypatch.setattr(gateway_client, "connect_gateway", connect)
    with pytest.raises(GatewayClientError, match="stale_kanban_claim"):
        await kanban_worker_client.run({"task_id": "t1"}, str(tmp_path / "kanban.db"))
    assert slept == [], "no retry for non-stall errors"


def test_main_books_owner_stall_as_infrastructure_rc69(monkeypatch, capsys):
    """Criterion (a), end of the path: a simulated stall that outlives the wait exits with
    KANBAN_INFRASTRUCTURE_EXIT_CODE (69) + trailer, so the sweep REQUEUES the card without
    spending the failure budget — no more ``gave_up`` from an owner's own health."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    for key, val in {"HERMES_KANBAN_BOARD": "b", "HERMES_KANBAN_TASK": "t1",
                     "HERMES_KANBAN_RUN_ID": "1", "HERMES_KANBAN_CLAIM_LOCK": "l",
                     "HERMES_KANBAN_DB": "/tmp/x.db"}.items():
        monkeypatch.setenv(key, val)

    async def _stalled_run(params, board_db):
        raise GatewayClientError("Gateway connection timed out; no local fallback")

    monkeypatch.setattr(kanban_worker_client, "run", _stalled_run)
    assert kanban_worker_client.main() == kb.KANBAN_INFRASTRUCTURE_EXIT_CODE == 69
    err = capsys.readouterr().err
    assert "Kanban infrastructure loss" in err
    assert f"{KANBAN_WORKER_EXIT_TRAILER}69" in err, "the sweep reads exactly this trailer"


def test_main_keeps_retained_by_owner_as_a_counted_failure(monkeypatch, capsys):
    """The deliberate half of patch 2/3: 'retained by owner' means the worker died mid-turn
    while the owner may STILL be running the attempt (owner_reclaim_paused) — requeuing it as
    infrastructure would double-run the card. It must stay a counted rc=1."""
    for key, val in {"HERMES_KANBAN_BOARD": "b", "HERMES_KANBAN_TASK": "t1",
                     "HERMES_KANBAN_RUN_ID": "1", "HERMES_KANBAN_CLAIM_LOCK": "l",
                     "HERMES_KANBAN_DB": "/tmp/x.db"}.items():
        monkeypatch.setenv(key, val)

    async def _unknown_run(params, board_db):
        raise GatewayClientError("Kanban execution unknown; retained by owner")

    monkeypatch.setattr(kanban_worker_client, "run", _unknown_run)
    assert kanban_worker_client.main() == 1
    err = capsys.readouterr().err
    assert "Kanban owner refused" in err, "keeps the counted-refusal path"
