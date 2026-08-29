"""Tests für die Energiebilanz — was die PV bringt, was davon die Optimierung ist.

Der wichtigste Test steht am Ende: Fährt die Anlage Standardbetrieb, MUSS der
ausgewiesene Optimierungs-Vorteil gegen null gehen. Er ist die eingebaute
Selbstprüfung des Verfahrens — jede Abweichung dort ist Modellfehler.
"""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.eeg_energy_optimizer import bilanz as bilanz_modul
from custom_components.eeg_energy_optimizer.bilanz import (
    SLOT_SEKUNDEN,
    EnergieBilanz,
)
from custom_components.eeg_energy_optimizer.schedule import ScheduleInputs

TAG = "2026-08-27"


def _inputs(**ueberschreiben):
    """Fahrplan-Inputs, wie sie die Bewertung braucht (Traun-nahe Zahlen)."""
    start = datetime(2026, 8, 27, 0, 0)
    basis = dict(
        start=start,
        time_res_s=SLOT_SEKUNDEN,
        timestamps=[start],
        consumption_kw=[0.4],
        production_kw=[0.0],
        min_production_kw=None,
        worst_case_factor=1.0,
        battery_free_kwh=7.5,
        battery_capacity_kwh=15.0,
        battery_power_limit_kw=5.0,
        soc_pct=50.0,
        ac_limit_kw=10.0,
        feedin_limit_kw=9.5,
        feedin_price=0.06146,
        feedin_price_night=None,
        night_start_hour=20,
        night_end_hour=6,
        consumption_price=0.26,
        battery_cost=0.01,
        min_soc_pct=5.0,
    )
    basis.update(ueberschreiben)
    return ScheduleInputs(**basis)


@pytest.fixture(autouse=True)
def _echte_zeitquelle(monkeypatch):
    """``homeassistant.util.dt`` ist in den Tests ein MagicMock — dessen
    ``strftime`` liefert keinen String, und der Tageswechsel liefe ins Leere.
    Hier gilt deshalb: lokale Zeit = die uebergebene Zeit."""
    monkeypatch.setattr(bilanz_modul, "_jetzt_lokal", lambda now_utc: now_utc)


def _bilanz(config=None):
    """EnergieBilanz ohne Home Assistant — nur die Rechenwege werden geprüft."""
    b = EnergieBilanz.__new__(EnergieBilanz)
    b._hass = None
    b._entry_id = "test"
    b._config = config or {}
    b._store = None
    b._heute = {"datum": TAG, "slots": {}}
    b._offen = []
    b._tage = {}
    b._monate = {}
    b._letzter_takt_utc = None
    b._dirty = False
    b._erster_takt = True
    b._quellen = None
    return b


def _slot(**werte):
    slot = bilanz_modul._leerer_slot()
    slot.update(werte)
    return slot


def _tag_mit(slots: dict[int, dict], datum: str = TAG) -> dict:
    return {"datum": datum, "slots": {str(k): v for k, v in slots.items()}}


# ---------------------------------------------------------------------------
# Aufzeichnung
# ---------------------------------------------------------------------------


def test_energie_wird_als_rechteck_gebucht():
    b = _bilanz()
    b._erster_takt = False
    b._letzter_takt_utc = None
    now = datetime(2026, 8, 27, 12, 5, tzinfo=timezone.utc)

    # 2 kW Einspeisung über 30 Sekunden = 0,0167 kWh
    b._summiere(
        "48",
        {"pv": 3.0, "haus": 1.0, "netz": 2.0, "batterie": 0.0, "soc": 61.0},
        30.0,
        "Ein",
        None,
        now,
    )
    slot = b._heute["slots"]["48"]

    assert slot["export"] == pytest.approx(2.0 * 30 / 3600)
    assert slot["bezug"] == 0.0
    assert slot["pv"] == pytest.approx(3.0 * 30 / 3600)
    assert slot["soc_a"] == 61.0 and slot["soc_e"] == 61.0
    assert slot["ein_s"] == 30.0


