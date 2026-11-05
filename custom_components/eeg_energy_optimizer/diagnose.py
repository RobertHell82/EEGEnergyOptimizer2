"""Diagnose der Ansteuerung: jeden Schreibweg einmal ausprobieren und messen.

Warum es das gibt: Ein ``True`` vom Treiber heißt nur „der Befehl wurde
angenommen". Ob er wirkt, zeigte bisher erst der Betrieb — Tage später über
Guard 3 und die Telemetrie, und nur in den Lagen, die der Fahrplan zufällig
erzeugte (Grünbach 21.09.2026: 1¾ h quittierte Entladung, nichts kam an).
Mehrere Treiberwege sind am Gerät nie gelaufen (Kostal-/Sigenergy-Deckel,
Netzladung außer Huawei, Victron ganz).

Die Diagnose geht die fünf Schreibwege von ``InverterBase`` der Reihe nach
durch — Ladelimit, Zwangsentladung, Freigabe, Entladedeckel, Netzladung —
und prüft jeden auf drei Ebenen, weil Fehler genau zwischen ihnen verloren
gehen:

1. **Angenommen** — Rückgabewert und ``last_write_error`` des Treibers.
2. **Zurückgelesen** — steht der Wert danach im Gerät (wo ein Leseweg
   existiert: ``async_get_charge_limit_kw``; die übrigen Stellwerte gehen
   roh ins Protokoll).
3. **Gewirkt** — bewegt sich die gemessene Batterie-/Netzleistung, wie der
   Befehl es verlangt.

Jede Wirkungsprüfung ist so gebaut, dass der Zustand VOR dem Befehl sie
nicht erfüllt — sonst bestünde sie auf einem veralteten Sensorwert (Huawei
liefert nur alle 30 s). Fehlt die Voraussetzung (kein PV-Überschuss, der
geladen wird; keine Entladung, die sich deckeln ließe), wird der Befehl
trotzdem geschrieben und zurückgelesen, die Wirkung aber als „nicht
prüfbar" ausgewiesen — nie als bestanden. Ergebnis dann „teilweise".

Während des Tests steht der Fahrplan still: Der Modus geht auf Aus (Executor
gibt frei, Heizstab auf 0, Spitzenkappung schaltet zurück), zusätzlich
überspringt der Guard-Lauf den Executor (``laeuft``), denn im Modus Aus
nähme er Deckel und Limits sonst selbst zurück. Am Ende gibt die Diagnose
über ``ScheduleExecutor.async_release`` frei und stellt den vorigen Modus
wieder her. Bricht HA mitten im Test ab, bleibt der Modus auf Aus — die
sichere Seite; der Executor holt nach dem Start die Freigabe im Modus Aus
von selbst nach (``_display_release_pending``).

Je Bereich wird festgehalten, wann er zuletzt lief und mit welchem Ergebnis
(``Store``), dazu das vollständige Protokoll des letzten Laufs — für den
Download im Panel und die Telemetrie (``/v1/diagnose``).
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .const import (
    CONF_BATTERY_SOC_SENSOR,
    CONF_DISCHARGE_POWER_KW,
    CONF_INVERTER_TYPE,
    DOMAIN,
    GUARD_WIRKUNG_MIN_ANTEIL,
    GUARD_WIRKUNG_MIN_LUECKE_KW,
    MODE_AUS,
    MODE_EIN,
)

try:
    from homeassistant.helpers.storage import Store
except ImportError:  # pragma: no cover — Testumgebung
    Store = None  # type: ignore[assignment]

try:
    from homeassistant.util import dt as dt_util
except ImportError:  # pragma: no cover — Testumgebung
    dt_util = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

PROTOKOLL_VERSION = 1

# Bereiche in der Reihenfolge des Ablaufs. ``freigabe`` sammelt jede
# Rücknahme (nach Ladelimit, Entladung und Netzladung).
BEREICHE = (
    "vorpruefung",
    "ladelimit",
    "entladung",
    "freigabe",
    "deckel",
    "netzladung",
)
BEREICH_TITEL = {
    "vorpruefung": "Vorprüfung",
    "ladelimit": "Ladelimit",
    "entladung": "Zwangsentladung",
    "freigabe": "Freigabe (Automatik)",
    "deckel": "Entladedeckel",
    "netzladung": "Netzladung",
}

# Ergebnisse eines Schritts und eines Bereichs.
OK = "ok"
TEILWEISE = "teilweise"       # angenommen + zurückgelesen, Wirkung nicht prüfbar
UNKLAR = "unklar"             # Messung uneindeutig — wiederholen
FEHLER = "fehler"
UEBERSPRUNGEN = "uebersprungen"
ABGEBROCHEN = "abgebrochen"

# Schwere für die Zusammenfassung eines Bereichs: der schlimmste Schritt zählt.
_SCHWERE = {OK: 0, UEBERSPRUNGEN: 0, TEILWEISE: 1, ABGEBROCHEN: 2, UNKLAR: 3, FEHLER: 4}

# Wirkung einer Beobachtung.
ERFUELLT = "erfuellt"
NICHT_ERFUELLT = "nicht_erfuellt"
UNEINDEUTIG = "unklar"
NICHT_PRUEFBAR = "nicht_pruefbar"

# Testleistung. Klein, damit der Test nichts kostet und keine Grenze reißt,
# groß genug, um über dem Messrauschen (Guard-3-Lücke 0,3 kW) zu liegen.
TEST_KW = 1.0
TEST_LADELIMIT_KW = 1.0
TEST_DECKEL_KW = 0.5

# Beobachtung: alle PROBE_S eine Messung, frühestens nach MIN_S und drei
# erfüllten Proben in Folge fertig, spätestens nach MAX_S. 120 s, weil
# Huawei-Sensoren nur alle 30 s kommen und Victron/Kostal erst nachregeln.
PROBE_S = 5.0
BEOBACHTUNG_MIN_S = 20.0
BEOBACHTUNG_MAX_S = 120.0
FOLGE_OK = 3
# Ruhephase vor jedem Bereich: Automatik einschwingen lassen und den
# Ausgangszustand messen, an dem sich die Voraussetzung entscheidet.
RUHE_S = 20.0
BASIS_S = 30.0
# Der ganze Test endet spätestens hiernach (Sicherheitsnetz).
# 45 min: Sigenergy braucht je Schaltschritt bis 60 s, und ein Kostal hält die
# Batterie nach einem 1034-Sollwert bis zum Watchdog-Rückfall.
GESAMT_MAX_S = 45 * 60.0
# Kostal: so lange auf den Watchdog-Rückfall warten (``batterie_gesperrt``),
# bevor ein Bereich als nicht prüfbar gilt.
RUECKFALL_WARTEN_MAX_S = 600.0
# Rücklesen: Zustände kommen bei manchen Integrationen verzögert (Victron
# über MQTT) — so lange auf den gesetzten Wert warten.
RUECKLESEN_WARTEN_S = 15.0

# Voraussetzungen.
LADEN_MIN_KW = 0.5            # so viel muss die Batterie laden, damit ein Limit sichtbar wird
ENTLADEN_MIN_KW = 0.5         # so viel muss sie entladen, damit ein Deckel sichtbar wird
SOC_ABSTAND_PCT = 5.0         # Abstand zu Min/Max-SOC für Entladung/Netzladung
SOC_SCHRITT_PCT = 3.0         # Ziel-SOC: so weit vom aktuellen weg
LIMIT_TOLERANZ_KW = 0.25      # „geladen wird nicht" heißt ≤ diesem Wert
RUECKLESEN_TOLERANZ_KW = 0.15
NETZ_SICHERHEIT_KW = 0.3      # Abstand zur Spitzenkappungs-/Netzladegrenze
NETZBEZUG_ABBRUCH_KW = 1.5    # Netzladung: so viel über Erwartung → Sicherheitsabbruch
FREI_KW = 0.3                 # Freigabe: unter diesem Wert gilt ein Fluss als „keiner"


def _jetzt() -> datetime:
    if dt_util is not None:
        jetzt = dt_util.now()
        if isinstance(jetzt, datetime):
            return jetzt
    return datetime.now(tz=timezone.utc)


# ----------------------------------------------------------------------
# Reine Bewertung (ohne hass, getestet in tests/test_diagnose.py)
# ----------------------------------------------------------------------
def urteil(werte: list[bool | None]) -> str:
    """Wirkung aus einer Reihe von Probenurteilen (None = nicht bewertbar).

    Die letzten ``FOLGE_OK`` bewertbaren Proben entscheiden: alle erfüllt →
    erfüllt — dieselbe Zahl, bei der ``frueh_fertig`` die Beobachtung
    beendet. Bis 2.1.70 verlangte das Urteil vier, die Beobachtung endete
    nach drei: Jeder Befehl, der nicht sofort wirkte, war „unklar"
    (Traun 05.11.2026, Huawei: Wirkung nach 35 s, drei erfüllte Proben,
    davor eine ohne). Nie erfüllt → nicht erfüllt. Dazwischen (kippt hin und
    her, oder erst erfüllt, dann nicht mehr) → unklar: dann hat vermutlich
    die Hauslast gesprungen, und ein falsches „Fehler" wäre schlimmer als
    „wiederholen".
    """
    bewertbar = [w for w in werte if w is not None]
    if not bewertbar:
        return NICHT_PRUEFBAR
    if all(bewertbar[-FOLGE_OK:]):
        return ERFUELLT
    if not any(bewertbar):
        return NICHT_ERFUELLT
    return UNEINDEUTIG


def frueh_fertig(werte: list[bool | None], vergangen_s: float) -> bool:
    """Darf die Beobachtung vorzeitig enden? Erst nach MIN_S, dann
    FOLGE_OK erfüllte bewertbare Proben in Folge (die letzten)."""
    if vergangen_s < BEOBACHTUNG_MIN_S:
        return False
    bewertbar = [w for w in werte if w is not None]
    return len(bewertbar) >= FOLGE_OK and all(bewertbar[-FOLGE_OK:])


def wirkung_erfuellt(soll: float, ist: float) -> bool:
    """Dieselbe Regel wie Guard 3: Anteil ODER Lücke reicht."""
    if ist >= soll * GUARD_WIRKUNG_MIN_ANTEIL:
        return True
    return (soll - ist) < GUARD_WIRKUNG_MIN_LUECKE_KW


def schritt_ergebnis(
    befehl_ok: bool | None, ruecklesen: str | None, wirkung: str | None
) -> str:
    """Ergebnis eines Schritts aus den drei Ebenen.

    ``befehl_ok`` None = es wurde nichts geschrieben (übersprungen).
    ``ruecklesen``: ``ok`` / ``abweichung`` / ``nicht_lesbar`` / None.
    """
    if befehl_ok is None:
        return UEBERSPRUNGEN
    if befehl_ok is False:
        return FEHLER
    if ruecklesen == "abweichung":
        return FEHLER
    if wirkung == NICHT_ERFUELLT:
        return FEHLER
    if wirkung == UNEINDEUTIG:
        return UNKLAR
    if wirkung in (None, NICHT_PRUEFBAR):
        return TEILWEISE
    return OK


def bereich_ergebnis(ergebnisse: list[str]) -> str:
    """Der schlimmste Schritt zählt; nur übersprungene → übersprungen.

    Ausnahme: „teilweise" neben einem „ok" desselben Bereichs ist ok — der
    Schreibweg ist dann einmal nachweislich wirksam gewesen (Ladelimit 0
    wirkte, das 1-kW-Limit war mangels Ladeleistung nur nicht zu sehen).
    Fehler und Unklares bleiben vorne.
    """
    if not ergebnisse:
        return UEBERSPRUNGEN
    if all(e == UEBERSPRUNGEN for e in ergebnisse):
        return UEBERSPRUNGEN
    relevant = [e for e in ergebnisse if e != UEBERSPRUNGEN]
    if OK in relevant:
        relevant = [e for e in relevant if e != TEILWEISE]
    return max(relevant, key=lambda e: _SCHWERE.get(e, 0))


def gesamt_ergebnis(bereiche: list[str]) -> str:
    """Ergebnis des ganzen Laufs: der schlimmste Bereich, ohne Ausnahme.

    Anders als innerhalb eines Bereichs macht hier ein „ok" kein
    „teilweise" wett — ein ungeprüfter Deckel bleibt ungeprüft, auch wenn
    die Entladung bestand.
    """
    relevant = [e for e in bereiche if e != UEBERSPRUNGEN]
    if not relevant:
        return UEBERSPRUNGEN
    return max(relevant, key=lambda e: _SCHWERE.get(e, 0))


def median(werte: list[float]) -> float | None:
    reihe = sorted(w for w in werte if isinstance(w, (int, float)) and math.isfinite(w))
    if not reihe:
        return None
    mitte = len(reihe) // 2
    if len(reihe) % 2:
        return reihe[mitte]
    return (reihe[mitte - 1] + reihe[mitte]) / 2


def pv_einbruch(basis_pv: float | None, proben: list[dict]) -> tuple[float, float] | None:
    """Fiel die PV während eines Zwangsmodus fast auf null? ``(vorher, nachher)``.

    Sigenergy, 05.11.2026: „Command Discharging (ESS First)" mit 1 kW — die
    Batterie lieferte exakt 1 kW, die PV fiel von 17,9 auf 0 kW, die
    Einspeisung sank. Das Gerät regelt in diesem Modus die PV ab. Gewertet
    werden die letzten ``FOLGE_OK`` Proben gegen den Ausgangswert; eine
    Wolke drückt die PV selten binnen zwei Minuten unter 30 %, ein
    abregelnder Modus auf null.
    """
    if basis_pv is None or basis_pv < 1.0:
        return None
    pv = [x.get("pv") for x in proben if isinstance(x, dict) and x.get("pv") is not None]
    if len(pv) < FOLGE_OK:
        return None
    nachher = median(pv[-FOLGE_OK:])
    if nachher is None or nachher > max(0.3, basis_pv * 0.3):
        return None
    return round(basis_pv, 2), round(nachher, 2)


def _kw_text(kw: float) -> str:
    """kW für Texte: zwei Stellen, nie „-0.00"."""
    wert = round(float(kw), 2)
    return f"{0.0 if wert == 0 else wert:.2f}"


