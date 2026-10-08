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
    (None, 100.0),      # nie gesetzt → Vorgabe
    ("", 100.0),        # leer → Vorgabe
    ("abc", 100.0),     # unlesbar → Vorgabe
    (0, 0.0),           # Panel speichert ein geleertes Zahlenfeld als 0 → aus
    (-5, 0.0),
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


def _termine(config, parameter, ueberschuss):
    return [
        (ueberschuss.index[i].day, ueberschuss.index[i].hour, ueberschuss.index[i].minute)
        for i in config.ladeziel_termine(parameter, ueberschuss)
    ]


def _tag(start: str, stunden: int, sonne_von: int, sonne_bis: int):
    """15-min-Raster mit Überschuss zwischen sonne_von und sonne_bis Uhr."""
    pd = pytest.importorskip("pandas")
    stempel = pd.date_range(start, periods=stunden * 4, freq="15min", tz="Europe/Vienna")
    werte = [3.0 if sonne_von <= t.hour < sonne_bis else -0.4 for t in stempel]
    return stempel, pd.Series(werte, index=stempel)


def test_ziel_sitzt_auf_dem_letzten_ueberschuss_slot_je_tag():
    stempel, ueberschuss = _tag("2026-10-05 00:00", 48, 9, 17)
    config = sched.HAConfig(_inputs_gruenbach(ladeziel_pct=100.0))
    assert _termine(config, _parameter(stempel), ueberschuss) == [(5, 16, 45), (6, 16, 45)]


def test_kein_ziel_wenn_der_horizont_mittags_endet():
    """Endet die Rechnung um 14:00, ist der letzte Überschuss-Slot das Ende der
    Rechnung, nicht das Ende der PV-Zeit — dort darf kein Ziel stehen."""
    stempel, ueberschuss = _tag("2026-10-05 14:00", 24, 9, 17)   # bis 06.10. 14:00
    config = sched.HAConfig(_inputs_gruenbach(ladeziel_pct=100.0))
    assert _termine(config, _parameter(stempel), ueberschuss) == [(5, 16, 45)]


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
    assert _termine(config, _parameter(stempel), ueberschuss) == [(5, 16, 45), (6, 16, 45)]


# ---------------------------------------------------------------------------
# Untergrenze vor dem Termin: das Ziel auch im schlechten Fall
# ---------------------------------------------------------------------------
#
# Anlage Schweiz, 07.10.2026: Die Prognose versprach 29 kWh, der Plan speiste
# nachts und vormittags ~8 kWh ein und wollte mittags nachladen. Es kamen
# 21 kWh, abends 77 % statt 100. Vor dem Termin muss die Batterie deshalb so
# viel halten, wie sie braucht, wenn nur der vorsichtige Pfad eintrifft.


def _parameter_vorsichtig(stempel, ueberschuss, faktor, verbrauch=0.4):
    """Hand-Parameter mit min_production = faktor × Erwartung."""
    pd = pytest.importorskip("pandas")
    eff = sched.HAConfig.ac_efficiency
    produktion = [max(0.0, float(u) + verbrauch / eff) if u > 0 else 0.0 for u in ueberschuss.values]
    return pd.DataFrame(
        {
            "consumption": [verbrauch] * len(stempel),
            "feedin_limit": [9.45] * len(stempel),
            "min_production": [p * faktor for p in produktion],
        },
        index=stempel,
    )


def test_untergrenze_steigt_rueckwaerts_bis_zum_termin():
    stempel, ueberschuss = _tag("2026-10-05 00:00", 24, 9, 17)
    config = sched.HAConfig(_inputs_gruenbach(ladeziel_pct=100.0))
    ziel = config.ladeziel(_parameter_vorsichtig(stempel, ueberschuss, 0.3), ueberschuss)
    (termin,) = config.ladeziel_termine(_parameter(stempel), ueberschuss)
    assert ziel.iloc[termin] == pytest.approx(config.ladeziel_kwh)
    # Nachts nimmt das Haus aus der Batterie: vorwärts fallend. Tagsüber lädt
    # selbst der vorsichtige Pfad: vorwärts steigend bis zum Termin.
    nacht = [v for t, v in ziel.items() if t.hour < 9]
    tag = [v for k, (t, v) in enumerate(ziel.items()) if t.hour >= 9 and k <= termin]
    assert all(a >= b - 1e-9 for a, b in zip(nacht, nacht[1:]))
    assert all(a <= b + 1e-9 for a, b in zip(tag, tag[1:]))
    assert ziel.iloc[0] > 0
    assert max(ziel) <= config.battery_capacity + 1e-9