def test_netzbezug_und_entladung_landen_in_eigenen_feldern():
    b = _bilanz()
    now = datetime(2026, 8, 27, 21, 0, tzinfo=timezone.utc)
    b._summiere(
        "84",
        {"pv": 0.0, "haus": 0.6, "netz": -0.6, "batterie": -1.5, "soc": 40.0},
        60.0,
        "Aus",
        None,
        now,
    )
    slot = b._heute["slots"]["84"]

    assert slot["bezug"] == pytest.approx(0.6 / 60)
    assert slot["export"] == 0.0
    assert slot["entladen"] == pytest.approx(1.5 / 60)
    assert slot["laden"] == 0.0
    # Modus Aus zählt Zeit, aber nicht als Ein-Zeit.
    assert slot["s"] == 60.0 and slot["ein_s"] == 0.0


async def test_grosse_luecke_wird_nicht_hochgerechnet():
    """Nach einem Neustart darf kein Takt eine Stunde Energie erfinden."""
    b = _bilanz()
    b._erster_takt = False
    b._letzter_takt_utc = datetime(2026, 8, 27, 10, 0, tzinfo=timezone.utc)
    b._lies_messwerte = lambda: {
        "pv": 5.0, "haus": 1.0, "netz": 4.0, "batterie": 0.0, "soc": 60.0
    }

    # Eine ganze Stunde spaeter — weit ueber MAX_TAKT_SEKUNDEN.
    await b.async_update("Ein", datetime(2026, 8, 27, 11, 0, tzinfo=timezone.utc))

    assert b._heute["slots"] == {}, "Die Luecke darf keine Energie erzeugen"

    # Der naechste normale Takt zaehlt dann wieder.
    await b.async_update("Ein", datetime(2026, 8, 27, 11, 0, 30, tzinfo=timezone.utc))

    assert b._heute["slots"], "Nach der Luecke muss es normal weitergehen"


# ---------------------------------------------------------------------------
# Tageswechsel
# ---------------------------------------------------------------------------


async def test_tag_wartet_auf_die_fahrplan_daten():
    """Nach einem Neustart ueber Mitternacht fehlen die Tarife noch.

    Der Vortag darf dann NICHT unbewertet ins Archiv wandern — sonst fehlt
    ihm dauerhaft der Einspeiseerloes. Er wartet, bis die Inputs da sind.
    """
    b = _bilanz()
    b._erster_takt = False
    b._letzter_takt_utc = None
    b._heute = _tag_mit(
        {48: _slot(export=2.0, basis=0.10, kwp=0.26, s=900.0)}, datum="2026-08-26"
    )
    b._lies_messwerte = lambda: {
        "pv": 0.0, "haus": 0.2, "netz": -0.2, "batterie": 0.0, "soc": 50.0
    }

    # Erster Takt am neuen Tag, noch ohne Fahrplan-Inputs.
    await b.async_update("Ein", datetime(2026, 8, 27, 0, 0, 30, tzinfo=timezone.utc), None)

    assert "2026-08-26" not in b._tage, "Ohne Tarife darf nichts archiviert werden"
    assert len(b._offen) == 1, "Der Vortag muss warten"
    assert b._heute["datum"] == "2026-08-27", "Der neue Tag laeuft trotzdem"

    # Eine Minute spaeter steht der Fahrplan.
    await b.async_update(
        "Ein", datetime(2026, 8, 27, 0, 1, 30, tzinfo=timezone.utc), _inputs()
    )

    assert b._offen == [], "Jetzt ist der Vortag abgeschlossen"
    assert b._tage["2026-08-26"]["erloes"] == pytest.approx(0.20, abs=1e-3), (
        "Mit Tarifen muss der Einspeiseerloes drinstehen"
    )


async def test_wartender_tag_wird_nach_zwei_tagen_notgeschlossen():
    """Kommt nie ein Fahrplan, darf die Warteschlange nicht ewig wachsen."""
    b = _bilanz()
    b._erster_takt = False
    b._letzter_takt_utc = None
    b._offen = [
        _tag_mit({48: _slot(haus=1.0, kwp=0.26, s=900.0)}, datum="2026-08-20")
    ]
    b._lies_messwerte = lambda: None

    await b.async_update("Ein", datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc), None)

    assert b._offen == []
    assert "2026-08-20" in b._tage
    # Der gemessene Teil steht auch ohne Tarife.
    assert b._tage["2026-08-20"]["vermieden"] == pytest.approx(0.26)


def test_wartende_tage_werden_mitgespeichert():
    """Sonst waere der wartende Tag nach dem naechsten Neustart weg —
    und der Neustart ist gerade der Grund, warum er wartet."""
    quelle = (
        __import__("pathlib").Path(bilanz_modul.__file__)
    ).read_text(encoding="utf-8")

    assert '"offen": self._offen' in quelle, "offen fehlt beim Speichern"
    assert 'stored.get("offen")' in quelle, "offen fehlt beim Laden"


