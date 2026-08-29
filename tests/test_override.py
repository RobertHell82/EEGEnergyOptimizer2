"""Befristeter Eingriff: Pause mit Ablaufzeit und/oder Ziel-Ladestand."""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.eeg_energy_optimizer.override import (
    ART_PAUSE,
    MAX_PAUSE_SOC_PCT,
    MAX_STUNDEN,
    MIN_PAUSE_SOC_PCT,
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
    assert o.pause_soc_pct(NOW) is None
    assert o.to_dict(NOW) == {"aktiv": False}


# ---------------------------------------------------------------------------
# Pause für eine Dauer
# ---------------------------------------------------------------------------

async def test_pause_wirkt_bis_ablauf(mock_hass):
    o, store = _override(mock_hass)
    await o.async_pause(4, NOW)

    assert o.pause_bis(NOW) == NOW + timedelta(hours=4)
    assert o.pause_soc_pct(NOW) is None
    # eine Minute vor Ablauf noch aktiv, danach nicht mehr
    assert o.pause_bis(NOW + timedelta(hours=3, minutes=59)) is not None
    assert o.pause_bis(NOW + timedelta(hours=4)) is None
    # persistiert — ein Neustart mitten in der Pause darf sie nicht verlieren
    assert store.saved and store.saved[-1]["art"] == ART_PAUSE


async def test_stunden_werden_geklemmt(mock_hass):
    """Über 48 h ist keine Befristung mehr — still auf die Grenze gesetzt."""
    o, _ = _override(mock_hass)
    await o.async_pause(1000, NOW)
    assert o.aktiv(NOW)["bis"] == (NOW + timedelta(hours=MAX_STUNDEN)).isoformat()


async def test_neue_pause_ersetzt_die_alte(mock_hass):
    o, _ = _override(mock_hass)
    await o.async_pause(4, NOW)
    await o.async_pause(2, NOW, bis_soc_pct=70)
    assert o.pause_bis(NOW) == NOW + timedelta(hours=2)
    assert o.pause_soc_pct(NOW) == 70.0


async def test_aufheben_leert_und_speichert(mock_hass):
    o, store = _override(mock_hass)
    await o.async_pause(4, NOW)
    await o.async_aufheben()
    assert o.aktiv(NOW) is None
    assert store.saved[-1] == {}


async def test_tick_raeumt_abgelaufenes_auch_im_store_weg(mock_hass):
    o, store = _override(mock_hass)
    await o.async_pause(1, NOW)
    await o.async_tick(NOW + timedelta(minutes=30))
    assert o.aktiv(NOW + timedelta(minutes=30)) is not None   # laeuft noch
    await o.async_tick(NOW + timedelta(hours=1, minutes=1))
    assert o.aktiv(NOW + timedelta(hours=1, minutes=1)) is None
    assert store.saved[-1] == {}


# ---------------------------------------------------------------------------
# Pause bis Ladestand
# ---------------------------------------------------------------------------

async def test_pause_bis_ladestand_endet_wenn_soc_erreicht(mock_hass):
    """Der Kernfall: 20 % in der Batterie, „lad bis 80 %" — die Pause bleibt,
    bis der gemessene Ladestand die Marke erreicht, und endet dann im Store."""
    o, store = _override(mock_hass)
    await o.async_pause(None, NOW, bis_soc_pct=80)

    assert o.pause_soc_pct(NOW) == 80.0
    # Ohne Stundenangabe gilt die Obergrenze als Sicherheitsnetz
    assert o.pause_bis(NOW) == NOW + timedelta(hours=MAX_STUNDEN)
    d = o.to_dict(NOW)
    assert d["aktiv"] and d["bis_soc_pct"] == 80.0

    t = NOW + timedelta(hours=1)
    await o.async_tick(t, soc_pct=45.0)
    assert o.aktiv(t) is not None
    await o.async_tick(t, soc_pct=79.9)
    assert o.aktiv(t) is not None
    await o.async_tick(t, soc_pct=80.0)
    assert o.aktiv(t) is None
    assert store.saved[-1] == {}


async def test_pause_bis_ladestand_ohne_messwert_zaehlt_nur_die_zeit(mock_hass):
    """Sensor nicht lesbar → kein Abbruch über den SOC, aber die Ablaufzeit
    greift trotzdem. Deshalb hat jede SOC-Pause eine Obergrenze."""
    o, _ = _override(mock_hass)
    await o.async_pause(2, NOW, bis_soc_pct=80)
    t = NOW + timedelta(hours=1)
    await o.async_tick(t, soc_pct=None)
    assert o.aktiv(t) is not None
    t2 = NOW + timedelta(hours=2, minutes=1)
    await o.async_tick(t2, soc_pct=None)
    assert o.aktiv(t2) is None


async def test_pause_bis_ladestand_mit_dauer_endet_was_zuerst_eintritt(mock_hass):
    o, _ = _override(mock_hass)
    await o.async_pause(3, NOW, bis_soc_pct=90)
    assert o.pause_bis(NOW) == NOW + timedelta(hours=3)
    assert o.pause_soc_pct(NOW) == 90.0
    # Zeit ist um, obwohl der SOC noch nicht erreicht ist
    t = NOW + timedelta(hours=3)
    await o.async_tick(t, soc_pct=60.0)
    assert o.aktiv(t) is None


async def test_ziel_ladestand_wird_auf_50_bis_100_geklemmt(mock_hass):
    o, _ = _override(mock_hass)
    await o.async_pause(None, NOW, bis_soc_pct=20)
    assert o.pause_soc_pct(NOW) == MIN_PAUSE_SOC_PCT
    await o.async_pause(None, NOW, bis_soc_pct=140)
    assert o.pause_soc_pct(NOW) == MAX_PAUSE_SOC_PCT


# ---------------------------------------------------------------------------
# Neustart
# ---------------------------------------------------------------------------

async def test_neustart_mitten_in_der_pause(mock_hass):
    """Der Kernfall: Store hat eine laufende Pause, die neue Instanz muss sie
    kennen — sonst wirft der Neustart die Steuerung wieder an."""
    gespeichert = {
        "art": ART_PAUSE,
        "bis": (NOW + timedelta(hours=2)).isoformat(),
        "bis_soc_pct": None,
        "gesetzt_am": NOW.isoformat(),
        "quelle": "panel",
    }
    o, _ = _override(mock_hass, gespeichert)
    await o.async_load()
    assert o.pause_bis(NOW + timedelta(minutes=30)) is not None


async def test_neustart_mitten_in_der_soc_pause_endet_korrekt(mock_hass):
    """Nach dem Neustart läuft die SOC-Pause weiter und endet, sobald der
    erste Guard-Lauf den Ziel-Ladestand sieht — nicht früher, nicht nie."""
    gespeichert = {
        "art": ART_PAUSE,
        "bis": (NOW + timedelta(hours=MAX_STUNDEN)).isoformat(),
        "bis_soc_pct": 80.0,
        "gesetzt_am": NOW.isoformat(),
        "quelle": "service",
    }
    o, store = _override(mock_hass, gespeichert)
    await o.async_load()
    t = NOW + timedelta(minutes=30)
    assert o.pause_soc_pct(t) == 80.0
    await o.async_tick(t, soc_pct=62.0)
    assert o.aktiv(t) is not None
    await o.async_tick(t + timedelta(minutes=30), soc_pct=81.0)
    assert o.aktiv(t + timedelta(minutes=30)) is None
    assert store.saved[-1] == {}


async def test_alte_reserve_im_store_wird_verworfen(mock_hass):
    """Bis 2.0.3-devfronius.4 gab es eine „Reserve" — ein solcher Eintrag
    darf nach dem Update nicht als Pause wiederauferstehen."""
    gespeichert = {
        "art": "reserve",
        "bis": (NOW + timedelta(hours=6)).isoformat(),
        "min_soc_pct": 60.0,
        "gesetzt_am": NOW.isoformat(),
        "quelle": "panel",
    }
    o, _ = _override(mock_hass, gespeichert)
    await o.async_load()
    assert o.aktiv(NOW) is None


async def test_kaputter_store_kippt_das_laden_nicht(mock_hass):
    o, _ = _override(mock_hass, gespeichert="unsinn")
    await o.async_load()
    assert o.aktiv(NOW) is None
