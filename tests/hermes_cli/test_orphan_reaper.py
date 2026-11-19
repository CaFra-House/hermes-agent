"""Worker death reaps its terminal children (31/10 incident).

A kanban worker's terminal spawns are ``start_new_session=True`` (``bash -lic`` leads its own
session), so every worker group kill misses them: an aborted run left a 43-min orphaned
``bash -lic pytest`` reparented to PID 1 that held load at 24 on 4 cores. This module regresses
the three reaps: owner teardown (close/_die_with_owner), the dead-spawner ledger sweep, and the
PPID-1 workspace/pytest recovery sweep — with real processes, real kills and an isolated machine
spawn ledger (``HERMES_SPAWN_LEDGER`` is env-based so spawned stubs write to the test ledger).

Heavy imports are module-level on purpose: tests/conftest.py sandboxes HERMES_HOME before test
modules load, and the home_io_guard refuses in-test I/O against the real home (import chains that
read the payload manifest must run at collection time, like the other gateway tests do).
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from gateway.session_managed_worker import ManagedWorker
from hermes_cli import orphan_reaper
from hermes_cli import process_identity as pi
from tools import process_registry

REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.platforms("linux", "macos")


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_SPAWN_LEDGER", str(tmp_path / "spawn_ledger.json"))
    monkeypatch.delenv("HERMES_ORPHAN_SWEEP_ROOTS", raising=False)
    yield


def _entries():
    with pi._LEDGER_LOCK:
        return pi._read_ledger_or_quarantine(pi._ledger_path()) or []


def _gone(pid):
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def _wait_gone(pid, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _gone(pid):
            return True
        time.sleep(0.05)
    return False


def _kill_quietly(pid):
    try:
        os.kill(pid, signal.SIGKILL)  # windows-footgun: ok — POSIX-only module
    except OSError:
        pass


# stub workers: spawn a terminal child through the REAL LocalEnvironment registration path


_STUB_TEMPLATE = """\
import os, sys, time
sys.path.insert(0, {repo!r})
from tools.environments.local import LocalEnvironment
env = LocalEnvironment(cwd={cwd!r})
proc = env._run_bash({cmd!r}, login=True)
print(proc.pid, flush=True)
{tail}
"""


def _spawn_stub(cwd: Path, cmd: str, tail: str = "time.sleep(300)") -> subprocess.Popen:
    script = _STUB_TEMPLATE.format(repo=str(REPO), cwd=str(cwd), cmd=cmd, tail=tail)
    return subprocess.Popen(
        [sys.executable, "-c", script], cwd=str(cwd),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


@pytest.mark.live_system_guard_bypass  # the reaper genuinely kills real, reparented processes
def test_close_reaps_setsid_terminal_children():
    """N55 escapee: a ``bash -lic`` child (own session) dies with the worker on close()."""
    workdir = Path(os.environ.get("TMPDIR", "/tmp")) / f"orphan-close-{os.getpid()}"
    workdir.mkdir(parents=True, exist_ok=True)
    worker = _spawn_stub(workdir, "sleep 300")
    bash_pid = int(worker.stdout.readline())
    sleep_pid = None
    try:
        # premise of the bug: the child is outside the worker's group (its own session)
        assert os.getpgid(bash_pid) != os.getpgid(worker.pid)
        # the foreground `sleep` runs in its own job group (interactive bash): the group kill
        # of close() cannot reach it either — the reaper must sweep it as an escapee
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            kids = psutil.Process(bash_pid).children()
            if kids:
                sleep_pid = kids[0].pid
                break
            time.sleep(0.05)
        ManagedWorker(worker).close()
        assert worker.poll() is not None
        assert _wait_gone(bash_pid), "close() left the worker's setsid terminal child alive"
        if sleep_pid is not None:
            assert _wait_gone(sleep_pid), "close() left the terminal child's escapee alive"
    finally:
        _kill_quietly(worker.pid)
        _kill_quietly(bash_pid)
        if sleep_pid is not None:
            _kill_quietly(sleep_pid)


@pytest.mark.live_system_guard_bypass  # the reaper genuinely kills real, reparented processes
def test_dead_spawner_background_job_is_reaped():
    """The incident shape: ``sleep 300 &`` of a killed worker dies within seconds.

    Interactive ``bash -lic`` runs background jobs in their own process group, so the job is
    doubly orphaned (own pgid + reparented to init). The dead-spawner + pattern reaps must
    still clear it well inside the card's 60-second window.
    """
    workdir = Path(os.environ.get("TMPDIR", "/tmp")) / f"orphan-bg-{os.getpid()}"
    workdir.mkdir(parents=True, exist_ok=True)
    marker = workdir / "sleep.pid"
    sleep_pid = None
    worker = _spawn_stub(workdir, f"set +m; sleep 300 >/dev/null 2>&1 & echo $! > {marker}", tail="os._exit(0)")
    try:
        worker.wait(timeout=10)  # worker dies at once (the crash), job keeps running
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        sleep_pid = int(marker.read_text().strip())
        assert not _gone(sleep_pid), "premise: the background job must outlive the dead worker"

        # the spawn guard registered the terminal child in the ledger
        entries = [e for e in _entries() if e.get("purpose") == pi.TERMINAL_CHILD_PURPOSE]
        assert entries, "the spawn guard must register terminal children in the ledger"

        # both reaps together clear the job: dead-spawner ledger sweep + pattern sweep.
        orphan_reaper.reap_dead_spawner_children(pattern_roots=orphan_reaper.workspace_roots())
        orphan_reaper.reap_orphaned_workspace_processes([str(workdir)], min_age_seconds=0)
        assert _wait_gone(sleep_pid), "background job of a dead worker survived the reaps"
    finally:
        _kill_quietly(worker.pid)
        if sleep_pid is not None:
            _kill_quietly(sleep_pid)


@pytest.mark.live_system_guard_bypass  # the reaper genuinely kills real, reparented processes
def test_reap_children_of_keeps_persist_jobs_unless_pattern():
    """persist_on_release (#41225) survives its owner; a sweep-pattern match still reaps it."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                            start_new_session=True)
    outside = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                               start_new_session=True)
    try:
        assert pi.register_child(proc.pid, pi.TERMINAL_CHILD_PURPOSE,
                                 detail={"pgid": os.getpgid(proc.pid), "persist": True,
                                         "cwd": "/nonexistent-other-root"})
        # pattern root matches this one's cwd -> reaped even though persisted
        assert pi.register_child(outside.pid, pi.TERMINAL_CHILD_PURPOSE,
                                 detail={"pgid": os.getpgid(outside.pid), "persist": True,
                                         "cwd": "/nonexistent-sweep-root"})
        killed = orphan_reaper.reap_children_of(os.getpid(), keep_persist=True,
                                                pattern_roots=["/nonexistent-sweep-root"])
        assert outside.pid in killed
        assert _wait_gone(outside.pid)
        assert proc.pid not in killed
        assert not _gone(proc.pid), "a persisted job outside sweep patterns must survive its owner"

        killed = orphan_reaper.reap_children_of(os.getpid(), keep_persist=False)
        assert proc.pid in killed
        assert _wait_gone(proc.pid)
    finally:
        _kill_quietly(proc.pid)
        _kill_quietly(outside.pid)


