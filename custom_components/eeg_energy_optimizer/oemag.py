"""Monatlicher Einspeisetarif der OeMAG.

Es gibt keine Schnittstelle: die Werte stehen als HTML-Tabelle auf
https://www.oem-ag.at/marktpreis — eine Zeile je Monat, die erste Preisspalte
ist der Satz für Photovoltaik (die zweite gilt für Windkraft). Genau das wird
hier gelesen, und bewusst schmal:

* Nur die erste Tabelle der Seite, nur Monatsname und erste Preisspalte.
* Der laufende Monat ist oft noch nicht veröffentlicht — am 25.08.2026 endete
  die Tabelle bei Juli. Dann gilt der jüngste vorhandene Monat, denn ein
  veralteter echter Tarif ist besser als kein Tarif.
* Jeder Fehler lässt den letzten erfolgreich gelesenen Wert stehen. Ohne
  gespeicherten Wert greift der händisch eingetragene Tarif. Der Ausfall einer
  Website darf den Fahrplan nicht anhalten.

Weil das HTML-Lesen bricht, sobald die Seite umgebaut wird, ist der Wert immer
mit Herkunft und Alter versehen: das Panel zeigt beides, damit ein stehender
Tarif auffällt.

Zusätzlich liest ``parse_seite`` aus derselben Tabelle die Berechnungsbasis je
Monat (Kommentarspalte: Obergrenze, Untergrenze oder echter Day-Ahead-Wert)
und aus dem Seitentext den Aufwand für Ausgleichsenergie. Beides braucht die
Hochrechnung des laufenden Monats (``oemag_schaetzung.py``): ein Monat, der auf
Ober- oder Untergrenze lag, verrät den Quartalsmarktpreis der E-Control —
``anker_aus_tabelle`` rechnet ihn zurück, falls die E-Control-Seite selbst
nicht lesbar ist.
"""

from __future__ import annotations

import html as html_entities
import json
import logging
import math
import re
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


OEMAG_URL = "https://www.oem-ag.at/marktpreis"
OEMAG_USER_AGENT = "HomeAssistant/EEGEnergyOptimizer"

# Der Tarif wechselt monatlich; zweimal täglich nachsehen genügt und ist der
# Website gegenüber sparsam.
CACHE_FRESH_SECONDS = 12 * 3600
# So lange gilt ein gespeicherter Wert weiter, wenn die Seite nicht antwortet.
# Großzügig, weil der Wert einen ganzen Monat gilt: lieber ein drei Wochen
# alter echter Tarif als ein Rückfall auf die Handeingabe.
CACHE_MAX_SECONDS = 40 * 24 * 3600

# Aufwand Ausgleichsenergie Photovoltaik 2026 in €/kWh — Rückfall, wenn der
# Seitentext den Wert nicht preisgibt. Die OeMAG legt ihn jährlich neu fest.
AUSGLEICHSENERGIE_PV_DEFAULT = 0.00408
# Untergrenze des Korridors nach § 41 Abs. 2a ÖSG: 60 % des Quartalspreises.
KORRIDOR_UNTEN = 0.6

# Plausibilitätsband für MONATSWERTE (Tarif, Referenzmarktwert, Rohwert der
# Hochrechnung) in €/kWh — gilt für OeMAG, Hochrechnung, SUNNY und Energie
# AG. Es soll Lesefehler fangen, nicht den Markt beurteilen: In der
# Energiekrise 2022 lagen Monatswerte zeitweise über 40 ct, darüber ist Luft.
# Ein verlorenes Komma („6146" statt „6,146") oder €/MWh als ct gelesen
# landet dagegen weit außerhalb. Unten −5 ct statt 0: Ein negatives
# PV-gewichtetes Monatsmittel gab es noch nie, unmöglich ist es nicht —
# ausgeschlossen werden soll nur Unsinn.
MONATSWERT_MIN_EUR = -0.05
MONATSWERT_MAX_EUR = 0.60
# Aufwand Ausgleichsenergie: 2026 sind es 0,408 ct. Über 2 ct wäre er fünfmal
# so hoch wie je — dann ist eher die Zahl im Fließtext falsch gelesen.
AUSGLEICHSENERGIE_MAX_EUR = 0.02

