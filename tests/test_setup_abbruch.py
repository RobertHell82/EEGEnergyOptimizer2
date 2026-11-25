"""Abgebrochenes Setup: die Integration lädt sich selbst neu.

Home Assistant bricht Setups ab, die beim Start über die Bootstrap-Frist
laufen („Global task timeout: Bootstrap stage 2 timeout“), und versucht sie
nicht erneut. Anderlik 23.11.2026: Danach liefen zweieinhalb Tage weder
Fahrplan noch Steuerung, und ein Entladedeckel 0 blieb im Huawei stehen.
"""

import asyncio
import importlib
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import custom_components.eeg_energy_optimizer as integ
from custom_components.eeg_energy_optimizer.const import DOMAIN


@pytest.fixture(autouse=True)
def _leeren():
    integ._SETUP_ABGEBROCHEN.clear()
    yield
    integ._SETUP_ABGEBROCHEN.clear()


def _hass(laeuft=False, stoppt=False):
    hass = MagicMock()
    hass.is_stopping = stoppt
    hass.state = SimpleNamespace(value="RUNNING" if laeuft else "STARTING")
    hass.data = {}
    hass.config_entries.async_schedule_reload = MagicMock()
    return hass


def _entry():
    return SimpleNamespace(entry_id="e1")


async def _abbrechen(hass, entry):
    async def _setup(_h, _e):
        raise asyncio.CancelledError("Global task timeout: Bootstrap stage 2 timeout")

    with patch.object(integ, "_async_setup_entry", _setup):
        with pytest.raises(asyncio.CancelledError):
            await integ.async_setup_entry(hass, entry)


async def test_abbruch_beim_start_laedt_nach_dem_start_neu():
    hass, entry = _hass(), _entry()
    await _abbrechen(hass, entry)
    # Der Abbruch geht weiter an HA — nur ein Listener bleibt zurück.
    assert "e1" in integ._SETUP_ABGEBROCHEN
    ereignis, nach_start = hass.bus.async_listen_once.call_args.args
    assert ereignis == integ.EVENT_HOMEASSISTANT_STARTED

    # Listener und Timer sind Coroutinen — eine schlichte Funktion liefe in
    # HA im Executor-Thread.
    assert inspect.iscoroutinefunction(nach_start)
    with patch.object(integ, "async_call_later") as spaeter:
        await nach_start(None)
    verzoegerung, neu_laden = spaeter.call_args.args[1:]
    assert verzoegerung == integ.NEUSTART_NACH_ABBRUCH_S
    assert inspect.iscoroutinefunction(neu_laden)

    with patch.object(integ, "_aufraeumen_nach_abbruch", AsyncMock()) as aufraeumen:
        await neu_laden(None)
    aufraeumen.assert_awaited_once_with(hass, entry)
    hass.config_entries.async_schedule_reload.assert_called_once_with("e1")


async def test_abbruch_im_laufenden_betrieb_wartet_nicht_auf_den_start():
    hass = _hass(laeuft=True)
    with patch.object(integ, "async_call_later") as spaeter:
        await _abbrechen(hass, _entry())
    hass.bus.async_listen_once.assert_not_called()
    spaeter.assert_called_once()


async def test_beim_herunterfahren_kein_neuladen():
    hass = _hass(stoppt=True)
    await _abbrechen(hass, _entry())
    hass.bus.async_listen_once.assert_not_called()
    assert integ._SETUP_ABGEBROCHEN == {}


async def test_aufraeumen_meldet_takte_ab_und_entlaedt_plattformen():
    hass, entry = _hass(), _entry()
    takt = MagicMock()
    hass.data = {DOMAIN: {"e1": {"sensor_takte": [takt, MagicMock(side_effect=RuntimeError)]}}}
    entladen = {}

    def _modul(name):
        plattform = name.rsplit(".", 1)[1]
        if plattform == "select":
            # Nie eingerichtet: Die Komponente meldet „never loaded“.
            return SimpleNamespace(async_unload_entry=AsyncMock(side_effect=ValueError))
        entladen[plattform] = AsyncMock(return_value=True)
        return SimpleNamespace(async_unload_entry=entladen[plattform])

    with patch.object(importlib, "import_module", side_effect=_modul):
        await integ._aufraeumen_nach_abbruch(hass, entry)
    takt.assert_called_once()
    entladen["sensor"].assert_awaited_once_with(hass, entry)
    assert hass.data[DOMAIN]["e1"]["stopping"] is True
    assert "sensor_takte" not in hass.data[DOMAIN]["e1"]


async def test_gelungenes_setup_bleibt_unberuehrt():
    hass = _hass()

    async def _setup(_h, _e):
        return True

    with patch.object(integ, "_async_setup_entry", _setup):
        assert await integ.async_setup_entry(hass, _entry()) is True
    hass.bus.async_listen_once.assert_not_called()
