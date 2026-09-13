"""Netznutzungsentgelt je Netzbereich aus der Verordnung (netzentgelt.py).

Die Tabelle kommt aus dem RIS; hier wird sie gegen eine echte Kopie von
§ 5 SNE-V 2018 (RIS NOR40273644, gültig ab 1.4.2026) gelesen und gegen eine
erfundene Tabelle im Aufbau, den die Tarifverordnung ab 2027 haben dürfte
(eine Zeile je Bereich, Spalten AP/SNAP/WiNAP).
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from custom_components.eeg_energy_optimizer import netzentgelt as n
from custom_components.eeg_energy_optimizer.netzentgelt import _utcnow

FIXTURE = Path(__file__).parent / "fixtures" / "ris_snev2018_p5_NOR40273644.html"
FIXTURE_P6 = Path(__file__).parent / "fixtures" / "ris_snev2018_p6_netzverlust.html"
FIXTURE_EFBV = Path(__file__).parent / "fixtures" / "ris_efbv2026_p2.html"
FIXTURE_ELABG_P4 = Path(__file__).parent / "fixtures" / "ris_elabgg_p4.html"
FIXTURE_ELABG_P7 = Path(__file__).parent / "fixtures" / "ris_elabgg_p7.html"

# Die vier Posten von Netz Oberösterreich auf Netzebene 7, Stand 2026,
# Cent/kWh netto — Grundlage aller Erwartungen hier.
OOE_NETZNUTZUNG = 6.29
OOE_VERLUST = 0.528
ELEKTRIZITAETSABGABE_2026 = 0.1
FOERDERBEITRAG_2026 = 0.62
OOE_SUMME_NETTO = (
    OOE_NETZNUTZUNG + OOE_VERLUST + ELEKTRIZITAETSABGABE_2026 + FOERDERBEITRAG_2026
)


# ---------------------------------------------------------------------------
# Tabelle lesen
# ---------------------------------------------------------------------------


def test_echte_ris_tabelle_wird_gelesen():
    """Alle 14 Bereiche, Zeile „nicht gemessene Leistung", AP und SNAP netto."""
    tarife = n.parse_tabelle(FIXTURE.read_text(encoding="utf-8"))
    assert len(tarife) == len(n.NETZBEREICHE)
    assert tarife["oberoesterreich"] == n.Netztarif(6.29, 5.03, None)
    assert tarife["linz"] == n.Netztarif(5.57, 4.46, None)
    assert tarife["wien"] == n.Netztarif(6.98, 5.58, None)
    assert tarife["kleinwalsertal"] == n.Netztarif(17.73, 14.18, None)


def test_eingebauter_schnappschuss_entspricht_der_verordnung():
    """Der Rückfall darf nicht von dem abweichen, was das RIS liefert.

    § 5 bringt Arbeitspreis und SNAP, § 6 das Netzverlustentgelt — der
    Schnappschuss trägt beides zusammen, also wird auch gegen beide geprüft.
    """
    tarife = n.parse_tabelle(FIXTURE.read_text(encoding="utf-8"))
    verluste = n.parse_verlust_tabelle(FIXTURE_P6.read_text(encoding="utf-8"))
    assert {k: (t.ap, t.snap, t.winap) for k, t in tarife.items()} == {
        k: (t.ap, t.snap, t.winap) for k, t in n.SNAPSHOT.tarife.items()
    }
    assert verluste == {k: t.verlust for k, t in n.SNAPSHOT.tarife.items()}
    assert n.SNAPSHOT.aus_snapshot is True


def test_netzverlustentgelt_kommt_aus_paragraf_sechs():
    """§ 6 ist eine Matrix: Zeilen Netzbereich, Spalten Netzebene."""
    verluste = n.parse_verlust_tabelle(FIXTURE_P6.read_text(encoding="utf-8"))
    assert len(verluste) == len(n.NETZBEREICHE)
    assert verluste["oberoesterreich"] == pytest.approx(OOE_VERLUST)
    assert verluste["wien"] == pytest.approx(0.700)
    # Burgenland verrechnet auf Netzebene 7 kein Netzverlustentgelt — eine
    # echte Null, kein fehlender Wert.
    assert verluste["burgenland"] == pytest.approx(0.0)


