"""forderungen.py direkt — ohne LP, ohne pandas.

Die Rechnung selbst prüfen die Fahrplan-Tests (test_ladeziel,
test_netzladen_abend, test_spitzenreserve_plan) und der Replay-Korpus über
``HAConfig``. Hier stehen die Regeln vom 03.11.2026: Mindestladung am
Sonnenuntergang, Termine bleiben bis zum Sonnenuntergang, am Planungsrand wird
jede Forderung abgesenkt statt gestrichen.
"""

import pytest

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.eeg_energy_optimizer import forderungen as fo

WIEN = ZoneInfo("Europe/Vienna")
START = datetime(2026, 11, 3, 13, 15, tzinfo=WIEN)
# Traun, aus Home Assistant (sun.sun): heute 16:41, morgen 16:39.
UNTERGAENGE = [
    datetime(2026, 11, 3, 16, 41, tzinfo=WIEN),
    datetime(2026, 11, 4, 16, 39, tzinfo=WIEN),
]


def _rechnung(abend_kwh=0.0, ladeziel_kwh=0.0, slots=4 * 30, stand=1.5,
              start=START, untergaenge=UNTERGAENGE, pv_faktor=1.0):
    """Traun 03.11.2026 nachgestellt: Überschuss heute schon vorbei (PV nur
    noch unter der Hauslast), morgen 10–14 Uhr Sonne."""
    stempel = [start + timedelta(minutes=15 * k) for k in range(slots)]
    pv = []
    for t in stempel:
        morgen = t.date() > START.date() and 10 <= t.hour < 14
        heute = t.date() == START.date() and t.hour < 16
        pv.append(pv_faktor * (2.5 if morgen else 0.6 if heute else 0.0))
    haus = [1.0] * slots
    ueberschuss = [p - h / 0.95 for p, h in zip(pv, haus)]
    akku = fo.Akku(kapazitaet=13.5, frei=13.5 - stand, leistung=5.0,
                   ac_limit=8.0, eta=0.95, p2e=0.25)
    lage = fo.Lage(stempel=stempel, ueberschuss=ueberschuss, pv=pv,
                   haus=haus, einspeisegrenze=[5.0] * slots,
                   preise=[0.25] * slots, fest=[True] * slots)
    vorgaben = fo.Vorgaben(abend_kwh=abend_kwh, ladeziel_kwh=ladeziel_kwh,
                           netzladen_max_kw=4.0, sonnenuntergaenge=untergaenge)
    return fo.Forderungen(lage, akku, vorgaben)


def _uhr(rechnung, i):
    return rechnung.lage.stempel[i].strftime("%d. %H:%M")


def test_mindestladung_steht_beim_sonnenuntergang():
    """Traun 03.11.2026: Der Überschuss war um 13:15 schon vorbei. Mit dem
    Termin „Ende der PV-Zeit" forderte für den Abend nichts mehr — die
    Batterie blieb bei 21 % statt 60 %. Jetzt steht die Mindestladung im
    Slot des Sonnenuntergangs (16:30–16:45), heute und morgen."""
    rechnung = _rechnung(abend_kwh=6.0)
    assert [_uhr(rechnung, i) for i in rechnung.termine_abend] == ["03. 16:30", "04. 16:30"]
    assert rechnung.pflicht is not None, "heute muss aus dem Netz nachgeladen werden"
    heute = rechnung.termine_abend[0]
    assert any(p < 0 for p in rechnung.pflicht[: heute + 1])


def test_auch_ein_tag_ganz_ohne_pv_hat_einen_termin():
    rechnung = _rechnung(abend_kwh=6.0, pv_faktor=0.0)
    assert len(rechnung.termine_abend) == 2


def test_termin_kurz_vor_sonnenuntergang_bleibt():
    """Um 16:00 gerechnet endet der Untergangs-Slot in 45 Minuten. Bis 2.1.66
    entfiel der Termin dann und brach die laufende Netzladung ab — Traun
    03.11.2026: Freigabe um 16:00 bei 30 % statt 60 %. Er bleibt jetzt."""
    rechnung = _rechnung(abend_kwh=6.0, start=datetime(2026, 11, 3, 16, 0, tzinfo=WIEN))
    assert [_uhr(rechnung, i) for i in rechnung.termine_abend] == ["03. 16:30", "04. 16:30"]
    assert rechnung.pflicht is not None and rechnung.pflicht[0] < 0

def test_ohne_bekannte_untergaenge_rechnet_der_ersatzstandort():
    rechnung = _rechnung(abend_kwh=6.0, untergaenge=None)
    assert [_uhr(rechnung, i) for i in rechnung.termine_abend] == ["03. 16:30", "04. 16:30"]