# Höchstgröße einer Antwort von außen. Die größte echte (Energy-Charts über
# einen Monat, rund 0,3 MB) bleibt weit darunter; mehr heißt, am anderen Ende
# stimmt etwas nicht, und ohne Grenze läge alles im Speicher, bevor der
# Parser überhaupt sieht, dass es Unsinn ist.
MAX_ANTWORT_BYTES = 5 * 1024 * 1024

# Berechnungsbasis eines Monats, aus der Kommentarspalte der Tabelle.
BASIS_DECKEL = "deckel"        # „Marktpreis gem. § 41 Abs. 1 ÖSG abzügl. …"
BASIS_BODEN = "boden"          # „60% des Marktpreises gemäß § 41 Abs. 1 ÖSG …"
BASIS_DAY_AHEAD = "day_ahead"  # „durchschnittlich mengengewichteter Day-Ahead-…"

# Österreichische und deutsche Schreibweise, beide kommen vor.
MONATE = {
    "jänner": 1, "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4,
    "mai": 5, "juni": 6, "juli": 7, "august": 8, "september": 9,
    "oktober": 10, "november": 11, "dezember": 12,
}


def parse_seite(html: str) -> dict[str, Any]:
    """Alles, was die OeMAG-Seite hergibt: Tarife, Berechnungsbasis, Ausgleichsenergie.

    Rückgabe ``{"tarife": {Monat: €/kWh}, "basis": {Monat: BASIS_*|None},
    "ausgleichsenergie": €/kWh|None}``.

    Gelesen wird die erste Tabelle: Spalte 1 der Monatsname, Spalte 2 der Satz
    für Photovoltaik (``6,146 ct/kWh``), die letzte Spalte der Kommentar, der
    sagt, ob der Monat auf der Ober- oder Untergrenze des Korridors lag.
    Zeilen ohne Monat und Preis fallen heraus, die Kopfzeile also von selbst.
    Die Ausgleichsenergie steht im Fließtext („… für Photovoltaik und andere
    Energieträger 0,408 ct/kWh").
    """
    tabellen = bloecke(html or "", _TABLE_AUF, _TABLE_ZU)
    tarife: dict[int, float] = {}
    basis: dict[int, str | None] = {}
    zeilen = bloecke(tabellen[0], _TR_AUF, _TR_ZU) if tabellen else []
    for zeile in zeilen:
        # Entities dekodieren, nicht nur &nbsp; ersetzen: die Seite schreibt
        # Umlaute teils als M&auml;rz, teils direkt in UTF-8.
        zellen = [
            html_entities.unescape(ohne_tags(z)).replace("\xa0", " ").strip()
            for z in bloecke(zeile, _TD_AUF, _TD_ZU)
        ]
        if len(zellen) < 2:
            continue
        monat = MONATE.get(zellen[0].strip().lower())
        if monat is None:
            continue
        preis = _ct_pro_kwh(zellen[1])
        if preis is not None:
            tarife[monat] = preis
            basis[monat] = _basis_aus_kommentar(zellen[-1]) if len(zellen) > 2 else None

    ausgleichsenergie = None
    text = re.sub(r"\s+", " ", html_entities.unescape(ohne_tags(html or "")))
    treffer = re.search(
        r"Photovoltaik und andere Energietr\S+ (\d+[.,]\d+) ct/kWh", text, re.I
    )
    if treffer:
        ausgleichsenergie = _ct_pro_kwh(treffer.group(1) + " ct/kWh")
        if ausgleichsenergie is not None and not im_band(
            ausgleichsenergie, 0.0, AUSGLEICHSENERGIE_MAX_EUR
        ):
            # Dann gilt der Vorgabewert — wie wenn der Satz nicht dastünde.
            ausgleichsenergie = None
    return {"tarife": tarife, "basis": basis, "ausgleichsenergie": ausgleichsenergie}


def parse_tarife(html: str) -> dict[int, float]:
    """Monat → Einspeisetarif in €/kWh aus dem HTML der OeMAG-Seite."""
    return parse_seite(html)["tarife"]


