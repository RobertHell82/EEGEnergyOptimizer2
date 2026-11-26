"""OpenADR 3.1 als Bedarfsquelle (``openadr.py``, Haralds EEG-Profil).

Geprüft wird:

* das Lesen der Events — so, wie Haralds Static VTN sie schreibt (jedes
  Intervall mit eigener ``intervalPeriod``), so, wie OpenADR 3 eine Reihe
  mit Kopf beschreibt, Stundenwerte auf Viertelstunden, Priorität;
* die Robustheit gegen einen statischen VTN (ignoriert ``programID`` und
  ``skip``) und gegen Preise in ct/kWh;
* dass die OLS dort ankommt, wo der Quotenmodus seine feste Quote einsetzt:
  Preisfunktion, Geldbewertung, EEG-Bezugspreis.
"""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.eeg_energy_optimizer import eeg_price
from custom_components.eeg_energy_optimizer import openadr as oa
from custom_components.eeg_energy_optimizer import schedule as sched

from .test_gewinn import MITTAG, _inputs
from .test_netzladen import NETZ, STAMPS

START = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
V0 = int(START.timestamp() // 900)


def _z(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def _static_event(pid: str, typ: str, werte: list[float], schritt_min: int = 15,
                  prio=0, eid=None, start=START) -> dict:
    """Wie csv2event.make_event: jedes Intervall mit eigener Periode."""
    return {
        "id": eid or f"{pid}-event-{typ}",
        "objectType": "EVENT",
        "programID": pid,
        "priority": prio,
        "modificationDateTime": "2026-11-26T00:00:00Z",
        "intervals": [
            {
                "id": i,
                "intervalPeriod": {
                    "start": _z(start + timedelta(minutes=schritt_min * i)),
                    "duration": f"PT{schritt_min}M",
                },
                "payloads": [{"type": typ, "values": [w]}],
            }
            for i, w in enumerate(werte)
        ],
    }


# ---------------------------------------------------------------------------
# Reine Hilfsfunktionen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("roh,erwartet", [
    ("https://meine-eeg.at/OpenADR/3.1.1", "https://meine-eeg.at/OpenADR/3.1.1"),
    ("https://meine-eeg.at/OpenADR/3.1.1/", "https://meine-eeg.at/OpenADR/3.1.1"),
    ("https://meine-eeg.at/OpenADR/3.1.1/events?programID=ec_demand",
     "https://meine-eeg.at/OpenADR/3.1.1"),
    ("https://meine-eeg.at/OpenADR/3.1.1/programs/ec_feedin",
     "https://meine-eeg.at/OpenADR/3.1.1"),
    ("  http://192.168.1.5:8080  ", "http://192.168.1.5:8080"),
    ("ftp://x.at", None), ("meine-eeg.at", None), ("", None), (None, None),
])
def test_basis_url(roh, erwartet):
    assert oa.basis_url(roh) == erwartet


@pytest.mark.parametrize("roh,erwartet", [
    ("PT15M", 900), ("PT1H", 3600), ("P1D", 86400), ("PT1H30M", 5400),
    ("pt5m", 300), ("P1W", 604800), ("P9999Y", None), ("P", None), ("PT0S", None), (None, None),
])
def test_dauer(roh, erwartet):
    assert oa.dauer_s(roh) == erwartet


def test_reihe_static_vtn_viertelstunden():
    e = _static_event("ec_feedin", "OLS", [0.2, 0.5, 1.0])
    assert oa.reihe_aus_events([e], "OLS") == {V0: 0.2, V0 + 1: 0.5, V0 + 2: 1.0}
    # Ein anderer Typ im selben Event zählt nicht.
    assert oa.reihe_aus_events([e], "EXPORT_PRICE") == {}


