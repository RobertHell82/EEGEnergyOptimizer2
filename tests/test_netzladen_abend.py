"""Mindestladung am Abend (``schedule_netzladen_abend_pct``).

Am Ende der PV-Zeit soll die Batterie mindestens den Schwellwert halten.
Reicht die erwartete PV nicht, wird der Rest aus dem Netz geladen —
unabhängig vom Preis, aber in den günstigsten Viertelstunden davor (dort,
wo die Gemeinschaft Überschuss hat). Was darüber hinausgeht, bleibt Sache
des Ladeziels: Die für die Pflicht gewählten Slots füllen es nicht aus dem
Netz auf.
"""

import dataclasses
from datetime import datetime

import pytest

from custom_components.eeg_energy_optimizer import schedule as sched

from .test_netzladen import _inputs, _netzladung_kwh


@pytest.fixture(autouse=True)
def lp():
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")


# Mittags 19 ct gegen 20,1 ct nachts: billiger, aber nach η² und Vorteil
# nicht wirtschaftlich — das Netzladen allein kauft dort nichts.
MITTAG = 0.19


def _abend(pv=0.1, abend=50.0, ladeziel=100.0, **kw):
    return dataclasses.replace(
        _inputs(pv=pv, mittag=MITTAG, ladeziel=ladeziel, **kw), netzladen_abend_pct=abend
    )


def _soc_am_pv_ende(plan) -> list[float]:
    return [t["geplant_pct"] for t in plan["ladeziel"]["termine"]]


def test_einstellung_geklemmt_und_leer_heisst_aus():
    assert sched.netzladen_abend_pct({}) == 0.0
    assert sched.netzladen_abend_pct({"schedule_netzladen_abend_pct": ""}) == 0.0
    assert sched.netzladen_abend_pct({"schedule_netzladen_abend_pct": "40"}) == 40.0
    assert sched.netzladen_abend_pct({"schedule_netzladen_abend_pct": 150}) == 100.0
    assert sched.netzladen_abend_pct({"schedule_netzladen_abend_pct": "x"}) == 0.0


def test_ohne_schwellwert_kauft_der_unwirtschaftliche_mittag_nichts():
    plan = sched.solve(_abend(abend=0.0))
    assert _netzladung_kwh(plan) == 0.0
    assert min(_soc_am_pv_ende(plan)) < 45.0


def test_schwellwert_wird_aus_dem_netz_erreicht():
    plan = sched.solve(_abend(abend=50.0))
    assert _netzladung_kwh(plan) > 1.0
    for soc in _soc_am_pv_ende(plan):
        assert soc >= 49.5, soc


def test_nur_bis_zum_schwellwert_nicht_bis_zum_ladeziel():
    """Ladeziel 100 %: Die Pflicht-Slots dürfen es nicht aus dem Netz füllen.
    Ohne die Sperre (``HAConfig._erreichbar``) stand der Plan hier bei 56 %."""
    plan = sched.solve(_abend(abend=50.0, ladeziel=100.0))
    for soc in _soc_am_pv_ende(plan):
        assert soc <= 51.0, soc


def test_geladen_wird_im_billigen_fenster_vor_dem_pv_ende():
    plan = sched.solve(_abend(abend=50.0))
    termine = [datetime.fromisoformat(t["t"]) for t in plan["ladeziel"]["termine"]]
    geladen = [s for s in plan["slots"] if s.get("netzladung", 0.0) > 0.05]
    assert geladen
    for slot in geladen:
        t = datetime.fromisoformat(slot["t"])
        assert 10 <= t.hour < 16, slot["t"]
        assert any(t <= termin and t.date() == termin.date() for termin in termine), slot["t"]


def test_sonniger_tag_kauft_nichts():
    assert _netzladung_kwh(sched.solve(_abend(pv=1.0, abend=50.0))) == 0.0


def test_ohne_netzladen_wirkt_der_schwellwert_nicht():
    plan = sched.solve(_abend(abend=50.0, max_kw=0.0))
    assert "netzladung" not in plan["slots"][0]
    assert min(_soc_am_pv_ende(plan)) < 45.0


def test_auch_ohne_ladeziel():
    """Der Schwellwert braucht kein Ladeziel — er hat seinen eigenen Termin."""
    plan = sched.solve(_abend(abend=50.0, ladeziel=0.0))
    assert _netzladung_kwh(plan) > 1.0
