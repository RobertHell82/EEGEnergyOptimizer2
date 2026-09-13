"""Netznutzungsentgelt je Netzbereich — aus der Verordnung statt von Hand.

In Österreich legt die E-Control das Netznutzungsentgelt je Netzbereich und
Netzebene per Verordnung fest; alle Netzbetreiber eines Bereichs verrechnen
denselben Satz. Die Verordnung steht im Rechtsinformationssystem (RIS) als
Open Data: eine JSON-Schnittstelle nennt für jeden Paragraphen die aktuell
gültige Dokumentnummer, dahinter liegt der Text als HTML — die Netzebene-7-
Tabelle mit den Spalten LP | AP | SNAP | DTAP | DNAP und je Bereich drei
Zeilen (gemessene Leistung, nicht gemessene Leistung, unterbrechbar).
Haushalte sind die Zeile „nicht gemessene Leistung".

Der Nutzer wählt nur seinen Netzbereich (14 Stück, Anlage I zum ElWG);
Arbeitspreis und SNAP-Satz kommen von hier. Die Werte in der Verordnung sind
netto — hier wird die Umsatzsteuer aufgeschlagen, weil der Fahrplan mit
Endkundenpreisen rechnet.

Zwei Verordnungen, ein Leser: Bis 31.12.2026 gilt die SNE-V 2018 (§ 5), ab
1.1.2027 die Tarifverordnung (SNE-T-V) zur Grundsatzverordnung SNE-G-V, die
zusätzlich einen Winter-Nieder-Arbeitspreis (WiNAP, Oktober–März 22–4 Uhr)
kennt. Der Tabellenleser sucht die Spalten nach Namen (AP, SNAP, WiNAP) und
kommt mit einer Zeile je Bereich ebenso zurecht wie mit den Unterzeilen der
SNE-V 2018. Die SNE-T-V ist noch nicht kundgemacht (erwartet Dezember 2026);
ihre RIS-Kennung fehlt deshalb und wird per Titelsuche ermittelt — sobald sie
bekannt ist, gehört sie in ``QUELLEN`` eingetragen, dann ist der Abruf gezielt.

Jeder Fehler lässt die zuletzt gelesene Tabelle stehen; ohne gespeicherte
Tabelle gilt der eingebaute Schnappschuss (``SNAPSHOT``). Der Stand steht
immer dabei, damit ein veralteter Satz im Panel auffällt.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Any

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from homeassistant.helpers.storage import Store
    from homeassistant.util import dt as dt_util

    _utcnow = dt_util.utcnow
except ImportError:  # Testumgebung
    _utcnow = lambda: datetime.now(tz=timezone.utc)  # noqa: E731
    async_get_clientsession = None  # type: ignore[assignment]
    Store = None  # type: ignore[assignment,misc]


# Konfigurationsschlüssel. Der Netzbereich ist ein Schlüssel aus NETZBEREICHE,
# „manual" (Netzgebühr von Hand, Feld schedule_network_fee) oder leer
# (keine getrennte Netzgebühr — sie steckt im Arbeitspreis).
CONF_SCHEDULE_NETZBEREICH = "schedule_netzbereich"
CONF_SCHEDULE_NETWORK_FEE = "schedule_network_fee"
NETZBEREICH_MANUELL = "manual"

# Umsatzsteuer auf Netzentgelte — die Verordnung nennt Nettowerte.
MWST = 1.20
# Sommer-Nieder-Arbeitspreis: 20 % unter dem Arbeitspreis (SNE-V 2018 § 5
# Abs. 1b). Genutzt für die Handeingabe; aus der Verordnung kommt der SNAP
# als eigene Spalte, gerundet wie er verrechnet wird.
SNAP_RABATT = 0.20

RIS_API_URL = "https://data.bka.gv.at/ris/api/v2.6/Bundesrecht"
RIS_USER_AGENT = "HomeAssistant/EEGEnergyOptimizer"
# Die Sätze ändern sich zum Jahreswechsel (2026 zusätzlich am 1. April);
# einmal am Tag nachsehen genügt und ist dem RIS gegenüber sparsam.
CACHE_FRESH_SECONDS = 24 * 3600
# Wie viele Dokumente einer Titelsuche höchstens gelesen werden, bevor
# aufgegeben wird — die Tabelle steht in genau einem Paragraphen.
MAX_DOKUMENTE_JE_QUELLE = 4


# Netzbereiche laut Anlage I zum ElWG (BGBl. I Nr. 91/2025), beschriftet mit
# dem Netzbetreiber, dessen Netz den Bereich bildet. Kleine Netzbetreiber
# (Stadtwerke, Genossenschaften) gehören zum Bereich ihres Bundeslandes.
NETZBEREICHE: tuple[tuple[str, str], ...] = (
    ("burgenland", "Burgenland (Netz Burgenland)"),
    ("kaernten", "Kärnten (KNG-Kärnten Netz)"),
    ("klagenfurt", "Klagenfurt (Energie Klagenfurt)"),
    ("niederoesterreich", "Niederösterreich (Netz NÖ)"),
    ("oberoesterreich", "Oberösterreich (Netz OÖ)"),
    ("linz", "Linz (LINZ NETZ)"),
    ("salzburg", "Salzburg (Salzburg Netz)"),
    ("steiermark", "Steiermark (Energienetze Steiermark)"),
    ("graz", "Graz (Stromnetz Graz)"),
    ("tirol", "Tirol (TINETZ)"),
    ("innsbruck", "Innsbruck (IKB Innsbruck)"),
    ("vorarlberg", "Vorarlberg (Vorarlberger Energienetze)"),
    ("wien", "Wien (Wiener Netze)"),
    ("kleinwalsertal", "Kleinwalsertal (Energieversorgung Kleinwalsertal)"),
)
NETZBEREICH_LABELS: dict[str, str] = dict(NETZBEREICHE)


@dataclass(frozen=True)
class Netztarif:
    """Netzentgelte eines Bereichs auf Netzebene 7, Cent/kWh netto.

    ``ap`` ist das Netznutzungsentgelt (Arbeitspreis) aus § 5, ``verlust``
    das Netzverlustentgelt aus § 6 derselben Verordnung. Nur der
    Arbeitspreis wird von den zeitvariablen Sätzen gesenkt — das
    Netzverlustentgelt gilt rund um die Uhr.
    """

    ap: float
    snap: float | None = None
    winap: float | None = None
    verlust: float | None = None


@dataclass(frozen=True)
class Abgaben:
    """Die bundesweiten kWh-Abgaben, Cent/kWh netto.

    Beide hängen nicht am Netzbereich und stehen in eigenen Rechtsquellen:
    die Elektrizitätsabgabe im Elektrizitätsabgabegesetz (Regelsatz § 4
    Abs. 2, befristete Sätze in den Übergangsbestimmungen des § 7), der
    Erneuerbaren-Förderbeitrag in der jährlich neuen
    Erneuerbaren-Förderbeitragsverordnung (§ 2). Von SNAP und WiNAP bleiben
    beide unberührt.

    Nicht enthalten sind die Posten je Zählpunkt statt je Kilowattstunde
    (Erneuerbaren-Pauschale, Messentgelt, Grundpreis): Für die Frage, was
    eine gespeicherte Kilowattstunde wert ist, zählen sie nicht mit.
    """

    elektrizitaet: float = 0.0
    foerderbeitrag: float = 0.0
    elektrizitaet_quelle: str | None = None
    foerderbeitrag_quelle: str | None = None

    @property
    def summe(self) -> float:
        return round(self.elektrizitaet + self.foerderbeitrag, 6)

    def als_dict(self) -> dict[str, Any]:
        return {
            "elektrizitaet": self.elektrizitaet,
            "foerderbeitrag": self.foerderbeitrag,
            "elektrizitaet_quelle": self.elektrizitaet_quelle,
            "foerderbeitrag_quelle": self.foerderbeitrag_quelle,
        }

    @classmethod
    def aus_dict(cls, roh: Any) -> "Abgaben | None":
        if not isinstance(roh, dict):
            return None
        try:
            return cls(
                elektrizitaet=float(roh.get("elektrizitaet") or 0.0),
                foerderbeitrag=float(roh.get("foerderbeitrag") or 0.0),
                elektrizitaet_quelle=roh.get("elektrizitaet_quelle"),
                foerderbeitrag_quelle=roh.get("foerderbeitrag_quelle"),
            )
        except (TypeError, ValueError):
            return None


@dataclass(frozen=True)
class Tariftabelle:
    """Die gelesene Netzebene-7-Tabelle samt Herkunft."""

    stand: str                     # Inkrafttreten der Fassung, ISO-Datum
    quelle: str                    # z. B. „SNE-V 2018 § 5 (RIS NOR40273644)"
    url: str | None
    tarife: dict[str, Netztarif] = field(default_factory=dict)
    aus_snapshot: bool = False
    abgaben: "Abgaben | None" = None
    verlust_quelle: str | None = None

    def als_dict(self) -> dict[str, Any]:
        return {
            "stand": self.stand,
            "quelle": self.quelle,
            "url": self.url,
            "aus_snapshot": self.aus_snapshot,
            "verlust_quelle": self.verlust_quelle,
            "abgaben": None if self.abgaben is None else self.abgaben.als_dict(),
            "tarife": {
                k: {
                    "ap": t.ap, "snap": t.snap, "winap": t.winap,
                    "verlust": t.verlust,
                }
                for k, t in self.tarife.items()
            },
        }

    @classmethod
    def aus_dict(cls, roh: dict[str, Any]) -> "Tariftabelle":
        tarife = {}
        for k, t in (roh.get("tarife") or {}).items():
            if k in NETZBEREICH_LABELS and t.get("ap") is not None:
                tarife[k] = Netztarif(
                    ap=float(t["ap"]),
                    snap=None if t.get("snap") is None else float(t["snap"]),
                    winap=None if t.get("winap") is None else float(t["winap"]),
                    verlust=None if t.get("verlust") is None else float(t["verlust"]),
                )
        return cls(
            stand=str(roh.get("stand") or ""),
            quelle=str(roh.get("quelle") or ""),
            url=roh.get("url"),
            tarife=tarife,
            aus_snapshot=bool(roh.get("aus_snapshot", False)),
            abgaben=Abgaben.aus_dict(roh.get("abgaben")),
            verlust_quelle=roh.get("verlust_quelle"),
        )


# Eingebauter Schnappschuss: SNE-V 2018 § 5 Z 6 idF BGBl. II Nr. 305/2025,
# gültig ab 1.4.2026 (RIS-Dokument NOR40273644), Zeile „nicht gemessene
# Leistung", Cent/kWh netto. Gilt, bis der erste RIS-Abruf gelungen ist, und
# als Rückfall, wenn das RIS nicht erreichbar oder die Tabelle nicht lesbar
# ist. Bei jeder neuen Verordnung nachziehen.
SNAPSHOT = Tariftabelle(
    stand="2026-04-01",
    quelle="SNE-V 2018 § 5 idF BGBl. II Nr. 305/2025 (RIS NOR40273644), eingebaut",
    url="https://ogd.ris.bka.gv.at/Dokumente/Bundesnormen/NOR40273644/NOR40273644.html",
    tarife={
        # ap, snap, winap, verlust — verlust aus § 6 derselben Fassung
        "burgenland": Netztarif(8.46, 6.77, None, 0.000),
        "kaernten": Netztarif(9.67, 7.74, None, 0.368),
        "klagenfurt": Netztarif(6.90, 5.52, None, 0.578),
        "niederoesterreich": Netztarif(8.79, 7.03, None, 0.384),
        "oberoesterreich": Netztarif(6.29, 5.03, None, 0.528),
        "linz": Netztarif(5.57, 4.46, None, 0.487),
        "salzburg": Netztarif(6.59, 5.27, None, 0.357),
        "steiermark": Netztarif(8.82, 7.06, None, 0.336),
        "graz": Netztarif(5.17, 4.14, None, 0.658),
        "tirol": Netztarif(6.81, 5.45, None, 0.293),
        "innsbruck": Netztarif(8.03, 6.42, None, 0.453),
        "vorarlberg": Netztarif(4.96, 3.97, None, 0.393),
        "wien": Netztarif(6.98, 5.58, None, 0.700),
        "kleinwalsertal": Netztarif(17.73, 14.18, None, 0.401),
    },
    aus_snapshot=True,
    verlust_quelle="SNE-V 2018 § 6 (RIS NOR40273639), eingebaut",
    # Elektrizitätsabgabe: für 2026 auf 0,1 ct gesenkt (ElAbgG § 7), ab
    # 1.1.2027 gilt wieder der Regelsatz von 1,5 ct aus § 4 Abs. 2 — der
    # Abruf holt das von selbst, der Schnappschuss altert an dieser Stelle
    # also planmäßig. Förderbeitrag: EFBV 2026 § 2, Netzebene 7 ohne
    # Leistungsmessung (0,583 Arbeit + 0,037 Verlust).
    abgaben=Abgaben(
        elektrizitaet=0.1,
        foerderbeitrag=0.62,
        elektrizitaet_quelle="ElAbgG § 7, befristet bis 2027-01-01 (eingebaut)",
        foerderbeitrag_quelle="Erneuerbaren-Förderbeitragsverordnung 2026 § 2 (eingebaut)",
    ),
)


@dataclass(frozen=True)
class Quelle:
    """Eine Verordnung im RIS, in der die Netzebene-7-Tabelle steht."""

    name: str
    # RIS-Gesetzesnummer (Anwendung BrKons); None = per Titel suchen.
    gesetzesnummer: str | None
    # Titelsuche, wenn die Gesetzesnummer noch nicht bekannt ist.
    titel: str | None
    # Paragraph mit der Tabelle; None = per Suchwort „Netzebene 7" finden.
    paragraf: str | None


# Reihenfolge = Vorrang. Das RIS liefert nur Fassungen, die am Stichtag in
# Kraft sind: Solange die SNE-T-V nicht gilt, findet ihre Suche nichts, und
# die SNE-V 2018 kommt zum Zug; ab 2027 ist es umgekehrt.
QUELLEN: tuple[Quelle, ...] = (
    # TODO 2027: Gesetzesnummer und Paragraph der kundgemachten SNE-T-V
    # eintragen (erwartet Dezember 2026), Schnappschuss nachziehen.
    Quelle("SNE-T-V", None, "Systemnutzungsentgelte-Tarifverordnung", None),
    Quelle("SNE-V 2018", "20010107", None, "§ 5"),
)

# Das Netzverlustentgelt steht in derselben Verordnung, einen Paragraphen
# weiter — als Matrix Netzbereich × Netzebene statt als Block je Netzebene.
QUELLE_NETZVERLUST = Quelle("SNE-V 2018", "20010107", None, "§ 6")
# Die beiden bundesweiten Abgaben, jede in ihrer eigenen Rechtsquelle. Die
# Förderbeitragsverordnung wird jährlich neu erlassen und trägt die Jahreszahl
# im Titel; die Titelsuche ohne Jahr findet die am Stichtag geltende Fassung.
QUELLE_ELEKTRIZITAETSABGABE = Quelle(
    "Elektrizitätsabgabegesetz", None, "Elektrizitätsabgabegesetz", "§ 4"
)
QUELLE_ELEKTRIZITAETSABGABE_UEBERGANG = Quelle(
    "Elektrizitätsabgabegesetz", None, "Elektrizitätsabgabegesetz", "§ 7"
)
QUELLE_FOERDERBEITRAG = Quelle(
    "Erneuerbaren-Förderbeitragsverordnung", None,
    "Erneuerbaren-Förderbeitragsverordnung", "§ 2",
)


# ---------------------------------------------------------------------------
# Tabelle lesen
# ---------------------------------------------------------------------------


class _Tabellenleser(HTMLParser):
    """Alle Tabellen einer Seite als Listen von Zeilen aus Zelltexten."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


