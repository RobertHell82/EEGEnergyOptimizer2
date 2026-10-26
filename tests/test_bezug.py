"""Tests für die Bezugs-Karte und das Blättern nach Tag, Monat und Jahr."""

from datetime import datetime

import pytest

from .test_bilanz import (  # noqa: F401 - Fixture wird per autouse gebraucht
    TAG,
    _bilanz,
    _echte_zeitquelle,
    _inputs,
    _slot,
    _tag_mit,
)

JETZT = datetime(2026, 8, 27, 12, 0)


def _tagesergebnis(**werte):
    leer = {"export_kwh": 0.0, "bezug_kwh": 0.0, "pv_kwh": 0.0, "haus_kwh": 0.0}
    leer.update(werte)
    return leer


def test_kosten_je_viertelstunde_mit_eingefrorenem_preis():
    b = _bilanz()
    b._heute = _tag_mit({
        8: _slot(bezug=0.5, kwp=0.30, s=900.0),
        9: _slot(bezug=0.25, kwp=0.20, s=900.0),
    })

    ergebnis = b.bezug("heute", JETZT, _inputs())

    k = ergebnis["kennzahlen"]
    assert k["bezug_kwh"] == pytest.approx(0.75)
    assert k["kosten"] == pytest.approx(0.2)
    assert k["kosten_je_kwh"] == pytest.approx(0.2 / 0.75, abs=1e-4)
    assert [r["t"][11:16] for r in ergebnis["reihe"]] == ["06:00", "06:15"]
    assert ergebnis["reihe"][0]["kosten"] == pytest.approx(0.15)


def test_kosten_nur_ueber_tage_die_sie_kennen():
    b = _bilanz()
    b._heute = {"datum": TAG, "slots": {}}
    b._tage = {
        # Vor der Einführung: Menge ohne Kosten.
        "2026-08-25": _tagesergebnis(bezug_kwh=10.0),
        "2026-08-26": _tagesergebnis(bezug_kwh=4.0, bezug_kosten=1.0),
    }

    ergebnis = b.bezug("monat", JETZT, _inputs())

    k = ergebnis["kennzahlen"]
    assert k["bezug_kwh"] == pytest.approx(14.0)
    assert k["kosten"] == pytest.approx(1.0)
    # 1 € auf 4 kWh — nicht auf 14.
    assert k["kosten_je_kwh"] == pytest.approx(0.25)
    assert ergebnis["kosten_seit"] == "2026-08-26"
    tage = {r["datum"]: r for r in ergebnis["reihe"]}
    assert tage["2026-08-25"]["kosten"] is None


def test_ohne_kosten_feld_keine_kosten():
    b = _bilanz()
    b._heute = {"datum": TAG, "slots": {}}
    b._tage = {"2026-07-02": _tagesergebnis(bezug_kwh=3.0)}

    k = b.bezug("monat", JETZT, _inputs(), datum="2026-07")["kennzahlen"]

    assert k["bezug_kwh"] == pytest.approx(3.0)
    assert k["kosten"] is None and k["kosten_je_kwh"] is None


def test_gewaehlter_monat_ohne_laufenden_tag():
    b = _bilanz()
    b._heute = _tag_mit({8: _slot(bezug=9.0, kwp=0.3, s=900.0)})
    b._tage = {
        "2026-07-10": _tagesergebnis(bezug_kwh=2.0, export_kwh=1.0),
        "2026-08-26": _tagesergebnis(bezug_kwh=4.0, export_kwh=5.0),
    }

    bezug = b.bezug("monat", JETZT, _inputs(), datum="2026-07")
    einsp = b.einspeisung("monat", JETZT, _inputs(), datum="2026-07")

    assert bezug["auswahl"] == "2026-07"
    assert bezug["kennzahlen"]["bezug_kwh"] == pytest.approx(2.0)
    assert [r["datum"] for r in bezug["reihe"]] == ["2026-07-10"]
    assert einsp["auswahl"] == "2026-07"
    assert einsp["kennzahlen"]["export_kwh"] == pytest.approx(1.0)
    assert bezug["aeltester"] == "2026-07-10" and bezug["neuester"] == TAG


def test_unpassendes_datum_gilt_als_laufender_zeitraum():
    b = _bilanz()
    b._heute = {"datum": TAG, "slots": {}}
    b._tage = {}

    # Ein Tagesdatum beim Zeitraum „jahr" passt nicht — dann das laufende Jahr.
    assert b.bezug("jahr", JETZT, _inputs(), datum="2025-03-01")["auswahl"] == "2026"
    assert b.bezug("gesamt", JETZT, _inputs(), datum="2025")["auswahl"] is None


def test_vergangener_tag_kommt_mit_viertelstunden_aus_dem_archiv():
    b = _bilanz()
    gestern = _tag_mit({
        8: _slot(bezug=0.5, kwp=0.30, s=900.0),
        40: _slot(pv=1.0, export=0.4, basis=0.08, kwp=0.30, s=900.0),
    }, datum="2026-08-26")
    b._archiviere(gestern, _inputs())
    b._heute = {"datum": TAG, "slots": {}}

    bezug = b.bezug("heute", JETZT, _inputs(), datum="2026-08-26")
    einsp = b.einspeisung("heute", JETZT, _inputs(), datum="2026-08-26")

    assert bezug["auswahl"] == "2026-08-26"
    assert bezug["kennzahlen"]["kosten"] == pytest.approx(0.15)
    viertel = [r for r in bezug["reihe"] if r["bezug"]]
    assert [r["t"][:16] for r in viertel] == ["2026-08-26T06:00"]
    assert viertel[0]["kosten"] == pytest.approx(0.15)
    exportiert = [r for r in einsp["reihe"] if r["export"]]
    assert [r["t"][11:16] for r in exportiert] == ["14:00"]


def test_tag_von_vor_der_einfuehrung_hat_nur_die_summe():
    b = _bilanz()
    b._heute = {"datum": TAG, "slots": {}}
    b._tage = {"2026-08-20": _tagesergebnis(bezug_kwh=6.0)}

    ergebnis = b.bezug("heute", JETZT, _inputs(), datum="2026-08-20")

    assert ergebnis["kennzahlen"]["bezug_kwh"] == pytest.approx(6.0)
    assert ergebnis["reihe"] == []
