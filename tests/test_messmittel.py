"""Gemittelte Messwerte für den ersten Stützpunkt (schedule._messmittel).

Anlass: Winterthur, 09.10.2026 — mit dem Augenblickswert einer Minute kippte
das LP an einem knappen Tag zwischen „jetzt laden" und „jetzt einspeisen",
das Ladelimit sprang alle paar Minuten zwischen 0 und 2 kW.
"""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.eeg_energy_optimizer import schedule as sched

T0 = datetime(2026, 10, 9, 9, 30, tzinfo=timezone(timedelta(hours=2)))


def _min(n: int) -> datetime:
    return T0 + timedelta(minutes=n)


def test_ein_ausreisser_wird_gedaempft():
    data: dict = {}
    for i, wert in enumerate([2.0, 2.0, 2.0, 2.0]):
        sched._messmittel(data, "pv", wert, _min(i))
    # Eine Wolke: 0,4 kW für eine Minute — der Plan sieht 1,68, nicht 0,4.
    assert sched._messmittel(data, "pv", 0.4, _min(4)) == pytest.approx(1.68)


def test_alte_werte_fallen_aus_dem_fenster():
    data: dict = {}
    sched._messmittel(data, "pv", 5.0, _min(0))
    sched._messmittel(data, "pv", 1.0, _min(3))
    # Nach fünf Minuten zählt die 5,0 nicht mehr.
    assert sched._messmittel(data, "pv", 1.0, _min(5)) == 1.0


def test_echter_trend_kommt_durch():
    data: dict = {}
    werte = [sched._messmittel(data, "pv", 0.5, _min(i)) for i in range(5)]
    werte += [sched._messmittel(data, "pv", 3.0, _min(i)) for i in range(5, 10)]
    assert werte[4] == 0.5
    assert werte[-1] == 3.0
    assert werte[6] == pytest.approx(1.5)  # nach zwei Minuten halb da
    assert werte[7] == pytest.approx(2.0)


def test_nicht_lesbar_haelt_das_mittel_und_fehlt_ganz_none():
    data: dict = {}
    assert sched._messmittel(data, "haus", None, _min(0)) is None
    sched._messmittel(data, "haus", 0.6, _min(1))
    assert sched._messmittel(data, "haus", None, _min(2)) == 0.6
    assert sched._messmittel(data, "haus", None, _min(7)) is None


def test_pv_und_haus_getrennt():
    data: dict = {}
    sched._messmittel(data, "pv", 3.0, _min(0))
    assert sched._messmittel(data, "haus", 0.5, _min(0)) == 0.5


def test_zeitsprung_rueckwaerts_verwirft():
    data: dict = {}
    sched._messmittel(data, "pv", 3.0, _min(10))
    assert sched._messmittel(data, "pv", 1.0, _min(0)) == 1.0
