"""Inbound duplicate guard: one WhatsApp message must never run two agent turns.

Two independent layers, both keyed per chat:

1. **Content** (``_inbound_duplicate_reason`` → ``"text"``): WhatsApp retransmits the same customer
   send ~2 s later under a DIFFERENT message id and a different native type (``conversation`` vs
   ``extendedTextMessage``).  An id-keyed guard never sees that pair, so the same normalized text
   inside a 6 s window is treated as one message.  Media and empty bodies are never keyed.
2. **Id** (``"id"``): the same ``(chatId, messageId)`` — Baileys' 'append' + 'notify' duplicate, or a
   bridge that predates its own id guard.

The bridge applies the same two guards before queueing (``scripts/whatsapp-bridge/bridge.js``);
this is the defensive layer for an older bridge or a retransmission that lands after a restart.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.event import MessageType
from plugins.platforms.whatsapp import adapter as adapter_module
from tests.gateway.test_whatsapp_formatting import _AsyncCM, _make_adapter

CHAT = "5491137928875@s.whatsapp.net"
OTHER_CHAT = "573185702102@s.whatsapp.net"
MESSAGE_ID = "AC9E100A40274AEB0BFFE15207A09993"
# The exact text of the 17:08 incident: 29 chars, sent twice 2.0 s apart.
TEXT = "¡Hola! Quiero más información"


def _dedupe_adapter():
    adapter = _make_adapter()
    adapter._recent_inbound_keys = {}
    adapter._recent_inbound_texts = {}
    adapter._send_read_receipts = False
    return adapter


def _freeze_clock(monkeypatch, start=1000.0):
    """Patch the adapter's monotonic clock; returns a dict to advance it."""
    clock = {"now": start}
    monkeypatch.setattr(adapter_module.time, "monotonic", lambda: clock["now"])
    return clock


# ── content guard ────────────────────────────────────────────────────────────────────────────────


def test_same_text_with_a_different_id_and_native_type_is_dropped():
    """The real incident: 'conversation' then 'extendedTextMessage', different ids, 2 s apart."""
    adapter = _dedupe_adapter()
    first = {"chatId": CHAT, "messageId": MESSAGE_ID, "nativeType": "extendedTextMessage", "body": TEXT}
    second = {"chatId": CHAT, "messageId": "AC48A2781E29B7A36870A57EAC6E2B4E", "nativeType": "conversation", "body": TEXT}

    assert adapter._inbound_duplicate_reason(first) is None
    assert adapter._inbound_duplicate_reason(second) == "text"
    assert adapter._is_duplicate_inbound(second) is True


def test_normalization_ignores_leading_trailing_and_repeated_whitespace():
    adapter = _dedupe_adapter()

    assert adapter._is_duplicate_inbound({"chatId": CHAT, "messageId": "AAA", "body": TEXT}) is False
    padded = {"chatId": CHAT, "messageId": "BBB", "body": "  ¡Hola!   Quiero\nmás   información \n"}
    assert adapter._inbound_duplicate_reason(padded) == "text"


def test_case_is_not_folded_into_a_duplicate():
    """'Hola' and 'hola' are different customer messages, not a retransmission."""
    adapter = _dedupe_adapter()

    assert adapter._is_duplicate_inbound({"chatId": CHAT, "messageId": "AAA", "body": "Hola"}) is False
    assert adapter._is_duplicate_inbound({"chatId": CHAT, "messageId": "BBB", "body": "hola"}) is False


def test_same_text_outside_the_window_passes(monkeypatch):
    clock = _freeze_clock(monkeypatch)
    adapter = _dedupe_adapter()
    first = {"chatId": CHAT, "messageId": "AAA", "body": TEXT}

    assert adapter._is_duplicate_inbound(first) is False
    clock["now"] += 6.01  # window is 6.0 s
    assert adapter._is_duplicate_inbound({"chatId": CHAT, "messageId": "BBB", "body": TEXT}) is False


