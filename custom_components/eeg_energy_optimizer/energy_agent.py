"""energy agent: Bedarfsprognose der Gemeinschaft als Alternative zu PeakShare.

energy agent (app.energy-agent.at) führt Energiegemeinschaften und bietet
seinen Mitgliedern eine API (v0, laut Anbieter noch nicht stabil). Jedes
Mitglied legt dort einen persönlichen Schlüssel an (``ea_…``); der Schlüssel
gehört zur Person, nicht zur Gemeinschaft — ``/v0/communities`` liefert alle
Gemeinschaften, in denen sie aktives Mitglied ist (in Österreich darf ein
Zählpunkt an mehreren teilnehmen, typisch EEG + BEG).

Die API kennt keine Bedarfsreihe, sondern **Erzeugung und Verbrauch der
ganzen Gemeinschaft** je Viertelstunde (kWh, UTC, P10/P50/P90). Daraus wird
dieselbe Größe wie bei PeakShare (``peakshare.py``)::

    saldoKwh = Verbrauch_P50 − Erzeugung_P50

positiv = Bedarf, negativ = Überschuss. Der Provider hat deshalb die
Schnittstelle des ``PeakShareProvider`` (``get_communities``,
``get_intervals``, ``get_warnings``, ``async_fetch``) und liegt unter
demselben Schlüssel ``data["peakshare"]`` — Fahrplan, Bilanz, Panel-Diagramm
und Netzladen rechnen unverändert weiter.

Tarife liefert die API nicht; sie kommen wie bei PeakShare aus den
Einstellungen oder vom Tarifserver (``gemeinschaftstarif.py``, Abgleich über
den Namen).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .const import DOMAIN
from .peakshare import (
    CACHE_FRESH_SECONDS,
    CACHE_MAX_SECONDS,
    _format_stamp,
    _ist_normalisiert,
    _laufende_uebernehmen,
    _parse_stamp,
    _zahl,
)

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


ENERGY_AGENT_API_URL = "https://app.energy-agent.at/api"
ENERGY_AGENT_USER_AGENT = "HomeAssistant/EEGEnergyOptimizer"
CONF_ENERGY_AGENT_KEY = "energy_agent_api_key"

# Zustände der Prognose, in denen sie als Steuersignal taugt. Alles andere
# (stale, failed, blocked, …) wird geholt und angezeigt, aber gewarnt.
GUTE_ZUSTAENDE = ("healthy", "estimated", "bootstrap")

TIMEOUT_S = 30


class EnergyAgentFehler(Exception):
    """Abruf gescheitert — ``grund`` ist kurz und für das Panel gedacht."""

    def __init__(self, grund: str) -> None:
        super().__init__(grund)
        self.grund = grund


def saldo_intervalle(erzeugung: list[dict], verbrauch: list[dict]) -> list[dict]:
    """Zwei Prognosereihen zu ``[{timestamp, saldoKwh}]`` verrechnen.

    Nur Viertelstunden, die in BEIDEN Reihen stehen: Ein fehlender Verbrauch
    neben vorhandener Erzeugung wäre sonst ein erfundener Überschuss. Fehlt
    eine Reihe ganz (``FORECAST_NOT_CONFIGURED``), gibt es keinen Saldo —
    Bedarf ohne Erzeugung oder umgekehrt ist kein Saldo der Gemeinschaft.
    """
    def _reihe(punkte: list[dict]) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in punkte or []:
            if not isinstance(p, dict):
                continue
            stempel = _parse_stamp(p.get("timestamp"))
            wert = _zahl(p.get("p50_kwh"))
            if stempel is None or wert is None:
                continue
            out[_format_stamp(stempel)] = max(0.0, wert)
        return out

    erz = _reihe(erzeugung)
    ver = _reihe(verbrauch)
    return [
        {"timestamp": t, "saldoKwh": round(ver[t] - erz[t], 4)}
        for t in sorted(erz.keys() & ver.keys())
    ]


class EnergyAgentProvider:
    """Holt Erzeugung und Verbrauch je Gemeinschaft und hält den Saldo im Cache."""

    def __init__(self, hass: Any, entry_id: str, api_key: str | None) -> None:
        self._hass = hass
        self._key = str(api_key or "").strip()
        if Store is not None and entry_id != "temp":
            self._store = Store(hass, 1, f"{DOMAIN}_{entry_id}_energy_agent")
        else:
            self._store = None  # type: ignore[assignment]
        self._cache: dict | None = None
        self._cache_time: datetime | None = None
        self.letzter_fehler: str | None = None

    # -- Netz ---------------------------------------------------------------

    async def _get(self, pfad: str, params: dict | None = None) -> Any:
        if not self._key:
            raise EnergyAgentFehler("Kein API-Schlüssel eingetragen")
        if async_get_clientsession is None:
            raise EnergyAgentFehler("Kein HTTP-Client")
        import aiohttp

        session = async_get_clientsession(self._hass)
        try:
            async with session.get(
                f"{ENERGY_AGENT_API_URL}{pfad}",
                params=params,
                headers={
                    "X-API-Key": self._key,
                    "User-Agent": ENERGY_AGENT_USER_AGENT,
                    "Accept": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_S),
            ) as resp:
                if resp.status == 401:
                    raise EnergyAgentFehler("API-Schlüssel ungültig")
                if resp.status == 404:
                    try:
                        code = (await resp.json(content_type=None)).get("error_code")
                    except Exception:
                        code = None
                    raise EnergyAgentFehler(code or "HTTP 404")
                if resp.status != 200:
                    raise EnergyAgentFehler(f"HTTP {resp.status}")
                return await resp.json(content_type=None)
        except EnergyAgentFehler:
            raise
        except Exception as err:  # Netz, Zeitlimit, kaputtes JSON
            raise EnergyAgentFehler(f"Nicht erreichbar ({type(err).__name__})") from err

    async def async_gemeinschaften(self) -> list[dict]:
        """``[{id, name}]`` aller Gemeinschaften des Schlüssels."""
        daten = await self._get("/v0/communities")
        if not isinstance(daten, list):
            raise EnergyAgentFehler("Ungültige Antwort")
        return [
            {"id": str(c["id"]), "name": str(c["name"])}
            for c in daten
            if isinstance(c, dict) and c.get("id") and c.get("name")
        ]

    async def _abrufen(self) -> dict:
        gemeinschaften: list[dict] = []
        for gem in await self.async_gemeinschaften():
            warnungen: list[str] = []
            reihen: dict[str, list[dict]] = {}
            for ziel in ("generation", "consumption"):
                try:
                    antwort = await self._get(
                        f"/v0/communities/{gem['id']}/forecast", {"target": ziel}
                    )
                except EnergyAgentFehler as err:
                    # Eine fehlende Reihe einer Gemeinschaft soll die andere
                    # Gemeinschaft nicht mitreißen.
                    warnungen.append(f"{ziel}: {err.grund}")
                    reihen[ziel] = []
                    continue
                zustand = str((antwort or {}).get("state") or "")
                if zustand and zustand not in GUTE_ZUSTAENDE:
                    warnungen.append(f"{ziel}: {zustand}")
                punkte = (antwort or {}).get("points")
                reihen[ziel] = punkte if isinstance(punkte, list) else []
            gemeinschaften.append(
                {
                    "name": gem["name"],
                    "id": gem["id"],
                    "warnings": warnungen,
                    "intervals": saldo_intervalle(
                        reihen["generation"], reihen["consumption"]
                    ),
                }
            )
        return {"communities": gemeinschaften}

    # -- Schnittstelle wie PeakShareProvider --------------------------------

    async def async_load(self) -> None:
        if self._store is None:
            return
        try:
            stored = await self._store.async_load()
        except Exception:
            return
        if not isinstance(stored, dict):
            return
        # Ein Cache gehört zu genau einem Schlüssel: ein neuer Schlüssel kann
        # eine andere Person mit anderen Gemeinschaften sein.
        if stored.get("key_prefix") != self._key[:8]:
            return
        daten = stored.get("data")
        if not _ist_normalisiert(daten):
            return
        self._cache = daten
        try:
            self._cache_time = datetime.fromisoformat(stored.get("fetched_at") or "")
        except ValueError:
            self._cache_time = None

    async def async_fetch(self) -> dict | None:
        """Frische Daten holen, wenn der Cache alt ist; bei Fehler der Cache."""
        now = _utcnow()
        if (
            self._cache_time
            and (now - self._cache_time).total_seconds() < CACHE_FRESH_SECONDS
        ):
            return self._cache
        try:
            neu = _laufende_uebernehmen(await self._abrufen(), self._cache, now)
        except EnergyAgentFehler as err:
            self.letzter_fehler = err.grund
            _LOGGER.warning("energy agent: %s, verwende Cache", err.grund)
        else:
            self.letzter_fehler = None
            for gem in neu["communities"]:
                # Ohne Saldo wirkt die Gemeinschaft nicht — das ist ein
                # Versagen, kein Hinweis (Telemetrie „eeg_bedarf“).
                if not gem["intervals"]:
                    self.letzter_fehler = "Kein Saldo: " + (
                        ", ".join(gem["warnings"]) or "leere Prognose"
                    )
                if gem["warnings"]:
                    _LOGGER.warning(
                        "energy agent '%s': %s", gem["name"], ", ".join(gem["warnings"])
                    )
            self._cache = neu
            self._cache_time = now
            if self._store is not None:
                await self._store.async_save(
                    {
                        "data": neu,
                        "fetched_at": now.isoformat(),
                        "key_prefix": self._key[:8],
                    }
                )
            return neu
        if (
            self._cache_time
            and (now - self._cache_time).total_seconds() < CACHE_MAX_SECONDS
        ):
            return self._cache
        return None

    def get_communities(self) -> list[str]:
        if not isinstance(self._cache, dict):
            return []
        return [
            c["name"]
            for c in self._cache.get("communities", [])
            if isinstance(c, dict) and "name" in c
        ]

    def _gemeinschaft(self, name: str) -> dict | None:
        if not isinstance(self._cache, dict):
            return None
        for c in self._cache.get("communities", []):
            if isinstance(c, dict) and c.get("name") == name:
                return c
        return None

    def get_intervals(self, community_name: str) -> list[dict]:
        gem = self._gemeinschaft(community_name)
        intervalle = (gem or {}).get("intervals")
        return intervalle if isinstance(intervalle, list) else []

    def get_warnings(self, community_name: str) -> list[str]:
        gem = self._gemeinschaft(community_name)
        warnungen = (gem or {}).get("warnings")
        return warnungen if isinstance(warnungen, list) else []
