"""Tests für die Hochrechnung des OeMAG-Tarifs im laufenden Monat.

Die Rechenvorschrift stammt aus den Berechnungsgrundlagen der OeMAG
(Excel je Monat): mengengewichtetes Day-Ahead-Mittel, Korridor 60–100 % des
E-Control-Quartalspreises, minus Ausgleichsenergie. Geprüft wird die
Arithmetik, das Lesen der beiden fremden Formate (Energy-Charts-JSON,
E-Control-Tabelle) und der Weg durch den Provider mit gefälschten Antworten.
Die Zahlen sind die echten aus dem Q3 2026: Anker 10,923 ct, Untergrenze
6,146 ct, Obergrenze 10,515 ct, Ausgleichsenergie 0,408 ct.
"""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.eeg_energy_optimizer import oemag_schaetzung as mod
from custom_components.eeg_energy_optimizer import oemag as oemag_mod

Q3_2026 = 0.10923       # €/kWh, E-Control § 41 Abs. 1
AE = 0.00408            # €/kWh, Ausgleichsenergie PV 2026

# Auszug der E-Control-Seite (Aufbau unverändert, 7.9.2026).
ECONTROL_HTML = """
<h2>Aktueller Marktpreis gem&auml;&szlig; &sect; 41 &Ouml;kostromgesetz 2012</h2>
<table>
<tr><th colspan="6">EEX Grundlast Quartalsfuture (Phelix) - Settlement Price (&euro;/MWh)</th></tr>
<tr><td>Handelstage</td><td>22.Jun 2026</td><td>23.Jun 2026</td><td>24.Jun 2026</td><td>25.Jun 2026</td><td>26.Jun 2026</td></tr>
<tr><td>Q3 2026 (Phelix AT)</td><td>103,58</td><td>105,61</td><td>103,13</td><td>102,70</td><td>103,30</td></tr>
<tr><td>Q4 2026 (Phelix AT)</td><td>130,36</td><td>130,91</td><td>128,29</td><td>128,64</td><td>127,67</td></tr>
<tr><td>Q1 2027 (Phelix AT)</td><td>126,87</td><td>127,55</td><td>125,37</td><td>125,67</td><td>124,20</td></tr>
<tr><td>Q2 2027 (Phelix AT)</td><td>78,23</td><td>79,04</td><td>77,40</td><td>78,22</td><td>77,77</td></tr>
<tr><td>Mittelwert &uuml;ber den jeweiligen Tag</td><td>109,76</td><td>110,78</td><td>108,55</td><td>108,81</td><td>108,24</td></tr>
<tr><td>Mittelwert &uuml;ber die 5 Tage</td><td>109,23</td></tr>
</table>
"""


# ---------------------------------------------------------------------------
# Arithmetik
# ---------------------------------------------------------------------------


def test_gewichtetes_mittel_zaehlt_nur_slots_mit_beidem():
    preise = {10: 0.10, 11: 0.20, 12: 0.30, 13: 0.40}
    gewichte = {11: 1.0, 12: 3.0, 14: 5.0}      # 14 hat keinen Preis, 10/13 kein Gewicht
    mittel, anzahl = mod.gewichtetes_mittel(preise, gewichte)
    assert anzahl == 2
    assert mittel == pytest.approx((0.20 * 1 + 0.30 * 3) / 4)


def test_gewichtetes_mittel_ohne_ueberlappung():
    assert mod.gewichtetes_mittel({1: 0.1}, {2: 1.0}) == (None, 0)
    assert mod.gewichtetes_mittel({}, {}) == (None, 0)


@pytest.mark.parametrize("roh,erwartet", [
    (0.112, 0.10515),     # über dem Deckel → Obergrenze (September 2026)
    (0.0654, 0.06146),    # unter dem Boden → Untergrenze (Juli 2026)
    (0.09405, 0.08997),   # dazwischen → Rohwert minus AE (August 2026)
])
def test_korridor_und_ausgleichsenergie(roh, erwartet):
    assert mod.tarif_aus_roh(roh, Q3_2026, AE) == pytest.approx(erwartet, abs=1e-5)


