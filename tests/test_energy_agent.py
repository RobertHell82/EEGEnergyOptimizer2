"""Tests für den energy-agent-Provider.

Geprüft wird, dass aus Erzeugung und Verbrauch der Gemeinschaft derselbe
Saldo entsteht, den der PeakShare-Provider liefert (positiv = Bedarf), und
dass der Provider dessen Schnittstelle erfüllt. Ohne HTTP: ``_get`` wird
ersetzt.
"""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.eeg_energy_optimizer import eeg_price
from custom_components.eeg_energy_optimizer import energy_agent as ea

START = datetime(2026, 11, 19, 20, 0, tzinfo=timezone.utc)
GEM_ID = "57ff42b4-d6c5-4c0f-b308-53c595d1ee5a"


def _punkte(werte):
    return [
        {
            "timestamp": (START + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "p10_kwh": 0.0,
            "p50_kwh": w,
            "p90_kwh": w * 2,
            "kind": "forecast",
        }
        for i, w in enumerate(werte)
    ]


def test_saldo_ist_verbrauch_minus_erzeugung():
    erz = _punkte([0.0, 1.0, 0.5])
    ver = _punkte([0.8, 0.4, 0.5])
    saldo = ea.saldo_intervalle(erz, ver)
    assert [s["saldoKwh"] for s in saldo] == [0.8, -0.6, 0.0]
    # Gleiches Stempelformat wie PeakShare — saldo_je_intervall liest es.
    assert eeg_price.saldo_je_intervall(saldo)


def test_saldo_nur_wo_beide_reihen_werte_haben():
    erz = _punkte([1.0, 1.0])
    ver = _punkte([0.5])
    assert len(ea.saldo_intervalle(erz, ver)) == 1
    # Fehlt eine Reihe ganz, gibt es keinen Saldo — kein erfundener Bedarf.
    assert ea.saldo_intervalle([], ver) == []


def test_bedarfsquelle_kennt_energy_agent():
    assert eeg_price.bedarfsquelle({"eeg_demand_source": "energy_agent"}) == "energy_agent"
    assert eeg_price.bedarfsquelle({"eeg_demand_source": "quote"}) == "quote"
    assert eeg_price.bedarfsquelle({}) == "peakshare"


def _provider(antworten):
    p = ea.EnergyAgentProvider(None, "temp", "ea_test")

    async def _get(pfad, params=None):
        schluessel = pfad if params is None else f"{pfad}?{params['target']}"
        wert = antworten[schluessel]
        if isinstance(wert, Exception):
            raise wert
        return wert

    p._get = _get  # type: ignore[method-assign]
    return p


@pytest.mark.asyncio
async def test_fetch_liefert_schnittstelle_wie_peakshare():
    fc = f"/v0/communities/{GEM_ID}/forecast"
    p = _provider({
        "/v0/communities": [{"id": GEM_ID, "name": "BEG Stromschnorrer", "short_name": None}],
        f"{fc}?generation": {"state": "healthy", "points": _punkte([0.0, 2.0])},
        f"{fc}?consumption": {"state": "healthy", "points": _punkte([1.0, 1.0])},
    })
    await p.async_fetch()
    assert p.letzter_fehler is None
    assert p.get_communities() == ["BEG Stromschnorrer"]
    assert [i["saldoKwh"] for i in p.get_intervals("BEG Stromschnorrer")[-2:]] == [1.0, -1.0]
    assert p.get_warnings("BEG Stromschnorrer") == []
    assert p.get_intervals("unbekannt") == []


@pytest.mark.asyncio
async def test_fehlende_prognose_wird_gemeldet():
    fc = f"/v0/communities/{GEM_ID}/forecast"
    p = _provider({
        "/v0/communities": [{"id": GEM_ID, "name": "EEG Test"}],
        f"{fc}?generation": ea.EnergyAgentFehler("FORECAST_NOT_CONFIGURED"),
        f"{fc}?consumption": {"state": "stale", "points": _punkte([1.0])},
    })
    await p.async_fetch()
    assert p.get_intervals("EEG Test") == []
    assert "FORECAST_NOT_CONFIGURED" in p.letzter_fehler
    assert "consumption: stale" in p.get_warnings("EEG Test")


@pytest.mark.asyncio
async def test_falscher_schluessel_behaelt_nichts_und_meldet():
    p = _provider({"/v0/communities": ea.EnergyAgentFehler("API-Schlüssel ungültig")})
    assert await p.async_fetch() is None
    assert p.letzter_fehler == "API-Schlüssel ungültig"
    assert p.get_communities() == []


@pytest.mark.asyncio
async def test_ohne_schluessel_kein_abruf():
    p = ea.EnergyAgentProvider(None, "temp", "  ")
    with pytest.raises(ea.EnergyAgentFehler):
        await p.async_gemeinschaften()
