"""Tests für den Monatstarif aWATTar SUNNY (awattar_sunny.py).

aWATTar bietet dafür keine Schnittstelle; gelesen werden die Preistabelle
(Google Sheet als CSV je Jahres-Tab) und als Rückfall die Tarifseite. Geprüft
wird das Zerlegen beider Quellen mit echten Auszügen vom 09.09.2026, die Wahl
von Monat und Vertragsvariante und das Verhalten, wenn eine Quelle ausfällt.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from custom_components.eeg_energy_optimizer import awattar_sunny as sunny

# gviz-CSV des Tabs „2026", wie am 09.09.2026 geliefert: Kopfzellen mit
# Zeilenumbruch, Werte mit Dezimalpunkt, leere Zeilen für künftige Monate,
# zwei SUNNY-Spalten (Verträge bis 25.02.2026 / danach).
ECHTES_CSV = '''"","Jahr ","MONTHLY Strombezug ","SUNNY
Einspeisevergütung Vertragsabschluss bis 25.02.2026","SUNNY
Einspeisevergütung "
"1","2026","13.656","10.969","10.969"
"2","2026","14.063","11.769","9.950"
"3","2026","11.814","8.602","5.415"
"4","2026","11.331","3.368","3.368"
"5","2026","9.963","1.223","1.223"
"6","2026","10.917","3.622","3.622"
"7","2026","12.198","6.464","6.464"
"8","2026","13.255","5.491","5.491"
"9","2026","16.768","8.989","8.989"
"10","2026","","",""
"11","2026","","",""
"12","2026","","",""
'''

# Tarifübersicht der Seite awattar.at/tariffs/sunny (gekürzt, Aufbau unverändert).
ECHTE_TARIFSEITE = '''<table class="table table-striped"><thead><tr><th>Beschreibung</th><th colspan="2">Wert</th></tr></thead>
<tbody><tr><td>Einspeisevergütung<p style="margin-bottom: 0px"><small>aktuell</small></p></td>
<td>8,989 Cent/kWh <p style="margin-bottom: 0px"><small>netto</small></p></td>
<td>8,989 Cent/kWh <p style="margin-bottom: 0px"><small>brutto</small></p></td></tr>
<tr><td>Preis&auml;nderung</td><td colspan="2">monatlich</td></tr>
<tr><td>Preisberechnungsmethode</td><td colspan="2">EEX Month Futures</td></tr></tbody></table>
<table class="table table-striped"><tbody><tr><th>Zahlung</th><th>Netto</th><th>Brutto</th></tr>
<tr><td>monatlich</td><td>4,79 Euro/Monat</td><td>5,75 Euro/Monat</td></tr></tbody></table>'''

JETZT = datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Preistabelle
# ---------------------------------------------------------------------------


def test_tabelle_wird_zerlegt():
    tab = sunny.parse_tabelle(ECHTES_CSV, 2026)

    assert tab.kopf_erkannt
    assert len(tab.neu) == 9 and len(tab.alt) == 9
    assert tab.neu[202609] == pytest.approx(0.08989)
    assert tab.neu[202605] == pytest.approx(0.01223)
    # März: die beiden Vertragsvarianten liegen mehr als 3 ct auseinander.
    assert tab.neu[202603] == pytest.approx(0.05415)
    assert tab.alt[202603] == pytest.approx(0.08602)
    # Leere Monate fallen heraus.
    assert 202610 not in tab.neu and 202612 not in tab.alt
    assert tab.alt_bis == "25.02.2026"


def test_monthly_spalte_wird_nicht_verwechselt():
    """Spalte 3 ist der Bezugstarif MONTHLY (16,768), nicht SUNNY."""
    tab = sunny.parse_tabelle(ECHTES_CSV, 2026)
    assert tab.neu[202609] != pytest.approx(0.16768)
    assert tab.alt[202609] != pytest.approx(0.16768)


def test_eine_sunny_spalte_gilt_fuer_beide_vertraege():
    """Ältere Jahres-Tabs (und künftige ohne Stichtag) haben nur eine Spalte."""
    csv_text = (
        '"","Jahr","MONTHLY","SUNNY Einspeisevergütung"\n'
        '"1","2025","14.52","12.18"\n'
        '"2","2025","13.98","11.02"\n'
    )
    tab = sunny.parse_tabelle(csv_text, 2025)
    assert tab.neu == tab.alt
    assert tab.neu[202501] == pytest.approx(0.1218)
    assert tab.alt_bis is None


def test_dezimalkomma_und_gleitkomma_monate():
    csv_text = '"","Jahr","SUNNY"\n"1.0","2026.0","10,969"\n'
    tab = sunny.parse_tabelle(csv_text, 2026)
    assert tab.neu[202601] == pytest.approx(0.10969)


def test_fremdes_jahr_und_muell_fallen_heraus():
    csv_text = (
        '"","Jahr","SUNNY"\n'
        '"1","2025","10.0"\n'      # anderes Jahr im falschen Tab
        '"2","2026","9.0"\n'
        '"x","2026","8.0"\n'       # kein Monat
        '"13","2026","7.0"\n'      # kein Monat
        '"3","2026","abc"\n'       # kein Preis
    )
    tab = sunny.parse_tabelle(csv_text, 2026)
    assert tab.neu == {202602: pytest.approx(0.09)}


def test_leerer_jahres_tab_ist_lesbar_aber_leer():
    """Am Jahresanfang steht der neue Tab mit Kopf, aber ohne Werte."""
    csv_text = '"","Jahr","MONTHLY","SUNNY"\n"1","2027","",""\n"2","2027","",""\n'
    tab = sunny.parse_tabelle(csv_text, 2027)
    assert tab.kopf_erkannt
    assert tab.leer()


@pytest.mark.parametrize("text", [
    "", None, "<html><body>Anmelden</body></html>",
    '"Jahr","MONTHLY"\n"1","2026","13.6"\n',   # kein SUNNY
])
def test_unlesbare_tabelle_ergibt_nichts(text):
    tab = sunny.parse_tabelle(text, 2026)
    assert tab.leer()
    assert not tab.kopf_erkannt


# ---------------------------------------------------------------------------
# Tarifseite
# ---------------------------------------------------------------------------


def test_tarifseite_liefert_den_nettowert():
    assert sunny.parse_tarifseite(ECHTE_TARIFSEITE) == pytest.approx(0.08989)


@pytest.mark.parametrize("html", ["", None, "<p>Seite umgebaut</p>",
                                  "<td>4,79 Euro/Monat</td>", "8,989 Cent/kWh brutto"])
def test_unlesbare_tarifseite_ergibt_nichts(html):
    assert sunny.parse_tarifseite(html) is None


# ---------------------------------------------------------------------------
# Monatswahl
# ---------------------------------------------------------------------------


def test_laufender_monat_fehlt_dann_gilt_der_vormonat():
    tab = sunny.parse_tabelle(ECHTES_CSV, 2026)
    assert sunny.tarif_fuer(tab.neu, 202610) == (pytest.approx(0.08989), 202609)
    assert sunny.tarif_fuer(tab.neu, 202609) == (pytest.approx(0.08989), 202609)
    assert sunny.tarif_fuer(tab.neu, 202604) == (pytest.approx(0.03368), 202604)


def test_jahreswechsel_nimmt_den_dezember():
    tarife = {202511: 0.05, 202512: 0.06}
    assert sunny.tarif_fuer(tarife, 202601) == (0.06, 202512)
    # Nur spätere Monate bekannt: der jüngste davon.
    assert sunny.tarif_fuer({202603: 0.07}, 202601) == (0.07, 202603)
    assert sunny.tarif_fuer({}, 202601) is None


def test_monatsschluessel_rundreise():
    assert sunny.monatsschluessel(2026, 9) == 202609
    assert sunny.jahr_monat(202609) == (2026, 9)
    assert sunny.jahr_monat(202512) == (2025, 12)


# ---------------------------------------------------------------------------
# Anbieter: Abruf, Rückfälle, Fristen
# ---------------------------------------------------------------------------


@pytest.fixture
def uhr(monkeypatch):
    """Laufender Monat und Uhrzeit, von den Tests verstellbar."""
    zustand = {"schluessel": 202609, "jetzt": JETZT}
    monkeypatch.setattr(sunny, "aktueller_schluessel", lambda: zustand["schluessel"])
    monkeypatch.setattr(sunny, "_utcnow", lambda: zustand["jetzt"])
    monkeypatch.setattr(sunny, "async_get_clientsession", lambda hass: object())
    return zustand


def _quellen(provider, zaehler=None, **antworten):
    """Antworten je URL-Bruchstück: Text oder Exception (wird geworfen)."""

    async def hole(session, url):
        if zaehler is not None:
            zaehler.append(url)
        for bruchstueck, antwort in antworten.items():
            if bruchstueck in url:
                if isinstance(antwort, Exception):
                    raise antwort
                return antwort
        raise AssertionError(f"unerwartete URL {url}")

    return patch.object(provider, "_hole_text", side_effect=hole)


async def test_abruf_liest_tabelle_und_waehlt_den_vertrag(uhr):
    p = sunny.AwattarSunnyProvider(hass=None, entry_id="e1")
    with _quellen(p, **{"sheet=2026": ECHTES_CSV}):
        await p.async_fetch()

    assert p.hat_daten()
    assert p.preis_fuer("neu") == pytest.approx(0.08989)
    assert p.preis_fuer("alt") == pytest.approx(0.08989)

    uhr["schluessel"] = 202603
    assert p.preis_fuer("neu") == pytest.approx(0.05415)
    assert p.preis_fuer("alt") == pytest.approx(0.08602)
    # Unbekannte Variante zählt als aktueller Vertrag.
    assert p.preis_fuer("unsinn") == pytest.approx(0.05415)

    st = p.status()
    assert st["quelle"] == sunny.QUELLE_TABELLE
    assert st["fehler"] is None
    assert st["alt_bis"] == "25.02.2026"
    assert st["alter_minuten"] == 0
    assert st["neu"] == {"preis": pytest.approx(0.05415), "jahr": 2026, "monat": 3}
    assert st["alt"] == {"preis": pytest.approx(0.08602), "jahr": 2026, "monat": 3}


async def test_laufender_monat_fehlt_dann_hilft_die_tarifseite(uhr):
    """1. Oktober: die Tabelle führt den Oktober noch nicht. Der Wert kommt
    von der Tarifseite — nur für den aktuellen Vertrag; der Altvertrag bleibt
    beim September, ausgewiesen als solcher."""
    uhr["schluessel"] = 202610
    p = sunny.AwattarSunnyProvider(hass=None, entry_id="e1")
    seite = ECHTE_TARIFSEITE.replace("8,989", "12,767")
    with _quellen(p, **{"sheet=2026": ECHTES_CSV, "tariffs/sunny": seite}):
        await p.async_fetch()

    assert p.preis_fuer("neu") == pytest.approx(0.12767)
    assert p.preis_fuer("alt") == pytest.approx(0.08989)
    st = p.status()
    assert st["quelle"] == sunny.QUELLE_TARIFSEITE
    assert st["fehler"] is None
    assert st["neu"]["monat"] == 10
    assert st["alt"]["monat"] == 9


async def test_tabelle_kaputt_tarifseite_rettet_den_aktuellen_vertrag(uhr):
    p = sunny.AwattarSunnyProvider(hass=None, entry_id="e1")
    with _quellen(p, **{"sheet=2026": RuntimeError("HTTP 500"),
                        "tariffs/sunny": ECHTE_TARIFSEITE}):
        await p.async_fetch()

    assert p.preis_fuer("neu") == pytest.approx(0.08989)
    assert p.preis_fuer("alt") is None      # die Seite kennt den Altvertrag nicht
    st = p.status()
    assert st["quelle"] == sunny.QUELLE_TARIFSEITE
    assert st["fehler"] is None
    assert st["alt"] is None


async def test_alles_kaputt_laesst_den_alten_wert_stehen(uhr):
    p = sunny.AwattarSunnyProvider(hass=None, entry_id="e1")
    with _quellen(p, **{"sheet=2026": ECHTES_CSV}):
        await p.async_fetch()
    geholt = p._geholt

    uhr["schluessel"] = 202610
    uhr["jetzt"] = JETZT + timedelta(days=22)
    with _quellen(p, **{"sheet=2026": RuntimeError("HTTP 500"),
                        "tariffs/sunny": RuntimeError("timeout")}):
        await p.async_fetch(force=True)

    # Rückfall auf den September, der Fehler steht dabei, der Abrufzeitpunkt
    # bleibt der des letzten Erfolgs.
    assert p.preis_fuer("neu") == pytest.approx(0.08989)
    st = p.status()
    assert "HTTP 500" in st["fehler"] and "timeout" in st["fehler"]
    assert st["neu"]["monat"] == 9
    assert p._geholt == geholt


async def test_ohne_jeden_wert_bleibt_es_bei_none(uhr):
    p = sunny.AwattarSunnyProvider(hass=None, entry_id="e1")
    with _quellen(p, **{"sheet=2026": RuntimeError("HTTP 500"),
                        "tariffs/sunny": RuntimeError("timeout")}):
        await p.async_fetch()
    assert not p.hat_daten()
    assert p.preis_fuer("neu") is None
    st = p.status()
    assert st["neu"] is None and st["alt"] is None
    assert st["fehler"]


async def test_frische_frist_haelt_abrufe_zurueck(uhr):
    p = sunny.AwattarSunnyProvider(hass=None, entry_id="e1")
    abrufe: list[str] = []
    with _quellen(p, abrufe, **{"sheet=2026": ECHTES_CSV, "tariffs/sunny": ECHTE_TARIFSEITE}):
        await p.async_fetch()
        assert len(abrufe) == 1

        # Laufender Monat bekannt: zwölf Stunden Ruhe.
        uhr["jetzt"] = JETZT + timedelta(hours=6)
        await p.async_fetch()
        assert len(abrufe) == 1
        uhr["jetzt"] = JETZT + timedelta(hours=13)
        await p.async_fetch()
        assert len(abrufe) == 2

        # Laufender Monat fehlt (Monatsanfang): stündlich nachsehen — Tabelle
        # und Tarifseite.
        uhr["schluessel"] = 202610
        uhr["jetzt"] = JETZT + timedelta(hours=13, minutes=30)
        await p.async_fetch()
        assert len(abrufe) == 2
        uhr["jetzt"] = JETZT + timedelta(hours=14, minutes=30)
        await p.async_fetch()
        assert len(abrufe) == 4
        assert "tariffs/sunny" in abrufe[-1]


async def test_alter_wert_verfaellt_nach_40_tagen(uhr):
    p = sunny.AwattarSunnyProvider(hass=None, entry_id="e1")
    with _quellen(p, **{"sheet=2026": ECHTES_CSV}):
        await p.async_fetch()
    uhr["jetzt"] = JETZT + timedelta(days=41)
    assert p.preis_fuer("neu") is None
    assert p.status()["neu"] is None


async def test_jahresanfang_liest_auch_das_vorjahr(uhr):
    """Im Jänner ist der neue Tab oft noch leer; der Dezember steht im alten.
    Antwortet auch die Tarifseite nicht, gilt der Dezember für beide."""
    uhr["schluessel"] = 202701
    leer_2027 = '"","Jahr","MONTHLY","SUNNY"\n"1","2027","",""\n'
    p = sunny.AwattarSunnyProvider(hass=None, entry_id="e1")
    with _quellen(p, **{"sheet=2027": leer_2027, "sheet=2026": ECHTES_CSV,
                        "tariffs/sunny": RuntimeError("timeout")}):
        await p.async_fetch()

    assert p.preis_fuer("neu") == pytest.approx(0.08989)
    st = p.status()
    assert st["quelle"] == sunny.QUELLE_TABELLE
    assert st["neu"] == {"preis": pytest.approx(0.08989), "jahr": 2026, "monat": 9}
    assert "Tarifseite" in st["fehler"]


async def test_vorjahr_wird_nur_am_jahresanfang_gelesen(uhr):
    """Ab März wird der Vorjahres-Tab nicht mehr angefasst — ein leerer
    aktueller Tab wäre dann ein echtes Problem, kein Jahreswechsel."""
    uhr["schluessel"] = 202703
    leer_2027 = '"","Jahr","MONTHLY","SUNNY"\n"3","2027","",""\n'
    p = sunny.AwattarSunnyProvider(hass=None, entry_id="e1")
    abrufe: list[str] = []
    with _quellen(p, abrufe, **{"sheet=2027": leer_2027, "tariffs/sunny": ECHTE_TARIFSEITE}):
        await p.async_fetch()
    assert not any("sheet=2026" in u for u in abrufe)
    assert p.preis_fuer("neu") == pytest.approx(0.08989)
    assert p.status()["quelle"] == sunny.QUELLE_TARIFSEITE


async def test_neue_werte_legen_sich_ueber_alte(uhr):
    """Erst hilft die Tarifseite über den Monatsanfang, dann veröffentlicht
    die Tabelle den echten Wert — er gewinnt, auch für den Altvertrag."""
    uhr["schluessel"] = 202610
    p = sunny.AwattarSunnyProvider(hass=None, entry_id="e1")
    with _quellen(p, **{"sheet=2026": ECHTES_CSV,
                        "tariffs/sunny": ECHTE_TARIFSEITE.replace("8,989", "12,700")}):
        await p.async_fetch()
    assert p.preis_fuer("neu") == pytest.approx(0.127)
    assert p.preis_fuer("alt") == pytest.approx(0.08989)

    mit_oktober = ECHTES_CSV.replace('"10","2026","","",""', '"10","2026","19.6","11.851","12.767"')
    with _quellen(p, **{"sheet=2026": mit_oktober}):
        await p.async_fetch(force=True)
    assert p.preis_fuer("neu") == pytest.approx(0.12767)
    assert p.preis_fuer("alt") == pytest.approx(0.11851)
    assert p.status()["quelle"] == sunny.QUELLE_TABELLE


async def test_ohne_home_assistant_kein_abruf(uhr, monkeypatch):
    monkeypatch.setattr(sunny, "async_get_clientsession", None)
    p = sunny.AwattarSunnyProvider(hass=None, entry_id="e1")
    with _quellen(p, **{"sheet=2026": ECHTES_CSV}) as hole:
        await p.async_fetch()
    hole.assert_not_called()
    assert p.preis_fuer("neu") is None
