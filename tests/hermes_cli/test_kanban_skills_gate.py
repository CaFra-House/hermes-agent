"""Regression tests — kanban skills gate (incident t_43f1c279, 31/10).

What broke: a planner wrote objective phrases (``'cárnica 60-65%'``,
``'BIOMIX M PET y calcio'``) into a card's ``skills`` field. The worker could
not resolve them and died BEFORE its first turn with
``ValueError: kanban_skills_unavailable`` (``gateway/session_kanban.py``), the
owner admission settled ``unknown`` and the dispatcher crash-looped the card
three times under the misleading message "Kanban execution unknown".

These tests pin the four acceptance criteria of the fix:

1. ``hermes kanban create --skill <unknown>`` is REJECTED at creation (exit 2)
   with a message naming the invalid entries — a typo fails loudly instead of
   crash-looping a worker.
2. A swarm whose planner wrote phrases into ``skills`` creates the card with
   ``skills=[]`` plus a ``skills-gate`` warning comment, and the swarm still
   builds completely (informative, never blocking, never crash-looping).
3. The worker is never killed for that input: the delivery layer drops the
   unresolvable entries with a visible ``[skills-gate]`` note instead of
   raising, so the turn actually runs.
4. Real, valid skill names keep passing through untouched — CLI, swarm,
   decomposer, tool route and delivery.

Hermetic by construction: no real profile is consulted and nothing outside
``tmp_path`` is read or written. The skill index is a temporary HERMES_HOME
holding a real ``skills/<name>/SKILL.md`` tree, so the production resolver
(``tools.skills_tool.skill_view``, called through
``hermes_cli.kanban_skills_gate``) runs for real against fixture data instead
of a mock that could silently drift away from it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_skills_gate import (
    SKILLS_GATE_ACTOR,
    sanitize_skills,
    warning_comment,
)
from hermes_cli.kanban_swarm import SwarmWorkerSpec, create_swarm

# The real incident input (card t_43f1c279): objective content, not skill names.
OBJECTIVE_PHRASES = ["cárnica 60-65%", "BIOMIX M PET y calcio"]
VALID_SKILL = "fixture-valid-skill"
OTHER_VALID_SKILL = "fixture-other-skill"


# ---------------------------------------------------------------------------
# Fixtures — a real (but temporary) skill index
# ---------------------------------------------------------------------------


def _write_skill(skills_root: Path, name: str) -> None:
    directory = skills_root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: Fixture skill for the kanban skills-gate regression tests.\n"
        "---\n\n"
        f"# {name}\n\n"
        "Fixture body.\n",
        encoding="utf-8",
    )


@pytest.fixture
def skill_index(tmp_path, monkeypatch):
    """Sandboxed HERMES_HOME with a real skills tree; returns the skills dir.

    ``requesting-code-review`` / ``humanizer`` are the skills the swarm hard-codes
    for the verifier and the synthesizer, so the fixture index has to carry them
    for a swarm to be built without spurious gate warnings.
    """
    home = tmp_path / ".hermes"
    skills = home / "skills"
    for name in (VALID_SKILL, OTHER_VALID_SKILL, "requesting-code-review", "humanizer"):
        _write_skill(skills, name)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from tools import skills_tool

    # A patched SKILLS_DIR wins over the import-time default in ``_skills_dir()``.
    monkeypatch.setattr(skills_tool, "SKILLS_DIR", skills)
    return skills


@pytest.fixture
def kanban_home(skill_index, tmp_path, monkeypatch):
    """Board DB inside the sandbox — never the operator's real kanban.db."""
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    for leaked in ("HERMES_KANBAN_DB", "HERMES_KANBAN_TASK", "HERMES_TENANT"):
        monkeypatch.delenv(leaked, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create(argv: list[str]) -> int:
    """Drive the real ``hermes kanban create`` parser into ``_cmd_create``."""
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    args = root.parse_args(["kanban", "create", *argv])
    return kc._cmd_create(args)


def _tasks() -> list:
    with kbc.connect_closing() as conn:
        return kb.list_tasks(conn)


def _only_task():
    tasks = _tasks()
    assert len(tasks) == 1, f"expected exactly one task on the board, got {len(tasks)}"
    return tasks[0]


def _gate_comments(conn, task_id: str):
    return [c for c in kb.list_comments(conn, task_id) if c.author == SKILLS_GATE_ACTOR]


def _resolves(name: str) -> bool:
    from tools.skills_tool import skill_view

    return bool(json.loads(skill_view(name, preprocess=False)).get("success"))


# ---------------------------------------------------------------------------
# (1) CLI: explicit --skill input is rejected at creation
# ---------------------------------------------------------------------------


def test_cli_create_rejects_unknown_skill_at_creation(kanban_home, capsys):
    """Criterion 1: `--skill inexistente` fails at creation, naming the entry."""
    rc = _create(["Ficha PET", "--skill", "cárnica 60-65%", "--assignee", "tester"])

    assert rc == 2, "an unknown --skill must abort creation with exit code 2"
    err = capsys.readouterr().err
    assert "cárnica 60-65%" in err, err
    assert "invalid --skill" in err, err
    assert "hermes skills list" in err, err
    # Point the operator at the right place for objective content.
    assert "--body" in err, err
    # Nothing landed on the board: no card can reach the dispatcher.
    assert _tasks() == []


def test_cli_create_rejects_mixed_skills_naming_only_the_invalid_entry(kanban_home, capsys):
    rc = _create([
        "Ficha PET", "--skill", VALID_SKILL, "--skill", "BIOMIX M PET y calcio",
        "--assignee", "tester",
    ])

    assert rc == 2
    err = capsys.readouterr().err
    assert "BIOMIX M PET y calcio" in err, err
    assert VALID_SKILL not in err, err
    assert _tasks() == []


def test_cli_create_accepts_valid_skill(kanban_home, capsys):
    """Criterion 4 (CLI): a real skill name still creates the card untouched."""
    rc = _create(["Ficha PET", "--skill", VALID_SKILL, "--assignee", "tester"])

    assert rc == 0, capsys.readouterr().err
    assert _only_task().skills == [VALID_SKILL]


# ---------------------------------------------------------------------------
# Gate module unit behaviour
# ---------------------------------------------------------------------------


def test_sanitize_skills_keeps_valid_and_drops_objective_phrases(skill_index):
    kept, dropped = sanitize_skills(
        [VALID_SKILL, *OBJECTIVE_PHRASES], profile="tester",
    )

    assert kept == [VALID_SKILL]
    assert dropped == OBJECTIVE_PHRASES


def test_sanitize_skills_empty_and_none_mean_no_skills_requested(skill_index):
    assert sanitize_skills([], profile="tester") == ([], [])
    assert sanitize_skills(None, profile="tester") == ([], [])
    assert sanitize_skills(["", "   ", None], profile="tester") == ([], [])


def test_sanitize_skills_strips_dedupes_and_preserves_input_order(skill_index):
    kept, dropped = sanitize_skills(
        [f"  {VALID_SKILL}  ", OTHER_VALID_SKILL, VALID_SKILL, "nope"], profile="tester",
    )

    assert kept == [VALID_SKILL, OTHER_VALID_SKILL]
    assert dropped == ["nope"]


def test_sanitize_skills_fails_open_when_the_index_cannot_be_consulted(monkeypatch):
    """A broken index must not strip legitimate skills (fail-open)."""
    import hermes_cli.kanban_skills_gate as gate

    def explode(*_args, **_kwargs):
        raise RuntimeError("skill index unavailable")

    monkeypatch.setattr(gate, "_resolve_under_profile", explode)
    assert gate.sanitize_skills(["anything"], profile="tester") == (["anything"], [])


def test_warning_comment_names_every_dropped_entry():
    body = warning_comment(OBJECTIVE_PHRASES, profile="tester")

    for phrase in OBJECTIVE_PHRASES:
        assert phrase in body
    assert "kanban_skills_unavailable" in body
    assert "skills=[]" in body
    assert "NO se bloquea" in body


# ---------------------------------------------------------------------------
# (2) Swarm: the real incident input creates the card with skills=[] + warning
# ---------------------------------------------------------------------------


def test_swarm_with_objective_phrases_creates_card_with_empty_skills_and_warning(
    skill_index, tmp_path,
):
    """Criterion 2 with the REAL input: no crash loop, card born with skills=[]."""
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Armar la ficha del alimento cárnico 60-65% BIOMIX M PET y calcio.",
            workers=[
                SwarmWorkerSpec(
                    profile="tester",
                    title="Ficha PET",
                    body="Armar la ficha del alimento cárnico 60-65% BIOMIX M PET y calcio.",
                    skills=list(OBJECTIVE_PHRASES),
                ),
            ],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
        )

        worker = kb.get_task(conn, created.worker_ids[0])
        assert worker is not None
        assert worker.skills == [], "objective phrases must never reach the card's skills"

        comments = _gate_comments(conn, worker.id)
        assert len(comments) == 1, [c.body for c in comments]
        for phrase in OBJECTIVE_PHRASES:
            assert phrase in comments[0].body

        # Informative, never blocking: the whole swarm graph exists and the
        # hard-coded verifier/synthesizer skills survive untouched.
        assert kb.get_task(conn, created.root_id).status == "done"
        verifier = kb.get_task(conn, created.verifier_id)
        synthesizer = kb.get_task(conn, created.synthesizer_id)
        assert verifier.skills == ["requesting-code-review"]
        assert synthesizer.skills == ["humanizer"]
        assert _gate_comments(conn, created.verifier_id) == []
        assert _gate_comments(conn, created.synthesizer_id) == []
    finally:
        conn.close()


