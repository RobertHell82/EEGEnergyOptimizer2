"""Tests für die Fahrplan-Sensoren.

Sie liefern die Plan-Werte des laufenden Slots in derselben
Vorzeichenkonvention wie die Ist-Sensoren, damit Plan und Ist in der
Recorder-Historie übereinandergelegt werden können.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.eeg_energy_optimizer.const import DOMAIN
from custom_components.eeg_energy_optimizer import sensor as sensor_modul
from custom_components.eeg_energy_optimizer.sensor import (
    FahrplanBatterieleistungSensor,
    FahrplanNetzleistungSensor,
    _aktueller_slot,
)

TZ = timezone(timedelta(hours=2))
JETZT = datetime(2026, 8, 24, 18, 37, tzinfo=TZ)


def _slot(minute_offset: int, **werte):
    stamp = JETZT.replace(minute=0, second=0, microsecond=0) + timedelta(
        minutes=minute_offset
    )
    basis = {
        "t": stamp.isoformat(),
        "PV": 0.5,
        "consumption": 1.2,
        "battery_p": 0.0,
        "grid_p": 0.0,
        "soc": 55.0,
        "bat_price": 0.0924,
        "feedin_price": 0.102,
    }
    basis.update(werte)
    return basis


def _hass_mit_fahrplan(zustand: dict):
    runner = MagicMock()
    runner.to_dict.return_value = zustand
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry1": {"schedule": runner}}}
    return hass


FAHRPLAN = {
    "available": True,
    "error": None,
    "last_run": "2026-08-24T18:30:11+02:00",
    "duration_ms": 42,
    "slots": [
        _slot(0, battery_p=-1.5, grid_p=2.0, soc=50.0),    # 18:00 laden
        _slot(15, battery_p=0.0, grid_p=0.4, soc=52.0),    # 18:15 halten
        _slot(30, battery_p=2.5, grid_p=-0.8, soc=48.0),   # 18:30 entladen  <- jetzt
        _slot(45, battery_p=3.0, grid_p=1.1, soc=44.0),    # 18:45 Zukunft
    ],
}

ENTRY = SimpleNamespace(entry_id="entry1")


# ---------------------------------------------------------------------------
# Slot-Auswahl
# ---------------------------------------------------------------------------


def test_slot_auswahl_nimmt_den_laufenden_slot():
    hass = _hass_mit_fahrplan(FAHRPLAN)

    with patch.object(sensor_modul, "_now_local", return_value=JETZT):
        slot, zustand = _aktueller_slot(hass, "entry1")

    assert slot is not None
    assert slot["t"][11:16] == "18:30", "18:37 muss im 18:30-Slot liegen"
    assert zustand["duration_ms"] == 42


def test_ohne_fahrplan_modul_kein_slot():
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry1": {}}}
    slot, zustand = _aktueller_slot(hass, "entry1")
    assert slot is None and zustand is None


def test_fehlerzustand_wird_durchgereicht():
    hass = _hass_mit_fahrplan(
        {"available": False, "error": "Keine PV-Prognose-Zeitreihe verfügbar"}
    )
    slot, zustand = _aktueller_slot(hass, "entry1")
    assert slot is None
    assert "PV-Prognose" in zustand["error"]


# ---------------------------------------------------------------------------
# Batterieleistung
# ---------------------------------------------------------------------------


async def test_batterieleistung_dreht_das_vorzeichen():
    """Fahrplan: positiv = entladen. Unser Ist-Sensor: positiv = laden."""
    sensor = FahrplanBatterieleistungSensor(_hass_mit_fahrplan(FAHRPLAN), ENTRY)

    with patch.object(sensor_modul, "_now_local", return_value=JETZT):
        await sensor.async_update()

    # Slot 18:30 plant battery_p = +2,5 (entladen) → Sensor zeigt -2,5
    assert sensor.native_value == pytest.approx(-2.5)

    attrs = sensor.extra_state_attributes
    assert attrs["slot"] == "18:30"
    assert attrs["ziel_soc_pct"] == 48.0
    assert attrs["netzleistung_kw"] == -0.8
    assert attrs["einspeisepreis_ct"] == pytest.approx(10.2)
    assert attrs["batteriewert_ct"] == pytest.approx(9.24)
    assert attrs["rechenzeit_ms"] == 42


async def test_laden_wird_positiv_gezeigt():
    plan = {**FAHRPLAN, "slots": [_slot(0, battery_p=-1.5)]}
    sensor = FahrplanBatterieleistungSensor(_hass_mit_fahrplan(plan), ENTRY)

    with patch.object(sensor_modul, "_now_local", return_value=JETZT):
        await sensor.async_update()

    assert sensor.native_value == pytest.approx(1.5)


async def test_batterieleistung_ohne_fahrplan_meldet_grund():
    hass = _hass_mit_fahrplan(
        {"available": False, "error": "Verbrauchsprofil noch nicht geladen"}
    )
    sensor = FahrplanBatterieleistungSensor(hass, ENTRY)

    await sensor.async_update()

    assert sensor.native_value is None
    assert "Verbrauchsprofil" in sensor.extra_state_attributes["hinweis"]


async def test_sensor_ueberlebt_fehlendes_modul():
    hass = MagicMock()
    hass.data = {DOMAIN: {"entry1": {}}}
    sensor = FahrplanBatterieleistungSensor(hass, ENTRY)

    await sensor.async_update()

    assert sensor.native_value is None
    assert sensor.extra_state_attributes["hinweis"] == "Fahrplan-Modul nicht aktiv"


# ---------------------------------------------------------------------------
# Netzleistung
# ---------------------------------------------------------------------------


async def test_netzleistung_behaelt_das_vorzeichen():
    """Beide Seiten rechnen positiv = Einspeisung, hier wird nichts gedreht."""
    sensor = FahrplanNetzleistungSensor(_hass_mit_fahrplan(FAHRPLAN), ENTRY)

    with patch.object(sensor_modul, "_now_local", return_value=JETZT):
        await sensor.async_update()

    assert sensor.native_value == pytest.approx(-0.8)   # Slot 18:30 bezieht
    assert sensor.extra_state_attributes["pv_kw"] == 0.5
    assert sensor.extra_state_attributes["verbrauch_kw"] == 1.2


async def test_beide_sensoren_haben_eigene_unique_ids():
    hass = _hass_mit_fahrplan(FAHRPLAN)
    batterie = FahrplanBatterieleistungSensor(hass, ENTRY)
    netz = FahrplanNetzleistungSensor(hass, ENTRY)

    assert batterie._attr_unique_id != netz._attr_unique_id
    assert batterie._attr_unique_id.endswith("fahrplan_batterieleistung")
    assert netz._attr_unique_id.endswith("fahrplan_netzleistung")
