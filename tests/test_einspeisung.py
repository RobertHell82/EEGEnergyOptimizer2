"""Tests für die Einspeise-Karte — was ins Netz ging, und woher.

Drei Regeln tragen die Karte, und jede hat hier ihren Test:

* „Aus der Batterie" ist Einspeisung in einer Viertelstunde ohne PV —
  unabhängig davon, ob die Steuerung gerade entladen hat.
* Die EEG-Aufteilung je Slot ist dieselbe Rechnung wie der Tageserlös:
  ihre Summe MUSS ``eeg_kwh`` aus ``bewerte_geldfluesse`` sein.
* Quoten entstehen aus Summen, und der Vergleich mit dem Standardbetrieb
  läuft nur über Tage, die eine Referenz haben — auf beiden Seiten.
"""

from datetime import datetime

import pytest

from custom_components.eeg_energy_optimizer import bilanz as bilanz_modul
from custom_components.eeg_energy_optimizer.schedule import (
    bewerte_geldfluesse,
    eeg_aufnahme_je_slot,
)

from .test_bilanz import (  # noqa: F401 - Fixture wird per autouse gebraucht
    TAG,
    _bilanz,
    _echte_zeitquelle,
    _inputs,
    _slot,
    _tag_mit,
    _tagesreihe_standardbetrieb,
)

JETZT = datetime(2026, 8, 27, 12, 0)


def _tarif(**extra):
    tarif = {"name": "EEG", "anteil": 1.0, "tag": 0.12, "nacht": 0.12}
    tarif.update(extra)
    return tarif


# ---------------------------------------------------------------------------
# Batterie-Anteil
# ---------------------------------------------------------------------------


def test_einspeisung_ohne_pv_zaehlt_als_batterie():
    b = _bilanz()
    tag = _tag_mit({
        # Abend: keine PV, 0,5 kWh ins Netz — aus der Batterie.
        80: _slot(pv=0.0, export=0.5, entladen=0.6, s=900.0),
        # Mittag: PV läuft, die Einspeisung ist PV.
        48: _slot(pv=1.0, export=0.5, s=900.0),
        # Nachtrauschen des Wechselrichters: 40 W gelten als keine PV.
        90: _slot(pv=0.01, export=0.2, entladen=0.25, s=900.0),
    })

    ergebnis = b.bewerte_tag(tag, None)

    assert ergebnis["export_kwh"] == pytest.approx(1.2)
    assert ergebnis["batterie_export_kwh"] == pytest.approx(0.7)


def test_ohne_pv_misst_die_leistung_ueber_die_gezaehlte_dauer():
    """Ein Slot mit Lücke hat weniger Energie, aber nicht weniger Leistung."""
    # 5 Wh in 5 Minuten = 60 W — über der Schwelle, obwohl 5 Wh auf die
    # volle Viertelstunde gerechnet nur 20 W wären.
    assert bilanz_modul.ohne_pv(_slot(pv=0.005, s=300.0)) is False
    assert bilanz_modul.ohne_pv(_slot(pv=0.005, s=900.0)) is True
    # Ein Slot ohne gezählte Zeit (alte Aufzeichnung) gilt als volle Viertelstunde.
    assert bilanz_modul.ohne_pv(_slot(pv=0.0, s=0.0)) is True


