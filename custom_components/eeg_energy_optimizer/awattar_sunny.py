"""Monatlicher Einspeisetarif aWATTar SUNNY.

SUNNY ist ein fester Netto-Preis je Monat (ohne Umsatzsteuer), den aWATTar
spätestens am 1. des Monats veröffentlicht. Er entsteht aus dem Mittel des
EEX-„Base Month Future" der ersten zehn Handelstage des Vormonats,
multipliziert mit dem Standardlastprofilfaktor E1 (PV-Einspeisung) des
Vormonats, abzüglich 9 % Vermarktungskomponente. Eine Schnittstelle gibt es
dafür nicht — die Marktdaten-API von aWATTar kennt nur die Börsenpreise
(siehe ``spot.py``). Gelesen werden deshalb zwei Stellen, in dieser Reihenfolge:

* **Die Preistabelle „Berechnungsmethodik & Preise"** — ein Google Sheet, das
  aWATTar auf der Tarifseite verlinkt, als CSV je Jahres-Tab. Eine Zeile je
  Monat: Monat, Jahr, MONTHLY, dann eine oder zwei SUNNY-Spalten. Seit dem
  25.02.2026 sind es zwei: eine für Verträge, die bis dahin abgeschlossen
  wurden („alt"), und eine für alle späteren („neu"). Die Werte liegen in
  manchen Monaten Cent auseinander (März 2026: 8,602 gegen 5,415 ct/kWh) —
  welche gilt, sagt die Einstellung ``awattar_sunny_vertrag``. Gibt es nur
  eine SUNNY-Spalte, gilt sie für beide.
* **Die Tarifseite** ``awattar.at/tariffs/sunny`` als Rückfall. Sie zeigt nur
  den aktuellen Wert des aktuellen Vertrags („8,989 Cent/kWh netto") und
  greift, wenn die Tabelle nicht lesbar ist oder den laufenden Monat noch
  nicht führt.

Wie bei der OeMAG gilt: jeder Fehler lässt den zuletzt gelesenen Wert
stehen, fehlt der laufende Monat, gilt der jüngste davor, und ohne jeden Wert
greift die Handeingabe — der Ausfall einer Website darf den Fahrplan nicht
anhalten. Der Wert kommt immer mit Monat, Herkunft und Alter, damit ein
stehender Tarif im Panel auffällt.
"""

from __future__ import annotations

import csv
import html as html_entities
import io
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


# Die Tabelle, die aWATTar auf der Tarifseite verlinkt („Berechnungsmethodik &
# Preise"). Der gviz-Export liefert einen Tab als CSV, der Tab heißt wie das
# Jahr. Frei lesbar, ohne Schlüssel.
SHEET_ID = "1emzAMIOEhKsddDUaeJkaEbIZTZ2fGLYKAfxGJhuvZrw"
SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/" + SHEET_ID
    + "/gviz/tq?tqx=out:csv&sheet={jahr}"
)
TARIF_URL = "https://www.awattar.at/tariffs/sunny"
USER_AGENT = "HomeAssistant/EEGEnergyOptimizer"

# Der Tarif wechselt monatlich; zweimal täglich nachsehen genügt. Fehlt der
# laufende Monat noch (Monatsanfang), wird stündlich nachgesehen — der Wert
# erscheint irgendwann am 1., und bis dahin rechnet der Fahrplan mit dem
# Vormonat.
CACHE_FRESH_SECONDS = 12 * 3600
CACHE_RETRY_SECONDS = 3600
# So lange gilt ein gespeicherter Wert weiter, wenn keine Quelle antwortet.
# Großzügig, weil der Wert einen ganzen Monat gilt.
CACHE_MAX_SECONDS = 40 * 24 * 3600
# Tarife älter als das Vorjahr fliegen aus dem Speicher — sie helfen keinem
# Rückfall mehr.
AUFBEWAHRUNG_JAHRE = 1

