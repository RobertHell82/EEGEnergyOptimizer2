"""Tests für die Gewinnberechnung (schedule.py).

Drei Teile: die Greedy-Referenz „Standardbetrieb" (lädt zuerst, speist den
Rest ein, entlädt bis zum Mindest-Ladestand), die gemeinsame Bewertung mit
ECHTEN Geldflüssen (Anteile, Tag/Nacht, Spotreihe, Alterung,
Endbestands-Gutschrift) und der Plausibilitätstest über den ganzen Solver:
bei identischen Preisen rund um die Uhr gibt es nichts zu verschieben, der
Vorteil muss dann ungefähr null sein.
"""

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.eeg_energy_optimizer import eeg_price
from custom_components.eeg_energy_optimizer import schedule as sched

TZ = timezone(timedelta(hours=2))  # Sommerzeit Wien, ausreichend für die Tests
NOW = datetime(2026, 8, 24, 5, 7, tzinfo=TZ)  # Montag, krumme Minute
MITTAG = NOW.replace(hour=12, minute=0)       # sicher außerhalb des Nachtfensters


def _inputs(**over) -> sched.ScheduleInputs:
    """Minimale Inputs für die reinen Rechenfunktionen (kein pandas nötig)."""
    basis = dict(
        start=NOW,
        time_res_s=900,
        timestamps=[NOW],
        consumption_kw=[0.5],
        production_kw=[0.0],
        min_production_kw=None,
        worst_case_factor=0.6,
        battery_free_kwh=6.0,
        battery_capacity_kwh=10.0,
        battery_power_limit_kw=5.0,
        soc_pct=40.0,
        ac_limit_kw=10.0,
        feedin_limit_kw=9.0,
        feedin_price=0.06,
        feedin_price_night=None,
        night_start_hour=22,
        night_end_hour=6,
        consumption_price=0.25,
        battery_cost=0.01,
        min_soc_pct=10.0,
    )
    basis.update(over)
    return sched.ScheduleInputs(**basis)


def _slot(start: datetime, minuten: int, **felder) -> dict:
    slot = {"t": (start + timedelta(minutes=minuten)).isoformat()}
    slot.update(felder)
    return slot


EFF = sched.HAConfig.ac_efficiency  # 0.95 — dieselbe Physik wie im Modell


# ---------------------------------------------------------------------------
# Greedy-Referenz „Standardbetrieb"
# ---------------------------------------------------------------------------


def test_standardbetrieb_laedt_zuerst_und_speist_den_rest_ein():
    """Überschuss geht in die Batterie, nur was die Leistungsgrenze nicht
    mehr nimmt, wird eingespeist."""
    inputs = _inputs(soc_pct=40.0, battery_power_limit_kw=3.0)
    # PV 6 kW, Hauslast 0,95 kW AC = 1 kW DC → 5 kW Überschuss
    slots = [_slot(MITTAG, 0, PV=6.0, consumption=0.95)]

    ref = sched.simuliere_standardbetrieb(slots, inputs)

    assert ref[0]["battery_p"] == pytest.approx(-3.0)          # lädt am Limit
    assert ref[0]["grid_p"] == pytest.approx((5.0 - 3.0) * EFF)
    # 3 kW über eine Viertelstunde = 0,75 kWh auf 10 kWh → +7,5 Punkte
    assert ref[0]["soc"] == pytest.approx(47.5)


def test_standardbetrieb_speist_alles_ein_wenn_die_batterie_voll_ist():
    inputs = _inputs(soc_pct=100.0)
    slots = [_slot(MITTAG, 0, PV=6.0, consumption=0.95)]

    ref = sched.simuliere_standardbetrieb(slots, inputs)

    assert ref[0]["battery_p"] == pytest.approx(0.0)
    assert ref[0]["grid_p"] == pytest.approx(5.0 * EFF)
    assert ref[0]["soc"] == pytest.approx(100.0)