def basis_aus_proben(proben: list[dict]) -> dict[str, float | None]:
    """Median je Größe — eine Wolke soll die Voraussetzung nicht kippen."""
    return {
        k: median([p.get(k) for p in proben if p.get(k) is not None])
        for k in ("soc", "pv", "batt", "netz", "haus")
    }


def _ladung(p: dict) -> float | None:
    """Ladeleistung (positiv), None ohne Messung."""
    b = p.get("batt")
    return None if b is None else b


def _entladung(p: dict) -> float | None:
    """Entladeleistung als positiver Wert (Laden = 0)."""
    b = p.get("batt")
    return None if b is None else max(-b, 0.0)


def pruef_laden_hoechstens(grenze_kw: float) -> Callable[[dict], bool | None]:
    def _p(p: dict) -> bool | None:
        ladung = _ladung(p)
        if ladung is None:
            return None
        return ladung <= grenze_kw + LIMIT_TOLERANZ_KW
    return _p


def pruef_laden_mindestens(soll_kw: float) -> Callable[[dict], bool | None]:
    def _p(p: dict) -> bool | None:
        ladung = _ladung(p)
        if ladung is None:
            return None
        return wirkung_erfuellt(soll_kw, max(ladung, 0.0))
    return _p


def pruef_laden_bei(ziel_kw: float) -> Callable[[dict], bool | None]:
    """Ein Limit ungleich 0: Die Batterie lädt NAHE dem Limit.

    Nur „höchstens" bestünde schon im Zustand davor (Limit 0, Batterie
    steht) — ein ignorierter oder falsch skalierter Wert fiele nie auf.
    Gefordert wird deshalb auch die Untergrenze; die Prüfung läuft nur,
    wenn die Batterie vorher deutlich mehr lud.
    """
    def _p(p: dict) -> bool | None:
        ladung = _ladung(p)
        if ladung is None:
            return None
        if ladung < ziel_kw - GUARD_WIRKUNG_MIN_LUECKE_KW:
            # Weniger als das Limit — begrenzt das Limit, geht der Rest ins
            # Netz. Geht nichts ins Netz, fehlt schlicht der Überschuss (Kostal,
            # 05.11.2026: Haus sprang auf 3,5 kW, für die Batterie blieben
            # 0,3 kW). Dann sagt die Probe nichts über das Limit.
            netz = p.get("netz")
            if netz is None or netz < FREI_KW:
                return None
            return False
        return ladung <= ziel_kw + LIMIT_TOLERANZ_KW
    return _p


def pruef_entladen_bei(ziel_kw: float) -> Callable[[dict], bool | None]:
    """Ein Deckel ungleich 0: Die Batterie entlädt NAHE dem Deckel."""
    def _p(p: dict) -> bool | None:
        entladung = _entladung(p)
        if entladung is None:
            return None
        if entladung < ziel_kw - GUARD_WIRKUNG_MIN_LUECKE_KW:
            # Spiegelbild zum Ladelimit: Ohne Netzbezug braucht das Haus
            # gerade weniger als der Deckel erlaubt — nichts zu sehen.
            netz = p.get("netz")
            if netz is None or netz > -FREI_KW:
                return None
            return False
        return entladung <= ziel_kw + LIMIT_TOLERANZ_KW
    return _p


def pruef_entladen_ueber(grenze_kw: float, soll_kw: float) -> Callable[[dict], bool | None]:
    """Nach dem Aufheben eines Deckels: entlädt sie wieder MEHR als er erlaubte?"""
    def _p(p: dict) -> bool | None:
        entladung = _entladung(p)
        if entladung is None:
            return None
        return entladung > grenze_kw + FREI_KW and wirkung_erfuellt(soll_kw, entladung)
    return _p


def pruef_entladen_hoechstens(grenze_kw: float) -> Callable[[dict], bool | None]:
    def _p(p: dict) -> bool | None:
        entladung = _entladung(p)
        if entladung is None:
            return None
        return entladung <= grenze_kw + LIMIT_TOLERANZ_KW
    return _p


def pruef_entladen_mindestens(soll_kw: float) -> Callable[[dict], bool | None]:
    def _p(p: dict) -> bool | None:
        entladung = _entladung(p)
        if entladung is None:
            return None
        return wirkung_erfuellt(soll_kw, entladung)
    return _p


def pruef_einspeisen_mindestens(soll_kw: float) -> Callable[[dict], bool | None]:
    """Netz-Sollwert-Treiber (SMA, Victron): der Netzzähler zählt."""
    def _p(p: dict) -> bool | None:
        netz = p.get("netz")
        if netz is None:
            return None
        return wirkung_erfuellt(soll_kw, max(netz, 0.0))
    return _p


def pruef_einspeisung_steigt(
    basis_netz: float, soll_kw: float, entladung_min_kw: float = 0.0
) -> Callable[[dict], bool | None]:
    """Zwangsentladung: Die Einspeisung steigt um ``soll_kw`` gegenüber vorher,
    UND die Batterie entlädt mindestens ``entladung_min_kw``.

    Nicht die Batterieleistung allein: Abends entlädt die Batterie im
    Automatikbetrieb ohnehin für das Haus — „Batterie entlädt ≥ x" bestünde
    dort auch mit einem wirkungslosen Befehl. Nicht der Zuwachs allein:
    Mittags hört eine geladene Batterie bei jedem Zwangsmodus auf zu laden,
    die Einspeisung stiege auch mit einem Befehl, der nur das Laden sperrt
    (die offene Frage bei Sigenergy). Beides zusammen ist die
    Zwangsentladung, und vor dem Befehl ist der Zuwachs 0.
    """
    def _p(p: dict) -> bool | None:
        netz = p.get("netz")
        batt = p.get("batt")
        if netz is None or batt is None:
            return None
        if entladung_min_kw > 0 and -batt < entladung_min_kw:
            return False
        return wirkung_erfuellt(soll_kw, netz - basis_netz)
    return _p


def pruef_laden_ueber(grenze_kw: float, soll_kw: float) -> Callable[[dict], bool | None]:
    """Nach dem Aufheben eines Ladelimits: lädt sie wieder MEHR als das Limit?"""
    def _p(p: dict) -> bool | None:
        ladung = _ladung(p)
        if ladung is None:
            return None
        return ladung > grenze_kw + FREI_KW and wirkung_erfuellt(soll_kw, ladung)
    return _p


def pruef_keine_zwangsentladung(p: dict) -> bool | None:
    """Automatik: Die Batterie speist nicht mehr ins Netz.

    Entladen darf sie (sie deckt das Haus), einspeisen darf die Anlage (PV
    bei voller Batterie) — nur beides zugleich ist die Zwangsentladung.
    """
    b, n = p.get("batt"), p.get("netz")
    if b is None or n is None:
        return None
    return not (b < -FREI_KW and n > FREI_KW)


def pruef_keine_netzladung(p: dict) -> bool | None:
    """Automatik: Die Batterie lädt nicht mehr aus dem Netz."""
    b, n = p.get("batt"), p.get("netz")
    if b is None or n is None:
        return None
    return not (b > FREI_KW and n < -FREI_KW)


def ruecklesen_bewerten(soll_kw: float, gelesen: float | None) -> str:
    if gelesen is None:
        return "nicht_lesbar"
    toleranz = max(RUECKLESEN_TOLERANZ_KW, abs(soll_kw) * 0.1)
    return "ok" if abs(gelesen - soll_kw) <= toleranz else "abweichung"


def _r(wert: Any, stellen: int = 3) -> Any:
    if isinstance(wert, float):
        return round(wert, stellen) if math.isfinite(wert) else None
    return wert


