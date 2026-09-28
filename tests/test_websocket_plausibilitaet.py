"""WebSocket-Befehle: Endpunkte für Nicht-Admins, Abrufbremse, Zahlenprüfung.

- ``get_config`` bleibt für alle offen (das Dashboard braucht es), gibt
  Nicht-Admins aber keine Modbus-Adressen, Ports und Unit-IDs mehr.
- ``refresh: true`` der Tarifquellen erzwingt für Nicht-Admins höchstens
  einen Abruf je Minute und Quelle; Admins bleiben ungebremst.
- ``get_activity_log`` klemmt Offset und Seitengröße.
- ``save_config`` (nur Admin) weist nicht-endliche und absurde Zahlen ab.

Die Einstufung admin/offen selbst pinnt ``test_websocket_admin.py``.
"""
from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.eeg_energy_optimizer import websocket_api
from custom_components.eeg_energy_optimizer.const import DOMAIN

ENTRY_ID = "entry-1"

ENTRY_DATA = {
    "setup_complete": True,
    "inverter_type": "fronius_gen24",
    "fronius_modbus_host": "192.168.1.20",
    "fronius_modbus_port": 502,
    "heizstab_enabled": True,
    "heizstab_host": "192.168.1.58",
    "heizstab_port": 502,
    "wallbox_type": "ambibox",
    "ambibox_host": "192.168.1.70",
    "ambibox_port": 502,
    "ambibox_unit_id": 1,
    "schedule_feedin_price": 0.082,
    "battery_soc_sensor": "sensor.soc",
}


def _hass(daten: dict | None = None, entry_data: dict | None = None):
    hass = MagicMock()
    entry = SimpleNamespace(
        entry_id=ENTRY_ID, data=dict(entry_data or ENTRY_DATA), options={}, version=29
    )
    hass.config_entries.async_entries = MagicMock(return_value=[entry])
    hass.config_entries.async_update_entry = MagicMock()
    hass.data = {DOMAIN: {ENTRY_ID: {"inverter": None, **(daten or {})}}}
    return hass


def _conn(admin: bool):
    conn = MagicMock()
    conn.user = SimpleNamespace(is_admin=admin)
    return conn


async def _run(name: str, hass, conn, **extra):
    msg = {"id": 7, "type": f"eeg_optimizer/{name}", **extra}
    handler = getattr(websocket_api, f"ws_{name}")
    await getattr(handler, "_func", handler)(hass, conn, msg)
    return conn


@pytest.fixture(autouse=True)
def frische_bremse(monkeypatch):
    monkeypatch.setattr(websocket_api, "_letzter_refresh", {})


# ---------------------------------------------------------------------------
# get_config
# ---------------------------------------------------------------------------


async def test_get_config_zeigt_admins_alle_endpunkte():
    conn = await _run("get_config", _hass(), _conn(admin=True))
    config = conn.send_result.call_args[0][1]
    assert config["fronius_modbus_host"] == "192.168.1.20"
    assert config["ambibox_unit_id"] == 1


async def test_get_config_verschweigt_nicht_admins_die_endpunkte():
    conn = await _run("get_config", _hass(), _conn(admin=False))
    config = conn.send_result.call_args[0][1]
    for schluessel in (
        "fronius_modbus_host", "fronius_modbus_port", "heizstab_host",
        "heizstab_port", "ambibox_host", "ambibox_port", "ambibox_unit_id",
    ):
        assert schluessel not in config, schluessel
    # Was das Dashboard liest, bleibt.
    assert config["heizstab_enabled"] is True
    assert config["wallbox_type"] == "ambibox"
    assert config["schedule_feedin_price"] == 0.082
    assert config["battery_soc_sensor"] == "sensor.soc"
    assert config["setup_complete"] is True and config["entry_id"] == ENTRY_ID


async def test_get_config_ohne_benutzer_gilt_als_nicht_admin():
    conn = MagicMock()
    conn.user = None
    await _run("get_config", _hass(), conn)
    assert "heizstab_host" not in conn.send_result.call_args[0][1]


# ---------------------------------------------------------------------------
# refresh-Bremse
# ---------------------------------------------------------------------------


def _energie_ag():
    p = MagicMock()
    p.hat_daten = MagicMock(return_value=True)
    p.async_fetch = AsyncMock()
    p.status = MagicMock(return_value={"float": None, "loyal_float": None})
    return p


async def test_refresh_eines_nicht_admins_hoechstens_einmal_je_minute():
    p = _energie_ag()
    hass = _hass({"energie_ag": p})

    await _run("get_energie_ag", hass, _conn(admin=False), refresh=True)
    await _run("get_energie_ag", hass, _conn(admin=False), refresh=True)

    p.async_fetch.assert_awaited_once_with(force=True)
    # Geantwortet wird trotzdem, mit dem vorhandenen Stand.
    assert p.status.call_count == 2


async def test_refresh_eines_nicht_admins_nach_der_pause_wieder_erlaubt():
    p = _energie_ag()
    hass = _hass({"energie_ag": p})

    await _run("get_energie_ag", hass, _conn(admin=False), refresh=True)
    # Die Pause ist um: den gemerkten Zeitpunkt zurückdatieren, statt die
    # Uhr zu verstellen (time.monotonic trägt auch die Event-Loop).
    websocket_api._letzter_refresh["energie_ag"] -= websocket_api.REFRESH_PAUSE_S + 1
    await _run("get_energie_ag", hass, _conn(admin=False), refresh=True)

    assert p.async_fetch.await_count == 2


