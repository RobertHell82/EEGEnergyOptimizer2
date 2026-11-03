"""forderungen.py direkt — ohne LP, ohne pandas.

Die Rechnung selbst prüfen die Fahrplan-Tests (test_ladeziel,
test_netzladen_abend, test_spitzenreserve_plan) und der Replay-Korpus über
``HAConfig``. Hier stehen die Regeln, die mit dem Umbau am 03.11.2026 neu
kamen: Mindestladungs-Termine unter einer Stunde entfallen, und am
Planungsrand wird jede Forderung abgesenkt statt gestrichen.
"""

from datetime import datetime, timedelta

from custom_components.eeg_energy_optimizer import forderungen as fo

START = datetime(2026, 10, 28, 15, 45)


def _rechnung(abend_kwh=0.0, ladeziel_kwh=0.0, slots=4 * 26, stand=2.0):
    """Ende des Überschusses heute 16:00 (zweiter Slot), morgen 9–15 Uhr Sonne."""
    stempel = [START + timedelta(minutes=15 * k) for k in range(slots)]
    ueberschuss = []
    for k, t in enumerate(stempel):
        heute_noch = k < 2
        morgen = t.date() > START.date() and 9 <= t.hour < 15
        ueberschuss.append(1.0 if heute_noch or morgen else -0.4)
    pv = [w + 0.4 if w > 0 else 0.2 if 7 <= t.hour < 17 else 0.0
          for w, t in zip(ueberschuss, stempel)]
    akku = fo.Akku(kapazitaet=10.0, frei=10.0 - stand, leistung=5.0,
                   ac_limit=8.0, eta=0.95, p2e=0.25)
    lage = fo.Lage(stempel=stempel, ueberschuss=ueberschuss, pv=pv,
                   haus=[0.4] * slots, einspeisegrenze=[5.0] * slots,
                   preise=[0.25] * slots, fest=[True] * slots)
    vorgaben = fo.Vorgaben(abend_kwh=abend_kwh, ladeziel_kwh=ladeziel_kwh,
                           netzladen_max_kw=5.0)
    return fo.Forderungen(lage, akku, vorgaben)


def test_mindestladung_termin_unter_einer_stunde_entfaellt():
    """Der Überschuss endet um 16:00 (Slot 1, Ende 16:15 — eine halbe
    Stunde ab jetzt). Früher kaufte der Plan dafür sofort mit voller
    Leistung (Kostal 28.10.2026). Der Termin von morgen bleibt."""
    rechnung = _rechnung(abend_kwh=8.0)
    assert 1 not in rechnung.termine_abend
    assert rechnung.termine_abend, "der Termin von morgen fehlt"
    morgen = rechnung.lage.stempel[rechnung.termine_abend[0]]
    assert morgen.date() > START.date()
    # Ohne Termin heute fordert auch niemand heute etwas.
    assert all(f == 0.0 for f in rechnung.netz_forderung[:8])


def test_ladeziel_behaelt_den_nahen_termin():
    """Das Ladeziel kauft nie — es darf am PV-Ende von heute bleiben und
    hält die Batterie davon ab, kurz davor noch einzuspeisen."""
    rechnung = _rechnung(ladeziel_kwh=8.0)
    assert 1 in rechnung.termine_ladeziel


def test_am_rand_abgesenkt_nicht_gestrichen():
    """Der Horizont endet morgen kurz nach dem PV-Ende. Ein volles Ziel dort
    käme nicht mehr auf den Endstand — es bleibt als Termin, aber abgesenkt,
    für Ladeziel und Mindestladung nach derselben Regel."""
    slots = 4 * 24  # bis morgen 15:45 — PV-Ende 14:45, eine Stunde Rand
    for rechnung, reihe in (
        (_rechnung(ladeziel_kwh=9.5, slots=slots), "ziel"),
        (_rechnung(abend_kwh=9.5, slots=slots), "netz_forderung"),
    ):
        werte = getattr(rechnung, reihe)
        rest, ende = rechnung.abbau_bis_ende
        termin = max(i for i, f in enumerate(werte) if f > 0)
        assert rechnung.lage.stempel[termin].date() > START.date(), reihe
        assert 0.0 < werte[termin] < 9.5, reihe
        for k, f in enumerate(werte):
            assert f <= ende + rest[k + 1] + 1e-9, (reihe, k)