VERTRAG_NEU = "neu"   # Vertragsabschluss nach dem Stichtag — die letzte SUNNY-Spalte
VERTRAG_ALT = "alt"   # Vertragsabschluss bis zum Stichtag („… bis 25.02.2026")
VERTRAEGE = (VERTRAG_NEU, VERTRAG_ALT)

QUELLE_TABELLE = "tabelle"
QUELLE_TARIFSEITE = "tarifseite"


def monatsschluessel(jahr: int, monat: int) -> int:
    """(2026, 9) → 202609 — sortierbar über Jahresgrenzen hinweg."""
    return jahr * 100 + monat


def jahr_monat(schluessel: int) -> tuple[int, int]:
    return schluessel // 100, schluessel % 100


@dataclass
class Tabelle:
    """Ein gelesener Jahres-Tab: Tarife je Vertragsvariante in €/kWh."""

    neu: dict[int, float] = field(default_factory=dict)
    alt: dict[int, float] = field(default_factory=dict)
    # Stichtag aus der Spaltenüberschrift („… bis 25.02.2026"), fürs Panel.
    alt_bis: str | None = None
    # Wurde überhaupt eine SUNNY-Spalte gefunden? Ein Tab mit Kopf, aber ohne
    # Werte (Jahresanfang) ist lesbar und nur leer — eine Anmeldeseite oder
    # ein umgebautes Blatt dagegen nicht.
    kopf_erkannt: bool = False

    def leer(self) -> bool:
        return not self.neu and not self.alt

    def fuer(self, vertrag: str) -> dict[int, float]:
        return self.alt if vertrag == VERTRAG_ALT else self.neu


def parse_tabelle(text: str | None, jahr: int) -> Tabelle:
    """CSV eines Jahres-Tabs → Tarife je Monat und Vertragsvariante.

    Die Kopfzeile trägt Zeilenumbrüche in den Zellen („SUNNY\\nEinspeise-
    vergütung …"), deshalb ein echter CSV-Leser statt Zeilentrennung. Die
    SUNNY-Spalten werden über die Überschrift gefunden: die letzte ist der
    aktuelle Vertrag, eine davor mit „bis <Datum>" der Altvertrag. Zeilen
    anderer Jahre und Monate ohne Wert fallen heraus, die Kopfzeile von selbst.
    """
    tabelle = Tabelle()
    if not text:
        return tabelle
    try:
        zeilen = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        return tabelle
    if not zeilen:
        return tabelle

    kopf = [re.sub(r"\s+", " ", (z or "")).strip().lower() for z in zeilen[0]]
    sunny_spalten = [i for i, k in enumerate(kopf) if "sunny" in k]
    if not sunny_spalten:
        return tabelle
    tabelle.kopf_erkannt = True
    neu_idx = sunny_spalten[-1]
    alt_idx = None
    for i in sunny_spalten[:-1]:
        if "bis" in kopf[i]:
            alt_idx = i
    if alt_idx is not None:
        treffer = re.search(r"bis\s+(\d{1,2}\.\d{1,2}\.\d{2,4})", kopf[alt_idx])
        tabelle.alt_bis = treffer.group(1) if treffer else None

    for zeile in zeilen[1:]:
        if not zeile:
            continue
        monat = _ganzzahl(zeile[0])
        if monat is None or not 1 <= monat <= 12:
            continue
        # Die Jahresspalte ist eine Sicherung gegen einen falsch benannten
        # Tab — steht dort ein anderes Jahr, gehört die Zeile nicht hierher.
        if len(zeile) > 1:
            zeilen_jahr = _ganzzahl(zeile[1])
            if zeilen_jahr is not None and zeilen_jahr != jahr:
                continue
        schluessel = monatsschluessel(jahr, monat)
        neu = _ct_wert(zeile[neu_idx]) if neu_idx < len(zeile) else None
        if neu is not None:
            tabelle.neu[schluessel] = neu
        if alt_idx is not None and alt_idx < len(zeile):
            alt = _ct_wert(zeile[alt_idx])
            if alt is not None:
                tabelle.alt[schluessel] = alt

    if alt_idx is None:
        # Nur eine SUNNY-Spalte: sie gilt für alle Verträge.
        tabelle.alt = dict(tabelle.neu)
    return tabelle


