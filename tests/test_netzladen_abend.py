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


def _soc_am_abendtermin(plan) -> list[float]:
    return [t["geplant_pct"] for t in plan["netzladen_abend"]["termine"]]


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
    """Gemessen an den Terminen der Mindestladung, nicht des Ladeziels: Ein
    Tag mit nur winzigem Überschuss hat seinen Termin am Ende der Erzeugung,
    das Ladeziel weiter am (frühen) Ende des Überschusses."""
    plan = sched.solve(_abend(abend=50.0))
    assert _netzladung_kwh(plan) > 1.0
    assert plan["netzladen_abend"]["termine"]
    for soc in _soc_am_abendtermin(plan):
        assert soc >= 49.5, soc


def test_nur_bis_zum_schwellwert_nicht_bis_zum_ladeziel():
    """Ladeziel 100 %: Die Pflicht-Slots dürfen es nicht aus dem Netz füllen.
    Ohne die Sperre (``HAConfig._erreichbar``) stand der Plan hier bei 56 %."""
    plan = sched.solve(_abend(abend=50.0, ladeziel=100.0))
    for soc in _soc_am_pv_ende(plan):
        assert soc <= 51.0, soc


def test_geladen_wird_im_billigen_fenster_vor_dem_pv_ende():
    plan = sched.solve(_abend(abend=50.0))
    termine = [datetime.fromisoformat(t["t"]) for t in plan["netzladen_abend"]["termine"]]
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
# Dunkle Tage: PV ganztags unter der Hauslast. Die Mindestladung steht wie
# an jedem Tag beim Sonnenuntergang (``Forderungen.termine_abend``).
# ---------------------------------------------------------------------------


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
    table = opt_highs.opt(config, inputs.start)
    # Die Rechnung, mit der das LP lief, liegt im Cache.
    lp = config._forderungen_cache[2]
    gesehen = [lp.lage.stempel[i] for i in lp.termine_abend]
    ueberschuss = table["PV"] - table["consumption"] / config.ac_efficiency
    ausgabe = [table.index[i] for i in config.abend_termine(table, ueberschuss, table["PV"])]
    assert gesehen
    assert [pd.Timestamp(t) for t in gesehen] == [pd.Timestamp(t) for t in ausgabe]


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


def test_forderungen_cache_haelt_die_objekte():
    """Gleiche Reihen → einmal gerechnet; neue Reihen → neu gerechnet, auch
    wenn ihre ``id()`` zufällig die alte wäre."""
    pd = pytest.importorskip("pandas")
    config = sched.HAConfig(_abend())
    index = pd.date_range("2026-11-02 18:00", periods=2, freq="15min", tz="Europe/Vienna")
    parameters = pd.DataFrame({"dc_production": [0.0, 0.0]}, index=index)
    ueberschuss = pd.Series([-1.0, -1.0], index=index)
    erste = config._forderungen(parameters, ueberschuss)
    assert config._forderungen(parameters, ueberschuss) is erste
    kopie = parameters.copy()
    assert config._forderungen(kopie, ueberschuss) is not erste
    assert config._forderungen_cache[0] is kopie


# ---------------------------------------------------------------------------
# Review Runde 2: Überschuss vor dem Horizontstart, winziger Überschuss,
# Entladung in der Pflicht-Rechnung.
# ---------------------------------------------------------------------------


def test_pflicht_rechnung_deckelt_die_entladung_nicht():
    """Das Haus braucht mehr, als die Batterie leisten darf: ``_netz_untergrenze``
    muss den Stand so tief fallen lassen wie ``opt_highs`` — sonst wählt es zu
    wenige Pflicht-Slots, und ``opt()`` kappt die Forderung still."""
    pd = pytest.importorskip("pandas")
    config = sched.HAConfig(_abend())
    n = 12
    index = pd.date_range("2026-10-05 10:00", periods=n, freq="15min", tz="Europe/Vienna")
    grenze = float(config.battery_power_limit)
    kapazitaet = float(config.battery_capacity)
    # Elf ruhige Slots zum Nachladen, dann einer, in dem das Haus das
    # Zweieinhalbfache der Batterieleistung zieht.
    werte = [-0.1] * (n - 1) + [-2.5 * grenze]
    ueberschuss = pd.Series(werte, index=index)
    parameters = pd.DataFrame(
        {
            "dc_production": [0.0] * n,
            "consumption": [0.1] * n,
            # Der Defizit-Slot ist teuer: gewählt werden die ruhigen davor.
            "consumption_price": [0.2 * 0.25] * (n - 1) + [1.0 * 0.25],
        },
        index=index,
    )
    start = kapazitaet - float(config.battery_free)
    forderung = [0.0] * n
    forderung[-1] = start + 0.2 * grenze
    assert start + 1.0 * grenze < kapazitaet  # der Test braucht Luft nach oben
    # Die Forderung von Hand setzen — die Pflicht-Rechnung liest sie aus der
    # Rechnung des Laufs (cached_property, also über das Instanz-Dict).
    config._forderungen(parameters, ueberschuss).__dict__["netz_forderung"] = forderung
    untergrenze = config._netz_untergrenze(parameters, ueberschuss)
    assert untergrenze is not None
    # Nachrechnen wie opt_highs: Laden gedeckelt, Entladung nicht.
    stand = kapazitaet - float(config.battery_free)
    for k in range(n):
        u = float(untergrenze.iloc[k])
        if u < 0:
            schritt = min(sched._ladeleistung_aus_dc(-u, kapazitaet), grenze)
        else:
            schritt = -sched._entladeleistung_fuer_dc(-werte[k], kapazitaet)
        stand = min(kapazitaet, max(0.0, stand + schritt * 0.25))
    assert stand >= forderung[-1] - 1e-6


def test_vorsichtige_hauslast_kommt_in_die_kette():
    """Die Reihe aus den Eingaben landet je LP-Slot in der Lage, nie unter
    der Prognose — und nur dort; der Plan bleibt lösbar."""
    basis = _abend(abend=50.0)
    n = len(basis.timestamps)
    inputs = dataclasses.replace(basis, haus_vorsichtig_kw=[3.5] * n)
    plan = sched.solve(inputs)
    assert plan["slots"]
    ohne = sched.solve(basis)
    # Die Kette fordert mit mehr Hauslast eher mehr, nie weniger
    assert _soc_am_abendtermin(plan)[0] >= _soc_am_abendtermin(ohne)[0] - 0.5


def test_haus_vorsichtig_aus_dem_verlauf():
    from datetime import timedelta, timezone

    class Reserve:
        def __init__(self, verlauf):
            self._v = verlauf

        def verlauf_nach_zeit(self):
            return self._v

    tz = timezone(timedelta(hours=1))
    stamps = [datetime(2026, 11, 6, 15, 0, tzinfo=tz), datetime(2026, 11, 6, 15, 15, tzinfo=tz)]
    verlauf = {
        datetime(2026, 11, 6, 15, 0) - timedelta(days=k): 4.5 for k in range(1, 29)
    }
    config = {"schedule_netzladen_abend_pct": 90}
    werte = sched._haus_vorsichtig({"spitzenreserve": Reserve(verlauf)}, config, stamps, [2.4, 2.4])
    assert werte == [4.5, 2.4]  # zweiter Slot ohne Verlauf: die Prognose
    assert sched._haus_vorsichtig({"spitzenreserve": Reserve(verlauf)}, {}, stamps, [2.4, 2.4]) is None
