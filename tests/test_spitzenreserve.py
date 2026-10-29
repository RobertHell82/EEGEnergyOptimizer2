"""Tests für die Spitzenreserve (spitzenreserve.py).

Die Rechnung ist rein: aus einem Nettolast-Verlauf (Haus − PV je
Viertelstunde) und einer Überschussmaske wird je Slot die Energie, die bis
zum nächsten Überschuss über der Grenze liegt, als Quantil über die
vergangenen Tage. Dazu die Ausführung im laufenden Betrieb
(``entladedeckel_kw``): unter der Schwelle deckt die Batterie nur noch, was
über dem erlaubten Netzbezug der laufenden Viertelstunde liegt.
"""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.eeg_energy_optimizer import spitzenreserve as sr

TZ = timezone(timedelta(hours=1))
# Ein Abend um 18:00 — die Nacht bis 08:00 hat keinen Überschuss.
START = datetime(2026, 10, 20, 18, 0, tzinfo=TZ)


def _slots(n: int, start: datetime = START) -> list[datetime]:
    return [start + timedelta(minutes=15 * i) for i in range(n)]


def _verlauf(tage: int, wert_fn, start: datetime = START, n: int = 56) -> dict[str, float]:
    """Verlauf über ``tage`` vergangene Tage: ``wert_fn(tag, slot_index)`` kW."""
    verlauf: dict[str, float] = {}
    for k in range(1, tage + 1):
        for i, t in enumerate(_slots(n, start - timedelta(days=k))):
            verlauf[sr.schluessel(t)] = wert_fn(k, i)
    return verlauf


# ---------------------------------------------------------------------------
# reservekurve
# ---------------------------------------------------------------------------


def _kurve(verlauf: dict[str, float], *args, **kw):
    """reservekurve mit dem Verlauf im Speicherformat (Text-Schlüssel)."""
    return sr.reservekurve(sr.verlauf_nach_zeit(verlauf), *args, **kw)


def test_reserve_summiert_den_ueberschuss_ueber_der_grenze_bis_zum_morgen():
    """Jeden Abend 2 h Auto mit 7 kW (Slots 8–15), sonst 0,5 kW, Grenze 5 kW:
    über der Grenze liegen 2 kW × 8 Viertelstunden = 4 kWh. Ab 18:00 braucht
    die Nacht also 4 kWh (plus Entladeverluste), nach dem Laden nichts mehr."""
    verlauf = _verlauf(10, lambda k, i: 7.0 if 8 <= i < 16 else 0.5)
    n = 56  # 18:00 bis 08:00
    kurve, tage = _kurve(verlauf, _slots(n), [False] * n, 5.0, 10.0)
    assert tage == 10
    assert kurve[0] == pytest.approx(4.0 / sr.ENTLADE_WIRKUNGSGRAD, abs=1e-3)
    # Mitten im Laden die Hälfte, danach nichts.
    assert kurve[12] == pytest.approx(2.0 / sr.ENTLADE_WIRKUNGSGRAD, abs=1e-3)
    assert kurve[16] == 0.0


def test_reserve_ist_ein_quantil_und_kein_mittelwert():
    """Das Auto lädt in 3 von 20 Nächten. Der Mittelwert gäbe ein Siebtel
    dessen, was in genau diesen Nächten fehlt — das 90-%-Quantil nimmt die
    ganze Nacht (der drittgrößte Wert von 20)."""
    auto_naechte = {2, 9, 16}
    verlauf = _verlauf(
        20, lambda k, i: (7.0 if 8 <= i < 16 else 0.5) if k in auto_naechte else 0.5
    )
    kurve, tage = _kurve(verlauf, _slots(56), [False] * 56, 5.0, 10.0)
    assert tage == 20
    assert kurve[0] == pytest.approx(4.0 / sr.ENTLADE_WIRKUNGSGRAD, abs=1e-3)
    # Zwei von 20 reichen nicht: dann ist das Quantil eine Nacht ohne Auto.
    verlauf2 = _verlauf(
        20, lambda k, i: (7.0 if 8 <= i < 16 else 0.5) if k in {2, 9} else 0.5
    )
    kurve2, _ = _kurve(verlauf2, _slots(56), [False] * 56, 5.0, 10.0)
    assert kurve2[0] == 0.0


