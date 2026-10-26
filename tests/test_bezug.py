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
        "2026-08-26": _tagesergebnis(bezug_kwh=4.0, bezug_kosten=1.0, bezug_bepreist_kwh=4.0),
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


def test_ausgeschaltet_werden_keine_viertelstunden_gespeichert():
    b = _bilanz({"bilanz_viertelstunden": False})
    gestern = _tag_mit({8: _slot(bezug=0.5, kwp=0.30, s=900.0)}, datum="2026-08-26")
    b._archiviere(gestern, _inputs())
    b._heute = {"datum": TAG, "slots": {}}

    ergebnis = b.bezug("heute", JETZT, _inputs(), datum="2026-08-26")

    assert b._viertelstunden == {}
    assert b._vs_dirty is False
    # Die Tagessumme kommt trotzdem ins Archiv.
    assert ergebnis["kennzahlen"]["bezug_kwh"] == pytest.approx(0.5)
    assert ergebnis["reihe"] == []


def test_schalter_wirkt_ohne_neustart():
    b = _bilanz({"bilanz_viertelstunden": False})
    b.update_config({"bilanz_viertelstunden": True})
    b._archiviere(_tag_mit({8: _slot(bezug=0.5, kwp=0.30, s=900.0)}, datum="2026-08-26"), _inputs())

    assert "2026-08-26" in b._viertelstunden


# ---------------------------------------------------------------------------
# Kosten nur für bepreiste Viertelstunden (Review #8, Punkt 2)
#
# Ein Slot ohne eingefrorenen Bezugspreis (noch kein Fahrplan, etwa nach
# einem Neustart) zählte als 0 € und drückte den Preis je kWh: 5 kWh zu
# 0,30 € und 5 kWh ohne Preis ergaben 0,15 €/kWh statt 0,30.
# ---------------------------------------------------------------------------


def test_teilweise_bepreister_tag_teilt_nur_durch_die_bepreiste_menge():
    b = _bilanz()
    b._heute = _tag_mit({
        8: _slot(bezug=5.0, kwp=None, s=900.0),
        9: _slot(bezug=5.0, kwp=0.30, s=900.0),
    })

    k = b.bezug("heute", JETZT, _inputs())["kennzahlen"]

    assert k["bezug_kwh"] == pytest.approx(10.0)
    assert k["kosten"] == pytest.approx(1.5)
    assert k["kosten_je_kwh"] == pytest.approx(0.30)
    assert k["ohne_preis_kwh"] == pytest.approx(5.0)


def test_bewerte_tag_traegt_die_bepreiste_menge():
    b = _bilanz()
    tag = _tag_mit({
        8: _slot(bezug=2.0, kwp=None, s=900.0),
        9: _slot(bezug=1.0, kwp=0.25, s=900.0),
    })

    e = b.bewerte_tag(tag, _inputs())

    assert e["bezug_kwh"] == pytest.approx(3.0)
    assert e["bezug_kosten"] == pytest.approx(0.25)
    assert e["bezug_bepreist_kwh"] == pytest.approx(1.0)


def test_tag_mit_bezug_aber_ohne_jeden_preis_hat_keine_kosten():
    b = _bilanz()
    tag = _tag_mit({8: _slot(bezug=2.0, kwp=None, s=900.0)})

    e = b.bewerte_tag(tag, _inputs())

    assert e["bezug_kosten"] is None
    b._heute = tag
    ergebnis = b.bezug("heute", JETZT, _inputs())
    assert ergebnis["kennzahlen"]["kosten"] is None
    assert ergebnis["kennzahlen"]["kosten_je_kwh"] is None
    # Ein solcher Tag setzt nicht den Beginn der Kostenerfassung.
    assert ergebnis["kosten_seit"] is None


def test_leerer_tag_behauptet_keine_kosten():
    b = _bilanz()
    e = b.bewerte_tag({"datum": TAG, "slots": {}}, _inputs())

    assert e["bezug_kosten"] is None
    assert e["bezug_bepreist_kwh"] is None


def test_tag_ohne_bezug_kostet_null():
    """Kein Bezug mit Preisen ist ein echter Nullbetrag, kein fehlender."""
    b = _bilanz()
    e = b.bewerte_tag(_tag_mit({8: _slot(pv=1.0, export=0.2, kwp=0.3, s=900.0)}), _inputs())

    assert e["bezug_kosten"] == 0.0
    assert e["bezug_bepreist_kwh"] == 0.0