def test_same_text_inside_the_window_is_dropped(monkeypatch):
    clock = _freeze_clock(monkeypatch)
    adapter = _dedupe_adapter()

    assert adapter._is_duplicate_inbound({"chatId": CHAT, "messageId": "AAA", "body": TEXT}) is False
    clock["now"] += 2.0  # the observed retransmission delay
    assert adapter._inbound_duplicate_reason({"chatId": CHAT, "messageId": "BBB", "body": TEXT}) == "text"


def test_repeat_does_not_extend_its_own_window(monkeypatch):
    clock = _freeze_clock(monkeypatch)
    adapter = _dedupe_adapter()
    data = {"chatId": CHAT, "messageId": "AAA", "body": TEXT}

    assert adapter._is_duplicate_inbound(data) is False
    clock["now"] += 4.0
    assert adapter._is_duplicate_inbound({"chatId": CHAT, "messageId": "BBB", "body": TEXT}) is True
    clock["now"] += 2.01  # 6.01 s after the FIRST sighting, 2.01 s after the repeat
    assert adapter._is_duplicate_inbound({"chatId": CHAT, "messageId": "CCC", "body": TEXT}) is False


def test_same_text_in_another_chat_is_never_dropped():
    adapter = _dedupe_adapter()

    assert adapter._is_duplicate_inbound({"chatId": CHAT, "messageId": "AAA", "body": TEXT}) is False
    assert adapter._is_duplicate_inbound({"chatId": OTHER_CHAT, "messageId": "BBB", "body": TEXT}) is False


def test_media_is_never_deduplicated_by_text():
    adapter = _dedupe_adapter()
    cases = [
        {"chatId": CHAT, "messageId": "A1", "body": "mirá", "hasMedia": True},
        {"chatId": CHAT, "messageId": "A2", "body": "mirá", "hasMedia": True},
        {"chatId": CHAT, "messageId": "A3", "body": "", "mediaType": "image"},
        {"chatId": CHAT, "messageId": "A4", "body": "", "mediaUrls": ["/cache/image/1.jpg"]},
        {"chatId": CHAT, "messageId": "A5", "body": "", "quotedMediaUrls": ["/cache/image/2.jpg"]},
    ]

    assert adapter._inbound_text_key(cases[0]) is None
    for data in cases:
        assert adapter._is_duplicate_inbound(data) is False


def test_empty_or_blank_text_is_never_deduplicated():
    adapter = _dedupe_adapter()

    assert adapter._inbound_text_key({"chatId": CHAT, "body": ""}) is None
    assert adapter._inbound_text_key({"chatId": CHAT, "body": "   \n\t "}) is None
    assert adapter._is_duplicate_inbound({"chatId": CHAT, "messageId": "AAA", "body": "   "}) is False
    assert adapter._is_duplicate_inbound({"chatId": CHAT, "messageId": "BBB", "body": "\n"}) is False


def test_text_table_is_bounded(monkeypatch):
    monkeypatch.setattr(adapter_module, "_INBOUND_TEXT_DEDUPE_MAX", 2)
    adapter = _dedupe_adapter()

    for i in range(4):
        assert adapter._is_duplicate_inbound({"chatId": CHAT, "messageId": f"m{i}", "body": f"texto {i}"}) is False
    assert len(adapter._recent_inbound_texts) <= 2


# ── id guard (unchanged behaviour) ──────────────────────────────────────────────────────────────


def test_same_chat_and_message_id_is_dropped_after_first_dispatch():
    adapter = _dedupe_adapter()
    data = {"chatId": CHAT, "messageId": MESSAGE_ID, "body": "Hola, vengo del portal de turnos."}

    assert adapter._is_duplicate_inbound(data) is False
    assert adapter._is_duplicate_inbound(dict(data)) is True


def test_same_message_id_without_text_is_dropped_by_the_id_guard():
    """The id guard still covers id-only events (media, status payloads, older bridges)."""
    adapter = _dedupe_adapter()
    data = {"chatId": CHAT, "messageId": MESSAGE_ID}

    assert adapter._inbound_duplicate_reason(data) is None
    assert adapter._inbound_duplicate_reason(dict(data)) == "id"


