"""Plausibilitätsband, NaN/unendlich und Größenbegrenzung der Tarifquellen.

Jede Quelle von außen (OeMAG, Hochrechnung, aWATTar SUNNY, Energie AG, Spot,
RIS) kann eine Zahl liefern, die kein Tarif ist: ein verlorenes Komma, eine
verrutschte Spalte, ``NaN`` im JSON. Geprüft wird hier, dass so ein Wert
verworfen wird, NICHT in den Speicher kommt, der Fehler dasteht und der
zuletzt gültige Wert weiter gilt — und dass die linearen Ersatzstücke für
die ``<table.*?</table>``-Ausdrücke dasselbe finden wie diese.
"""
from __future__ import annotations

import json
import re
import sys
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.eeg_energy_optimizer import awattar_sunny as sunny
from custom_components.eeg_energy_optimizer import eeg_price
from custom_components.eeg_energy_optimizer import energie_ag as eag
from custom_components.eeg_energy_optimizer import netzentgelt as n
from custom_components.eeg_energy_optimizer import oemag
from custom_components.eeg_energy_optimizer import oemag_schaetzung as schaetzung
from custom_components.eeg_energy_optimizer import peakshare
from custom_components.eeg_energy_optimizer import spot

JETZT = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Nachgestelltes HTTP: Antwort mit echtem Datenstrom (iter_chunked)
# ---------------------------------------------------------------------------


class _Strom:
    def __init__(self, daten: bytes, stueck: int = 7):
        self._daten, self._stueck = daten, stueck

    def iter_chunked(self, _n):
        async def erzeuge():
            for i in range(0, len(self._daten), self._stueck):
                yield self._daten[i:i + self._stueck]

        return erzeuge()


class _Antwort:
    def __init__(self, text: str = "", status: int = 200, charset="utf-8",
                 content_length=None):
        self.status = status
        self.charset = charset
        self.content_length = content_length
        self.content = _Strom(text.encode(charset or "utf-8"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Session:
    def __init__(self, antworten: dict):
        self.antworten = antworten
        self.aufrufe: list[str] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.aufrufe.append(url)
        antwort = self.antworten[url]
        return antwort() if callable(antwort) else antwort


class _Store:
    def __init__(self):
        self.gespeichert: list = []

    async def async_save(self, daten):
        self.gespeichert.append(daten)


@pytest.fixture
def aiohttp_attrappe(monkeypatch):
    monkeypatch.setitem(
        sys.modules, "aiohttp", types.SimpleNamespace(ClientTimeout=lambda total=None: None)
    )


# ---------------------------------------------------------------------------
# Bausteine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("wert, erwartet", [
    (0.08, True), (-0.05, True), (0.60, True), (0.61, False), (-0.051, False),
    (float("nan"), False), (float("inf"), False), (float("-inf"), False),
    (True, False), ("0.08", False), (None, False),
])
def test_monatsband(wert, erwartet):
    assert oemag.ist_monatswert(wert) is erwartet


@pytest.mark.parametrize("text", [
    "<table><tr><td>a</td></tr></table>",
    "x<TABLE a=1>1</table><table>2</TABLE>y",
    "<table>offen ohne Ende",
    "<table>1</table><table>offen",
    "<table><table>verschachtelt</table></table>",
    "<table</table>",
    "",
    "nichts",
])
def test_bloecke_findet_dasselbe_wie_der_ausdruck(text):
    alt = re.findall(r"<table.*?</table>", text, re.S | re.I)
    assert oemag.bloecke(text, oemag._TABLE_AUF, oemag._TABLE_ZU) == alt
    alt_td = re.findall(r"<t[dh].*?</t[dh]>", text, re.S | re.I)
    assert oemag.bloecke(text, oemag._TD_AUF, oemag._TD_ZU) == alt_td


def test_bloecke_bleibt_bei_offenen_anfaengen_linear():
    """200 000 offene ``<table`` ohne Ende: der Ausdruck verfolgte jeden bis
    ans Textende (quadratisch), ``bloecke`` gibt nach dem ersten auf."""
    text = "<table" * 200_000
    beginn = time.perf_counter()
    assert oemag.bloecke(text, oemag._TABLE_AUF, oemag._TABLE_ZU) == []
    assert oemag.parse_seite(text)["tarife"] == {}
    assert time.perf_counter() - beginn < 2.0