_RE_NETZEBENE_7 = re.compile(r"Netzebene\s*7\b", re.IGNORECASE)
_RE_ANDERE_EBENE = re.compile(r"Netzebene\s*[1-6]\b|Netzebene\s*[1-6]\s*[–-]", re.IGNORECASE)
_RE_BEREICH = re.compile(r"(?:Netz)?Bereich\s+(.+?)\s*:?\s*$", re.IGNORECASE)
_RE_NEUE_ZIFFER = re.compile(r"^\s*\d+\s*\.\s*(Ziffer\s*\d+)?", re.IGNORECASE)
_RE_ZAHL = re.compile(r"^-?\d{1,3}(?:[.\s]\d{3})*(?:,\d+)?$|^-?\d+(?:[.,]\d+)?$")


def _zahl(text: str) -> float | None:
    """„8,46" → 8.46; leere Zelle oder Text → None."""
    t = text.strip().replace("\xa0", " ")
    if not t or not _RE_ZAHL.match(t):
        return None
    t = t.replace(" ", "")
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def _bereich_schluessel(name: str) -> str | None:
    """„Oberösterreich" → „oberoesterreich"; unbekannte Namen → None."""
    n = name.strip().lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        n = n.replace(a, b)
    n = re.sub(r"[^a-z]", "", n)
    return n if n in NETZBEREICH_LABELS else None