def test_viel_sicherer_ueberschuss_verlangt_vorher_nichts():
    """Lädt schon der vorsichtige Pfad das Ziel mehrfach voll, bleibt die
    Nacht frei — an klaren Tagen ändert die Untergrenze nichts."""
    stempel, ueberschuss = _tag("2026-10-05 00:00", 24, 9, 17)
    config = sched.HAConfig(_inputs_gruenbach(ladeziel_pct=100.0))
    ziel = config.ladeziel(_parameter_vorsichtig(stempel, ueberschuss, 1.0), ueberschuss)
    nachts = [v for t, v in ziel.items() if t.hour < 9]
    assert max(nachts) == 0.0


def test_schwacher_vorsichtiger_pfad_verlangt_mehr():
    stempel, ueberschuss = _tag("2026-10-05 00:00", 24, 9, 17)
    config = sched.HAConfig(_inputs_gruenbach(ladeziel_pct=100.0))
    gut = config.ladeziel(_parameter_vorsichtig(stempel, ueberschuss, 0.6), ueberschuss)
    schlecht = config.ladeziel(_parameter_vorsichtig(stempel, ueberschuss, 0.2), ueberschuss)
    assert all(s >= g - 1e-9 for s, g in zip(schlecht, gut))
    assert float(schlecht.sum()) > float(gut.sum())


class _NurTermin(sched.HAConfig):
    """Das Ladeziel bis 2.1.38: Untergrenze nur am Termin."""

    def ladeziel(self, parameters, ueberschuss):
        pd = pytest.importorskip("pandas")
        ziel = pd.Series(0.0, index=ueberschuss.index)
        for i in self.ladeziel_termine(parameters, ueberschuss):
            ziel.iloc[i] = self.ladeziel_kwh
        return ziel


def _batterie_vor_pv_kwh(table, kapazitaet: float) -> float:
    """Batterieinhalt im ersten Slot mit PV-Überschuss (kWh im Modellfenster).

    Die Spalte ``battery`` ist der FREIE Platz (battery_free), nicht der Inhalt.
    """
    for _, row in table.iterrows():
        if float(row["PV"]) > float(row["consumption"]):
            return kapazitaet - float(row["battery"])
    raise AssertionError("kein Überschuss im Horizont")


def test_im_plan_haelt_die_batterie_vor_der_sonne_mehr():
    """Gegen das alte Ladeziel: Bei vorsichtigem Pfad (60 % der Erwartung)
    geht morgens nicht weniger in den Tag, und Netzbezug kommt keiner dazu."""
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")
    from custom_components.eeg_energy_optimizer.chamo import opt_highs

    for faktor in (1.0, 0.6, 0.4):
        inputs = _inputs_gruenbach(ladeziel_pct=100.0, pv_faktor=faktor)
        kap = sched.HAConfig(inputs).battery_capacity
        alt = opt_highs.opt(_NurTermin(inputs), inputs.start)
        neu = opt_highs.opt(sched.HAConfig(inputs), inputs.start)
        assert _batterie_vor_pv_kwh(neu, kap) >= _batterie_vor_pv_kwh(alt, kap) - 1e-3, faktor
        netz_alt = float((-alt["grid_p"]).clip(lower=0).sum()) * 0.25
        netz_neu = float((-neu["grid_p"]).clip(lower=0).sum()) * 0.25
        assert netz_neu <= netz_alt + 0.05, faktor


def test_termine_in_der_anzeige_bleiben_die_pv_enden():
    """Die Anzeige zeigt Termine, nicht jeden Slot mit Untergrenze."""
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")
    termine = sched.solve(_inputs_gruenbach(ladeziel_pct=100.0))["ladeziel"]["termine"]
    assert 1 <= len(termine) <= 2
    for termin in termine:
        assert datetime.fromisoformat(termin["t"]).hour >= 15, termin
