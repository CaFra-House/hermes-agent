"""Tests for the hard approval gate (kanban_approval_gate).

Reproduces the 31/10 incident (``t_fff1ff26``): a card born in ``triage`` with
the clef marker ``aprobacion_julio=si`` must never be promoted/decomposed/
claimed — it stays ``blocked`` across ticks and only an explicit ``unblock``
releases it. LLM-free: the auxiliary client is patched and asserted NOT called.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_approval_gate as gate
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp
from hermes_cli import kanban_specify as spec

CLEF_MARKED = (
    "Idea: aceptar hosting del proveedor.\n\n"
    "CLEF-TRIAGE: tipo=mail(0.94) complejidad=baja(0.88) riesgo=alto(0.91) "
    "ejecutor=humano(0.72) aprobacion_julio=si"
)
#: Portero classification line with a CONFIDENCE SUFFIX on the affirmative
#: value: DATA, not an order — must never park the card (t_62fa49e1, 03/11).
CLEF_DATA_LINE = (
    "Trabajo: arreglar el deploy.\n\n"
    "CLEF-TRIAGE: tipo=codigo(0.78) complejidad=media(0.76) riesgo=medio(0.68) "
    "ejecutor=servidor(0.93) aprobacion_julio=si(0.96) lat=1120ms coste=$0"
)
IDEA_MARKED = (
    "Idea: aceptar hosting del proveedor.\n\n"
    "APROBACION_JULIO: REQUERIDA — carta bloqueada a proposito; desbloquear "
    "solo con la decision de Julio."
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _aux_that_must_not_run():
    """Patch call_llm with a mock that fails the test if it is ever called."""
    mock_fn = MagicMock(side_effect=AssertionError("aux LLM must not be called"))
    return patch("agent.auxiliary_client.call_llm", mock_fn), mock_fn


def _legacy_triage_card(conn, body: str, title: str = "legacy marked card") -> str:
    """A card that reached ``triage`` WITHOUT the birth gate (stale writer).

    Born clean, then its body is edited to carry the marker — the exact shape
    of the incident: the marker arrives through the clef classifier/edit path,
    not through ``create_task``.
    """
    tid = kb.create_task(conn, title=title, body="rough idea", triage=True)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (body, tid))
    assert kb.get_task(conn, tid).status == "triage"
    return tid


def _events(conn, tid: str, kind: str | None = None) -> list[dict]:
    sql = "SELECT kind, payload FROM task_events WHERE task_id = ?"
    params: tuple = (tid,)
    if kind:
        sql += " AND kind = ?"
        params += (kind,)
    rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    return [{"kind": r["kind"], "payload": json.loads(r["payload"] or "{}")} for r in rows]


def _gate_events(conn, tid: str) -> list[dict]:
    return [e for e in _events(conn, tid, "blocked")
            if e["payload"].get("reason") == gate.GATE_REASON]


# ---------------------------------------------------------------------------
# Marker detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    CLEF_MARKED,
    IDEA_MARKED,
    "aprobacion_julio=si",
    "aprobacion_julio = si",
    "aprobacion_julio=sí",
    "aprobacion_julio=si, revisar luego",
    "APROBACION_JULIO: requerida",
])
def test_marker_detected(text):
    assert gate.marker_hit(text) is not None


@pytest.mark.parametrize("text", [
    "CLEF-TRIAGE: tipo=mail aprobacion_julio=no(0.9)",
    # t_62fa49e1: the portero's affirmative value with a confidence suffix is
    # data, not an order — with or without a space before the parenthesis.
    "aprobacion_julio=si(0.96)",
    "aprobacion_julio=si (0.96)",
    "APROBACION_JULIO=SI(1.00)",
    CLEF_DATA_LINE,
    "aprobacion_julio=siguiente",
    "sin marcador de aprobacion",
    "",
    None,
])
def test_marker_not_detected(text):
    assert gate.marker_hit(text) is None


def test_marker_in_title_counts():
    assert gate.marker_hit("APROBACION_JULIO: REQUERIDA", None) is not None


# ---------------------------------------------------------------------------
# Birth gate: create --triage + marker -> parked at birth
# ---------------------------------------------------------------------------


def test_triage_marked_card_is_born_blocked(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="aceptar hosting", body=CLEF_MARKED, triage=True)
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert _gate_events(conn, tid), "birth park must record the gate event"
        assert [c for c in kb.list_comments(conn, tid) if c.author == gate.GATE_ACTOR]
        assert tid not in [t.id for t in kb.list_tasks(conn, status="triage")]


def test_incident_reproduction_stays_blocked_after_two_ticks(kanban_home):
    """The incident case: triage + aprobacion_julio=si -> no dispatch, ever."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="aceptar hosting", body=CLEF_MARKED, triage=True)

    for _tick in range(2):
        with kbc.connect() as conn:
            kb.recompute_ready(conn)          # the promoter
            assert kb.claim_task(conn, tid) is None  # the dispatcher's claim

    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        assert not [e for e in _events(conn, tid, "promoted")], "never promoted"
        assert not [e for e in _events(conn, tid, "claimed")], "never claimed"
        runs = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (tid,)
        ).fetchone()[0]
        assert runs == 0
        # One gate park only — repeat ticks are idempotent.
        assert len(_gate_events(conn, tid)) == 1


