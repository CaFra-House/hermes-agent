#!/usr/bin/env python3
"""Tests for CAFRA subagent external memory inheritance (Opción B) and 4x2 hierarchy caps."""

from __future__ import annotations

import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.memory_manager import MemoryManager
from agent.memory_manager_tools import inject_memory_provider_tools, memory_provider_tools_exposed
from agent.memory_provider import MemoryProvider
from tools.delegate_tool import delegate_task
from tools.delegate_tool_child_run import _ChildRun
from tools.delegate_tool_config import (
    _get_max_async_children,
    _get_max_concurrent_children_for_depth,
    _get_max_concurrent_grandchildren,
)
from tools.delegate_tool_progress import (
    _build_child_system_prompt,
    _extract_brief_project_and_path,
    _format_project_ownership_block,
)


class _DummyExternalProvider(MemoryProvider):
    """Minimal external provider double mimicking Hindsight >= f14cc1eb."""

    def __init__(self) -> None:
        self.init_kwargs: dict = {}
        self.prefetches: list[str] = []
        self.turn_starts: list[tuple[int, str]] = []
        self.synced: list[tuple[str, str]] = []
        self.session_ends: list[list] = []
        self.shutdown_called = False
        self.auto_retain = False
        self._recall_sync = False

    @property
    def name(self) -> str:
        return "hindsight"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self.init_kwargs = dict(kwargs)
        ctx = str(kwargs.get("agent_context") or "primary")
        self.auto_retain = ctx not in ("subagent", "kanban", "cron")

    def get_tool_schemas(self) -> list[dict]:
        return [
            {"name": "hindsight_recall", "description": "recall", "parameters": {"type": "object", "properties": {}}},
            {"name": "hindsight_retain", "description": "retain", "parameters": {"type": "object", "properties": {}}},
            {"name": "hindsight_reflect", "description": "reflect", "parameters": {"type": "object", "properties": {}}},
        ]

    def system_prompt_block(self) -> str:
        return "Use hindsight_recall and hindsight_retain."

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self.prefetches.append(query)
        return f"Recalled for: {query}"

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        self.turn_starts.append((turn_number, message))

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        if self.auto_retain:
            self.synced.append((user_content, assistant_content))

    def on_session_end(self, messages: list) -> None:
        self.session_ends.append(list(messages))

    def shutdown(self) -> None:
        self.shutdown_called = True


class TestSubagentMemoryHooksBloqueoA(unittest.TestCase):
    def test_subagent_with_allow_fork_hooks_enables_all_hooks_and_provider_auto_retain(self):
        mgr = MemoryManager()
        prov = _DummyExternalProvider()
        mgr.add_provider(prov)
        mgr.initialize_all(session_id="sub-1", agent_context="subagent", allow_fork_hooks=True)

        self.assertFalse(mgr._fork_turn_hooks_suppressed())
        self.assertEqual(prov.init_kwargs.get("agent_context"), "primary")
        self.assertNotIn("allow_fork_hooks", prov.init_kwargs)
        self.assertTrue(prov.auto_retain)
        self.assertTrue(prov._recall_sync)

        recalled = mgr.prefetch_all("check project status", session_id="sub-1")
        self.assertIn("check project status", recalled)
        mgr.on_turn_start(1, "check project status")
        mgr.sync_all("check project status", "done and verified", session_id="sub-1")
        self.assertTrue(mgr.flush_pending(timeout=5.0))
        mgr.on_session_end([{"role": "user", "content": "hi"}])
        mgr.shutdown_all()

        self.assertEqual(len(prov.prefetches), 1)
        self.assertEqual(len(prov.turn_starts), 1)
        self.assertEqual(prov.synced, [("check project status", "done and verified")])
        self.assertEqual(len(prov.session_ends), 1)
        self.assertTrue(prov.shutdown_called)

    def test_subagent_without_allow_fork_hooks_preserves_upstream_suppression(self):
        mgr = MemoryManager()
        prov = _DummyExternalProvider()
        mgr.add_provider(prov)
        mgr.initialize_all(session_id="sub-2", agent_context="subagent")

        self.assertTrue(mgr._fork_turn_hooks_suppressed())
        self.assertEqual(prov.init_kwargs.get("agent_context"), "subagent")
        self.assertFalse(prov.auto_retain)
        self.assertFalse(prov._recall_sync)
        self.assertEqual(mgr.prefetch_all("hello", session_id="sub-2"), "")
        mgr.sync_all("hello", "world", session_id="sub-2")
        mgr.flush_pending(timeout=2.0)
        mgr.on_session_end([{"role": "user", "content": "hello"}])
        self.assertEqual(prov.synced, [])
        self.assertEqual(prov.session_ends, [])
        mgr.shutdown_all()


class TestSubagentMemoryToolsBloqueoB(unittest.TestCase):
    def test_inherit_memory_hooks_exposes_external_tools_while_builtin_memory_blocked(self):
        mgr = MemoryManager()
        prov = _DummyExternalProvider()
        mgr.add_provider(prov)

        agent = SimpleNamespace(
            _memory_manager=mgr,
            tools=[],
            valid_tool_names=set(),
            enabled_toolsets=["terminal", "file", "web"],
            disabled_toolsets=["clarify", "cronjob", "memory", "send_message"],
            inherit_memory_hooks=True,
        )

        self.assertTrue(memory_provider_tools_exposed(agent))
        added = inject_memory_provider_tools(agent)
        self.assertEqual(added, 3)
        self.assertEqual(agent.valid_tool_names, {"hindsight_recall", "hindsight_retain", "hindsight_reflect"})
        self.assertNotIn("memory", agent.valid_tool_names)

    def test_wildcard_disable_still_blocks_external_tools(self):
        agent = SimpleNamespace(
            enabled_toolsets=["terminal"],
            disabled_toolsets=["all"],
            inherit_memory_hooks=True,
            tools=[],
        )
        self.assertFalse(memory_provider_tools_exposed(agent))

    def test_without_inherit_memory_hooks_disabled_memory_still_blocks_external_tools(self):
        agent = SimpleNamespace(
            enabled_toolsets=["terminal"],
            disabled_toolsets=["memory"],
            inherit_memory_hooks=False,
            tools=[],
        )
        self.assertFalse(memory_provider_tools_exposed(agent))