def fuer_telemetrie(protokoll: dict) -> dict:
    """Protokoll ohne Entitätsnamen und Einstellungen (die stehen im Profil).

    Die Stellwert-Zeilen behalten Bezeichnung, Rolle und Wert — die
    entity_id fällt weg, wie überall in der Telemetrie.
    """
    def _zeilen(zeilen: Any) -> list[dict]:
        if not isinstance(zeilen, list):
            return []
        return [
            {k: v for k, v in z.items() if k != "entity_id"}
            for z in zeilen if isinstance(z, dict)
        ]

    aus = {k: v for k, v in protokoll.items() if k not in ("einstellungen",)}
    if protokoll.get("abbruch_ausnahme"):
        aus["abbruch_grund"] = f"Interner Fehler: {protokoll['abbruch_ausnahme']}"
    aus["stellwerte_vorher"] = _zeilen(protokoll.get("stellwerte_vorher"))
    aus["stellwerte_nachher"] = _zeilen(protokoll.get("stellwerte_nachher"))
    schritte = []
    for s in protokoll.get("schritte") or []:
        s2 = dict(s)
        if "stellwerte" in s2:
            s2["stellwerte"] = _zeilen(s2.get("stellwerte"))
        if s2.get("ausnahme"):
            s2["fehler"] = s2["ausnahme"]
            s2["text"] = f"Befehl abgelehnt: {s2['ausnahme']}"
        schritte.append(s2)
    aus["schritte"] = schritte
    bereiche = {}
    for name, b in (protokoll.get("bereiche") or {}).items():
        b2 = dict(b) if isinstance(b, dict) else b
        bereiche[name] = b2
    aus["bereiche"] = bereiche
    # Was durchrutscht (Treibertexte, Bereichstexte aus einer Ausnahme):
    # Adressen tilgen.
    return _adressen_tilgen(aus)


_ADRESSE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b|\[[0-9a-fA-F:]+\](?::\d+)?")


def _adressen_tilgen(wert: Any) -> Any:
    if isinstance(wert, str):
        return _ADRESSE.sub("<adresse>", wert)
    if isinstance(wert, dict):
        return {k: _adressen_tilgen(v) for k, v in wert.items()}
    if isinstance(wert, list):
        return [_adressen_tilgen(v) for v in wert]
    return wert


def befunde(protokoll: dict) -> list[dict]:
    """Fehlgeschlagene Schritte für die Telemetrie-Kategorie ``diagnose``.

    ``art`` ist stabil und zahlenfrei (geht in den ``message_hash``):
    ``abgelehnt`` (+ Fehlergrund des Treibers), ``ruecklesen``,
    ``wirkungslos``. Ein interner Abbruch wird als ``intern`` gemeldet.
    Uneindeutige Messungen nicht — sie sagen „wiederholen", nicht „kaputt".
    """
    aus: list[dict] = []
    for s in protokoll.get("schritte") or []:
        if s.get("ergebnis") != FEHLER:
            continue
        if s.get("befehl_ok") is False:
            # Ausnahmen nur mit dem Typ (die Meldung trägt Adressen und
            # Zahlen); der Treibertext ganz — „number.set_value: ServiceNotFound"
            # und „number.set_value: Timeout" sind zwei Ursachen.
            grund = s.get("ausnahme") or _adressen_tilgen(str(s.get("fehler") or ""))[:120]
            art = f"abgelehnt ({grund})" if grund else "abgelehnt"
        elif s.get("ruecklesen") == "abweichung":
            art = "ruecklesen"
        elif s.get("wirkung") == NICHT_ERFUELLT:
            art = "wirkungslos"
        else:
            art = "fehler"
        proben = [p for p in s.get("proben") or [] if isinstance(p, dict)]
        aus.append({
            "bereich": s.get("bereich"),
            "schritt": s.get("titel"),
            "art": art,
            "befehl": s.get("befehl"),
            "ruecklesen_wert": s.get("ruecklesen_wert"),
            "batt_median": median([p.get("batt") for p in proben if p.get("batt") is not None]),
            "netz_median": median([p.get("netz") for p in proben if p.get("netz") is not None]),
        })
    grund = str(protokoll.get("abbruch_grund") or "")
    if grund.startswith("Interner Fehler"):
        aus.append({"bereich": "ablauf", "schritt": None, "art": "intern",
                    "grund": grund.split(":", 2)[1].strip() if grund.count(":") >= 1 else grund})
    return aus


class _Abbruch(Exception):
    """Test abbrechen (Nutzer, Entladen der Integration, Moduswechsel)."""


