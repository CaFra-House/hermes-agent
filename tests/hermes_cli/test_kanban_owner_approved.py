"""owner_approved event tests (LOCAL PATCH t_bd34e150, carril-C permission by operator event).

The GitHub label ``ceo:aprobado`` is no longer the permission; the durable write layer in
``hermes_cli.kanban_db.record_owner_approved`` must accept interactive operator sessions and
fail closed for dispatcher workers / delegate children / worker-descendant PIDs, recording an
``owner_approve_denied`` audit row for every denial.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB, free of worker-env markers."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_SESSION_ID",
                "HERMES_KANBAN_DB", "HERMES_KANBAN_HOME", "HERMES_KANBAN_WORKSPACE",
                "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kbc.connect() as c:
        yield c


@pytest.fixture
def task(conn):
    t = kb.create_task(conn, title="carril C probe", assignee="cto")
    return t if isinstance(t, str) else t.id


def _events(conn, task_id):
    return [
        (row[0], json.loads(row[1]))
        for row in conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? AND kind LIKE 'owner_%' ORDER BY id",
            (task_id,),
        ).fetchall()
    ]


def test_operator_emits_owner_approved(conn, task):
    payload = kb.record_owner_approved(conn, task, repo="CaFra-House/GATE-INFRA", pr=3, sha_aprobado=SHA_A)
    assert payload["schema"] == 1
    assert payload["origin"] == "operator"
    assert payload["repo"] == "CaFra-House/GATE-INFRA"
    assert payload["pr"] == 3
    assert payload["sha_aprobado"] == SHA_A
    assert payload["pid"] == os.getpid()
    rows = _events(conn, task)
    assert len(rows) == 1 and rows[0][0] == "owner_approved"
    stored = rows[0][1]
    assert stored["sha_aprobado"] == SHA_A and stored["pr"] == 3


def test_env_worker_denied_and_recorded(conn, task, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", task)
    with pytest.raises(PermissionError) as exc:
        kb.record_owner_approved(conn, task, repo="CaFra-House/GATE-INFRA", pr=3, sha_aprobado=SHA_A)
    assert "owner_approve_denied" in str(exc.value)
    rows = _events(conn, task)
    assert [k for k, _ in rows] == ["owner_approve_denied"]
    assert "env_kanban_task" in rows[0][1]["reasons"]
    assert not any(k == "owner_approved" for k, _ in rows), "denial must never grant permission"


def test_delegated_child_fence_denied(conn, task, monkeypatch):
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(Path(kb.kanban_home())))
    with pytest.raises(PermissionError):
        kb.record_owner_approved(conn, task, repo="CaFra-House/GATE-INFRA", pr=3, sha_aprobado=SHA_A)
    rows = _events(conn, task)
    assert rows and rows[-1][0] == "owner_approve_denied"
    assert "delegation_fence" in rows[-1][1]["reasons"]


def test_worker_pid_ancestor_denied(conn, task):
    """A process whose PID chain contains a live run's worker_pid is a worker descendant."""
    tid = task
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, worker_pid, ended_at) "
        "VALUES (?, 'ghost', 'running', CAST(strftime('%s','now') AS INTEGER), ?, NULL)",
        (tid, os.getpid()),
    )
    conn.commit()
    with pytest.raises(PermissionError):
        kb.record_owner_approved(conn, tid, repo="CaFra-House/GATE-INFRA", pr=3, sha_aprobado=SHA_A)
    rows = _events(conn, tid)
    assert rows[-1][0] == "owner_approve_denied"
    assert "worker_pid_ancestor" in rows[-1][1]["reasons"]
    assert os.getpid() in rows[-1][1]["markers"]["pid_chain"]


def test_finished_runs_do_not_fence_operator(conn, task):
    tid = task
    conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, started_at, worker_pid, ended_at) "
        "VALUES (?, 'ghost', 'completed', CAST(strftime('%s','now') AS INTEGER) - 100, ?, "
        " CAST(strftime('%s','now') AS INTEGER))",
        (tid, os.getpid()),
    )
    conn.commit()
    payload = kb.record_owner_approved(conn, tid, repo="CaFra-House/GATE-INFRA", pr=3, sha_aprobado=SHA_A)
    assert payload["origin"] == "operator"


def test_sha_must_be_full_hex(conn, task):
    for bad in ("deadbeef", "a" * 39, "z" * 40, "", None):
        with pytest.raises(ValueError):
            kb.record_owner_approved(conn, task, repo="CaFra-House/GATE-INFRA", pr=3, sha_aprobado=bad)
    assert _events(conn, task) == []


def test_repo_must_be_owner_slash_repo(conn, task):
    with pytest.raises(ValueError):
        kb.record_owner_approved(conn, task, repo="GATE-INFRA", pr=3, sha_aprobado=SHA_A)


def test_unknown_task_rejected(conn, kanban_home):
    with pytest.raises(ValueError):
        kb.record_owner_approved(conn, "t_nosuch00", repo="CaFra-House/R", pr=1, sha_aprobado=SHA_A)


def test_multiple_valid_events_coexist(conn, task):
    """Re-approval after a new push appends a second event; both shas are auditable."""
    kb.record_owner_approved(conn, task, repo="CaFra-House/GATE-INFRA", pr=3, sha_aprobado=SHA_A)
    kb.record_owner_approved(conn, task, repo="CaFra-House/GATE-INFRA", pr=3, sha_aprobado=SHA_B)
    rows = _events(conn, task)
    assert [k for k, _ in rows] == ["owner_approved", "owner_approved"]
    assert [r[1]["sha_aprobado"] for r in rows] == [SHA_A, SHA_B]