def test_standardbetrieb_entlaedt_bis_zum_min_soc_dann_netz():
    """Der Mindest-Ladestand ist die Grenze: darunter kommt alles aus dem
    Netz. Der Übergang darf auch mitten im Slot liegen."""
    # 10,5 % bei 10 kWh und Boden 10 % → noch 0,05 kWh = 0,2 kW je Viertelstunde
    inputs = _inputs(soc_pct=10.5)
    slots = [
        _slot(MITTAG, 0, PV=0.0, consumption=0.95),
        _slot(MITTAG, 15, PV=0.0, consumption=0.95),
    ]

    ref = sched.simuliere_standardbetrieb(slots, inputs)

    # Erster Slot: Restenergie deckt nur einen Teil, der Rest ist Bezug.
    assert ref[0]["battery_p"] == pytest.approx(0.2)
    assert ref[0]["grid_p"] == pytest.approx(-(1.0 - 0.2) * EFF)
    assert ref[0]["soc"] == pytest.approx(10.0)
    # Zweiter Slot: Boden erreicht, alles aus dem Netz.
    assert ref[1]["battery_p"] == pytest.approx(0.0)
    assert ref[1]["grid_p"] == pytest.approx(-1.0 * EFF)
    assert ref[1]["soc"] == pytest.approx(10.0)


def test_standardbetrieb_respektiert_die_einspeisegrenze():
    """Was über die Grenze hinausgeht, wird abgeregelt — wie im LP."""
    inputs = _inputs(soc_pct=100.0, feedin_limit_kw=4.0)
    slots = [_slot(MITTAG, 0, PV=12.0, consumption=0.95)]

    ref = sched.simuliere_standardbetrieb(slots, inputs)

    assert ref[0]["grid_p"] == pytest.approx(4.0)


def test_standardbetrieb_hat_die_slotstruktur_des_fahrplans():
    inputs = _inputs()
    slots = [_slot(MITTAG, i * 15, PV=1.0, consumption=0.5) for i in range(4)]

    ref = sched.simuliere_standardbetrieb(slots, inputs)

    assert [s["t"] for s in ref] == [s["t"] for s in slots]
    for slot in ref:
        assert set(slot) == {"t", "grid_p", "battery_p", "soc"}


# ---------------------------------------------------------------------------
# Bewertung mit echten Geldflüssen
# ---------------------------------------------------------------------------


