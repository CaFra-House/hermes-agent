"""Reap the progeny a dead worker leaves behind (orphaned terminal children).

Why this exists: every terminal spawn is ``start_new_session=True`` (the ``bash -lic`` wrapper
leads its own process group/session), so the worker's group kill — ``ManagedWorker.close()``
(#N55) and ``_die_with_owner`` — cannot reach it. When a kanban worker dies (complete, blocked,
timeout, reclaim or crash) its foreground ``pytest``/compilers and background jobs are
re-parented to PID 1 and keep burning CPU (the 31/10 incident: a 43-min orphaned
``bash -lic pytest`` pushed load to 24 on 4 cores).

Three layered reaps, all identity-checked (``(pid, create_time)`` pairs from the machine spawn
ledger, ``hermes_cli.process_identity``):

1. ``reap_children_of(spawner_pid)`` — a worker's registered terminal children. Called from the
   owner-initiated teardowns (``ManagedWorker.close``, ``_die_with_owner``) and the dispatcher's
   run-end paths.
2. ``reap_dead_spawner_children()`` — every registered terminal child whose spawner is provably
   dead (covers hard SIGKILL / gateway crash where no teardown ran). Persisted jobs
   (``persist_on_release``, #41225) survive their owner UNLESS they match a sweep pattern.
3. ``reap_orphaned_workspace_processes(roots)`` — the recovery sweep: PPID-1 processes whose
   cmdline/cwd comes from a kanban workspace or a pytest temp dir, including leftovers that
   predate registration.

Every kill goes through :func:`kill_tree`: TERM the recorded process group, wait, KILL, then
SIGKILL any ``setsid`` escapee snapshotted before the first signal (the #85125 recipe).
"""

from __future__ import annotations

import logging
import os
import signal
import time
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

#: Default floor for the pattern sweep: a fresh spawn can briefly look like an orphan while its
#: registering worker is still writing the ledger entry. Run-end reaps pass 0 (the run is over).
SWEEP_MIN_AGE_SECONDS = 120.0


def _test_run_without_ledger_override() -> bool:
    """True under a test runner without ``HERMES_SPAWN_LEDGER``: the machine ledger and the
    workspace sweep roots are PRODUCTION resources, and the dispatcher hooks call this module
    from inside test suites. Without the explicit override a test would reap real processes of
    live workers — so every real-resource access here is inert. Production never sets
    ``PYTEST_CURRENT_TEST`` and is unaffected."""
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) and not os.environ.get("HERMES_SPAWN_LEDGER", "").strip()


# ---------------------------------------------------------------------------- group kill


def _group_alive(pgid: Optional[int]) -> bool:
    if not isinstance(pgid, int) or pgid <= 1 or os.name != "posix":
        return False
    try:
        os.killpg(pgid, 0)  # windows-footgun: ok — POSIX process-group alive probe
        return True
    except OSError:
        return False


def kill_tree(pid: int, pgid: Optional[int] = None, create_time: Optional[float] = None,
              *, grace: float = 3.0) -> bool:
    """Kill a recorded child and everything under it; True when a signal was delivered.

    SIGTERM the recorded process GROUP (``start_new_session`` made the child its own leader, so
    the group holds the ``bash -lic`` wrapper plus its foreground command and in-group background
    jobs), poll, then SIGKILL. Descendants are snapshotted via psutil BEFORE the first signal and
    any survivor that escaped the group (a grandchild that called ``setsid``) is SIGKILLed after —
    children re-parent to init once the parent dies, so a later walk finds nothing (#85125).
    A recycled PID/PGID (leader's start time no longer matches) is never signalled (#43044).
    """
    import psutil

    from hermes_cli import process_identity as pi

    descendants = []
    try:
        proc = psutil.Process(int(pid))
        if pi._same_incarnation(proc, create_time):
            descendants = proc.children(recursive=True)
    except Exception:
        pass

    posix = os.name == "posix"
    group = pgid if (posix and isinstance(pgid, int) and pgid > 1) else None
    if group is not None:
        from tools.environments.local import _leader_is_ours

        expected = create_time if group == pid else None
        if not _leader_is_ours(group, expected):
            group = None  # recycled PGID: never signal a stranger's group

    signaled = False
    if group is not None:
        try:
            os.killpg(group, signal.SIGTERM)  # windows-footgun: ok — POSIX only
            signaled = True
        except OSError:
            pass
    if not signaled:
        try:
            proc = psutil.Process(int(pid))
            if pi._same_incarnation(proc, create_time):
                proc.terminate()
                signaled = True
        except Exception:
            pass

    if signaled and grace > 0:
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if not (_group_alive(group) or pi._pid_alive_matches(int(pid), create_time, strict=True) is True):
                break
            time.sleep(0.1)

    if _group_alive(group):
        try:
            os.killpg(group, signal.SIGKILL)  # windows-footgun: ok — POSIX only
        except OSError:
            pass
    try:
        proc = psutil.Process(int(pid))
        if pi._same_incarnation(proc, create_time):
            proc.kill()
    except Exception:
        pass
    if descendants and posix:
        from tools.environments.local import _sweep_escaped_descendants

        _sweep_escaped_descendants(descendants, group or 0)
    return signaled