def test_initial_status_running_with_marker_is_born_blocked(kanban_home):
    """`create --initial-status running` must not smuggle a marked card past the gate."""
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="sneaky", body=IDEA_MARKED, initial_status="running")
        assert kb.get_task(conn, tid).status == "blocked"
        assert _gate_events(conn, tid)
        assert kb.claim_task(conn, tid) is None


def test_born_ready_marked_card_is_parked_by_claim(kanban_home):
    """A stale writer can still create a card straight into ``ready``."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="born ready", body="clean body")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (IDEA_MARKED, tid))
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.claim_task(conn, tid) is None
        task = kb.get_task(conn, tid)
        assert task.status == "blocked"
        rejected = _events(conn, tid, "claim_rejected")
        assert rejected and rejected[-1]["payload"]["reason"] == gate.GATE_REASON


# ---------------------------------------------------------------------------
# Specifier / decomposer refuse WITHOUT spending the aux LLM
# ---------------------------------------------------------------------------


def test_specify_refuses_marked_triage_card(kanban_home):
    with kbc.connect() as conn:
        tid = _legacy_triage_card(conn, CLEF_MARKED)
    patcher, mock_fn = _aux_that_must_not_run()
    with patcher:
        outcome = spec.specify_task(tid)
    assert outcome.ok is False
    assert "approval gate" in outcome.reason
    mock_fn.assert_not_called()
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "blocked"
        assert _gate_events(conn, tid)


def test_decompose_refuses_marked_triage_card(kanban_home):
    with kbc.connect() as conn:
        tid = _legacy_triage_card(conn, IDEA_MARKED)
    patcher, mock_fn = _aux_that_must_not_run()
    with patcher:
        outcome = decomp.decompose_task(tid)
    assert outcome.ok is False
    assert "approval gate" in outcome.reason
    mock_fn.assert_not_called()
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "blocked"
        children = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE id != ?", (tid,)
        ).fetchone()[0]
        assert children == 0
        assert not [e for e in _events(conn, tid, "decomposed")]


def test_db_layer_specify_and_decompose_refuse_marked_root(kanban_home):
    """Direct DB callers (dashboard, scripts) get the same refusal."""
    with kbc.connect() as conn:
        tid = _legacy_triage_card(conn, CLEF_MARKED)
        assert kb.specify_triage_task(
            conn, tid, title="Refined", body="new body", author="tester") is False
        from hermes_cli.kanban_db_graph import decompose_triage_task
        assert decompose_triage_task(
            conn, tid, root_assignee="orchestrator",
            children=[{"title": "child", "body": "x", "assignee": "cto", "parents": []}],
            author="decomposer",
        ) is None
        assert kb.get_task(conn, tid).status == "triage"


# ---------------------------------------------------------------------------
# Manual promote is not the approval valve
# ---------------------------------------------------------------------------


def test_promote_refuses_gated_card(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="gated", body=IDEA_MARKED)
        ok, reason = kb.promote_task(conn, tid, actor="operator")
        assert ok is False
        assert "approval marker" in reason
        assert kb.get_task(conn, tid).status == "blocked"


# ---------------------------------------------------------------------------
# Explicit unblock IS the approval
# ---------------------------------------------------------------------------


def test_unblock_releases_and_never_reparked(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="gated", body=CLEF_MARKED)
        assert kb.get_task(conn, tid).status == "blocked"

    with kbc.connect() as conn:
        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "ready"
        assert gate.is_released(conn, tid) is True

    # Two more ticks: no re-park, no extra gate event, dispatchable again.
    for _tick in range(2):
        with kbc.connect() as conn:
            kb.recompute_ready(conn)
            assert kb.get_task(conn, tid).status == "ready"

    with kbc.connect() as conn:
        assert len(_gate_events(conn, tid)) == 1
        assert kb.claim_task(conn, tid) is not None


def test_born_blocked_idea_card_releases_with_one_unblock(kanban_home):
    """kanban-idea.py flow: born blocked + marker -> one unblock releases."""
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="aceptar hosting", body=IDEA_MARKED, initial_status="blocked")
        assert kb.get_task(conn, tid).status == "blocked"
        assert _gate_events(conn, tid)
        assert kb.claim_task(conn, tid) is None  # not ready, nothing to claim

    with kbc.connect() as conn:
        assert kb.unblock_task(conn, tid) is True
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.claim_task(conn, tid) is not None


# ---------------------------------------------------------------------------
# Regression: unmarked cards keep working
# ---------------------------------------------------------------------------


def test_unmarked_triage_card_still_specifies(kanban_home):
    reply = json.dumps({"title": "Refined idea", "body": "**Goal**\nDo it."})
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = reply
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rough idea", body="plain body", triage=True)
    with patch("agent.auxiliary_client.call_llm", MagicMock(return_value=resp)):
        outcome = spec.specify_task(tid)
    assert outcome.ok is True
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).status == "ready"
        assert not _gate_events(conn, tid)


def test_unmarked_create_and_claim_unaffected(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="plain", body="nothing special here")
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.claim_task(conn, tid) is not None


# ---------------------------------------------------------------------------
# Regression t_62fa49e1 (03/11): portero verdict value vs. standalone literal
# ---------------------------------------------------------------------------


def test_clef_affirmative_conf_suffix_never_parks(kanban_home):
    """`aprobacion_julio=si(0.96)` in the portero line is DATA: not parked at
    birth, not parked by later ticks, claimable — the exact collision that
    sticky-blocked t_24a1dee6/t_71b6866b/t_1772746b must not recur."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="fix deploy", body=CLEF_DATA_LINE)
        assert kb.get_task(conn, tid).status == "ready"
        assert not _gate_events(conn, tid)
    for _tick in range(2):
        with kbc.connect() as conn:
            kb.recompute_ready(conn)
            assert kb.get_task(conn, tid).status == "ready"
    with kbc.connect() as conn:
        assert kb.claim_task(conn, tid) is not None
        assert not _gate_events(conn, tid)


def test_bare_affirmative_literal_still_parks(kanban_home):
    """Historical semantics preserved: the standalone literal (followed by
    end/space/punctuation, no numeric suffix) still parks at birth."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="trabajo", body="hay que hacerlo\n\naprobacion_julio=si")
        assert kb.get_task(conn, tid).status == "blocked"
        assert _gate_events(conn, tid)
        assert kb.claim_task(conn, tid) is None