def test_swarm_valid_skills_pass_through_untouched(skill_index, tmp_path):
    """Criterion 4 (swarm): valid names are kept, order intact, no warning."""
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Ficha PET.",
            workers=[
                SwarmWorkerSpec(
                    profile="tester",
                    title="Ficha",
                    body="Ficha.",
                    skills=[OTHER_VALID_SKILL, VALID_SKILL],
                ),
            ],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
        )

        worker = kb.get_task(conn, created.worker_ids[0])
        assert worker.skills == [OTHER_VALID_SKILL, VALID_SKILL]
        assert _gate_comments(conn, worker.id) == []
    finally:
        conn.close()


def test_swarm_mixed_skills_keeps_valid_and_names_only_the_dropped(skill_index, tmp_path):
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Ficha PET.",
            workers=[
                SwarmWorkerSpec(
                    profile="tester",
                    title="Ficha",
                    body="Ficha.",
                    skills=[VALID_SKILL, "BIOMIX M PET y calcio"],
                ),
            ],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
        )

        worker = kb.get_task(conn, created.worker_ids[0])
        assert worker.skills == [VALID_SKILL]
        comments = _gate_comments(conn, worker.id)
        assert len(comments) == 1
        assert "BIOMIX M PET y calcio" in comments[0].body
        assert VALID_SKILL not in comments[0].body
    finally:
        conn.close()