# ---------------------------------------------------------------------------- ledger sweeps


def _terminal_child_entries() -> list[dict]:
    """Raw ledger rows for terminal children (INCLUDING dead-pid/live-group entries that
    ``ledger_entries`` filters out — their background grandchildren are the point)."""
    if _test_run_without_ledger_override():
        return []
    from hermes_cli import process_identity as pi

    with pi._LEDGER_LOCK:
        entries = pi._read_ledger_or_quarantine(pi._ledger_path())
    if not entries:
        return []
    return [e for e in entries if e.get("purpose") == pi.TERMINAL_CHILD_PURPOSE]


def _drop_entries(match) -> None:
    """Remove ledger entries for which ``match(entry)`` is True (the sweep handled them)."""
    if _test_run_without_ledger_override():
        return
    from hermes_cli import process_identity as pi

    with pi._LEDGER_LOCK:
        entries = pi._read_ledger_or_quarantine(pi._ledger_path()) or []
        kept = [e for e in entries if not match(e)]
        if len(kept) == len(entries):
            return
        try:
            from utils import atomic_json_write

            path = pi._ledger_path()
            pi._prepare_ledger_parent(path)
            atomic_json_write(path, kept, mode=0o600, ensure_ascii=True)
        except OSError:
            logger.debug("orphan reaper: ledger prune failed", exc_info=True)


def _entry_matches_roots(entry: dict, roots: Iterable[str]) -> bool:
    haystacks = [str(entry.get("cwd") or ""), str(entry.get("argv") or "")]
    return any(root and any(root in h for h in haystacks) for root in roots)


def _entry_is_killable(entry: dict, *, keep_persist: bool, pattern_roots: Iterable[str]) -> bool:
    if entry.get("persist") and keep_persist and not _entry_matches_roots(entry, pattern_roots):
        return False  # an explicitly persisted job survives its owner (#41225) outside sweep patterns
    pid = entry.get("pid")
    create = entry.get("create_time")
    from hermes_cli import process_identity as pi

    return bool(
        isinstance(pid, int) and pid > 1
        and (pi._pid_alive_matches(pid, create) is not False or _group_alive(entry.get("pgid")))
    )


def _entry_is_stale(entry: dict) -> bool:
    """True when nothing is left to kill (dead pid, no group members): the entry can be dropped."""
    from hermes_cli import process_identity as pi

    pid = entry.get("pid")
    return bool(
        isinstance(pid, int) and pid > 1
        and pi._pid_alive_matches(pid, entry.get("create_time")) is False
        and not _group_alive(entry.get("pgid"))
    )


def _kill_entry(entry: dict) -> bool:
    pid = int(entry["pid"])
    return kill_tree(pid, entry.get("pgid"), entry.get("create_time"))