@pytest.mark.parametrize("text", [
    "a<script>x</script>b",
    "a<SCRIPT type='t'>x</Script>b<style>y</style>c",
    "a<script>nie geschlossen <style>s</style> ende",
    "<style>1</style><script>2",
    "<script><script>x</script>",
    "kein Skript",
])
def test_ohne_skripte_wie_der_ausdruck(text):
    alt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    assert eag._ohne_skripte(text) == alt


@pytest.mark.parametrize("text", [
    "<head><title>t</title></head><header>h</header><p>x</p>",
    "<HEAD>kopf</head><style>s</style>",
    "<header>ohne Ende",
])
def test_ohne_elemente_wie_der_ausdruck_im_ris(text):
    """netzentgelt._flachtext nimmt dieselbe Mechanik mit ``head`` dazu —
    einschließlich der Eigenheit, dass ``<head`` auch ``<header>`` trifft."""
    alt = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", " ", text)
    assert oemag.ohne_elemente(text, ("style", "script", "head")) == alt


@pytest.mark.parametrize("text", [
    "<p>a</p> b <br/>c", "a < b > c", "<>", "offen <ohne Ende", "x<a<b>y<", "",
])
def test_ohne_tags_wie_der_ausdruck(text):
    assert oemag.ohne_tags(text) == re.sub(r"<[^>]+>", " ", text)


def test_ohne_tags_bleibt_linear():
    text = "<p>x</p>" + "<" * 300_000
    beginn = time.perf_counter()
    assert oemag.ohne_tags(text) == " x " + "<" * 300_000
    assert time.perf_counter() - beginn < 2.0


def test_ohne_skripte_bleibt_linear():
    text = "<script>" * 100_000 + "<style>s</style>"
    beginn = time.perf_counter()
    assert eag._ohne_skripte(text).endswith(" ")
    assert time.perf_counter() - beginn < 2.0


async def test_lies_text_begrenzt_die_groesse():
    with pytest.raises(RuntimeError, match="größer"):
        await oemag.lies_text(_Antwort("x" * 100), grenze=50)
    # Schon die angekündigte Länge genügt, dann wird gar nicht gelesen.
    with pytest.raises(RuntimeError, match="zu groß"):
        await oemag.lies_text(_Antwort("x", content_length=10**9))
    assert await oemag.lies_text(_Antwort("Jänner", charset="latin-1")) == "Jänner"


async def test_lies_json_macht_nan_und_unendlich_zu_none():
    daten = await oemag.lies_json(
        _Antwort('{"a": NaN, "b": Infinity, "c": -Infinity, "d": 1.5}')
    )
    assert daten == {"a": None, "b": None, "c": None, "d": 1.5}


def test_peakshare_und_eeg_price_lesen_kein_nan():
    assert peakshare._zahl("nan") is None
    assert peakshare._zahl(float("inf")) is None
    assert peakshare._zahl("1.5") == 1.5
    assert eeg_price._zahl("nan") == 0.0
    werte = eeg_price.saldo_je_intervall([
        {"timestamp": "2026-09-07T10:00:00.000Z", "saldoKwh": float("nan")},
        {"timestamp": "2026-09-07T10:15:00.000Z", "saldoKwh": 1.2},
    ])
    assert list(werte.values()) == [1.2]


# ---------------------------------------------------------------------------
# OeMAG
# ---------------------------------------------------------------------------


def _oemag_seite(juli: str) -> str:
    return (
        "<table><tr><th>Monat</th><th>PV</th></tr>"
        "<tr><td>Juni</td><td>6,772 ct/kWh</td></tr>"
        f"<tr><td>Juli</td><td>{juli}</td></tr></table>"
        "<p>Photovoltaik und andere Energieträger 0,408 ct/kWh</p>"
    )


