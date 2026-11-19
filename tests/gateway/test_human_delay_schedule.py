"""``human_delay.schedule``: per-profile hourly windows for the human pacing delay.

TDD for OLEADA 2 of the ENUA plan (2026-11-15): 2-3 minutes during the workday, 5-6 minutes at
night, configurable per profile. A profile without ``schedule`` keeps the static range byte for
byte, so the other profiles on this build are untouched.
"""
import datetime as _dt

from gateway.platforms.base import BasePlatformAdapter
from gateway.run_config_loaders import GatewayConfigLoadersMixin

# The loader is a staticmethod on the runner mixin (never a module-level function), so bind it here
# instead of importing a name the module does not export.
_human_delay_from_config = GatewayConfigLoadersMixin._human_delay_from_config


class _ConcreteAdapter(BasePlatformAdapter):
    """Minimal concrete subclass: ``BasePlatformAdapter`` is an ABC (``connect``/``disconnect``/
    ``send``/``get_chat_info`` abstract), so it cannot be built with ``__new__``; the delay maths
    never touches the transport, so no ``__init__`` is needed."""

    async def connect(self, *args, **kwargs):
        raise NotImplementedError

    async def disconnect(self, *args, **kwargs):
        raise NotImplementedError

    async def send(self, *args, **kwargs):
        raise NotImplementedError

    async def get_chat_info(self, *args, **kwargs):
        raise NotImplementedError


def _adapter(range_ms, schedule=None):
    adapter = _ConcreteAdapter.__new__(_ConcreteAdapter)
    adapter._human_delay_range_ms = range_ms
    adapter._human_delay_schedule = schedule
    return adapter


def _freeze_at(monkeypatch, hour):
    frozen = type("D", (), {"now": staticmethod(lambda: _dt.datetime(2026, 11, 15, hour, 0))})
    monkeypatch.setattr("gateway.platforms.base.datetime", frozen)


def test_sin_schedule_devuelve_rango_estatico():
    cfg = {"human_delay": {"mode": "custom", "min_ms": 25000, "max_ms": 95000}}
    lo, hi, sched = _human_delay_from_config(cfg)
    assert (lo, hi) == (25000, 95000) and sched is None


def test_con_schedule_lo_parsea():
    cfg = {"human_delay": {"mode": "custom", "min_ms": 1000, "max_ms": 2000,
                           "schedule": [{"from_hour": 8, "to_hour": 20, "min_ms": 120000, "max_ms": 180000},
                                        {"from_hour": 20, "to_hour": 8, "min_ms": 300000, "max_ms": 360000}]}}
    lo, hi, sched = _human_delay_from_config(cfg)
    assert len(sched) == 2 and sched[0]["min_ms"] == 120000


def test_franja_laboral_usa_rango_diurno(monkeypatch):
    a = _adapter((25000, 95000),
                 [{"from_hour": 8, "to_hour": 20, "min_ms": 120000, "max_ms": 180000}])
    _freeze_at(monkeypatch, 10)
    d = a._get_human_delay()
    assert 120 <= d <= 180


def test_franja_nocturna_usa_rango_nocturno(monkeypatch):
    a = _adapter((25000, 95000),
                 [{"from_hour": 20, "to_hour": 8, "min_ms": 300000, "max_ms": 360000}])
    _freeze_at(monkeypatch, 23)
    d = a._get_human_delay()
    assert 300 <= d <= 360