def _viertel(stamp) -> int:
    return int(stamp.timestamp() // 900)


def test_bewertung_gewichtet_die_anteile_bei_gedecktem_bedarf():
    """50 % Gemeinschaft, 50 % Basistarif — je Tageszeit der richtige Satz.

    Der Gemeinschaftssatz fließt nur, weil der Saldo der Viertelstunde den
    angebotenen Anteil deckt — die Zuteilung ist bedarfsbegrenzt.
    """
    tag = _slot(MITTAG, 0, grid_p=4.0, battery_p=0.0, soc=10.0)
    nacht = _slot(NOW.replace(hour=23, minute=0), 0, grid_p=4.0, battery_p=0.0, soc=10.0)
    inputs = _inputs(
        feedin_price=0.06146,
        feedin_price_night=0.05,
        eeg_tarife=[{"name": "Pucking", "anteil": 0.5, "tag": 0.092, "nacht": 0.112}],
        eeg_bedarf={"Pucking": {
            _viertel(MITTAG): 100.0,
            _viertel(NOW.replace(hour=23, minute=0)): 100.0,
        }},
        min_soc_pct=10.0,
    )

    ergebnis = sched.bewerte_geldfluesse([tag, nacht], inputs)

    erwartet = (0.5 * 0.092 + 0.5 * 0.06146) + (0.5 * 0.112 + 0.5 * 0.05)
    assert ergebnis["erloes"] == pytest.approx(erwartet, abs=1e-4)
    assert ergebnis["bezug"] == 0.0
    assert ergebnis["alterung"] == 0.0
    assert ergebnis["endbestand"] == 0.0  # Ende genau am Mindest-Ladestand
    # 0,5 kWh je Slot angeboten und voll aufgenommen; 2 kWh gesamt exportiert.
    assert ergebnis["eeg_kwh"] == pytest.approx(1.0)
    assert ergebnis["export_kwh"] == pytest.approx(2.0)


def test_bewertung_ueberschuss_der_gemeinschaft_faellt_zum_basistarif():
    """Hat die Gemeinschaft selbst Überschuss (Saldo negativ), nimmt sie
    nichts auf — der Export wird komplett zum Basistarif vergütet. Genau der
    Fall Mittagsexport im Standardbetrieb."""
    slot = _slot(MITTAG, 0, grid_p=4.0, battery_p=0.0, soc=10.0)
    inputs = _inputs(
        feedin_price=0.06146,
        eeg_tarife=[{"name": "Pucking", "anteil": 0.5, "tag": 0.092, "nacht": 0.112}],
        eeg_bedarf={"Pucking": {_viertel(MITTAG): -30.0}},
    )

    ergebnis = sched.bewerte_geldfluesse([slot], inputs)

    assert ergebnis["erloes"] == pytest.approx(1.0 * 0.06146, abs=1e-4)
    assert ergebnis["eeg_kwh"] == 0.0


def test_bewertung_bedarf_deckelt_die_zuteilung():
    """Kleiner Bedarf: nur der aufgenommene Teil bekommt den Gemeinschafts-
    satz, der Rest des Anteils fällt zum Basistarif durch."""
    slot = _slot(MITTAG, 0, grid_p=4.0, battery_p=0.0, soc=10.0)
    inputs = _inputs(
        feedin_price=0.06,
        eeg_tarife=[{"name": "Pucking", "anteil": 0.5, "tag": 0.10, "nacht": 0.10}],
        # 1 kWh Export, 0,5 kWh angeboten, aber nur 0,2 kWh Bedarf.
        eeg_bedarf={"Pucking": {_viertel(MITTAG): 0.2}},
    )

    ergebnis = sched.bewerte_geldfluesse([slot], inputs)

    erwartet = 0.2 * 0.10 + 0.3 * 0.06 + 0.5 * 0.06
    assert ergebnis["erloes"] == pytest.approx(erwartet)
    assert ergebnis["eeg_kwh"] == pytest.approx(0.2)


def test_bewertung_ohne_saldodaten_gilt_der_basistarif():
    """Fehlende Bedarfsprognose darf keinen erfundenen EEG-Erlös erzeugen —
    dieselbe Regel wie in der Preisfunktion der Steuerung."""
    slot = _slot(MITTAG, 0, grid_p=4.0, battery_p=0.0, soc=10.0)
    inputs = _inputs(
        feedin_price=0.06146,
        eeg_tarife=[{"name": "Pucking", "anteil": 0.5, "tag": 0.092, "nacht": 0.112}],
        eeg_bedarf=None,
    )

    ergebnis = sched.bewerte_geldfluesse([slot], inputs)

    assert ergebnis["erloes"] == pytest.approx(1.0 * 0.06146, abs=1e-4)
    assert ergebnis["eeg_kwh"] == 0.0


def test_bewertung_nutzt_die_spotreihe_auch_negativ():
    """Bei Quelle Spot gilt je Slot der letzte Stützpunkt — und ein negativer
    Börsenpreis zählt wirklich negativ."""
    inputs = _inputs(
        timestamps=[MITTAG, MITTAG + timedelta(minutes=30)],
        feedin_price_series=[0.10, -0.05],
        eeg_tarife=None,
    )
    slots = [
        _slot(MITTAG, 0, grid_p=4.0, battery_p=0.0, soc=10.0),
        _slot(MITTAG, 30, grid_p=4.0, battery_p=0.0, soc=10.0),
        # Jenseits des letzten Stützpunkts gilt dieser weiter.
        _slot(MITTAG, 45, grid_p=4.0, battery_p=0.0, soc=10.0),
    ]

    ergebnis = sched.bewerte_geldfluesse(slots, inputs)

    assert ergebnis["erloes"] == pytest.approx(1.0 * 0.10 - 1.0 * 0.05 - 1.0 * 0.05)


def test_bewertung_netzbezug_und_alterung():
    """Bezug × Bezugspreis; Alterung zählt nur die Entladung — wie in Haralds
    Zielfunktion kostet jeder Zyklus einmal, nicht doppelt."""
    inputs = _inputs(consumption_price=0.25, battery_cost=0.01)
    slots = [
        # 2 kW Entladung, 1 kW Bezug über eine Viertelstunde
        _slot(MITTAG, 0, grid_p=-1.0, battery_p=2.0, soc=10.0),
        # Laden erzeugt keine Alterungskosten (sonst zählte der Zyklus doppelt)
        _slot(MITTAG, 15, grid_p=0.0, battery_p=-4.0, soc=20.0),
    ]

    ergebnis = sched.bewerte_geldfluesse(slots, inputs)

    assert ergebnis["bezug"] == pytest.approx(0.25 * 0.25)      # 0,25 kWh
    assert ergebnis["alterung"] == pytest.approx(0.5 * 0.01)    # 0,5 kWh entladen
    assert ergebnis["erloes"] == 0.0


def test_endbestands_gutschrift_bewertet_die_restenergie():
    """Restenergie über dem Mindest-Ladestand × Basistarif, als eigene Zeile.

    Ohne sie verglichen wir ungleiche Endzustände — die bekannte Falle aus
    Horizont- und Deckel-Messung.
    """
    inputs = _inputs(feedin_price=0.06, min_soc_pct=10.0, battery_capacity_kwh=10.0)
    slots = [_slot(MITTAG, 0, grid_p=0.0, battery_p=0.0, soc=60.0)]

    ergebnis = sched.bewerte_geldfluesse(slots, inputs)

    assert ergebnis["rest_kwh"] == pytest.approx(5.0)           # (60−10) % von 10 kWh
    assert ergebnis["endbestand"] == pytest.approx(5.0 * 0.06)
    assert ergebnis["summe"] == pytest.approx(ergebnis["endbestand"])


def test_echte_tarife_lassen_die_gewichtung_weg():
    """Die Gewichtung ist ein Steuersignal ohne Geldfluss — für die
    Gewinnberechnung zählen nur die echten Sätze."""
    config = {
        "peakshare_community": "Pucking",
        "peakshare_share_pct": 50,
        "peakshare_price": 0.092,
        "peakshare_price_night": 0.112,
        "peakshare_weight": 0.01,
        # Zweite Gemeinschaft ohne echten Satz: fließt kein Geld, fällt raus —
        # auch wenn die Gewichtung sie für die STEUERUNG am Leben hielte.
        "peakshare_community_2": "NurSignal",
        "peakshare_share_pct_2": 30,
        "peakshare_price_2": 0,
        "peakshare_weight_2": 0.01,
    }

    tarife = eeg_price.echte_tarife_aus_config(config)

    assert tarife == [
        {"name": "Pucking", "anteil": 0.5, "tag": 0.092, "nacht": 0.112}
    ]
    # Leeres Nachtfeld heißt: derselbe Satz wie am Tag.
    config["peakshare_price_night"] = ""
    assert eeg_price.echte_tarife_aus_config(config)[0]["nacht"] == 0.092
    # PeakShare aus → keine Geldflüsse an Gemeinschaften.
    assert eeg_price.echte_tarife_aus_config({**config, "enable_peakshare": False}) == []


# ---------------------------------------------------------------------------
# Der ganze Weg durch den Solver
# ---------------------------------------------------------------------------


def _profile_coordinator(watts_per_hour: float = 600.0):
    from unittest.mock import MagicMock

    coordinator = MagicMock()
    coordinator.hourly_avg = {
        day: {hour: watts_per_hour for hour in range(24)}
        for day in ("mo", "di", "mi", "do", "fr", "sa", "so")
    }
    coordinator.hourly_for = lambda stamp: watts_per_hour
    return coordinator


def _wh_hours(start: datetime, hours: int = 48) -> dict[str, float]:
    result = {}
    cursor = start.replace(minute=0, second=0, microsecond=0)
    for offset in range(hours):
        stamp = cursor + timedelta(hours=offset)
        hour = stamp.hour
        wh = 0.0 if hour < 6 or hour > 20 else 6000.0 * (1 - abs(13 - hour) / 7)
        result[stamp.isoformat()] = round(max(0.0, wh), 1)
    return result


def _inputs_for_solve(horizon_hours: int = 36) -> sched.ScheduleInputs:
    stamps = sched._grid_timestamps(NOW, hours=horizon_hours)
    return sched.ScheduleInputs(
        start=NOW.replace(minute=0),
        time_res_s=900,
        timestamps=stamps,
        consumption_kw=sched._consumption_from_profile(_profile_coordinator(600), stamps),
        production_kw=sched._production_from_wh(_wh_hours(NOW, horizon_hours + 2), stamps),
        min_production_kw=None,
        worst_case_factor=0.6,
        battery_free_kwh=7.5,
        battery_capacity_kwh=12.5,
        battery_power_limit_kw=5.0,
        soc_pct=40.0,
        ac_limit_kw=10.0,
        feedin_limit_kw=9.5,
        feedin_price=0.0973,
        feedin_price_night=None,
        night_start_hour=22,
        night_end_hour=6,
        consumption_price=0.2620,
        battery_cost=0.01,
        min_soc_pct=10.0,
        forecast_source="solcast_solar",
    )


def test_solve_liefert_referenz_und_gewinn():
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")

    result = sched.solve(_inputs_for_solve())

    ref = result["referenz_slots"]
    assert [s["t"] for s in ref] == [s["t"] for s in result["slots"]]
    gewinn = result["gewinn"]
    for seite in ("mit", "ohne"):
        for feld in ("erloes", "bezug", "alterung", "endbestand", "rest_kwh",
                     "eeg_kwh", "export_kwh", "summe"):
            assert feld in gewinn[seite], f"'{feld}' fehlt in gewinn['{seite}']"
    assert gewinn["vorteil"] == pytest.approx(
        gewinn["mit"]["summe"] - gewinn["ohne"]["summe"], abs=1e-3
    )
    assert gewinn["horizont_h"] == pytest.approx(36.0, abs=0.5)
    # Die Referenz hält dieselben Grenzen wie der Plan.
    assert min(s["soc"] for s in ref) >= 10.0 - 0.1
    assert max(s["soc"] for s in ref) <= 100.0


def test_gewinn_wird_auch_bei_quelle_spot_gerechnet():
    """Spotpreis ohne Gemeinschaft: die Bewertung folgt der Börsenreihe je
    Slot. Bei deutlicher Nacht-Spreizung verschiebt die Optimierung den
    Export in die teuren Stunden — der Vorteil muss klar positiv sein."""
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")

    basis = _inputs_for_solve()
    serie = [0.15 if s.hour >= 20 or s.hour < 6 else 0.05 for s in basis.timestamps]
    inputs = dataclasses.replace(
        basis,
        feedin_price_series=serie,
        feedin_price=sum(serie) / len(serie),
        eeg_tarife=None,
        eeg_bedarf=None,
    )

    gewinn = sched.solve(inputs)["gewinn"]

    assert gewinn["mit"]["eeg_kwh"] == 0.0   # keine Gemeinschaft im Spiel
    assert gewinn["vorteil"] > 0, f"Spreizung ungenutzt: {gewinn}"


def test_gewinn_ist_ungefaehr_null_bei_identischen_preisen():
    """Plausibilität: bei identischen Preisen rund um die Uhr gibt es nichts
    zu verschieben — ein deutlich positiver „Gewinn" wäre ein Rechenfehler
    (etwa eine Bewertungslücke zwischen den Plänen). Klein negativ ist
    erlaubt: das LP modelliert Zusatzverluste bei hoher Leistung, die die
    Greedy-Referenz bewusst nicht kennt.
    """
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")

    inputs = _inputs_for_solve()  # ein Preis, kein Nachtsatz, keine Gemeinschaft
    result = sched.solve(inputs)

    gewinn = result["gewinn"]
    umsatz = max(abs(gewinn["mit"]["summe"]), abs(gewinn["ohne"]["summe"]), 0.01)
    assert abs(gewinn["vorteil"]) <= max(0.05, 0.03 * umsatz), (
        f"Vorteil {gewinn['vorteil']} € bei flachen Preisen — "
        f"mit {gewinn['mit']}, ohne {gewinn['ohne']}"
    )


def test_bewertung_nutzt_das_eigene_nachtfenster_der_gemeinschaft():
    """Gemeinschafts-Nachtsatz nach dem EEG-Fenster, Basistarif nach dem
    Standard-Fenster — die beiden Verträge können verschiedene Nächte haben."""
    inputs = _inputs(
        feedin_price=0.08,
        feedin_price_night=0.06,        # Standard-Fenster 22–06
        night_start_hour=22,
        night_end_hour=6,
        eeg_night_start_hour=20,        # Gemeinschafts-Fenster 20–05
        eeg_night_end_hour=5,
        eeg_tarife=[{"name": "Pucking", "anteil": 0.5, "tag": 0.10, "nacht": 0.12}],
        eeg_bedarf={"Pucking": {
            _viertel(NOW.replace(hour=21, minute=0)): 100.0,
            _viertel(NOW.replace(hour=23, minute=0)): 100.0,
        }},
    )
    # 21 Uhr: Gemeinschaft nachts (0,12), Basis am Tag (0,08).
    um21 = _slot(NOW.replace(hour=21, minute=0), 0, grid_p=4.0, battery_p=0.0, soc=10.0)
    # 23 Uhr: beide nachts — Gemeinschaft 0,12, Basis 0,06.
    um23 = _slot(NOW.replace(hour=23, minute=0), 0, grid_p=4.0, battery_p=0.0, soc=10.0)

    ergebnis = sched.bewerte_geldfluesse([um21, um23], inputs)

    erwartet = (0.5 * 0.12 + 0.5 * 0.08) + (0.5 * 0.12 + 0.5 * 0.06)
    assert ergebnis["erloes"] == pytest.approx(erwartet, abs=1e-4)


def test_bewertung_ohne_eeg_fenster_gilt_das_standardfenster():
    """None heißt: wie das Standard-Fenster — Bestandsverhalten."""
    inputs = _inputs(
        feedin_price=0.08,
        night_start_hour=22,
        night_end_hour=6,
        eeg_tarife=[{"name": "Pucking", "anteil": 1.0, "tag": 0.10, "nacht": 0.12}],
        eeg_bedarf={"Pucking": {_viertel(NOW.replace(hour=23, minute=0)): 100.0}},
    )
    slot = _slot(NOW.replace(hour=23, minute=0), 0, grid_p=4.0, battery_p=0.0, soc=10.0)

    ergebnis = sched.bewerte_geldfluesse([slot], inputs)

    assert ergebnis["erloes"] == pytest.approx(1.0 * 0.12, abs=1e-4)


def test_gemeinschaft_mit_reinem_nachttarif_zaehlt():
    """Tagfeld leer (Panel schickt 0), nur ein Nachtsatz — die Gemeinschaft
    darf nicht herausfallen, sonst fehlt ihr echter Geldfluss komplett."""
    config = {
        "peakshare_community": "NurNachts",
        "peakshare_share_pct": 100,
        "peakshare_price": 0,          # leeres Panel-Feld
        "peakshare_price_night": 0.15,
        "peakshare_weight": 0,
    }

    tarife = eeg_price.echte_tarife_aus_config(config)
    assert tarife and tarife[0]["nacht"] == pytest.approx(0.15)

    # Und die Steuerung sieht sie ebenfalls (sonst verschiebt der Fahrplan
    # die Einspeisung gar nicht in die Nachtstunden).
    assert eeg_price.gemeinschaften_aus_config(config)


def test_deckel_kappt_echte_boersenpreise_nicht():
    """Der Deckel soll Scheinhandel verhindern, nicht die Börse zensieren.

    Bei Quelle Spot lag der Abendpreis über dem Bezugspreis; der Deckel
    machte 42, 35 und 25 ct für das Modell ununterscheidbar, während die
    Gewinnbewertung weiter mit dem echten Preis rechnete.
    """
    pytest.importorskip("pandas")

    stamps = [MITTAG + timedelta(minutes=30 * i) for i in range(4)]
    inputs = _inputs(
        timestamps=stamps,
        consumption_kw=[0.5] * 4,
        production_kw=[0.0] * 4,
        consumption_price=0.247,
        feedin_price_series=[0.42, 0.35, 0.25, 0.10],
        feedin_price=0.28,
    )
    reihe = sched.HAConfig(inputs).feedin_price(stamps[0])

    werte = [round(float(v), 4) for v in list(reihe)[:4]]
    assert werte == [0.42, 0.35, 0.25, 0.10], f"Börsenpreise gekappt: {werte}"