async def test_admins_bleiben_ungebremst():
    p = _energie_ag()
    hass = _hass({"energie_ag": p})
    for _ in range(3):
        await _run("get_energie_ag", hass, _conn(admin=True), refresh=True)
    assert p.async_fetch.await_count == 3


async def test_bremse_gilt_je_quelle():
    """Ein gebremster OeMAG-Abruf hält den Spotpreis nicht auf."""
    oemag = MagicMock()
    oemag.async_fetch = AsyncMock()
    oemag.status = MagicMock(return_value={"preis": 0.06})
    spot = MagicMock()
    spot.async_fetch = AsyncMock()
    spot.preis_jetzt = MagicMock(return_value=0.09)
    spot.status = MagicMock(return_value={"preis": 0.09})
    hass = _hass({"oemag": oemag, "spot": spot})

    await _run("get_oemag_tarif", hass, _conn(admin=False), refresh=True)
    await _run("get_oemag_tarif", hass, _conn(admin=False), refresh=True)
    await _run("get_spot_preis", hass, _conn(admin=False), refresh=True)

    oemag.async_fetch.assert_awaited_once_with(force=True)
    spot.async_fetch.assert_awaited_once_with(force=True)


async def test_gebremste_hochrechnung_wird_nicht_erzwungen():
    oemag = MagicMock()
    oemag.async_fetch = AsyncMock()
    oemag.status = MagicMock(return_value={"preis": 0.06})
    schaetzer = MagicMock()
    schaetzer.async_fetch = AsyncMock()
    schaetzer.status = MagicMock(return_value={})
    hass = _hass({"oemag": oemag, "oemag_schaetzung": schaetzer})

    await _run("get_oemag_tarif", hass, _conn(admin=False), refresh=True, schaetzung=True)
    await _run("get_oemag_tarif", hass, _conn(admin=False), refresh=True, schaetzung=True)

    assert [c.kwargs for c in schaetzer.async_fetch.await_args_list] == [
        {"force": True}, {"force": False},
    ]


# ---------------------------------------------------------------------------
# Aktivitätsprotokoll
# ---------------------------------------------------------------------------


async def test_aktivitaetsprotokoll_klemmt_offset_und_seite():
    log = deque({"n": i} for i in range(1000))
    hass = _hass({"activity_log": log})

    conn = await _run("get_activity_log", hass, _conn(admin=False), offset=-5, limit=10**6)
    antwort = conn.send_result.call_args[0][1]
    assert antwort["offset"] == 0
    assert len(antwort["entries"]) == websocket_api.ACTIVITY_LOG_MAX_SEITE
    assert antwort["entries"][0] == {"n": 999}   # neueste zuerst
    assert antwort["has_more"] is True

    conn = await _run("get_activity_log", hass, _conn(admin=False), offset=10, limit=0)
    assert len(conn.send_result.call_args[0][1]["entries"]) == 1


# ---------------------------------------------------------------------------
# save_config: Zahlen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("schluessel, wert", [
    ("schedule_feedin_price", float("nan")),
    ("schedule_feedin_price", "inf"),
    ("schedule_feedin_price", 8.2),              # Cent statt Euro
    ("schedule_energy_price", -0.1),
    ("energie_ag_abschlag", 0.5),
    ("spot_feedin_fee_pct", 150),
    ("discharge_power_kw", 5000),                # W statt kW
    ("inverter_ac_limit_kw", float("inf")),
    ("peakshare_price_2", "abc"),
    ("schedule_battery_cost", True),
])
async def test_save_config_weist_unsinnige_zahlen_ab(schluessel, wert):
    hass = _hass(entry_data={"setup_complete": True})
    conn = await _run("save_config", hass, _conn(admin=True), config={schluessel: wert})
    assert conn.send_error.call_args[0][1] == "invalid_config"
    hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.parametrize("config", [
    {"schedule_feedin_price": 0.082, "schedule_energy_price": 0.21},
    {"schedule_feedin_price": "", "energie_ag_abschlag": None},   # leere Felder
    {"schedule_feedin_price_night": 0, "spot_feedin_fee": -0.01},  # 0 = nicht gesetzt
    {"energie_ag_abschlag": 0.2, "discharge_power_kw": "5.5"},
])
async def test_save_config_nimmt_gueltige_und_leere_zahlen(config):
    hass = _hass(entry_data={"setup_complete": True})
    conn = await _run("save_config", hass, _conn(admin=True), config=config)
    conn.send_error.assert_not_called()
    hass.config_entries.async_update_entry.assert_called_once()


async def test_save_config_prueft_altwerte_im_entry_nicht():
    """Ein Altwert im Entry soll nicht jedes weitere Speichern blockieren."""
    hass = _hass(entry_data={"setup_complete": True, "schedule_feedin_price": "kaputt"})
    conn = await _run("save_config", hass, _conn(admin=True), config={"pv_peak_kwp": 8.0})
    conn.send_error.assert_not_called()


async def test_save_config_einspeisegrenze_nan_wird_abgewiesen():
    hass = _hass(entry_data={"setup_complete": True})
    conn = await _run(
        "save_config", hass, _conn(admin=True),
        config={"grid_export_limit_enabled": True, "grid_export_limit_kw": float("nan")},
    )
    assert conn.send_error.call_args[0][1] == "invalid_config"
