"""Replay-Korpus: echte, archivierte Fahrpläne gegen feste Regeln.

Jeder Plan in ``tests/fixtures/replay/`` stammt aus dem Fahrplan-Archiv
einer Anlage (aufgenommen mit ``scripts/replay_korpus.py aufnehmen``) und
wird hier mit dem heutigen Code neu gerechnet — einmal wie archiviert und
einmal je Variante, die Funktionen auf die echten Reihen schaltet, die es
damals nicht gab oder die dort aus waren (Netzladen, Mindestladung,
Spitzenreserve, dunkler Tag, Heizstab).

Geprüft wird kein Sollwert, sondern was immer gelten muss
(``replay_korpus.verstoesse``): lösbar, Ladestand im Band, Einspeise- und
Netzgrenzen, kein Netzladen ohne Freigabe, kein Kauf und Verkauf zugleich,
Heizstab nur aus PV-Überschuss und im Tagesbudget, Mindestladung am Termin.
Ein exakter Planvergleich wäre wertlos: Das LP hat oft mehrere gleich gute
Lösungen, die Batterieleistung springt dann um über 1 kW bei weniger als
einem halben Cent Unterschied.
"""

from __future__ import annotations

import dataclasses

import pytest

from custom_components.eeg_energy_optimizer import schedule as sched

from . import replay_korpus as rk

DATEIEN = rk.korpus_dateien()


def test_korpus_ist_da():
    assert len(DATEIEN) >= 10


@pytest.mark.parametrize("pfad", DATEIEN, ids=lambda p: p.name.removesuffix(".json.gz"))
def test_loader_trifft_die_eingaenge(pfad):
    """PV, Verbrauch und Zeitachse kommen exakt so zurück, wie das LP sie sah —
    sonst prüften alle Regeln unten einen anderen Plan als den archivierten."""
    eintrag = rk.eintrag_laden(pfad)
    plan = sched.solve(rk.inputs_aus_eintrag(eintrag))
    alt = eintrag["plan"]["slots"]
    assert len(plan["slots"]) == len(alt)
    for spalte in ("PV", "consumption", "feedin_price"):
        assert rk.abweichung(alt, plan["slots"], spalte) < 1e-3, spalte


@pytest.mark.parametrize("variante", list(rk.VARIANTEN))
@pytest.mark.parametrize("pfad", DATEIEN, ids=lambda p: p.name.removesuffix(".json.gz"))
def test_regeln(pfad, variante):
    inputs = rk.VARIANTEN[variante](rk.inputs_aus_eintrag(rk.eintrag_laden(pfad)))
    plan = sched.solve(inputs)
    assert rk.verstoesse(inputs, plan) == []


# -- Die Regeln selbst: schlagen sie an? -----------------------------------
#
# Ein Regelwerk, das nie rot wird, sieht genauso aus wie eines, das nichts
# prüft. Darum je Regel ein absichtlich kaputter Plan.


@pytest.fixture(scope="module")
def basis():
    inputs = rk.inputs_aus_eintrag(rk.eintrag_laden(DATEIEN[0]))
    return inputs, sched.solve(inputs)


def _kaputt(plan: dict, i: int = 10, **werte) -> dict:
    slots = [dict(s) for s in plan["slots"]]
    slots[i].update(werte)
    return {**plan, "slots": slots}


def test_regel_ladestand(basis):
    inputs, plan = basis
    assert any("Ladestand" in v for v in rk.verstoesse(
        inputs, _kaputt(plan, soc=inputs.min_soc_pct - 5)))


def test_regel_einspeisegrenze(basis):
    inputs, plan = basis
    assert any("Einspeisung" in v for v in rk.verstoesse(
        inputs, _kaputt(plan, grid_p=inputs.feedin_limit_kw + 1)))


def test_regel_netzladen_ohne_freigabe(basis):
    inputs, plan = basis
    assert inputs.netzladen_max_kw == 0
    assert any("ohne Freigabe" in v for v in rk.verstoesse(
        inputs, _kaputt(plan, PV=0.0, discard=0.0, battery_p=-2.0, grid_p=-2.5)))


def test_regel_kauf_und_verkauf(basis):
    inputs, plan = basis
    mit = rk.VARIANTEN["netzladen"](inputs)
    assert any("speist" in v for v in rk.verstoesse(
        mit, _kaputt(plan, PV=0.0, discard=0.0, battery_p=-2.0, grid_p=0.5)))


def test_regel_netzgrenze(basis):
    inputs, plan = basis
    mit = rk.VARIANTEN["netzladen"](inputs)
    assert any("Netzgrenze" in v for v in rk.verstoesse(
        mit, _kaputt(plan, PV=0.0, discard=0.0, battery_p=-2.0,
                     grid_p=-(mit.netzladen_netzgrenze_kw + 1))))


def test_regel_heizstab_ohne_ueberschuss(basis):
    inputs, plan = basis
    mit = rk.VARIANTEN["heizstab"](inputs)
    assert any("ohne PV-Überschuss" in v for v in rk.verstoesse(
        mit, _kaputt(plan, PV=0.0, heizstab=2.0)))


def test_regel_mindestladung(basis):
    inputs, plan = basis
    mit = dataclasses.replace(inputs, netzladen_max_kw=5.0, netzladen_abend_pct=90.0)
    termin = plan["slots"][60]
    kaputt = {**plan, "netzladen_abend": {
        "ziel_pct": 90.0, "termine": [{"t": termin["t"], "geplant_pct": 40.0}],
    }}
    assert any("Mindestladung" in v for v in rk.verstoesse(mit, kaputt))
