"""Ladeziel zum Ende der PV-Zeit (schedule_ladeziel_pct).

Anlass: Grünbach, 04.10.2026. Seit OeMAG über dem Tagessatz der Gemeinschaft
lag, war eine gespeicherte Kilowattstunde nachts nichts mehr wert als
mittags, und der Fahrplan lud nur noch, was das Haus über Nacht braucht. Das
Ladeziel ist eine Vorgabe des Betreibers, die sich über Preise nicht sauber
ausdrücken lässt.

Was es nie darf — und was diese Tests deshalb festhalten:

* ausgeschaltet etwas verschieben (``test_aus_ist_identisch``),
* Strom aus dem Netz holen, um das Ziel zu erreichen,
* den Plan unlösbar machen (trüber Tag, Horizontende).
"""

import json
import pathlib
from datetime import datetime

import pytest

from custom_components.eeg_energy_optimizer import schedule as sched

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _inputs_gruenbach(ladeziel_pct: float = 0.0, pv_faktor: float = 1.0) -> sched.ScheduleInputs:
    """Echter Plan der Anlage Grünbach vom 04.10.2026, 20:00.

    Der Fall, in dem der Fahrplan ohne Ladeziel morgen nur bis ~85 % lud:
    Einspeisung praktisch flach bei 10,17 ct, Heizstab mit Wärmewert 12 ct.
    """
    daten = json.loads((FIXTURES / "fahrplan_gruenbach_2026-10-04.json").read_text(encoding="utf-8"))
    return sched.ScheduleInputs(
        start=datetime.fromisoformat(daten["start"]),
        time_res_s=900,
        timestamps=[datetime.fromisoformat(t) for t in daten["t"]],
        consumption_kw=daten["consumption_kw"],
        production_kw=[v * pv_faktor for v in daten["production_kw"]],
        min_production_kw=None,
        worst_case_factor=daten["worst_case_factor"],
        battery_free_kwh=daten["battery_free_kwh"],
        battery_capacity_kwh=daten["battery_capacity_kwh"],
        battery_power_limit_kw=daten["battery_power_limit_kw"],
        soc_pct=daten["soc_pct"],
        ac_limit_kw=daten["ac_limit_kw"],
        feedin_limit_kw=daten["feedin_limit_kw"],
        feedin_price=daten["feedin_price"],
        feedin_price_night=daten["feedin_price_night"],
        night_start_hour=daten["night_start_hour"],
        night_end_hour=daten["night_end_hour"],
        consumption_price=daten["consumption_price"],
        battery_cost=daten["battery_cost"],
        min_soc_pct=daten["min_soc_pct"],
        max_soc_pct=daten["max_soc_pct"],
        ladeziel_pct=ladeziel_pct,
        feedin_price_series=daten["feedin_price_series"],
        heizstab_max_kw=daten["heizstab_max_kw"],
        heizstab_waermewert=daten["heizstab_waermewert"],
        heizstab_budget_kwh=daten["heizstab_budget_kwh"],
    )


def _netzbezug_kwh(slots: list[dict]) -> float:
    return sum(max(-s["grid_p"], 0.0) for s in slots) * 0.25


# ---------------------------------------------------------------------------
# Einstellung lesen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("roh,erwartet", [
    (None, 0.0),        # nie gesetzt
    ("", 0.0),          # leeres Feld
    (0, 0.0),           # Panel speichert ein geleertes Zahlenfeld als 0
    (-5, 0.0),
    ("abc", 0.0),
    (30, 50.0),         # unter dem Mindestwert → hochgeklemmt
    (100, 100.0),
    (120, 100.0),
])
def test_ladeziel_aus_der_konfiguration(roh, erwartet):
    assert sched._ladeziel_pct({sched.CONF_SCHEDULE_LADEZIEL_PCT: roh}) == erwartet


def test_ladeziel_hoechstens_der_maximum_ladestand():
    config = {sched.CONF_SCHEDULE_LADEZIEL_PCT: 100, sched.CONF_SCHEDULE_MAX_SOC_PCT: 90}
    assert sched._ladeziel_pct(config) == 90.0


# ---------------------------------------------------------------------------
# Im Modell
# ---------------------------------------------------------------------------


def test_aus_ist_identisch():
    """Ladeziel aus muss Zeile für Zeile dasselbe liefern wie ein Modell, das
    die Methode gar nicht kennt — so wie Haralds eigene Konfiguration."""
    pd = pytest.importorskip("pandas")
    pytest.importorskip("highspy")
    from custom_components.eeg_energy_optimizer.chamo import opt_highs

    class OhneLadeziel(sched.HAConfig):
        ladeziel = None

    inputs = _inputs_gruenbach(ladeziel_pct=0.0)
    a = opt_highs.opt(sched.HAConfig(inputs), inputs.start)
    b = opt_highs.opt(OhneLadeziel(inputs), inputs.start)
    for spalte in ("grid_p", "battery_p", "battery", "battery_ub", "discard"):
        assert pd.Series(a[spalte]).astype(float).round(9).equals(
            pd.Series(b[spalte]).astype(float).round(9)
        ), f"Spalte {spalte} weicht ab"
    assert "ladeziel" not in sched.solve(inputs)


def test_ohne_ladeziel_wird_die_batterie_nicht_voll():
    """Die Ausgangslage, damit der nächste Test etwas beweist."""
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")
    slots = sched.solve(_inputs_gruenbach())["slots"]
    assert max(s["soc"] for s in slots) < 95.0


