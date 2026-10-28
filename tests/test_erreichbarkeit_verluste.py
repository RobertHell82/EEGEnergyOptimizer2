"""Die Erreichbarkeitsschranke rechnet mit Haralds gestuften Verlusten.

Anlass: Linz, 26.10.2026. Das Ladeziel (100 %) war ab etwa 10 Uhr nicht
mehr erreichbar; die Schranke kappte es auf das ohne Netzbezug Erreichbare
— gerechnet mit pauschal 2 · ``battery_resistance`` Verlust auf jede
Ladeleistung. Das LP verliert aber bis 0,1 C nichts, bis 0,2 C 4 % und erst
darüber 8 %. Die Schranke lag deshalb unter dem, was das LP tatsächlich
erreicht, und das LP speiste die Differenz ein: 0,18 kW in jedem sonnigen
Slot, 0,3 kWh je Plan bis zum Termin. Die Steuerung setzte dafür ein
Ladelimit auf die Planleistung statt freizugeben, und bei wechselhafter
Sonne ging jede Spitze darüber ins Netz — 1,9 kWh statt der geplanten 0,36,
die Batterie endete bei 81 %.

Festgehalten wird:

* die Umkehrfunktionen passen zur Verlustfunktion (Hin und zurück),
* der echte Plan von 12:30 speist vor dem Termin nichts mehr ein, und die
  Steuerung gibt im ersten Slot frei,
* die Schranke ist scharf: am Termin verlangt sie genau das Erreichbare,
  nicht weniger — und nirgends mehr (sonst wäre das LP unlösbar),
* über ein Raster aus Ladestand, Sonne, Ladeleistung und Ladeziel bleibt
  jeder Plan lösbar, kauft nichts zu und speist bei unerreichbarem Ziel
  vor dem Termin nur ein, was die Batterie nicht schneller aufnehmen kann.

Gegengeprüft gegen eine eigene Bisektion über die Verlustfunktion
(``_schritt_gestuft`` in test_schedule.py), nicht gegen die Umkehrfunktionen
des Codes selbst.
"""

import json
import pathlib
from datetime import datetime

import pytest

from custom_components.eeg_energy_optimizer import schedule as sched
from custom_components.eeg_energy_optimizer import schedule_executor as executor

from .test_ladeziel import _inputs_gruenbach
from .test_schedule import _schritt_gestuft

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _daten_linz() -> dict:
    return json.loads(
        (FIXTURES / "fahrplan_linz_2026-10-26_1230.json").read_text(encoding="utf-8")
    )


def _inputs_linz(
    pv_faktor: float = 1.0,
    soc_pct: float | None = None,
    leistung_kw: float | None = None,
    ladeziel_pct: float | None = None,
) -> sched.ScheduleInputs:
    """Echter Plan der Anlage Linz vom 26.10.2026, 12:30 — mit Stellschrauben."""
    d = _daten_linz()
    kapazitaet = d["battery_capacity_kwh"]
    soc = d["soc_pct"] if soc_pct is None else soc_pct
    frei = d["battery_free_kwh"] if soc_pct is None else kapazitaet * (1 - soc / 100.0)
    return sched.ScheduleInputs(
        start=datetime.fromisoformat(d["start"]),
        time_res_s=900,
        timestamps=[datetime.fromisoformat(t) for t in d["t"]],
        consumption_kw=d["consumption_kw"],
        production_kw=[v * pv_faktor for v in d["production_kw"]],
        min_production_kw=[v * pv_faktor for v in d["min_production_kw"]],
        worst_case_factor=d["worst_case_factor"],
        battery_free_kwh=frei,
        battery_capacity_kwh=kapazitaet,
        battery_power_limit_kw=d["battery_power_limit_kw"] if leistung_kw is None else leistung_kw,
        soc_pct=soc,
        ac_limit_kw=d["ac_limit_kw"],
        feedin_limit_kw=d["feedin_limit_kw"],
        feedin_price=d["feedin_price"],
        feedin_price_night=d["feedin_price_night"],
        night_start_hour=d["night_start_hour"],
        night_end_hour=d["night_end_hour"],
        consumption_price=d["consumption_price"],
        battery_cost=d["battery_cost"],
        min_soc_pct=d["min_soc_pct"],
        max_soc_pct=d["max_soc_pct"],
        ladeziel_pct=d["ladeziel_pct"] if ladeziel_pct is None else ladeziel_pct,
        eeg_bonus=d["eeg_bonus"],
    )


def _export_bis(slots: list[dict], termin: str) -> float:
    return sum(max(s["grid_p"], 0.0) for s in slots if s["t"] <= termin) * 0.25


def _bezug(slots: list[dict]) -> float:
    return sum(max(-s["grid_p"], 0.0) for s in slots) * 0.25


def _fenster(inputs: sched.ScheduleInputs) -> tuple[float, float]:
    """Modellfenster [Boden, Deckel] in kWh und der Startinhalt darin."""
    boden = inputs.battery_capacity_kwh * inputs.min_soc_pct / 100.0
    deckel = inputs.battery_capacity_kwh * inputs.max_soc_pct / 100.0
    kapazitaet = deckel - boden
    return kapazitaet, kapazitaet - (inputs.battery_free_kwh - (inputs.battery_capacity_kwh - deckel))