def test_reihe_mit_kopf_und_stundenwerten():
    """OpenADR 3: Periode am Event, Intervalle ohne eigene folgen lückenlos."""
    e = {
        "programID": "ec_feedin",
        "intervalPeriod": {"start": _z(START), "duration": "PT1H"},
        "intervals": [
            {"id": 0, "payloads": [{"type": "OLS", "values": [0.4]}]},
            {"id": 1, "payloads": [{"type": "OLS", "values": [0.8]}]},
        ],
    }
    reihe = oa.reihe_aus_events([e], "OLS")
    assert [reihe[V0 + i] for i in range(8)] == [0.4] * 4 + [0.8] * 4
    assert len(reihe) == 8


def test_reihe_kurze_intervalle_werden_gemittelt():
    e = _static_event("ec_feedin", "OLS", [0.0, 0.3, 0.6], schritt_min=5)
    assert oa.reihe_aus_events([e], "OLS") == {V0: pytest.approx(0.3)}


def test_kleinere_prioritaet_gewinnt_der_rest_fuellt_auf():
    oeffentlich = _static_event("ec_feedin", "OLS", [0.5] * 4, prio=100, eid="a")
    mitglied = _static_event("ec_feedin", "OLS", [0.9] * 2, prio=50, eid="b")
    reihe = oa.reihe_aus_events([oeffentlich, mitglied], "OLS")
    assert [reihe[V0 + i] for i in range(4)] == [0.9, 0.9, 0.5, 0.5]


def test_preise_in_cent_werden_erkannt():
    """Haralds Beispieldaten: currency EUR, Werte um 22 — das sind ct/kWh."""
    reihe, war_ct = oa.preise_in_euro({V0: 22.268, V0 + 1: 18.0})
    assert war_ct and reihe == {V0: pytest.approx(0.22268), V0 + 1: pytest.approx(0.18)}
    reihe, war_ct = oa.preise_in_euro({V0: 0.12})
    assert not war_ct and reihe == {V0: 0.12}


def test_konstanter_wert():
    assert oa.konstanter_wert({V0: 0.1, V0 + 1: 0.1}, V0) == 0.1
    assert oa.konstanter_wert({V0: 0.1, V0 + 1: 0.2}, V0) is None
    # Vergangenes zählt nicht mit.
    assert oa.konstanter_wert({V0 - 1: 0.3, V0: 0.1}, V0) == 0.1
    # Ein Dauerwert, dessen einziges Intervall schon begonnen hat.
    assert oa.konstanter_wert({V0 - 5: 0.11}, V0) == 0.11
    assert oa.konstanter_wert({}, V0) is None


# ---------------------------------------------------------------------------
# Provider gegen einen statischen VTN
# ---------------------------------------------------------------------------


def _static_vtn(events: list[dict], programme: list[dict] | None = None):
    """Liefert wie lighttpd + index.json: immer ALLES, Parameter egal."""
    aufrufe: list[tuple[str, dict]] = []

    async def _get(url, params=None):
        aufrufe.append((url, params or {}))
        if url.endswith("/programs"):
            if programme is None:
                raise oa.OpenAdrFehler("Adresse nicht gefunden (HTTP 404)")
            return programme
        if url.endswith("/events"):
            return events
        raise oa.OpenAdrFehler("HTTP 404")

    return _get, aufrufe


def _provider(events, programme=None, quellen=(("EEG Wien", "https://vtn.at/oa"),)):
    p = oa.OpenAdrProvider(None, "temp", list(quellen))
    p._get, aufrufe = _static_vtn(events, programme)  # type: ignore[method-assign]
    return p, aufrufe


def _jetzt(monkeypatch, t=START):
    monkeypatch.setattr(oa, "_utcnow", lambda: t)