def test_elektrizitaetsabgabe_nimmt_die_befristung_und_faellt_danach_zurueck():
    """2026 gilt der gesenkte Satz aus § 7, ab 2027 wieder § 4 Abs. 2."""
    regel = FIXTURE_ELABG_P4.read_text(encoding="utf-8")
    uebergang = FIXTURE_ELABG_P7.read_text(encoding="utf-8")

    satz, quelle = n.parse_elektrizitaetsabgabe(regel, uebergang, date(2026, 9, 13))
    assert satz == pytest.approx(ELEKTRIZITAETSABGABE_2026)
    assert "§ 7" in quelle

    # Die Befristung endet mit 2026 — ohne Zutun gilt dann der Regelsatz.
    satz27, quelle27 = n.parse_elektrizitaetsabgabe(regel, uebergang, date(2027, 6, 1))
    assert satz27 == pytest.approx(1.5)
    assert "§ 4" in quelle27

    # Ohne Übergangsbestimmungen bleibt es beim Regelsatz.
    assert n.parse_elektrizitaetsabgabe(regel, None, date(2026, 9, 13))[0] == pytest.approx(1.5)


def test_foerderbeitrag_summiert_arbeits_und_verlustanteil():
    """Je kWh zählen beide Komponenten; die je Zählpunkt bleibt außen vor."""
    satz, quelle = n.parse_foerderbeitrag(FIXTURE_EFBV.read_text(encoding="utf-8"))
    # 0,583 (Netznutzung Arbeit) + 0,037 (Netzverlust) auf Netzebene 7
    assert satz == pytest.approx(FOERDERBEITRAG_2026)
    assert "Förderbeitragsverordnung" in quelle


def _tabelle_2027(zeilen: str) -> str:
    return f"""<html><body>
    <table>
      <tr><td>1. Netznutzungsentgelt für die Netzebene 6:</td><td></td><td></td><td></td></tr>
      <tr><td></td><td></td><td>LP</td><td>AP</td></tr>
      <tr><td>a)</td><td>Bereich Wien:</td><td>5 952</td><td>1,93</td></tr>
      <tr><td>2. Netznutzungsentgelt für die Netzebene 7:</td><td></td><td></td><td></td></tr>
      <tr><td></td><td></td><td>LP bis 10 kW</td><td>LP</td><td>AP</td><td>SNAP</td><td>WiNAP</td></tr>
      {zeilen}
      <tr><td>3. Netzverlustentgelt:</td><td></td></tr>
    </table></body></html>"""


def test_tabelle_mit_einer_zeile_je_bereich_und_winap():
    """Aufbau der Tarifverordnung ab 2027: Werte direkt in der Bereichszeile,
    WiNAP als eigene Spalte. Netzebene 6 davor darf nicht hineinrutschen."""
    zeilen = "".join(
        f"<tr><td>{buchstabe})</td><td>Bereich {name}:</td><td>1 200</td><td>2 400</td>"
        f"<td>{ap}</td><td>{snap}</td><td>{winap}</td></tr>"
        for buchstabe, name, ap, snap, winap in (
            ("a", "Burgenland", "4,10", "3,28", "3,28"),
            ("b", "Kärnten", "4,20", "3,36", "3,36"),
            ("c", "Klagenfurt", "4,30", "3,44", "3,44"),
            ("d", "Niederösterreich", "4,40", "3,52", "3,52"),
            ("e", "Oberösterreich", "4,50", "3,60", "3,60"),
            ("f", "Linz", "4,60", "3,68", "3,68"),
            ("g", "Salzburg", "4,70", "3,76", "3,76"),
            ("h", "Steiermark", "4,80", "3,84", "3,84"),
            ("i", "Graz", "4,90", "3,92", "3,92"),
            ("j", "Tirol", "5,00", "4,00", "4,00"),
            ("k", "Innsbruck", "5,10", "4,08", "4,08"),
            ("l", "Vorarlberg", "5,20", "4,16", "4,16"),
            ("m", "Wien", "5,30", "4,24", "4,24"),
            ("n", "Kleinwalsertal", "9,00", "7,20", "7,20"),
        )
    )
    tarife = n.parse_tabelle(_tabelle_2027(zeilen))
    assert len(tarife) == 14
    assert tarife["wien"] == n.Netztarif(5.30, 4.24, 4.24)
    assert tarife["oberoesterreich"].winap == pytest.approx(3.60)


