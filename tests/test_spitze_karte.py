"""Karte „Bezugsspitze & Spitzenkappung": was websocket_api.spitze_karte liefert."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from custom_components.eeg_energy_optimizer.leistungsspitze import netzkosten_monat
from custom_components.eeg_energy_optimizer.spitzenkappung import (
    MIN_AUS_S,
    Spitzenkappung,
)
from custom_components.eeg_energy_optimizer.websocket_api import spitze_karte

JETZT = datetime(2026, 10, 20, 17, 0, tzinfo=timezone.utc)


class _Spitze:
    monat = "2026-10"
    spitze = {"kw": 7.43, "start": "2026-10-14T16:45:00+00:00", "vollstaendig": True}
    verlauf = {"2026-09": {"kw": 6.1, "start": "2026-09-03T18:00:00+00:00", "vollstaendig": False}}
    letzte_viertelstunde = {"kw": 1.2}

    def laufend(self, _t):
        return {"bisher_kw": 1.0, "hochrechnung_kw": 2.0, "rest_s": 300}


class _Bilanz:
    _tage = {"2026-08-30": {}, "2026-10-01": {"bezug_spitze_kw": 3.2}}

    def tagesspitzen(self, monat):
        return [{"datum": f"{monat}-01", "spitze_kw": 3.2, "zeit": None}]


def test_netzkosten_tragen_umstellung_und_aufteilung():
    k = netzkosten_monat(7.43)
    assert k["umstellung"]["vorgabe"] == "2027-12-01"
    assert k["umstellung"]["spaetestens"] == "2028-07-01"
    assert {"ab": "2029", "pct": 50} in k["anteil_pct"]


def test_laufender_monat_mit_kappung_aus():
    data = {"leistungsspitze": _Spitze(), "bilanz": _Bilanz(), "config": {"spitzenkappung_grenze_kw": 5}}
    r = spitze_karte(data, None, JETZT)
    assert r["monat"] == "2026-10"
    assert r["aeltester"] == "2026-08"
    assert r["spitze"]["kw"] == 7.43
    assert r["schwelle_kw"] == 7.43
    assert r["laufend"]["hochrechnung_kw"] == 2.0
    assert r["kappung"] is None
    assert r["tage"][0]["datum"] == "2026-10-01"


def test_vergangener_monat_ohne_laufende_werte():
    data = {"leistungsspitze": _Spitze(), "bilanz": _Bilanz(), "config": {}}
    r = spitze_karte(data, "2026-09", JETZT)
    assert r["spitze"]["kw"] == 6.1
    assert r["spitze"]["vollstaendig"] is False
    assert "laufend" not in r and "kappung" not in r


def test_verbraucherstatus():
    zustaende = {"switch.a": "off", "sensor.a": "0", "switch.b": "on", "sensor.b": "1.5"}
    hass = SimpleNamespace(states=SimpleNamespace(
        get=lambda e: SimpleNamespace(state=zustaende[e], attributes={"unit_of_measurement": "kW"})
        if e in zustaende else None
    ))
    cfg = {"spitzenkappung_enabled": True, "spitzenkappung_verbraucher": [
        {"name": "A", "leistung_entity": "sensor.a", "schalter_entity": "switch.a"},
        {"name": "B", "leistung_entity": "sensor.b", "schalter_entity": "switch.b"},
        {"name": "C", "leistung_entity": "sensor.c", "schalter_entity": "switch.c"},
    ]}
    k = Spitzenkappung(hass, "t", lambda: cfg, None, lambda: True)
    k._abgeschaltet["switch.a"] = {"seit": JETZT - timedelta(seconds=60), "leistung_kw": 3.0, "min_aus_s": MIN_AUS_S}
    status = {v["name"]: v for v in k.verbraucher_status(JETZT)}
    assert status["A"]["zustand"] == "abgeschaltet"
    assert status["A"]["frei_ab"] == (JETZT + timedelta(seconds=MIN_AUS_S - 60)).isoformat()
    assert status["B"]["zustand"] == "an" and status["B"]["leistung_kw"] == 1.5
    assert status["C"]["zustand"] == "unbekannt"


def test_tagesspitzen_aus_archiv_und_laufendem_tag():
    from custom_components.eeg_energy_optimizer.bilanz import EnergieBilanz

    b = EnergieBilanz(None, "t", {})
    b._tage = {
        "2026-09-30": {"bezug_spitze_kw": 9.0},
        "2026-10-01": {"bezug_spitze_kw": 3.21, "bezug_spitze_zeit": "2026-10-01T18:00"},
        "2026-10-02": {"bezug_kwh": 1.0},  # vor 2.1.30: ohne Spitze
    }
    b._heute = {"datum": "2026-10-03", "start": 4, "slots": {"0": {"bezug": 1.0}, "1": {"bezug": 0.5}}}
    reihe = b.tagesspitzen("2026-10")
    assert [r["datum"] for r in reihe] == ["2026-10-01", "2026-10-03"]
    assert reihe[1]["spitze_kw"] == 4.0
    assert reihe[1]["zeit"] == "2026-10-03T04:00"


def test_monatswechsel_nach_der_uhr():
    """Am 1. um 00:05 hat die Leistungsspitze noch die Oktober-Spitze — die
    Karte zeigt trotzdem November als laufend, Oktober im Verlauf."""
    data = {"leistungsspitze": _Spitze(), "bilanz": _Bilanz(), "config": {"spitzenkappung_enabled": True}}
    r = spitze_karte(data, None, datetime(2026, 10, 31, 23, 5, tzinfo=timezone.utc))
    assert r["monat"] == "2026-11"
    assert r["spitze"] is None
    assert r["schwelle_kw"] == 5.0
    alt = spitze_karte(data, "2026-10", datetime(2026, 10, 31, 23, 5, tzinfo=timezone.utc))
    assert alt["spitze"]["kw"] == 7.43


def test_tarif_auch_ohne_spitze_und_kappung_flags():
    data = {"leistungsspitze": _Spitze(), "bilanz": _Bilanz(), "config": {}}
    r = spitze_karte(data, "2026-08", JETZT)
    assert r["spitze"] is None and r["netzkosten"] is None
    assert r["tarif"]["umstellung"]["vorgabe"] == "2027-12-01"
    assert r["kappung_an"] is False