def test_ohne_preis_tag_im_monat_zaehlt_zur_menge_nicht_zu_den_kosten():
    b = _bilanz()
    b._heute = {"datum": TAG, "slots": {}}
    b._tage = {
        "2026-08-24": _tagesergebnis(bezug_kwh=6.0, bezug_kosten=None, bezug_bepreist_kwh=None),
        "2026-08-25": _tagesergebnis(bezug_kwh=4.0, bezug_kosten=1.2, bezug_bepreist_kwh=4.0),
    }

    ergebnis = b.bezug("monat", JETZT, _inputs())
    k = ergebnis["kennzahlen"]

    assert k["bezug_kwh"] == pytest.approx(10.0)
    assert k["kosten"] == pytest.approx(1.2)
    assert k["kosten_je_kwh"] == pytest.approx(0.30)
    assert k["ohne_preis_kwh"] == pytest.approx(6.0)
    assert ergebnis["kosten_seit"] == "2026-08-25"


def test_monatssumme_addiert_kosten_und_bepreiste_menge():
    b = _bilanz()
    b._archiviere(_tag_mit({8: _slot(bezug=1.0, kwp=0.2, s=900.0)}, datum="2026-08-25"), _inputs())
    b._archiviere(_tag_mit({8: _slot(bezug=3.0, kwp=None, s=900.0)}, datum="2026-08-26"), _inputs())

    monat = b._monate["2026-08"]

    assert monat["bezug_kwh"] == pytest.approx(4.0)
    assert monat["bezug_kosten"] == pytest.approx(0.2)
    assert monat["bezug_bepreist_kwh"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Laufender Tag ohne volle Bewertung (Review #8, Punkt 7)
# ---------------------------------------------------------------------------


def test_bezug_bewertet_den_laufenden_tag_nicht_voll(monkeypatch):
    b = _bilanz()
    b._heute = _tag_mit({8: _slot(bezug=0.5, kwp=0.30, s=900.0)})

    def verboten(*_a, **_k):
        raise AssertionError("bewerte_tag darf für die Bezugs-Karte nicht laufen")

    monkeypatch.setattr(b, "bewerte_tag", verboten)
    for zeitraum in ("heute", "monat", "jahr", "gesamt"):
        k = b.bezug(zeitraum, JETZT, _inputs())["kennzahlen"]
        assert k["bezug_kwh"] == pytest.approx(0.5)
        assert k["kosten"] == pytest.approx(0.15)


def test_bezug_laufender_tag_mit_spitze():
    b = _bilanz()
    b._heute = _tag_mit({
        8: _slot(bezug=0.5, kwp=0.30, s=900.0),
        9: _slot(bezug=1.0, kwp=0.30, s=900.0),
    })

    k = b.bezug("heute", JETZT, _inputs())["kennzahlen"]

    assert k["spitze_kw"] == pytest.approx(4.0)
    assert k["spitze_zeit"].endswith("06:15")


def test_bezug_spitze_gleich_wie_in_bewerte_tag():
    b = _bilanz()
    tag = _tag_mit({
        3: _slot(bezug=0.3, kwp=0.30, s=900.0),
        20: _slot(bezug=0.9, kwp=0.30, s=900.0),
    })

    voll = b.bewerte_tag(tag, _inputs())
    leicht = b._bezug_tag(tag)

    for feld in ("bezug_kwh", "bezug_kosten", "bezug_bepreist_kwh",
                 "bezug_spitze_kw", "bezug_spitze_zeit"):
        assert leicht[feld] == voll[feld], feld


def test_bezug_reihe_heute_ohne_gemeinschaftsaufteilung(monkeypatch):
    from custom_components.eeg_energy_optimizer import schedule as schedule_modul

    def verboten(*_a, **_k):
        raise AssertionError("die Bezugs-Karte braucht keine EEG-Aufteilung")

    monkeypatch.setattr(schedule_modul, "eeg_aufnahme_je_slot", verboten)
    b = _bilanz()
    b._heute = _tag_mit({8: _slot(bezug=0.5, kwp=0.30, s=900.0)})

    reihe = b.bezug("heute", JETZT, _inputs())["reihe"]

    assert len(reihe) == 1 and reihe[0]["bezug"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Tageswerte für die Statuskarte (Review #8, Punkt 4)
# ---------------------------------------------------------------------------


def test_tageswerte_summieren_die_laufende_reihe():
    b = _bilanz()
    b._heute = _tag_mit({
        8: _slot(pv=0.2, bezug=0.5, s=900.0),
        40: _slot(pv=2.0, export=0.75, s=900.0),
    })

    assert b.tageswerte() == {"pv_kwh": 2.2, "bezug_kwh": 0.5, "export_kwh": 0.75}


def test_tageswerte_ohne_bilanztag_sind_none():
    b = _bilanz()
    b._heute = {"datum": "", "slots": {}}

    assert b.tageswerte() is None


def test_tageswerte_stimmen_mit_der_tagesbewertung():
    b = _bilanz()
    b._heute = _tag_mit({
        8: _slot(pv=0.1, bezug=0.4, s=900.0),
        40: _slot(pv=1.5, export=0.6, s=900.0),
    })

    tag = b.tageswerte()
    voll = b.heute(_inputs())

    assert tag["pv_kwh"] == pytest.approx(voll["pv_kwh"])
    assert tag["bezug_kwh"] == pytest.approx(voll["bezug_kwh"])
    assert tag["export_kwh"] == pytest.approx(voll["export_kwh"])


# ---------------------------------------------------------------------------
# Viertelstunden-Archiv: robust lesen, klein speichern (Review #8, Punkte 3, 10)
# ---------------------------------------------------------------------------

from custom_components.eeg_energy_optimizer.bilanz import (  # noqa: E402
    _entpacke_viertelstunden,
    _packe_viertelstunden,
)


def test_entpacken_mit_zu_kurzer_spalte_liefert_none_statt_fehler():
    gepackt = {"i": [0, 1, 2], "e": [0.1, 0.2], "z": [0.3]}

    reihe = _entpacke_viertelstunden(gepackt, "2026-08-26")

    assert [r["export"] for r in reihe] == [0.1, 0.2, None]
    assert [r["bezug"] for r in reihe] == [0.3, None, None]
    assert all(r["kosten"] is None for r in reihe)


def test_entpacken_ueberspringt_unlesbare_indizes():
    gepackt = {"i": [0, "x", None, 4], "e": [0.1, 0.2, 0.3, 0.4]}

    reihe = _entpacke_viertelstunden(gepackt, "2026-08-26")

    assert [r["i"] for r in reihe] == [0, 4]
    assert [r["export"] for r in reihe] == [0.1, 0.4]
    assert reihe[1]["t"].startswith("2026-08-26T05:00")


@pytest.mark.parametrize("gepackt", [None, [], "kaputt", {}, {"i": None}, {"i": "0"}])
def test_entpacken_kaputter_eintrag_ist_leer(gepackt):
    assert _entpacke_viertelstunden(gepackt, "2026-08-26") == []


def test_entpacken_mit_spalte_falschen_typs():
    reihe = _entpacke_viertelstunden({"i": [0], "e": "0.5"}, "2026-08-26")

    assert reihe[0]["export"] is None


def test_kaputter_archivtag_kippt_die_karte_nicht():
    b = _bilanz()
    b._heute = {"datum": TAG, "slots": {}}
    b._tage = {"2026-08-26": _tagesergebnis(bezug_kwh=2.0)}
    b._viertelstunden = {"2026-08-26": {"i": [0, 1], "z": [0.5]}}

    ergebnis = b.bezug("heute", JETZT, _inputs(), datum="2026-08-26")

    assert ergebnis["kennzahlen"]["bezug_kwh"] == pytest.approx(2.0)
    assert len(ergebnis["reihe"]) == 2


def test_packen_rundet_auf_wattstunden():
    gepackt = _packe_viertelstunden([
        {"i": 0, "export": 0.123456, "batterie": 0.0, "eeg": None, "bedarf": True,
         "bezug": 0.00049, "kosten": 0.0123456},
    ])

    assert gepackt["e"] == [0.123]
    assert gepackt["z"] == [0.0]
    assert gepackt["k"] == [0.0123]
    assert gepackt["g"] == [None]
    assert gepackt["d"] == [True]


def test_packen_und_entpacken_hin_und_zurueck():
    b = _bilanz()
    gestern = _tag_mit({
        8: _slot(bezug=0.5, kwp=0.30, s=900.0),
        40: _slot(pv=1.0, export=0.4, kwp=0.30, s=900.0),
    }, datum="2026-08-26")
    b._archiviere(gestern, _inputs())

    reihe = _entpacke_viertelstunden(b._viertelstunden["2026-08-26"], "2026-08-26")

    assert [(r["i"], r["bezug"], r["export"]) for r in reihe] == [(8, 0.5, 0.0), (40, 0.0, 0.4)]
    assert reihe[0]["kosten"] == pytest.approx(0.15)


def test_archiv_nach_400_tagen_bleibt_unter_zwei_megabyte():
    """Ein volles Jahr Viertelstunden, wie sie das Archiv hält (TAGE_ROH).

    Die Einstellung nennt die Größe; ändert sich das Format, muss dieser
    Test und der Text dort mitgehen.
    """
    import json
    import random

    from custom_components.eeg_energy_optimizer.bilanz import TAGE_ROH

    zufall = random.Random(1)
    reihe = [
        {
            "i": i,
            "export": zufall.uniform(0, 1.5),
            "batterie": 0.0 if i < 60 else zufall.uniform(0, 0.5),
            "eeg": zufall.uniform(0, 1.0),
            "bedarf": zufall.random() < 0.5,
            "bezug": zufall.uniform(0, 0.4),
            "kosten": zufall.uniform(0, 0.1),
        }
        for i in range(96)
    ]
    tag = _packe_viertelstunden(reihe)
    groesse = len(json.dumps({"version": 1, "tage": {f"t{n}": tag for n in range(TAGE_ROH)}}))

    assert groesse < 2_000_000, groesse
