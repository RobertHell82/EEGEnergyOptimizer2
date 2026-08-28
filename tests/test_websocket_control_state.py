"""Tests für ``eeg_optimizer/get_control_state``.

Speist die Transparenz-Ansicht im Panel: pro Stellgröße der Ist-Wert im
Wechselrichter neben dem Wert, den die Steuerung zuletzt geschrieben hat.
Weichen beide ab, hat entweder jemand anderes gestellt oder ein Schreibbefehl
ist nicht angekommen.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.eeg_energy_optimizer.const import DOMAIN

ENTRY_ID = "entry-1"


def _state(value, unit=None, maximum=None):
    return SimpleNamespace(
        state=value,
        attributes={"unit_of_measurement": unit, "max": maximum},
    )


def _make_hass(inverter, executor, states=None, entries=True):
    hass = MagicMock()
    entry = SimpleNamespace(entry_id=ENTRY_ID, data={}, options={}, version=20)
    hass.config_entries.async_entries = MagicMock(
        return_value=[entry] if entries else []
    )
    hass.data = {
        DOMAIN: {ENTRY_ID: {"inverter": inverter, "executor": executor}}
    }
    hass.states.get = MagicMock(side_effect=lambda eid: (states or {}).get(eid))
    return hass


def _make_inverter(rows, supported=True):
    inv = MagicMock()
    inv.get_control_entities = MagicMock(return_value=rows)
    inv.supports_schedule_control = supported
    return inv


def _make_executor(**status):
    ex = MagicMock()
    base = {
        "mode": "Ein",
        "active_kind": "charge_limit",
        "written_charge_limit_kw": None,
        "written_discharge_kw": None,
        "written_target_soc": None,
        "last_run": "2026-08-24T15:00:00+02:00",
        "status": "Laden begrenzt auf 2,00 kW",
    }
    base.update(status)
    ex.status = MagicMock(return_value=base)
    return ex


def _connection():
    conn = MagicMock()
    conn.send_result = MagicMock()
    conn.send_error = MagicMock()
    return conn


async def _run(hass):
    from custom_components.eeg_energy_optimizer import websocket_api

    conn = _connection()
    msg = {"id": 5, "type": "eeg_optimizer/get_control_state"}
    handler = getattr(
        websocket_api.ws_get_control_state, "_func", websocket_api.ws_get_control_state
    )
    await handler(hass, conn, msg)
    return conn


@pytest.mark.asyncio
async def test_ist_wert_und_schreibwert_je_stellgroesse():
    """Der Ist-Wert kommt aus der Entität, der Schreibwert vom Executor."""
    rows = [
        {
            "label": "Ladeleistung max",
            "entity_id": "number.batteries_maximale_ladeleistung",
            "role": "charge_limit",
        }
    ]
    hass = _make_hass(
        _make_inverter(rows),
        _make_executor(written_charge_limit_kw=2.0),
        states={
            "number.batteries_maximale_ladeleistung": _state("2000", "W", 5000)
        },
    )

    payload = (await _run(hass)).send_result.call_args[0][1]

    assert payload["supported"] is True
    assert len(payload["rows"]) == 1
    row = payload["rows"][0]
    assert row["value"] == "2000"
    assert row["unit"] == "W"
    assert row["max"] == 5000
    assert row["written"] == 2.0
    assert row["written_unit"] == "kW"


@pytest.mark.asyncio
async def test_ohne_schreibwert_bleibt_written_leer():
    """Im Anzeige-Modus schreiben wir nichts — das Panel zeigt dann Standard."""
    rows = [
        {
            "label": "Ladeleistung max",
            "entity_id": "number.x",
            "role": "charge_limit",
        }
    ]
    hass = _make_hass(
        _make_inverter(rows),
        _make_executor(mode="Test", active_kind=None),
        states={"number.x": _state("5000", "W", 5000)},
    )

    payload = (await _run(hass)).send_result.call_args[0][1]

    assert payload["mode"] == "Test"
    assert payload["rows"][0]["written"] is None


@pytest.mark.asyncio
async def test_fehlende_entitaet_liefert_none_statt_absturz():
    """Eine noch nicht geladene Entität darf die Ansicht nicht sprengen."""
    rows = [{"label": "Betriebsmodus", "entity_id": "select.weg", "role": "mode"}]
    hass = _make_hass(_make_inverter(rows), _make_executor(), states={})

    payload = (await _run(hass)).send_result.call_args[0][1]

    assert payload["rows"][0]["value"] is None
    assert payload["rows"][0]["unit"] is None


@pytest.mark.asyncio
async def test_treiber_ohne_steuerung_melden_supported_false():
    """Fronius & Co. rechnen nur — die Ansicht muss das sagen können."""
    hass = _make_hass(_make_inverter([], supported=False), _make_executor())

    payload = (await _run(hass)).send_result.call_args[0][1]

    assert payload["supported"] is False
    assert payload["rows"] == []


@pytest.mark.asyncio
async def test_entladung_liefert_ziel_soc():
    rows = [
        {
            "label": "Entladeleistung max",
            "entity_id": "number.d",
            "role": "discharge_limit",
        }
    ]
    hass = _make_hass(
        _make_inverter(rows),
        _make_executor(
            active_kind="discharge", written_discharge_kw=2.8, written_target_soc=43.0
        ),
        states={"number.d": _state("5000", "W", 5000)},
    )

    payload = (await _run(hass)).send_result.call_args[0][1]

    assert payload["target_soc"] == 43.0
    assert payload["rows"][0]["written"] == 2.8


@pytest.mark.asyncio
async def test_ohne_wechselrichter_kein_fehler():
    """Vor abgeschlossenem Setup gibt es keinen Treiber — sauber melden."""
    hass = _make_hass(None, None)
    hass.data = {DOMAIN: {ENTRY_ID: {}}}

    payload = (await _run(hass)).send_result.call_args[0][1]

    assert payload["supported"] is False
    assert payload["rows"] == []


@pytest.mark.asyncio
async def test_ohne_config_entry_fehler():
    hass = _make_hass(None, None, entries=False)

    conn = await _run(hass)

    conn.send_error.assert_called_once()
    assert conn.send_error.call_args[0][1] == "not_configured"


def test_befehl_ist_registriert():
    from unittest.mock import patch

    from custom_components.eeg_energy_optimizer import websocket_api

    registered = []
    with patch.object(
        websocket_api.websocket_api,
        "async_register_command",
        side_effect=lambda hass, handler: registered.append(handler),
    ):
        websocket_api.async_register_websocket_commands(MagicMock())

    assert websocket_api.ws_get_control_state in registered
