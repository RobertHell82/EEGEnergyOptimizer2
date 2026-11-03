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
import os

import pytest

from custom_components.eeg_energy_optimizer import schedule as sched

from . import replay_korpus as rk

DATEIEN = rk.korpus_dateien()

# Pläne, an denen ein Fehler auftrat — sie laufen immer in allen Varianten.
# Gegen den Stand vor dem jeweiligen Fix wird jeder davon rot.
FUNDSTELLEN = {
    "fronius-a_2026-10-25_025848",  # Zeitumstellung im Loader
    "fronius-b_2026-11-02_131628",  # Mindestladung, Kappungsgrenze bindet
    "fronius-b_2026-11-03_101549",
    "huawei-a_2026-10-28_170232",   # Heizstab aus abgeregelter PV
    "huawei-a_2026-11-01_194432",   # infeasible am Horizontende
    "huawei-a_2026-11-02_010841",   # Netzladen über der Kappungsgrenze
    "huawei-a_2026-11-02_155259",   # Mindestladung, reale Einstellung
    "kostal-a_2026-10-27_160353",   # Mindestladung, break
    "kostal-a_2026-10-28_111355",
    "kostal-a_2026-11-01_130901",
    "kostal-a_2026-11-02_160008",
    "solax-a_2026-11-01_155757",
    "solax-a_2026-11-01_190457",    # infeasible am Horizontende
}


def _name(pfad) -> str:
    return pfad.name.removesuffix(".json.gz")


def _mit_varianten() -> list:
    """Jeder dritte Plan je Anlage plus alle Fundstellen — oder alle, mit
    ``EEG_KORPUS_VOLL=1``. Die Fehler fanden bisher die Fundstellen und die
    Varianten, nicht die schiere Menge; alle Pläne in allen Varianten
    kosteten über vier Minuten."""
    if os.environ.get("EEG_KORPUS_VOLL"):
        return DATEIEN
    je_anlage: dict[str, int] = {}
    auswahl = []
    for pfad in DATEIEN:
        anlage = _name(pfad).split("_")[0]
        nummer = je_anlage[anlage] = je_anlage.get(anlage, -1) + 1
        if nummer % 3 == 0 or _name(pfad) in FUNDSTELLEN:
            auswahl.append(pfad)
    return auswahl


def test_korpus_ist_da():
    assert len(DATEIEN) >= 10


def test_fundstellen_sind_im_korpus():
    assert FUNDSTELLEN <= {_name(p) for p in DATEIEN}


@pytest.mark.parametrize("pfad", DATEIEN, ids=_name)
def test_wie_archiviert(pfad):
    """Jeder Plan wie archiviert: Erst muss der Loader die Eingänge exakt
    treffen — PV, Verbrauch, Preis, Zahl der Slots —, sonst prüften die
    Regeln einen anderen Plan als den archivierten. Dann die Regeln."""
    eintrag = rk.eintrag_laden(pfad)
    inputs = rk.inputs_aus_eintrag(eintrag)
    plan = sched.solve(inputs)
    alt = eintrag["plan"]["slots"]
    assert len(plan["slots"]) == len(alt)
    for spalte in ("PV", "consumption", "feedin_price"):
        assert rk.abweichung(alt, plan["slots"], spalte) < 1e-3, spalte
    assert rk.verstoesse(inputs, plan) == []


@pytest.mark.parametrize("variante", [v for v in rk.VARIANTEN if v != "archiv"])
@pytest.mark.parametrize("pfad", _mit_varianten(), ids=_name)
def test_regeln(pfad, variante):
    inputs = rk.VARIANTEN[variante](rk.inputs_aus_eintrag(rk.eintrag_laden(pfad)))
    plan = sched.solve(inputs)
    assert rk.verstoesse(inputs, plan) == []


# -- Grund je Slot: an echten Fundstellen ----------------------------------


def _plan(name: str, variante: str) -> dict:
    inputs = rk.VARIANTEN[variante](
        rk.inputs_aus_eintrag(rk.eintrag_laden(rk.KORPUS / f"{name}.json.gz"))
    )
    return sched.solve(inputs)


def test_grund_nennt_pflicht_und_kappungsgrenze():
    """Fronius 19 kWh, 02.11. 13:16, Mindestladung 90 %: Der Plan lädt mit dem
    höchsten Bezug, den die Spitzenkappung (4 kW) zulässt — genau das soll
    im Grund stehen, nicht von Hand nachgerechnet werden müssen."""
    plan = _plan("fronius-b_2026-11-02_131628", "mindestladung")
    grund = plan["slots"][0].get("grund") or ""
    assert "Netzladung für die Mindestladung (90 % um 16:15)" in grund
    assert "Spitzenkappung (4,0 kW)" in grund


def test_grund_am_planungsrand_nennt_den_abgesenkten_wert():
    plan = _plan("fronius-a_2026-10-21_155642", "archiv")
    (rand,) = [t for t in plan["ladeziel"]["termine"] if t.get("abgesenkt")]
    assert rand["abgesenkt"] == "planungsrand"
    gruende = [s.get("grund") or "" for s in plan["slots"]]
    assert any("am Planungsrand auf 63 % abgesenkt" in g for g in gruende)


def test_ohne_forderung_kein_grund():
    """Eine Anlage ohne Ladeziel, Mindestladung und Reserve hat nichts, was
    einen Slot erklärt — dann bleibt das Feld weg (Archiv bleibt klein)."""
    eintrag = rk.eintrag_laden(rk.KORPUS / "sigenergy-a_2026-10-29_050721.json.gz")
    inputs = dataclasses.replace(rk.inputs_aus_eintrag(eintrag), ladeziel_pct=0.0)
    plan = sched.solve(inputs)
    assert not any("grund" in s for s in plan["slots"])


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