# ---------------------------------------------------------------------------
# EEG-Aufteilung je Slot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tarif", [_tarif(), _tarif(quote_tag=0.4, quote_nacht=0.1)])
def test_aufteilung_je_slot_summiert_sich_zum_tageswert(tarif):
    start = datetime(2026, 8, 27, 12, 0)
    slots = []
    bedarf = {}
    for i in range(8):
        t = datetime(2026, 8, 27, 12 + i // 4, (i % 4) * 15)
        slots.append({"t": t.isoformat(), "grid_p": 2.0 if i % 3 else -1.0, "battery_p": 0.0})
        # Wechselnder Bedarf: mal mehr als angeboten, mal weniger, mal keiner.
        bedarf[int(t.timestamp() // 900)] = [0.0, 0.2, 1.5, None][i % 4]
    bedarf = {k: v for k, v in bedarf.items() if v is not None}
    inputs = _inputs(
        start=start,
        timestamps=[datetime.fromisoformat(s["t"]) for s in slots],
        eeg_tarife=[tarif],
        eeg_bedarf={"EEG": bedarf},
    )

    je_slot = eeg_aufnahme_je_slot(slots, inputs)

    assert len(je_slot) == len(slots)
    assert sum(je_slot) == pytest.approx(bewerte_geldfluesse(slots, inputs)["eeg_kwh"], abs=0.01)
    # Bezug nimmt nichts auf.
    assert je_slot[0] == 0.0


# ---------------------------------------------------------------------------
# Referenz
# ---------------------------------------------------------------------------


def test_standardbetrieb_hat_dieselben_mengen_wie_die_referenz():
    """Die Selbstprüfung der Karte: Fährt die Anlage Standardbetrieb, stehen
    links und rechts dieselben Zahlen."""
    b = _bilanz()
    inputs = _inputs()
    tag = _tagesreihe_standardbetrieb(inputs)

    ergebnis = b.bewerte_tag(tag, inputs)

    assert ergebnis["ref_export_kwh"] == pytest.approx(ergebnis["export_kwh"], abs=0.05)
    assert ergebnis["ref_batterie_export_kwh"] == pytest.approx(
        ergebnis["batterie_export_kwh"], abs=0.05
    )
    assert ergebnis["ref_erloes"] is not None


def test_ohne_ladestand_keine_referenzwerte():
    b = _bilanz()
    tag = _tag_mit({48: _slot(export=1.0, basis=0.08, kwp=0.26, s=900.0)})

    ergebnis = b.bewerte_tag(tag, _inputs())

    assert ergebnis["ref_export_kwh"] is None
    assert ergebnis["ref_batterie_export_kwh"] is None


# ---------------------------------------------------------------------------
# Abfrage der Karte
# ---------------------------------------------------------------------------


def _tagesergebnis(**werte):
    leer = {"export_kwh": 0.0, "eeg_kwh": 0.0, "erloes": 0.0,
            "batterie_export_kwh": 0.0, "pv_kwh": 0.0, "haus_kwh": 0.0,
            "heizstab_kwh": 0.0, "bezug_kwh": 0.0}
    leer.update(werte)
    return leer


def test_quoten_kommen_aus_summen_nicht_aus_tagesmitteln():
    b = _bilanz()
    b._heute = {"datum": TAG, "slots": {}}
    b._tage = {
        "2026-08-25": _tagesergebnis(export_kwh=10.0, eeg_kwh=1.0),
        "2026-08-26": _tagesergebnis(export_kwh=1.0, eeg_kwh=1.0),
    }

    k = b.einspeisung("monat", JETZT, _inputs())["kennzahlen"]

    # 2 von 11 kWh — nicht das Mittel aus 10 % und 100 %.
    assert k["eeg_anteil"] == pytest.approx(2 / 11, abs=1e-3)


def test_vergleich_nur_ueber_tage_mit_referenz():
    b = _bilanz()
    b._heute = {"datum": TAG, "slots": {}}
    b._tage = {
        # Vor der Einführung: keine Referenzwerte.
        "2026-08-25": _tagesergebnis(export_kwh=20.0, eeg_kwh=2.0),
        "2026-08-26": _tagesergebnis(
            export_kwh=10.0, eeg_kwh=4.0,
            ref_export_kwh=12.0, ref_eeg_kwh=1.0, ref_erloes=0.9,
            ref_batterie_export_kwh=0.0,
        ),
    }

    v = b.einspeisung("monat", JETZT, _inputs())["vergleich"]

    assert v["tage"] == 1
    # Ist-Seite nur über den Tag mit Referenz — nicht 30 gegen 12 kWh.
    assert v["ist"]["export_kwh"] == pytest.approx(10.0)
    assert v["ist"]["eeg_anteil"] == pytest.approx(0.4)
    assert v["ref"]["eeg_anteil"] == pytest.approx(1 / 12, abs=1e-3)


def test_autarkie_und_eigenverbrauch():
    b = _bilanz()
    b._heute = {"datum": TAG, "slots": {}}
    b._tage = {
        "2026-08-26": _tagesergebnis(
            pv_kwh=20.0, export_kwh=5.0, haus_kwh=9.0, heizstab_kwh=1.0, bezug_kwh=2.0,
        ),
    }

    k = b.einspeisung("monat", JETZT, _inputs())["kennzahlen"]

    assert k["autarkie"] == pytest.approx(0.8)
    assert k["eigenverbrauch"] == pytest.approx(0.75)


def test_jahr_gruppiert_nach_monat_und_kennt_alte_tage():
    b = _bilanz()
    b._heute = {"datum": TAG, "slots": {}}
    alt = _tagesergebnis(export_kwh=3.0, eeg_kwh=1.0)
    del alt["batterie_export_kwh"]
    b._tage = {
        "2026-07-10": alt,
        "2026-08-26": _tagesergebnis(export_kwh=4.0, batterie_export_kwh=1.5),
        "2025-12-31": _tagesergebnis(export_kwh=99.0),
    }

    ergebnis = b.einspeisung("jahr", JETZT, _inputs())

    monate = {m["monat"]: m for m in ergebnis["reihe"]}
    assert set(monate) == {"2026-07", "2026-08"}
    assert monate["2026-07"]["batterie"] is None
    assert monate["2026-08"]["batterie"] == pytest.approx(1.5)
    assert ergebnis["batterie_seit"] == "2026-08-26"
    assert ergebnis["erster_tag"] == "2026-07-10"


def test_heute_je_viertelstunde_mit_bedarf_und_aufnahme():
    b = _bilanz()
    b._heute = _tag_mit({
        # 12:00 (Slot 32 ab 04:00): PV-Einspeisung, Gemeinschaft hat Bedarf.
        32: _slot(pv=1.0, export=0.5, basis=0.08, kwp=0.26, s=900.0, eeg={"EEG": 0.3}),
        # 20:00: Batterie-Einspeisung, kein Bedarf.
        64: _slot(pv=0.0, export=0.4, basis=0.08, kwp=0.26, s=900.0, eeg={"EEG": -1.0}),
    })

    reihe = b.einspeisung("heute", JETZT, _inputs(eeg_tarife=[_tarif()]))["reihe"]

    assert [r["t"][11:16] for r in reihe] == ["12:00", "20:00"]
    assert reihe[0]["bedarf"] is True and reihe[1]["bedarf"] is False
    assert reihe[0]["eeg"] == pytest.approx(0.3)
    assert reihe[1]["eeg"] == pytest.approx(0.0)
    assert reihe[0]["batterie"] == 0.0
    assert reihe[1]["batterie"] == pytest.approx(0.4)


def test_quotenmodus_wird_gemeldet():
    b = _bilanz()
    ergebnis = b.einspeisung(
        "heute", JETZT, _inputs(eeg_tarife=[_tarif(quote_tag=0.5, quote_nacht=0.1)])
    )

    assert ergebnis["gemeinschaft"] is True
    assert ergebnis["quotenmodus"] is True


# ---------------------------------------------------------------------------
# Gesamt
# ---------------------------------------------------------------------------


def test_gesamt_nimmt_vor_dem_tagesarchiv_die_monatssummen():
    """Die Tage reichen 400 Tage zurück, davor stehen nur Monatssummen. Ein
    Monat darf dabei nicht doppelt zählen, und seine ref_*-Summe gehört nicht
    in den Vergleich (der läuft nur über Tage)."""
    b = _bilanz()
    b._heute = {"datum": TAG, "slots": {}}
    b._monate = {
        "2025-06": _tagesergebnis(export_kwh=100.0, eeg_kwh=10.0, ref_export_kwh=90.0),
        # Monat des ältesten Tages — steht auch als Tage im Archiv.
        "2025-08": _tagesergebnis(export_kwh=999.0),
    }
    b._tage = {
        "2025-08-30": _tagesergebnis(export_kwh=5.0),
        "2026-08-26": _tagesergebnis(export_kwh=7.0, ref_export_kwh=6.0),
    }

    ergebnis = b.einspeisung("gesamt", JETZT, _inputs())

    assert ergebnis["kennzahlen"]["export_kwh"] == pytest.approx(112.0)
    assert ergebnis["vergleich"]["tage"] == 1
    # Heute und zwei Archivtage — der Monatsposten ist kein Tag.
    assert ergebnis["tage"] == 3


def test_gesamt_reihe_ist_lueckenlos_und_wird_ueber_zwei_jahre_jaehrlich():
    b = _bilanz()
    b._heute = {"datum": TAG, "slots": {}}
    b._tage = {
        "2026-05-10": _tagesergebnis(export_kwh=3.0),
        "2026-08-26": _tagesergebnis(export_kwh=4.0),
    }

    monate = b.einspeisung("gesamt", JETZT, _inputs())["reihe"]

    assert [m["monat"] for m in monate] == ["2026-05", "2026-06", "2026-07", "2026-08"]
    assert monate[1]["export"] == 0.0

    b._monate = {"2023-01": _tagesergebnis(export_kwh=50.0)}
    jahre = b.einspeisung("gesamt", JETZT, _inputs())["reihe"]

    assert [j["jahr"] for j in jahre] == ["2023", "2024", "2025", "2026"]
    assert jahre[0]["export"] == pytest.approx(50.0)
    assert jahre[3]["export"] == pytest.approx(7.0)
