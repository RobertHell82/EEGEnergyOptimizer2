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
"""

from __future__ import annotations

import html as html_entities
import logging
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

# Österreichische und deutsche Schreibweise, beide kommen vor.
MONATE = {
    "jänner": 1, "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4,
    "mai": 5, "juni": 6, "juli": 7, "august": 8, "september": 9,
    "oktober": 10, "november": 11, "dezember": 12,
}


def parse_tarife(html: str) -> dict[int, float]:
    """Monat → Einspeisetarif in €/kWh aus dem HTML der OeMAG-Seite.

    Gelesen wird die erste Tabelle: Spalte 1 der Monatsname, Spalte 2 der Satz
    für Photovoltaik (``6,146 ct/kWh``). Zeilen ohne beides fallen heraus, die
    Kopfzeile also von selbst.
    """
    tabellen = re.findall(r"<table.*?</table>", html or "", re.S | re.I)
    if not tabellen:
        return {}

    tarife: dict[int, float] = {}
    for zeile in re.findall(r"<tr.*?</tr>", tabellen[0], re.S | re.I):
        # Entities dekodieren, nicht nur &nbsp; ersetzen: die Seite schreibt
        # Umlaute teils als M&auml;rz, teils direkt in UTF-8.
        zellen = [
            html_entities.unescape(re.sub(r"<[^>]+>", " ", z)).replace(" ", " ").strip()
            for z in re.findall(r"<t[dh].*?</t[dh]>", zeile, re.S | re.I)
        ]
        if len(zellen) < 2:
            continue
        monat = MONATE.get(zellen[0].strip().lower())
        if monat is None:
            continue
        preis = _ct_pro_kwh(zellen[1])
        if preis is not None:
            tarife[monat] = preis
    return tarife


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

    # -- Zustand -------------------------------------------------------

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
                self._preis = stored.get("preis")
                self._monat = stored.get("monat")
                geholt = stored.get("geholt")
                if geholt:
                    self._geholt = datetime.fromisoformat(geholt)
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
                html = await resp.text()
        except Exception as err:
            self._fehler = str(err)
            _LOGGER.warning(
                "OeMAG-Tarif nicht abrufbar (%s) — es gilt weiter %s",
                err,
                f"{self._preis:.5f} €/kWh" if self._preis else "die Handeingabe",
            )
            return self.preis

        tarife = parse_tarife(html)
        treffer = tarif_fuer(tarife, dt_now_monat())
        if treffer is None:
            self._fehler = "Tabelle nicht lesbar"
            _LOGGER.warning(
                "OeMAG-Seite gelesen, aber kein Tarif erkannt — Aufbau der Seite "
                "geändert? Es gilt weiter %s",
                f"{self._preis:.5f} €/kWh" if self._preis else "die Handeingabe",
            )
            return self.preis

        preis, monat = treffer
        self._preis, self._monat, self._geholt, self._fehler = preis, monat, jetzt, None
        _LOGGER.debug("OeMAG-Tarif: %.5f €/kWh (Monat %d)", preis, monat)

        if self._store is not None:
            try:
                await self._store.async_save(
                    {"preis": preis, "monat": monat, "geholt": jetzt.isoformat()}
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