def test_kurve_faellt_im_fenster_nie_an():
    """Die Summe zählt nur, was ab dem Slot noch kommt — sie fällt nach hinten."""
    verlauf = _verlauf(12, lambda k, i: 3.0 + (k % 5) + (i % 7) * 0.5)
    kurve, _ = _kurve(verlauf, _slots(56), [False] * 56, 4.0, 10.0)
    assert all(a >= b - 1e-9 for a, b in zip(kurve, kurve[1:]))


def test_ueberschuss_setzt_die_summe_zurueck():
    """Ein Überschuss-Slot beendet das Fenster: davor zählt nur bis dorthin,
    der Überschuss selbst braucht keine Reserve."""
    verlauf = _verlauf(10, lambda k, i: 6.0)
    ueber = [False] * 4 + [True] * 4 + [False] * 4
    kurve, _ = _kurve(verlauf, _slots(12), ueber, 5.0, 10.0)
    viertel = 1.0 * sr.SLOT_H / sr.ENTLADE_WIRKUNGSGRAD
    assert kurve[0] == pytest.approx(4 * viertel, abs=1e-3)
    assert kurve[3] == pytest.approx(viertel, abs=1e-3)
    assert kurve[4:8] == [0.0] * 4
    assert kurve[8] == pytest.approx(4 * viertel, abs=1e-3)


def test_was_ueber_der_entladeleistung_liegt_braucht_keine_reserve():
    """11-kW-Auto, Grenze 5 kW, Batterie 3 kW: kappen kann sie nur 3 kW,
    die übrigen 3 kW sind Sache der Verbraucherabschaltung."""
    verlauf = _verlauf(10, lambda k, i: 11.0 if i < 4 else 0.0)
    kurve, _ = _kurve(verlauf, _slots(8), [False] * 8, 5.0, 3.0)
    assert kurve[0] == pytest.approx(4 * 3.0 * sr.SLOT_H / sr.ENTLADE_WIRKUNGSGRAD, abs=1e-3)


def test_hoehere_grenze_heisst_weniger_reserve():
    verlauf = _verlauf(10, lambda k, i: 9.0 if 8 <= i < 16 else 1.0)
    tief, _ = _kurve(verlauf, _slots(56), [False] * 56, 5.0, 10.0)
    hoch, _ = _kurve(verlauf, _slots(56), [False] * 56, 8.0, 10.0)
    assert tief[0] > hoch[0] > 0
    keine, _ = _kurve(verlauf, _slots(56), [False] * 56, 11.0, 10.0)
    assert keine[0] == 0.0


def test_zu_wenige_tage_ergeben_keine_kurve():
    verlauf = _verlauf(sr.MIN_TAGE - 1, lambda k, i: 9.0)
    kurve, tage = _kurve(verlauf, _slots(56), [False] * 56, 5.0, 10.0)
    assert kurve == [None] * 56
    assert tage == sr.MIN_TAGE - 1


def test_tag_mit_luecke_zaehlt_nicht():
    """Ein Tag, der den Zeitraum zu weniger als 80 % abdeckt (HA aus), ist
    kein Vergleichstag — eine Lücke mit 0 aufzufüllen hieße „keine Spitze"."""
    verlauf = _verlauf(sr.MIN_TAGE, lambda k, i: 9.0)
    # Tag 1: die Hälfte der Werte fehlt.
    for i, t in enumerate(_slots(56, START - timedelta(days=1))):
        if i % 2:
            verlauf.pop(sr.schluessel(t))
    kurve, tage = _kurve(verlauf, _slots(56), [False] * 56, 5.0, 10.0)
    assert tage == sr.MIN_TAGE - 1
    assert kurve[0] is None


