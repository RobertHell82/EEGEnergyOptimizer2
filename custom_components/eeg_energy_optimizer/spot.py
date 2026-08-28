"""Spotpreis der Strombörse (EPEX Day-Ahead) über die aWATTar-API.

Die API ist frei und ohne Schlüssel nutzbar (https://api.awattar.at bzw. .de,
JSON, Preise in Eur/MWh). Sie liefert die Day-Ahead-Preise der eigenen
Preiszone; die Werte für den Folgetag erscheinen nach der Börsenauktion am
frühen Nachmittag (~14:00). Abgerufen wird ab 48 Stunden zurück — die
Vergangenheit braucht die Fortschreibung (unten), die Zukunft der Fahrplan.

Drei Eigenheiten, bewusst so gebaut:

* **Negative Preise bleiben negativ.** Sie sind an der Börse real, und genau
  dann soll der Fahrplan nicht einspeisen, sondern abregeln (``discard_p``)
  oder speichern. Geklemmt wird in ``schedule.py`` nur die *Fiktion* des
  Gemeinschafts-Abschlags, nie der echte Börsenpreis.
* **Fehlende Slots werden vom Vortag fortgeschrieben.** Der Fahrplan rechnet
  48 Stunden voraus, die Börse veröffentlicht aber nur bis Ende des
  Folgetags. Spotpreise haben eine starke Tagesstruktur — der gleiche
  Viertelstundenwert von vor 24 Stunden ist der beste verfügbare Schätzer
  (gemessen an der Alternative, den Horizont auf unter 24 h zu kürzen, was
  über den festgenagelten Endbestand teurer ist). Wie viele Slots
  fortgeschrieben wurden, wird mitgezählt und angezeigt.
* **Jeder Fehler lässt die zuletzt geholten Preise stehen** (über Neustarts
  gespeichert). Ihre Zeitstempel bleiben gültig; was fehlt, deckt die
  Fortschreibung. Ohne jegliche Daten greift die Handeingabe — der Ausfall
  einer API darf den Fahrplan nicht anhalten.

Intern liegt alles auf dem Viertelstundenraster (Epochensekunden // 900, wie
in ``eeg_price.py``): die API liefert heute Stundenwerte, seit Oktober 2025
handelt die Börse aber viertelstündlich — liefert aWATTar irgendwann 15-min-
Einträge, ändert sich hier nichts.
"""

from __future__ import annotations

import logging
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


SPOT_URLS = {
    "at": "https://api.awattar.at/v1/marketdata",
    "de": "https://api.awattar.de/v1/marketdata",
}
SPOT_USER_AGENT = "HomeAssistant/EEGEnergyOptimizer"

# Die Preise ändern sich einmal täglich (Auktion ~14:00); stündlich nachsehen
# genügt und ist der API gegenüber sparsam. Der 30-Minuten-Takt der
# Integration ist nur der Anlass, diese Frist entscheidet.
CACHE_FRESH_SECONDS = 55 * 60
# Wie weit zurück abgerufen und aufbewahrt wird. Die Vergangenheit trägt die
# Fortschreibung; mehr als eine Woche hilft ihr nicht.
FETCH_PAST_SECONDS = 48 * 3600
# Bis wohin nach vorn abgerufen wird. Der ``end``-Parameter ist PFLICHT:
# mit nur ``start`` liefert die aWATTar-API genau 24 Stunden AB start —
# bei start = jetzt − 48 h also ausschließlich Vergangenheit, nie den
# aktuellen oder morgigen Preis (live gemessen, 27.08.2026). Die API kappt
# selbst am Ende der veröffentlichten Daten, ein großzügiges Ende schadet
# nicht.
FETCH_FUTURE_SECONDS = 48 * 3600
KEEP_PAST_SECONDS = 7 * 24 * 3600
# Ein Tag in Viertelstunden — Schrittweite der Fortschreibung.
_TAG_SLOTS = 96