def parse_tarifseite(html: str | None) -> float | None:
    """Aktueller Netto-Tarif von der Tarifseite in €/kWh, oder None.

    Die Seite zeigt in der Tarifübersicht „8,989 Cent/kWh netto" neben dem
    Brutto-Wert (bei SUNNY identisch, es fällt keine Umsatzsteuer an). Gelesen
    wird bewusst nur der Wert, der ausdrücklich als netto bezeichnet ist.
    """
    if not html:
        return None
    text = html_entities.unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"\s+", " ", text)
    treffer = re.search(r"(\d+(?:[.,]\d+)?)\s*Cent/kWh\s*netto", text, re.I)
    if not treffer:
        return None
    try:
        return round(float(treffer.group(1).replace(",", ".")) / 100.0, 6)
    except ValueError:
        return None


def tarif_fuer(tarife: dict[int, float], schluessel: int) -> tuple[float, int] | None:
    """Tarif des Monats, sonst der jüngste davor bekannte.

    Rückgabe: (€/kWh, Monatsschlüssel). Am Monatsanfang fehlt der laufende
    Monat oft noch — bis dahin ist der Vormonat der beste bekannte Wert. Sind
    nur spätere Monate bekannt, gilt der jüngste davon.
    """
    if not tarife:
        return None
    if schluessel in tarife:
        return tarife[schluessel], schluessel
    frueher = [s for s in tarife if s < schluessel]
    if frueher:
        letzter = max(frueher)
        return tarife[letzter], letzter
    letzter = max(tarife)
    return tarife[letzter], letzter