def test_unterzeilen_nicht_gemessen_haben_vorrang():
    """SNE-V-2018-Aufbau: Haushalt ist „nicht gemessene Leistung", nicht die
    Bereichszeile und nicht „gemessene Leistung"."""
    zeilen = "".join(
        f"<tr><td>{b})</td><td>Bereich {name}:</td><td></td><td></td><td></td><td></td><td></td></tr>"
        f"<tr><td></td><td>aa) gemessene Leistung</td><td>7 656</td><td></td><td>5,83</td><td>4,66</td><td></td></tr>"
        f"<tr><td></td><td>bb) nicht gemessene Leist.</td><td>5 400 /Jahr</td><td></td><td>{ap}</td><td>{snap}</td><td></td></tr>"
        f"<tr><td></td><td>cc) unterbrechbar</td><td></td><td></td><td>1,00</td><td>0,80</td><td></td></tr>"
        for b, name, ap, snap in (
            ("a", "Burgenland", "8,46", "6,77"), ("b", "Kärnten", "9,67", "7,74"),
            ("c", "Klagenfurt", "6,90", "5,52"), ("d", "Niederösterreich", "8,79", "7,03"),
            ("e", "Oberösterreich", "6,29", "5,03"), ("f", "Linz", "5,57", "4,46"),
            ("g", "Salzburg", "6,59", "5,27"), ("h", "Steiermark", "8,82", "7,06"),
            ("i", "Graz", "5,17", "4,14"), ("j", "Tirol", "6,81", "5,45"),
        )
    )
    tarife = n.parse_tabelle(_tabelle_2027(zeilen))
    assert tarife["burgenland"] == n.Netztarif(8.46, 6.77, None)
    assert len(tarife) == 10


@pytest.mark.parametrize(
    "html",
    [
        "<html><body><p>kein Inhalt</p></body></html>",
        # Netzebene 7 da, aber nur zwei Bereiche lesbar
        _tabelle_2027(
            "<tr><td>a)</td><td>Bereich Wien:</td><td></td><td></td><td>5,30</td><td>4,24</td><td></td></tr>"
            "<tr><td>b)</td><td>Bereich Linz:</td><td></td><td></td><td>5,57</td><td>4,46</td><td></td></tr>"
        ),
    ],
)
def test_unlesbare_tabelle_ist_ein_fehler(html):
    with pytest.raises(ValueError):
        n.parse_tabelle(html)


@pytest.mark.parametrize(
    ("text", "erwartet"),
    [("8,46", 8.46), ("5 400 /Jahr", None), ("7 656", 7656.0), ("", None), ("LP", None), ("17,73", 17.73)],
)
def test_zahlen_lesen(text, erwartet):
    assert n._zahl(text) == erwartet


# ---------------------------------------------------------------------------
# Wirksame Netzgebühr
# ---------------------------------------------------------------------------


