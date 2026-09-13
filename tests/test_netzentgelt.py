"""Netznutzungsentgelt je Netzbereich aus der Verordnung (netzentgelt.py).

Die Tabelle kommt aus dem RIS; hier wird sie gegen eine echte Kopie von
§ 5 SNE-V 2018 (RIS NOR40273644, gültig ab 1.4.2026) gelesen und gegen eine
erfundene Tabelle im Aufbau, den die Tarifverordnung ab 2027 haben dürfte
(eine Zeile je Bereich, Spalten AP/SNAP/WiNAP).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from custom_components.eeg_energy_optimizer import netzentgelt as n

FIXTURE = Path(__file__).parent / "fixtures" / "ris_snev2018_p5_NOR40273644.html"


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
    """Der Rückfall darf nicht von dem abweichen, was das RIS liefert."""
    tarife = n.parse_tabelle(FIXTURE.read_text(encoding="utf-8"))
    assert tarife == n.SNAPSHOT.tarife
    assert n.SNAPSHOT.aus_snapshot is True


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


def test_netzgebuehr_aus_dem_netzbereich_ist_brutto():
    g = n.netzgebuehr_fuer({"schedule_netzbereich": "oberoesterreich"})
    assert g is not None
    assert g.ap == pytest.approx(6.29 * 1.2 / 100)
    assert g.snap == pytest.approx(5.03 * 1.2 / 100)
    assert g.winap is None
    assert g.bereich == "oberoesterreich"
    assert g.stand == "2026-04-01"
    assert "eingebaut" in (g.quelle or "")


def test_netzgebuehr_manuell_rechnet_den_snap_selbst():
    g = n.netzgebuehr_fuer({"schedule_netzbereich": "manual", "schedule_network_fee": 0.06})
    assert g is not None
    assert g.ap == pytest.approx(0.06)
    assert g.snap == pytest.approx(0.048)
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
    wien = n.netzgebuehr_fuer({"schedule_netzbereich": "wien"}, tabelle)
    assert wien.ap == pytest.approx(5.30 * 1.2 / 100)
    assert wien.winap == pytest.approx(4.24 * 1.2 / 100)
    assert wien.stand == "2027-01-01"
    # Ein Bereich, den die gelesene Tabelle nicht kennt, kommt aus dem Schnappschuss
    linz = n.netzgebuehr_fuer({"schedule_netzbereich": "linz"}, tabelle)
    assert linz.ap == pytest.approx(5.57 * 1.2 / 100)
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
    assert tabelle.tarife == n.SNAPSHOT.tarife
    # Titelsuche (SNE-T-V) → Gesetzesnummer (SNE-V 2018) → ein HTML
    assert [u for u, _ in netz.aufrufe] == [n.RIS_API_URL, n.RIS_API_URL, "https://ogd.example/NOR40273644.html"]
    assert netz.aufrufe[1][1]["Gesetzesnummer"] == "20010107"
    assert netz.aufrufe[1][1]["Fassung.FassungVom"]
    status = provider.status()
    assert status["aus_snapshot"] is False and status["fehler"] is None

    # Innerhalb der Tagesfrist kein zweiter Abruf
    await provider.async_fetch()
    assert len(netz.aufrufe) == 3


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


async def test_provider_faellt_bei_fehler_auf_den_schnappschuss(monkeypatch):
    provider = n.NetzentgeltProvider(hass=None, entry_id="e1")

    async def kaputt(url, params=None):
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(provider, "_get_text", kaputt)
    tabelle = await provider.async_fetch()
    assert tabelle is n.SNAPSHOT
    assert "503" in provider.status()["fehler"]
    assert provider.netzgebuehr({"schedule_netzbereich": "graz"}).ap == pytest.approx(5.17 * 1.2 / 100)


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