# ---------------------------------------------------------------------------
# Ersparnis durch PV
# ---------------------------------------------------------------------------


def test_eigenverbrauch_wird_mit_dem_bezugspreis_bewertet():
    """Was nicht aus dem Netz kam, hätte gekauft werden müssen."""
    b = _bilanz()
    # Eine Viertelstunde: 1 kWh Hauslast, davon 0,25 kWh aus dem Netz.
    tag = _tag_mit({
        48: _slot(pv=2.0, haus=1.0, bezug=0.25, export=0.75, kwp=0.26, s=900.0),
    })

    ergebnis = b.bewerte_tag(tag, None)

    assert ergebnis["eigen_kwh"] == pytest.approx(0.75)
    assert ergebnis["vermieden"] == pytest.approx(0.75 * 0.26)
    # Ohne Inputs gibt es keinen Einspeiseerlös — nur der gemessene Teil.
    assert ergebnis["pv_ersparnis"] == pytest.approx(0.75 * 0.26)


def test_batterieentladung_zaehlt_als_eigenverbrauch():
    """Strom aus der Batterie war vorher PV — er zählt zur Ersparnis."""
    b = _bilanz()
    tag = _tag_mit({
        84: _slot(pv=0.0, haus=0.5, bezug=0.0, entladen=0.55, kwp=0.26, s=900.0),
    })

    ergebnis = b.bewerte_tag(tag, None)

    assert ergebnis["eigen_kwh"] == pytest.approx(0.5)
    assert ergebnis["vermieden"] == pytest.approx(0.5 * 0.26)


def test_einspeiseerloes_kommt_aus_der_geldfunktion():
    b = _bilanz()
    tag = _tag_mit({
        48: _slot(pv=4.0, haus=0.0, export=1.0, kwp=0.26, basis=0.08, s=900.0),
    })

    ergebnis = b.bewerte_tag(tag, _inputs())

    # 1 kWh zum eingefrorenen Basistarif von 8 ct.
    assert ergebnis["erloes"] == pytest.approx(0.08, abs=1e-3)
    assert ergebnis["export_kwh"] == pytest.approx(1.0)


def test_eingefrorener_preis_schlaegt_den_aktuellen():
    """Eine spätere Tarifänderung schreibt die Vergangenheit nicht um."""
    b = _bilanz()
    tag = _tag_mit({
        48: _slot(export=1.0, basis=0.20, kwp=0.26, s=900.0),
    })

    # Inputs sagen 6,146 ct — der Slot hat 20 ct eingefroren.
    ergebnis = b.bewerte_tag(tag, _inputs(feedin_price=0.06146))

    assert ergebnis["erloes"] == pytest.approx(0.20, abs=1e-3)


# ---------------------------------------------------------------------------
# Ersparnis durch die Optimierung
# ---------------------------------------------------------------------------


def _tagesreihe_standardbetrieb(inputs) -> dict:
    """Ein Tag, der WIRKLICH Standardbetrieb ist.

    Von Hand nachgebaut wäre er es nicht: Beim ersten Versuch lud die Reihe
    0,6 kWh je Viertelstunde aus 0,225 kWh verfügbarem Überschuss — die
    Selbstprüfung hat das sofort als knappen Euro Abweichung gemeldet. Deshalb
    erzeugt hier ``simuliere_standardbetrieb`` selbst die Reihe, die dann als
    „gemessen" in die Bilanz geht. Das prüft zugleich die Umrechnung in
    ``_als_slots`` in beide Richtungen: Vorzeichen, Einheiten, Ladestand.
    """
    from custom_components.eeg_energy_optimizer.schedule import (
        simuliere_standardbetrieb,
    )

    stunden = SLOT_SEKUNDEN / 3600.0
    roh = []
    for i in range(96):
        stunde = i // 4
        pv = 1.0 if 9 <= stunde < 15 else 0.0
        haus = 0.25 if 18 <= stunde < 23 else 0.1
        roh.append({
            "t": f"{TAG}T{(i * 15) // 60:02d}:{(i * 15) % 60:02d}:00",
            "PV": pv,
            "consumption": haus,
        })

    gefahren = simuliere_standardbetrieb(roh, inputs)

    slots: dict[int, dict] = {}
    vorheriger_soc = float(inputs.soc_pct)
    for i, (basis_slot, ergebnis) in enumerate(zip(roh, gefahren)):
        netz = ergebnis["grid_p"]
        batterie = ergebnis["battery_p"]
        slots[i] = _slot(
            pv=basis_slot["PV"] * stunden,
            haus=basis_slot["consumption"] * stunden,
            export=max(netz, 0.0) * stunden,
            bezug=max(-netz, 0.0) * stunden,
            entladen=max(batterie, 0.0) * stunden,
            laden=max(-batterie, 0.0) * stunden,
            kwp=0.26,
            basis=0.06146,
            s=900.0,
            ein_s=0.0,
            soc_a=round(vorheriger_soc, 1),
            soc_e=ergebnis["soc"],
        )
        vorheriger_soc = ergebnis["soc"]
    return _tag_mit(slots)


