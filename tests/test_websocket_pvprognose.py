"""Tests für die WebSocket-Befehle der eigenen PV-Prognose und des Vergleichs.

``get_pvprognose`` (Stand, ``refresh`` holt), ``probe_pvprognose`` (Probe mit
ungespeicherten Flächen, Fehler als ``invalid_config`` / ``fetch_failed``),
``get_prognosevergleich`` (Übersicht, Tag im Detail, Schalterzustand) und die
Prüfung der Flächen in ``save_config`` — auch dann, wenn die eigene Prognose
nur als Vergleichsquelle mitläuft.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.eeg_energy_optimizer.const import DOMAIN

ENTRY_ID = "entry-1"


def _hass(daten: dict | None = None, entry_data: dict | None = None):
    hass = MagicMock()
    entry = SimpleNamespace(entry_id=ENTRY_ID, data=entry_data or {}, options={}, version=29)
    hass.config_entries.async_entries = MagicMock(return_value=[entry])
    hass.config_entries.async_update_entry = MagicMock()
    hass.data = {DOMAIN: {ENTRY_ID: {"inverter": None, **(daten or {})}}}
    return hass


async def _run(name: str, hass, **extra):
    from custom_components.eeg_energy_optimizer import websocket_api

    conn = MagicMock()
    msg = {"id": 7, "type": f"eeg_optimizer/{name}", **extra}
    handler = getattr(websocket_api, f"ws_{name}")
    await getattr(handler, "_func", handler)(hass, conn, msg)
    return conn


# ---------------------------------------------------------------------------
# get_pvprognose
# ---------------------------------------------------------------------------


async def test_get_pvprognose_ohne_anbieter():
    conn = await _run("get_pvprognose", _hass())
    assert conn.send_result.call_args[0][1]["fehler"] == "Anbieter nicht geladen"


async def test_get_pvprognose_liefert_den_stand_und_holt_auf_wunsch():
    provider = MagicMock()
    provider.async_fetch = AsyncMock()
    provider.status = MagicMock(return_value={"tage_kwh": [20.0] * 7, "fehler": None})
    hass = _hass({"pvprognose": provider})

    conn = await _run("get_pvprognose", hass)
    assert conn.send_result.call_args[0][1]["tage_kwh"][0] == 20.0
    provider.async_fetch.assert_not_awaited()

    await _run("get_pvprognose", hass, refresh=True)
    provider.async_fetch.assert_awaited_once_with(force=True)


# ---------------------------------------------------------------------------
# probe_pvprognose
# ---------------------------------------------------------------------------


async def test_probe_reicht_die_eingaben_durch():
    from custom_components.eeg_energy_optimizer.pvprognose import provider as prov

    rechne = AsyncMock(return_value={"tage_kwh": [30.0] * 7, "kwp_gesamt": 8.0})
    flaechen = [{"kwp": 5, "neigung": 30, "azimut": 180, "max_kw": 4}]
    with patch.object(prov, "berechne_einmalig", rechne), patch(
        "custom_components.eeg_energy_optimizer.pvprognose.berechne_einmalig", rechne
    ):
        conn = await _run(
            "probe_pvprognose", _hass(), flaechen=flaechen, verluste_pct=12, ac_limit_kw=6
        )
    assert conn.send_result.call_args[0][1]["kwp_gesamt"] == 8.0
    args = rechne.await_args[0]
    assert args[1] == flaechen and args[2] == 12 and args[3] == 6.0


async def test_probe_meldet_eingabe_und_netzfehler_getrennt():
    for fehler, code in ((ValueError("Kein Standort"), "invalid_config"), (RuntimeError("HTTP 502"), "fetch_failed")):
        rechne = AsyncMock(side_effect=fehler)
        with patch("custom_components.eeg_energy_optimizer.pvprognose.berechne_einmalig", rechne):
            conn = await _run("probe_pvprognose", _hass(), flaechen=[{"kwp": 5}])
        assert conn.send_error.call_args[0][1] == code
        assert str(fehler) in conn.send_error.call_args[0][2]


# ---------------------------------------------------------------------------
# get_prognosevergleich
# ---------------------------------------------------------------------------


async def test_vergleich_ohne_objekt_und_mit_objekt():
    conn = await _run("get_prognosevergleich", _hass({"config": {"pv_prognose_vergleich": True}}))
    antwort = conn.send_result.call_args[0][1]
    assert antwort["aktiv"] is True and antwort["fehler"] == "nicht geladen"

    vergleich = MagicMock()
    vergleich.status = MagicMock(return_value={"tage": ["2026-09-27"], "tag": {"datum": "2026-09-27"}})
    hass = _hass({"config": {}, "prognosevergleich": vergleich})
    conn = await _run("get_prognosevergleich", hass, datum="2026-09-27")
    antwort = conn.send_result.call_args[0][1]
    vergleich.status.assert_called_once_with("2026-09-27")
    assert antwort["aktiv"] is False
    assert antwort["tage"] == ["2026-09-27"]


# ---------------------------------------------------------------------------
# save_config: Flächen werden geprüft, auch für die Vergleichsquelle
# ---------------------------------------------------------------------------


async def test_save_config_prueft_flaechen_bei_eigener_quelle():
    hass = _hass()
    conn = await _run("save_config", hass, config={"forecast_source": "eigen", "pv_flaechen": []})
    assert conn.send_error.call_args[0][1] == "invalid_config"
    hass.config_entries.async_update_entry.assert_not_called()


async def test_save_config_prueft_flaechen_auch_als_vergleichsquelle():
    hass = _hass(entry_data={"forecast_source": "solcast_solar"})
    conn = await _run("save_config", hass, config={"pv_prognose_vergleich": True})
    assert conn.send_error.call_args[0][1] == "invalid_config"

    conn = await _run(
        "save_config",
        hass,
        config={
            "pv_prognose_vergleich": True,
            "pv_flaechen": [{"name": " Süd ", "kwp": "5", "max_kw": ""}],
            "pv_verluste_pct": "",
        },
    )
    conn.send_error.assert_not_called()
    gespeichert = hass.config_entries.async_update_entry.call_args[1]["data"]
    assert gespeichert["pv_flaechen"] == [
        {"name": "Süd", "kwp": 5.0, "neigung": 30.0, "azimut": 180.0, "max_kw": None}
    ]
    assert gespeichert["pv_verluste_pct"] == 14.0


async def test_save_config_ohne_eigene_prognose_prueft_nichts():
    hass = _hass(entry_data={"forecast_source": "solcast_solar"})
    conn = await _run("save_config", hass, config={"lookback_weeks": 5})
    conn.send_error.assert_not_called()
    hass.config_entries.async_update_entry.assert_called_once()