@pytest.mark.asyncio
async def test_static_vtn_programme_werden_selbst_getrennt(monkeypatch):
    _jetzt(monkeypatch)
    lang = 48 * 4
    p, aufrufe = _provider([
        _static_event("ec_feedin", "OLS", [0.3] * lang),
        _static_event("ec_feedin", "EXPORT_PRICE", [12.0] * lang),
        _static_event("ec_demand", "OLS", [0.7] * lang),
        _static_event("ec_demand", "PRICE", [0.15] * lang),
        _static_event("default-price", "PRICE", [99.0] * lang),
    ])
    assert await p.async_fetch() is not None
    assert p.letzter_fehler is None
    assert p.get_communities() == ["EEG Wien"]
    assert p.ols("EEG Wien", "einspeisung")[V0] == 0.3
    assert p.ols("EEG Wien", "bezug")[V0] == 0.7
    assert p.preis("EEG Wien", "einspeisung")[V0] == pytest.approx(0.12)
    assert p.preis("EEG Wien", "bezug")[V0] == pytest.approx(0.15)
    assert "ec_feedin: Preise als ct/kWh gelesen" in p.get_warnings("EEG Wien")
    # Der statische VTN ignoriert skip: dieselbe Seite beendet das Lesen.
    event_aufrufe = [a for a in aufrufe if a[0].endswith("/events")]
    assert len(event_aufrufe) == 2
    # Panel-Intervalle ohne saldoKwh (das Bedarfsdiagramm überspringt sie).
    assert "saldoKwh" not in p.get_intervals("EEG Wien")[0]


@pytest.mark.asyncio
async def test_programm_id_aus_programmliste(monkeypatch):
    """Weicht id vom programName ab, gilt die id aus /programs."""
    _jetzt(monkeypatch)
    p, _ = _provider(
        [_static_event("p-17", "OLS", [0.6] * 200)],
        programme=[{"id": "p-17", "programName": "ec_feedin"}],
    )
    await p.async_fetch()
    assert p.ols("EEG Wien", "einspeisung")[V0] == 0.6


@pytest.mark.asyncio
async def test_ohne_ols_ist_es_ein_fehler_und_der_cache_bleibt(monkeypatch):
    _jetzt(monkeypatch)
    p, _ = _provider([_static_event("ec_feedin", "OLS", [0.4] * 200)])
    await p.async_fetch()
    assert p.ols("EEG Wien", "einspeisung")[V0] == 0.4

    # Eine Stunde später liefert der VTN nichts Brauchbares.
    _jetzt(monkeypatch, START + timedelta(hours=1, minutes=1))
    p._get, _ = _static_vtn([])  # type: ignore[method-assign]
    assert await p.async_fetch() is not None
    assert p.letzter_fehler and "keine OLS" in p.letzter_fehler
    assert p.ols("EEG Wien", "einspeisung")[V0] == 0.4


@pytest.mark.asyncio
async def test_hoechstens_stuendlich_nach_fehlschlag_frueher(monkeypatch):
    _jetzt(monkeypatch)
    # Nur 6 h Vorlauf: gewarnt, aber trotzdem nicht öfter als stündlich.
    p, aufrufe = _provider([_static_event("ec_feedin", "OLS", [0.4] * 24)])
    await p.async_fetch()
    assert "reicht nur" in " ".join(p.get_warnings("EEG Wien"))
    n = len(aufrufe)
    _jetzt(monkeypatch, START + timedelta(minutes=50))
    await p.async_fetch()
    assert len(aufrufe) == n
    _jetzt(monkeypatch, START + timedelta(minutes=61))
    await p.async_fetch()
    assert len(aufrufe) > n

    # Kein Erfolg: nach 15 min wieder, nicht erst nach einer Stunde.
    _jetzt(monkeypatch)
    q, aufrufe_q = _provider([])
    await q.async_fetch()
    assert q.letzter_fehler
    n = len(aufrufe_q)
    _jetzt(monkeypatch, START + timedelta(minutes=10))
    await q.async_fetch()
    assert len(aufrufe_q) == n
    _jetzt(monkeypatch, START + timedelta(minutes=16))
    await q.async_fetch()
    assert len(aufrufe_q) > n


def test_quellen_nur_mit_name_und_adresse():
    config = {
        "peakshare_community": "EEG Wien", "openadr_url": "https://a.at/oa/",
        "peakshare_community_2": "BEG X", "openadr_url_2": "",
    }
    assert oa.quellen_aus_config(config) == [("EEG Wien", "https://a.at/oa")]