def _spaltenindex(header: list[str], name: str) -> int | None:
    for i, zelle in enumerate(header):
        if zelle.strip().lower() == name.lower():
            return i
    return None


def parse_tabelle(html: str) -> dict[str, Netztarif]:
    """Die Netzebene-7-Tabelle aus dem Paragraphentext lesen.

    Spalten werden nach Namen gefunden (AP, SNAP, WiNAP), Bereiche an der
    Zeile „Bereich X:". Stehen die Werte in Unterzeilen (SNE-V 2018), gilt
    „nicht gemessene Leistung" — das ist der Haushalt; fehlt sie, die
    Bereichszeile selbst, sonst „gemessene Leistung". Löst
    ``ValueError`` aus, wenn keine brauchbare Tabelle da ist.
    """
    leser = _Tabellenleser()
    leser.feed(html)

    zeilen: list[list[str]] | None = None
    start = 0
    for tabelle in leser.tables:
        for i, zeile in enumerate(tabelle):
            if zeile and _RE_NETZEBENE_7.search(zeile[0]):
                zeilen, start = tabelle, i
                break
        if zeilen is not None:
            break
    if zeilen is None:
        raise ValueError("keine Tabelle mit 'Netzebene 7' gefunden")

    ap_idx = snap_idx = winap_idx = None
    tarife: dict[str, tuple[int, Netztarif]] = {}
    bereich: str | None = None

    for zeile in zeilen[start + 1:]:
        if not zeile:
            continue
        erste = zeile[0]
        # Nächste Ziffer / andere Netzebene: Ende des Abschnitts.
        if ap_idx is not None and (
            _RE_ANDERE_EBENE.search(erste) or (_RE_NEUE_ZIFFER.match(erste) and "Ziffer" in erste)
        ):
            break
        if ap_idx is None:
            idx = _spaltenindex(zeile, "AP")
            if idx is not None:
                ap_idx = idx
                snap_idx = _spaltenindex(zeile, "SNAP")
                winap_idx = _spaltenindex(zeile, "WiNAP")
            continue

        # Bereichszeile? Der Name steht in der ersten oder zweiten Zelle.
        neuer_bereich = None
        for zelle in zeile[:2]:
            m = _RE_BEREICH.match(zelle.strip())
            if m:
                neuer_bereich = _bereich_schluessel(m.group(1))
                break
        if neuer_bereich is not None:
            bereich = neuer_bereich
            prioritaet = 2
        elif bereich is None:
            continue
        else:
            text = " ".join(zeile[:2]).lower()
            if "unterbrechbar" in text:
                continue
            if "nicht gemessen" in text:
                prioritaet = 1
            elif "gemessen" in text:
                prioritaet = 3
            else:
                continue

        def wert(idx: int | None) -> float | None:
            return _zahl(zeile[idx]) if idx is not None and idx < len(zeile) else None

        ap = wert(ap_idx)
        if ap is None:
            continue
        tarif = Netztarif(ap=ap, snap=wert(snap_idx), winap=wert(winap_idx))
        bisher = tarife.get(bereich)
        if bisher is None or prioritaet < bisher[0]:
            tarife[bereich] = (prioritaet, tarif)

    ergebnis = {k: t for k, (_, t) in tarife.items()}
    if len(ergebnis) < 10:
        raise ValueError(
            f"Netzebene-7-Tabelle unvollständig: {len(ergebnis)} von "
            f"{len(NETZBEREICHE)} Bereichen gelesen"
        )
    return ergebnis