# ----------------------------------------------------------------------
# Ablauf
# ----------------------------------------------------------------------
class AnsteuerungsDiagnose:
    """Führt die Diagnose aus, hält ihren Zustand und das letzte Protokoll.

    ``data`` ist das ``hass.data[DOMAIN][entry_id]``-Dict; Wechselrichter,
    Executor, Select und Konfiguration werden bei Bedarf daraus gelesen —
    ein Hot-Reload tauscht ``data["config"]`` aus.

    ``messen``, ``schlafen`` und ``uhr`` sind für Tests austauschbar.
    """

    def __init__(
        self,
        hass: Any,
        entry_id: str,
        data: dict,
        *,
        messen: Callable[[], dict] | None = None,
        schlafen: Callable[[float], Awaitable[None]] | None = None,
        uhr: Callable[[], float] | None = None,
        bei_ende: Callable[[dict], Any] | None = None,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._data = data
        self._messen_fn = messen
        self._schlafen = schlafen or asyncio.sleep
        self._uhr = uhr or time.monotonic
        # Wird nach jedem Lauf mit dem Protokoll aufgerufen (Telemetrie).
        self.bei_ende = bei_ende
        store_key = f"{DOMAIN}_{entry_id}_diagnose"
        self._store: Any = Store(hass, 1, store_key) if Store is not None else None
        # Persistiert: je Bereich das letzte Ergebnis, das letzte Protokoll,
        # und ob gerade ein Test läuft (für den Neustart mitten im Test).
        self._bereiche: dict[str, dict] = {}
        self._letztes: dict | None = None
        self._task: asyncio.Task | None = None
        # Die Abschluss-Freigabe läuft als eigener, abgeschirmter Task: Bricht
        # HA den Test beim Herunterfahren ab, muss sie trotzdem zu Ende gehen.
        self._abschluss_task: asyncio.Task | None = None
        self._von_ha_beendet = False
        self._abbruch_grund: str | None = None
        self._start_mono: float = 0.0
        # Live-Zustand für das Panel.
        self._protokoll: dict | None = None
        self._schritt: dict | None = None
        self._letzte_probe: dict | None = None
        self.laeuft = False

    # ------------------------------------------------------------------
    # Persistenz
    # ------------------------------------------------------------------
    async def async_load(self) -> None:
        if self._store is None:
            return
        try:
            gespeichert = await self._store.async_load()
        except Exception:  # noqa: BLE001 — ein kaputter Store darf das Setup nicht kippen
            _LOGGER.warning("Diagnose: Speicher nicht lesbar — starte ohne", exc_info=True)
            return
        if not isinstance(gespeichert, dict):
            return
        bereiche = gespeichert.get("bereiche")
        if isinstance(bereiche, dict):
            self._bereiche = {
                k: v for k, v in bereiche.items() if k in BEREICHE and isinstance(v, dict)
            }
        letztes = gespeichert.get("letztes")
        if isinstance(letztes, dict):
            self._letztes = letztes
        offen = gespeichert.get("laeuft_seit")
        if offen:
            # HA ging mitten im Test aus (Absturz, Stromausfall). Was am
            # Gerät steht, weiß niemand — ein Huawei behält einen Deckel 0
            # über jeden Neustart. Der Executor erfährt es, damit seine
            # Freigabe nach der Startphase auch den Deckel aufhebt, und der
            # Modus geht auf Aus: Der Select stellt seinen Zustand aus einem
            # Speicher wieder her, der nur alle 15 min geschrieben wird —
            # nach einem Absturz kann dort noch „Ein" stehen.
            executor = self._data.get("executor")
            nach = getattr(executor, "nach_fremdeingriff", None)
            if nach is not None:
                try:
                    nach()
                except Exception:  # noqa: BLE001
                    _LOGGER.debug("Diagnose: Executor nicht informiert", exc_info=True)
            if self._modus() == MODE_EIN:
                await self._modus_setzen(MODE_AUS)
            _LOGGER.warning(
                "Diagnose: Der Test vom %s wurde durch einen Neustart unterbrochen "
                "— die Steuerung bleibt aus", offen,
            )
            self._letztes = {
                "version": PROTOKOLL_VERSION,
                "gestartet": offen,
                "beendet": None,
                "ergebnis": ABGEBROCHEN,
                "abbruch_grund": "Neustart von Home Assistant während des Tests",
                "modus_wiederhergestellt": False,
                "bereiche": {},
                "schritte": [],
            }
            await self._async_save(laeuft_seit=None)

    async def _async_save(self, *, laeuft_seit: str | None = None) -> None:
        if self._store is None:
            return
        try:
            await self._store.async_save({
                "bereiche": self._bereiche,
                "letztes": self._letztes,
                "laeuft_seit": laeuft_seit,
            })
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Diagnose: Speichern fehlgeschlagen: %s", err)

    # ------------------------------------------------------------------
    # Öffentliches API
    # ------------------------------------------------------------------
    @property
    def nie_vollstaendig(self) -> bool:
        """Lief noch nie ein Test, in dem jeder Schreibweg bestand?

        Für den Hinweis beim Einschalten: Steht ein Bereich auf Fehler oder
        wurde nie geprüft, ist das ein Grund, vorher (erneut) zu testen.
        """
        for bereich in BEREICHE:
            eintrag = self._bereiche.get(bereich)
            if eintrag is None:
                return True
            ergebnis = eintrag.get("ergebnis")
            if ergebnis in (FEHLER, UNKLAR, ABGEBROCHEN):
                return True
            # Übersprungen, weil die Lage nicht passte (Ladestand, Tageszeit):
            # nie geprüft — ein übersprungener Bereich überschreibt kein
            # echtes Ergebnis, steht er hier, gab es nie eins. Fehlt dem
            # Treiber die Fähigkeit, gibt es nichts zu prüfen.
            if ergebnis == UEBERSPRUNGEN and not eintrag.get("ohne_faehigkeit"):
                return True
        return False

    def status(self, *, mit_protokoll: bool = False) -> dict[str, Any]:
        """Zustand für das Panel (Live während des Laufs)."""
        aus: dict[str, Any] = {
            "laeuft": self.laeuft,
            "bereiche": {
                b: {"titel": BEREICH_TITEL[b], **(self._bereiche.get(b) or {})}
                for b in BEREICHE
            },
            "reihenfolge": list(BEREICHE),
            "nie_vollstaendig": self.nie_vollstaendig,
        }
        if self.laeuft and self._protokoll is not None:
            aus["lauf"] = self._live_auszug(self._protokoll)
            aus["schritt"] = self._schritt_auszug(self._schritt)
            aus["probe"] = self._letzte_probe
        letztes = self._letztes
        if letztes is not None:
            aus["letztes"] = (
                letztes if mit_protokoll else self._live_auszug(letztes)
            )
        return aus

    def _live_auszug(self, protokoll: dict) -> dict:
        """Kopf und Schrittliste ohne Messreihen — das Panel fragt alle 2 s."""
        return {
            "gestartet": protokoll.get("gestartet"),
            "beendet": protokoll.get("beendet"),
            "ergebnis": protokoll.get("ergebnis"),
            "abbruch_grund": protokoll.get("abbruch_grund"),
            "modus_vorher": protokoll.get("modus_vorher"),
            "modus_wiederhergestellt": protokoll.get("modus_wiederhergestellt"),
            "bereiche": protokoll.get("bereiche") or {},
            "schritte": [
                self._schritt_auszug(s) for s in protokoll.get("schritte") or []
            ],
        }

    @staticmethod
    def _schritt_auszug(schritt: dict | None) -> dict | None:
        if schritt is None:
            return None
        return {k: v for k, v in schritt.items() if k not in ("proben", "stellwerte")}

    def async_start(self) -> tuple[bool, str]:
        """Test starten; ``(False, Grund)``, wenn es nicht geht."""
        if self.laeuft:
            return False, "Die Diagnose läuft bereits."
        if self._data.get("stopping"):
            return False, "Die Integration wird gerade gestoppt."
        inverter = self._data.get("inverter")
        if inverter is None or not getattr(inverter, "supports_schedule_control", False):
            return False, "Dieser Wechselrichter wird nicht gesteuert — es gibt nichts zu prüfen."
        if self._data.get("executor") is None:
            return False, "Die Steuerung ist noch nicht bereit."
        self.laeuft = True
        self._abbruch_grund = None
        coro = self._lauf()
        try:
            self._task = self._hass.async_create_background_task(
                coro, "eeg_optimizer_diagnose"
            )
        except AttributeError:  # pragma: no cover — ältere HA / Test
            self._task = asyncio.get_running_loop().create_task(coro)
        return True, "Diagnose gestartet."

    def abbrechen(self, grund: str = "Vom Nutzer abgebrochen") -> bool:
        """Laufenden Test abbrechen — er gibt danach selbst frei."""
        if not self.laeuft:
            return False
        if self._abbruch_grund is None:
            self._abbruch_grund = grund
        return True

    async def async_stilllegen(self, timeout_s: float = 20.0) -> None:
        """Integration wird entladen: abbrechen und auf die Freigabe warten.

        ``asyncio.wait`` statt ``wait_for``: Es wirft nie — auch nicht, wenn
        HA den Task beim Herunterfahren abgebrochen hat (``CancelledError``
        ist keine ``Exception`` und risse sonst ``_stilllegen`` mit, samt
        Freigabe, Heizstab und Wallbox dahinter). Gewartet wird auch auf die
        abgeschirmte Freigabe, die nach einem Abbruch weiterläuft.
        """
        if not self.laeuft or self._task is None:
            return
        self.abbrechen("Integration wird entladen")
        offen = {t for t in (self._task, self._abschluss_task) if t is not None}
        _, haengt = await asyncio.wait(offen, timeout=timeout_s)
        if self._abschluss_task is not None and self._abschluss_task not in offen:
            _, haengt2 = await asyncio.wait({self._abschluss_task}, timeout=timeout_s)
            haengt = haengt | haengt2
        if haengt:
            # Hängt in einem Treiberaufruf (Sigenergy bis 60 s). Der Befehl
            # kann nach der Freigabe des Entladens ankommen; der Executor
            # weiß von nichts (``hat_eingriff`` False → keine Freigabe beim
            # HA-Stopp). Also als Fremdeingriff markieren und die zweite
            # Freigabe nach dem offenen Lauf anfordern.
            _LOGGER.warning("Diagnose: nach %.0f s nicht beendet", timeout_s)
            executor = self._data.get("executor")
            nach = getattr(executor, "nach_fremdeingriff", None)
            if nach is not None:
                nach()
            self._data["guard_lauf_offen"] = True

    # ------------------------------------------------------------------
    # Hilfen
    # ------------------------------------------------------------------
    @property
    def _config(self) -> dict:
        return self._data.get("config") or {}

    @property
    def _inverter(self) -> Any:
        return self._data.get("inverter")

    def _modus(self) -> str | None:
        select = self._data.get("select")
        return getattr(select, "_attr_current_option", None) if select else None

    async def _modus_setzen(self, modus: str) -> None:
        select = self._data.get("select")
        if select is None:
            return
        try:
            await select.async_select_option(modus)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Diagnose: Modus %s nicht setzbar", modus, exc_info=True)

    def _messen(self) -> dict:
        """Eine Probe: SOC, PV, Batterie (+ = laden), Netz (+ = Einspeisung), Haus."""
        if self._messen_fn is not None:
            probe = dict(self._messen_fn())
        else:
            from .power_readings import (
                compute_battery_now_kw,
                compute_grid_export_kw,
                compute_house_load_kw,
                compute_pv_now_kw,
            )
            hass, cfg = self._hass, self._config
            probe = {
                "soc": self._soc(),
                "pv": compute_pv_now_kw(hass, cfg),
                "batt": compute_battery_now_kw(hass, cfg),
                "netz": compute_grid_export_kw(hass, cfg),
                "haus": compute_house_load_kw(hass, cfg),
            }
        probe = {k: _r(v) for k, v in probe.items()}
        probe["t"] = round(self._uhr() - self._start_mono, 1)
        return probe

    def _soc(self) -> float | None:
        inverter = self._inverter
        try:
            kombiniert, _ = inverter.get_combined_battery_state()
        except Exception:  # noqa: BLE001
            kombiniert = None
        if isinstance(kombiniert, (int, float)) and math.isfinite(kombiniert):
            return float(kombiniert)
        entity = self._config.get(CONF_BATTERY_SOC_SENSOR)
        if not entity:
            return None
        state = self._hass.states.get(entity)
        if state is None:
            return None
        try:
            wert = float(state.state)
        except (TypeError, ValueError):
            return None
        return wert if math.isfinite(wert) else None

    def _min_soc(self) -> float:
        try:
            basis = float(self._config.get("schedule_min_soc_pct", 10.0) or 0.0)
        except (TypeError, ValueError):
            basis = 10.0
        try:
            geraet = self._inverter.get_backup_reserve_soc_pct()
        except Exception:  # noqa: BLE001
            geraet = None
        if isinstance(geraet, (int, float)) and math.isfinite(geraet):
            basis = max(basis, float(geraet))
        return basis

    def _max_soc(self) -> float:
        try:
            wert = float(self._config.get("schedule_max_soc_pct", 100.0) or 100.0)
        except (TypeError, ValueError):
            wert = 100.0
        return min(max(wert, 50.0), 100.0)

    def _entlade_max_kw(self) -> float | None:
        try:
            cap = self._inverter.get_max_discharge_power_kw()
        except Exception:  # noqa: BLE001
            cap = None
        if not cap:
            try:
                cap = float(self._config.get(CONF_DISCHARGE_POWER_KW) or 0.0) or None
            except (TypeError, ValueError):
                cap = None
        return cap

    def _einspeise_grenze_kw(self) -> tuple[float | None, str]:
        """Wie viel die Anlage höchstens einspeisen kann — Grenze und Name."""
        cfg = self._config
        kandidaten = []
        if cfg.get("grid_export_limit_enabled"):
            try:
                kandidaten.append((float(cfg.get("grid_export_limit_kw")), "Einspeisegrenze"))
            except (TypeError, ValueError):
                pass
        try:
            ac = float(cfg.get("inverter_ac_limit_kw") or 0.0)
        except (TypeError, ValueError):
            ac = 0.0
        if ac > 0:
            kandidaten.append((ac, "Wechselrichter-Leistung"))
        kandidaten = [(k, n) for k, n in kandidaten if math.isfinite(k) and k > 0]
        if not kandidaten:
            return None, ""
        return min(kandidaten)

    def _netzladung_grenze_kw(self, basis: dict) -> tuple[float, str | None]:
        """Höchste Testleistung der Netzladung und was sie begrenzt."""
        kw = TEST_KW
        grund = None
        haus = basis.get("haus") or 0.0
        pv = basis.get("pv") or 0.0
        cfg = self._config
        if cfg.get("spitzenkappung_enabled"):
            try:
                from .spitzenkappung import grenze_aus_config
                luft = grenze_aus_config(cfg) - NETZ_SICHERHEIT_KW - haus + pv
            except Exception:  # noqa: BLE001
                luft = kw
            if luft < kw:
                kw, grund = luft, "Spitzenkappungs-Grenze"
        try:
            meter = float(cfg.get("schedule_netzladen_max_kw") or 0.0)
        except (TypeError, ValueError):
            meter = 0.0
        if meter > 0:
            luft = meter - haus + pv
            if luft < kw:
                kw, grund = luft, "Netzgrenze fürs Laden"
        return kw, grund

    def _pruefen_abbruch(self) -> None:
        if self._abbruch_grund is not None:
            raise _Abbruch(self._abbruch_grund)
        if self._data.get("stopping"):
            raise _Abbruch("Integration wird entladen")
        if self._modus() == MODE_EIN:
            raise _Abbruch("Der Modus wurde während des Tests auf Ein gestellt")
        if self._uhr() - self._start_mono > GESAMT_MAX_S:
            raise _Abbruch("Zeitlimit des Tests überschritten")

    async def _warten(self, sekunden: float) -> None:
        """Schlafen in kleinen Stücken, damit ein Abbruch schnell greift."""
        ende = self._uhr() + sekunden
        while True:
            self._pruefen_abbruch()
            rest = ende - self._uhr()
            if rest <= 0:
                return
            await self._schlafen(min(rest, 1.0))

    async def _proben_sammeln(self, sekunden: float) -> list[dict]:
        proben = []
        ende = self._uhr() + sekunden
        while True:
            self._pruefen_abbruch()
            probe = self._messen()
            self._letzte_probe = probe
            proben.append(probe)
            if self._uhr() >= ende:
                return proben
            await self._warten(min(PROBE_S, max(ende - self._uhr(), 0.0)))

    async def _stellwerte(self) -> list[dict]:
        """Was der Treiber an Stellgrößen zeigt — roh, für das Protokoll."""
        zeilen: list[dict] = []
        inverter = self._inverter
        try:
            for z in await inverter.async_get_control_values() or []:
                if isinstance(z, dict):
                    zeilen.append({
                        "label": z.get("label"), "role": z.get("role"),
                        "value": _r(z.get("value")) if not isinstance(z.get("value"), (dict, list)) else str(z.get("value")),
                        "unit": z.get("unit"),
                    })
        except Exception as err:  # noqa: BLE001
            zeilen.append({"label": "Stellwerte (Modbus)", "fehler": type(err).__name__})
        try:
            entitaeten = inverter.get_control_entities() or []
        except Exception as err:  # noqa: BLE001
            entitaeten = []
            zeilen.append({"label": "Stellwerte (Entitäten)", "fehler": type(err).__name__})
        for e in entitaeten:
            if not isinstance(e, dict):
                continue
            eid = e.get("entity_id")
            state = self._hass.states.get(eid) if eid else None
            attrs = getattr(state, "attributes", None) or {}
            zeilen.append({
                "label": e.get("label"), "role": e.get("role"), "entity_id": eid,
                "value": getattr(state, "state", None),
                "unit": attrs.get("unit_of_measurement") if hasattr(attrs, "get") else None,
            })
        return zeilen

    def _schritt_neu(self, bereich: str, titel: str, **felder: Any) -> dict:
        schritt = {
            "bereich": bereich,
            "titel": titel,
            "start": round(self._uhr() - self._start_mono, 1),
            "ende": None,
            "befehl": None,
            "befehl_ok": None,
            "fehler": None,
            "dauer_ms": None,
            "ruecklesen": None,
            "ruecklesen_wert": None,
            "voraussetzung": None,
            "erwartung": None,
            "wirkung": None,
            "ergebnis": None,
            "text": None,
            "proben": [],
            **felder,
        }
        self._schritt = schritt
        assert self._protokoll is not None
        self._protokoll["schritte"].append(schritt)
        return schritt

    def _schritt_ende(self, schritt: dict, ergebnis: str | None = None, text: str | None = None) -> None:
        if ergebnis is None:
            ergebnis = schritt_ergebnis(
                schritt["befehl_ok"], schritt["ruecklesen"], schritt["wirkung"]
            )
        schritt["ergebnis"] = ergebnis
        if text is not None:
            schritt["text"] = text
        elif schritt["text"] is None:
            schritt["text"] = self._standardtext(schritt)
            if schritt.get("stopp"):
                schritt["text"] += f" — beendet: {schritt['stopp']}"
        schritt["ende"] = round(self._uhr() - self._start_mono, 1)
        _LOGGER.info(
            "Diagnose: %s — %s (%s)", schritt["titel"], ergebnis, schritt["text"]
        )

    def _pv_einbruch_pruefen(self, schritt: dict) -> None:
        """Nach einem Zwangsmodus: Hat das Gerät dabei die PV abgeregelt?

        Dann lässt sich der Befehl bei Sonne nicht sauber prüfen: Die
        Einspeisung steigt nicht, weil die PV fehlt, obwohl die Batterie
        liefert. Kein Fehler — zwangsentladen wird nachts, und dort gibt es
        nichts abzuregeln —, sondern „teilweise" mit dem Hinweis, nachts zu
        wiederholen. Ein „teilweise" überschreibt kein früheres „ok".
        Sigenergy „ESS First", 05.11.2026: Batterie exakt 1 kW, PV 17,9 → 0.
        """
        einbruch = pv_einbruch((schritt.get("basis") or {}).get("pv"), schritt.get("proben") or [])
        if einbruch is None:
            return
        vorher, nachher = einbruch
        schritt["pv_eingebrochen"] = {"vorher_kw": vorher, "nachher_kw": nachher}
        schritt["wirkung"] = NICHT_PRUEFBAR
        proben = schritt.get("proben") or []
        batt = median([x.get("batt") for x in proben[-FOLGE_OK:] if x.get("batt") is not None])
        batt_text = (
            f"die Batterie {'entlädt' if batt < 0 else 'lädt'} {_kw_text(abs(batt))} kW, "
            if batt is not None else ""
        )
        schritt["voraussetzung"] = "der Wechselrichter regelt in diesem Modus die PV ab"
        schritt["text"] = (
            f"Befehl angenommen, {batt_text}aber das Gerät regelt dabei die PV ab "
            f"({_kw_text(vorher)} → {_kw_text(nachher)} kW) — bei Sonne nicht sauber "
            "prüfbar, nachts wiederholen"
        )

    @staticmethod
    def _standardtext(schritt: dict) -> str:
        if schritt["befehl_ok"] is False:
            grund = schritt.get("fehler") or "ohne Angabe"
            return f"Befehl abgelehnt: {grund}"
        if schritt["ruecklesen"] == "abweichung":
            return (
                f"Befehl angenommen, aber zurückgelesen {schritt.get('ruecklesen_wert')} "
                "statt des gesetzten Werts"
            )
        wirkung = schritt.get("wirkung")
        if wirkung == NICHT_ERFUELLT:
            # Was stattdessen gemessen wurde — „keine Wirkung" allein sagt
            # nicht, ob nichts geschah oder das Falsche.
            proben = [x for x in schritt.get("proben") or [] if isinstance(x, dict)][-FOLGE_OK:]
            batt = median([x.get("batt") for x in proben if x.get("batt") is not None])
            netz = median([x.get("netz") for x in proben if x.get("netz") is not None])
            if batt is not None and netz is not None:
                return (
                    "Befehl angenommen, aber nicht die erwartete Wirkung — gemessen: "
                    f"Batterie {_kw_text(batt)} kW, Netz {_kw_text(netz)} kW"
                    f"{' (erwartet: ' + schritt['erwartung'] + ')' if schritt.get('erwartung') else ''}"
                )
            return "Befehl angenommen, aber keine Wirkung gemessen"
        if wirkung == UNEINDEUTIG:
            return "Messung uneindeutig — bitte wiederholen"
        if wirkung == NICHT_PRUEFBAR or wirkung is None:
            v = schritt.get("voraussetzung")
            return f"Befehl angenommen, Wirkung nicht prüfbar ({v})" if v else (
                "Befehl angenommen, Wirkung nicht prüfbar"
            )
        return "Befehl angenommen und wirksam"

    async def _befehl(self, schritt: dict, name: str, aufruf: Callable[[], Awaitable[bool]], **args: Any) -> bool:
        """Treiberaufruf mit Zeitmessung; Ausnahme zählt als abgelehnt."""
        schritt["befehl"] = {"name": name, **{k: _r(v) for k, v in args.items()}}
        inverter = self._inverter
        try:
            inverter.last_write_error = None
        except Exception:  # noqa: BLE001
            pass
        t0 = time.perf_counter()
        try:
            ok = bool(await aufruf())
        except Exception as err:  # noqa: BLE001 — gehört ins Protokoll, nicht in den Log-Spam
            ok = False
            schritt["fehler"] = f"{type(err).__name__}: {err}"[:200]
            # Für die Telemetrie nur der Typ — Modbus-Meldungen tragen oft
            # Host und Port (fuer_telemetrie).
            schritt["ausnahme"] = type(err).__name__
            _LOGGER.warning("Diagnose: %s warf %s", name, err)
        schritt["dauer_ms"] = int((time.perf_counter() - t0) * 1000)
        schritt["befehl_ok"] = ok
        grund = getattr(inverter, "last_write_error", None)
        if not ok and schritt["fehler"] is None:
            schritt["fehler"] = grund or "Treiber meldet Fehlschlag"
        elif ok and grund:
            # Teilerfolg (Victron ``_erfolg_ausser``, Sigenergy
            # ``_teilerfolg``): angenommen, aber mit Grund — erklärt oft das
            # „wirkungslos", das danach kommt.
            schritt["hinweis"] = str(grund)[:200]
        return ok

    async def _beobachten(
        self,
        schritt: dict,
        pruef: Callable[[dict], bool | None],
        *,
        stopp: Callable[[dict], str | None] | None = None,
    ) -> str:
        """Messen, bis die Wirkung stabil erfüllt ist oder die Zeit um ist."""
        werte: list[bool | None] = []
        start = self._uhr()
        while True:
            self._pruefen_abbruch()
            probe = self._messen()
            ok = pruef(probe)
            probe["ok"] = ok
            werte.append(ok)
            schritt["proben"].append(probe)
            self._letzte_probe = probe
            if stopp is not None:
                grund = stopp(probe)
                if grund:
                    schritt["stopp"] = grund
                    break
            vergangen = self._uhr() - start
            if frueh_fertig(werte, vergangen):
                break
            if vergangen >= BEOBACHTUNG_MAX_S:
                break
            await self._warten(PROBE_S)
        wirkung = urteil(werte)
        schritt["wirkung"] = wirkung
        return wirkung

    async def _ruhe(self) -> dict:
        """Automatik einschwingen lassen, Ausgangszustand messen."""
        proben = await self._proben_sammeln(RUHE_S)
        return basis_aus_proben(proben[len(proben) // 2:] or proben)

    async def _freigeben(self, nach: str, pruef: Callable[[dict], bool | None], pruefbar: str | None) -> None:
        """``async_stop_forcible`` und prüfen, dass die Automatik zurück ist."""
        inverter = self._inverter
        s = self._schritt_neu("freigabe", f"Freigabe nach {nach}")
        if not await self._befehl(s, "async_stop_forcible", inverter.async_stop_forcible):
            self._schritt_ende(s)
            return
        # Nach der Freigabe sollte das Ladelimit wieder am Maximum stehen —
        # roh ins Protokoll, bewertet wird es nicht (das Maximum kennt nicht
        # jeder Treiber). Gewartet wird wie beim Rücklesen: Die Entität des
        # Huawei meldete den zurückgesetzten Wert erst Sekunden später
        # (Traun 05.11.2026: direkt danach 1 kW, zwei Minuten später 5 kW).
        maximum = self._sicher(inverter.get_charge_limit_max_kw)
        wert = await self._lies_ladelimit()
        if isinstance(maximum, (int, float)) and maximum > 0:
            ende = self._uhr() + RUECKLESEN_WARTEN_S
            while ruecklesen_bewerten(float(maximum), wert) == "abweichung" and self._uhr() < ende:
                await self._warten(2.5)
                wert = await self._lies_ladelimit()
        s["ruecklesen_wert"] = _r(wert)
        if pruefbar is None and getattr(inverter, "batterie_gesperrt", False) is True:
            # Kostal: Die Batterie steht bis zum Watchdog-Rückfall — „keine
            # Zwangsentladung mehr" wäre geschenkt.
            pruefbar = "der Wechselrichter hält die Batterie bis zum Watchdog-Rückfall"
        if pruefbar is None:
            await self._beobachten(s, pruef)
        else:
            s["voraussetzung"] = pruefbar
            s["wirkung"] = NICHT_PRUEFBAR
            await self._warten(RUHE_S)
        self._schritt_ende(s)

    # ------------------------------------------------------------------
    # Der Lauf
    # ------------------------------------------------------------------
    async def _lauf(self) -> None:
        self._start_mono = self._uhr()
        gestartet = _jetzt().isoformat()
        cfg = self._config
        inverter = self._inverter
        modus_vorher = self._modus()
        try:
            from . import _cached_app_version  # type: ignore[attr-defined]
            app_version = _cached_app_version()
        except Exception:  # noqa: BLE001
            app_version = None
        try:
            from .schedule_archive import einstellungen_filtern
            einstellungen = einstellungen_filtern(cfg)
        except Exception:  # noqa: BLE001
            einstellungen = {}
        self._protokoll = {
            "version": PROTOKOLL_VERSION,
            "app_version": app_version,
            "gestartet": gestartet,
            "beendet": None,
            "ergebnis": None,
            "abbruch_grund": None,
            "modus_vorher": modus_vorher,
            "modus_wiederhergestellt": None,
            "treiber": cfg.get(CONF_INVERTER_TYPE),
            "faehigkeiten": {
                "forced_discharge": bool(getattr(inverter, "supports_forced_discharge", True)),
                "discharge_limit": bool(getattr(inverter, "supports_discharge_limit", False)),
                "grid_charge": bool(getattr(inverter, "supports_grid_charge", False)),
                "discharge_is_grid_setpoint": bool(getattr(inverter, "discharge_is_grid_setpoint", False)),
            },
            "grenzen": {},
            "einstellungen": einstellungen,
            "basis": None,
            "stellwerte_vorher": None,
            "stellwerte_nachher": None,
            "bereiche": {},
            "schritte": [],
        }
        await self._async_save(laeuft_seit=gestartet)
        self._von_ha_beendet = False
        try:
            await self._vorbereiten(modus_vorher)
            await self._ablauf()
        except asyncio.CancelledError:
            # HA bricht Hintergrund-Tasks beim Herunterfahren ab. Freigeben
            # muss trotzdem sein — unten, abgeschirmt — und danach geht der
            # Abbruch weiter, wie asyncio es verlangt.
            self._von_ha_beendet = True
            self._protokoll["abbruch_grund"] = "Home Assistant hat den Test beendet"
            if self._schritt is not None and self._schritt.get("ergebnis") is None:
                self._schritt_ende(self._schritt, ABGEBROCHEN, "Abgebrochen (Home Assistant beendet)")
        except _Abbruch as abbruch:
            self._protokoll["abbruch_grund"] = str(abbruch)
            if self._schritt is not None and self._schritt.get("ergebnis") is None:
                self._schritt_ende(self._schritt, ABGEBROCHEN, f"Abgebrochen: {abbruch}")
        except Exception as err:  # noqa: BLE001 — ein Fehler im Test darf nichts stehen lassen
            _LOGGER.exception("Diagnose: unerwarteter Fehler")
            self._protokoll["abbruch_grund"] = f"Interner Fehler: {type(err).__name__}: {err}"[:300]
            self._protokoll["abbruch_ausnahme"] = type(err).__name__
            if self._schritt is not None and self._schritt.get("ergebnis") is None:
                self._schritt_ende(self._schritt, ABGEBROCHEN, "Abgebrochen (interner Fehler)")
        # Abschluss als eigener Task hinter ``shield``: Ein (weiterer)
        # Abbruch dieses Tasks unterbricht dann nur das Warten, nicht die
        # Freigabe selbst. ``async_stilllegen`` wartet auf beide.
        self._abschluss_task = asyncio.ensure_future(
            self._abschluss_und_beenden(modus_vorher)
        )
        try:
            await asyncio.shield(self._abschluss_task)
        except asyncio.CancelledError:
            # Die Freigabe läuft weiter; ``_beenden`` liest das Merkmal erst
            # nach ihr und stellt dann keinen Modus zurück.
            self._von_ha_beendet = True
        if self._von_ha_beendet:
            raise asyncio.CancelledError

    async def _abschluss_und_beenden(self, modus_vorher: str | None) -> None:
        freigabe_ok = await self._abschluss()
        await self._beenden(modus_vorher, freigabe_ok)

    async def _vorbereiten(self, modus_vorher: str | None) -> None:
        """Modus Aus, Executor still, Ausgangslage freigeben."""
        assert self._protokoll is not None
        if modus_vorher == MODE_EIN:
            await self._modus_setzen(MODE_AUS)
        lauf = self._data.get("_run_cycle")
        if lauf is not None:
            # Ein Guard-Lauf im Modus Aus: Der Executor nimmt zurück, der
            # Heizstab geht auf 0. Er wartet auf einen laufenden Lauf.
            try:
                await lauf()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Diagnose: Guard-Lauf vor dem Test fehlgeschlagen", exc_info=True)
        # Ab hier überspringt der Guard-Lauf den Executor — gesetzt ohne
        # await dazwischen, also bevor ein Timer-Lauf dazwischenkommt.
        self._data["diagnose_laeuft"] = True
        self._pruefen_abbruch()

    async def _ablauf(self) -> None:
        assert self._protokoll is not None
        p = self._protokoll
        inverter = self._inverter

        # --- Vorprüfung --------------------------------------------------
        s = self._schritt_neu("vorpruefung", "Wechselrichter erreichbar")
        s["befehl_ok"] = bool(getattr(inverter, "is_available", False))
        if not s["befehl_ok"]:
            s["fehler"] = "Treiber meldet: nicht verfügbar"
            self._schritt_ende(s, FEHLER, "Wechselrichter nicht erreichbar")
            raise _Abbruch("Wechselrichter nicht erreichbar")
        self._schritt_ende(s, OK, "Treiber meldet verfügbar")

        s = self._schritt_neu("vorpruefung", "Messwerte lesbar")
        probe = self._messen()
        s["proben"].append(probe)
        fehlend = [n for k, n in (("soc", "Ladestand"), ("batt", "Batterieleistung"), ("netz", "Netzleistung")) if probe.get(k) is None]
        if fehlend:
            s["befehl_ok"] = False
            s["fehler"] = "nicht lesbar: " + ", ".join(fehlend)
            self._schritt_ende(s, FEHLER, s["fehler"] + " — ohne sie lässt sich keine Wirkung prüfen")
            raise _Abbruch("Messwerte nicht lesbar: " + ", ".join(fehlend))
        s["befehl_ok"] = True
        self._schritt_ende(s, OK, "Ladestand, Batterie- und Netzleistung lesbar")

        s = self._schritt_neu("vorpruefung", "Stellwerte lesbar")
        p["stellwerte_vorher"] = await self._stellwerte()
        s["stellwerte"] = p["stellwerte_vorher"]
        lesefehler = [z for z in p["stellwerte_vorher"] if z.get("fehler")]
        s["befehl_ok"] = True
        try:
            limit = await inverter.async_get_charge_limit_kw()
        except Exception:  # noqa: BLE001
            limit = None
        s["ruecklesen_wert"] = _r(limit)
        p["grenzen"] = {
            "min_soc_pct": _r(self._min_soc()),
            "max_soc_pct": _r(self._max_soc()),
            "entlade_max_kw": _r(self._entlade_max_kw()),
            "ladelimit_max_kw": _r(self._sicher(inverter.get_charge_limit_max_kw)),
            "ladelimit_jetzt_kw": _r(limit),
        }
        if lesefehler:
            self._schritt_ende(s, TEILWEISE, "Ein Teil der Stellwerte ist nicht lesbar")
        else:
            self._schritt_ende(s, OK, f"{len(p['stellwerte_vorher'])} Stellwerte gelesen")

        # Ausgangslage: alles zurück auf Automatik, dann Basis messen.
        s = self._schritt_neu("freigabe", "Ausgangslage: Automatik herstellen", neutral=True)
        if await self._befehl(s, "async_stop_forcible", inverter.async_stop_forcible):
            self._schritt_ende(s, OK, "Automatik hergestellt (Ausgangslage, ohne Erwartung)")
        else:
            self._schritt_ende(s)

        basis_proben = await self._proben_sammeln(BASIS_S)
        p["basis"] = basis_aus_proben(basis_proben)
        p["basis_proben"] = basis_proben

        await self._bereich_ladelimit()
        await self._bereich_entladung()
        await self._bereich_deckel()
        await self._bereich_netzladung()

    @staticmethod
    def _sicher(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return None

    async def _bereich_ladelimit(self) -> None:
        inverter = self._inverter
        if (gesperrt := await self._warten_bis_frei("ladelimit")) is not None:
            s = self._schritt_neu("ladelimit", "Ladelimit")
            self._schritt_ende(s, UEBERSPRUNGEN, gesperrt)
            return
        basis = await self._ruhe()
        ladung = basis.get("batt") or 0.0
        soc = basis.get("soc")
        voll = soc is not None and soc >= self._max_soc() - 1
        pruefbar = ladung >= LADEN_MIN_KW and not voll
        if pruefbar:
            grund = None
        elif voll:
            grund = (
                f"die Batterie ist voll ({soc:.0f} %) und lädt nicht — "
                "bei niedrigerem Ladestand mit PV-Überschuss wiederholen"
            )
        else:
            grund = (
                f"die Batterie lädt gerade nicht (≈{_kw_text(ladung)} kW) — "
                "mit PV-Überschuss wiederholen"
            )

        # Ladelimit 0
        s = self._schritt_neu("ladelimit", "Ladelimit 0 kW (Laden sperren)", basis=basis)
        if await self._befehl(s, "async_set_charge_limit", lambda: inverter.async_set_charge_limit(0.0), power_kw=0.0):
            await self._ruecklesen_ladelimit(s, 0.0)
            s["erwartung"] = f"Batterie lädt ≤ {LIMIT_TOLERANZ_KW:.2f} kW"
            if pruefbar:
                await self._beobachten(s, pruef_laden_hoechstens(0.0))
            else:
                s["voraussetzung"] = grund
                s["wirkung"] = NICHT_PRUEFBAR
                await self._warten(5.0)
        self._schritt_ende(s)

        # Ladelimit 1 kW — ein Wert ungleich 0 geht bei manchen Treibern
        # einen anderen Weg (Strom statt Freigabe, Skalierung).
        ziel = TEST_LADELIMIT_KW
        pruefbar_1 = pruefbar and ladung >= ziel + LADEN_MIN_KW
        s = self._schritt_neu("ladelimit", f"Ladelimit {ziel:.1f} kW", basis=basis)
        if await self._befehl(s, "async_set_charge_limit", lambda: inverter.async_set_charge_limit(ziel), power_kw=ziel):
            await self._ruecklesen_ladelimit(s, ziel)
            s["erwartung"] = (
                f"Batterie lädt {ziel - GUARD_WIRKUNG_MIN_LUECKE_KW:.2f}–"
                f"{ziel + LIMIT_TOLERANZ_KW:.2f} kW"
            )
            if pruefbar_1:
                await self._beobachten(s, pruef_laden_bei(ziel))
            else:
                s["voraussetzung"] = grund or (
                    f"die Batterie lädt nur ≈{ladung:.2f} kW, ein Limit von {ziel:.1f} kW "
                    "wäre nicht zu sehen"
                )
                s["wirkung"] = NICHT_PRUEFBAR
                await self._warten(5.0)
        self._schritt_ende(s)

        # Freigabe: Laden kommt zurück.
        # Zuletzt stand das 1-kW-Limit; sichtbar ist die Freigabe nur, wenn
        # die Batterie vorher deutlich mehr lud.
        await self._freigeben(
            "Ladelimit",
            pruef_laden_ueber(ziel, ladung),
            None if pruefbar_1 and s.get("wirkung") == ERFUELLT else (
                grund
                or (f"die Batterie lud vorher nur ≈{ladung:.2f} kW" if not pruefbar_1
                    else "das 1-kW-Limit wirkte nicht — eine Rücknahme ist nicht zu sehen")
            ),
        )

    async def _ruecklesen_ladelimit(self, schritt: dict, soll_kw: float) -> None:
        """Ladelimit zurücklesen und bewerten — bis ``RUECKLESEN_WARTEN_S``.

        Victron meldet den gesetzten DVCC-Strom über MQTT verzögert zurück;
        sofort gelesen stünde dort noch der alte Wert. Ein Treiber, der nur
        seinen eigenen letzten Befehl zurückgibt (SMA,
        ``ladelimit_vom_geraet_gelesen`` False), beweist nichts — dort gilt
        der Wert als nicht lesbar und steht nur zur Information im Protokoll.
        """
        wert = await self._lies_ladelimit()
        if getattr(self._inverter, "ladelimit_vom_geraet_gelesen", True) is False:
            schritt["ruecklesen_wert"] = _r(wert)
            schritt["ruecklesen"] = "nicht_lesbar"
            schritt["ruecklesen_hinweis"] = "Treiber liest das Ladelimit nicht vom Gerät"
            return
        ende = self._uhr() + RUECKLESEN_WARTEN_S
        while ruecklesen_bewerten(soll_kw, wert) == "abweichung" and self._uhr() < ende:
            await self._warten(2.5)
            wert = await self._lies_ladelimit()
        schritt["ruecklesen_wert"] = _r(wert)
        schritt["ruecklesen"] = ruecklesen_bewerten(soll_kw, wert)

    async def _warten_bis_frei(self, bereich: str) -> str | None:
        """Kostal: warten, bis die Batterie nach einem 1034-Sollwert wieder frei ist.

        Nach einer Zwangsentladung, Netzladung oder auch nur einem Stopp hält
        der Kostal die Batterie bis zum Watchdog-Rückfall (bis 15 min) —
        jeder Befehl in dieser Zeit wird nur vorgemerkt, und die Batterie
        steht. Ohne Warten bestünde jede Freigabe und jede Deckelprüfung
        wäre „nicht prüfbar" mit der falschen Empfehlung. ``None`` = frei,
        sonst der Grund, warum der Bereich nicht prüfbar ist.
        """
        if getattr(self._inverter, "batterie_gesperrt", False) is not True:
            return None
        s = self._schritt_neu(bereich, "Warten auf den Watchdog-Rückfall des Wechselrichters", neutral=True)
        ende = self._uhr() + RUECKFALL_WARTEN_MAX_S
        while getattr(self._inverter, "batterie_gesperrt", False) is True and self._uhr() < ende:
            self._letzte_probe = self._messen()
            await self._warten(PROBE_S)
        frei = getattr(self._inverter, "batterie_gesperrt", False) is not True
        s["befehl_ok"] = True
        if frei:
            self._schritt_ende(s, OK, "Batterie wieder frei")
            return None
        grund = (
            f"der Wechselrichter hält die Batterie nach dem letzten Sollwert noch "
            f"fest (nach {RUECKFALL_WARTEN_MAX_S / 60:.0f} min kein Watchdog-Rückfall)"
        )
        self._schritt_ende(s, TEILWEISE, grund)
        return grund

    async def _lies_ladelimit(self) -> float | None:
        try:
            wert = await self._inverter.async_get_charge_limit_kw()
        except Exception:  # noqa: BLE001
            return None
        return float(wert) if isinstance(wert, (int, float)) and math.isfinite(wert) else None

    async def _bereich_entladung(self) -> None:
        inverter = self._inverter
        if getattr(inverter, "supports_forced_discharge", True) is False:
            s = self._schritt_neu("entladung", "Zwangsentladung")
            s["ohne_faehigkeit"] = True
            self._schritt_ende(s, UEBERSPRUNGEN, "Der Treiber kann nicht zwangsentladen")
            return
        if (gesperrt := await self._warten_bis_frei("entladung")) is not None:
            s = self._schritt_neu("entladung", "Zwangsentladung")
            self._schritt_ende(s, UEBERSPRUNGEN, gesperrt)
            return
        basis = await self._ruhe()
        soc = basis.get("soc")
        min_soc = self._min_soc()
        s = self._schritt_neu("entladung", f"Zwangsentladung {TEST_KW:.1f} kW", basis=basis)
        if soc is None or soc < min_soc + SOC_ABSTAND_PCT:
            s["voraussetzung"] = (
                f"Ladestand {soc if soc is not None else '?'} % zu nah am Mindest-Ladestand "
                f"{min_soc:.0f} % — bei vollerer Batterie wiederholen"
            )
            self._schritt_ende(s, UEBERSPRUNGEN, s["voraussetzung"])
            return
        # Sollwert so, dass TEST_KW zusätzlich ins Netz gehen: Ein Treiber
        # mit Netz-Sollwert bekommt die Einspeisung, die anderen die
        # Batterieleistung — Hausbedarf (was die Automatik schon liefert)
        # plus TEST_KW. Genau so rechnet Guard 2.
        basis_netz = basis.get("netz") or 0.0
        # Was die Batterie vorher lud, geht bei der Zwangsentladung zusätzlich
        # ins Netz (PV-Überschuss am Mittag).
        ladung_vorher = max(basis.get("batt") or 0.0, 0.0)
        bedarf = max((basis.get("haus") or 0.0) - (basis.get("pv") or 0.0), 0.0)
        netz_sollwert = bool(getattr(inverter, "discharge_is_grid_setpoint", False))
        cap = self._entlade_max_kw()
        if netz_sollwert:
            # Netz-Sollwert: so viel einspeisen, dass es nur MIT Entladung
            # geht — vorherige Einspeisung + was die Batterie lud + TEST_KW.
            kw = max(basis_netz, 0.0) + ladung_vorher + TEST_KW
            entladung = TEST_KW if not cap else min(TEST_KW, cap - bedarf)
        else:
            kw = bedarf + TEST_KW
            if cap:
                kw = min(kw, cap)
            entladung = kw - bedarf
        zuwachs = ladung_vorher + entladung
        kw = round(kw, 2)
        s["titel"] = f"Zwangsentladung (Einspeisung +{TEST_KW:.1f} kW)"
        if entladung < ENTLADEN_MIN_KW:
            s["voraussetzung"] = (
                f"das Haus braucht ≈{bedarf:.2f} kW, die Batterie schafft höchstens "
                f"{cap or 0:.2f} kW — zu wenig Rest, um eine Einspeisung zu sehen"
            )
            self._schritt_ende(s, UEBERSPRUNGEN, s["voraussetzung"])
            return
        grenze, grenze_name = self._einspeise_grenze_kw()
        if grenze is not None and basis_netz + zuwachs > grenze - NETZ_SICHERHEIT_KW:
            s["voraussetzung"] = (
                f"die Anlage speist schon ≈{basis_netz:.2f} kW ein, mit der Testleistung "
                f"wären es über der {grenze_name} von {grenze:.1f} kW — bei weniger "
                "PV-Überschuss wiederholen"
            )
            self._schritt_ende(s, UEBERSPRUNGEN, s["voraussetzung"])
            return
        ziel = round(max(min_soc, soc - SOC_SCHRITT_PCT))
        entladung_min = entladung * GUARD_WIRKUNG_MIN_ANTEIL
        pruef = pruef_einspeisung_steigt(basis_netz, zuwachs, entladung_min)
        s["erwartung"] = (
            f"Einspeisung steigt um ≥ {zuwachs * GUARD_WIRKUNG_MIN_ANTEIL:.2f} kW "
            f"(vorher {basis_netz:+.2f} kW am Netz), Batterie entlädt ≥ {entladung_min:.2f} kW"
        )

        def _stopp(p: dict) -> str | None:
            soc_jetzt = p.get("soc")
            if soc_jetzt is not None and soc_jetzt <= ziel:
                return f"Ziel-Ladestand {ziel} % erreicht"
            return None

        if await self._befehl(
            s, "async_set_discharge",
            lambda: inverter.async_set_discharge(kw, ziel), power_kw=kw, target_soc=ziel,
        ):
            await self._beobachten(s, pruef, stopp=_stopp)
            self._pv_einbruch_pruefen(s)
        self._schritt_ende(s)
        # Freigabe immer, auch nach einem abgelehnten Befehl — der kann halb
        # angekommen sein. Prüfbar ist sie nur, wenn die Entladung wirkte:
        # sonst gilt „keine Zwangsentladung mehr" schon vorher.
        await self._freigeben(
            "Zwangsentladung",
            pruef_keine_zwangsentladung,
            None if s.get("wirkung") == ERFUELLT else "die Zwangsentladung wirkte nicht nachweislich",
        )

    async def _bereich_deckel(self) -> None:
        inverter = self._inverter
        if not getattr(inverter, "supports_discharge_limit", False):
            s = self._schritt_neu("deckel", "Entladedeckel")
            s["ohne_faehigkeit"] = True
            self._schritt_ende(s, UEBERSPRUNGEN, "Der Treiber kann die Entladung nicht deckeln")
            return
        if (gesperrt := await self._warten_bis_frei("deckel")) is not None:
            s = self._schritt_neu("deckel", "Entladedeckel")
            self._schritt_ende(s, UEBERSPRUNGEN, gesperrt)
            return
        basis = await self._ruhe()
        entladung = max(-(basis.get("batt") or 0.0), 0.0)
        soc = basis.get("soc")
        min_soc = self._min_soc()
        leer = soc is not None and soc <= min_soc + 2
        pruefbar = entladung >= ENTLADEN_MIN_KW and not leer
        if pruefbar:
            grund = None
        elif leer:
            grund = (
                f"die Batterie steht am Mindest-Ladestand ({soc:.0f} %) — "
                "bei vollerer Batterie wiederholen"
            )
        else:
            grund = (
                f"die Batterie entlädt gerade nicht (≈{_kw_text(entladung)} kW), "
                "die PV deckt das Haus — abends wiederholen, wenn das Haus mehr "
                "braucht als die PV liefert"
            )
        pruefbar_t = False
        try:
            # Deckel 0 = halten
            s = self._schritt_neu("deckel", "Entladedeckel 0 kW (halten)", basis=basis)
            if await self._befehl(s, "async_set_discharge_limit", lambda: inverter.async_set_discharge_limit(0.0), power_kw=0.0):
                s["erwartung"] = f"Batterie entlädt ≤ {LIMIT_TOLERANZ_KW:.2f} kW"
                s["stellwerte"] = await self._stellwerte()
                if pruefbar:
                    await self._beobachten(s, pruef_entladen_hoechstens(0.0))
                else:
                    s["voraussetzung"] = grund
                    s["wirkung"] = NICHT_PRUEFBAR
                    await self._warten(5.0)
            self._schritt_ende(s)

            # Deckel 0,5 kW
            # Sichtbar ist der Deckel nur, wenn die Batterie vorher deutlich
            # mehr gab — und nur dann auch sein Aufheben (unten).
            pruefbar_t = pruefbar and entladung >= TEST_DECKEL_KW + LIMIT_TOLERANZ_KW + FREI_KW
            s = self._schritt_neu("deckel", f"Entladedeckel {TEST_DECKEL_KW:.1f} kW", basis=basis)
            if await self._befehl(s, "async_set_discharge_limit", lambda: inverter.async_set_discharge_limit(TEST_DECKEL_KW), power_kw=TEST_DECKEL_KW):
                s["erwartung"] = (
                    f"Batterie entlädt {TEST_DECKEL_KW - GUARD_WIRKUNG_MIN_LUECKE_KW:.2f}–"
                    f"{TEST_DECKEL_KW + LIMIT_TOLERANZ_KW:.2f} kW"
                )
                s["stellwerte"] = await self._stellwerte()
                if pruefbar_t:
                    await self._beobachten(s, pruef_entladen_bei(TEST_DECKEL_KW))
                else:
                    s["voraussetzung"] = grund or (
                        f"die Batterie entlädt nur ≈{entladung:.2f} kW, ein Deckel von "
                        f"{TEST_DECKEL_KW:.1f} kW wäre nicht zu sehen"
                    )
                    s["wirkung"] = NICHT_PRUEFBAR
                    await self._warten(5.0)
            self._schritt_ende(s)
        finally:
            # Aufheben — auch nach einem Abbruch (dann ohne Beobachtung).
            # Ein abgebrochener Schritt wird vorher abgeschlossen, sonst
            # bliebe er ohne Ergebnis, sobald der neue ihn ablöst.
            offen = self._schritt
            if offen is not None and offen.get("ergebnis") is None:
                self._schritt_ende(offen, ABGEBROCHEN, "Abgebrochen")
            s = self._schritt_neu("deckel", "Entladedeckel aufheben", basis=basis)
            if await self._befehl(s, "async_set_discharge_limit", lambda: inverter.async_set_discharge_limit(None), power_kw=None):
                s["stellwerte"] = await self._stellwerte()
                if self._abbruch_grund is None and not self._data.get("stopping"):
                    if pruefbar_t:
                        # Zuletzt stand der 0,5-kW-Deckel: erst mehr als er
                        # erlaubte zeigt, dass er weg ist.
                        s["erwartung"] = (
                            f"Batterie entlädt wieder > {TEST_DECKEL_KW + LIMIT_TOLERANZ_KW + FREI_KW:.2f} kW"
                        )
                        await self._beobachten(
                            s, pruef_entladen_ueber(TEST_DECKEL_KW + LIMIT_TOLERANZ_KW, entladung)
                        )
                    else:
                        s["voraussetzung"] = grund or (
                            f"die Batterie entlud vorher nur ≈{entladung:.2f} kW — "
                            "neben dem 0,5-kW-Deckel ist das Aufheben nicht zu sehen"
                        )
                        s["wirkung"] = NICHT_PRUEFBAR
                else:
                    s["voraussetzung"] = "Test abgebrochen"
                    s["wirkung"] = NICHT_PRUEFBAR
            self._schritt_ende(s)

    async def _bereich_netzladung(self) -> None:
        inverter = self._inverter
        if not getattr(inverter, "supports_grid_charge", False):
            s = self._schritt_neu("netzladung", "Netzladung")
            s["ohne_faehigkeit"] = True
            self._schritt_ende(s, UEBERSPRUNGEN, "Der Treiber kann nicht aus dem Netz laden")
            return
        if (gesperrt := await self._warten_bis_frei("netzladung")) is not None:
            s = self._schritt_neu("netzladung", "Netzladung")
            self._schritt_ende(s, UEBERSPRUNGEN, gesperrt)
            return
        basis = await self._ruhe()
        soc = basis.get("soc")
        max_soc = self._max_soc()
        s = self._schritt_neu("netzladung", f"Netzladung {TEST_KW:.1f} kW", basis=basis)
        if soc is None or soc > max_soc - SOC_ABSTAND_PCT:
            s["voraussetzung"] = (
                f"Ladestand {soc if soc is not None else '?'} % zu nah am Höchst-Ladestand "
                f"{max_soc:.0f} % — bei leererer Batterie wiederholen"
            )
            self._schritt_ende(s, UEBERSPRUNGEN, s["voraussetzung"])
            return
        kw, begrenzt = self._netzladung_grenze_kw(basis)
        if kw < LADEN_MIN_KW:
            s["voraussetzung"] = (
                f"zu wenig Luft bis zur {begrenzt} ({kw:.2f} kW) — "
                "bei geringerem Hausverbrauch wiederholen"
            )
            self._schritt_ende(s, UEBERSPRUNGEN, s["voraussetzung"])
            return
        kw = round(kw, 2)
        ziel = round(min(max_soc, soc + SOC_SCHRITT_PCT))
        ladung_vorher = basis.get("batt") or 0.0
        # Lädt die Batterie schon aus PV-Überschuss, ist eine Netzladung
        # davon nicht zu unterscheiden.
        pruefbar = ladung_vorher < FREI_KW
        s["erwartung"] = f"Batterie lädt ≥ {kw * GUARD_WIRKUNG_MIN_ANTEIL:.2f} kW"
        if begrenzt:
            s["begrenzt_durch"] = begrenzt
        haus = basis.get("haus") or 0.0
        pv = basis.get("pv") or 0.0
        bezug_grenze = max(haus - pv, 0.0) + kw + NETZBEZUG_ABBRUCH_KW

        def _stopp(p: dict) -> str | None:
            netz = p.get("netz")
            if netz is not None and -netz > bezug_grenze:
                return f"Sicherheitsabbruch: Netzbezug {-netz:.2f} kW"
            soc_jetzt = p.get("soc")
            if soc_jetzt is not None and soc_jetzt >= ziel:
                return f"Ziel-Ladestand {ziel} % erreicht"
            return None

        if await self._befehl(
            s, "async_set_grid_charge",
            lambda: inverter.async_set_grid_charge(kw, ziel), power_kw=kw, target_soc=ziel,
        ):
            if pruefbar:
                await self._beobachten(s, pruef_laden_mindestens(kw), stopp=_stopp)
                self._pv_einbruch_pruefen(s)
            else:
                s["voraussetzung"] = (
                    f"die Batterie lädt schon ≈{ladung_vorher:.2f} kW aus PV — "
                    "abends oder nachts wiederholen"
                )
                s["wirkung"] = NICHT_PRUEFBAR
                await self._warten(10.0)
        self._schritt_ende(s)
        await self._freigeben(
            "Netzladung",
            pruef_keine_netzladung,
            None if pruefbar and s.get("wirkung") == ERFUELLT else (
                "die Batterie lud vorher schon aus PV" if not pruefbar
                else "die Netzladung wirkte nicht nachweislich"
            ),
        )

    # ------------------------------------------------------------------
    # Abschluss
    # ------------------------------------------------------------------
    async def _abschluss(self) -> bool:
        """Immer: alles freigeben (Executor-Weg, hebt auch den Deckel auf)."""
        assert self._protokoll is not None
        inverter = self._inverter
        executor = self._data.get("executor")
        s = self._schritt_neu("freigabe", "Abschluss: Steuerung freigeben", neutral=True)
        try:
            if executor is not None:
                # Der Executor weiß nichts von dem, was wir direkt am Treiber
                # geschrieben haben — so hebt seine Freigabe auch den Deckel auf.
                nach = getattr(executor, "nach_fremdeingriff", None)
                if nach is not None:
                    nach()
                ok = await self._befehl(s, "executor.async_release", executor.async_release)
            else:
                ok = await self._befehl(s, "async_stop_forcible", inverter.async_stop_forcible)
                if getattr(inverter, "supports_discharge_limit", False):
                    ok = await inverter.async_set_discharge_limit(None) and ok
        except Exception:  # noqa: BLE001
            ok = False
        try:
            self._protokoll["stellwerte_nachher"] = await self._stellwerte()
            s["stellwerte"] = self._protokoll["stellwerte_nachher"]
        except Exception:  # noqa: BLE001
            pass
        try:
            s["ruecklesen_wert"] = _r(await self._lies_ladelimit())
        except Exception:  # noqa: BLE001
            pass
        if ok:
            s["wirkung"] = NICHT_PRUEFBAR
            s["voraussetzung"] = "Abschluss, keine Erwartung"
            self._schritt_ende(s, OK, "Freigegeben — der Wechselrichter läuft im Automatikbetrieb")
        else:
            self._schritt_ende(s, FEHLER, "Freigabe fehlgeschlagen — bitte am Wechselrichter prüfen")
        return ok

    async def _beenden(self, modus_vorher: str | None, freigabe_ok: bool) -> None:
        assert self._protokoll is not None
        p = self._protokoll
        # Executor wieder zulassen, bevor der Modus zurückgeht.
        self._data["diagnose_laeuft"] = False
        bereiche: dict[str, dict] = {}
        jetzt_iso = _jetzt().isoformat()
        for bereich in BEREICHE:
            schritte = [s for s in p["schritte"] if s["bereich"] == bereich]
            if not schritte:
                continue
            # Neutrale Schritte (Ausgangslage, Abschluss, Warten auf den
            # Rückfall) prüfen keine Wirkung — als „ok" machten sie jedes
            # ungeprüfte „teilweise" desselben Bereichs zu „ok". Zählen
            # dürfen sie nur, wenn sie scheiterten.
            zaehlen = [
                s for s in schritte
                if not s.get("neutral") or (s.get("ergebnis") or ABGEBROCHEN) not in (OK, TEILWEISE)
            ]
            if not zaehlen:
                zaehlen = schritte
                ergebnis = TEILWEISE
            else:
                ergebnis = bereich_ergebnis([s.get("ergebnis") or ABGEBROCHEN for s in zaehlen])
            schritte = zaehlen
            schlimmster = next(
                (s for s in reversed(schritte) if (s.get("ergebnis") or ABGEBROCHEN) == ergebnis),
                schritte[-1],
            )
            bereiche[bereich] = {
                "ergebnis": ergebnis,
                "text": schlimmster.get("text"),
                "zeit": jetzt_iso,
            }
            if ergebnis == UEBERSPRUNGEN and all(s.get("ohne_faehigkeit") for s in schritte):
                bereiche[bereich]["ohne_faehigkeit"] = True
        p["bereiche"] = bereiche
        p["beendet"] = jetzt_iso
        if p.get("abbruch_grund"):
            p["ergebnis"] = ABGEBROCHEN
        else:
            p["ergebnis"] = gesamt_ergebnis([b["ergebnis"] for b in bereiche.values()])

        # Je Bereich merken — aber ein übersprungener oder abgebrochener
        # Bereich überschreibt kein früheres echtes Ergebnis: „bestanden am
        # Mittag" bleibt stehen, wenn der Abendtest die Ladelimit-Prüfung
        # mangels Überschuss nur teilweise schaffen konnte.
        for bereich, eintrag in bereiche.items():
            alt = self._bereiche.get(bereich)
            neu = eintrag["ergebnis"]
            if neu == UEBERSPRUNGEN and alt is not None:
                continue
            if neu == TEILWEISE and alt is not None and alt.get("ergebnis") == OK:
                self._bereiche[bereich] = {**alt, "zuletzt_teilweise": jetzt_iso}
                continue
            if neu == ABGEBROCHEN and alt is not None:
                continue
            self._bereiche[bereich] = dict(eintrag)

        # Modus wiederherstellen — nur wenn wir ihn umgestellt haben, er
        # noch auf Aus steht und die Freigabe gelang.
        wiederhergestellt = False
        if (
            modus_vorher == MODE_EIN
            and self._modus() == MODE_AUS
            and freigabe_ok
            and not self._data.get("stopping")
            and not self._von_ha_beendet
        ):
            await self._modus_setzen(MODE_EIN)
            wiederhergestellt = True
        p["modus_wiederhergestellt"] = wiederhergestellt

        self._letztes = p
        self.laeuft = False
        self._schritt = None
        self._protokoll = None
        await self._async_save(laeuft_seit=None)
        lauf = self._data.get("_run_cycle")
        if lauf is not None and not self._data.get("stopping") and not self._von_ha_beendet:
            try:
                await lauf()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Diagnose: Guard-Lauf nach dem Test fehlgeschlagen", exc_info=True)
        if self.bei_ende is not None:
            try:
                ergebnis = self.bei_ende(p)
                if asyncio.iscoroutine(ergebnis):
                    await ergebnis
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Diagnose: Telemetrie fehlgeschlagen", exc_info=True)
        _LOGGER.info("Diagnose beendet: %s", p["ergebnis"])