def test_abdeckung_zaehlt_im_fenster_nicht_im_horizont():
    """Gestern zählt, auch wenn die zweite Hälfte eines 48-h-Horizonts für
    gestern noch in der Zukunft liegt — es zählt das Fenster bis zum
    nächsten Überschuss. Und eine Lücke genau in diesem Fenster schließt den
    Tag aus, selbst wenn der Rest des Horizonts vollständig ist."""
    n = 192
    ueber = [False] * 56 + [True] * 40 + [False] * 96

    def vergangenheit(tage, wert_fn):
        """Nur, was es schon gibt: jede Viertelstunde bis jetzt."""
        verlauf = {}
        t = START - timedelta(days=tage)
        while t < START:
            verlauf[sr.schluessel(t)] = wert_fn(t)
            t += timedelta(minutes=15)
        return verlauf

    gestern = START - timedelta(days=1)
    verlauf = vergangenheit(
        10, lambda t: 7.0 if gestern <= t < gestern + timedelta(hours=2) else 0.5
    )
    kurve, tage = _kurve(verlauf, _slots(n), ueber, 5.0, 10.0, min_tage=1, quantil=1.0)
    assert tage == 10
    assert kurve[0] == pytest.approx(2.0 * 8 * sr.SLOT_H / sr.ENTLADE_WIRKUNGSGRAD, abs=1e-3)
    # Vorgestern fehlen die ersten 8 Stunden der Nacht: kein Vergleichstag
    # für diese Nacht, obwohl er über den Horizont gerechnet 83 % abdeckt.
    vorgestern = START - timedelta(days=2)
    verlauf2 = {
        k: v for k, v in vergangenheit(8, lambda t: 0.5).items()
        if not (sr.schluessel(vorgestern) <= k < sr.schluessel(vorgestern + timedelta(hours=8)))
    }
    _kurve2, tage2 = _kurve(verlauf2, _slots(n), ueber, 5.0, 10.0)
    assert tage2 == 7