class _Preise:
    def __init__(self, reihen):
        self.reihen = reihen

    def preis(self, name, richtung):
        return self.reihen.get((name, richtung), {})


def test_konstante_preise_ueberlagern_variable_nicht():
    config = {
        "peakshare_community": "EEG Wien", "peakshare_price": 0.10,
        "peakshare_price_night": 0.08, "peakshare_bezug_price": 0.20,
    }
    prov = _Preise({
        ("EEG Wien", "einspeisung"): {V0: 0.12, V0 + 1: 0.12},
        ("EEG Wien", "bezug"): {V0: 0.15, V0 + 1: 0.18},
    })
    neu, hinweise = oa.tarife_ueberlagern(config, prov, START)
    assert neu["peakshare_price"] == 0.12
    assert neu["peakshare_price_night"] is None   # ein Dauerwert hat keine Nacht
    assert neu["peakshare_bezug_price"] == 0.20   # variabel → bleibt
    assert hinweise and "Bezugspreis" in hinweise[0]
    assert config["peakshare_price"] == 0.10      # Original unverändert


# ---------------------------------------------------------------------------
# Wo die OLS wirkt
# ---------------------------------------------------------------------------

OADR = {
    "eeg_demand_source": "openadr",
    "peakshare_community": "EEG Wien",
    "peakshare_share_pct": 100,
    "peakshare_price": 0.12,
}


def test_bedarfsquelle_und_flags():
    assert eeg_price.bedarfsquelle(OADR) == "openadr"
    (g,) = eeg_price.gemeinschaften_aus_config(OADR)
    assert g.ols and g.quote_tag is None
    (t,) = eeg_price.echte_tarife_aus_config(OADR)
    assert t["ols"] is True and "quote_tag" not in t


def test_ols_aufschlag_ist_mischpreis_je_viertelstunde():
    (g,) = eeg_price.gemeinschaften_aus_config(OADR)
    stamps = [START + timedelta(minutes=15 * i) for i in range(3)]
    ols = {"EEG Wien": {V0: 1.0, V0 + 1: 0.25}}   # dritte ohne OLS
    auf, diag = eeg_price.ols_aufschlag_reihe([g], ols, stamps, 0.08)
    assert auf == [pytest.approx(0.04), pytest.approx(0.01), 0.0]
    assert diag[0]["ols_min_pct"] == 25.0 and diag[0]["stunden"] == 2