def reap_children_of(spawner_pid: int, *, spawner_create: Optional[float] = None,
                     keep_persist: bool = True, pattern_roots: Iterable[str] = ()) -> list[int]:
    """Kill the registered terminal children of one spawner (owner-initiated teardown).

    Used by ``ManagedWorker.close()`` / ``_die_with_owner`` and by the dispatcher's run-end
    paths. ``keep_persist=True`` honours ``persist_on_release`` (#41225); a persisted job that
    matches a sweep root is reaped regardless. Returns the killed PIDs.
    """
    killed: list[int] = []
    for entry in _terminal_child_entries():
        if entry.get("spawner_pid") != spawner_pid:
            continue
        if spawner_create is not None and entry.get("spawner_create") not in (None, spawner_create):
            continue
        if not _entry_is_killable(entry, keep_persist=keep_persist, pattern_roots=pattern_roots):
            if _entry_is_stale(entry):
                _drop_entries(lambda e, pid=entry.get("pid"): e.get("pid") == pid and e.get("spawner_pid") == spawner_pid)
            continue  # a kept entry (persisted job, ambiguous identity) stays for later sweeps
        try:
            if _kill_entry(entry):
                killed.append(int(entry["pid"]))
        except Exception:
            logger.debug("orphan reaper: kill failed for pid %s", entry.get("pid"), exc_info=True)
        _drop_entries(lambda e, pid=entry.get("pid"): e.get("pid") == pid and e.get("spawner_pid") == spawner_pid)
    return killed


def reap_dead_spawner_children(*, pattern_roots: Iterable[str] = (), keep_persist: bool = True) -> list[int]:
    """Kill every registered terminal child whose spawner is provably gone.

    The hard-kill safety net: a SIGKILLed worker (stale reclaim, OOM, ``kill -9``) never runs its
    teardown, so its registered children would linger until this sweep. Persisted jobs survive
    unless they match a sweep pattern (a kanban-workspace job of a dead run must not outlive it).
    """
    from hermes_cli import process_identity as pi

    killed: list[int] = []
    for entry in _terminal_child_entries():
        if pi.spawner_is_dead(entry) is not True:
            continue
        if not _entry_is_killable(entry, keep_persist=keep_persist, pattern_roots=pattern_roots):
            if _entry_is_stale(entry):
                _drop_entries(lambda e, pid=entry.get("pid"): e.get("pid") == pid)
            continue  # a kept entry (persisted job, ambiguous identity) stays for later sweeps
        try:
            if _kill_entry(entry):
                killed.append(int(entry["pid"]))
        except Exception:
            logger.debug("orphan reaper: kill failed for pid %s", entry.get("pid"), exc_info=True)
        _drop_entries(lambda e, pid=entry.get("pid"): e.get("pid") == pid)
    return killed


# ---------------------------------------------------------------------------- pattern sweep


def workspace_roots() -> list[str]:
    """Roots whose processes count as run progeny: kanban workspaces + pytest temp dirs.

    ``HERMES_ORPHAN_SWEEP_ROOTS`` (``os.pathsep``-joined) overrides for tests/hosts with a
    custom layout. Duplicates removed, order preserved.
    """
    override = os.environ.get("HERMES_ORPHAN_SWEEP_ROOTS", "").strip()
    if override:
        roots = [p for p in override.split(os.pathsep) if p]
    else:
        roots = []
        try:
            from hermes_cli import kanban_db as _kb

            roots.append(str(_kb.workspaces_root()))
            roots.append(str(_kb.kanban_home() / "kanban" / "workspaces"))
        except Exception:
            logger.debug("orphan reaper: kanban workspace roots unavailable", exc_info=True)
        try:
            import getpass

            roots.append(f"/tmp/pytest-of-{getpass.getuser()}")
        except Exception:
            pass
    seen, out = set(), []
    for r in roots:
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _read_ppid(pid: int) -> Optional[int]:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
        # comm may contain spaces/parens: the fields after the LAST ')' are stable.
        tail = data[data.rfind(b")") + 2:].split()
        return int(tail[1])  # ppid is field 4 overall = index 1 in the post-comm tail
    except Exception:
        return None


def _read_proc_start(pid: int) -> Optional[float]:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read()
        tail = data[data.rfind(b")") + 2:].split()
        return float(tail[19])  # starttime field 22 overall = index 19 in the post-comm tail
    except Exception:
        return None