def parse_marketdata(payload: Any) -> dict[int, float]:
    """aWATTar-JSON → ``{Epochenviertelstunde: €/kWh}``.

    Einträge tragen ``start_timestamp``/``end_timestamp`` (Unix-Millisekunden)
    und ``marketprice`` in Eur/MWh. Jeder Eintrag füllt alle Viertelstunden
    seines Zeitraums — Stunden- wie 15-Minuten-Einträge landen so im selben
    Raster. Negative Preise bleiben erhalten.
    """
    daten = (payload or {}).get("data") if isinstance(payload, dict) else None
    preise: dict[int, float] = {}
    for eintrag in daten or []:
        if not isinstance(eintrag, dict):
            continue
        try:
            start = int(eintrag["start_timestamp"]) // 1000
            ende = int(eintrag["end_timestamp"]) // 1000
            preis = float(eintrag["marketprice"]) / 1000.0  # Eur/MWh → €/kWh
        except (KeyError, TypeError, ValueError):
            continue
        if ende <= start or ende - start > 24 * 3600:
            continue
        for slot in range(start // 900, ende // 900):
            preise[slot] = preis
    return preise


def reihe_fuer(
    preise: dict[int, float], stamps: list[datetime]
) -> tuple[list[float] | None, int]:
    """Preis je Zeitstempel, fehlende Slots vom Vortag fortgeschrieben.

    Rückgabe: (€/kWh je Stempel, Anzahl fortgeschriebener Slots). Ohne
    jegliche Daten ``(None, 0)`` — dann soll die Handeingabe gelten. Die
    Fortschreibung sucht kaskadiert bis eine Woche zurück (t−24h, t−48h, …);
    findet auch das nichts, gilt der zuletzt bestimmte Wert der Reihe.
    """
    if not preise or not stamps:
        return None, 0

    werte: list[float] = []
    fortgeschrieben = 0
    letzter: float | None = None
    for stamp in stamps:
        slot = int(stamp.timestamp() // 900)
        preis = preise.get(slot)
        if preis is None:
            fortgeschrieben += 1
            for tage in range(1, 8):
                preis = preise.get(slot - tage * _TAG_SLOTS)
                if preis is not None:
                    break
            if preis is None:
                # Vor allen bekannten Daten (oder riesige Lücke): der letzte
                # Reihenwert, sonst der zeitlich jüngste bekannte Preis.
                preis = letzter if letzter is not None else preise[max(preise)]
        werte.append(preis)
        letzter = preis
    return werte, fortgeschrieben


class SpotProvider:
    """Holt die Börsenpreise und hält sie über Neustarts hinweg."""

    def __init__(self, hass: Any, entry_id: str, market: str = "at") -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._market = market if market in SPOT_URLS else "at"
        # Marktgebiet im Store-SCHLÜSSEL: AT und DE sind getrennte Preiszonen.
        # Mit gemeinsamem Schlüssel lud ein Wechsel auf DE die österreichischen
        # Preise samt frischem Abrufzeitpunkt — der Fahrplan rechnete bis zu
        # 55 Minuten mit den falschen Preisen, und weil der Merge die alten
        # Slots nur überlagert, speisten sie danach weiter die Fortschreibung.
        if Store is not None:
            self._store = Store(hass, 1, f"{DOMAIN}_{entry_id}_spot_{self._market}")
        else:
            self._store = None  # type: ignore[assignment]
        self._preise: dict[int, float] = {}
        self._geholt: datetime | None = None
        self._fehler: str | None = None

    # -- Zustand -------------------------------------------------------

    def reihe_fuer(self, stamps: list[datetime]) -> tuple[list[float] | None, int]:
        return reihe_fuer(self._preise, stamps)

    def preis_jetzt(self) -> float | None:
        """Preis des aktuellen Viertelstundenslots, oder None."""
        if not self._preise:
            return None
        return self._preise.get(int(_utcnow().timestamp() // 900))

    def status(self) -> dict[str, Any]:
        """Für die Anzeige: aktueller Preis, Datenreichweite, Alter, Fehler."""
        alter_min = None
        if self._geholt is not None:
            alter_min = int((_utcnow() - self._geholt).total_seconds() / 60)
        daten_bis = None
        if self._preise:
            daten_bis = datetime.fromtimestamp(
                (max(self._preise) + 1) * 900, tz=timezone.utc
            ).isoformat()
        return {
            "preis": self.preis_jetzt(),
            "daten_bis": daten_bis,
            "alter_minuten": alter_min,
            "fehler": self._fehler,
            "markt": self._market,
            "quelle": SPOT_URLS[self._market],
        }

    # -- Laden und Holen -----------------------------------------------

    async def async_load(self) -> None:
        if self._store is None:
            return
        try:
            stored = await self._store.async_load()
            if stored and isinstance(stored, dict):
                roh = stored.get("preise") or {}
                self._preise = {int(k): float(v) for k, v in roh.items()}
                geholt = stored.get("geholt")
                if geholt:
                    self._geholt = datetime.fromisoformat(geholt)
        except Exception:
            _LOGGER.debug("Spot: keine gespeicherten Preise vorhanden")

    async def async_fetch(self, force: bool = False) -> None:
        """Preise holen, wenn der letzte Abruf älter als eine Stunde ist."""
        jetzt = _utcnow()
        if (
            not force
            and self._geholt is not None
            and (jetzt - self._geholt).total_seconds() < CACHE_FRESH_SECONDS
        ):
            return

        if async_get_clientsession is None:
            return

        start_ms = int((jetzt.timestamp() - FETCH_PAST_SECONDS) * 1000)
        end_ms = int((jetzt.timestamp() + FETCH_FUTURE_SECONDS) * 1000)
        try:
            import aiohttp

            session = async_get_clientsession(self._hass)
            async with session.get(
                SPOT_URLS[self._market],
                params={"start": str(start_ms), "end": str(end_ms)},
                headers={"User-Agent": SPOT_USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                payload = await resp.json()
        except Exception as err:
            self._fehler = str(err)
            _LOGGER.warning(
                "Spotpreise nicht abrufbar (%s) — es gelten die zuletzt "
                "geholten Preise%s",
                err,
                "" if self._preise else " (keine vorhanden, es gilt die Handeingabe)",
            )
            return

        neu = parse_marketdata(payload)
        if not neu:
            self._fehler = "Antwort ohne Preisdaten"
            _LOGGER.warning(
                "Spot-API gelesen, aber keine Preise erkannt — Format geändert?"
            )
            return

        # Neue Werte über die alten legen, Uraltes wegräumen. So überstehen
        # die Vortage einen Abruf, der nur die Zukunft liefert.
        grenze = int((jetzt.timestamp() - KEEP_PAST_SECONDS) // 900)
        self._preise = {
            k: v
            for k, v in {**self._preise, **neu}.items()
            if k >= grenze
        }
        self._geholt, self._fehler = jetzt, None
        _LOGGER.debug(
            "Spotpreise (%s): %d Slots, bis %s",
            self._market,
            len(self._preise),
            self.status().get("daten_bis"),
        )

        if self._store is not None:
            try:
                await self._store.async_save(
                    {
                        "preise": {str(k): v for k, v in self._preise.items()},
                        "geholt": jetzt.isoformat(),
                    }
                )
            except Exception:
                _LOGGER.debug("Spotpreise konnten nicht gespeichert werden")
