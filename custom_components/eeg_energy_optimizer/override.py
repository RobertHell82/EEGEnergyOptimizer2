"""Befristeter Eingriff des Nutzers: die Pause.

Pause heißt: für eine Weile nicht steuern — der Wechselrichter läuft in
seiner eigenen Eigenverbrauchs-Automatik, genau wie im Modus Aus, nur mit
Endbedingung. Davon gibt es zwei, wahlweise oder kombiniert:

* **Zeit** — „für 4 Stunden". Endet zur Ablaufzeit.
* **Ladestand** — „bis die Batterie 80 % hat". Endet, sobald der gemessene
  SOC die Marke erreicht. Der typische Fall: Das Auto lädt nachher, die
  Batterie soll bis dahin voll werden, statt dass der Fahrplan sie in die
  Gemeinschaft entlädt. Damit so eine Pause nicht ewig hängt (trüber Tag,
  Sensor ausgefallen), gilt zusätzlich eine Ablaufzeit — ohne Angabe die
  Obergrenze von 48 h.

Der Eingriff wird **persistiert**. Das ist der entscheidende Punkt gegenüber
einer HA-Automation mit ``delay``: Ein Neustart mitten in der Pause darf die
Steuerung nicht wieder anwerfen, während das Auto gerade lädt. Ist die
Bedingung erfüllt, verschwindet die Pause von selbst — nichts muss
zurückgestellt werden, deshalb gibt es auch keinen Zustand, der hängen
bleiben kann.

Die Pause kennt ihre Wirkung nicht selbst. Sie ist nur ein Datum mit
Endbedingung; ausgewertet wird sie im Guard-Lauf in ``__init__.py``, der
sie wie Modus Aus behandelt und ihr je Takt den gemessenen Ladestand reicht.
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

# Länger als zwei Tage ist kein befristeter Eingriff mehr, sondern eine
# Einstellung — dafür gibt es den Modus Aus.
MAX_STUNDEN = 48.0
MIN_STUNDEN = 0.25

# Ziel-Ladestand einer Pause „bis Ladestand". Unter 50 % ist keine sinnvolle
# Vorhaltung — so tief steht die Batterie im Normalbetrieb ohnehin selten.
MIN_PAUSE_SOC_PCT = 50.0
MAX_PAUSE_SOC_PCT = 100.0


class SteuerOverride:
    """Eine Pause mit Endbedingung (Zeit und/oder Ladestand), persistent."""

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
        # Nur eine Pause wird wiederbelebt. Eine gespeicherte „Reserve" aus
        # einer älteren Version (bis 2.0.3-devfronius.4) fällt hier still weg.
        if isinstance(gespeichert, dict) and gespeichert.get("art") == ART_PAUSE:
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
        self,
        stunden: float | None,
        now: datetime,
        quelle: str = "panel",
        bis_soc_pct: float | None = None,
    ) -> dict[str, Any]:
        """Nicht steuern, bis eine der Bedingungen erfüllt ist.

        ``stunden`` — Ablaufzeit; ohne Angabe die Obergrenze (nur sinnvoll
        zusammen mit ``bis_soc_pct``, sonst wäre es keine Befristung).
        ``bis_soc_pct`` — Ladestand, bei dessen Erreichen die Pause endet.
        Ersetzt eine laufende Pause.
        """
        if stunden is None:
            stunden = MAX_STUNDEN
        stunden = max(MIN_STUNDEN, min(MAX_STUNDEN, float(stunden)))
        soc_ziel = None
        if bis_soc_pct is not None:
            soc_ziel = max(MIN_PAUSE_SOC_PCT, min(MAX_PAUSE_SOC_PCT, float(bis_soc_pct)))
        self._data = {
            "art": ART_PAUSE,
            "bis": (now + timedelta(hours=stunden)).isoformat(),
            "bis_soc_pct": soc_ziel,
            "gesetzt_am": now.isoformat(),
            "quelle": quelle,
        }
        await self._async_save()
        if soc_ziel is not None:
            _LOGGER.info(
                "Pause gesetzt: bis Ladestand %.0f %%, längstens %.2f h bis %s (%s)",
                soc_ziel, stunden, self._data["bis"], quelle,
            )
        else:
            _LOGGER.info("Pause gesetzt: %.2f h bis %s (%s)", stunden, self._data["bis"], quelle)
        return self.to_dict(now)

    async def async_aufheben(self, quelle: str = "panel") -> None:
        if not self._data:
            return
        self._data = {}
        await self._async_save()
        _LOGGER.info("Pause aufgehoben (%s)", quelle)

    async def async_tick(self, now: datetime, soc_pct: float | None = None) -> None:
        """Endbedingungen prüfen — einmal je Guard-Lauf.

        Beendet die Pause, wenn die Zeit abgelaufen ist oder der gemessene
        Ladestand ``soc_pct`` das Ziel erreicht hat. Ohne Messwert (Sensor
        nicht lesbar) zählt nur die Zeit — deshalb hat auch eine Pause „bis
        Ladestand" immer eine Ablaufzeit.

        Das Auslesen (``aktiv``) ignoriert Abgelaufenes ohnehin; hier wird
        der Eintrag auch aus dem Store entfernt, damit nach einem Neustart
        keine alte Pause wieder auftaucht.
        """
        if not self._data:
            return
        bis = self._bis()
        if bis is not None and bis <= now:
            _LOGGER.info("Pause abgelaufen")
            self._data = {}
            await self._async_save()
            return
        ziel = self.pause_soc_pct(now)
        if ziel is not None and soc_pct is not None and float(soc_pct) >= ziel:
            _LOGGER.info(
                "Pause beendet: Ladestand %.0f %% erreicht (Ziel %.0f %%)", soc_pct, ziel
            )
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
        """Die laufende Pause oder None — abgelaufene zählen nicht."""
        if not self._data:
            return None
        bis = self._bis()
        if bis is None or bis <= now:
            return None
        return dict(self._data)

    def pause_bis(self, now: datetime) -> datetime | None:
        """Ablaufzeit einer aktiven Pause, sonst None.

        Bei einer Pause „bis Ladestand" ist das die Sicherheits-Obergrenze;
        wann sie wirklich endet, entscheidet der Ladestand.
        """
        eintrag = self.aktiv(now)
        if eintrag is None or eintrag.get("art") != ART_PAUSE:
            return None
        return self._bis()

    def pause_soc_pct(self, now: datetime) -> float | None:
        """Ziel-Ladestand einer aktiven Pause „bis Ladestand", sonst None."""
        eintrag = self.aktiv(now)
        if eintrag is None or eintrag.get("bis_soc_pct") is None:
            return None
        try:
            return float(eintrag["bis_soc_pct"])
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
            "bis_soc_pct": eintrag.get("bis_soc_pct"),
            "gesetzt_am": eintrag.get("gesetzt_am"),
            "quelle": eintrag.get("quelle"),
        }