def test_netzgebuehr_aus_dem_netzbereich_ist_die_summe_aller_posten():
    """Netznutzung, Netzverlust, Elektrizitätsabgabe, Förderbeitrag — brutto.

    Bis 2.1.1-dev14 stand hier nur das Netznutzungsentgelt; auf einer Anlage
    in Oberösterreich fehlten dadurch rund 1,5 ct je Kilowattstunde.
    """
    g = n.netzgebuehr_fuer({"schedule_netzbereich": "oberoesterreich"})
    assert g is not None
    assert g.ap == pytest.approx(OOE_SUMME_NETTO * 1.2 / 100)
    # Nur das Netznutzungsentgelt wird vom SNAP gesenkt
    assert g.snap == pytest.approx(
        (5.03 + OOE_VERLUST + ELEKTRIZITAETSABGABE_2026 + FOERDERBEITRAG_2026) * 1.2 / 100
    )
    assert g.winap is None
    assert g.bereich == "oberoesterreich"
    assert g.stand == "2026-04-01"
    assert "eingebaut" in (g.quelle or "")
    # Die Einzelposten stehen für die Anzeige daneben und ergeben die Summe
    assert g.netznutzung == pytest.approx(OOE_NETZNUTZUNG * 1.2 / 100)
    assert g.netzverlust == pytest.approx(OOE_VERLUST * 1.2 / 100)
    assert g.elektrizitaetsabgabe == pytest.approx(ELEKTRIZITAETSABGABE_2026 * 1.2 / 100)
    assert g.foerderbeitrag == pytest.approx(FOERDERBEITRAG_2026 * 1.2 / 100)
    assert g.ap == pytest.approx(
        g.netznutzung + g.netzverlust + g.elektrizitaetsabgabe + g.foerderbeitrag
    )


def test_netzgebuehr_manuell_rechnet_den_snap_selbst():
    """Handeingabe = Netznutzungsentgelt; die bundesweiten Abgaben kommen dazu.

    Das Netzverlustentgelt hängt am Netzbereich — ohne ihn ist es nicht
    bekannt und bleibt draußen, statt geraten zu werden.
    """
    abgaben = (ELEKTRIZITAETSABGABE_2026 + FOERDERBEITRAG_2026) * 1.2 / 100
    g = n.netzgebuehr_fuer({"schedule_netzbereich": "manual", "schedule_network_fee": 0.06})
    assert g is not None
    assert g.ap == pytest.approx(0.06 + abgaben)
    assert g.snap == pytest.approx(0.048 + abgaben)
    assert g.netzverlust == 0.0
    assert g.winap is None
    assert g.quelle == "Handeingabe"
    # Handeingabe ohne Wert: keine Netzgebühr
    assert n.netzgebuehr_fuer({"schedule_netzbereich": "manual", "schedule_network_fee": 0}) is None


def test_ohne_netzbereich_keine_netzgebuehr():
    assert n.netzgebuehr_fuer({}) is None
    assert n.netzgebuehr_fuer({"schedule_netzbereich": ""}) is None
    assert n.netzgebuehr_fuer({"schedule_netzbereich": "atlantis"}) is None


def test_gelesene_tabelle_schlaegt_den_schnappschuss_bereichsweise():
    tabelle = n.Tariftabelle(
        stand="2027-01-01", quelle="SNE-T-V § 9 (RIS NOR1)", url=None,
        tarife={"wien": n.Netztarif(5.30, 4.24, 4.24)},
    )
    # Die erfundene Tabelle kennt kein Netzverlustentgelt und keine Abgaben;
    # beides kommt für diese Posten weiter aus dem Schnappschuss, statt mit
    # null angesetzt zu werden.
    fest = (0.700 + ELEKTRIZITAETSABGABE_2026 + FOERDERBEITRAG_2026) * 1.2 / 100
    wien = n.netzgebuehr_fuer({"schedule_netzbereich": "wien"}, tabelle)
    assert wien.ap == pytest.approx(5.30 * 1.2 / 100 + fest)
    assert wien.winap == pytest.approx(4.24 * 1.2 / 100 + fest)
    assert wien.stand == "2027-01-01"
    # Ein Bereich, den die gelesene Tabelle nicht kennt, kommt aus dem Schnappschuss
    abgaben = (ELEKTRIZITAETSABGABE_2026 + FOERDERBEITRAG_2026) * 1.2 / 100
    linz = n.netzgebuehr_fuer({"schedule_netzbereich": "linz"}, tabelle)
    assert linz.ap == pytest.approx((5.57 + 0.487) * 1.2 / 100 + abgaben)
    assert linz.stand == "2026-04-01"


