"""Hard approval gate for cards that carry Julio's approval marker.

Some cards are born carrying the human-approval marker written by the intake
tooling (``kanban-idea.py``, ``clef-kanban-triage.py``)::

    CLEF-TRIAGE: tipo=... aprobacion_julio=si(0.96) ...
    APROBACION_JULIO: REQUERIDA — carta bloqueada a proposito; ...

Such a card must never be *promoted* (``triage -> todo``, ``todo -> ready``)
nor *claimed* by an automatic path. The specifier, the decomposer, the ready
promoter (``recompute_ready``), the manual promoter and the claim path all
refuse it and park it as ``blocked`` (sticky) until an operator runs an
explicit ``hermes kanban unblock`` — that unblock IS the approval act.

Why the check lives at every entry point instead of only in the intake tool:
incident 31/10/2026 00:30-00:34 (task ``t_fff1ff26``). A card created with
``hermes kanban create --triage`` (bypassing ``kanban-idea.py``) carried the
clef verdict ``aprobacion_julio=si``; the gateway's ``auto_decompose`` moved it
triage -> specified -> promoted -> todo inside one 60 s tick and the dispatcher
handed it to a worker that started drafting a REAL USD-450/month acceptance
mail to a third-party hosting provider. The only defence was intake discipline,
which any human or agent can skip without warning — so the gate is enforced
independently of how the card is born.

Release semantics (:func:`is_released`): an explicit ``unblocked`` /
``promoted_manual`` event *newer* than the newest ``approval_gate`` park event
releases the card for good — the operator's unblock is the approval, and the
marker stays in the body as a historical note. Cards parked before this gate
existed (no ``approval_gate`` event yet) are released by an unblock newer than
their ``created`` event, so one unblock always suffices.

Import safety: the ``kanban_db`` helpers are imported lazily inside the
functions, so ``kanban_db`` can import this module at module level without a
cycle.
"""

from __future__ import annotations

import re
import time
from typing import Optional

#: Author recorded on the gate's audit comment and on the park event.
GATE_ACTOR = "approval-gate"

#: ``reason`` stamped on the gate's ``blocked`` event (the release anchor).
GATE_REASON = "approval_gate"

#: Explicit operator actions that count as approval (newer than the park).
RELEASE_EVENT_KINDS = ("unblocked", "promoted_manual")

#: Statuses the gate may park from (a running card keeps its worker).
PARKABLE_STATUSES = ("triage", "todo", "ready")

_MARKERS = (
    # clef verdict line: `... aprobacion_julio=si(0.96) ...` / `aprobacion_julio = sí`
    re.compile(r"aprobacion_julio\s*=\s*s[ií]\b", re.IGNORECASE),
    # kanban-idea.py note: `APROBACION_JULIO: REQUERIDA — ...`
    re.compile(r"aprobacion_julio\s*:\s*requerida\b", re.IGNORECASE),
)


def marker_hit(*texts: Optional[str]) -> Optional[str]:
    """First approval marker found in ``texts`` (title, body, ...), else ``None``."""
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        for pattern in _MARKERS:
            match = pattern.search(text)
            if match:
                return match.group(0)
    return None


def comment_text(marker: str) -> str:
    """Audit comment left on a gated card (never re-states the marker form)."""
    return (
        "Gate de aprobacion: esta carta lleva el marcador "
        f"`{marker}` y queda BLOQUEADA. Ningun specifier/decomposer/promotor "
        "puede promoverla ni el dispatcher reclamarla hasta que Julio apruebe. "
        "Al aprobar, liberar con: `hermes kanban unblock <id>` (ese unblock es "
        "el acto de aprobacion; a partir de ahi la carta fluye normal)."
    )


# --- event bookkeeping (read-only) -----------------------------------------


def _latest_gate_event_id(conn, task_id: str) -> int:
    """Id of the newest ``approval_gate`` park event, or 0 when never parked."""
    from hermes_cli.kanban_db import _json_dict

    rows = conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id = ? AND kind = 'blocked' "
        "ORDER BY id DESC", (task_id,),
    ).fetchall()
    for row in rows:
        if _json_dict(row["payload"]).get("reason") == GATE_REASON:
            return int(row["id"])
    return 0


def _latest_release_event_id(conn, task_id: str) -> int:
    """Id of the newest explicit release (unblock / manual promote), or 0."""
    row = conn.execute(
        "SELECT MAX(id) AS last FROM task_events WHERE task_id = ? "
        "AND kind IN ('unblocked', 'promoted_manual')", (task_id,),
    ).fetchone()
    return int((row["last"] if row is not None else 0) or 0)


def _created_event_id(conn, task_id: str) -> int:
    row = conn.execute(
        "SELECT MIN(id) AS first FROM task_events WHERE task_id = ? AND kind = 'created'",
        (task_id,),
    ).fetchone()
    return int((row["first"] if row is not None else 0) or 0)


def is_released(conn, task_id: str) -> bool:
    """True once an operator explicitly released the card after it was parked."""
    released_at = _latest_release_event_id(conn, task_id)
    if not released_at:
        return False
    baseline = _latest_gate_event_id(conn, task_id) or _created_event_id(conn, task_id)
    return released_at > baseline


def gate_active(
    conn, task_id: str, *, title: Optional[str] = None, body: Optional[str] = None,
) -> Optional[str]:
    """Matched marker when the card must stay parked, else ``None``.

    ``title``/``body`` let callers that already hold the row skip the extra
    SELECT; passing neither reads them from ``tasks``.
    """
    if title is None or body is None:
        row = conn.execute(
            "SELECT title, body FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return None
        title, body = row["title"], row["body"]
    hit = marker_hit(title, body)
    if not hit:
        return None
    return None if is_released(conn, task_id) else hit


# --- park ------------------------------------------------------------------


def park(conn, task_id: str, marker: str, *, actor: str = GATE_ACTOR) -> bool:
    """Ensure a gated card is ``blocked`` with ONE gate event + audit comment.

    Idempotent: the first park records the ``approval_gate`` event and the
    comment; later ticks never duplicate them (the event is also the release
    anchor for :func:`is_released`). Only parked from
    ``triage``/``todo``/``ready`` — a ``running`` card keeps its worker; the
    claim path already refuses new claims. Safe inside an open write txn
    (savepoint) or standalone.
    """
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn, allow_nested=True):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,),
        ).fetchone()
        if row is None:
            return False
        acted = False
        if row["status"] in PARKABLE_STATUSES:
            cur = conn.execute(
                "UPDATE tasks SET status = 'blocked' WHERE id = ? "
                "AND status IN ('triage', 'todo', 'ready')", (task_id,),
            )
            acted = cur.rowcount == 1
        if _latest_gate_event_id(conn, task_id) == 0:
            now = int(time.time())
            kb._append_event(conn, task_id, "blocked", {
                "reason": GATE_REASON,
                "status": "blocked",
                "actor": actor,
                "marker": marker[:120],
            })
            text = comment_text(marker)
            kb._insert_comment(conn, task_id, actor, text, now)
            # Mirror add_comment's event so the board's diagnostics (which only
            # look at events) see the human-facing note.
            kb._append_event(conn, task_id, "commented",
                             {"author": actor, "len": len(text)})
            acted = True
        return acted