# ---------------------------------------------------------------------------
# Wirksame Netzgebühr eines Anschlusses
# ---------------------------------------------------------------------------


# Netzebene-7-Spalte in der Matrix des § 6: „NE 7", in der Tarifverordnung
# ab 2027 vielleicht wieder ausgeschrieben.
_RE_NE7_SPALTE = re.compile(r"^\s*(?:NE|Netzebene)\s*7\s*$", re.IGNORECASE)


def parse_verlust_tabelle(html: str) -> dict[str, float]:
    """Netzverlustentgelt je Netzbereich aus § 6 SNE-V lesen, Cent/kWh netto.

    Anderer Aufbau als § 5: eine Matrix, Zeilen sind die Netzbereiche,
    Spalten die Netzebenen („NE 1" … „NE 7"). Gesucht wird die NE-7-Spalte;
    ein Strich („-") heißt, dass der Bereich diese Ebene nicht hat, und wird
    übergangen.

    Löst ``ValueError`` aus, wenn keine NE-7-Spalte zu finden ist.
    """
    leser = _Tabellenleser()
    leser.feed(html)

    for tabelle in leser.tables:
        spalte = None
        for zeile in tabelle:
            treffer = [i for i, z in enumerate(zeile) if _RE_NE7_SPALTE.match(z)]
            if treffer:
                spalte = treffer[-1]
                break
        if spalte is None:
            continue
        werte: dict[str, float] = {}
        for zeile in tabelle:
            if len(zeile) <= spalte:
                continue
            # Der Bereichsname steht nicht zwingend in der ersten Zelle — die
            # Zeilen der Verordnung beginnen mit ihrer Nummer („2.").
            key = None
            for zelle in zeile[:spalte]:
                key = _bereich_schluessel(zelle)
                if key is not None:
                    break
            if key is None:
                continue
            zahl = _zahl(zeile[spalte])
            if zahl is not None:
                werte[key] = zahl
        if werte:
            return werte
    raise ValueError("keine Tabelle mit einer Netzebene-7-Spalte gefunden")


