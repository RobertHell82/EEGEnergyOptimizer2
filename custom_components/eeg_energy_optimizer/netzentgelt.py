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
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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
    """Netznutzungsentgelt eines Bereichs auf Netzebene 7, Cent/kWh netto."""

    ap: float
    snap: float | None = None
    winap: float | None = None


@dataclass(frozen=True)
class Tariftabelle:
    """Die gelesene Netzebene-7-Tabelle samt Herkunft."""

    stand: str                     # Inkrafttreten der Fassung, ISO-Datum
    quelle: str                    # z. B. „SNE-V 2018 § 5 (RIS NOR40273644)"
    url: str | None
    tarife: dict[str, Netztarif] = field(default_factory=dict)
    aus_snapshot: bool = False

    def als_dict(self) -> dict[str, Any]:
        return {
            "stand": self.stand,
            "quelle": self.quelle,
            "url": self.url,
            "aus_snapshot": self.aus_snapshot,
            "tarife": {
                k: {"ap": t.ap, "snap": t.snap, "winap": t.winap}
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
                )
        return cls(
            stand=str(roh.get("stand") or ""),
            quelle=str(roh.get("quelle") or ""),
            url=roh.get("url"),
            tarife=tarife,
            aus_snapshot=bool(roh.get("aus_snapshot", False)),
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
        "burgenland": Netztarif(8.46, 6.77),
        "kaernten": Netztarif(9.67, 7.74),
        "klagenfurt": Netztarif(6.90, 5.52),
        "niederoesterreich": Netztarif(8.79, 7.03),
        "oberoesterreich": Netztarif(6.29, 5.03),
        "linz": Netztarif(5.57, 4.46),
        "salzburg": Netztarif(6.59, 5.27),
        "steiermark": Netztarif(8.82, 7.06),
        "graz": Netztarif(5.17, 4.14),
        "tirol": Netztarif(6.81, 5.45),
        "innsbruck": Netztarif(8.03, 6.42),
        "vorarlberg": Netztarif(4.96, 3.97),
        "wien": Netztarif(6.98, 5.58),
        "kleinwalsertal": Netztarif(17.73, 14.18),
    },
    aus_snapshot=True,
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


@dataclass(frozen=True)
class Netzgebuehr:
    """Netzgebühr, mit der der Fahrplan rechnet — €/kWh brutto."""

    ap: float
    snap: float | None
    winap: float | None
    bereich: str          # Schlüssel aus NETZBEREICHE oder NETZBEREICH_MANUELL
    stand: str | None
    quelle: str | None


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
    if bereich == NETZBEREICH_MANUELL:
        fee = _positiv(config.get(CONF_SCHEDULE_NETWORK_FEE))
        if fee is None:
            return None
        return Netzgebuehr(
            ap=fee, snap=round(fee * (1 - SNAP_RABATT), 6), winap=None,
            bereich=bereich, stand=None, quelle="Handeingabe",
        )
    if bereich not in NETZBEREICH_LABELS:
        return None
    if not isinstance(tabelle, Tariftabelle) or bereich not in tabelle.tarife:
        tabelle = SNAPSHOT
    tarif = tabelle.tarife.get(bereich)
    if tarif is None:
        return None
    return Netzgebuehr(
        ap=brutto(tarif.ap) or 0.0,
        snap=brutto(tarif.snap),
        winap=brutto(tarif.winap),
        bereich=bereich,
        stand=tabelle.stand,
        quelle=tabelle.quelle,
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
                "ap_brutto": brutto(tarif.ap),
                "snap_brutto": brutto(tarif.snap),
                "winap_brutto": brutto(tarif.winap),
            }
        )
    return {
        "stand": tabelle.stand,
        "quelle": tabelle.quelle,
        "url": tabelle.url,
        "aus_snapshot": tabelle.aus_snapshot,
        "alter_minuten": alter_min,
        "fehler": fehler,
        "mwst": MWST,
        "bereiche": bereiche,
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

    async def _hole_quelle(self, quelle: Quelle, heute: date) -> Tariftabelle | None:
        """Eine Verordnung im RIS aufsuchen und ihre Tabelle lesen.

        None, wenn das RIS für den Stichtag nichts liefert (Verordnung noch
        nicht oder nicht mehr in Kraft); ValueError, wenn Dokumente da sind,
        aber keine lesbare Tabelle enthalten.
        """
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
        if not dokumente:
            return None

        letzter_fehler: Exception | None = None
        for dok in dokumente[:MAX_DOKUMENTE_JE_QUELLE]:
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
