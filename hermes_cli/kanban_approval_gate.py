"""Hard approval gate for cards that carry Julio's approval marker.

Some cards are born carrying the human-approval marker — the STANDALONE
affirmative literal — or the intake note written by ``kanban-idea.py``
(``aprobacion_julio=si(0.96)`` is NOT one of them, see below)::

    aprobacion_julio=si            (bare literal — an order)
    APROBACION_JULIO: REQUERIDA — carta bloqueada a proposito; ...

The portero's classification line is DATA, not an order. Its affirmative
value carries a confidence suffix (``aprobacion_julio=si(0.96)``) and the
unbounded match parked every swept card on the next claim/scan — incident
t_62fa49e1, 03/11/2026. The matcher therefore excludes the numeric
``(conf)`` suffix; cards that truly need Julio's decision are still held by
the ``APROBACION_JULIO: REQUERIDA`` note (``kanban-idea.py``) or by the
portero's explicit ``block --kind needs_input``
(``clef-kanban-triage.py``), both independent of this matcher.

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
    # Bare affirmative literal only: `aprobacion_julio=si` / `= sí` followed by
    # end, space or punctuation — NOT by the portero's confidence suffix
    # `(0.96)` / ` (0.96)` (t_62fa49e1: the unbounded match treated the
    # verdict value as an order and parked every swept card).
    re.compile(r"aprobacion_julio\s*=\s*s[ií]\b(?!\s*\(\s*\d)", re.IGNORECASE),
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


# --- retention gate (incident 03/11 ESPECIFICADOR-SIN-GATE) -------------------
#
# A triage card whose TITLE/BODY carries a human-authorization phrase ("no
# ejecutar el corte sin OK del CEO") or that awaits a human decision
# (``block_kind='needs_input'``) must never have its body rewritten by the
# automatic specifier/decomposer. Measured twice on 03/11: ``t_9ca30b11``
# (08:14 — the rewrite erased the pending-authorization sentence, the card was
# promoted, claimed, and the site was cut to prod without approval) and
# ``t_a68cd4ee`` (09:29:40 — a card sitting in triage with
# ``block_kind='needs_input'`` was specified, retitled and promoted to ready
# by a heartbeat-run specify).
#
# Unlike the approval markers above this gate REFUSES (does not park): the card
# stays where it is, untouched, and a ``specify_refused_retention`` event
# records why, so the board shows the reason without an LLM call being spent.
# A human overrides with ``--force``; the forced retitle keeps the retention
# words in the NEW TITLE (incident rule 2: a gate that lives only in the body
# can be erased by the very rewrite it is supposed to survive). A title marker
# is chosen over a new ``tasks`` column because a schema field means migrating
# ``kanban.db`` across every profile's board — far beyond this fix.

#: ``kind`` of the audit event recorded when specify/decompose refuse.
REFUSAL_EVENT_KIND = "specify_refused_retention"

#: Retention phrases: plain-language authorization gates (accent/spacing
#: tolerant) plus the structured block kind written as prose.
_RETENTION_PATTERNS = (
    re.compile(r"\bOK\s+del\s+CEO\b", re.IGNORECASE),
    re.compile(r"\bsin\s+OK\b", re.IGNORECASE),
    re.compile(r"\bno\s+ejecutar\b", re.IGNORECASE),
    re.compile(r"\bno\s+cortar\b", re.IGNORECASE),
    re.compile(r"\bno\s+rotar\b", re.IGNORECASE),
    re.compile(r"\besperar\s+autoriza[cc]i[oó]n\b", re.IGNORECASE),
    re.compile(r"\bneeds_input\b", re.IGNORECASE),
)

#: ``block_kind`` value that marks a card as awaiting a human decision.
NEEDS_INPUT_KIND = "needs_input"


def retention_hit(*texts: Optional[str]) -> Optional[str]:
    """First authorization-retention phrase found in ``texts``, else ``None``."""
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        for pattern in _RETENTION_PATTERNS:
            match = pattern.search(text)
            if match:
                return match.group(0)
    return None


def retention_blocks(title: Optional[str], body: Optional[str],
                     block_kind: Optional[str]) -> tuple[Optional[str], bool]:
    """``(text_hit, needs_input)`` — either truthy means specify must refuse."""
    return retention_hit(title, body), (block_kind or "") == NEEDS_INPUT_KIND


def refusal_reason(task_id: str, hit: Optional[str], needs_input: bool) -> str:
    """Explicit refusal line (CLI stderr / dashboard inline / outcome reason)."""
    what = (f"body/title matches {hit!r}" if hit
            else f"card is blocked with block_kind={NEEDS_INPUT_KIND!r}")
    return (f"retention gate: {what} — the rewrite would erase a pending human "
            f"authorization, so the body was left untouched. Override explicitly "
            f"with: `hermes kanban specify {task_id} --force` (the retention "
            f"marker is kept in the new title) or edit the body by hand once the "
            f"human decision has landed.")


def record_refusal(conn, task_id: str, *, hit: Optional[str], needs_input: bool,
                   via: str) -> bool:
    """Append a ``specify_refused_retention`` event, deduped on the matched
    phrase against the newest refusal so the 60 s auto-decompose tick cannot
    spam ``task_events`` for a card that stays in triage. True when written."""
    from hermes_cli.kanban_db import _append_event, _json_dict

    marker = (hit or NEEDS_INPUT_KIND)[:120]
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, REFUSAL_EVENT_KIND),
    ).fetchone()
    if row is not None and _json_dict(row["payload"]).get("match") == marker:
        return False
    _append_event(conn, task_id, REFUSAL_EVENT_KIND,
                  {"match": marker, "needs_input": bool(needs_input), "via": via})
    return True


def guarded_title(hit: Optional[str], new_title: str) -> str:
    """Force-path title preservation: when the original card matched retention
    and the LLM's new title says nothing about it, prepend the marker so the
    gate survives the body rewrite (incident rule 2)."""
    if not new_title or new_title.strip() == "":
        return new_title
    if retention_hit(new_title):
        return new_title
    return f"[GATE-AUTH: {hit or NEEDS_INPUT_KIND}] {new_title.strip()}"


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
