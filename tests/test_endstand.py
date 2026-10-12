"""Haralds Endstand (halb voll am Horizontende) nur, soweit die PV hinkommt.

``opt()`` legt den letzten Slot auf halben Inhalt fest. Startet die Batterie
darunter und reicht die Sonne in 48 Stunden nicht bis dorthin, war das LP
unlösbar: kein Fahrplan, Failsafe — an jedem dunklen Wintertag mit leerer
Batterie. Mit erlaubtem Netzladen wäre es lösbar gewesen, aber nur, indem der
Plan Strom kauft, damit die Endbedingung stimmt (gemessen am Grünbach-Plan
vom 04.10.2026: 5,8 kWh). chamo/README.md § 4.
"""

import dataclasses

import pytest

from custom_components.eeg_energy_optimizer import schedule as sched

from .test_ladeziel import _inputs_gruenbach

pytest.importorskip("pandas")
pytest.importorskip("highspy")


def _inputs(pv_faktor: float, soc: float) -> sched.ScheduleInputs:
    inputs = _inputs_gruenbach(pv_faktor=pv_faktor)
    kapazitaet = inputs.battery_capacity_kwh
    return dataclasses.replace(
        inputs, soc_pct=soc, battery_free_kwh=kapazitaet * (1 - soc / 100)
    )


def _tabelle(inputs, config_cls=sched.HAConfig):
    from custom_components.eeg_energy_optimizer.chamo import opt_highs

    return opt_highs.opt(config_cls(inputs), inputs.start)


def _netzladung_kwh(tabelle) -> float:
    """Was die Batterie über den PV-Überschuss hinaus lädt."""
    ueberschuss = (tabelle["PV"] - tabelle["consumption"] / 0.95).clip(lower=0)
    ladung = (-tabelle["battery_p"]).clip(lower=0)
    return float((ladung - ueberschuss).clip(lower=0).sum() * 0.25)


class _MitNetzladen(sched.HAConfig):
    no_grid_charging = False


@pytest.mark.parametrize("pv_faktor", [0.0, 0.05])
def test_dunkler_horizont_mit_leerer_batterie_ergibt_einen_plan(pv_faktor):
    """Vorher: ``Solver-Status: infeasible``."""
    tabelle = _tabelle(_inputs(pv_faktor, soc=25))
    assert len(tabelle) > 0


@pytest.mark.parametrize("pv_faktor", [0.0, 0.05])
def test_endstand_kauft_keinen_strom_zum_laden(pv_faktor):
    """Mit erlaubtem Netzladen und Einheitspreis lohnt sich kein Zukauf —
    vorher kaufte der Plan 5,8 kWh nur für den halben Endstand."""
    tabelle = _tabelle(_inputs(pv_faktor, soc=25), _MitNetzladen)
    assert _netzladung_kwh(tabelle) < 0.05


@pytest.mark.parametrize("pv_faktor,soc", [(1.0, 25), (1.0, 60), (0.2, 25), (0.2, 60), (0.05, 60)])
def test_erreichbarer_endstand_bleibt_halb(pv_faktor, soc):
    """Wo die PV bis halb reicht, ist der Endstand Haralds."""
    inputs = _inputs(pv_faktor, soc)
    tabelle = _tabelle(inputs)
    kapazitaet = sched.HAConfig(inputs).battery_capacity
    assert float(tabelle["battery"].iloc[-1]) == pytest.approx(kapazitaet / 2, abs=1e-6)