def test_status_traegt_alle_bereiche_mit_brutto_und_netto():
    status = n.tabelle_status(n.SNAPSHOT)
    assert status["aus_snapshot"] is True
    assert status["mwst"] == 1.2
    assert [b["key"] for b in status["bereiche"]] == [k for k, _ in n.NETZBEREICHE]
    wien = next(b for b in status["bereiche"] if b["key"] == "wien")
    assert wien["label"] == "Wien (Wiener Netze)"
    assert wien["ap_netto"] == 6.98
    assert wien["ap_brutto"] == pytest.approx(0.08376)
    assert wien["winap_brutto"] is None


def test_tabelle_ueberlebt_die_speicherung():
    roh = n.SNAPSHOT.als_dict()
    zurueck = n.Tariftabelle.aus_dict(json.loads(json.dumps(roh)))
    assert zurueck.tarife == n.SNAPSHOT.tarife
    assert zurueck.stand == n.SNAPSHOT.stand


# ---------------------------------------------------------------------------
# Provider: RIS-Abruf mit nachgestelltem HTTP
# ---------------------------------------------------------------------------


def _ris_antwort(dokumente: list[dict]) -> str:
    """Eine RIS-API-Antwort im echten Aufbau, reduziert auf das Gelesene."""
    refs = [
        {
            "Data": {
                "Metadaten": {
                    "Technisch": {"ID": d["nor"]},
                    "Bundesrecht": {
                        "Kurztitel": d.get("kurztitel", "Systemnutzungsentgelte-Verordnung 2018"),
                        "BrKons": {
                            "ArtikelParagraphAnlage": d["paragraf"],
                            "Inkrafttretensdatum": d.get("inkrafttreten", "2026-04-01"),
                        },
                    },
                },
                "Dokumentliste": {
                    "ContentReference": {
                        "Urls": {
                            "ContentUrl": [
                                {"DataType": "Xml", "Url": f"https://ogd.example/{d['nor']}.xml"},
                                {"DataType": "Html", "Url": f"https://ogd.example/{d['nor']}.html"},
                            ]
                        }
                    }
                },
            }
        }
        for d in dokumente
    ]
    return json.dumps(
        {
            "OgdSearchResult": {
                "OgdDocumentResults": {
                    "Hits": {"#text": str(len(refs))},
                    "OgdDocumentReference": refs if len(refs) != 1 else refs[0],
                }
            }
        }
    )


class _Netz:
    """Antwortet auf RIS-Abfragen wie das echte RIS am 12.09.2026."""

    def __init__(self, html: str, tarifverordnung: list[dict] | None = None):
        self.html = html
        self.tarifverordnung = tarifverordnung or []
        self.aufrufe: list[tuple[str, dict | None]] = []

    async def __call__(self, url: str, params: dict | None = None) -> str:
        self.aufrufe.append((url, params))
        if url == n.RIS_API_URL:
            if params.get("Titel"):
                # Die SNE-T-V ist noch nicht in Kraft → keine Treffer
                return _ris_antwort(self.tarifverordnung)
            return _ris_antwort(
                [
                    {"nor": "NOR40273636", "paragraf": "§ 2", "inkrafttreten": "2026-01-01"},
                    {"nor": "NOR40273644", "paragraf": "§ 5", "inkrafttreten": "2026-04-01"},
                ]
            )
        return self.html


