"""``muted_chats.json``: el gate de takeover humano debe cubrir alias y formato.

Auditoría externa 2026-11-15 (D5): WhatsApp/Baileys entrega el mismo contacto a
veces como ``@lid`` y a veces como ``@s.whatsapp.net``. El gate compara literal,
por dígitos antes de ``@`` y contra ``aliases`` (el LID no se puede unir al
teléfono por dígitos: hace falta el alias explícito).
"""
import json
import time

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


class _ConcreteAdapter(BasePlatformAdapter):
    """``BasePlatformAdapter`` es ABC; el gate no toca el transporte."""

    async def connect(self, *args, **kwargs):
        raise NotImplementedError

    async def disconnect(self, *args, **kwargs):
        raise NotImplementedError

    async def send(self, *args, **kwargs):
        raise NotImplementedError

    async def get_chat_info(self, *args, **kwargs):
        raise NotImplementedError


def _adapter(tmp_path, monkeypatch, state):
    (tmp_path / "muted_chats.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _ConcreteAdapter.__new__(_ConcreteAdapter)
    adapter._muted_chats_cache = ({}, None)
    return adapter


def _event(chat_id, metadata=None):
    return MessageEvent(
        text="hola",
        source=SessionSource(platform=Platform.WHATSAPP, chat_id=chat_id),
        metadata=metadata or {},
    )


def _live(reason="test", **extra):
    return {"until": time.time() + 3600, "reason": reason, **extra}


def test_match_literal(tmp_path, monkeypatch):
    entry_id = "5493825669508@s.whatsapp.net"
    adapter = _adapter(tmp_path, monkeypatch, {entry_id: _live()})
    assert adapter._chat_muted_by_human(_event(entry_id)) is True


def test_match_por_alias(tmp_path, monkeypatch):
    """Un mute creado con el @lid cubre el mismo contacto cuando llega por número."""
    lid = "252758942826641@lid"
    phone = "5493825669508@s.whatsapp.net"
    adapter = _adapter(tmp_path, monkeypatch, {lid: _live(aliases=[phone])})
    assert adapter._chat_muted_by_human(_event(phone)) is True


def test_no_match(tmp_path, monkeypatch):
    adapter = _adapter(
        tmp_path,
        monkeypatch,
        {"5493825669508@s.whatsapp.net": _live()},
    )
    assert adapter._chat_muted_by_human(_event("5493825000000@s.whatsapp.net")) is False


def test_match_por_digitos_cuando_cambia_el_sufijo(tmp_path, monkeypatch):
    adapter = _adapter(
        tmp_path,
        monkeypatch,
        {"5493825669508@s.whatsapp.net": _live()},
    )
    assert adapter._chat_muted_by_human(_event("5493825669508@c.us")) is True
