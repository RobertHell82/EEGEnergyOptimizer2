"""Tests für ``eeg_optimizer/get_entity_ids``.

Der Befehl löst unsere unique_ids über die Entity-Registry auf echte entity_ids
auf. Nötig, weil Home Assistant die entity_id beim erstmaligen Anlegen aus dem
ANZEIGENAMEN bildet: Der Statussensor trägt die unique_id ``..._entscheidung``,
heißt auf frischen Installationen aber ``sensor...._fahrplan_status``. Wer die
entity_id aus dem Suffix errät, findet ihn dort nicht — das Panel blieb genau
deshalb im Ladezustand hängen.

Test-Idiom wie in test_websocket_telemetry.py: die innere Coroutine direkt
aufrufen, statt einen echten WebSocket-Server zu starten.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.eeg_energy_optimizer.const import DOMAIN

ENTRY_ID = "entry-1"
PREFIX = f"{DOMAIN}_{ENTRY_ID}_"


def _make_hass(entries=None):
    hass = MagicMock()
    entry = SimpleNamespace(entry_id=ENTRY_ID, data={}, options={}, version=20)
    hass.config_entries.async_entries = MagicMock(
        return_value=[entry] if entries is None else entries
    )
    hass.data = {DOMAIN: {ENTRY_ID: {}}}
    return hass


def _make_connection():
    conn = MagicMock()
    conn.send_result = MagicMock()
    conn.send_error = MagicMock()
    return conn


def _reg(unique_id: str, entity_id: str):
    """Ein Registry-Eintrag, wie er_async_entries_for_config_entry ihn liefert."""
    return SimpleNamespace(unique_id=unique_id, entity_id=entity_id)


def _call(handler, hass, connection, msg):
    inner = getattr(handler, "_func", handler)
    return inner(hass, connection, msg)


async def _run(registry_entries, hass=None):
    """Ruft den Befehl mit gepatchter Entity-Registry auf, liefert das Ergebnis."""
    from custom_components.eeg_energy_optimizer import websocket_api

    hass = hass or _make_hass()
    conn = _make_connection()
    msg = {"id": 7, "type": "eeg_optimizer/get_entity_ids"}

    with patch.object(websocket_api.er, "async_get", return_value=MagicMock()), patch.object(
        websocket_api.er,
        "async_entries_for_config_entry",
        return_value=registry_entries,
    ):
        await _call(websocket_api.ws_get_entity_ids, hass, conn, msg)
    return conn


@pytest.mark.asyncio
async def test_umbenannter_statussensor_wird_gefunden():
    """Der eigentliche Bug: unique_id ..._entscheidung, entity_id ..._fahrplan_status."""
    conn = await _run(
        [_reg(f"{PREFIX}entscheidung", "sensor.eeg_energy_optimizer_fahrplan_status")]
    )
    conn.send_result.assert_called_once()
    payload = conn.send_result.call_args[0][1]
    assert payload["entity_ids"]["entscheidung"] == (
        "sensor.eeg_energy_optimizer_fahrplan_status"
    )


@pytest.mark.asyncio
async def test_bestandsinstallation_behaelt_alte_entity_id():
    """Upgrade-Pfad: dort heißt die Entität weiter ..._entscheidung."""
    conn = await _run(
        [_reg(f"{PREFIX}entscheidung", "sensor.eeg_energy_optimizer_entscheidung")]
    )
    payload = conn.send_result.call_args[0][1]
    assert payload["entity_ids"]["entscheidung"] == (
        "sensor.eeg_energy_optimizer_entscheidung"
    )


@pytest.mark.asyncio
async def test_alle_suffixe_werden_abgebildet():
    """Sensoren und Select landen gemeinsam in der Map, Suffix ohne Präfix."""
    conn = await _run(
        [
            _reg(f"{PREFIX}entscheidung", "sensor.x_fahrplan_status"),
            _reg(f"{PREFIX}pv_prognose_heute", "sensor.x_pv_heute"),
            _reg(f"{PREFIX}optimizer", "select.x_optimizer"),
        ]
    )
    mapping = conn.send_result.call_args[0][1]["entity_ids"]
    assert mapping == {
        "entscheidung": "sensor.x_fahrplan_status",
        "pv_prognose_heute": "sensor.x_pv_heute",
        "optimizer": "select.x_optimizer",
    }


@pytest.mark.asyncio
async def test_fremde_unique_ids_werden_ignoriert():
    """Nur Einträge mit unserem Präfix — nichts aus anderen Integrationen."""
    conn = await _run(
        [
            _reg(f"{PREFIX}entscheidung", "sensor.x_fahrplan_status"),
            _reg("huawei_solar_abc_soc", "sensor.batteries_soc"),
            _reg(None, "sensor.ohne_unique_id"),
            _reg(f"{DOMAIN}_anderer_entry_entscheidung", "sensor.fremd"),
        ]
    )
    mapping = conn.send_result.call_args[0][1]["entity_ids"]
    assert mapping == {"entscheidung": "sensor.x_fahrplan_status"}


@pytest.mark.asyncio
async def test_leerer_suffix_wird_ignoriert():
    """Eine unique_id, die genau dem Präfix entspricht, ergibt keinen Schlüssel."""
    conn = await _run([_reg(PREFIX, "sensor.x_leer")])
    assert conn.send_result.call_args[0][1]["entity_ids"] == {}


@pytest.mark.asyncio
async def test_ohne_config_entry_fehler():
    """Ohne Config-Entry ein klarer Fehler statt einer leeren Map."""
    conn = await _run([], hass=_make_hass(entries=[]))
    conn.send_error.assert_called_once()
    assert conn.send_error.call_args[0][1] == "not_configured"
    conn.send_result.assert_not_called()


def test_befehl_ist_registriert():
    """Ohne Registrierung wäre der Befehl im Panel nicht aufrufbar."""
    from custom_components.eeg_energy_optimizer import websocket_api

    registered = []
    with patch.object(
        websocket_api.websocket_api,
        "async_register_command",
        side_effect=lambda hass, handler: registered.append(handler),
    ):
        websocket_api.async_register_websocket_commands(MagicMock())

    assert websocket_api.ws_get_entity_ids in registered