class TestChildCloseProfileScopeBloqueoC(unittest.TestCase):
    def test_close_resources_wraps_child_close_in_child_profile_scope(self):
        entered: list[str] = []

        class _FakeScope:
            def __enter__(self):
                entered.append("enter")

            def __exit__(self, *args):
                entered.append("exit")

        child = MagicMock()
        child.session_id = "sub-scope-1"
        child.close.side_effect = lambda: entered.append("close")
        run = _ChildRun(child=child, parent_agent=MagicMock(), task_index=0, goal="g", subagent_id="s1", child_progress_cb=None)

        with patch("tools.delegate_tool_routes.child_profile_scope", return_value=_FakeScope()) as mock_scope:
            run.close_resources()

        mock_scope.assert_called_once_with(child)
        self.assertEqual(entered, ["enter", "close", "exit"])


class TestHierarchy4x2Concurrency(unittest.TestCase):
    def test_depth_0_gets_4_and_depth_1_gets_2(self):
        cfg = {"max_concurrent_children": 4, "max_concurrent_grandchildren": 2, "max_spawn_depth": 2}
        with patch("tools.delegate_tool_config._load_config", return_value=cfg):
            self.assertEqual(_get_max_concurrent_grandchildren(), 2)
            self.assertEqual(_get_max_concurrent_children_for_depth(0), 4)
            self.assertEqual(_get_max_concurrent_children_for_depth(1), 2)
            self.assertEqual(_get_max_async_children(0), 4)
            self.assertEqual(_get_max_async_children(1), 2)

    def test_depth_1_rejects_3_grandchildren_batch(self):
        parent = MagicMock()
        parent._delegate_depth = 1
        parent._active_children = []
        parent._active_children_lock = threading.Lock()
        parent._session_db = None
        parent.platform = "cli"
        parent.provider = "openrouter"
        parent.base_url = "https://openrouter.ai/api/v1"
        parent.api_key = "test-key"
        parent.model = "test-model"

        cfg = {"max_concurrent_children": 4, "max_concurrent_grandchildren": 2, "max_spawn_depth": 2}
        tasks = [
            {"goal": "Audit module alpha end to end for safety"},
            {"goal": "Audit module beta end to end for safety"},
            {"goal": "Audit module gamma end to end for safety"},
        ]
        with patch("tools.delegate_tool_config._load_config", return_value=cfg):
            res = json.loads(delegate_task(tasks=tasks, parent_agent=parent))
        self.assertIn("error", res)
        self.assertIn("Too many tasks: 3 provided", res["error"])
        self.assertIn("2", res["error"])


class TestProjectBriefAndOwnershipAutoInjection(unittest.TestCase):
    def test_extracts_linux_and_windows_project_and_ruta(self):
        p1, r1 = _extract_brief_project_and_path(
            "PROYECTO: ADEN_CRM | RUTA: `/home/ubuntu/WORKSPACE/PROJECTS/ADEN/CRM/ADEN_CRM-PROD`\nRevisar API",
            None,
        )
        self.assertEqual(p1, "ADEN_CRM")
        self.assertEqual(r1, "/home/ubuntu/WORKSPACE/PROJECTS/ADEN/CRM/ADEN_CRM-PROD")

        p2, r2 = _extract_brief_project_and_path(
            "Segmentar caso",
            "PROYECTO: ADEN_DENTAL | RUTA: C:\\WORKSPACE\\PROJECTS\\ADEN_DENTAL",
        )
        self.assertEqual(p2, "ADEN_DENTAL")
        self.assertEqual(r2, r"C:\WORKSPACE\PROJECTS\ADEN_DENTAL")

    def test_formats_compact_ownership_block_and_embeds_in_prompt(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            docs = Path(td) / "docs"
            docs.mkdir(parents=True)
            (docs / "OWNERSHIP.yaml").write_text(
                "repos:\n"
                "  ADEN_CRM:\n"
                "    clase: P\n"
                "    estado: vivo\n"
                "    owner: aden\n"
                "    rama_integracion: dev\n"
                "    rama_produccion: main\n"
                "    ruta_local: /home/ubuntu/WORKSPACE/PROJECTS/ADEN/CRM/ADEN_CRM-PROD\n"
                "    code_owners: [dev-backend, dev-frontend]\n"
                "    reviewers: [qa]\n"
                "    rutas_criticas: [src/backend/billing/, docker-compose.prod.yml]\n",
                encoding="utf-8",
            )
            block = _format_project_ownership_block("ADEN_CRM", None, home=Path(td))
            self.assertIn("PROJECT OWNERSHIP (docs/OWNERSHIP.yaml — ADEN_CRM):", block)
            self.assertIn("Owner: aden | Class: P (vivo)", block)
            self.assertIn("integration=dev, production=main", block)
            self.assertIn("src/backend/billing/", block)

            prompt = _build_child_system_prompt(project_ownership_block=block)
            self.assertIn("PROJECT OWNERSHIP (docs/OWNERSHIP.yaml — ADEN_CRM):", prompt)


if __name__ == "__main__":
    unittest.main()