# „Die Abgabe beträgt 0,015 Euro je kWh." (§ 4 Abs. 2 ElAbgG)
_RE_ABGABE_REGEL = re.compile(
    r"Die Abgabe beträgt\s+(\d+[,.]\d+)\s*Euro je kWh", re.IGNORECASE
)
# Befristete Sätze in § 7: „… 0,001 Euro je kWh für die Lieferung von
# elektrischer Energie an natürliche Personen …"
_RE_ABGABE_HAUSHALT = re.compile(
    r"(\d+[,.]\d+)\s*Euro je kWh für die Lieferung von elektrischer Energie\s+"
    r"an natürliche Personen",
    re.IGNORECASE,
)
# Zeitraum einer Übergangsbestimmung: „Für Vorgänge nach dem 31. Dezember 2025
# und vor dem 1. Jänner 2027"
_MONATE = {
    "jänner": 1, "januar": 1, "februar": 2, "märz": 3, "april": 4, "mai": 5,
    "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10,
    "november": 11, "dezember": 12,
}
_RE_ZEITRAUM = re.compile(
    r"nach dem\s+(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\s*(\d{4})\s+und vor dem\s+"
    r"(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)\s*(\d{4})",
    re.IGNORECASE,
)


def _datum(tag: str, monat: str, jahr: str) -> date | None:
    m = _MONATE.get(monat.strip().lower())
    if m is None:
        return None
    try:
        return date(int(jahr), m, int(tag))
    except ValueError:
        return None


def parse_elektrizitaetsabgabe(
    text_regel: str, text_uebergang: str | None, stichtag: date
) -> tuple[float, str] | None:
    """Elektrizitätsabgabe für Haushalte am Stichtag, Cent/kWh netto.

    Der Regelsatz steht in § 4 Abs. 2 („Die Abgabe beträgt X Euro je kWh").
    Befristete Senkungen stehen als Übergangsbestimmung in § 7 und nennen
    einen Zeitraum — gilt er am Stichtag, hat er Vorrang. 2026 sind das
    0,001 Euro/kWh für Lieferungen an natürliche Personen statt der
    regulären 0,015; Anfang 2027 fällt der Satz ohne Zutun zurück.

    ``None``, wenn nicht einmal der Regelsatz lesbar ist.
    """
    regel = _RE_ABGABE_REGEL.search(_flachtext(text_regel))
    satz = _zahl(regel.group(1)) if regel else None

    if text_uebergang:
        flach = _flachtext(text_uebergang)
        # Absatzweise, damit Zeitraum und Satz zusammengehören. Die RIS-
        # Langform wiederholt jeden Absatz ausgeschrieben — das stört nicht,
        # beide Schreibweisen nennen denselben Satz.
        for stueck in re.split(r"\((?=\d+\))|Absatz\s+\d+,", flach):
            treffer = _RE_ABGABE_HAUSHALT.search(stueck)
            if not treffer:
                continue
            zeitraum = _RE_ZEITRAUM.search(stueck)
            if zeitraum is None:
                continue
            von = _datum(*zeitraum.group(1, 2, 3))
            bis = _datum(*zeitraum.group(4, 5, 6))
            if von is None or bis is None or not (von < stichtag < bis):
                continue
            befristet = _zahl(treffer.group(1))
            if befristet is not None:
                return (
                    round(befristet * 100.0, 6),
                    f"ElAbgG § 7, befristet bis {bis.isoformat()}",
                )

    if satz is None:
        return None
    return round(satz * 100.0, 6), "ElAbgG § 4 Abs. 2"


# „auf der Netzebene 7 (nicht gemessene Leistung) 0,583 Cent/kWh"
_RE_EFB_NE7 = re.compile(
    r"Netzebene\s*7\s*\(nicht gemessene Leistung\)\s*(\d+[,.]\d+)\s*Cent/kWh",
    re.IGNORECASE,
)
# „auf der Netzebene 7 0,037 Cent/kWh" — im Absatz zum Netzverlustentgelt
_RE_EFB_VERLUST_NE7 = re.compile(
    r"Netzebene\s*7\s+(\d+[,.]\d+)\s*Cent/kWh", re.IGNORECASE
)


def parse_foerderbeitrag(html: str) -> tuple[float, str] | None:
    """Erneuerbaren-Förderbeitrag je kWh auf Netzebene 7, Cent/kWh netto.

    Die Verordnung teilt ihn auf die Netzentgelt-Komponenten auf: ein Anteil
    am Netznutzungsentgelt (Arbeit) und einer am Netzverlustentgelt. Für eine
    Kilowattstunde zählt die Summe der beiden. Der dritte Absatz — der Anteil
    am Netznutzungsentgelt (Leistung) — bleibt außen vor: Er wird je
    Zählpunkt und Jahr verrechnet, nicht je Kilowattstunde.

    Haushalte hängen an der Netzebene 7 ohne Leistungsmessung.
    """
    flach = _flachtext(html)
    arbeit = _RE_EFB_NE7.search(flach)
    if arbeit is None:
        return None
    summe = _zahl(arbeit.group(1))
    if summe is None:
        return None
    # Der Verlust-Anteil steht im Absatz „Netzentgeltkomponente
    # Netzverlustentgelt"; ab dort suchen, damit nicht der Arbeits-Absatz
    # noch einmal trifft.
    i = flach.lower().find("netzentgeltkomponente netzverlustentgelt")
    if i >= 0:
        verlust = _RE_EFB_VERLUST_NE7.search(flach[i:])
        if verlust is not None:
            zahl = _zahl(verlust.group(1))
            if zahl is not None:
                summe += zahl
    return round(summe, 6), "Erneuerbaren-Förderbeitragsverordnung § 2"