def test_same_message_id_in_another_chat_is_never_dropped():
    adapter = _dedupe_adapter()

    assert adapter._is_duplicate_inbound({"chatId": CHAT, "messageId": MESSAGE_ID}) is False
    assert adapter._is_duplicate_inbound({"chatId": OTHER_CHAT, "messageId": MESSAGE_ID}) is False


def test_events_without_a_message_id_always_pass():
    adapter = _dedupe_adapter()

    assert adapter._is_duplicate_inbound({"chatId": CHAT}) is False
    assert adapter._is_duplicate_inbound({"chatId": CHAT, "messageId": ""}) is False
    assert adapter._recent_inbound_keys == {}


def test_expired_id_entry_allows_redelivery(monkeypatch):
    monkeypatch.setattr(adapter_module, "_INBOUND_DEDUPE_TTL_S", 0.0)
    adapter = _dedupe_adapter()
    data = {"chatId": CHAT, "messageId": MESSAGE_ID}

    assert adapter._is_duplicate_inbound(data) is False
    assert adapter._is_duplicate_inbound(data) is False


def test_id_cap_evicts_the_oldest_entry(monkeypatch):
    monkeypatch.setattr(adapter_module, "_INBOUND_DEDUPE_MAX", 2)
    adapter = _dedupe_adapter()

    for message_id in ("m1", "m2", "m3"):
        assert adapter._is_duplicate_inbound({"chatId": CHAT, "messageId": message_id}) is False
    assert len(adapter._recent_inbound_keys) == 2
    assert adapter._is_duplicate_inbound({"chatId": CHAT, "messageId": "m1"}) is False  # evicted, not a dup


# ── poll loop wiring ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_drops_the_content_duplicate_before_dispatch(monkeypatch):
    """The incident pair must reach the pipeline once, even though the ids differ."""
    adapter = _dedupe_adapter()
    retransmission = [
        {"chatId": CHAT, "messageId": MESSAGE_ID, "nativeType": "extendedTextMessage", "body": TEXT},
        {"chatId": CHAT, "messageId": "AC48A2781E29B7A36870A57EAC6E2B4E", "nativeType": "conversation", "body": TEXT},
    ]
    dispatched, built = [], []

    async def fake_build(data):
        built.append(data["messageId"])
        adapter._running = False  # one batch is enough
        return MagicMock(message_type=MessageType.TEXT)

    monkeypatch.setattr(adapter, "_build_message_event", fake_build)
    monkeypatch.setattr(adapter, "_enqueue_text_event", lambda event: dispatched.append(event))
    monkeypatch.setattr(adapter, "_report_bridge_exit", AsyncMock(return_value=False))
    monkeypatch.setattr(adapter, "_bridge_req", lambda *a, **k: _AsyncCM(MagicMock(status=200, json=AsyncMock(return_value=retransmission))))

    await adapter._poll_messages()

    assert len(dispatched) == 1
    assert built == [MESSAGE_ID]


@pytest.mark.asyncio
async def test_poll_drops_the_id_duplicate_before_dispatch(monkeypatch):
    adapter = _dedupe_adapter()
    duplicate_pair = [{"chatId": CHAT, "messageId": MESSAGE_ID, "body": "Hola"}] * 2
    dispatched = []

    async def fake_build(_data):
        adapter._running = False  # one batch is enough
        return MagicMock(message_type=MessageType.TEXT)

    monkeypatch.setattr(adapter, "_build_message_event", fake_build)
    monkeypatch.setattr(adapter, "_enqueue_text_event", lambda event: dispatched.append(event))
    monkeypatch.setattr(adapter, "_report_bridge_exit", AsyncMock(return_value=False))
    monkeypatch.setattr(adapter, "_bridge_req", lambda *a, **k: _AsyncCM(MagicMock(status=200, json=AsyncMock(return_value=duplicate_pair))))

    await adapter._poll_messages()

    assert len(dispatched) == 1
