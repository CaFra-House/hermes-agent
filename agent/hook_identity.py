"""Ambient agent identity for tool hooks (platform / delegate depth / subagent id).

A ``pre_tool_call`` shell hook must be able to tell an orchestrator (depth 0) from a
delegated subagent (depth >= 1). The agent object is not in hand on every dispatch
path (execute_code kernel RPC, connector batches, the MCP bridge reach
``model_tools.handle_function_call`` with no agent), so the identity is remembered in
a ContextVar at the points where the agent IS known — ``tool_hook_ids`` and
``_pre_tool_block_message`` — and read back when serializing the hook payload.

CAFRA 2026-11-19 (orchestrator-guard): without this the payload carries no way to
distinguish a parent from its child, and a policy hook cannot enforce the
orchestrator rule.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

_IDENTITY: ContextVar[dict[str, Any] | None] = ContextVar("hermes_hook_agent_identity", default=None)


def _coerce_depth(value: Any) -> int:
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return max(value, 0)
        text = str(value).strip()
        return max(int(text), 0) if text.lstrip("-").isdigit() else 0
    except Exception:
        return 0


def agent_identity(agent: Any) -> dict[str, Any]:
    """``{platform, delegate_depth, subagent_id}`` for *agent* — never raises."""
    try:
        platform = str(getattr(agent, "platform", "") or "")
    except Exception:
        platform = ""
    try:
        subagent_id = getattr(agent, "_subagent_id", None)
        subagent_id = str(subagent_id) if subagent_id else ""
    except Exception:
        subagent_id = ""
    # Strings on purpose: the hook wire contract is ``dict[str, str]`` (every other
    # identity kwarg is a string); payload readers coerce the depth back to int.
    return {
        "platform": platform,
        "delegate_depth": str(_coerce_depth(getattr(agent, "_delegate_depth", 0))),
        "subagent_id": subagent_id,
    }


def remember_identity(agent: Any) -> dict[str, Any]:
    """Bind *agent*'s identity to this context and return it for the hook kwargs."""
    identity = agent_identity(agent)
    try:
        _IDENTITY.set(identity)
    except Exception:
        pass
    return identity


def current_identity() -> dict[str, Any]:
    """Identity remembered for the current context (empty dict when unknown)."""
    try:
        return dict(_IDENTITY.get() or {})
    except Exception:
        return {}
