"""Regression: every dispatch entry point must enforce the dispatcher caps (2026-10-31).

Incident: three dispatchers raced the same ``kanban.db`` (gateway + recepcion + dashboard) and
``kanban.max_in_progress_per_profile: 1`` was violated — cto reached 3 running cards because the
dashboard ``/dispatch`` endpoint called ``dispatch_once`` WITHOUT any caps and ``run_daemon`` only
forwarded ``max_spawn``. Guards:

- ``dispatch_once`` enforces the per-profile cap within one tick AND across consecutive ticks;
- the dashboard ``/dispatch`` endpoint is preview-only: it never claims/spawns and forwards the
  caps to ``dispatch_once`` (spawning belongs to the gateway dispatcher);
- ``run_daemon`` and ``hermes kanban dispatch`` build their caps via
  ``configured_dispatch_caps`` so no entry point can silently drop one again;
- ``configured_dispatch_caps`` maps the config keys to ``dispatch_once`` kwargs.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# 1. dispatch_once honors max_in_progress_per_profile (core regression)
# ---------------------------------------------------------------------------


def test_per_profile_cap_second_tick_does_not_start(kanban_home, all_assignees_spawnable):
    """With cap=1, two ready cards of one profile must run one at a time.

    The incident shape: two dispatch ticks seconds apart both started cto work
    while cto already had a running card.
    """
    with kbc.connect() as conn:
        first = kb.create_task(conn, title="cap one", assignee="cto")
        second = kb.create_task(conn, title="cap two", assignee="cto")
        runs: list[str] = []

        def fake_spawn(task, workspace, board=None):
            runs.append(task.id)
            return 4242

        res1 = kbd.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress_per_profile=1)
        res2 = kbd.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress_per_profile=1)

    assert len(res1.spawned) == 1
    assert res2.spawned == []
    assert len(runs) == 1
    assert runs[0] == res1.spawned[0][0]
    assert runs[0] in (first, second)
    with kbc.connect() as conn:
        statuses = {
            first: kb.get_task(conn, first).status,
            second: kb.get_task(conn, second).status,
        }
    assert sorted(statuses.values()) == ["ready", "running"]


def test_per_profile_cap_within_single_tick(kanban_home, all_assignees_spawnable):
    """One tick with 3 ready cards and cap=1 spawns exactly 1 — the tick-local
    running counter must update as each card claims."""
    with kbc.connect() as conn:
        for i in range(3):
            kb.create_task(conn, title=f"tick cap {i}", assignee="cto")
        runs: list[str] = []

        def fake_spawn(task, workspace, board=None):
            runs.append(task.id)
            return 4242

        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress_per_profile=1)

    assert len(res.spawned) == 1
    assert len(runs) == 1
    assert len(res.skipped_per_profile_capped) == 2


def test_per_profile_cap_counts_existing_running(kanban_home, all_assignees_spawnable):
    """A card already running blocks new spawns for that profile at cap=1."""
    with kbc.connect() as conn:
        busy = kb.create_task(conn, title="already busy", assignee="cto")
        kb.claim_task(conn, busy)
        ready = kb.create_task(conn, title="wants to run", assignee="cto")

        def fake_spawn(task, workspace, board=None):
            raise AssertionError("spawn_fn must not be called at the cap")

        res = kbd.dispatch_once(conn, spawn_fn=fake_spawn, max_in_progress_per_profile=1)

    assert res.spawned == []
    assert res.skipped_per_profile_capped
    assert res.skipped_per_profile_capped[0][0] == ready


# ---------------------------------------------------------------------------
# 2. configured_dispatch_caps: one source of truth for config -> kwargs
# ---------------------------------------------------------------------------


def test_configured_dispatch_caps_maps_config_keys(monkeypatch):
    import hermes_cli.config as cfgmod

    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda: {
            "kanban": {
                "max_in_progress": 5,
                "max_in_progress_per_profile": "2",
                "failure_limit": 0,
                "dispatch_stale_timeout_seconds": 1800,
                "default_assignee": " default ",
                "reconcile_orphans": False,
            }
        },
    )
    caps = kbd.configured_dispatch_caps()
    assert caps["max_in_progress"] == 5
    assert caps["max_in_progress_per_profile"] == 2
    assert caps["failure_limit"] == kbd.DEFAULT_FAILURE_LIMIT  # 0 is invalid
    assert caps["stale_timeout_seconds"] == 1800
    assert caps["default_assignee"] == "default"
    assert caps["reconcile_orphans"] is False


def test_configured_dispatch_caps_invalid_values(monkeypatch):
    import hermes_cli.config as cfgmod

    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda: {
            "kanban": {
                "max_in_progress": 0,
                "max_in_progress_per_profile": "lots",
                "dispatch_stale_timeout_seconds": "never",
            }
        },
    )
    caps = kbd.configured_dispatch_caps()
    assert caps["max_in_progress_per_profile"] is None
    assert caps["stale_timeout_seconds"] == 0
    assert caps["default_assignee"] is None


# ---------------------------------------------------------------------------
# 3. run_daemon passes every cap (same bug class as the dashboard bypass)
# ---------------------------------------------------------------------------


def test_run_daemon_passes_every_dispatch_cap(kanban_home, monkeypatch):
    """Regression: ``run_daemon`` forwarded only ``max_spawn`` — the per-profile
    cap, failure_limit, stale timeout and default_assignee never reached
    ``dispatch_once`` from the standalone daemon."""
    import hermes_cli.config as cfgmod

    captured: dict = {}
    stop = threading.Event()

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kbd.DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    monkeypatch.setattr(kbd, "derive_default_max_in_progress", lambda sample=None: 2)
    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda: {
            "kanban": {
                "max_in_progress_per_profile": 1,
                "failure_limit": 2,
                "dispatch_stale_timeout_seconds": 1800,
                "default_assignee": "default",
            }
        },
    )

    kbd.run_daemon(interval=0.01, stop_event=stop, on_tick=lambda res: stop.set())

    assert captured.get("max_in_progress") == 2
    assert captured.get("max_in_progress_per_profile") == 1
    assert captured.get("failure_limit") == 2
    assert captured.get("stale_timeout_seconds") == 1800
    assert captured.get("default_assignee") == "default"


# ---------------------------------------------------------------------------
# 4. Dashboard /dispatch: preview-only, never claims, forwards the caps
# ---------------------------------------------------------------------------


def _load_dashboard_plugin_api():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_caps_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _dispatch_client(mod):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app = fastapi.FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    return TestClient(app)


def test_dashboard_dispatch_is_preview_only_and_never_claims(
    kanban_home, all_assignees_spawnable,
):
    """Even with dry_run=false the dashboard must not claim or spawn: claims
    belong to the gateway dispatcher (singleton lock kanban/.dispatcher.lock)."""
    client = _dispatch_client(_load_dashboard_plugin_api())
    with kbc.connect() as conn:
        task = kb.create_task(conn, title="dash cap", assignee="cto")

    r = client.post("/api/plugins/kanban/dispatch", params={"dry_run": False, "max": 8})
    assert r.status_code == 200, r.text
    payload = r.json()
    # Dry-run preview: reports what the gateway tick WOULD spawn, claims nothing.
    assert [entry[0] for entry in payload["spawned"]] == [task]
    assert payload["dispatch_owner"] == "gateway"
    assert payload["spawned_by_dashboard"] is False
    assert payload["dry_run"] is True

    with kbc.connect() as conn:
        assert kb.get_task(conn, task).status == "ready"
        kinds = [e.kind for e in kb.list_events(conn, task)]
    assert "claimed" not in kinds
    assert "spawned" not in kinds


def test_dashboard_dispatch_preview_reports_per_profile_cap(
    kanban_home, all_assignees_spawnable, monkeypatch,
):
    """The preview must run with the SAME caps the gateway resolves — the bypass
    that caused the incident (cto at 3 running with the cap set to 1)."""
    import hermes_cli.config as cfgmod

    monkeypatch.setattr(
        cfgmod, "load_config",
        lambda: {"kanban": {"max_in_progress_per_profile": 1}},
    )
    client = _dispatch_client(_load_dashboard_plugin_api())
    with kbc.connect() as conn:
        busy = kb.create_task(conn, title="dash busy", assignee="cto")
        ready = kb.create_task(conn, title="dash ready", assignee="cto")
        assert kb.claim_task(conn, busy) is not None

    r = client.post("/api/plugins/kanban/dispatch", params={"max": 8})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["spawned"] == []
    assert payload["caps"]["max_in_progress_per_profile"] == 1
    assert ready in [entry[0] for entry in payload["skipped_per_profile_capped"]]