def _read_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def _read_cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def _proc_matches_roots(pid: int, roots: Iterable[str]) -> bool:
    roots = [r for r in roots if r]
    if not roots:
        return False
    cwd = _read_cwd(pid)
    cmdline = _read_cmdline(pid)
    for root in roots:
        if root in cmdline:
            return True
        if cwd and (cwd == root or cwd.startswith(root.rstrip("/") + "/")):
            return True
    return False


def reap_orphaned_workspace_processes(
    roots: Iterable[str], *, min_age_seconds: float = SWEEP_MIN_AGE_SECONDS,
    exclude_pids: Iterable[int] = (),
) -> list[int]:
    """Recovery sweep: kill PPID-1 processes whose cmdline/cwd comes from *roots*.

    The card's ``barrido de recovery``: a ghost is an orphan (PPID 1) whose command line or
    working directory belongs to a kanban workspace or a pytest temp dir. Protections: never
    our own pid, never younger than ``min_age_seconds`` (a fresh spawn can briefly look orphaned),
    never a pid the caller lists as live (``exclude_pids``: live workers of running claims), and
    never a pid whose ledger entry has a LIVE spawner (a deliberately backgrounded job of a
    running worker re-parents to init as soon as its shell exits).
    """
    if os.name != "posix":
        return []
    from hermes_cli import process_identity as pi

    roots = [r for r in roots if r]
    if not roots:
        return []
    exclude = {int(p) for p in exclude_pids if p}
    registered_live = {
        int(e["pid"]) for e in _terminal_child_entries()
        if isinstance(e.get("pid"), int) and pi.spawner_is_dead(e) is False
    }
    killed: list[int] = []
    if _test_run_without_ledger_override():
        return killed
    me = os.getpid()
    now = time.time()
    try:
        pids = [int(p) for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return []
    for pid in pids:
        if pid in (1, me) or pid in exclude or pid in registered_live:
            continue
        if _read_ppid(pid) != 1:
            continue
        try:
            import psutil

            age = now - psutil.Process(pid).create_time()
        except Exception:
            continue
        if age < min_age_seconds:
            continue
        if not _proc_matches_roots(pid, roots):
            continue
        create = pi._process_create_time(pid)
        try:
            if kill_tree(pid, os.getpgid(pid), create):  # windows-footgun: ok — POSIX-only branch
                killed.append(pid)
                logger.info("orphan reaper: killed workspace orphan pid %s (age %.0fs)", pid, age)
        except Exception:
            logger.debug("orphan reaper: workspace orphan kill failed for pid %s", pid, exc_info=True)
    return killed


# ---------------------------------------------------------------------------- entry points


def live_worker_pids(conn=None) -> set[int]:
    """Worker PIDs of currently ``running`` claims on the board — never swept as ghosts."""
    close = False
    if conn is None:
        try:
            from hermes_cli import kanban_db_connect as _kbc

            conn = _kbc.connect()
            close = True
        except Exception:
            logger.debug("orphan reaper: board db unavailable for live-worker guard", exc_info=True)
            return set()
    try:
        rows = conn.execute(
            "SELECT worker_pid FROM tasks WHERE status = 'running' AND worker_pid IS NOT NULL"
        ).fetchall()
        return {int(r[0]) for r in rows if r and r[0]}
    except Exception:
        logger.debug("orphan reaper: live-worker query failed", exc_info=True)
        return set()
    finally:
        if close:
            try:
                conn.close()
            except Exception:
                pass


def periodic_sweep(conn=None, *, pattern_roots: Optional[Iterable[str]] = None) -> dict:
    """Dead-spawner ledger reap + workspace pattern sweep. Safe to run from any dispatcher tick.

    Fails CLOSED on the pattern sweep: when the board DB cannot be read, live workers cannot be
    excluded, so only the (precise) ledger-based reap runs.
    """
    roots = list(pattern_roots) if pattern_roots is not None else workspace_roots()
    killed = reap_dead_spawner_children(pattern_roots=roots)
    live = live_worker_pids(conn)
    if live or conn is not None:
        killed += reap_orphaned_workspace_processes(roots, exclude_pids=live)
    else:
        logger.debug("orphan reaper: no live-worker guard (board db unreadable); pattern sweep skipped")
    return {"killed": killed}


def reap_run_progeny(task_id: str, run_id: Optional[int], pid: Optional[int] = None,
                     started_at=None, *, workspace: Optional[str] = None,
                     conn=None) -> list[int]:
    """Run-end tree kill: everything a finished kanban run left behind.

    1. Stop the run's transient systemd scope (``hermes-worker-kanban-<task>-run-<run>.scope``):
       cgroup-wide teardown, catches setsid escapees a group kill cannot reach.
    2. Kill the worker's registered terminal children (the submitter's and — via the pattern
       sweep — the managed worker's).
    3. Kill the worker's own process group (``os.killpg`` on the group it leads), identity-checked
       against the spawn fingerprint.
    4. Sweep the run's workspace for PPID-1 leftovers (cmdline/cwd anchors), MIN_AGE 0.

    A run that is no longer the task's live run owns its workspace outright; a task already
    re-claimed by a NEWER run skips step 4 so the new run's processes are never swept.
    """
    killed: list[int] = []
    if os.name == "nt":
        return killed

    # 1 — scope teardown (unique per run; a missing unit is a no-op success).
    if run_id is not None:
        try:
            from tools.process_registry import _stop_systemd_unit

            _stop_systemd_unit(f"hermes-worker-kanban-{task_id}-run-{int(run_id)}.scope")
        except Exception:
            logger.debug("orphan reaper: scope stop failed for %s run %s", task_id, run_id, exc_info=True)

    # 2 — registered terminal children of the worker.
    if pid:
        try:
            killed += reap_children_of(int(pid), keep_persist=False, pattern_roots=workspace_roots())
        except Exception:
            logger.debug("orphan reaper: children reap failed for pid %s", pid, exc_info=True)

        # 3 — the worker's own group (non-escapees), identity-checked by the spawn fingerprint.
        try:
            start = _fingerprint_start(started_at)
            if start is not None:
                pgid = os.getpgid(int(pid)) if _pid_exists(int(pid)) else int(pid)
                if pgid == int(pid) or not _pid_exists(int(pid)):
                    kill_tree(int(pid), int(pid), start, grace=2.0)
        except Exception:
            logger.debug("orphan reaper: group kill failed for pid %s", pid, exc_info=True)

    # 4 — workspace pattern sweep, guarded against a newer run already owning the workspace.
    try:
        root = workspace or _task_workspace(task_id)
        if root:
            if _workspace_owned_by_newer_run(conn, task_id, run_id):
                logger.debug("orphan reaper: %s re-claimed by a newer run; workspace sweep skipped", task_id)
            else:
                killed += reap_orphaned_workspace_processes(
                    [root], min_age_seconds=0,
                    exclude_pids=live_worker_pids(conn) - ({int(pid)} if pid else set()),
                )
    except Exception:
        logger.debug("orphan reaper: workspace sweep failed for %s", task_id, exc_info=True)
    return killed


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)  # windows-footgun: ok — POSIX-only callers
        return True
    except OSError:
        return False


def _fingerprint_start(started_at) -> Optional[float]:
    """The start-time part of a ``_process_fingerprint`` value (``"<epoch>|<start>"``), or a
    legacy bare start time. ``None`` for UNVERIFIED/absent: never guess a kill identity."""
    if started_at is None:
        return None
    if isinstance(started_at, str):
        if "|" not in started_at:
            return None
        started_at = started_at.split("|", 1)[1]
    try:
        return float(started_at)
    except (TypeError, ValueError):
        return None


def _task_workspace(task_id: str) -> Optional[str]:
    try:
        from hermes_cli import kanban_db_workspace as _kbw

        return str(_kbw.task_workspace(task_id))
    except Exception:
        try:
            from hermes_cli import kanban_db as _kb

            return str(_kb.workspaces_root() / task_id)
        except Exception:
            return None


def _workspace_owned_by_newer_run(conn, task_id: str, run_id: Optional[int]) -> bool:
    if conn is None or run_id is None:
        return False
    try:
        row = conn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    except Exception:
        return True  # unprovable → fail closed, never sweep a workspace a new run may own
    if row is None:
        return False
    status, current = row[0], row[1]
    return status == "running" and current is not None and int(current) != int(run_id)
