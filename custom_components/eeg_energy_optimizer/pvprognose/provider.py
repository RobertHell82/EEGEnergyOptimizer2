"""Eigene PV-Prognose — Abruf, Rechnung, Zwischenspeicher.

Die dritte Prognosequelle neben Solcast und Forecast.Solar, und die einzige,
die ohne fremde HA-Integration und ohne Konto auskommt: Wetterdaten von
Open-Meteo (``openmeteo.py``), Anlage aus der Konfiguration (``pv_flaechen``,
``pv_verluste_pct``, ``inverter_ac_limit_kw``), Standort aus der
Home-Assistant-Konfiguration, Leistung aus dem PV-Modell (``modell.py``).

Der Provider hält EINE Leistungsreihe (sieben Tage, 15 Minuten, AC-kW) und
leitet alles daraus ab, was die Integration von einer Prognose wissen will:

* ``halbstunden()`` — Halbstundenmittel ab Slot-Anfang für den Fahrplan, im
  selben Raster wie Solcasts ``detailedForecast``. Ohne p10-Pfad: die
  Reserve rechnet dann, wie bei Forecast.Solar, mit 60 % der Erwartung
  (``DEFAULT_WORST_CASE_FACTOR``) — ein erfundenes Perzentil wäre keins.
* ``rest_heute_kwh()`` / ``morgen_kwh()`` — die beiden Prognosesensoren.
* ``tage_kwh()`` — sieben Tagessummen fürs Wochendiagramm des Panels.

Abgerufen wird im halbstündigen Fremddaten-Takt (``__init__.py``); der
Zwischenspeicher (``Store``) überlebt Neustarts, damit der erste Fahrplan
nach dem Boot nicht auf das Netz wartet. Schlägt ein Abruf fehl, bleibt die
letzte Reihe stehen; sie deckt mit sieben Tagen den 48-h-Horizont auch nach
zwei Tagen Ausfall noch ab. Erst ab ``MAX_ALTER_S`` gilt sie als nicht mehr
vorhanden — dann gibt es keinen Fahrplan, und das soll laut sein, nicht ein
stiller Plan auf uraltem Wetter.

**Kalibrierung — vorgesehen, noch nicht gebaut.** Das Modell kennt weder
Horizont noch Verschattung noch Schnee. Beides steht in der eigenen
Erzeugungshistorie: Das Verhältnis gemessen zu modelliert, gebinnt nach
Sonnenstand und über Wochen gemittelt, ergibt je Bin einen Korrekturfaktor,
der genau die Abweichungen einfängt, die kein Rechenmodell weiß. Der Ort
dafür ist EIN Faktor je Zeitpunkt zwischen ``leistungsreihe()`` und dieser
Klasse — nichts anderes muss sich ändern. Bis dahin gilt: Tagessummen
stimmen erfahrungsgemäß, der Verlauf im Verschattungsfall nicht.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ..const import CONF_INVERTER_AC_LIMIT_KW, DOMAIN
from .modell import (
    Flaeche,
    Leistungsreihe,
    flaechen_aus_config,
    halbstunden,
    leistungsreihe,
    pruefe_flaechen,
    rest_heute_kwh,
    tagessummen,
    verluste_aus_config,
)
from .openmeteo import Wetterreihe, baue_url, hole_wetter

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from homeassistant.helpers.storage import Store
    from homeassistant.util import dt as dt_util

    _utcnow = dt_util.utcnow
    _lokal = dt_util.as_local
except ImportError:  # Testumgebung
    _utcnow = lambda: datetime.now(tz=timezone.utc)  # noqa: E731
    _lokal = lambda dt: dt.astimezone()  # noqa: E731
    async_get_clientsession = None  # type: ignore[assignment]
    Store = None  # type: ignore[assignment,misc]

# Der Fremddaten-Takt läuft alle 30 Minuten; mit 25 Minuten Frist holt jeder
# Tick, ohne dass ein knapp verpasster Tick eine Stunde kostet.
FRISCH_S = 25 * 60
# Ab hier zählt die Reihe als nicht mehr vorhanden (siehe Modul-Doku).
MAX_ALTER_S = 48 * 3600
# Ab hier steht im Fahrplan-Protokoll, dass mit altem Wetter gerechnet wird.
WARN_ALTER_S = 3 * 3600

QUELLE = "open_meteo"


def standort(hass: Any) -> tuple[float, float] | None:
    """Breite/Länge aus der HA-Konfiguration — None, wenn nicht gesetzt."""
    try:
        breite = float(hass.config.latitude)
        laenge = float(hass.config.longitude)
    except (TypeError, ValueError, AttributeError):
        return None
    if not (-90.0 <= breite <= 90.0 and -180.0 <= laenge <= 180.0):
        return None
    if breite == 0.0 and laenge == 0.0:
        return None
    return breite, laenge


async def hole_reihe(
    session: Any,
    breite: float,
    laenge: float,
    flaechen: list[Flaeche],
    verluste_pct: float,
    ac_limit_kw: float | None,
) -> Leistungsreihe:
    """Alle Flächen abrufen und zur Anlagenleistung verrechnen.

    Nacheinander, nicht parallel: höchstens acht Flächen, je etwa eine
    Sekunde — und Open-Meteo dankt es mit weniger gleichzeitigen Anfragen.
    """
    paare: list[tuple[Flaeche, Wetterreihe]] = []
    for flaeche in flaechen:
        wetter = await hole_wetter(session, baue_url(breite, laenge, flaeche.neigung, flaeche.azimut))
        paare.append((flaeche, wetter))
    return leistungsreihe(paare, verluste_pct, ac_limit_kw)


def _zusammenfassung(reihe: Leistungsreihe, jetzt: datetime) -> dict[str, Any]:
    tage = tagessummen(reihe, jetzt, _lokal)
    return {
        "tage_kwh": tage,
        "rest_heute_kwh": rest_heute_kwh(reihe, jetzt, _lokal),
        "morgen_kwh": tage[1] if len(tage) > 1 else None,
        "spitze_kw": round(max(reihe.kw), 3) if reihe.kw else 0.0,
        "werte": len(reihe),
        "von": reihe.ende[0].isoformat() if reihe.ende else None,
        "bis": reihe.ende[-1].isoformat() if reihe.ende else None,
    }


async def berechne_einmalig(
    hass: Any,
    flaechen_roh: Any,
    verluste_pct: float | None,
    ac_limit_kw: float | None,
) -> dict[str, Any]:
    """Für „Prognose berechnen" im Panel: einmal rechnen, nichts speichern.

    Nimmt die UNGESPEICHERTEN Eingaben entgegen — im Assistenten gibt es
    noch keine Konfiguration, in den Einstellungen will man vor dem
    Speichern sehen, was dabei herauskommt. ``ValueError`` bei Eingabe- oder
    Standortproblemen, ``RuntimeError`` bei Netz/HTTP.
    """
    sauber, fehler = pruefe_flaechen(flaechen_roh)
    if fehler:
        raise ValueError(fehler)
    ort = standort(hass)
    if ort is None:
        raise ValueError(
            "Kein Standort — bitte Breiten- und Längengrad in den "
            "Home-Assistant-Einstellungen (System → Allgemein) eintragen"
        )
    if async_get_clientsession is None:
        raise RuntimeError("Kein HTTP-Client verfügbar")
    flaechen = flaechen_aus_config({"pv_flaechen": sauber})
    verluste = (
        verluste_aus_config({"pv_verluste_pct": verluste_pct})
        if verluste_pct is not None
        else verluste_aus_config({})
    )
    session = async_get_clientsession(hass)
    reihe = await hole_reihe(session, ort[0], ort[1], flaechen, verluste, ac_limit_kw)
    if reihe.leer():
        raise RuntimeError("Open-Meteo lieferte keine Werte")
    jetzt = _utcnow()
    ergebnis = _zusammenfassung(reihe, jetzt)
    ergebnis.update(
        {
            "quelle": QUELLE,
            "standort": {"breite": ort[0], "laenge": ort[1]},
            "flaechen": sauber,
            "verluste_pct": verluste,
            "ac_limit_kw": ac_limit_kw,
            "kwp_gesamt": round(sum(f.kwp for f in flaechen), 3),
        }
    )
    return ergebnis


class PvPrognoseProvider:
    """Hält die Leistungsreihe der eigenen Prognose und frischt sie auf."""

    def __init__(self, hass: Any, entry_id: str, config: dict[str, Any]) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store = None
        if Store is not None:
            self._store = Store(hass, 1, f"{DOMAIN}_{entry_id}_pvprognose")
        self._reihe = Leistungsreihe([], [])
        self._geholt: datetime | None = None
        self._fehler: str | None = None
        self._fehler_gemeldet = False
        self._sperre = asyncio.Lock()
        self._flaechen: list[Flaeche] = []
        self._verluste_pct = 0.0
        self._ac_limit_kw: float | None = None
        self.update_config(config)

    # -- Konfiguration --------------------------------------------------

    def update_config(self, config: dict[str, Any]) -> None:
        self._flaechen = flaechen_aus_config(config)
        self._verluste_pct = verluste_aus_config(config)
        try:
            ac = float(config.get(CONF_INVERTER_AC_LIMIT_KW) or 0.0)
        except (TypeError, ValueError):
            ac = 0.0
        self._ac_limit_kw = ac if ac > 0 else None

    def _kennung(self) -> dict[str, Any]:
        """Womit die gespeicherte Reihe gerechnet wurde — passt sie nicht
        zur aktuellen Anlage, ist sie wertlos."""
        return {
            "flaechen": [f.als_dict() for f in self._flaechen],
            "verluste_pct": self._verluste_pct,
            "ac_limit_kw": self._ac_limit_kw,
        }

    @property
    def flaechen(self) -> list[Flaeche]:
        return list(self._flaechen)

    @property
    def kwp_gesamt(self) -> float:
        return round(sum(f.kwp for f in self._flaechen), 3)

    # -- Lesen ----------------------------------------------------------

    @property
    def hat_daten(self) -> bool:
        return self._geholt is not None and not self._reihe.leer()

    def alter_s(self, jetzt: datetime | None = None) -> float | None:
        if self._geholt is None:
            return None
        return max(0.0, ((jetzt or _utcnow()) - self._geholt).total_seconds())

    def reihe(self, jetzt: datetime | None = None) -> Leistungsreihe | None:
        """Die Leistungsreihe — None ohne Daten oder wenn sie zu alt ist."""
        if not self.hat_daten:
            return None
        alter = self.alter_s(jetzt)
        if alter is not None and alter > MAX_ALTER_S:
            return None
        return self._reihe

    def halbstunden(self, jetzt: datetime | None = None) -> dict[datetime, float]:
        reihe = self.reihe(jetzt)
        return halbstunden(reihe) if reihe is not None else {}

    def tage_kwh(self, jetzt: datetime | None = None) -> list[float] | None:
        reihe = self.reihe(jetzt)
        if reihe is None:
            return None
        return tagessummen(reihe, jetzt or _utcnow(), _lokal)

    def rest_heute_kwh(self, jetzt: datetime | None = None) -> float | None:
        reihe = self.reihe(jetzt)
        if reihe is None:
            return None
        return rest_heute_kwh(reihe, jetzt or _utcnow(), _lokal)

    def leistung_jetzt_kw(self, jetzt: datetime | None = None) -> float | None:
        """Prognostizierte AC-Leistung der laufenden Viertelstunde (kW).

        Das Gegenstück zum Sensor „PV-Leistung" — nebeneinander im Verlauf
        zeigen beide, wie gut die Prognose den Tagesgang trifft.
        """
        reihe = self.reihe(jetzt)
        if reihe is None:
            return None
        jetzt = jetzt or _utcnow()
        for ende, kw in zip(reihe.ende, reihe.kw):
            if ende - timedelta(minutes=15) <= jetzt < ende:
                return kw
        return None

    def morgen_kwh(self, jetzt: datetime | None = None) -> float | None:
        tage = self.tage_kwh(jetzt)
        return tage[1] if tage and len(tage) > 1 else None

    def status(self, jetzt: datetime | None = None) -> dict[str, Any]:
        """Stand für Panel und Protokoll — immer vollständig, auch ohne Daten."""
        jetzt = jetzt or _utcnow()
        alter = self.alter_s(jetzt)
        ort = standort(self._hass)
        ergebnis: dict[str, Any] = {
            "quelle": QUELLE,
            "geholt": self._geholt.isoformat() if self._geholt else None,
            "alter_minuten": None if alter is None else int(alter // 60),
            "veraltet": alter is not None and alter > MAX_ALTER_S,
            "fehler": self._fehler,
            "standort": None if ort is None else {"breite": ort[0], "laenge": ort[1]},
            "flaechen": [f.als_dict() for f in self._flaechen],
            "kwp_gesamt": self.kwp_gesamt,
            "verluste_pct": self._verluste_pct,
            "ac_limit_kw": self._ac_limit_kw,
            "tage_kwh": None,
            "rest_heute_kwh": None,
            "morgen_kwh": None,
            "spitze_kw": None,
            "werte": 0,
            "von": None,
            "bis": None,
        }
        reihe = self.reihe(jetzt)
        if reihe is not None:
            ergebnis.update(_zusammenfassung(reihe, jetzt))
        return ergebnis

    # -- Speicher -------------------------------------------------------

    async def async_load(self) -> None:
        if self._store is None:
            return
        try:
            stored = await self._store.async_load()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("PV-Prognose: kein gespeicherter Stand lesbar")
            return
        if not stored or not isinstance(stored, dict):
            return
        if stored.get("kennung") != self._kennung():
            _LOGGER.debug("PV-Prognose: gespeicherter Stand passt nicht zur Anlage — verworfen")
            return
        try:
            ende = [datetime.fromisoformat(s) for s in stored.get("ende") or []]
            kw = [float(v) for v in stored.get("kw") or []]
            geholt = stored.get("geholt")
            if not ende or len(ende) != len(kw) or not geholt:
                return
            self._reihe = Leistungsreihe(ende=ende, kw=kw)
            self._geholt = datetime.fromisoformat(geholt)
            self._fehler = stored.get("fehler")
        except (TypeError, ValueError):
            _LOGGER.debug("PV-Prognose: gespeicherter Stand unlesbar — verworfen")
            self._reihe = Leistungsreihe([], [])
            self._geholt = None

    async def _async_save(self) -> None:
        if self._store is None:
            return
        try:
            await self._store.async_save(
                {
                    "kennung": self._kennung(),
                    "geholt": self._geholt.isoformat() if self._geholt else None,
                    "ende": [t.isoformat() for t in self._reihe.ende],
                    "kw": self._reihe.kw,
                    "fehler": self._fehler,
                }
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("PV-Prognose konnte nicht gespeichert werden")

    # -- Abruf ----------------------------------------------------------

    async def async_fetch(self, force: bool = False) -> bool:
        """Reihe auffrischen, wenn sie alt genug ist. True bei neuen Daten."""
        if not self._flaechen:
            self._fehler = "Keine PV-Fläche konfiguriert"
            return False
        ort = standort(self._hass)
        if ort is None:
            self._fehler = "Kein Standort in der Home-Assistant-Konfiguration"
            return False
        if async_get_clientsession is None:
            return False
        async with self._sperre:
            jetzt = _utcnow()
            if not force and self._geholt is not None:
                if (jetzt - self._geholt).total_seconds() < FRISCH_S:
                    return False
            session = async_get_clientsession(self._hass)
            try:
                reihe = await hole_reihe(
                    session, ort[0], ort[1], self._flaechen, self._verluste_pct, self._ac_limit_kw
                )
                if reihe.leer():
                    raise RuntimeError("Open-Meteo lieferte keine Werte")
            except Exception as err:  # noqa: BLE001 — jeder Fehler heißt: alte Reihe behalten
                self._fehler = f"{type(err).__name__}: {err}" if not str(err) else str(err)
                if not self._fehler_gemeldet:
                    _LOGGER.warning(
                        "PV-Prognose (Open-Meteo) nicht abrufbar: %s — %s",
                        self._fehler,
                        "die letzte Reihe bleibt stehen"
                        if self.hat_daten
                        else "noch keine Prognose vorhanden",
                    )
                    self._fehler_gemeldet = True
                else:
                    _LOGGER.debug("PV-Prognose weiter nicht abrufbar: %s", self._fehler)
                await self._async_save()
                return False

            self._reihe = reihe
            self._geholt = _utcnow()
            if self._fehler_gemeldet:
                _LOGGER.info("PV-Prognose (Open-Meteo) wieder abrufbar")
            self._fehler = None
            self._fehler_gemeldet = False
            _LOGGER.debug(
                "PV-Prognose: %d Werte, %s Flächen, %.1f kWp — heute noch %.2f kWh, morgen %.2f kWh",
                len(reihe),
                len(self._flaechen),
                self.kwp_gesamt,
                self.rest_heute_kwh() or 0.0,
                self.morgen_kwh() or 0.0,
            )
            await self._async_save()
            return True