async def test_provider_liest_die_tabelle_aus_dem_ris(monkeypatch):
    provider = n.NetzentgeltProvider(hass=None, entry_id="e1")
    netz = _Netz(FIXTURE.read_text(encoding="utf-8"))
    monkeypatch.setattr(provider, "_get_text", netz)

    tabelle = await provider.async_fetch()

    assert tabelle.aus_snapshot is False
    assert tabelle.stand == "2026-04-01"
    assert "NOR40273644" in tabelle.quelle and "SNE-V 2018" in tabelle.quelle
    # Dieselben Sätze wie der Schnappschuss; das Netzverlustentgelt steht
    # nicht in § 5, der Attrappe fehlt es also und es bleibt beim Rückfall.
    assert {k: (t.ap, t.snap) for k, t in tabelle.tarife.items()} == {
        k: (t.ap, t.snap) for k, t in n.SNAPSHOT.tarife.items()
    }
    # Titelsuche (SNE-T-V) → Gesetzesnummer (SNE-V 2018) → ein HTML …
    assert [u for u, _ in netz.aufrufe[:3]] == [
        n.RIS_API_URL, n.RIS_API_URL, "https://ogd.example/NOR40273644.html",
    ]
    assert netz.aufrufe[1][1]["Gesetzesnummer"] == "20010107"
    assert netz.aufrufe[1][1]["Fassung.FassungVom"]
    # … danach die Zusatzposten, jeder mit eigener Suche
    gesucht = [p for u, p in netz.aufrufe[3:] if u == n.RIS_API_URL and p]
    assert any(p.get("Titel") == "Elektrizitätsabgabegesetz" for p in gesucht)
    assert any(
        p.get("Titel") == "Erneuerbaren-Förderbeitragsverordnung" for p in gesucht
    )
    status = provider.status()
    assert status["aus_snapshot"] is False and status["fehler"] is None

    # Innerhalb der Tagesfrist kein zweiter Abruf
    vorher = len(netz.aufrufe)
    await provider.async_fetch()
    assert len(netz.aufrufe) == vorher


async def test_provider_nimmt_die_tarifverordnung_sobald_sie_gilt(monkeypatch):
    """Ab 2027: die Titelsuche liefert die SNE-T-V, sie hat Vorrang."""
    zeilen = "".join(
        f"<tr><td>x)</td><td>Bereich {name}:</td><td>1</td><td>2</td><td>{ap}</td><td>{snap}</td><td>{winap}</td></tr>"
        for name, ap, snap, winap in (
            ("Burgenland", "4,10", "3,28", "3,28"), ("Kärnten", "4,20", "3,36", "3,36"),
            ("Klagenfurt", "4,30", "3,44", "3,44"), ("Niederösterreich", "4,40", "3,52", "3,52"),
            ("Oberösterreich", "4,50", "3,60", "3,60"), ("Linz", "4,60", "3,68", "3,68"),
            ("Salzburg", "4,70", "3,76", "3,76"), ("Steiermark", "4,80", "3,84", "3,84"),
            ("Graz", "4,90", "3,92", "3,92"), ("Tirol", "5,00", "4,00", "4,00"),
            ("Innsbruck", "5,10", "4,08", "4,08"), ("Vorarlberg", "5,20", "4,16", "4,16"),
            ("Wien", "5,30", "4,24", "4,24"), ("Kleinwalsertal", "9,00", "7,20", "7,20"),
        )
    )
    provider = n.NetzentgeltProvider(hass=None, entry_id="e1")
    netz = _Netz(
        _tabelle_2027(zeilen),
        tarifverordnung=[
            {"nor": "NOR9", "paragraf": "§ 9", "inkrafttreten": "2027-01-01",
             "kurztitel": "Systemnutzungsentgelte-Tarifverordnung 2027"},
            # Eine Gas-Verordnung im Titeltreffer darf nicht gelesen werden
            {"nor": "NORGAS", "paragraf": "§ 3", "kurztitel": "Gas-Systemnutzungsentgelte-Verordnung 2013"},
        ],
    )
    monkeypatch.setattr(provider, "_get_text", netz)

    tabelle = await provider.async_fetch()

    assert "SNE-T-V § 9" in tabelle.quelle and "NOR9" in tabelle.quelle
    assert tabelle.stand == "2027-01-01"
    assert tabelle.tarife["wien"].winap == pytest.approx(4.24)
    assert not any("NORGAS" in u for u, _ in netz.aufrufe)
    assert netz.aufrufe[0][1]["Suchworte"] == "Netzebene 7"