def test_swarm_cards_never_carry_unresolvable_skills(skill_index, tmp_path):
    """Criterion 3 (invariant): nothing the gate lets through can kill a worker."""
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        created = create_swarm(
            conn,
            goal="Ficha PET con frases en skills.",
            workers=[
                SwarmWorkerSpec(
                    profile="tester", title="A", body="A",
                    skills=[*OBJECTIVE_PHRASES, VALID_SKILL],
                ),
                SwarmWorkerSpec(
                    profile="tester", title="B", body="B", skills=list(OBJECTIVE_PHRASES),
                ),
            ],
            verifier_assignee="reviewer",
            synthesizer_assignee="writer",
        )
        ids = [created.root_id, *created.worker_ids, created.verifier_id, created.synthesizer_id]
        for task_id in ids:
            task = kb.get_task(conn, task_id)
            for name in task.skills or []:
                assert _resolves(name), f"{task_id} carries unresolvable skill {name!r}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Decomposer: planner-written skills are sanitised before persistence
# ---------------------------------------------------------------------------


def _routing():
    from hermes_cli.kanban_decompose import _Routing

    return _Routing(
        orchestrator="tester",
        default_assignee="tester",
        auto_promote=True,
        roster=[{"name": "tester"}],
        valid_names={"tester"},
    )


def test_decomposer_children_filter_planner_skills(skill_index):
    from hermes_cli.kanban_decompose import _clean_children

    children, reason = _clean_children(
        "t_root",
        [
            {"title": "A", "body": "a", "assignee": "tester", "skills": OBJECTIVE_PHRASES},
            {"title": "B", "body": "b", "assignee": "tester", "skills": [VALID_SKILL]},
            {"title": "C", "body": "c", "assignee": "tester"},
        ],
        _routing(),
    )

    assert reason == ""
    assert children[0]["skills"] == []
    assert children[0]["skills_dropped"] == OBJECTIVE_PHRASES
    assert children[1]["skills"] == [VALID_SKILL]
    assert children[1]["skills_dropped"] == []
    # No skills requested at all stays None (defaults only), never [].
    assert children[2]["skills"] is None
    assert children[2]["skills_dropped"] == []