def test_standardbetrieb_ergibt_praktisch_keinen_vorteil():
    """DIE Selbstprüfung: Ohne Steuerung darf kein Vorteil ausgewiesen werden.

    Der Tag ist so aufgezeichnet, wie ihn ein Gerät ohne Vorausschau fährt.
    Die Referenzsimulation bildet genau das nach — Ist und Referenz müssen
    zusammenfallen. Was übrig bleibt, ist Modellfehler, und der muss klein
    gegen den Tagesumsatz sein.
    """
    b = _bilanz()
    inputs = _inputs()
    tag = _tagesreihe_standardbetrieb(inputs)

    ergebnis = b.bewerte_tag(tag, inputs)

    assert ergebnis["opt_vorteil"] is not None
    assert abs(ergebnis["opt_vorteil"]) < 0.02, (
        "Ohne Steuerung darf kein nennenswerter Optimierungs-Vorteil "
        f"entstehen, ausgewiesen wurden {ergebnis['opt_vorteil']} EUR"
    )


def test_abendeinspeisung_zum_hoeheren_satz_bringt_vorteil():
    """Wer einspeist, wenn es mehr wert ist, muss besser dastehen.

    Derselbe Energieinhalt, aber abends ins Netz statt mittags — bei einem
    Nachtsatz über dem Tagsatz muss der Vorteil positiv sein.
    """
    b = _bilanz()
    slots: dict[int, dict] = {}
    for i in range(96):
        stunde = i // 4
        if 9 <= stunde < 15:
            # PV läuft, aber es wird NICHT geladen: alles geht ins Netz …
            slots[i] = _slot(
                pv=1.0, haus=0.1, export=0.85, kwp=0.26, basis=0.06,
                s=900.0, ein_s=900.0, soc_a=50.0, soc_e=50.0,
            )
        elif 20 <= stunde < 23:
            # … abends wird die Batterie ins Netz entladen, zum Nachtsatz.
            slots[i] = _slot(
                pv=0.0, haus=0.1, entladen=1.0, export=0.85, kwp=0.26,
                basis=0.12, s=900.0, ein_s=900.0, soc_a=50.0, soc_e=45.0,
            )
        else:
            slots[i] = _slot(
                pv=0.0, haus=0.1, bezug=0.1, kwp=0.26, basis=0.06,
                s=900.0, ein_s=900.0, soc_a=50.0, soc_e=50.0,
            )
    ergebnis = b.bewerte_tag(_tag_mit(slots), _inputs())

    assert ergebnis["opt_vorteil"] is not None
    assert ergebnis["opt_vorteil"] > 0, (
        "Abendeinspeisung zum höheren Satz muss einen Vorteil ergeben"
    )


def test_ohne_ladestand_kein_vorteil_sondern_none():
    """Ohne Start-Ladestand ist die Referenz nicht rechenbar — dann kein Wert.

    Lieber gar keine Zahl als eine erfundene: Der Sensor zeigt dann
    „nicht verfügbar" statt einer Null, die wie ein Messwert aussähe.
    """
    b = _bilanz()
    tag = _tag_mit({48: _slot(export=1.0, basis=0.08, kwp=0.26, s=900.0)})

    ergebnis = b.bewerte_tag(tag, _inputs())

    assert ergebnis["opt_vorteil"] is None
    # Der gemessene Teil steht trotzdem.
    assert ergebnis["pv_ersparnis"] > 0


# ---------------------------------------------------------------------------
# Archiv
# ---------------------------------------------------------------------------