def _flachtext(html: str) -> str:
    """HTML zu einer Textzeile — Tags weg, Weißraum vereinheitlicht."""
    ohne = re.sub(r"(?is)<(style|script|head)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"<[^>]+>", " ", ohne)
    text = unescape(text)
    return re.sub(r"\s+", " ", text)


@dataclass(frozen=True)
class Netzgebuehr:
    """Was neben dem Arbeitspreis des Lieferanten je kWh anfällt, €/kWh brutto.

    ``ap`` ist die Summe aller vier Posten — das ist der Wert, mit dem der
    Fahrplan rechnet. ``snap`` und ``winap`` sind dieselbe Summe mit dem
    verbilligten Netznutzungsentgelt; die übrigen drei Posten kennen kein
    Zeitfenster. Die Einzelposten stehen daneben, damit die Anzeige die
    Rechnung zeigen kann, statt nur das Ergebnis.
    """

    ap: float
    snap: float | None
    winap: float | None
    bereich: str          # Schlüssel aus NETZBEREICHE oder NETZBEREICH_MANUELL
    stand: str | None
    quelle: str | None
    # Einzelposten, €/kWh brutto
    netznutzung: float = 0.0
    netzverlust: float = 0.0
    elektrizitaetsabgabe: float = 0.0
    foerderbeitrag: float = 0.0


def _positiv(wert: Any) -> float | None:
    try:
        zahl = float(wert) if wert not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return zahl if zahl is not None and zahl > 0 else None


def brutto(ct_netto: float | None) -> float | None:
    """Cent/kWh netto → €/kWh brutto."""
    if ct_netto is None:
        return None
    return round(ct_netto * MWST / 100.0, 6)


def netzgebuehr_fuer(
    config: dict[str, Any], tabelle: Tariftabelle | None = None
) -> Netzgebuehr | None:
    """Die Netzgebühr laut Konfiguration — aus der Tabelle, von Hand oder keine.

    ``tabelle`` ist die zuletzt gelesene Verordnung; ohne sie (oder ohne den
    Bereich darin) gilt der Schnappschuss. Bei Handeingabe wird der SNAP
    als 20 % Abschlag gerechnet; einen WiNAP gibt es dort nicht, weil nicht
    bekannt ist, ob er schon gilt.
    """
    bereich = str(config.get(CONF_SCHEDULE_NETZBEREICH) or "").strip()
    abgaben = None
    if isinstance(tabelle, Tariftabelle) and tabelle.abgaben is not None:
        abgaben = tabelle.abgaben
    elif SNAPSHOT.abgaben is not None:
        abgaben = SNAPSHOT.abgaben
    elektrizitaet = brutto(abgaben.elektrizitaet) or 0.0 if abgaben else 0.0
    foerder = brutto(abgaben.foerderbeitrag) or 0.0 if abgaben else 0.0
    zusatz = elektrizitaet + foerder

    if bereich == NETZBEREICH_MANUELL:
        fee = _positiv(config.get(CONF_SCHEDULE_NETWORK_FEE))
        if fee is None:
            return None
        # Die Handeingabe ist das Netznutzungsentgelt (so steht es am Feld);
        # das Netzverlustentgelt hängt am Netzbereich und ist ohne ihn nicht
        # bekannt, die bundesweiten Abgaben dagegen schon.
        return Netzgebuehr(
            ap=round(fee + zusatz, 6),
            snap=round(fee * (1 - SNAP_RABATT) + zusatz, 6),
            winap=None,
            bereich=bereich, stand=None, quelle="Handeingabe",
            netznutzung=fee, netzverlust=0.0,
            elektrizitaetsabgabe=elektrizitaet, foerderbeitrag=foerder,
        )
    if bereich not in NETZBEREICH_LABELS:
        return None
    if not isinstance(tabelle, Tariftabelle) or bereich not in tabelle.tarife:
        tabelle = SNAPSHOT
    tarif = tabelle.tarife.get(bereich)
    if tarif is None:
        return None
    verlust = brutto(tarif.verlust) or 0.0
    fest = verlust + zusatz
    nutzung = brutto(tarif.ap) or 0.0
    snap = brutto(tarif.snap)
    winap = brutto(tarif.winap)
    return Netzgebuehr(
        ap=round(nutzung + fest, 6),
        snap=None if snap is None else round(snap + fest, 6),
        winap=None if winap is None else round(winap + fest, 6),
        bereich=bereich,
        stand=tabelle.stand,
        quelle=tabelle.quelle,
        netznutzung=nutzung,
        netzverlust=verlust,
        elektrizitaetsabgabe=elektrizitaet,
        foerderbeitrag=foerder,
    )


def tabelle_status(
    tabelle: Tariftabelle,
    geholt: datetime | None = None,
    fehler: str | None = None,
) -> dict[str, Any]:
    """Für das Panel: alle Bereiche mit Netto- und Bruttosätzen samt Herkunft."""
    alter_min = None
    if geholt is not None:
        alter_min = int((_utcnow() - geholt).total_seconds() / 60)
    bereiche = []
    for key, label in NETZBEREICHE:
        tarif = tabelle.tarife.get(key) or SNAPSHOT.tarife.get(key)
        if tarif is None:
            continue
        bereiche.append(
            {
                "key": key,
                "label": label,
                "ap_netto": tarif.ap,
                "snap_netto": tarif.snap,
                "winap_netto": tarif.winap,
                "verlust_netto": tarif.verlust,
                "ap_brutto": brutto(tarif.ap),
                "snap_brutto": brutto(tarif.snap),
                "winap_brutto": brutto(tarif.winap),
                "verlust_brutto": brutto(tarif.verlust),
            }
        )
    abgaben = tabelle.abgaben or (SNAPSHOT.abgaben if tabelle.abgaben is None else None)
    return {
        "stand": tabelle.stand,
        "quelle": tabelle.quelle,
        "url": tabelle.url,
        "aus_snapshot": tabelle.aus_snapshot,
        "alter_minuten": alter_min,
        "fehler": fehler,
        "mwst": MWST,
        "bereiche": bereiche,
        "verlust_quelle": tabelle.verlust_quelle,
        "elektrizitaetsabgabe_netto": abgaben.elektrizitaet if abgaben else None,
        "elektrizitaetsabgabe_quelle": abgaben.elektrizitaet_quelle if abgaben else None,
        "foerderbeitrag_netto": abgaben.foerderbeitrag if abgaben else None,
        "foerderbeitrag_quelle": abgaben.foerderbeitrag_quelle if abgaben else None,
    }


# ---------------------------------------------------------------------------
# Abruf aus dem RIS
# ---------------------------------------------------------------------------


def _dokumente_aus_antwort(antwort: dict[str, Any]) -> list[dict[str, Any]]:
    """Die Dokumentliste der RIS-Antwort in flache Einträge verwandeln."""
    try:
        res = antwort["OgdSearchResult"]["OgdDocumentResults"]
    except (KeyError, TypeError):
        return []
    docs = res.get("OgdDocumentReference") or []
    if isinstance(docs, dict):
        docs = [docs]
    eintraege = []
    for doc in docs:
        try:
            md = doc["Data"]["Metadaten"]
            kons = md["Bundesrecht"].get("BrKons", {})
            urls = doc["Data"]["Dokumentliste"]["ContentReference"]["Urls"]["ContentUrl"]
            if isinstance(urls, dict):
                urls = [urls]
            html_url = next(
                (u["Url"] for u in urls if str(u.get("DataType", "")).lower() == "html"),
                None,
            )
            eintraege.append(
                {
                    "nor": md["Technisch"]["ID"],
                    "kurztitel": md["Bundesrecht"].get("Kurztitel", ""),
                    "paragraf": kons.get("ArtikelParagraphAnlage", ""),
                    "inkrafttreten": kons.get("Inkrafttretensdatum", ""),
                    "html_url": html_url,
                }
            )
        except (KeyError, TypeError):
            continue
    return eintraege


class NetzentgeltProvider:
    """Liest die Netzebene-7-Tabelle aus dem RIS und hält sie über Neustarts."""

    def __init__(self, hass: Any, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        if Store is not None:
            self._store = Store(hass, 1, f"{DOMAIN}_{entry_id}_netzentgelt")
        else:
            self._store = None  # type: ignore[assignment]
        self._tabelle: Tariftabelle | None = None
        self._geholt: datetime | None = None
        self._fehler: str | None = None

    # -- Zustand -------------------------------------------------------

    @property
    def tabelle(self) -> Tariftabelle:
        """Die zuletzt gelesene Tabelle, sonst der eingebaute Schnappschuss."""
        return self._tabelle or SNAPSHOT

    def netzgebuehr(self, config: dict[str, Any]) -> Netzgebuehr | None:
        return netzgebuehr_fuer(config, self.tabelle)

    def status(self) -> dict[str, Any]:
        return tabelle_status(self.tabelle, self._geholt, self._fehler)

    # -- Laden und Holen -----------------------------------------------

    async def async_load(self) -> None:
        if self._store is None:
            return
        try:
            stored = await self._store.async_load()
            if stored and isinstance(stored, dict) and stored.get("tabelle"):
                tabelle = Tariftabelle.aus_dict(stored["tabelle"])
                if tabelle.tarife:
                    self._tabelle = tabelle
                geholt = stored.get("geholt")
                if geholt:
                    self._geholt = datetime.fromisoformat(geholt)
        except Exception:
            _LOGGER.debug("Netzentgelte: keine gespeicherte Tabelle vorhanden")

    async def async_fetch(self, force: bool = False) -> Tariftabelle:
        """Tabelle holen, wenn die gespeicherte älter als einen Tag ist."""
        jetzt = _utcnow()
        if (
            not force
            and self._geholt is not None
            and (jetzt - self._geholt).total_seconds() < CACHE_FRESH_SECONDS
        ):
            return self.tabelle

        heute = jetzt.date()
        for quelle in QUELLEN:
            try:
                tabelle = await self._hole_quelle(quelle, heute)
            except Exception as err:
                self._fehler = f"{quelle.name}: {err}"
                _LOGGER.debug("Netzentgelte %s: %s", quelle.name, err)
                continue
            if tabelle is None:
                continue
            tabelle = await self._ergaenze(tabelle, heute)
            self._tabelle, self._geholt, self._fehler = tabelle, jetzt, None
            _LOGGER.debug(
                "Netzentgelte: %s, Stand %s, %d Bereiche",
                tabelle.quelle, tabelle.stand, len(tabelle.tarife),
            )
            await self._speichern()
            return tabelle

        _LOGGER.warning(
            "Netzentgelte nicht aus dem RIS lesbar (%s) — es gilt weiter %s "
            "(Stand %s)",
            self._fehler or "keine Quelle gefunden",
            "die gespeicherte Tabelle" if self._tabelle else "der eingebaute Schnappschuss",
            self.tabelle.stand,
        )
        return self.tabelle

    async def _ergaenze(self, tabelle: Tariftabelle, heute: date) -> Tariftabelle:
        """Netzverlustentgelt und die beiden Abgaben dazuholen.

        Jeder Posten für sich: Scheitert einer, behält die Tabelle an dieser
        Stelle den Wert des Schnappschusses — ein fehlender Förderbeitrag
        soll nicht den Arbeitspreis mitreißen, der gerade frisch gelesen
        wurde. Was nicht kam, steht als Herkunft „eingebaut" in der Anzeige.
        """
        tarife = dict(tabelle.tarife)
        verlust_quelle = SNAPSHOT.verlust_quelle
        try:
            dok, html = await self._hole_dokument(QUELLE_NETZVERLUST, heute)
            verluste = parse_verlust_tabelle(html)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Netzverlustentgelt nicht lesbar: %s", err)
            verluste = {k: t.verlust for k, t in SNAPSHOT.tarife.items()}
        else:
            verlust_quelle = f"SNE-V 2018 § 6 (RIS {dok['nor']})"
        for key, wert in verluste.items():
            if key in tarife and wert is not None:
                tarife[key] = replace(tarife[key], verlust=wert)

        abgaben = SNAPSHOT.abgaben or Abgaben()
        elektrizitaet = (abgaben.elektrizitaet, abgaben.elektrizitaet_quelle)
        foerder = (abgaben.foerderbeitrag, abgaben.foerderbeitrag_quelle)
        try:
            dok4, html4 = await self._hole_dokument(QUELLE_ELEKTRIZITAETSABGABE, heute)
            try:
                _, html7 = await self._hole_dokument(
                    QUELLE_ELEKTRIZITAETSABGABE_UEBERGANG, heute
                )
            except Exception:  # noqa: BLE001 — ohne Übergang gilt der Regelsatz
                html7 = None
            gelesen = parse_elektrizitaetsabgabe(html4, html7, heute)
            if gelesen is not None:
                elektrizitaet = (gelesen[0], f"{gelesen[1]} (RIS {dok4['nor']})")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Elektrizitätsabgabe nicht lesbar: %s", err)
        try:
            dokf, htmlf = await self._hole_dokument(QUELLE_FOERDERBEITRAG, heute)
            gelesen = parse_foerderbeitrag(htmlf)
            if gelesen is not None:
                foerder = (gelesen[0], f"{gelesen[1]} (RIS {dokf['nor']})")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Erneuerbaren-Förderbeitrag nicht lesbar: %s", err)

        return replace(
            tabelle,
            tarife=tarife,
            verlust_quelle=verlust_quelle,
            abgaben=Abgaben(
                elektrizitaet=elektrizitaet[0],
                foerderbeitrag=foerder[0],
                elektrizitaet_quelle=elektrizitaet[1],
                foerderbeitrag_quelle=foerder[1],
            ),
        )

    async def _hole_dokument(
        self, quelle: Quelle, heute: date
    ) -> tuple[dict[str, Any], str]:
        """Das erste passende RIS-Dokument einer Quelle samt HTML."""
        for dok in await self._suche_dokumente(quelle, heute):
            return dok, await self._get_text(dok["html_url"])
        raise ValueError(f"{quelle.name}: kein Dokument für {quelle.paragraf}")

    async def _suche_dokumente(
        self, quelle: Quelle, heute: date
    ) -> list[dict[str, Any]]:
        """Dokumentliste einer Quelle aus dem RIS, gefiltert auf den Paragraphen."""
        params = {
            "Applikation": "BrKons",
            "Fassung.FassungVom": heute.isoformat(),
            "DokumenteProSeite": "OneHundred",
        }
        if quelle.gesetzesnummer:
            params["Gesetzesnummer"] = quelle.gesetzesnummer
        if quelle.titel:
            params["Titel"] = quelle.titel
        if quelle.paragraf is None:
            params["Suchworte"] = "Netzebene 7"

        antwort = json.loads(await self._get_text(RIS_API_URL, params))
        dokumente = [
            d for d in _dokumente_aus_antwort(antwort)
            if "gas" not in d["kurztitel"].lower() and d["html_url"]
        ]
        if quelle.paragraf:
            dokumente = [
                d for d in dokumente
                if d["paragraf"].replace(" ", "") == quelle.paragraf.replace(" ", "")
            ]
        return dokumente[:MAX_DOKUMENTE_JE_QUELLE]

    async def _hole_quelle(self, quelle: Quelle, heute: date) -> Tariftabelle | None:
        """Eine Verordnung im RIS aufsuchen und ihre Tabelle lesen.

        None, wenn das RIS für den Stichtag nichts liefert (Verordnung noch
        nicht oder nicht mehr in Kraft); ValueError, wenn Dokumente da sind,
        aber keine lesbare Tabelle enthalten.
        """
        dokumente = await self._suche_dokumente(quelle, heute)
        if not dokumente:
            return None

        letzter_fehler: Exception | None = None
        for dok in dokumente:
            html = await self._get_text(dok["html_url"])
            try:
                tarife = parse_tabelle(html)
            except ValueError as err:
                letzter_fehler = err
                continue
            para = dok["paragraf"] or "?"
            return Tariftabelle(
                stand=str(dok["inkrafttreten"] or heute.isoformat())[:10],
                quelle=f"{quelle.name} {para} (RIS {dok['nor']})",
                url=dok["html_url"],
                tarife=tarife,
            )
        raise ValueError(str(letzter_fehler or "keine lesbare Tabelle"))

    async def _get_text(self, url: str, params: dict[str, str] | None = None) -> str:
        """HTTP GET als Text — in Tests überschreibbar."""
        if async_get_clientsession is None:
            raise RuntimeError("kein HTTP-Client (Testumgebung)")
        import aiohttp

        session = async_get_clientsession(self._hass)
        async with session.get(
            url,
            params=params,
            headers={"User-Agent": RIS_USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return await resp.text()

    async def _speichern(self) -> None:
        if self._store is None or self._tabelle is None:
            return
        try:
            await self._store.async_save(
                {
                    "tabelle": self._tabelle.als_dict(),
                    "geholt": self._geholt.isoformat() if self._geholt else None,
                }
            )
        except Exception:
            _LOGGER.debug("Netzentgelte: Speichern fehlgeschlagen", exc_info=True)
