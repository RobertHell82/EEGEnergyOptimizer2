"""Zeitlich begrenzte Eingriffe des Nutzers: Pause und Reserve.

Zwei Bedürfnisse, eine Grundlage. **Pause** heißt: für eine Weile nicht
steuern — der Wechselrichter läuft in seiner eigenen Eigenverbrauchs-
Automatik, genau wie im Modus Aus, nur mit Ablaufzeit. **Reserve** heißt:
weiter steuern, aber mit höherem Mindest-Ladestand — der Fahrplan optimiert
weiter, nur eben nie unter die gewünschte Marke. Wo immer möglich ist die
Reserve die bessere Wahl: Sie behält den Ertrag, den eine Pause wegwirft.

Beides ist ein Override mit Ablaufzeit, und beides wird **persistiert**. Das
ist der entscheidende Punkt gegenüber einer HA-Automation mit ``delay``: Ein
Neustart mitten in der Pause darf die Steuerung nicht wieder anwerfen,
während das Auto gerade lädt. Läuft die Zeit ab, verschwindet der Override
von selbst — nichts muss zurückgestellt werden, deshalb gibt es auch keinen
Zustand, der hängen bleiben kann.

Der Override kennt seine Wirkung nicht selbst. Er ist nur ein Datum mit
Ablaufzeit; ausgewertet wird er an zwei Stellen: ``async_collect_inputs``
in ``schedule.py`` hebt bei aktiver Reserve den Mindest-Ladestand an, und
der Guard-Lauf in ``__init__.py`` behandelt eine aktive Pause wie Modus Aus.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from .const import DOMAIN

try:
    from homeassistant.helpers.storage import Store
except ImportError:  # pragma: no cover — Testumgebung
    Store = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

ART_PAUSE = "pause"
ART_RESERVE = "reserve"

# Länger als zwei Tage ist kein befristeter Eingriff mehr, sondern eine
# Einstellung — dafür gibt es den Modus Aus bzw. den Mindest-Ladestand.
MAX_STUNDEN = 48.0
MIN_STUNDEN = 0.25

# Die Reserve darf höher liegen als der dauerhafte Mindest-Ladestand (der ist
# auf 30 % gedeckelt, damit niemand die Optimierung permanent lahmlegt). Für
# einen befristeten Eingriff gilt das Argument nicht — aber über 90 % modelliert
# der Fahrplan nicht (HAConfig klemmt den Boden dort ab).
MAX_RESERVE_PCT = 90.0
MIN_RESERVE_PCT = 5.0


class SteuerOverride:
    """Ein Pause- oder Reserve-Eingriff mit Ablaufzeit, persistent."""

    def __init__(self, hass: Any, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        store_key = f"{DOMAIN}_{entry_id}_override"
        self._store: Any = Store(hass, 1, store_key) if Store is not None else None
        self._data: dict[str, Any] = {}
        self._loaded = False

    # ------------------------------------------------------------------
    # Persistenz
    # ------------------------------------------------------------------
    async def async_load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if self._store is None:
            return
        try:
            gespeichert = await self._store.async_load()
        except Exception:  # noqa: BLE001 — ein kaputter Store darf das Setup nicht kippen
            _LOGGER.warning("Override nicht ladbar — starte ohne", exc_info=True)
            return
        if isinstance(gespeichert, dict) and gespeichert.get("art"):
            self._data = dict(gespeichert)

    async def _async_save(self) -> None:
        if self._store is None:
            return
        try:
            await self._store.async_save(dict(self._data))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Override nicht speicherbar: %s", err)

    # ------------------------------------------------------------------
    # Setzen / Aufheben
    # ------------------------------------------------------------------
    async def async_pause(
        self, stunden: float, now: datetime, quelle: str = "panel"
    ) -> dict[str, Any]:
        """Für ``stunden`` nicht steuern. Ersetzt einen laufenden Override."""
        stunden = max(MIN_STUNDEN, min(MAX_STUNDEN, float(stunden)))
        self._data = {
            "art": ART_PAUSE,
            "bis": (now + timedelta(hours=stunden)).isoformat(),
            "min_soc_pct": None,
            "gesetzt_am": now.isoformat(),
            "quelle": quelle,
        }
        await self._async_save()
        _LOGGER.info("Pause gesetzt: %.2f h bis %s (%s)", stunden, self._data["bis"], quelle)
        return self.to_dict(now)

    async def async_reserve(
        self, min_soc_pct: float, stunden: float, now: datetime, quelle: str = "panel"
    ) -> dict[str, Any]:
        """Für ``stunden`` mindestens ``min_soc_pct`` in der Batterie halten."""
        stunden = max(MIN_STUNDEN, min(MAX_STUNDEN, float(stunden)))
        pct = max(MIN_RESERVE_PCT, min(MAX_RESERVE_PCT, float(min_soc_pct)))
        self._data = {
            "art": ART_RESERVE,
            "bis": (now + timedelta(hours=stunden)).isoformat(),
            "min_soc_pct": pct,
            "gesetzt_am": now.isoformat(),
            "quelle": quelle,
        }
        await self._async_save()
        _LOGGER.info(
            "Reserve gesetzt: %.0f %% für %.2f h bis %s (%s)",
            pct, stunden, self._data["bis"], quelle,
        )
        return self.to_dict(now)

    async def async_aufheben(self, quelle: str = "panel") -> None:
        if not self._data:
            return
        art = self._data.get("art")
        self._data = {}
        await self._async_save()
        _LOGGER.info("Override (%s) aufgehoben (%s)", art, quelle)

    async def async_tick(self, now: datetime) -> None:
        """Abgelaufenen Override wegräumen — einmal je Guard-Lauf.

        Das Auslesen (``aktiv``) ignoriert Abgelaufenes ohnehin; hier wird
        der Eintrag auch aus dem Store entfernt, damit nach einem Neustart
        kein alter Override wieder auftaucht.
        """
        if self._data and self._bis() is not None and self._bis() <= now:
            _LOGGER.info("Override (%s) abgelaufen", self._data.get("art"))
            self._data = {}
            await self._async_save()

    # ------------------------------------------------------------------
    # Auswertung
    # ------------------------------------------------------------------
    def _bis(self) -> datetime | None:
        roh = self._data.get("bis")
        if not roh:
            return None
        try:
            return datetime.fromisoformat(roh)
        except (TypeError, ValueError):
            return None

    def aktiv(self, now: datetime) -> dict[str, Any] | None:
        """Der laufende Override oder None — abgelaufene zählen nicht."""
        if not self._data:
            return None
        bis = self._bis()
        if bis is None or bis <= now:
            return None
        return dict(self._data)

    def pause_bis(self, now: datetime) -> datetime | None:
        """Ablaufzeit einer aktiven Pause, sonst None."""
        eintrag = self.aktiv(now)
        if eintrag is None or eintrag.get("art") != ART_PAUSE:
            return None
        return self._bis()

    def reserve_pct(self, now: datetime) -> float | None:
        """Mindest-Ladestand einer aktiven Reserve, sonst None."""
        eintrag = self.aktiv(now)
        if eintrag is None or eintrag.get("art") != ART_RESERVE:
            return None
        try:
            return float(eintrag.get("min_soc_pct"))
        except (TypeError, ValueError):
            return None

    def to_dict(self, now: datetime) -> dict[str, Any]:
        """Für Panel, Sensor und WebSocket."""
        eintrag = self.aktiv(now)
        if eintrag is None:
            return {"aktiv": False}
        bis = self._bis()
        rest_min = None
        if bis is not None:
            rest_min = max(0, int((bis - now).total_seconds() // 60))
        return {
            "aktiv": True,
            "art": eintrag.get("art"),
            "bis": eintrag.get("bis"),
            "rest_minuten": rest_min,
            "min_soc_pct": eintrag.get("min_soc_pct"),
            "gesetzt_am": eintrag.get("gesetzt_am"),
            "quelle": eintrag.get("quelle"),
        }