async def test_alter_cache_gilt_nicht_als_frisch(monkeypatch):
    """Ein gespeicherter Stand ohne die neuen Posten muss neu geholt werden.

    Auf einer Anlage stand nach dem Update auf 2.1.1-dev15 ein
    Netzverlustentgelt von null: Der Cache stammte aus der Vorversion, kannte
    das Feld nicht — und weil er erst drei Stunden alt war, hielt die
    Tagesfrist den Abruf auf, der es ergänzt hätte.
    """
    alt = {
        "stand": "2026-04-01",
        "quelle": "SNE-V 2018 § 5 (RIS NOR40273644)",
        "url": None,
        "tarife": {"oberoesterreich": {"ap": 6.29, "snap": 5.03, "winap": None}},
    }
    assert n.ist_vollstaendig(n.Tariftabelle.aus_dict(alt)) is False
    assert n.ist_vollstaendig(n.SNAPSHOT) is True

    provider = n.NetzentgeltProvider(hass=None, entry_id="e1")

    class _Store:
        async def async_load(self):
            return {"tabelle": alt, "geholt": _utcnow().isoformat()}

    provider._store = _Store()
    await provider.async_load()
    # Tabelle übernommen, aber nicht als frisch verbucht
    assert provider.tabelle.tarife["oberoesterreich"].ap == 6.29
    assert provider._geholt is None

    # Solange der Abruf noch nicht lief, trägt der Schnappschuss den Posten
    g = provider.netzgebuehr({"schedule_netzbereich": "oberoesterreich"})
    assert g.netzverlust == pytest.approx(OOE_VERLUST * 1.2 / 100)


async def test_provider_faellt_bei_fehler_auf_den_schnappschuss(monkeypatch):
    provider = n.NetzentgeltProvider(hass=None, entry_id="e1")

    async def kaputt(url, params=None):
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(provider, "_get_text", kaputt)
    tabelle = await provider.async_fetch()
    assert tabelle is n.SNAPSHOT
    assert "503" in provider.status()["fehler"]
    # Alle vier Posten aus dem Schnappschuss
    assert provider.netzgebuehr({"schedule_netzbereich": "graz"}).ap == pytest.approx(
        (5.17 + 0.658 + ELEKTRIZITAETSABGABE_2026 + FOERDERBEITRAG_2026) * 1.2 / 100
    )


async def test_provider_meldet_unlesbare_tabelle(monkeypatch):
    provider = n.NetzentgeltProvider(hass=None, entry_id="e1")
    netz = _Netz("<html><body><p>Seite umgebaut</p></body></html>")
    monkeypatch.setattr(provider, "_get_text", netz)
    tabelle = await provider.async_fetch()
    assert tabelle is n.SNAPSHOT
    assert "Netzebene 7" in provider.status()["fehler"]


# ---------------------------------------------------------------------------
# Anbindung: Panel-Liste und Anlegen des Anbieters
# ---------------------------------------------------------------------------


def _quelldatei(*teile: str) -> str:
    return (
        Path(__file__).resolve().parents[1].joinpath(*teile)
    ).read_text(encoding="utf-8")


def test_panel_kennt_dieselben_netzbereiche():
    """Das Dropdown im Panel und NETZBEREICHE hier müssen dieselbe Liste
    sein — sonst schickt das Panel einen Schlüssel, zu dem das Backend
    keinen Satz kennt, und der Fahrplan rechnet stumm ohne Netzgebühr."""
    js = _quelldatei(
        "custom_components", "eeg_energy_optimizer", "frontend", "eeg-optimizer-panel.js"
    )
    block = js.split("const NETZBEREICHE = [", 1)[1].split("];", 1)[0]
    im_panel = re.findall(r'\["([^"]+)",\s*"([^"]+)"\]', block)
    assert im_panel == [list(p) and (p[0], p[1]) for p in n.NETZBEREICHE]


def test_anbieter_wird_vor_dem_wizard_abbruch_angelegt():
    """Wie die Preis-Anbieter: Der Einrichtungsassistent zeigt die Sätze zum
    gewählten Netzbereich, und der WebSocket-Befehl findet den Anbieter nur,
    wenn er vor dem frühen Ausstieg bei unvollständiger Einrichtung steht."""
    quelle = _quelldatei("custom_components", "eeg_energy_optimizer", "__init__.py")
    assert quelle.index('["netzentgelt"] = netzentgelt_provider') < quelle.index(
        "if not setup_complete:"
    )
