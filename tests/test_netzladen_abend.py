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
# Dunkle Tage: PV ganztags unter der Hauslast — kein Überschuss-Slot, also
# kein Ende der PV-Zeit im Sinne von ``pv_enden``. Die Mindestladung steht
# dann am Ende der Erzeugung (``pv_erzeugung_enden``).
# ---------------------------------------------------------------------------


def test_pv_erzeugung_enden_nur_an_tagen_ohne_ueberschuss():
    tage = ["a"] * 6 + ["b"] * 6 + ["c"] * 3
    pv = [0, 0.2, 0.3, 0.1, 0.02, 0] + [0, 3.5, 4.6, 0.1, 0, 0] + [0, 0.2, 0.3]
    werte = [-1, -0.5, -0.4, -0.6, -1, -1] + [-1, 3.0, 4.0, -0.5, -1, -1] + [-1, -0.5, -0.4]
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
    table = opt_highs.opt(config, inputs.start)
    # Die Rechnung, mit der das LP lief, liegt im Cache.
    lp = config._forderungen_cache[2]
    gesehen = [lp.lage.stempel[i] for i in lp.termine_abend]
    ueberschuss = table["PV"] - table["consumption"] / config.ac_efficiency
    ausgabe = [table.index[i] for i in config.abend_termine(table, ueberschuss, table["PV"])]
    assert gesehen
    assert [pd.Timestamp(t) for t in gesehen] == [pd.Timestamp(t) for t in ausgabe]


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


def test_sonniger_tag_ab_1530_bekommt_keinen_zweiten_termin():
    """Horizont ab 15:30, der Überschuss des Tages ist schon vorbei: Ohne den
    Teil vor dem Start sah der Tag dunkel aus und bekam einen Termin am Ende
    der Erzeugung (~16:30) — ein Zwangskauf am Nachmittag."""
    # 15:30 … 17:30, PV fällt unter die Hauslast, kein Überschuss mehr.
    tage = ["a"] * 9
    pv = [0.5, 0.4, 0.3, 0.2, 0.1, 0.04, 0, 0, 0]
    werte = [p - 0.5 / 0.95 for p in pv]
    assert sched.abend_kandidaten(pv, werte, tage) == [4]  # ohne Wissen: dunkel
    assert sched.abend_kandidaten(pv, werte, tage, {"a": 12.0}) == []
    assert sched.pv_erzeugung_enden(pv, werte, tage, {"a": 12.0}) == []


def test_haconfig_nutzt_den_ueberschuss_vor_dem_start():
    """``ueberschuss_vor_start_kwh`` gilt für den Tag des Horizontstarts
    (Horizont ab 15:30, der Überschuss war am Vormittag): Ohne ihn sieht der
    Tag dunkel aus und bekommt einen Termin am Ende der Erzeugung, mit ihm
    keinen."""
    pd = pytest.importorskip("pandas")
    index = pd.date_range("2026-11-02 15:30", periods=40, freq="15min", tz="Europe/Vienna")
    pv = [max(0.0, 0.5 - 0.1 * i) for i in range(6)] + [0.0] * 34
    parameters = pd.DataFrame(
        {"consumption": [0.5] * 40, "feedin_limit": [10.0] * 40}, index=index
    )
    ueberschuss = pd.Series([p - 0.5 / 0.95 for p in pv], index=index)
    pv_reihe = pd.Series(pv, index=index)
    ohne = sched.HAConfig(_abend(abend=50.0))
    mit = sched.HAConfig(dataclasses.replace(_abend(abend=50.0), ueberschuss_vor_start_kwh=8.0))
    assert ohne.abend_termine(parameters, ueberschuss, pv_reihe) == [4]
    assert mit.abend_termine(parameters, ueberschuss, pv_reihe) == []


def test_winziger_ueberschuss_zieht_den_termin_nicht_auf_mittag():
    """Ein Slot mit 0,01 kW über der Hauslast zu Mittag: Die Mindestladung
    bleibt am Ende der Erzeugung; ``pv_enden`` (und damit das Ladeziel) sieht
    weiter den Mittag."""
    tage = ["a"] * 10
    pv = [0.1, 0.3, 0.54, 0.4, 0.3, 0.2, 0.1, 0.02, 0, 0]
    werte = [p - 0.5 / 0.95 for p in pv]
    assert werte[2] > 0 and sum(w for w in werte if w > 0) * 0.25 < 0.5
    assert sched.pv_enden(werte, tage) == [2]
    assert sched.abend_kandidaten(pv, werte, tage) == [6]
    # Nennenswerter Überschuss: wieder das Ende des Überschusses.
    viel = [w + (3.0 if i == 2 else 0.0) for i, w in enumerate(werte)]
    assert sched.abend_kandidaten(pv, viel, tage) == [2]


def test_winziger_ueberschuss_bei_offenem_horizont_kein_mittagstermin():
    """Endet der Horizont mitten in der Erzeugung, fällt der Tag weg — statt
    auf den winzigen Mittagsslot zurückzufallen."""
    tage = ["a"] * 6
    pv = [0.1, 0.3, 0.54, 0.4, 0.3, 0.2]
    werte = [p - 0.5 / 0.95 for p in pv]
    assert sched.abend_kandidaten(pv, werte, tage) == []


def test_ueberschuss_vor_start_aus_der_prognose():
    from datetime import timedelta, timezone

    tz = timezone(timedelta(hours=1))

    class Profil:
        hourly_avg = {"x": 1}

        def hourly_for(self, stamp):
            return 500.0

    start = datetime(2026, 11, 2, 15, 30, tzinfo=tz)
    mittag = datetime(2026, 11, 2, 12, 0, tzinfo=tz)
    detailed = {
        mittag: (2.5, 2.0),
        mittag + timedelta(minutes=30): (0.0, 0.0),
    }
    kwh = sched._ueberschuss_vor_start_prognose(Profil(), start, detailed, None)
    # 12:00–12:30 = zwei Viertelstunden zu (2,5 − 0,5/0,95) kW.
    assert kwh == pytest.approx(2 * 0.25 * (2.5 - 0.5 / 0.95))
    assert sched._ueberschuss_vor_start_prognose(Profil(), start, None, None) == 0.0
    um_mitternacht = start.replace(hour=0, minute=0)
    assert sched._ueberschuss_vor_start_prognose(Profil(), um_mitternacht, detailed, None) == 0.0


def test_ueberschuss_gemessen_summiert_und_beginnt_um_mitternacht():
    from datetime import timedelta, timezone

    tz = timezone(timedelta(hours=1))
    data = {}
    t0 = datetime(2026, 11, 2, 12, 0, tzinfo=tz)
    assert sched._ueberschuss_gemessen(data, t0, 3.0, 0.95) == 0.0
    kwh = sched._ueberschuss_gemessen(data, t0 + timedelta(minutes=1), 3.0, 0.95)
    assert kwh == pytest.approx(2.0 / 60)
    # Eine lange Pause zählt höchstens fünf Minuten.
    kwh = sched._ueberschuss_gemessen(data, t0 + timedelta(hours=1), 3.0, 0.95)
    assert kwh == pytest.approx(2.0 / 60 + 2.0 * 5 / 60)
    # Nächster Tag: von vorn.
    assert sched._ueberschuss_gemessen(data, t0 + timedelta(days=1), 3.0, 0.95) == 0.0


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