def test_ladeziel_wird_am_ende_der_pv_zeit_erreicht():
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")
    plan = sched.solve(_inputs_gruenbach(ladeziel_pct=100.0))
    ladeziel = plan["ladeziel"]
    assert ladeziel["ziel_pct"] == 100.0
    assert ladeziel["termine"], "kein Termin für das Ladeziel"
    for termin in ladeziel["termine"]:
        assert termin["geplant_pct"] >= 99.5, termin
        # Ende der PV-Zeit, nicht mittags
        assert datetime.fromisoformat(termin["t"]).hour >= 15, termin


def test_ladeziel_kauft_keinen_strom():
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")
    ohne = sched.solve(_inputs_gruenbach())["slots"]
    mit = sched.solve(_inputs_gruenbach(ladeziel_pct=100.0))["slots"]
    assert _netzbezug_kwh(mit) <= _netzbezug_kwh(ohne) + 0.05


def test_trueber_tag_ergibt_einen_plan_mit_dem_erreichbaren():
    """Ein Fünftel der Sonne: Das Ziel wird zu dem, was die PV schafft —
    der Plan bleibt lösbar und holt nichts aus dem Netz dafür."""
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")
    ohne = sched.solve(_inputs_gruenbach(pv_faktor=0.2))["slots"]
    plan = sched.solve(_inputs_gruenbach(ladeziel_pct=100.0, pv_faktor=0.2))
    assert plan["slots"]
    assert _netzbezug_kwh(plan["slots"]) <= _netzbezug_kwh(ohne) + 0.05
    termine = plan["ladeziel"]["termine"]
    assert termine and any(t["geplant_pct"] < 100.0 for t in termine)


def test_ziel_unter_dem_boden_ist_aus():
    inputs = _inputs_gruenbach(ladeziel_pct=15.0)   # Boden 20 %
    assert sched.HAConfig(inputs).ladeziel_kwh == 0.0


def test_ziel_wird_im_modellfenster_gerechnet():
    """Gezählt ab dem Mindest-Ladestand: 100 % bei Boden 20 % sind 80 % der
    Kapazität — genau das ganze Modellfenster."""
    config = sched.HAConfig(_inputs_gruenbach(ladeziel_pct=100.0))
    assert config.ladeziel_kwh == pytest.approx(config.battery_capacity)


# ---------------------------------------------------------------------------
# Wann das Ziel gilt
# ---------------------------------------------------------------------------


def _parameter(stempel, verbrauch=0.4, grenze=9.45):
    pd = pytest.importorskip("pandas")
    return pd.DataFrame(
        {"consumption": [verbrauch] * len(stempel), "feedin_limit": [grenze] * len(stempel)},
        index=stempel,
    )


def _tag(start: str, stunden: int, sonne_von: int, sonne_bis: int):
    """15-min-Raster mit Überschuss zwischen sonne_von und sonne_bis Uhr."""
    pd = pytest.importorskip("pandas")
    stempel = pd.date_range(start, periods=stunden * 4, freq="15min", tz="Europe/Vienna")
    werte = [3.0 if sonne_von <= t.hour < sonne_bis else -0.4 for t in stempel]
    return stempel, pd.Series(werte, index=stempel)


def test_ziel_sitzt_auf_dem_letzten_ueberschuss_slot_je_tag():
    stempel, ueberschuss = _tag("2026-10-05 00:00", 48, 9, 17)
    config = sched.HAConfig(_inputs_gruenbach(ladeziel_pct=100.0))
    ziel = config.ladeziel(_parameter(stempel), ueberschuss)
    gesetzt = [t for t, v in ziel.items() if v > 0]
    assert [(t.day, t.hour, t.minute) for t in gesetzt] == [(5, 16, 45), (6, 16, 45)]


def test_kein_ziel_wenn_der_horizont_mittags_endet():
    """Endet die Rechnung um 14:00, ist der letzte Überschuss-Slot das Ende der
    Rechnung, nicht das Ende der PV-Zeit — dort darf kein Ziel stehen."""
    stempel, ueberschuss = _tag("2026-10-05 14:00", 24, 9, 17)   # bis 06.10. 14:00
    config = sched.HAConfig(_inputs_gruenbach(ladeziel_pct=100.0))
    ziel = config.ladeziel(_parameter(stempel), ueberschuss)
    gesetzt = [t for t, v in ziel.items() if v > 0]
    assert [(t.day, t.hour, t.minute) for t in gesetzt] == [(5, 16, 45)]


def test_kein_ziel_das_die_endbedingung_unloesbar_macht():
    """opt() legt den Speicher am Horizontende auf halb fest. Ohne Einspeisung
    und mit kleiner Hauslast kommt ein voller Speicher in ein paar Stunden
    nicht mehr auf halb — dann bleibt das Ziel weg, statt das LP zu kippen."""
    stempel, ueberschuss = _tag("2026-10-05 06:00", 14, 9, 17)   # bis 20:00
    config = sched.HAConfig(_inputs_gruenbach(ladeziel_pct=100.0))
    ziel = config.ladeziel(_parameter(stempel, verbrauch=0.3, grenze=0.0), ueberschuss)
    assert float(ziel.sum()) == 0.0


def test_eine_wolke_ist_kein_ende_der_pv_zeit():
    pd = pytest.importorskip("pandas")
    stempel = pd.date_range("2026-10-05 00:00", periods=48 * 4, freq="15min", tz="Europe/Vienna")
    werte = [
        3.0 if (9 <= t.hour < 17 and not (t.hour == 12)) else -0.4
        for t in stempel
    ]
    ueberschuss = pd.Series(werte, index=stempel)
    config = sched.HAConfig(_inputs_gruenbach(ladeziel_pct=100.0))
    ziel = config.ladeziel(_parameter(stempel), ueberschuss)
    gesetzt = [(t.day, t.hour, t.minute) for t, v in ziel.items() if v > 0]
    assert gesetzt == [(5, 16, 45), (6, 16, 45)]
