"""Befristete Eingriffe: Pause und Reserve mit Ablaufzeit."""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.eeg_energy_optimizer.override import (
    ART_PAUSE,
    ART_RESERVE,
    MAX_RESERVE_PCT,
    MAX_STUNDEN,
    SteuerOverride,
)

TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 8, 29, 14, 0, tzinfo=TZ)


class _NoopStore:
    def __init__(self, vorhanden=None):
        self._data = vorhanden
        self.saved: list[dict] = []

    async def async_load(self):
        return self._data

    async def async_save(self, data):
        self._data = dict(data)
        self.saved.append(dict(data))


def _override(mock_hass, gespeichert=None) -> tuple[SteuerOverride, _NoopStore]:
    o = SteuerOverride(mock_hass, "entry1")
    store = _NoopStore(gespeichert)
    o._store = store
    return o, store


async def test_ohne_eingriff_ist_nichts_aktiv(mock_hass):
    o, _ = _override(mock_hass)
    await o.async_load()
    assert o.aktiv(NOW) is None
    assert o.pause_bis(NOW) is None
    assert o.reserve_pct(NOW) is None
    assert o.to_dict(NOW) == {"aktiv": False}


async def test_pause_wirkt_bis_ablauf(mock_hass):
    o, store = _override(mock_hass)
    await o.async_pause(4, NOW)

    assert o.pause_bis(NOW) == NOW + timedelta(hours=4)
    assert o.reserve_pct(NOW) is None
    # eine Minute vor Ablauf noch aktiv, danach nicht mehr
    assert o.pause_bis(NOW + timedelta(hours=3, minutes=59)) is not None
    assert o.pause_bis(NOW + timedelta(hours=4)) is None
    # persistiert — ein Neustart mitten in der Pause darf sie nicht verlieren
    assert store.saved and store.saved[-1]["art"] == ART_PAUSE


async def test_reserve_liefert_prozent_und_keine_pause(mock_hass):
    o, _ = _override(mock_hass)
    await o.async_reserve(60, 6, NOW)
    assert o.reserve_pct(NOW) == 60.0
    assert o.pause_bis(NOW) is None
    d = o.to_dict(NOW)
    assert d["aktiv"] and d["art"] == ART_RESERVE and d["rest_minuten"] == 360


async def test_grenzen_werden_geklemmt(mock_hass):
    """Ueber 48 h ist keine Befristung mehr, ueber 90 % modelliert der
    Fahrplan nicht — beides wird still auf die Grenze gesetzt."""
    o, _ = _override(mock_hass)
    await o.async_reserve(99, 1000, NOW)
    assert o.reserve_pct(NOW) == MAX_RESERVE_PCT
    assert o.aktiv(NOW)["bis"] == (NOW + timedelta(hours=MAX_STUNDEN)).isoformat()


async def test_neuer_eingriff_ersetzt_den_alten(mock_hass):
    o, _ = _override(mock_hass)
    await o.async_pause(4, NOW)
    await o.async_reserve(50, 2, NOW)
    assert o.pause_bis(NOW) is None
    assert o.reserve_pct(NOW) == 50.0


async def test_aufheben_leert_und_speichert(mock_hass):
    o, store = _override(mock_hass)
    await o.async_pause(4, NOW)
    await o.async_aufheben()
    assert o.aktiv(NOW) is None
    assert store.saved[-1] == {}


async def test_neustart_mitten_in_der_pause(mock_hass):
    """Der Kernfall: Store hat eine laufende Pause, die neue Instanz muss sie
    kennen — sonst wirft der Neustart die Steuerung wieder an."""
    gespeichert = {
        "art": ART_PAUSE,
        "bis": (NOW + timedelta(hours=2)).isoformat(),
        "min_soc_pct": None,
        "gesetzt_am": NOW.isoformat(),
        "quelle": "panel",
    }
    o, _ = _override(mock_hass, gespeichert)
    await o.async_load()
    assert o.pause_bis(NOW + timedelta(minutes=30)) is not None


async def test_tick_raeumt_abgelaufenes_auch_im_store_weg(mock_hass):
    o, store = _override(mock_hass)
    await o.async_pause(1, NOW)
    await o.async_tick(NOW + timedelta(minutes=30))
    assert o.aktiv(NOW + timedelta(minutes=30)) is not None   # laeuft noch
    await o.async_tick(NOW + timedelta(hours=1, minutes=1))
    assert o.aktiv(NOW + timedelta(hours=1, minutes=1)) is None
    assert store.saved[-1] == {}


async def test_kaputter_store_kippt_das_laden_nicht(mock_hass):
    o, _ = _override(mock_hass, gespeichert="unsinn")
    await o.async_load()
    assert o.aktiv(NOW) is None