def test_decomposed_child_persists_empty_skills_and_warning_comment(kanban_home):
    """The DB write path: skills=[] lands on the row, warning on the thread."""
    from hermes_cli.kanban_db_graph import decompose_triage_task

    with kbc.connect_closing() as conn:
        root = kb.create_task(
            conn, title="Root triage", body="Plain objective, no gate markers.",
            assignee="tester", triage=True,
        )
        ids = decompose_triage_task(
            conn, root, root_assignee="tester",
            children=[{
                "title": "Child", "body": "child body", "assignee": "tester",
                "skills": [], "skills_dropped": list(OBJECTIVE_PHRASES),
            }],
        )
        assert ids and len(ids) == 1
        child = kb.get_task(conn, ids[0])
        assert child.skills == []
        comments = _gate_comments(conn, ids[0])
        assert len(comments) == 1
        for phrase in OBJECTIVE_PHRASES:
            assert phrase in comments[0].body


# ---------------------------------------------------------------------------
# Tool route (LLM `kanban_create`): filters + warns, never blocks
# ---------------------------------------------------------------------------


def _tool_create(args: dict) -> dict:
    from tools import kanban_tools as kt

    return json.loads(kt._handle_create(args))


def test_kanban_create_tool_filters_objective_phrases_and_warns(kanban_home):
    payload = _tool_create({
        "title": "Ficha PET", "assignee": "tester", "skills": OBJECTIVE_PHRASES,
    })

    assert payload["ok"] is True, payload
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, payload["task_id"])
        assert task.skills == []
        comments = _gate_comments(conn, payload["task_id"])
        assert len(comments) == 1
        for phrase in OBJECTIVE_PHRASES:
            assert phrase in comments[0].body


def test_kanban_create_tool_keeps_valid_skill(kanban_home):
    payload = _tool_create({
        "title": "Ficha PET", "assignee": "tester", "skills": [VALID_SKILL],
    })

    assert payload["ok"] is True, payload
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, payload["task_id"]).skills == [VALID_SKILL]
        assert _gate_comments(conn, payload["task_id"]) == []


# ---------------------------------------------------------------------------
# (3) Delivery: the worker is never killed before its first turn
# ---------------------------------------------------------------------------


class _FakeAgent:
    """Records the prompts the worker would have sent to the model."""

    session_id = "sess-skills-gate-test"
    _session_db = None

    def __init__(self):
        self.prompts: list[str] = []

    def run_conversation(self, prompt, conversation_history=None):
        self.prompts.append(prompt)
        return {"final_response": "ok", "messages": []}


def _delivery_context(skills):
    return {
        "skills": skills,
        "context": "CARD BODY: armar la ficha del alimento.",
        "task_id": "t_skills_gate",
        "run_id": 1,
        "goal_mode": False,
        "goal_max_turns": None,
        "goal_text": "",
    }


def test_delivery_drops_unresolvable_skills_and_still_runs_the_turn(skill_index):
    """Criterion 3: no ValueError, the turn actually runs, note is visible."""
    from gateway.session_kanban import _run_task_turns

    agent = _FakeAgent()
    result = _run_task_turns(
        agent, {"text": "Ejecutá la carta."}, [], _delivery_context(list(OBJECTIVE_PHRASES)),
    )

    assert result["final_response"] == "ok"
    assert len(agent.prompts) == 1, "the worker turn must run exactly once"
    prompt = agent.prompts[0]
    assert "[skills-gate]" in prompt
    for phrase in OBJECTIVE_PHRASES:
        assert phrase in prompt, "the dropped entries must stay visible to the model"
    assert "CARD BODY" in prompt


def test_delivery_handles_none_skills_without_raising(skill_index):
    from gateway.session_kanban import _run_task_turns

    agent = _FakeAgent()
    _run_task_turns(agent, {"text": "Ejecutá la carta."}, [], _delivery_context(None))

    assert len(agent.prompts) == 1
    assert "[skills-gate]" not in agent.prompts[0]
    assert "CARD BODY" in agent.prompts[0]


def test_delivery_preloads_a_valid_skill(skill_index):
    """Criterion 4 (delivery): valid skills still reach the prompt as content."""
    from gateway.session_kanban import _run_task_turns

    agent = _FakeAgent()
    _run_task_turns(agent, {"text": "Ejecutá la carta."}, [], _delivery_context([VALID_SKILL]))

    prompt = agent.prompts[0]
    assert VALID_SKILL in prompt
    assert "Fixture body." in prompt, "the real SKILL.md content must be preloaded"
    assert "[skills-gate]" not in prompt


def test_delivery_source_never_raises_kanban_skills_unavailable():
    """The exact original defect: this raise must never come back."""
    from gateway import session_kanban

    source = Path(session_kanban.__file__).read_text(encoding="utf-8")
    assert 'raise ValueError("kanban_skills_unavailable")' not in source
    assert "raise ValueError('kanban_skills_unavailable')" not in source