def test_geldbewertung_nimmt_die_ols():
    viertel = int(MITTAG.timestamp() // 900)
    inputs = _inputs(
        eeg_tarife=[{"name": "EEG Wien", "anteil": 0.5, "tag": 0.12, "nacht": 0.12, "ols": True}],
        eeg_bedarf={"EEG Wien": {viertel: 0.4}},
    )
    erloes, eeg_kwh = sched._eeg_zuteilung(10.0, MITTAG, 0.06, inputs)
    # 5 kWh angeboten, 40 % aufgenommen = 2 kWh zu 12 ct, 8 kWh zu 6 ct.
    assert eeg_kwh == pytest.approx(2.0)
    assert erloes == pytest.approx(2 * 0.12 + 8 * 0.06)
    # Ohne OLS in dieser Viertelstunde: nichts aufgenommen, kein erfundener Erlös.
    leer = _inputs(eeg_tarife=inputs.eeg_tarife, eeg_bedarf={"EEG Wien": {}})
    assert sched._eeg_zuteilung(10.0, MITTAG, 0.06, leer) == (pytest.approx(0.6), 0.0)


def test_eeg_bezugspreis_mit_bezugs_ols():
    config = {**OADR, "peakshare_bezug_price": 0.10, "peakshare_share_pct": 50,
              "peakshare_bezug_ebene": "lokal"}
    v = [int(s.timestamp() // 900) for s in STAMPS]
    ols_bezug = {"EEG Wien": {v[0]: 1.0, v[1]: 0.5, v[2]: 0.0}}
    # eeg_bedarf (OLS der Einspeisung) darf hier nicht als Saldo gelesen werden.
    bedarf = {"EEG Wien": {x: 0.9 for x in v}}
    reihe, _ = sched.bezugsreihe_netzladen(
        config, STAMPS, 0.25, None, None, NETZ, None, None, bedarf,
        [0.5] * 4, [0.0] * 4, 4.0, ols_bezug,
    )
    lieferant = 0.25
    (g,) = sched.eeg_bezug_aus_config(config)
    netzanteil = NETZ.netznutzung * (1 - g["reduktion"]) + (
        NETZ.netzverlust if g["abgabenfrei"] else NETZ.ap - NETZ.netznutzung
    )
    eeg = g["preis"] + netzanteil
    assert reihe[0] == pytest.approx(0.5 * eeg + 0.5 * lieferant)
    assert reihe[1] == pytest.approx(0.25 * eeg + 0.75 * lieferant)
    assert reihe[2] == pytest.approx(lieferant)
    assert reihe[3] == pytest.approx(lieferant)   # keine OLS → Lieferant

    vorzug = sched.eeg_bezug_vorzug(config, STAMPS, bedarf, ols_bezug)
    assert vorzug[0] == pytest.approx(sched.EEG_BEZUG_VORZUG_MAX)
    assert vorzug[1] == pytest.approx(sched.EEG_BEZUG_VORZUG_MAX / 2)
    assert vorzug[2] == 0.0


def test_quellwechsel_und_namen_brauchen_reload():
    from custom_components.eeg_energy_optimizer import _requires_full_reload

    alt = {"eeg_demand_source": "peakshare", "peakshare_community": "A"}
    assert _requires_full_reload(alt, {**alt, "eeg_demand_source": "openadr"}) is True
    o = {**OADR, "openadr_url": "https://a.at"}
    assert _requires_full_reload(o, {**o, "openadr_url": "https://b.at"}) is True
    assert _requires_full_reload(o, {**o, "peakshare_community": "EEG Neu"}) is True
    assert _requires_full_reload(o, {**o, "peakshare_price": 0.2}) is False


# ---------------------------------------------------------------------------
# Feedbackrunde 26.11.2026
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dauer", ["P9999Y", None])
def test_dauerwert_gilt_bis_auf_weiteres(dauer):
    """Profil: auch ein einzelner Dauerwert ist zulässig (OpenADR: P9999Y)."""
    periode = {"start": "2026-01-01T00:00:00Z"}
    if dauer:
        periode["duration"] = dauer
    e = {"programID": "ec_feedin", "intervals": [
        {"id": 0, "intervalPeriod": periode,
         "payloads": [{"type": "EXPORT_PRICE", "values": [0.11]}]}]}
    reihe = oa.reihe_aus_events([e], "EXPORT_PRICE", START)
    assert reihe[V0] == 0.11
    assert reihe[V0 + 6 * 96] == 0.11              # sechs Tage voraus
    assert min(reihe) >= V0 - 96                    # nicht ab Jänner ausgerollt
    assert oa.konstanter_wert(reihe, V0) == 0.11


def test_dauerwert_unterliegt_einer_reihe_mit_vorrang():
    dauer = {"programID": "ec_feedin", "priority": 100, "intervals": [
        {"id": 0, "intervalPeriod": {"start": _z(START), "duration": "P9999Y"},
         "payloads": [{"type": "OLS", "values": [0.5]}]}]}
    reihe = oa.reihe_aus_events(
        [dauer, _static_event("ec_feedin", "OLS", [0.9] * 2, prio=50)], "OLS", START)
    assert [reihe[V0 + i] for i in range(3)] == [0.9, 0.9, 0.5]


def test_einheit_mwh_aus_dem_deskriptor():
    assert oa.einheit_aus_deskriptoren(
        [{"payloadType": "PRICE", "units": "MWH", "currency": "EUR"}], "PRICE") == "MWH"
    reihe, war_ct = oa.preise_in_euro({V0: 90.0}, "MWH")
    assert reihe[V0] == pytest.approx(0.09) and not war_ct
    reihe, war_ct = oa.preise_in_euro({V0: 22.0}, "KWH")
    assert reihe[V0] == pytest.approx(0.22) and war_ct


@pytest.mark.asyncio
async def test_einheit_vom_programm(monkeypatch):
    _jetzt(monkeypatch)
    p, _ = _provider(
        [_static_event("ec_feedin", "OLS", [0.5] * 200),
         _static_event("ec_feedin", "EXPORT_PRICE", [95.0] * 200)],
        programme=[{"id": "ec_feedin", "programName": "ec_feedin", "payloadDescriptors": [
            {"payloadType": "EXPORT_PRICE", "units": "MWH", "currency": "EUR"}]}],
    )
    await p.async_fetch()
    assert p.preis("EEG Wien", "einspeisung")[V0] == pytest.approx(0.095)


@pytest.mark.asyncio
async def test_ohne_einspeise_ols_ist_es_ein_fehler(monkeypatch):
    """Nur ec_demand liefert: die Gemeinschaft wirkt nicht auf die Einspeisung."""
    _jetzt(monkeypatch)
    p, _ = _provider([_static_event("ec_demand", "OLS", [0.5] * 200)])
    await p.async_fetch()
    assert p.letzter_fehler and "ec_feedin: keine OLS ab jetzt" in p.letzter_fehler
    assert p.ols("EEG Wien", "bezug")[V0] == 0.5   # die Daten sind trotzdem da


@pytest.mark.asyncio
async def test_ein_ausfall_zieht_den_gesunden_vtn_nicht_mit(monkeypatch):
    _jetzt(monkeypatch)
    gut = [_static_event("ec_feedin", "OLS", [0.4] * 200)]
    aufrufe: list[str] = []

    async def _get(url, params=None):
        aufrufe.append(url)
        if "kaputt" in url:
            raise oa.OpenAdrFehler("HTTP 500")
        if url.endswith("/programs"):
            return []
        return gut

    p = oa.OpenAdrProvider(None, "temp", [("A", "https://gut.at/oa"), ("B", "https://kaputt.at/oa")])
    p._get = _get  # type: ignore[method-assign]
    await p.async_fetch()
    assert p.letzter_fehler and p.letzter_fehler.startswith("B:")
    assert p.ols("A", "einspeisung")[V0] == 0.4
    aufrufe.clear()
    _jetzt(monkeypatch, START + timedelta(minutes=20))
    await p.async_fetch()
    assert aufrufe and all("kaputt" in u for u in aufrufe)   # nur B erneut


def test_bilanz_liest_nur_werte_derselben_art():
    """Quellwechsel am selben Tag: Saldo-kWh werden nicht als Quote gelesen."""
    from custom_components.eeg_energy_optimizer.bilanz import EnergieBilanz

    t1, t2 = MITTAG, MITTAG + timedelta(minutes=15)
    ist = [{"t": t1.isoformat()}, {"t": t2.isoformat()}]
    roh = [{"eeg": {"EEG Wien": 7.5}},                          # PeakShare-Saldo
           {"eeg": {"EEG Wien": 0.4}, "eeg_art": "ols"}]       # OpenADR-Quote
    inputs = _inputs(eeg_tarife=[{"name": "EEG Wien", "anteil": 1.0, "tag": 0.12,
                                  "nacht": 0.12, "ols": True}])
    neu = EnergieBilanz._inputs_fuer(None, ist, roh, inputs)
    assert neu.eeg_bedarf == {"EEG Wien": {int(t2.timestamp() // 900): 0.4}}
    alt = _inputs(eeg_tarife=[{"name": "EEG Wien", "anteil": 1.0, "tag": 0.12, "nacht": 0.12}])
    assert EnergieBilanz._inputs_fuer(None, ist, roh, alt).eeg_bedarf == {
        "EEG Wien": {int(t1.timestamp() // 900): 7.5}}
