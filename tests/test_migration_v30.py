"""Tests für die v30-Migration: leere Solcast-Sensorfelder einmal füllen.

Seit 2.1.69 liest der Code nur noch die eingetragenen Prognose-Sensoren.
Grünbach und Traun steuern mit der eigenen Berechnung, der Wechsel dorthin
hatte alle Solcast-Felder geleert — der Prognosevergleich hätte nichts mehr.
"""

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.eeg_energy_optimizer import schedule as sched


def _eintrag(entity_id, unique_id, platform="solcast_solar", entry="solcast1"):
    return SimpleNamespace(
        entity_id=entity_id, unique_id=unique_id, platform=platform, config_entry_id=entry
    )


GRUENBACH = [
    _eintrag("sensor.solcast_pv_forecast_prognose_verbleibende_leistung_heute", "get_remaining_today"),
    _eintrag("sensor.solcast_pv_forecast_prognose_morgen", "total_kwh_forecast_tomorrow"),
    _eintrag("sensor.solcast_pv_forecast_prognose_heute", "total_kwh_forecast_today"),
    *[_eintrag(f"sensor.solcast_pv_forecast_prognose_tag_{n}", f"total_kwh_forecast_d{n}") for n in range(3, 8)],
    # gespiegelt aus Traun — nie
    _eintrag("sensor.traun_solcast_pv_forecast_prognose_heute", None, "remote_homeassistant", None),
]


def test_registry_liefert_alle_acht_ohne_spiegel():
    gefunden = sched.solcast_sensoren_aus_registry(GRUENBACH)
    assert gefunden["forecast_today_entity"] == "sensor.solcast_pv_forecast_prognose_heute"
    assert gefunden["forecast_day7_entity"] == "sensor.solcast_pv_forecast_prognose_tag_7"
    assert len(gefunden) == 8
    assert all("traun" not in v for v in gefunden.values())


def test_englische_namen_ueber_die_unique_id():
    eintraege = [_eintrag("sensor.solcast_pv_forecast_forecast_today", "total_kwh_forecast_today")]
    assert sched.solcast_sensoren_aus_registry(eintraege) == {
        "forecast_today_entity": "sensor.solcast_pv_forecast_forecast_today"
    }


def test_zwei_solcast_integrationen_wird_nicht_geraten():
    eintraege = GRUENBACH + [_eintrag("sensor.zweite_heute", "total_kwh_forecast_today", entry="solcast2")]
    assert sched.solcast_sensoren_aus_registry(eintraege) == {}


async def _migriere(monkeypatch, data: dict, eintraege=GRUENBACH) -> dict:
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    registry = SimpleNamespace(entities={e.entity_id: e for e in eintraege})
    modul = types.ModuleType("homeassistant.helpers.entity_registry")
    modul.async_get = lambda hass: registry
    for name in ("homeassistant", "homeassistant.helpers"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setattr(sys.modules["homeassistant.helpers"], "entity_registry", modul, raising=False)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", modul)

    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    entry = MagicMock()
    entry.version = 29
    entry.data = data
    await async_migrate_entry(hass, entry)
    aufruf = next(
        c for c in hass.config_entries.async_update_entry.call_args_list
        if c.kwargs.get("version") == 30
    )
    return aufruf.kwargs["data"]


@pytest.mark.asyncio
async def test_eigene_berechnung_bekommt_die_solcast_sensoren(monkeypatch):
    daten = {"forecast_source": "eigen", "pv_prognose_vergleich": True,
             **{k: "" for k in sched.PROGNOSE_SENSOR_SCHLUESSEL}}
    neu = await _migriere(monkeypatch, daten)
    assert neu["forecast_tomorrow_entity"] == "sensor.solcast_pv_forecast_prognose_morgen"
    assert neu["forecast_today_entity"] == "sensor.solcast_pv_forecast_prognose_heute"


@pytest.mark.asyncio
async def test_eingetragene_bleiben_stehen(monkeypatch):
    daten = {"forecast_source": "solcast_solar",
             "forecast_tomorrow_entity": "sensor.von_hand", "forecast_today_entity": ""}
    neu = await _migriere(monkeypatch, daten)
    assert neu["forecast_tomorrow_entity"] == "sensor.von_hand"
    assert neu["forecast_today_entity"] == "sensor.solcast_pv_forecast_prognose_heute"


@pytest.mark.asyncio
async def test_forecast_solar_bleibt_unberuehrt(monkeypatch):
    daten = {"forecast_source": "forecast_solar",
             "forecast_tomorrow_entity": "sensor.energy_production_tomorrow"}
    neu = await _migriere(monkeypatch, dict(daten))
    assert neu == daten