def _erreichbar(slots: list[dict], kapazitaet: float, start: float, grenze_kw: float) -> list[float]:
    """Stand am Ende jedes Slots, wenn jeder Überschuss geladen und jeder
    Fehlbetrag aus der Batterie gedeckt wird — unabhängig nachgerechnet."""
    stand, reihe = start, []
    for s in slots:
        bilanz = s["PV"] - s["consumption"] / sched.HAConfig.ac_efficiency
        schritt = min(_schritt_gestuft(bilanz, kapazitaet) * 0.25, grenze_kw * 0.25)
        stand = min(kapazitaet, max(0.0, stand + schritt))
        reihe.append(stand)
    return reihe


# ---------------------------------------------------------------------------
# Umkehrfunktionen gegen die Verlustfunktion
# ---------------------------------------------------------------------------


KAPAZITAETEN = [2.0, 5.0, 10.0, 14.2, 15.795, 30.0]
LEISTUNGEN = [0.0, 0.01, 0.5, 0.999, 1.0, 1.001, 1.5, 1.999, 2.0, 2.001, 2.5, 3.16, 4.0, 7.5, 12.0]


@pytest.mark.parametrize("kapazitaet", KAPAZITAETEN)
@pytest.mark.parametrize("p", LEISTUNGEN)
def test_laden_hin_und_zurueck(kapazitaet, p):
    """Batterieleistung p braucht p + Verluste(p) vom DC-Bus — und genau
    dieses Angebot muss wieder p ergeben, auf jeder Stufe und an den Kanten."""
    p = p * kapazitaet / 10.0
    angebot = p + sched._batterie_verluste_kw(p, kapazitaet)
    assert sched._ladeleistung_aus_dc(angebot, kapazitaet) == pytest.approx(p, abs=1e-9)


@pytest.mark.parametrize("kapazitaet", KAPAZITAETEN)
@pytest.mark.parametrize("p", LEISTUNGEN)
def test_entladen_hin_und_zurueck(kapazitaet, p):
    p = p * kapazitaet / 10.0
    bedarf = p - sched._batterie_verluste_kw(p, kapazitaet)
    assert sched._entladeleistung_fuer_dc(bedarf, kapazitaet) == pytest.approx(p, abs=1e-9)


@pytest.mark.parametrize("kapazitaet", KAPAZITAETEN)
def test_umkehrung_stimmt_mit_der_bisektion_ueberein(kapazitaet):
    """Zwei unabhängige Wege, ein Ergebnis — Laden wie Entladen."""
    for i in range(-300, 301):
        bilanz = i * kapazitaet / 1000.0
        erwartet = _schritt_gestuft(bilanz, kapazitaet)
        if bilanz > 0:
            code = sched._ladeleistung_aus_dc(bilanz, kapazitaet)
        else:
            code = -sched._entladeleistung_fuer_dc(-bilanz, kapazitaet)
        assert code == pytest.approx(erwartet, abs=1e-7), bilanz


@pytest.mark.parametrize("kapazitaet", KAPAZITAETEN)
def test_laden_ist_nie_schlechter_als_die_pauschale(kapazitaet):
    """Die alte Schranke war die untere Abschätzung: Mit gestuften Verlusten
    kommt aus demselben Angebot nie weniger in die Batterie — und unter
    0,1 C sogar alles."""
    pauschal = 1 - 2 * sched.HAConfig.battery_resistance
    for i in range(1, 301):
        angebot = i * kapazitaet / 1000.0
        gestuft = sched._ladeleistung_aus_dc(angebot, kapazitaet)
        assert gestuft >= angebot * pauschal - 1e-12
        assert gestuft <= angebot + 1e-12
        if angebot <= kapazitaet / 10.0:
            assert gestuft == pytest.approx(angebot)


# ---------------------------------------------------------------------------
# Der echte Fall: Linz, 26.10.2026, 12:30
# ---------------------------------------------------------------------------


def test_archivierter_plan_speiste_vor_dem_termin_ein():
    """Ausgangslage aus dem Archiv, damit die Tests danach etwas beweisen."""
    archiv = _daten_linz()["archiv"]
    assert archiv["termin"]["geplant_pct"] < 80.0
    assert archiv["export_bis_termin_kwh"] > 0.25
    assert archiv["erster_slot"]["grid_p"] > executor._ALLES_LADEN_EXPORT_KW


def test_plan_speist_vor_dem_unerreichbaren_ziel_nichts_mehr_ein():
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")
    plan = sched.solve(_inputs_linz())
    termin = plan["ladeziel"]["termine"][0]
    assert termin["geplant_pct"] < 99.0, "Ziel muss unerreichbar sein, sonst prüft der Test nichts"
    assert _export_bis(plan["slots"], termin["t"]) <= 0.01
    # Die frei gewordene Energie landet in der Batterie, nicht im Nichts.
    assert termin["geplant_pct"] > _daten_linz()["archiv"]["termin"]["geplant_pct"] + 1.0
    assert _bezug(plan["slots"]) == pytest.approx(0.0, abs=0.01)


