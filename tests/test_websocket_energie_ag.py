"""Tests für ``eeg_optimizer/get_energie_ag``.

Der Befehl liefert dem Panel den Einspeisetarif der Energie AG je
Preisvariante samt Referenzmarktwert, Monat und Alter. Ohne Daten holt er
einmal, ``refresh`` erzwingt den Abruf — wie beim Spotpreis und bei SUNNY.
Besonderheit: Der Abschlag geht mit, damit die Vorschau schon beim Tippen
den Preis zeigt, den der Fahrplan später rechnet.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.eeg_energy_optimizer.const import DOMAIN

ENTRY_ID = "entry-1"


def _hass(provider, schaetzer=None, config=None):
    hass = MagicMock()
    entry = SimpleNamespace(entry_id=ENTRY_ID, data={}, options={}, version=20)
    hass.config_entries.async_entries = MagicMock(return_value=[entry])
    daten = {"inverter": None, "config": config or {}}
    if provider is not None:
        daten["energie_ag"] = provider
    if schaetzer is not None:
        daten["oemag_schaetzung"] = schaetzer
    hass.data = {DOMAIN: {ENTRY_ID: daten}}
    return hass


def _provider(hat_daten=True):
    p = MagicMock()
    p.hat_daten = MagicMock(return_value=hat_daten)
    p.async_fetch = AsyncMock()
    p.status = MagicMock(return_value={
        "float": {"preis": 0.0792, "jahr": 2026, "monat": 8},
        "loyal_float": {"preis": 0.0792, "jahr": 2026, "monat": 8},
        "referenzwert": 0.0942,
        "abschlag": 0.015,
        "jahr": 2026,
        "monat": 8,
        "alter_minuten": 3,
        "fehler": None,
        "schaetzung": None,
    })
    return p


async def _run(hass, **extra):
    from custom_components.eeg_energy_optimizer import websocket_api

    conn = MagicMock()
    conn.send_result = MagicMock()
    conn.send_error = MagicMock()
    msg = {"id": 7, "type": "eeg_optimizer/get_energie_ag", **extra}
    handler = getattr(
        websocket_api.ws_get_energie_ag, "_func", websocket_api.ws_get_energie_ag
    )
    await handler(hass, conn, msg)
    return conn


async def test_ohne_anbieter_kommt_eine_klare_meldung():
    conn = await _run(_hass(None))
    payload = conn.send_result.call_args[0][1]
    assert payload["fehler"] == "Anbieter nicht geladen"
    assert payload["float"] is None and payload["loyal_float"] is None


async def test_mit_daten_wird_nicht_erneut_geholt():
    p = _provider(hat_daten=True)
    conn = await _run(_hass(p))
    p.async_fetch.assert_not_called()
    payload = conn.send_result.call_args[0][1]
    assert payload["float"]["preis"] == 0.0792
    assert payload["referenzwert"] == 0.0942


async def test_ohne_daten_wird_einmal_geholt():
    p = _provider(hat_daten=False)
    await _run(_hass(p))
    p.async_fetch.assert_awaited_once_with()


async def test_refresh_erzwingt_den_abruf():
    p = _provider(hat_daten=True)
    await _run(_hass(p), refresh=True)
    p.async_fetch.assert_awaited_once_with(force=True)


async def test_refresh_frischt_auch_die_hochrechnung_auf():
    """Die Hochrechnung kommt aus dem OeMAG-Schätzer — ohne ihn bliebe die
    Vorschau des laufenden Monats nach „Jetzt holen" leer."""
    p = _provider(hat_daten=True)
    schaetzer = MagicMock()
    schaetzer.async_fetch = AsyncMock()
    await _run(_hass(p, schaetzer), refresh=True)
    schaetzer.async_fetch.assert_awaited_once()


async def test_ein_kaputter_schaetzer_haelt_den_monatswert_nicht_auf():
    p = _provider(hat_daten=True)
    schaetzer = MagicMock()
    schaetzer.async_fetch = AsyncMock(side_effect=RuntimeError("API down"))
    conn = await _run(_hass(p, schaetzer), refresh=True)
    assert conn.send_result.call_args[0][1]["float"]["preis"] == 0.0792


async def test_uebergebener_abschlag_schlaegt_den_gespeicherten():
    """Beim Tippen im Panel: Die Vorschau soll den getippten Abschlag
    rechnen, nicht den zuletzt gespeicherten."""
    p = _provider(hat_daten=True)
    await _run(_hass(p, config={"energie_ag_abschlag": 0.015}), abschlag=0.017)
    p.status.assert_called_once_with(0.017)


async def test_ohne_uebergabe_gilt_der_gespeicherte_abschlag():
    p = _provider(hat_daten=True)
    await _run(_hass(p, config={"energie_ag_abschlag": 0.017}))
    p.status.assert_called_once_with(0.017)
