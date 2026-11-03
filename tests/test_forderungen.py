"""forderungen.py direkt — ohne LP, ohne pandas.

Die Rechnung selbst prüfen die Fahrplan-Tests (test_ladeziel,
test_netzladen_abend, test_spitzenreserve_plan) und der Replay-Korpus über
``HAConfig``. Hier stehen die Regeln vom 03.11.2026: Mindestladung am
Sonnenuntergang, Termine unter einer Stunde entfallen, am Planungsrand wird
jede Forderung abgesenkt statt gestrichen.
"""

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


def test_termin_unter_einer_stunde_entfaellt():
    """Um 16:00 gerechnet endet der Untergangs-Slot in 45 Minuten — kein
    Sofortkauf mit voller Leistung (Kostal 28.10.2026). Morgen bleibt."""
    rechnung = _rechnung(abend_kwh=6.0, start=datetime(2026, 11, 3, 16, 0, tzinfo=WIEN))
    assert [_uhr(rechnung, i) for i in rechnung.termine_abend] == ["04. 16:30"]


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
