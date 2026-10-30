"""Tarife der Energiegemeinschaften vom Telemetrie-Server.

Manche Gemeinschaften ändern ihre Sätze jedes Quartal (Energiewende
Ansfelden: Tarifblatt zu Quartalsbeginn). Damit nicht jedes Mitglied nach
jedem Tarifblatt die Einstellungen nachziehen muss, pflegt der Verein die
Sätze einmal am Server (``GET /v1/tarife``, Tabelle ``community_tariffs``),
und die Integration übernimmt sie.

Abgeholt wird die **ganze** Liste, ohne Kennung und ohne Gemeinschaftsnamen:
Der Server erfährt nicht, wer welche Gemeinschaft hat — er sieht nur, dass
eine Anlage Tarife holt. Das ist keine Telemetrie und braucht deren
Zustimmung nicht; abschalten lässt es sich mit ``gemeinschaftstarif_auto``.

Jeder Tarif hat ein ``gueltig_ab``. Der nächste Quartalstarif kann also vorab
am Server stehen, und die Integration wechselt am Stichtag von selbst — auch
ohne Netz, aus dem Speicher. Ein Fehler beim Abruf lässt die zuletzt
geholten Tarife stehen; ohne je geholte Daten gelten die eingetippten Werte.

Wirkung nur an einer Stelle: ``ueberlagern`` ersetzt in der Konfiguration,
mit der ``schedule.async_collect_inputs`` rechnet, die Sätze der Gemeinschaft
(Tag, Nacht, Nachtfenster, Bezugspreis fürs Netzladen). Bilanz und Gewinn
frieren die Preise aus dem Plan ein und sehen damit dieselben Werte.
Anteil, Gewichtung, Ebene und Abnahmequote bleiben Sache des Mitglieds.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime, timezone
from typing import Any

from .const import DOMAIN, TELEMETRY_BACKEND_URL

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

TARIF_URL = TELEMETRY_BACKEND_URL + "/v1/tarife"
USER_AGENT = "HomeAssistant/EEGEnergyOptimizer"

# Konfigurationsschlüssel: Server-Tarife übernehmen (Vorgabe an). Gilt für
# beide Gemeinschaften; eine ohne Eintrag am Server behält ihre Werte.
CONF_GEMEINSCHAFTSTARIF_AUTO = "gemeinschaftstarif_auto"

# Tarife wechseln vierteljährlich, und der nächste steht vorab am Server —
# viermal am Tag nachsehen genügt. Der Server cacht selbst eine Stunde.
CACHE_FRESH_SECONDS = 6 * 3600
# Nach einem Fehler früher wieder versuchen, aber nicht jede halbe Stunde.
CACHE_RETRY_SECONDS = 3600

# Plausibilität: Sätze in €/kWh. Ein Tippfehler am Server (9,8 statt 0,098)
# darf keinen Fahrplan auf 9,80 € je kWh einspeisen lassen.
PREIS_MAX = 1.0
NACHT_SONNE = "sonne"
_DATUM = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FENSTER = re.compile(r"^(\d{1,2})-(\d{1,2})$")

# Gemeinschafts-Schlüssel je Block: (Name, Tag, Nacht, Bezug)
_BLOECKE = (
    ("peakshare_community", "peakshare_price", "peakshare_price_night", "peakshare_bezug_price"),
    ("peakshare_community_2", "peakshare_price_2", "peakshare_price_night_2", "peakshare_bezug_price_2"),
)


def abruf_gewuenscht(config: dict[str, Any]) -> bool:
    """Lohnt ein Abruf? Nur mit aktiver Gemeinschaft, gewählt, Schalter an."""
    if config.get("enable_peakshare") is False:
        return False
    if config.get(CONF_GEMEINSCHAFTSTARIF_AUTO) is False:
        return False
    return bool(config.get("peakshare_community") or config.get("peakshare_community_2"))


def _preis(wert: Any) -> float | None:
    try:
        zahl = float(wert)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(zahl) or zahl < 0 or zahl > PREIS_MAX:
        return None
    return zahl


def _nacht(wert: Any) -> str | None:
    """'sonne', 'HH-HH' (volle Stunden) oder None; alles andere fällt weg."""
    if wert is None:
        return None
    text = str(wert).strip().lower()
    if text == NACHT_SONNE:
        return NACHT_SONNE
    treffer = _FENSTER.match(text)
    if treffer and all(0 <= int(g) <= 23 for g in treffer.groups()):
        return f"{int(treffer.group(1))}-{int(treffer.group(2))}"
    return None


def parse_antwort(daten: Any) -> dict[str, dict[str, Any]]:
    """Serverantwort → {Name: {verband_name, tarife: [...]}}, geprüft.

    Ein Tarif ohne gültiges Datum oder Tagessatz fällt weg; ein unplausibler
    Nacht- oder Bezugssatz wird None (dann gilt der Tag bzw. die Handeingabe).
    """
    ergebnis: dict[str, dict[str, Any]] = {}
    if not isinstance(daten, dict):
        return ergebnis
    gemeinschaften = daten.get("gemeinschaften")
    if not isinstance(gemeinschaften, dict):
        return ergebnis
    for name, eintrag in gemeinschaften.items():
        if not isinstance(eintrag, dict) or not str(name).strip():
            continue
        tarife = []
        for t in eintrag.get("tarife") or []:
            if not isinstance(t, dict):
                continue
            ab = str(t.get("gueltig_ab") or "")
            tag = _preis(t.get("einspeisung_tag"))
            if not _DATUM.match(ab) or tag is None:
                continue
            tarife.append({
                "gueltig_ab": ab,
                "einspeisung_tag": tag,
                "einspeisung_nacht": _preis(t.get("einspeisung_nacht")),
                "nacht": _nacht(t.get("nacht")),
                "bezug": _preis(t.get("bezug")),
                "quelle": str(t.get("quelle") or "") or None,
                "hinweis": str(t.get("hinweis") or "") or None,
            })
        if tarife:
            tarife.sort(key=lambda t: t["gueltig_ab"])
            ergebnis[str(name).strip()] = {
                "verband_name": str(eintrag.get("verband_name") or name),
                "tarife": tarife,
            }
    return ergebnis


def _schluessel(name: Any) -> str:
    return str(name or "").strip().casefold()


def eintrag_fuer(daten: dict[str, dict[str, Any]], name: Any) -> dict[str, Any] | None:
    """Server-Eintrag einer Gemeinschaft, Groß-/Kleinschreibung egal."""
    gesucht = _schluessel(name)
    if not gesucht:
        return None
    for n, eintrag in daten.items():
        if _schluessel(n) == gesucht:
            return eintrag
    return None


def tarif_am(eintrag: dict[str, Any] | None, tag: date) -> dict[str, Any] | None:
    """Der am Tag gültige Tarif: der jüngste mit gueltig_ab ≤ Tag."""
    if not eintrag:
        return None
    heute = tag.isoformat()
    gueltig = [t for t in eintrag.get("tarife") or [] if t["gueltig_ab"] <= heute]
    return gueltig[-1] if gueltig else None


def naechster_tarif(eintrag: dict[str, Any] | None, tag: date) -> dict[str, Any] | None:
    """Der nächste angekündigte Tarif (gueltig_ab > Tag), für die Anzeige."""
    if not eintrag:
        return None
    heute = tag.isoformat()
    kommend = [t for t in eintrag.get("tarife") or [] if t["gueltig_ab"] > heute]
    return kommend[0] if kommend else None


def nachtfenster(
    nacht: str | None, sonne: tuple[float, float] | None
) -> tuple[int, int] | None:
    """Nachtfenster in vollen Stunden (von, bis) oder None.

    ``sonne`` = (Untergang, Aufgang) als lokale Stunde mit Bruchteil. Der
    Fahrplan kennt nur volle Stunden (``_stunde_aus_zeit``); gerundet wird
    nach innen — Untergang auf, Aufgang ab —, damit keine Stunde als Nacht
    zählt, in der noch Tagtarif gilt. 16:40 → ab 17 Uhr, 07:20 → bis 7 Uhr.
    Ohne Sonnenstand bleibt das eingestellte Fenster.
    """
    if nacht == NACHT_SONNE:
        if not sonne:
            return None
        untergang, aufgang = sonne
        return int(math.ceil(untergang)) % 24, int(math.floor(aufgang)) % 24
    if nacht:
        treffer = _FENSTER.match(nacht)
        if treffer:
            return int(treffer.group(1)), int(treffer.group(2))
    return None


def ueberlagern(
    config: dict[str, Any],
    daten: dict[str, dict[str, Any]] | None,
    tag: date,
    sonne: tuple[float, float] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Konfiguration mit den Server-Tarifen; dazu, was übernommen wurde.

    Nur bei eingeschaltetem ``gemeinschaftstarif_auto`` (Vorgabe an) und nur
    für Gemeinschaften mit einem am Tag gültigen Server-Tarif. Das
    Nachtfenster ist eines für beide Gemeinschaften (``peakshare_night_*``):
    Der Server-Tarif setzt es, wenn er einen Nachtsatz hat.
    """
    if not daten or config.get(CONF_GEMEINSCHAFTSTARIF_AUTO) is False:
        return config, []
    neu = dict(config)
    uebernommen: list[dict[str, Any]] = []
    for name_key, tag_key, nacht_key, bezug_key in _BLOECKE:
        eintrag = eintrag_fuer(daten, config.get(name_key))
        tarif = tarif_am(eintrag, tag)
        if tarif is None:
            continue
        neu[tag_key] = tarif["einspeisung_tag"]
        # Leer heißt im Fahrplan „wie am Tag" (eeg_price) — ohne Nachtsatz
        # darf kein alter Handwert stehen bleiben.
        neu[nacht_key] = tarif["einspeisung_nacht"]
        if tarif["bezug"] is not None:
            neu[bezug_key] = tarif["bezug"]
        fenster = None
        if tarif["einspeisung_nacht"] is not None:
            fenster = nachtfenster(tarif["nacht"], sonne)
            if fenster is not None:
                neu["peakshare_night_start"] = f"{fenster[0]:02d}:00"
                neu["peakshare_night_end"] = f"{fenster[1]:02d}:00"
        uebernommen.append({
            "name": str(config.get(name_key)),
            "verband_name": eintrag["verband_name"],
            "fenster": fenster,
            **tarif,
        })
    return neu, uebernommen