@pytest.mark.live_system_guard_bypass  # the reaper genuinely kills real, reparented processes
def test_workspace_orphan_sweep_respects_guards():
    """Card item 3: PPID-1 cmdline/cwd from a workspace root dies; guarded pids survive."""
    root = Path(os.environ.get("TMPDIR", "/tmp")) / f"ws-root-{os.getpid()}"
    ws = root / "t_deadrun"
    ws.mkdir(parents=True, exist_ok=True)

    def _orphan():
        # bash starts `sleep 300 &` and exits: the sleep is reparented to init inside this cwd
        out = subprocess.Popen(["/bin/bash", "-c", "set +m; sleep 300 >/dev/null 2>&1 & echo $!"],
                               cwd=str(ws), stdout=subprocess.PIPE,
                               stdin=subprocess.DEVNULL, start_new_session=True)
        return int(out.communicate()[0])

    ghost, excluded, guarded = _orphan(), _orphan(), _orphan()
    try:
        for pid in (ghost, excluded, guarded):
            assert psutil.Process(pid).ppid() == 1

        # a registered child of a LIVE spawner (this test) must never be swept ...
        assert pi.register_child(guarded, pi.TERMINAL_CHILD_PURPOSE,
                                 detail={"pgid": None, "persist": False, "cwd": str(ws)})
        killed = orphan_reaper.reap_orphaned_workspace_processes(
            [str(root)], min_age_seconds=0, exclude_pids={excluded})
        assert ghost in killed
        assert _wait_gone(ghost)
        assert excluded not in killed and not _gone(excluded), "exclude_pids must be honoured"
        assert guarded not in killed and not _gone(guarded), "live-spawner registrations are not ghosts"

        # once the guard is lifted the sweep clears them too
        orphan_reaper._drop_entries(lambda e: e.get("pid") == guarded)  # registration ends with its owner
        killed = orphan_reaper.reap_orphaned_workspace_processes([str(root)], min_age_seconds=0)
        assert {excluded, guarded} <= set(killed)
        assert _wait_gone(excluded) and _wait_gone(guarded)
    finally:
        for pid in (ghost, excluded, guarded):
            _kill_quietly(pid)