def _basis_aus_kommentar(text: str) -> str | None:
    """Kommentarspalte → BASIS_*: „60%" ist der Boden, „Day-Ahead" der echte
    Monatswert, „§ 41 Abs. 1" ohne Prozentangabe der Deckel."""
    t = (text or "").lower()
    if "60" in t and "%" in t:
        return BASIS_BODEN
    if "day-ahead" in t or "mengengewicht" in t:
        return BASIS_DAY_AHEAD
    if "41" in t and "abs. 1" in t:
        return BASIS_DECKEL
    return None


def anker_aus_tabelle(
    tarife: dict[int, float],
    basis: dict[int, str | None],
    ausgleichsenergie: float,
    quartal: int,
) -> float | None:
    """Quartalsmarktpreis Q (€/kWh) aus einem Monat des Quartals zurückrechnen.

    Ein Monat auf der Untergrenze zeigt ``0,6·Q − AE``, einer auf der
    Obergrenze ``Q − AE``. Ein Day-Ahead-Monat sagt über Q nichts. Genommen
    wird der späteste Monat des Quartals, der etwas verrät — im Zweifel
    stimmen alle überein, es ist dieselbe Zahl.
    """
    for monat in range(quartal * 3, quartal * 3 - 3, -1):
        art = basis.get(monat)
        preis = tarife.get(monat)
        if preis is None or art is None:
            continue
        if art == BASIS_BODEN:
            return round((preis + ausgleichsenergie) / KORRIDOR_UNTEN, 6)
        if art == BASIS_DECKEL:
            return round(preis + ausgleichsenergie, 6)
    return None


def tarif_fuer(tarife: dict[int, float], monat: int) -> tuple[float, int] | None:
    """Tarif des Monats, sonst der jüngste davor veröffentlichte.

    Rückgabe: (€/kWh, Monat). Die OeMAG veröffentlicht den laufenden Monat erst
    im Laufe des Monats — bis dahin ist der Vormonat der beste bekannte Wert.
    """
    if not tarife:
        return None
    if monat in tarife:
        return tarife[monat], monat
    frueher = [m for m in tarife if m < monat]
    if frueher:
        letzter = max(frueher)
        return tarife[letzter], letzter
    # Nur spätere Monate bekannt (Jahreswechsel): den jüngsten davon nehmen.
    letzter = max(tarife)
    return tarife[letzter], letzter