def test_steuerung_gibt_im_ersten_slot_frei():
    """Der eigentliche Schaden kam aus der Ausführung: ein Ladelimit auf die
    Planleistung. Ohne geplante Einspeisung gibt der Executor frei, und der
    Gen24 lädt jede Sonnenspitze selbst."""
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")
    plan = sched.solve(_inputs_linz())
    jetzt = datetime.fromisoformat(plan["slots"][0]["t"])
    absicht = executor.plan_action(plan, jetzt)
    assert absicht is not None
    assert absicht.kind == "release"
    assert absicht.reason == executor.RELEASE_GRUND_ALLES_LADEN


def test_schranke_ist_am_termin_scharf():
    """Am Termin verlangt die Schranke genau das ohne Zukauf Erreichbare.
    Läge sie darunter, bliebe wieder ein Rest, den das LP verkauft; läge
    sie darüber, wäre das LP unlösbar."""
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")
    inputs = _inputs_linz()
    plan = sched.solve(inputs)
    slots = plan["slots"]
    kapazitaet, start = _fenster(inputs)
    reihe = _erreichbar(slots, kapazitaet, start, inputs.battery_power_limit_kw)
    termin = plan["ladeziel"]["termine"][0]["t"]
    i = next(k for k, s in enumerate(slots) if s["t"] == termin)
    gefordert = kapazitaet - slots[i]["battery_ub"]
    assert gefordert == pytest.approx(reihe[i], abs=0.02)
    for k, s in enumerate(slots):
        assert kapazitaet - s["battery_ub"] <= reihe[k] + 0.01, s["t"]


# ---------------------------------------------------------------------------
# Raster: lösbar, kein Zukauf, kein Verkauf vor einem unerreichbaren Ziel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ladeziel", [0.0, 100.0])
# Ab 0,2 C des Modellfensters (14,2 kWh → 2,84 kW): darunter ist Haralds
# Obergrenze für battery_high2_p negativ und das Modell baut nicht — ein
# eigener Befund, unabhängig von der Schranke.
@pytest.mark.parametrize("leistung", [2.9, 4.0, 5.0])
@pytest.mark.parametrize("soc", [10.0, 30.0, 55.0, 85.0, 99.0])
@pytest.mark.parametrize("pv_faktor", [0.2, 0.6, 1.0, 1.6])
def test_raster_linz(pv_faktor, soc, leistung, ladeziel):
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")
    inputs = _inputs_linz(pv_faktor=pv_faktor, soc_pct=soc, leistung_kw=leistung, ladeziel_pct=ladeziel)
    plan = sched.solve(inputs)
    slots = plan["slots"]
    assert slots, plan.get("error")

    # Nie mehr verlangt als erreichbar (sonst wäre das LP unlösbar geworden).
    kapazitaet, start = _fenster(inputs)
    reihe = _erreichbar(slots, kapazitaet, start, leistung)
    for k, s in enumerate(slots):
        assert kapazitaet - s["battery_ub"] <= reihe[k] + 0.01, s["t"]

    # Kein Zukauf, den der Plan ohne Ladeziel nicht auch hätte.
    if ladeziel:
        ohne = sched.solve(_inputs_linz(pv_faktor=pv_faktor, soc_pct=soc, leistung_kw=leistung, ladeziel_pct=0.0))
        assert _bezug(slots) <= _bezug(ohne["slots"]) + 0.05

    # Unerreichbares Ziel: vor dem Termin geht nur ins Netz, was die Batterie
    # mit ihrer Leistung nicht schneller aufnimmt.
    for termin in (plan.get("ladeziel") or {}).get("termine") or []:
        if termin["geplant_pct"] >= inputs.max_soc_pct - 0.5:
            continue
        for s in slots:
            if s["t"] > termin["t"]:
                break
            if s["grid_p"] > 0.01:
                assert -s["battery_p"] >= leistung - 0.05, (
                    f"{s['t']}: speist {s['grid_p']:.2f} kW ein, lädt nur "
                    f"{-s['battery_p']:.2f} von {leistung:.1f} kW, Ziel {termin}"
                )


@pytest.mark.parametrize("pv_faktor", [0.2, 0.5, 1.0])
def test_raster_gruenbach(pv_faktor):
    """Zweite Anlage, mit Heizstab im Modell: dieselben Eigenschaften."""
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")
    inputs = _inputs_gruenbach(ladeziel_pct=100.0, pv_faktor=pv_faktor)
    plan = sched.solve(inputs)
    slots = plan["slots"]
    assert slots, plan.get("error")
    ohne = sched.solve(_inputs_gruenbach(pv_faktor=pv_faktor))["slots"]
    assert _bezug(slots) <= _bezug(ohne) + 0.05
    kapazitaet, start = _fenster(inputs)
    reihe = _erreichbar(slots, kapazitaet, start, inputs.battery_power_limit_kw)
    for k, s in enumerate(slots):
        assert kapazitaet - s["battery_ub"] <= reihe[k] + 0.01, s["t"]