def test_tagesabschluss_schreibt_tag_und_monat_fort():
    b = _bilanz()
    b._heute = _tag_mit(
        {48: _slot(pv=2.0, haus=1.0, bezug=0.25, export=0.75, kwp=0.26, s=900.0)}
    )

    b._archiviere(b._heute, None)

    assert TAG in b._tage
    assert b._tage[TAG]["vermieden"] == pytest.approx(0.75 * 0.26)
    assert b._monate["2026-08"]["vermieden"] == pytest.approx(0.75 * 0.26)


def test_zweiter_tag_addiert_sich_im_monat():
    b = _bilanz()
    for datum in ("2026-08-26", "2026-08-27"):
        b._heute = _tag_mit(
            {48: _slot(haus=1.0, bezug=0.0, kwp=0.26, s=900.0)}, datum=datum
        )
        b._archiviere(b._heute, None)

    assert b._monate["2026-08"]["vermieden"] == pytest.approx(2 * 0.26)
    assert b.summe("vermieden", monat="2026-08") == pytest.approx(2 * 0.26)
    assert b.summe("vermieden", jahr="2026") == pytest.approx(2 * 0.26)


def test_alte_tage_fallen_raus_monatssummen_bleiben():
    b = _bilanz()
    alt = (datetime.now() - timedelta(days=bilanz_modul.TAGE_ROH + 10)).date()
    b._tage[alt.isoformat()] = {"vermieden": 1.0}
    b._monate[alt.strftime("%Y-%m")] = {"vermieden": 1.0}

    b._verdichte_alte_tage()

    assert alt.isoformat() not in b._tage
    assert b._monate[alt.strftime("%Y-%m")]["vermieden"] == 1.0


# ---------------------------------------------------------------------------
# Sonderfaelle der Bewertung
# ---------------------------------------------------------------------------


def test_netzgeladene_energie_zaehlt_nicht_als_ersparnis():
    """Aus dem Netz geladener Strom ist keine PV-Ersparnis.

    Sonst waere er doppelt gerechnet: einmal als Netzbezug bezahlt, einmal
    beim Entladen als vermiedener Einkauf gutgeschrieben.
    """
    b = _bilanz()
    # Nachts 2 kWh aus dem Netz in die Batterie, danach 2 kWh ins Haus.
    tag = _tag_mit({
        8: _slot(haus=0.1, bezug=2.1, laden=2.0, kwp=0.26, s=900.0),
        40: _slot(haus=2.0, bezug=0.0, entladen=2.0, kwp=0.26, s=900.0),
    })

    ergebnis = b.bewerte_tag(tag, None)

    # Ohne Abzug staenden hier 2,0 kWh Eigenverbrauch — sie kamen aber
    # aus dem Netz und sind laengst bezahlt.
    assert ergebnis["eigen_kwh"] == pytest.approx(0.0, abs=0.01)
    assert ergebnis["vermieden"] == pytest.approx(0.0, abs=0.01)


def test_normaler_tag_bleibt_vom_netzlade_abzug_unberuehrt():
    """Der Abzug darf nur greifen, wenn wirklich aus dem Netz geladen wurde."""
    b = _bilanz()
    tag = _tag_mit({
        48: _slot(pv=3.0, haus=1.0, laden=1.5, export=0.4, kwp=0.26, s=900.0),
    })

    ergebnis = b.bewerte_tag(tag, None)

    assert ergebnis["eigen_kwh"] == pytest.approx(1.0)
    assert ergebnis["vermieden"] == pytest.approx(0.26)


def test_modus_anteil_wird_nicht_ueber_den_monat_summiert():
    """ein_anteil ist ein Anteil — 30 Tage lang addiert ergaebe er 30."""
    b = _bilanz()
    for tag_nr in (25, 26, 27):
        b._heute = _tag_mit(
            {48: _slot(haus=1.0, kwp=0.26, s=900.0, ein_s=900.0)},
            datum=f"2026-08-{tag_nr}",
        )
        b._archiviere(b._heute, None)

    assert b._tage["2026-08-27"]["ein_anteil"] == pytest.approx(1.0)
    assert "ein_anteil" not in b._monate["2026-08"]
    # Die Geldfelder summieren sich weiterhin.
    assert b._monate["2026-08"]["vermieden"] == pytest.approx(3 * 0.26)