def test_ohne_anker_kein_korridor():
    assert mod.tarif_aus_roh(0.112, None, AE) == pytest.approx(0.10792)


def test_quartal():
    assert [mod.quartal(m) for m in range(1, 13)] == [1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4]


# ---------------------------------------------------------------------------
# Energy-Charts
# ---------------------------------------------------------------------------


def test_parse_solar_liest_nur_solar_und_laesst_luecken_weg():
    t0 = 1_756_000_800           # ein Viertelstundenraster
    payload = {
        "unix_seconds": [t0, t0 + 900, t0 + 1800, t0 + 2700],
        "production_types": [
            {"name": "Wind onshore", "data": [500, 500, 500, 500]},
            {"name": "Solar", "data": [0, 1200.5, None, 3000]},
        ],
    }
    gewichte = mod.parse_solar(payload)
    # Null trägt nichts, None ist noch nicht gemeldet — beides fällt weg.
    assert gewichte == {(t0 + 900) // 900: 1200.5, (t0 + 2700) // 900: 3000.0}


def test_parse_solar_uebersteht_muell():
    assert mod.parse_solar(None) == {}
    assert mod.parse_solar({"unix_seconds": [1], "production_types": [{"name": "Wind", "data": [1]}]}) == {}
    assert mod.parse_solar({"production_types": "kaputt"}) == {}


# ---------------------------------------------------------------------------
# E-Control
# ---------------------------------------------------------------------------


def test_parse_econtrol_liest_quartal_und_mittelwert():
    assert mod.parse_econtrol(ECONTROL_HTML) == (2026, 3, pytest.approx(0.10923))


def test_parse_econtrol_erstes_future_bestimmt_das_quartal():
    """Die Handelstage liegen im Juni, der Anker gilt aber für Q3 — das
    erste gelistete Future, nicht das Datum, sagt das Quartal."""
    jahr, q, _ = mod.parse_econtrol(ECONTROL_HTML)
    assert (jahr, q) == (2026, 3)


@pytest.mark.parametrize("html", ["", None, "<table><tr><td>Q3 2026</td></tr></table>",
                                  "<table><tr><td>Mittelwert über die 5 Tage</td><td>109,23</td></tr></table>"])
def test_parse_econtrol_unvollstaendig(html):
    assert mod.parse_econtrol(html) is None


@pytest.mark.parametrize("text,erwartet", [
    ("109,23", 109.23), ("1.234,56", 1234.56), ("103.58", 103.58), ("  -12,5 ", -12.5), ("", None),
])
def test_zahl(text, erwartet):
    assert mod._zahl(text) == erwartet


# ---------------------------------------------------------------------------
# Rückrechnung des Ankers aus der OeMAG-Tabelle (oemag.py)
# ---------------------------------------------------------------------------


def test_anker_aus_boden_und_deckel():
    tarife = {1: 0.08842, 2: 0.08457, 7: 0.06146, 8: 0.08997}
    basis = {1: oemag_mod.BASIS_DECKEL, 2: oemag_mod.BASIS_DAY_AHEAD,
             7: oemag_mod.BASIS_BODEN, 8: oemag_mod.BASIS_DAY_AHEAD}
    # Q3: Juli auf der Untergrenze → (6,146 + 0,408) / 0,6 = 10,923
    assert oemag_mod.anker_aus_tabelle(tarife, basis, AE, 3) == pytest.approx(0.10923, abs=1e-5)
    # Q1: Jänner auf der Obergrenze → 8,842 + 0,408 = 9,250
    assert oemag_mod.anker_aus_tabelle(tarife, basis, AE, 1) == pytest.approx(0.0925, abs=1e-5)
    # Q4: noch kein Monat → nichts
    assert oemag_mod.anker_aus_tabelle(tarife, basis, AE, 4) is None


def test_day_ahead_monat_verraet_nichts():
    assert oemag_mod.anker_aus_tabelle({8: 0.08997}, {8: oemag_mod.BASIS_DAY_AHEAD}, AE, 3) is None


# ---------------------------------------------------------------------------
# Der ganze Weg mit gefälschten Antworten
# ---------------------------------------------------------------------------


class _Antwort:
    def __init__(self, json=None, text="", status=200):
        self.status = status
        self._json, self._text = json, text

    async def json(self, content_type=None):
        return self._json

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Session:
    def __init__(self, antworten):
        self.antworten = antworten
        self.aufrufe = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.aufrufe.append((url, params))
        return self.antworten[url]


class _Oemag:
    ausgleichsenergie = AE

    def anker_fuer_quartal(self, q):
        return None


def _stunde(start: datetime, preis_mwh: float) -> dict:
    return {
        "start_timestamp": int(start.timestamp() * 1000),
        "end_timestamp": int((start + timedelta(hours=1)).timestamp() * 1000),
        "marketprice": preis_mwh,
    }


def _provider(monkeypatch, antworten, jetzt_lokal):
    import sys
    import types

    monkeypatch.setitem(
        sys.modules, "aiohttp", types.SimpleNamespace(ClientTimeout=lambda total=None: None)
    )
    session = _Session(antworten)
    monkeypatch.setattr(mod, "async_get_clientsession", lambda hass: session)
    monkeypatch.setattr(mod, "_now_local", lambda: jetzt_lokal)
    monkeypatch.setattr(mod, "_utcnow", lambda: jetzt_lokal.astimezone(timezone.utc))
    return mod.OemagSchaetzer(hass=None, entry_id="e1", oemag=_Oemag()), session


async def test_fetch_rechnet_pv_gewichtet_und_klemmt_an_den_deckel(monkeypatch):
    """Zwei Stunden: billig ohne Sonne, teuer mit viel Sonne. Ungewichtet
    wären es 100 €/MWh; PV-gewichtet zählt fast nur die teure Stunde — und
    die liegt über dem Deckel, also greift die Obergrenze 10,515 ct."""
    tz = timezone(timedelta(hours=2))
    jetzt = datetime(2026, 9, 7, 14, 0, tzinfo=tz)
    h1 = datetime(2026, 9, 7, 6, 0, tzinfo=tz)      # 20 €/MWh, kaum Sonne
    h2 = datetime(2026, 9, 7, 12, 0, tzinfo=tz)     # 180 €/MWh, viel Sonne
    t1, t2 = int(h1.timestamp()), int(h2.timestamp())
    antworten = {
        mod.AWATTAR_URL: _Antwort(json={"data": [_stunde(h1, 20.0), _stunde(h2, 180.0)]}),
        mod.ENERGY_CHARTS_URL: _Antwort(json={
            "unix_seconds": [t1, t1 + 900, t2, t2 + 900],
            "production_types": [{"name": "Solar", "data": [10, 10, 4000, 4000]}],
        }),
        mod.ECONTROL_URL: _Antwort(text=ECONTROL_HTML),
    }
    provider, session = _provider(monkeypatch, antworten, jetzt)

    preis = await provider.async_fetch(force=True)

    roh = (0.020 * 20 + 0.180 * 8000) / 8020
    assert provider.status()["roh"] == pytest.approx(roh, abs=1e-6)
    assert roh > Q3_2026                                   # über dem Deckel
    assert preis == pytest.approx(0.10515, abs=1e-6)
    assert provider.preis == pytest.approx(0.10515, abs=1e-6)
    st = provider.status()
    assert (st["jahr"], st["monat"], st["slots"]) == (2026, 9, 4)
    assert st["anker"] == pytest.approx(0.10923) and st["anker_quelle"] == "E-Control"
    assert st["korridor_von"] == pytest.approx(0.06146, abs=1e-5)
    assert st["korridor_bis"] == pytest.approx(0.10515, abs=1e-6)
    # aWATTar ab Monatsanfang, mit Ende — sonst liefert die API nur 24 h.
    aw = next(p for u, p in session.aufrufe if u == mod.AWATTAR_URL)
    assert aw["start"] == str(int(datetime(2026, 9, 1, tzinfo=tz).timestamp()) * 1000)
    assert int(aw["end"]) > int(jetzt.timestamp() * 1000)


async def test_anker_faellt_auf_die_oemag_tabelle_zurueck(monkeypatch):
    tz = timezone(timedelta(hours=2))
    jetzt = datetime(2026, 9, 7, 14, 0, tzinfo=tz)
    h = datetime(2026, 9, 7, 12, 0, tzinfo=tz)
    t = int(h.timestamp())

    antworten = {
        mod.AWATTAR_URL: _Antwort(json={"data": [_stunde(h, 60.0)]}),
        mod.ENERGY_CHARTS_URL: _Antwort(json={
            "unix_seconds": [t], "production_types": [{"name": "Solar", "data": [1000]}],
        }),
        mod.ECONTROL_URL: _Antwort(status=500),
    }
    provider, _ = _provider(monkeypatch, antworten, jetzt)
    provider._oemag.anker_fuer_quartal = lambda q: Q3_2026 if q == 3 else None

    preis = await provider.async_fetch(force=True)

    # 6,0 ct roh liegt unter dem Boden 6,554 → Untergrenze minus AE
    assert preis == pytest.approx(0.06146, abs=1e-5)
    st = provider.status()
    assert st["anker_quelle"] == "OeMAG-Tabelle"
    assert st["anker_fehler"]                     # der Ausfall steht im Status


async def test_ohne_anker_gilt_der_rohwert_und_der_status_sagt_es(monkeypatch):
    tz = timezone(timedelta(hours=2))
    jetzt = datetime(2026, 9, 7, 14, 0, tzinfo=tz)
    h = datetime(2026, 9, 7, 12, 0, tzinfo=tz)
    t = int(h.timestamp())
    antworten = {
        mod.AWATTAR_URL: _Antwort(json={"data": [_stunde(h, 60.0)]}),
        mod.ENERGY_CHARTS_URL: _Antwort(json={
            "unix_seconds": [t], "production_types": [{"name": "Solar", "data": [1000]}],
        }),
        mod.ECONTROL_URL: _Antwort(text="<p>umgebaut</p>"),
    }
    provider, _ = _provider(monkeypatch, antworten, jetzt)

    preis = await provider.async_fetch(force=True)

    assert preis == pytest.approx(0.060 - AE, abs=1e-6)
    st = provider.status()
    assert st["anker"] is None and st["korridor_von"] is None
    assert st["anker_fehler"] == "Tabelle nicht lesbar"


async def test_fehler_laesst_den_letzten_wert_stehen(monkeypatch):
    tz = timezone(timedelta(hours=2))
    jetzt = datetime(2026, 9, 7, 14, 0, tzinfo=tz)

    kaputt = _Antwort(status=429)
    antworten = {mod.AWATTAR_URL: kaputt, mod.ENERGY_CHARTS_URL: kaputt, mod.ECONTROL_URL: kaputt}
    provider, _ = _provider(monkeypatch, antworten, jetzt)
    provider._preis, provider._jahr, provider._monat = 0.105, 2026, 9
    provider._geholt = jetzt.astimezone(timezone.utc) - timedelta(hours=5)

    preis = await provider.async_fetch(force=True)

    assert preis == 0.105
    assert "429" in provider.status()["fehler"]


def test_hochrechnung_aus_anderem_monat_gilt_nicht(monkeypatch):
    """Ein Monatswechsel ohne neuen Abruf: der alte Wert wäre der falsche
    Monat — dann soll schedule.py auf den veröffentlichten zurückfallen."""
    tz = timezone(timedelta(hours=2))
    monkeypatch.setattr(mod, "_now_local", lambda: datetime(2026, 10, 1, 8, 0, tzinfo=tz))
    provider = mod.OemagSchaetzer(hass=None, entry_id="e1")
    provider._preis, provider._jahr, provider._monat = 0.105, 2026, 9
    provider._geholt = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)
    assert provider.preis is None
    assert provider.status()["preis"] is None


async def test_frist_verhindert_dauerabrufe(monkeypatch):
    tz = timezone(timedelta(hours=2))
    jetzt = datetime(2026, 9, 7, 14, 0, tzinfo=tz)
    provider, session = _provider(monkeypatch, {}, jetzt)
    provider._preis, provider._jahr, provider._monat = 0.105, 2026, 9
    provider._geholt = jetzt.astimezone(timezone.utc) - timedelta(minutes=30)

    assert await provider.async_fetch() == 0.105
    assert session.aufrufe == []