class AwattarSunnyProvider:
    """Holt den Monatstarif und hält ihn über Neustarts hinweg.

    Die Vertragsvariante ist keine Eigenschaft des Anbieters, sondern der
    Abfrage (``preis_fuer``): so kann ein Wechsel in den Einstellungen sofort
    wirken, und das Panel zeigt beide Werte nebeneinander.
    """

    def __init__(self, hass: Any, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        if Store is not None:
            self._store = Store(hass, 1, f"{DOMAIN}_{entry_id}_awattar_sunny")
        else:
            self._store = None  # type: ignore[assignment]
        self._tarife: dict[str, dict[int, float]] = {VERTRAG_NEU: {}, VERTRAG_ALT: {}}
        self._alt_bis: str | None = None
        self._geholt: datetime | None = None
        self._fehler: str | None = None
        self._quelle: str | None = None

    # -- Zustand -------------------------------------------------------

    def hat_daten(self) -> bool:
        return any(self._tarife.values())

    def _gueltig(self) -> bool:
        if self._geholt is None:
            return False
        return (_utcnow() - self._geholt).total_seconds() <= CACHE_MAX_SECONDS

    def _treffer(self, vertrag: str) -> tuple[float, int] | None:
        vertrag = vertrag if vertrag in VERTRAEGE else VERTRAG_NEU
        return tarif_fuer(self._tarife.get(vertrag) or {}, aktueller_schluessel())

    def preis_fuer(self, vertrag: str) -> float | None:
        """Tarif der Vertragsvariante für den laufenden Monat in €/kWh.

        Fehlt der laufende Monat, der jüngste davor; ohne Daten oder wenn der
        letzte Abruf länger als CACHE_MAX_SECONDS zurückliegt: None.
        """
        if not self._gueltig():
            return None
        treffer = self._treffer(vertrag)
        return treffer[0] if treffer else None

    def status(self) -> dict[str, Any]:
        """Für die Anzeige: Wert und Monat je Variante, Herkunft, Alter, Fehler."""
        alter_min = None
        if self._geholt is not None:
            alter_min = int((_utcnow() - self._geholt).total_seconds() / 60)
        ergebnis: dict[str, Any] = {
            "alt_bis": self._alt_bis,
            "alter_minuten": alter_min,
            "fehler": self._fehler,
            "quelle": self._quelle,
            "quelle_url": TARIF_URL if self._quelle == QUELLE_TARIFSEITE
            else SHEET_URL.format(jahr=aktueller_schluessel() // 100),
        }
        for vertrag in VERTRAEGE:
            treffer = self._treffer(vertrag) if self._gueltig() else None
            if treffer is None:
                ergebnis[vertrag] = None
                continue
            preis, schluessel = treffer
            jahr, monat = jahr_monat(schluessel)
            ergebnis[vertrag] = {"preis": preis, "jahr": jahr, "monat": monat}
        return ergebnis

    def _laufender_monat_bekannt(self) -> bool:
        jetzt = aktueller_schluessel()
        return jetzt in self._tarife[VERTRAG_NEU] or jetzt in self._tarife[VERTRAG_ALT]

    # -- Laden und Holen -----------------------------------------------

    async def async_load(self) -> None:
        if self._store is None:
            return
        try:
            stored = await self._store.async_load()
            if stored and isinstance(stored, dict):
                roh = stored.get("tarife") or {}
                for vertrag in VERTRAEGE:
                    self._tarife[vertrag] = {
                        int(k): float(v) for k, v in (roh.get(vertrag) or {}).items()
                    }
                self._alt_bis = stored.get("alt_bis")
                self._quelle = stored.get("quelle")
                geholt = stored.get("geholt")
                if geholt:
                    self._geholt = datetime.fromisoformat(geholt)
        except Exception:
            _LOGGER.debug("aWATTar SUNNY: kein gespeicherter Tarif vorhanden")

    async def _hole_text(self, session: Any, url: str) -> str:
        import aiohttp

        async with session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return await resp.text()

    async def _hole_tabelle(self, session: Any, jahr: int) -> Tabelle | None:
        """Jahres-Tab lesen; None bei Netz- oder Formatfehler."""
        try:
            text = await self._hole_text(session, SHEET_URL.format(jahr=jahr))
        except Exception as err:  # noqa: BLE001 - jeder Fehler heißt: Rückfall
            self._fehler = f"Preistabelle {jahr}: {err}"
            return None
        tabelle = parse_tabelle(text, jahr)
        if not tabelle.kopf_erkannt:
            self._fehler = f"Preistabelle {jahr}: keine SUNNY-Spalte erkannt"
            return None
        return tabelle

    async def async_fetch(self, force: bool = False) -> None:
        """Tarif holen, wenn der gespeicherte Wert alt genug ist.

        Zuerst die Preistabelle (laufendes Jahr, am Jahresanfang zusätzlich
        das Vorjahr für den Rückfall auf den Dezember), dann — falls der
        laufende Monat dort fehlt — die Tarifseite für den aktuellen Vertrag.
        Neue Werte legen sich über die alten; was keine Quelle liefert, bleibt
        wie gespeichert.
        """
        jetzt = _utcnow()
        if not force and self._geholt is not None:
            frist = (
                CACHE_FRESH_SECONDS if self._laufender_monat_bekannt()
                else CACHE_RETRY_SECONDS
            )
            if (jetzt - self._geholt).total_seconds() < frist:
                return
        if async_get_clientsession is None:
            return

        session = async_get_clientsession(self._hass)
        jahr, monat = jahr_monat(aktueller_schluessel())
        self._fehler = None
        gelesen = False

        tabelle = await self._hole_tabelle(session, jahr)
        if tabelle is not None:
            self._uebernehmen(tabelle)
            gelesen = True
            if not tabelle.leer():
                self._quelle = QUELLE_TABELLE
        # Jahresanfang: der neue Tab ist oft noch leer, der Dezember steht im
        # alten. Auch nach Erfolg lesen, wenn dieses Jahr noch nichts trägt.
        if not any(
            s // 100 == jahr for s in self._tarife[VERTRAG_NEU]
        ) and monat <= 2:
            vorjahr = await self._hole_tabelle(session, jahr - 1)
            if vorjahr is not None and not vorjahr.leer():
                self._uebernehmen(vorjahr)
                gelesen = True
                self._quelle = QUELLE_TABELLE

        schluessel = monatsschluessel(jahr, monat)
        if schluessel not in self._tarife[VERTRAG_NEU]:
            # Die Tarifseite zeigt „aktuell" — den laufenden Monat des
            # aktuellen Vertrags. Zeigt sie noch den Vormonat, ist der Wert
            # derselbe, den der Rückfall ohnehin nähme.
            try:
                seite = await self._hole_text(session, TARIF_URL)
                preis = parse_tarifseite(seite)
            except Exception as err:  # noqa: BLE001
                preis = None
                self._fehler = (self._fehler + "; " if self._fehler else "") + f"Tarifseite: {err}"
            if preis is not None:
                self._tarife[VERTRAG_NEU][schluessel] = preis
                gelesen = True
                self._quelle = QUELLE_TARIFSEITE
                if self._fehler:
                    _LOGGER.debug("aWATTar SUNNY: %s — Wert von der Tarifseite", self._fehler)
                self._fehler = None
            elif self._fehler is None:
                self._fehler = "Tarifseite: kein Netto-Tarif erkannt"

        if not gelesen:
            _LOGGER.warning(
                "aWATTar-SUNNY-Tarif nicht abrufbar (%s) — es gilt weiter %s",
                self._fehler,
                "der zuletzt gelesene Wert" if self.hat_daten() else "die Handeingabe",
            )
            return

        self._geholt = jetzt
        grenze = monatsschluessel(jahr - AUFBEWAHRUNG_JAHRE, 1)
        for vertrag in VERTRAEGE:
            self._tarife[vertrag] = {
                s: p for s, p in self._tarife[vertrag].items() if s >= grenze
            }
        treffer = self._treffer(VERTRAG_NEU)
        if treffer:
            _LOGGER.debug(
                "aWATTar SUNNY: %.5f €/kWh (Monat %d, %s)", treffer[0], treffer[1], self._quelle
            )

        if self._store is not None:
            try:
                await self._store.async_save(
                    {
                        "tarife": {
                            v: {str(k): p for k, p in t.items()}
                            for v, t in self._tarife.items()
                        },
                        "alt_bis": self._alt_bis,
                        "quelle": self._quelle,
                        "geholt": jetzt.isoformat(),
                    }
                )
            except Exception:
                _LOGGER.debug("aWATTar-SUNNY-Tarif konnte nicht gespeichert werden")

    def _uebernehmen(self, tabelle: Tabelle) -> None:
        self._tarife[VERTRAG_NEU].update(tabelle.neu)
        self._tarife[VERTRAG_ALT].update(tabelle.alt)
        if tabelle.alt_bis:
            self._alt_bis = tabelle.alt_bis


# ---------------------------------------------------------------------------
# Kleinkram
# ---------------------------------------------------------------------------


def aktueller_schluessel() -> int:
    """Laufender Monat in Ortszeit als Schlüssel (JJJJMM) — als eigene
    Funktion, damit Tests sie ersetzen können."""
    jetzt = datetime.now()
    return monatsschluessel(jetzt.year, jetzt.month)


def _ganzzahl(text: str | None) -> int | None:
    """„1", „1.0", „ 2026 " → int; alles andere None."""
    if text is None:
        return None
    t = str(text).strip().replace(",", ".")
    if not t:
        return None
    try:
        wert = float(t)
    except ValueError:
        return None
    if wert != int(wert):
        return None
    return int(wert)


def _ct_wert(text: str | None) -> float | None:
    """„10.969" oder „10,969" (Cent/kWh) → 0,10969 €/kWh; leer → None."""
    if text is None:
        return None
    t = str(text).strip()
    if not t:
        return None
    if "," in t and "." not in t:
        t = t.replace(",", ".")
    try:
        return round(float(t) / 100.0, 6)
    except ValueError:
        return None