def test_noaa_sonnenuntergang_traun():
    """NOAA gegen Home Assistant (sun.sun next_setting 16:40:56) — auf zwei
    Minuten genau reicht für den Termin einer Viertelstunde."""
    u = fo.sonnenuntergang(date(2026, 11, 3), 48.226, 14.220, WIEN)
    soll = datetime(2026, 11, 3, 16, 40, 56, tzinfo=WIEN)
    assert abs((u - soll).total_seconds()) < 180


def test_ladeziel_bleibt_am_ende_der_pv_zeit():
    """Das Ladeziel kauft nie — sein Termin bleibt das Ende der PV-Zeit
    (morgen 13:45, der letzte Überschuss-Slot)."""
    rechnung = _rechnung(ladeziel_kwh=8.0)
    assert [_uhr(rechnung, i) for i in rechnung.termine_ladeziel] == ["04. 13:45"]


def test_am_rand_abgesenkt_nicht_gestrichen():
    """Der Horizont endet morgen kurz nach dem Untergang bzw. dem PV-Ende.
    Ein volles Ziel dort käme nicht mehr auf den Endstand — es bleibt als
    Termin, aber abgesenkt, für Ladeziel und Mindestladung nach derselben
    Regel."""
    for rechnung, reihe in (
        (_rechnung(ladeziel_kwh=13.0, slots=4 * 25 + 3), "ziel"),     # bis 14:30
        (_rechnung(abend_kwh=13.0, slots=4 * 27 + 3), "netz_forderung"),  # bis 17:00
    ):
        werte = getattr(rechnung, reihe)
        rest, ende = rechnung.abbau_bis_ende
        termin = max(i for i, f in enumerate(werte) if f > 0)
        assert rechnung.lage.stempel[termin].date() > START.date(), reihe
        assert 0.0 < werte[termin] < 13.0, reihe
        assert termin in rechnung.am_rand_abgesenkt, reihe
        for k, f in enumerate(werte):
            assert f <= ende + rest[k + 1] + 1e-9, (reihe, k)


def test_mindestladung_laedt_mit_puffer_frueher(monkeypatch):
    """Traun 03.11.2026, 14:21: Mit voller Netzladeleistung gerechnet lud der
    Plan genau in den letzten Viertelstunden vor dem Termin — braucht das
    Haus mehr, bleibt unter der Netzgrenze weniger für die Batterie, und
    nach hinten ist kein Platz. Mit 70 % der Leistung beginnt die
    Pflichtladung früher, und schon vor dem Termin wird ein Teil gefordert."""
    def erster_pflicht_slot(anteil):
        monkeypatch.setattr(fo, "MINDESTLADUNG_LEISTUNG_ANTEIL", anteil)
        rechnung = _rechnung(abend_kwh=6.0, stand=0.0)
        return min(i for i, p in enumerate(rechnung.pflicht) if p < 0), rechnung

    anteil = fo.MINDESTLADUNG_LEISTUNG_ANTEIL
    ohne_puffer, _ = erster_pflicht_slot(1.0)
    mit_puffer, rechnung = erster_pflicht_slot(anteil)
    assert mit_puffer < ohne_puffer
    termin = rechnung.termine_abend[0]
    assert 0.0 < rechnung.netz_forderung[termin - 4] < rechnung.netz_forderung[termin]
    # Der Grund nennt auch vor dem Termin die Mindestladung, nicht die Reserve.
    assert rechnung.abend_termin_je_slot[termin - 4] == termin


def test_begrenzung_bis_nennt_die_spitzenkappung():
    """Wolfsteiner 03.11.2026: Haus 4,5 kW, Spitzenkappung 5 kW — bis zum
    Termin konnten je Viertelstunde nur 0,2 kW aus dem Netz in die Batterie."""
    rechnung = _rechnung(abend_kwh=6.0)
    assert rechnung.begrenzung_bis(rechnung.termine_abend[0]) is None
    rechnung.lage.haus[:] = [4.5] * len(rechnung.lage.haus)
    rechnung.v.reserve_aktiv = True
    rechnung.v.kappung_standard_kw = 5.0
    termin = rechnung.termine_abend[0]
    b = rechnung.begrenzung_bis(termin)
    assert b["durch"] == "kappung" and b["grenze_kw"] == 5.0
    assert b["slots"] == b["von"] == termin + 1
    assert b["moeglich_kwh"] == round(0.2 * 0.25 * (termin + 1), 2)