def sonnenstand(hass: Any) -> tuple[float, float] | None:
    """(Untergang, Aufgang) als lokale Stunde aus ``sun.sun``, sonst None."""
    try:
        zustand = hass.states.get("sun.sun")
        if zustand is None:
            return None
        werte = []
        for attr in ("next_setting", "next_rising"):
            roh = zustand.attributes.get(attr)
            if not roh:
                return None
            zeit = datetime.fromisoformat(str(roh).replace("Z", "+00:00"))
            lokal = dt_util.as_local(zeit)
            werte.append(lokal.hour + lokal.minute / 60.0)
        return werte[0], werte[1]
    except Exception:  # noqa: BLE001 - ohne Sonne gilt das eingestellte Fenster
        return None


class GemeinschaftstarifProvider:
    """Holt und speichert die Server-Tarife; liefert sie für Fahrplan und Panel."""

    def __init__(self, hass: Any, entry_id: str) -> None:
        self._hass = hass
        if Store is not None:
            self._store = Store(hass, 1, f"{DOMAIN}_{entry_id}_gemeinschaftstarif")
        else:
            self._store = None  # type: ignore[assignment]
        self._daten: dict[str, dict[str, Any]] = {}
        self._geholt: datetime | None = None
        self._versucht: datetime | None = None
        self._fehler: str | None = None

    @property
    def daten(self) -> dict[str, dict[str, Any]]:
        return self._daten

    def status(self, config: dict[str, Any], tag: date) -> dict[str, Any]:
        """Für das Panel: je konfigurierter Gemeinschaft der Server-Tarif."""
        alter_min = None
        if self._geholt is not None:
            alter_min = int((_utcnow() - self._geholt).total_seconds() / 60)
        sonne = sonnenstand(self._hass)
        gemeinschaften = []
        for name_key, *_ in _BLOECKE:
            name = config.get(name_key)
            eintrag = eintrag_fuer(self._daten, name)
            if not name or eintrag is None:
                continue
            tarif = tarif_am(eintrag, tag)
            gemeinschaften.append({
                "name": str(name),
                "verband_name": eintrag["verband_name"],
                "tarif": tarif,
                "fenster": nachtfenster(tarif["nacht"], sonne) if tarif else None,
                "naechster": naechster_tarif(eintrag, tag),
            })
        return {
            "auto": config.get(CONF_GEMEINSCHAFTSTARIF_AUTO) is not False,
            "gemeinschaften": gemeinschaften,
            "namen": sorted(self._daten),
            "alter_minuten": alter_min,
            "fehler": self._fehler,
        }

    async def async_load(self) -> None:
        if self._store is None:
            return
        try:
            stored = await self._store.async_load()
            if isinstance(stored, dict):
                self._daten = parse_antwort(stored.get("antwort"))
                geholt = stored.get("geholt")
                if geholt:
                    self._geholt = datetime.fromisoformat(geholt)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Gemeinschaftstarife: kein gespeicherter Stand")

    def _faellig(self, erzwingen: bool) -> bool:
        if erzwingen:
            return True
        jetzt = _utcnow()
        if self._versucht is not None and self._fehler and (
            (jetzt - self._versucht).total_seconds() < CACHE_RETRY_SECONDS
        ):
            return False
        if self._geholt is None:
            return True
        return (jetzt - self._geholt).total_seconds() >= CACHE_FRESH_SECONDS

    async def async_fetch(self, erzwingen: bool = False) -> None:
        if async_get_clientsession is None or not self._faellig(erzwingen):
            return
        import aiohttp

        self._versucht = _utcnow()
        try:
            session = async_get_clientsession(self._hass)
            async with session.get(
                TARIF_URL,
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                roh = await resp.json(content_type=None)
        except Exception as err:  # noqa: BLE001 - alter Stand bleibt
            self._fehler = str(err) or type(err).__name__
            _LOGGER.debug("Gemeinschaftstarife: Abruf fehlgeschlagen: %s", err)
            return
        self._daten = parse_antwort(roh)
        self._geholt = _utcnow()
        self._fehler = None
        if self._store is not None:
            await self._store.async_save({"antwort": roh, "geholt": self._geholt.isoformat()})
