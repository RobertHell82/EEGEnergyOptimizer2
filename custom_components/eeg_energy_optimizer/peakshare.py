"""PeakShare community grid import forecast provider (API V2).

Holt die Viertelstundenprognose je Energiegemeinschaft und legt sie im Cache
ab. Reine Datenbeschaffung — gesteuert wird über die Preisfunktion in
``eeg_price.py``, die aus dem Gemeinschaftssaldo einen Auf- oder Abschlag auf
den Einspeisetarif rechnet.

**Ein Wert je Viertelstunde, mit Vorzeichen.** Die API liefert zwei
komplementäre Felder, die nie gleichzeitig positiv sein können::

    intervalNetKwh = totalGeneration - totalConsumption
    deficitKwh = max(-intervalNetKwh, 0)     # die Gemeinschaft braucht Strom
    surplusKwh = max( intervalNetKwh, 0)     # die Gemeinschaft hat Strom übrig

Gespeichert wird daraus ``saldoKwh = deficitKwh - surplusKwh``:

* **positiv = Bedarf** — eingespeister Strom findet Abnehmer in der
  Gemeinschaft und bekommt den EEG-Tarif.
* **negativ = Überschuss** — der Strom geht zum Standardtarif ans EVU.
* **0** — ausgeglichenes Intervall *oder* keine Datengrundlage (siehe
  ``warnings``). Beides führt zum selben Ergebnis: kein Auf- und kein
  Abschlag, es gilt der Basistarif.

Achtung beim Vorzeichen: ``saldoKwh`` ist das **Negative** von
``intervalNetKwh`` der API. Das ist Absicht — der Bedarf ist die Größe, um
die es der Integration geht, und er soll positiv zählen.

V1 wird nicht mehr unterstützt. Der alte Endpunkt liefert nur 24 Stunden in
Stundenauflösung und kennt den Überschuss nicht; ein Rückfall darauf würde
genau die halbe Information liefern, auf der die Preisfunktion aufbaut.
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
except ImportError:
    _utcnow = lambda: datetime.now(tz=timezone.utc)  # noqa: E731
    async_get_clientsession = None  # type: ignore[assignment]
    Store = None  # type: ignore[assignment,misc]


PEAKSHARE_API_URL = (
    "https://peakshare.app/api/public/v2/community-grid-import-forecast"
)
PEAKSHARE_USER_AGENT = "HomeAssistant/EEGEnergyOptimizer"

# Wie oft neu geholt wird. V2 liefert 48 Stunden ab dem Abruf, und das Fenster
# wandert mit jeder Viertelstunde weiter — ein alter Cache deckt das Ende des
# Planungshorizonts nicht mehr ab, und dort entstünden dann Lücken. Eine halbe
# Stunde hält den Verlust unter einer Stunde und liegt weit über dem, was die
# API selbst cacht (max-age höchstens 300 s).
CACHE_FRESH_SECONDS = 1800
# Notfallreserve, wenn die API nicht erreichbar ist: lieber ein alternder
# Bedarfsverlauf als gar keiner. Was über das Ende hinausragt, fehlt einfach —
# fehlende Intervalle erzeugen weder Auf- noch Abschlag.
CACHE_MAX_SECONDS = 24 * 3600

# Warnungen der API zur Datengrundlage
WARN_STALE = "STALE_SOURCE_DAYS"
WARN_NO_SOURCE = "NO_SOURCE_DAYS"


def _parse_stamp(raw: Any) -> datetime | None:
    """ISO-Zeitstempel der API lesen. Deren Form ist ...T07:00:00.000Z."""
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        stempel = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stempel if stempel.tzinfo else stempel.replace(tzinfo=timezone.utc)


def _format_stamp(stempel: datetime) -> str:
    """Zurück in die Schreibweise der API, damit alles gleich aussieht."""
    return stempel.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _zahl(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _validate_api_response(data: Any) -> bool:
    """Struktur einer V2-Antwort prüfen, bevor sie den Cache ersetzt."""
    if not isinstance(data, dict):
        return False
    communities = data.get("communities")
    if not isinstance(communities, list):
        return False
    for community in communities:
        if not isinstance(community, dict):
            return False
        if "name" not in community:
            return False
        intervals = community.get("intervals")
        if not isinstance(intervals, list):
            return False
        for eintrag in intervals:
            if not isinstance(eintrag, dict):
                return False
            if "timestamp" not in eintrag:
                return False
            # Eines der beiden Felder genügt; fehlen beide, ist es keine
            # V2-Antwort, sondern etwas anderes.
            if "deficitKwh" not in eintrag and "surplusKwh" not in eintrag:
                return False
    return True


def _normalisieren(data: dict) -> dict:
    """V2-Antwort auf das eigene, schlanke Format bringen.

    Aus zwei komplementären Feldern wird ein Wert mit Vorzeichen (siehe
    Modul-Docstring). Das halbiert den Cache und macht jede spätere Rechnung
    vorzeichenrichtig, ohne dass sie beide Felder kennen muss.
    """
    gemeinschaften: list[dict] = []
    for community in data.get("communities", []):
        if not isinstance(community, dict) or "name" not in community:
            continue
        intervalle: list[dict] = []
        for eintrag in community.get("intervals", []):
            if not isinstance(eintrag, dict):
                continue
            stempel = _parse_stamp(eintrag.get("timestamp"))
            if stempel is None:
                continue
            defizit = _zahl(eintrag.get("deficitKwh")) or 0.0
            ueberschuss = _zahl(eintrag.get("surplusKwh")) or 0.0
            intervalle.append(
                {
                    "timestamp": _format_stamp(stempel),
                    # Negative Einzelwerte wären ein API-Fehler; wegkappen,
                    # damit ein Vorzeichendreher nicht zur Preisumkehr wird.
                    "saldoKwh": round(max(0.0, defizit) - max(0.0, ueberschuss), 4),
                }
            )
        intervalle.sort(key=lambda e: e["timestamp"])
        warnungen = community.get("warnings")
        gemeinschaften.append(
            {
                "name": community["name"],
                "xTenant": community.get("xTenant"),
                "sourceDays": community.get("sourceDays") or [],
                "warnings": warnungen if isinstance(warnungen, list) else [],
                "intervals": intervalle,
            }
        )
    return {
        "generatedAt": data.get("generatedAt"),
        "windowStart": data.get("windowStart"),
        "windowEndExclusive": data.get("windowEndExclusive"),
        "communities": gemeinschaften,
    }


def _ist_normalisiert(data: Any) -> bool:
    """Erkennt den eigenen Cache — alte V1-Persistate fallen hier durch."""
    if not isinstance(data, dict):
        return False
    communities = data.get("communities")
    if not isinstance(communities, list):
        return False
    for community in communities:
        if not isinstance(community, dict):
            return False
        intervals = community.get("intervals")
        if not isinstance(intervals, list):
            return False
        for eintrag in intervals:
            if not isinstance(eintrag, dict) or "saldoKwh" not in eintrag:
                return False
    return True


class PeakShareProvider:
    """Holt die Gemeinschaftsprognose und hält sie im Cache."""

    def __init__(self, hass: Any, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        if Store is not None:
            self._store = Store(hass, 1, f"{DOMAIN}_{entry_id}_peakshare")
        else:
            self._store = None  # type: ignore[assignment]
        self._cache: dict | None = None
        self._cache_time: datetime | None = None

    async def async_load(self) -> None:
        """Persistierten Cache beim Start laden.

        Ein Persistat aus der V1-Zeit (``hours`` mit ``deficitKwh``) wird
        verworfen statt umgerechnet: es hat nur 24 Stunden und kennt keinen
        Überschuss, taugt also nicht als Grundlage. Beim ersten Abruf steht
        die volle Prognose ohnehin wieder bereit.
        """
        if self._store is None:
            return
        try:
            stored = await self._store.async_load()
        except Exception:
            _LOGGER.debug("PeakShare: kein persistierter Cache gefunden")
            return
        if not stored or not isinstance(stored, dict):
            return
        daten = stored.get("data")
        if not _ist_normalisiert(daten):
            _LOGGER.debug("PeakShare: Cache aus der V1-Zeit verworfen")
            return
        self._cache = daten
        fetched_at = stored.get("fetched_at")
        if fetched_at:
            try:
                self._cache_time = datetime.fromisoformat(fetched_at)
            except ValueError:
                self._cache_time = None
        _LOGGER.debug("PeakShare: Cache geladen (fetched_at=%s)", fetched_at)

    async def async_fetch(self) -> dict | None:
        """Frische Daten holen, wenn der Cache alt ist.

        Bei einem Fehler gilt der Cache weiter, solange er jünger als
        ``CACHE_MAX_SECONDS`` ist. Ein Zeitlimit von 30 Sekunden verhindert,
        dass der Planungstakt hängen bleibt.
        """
        now = _utcnow()

        if (
            self._cache_time
            and (now - self._cache_time).total_seconds() < CACHE_FRESH_SECONDS
        ):
            return self._cache

        if async_get_clientsession is not None:
            try:
                import aiohttp

                session = async_get_clientsession(self._hass)
                async with session.get(
                    PEAKSHARE_API_URL,
                    headers={"User-Agent": PEAKSHARE_USER_AGENT},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if not _validate_api_response(data):
                            _LOGGER.warning(
                                "PeakShare API: ungültige Antwortstruktur, "
                                "verwende Cache"
                            )
                        else:
                            normalisiert = _normalisieren(data)
                            self._warnungen_melden(normalisiert)
                            self._cache = normalisiert
                            self._cache_time = now
                            if self._store is not None:
                                await self._store.async_save(
                                    {
                                        "data": normalisiert,
                                        "fetched_at": now.isoformat(),
                                    }
                                )
                            return normalisiert
                    elif resp.status == 503:
                        # Der V2-Cache der API ist noch nicht vorgewärmt. Das
                        # ist ein normaler Übergangszustand, kein Fehler —
                        # der nächste Takt versucht es erneut.
                        _LOGGER.debug(
                            "PeakShare API: Cache noch nicht bereit (503), "
                            "verwende eigenen Cache"
                        )
                    else:
                        _LOGGER.warning(
                            "PeakShare API: HTTP %s, verwende Cache", resp.status
                        )
            except Exception:
                _LOGGER.warning("PeakShare API Abfrage fehlgeschlagen, verwende Cache")

        if (
            self._cache_time
            and (now - self._cache_time).total_seconds() < CACHE_MAX_SECONDS
        ):
            return self._cache
        return None

    def _warnungen_melden(self, daten: dict) -> None:
        """Datengrundlage-Warnungen der API ins Log heben.

        ``NO_SOURCE_DAYS`` ist der wichtigere Fall: die betroffenen Intervalle
        kommen als 0 an, und 0 heißt dort *keine Daten*, nicht *kein Bedarf*.
        Für die Preisfunktion ist beides gleichbedeutend (kein Auf-, kein
        Abschlag), aber wer sich über einen flachen Preis wundert, soll den
        Grund im Log finden.
        """
        for community in daten.get("communities", []):
            warnungen = community.get("warnings") or []
            if WARN_NO_SOURCE in warnungen:
                _LOGGER.warning(
                    "PeakShare '%s': für mindestens eine Tagesklasse fehlt ein "
                    "Quelltag — diese Intervalle stehen auf 0 und wirken nicht "
                    "auf den Preis",
                    community.get("name"),
                )
            elif WARN_STALE in warnungen:
                _LOGGER.info(
                    "PeakShare '%s': die Prognose stützt sich auf Quelltage, "
                    "die älter als 28 Tage sind",
                    community.get("name"),
                )

    def get_communities(self) -> list[str]:
        """Namen der Gemeinschaften aus dem Cache."""
        if not self._cache or not isinstance(self._cache, dict):
            return []
        communities = self._cache.get("communities", [])
        return [c["name"] for c in communities if isinstance(c, dict) and "name" in c]

    def get_intervals(self, community_name: str) -> list[dict]:
        """Viertelstundenwerte einer Gemeinschaft.

        Jeder Eintrag ist ``{"timestamp": ISO-UTC, "saldoKwh": float}`` mit
        positivem Wert für Bedarf und negativem für Überschuss. Die API
        liefert 192 Intervalle über 48 Stunden — der Planungshorizont wird
        davon vollständig abgedeckt, Kopien wie zur V1-Zeit braucht es nicht
        mehr.
        """
        if not self._cache or not isinstance(self._cache, dict):
            return []
        for community in self._cache.get("communities", []):
            if isinstance(community, dict) and community.get("name") == community_name:
                intervalle = community.get("intervals")
                return intervalle if isinstance(intervalle, list) else []
        return []

    def get_warnings(self, community_name: str) -> list[str]:
        """Warnungen der API zur Datengrundlage einer Gemeinschaft."""
        if not self._cache or not isinstance(self._cache, dict):
            return []
        for community in self._cache.get("communities", []):
            if isinstance(community, dict) and community.get("name") == community_name:
                warnungen = community.get("warnings")
                return warnungen if isinstance(warnungen, list) else []
        return []