# ---------------------------------------------------------------------------
# Begruendung eines negativen Optimierungs-Vorteils
# ---------------------------------------------------------------------------

from custom_components.eeg_energy_optimizer.bilanz import (  # noqa: E402
    begruende_vorteil,
    vorteil_details,
)


def test_vorteil_details_haben_das_vorzeichen_des_beitrags():
    """Kosten werden negiert: mehr Bezug im Ist = negativer Beitrag."""
    ist = {"erloes": 1.0, "bezug": 0.8, "alterung": 0.1, "endbestand": 0.5}
    ref = {"erloes": 1.2, "bezug": 0.3, "alterung": 0.05, "endbestand": 0.9}
    d = vorteil_details(ist, ref)
    assert d == {"erloes": -0.2, "bezug": -0.5, "alterung": -0.05, "endbestand": -0.4}


def test_begruendung_nennt_den_groessten_posten_zuerst():
    d = {"erloes": -0.05, "bezug": -0.30, "alterung": 0.0, "endbestand": -0.10}
    saetze = begruende_vorteil(d, ein_anteil=1.0, abgeschlossen=True)
    assert saetze[0].startswith("Mehr Netzbezug")
    assert "0,30 €" in saetze[0]
    assert saetze[1].startswith("Am Tagesende weniger Energie")
    assert saetze[2].startswith("Weniger Einspeiseerlös")
    # generischer Schlusssatz immer dabei
    assert "ohne Vorausschau" in saetze[-1]


def test_laufender_tag_beginnt_mit_zwischenstand():
    d = {"erloes": 0.0, "bezug": -0.2, "alterung": 0.0, "endbestand": 0.0}
    saetze = begruende_vorteil(d, ein_anteil=1.0, abgeschlossen=False)
    assert saetze[0].startswith("Zwischenstand")


def test_rauschen_wird_als_solches_benannt():
    """Unter 2 Cent je Posten gibt es keinen Schuldigen — dann steht das da."""
    d = {"erloes": -0.01, "bezug": -0.01, "alterung": 0.0, "endbestand": 0.0}
    saetze = begruende_vorteil(d, ein_anteil=1.0, abgeschlossen=True)
    assert any("Messungenauigkeit" in t for t in saetze)
    assert not any("Netzbezug" in t for t in saetze)


def test_teilweise_gesteuerter_tag_wird_erwaehnt():
    d = {"erloes": 0.0, "bezug": -0.2, "alterung": 0.0, "endbestand": 0.0}
    saetze = begruende_vorteil(d, ein_anteil=0.4, abgeschlossen=True)
    assert any("nur 40 % des Tages" in t for t in saetze)


def test_bewerte_tag_liefert_begruendung_nur_bei_negativem_vorteil(monkeypatch):
    """Integration: der Tageswert traegt Details immer, die Begruendung nur
    wenn der Vorteil negativ ist."""
    b = _bilanz()
    tag = {
        "datum": "2026-08-27",
        "slots": {
            "40": {"pv": 0.0, "export": 0.0, "bezug": 0.5, "haus": 0.5, "laden": 0.0,
                   "entladen": 0.0, "soc_a": 50.0, "soc_e": 50.0, "s": 900, "ein_s": 900,
                   "kwp": 0.26, "basis": 0.06},
        },
    }
    # Referenz kuenstlich besser stellen, damit der Vorteil sicher negativ ist.
    monkeypatch.setattr(
        b, "_optimierungs_vorteil",
        lambda *a, **k: (-0.5, {"erloes": 0.0, "bezug": 0.0, "alterung": 0.0,
                                "endbestand": 0.5, "summe": 0.5}),
    )
    ergebnis = b.bewerte_tag(tag, _inputs(), abgeschlossen=False)
    assert ergebnis["opt_vorteil"] == -0.5
    assert ergebnis["vorteil_details"] is not None
    assert ergebnis["vorteil_begruendung"]
    assert ergebnis["vorteil_begruendung"][0].startswith("Zwischenstand")

    monkeypatch.setattr(
        b, "_optimierungs_vorteil",
        lambda *a, **k: (0.3, {"erloes": 0.0, "bezug": 0.0, "alterung": 0.0,
                               "endbestand": 0.0, "summe": 0.0}),
    )
    ergebnis = b.bewerte_tag(tag, _inputs())
    assert ergebnis["vorteil_begruendung"] is None
    assert ergebnis["vorteil_details"] is not None
