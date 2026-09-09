"""Tests für ``eeg_optimizer/get_awattar_sunny``.

Der Befehl liefert dem Panel den Monatstarif aWATTar SUNNY je Vertragsvariante
samt Monat, Herkunft und Alter. Ohne Daten holt er einmal, ``refresh`` erzwingt
den Abruf — wie beim Spotpreis.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.eeg_energy_optimizer.const import DOMAIN

ENTRY_ID = "entry-1"


def _hass(provider):
    hass = MagicMock()
    entry = SimpleNamespace(entry_id=ENTRY_ID, data={}, options={}, version=20)
    hass.config_entries.async_entries = MagicMock(return_value=[entry])
    daten = {"inverter": None}
    if provider is not None:
        daten["awattar_sunny"] = provider
    hass.data = {DOMAIN: {ENTRY_ID: daten}}
    return hass


def _provider(hat_daten=True):
    p = MagicMock()
    p.hat_daten = MagicMock(return_value=hat_daten)
    p.async_fetch = AsyncMock()
    p.status = MagicMock(return_value={
        "neu": {"preis": 0.08989, "jahr": 2026, "monat": 9},
        "alt": {"preis": 0.08989, "jahr": 2026, "monat": 9},
        "alt_bis": "25.02.2026",
        "quelle": "tabelle",
        "alter_minuten": 3,
        "fehler": None,
    })
    return p


async def _run(hass, **extra):
    from custom_components.eeg_energy_optimizer import websocket_api

    conn = MagicMock()
    conn.send_result = MagicMock()
    conn.send_error = MagicMock()
    msg = {"id": 7, "type": "eeg_optimizer/get_awattar_sunny", **extra}
    handler = getattr(
        websocket_api.ws_get_awattar_sunny, "_func", websocket_api.ws_get_awattar_sunny
    )
    await handler(hass, conn, msg)
    return conn


async def test_ohne_anbieter_kommt_eine_klare_meldung():
    conn = await _run(_hass(None))
    payload = conn.send_result.call_args[0][1]
    assert payload["fehler"] == "Anbieter nicht geladen"
    assert payload["neu"] is None and payload["alt"] is None


async def test_mit_daten_wird_nicht_erneut_geholt():
    p = _provider(hat_daten=True)
    conn = await _run(_hass(p))
    p.async_fetch.assert_not_called()
    payload = conn.send_result.call_args[0][1]
    assert payload["neu"]["preis"] == 0.08989
    assert payload["alt_bis"] == "25.02.2026"


async def test_ohne_daten_wird_einmal_geholt():
    """Erstes Öffnen im Panel: ohne Daten wäre der Status eine leere
    Behauptung — einmal holen, die Frische-Frist drosselt Wiederholungen."""
    p = _provider(hat_daten=False)
    await _run(_hass(p))
    p.async_fetch.assert_awaited_once_with()


async def test_refresh_erzwingt_den_abruf():
    p = _provider(hat_daten=True)
    await _run(_hass(p), refresh=True)
    p.async_fetch.assert_awaited_once_with(force=True)
