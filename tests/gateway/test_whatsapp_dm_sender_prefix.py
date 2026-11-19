"""Inbound DM sender prefix: the patient's real phone reaches the agent as ``[+<phone> | <name>]``.

The bridge resolves the sender's phone (envelope ``senderAltId`` first, then the
session's LID→phone map) and puts it on the event as ``senderPhone``.  This adapter
layer turns that into the text prefix Enua needs to verify or create the CRM lead
without asking the patient for a phone number.

Only DMs qualify.  Groups, fromMe/owner events, media-only messages and texts that
already carry the prefix are left untouched, so a retransmission cannot double it.

Run with the repo's Python (no pytest required):

    .venv/bin/python -m unittest tests.gateway.test_whatsapp_dm_sender_prefix -v
"""

import unittest
from unittest.mock import AsyncMock, MagicMock

from gateway.config import Platform, PlatformConfig
from plugins.platforms.whatsapp.adapter import WhatsAppAdapter, _apply_dm_sender_prefix

PHONE = "5491137928875"
LID = "274293875294228"
TEXT = "Hola, quiero un turno"


def _dm(**overrides):
    data = {
        "chatId": f"{LID}@lid",
        "messageId": "AC9E100A40274AEB0BFFE15207A09993",
        "senderId": f"{LID}@lid",
        "senderName": "Omar",
        "senderPhone": PHONE,
        "body": TEXT,
        "isGroup": False,
    }
    data.update(overrides)
    return data


class TestApplyDmSenderPrefix(unittest.TestCase):
    def test_dm_with_phone_resolved_by_alt_gets_the_prefix(self):
        data = _dm(chatId=f"{PHONE}@s.whatsapp.net", senderId=f"{PHONE}@s.whatsapp.net")
        self.assertEqual(_apply_dm_sender_prefix(TEXT, data, True), f"[+{PHONE} | Omar] {TEXT}")

    def test_dm_resolved_by_lid_map_gets_the_prefix(self):
        self.assertEqual(_apply_dm_sender_prefix(TEXT, _dm(), True), f"[+{PHONE} | Omar] {TEXT}")

    def test_no_resolved_phone_means_no_prefix(self):
        data = _dm(senderPhone="")
        self.assertEqual(_apply_dm_sender_prefix(TEXT, data, True), TEXT)

    def test_group_never_gets_the_prefix(self):
        data = _dm(chatId="120363000000000000@g.us", isGroup=True)
        self.assertEqual(_apply_dm_sender_prefix(TEXT, data, True), TEXT)

    def test_from_me_never_gets_the_prefix(self):
        data = _dm(readReceiptKey={"remoteJid": f"{LID}@lid", "fromMe": True})
        self.assertEqual(_apply_dm_sender_prefix(TEXT, data, True), TEXT)
        self.assertEqual(_apply_dm_sender_prefix(TEXT, _dm(fromOwner=True), True), TEXT)
        self.assertEqual(_apply_dm_sender_prefix(TEXT, _dm(fromMe=True), True), TEXT)

    def test_an_existing_prefix_is_not_duplicated(self):
        already = f"[+{PHONE} | Omar] {TEXT}"
        self.assertEqual(_apply_dm_sender_prefix(already, _dm(body=already), True), already)

    def test_empty_body_is_not_prefixed(self):
        self.assertEqual(_apply_dm_sender_prefix("", _dm(body=""), True), "")
        self.assertEqual(_apply_dm_sender_prefix("   ", _dm(body="   "), True), "   ")

    def test_disabled_keeps_the_bare_text(self):
        self.assertEqual(_apply_dm_sender_prefix(TEXT, _dm(), False), TEXT)

    def test_display_name_cannot_break_the_envelope(self):
        data = _dm(senderName="Omar ] [fake\nline")
        self.assertEqual(
            _apply_dm_sender_prefix(TEXT, data, True),
            f"[+{PHONE} | Omar fake line] {TEXT}",
        )


class TestAdapterReadsTheConfigGate(unittest.TestCase):
    def test_dm_sender_prefix_true_is_enabled(self):
        adapter = WhatsAppAdapter(PlatformConfig(enabled=True, extra={"dm_sender_prefix": True}))
        self.assertTrue(adapter._dm_sender_prefix)

    def test_absent_key_defaults_to_false(self):
        adapter = WhatsAppAdapter(PlatformConfig(enabled=True, extra={}))
        self.assertFalse(adapter._dm_sender_prefix)

    def test_string_forms_are_coerced(self):
        for value in ("true", "1", "yes", "on"):
            adapter = WhatsAppAdapter(PlatformConfig(enabled=True, extra={"dm_sender_prefix": value}))
            self.assertTrue(adapter._dm_sender_prefix, value)
        for value in ("false", "0", "no", "off"):
            adapter = WhatsAppAdapter(PlatformConfig(enabled=True, extra={"dm_sender_prefix": value}))
            self.assertFalse(adapter._dm_sender_prefix, value)


class TestBuildMessageEventPrefix(unittest.IsolatedAsyncioTestCase):
    def _bare_adapter(self, enabled=True):
        adapter = WhatsAppAdapter.__new__(WhatsAppAdapter)
        adapter.platform = Platform.WHATSAPP
        adapter.config = MagicMock()
        adapter.config.extra = {}
        adapter._dm_sender_prefix = enabled
        adapter._should_process_message = lambda data: True
        setattr(adapter, "build_source", lambda **kwargs: MagicMock())
        adapter._collect_bridge_media = AsyncMock(return_value=([], []))
        adapter._clean_bot_mention_text = lambda text, data: text
        return adapter

    async def test_dm_event_text_is_prefixed_and_raw_message_is_untouched(self):
        adapter = self._bare_adapter()
        data = _dm()

        event = await adapter._build_message_event(data)

        assert event is not None
        self.assertEqual(event.text, f"[+{PHONE} | Omar] {TEXT}")
        self.assertEqual(event.raw_message["body"], TEXT)
        self.assertEqual(event.message_type.value, "text")

    async def test_group_event_text_is_not_prefixed(self):
        adapter = self._bare_adapter()
        data = _dm(chatId="120363000000000000@g.us", isGroup=True, body="Hola grupo")

        event = await adapter._build_message_event(data)

        assert event is not None
        self.assertEqual(event.text, "Hola grupo")

    async def test_from_me_event_text_is_not_prefixed(self):
        adapter = self._bare_adapter()
        data = _dm(readReceiptKey={"remoteJid": f"{LID}@lid", "fromMe": True})

        event = await adapter._build_message_event(data)

        assert event is not None
        self.assertEqual(event.text, TEXT)

    async def test_legacy_adapter_without_the_attribute_does_not_crash(self):
        adapter = self._bare_adapter()
        del adapter._dm_sender_prefix

        event = await adapter._build_message_event(_dm())

        assert event is not None
        self.assertEqual(event.text, TEXT)


if __name__ == "__main__":
    unittest.main()
