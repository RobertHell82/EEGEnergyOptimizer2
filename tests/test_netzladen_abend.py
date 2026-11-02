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


# ---------------------------------------------------------------------------
# Dunkle Tage: PV ganztags unter der Hauslast — kein Überschuss-Slot, also
# kein Ende der PV-Zeit im Sinne von ``pv_enden``. Die Mindestladung steht
# dann am Ende der Erzeugung (``pv_erzeugung_enden``).
# ---------------------------------------------------------------------------


def test_pv_erzeugung_enden_nur_an_tagen_ohne_ueberschuss():
    tage = ["a"] * 6 + ["b"] * 6 + ["c"] * 3
    pv = [0, 0.2, 0.3, 0.1, 0.02, 0] + [0, 0.5, 0.6, 0.1, 0, 0] + [0, 0.2, 0.3]
    werte = [-1, -0.5, -0.4, -0.6, -1, -1] + [-1, 0.2, 0.3, -0.5, -1, -1] + [-1, -0.5, -0.4]
    # Tag a: letzte Erzeugung über 0,05 kW bei Index 3 (0,02 ist Dämmerung).
    # Tag b hat Überschuss — das ist Sache von pv_enden. Tag c endet mit dem
    # Horizont, mitten in der Erzeugung: kein Termin.
    assert sched.pv_erzeugung_enden(pv, werte, tage) == [3]
    assert sched.pv_erzeugung_enden([0.0] * 6, [-1.0] * 6, ["a"] * 6) == []


def test_dunkler_tag_bekommt_die_mindestladung():
    """PV-Faktor 0,03: kein Slot über der Hauslast. Bis 2.1.63 gab es keinen
    Termin, der Plan kaufte nichts und blieb bei 27,6 %."""
    plan = sched.solve(_abend(pv=0.03, abend=50.0))
    abend = plan["netzladen_abend"]
    assert abend["termine"], abend
    assert _netzladung_kwh(plan) > 1.0
    for termin in abend["termine"]:
        assert termin["geplant_pct"] >= 49.5, termin
        # Am Ende der Erzeugung, nicht irgendwann.
        t = datetime.fromisoformat(termin["t"])
        assert 15 <= t.hour < 19, termin["t"]
    # Das Ladeziel bleibt unberührt: An einem Tag ohne Überschuss hat es
    # keinen Termin.
    assert plan["ladeziel"]["termine"] == []


def test_dunkle_termine_sind_die_des_lp():
    """Plan-Ausgabe (``_abend_termine``) und LP (``_abend_forderung``)
    rechnen dieselben Termine — die Telemetrie prüft, was das LP forderte."""
    pd = pytest.importorskip("pandas")
    from custom_components.eeg_energy_optimizer.chamo import opt_highs

    inputs = _abend(pv=0.03, abend=50.0)
    config = sched.HAConfig(inputs)
    gesehen = {}
    original = config.abend_termine

    def mitschreiben(parameters, ueberschuss, pv):
        ergebnis = original(parameters, ueberschuss, pv)
        if "dc_production" in parameters:
            gesehen["lp"] = [ueberschuss.index[i] for i in ergebnis]
        return ergebnis

    config.abend_termine = mitschreiben
    table = opt_highs.opt(config, inputs.start)
    ueberschuss = table["PV"] - table["consumption"] / config.ac_efficiency
    ausgabe = [table.index[i] for i in config.abend_termine(table, ueberschuss, table["PV"])]
    assert gesehen["lp"]
    assert [pd.Timestamp(t) for t in gesehen["lp"]] == [pd.Timestamp(t) for t in ausgabe]


def test_ganz_ohne_pv_kein_termin():
    """Prognose 0 den ganzen Tag: Es gibt kein Ende der Erzeugung, an dem die
    Mindestladung stehen könnte — kein Termin, kein Kauf (bewusst)."""
    plan = sched.solve(_abend(pv=0.0, abend=50.0))
    assert plan["netzladen_abend"]["termine"] == []
    assert _netzladung_kwh(plan) == 0.0


def test_erreichbar_deckelt_die_entladung_nicht_wie_opt_highs():
    """``opt_highs`` deckelt in seiner Erreichbarkeit nur das Laden
    (``min(step, max_step)``). ``_erreichbar`` tat beides — der Stand blieb
    über dem des LP."""
    pd = pytest.importorskip("pandas")
    config = sched.HAConfig(_abend())
    index = pd.date_range("2026-11-02 18:00", periods=4, freq="15min", tz="Europe/Vienna")
    defizit = -3.0 * float(config.battery_power_limit)
    ueberschuss = pd.Series([defizit] * 4, index=index)
    parameters = pd.DataFrame({"dc_production": [0.0] * 4}, index=index)
    stand = float(config.battery_capacity) - float(config.battery_free)
    kapazitaet = float(config.battery_capacity)
    erwartet = []
    for _ in range(4):
        stand = max(0.0, stand - sched._entladeleistung_fuer_dc(-defizit, kapazitaet) * 0.25)
        erwartet.append(stand)
    assert config._erreichbar(parameters, ueberschuss, None) == pytest.approx(erwartet)


def test_netz_grenzen_cache_haelt_die_objekte():
    """Gleiche Reihen → einmal gerechnet; neue Reihen → neu gerechnet, auch
    wenn ihre ``id()`` zufällig die alte wäre."""
    pd = pytest.importorskip("pandas")
    config = sched.HAConfig(_abend())
    aufrufe = []
    config._netzladen_wirtschaftlich = lambda p, u: aufrufe.append(1) or None
    config._netz_untergrenze = lambda p, u: None
    index = pd.date_range("2026-11-02 18:00", periods=2, freq="15min", tz="Europe/Vienna")
    parameters = pd.DataFrame({"dc_production": [0.0, 0.0]}, index=index)
    ueberschuss = pd.Series([-1.0, -1.0], index=index)
    config._netz_grenzen(parameters, ueberschuss)
    config._netz_grenzen(parameters, ueberschuss)
    assert len(aufrufe) == 1
    config._netz_grenzen(parameters.copy(), ueberschuss)
    assert len(aufrufe) == 2
    assert config._netz_cache[0] is not parameters