class OemagProvider:
    """Holt den Einspeisetarif und hält ihn über Neustarts hinweg."""

    def __init__(self, hass: Any, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        if Store is not None:
            self._store = Store(hass, 1, f"{DOMAIN}_{entry_id}_oemag")
        else:
            self._store = None  # type: ignore[assignment]
        self._preis: float | None = None
        self._monat: int | None = None
        self._geholt: datetime | None = None
        self._fehler: str | None = None
        # Für die Hochrechnung (oemag_schaetzung.py): alle Tarife des Jahres
        # mit Berechnungsbasis und der Aufwand Ausgleichsenergie.
        self._tarife: dict[int, float] = {}
        self._basis: dict[int, str | None] = {}
        self._ausgleichsenergie: float | None = None

    # -- Zustand -------------------------------------------------------

    @property
    def ausgleichsenergie(self) -> float:
        """Aufwand Ausgleichsenergie PV in €/kWh — gelesen oder der Vorgabewert."""
        ae = self._ausgleichsenergie
        if ae and im_band(ae, 0.0, AUSGLEICHSENERGIE_MAX_EUR):
            return ae
        return AUSGLEICHSENERGIE_PV_DEFAULT

    def anker_fuer_quartal(self, quartal: int) -> float | None:
        """Quartalsmarktpreis Q (€/kWh), zurückgerechnet aus der Tabelle, sonst None."""
        return anker_aus_tabelle(
            self._tarife, self._basis, self.ausgleichsenergie, quartal
        )

    @property
    def preis(self) -> float | None:
        """Zuletzt gelesener Tarif in €/kWh, oder None."""
        if self._preis is None or self._geholt is None:
            return None
        if (_utcnow() - self._geholt).total_seconds() > CACHE_MAX_SECONDS:
            return None
        return self._preis

    def status(self) -> dict[str, Any]:
        """Für die Anzeige: Wert, Monat, Alter, letzter Fehler."""
        alter_min = None
        if self._geholt is not None:
            alter_min = int((_utcnow() - self._geholt).total_seconds() / 60)
        return {
            "preis": self.preis,
            "monat": self._monat,
            "alter_minuten": alter_min,
            "fehler": self._fehler,
            "quelle": OEMAG_URL,
        }

    # -- Laden und Holen -----------------------------------------------

    async def async_load(self) -> None:
        if self._store is None:
            return
        try:
            stored = await self._store.async_load()
            if stored and isinstance(stored, dict):
                # Ein Speicherstand aus der Zeit vor dem Plausibilitätsband
                # darf nicht zurück in den Fahrplan, was ein Abruf heute
                # verwerfen würde.
                preis = stored.get("preis")
                self._preis = preis if ist_monatswert(preis) else None
                self._monat = stored.get("monat")
                geholt = stored.get("geholt")
                if geholt:
                    self._geholt = datetime.fromisoformat(geholt)
                self._tarife = {
                    int(k): float(v)
                    for k, v in (stored.get("tarife") or {}).items()
                    if ist_monatswert(v)
                }
                self._basis = {
                    int(k): v for k, v in (stored.get("basis") or {}).items()
                }
                self._ausgleichsenergie = stored.get("ausgleichsenergie")
        except Exception:
            _LOGGER.debug("OeMAG: kein gespeicherter Tarif vorhanden")

    async def async_fetch(self, force: bool = False) -> float | None:
        """Tarif holen, wenn der gespeicherte Wert älter als 12 Stunden ist."""
        jetzt = _utcnow()
        if (
            not force
            and self._geholt is not None
            and (jetzt - self._geholt).total_seconds() < CACHE_FRESH_SECONDS
        ):
            return self.preis

        if async_get_clientsession is None:
            return self.preis

        try:
            import aiohttp

            session = async_get_clientsession(self._hass)
            async with session.get(
                OEMAG_URL,
                headers={"User-Agent": OEMAG_USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                html = await lies_text(resp)
        except Exception as err:
            self._fehler = str(err)
            _LOGGER.warning(
                "OeMAG-Tarif nicht abrufbar (%s) — es gilt weiter %s",
                err,
                f"{self._preis:.5f} €/kWh" if self._preis else "die Handeingabe",
            )
            return self.preis

        seite = parse_seite(html)
        tarife = seite["tarife"]
        treffer = tarif_fuer(tarife, dt_now_monat())
        if treffer is None:
            self._fehler = "Tabelle nicht lesbar"
            _LOGGER.warning(
                "OeMAG-Seite gelesen, aber kein Tarif erkannt — Aufbau der Seite "
                "geändert? Es gilt weiter %s",
                f"{self._preis:.5f} €/kWh" if self._preis else "die Handeingabe",
            )
            return self.preis

        # Eine Zahl außerhalb des Bands heißt: die Tabelle ist anders gelesen
        # als gemeint. Dann ist keiner ihrer Werte vertrauenswürdig — auch
        # nicht die übrigen Monate, aus denen die Hochrechnung ihren Anker
        # zurückrechnet.
        unplausibel = sorted(m for m, p in tarife.items() if not ist_monatswert(p))
        if unplausibel:
            self._fehler = "Tarif unplausibel (Monat {}: {:.3f} ct/kWh)".format(
                unplausibel[0], tarife[unplausibel[0]] * 100
            )
            _LOGGER.warning(
                "OeMAG-Seite gelesen, aber %s — es gilt weiter %s",
                self._fehler,
                f"{self._preis:.5f} €/kWh" if self._preis else "die Handeingabe",
            )
            return self.preis

        preis, monat = treffer
        self._preis, self._monat, self._geholt, self._fehler = preis, monat, jetzt, None
        self._tarife, self._basis = tarife, seite["basis"]
        if seite["ausgleichsenergie"]:
            self._ausgleichsenergie = seite["ausgleichsenergie"]
        _LOGGER.debug("OeMAG-Tarif: %.5f €/kWh (Monat %d)", preis, monat)

        if self._store is not None:
            try:
                await self._store.async_save(
                    {
                        "preis": preis,
                        "monat": monat,
                        "geholt": jetzt.isoformat(),
                        "tarife": {str(k): v for k, v in tarife.items()},
                        "basis": {str(k): v for k, v in seite["basis"].items()},
                        "ausgleichsenergie": self._ausgleichsenergie,
                    }
                )
            except Exception:
                _LOGGER.debug("OeMAG-Tarif konnte nicht gespeichert werden")
        return preis


# ---------------------------------------------------------------------------
# Kleinkram
# ---------------------------------------------------------------------------


def dt_now_monat() -> int:
    """Aktueller Monat in Ortszeit — als eigene Funktion, damit Tests sie
    ersetzen können."""
    return datetime.now().month


def im_band(wert: Any, unten: float, oben: float) -> bool:
    """Endliche Zahl zwischen ``unten`` und ``oben`` (beide eingeschlossen).

    ``float("nan")`` besteht jeden Vergleich mit False und rutscht deshalb
    durch ``min``/``max``-Klemmen hindurch; hier fällt er heraus.
    """
    if isinstance(wert, bool) or not isinstance(wert, (int, float)):
        return False
    return math.isfinite(wert) and unten <= wert <= oben


def ist_monatswert(wert: Any) -> bool:
    """Liegt ein Monatstarif (€/kWh) im Plausibilitätsband?"""
    return im_band(wert, MONATSWERT_MIN_EUR, MONATSWERT_MAX_EUR)


# Die Tabellenausdrücke der Tarifseiten. Als ``re.findall(r"<table.*?</table>")``
# geschrieben, sucht jeder offene Anfang ohne Ende bis ans Textende — bei
# vielen davon quadratisch. ``bloecke`` findet dieselben Treffer linear.
_TABLE_AUF = re.compile(r"<table", re.I)
_TABLE_ZU = re.compile(r"</table>", re.I)
_TR_AUF = re.compile(r"<tr", re.I)
_TR_ZU = re.compile(r"</tr>", re.I)
_TD_AUF = re.compile(r"<t[dh]", re.I)
_TD_ZU = re.compile(r"</t[dh]>", re.I)


def bloecke(text: str, auf: re.Pattern, zu: re.Pattern) -> list[str]:
    """Alle Abschnitte von ``auf`` bis zum nächsten ``zu``, einschließlich.

    Dasselbe Ergebnis wie ``re.findall(auf + ".*?" + zu, text, re.S)``: der
    kürzeste Treffer, nicht überlappend, weiter hinter seinem Ende. Fehlt
    zu einem Anfang das Ende, kann auch kein späterer eins haben — dort ist
    Schluss, statt jeden weiteren Anfang bis ans Textende zu verfolgen.
    """
    ergebnis: list[str] = []
    pos = 0
    while True:
        anfang = auf.search(text, pos)
        if anfang is None:
            return ergebnis
        ende = zu.search(text, anfang.end())
        if ende is None:
            return ergebnis
        ergebnis.append(text[anfang.start():ende.end()])
        pos = ende.end()


_TAG = re.compile(r"<[^>]+>")


def ohne_tags(text: str, ersatz: str = " ") -> str:
    """Wie ``re.sub(r"<[^>]+>", ersatz, text)``, aber linear.

    Der Ausdruck scheitert an jedem ``<`` ohne folgendes ``>`` erst am
    Textende — bei vielen davon quadratisch. Hinter dem letzten ``>`` kann
    aber kein Treffer mehr beginnen; dieser Rest bleibt deshalb unberührt.
    """
    letztes = text.rfind(">")
    if letztes < 0:
        return text
    return _TAG.sub(ersatz, text[:letztes + 1]) + text[letztes + 1:]


def ohne_elemente(text: str, namen: tuple[str, ...]) -> str:
    """Elemente samt Inhalt durch ein Leerzeichen ersetzen, z. B. Skripte.

    Dasselbe wie ``re.sub(r"<(script|style)[^>]*>.*?</\\1>", " ", text,
    flags=re.S | re.I)`` für ``namen=("script", "style")``, nur linear: Der
    Ausdruck verfolgte jeden offenen Anfang ohne Ende bis ans Textende. Hier
    wird je Name gemerkt, ab wo es kein Ende mehr gibt, und Anfänge werden
    nur vor dem letzten ``>`` gesucht (dahinter kann keiner vollständig sein).
    """
    auf = re.compile(r"<(" + "|".join(namen) + r")[^>]*>", re.I)
    grenze = text.rfind(">") + 1
    teile: list[str] = []
    pos = 0
    kein_ende_ab: dict[str, int] = {}
    while pos < grenze:
        anfang = auf.search(text, pos, grenze)
        if anfang is None:
            break
        name = anfang.group(1).lower()
        ende = None
        if anfang.end() < kein_ende_ab.get(name, len(text) + 1):
            ende = re.compile("</" + re.escape(name) + ">", re.I).search(text, anfang.end())
            if ende is None:
                kein_ende_ab[name] = anfang.end()
        if ende is None:
            # Kein Treffer an dieser Stelle — der Ausdruck suchte ab dem
            # nächsten Zeichen weiter; ein späteres Element kann geschlossen sein.
            teile.append(text[pos:anfang.start() + 1])
            pos = anfang.start() + 1
            continue
        teile.append(text[pos:anfang.start()])
        teile.append(" ")
        pos = ende.end()
    teile.append(text[pos:])
    return "".join(teile)


async def _lies_bytes(resp: Any, grenze: int) -> bytes | None:
    """Rumpf einer aiohttp-Antwort lesen, höchstens ``grenze`` Bytes.

    None, wenn die Antwort keinen Datenstrom hat (Testattrappen, die nur
    ``text()``/``json()`` kennen) — dann liest der Aufrufer wie bisher.
    """
    laenge = getattr(resp, "content_length", None)
    if isinstance(laenge, int) and laenge > grenze:
        raise RuntimeError(f"Antwort zu groß ({laenge} Bytes)")
    strom = getattr(resp, "content", None)
    stuecke = getattr(strom, "iter_chunked", None)
    if not callable(stuecke):
        return None
    teile: list[bytes] = []
    summe = 0
    async for teil in stuecke(64 * 1024):
        summe += len(teil)
        if summe > grenze:
            raise RuntimeError(f"Antwort größer als {grenze} Bytes")
        teile.append(teil)
    return b"".join(teile)


async def lies_text(resp: Any, grenze: int = MAX_ANTWORT_BYTES) -> str:
    """Wie ``resp.text()``, aber mit Größenbegrenzung."""
    roh = await _lies_bytes(resp, grenze)
    if roh is None:
        text = await resp.text()
        if len(text) > grenze:
            raise RuntimeError(f"Antwort größer als {grenze} Zeichen")
        return text
    zeichensatz = getattr(resp, "charset", None) or "utf-8"
    try:
        return roh.decode(zeichensatz, errors="replace")
    except LookupError:  # unbekannter Zeichensatz im Header
        return roh.decode("utf-8", errors="replace")


def _kein_nan(_konstante: str) -> None:
    """``NaN``/``Infinity`` im JSON gelten als fehlender Wert (``null``)."""
    return None


async def lies_json(resp: Any, grenze: int = MAX_ANTWORT_BYTES) -> Any:
    """Wie ``resp.json(content_type=None)``, mit Größenbegrenzung.

    Pythons ``json`` nimmt ``NaN`` und ``Infinity`` ohne Murren an — kein
    gültiges JSON, aber genau das, was ein Dienst mit kaputter Rechnung
    ausliefert. Hier werden sie zu ``None``, wie ein noch nicht gemeldeter
    Wert, und fallen beim Lesen der Reihe heraus.
    """
    roh = await _lies_bytes(resp, grenze)
    if roh is None:
        return await resp.json(content_type=None)
    return json.loads(roh.decode("utf-8", errors="replace"), parse_constant=_kein_nan)


def _ct_pro_kwh(text: str) -> float | None:
    """``6,146 ct/kWh`` → 0,06146 €/kWh. Ohne ct-Angabe: kein Wert."""
    if "ct" not in text.lower():
        return None
    treffer = re.search(r"(\d+(?:[.,]\d+)?)", text)
    if not treffer:
        return None
    try:
        return round(float(treffer.group(1).replace(",", ".")) / 100.0, 6)
    except ValueError:
        return None
