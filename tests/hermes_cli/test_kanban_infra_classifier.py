"""Ficha t_e6023e26 (D1): _is_infrastructure_loss must book every real owner-loss literal
as INFRASTRUCTURE (rc=69, requeue without spending the card's failure budget).

The literals are the ones actually produced on the live paths (verified in source):
  * "Gateway disconnected; turn outcome is unknown." — hermes_cli/gateway_connection.py
    (the only string the pre-patch dispatcher marker-scan matched)
  * "Gateway connection failed; no local fallback"  — gateway_client.py:310 (OSError wrap)
  * "Gateway connection timed out; no local fallback" — gateway_connection.py:187/195/203
  * "Kanban execution unknown; retained by owner"   — kanban_worker_client._submit_claim,
    EXCLUDED on purpose: the owner may still be running that attempt (owner_reclaim_paused);
    requeuing would double-run the card.
"""

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.gateway_client import GatewayClientError, GatewayUnavailableError
from hermes_cli.kanban_worker_client import _is_infrastructure_loss

# The four GatewayClientError literals + the owner-refusal exclusion, per the card.
_LOSS_LITERALS = (
    "Gateway disconnected; turn outcome is unknown.",
    "Gateway connection failed; no local fallback",
    "Gateway connection timed out; no local fallback",
)


@pytest.mark.parametrize("literal", _LOSS_LITERALS)
def test_owner_transport_losses_are_infrastructure(literal):
    assert _is_infrastructure_loss(GatewayClientError(literal))


def test_owner_refusal_unknown_is_not_infrastructure():
    # Never move "retained by owner" into KANBAN_OWNER_STALL_MARKERS: double-run risk.
    assert not _is_infrastructure_loss(
        GatewayClientError("Kanban execution unknown; retained by owner")
    )


def test_misconfigured_remote_owner_is_not_infrastructure():
    assert not _is_infrastructure_loss(
        GatewayClientError("Kanban requires the assigned local profile owner")
    )


def test_unavailable_transient_owner_is_infrastructure():
    # Past OWNER_WAIT_S the raise stands and main() must book rc=69, not rc=1.
    assert _is_infrastructure_loss(
        GatewayUnavailableError("owner still draining", state="draining")
    )


def test_classifier_and_sweep_marker_lists_stay_in_sync():
    # kanban_db_dispatch._classify_dead_worker scans (KANBAN_GATEWAY_DISCONNECT_MARKER,)
    # + KANBAN_OWNER_STALL_MARKERS over the worker log; every literal the client classifies
    # must contain at least one of those markers, else a SIGTERMed submitter (no exit
    # trailer) would be booked crashed by the sweep while main() called it infrastructure.
    markers = (kb.KANBAN_GATEWAY_DISCONNECT_MARKER,) + kb.KANBAN_OWNER_STALL_MARKERS
    for literal in _LOSS_LITERALS:
        assert any(m in literal for m in markers), literal
    assert "retained by owner" not in " ".join(markers)