def _mit_flacher_reserve(rechnung, boden_basis=11.0, boden=11.0, reserve=10.0):
    """Spitzenkappung mit „Reserve notfalls aus dem Netz“ und nur der
    Mindestreserve: die Kurve ist überall gleich, sie fällt nirgends."""
    schritt = timedelta(minutes=15)
    stempel = rechnung.lage.stempel + [rechnung.lage.stempel[-1] + schritt]
    v = rechnung.v
    v.spitzenreserve_netz_kw = 5.0
    v.reserve_aktiv = True
    v.reserve_pct = {int(t.timestamp()): reserve for t in stempel}
    v.kappung_standard_kw = 6.0
    v.kapazitaet_echt_kwh = 13.5
    v.boden_basis_pct = boden_basis
    v.boden_pct = boden
    return rechnung


def test_mindestreserve_wird_aus_dem_netz_nachgeladen():
    """Werenka 29.10.–04.11.2026: Mindestreserve 10 % mit „notfalls aus dem
    Netz“, Ladestand sechs Tage bei 9–13 %, keine Netzladung — eine flache
    Kurve forderte nie etwas. Jetzt gilt sie zum nächsten Sonnenuntergang."""
    rechnung = _mit_flacher_reserve(_rechnung(stand=0.0))
    termin = rechnung.untergaenge[0]
    assert _uhr(rechnung, termin) == "03. 16:30"
    forderung = rechnung.netz_forderung
    assert forderung[termin] == pytest.approx(1.35)
    assert not any(forderung[:termin]), "bis dahin ist jeder Slot gleich gut"
    assert rechnung.pflicht is not None
    assert any(p < 0 for p in rechnung.pflicht[: termin + 1])
    assert termin in rechnung.mindestreserve_slots


def test_gehaltene_mindestreserve_fordert_nichts():
    """Hält der Boden die Reserve schon, ist nichts nachzuladen."""
    rechnung = _mit_flacher_reserve(_rechnung(stand=5.0), boden=21.0)
    assert rechnung.reserve_forderung is None


def test_mindestreserve_ohne_netz_schalter_fordert_nichts():
    rechnung = _mit_flacher_reserve(_rechnung(stand=0.0))
    rechnung.v.spitzenreserve_netz_kw = 0.0
    assert rechnung.reserve_forderung is None


def _mit_eeg_bezug(rechnung, vorteil=0.0):
    """Vienna 06.11.2026: untertags Bezug aus der EEG zu 19,1 ct, ab dem
    Sonnenuntergang bis 10 Uhr früh der Lieferant zu 22,8 ct; Alterung 1 ct."""
    untergaenge = {u.date(): u for u in UNTERGAENGE}
    rechnung.lage.preise = [
        0.2284 if (u := untergaenge.get(t.date())) is not None and t >= u
        or t.hour < 10 else 0.1912
        for t in rechnung.lage.stempel
    ]
    rechnung.v.battery_cost = 0.01
    rechnung.v.netzladen_vorteil = vorteil
    return rechnung


def test_wirtschaftliches_netzladen_bis_zum_sonnenuntergang():
    """Vienna 06.11.2026: Der Überschuss endete mittags (heute gar keiner).
    Bis 2.1.78 durfte wirtschaftlich nur bis zum PV-Ende geladen werden — am
    trüben Tag gar nicht, das Ladeziel 100 % blieb bei der Mindestladung
    (91 %), obwohl der Tagesbezug billiger war als die Nacht. Jetzt gilt der
    Nachmittag bis zum Sonnenuntergang, und dort steht auch das Ladeziel."""
    rechnung = _mit_eeg_bezug(_rechnung(ladeziel_kwh=13.0))
    w = rechnung.wirtschaftlich
    assert w is not None
    heute = rechnung.untergaenge[0]
    assert all(w[j] < 0 for j in range(0, heute + 1)), "heute bis 16:45 günstig"
    assert w[heute + 1] == 0.0, "nach Sonnenuntergang ist es die teure Nacht"
    assert [_uhr(rechnung, i) for i in rechnung.termine_ladeziel] == ["03. 16:30", "04. 16:30"]


def test_mindestvorteil_schliesst_das_fenster_wieder():
    """19,1 ct ÷ 0,95² + 1 ct Alterung = 22,2 ct < 22,8 ct; mit 1 ct
    Mindestvorteil 23,2 ct — dann lohnt es nicht, und das Ladeziel bleibt am
    PV-Ende."""
    rechnung = _mit_eeg_bezug(_rechnung(ladeziel_kwh=13.0), vorteil=0.01)
    assert rechnung.wirtschaftlich is None
    assert [_uhr(rechnung, i) for i in rechnung.termine_ladeziel] == ["04. 13:45"]