def _oemag_provider(monkeypatch, seite: str):
    session = _Session({oemag.OEMAG_URL: lambda: _Antwort(seite)})
    monkeypatch.setattr(oemag, "async_get_clientsession", lambda hass: session)
    monkeypatch.setattr(oemag, "dt_now_monat", lambda: 7)
    monkeypatch.setattr(oemag, "_utcnow", lambda: JETZT)
    p = oemag.OemagProvider(hass=None, entry_id="e1")
    p._store = _Store()
    return p


async def test_oemag_unplausibler_tarif_bleibt_draussen(monkeypatch, aiohttp_attrappe):
    """„6146 ct/kWh" statt „6,146": 61 €/kWh. Der alte Wert bleibt, nichts
    wird gespeichert, der Fehler steht im Status."""
    p = _oemag_provider(monkeypatch, _oemag_seite("6146 ct/kWh"))
    p._preis, p._monat, p._geholt = 0.06772, 6, JETZT - timedelta(days=1)

    assert await p.async_fetch(force=True) == pytest.approx(0.06772)
    assert "unplausibel" in p.status()["fehler"]
    assert p._store.gespeichert == []
    assert p._tarife == {}   # die Hochrechnung sieht die kaputte Tabelle nicht


async def test_oemag_plausibler_tarif_wird_uebernommen(monkeypatch, aiohttp_attrappe):
    p = _oemag_provider(monkeypatch, _oemag_seite("6,146 ct/kWh"))
    assert await p.async_fetch(force=True) == pytest.approx(0.06146)
    assert p.status()["fehler"] is None
    assert len(p._store.gespeichert) == 1
    assert p.ausgleichsenergie == pytest.approx(0.00408)


def test_oemag_unplausible_ausgleichsenergie_gilt_als_nicht_gelesen():
    seite = oemag.parse_seite(_oemag_seite("6,146 ct/kWh").replace("0,408", "40,8"))
    assert seite["ausgleichsenergie"] is None


async def test_oemag_alter_speicherstand_mit_unsinn_wird_nicht_geladen():
    p = oemag.OemagProvider(hass=None, entry_id="e1")

    class _Alt:
        async def async_load(self):
            return {"preis": float("nan"), "monat": 7, "geholt": JETZT.isoformat(),
                    "tarife": {"6": 0.06772, "7": 61.46}}

    p._store = _Alt()
    await p.async_load()
    assert p._preis is None
    assert p._tarife == {6: 0.06772}


# ---------------------------------------------------------------------------
# Spot
# ---------------------------------------------------------------------------


def _eintrag(start: datetime, preis) -> dict:
    return {
        "start_timestamp": int(start.timestamp() * 1000),
        "end_timestamp": int((start + timedelta(hours=1)).timestamp() * 1000),
        "marketprice": preis,
    }


def test_spot_verwirft_nan_und_unsinn_und_zaehlt_mit():
    stunde = JETZT.replace(minute=0)
    preise, verworfen = spot.parse_marketdata_gezaehlt({"data": [
        _eintrag(stunde, 90.0),
        _eintrag(stunde + timedelta(hours=1), "NaN"),
        _eintrag(stunde + timedelta(hours=2), float("inf")),
        _eintrag(stunde + timedelta(hours=3), 90_000.0),   # €/kWh statt €/MWh
        _eintrag(stunde + timedelta(hours=4), -400.0),     # negativ, aber echt
    ]})
    assert verworfen == 3
    assert len(preise) == 8
    assert min(preise.values()) == pytest.approx(-0.4)


def test_spot_liest_hoechstens_max_eintraege():
    stunde = JETZT.replace(minute=0)
    daten = [_eintrag(stunde + timedelta(hours=i), 50.0)
             for i in range(spot.MAX_EINTRAEGE + 5)]
    preise, _ = spot.parse_marketdata_gezaehlt({"data": daten})
    assert len(preise) == spot.MAX_EINTRAEGE * 4


def _spot_provider(monkeypatch, body: str):
    session = _Session({spot.SPOT_URLS["at"]: lambda: _Antwort(body)})
    monkeypatch.setattr(spot, "async_get_clientsession", lambda hass: session)
    monkeypatch.setattr(spot, "_utcnow", lambda: JETZT)
    p = spot.SpotProvider(hass=None, entry_id="e1")
    p._store = _Store()
    return p