@pytest.mark.live_system_guard_bypass  # the reaper genuinely kills real, reparented processes
def test_reap_run_progeny_stops_scope_and_clears_workspace():
    """Run end: the transient scope is stopped, children die, workspace PPID-1 leftovers die."""
    root = Path(os.environ.get("TMPDIR", "/tmp")) / f"ws-run-{os.getpid()}"
    ws = root / "t_abc123"
    ws.mkdir(parents=True, exist_ok=True)

    stopped = []
    real_stop = process_registry._stop_systemd_unit
    process_registry._stop_systemd_unit = lambda unit: stopped.append(unit) or True
    real_live = orphan_reaper.live_worker_pids
    orphan_reaper.live_worker_pids = lambda conn=None: set()  # never touch the live board from a test
    worker = _spawn_stub(ws, "sleep 300")
    bash_pid = int(worker.stdout.readline())
    # an unregistered workspace leftover (pre-fix style): PPID-1 under the run workspace
    leftover = subprocess.Popen(["/bin/bash", "-c", "set +m; sleep 300 >/dev/null 2>&1 & echo $!"],
                                cwd=str(ws), stdout=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    leftover_pid = int(leftover.communicate()[0])
    try:
        os.kill(worker.pid, signal.SIGKILL)  # windows-footgun: ok — POSIX-only module
        worker.wait(timeout=5)
        create = psutil.Process(worker.pid).create_time() if not _gone(worker.pid) else None
        killed = orphan_reaper.reap_run_progeny(
            "t_abc123", 7, worker.pid, f"{time.time():.3f}|{create or 0}",
            workspace=str(ws), conn=None)
        assert stopped == ["hermes-worker-kanban-t_abc123-run-7.scope"]
        assert _wait_gone(bash_pid), "run end must kill the worker's registered terminal children"
        assert leftover_pid in killed and _wait_gone(leftover_pid), \
            "run end must sweep PPID-1 leftovers under the run workspace"
    finally:
        process_registry._stop_systemd_unit = real_stop
        orphan_reaper.live_worker_pids = real_live
        _kill_quietly(worker.pid)
        _kill_quietly(bash_pid)
        _kill_quietly(leftover_pid)


@pytest.mark.live_system_guard_bypass  # the reaper genuinely kills real, reparented processes
def test_periodic_sweep_skips_pattern_without_live_guard():
    """Fails closed: with no board DB to prove live workers, the pattern sweep stays off."""
    root = Path(os.environ.get("TMPDIR", "/tmp")) / f"ws-closed-{os.getpid()}"
    root.mkdir(parents=True, exist_ok=True)
    out = subprocess.Popen(["/bin/bash", "-c", "set +m; sleep 300 >/dev/null 2>&1 & echo $!"],
                           cwd=str(root), stdout=subprocess.PIPE,
                           stdin=subprocess.DEVNULL, start_new_session=True)
    ghost = int(out.communicate()[0])
    try:
        real_live = orphan_reaper.live_worker_pids
        orphan_reaper.live_worker_pids = lambda conn=None: set()  # board db unreadable -> no live guard
        try:
            orphan_reaper.periodic_sweep(conn=None, pattern_roots=[str(root)])
        finally:
            orphan_reaper.live_worker_pids = real_live
        assert not _gone(ghost), "pattern sweep must not run without the live-worker guard"
    finally:
        orphan_reaper.reap_orphaned_workspace_processes([str(root)], min_age_seconds=0)
        _kill_quietly(ghost)


def test_fingerprint_start_never_guesses():
    assert orphan_reaper._fingerprint_start(None) is None
    assert orphan_reaper._fingerprint_start("UNVERIFIED") is None
    assert orphan_reaper._fingerprint_start("nonsense") is None
    assert orphan_reaper._fingerprint_start(f"{time.time():.3f}|123.456") == 123.456
    assert orphan_reaper._fingerprint_start(123.456) == 123.456
