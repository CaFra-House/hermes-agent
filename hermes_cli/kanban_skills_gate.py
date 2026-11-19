"""Kanban skills gate — validate a card's force-loaded ``skills`` against the
target profile's REAL skill index before the card reaches the dispatcher.

Incident (t_43f1c279, 31/10): a planner wrote objective phrases
(``cárnica 60-65%``, ``BIOMIX M PET y calcio``) into the ``skills`` field; the
worker then died BEFORE its first turn with
``ValueError: kanban_skills_unavailable``
(``gateway/session_kanban.py::_run_task_turns``) and the dispatcher crash-looped
the card.

Policy per route:
* LLM-driven card creation (swarm specs, decomposer children, ``kanban_create``
  tool) FILTERS: unresolvable entries are dropped, the card is created with the
  valid subset (or ``skills=[]`` when none survive) and a warning comment on the
  card names the dropped entries. Informative, never blocking — misplaced
  phrases are usually objective CONTENT and the comment preserves them.
* Explicit CLI input (``hermes kanban create --skill``) REJECTS at creation
  with a clear message naming the invalid entries (``hermes_cli/kanban.py``).
* Worker delivery (``gateway/session_kanban.py``) drops whatever still fails to
  resolve instead of raising before the first turn — the last-resort layer so
  ``kanban_skills_unavailable`` can never crash-loop a run again.

Validity mirrors the worker's own loader (``agent.skill_commands._load_skill_payload``
→ ``tools.skills_tool.skill_view``): a name is valid iff ``skill_view`` resolves
it under the ASSIGNEE profile's home, which also rejects operator-disabled and
platform-unsupported skills (they fail the lookup exactly as in the worker).

Fail-open on infrastructure errors: when the index cannot be consulted at all,
names pass unfiltered (logged) — a broken gate must not strip legitimate
skills. Per-name resolution is authoritative: a definitive "not found" is
always dropped.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# Comment author on cards where the gate dropped entries (greppable provenance).
SKILLS_GATE_ACTOR = "skills-gate"


def _profile_home(profile: Optional[str]) -> Optional[str]:
    """HERMES_HOME of *profile* (None → resolve under the active home)."""
    if not profile:
        return None
    try:
        from hermes_cli.profiles import resolve_profile_env
        return resolve_profile_env(str(profile).strip())
    except Exception:
        return None


def _resolve_under_profile(names: list[str], profile: Optional[str]) -> dict[str, bool]:
    """``{name: resolves}`` exactly as the worker's preload loader would see it."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    home = _profile_home(profile)
    token = set_hermes_home_override(home) if home else None
    try:
        import json

        from agent.skill_utils import normalize_skill_lookup_name
        from tools.skills_tool import skill_view

        out: dict[str, bool] = {}
        for name in names:
            try:
                payload = json.loads(skill_view(normalize_skill_lookup_name(name), preprocess=False))
                out[name] = bool(payload.get("success"))
            except Exception:
                logger.warning("skills-gate: skill_view failed for %r — keeping it (fail-open)", name)
                out[name] = True
        return out
    finally:
        if token is not None:
            reset_hermes_home_override(token)


def sanitize_skills(
    skills: Optional[Iterable], *, profile: Optional[str] = None,
) -> tuple[list[str], list[str]]:
    """Split a card's skill identifiers into ``(kept, dropped)``.

    * ``kept``: input order, stripped/deduped, every name that resolves in
      *profile*'s real skill index.
    * ``dropped``: the rest — never silently lost: callers put them in a
      warning comment on the card (see :func:`warning_comment`).
    * Empty/None input → ``([], [])`` (callers keep "no skills requested"
      distinct from "requested but none valid" → ``skills=[]``).
    """
    entries: list[str] = []
    seen: set[str] = set()
    for raw in skills or ():
        name = str(raw).strip() if raw is not None else ""
        if not name or name in seen:
            continue
        seen.add(name)
        entries.append(name)
    if not entries:
        return [], []
    try:
        resolution = _resolve_under_profile(entries, profile)
    except Exception:
        logger.warning(
            "skills-gate: cannot consult the skill index (profile=%r) — "
            "passing %d name(s) unfiltered", profile, len(entries), exc_info=True,
        )
        return entries, []
    kept = [n for n in entries if resolution.get(n)]
    dropped = [n for n in entries if not resolution.get(n)]
    return kept, dropped


def warning_comment(
    dropped: Iterable[str], *, profile: Optional[str] = None,
) -> str:
    """The informative, non-blocking warning body for a card's filtered entries."""
    names = ", ".join(repr(str(n)) for n in dropped)
    who = f"del perfil {profile!r}" if profile else "del perfil activo"
    return (
        f"⚠️ skills-gate — entradas filtradas del campo `skills`: {names}. "
        f"No resuelven en el índice real de skills {who} (no existen, están "
        f"deshabilitadas o no son soportadas en esta plataforma) y el worker "
        f"moriría antes del primer turno con `ValueError: kanban_skills_unavailable`. "
        f"La carta sigue con las skills válidas restantes (o `skills=[]` si no "
        f"quedó ninguna); el flujo NO se bloquea. Si esas entradas son contenido "
        f"del objetivo y no nombres de skill, movélas al body: `skills` solo "
        f"admite nombres del índice (`hermes skills list`)."
    )