async def test_spot_json_mit_nan_behaelt_die_guten_preise(monkeypatch, aiohttp_attrappe):
    stunde = JETZT.replace(minute=0)
    gut = _eintrag(stunde, 90.0)
    kaputt = _eintrag(stunde + timedelta(hours=1), 0.0)
    body = json.dumps({"data": [gut, kaputt]}).replace('"marketprice": 0.0', '"marketprice": NaN')
    p = _spot_provider(monkeypatch, body)

    await p.async_fetch(force=True)

    assert len(p._preise) == 4 and all(v == pytest.approx(0.09) for v in p._preise.values())
    assert len(p._store.gespeichert) == 1


async def test_spot_nur_unsinn_laesst_die_alten_preise_stehen(monkeypatch, aiohttp_attrappe):
    stunde = JETZT.replace(minute=0)
    p = _spot_provider(monkeypatch, json.dumps({"data": [_eintrag(stunde, 90_000.0)]}))
    p._preise = {int(stunde.timestamp() // 900): 0.09}

    await p.async_fetch(force=True)

    assert p._preise == {int(stunde.timestamp() // 900): 0.09}
    assert "verworfen" in p.status()["fehler"]
    assert p._store.gespeichert == []


async def test_spot_haelt_keine_preise_weit_in_der_zukunft(monkeypatch, aiohttp_attrappe):
    stunde = JETZT.replace(minute=0)
    body = json.dumps({"data": [
        _eintrag(stunde, 90.0), _eintrag(stunde + timedelta(days=30), 80.0),
    ]})
    p = _spot_provider(monkeypatch, body)
    await p.async_fetch(force=True)
    assert max(p._preise) < int((stunde + timedelta(days=8)).timestamp() // 900)


# ---------------------------------------------------------------------------
# OeMAG-Hochrechnung (auch Rohwert für energie_ag_estimate)
# ---------------------------------------------------------------------------


def test_solar_mit_nan_verdirbt_das_mittel_nicht():
    gewichte = schaetzung.parse_solar({
        "unix_seconds": [0, 900, 1800],
        "production_types": [{"name": "Solar", "data": [100.0, float("nan"), float("inf")]}],
    })
    assert gewichte == {0: 100.0}


def test_unendlicher_anker_gilt_als_kein_anker():
    assert schaetzung.tarif_aus_roh(0.12, float("inf"), 0.004) == pytest.approx(0.116)
    assert schaetzung.tarif_aus_roh(0.12, float("nan"), 0.004) == pytest.approx(0.116)
    assert schaetzung.tarif_aus_roh(0.12, 0.10, 0.004) == pytest.approx(0.096)


async def test_hochrechnung_verwirft_unplausiblen_rohwert(monkeypatch, aiohttp_attrappe):
    """1000 €/MWh sind ein möglicher Börsenpreis, aber kein PV-Monatsmittel.
    Rohwert und Tarif bleiben beim alten Stand — auch für energie_ag_estimate."""
    tz = timezone(timedelta(hours=2))
    lokal = datetime(2026, 9, 7, 14, 0, tzinfo=tz)
    stunde = datetime(2026, 9, 7, 10, 0, tzinfo=tz)
    preise = json.dumps({"data": [_eintrag(stunde, 1000.0)]})
    solar = json.dumps({
        "unix_seconds": [int(stunde.timestamp()) + 900 * i for i in range(4)],
        "production_types": [{"name": "Solar", "data": [100.0] * 4}],
    })
    session = _Session({
        schaetzung.AWATTAR_URL: lambda: _Antwort(preise),
        schaetzung.ENERGY_CHARTS_URL: lambda: _Antwort(solar),
        schaetzung.ECONTROL_URL: lambda: _Antwort(""),
    })
    monkeypatch.setattr(schaetzung, "async_get_clientsession", lambda hass: session)
    monkeypatch.setattr(schaetzung, "_now_local", lambda: lokal)
    monkeypatch.setattr(schaetzung, "_utcnow", lambda: lokal.astimezone(timezone.utc))
    s = schaetzung.OemagSchaetzer(hass=None, entry_id="e1")
    s._store = _Store()
    s._preis, s._roh, s._jahr, s._monat = 0.09, 0.094, 2026, 9
    s._geholt = lokal.astimezone(timezone.utc) - timedelta(hours=4)

    assert await s.async_fetch(force=True) == pytest.approx(0.09)
    assert s.roh == pytest.approx(0.094)
    assert "unplausibel" in s.status()["fehler"]
    assert s._store.gespeichert == []


def test_econtrol_anker_ausserhalb_des_bands_zaehlt_nicht():
    seite = (
        "<table><tr><td>Q4 2026</td><td>1</td></tr>"
        "<tr><td>Mittelwert über die 5 Tage</td><td>{}</td></tr></table>"
    )
    assert schaetzung.parse_econtrol(seite.format("103,58"))[2] == pytest.approx(0.10358)
    assert schaetzung.parse_econtrol(seite.format("10358")) is None


# ---------------------------------------------------------------------------
# aWATTar SUNNY
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_sunny_zelle_nan_ist_kein_tarif(text):
    assert sunny._ct_wert(text) is None


CSV_KAPUTT = '''"","Jahr ","MONTHLY ","SUNNY Einspeisevergütung "
"8","2026","13.255","5.491"
"9","2026","16.768","8989"
'''


def test_sunny_tabelle_markiert_unplausible_werte():
    tab = sunny.parse_tabelle(CSV_KAPUTT, 2026)
    assert tab.unplausibel and "9:" in tab.unplausibel[0]
    assert 202609 not in tab.neu


async def test_sunny_verwirft_den_tab_mit_unplausiblem_wert():
    p = sunny.AwattarSunnyProvider(hass=None, entry_id="e1")

    async def hole(session, url):
        return CSV_KAPUTT

    p._hole_text = hole
    assert await p._hole_tabelle(object(), 2026) is None
    assert "unplausibel" in p._fehler


# ---------------------------------------------------------------------------
# Energie AG
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("roh, erwartet", [
    ("nan", eag.DEFAULT_ABSCHLAG_EUR), (float("inf"), eag.DEFAULT_ABSCHLAG_EUR),
    (-0.01, 0.0), (0.5, eag.ABSCHLAG_MAX_EUR), (0.017, 0.017),
])
def test_energie_ag_abschlag_endlich_und_begrenzt(roh, erwartet):
    assert eag._abschlag(roh) == pytest.approx(erwartet)


def test_energie_ag_schaetzung_mit_nan_rohwert_gilt_nicht():
    p = eag.EnergieAgProvider(hass=None, entry_id="e1",
                              schaetzer=types.SimpleNamespace(roh=float("nan")))
    assert p.preis_geschaetzt("float") is None
    assert p.status()["schaetzung"] is None


async def test_energie_ag_unplausibler_referenzmarktwert_bleibt_draussen(
    monkeypatch, aiohttp_attrappe
):
    seite = ("<p>Referenzmarktwert im August 2026 : für Photovoltaikanlagen "
             "bei 942 Cent/kWh</p>")
    session = _Session({eag.ECONTROL_URL: lambda: _Antwort(seite)})
    monkeypatch.setattr(eag, "async_get_clientsession", lambda hass: session)
    monkeypatch.setattr(eag, "_utcnow", lambda: JETZT)
    p = eag.EnergieAgProvider(hass=None, entry_id="e1")
    p._store = _Store()
    p._werte = {202607: 0.0801}

    await p.async_fetch(force=True)

    assert p._werte == {202607: 0.0801}
    assert "unplausibel" in p._fehler
    assert p._store.gespeichert == []


# ---------------------------------------------------------------------------
# Netzentgelte (RIS)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url, erwartet", [
    ("https://ogd.ris.bka.gv.at/Dokumente/Bundesnormen/NOR1/NOR1.html", True),
    ("https://www.ris.bka.gv.at/x", True),
    ("https://ris.bka.gv.at/x", True),
    ("http://ogd.ris.bka.gv.at/x", False),                 # nur https
    ("https://ris.bka.gv.at.boese.example/x", False),
    ("https://boeseris.bka.gv.at/x", False),
    ("https://192.168.1.10/x", False),
    ("https://data.bka.gv.at/x", False),
    (None, False),
])
def test_ris_adresse(url, erwartet):
    assert n.ist_ris_adresse(url) is erwartet


def test_dokument_ausserhalb_des_ris_wird_nicht_abgerufen():
    antwort = {"OgdSearchResult": {"OgdDocumentResults": {"OgdDocumentReference": {
        "Data": {
            "Metadaten": {"Technisch": {"ID": "NOR1"},
                          "Bundesrecht": {"Kurztitel": "SNE-V", "BrKons": {}}},
            "Dokumentliste": {"ContentReference": {"Urls": {"ContentUrl": [
                {"DataType": "Html", "Url": "http://192.168.1.1/admin"},
            ]}}},
        }
    }}}}
    assert n._dokumente_aus_antwort(antwort)[0]["html_url"] is None


_BEREICHE = (
    "Burgenland", "Kärnten", "Klagenfurt", "Niederösterreich", "Oberösterreich",
    "Linz", "Salzburg", "Steiermark", "Graz", "Tirol", "Innsbruck", "Vorarlberg",
    "Wien", "Kleinwalsertal",
)


def _netzebene7(werte: dict[str, tuple[str, str]]) -> str:
    zeilen = "".join(
        f"<tr><td>x)</td><td>Bereich {name}:</td><td>1</td><td>{ap}</td><td>{snap}</td></tr>"
        for name, (ap, snap) in werte.items()
    )
    return (
        "<table><tr><td>2. Netznutzungsentgelt für die Netzebene 7:</td></tr>"
        "<tr><td></td><td></td><td>LP</td><td>AP</td><td>SNAP</td></tr>"
        f"{zeilen}<tr><td>3. Netzverlustentgelt:</td></tr></table>"
    )


def test_unplausible_zeilen_fallen_heraus():
    werte = {name: ("6,00", "4,80") for name in _BEREICHE}
    werte["Wien"] = ("698", "558")        # Komma verloren
    werte["Linz"] = ("5,57", "6,00")      # SNAP über AP
    werte["Graz"] = ("0,01", "0,01")      # unter dem Band
    tarife = n.parse_tabelle(_netzebene7(werte))
    assert set(tarife) == {n._bereich_schluessel(b) for b in _BEREICHE} - {"wien", "linz", "graz"}


def test_gespeicherte_unplausible_saetze_werden_nicht_geladen():
    roh = n.SNAPSHOT.als_dict()
    roh["tarife"]["wien"]["ap"] = float("nan")
    roh["tarife"]["linz"]["snap"] = 99.0
    tabelle = n.Tariftabelle.aus_dict(roh)
    assert "wien" not in tabelle.tarife and "linz" not in tabelle.tarife
    assert "graz" in tabelle.tarife


async def test_fehlender_bereich_behaelt_den_zuletzt_gueltigen_satz(monkeypatch):
    """Fällt ein Bereich heraus, gilt sein zuletzt GELESENER Satz, nicht der
    Schnappschuss."""
    provider = n.NetzentgeltProvider(hass=None, entry_id="e1")
    alt = n.Tariftabelle(
        stand="2026-04-01", quelle="alt", url=None,
        tarife={"wien": n.Netztarif(7.11, 5.69, None, 0.7)},
    )
    provider._tabelle = alt

    async def kein_netz(url, params=None):
        raise RuntimeError("offline")

    monkeypatch.setattr(provider, "_get_text", kein_netz)
    neu = n.Tariftabelle(
        stand="2026-10-01", quelle="neu", url=None,
        tarife={"graz": n.Netztarif(5.17, 4.14)},
    )
    ergaenzt = await provider._ergaenze(neu, JETZT.date())
    assert ergaenzt.tarife["wien"].ap == pytest.approx(7.11)
    assert ergaenzt.tarife["graz"].ap == pytest.approx(5.17)


def test_netzbaender_sind_nan_fest():
    assert not n.ist_plausibel(n.Netztarif(float("nan")))
    assert not n.ist_plausibel(n.Netztarif(6.0, snap=float("inf")))
    assert not n.ist_plausibel(n.Netztarif(6.0, winap=7.0))
    assert not n.ist_plausibel(n.Netztarif(6.0, verlust=-0.1))
    assert n.ist_plausibel(n.Netztarif(17.73, 14.18, None, 0.401))