def test_vergleich_nach_wanduhrzeit_ueber_die_zeitumstellung():
    """Am 25.10.2026 endet die Sommerzeit. Der Vergleichstag liegt um
    Kalendertage zurück, nicht um 24-h-Blöcke: 19:00 heute vergleicht mit
    19:00 an den Tagen davor, auch wenn dazwischen eine Stunde mehr lag."""
    start = datetime(2026, 10, 26, 19, 0, tzinfo=timezone(timedelta(hours=1)))
    verlauf = {}
    for k in range(1, 11):
        # Nur 19:00 Wanduhr ist teuer.
        tag = (start - timedelta(days=k)).replace(tzinfo=None)
        verlauf[tag.strftime("%Y-%m-%dT%H:%M")] = 9.0
        verlauf[(tag + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M")] = 0.0
    kurve, tage = _kurve(verlauf, _slots(2, start), [False, False], 5.0, 10.0)
    assert tage == 10
    assert kurve[0] == pytest.approx(4.0 * sr.SLOT_H / sr.ENTLADE_WIRKUNGSGRAD, abs=1e-3)


# ---------------------------------------------------------------------------
# Prozent, Plan-Wert, Interpolation
# ---------------------------------------------------------------------------


def test_kurve_in_pct_klemmt_zwischen_mindest_und_maximalreserve():
    pct = sr.kurve_in_pct([0.0, 1.0, 5.0], 10.0, 5.0, 30.0)
    assert pct == [5.0, 10.0, 30.0]


def test_ohne_statistik_gilt_die_mindestreserve():
    assert sr.kurve_in_pct([None] * 4, 10.0, 7.0, 30.0) == [7.0] * 4
    assert sr.kurve_in_pct([None, 2.0], 10.0, 0.0, 30.0) == [0.0, 20.0]


def test_plan_wert_ist_die_reserve_des_naechsten_fensters():
    """Im Überschuss rechnet der Plan schon mit der Reserve des Abends, damit
    er tagsüber nicht verkauft, was abends fehlt."""
    pct = [0.0, 0.0, 18.0, 12.0]
    assert sr.plan_reserve_pct(pct, [True, True, False, False]) == 18.0
    assert sr.plan_reserve_pct([22.0, 18.0], [False, False]) == 22.0
    assert sr.plan_reserve_pct([0.0, 0.0], [True, True]) == 0.0
    assert sr.plan_reserve_pct([], []) is None


def test_mittagsdelle_verdeckt_die_abendreserve_nicht():
    """Ein kurzes Fenster ohne Überschuss zu Mittag (Reserve ~0) darf nicht
    der Wert sein, mit dem der Plan bis dahin rechnet — es zählt das Höchste
    bis Mitternacht."""
    mittag = datetime(2026, 10, 20, 10, 0, tzinfo=TZ)
    slots = _slots(4 * 30, mittag)
    ueber = [True] * 8 + [False] * 2 + [True] * 20 + [False] * 90
    pct = [0.0] * 8 + [1.0, 0.5] + [0.0] * 20 + [25.0] + [20.0] * 89
    assert sr.plan_reserve_pct(pct, ueber, slots) == 25.0


def test_kleiner_deckel_wird_zu_halten():
    """Bedarf knapp über dem erlaubten Netzbezug: ein Deckel von 0,1 kW
    pendelte bei jedem Rauschen mit 0 — unter DECKEL_MIN_KW heißt es halten."""
    aktiv, deckel, _ = _deckel(bedarf_kw=4.8)
    assert (aktiv, deckel) == (True, 0.0)


def test_reserve_zu_interpoliert_im_slot():
    kurve = [[t.isoformat(), p] for t, p in zip(_slots(3), [20.0, 10.0, 0.0])]
    assert sr.reserve_zu(kurve, START) == 20.0
    assert sr.reserve_zu(kurve, START + timedelta(minutes=7, seconds=30)) == pytest.approx(15.0)
    assert sr.reserve_zu(kurve, START + timedelta(minutes=30)) == 0.0
    assert sr.reserve_zu(kurve, START - timedelta(minutes=1)) is None
    assert sr.reserve_zu(kurve, START + timedelta(hours=2)) is None
    assert sr.reserve_zu([], START) is None
    assert sr.reserve_zu([["kaputt", 1]], START) is None


def test_ueberschuss_je_slot_stufenweise():
    stamps = [START, START + timedelta(minutes=30)]
    viertel = _slots(4)
    assert sr.ueberschuss_je_slot(viertel, stamps, [0.0, 3.0], [0.5, 0.5]) == [
        False, False, True, True,
    ]
    assert sr.ueberschuss_je_slot(viertel, [], [], []) == [False] * 4


def test_viertelstunden_aus_5min():
    t = datetime(2026, 10, 20, 17, 0, tzinfo=timezone.utc)
    haus = [(t + timedelta(minutes=5 * i), w) for i, w in enumerate([3.0, 6.0, 9.0, 1.0])]
    pv = [(t, 1.0)]
    werte = sr.viertelstunden_aus_5min(haus, pv)
    # (3−1 + 6 + 9) / 3; die zweite Viertelstunde hat nur einen Wert.
    assert werte == {t: pytest.approx(17.0 / 3)}


@pytest.mark.parametrize("config,erwartet", [
    ({}, (0.0, 30.0)),
    ({"spitzenkappung_reserve_min_pct": 10, "spitzenkappung_reserve_max_pct": 40}, (10.0, 40.0)),
    ({"spitzenkappung_reserve_min_pct": 50, "spitzenkappung_reserve_max_pct": 20}, (20.0, 20.0)),
    ({"spitzenkappung_reserve_max_pct": 200}, (0.0, 80.0)),
    ({"spitzenkappung_reserve_min_pct": "x", "spitzenkappung_reserve_max_pct": ""}, (0.0, 30.0)),
    ({"spitzenkappung_reserve_max_pct": 0}, (0.0, 0.0)),
    ({"spitzenkappung_reserve_min_pct": float("nan")}, (0.0, 30.0)),
])
def test_reserve_grenzen(config, erwartet):
    assert sr.reserve_grenzen_pct(config) == erwartet


def test_verlauf_uebernehmen_und_aufraeumen():
    reserve = sr.Spitzenreserve(None, "e1")
    t = datetime(2026, 10, 20, 17, 0, tzinfo=timezone.utc)
    alt = t - timedelta(days=sr.AUFBEWAHRUNG_TAGE + 2)
    neu = reserve.uebernehmen({t: 2.5, alt: 1.0, t + timedelta(minutes=15): float("nan")}, TZ)
    assert neu == 2
    assert reserve.verlauf[sr.schluessel(t.astimezone(TZ))] == 2.5
    reserve._aufraeumen(t.astimezone(TZ))
    assert len(reserve.verlauf) == 1
    assert sr.tage_im_verlauf(reserve.verlauf) == 1


# ---------------------------------------------------------------------------
# Ausführung: erlaubter Netzbezug und Entladedeckel
# ---------------------------------------------------------------------------


def test_erlaubter_netzbezug_aus_dem_restbudget_der_viertelstunde():
    """Grenze 5 kW: 1,175 kWh Budget (5 − 0,3 Sicherheit). Nach 7,5 ruhigen
    Minuten (bisher 0,5 kW gemittelt über die ganze Viertelstunde, also
    0,125 kWh) darf der Rest mehr als 5 kW."""
    erlaubt = sr.erlaubter_netzbezug_kw(5.0, {"bisher_kw": 0.5, "rest_s": 450})
    assert erlaubt == pytest.approx((4.7 * 0.25 - 0.125) / 0.125)
    # Budget schon verbraucht: nichts mehr erlaubt.
    assert sr.erlaubter_netzbezug_kw(5.0, {"bisher_kw": 5.0, "rest_s": 300}) == 0.0
    # Letzte Minute und ohne Stand: die Grenze selbst.
    assert sr.erlaubter_netzbezug_kw(5.0, {"bisher_kw": 9.0, "rest_s": 30}) == pytest.approx(4.7)
    assert sr.erlaubter_netzbezug_kw(5.0, None) == pytest.approx(4.7)


def _deckel(**kw):
    werte = dict(
        soc_pct=25.0, basis_pct=10.0, reserve_pct=20.0, war_aktiv=False,
        grenze_kw=5.0, laufend={"bisher_kw": 0.0, "rest_s": 900}, bedarf_kw=1.0,
        entlade_max_kw=5.0,
    )
    werte.update(kw)
    return sr.entladedeckel_kw(**werte)


def test_ueber_der_schwelle_ist_die_batterie_frei():
    aktiv, deckel, grund = _deckel(soc_pct=31.0)
    assert (aktiv, deckel) == (False, None)
    assert "frei" in grund


def test_in_der_reserve_haelt_sie_die_grundlast_zurueck():
    aktiv, deckel, _ = _deckel(soc_pct=29.0, bedarf_kw=1.0)
    assert (aktiv, deckel) == (True, 0.0)


def test_in_der_reserve_deckt_sie_nur_was_ueber_der_grenze_liegt():
    """Bedarf 8 kW, erlaubt 4,7 kW aus dem Netz → 3,3 kW aus der Batterie,
    geteilt durch den Wirkungsgrad."""
    aktiv, deckel, _ = _deckel(bedarf_kw=8.0)
    assert aktiv is True
    assert deckel == pytest.approx(round(3.3 / sr.ENTLADE_WIRKUNGSGRAD, 2))


def test_deckel_bleibt_unter_der_entladeleistung():
    _, deckel, _ = _deckel(bedarf_kw=20.0, entlade_max_kw=4.0)
    assert deckel == 4.0


def test_hysterese_beim_verlassen_der_reserve():
    """Einmal drin, bleibt sie, bis der Ladestand die Schwelle um 1 Punkt
    überschreitet — sonst schaltet jedes Prozent Rauschen hin und her."""
    assert _deckel(soc_pct=30.5, war_aktiv=True)[0] is True
    assert _deckel(soc_pct=30.5, war_aktiv=False)[0] is False
    assert _deckel(soc_pct=31.5, war_aktiv=True)[0] is False


def test_am_mindest_ladestand_gibt_sie_nichts_mehr_ab():
    aktiv, deckel, grund = _deckel(soc_pct=10.2, bedarf_kw=9.0)
    assert (aktiv, deckel) == (True, 0.0)
    assert "Mindest-Ladestand" in grund


def test_ohne_messung_bleibt_der_deckel():
    assert _deckel(soc_pct=None, war_aktiv=True)[:2] == (True, None)
    assert _deckel(bedarf_kw=None)[:2] == (True, None)


def test_ohne_reserve_ist_die_schwelle_der_mindest_ladestand():
    assert _deckel(soc_pct=11.0, reserve_pct=0.0)[0] is False



def test_hysterese_am_geschriebenen_deckel():
    # Bedarf 4,9 kW → (4,9 − 4,7) / 0,95 ≈ 0,21 kW
    assert _deckel(bedarf_kw=4.9)[1] == pytest.approx(0.21, abs=0.01)
    assert _deckel(bedarf_kw=4.9, bisher_kw=0.0)[1] == 0.0
    assert _deckel(bedarf_kw=4.9, bisher_kw=0.5)[1] == pytest.approx(0.21, abs=0.01)
    assert _deckel(bedarf_kw=5.2, bisher_kw=0.0)[1] == pytest.approx(0.53, abs=0.01)
