"""Einspeise-Statistik — wie viel ging während einer gesteuerten Entladung ins Netz?

Kehrt mit 1.5.37 zurück, nachdem sie mit 1.5.1 zusammen mit der
Zustands-Heuristik entfernt wurde. Drei Dinge sind bewusst wie vorher, eines
ist bewusst anders:

**Gleich geblieben** — damit die Langzeithistorie weiterläuft:

- Der Speicher heißt weiter ``{DOMAIN}_{entry_id}_feedin_stats`` und trägt
  dasselbe Format (``version 1``, ``current_session``, ``daily`` mit den
  Schlüsseln ``morning`` und ``evening``). Alte Dateien werden gelesen, alte
  Tage bleiben stehen.
- Der Sensor behält seine ``unique_id`` (``..._feedin_evening_heute``), also
  auch seine Entität und ihre Statistik in der Datenbank.
- Aufsummiert wird wie vorher als Rechteck über den Guard-Takt
  (``Leistung × vergangene Zeit``), nicht als Trapez. Ein genaueres Verfahren
  wäre hier das schlechtere: es würde die Reihe an der Umstellung ein zweites
  Mal brechen, diesmal unsichtbar.

**Neu ist die Quelle.** Vorher zählte die Statistik, solange der Zustand
„Nacht-Entladung" anlag. Diesen Zustand gibt es nicht mehr — der Fahrplan
entlädt, wann es sich rechnet, gelegentlich auch mittags. Gezählt wird jetzt,
solange der Executor tatsächlich eine Entladung stellt (``active_kind ==
"discharge"``) und der Modus **Ein** ist. Das ist näher an der Sache als jedes
nachgebaute Zeitfenster, aber es ist **nicht dieselbe Größe wie vorher**.

Dieser Bedeutungswechsel wird deshalb nicht verschwiegen, sondern angezeigt:
der Sensor heißt jetzt „Entladung ins Netz heute" und trägt die Attribute
``zaehlweise`` und ``umgestellt_am``. Wer die Reihe später auswertet, findet
den Sprung — das ist der ganze Unterschied zu einer stillen Umdeutung.

**Der Morgen-Zähler kommt nicht zurück.** Ein laufend nachgeführtes Ladelimit
ist kein Zustand mit Anfang und Ende; eine Tagessumme dafür wäre erfunden.
Sein Schlüssel bleibt im Speicherformat erhalten und alte Tage behalten ihre
Werte — geschrieben wird nur nichts mehr. Seine Entität bleibt ebenfalls
stehen, als „nicht verfügbar": ihre Langzeitstatistik hängt daran, und sie
zu entfernen hieße, gemessene Werte in einem Update wegzuwerfen.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .const import (
    CONF_GRID_POWER_SENSOR,
    CONF_INVERTER_TYPE,
    DOMAIN,
    MODE_EIN,
    STATS_COMPACT_AFTER_DAYS,
)

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.helpers.storage import Store
except ImportError:  # pragma: no cover — Testumgebung ohne HA
    Store = None  # type: ignore[assignment]

try:
    from homeassistant.util import dt as dt_util
except ImportError:  # pragma: no cover
    dt_util = None  # type: ignore[assignment]


# Schlüssel im Speicher. "morning" wird nicht mehr geschrieben, bleibt aber
# Teil des Formats, damit alte Tage lesbar bleiben.
STATS_KEY_ENTLADUNG = "evening"
STATS_KEYS = ("morning", "evening")

# Zählweise, die im Sensor-Attribut steht. Ändert sich die Quelle erneut, muss
# sich dieser Wert ändern — daran erkennt eine Auswertung den Bruch.
ZAEHLWEISE = "fahrplan_entladung"
UMGESTELLT_AM = "2026-08-27"

# Ein Takt, der länger als das her ist, wird nicht aufsummiert: nach einem
# Neustart oder einem hängenden Zyklus wäre die Rechnung sonst ein Sprung.
_MAX_ELAPSED_SECONDS = 120

# Abschnitte unter zwei Minuten werden mit dem vorherigen verschmolzen, statt
# die Liste mit Bruchstücken zu füllen.
_MICRO_SESSION_SECONDS = 120


def _now() -> datetime:
    if dt_util is not None:
        return dt_util.now()
    return datetime.now()  # pragma: no cover — nur ohne HA


def _as_local(zeit: datetime) -> datetime:
    if dt_util is not None:
        return dt_util.as_local(zeit)
    return zeit  # pragma: no cover


def _leerer_schluessel() -> dict:
    return {"sessions": [], "total_kwh": 0.0, "total_duration_min": 0, "count": 0}


def _leerer_tag() -> dict:
    return {key: _leerer_schluessel() for key in STATS_KEYS}


class FeedinStatistics:
    """Zählt die Netzeinspeisung während gesteuerter Entladungen."""

    def __init__(self, hass: Any, entry_id: str, config: dict) -> None:
        self._hass = hass
        self._entry_id = entry_id

        from .power_readings import resolve_sign

        self._grid_sensor_id = config.get(CONF_GRID_POWER_SENSOR, "")
        inv_type = config.get(CONF_INVERTER_TYPE, "")
        self._grid_sign = resolve_sign(inv_type, self._grid_sensor_id, "grid_sign")

        store_key = f"{DOMAIN}_{entry_id}_feedin_stats"
        self._store: Any = Store(hass, 1, store_key) if Store is not None else None

        self._daily: dict[str, dict] = {}
        self._current_session: dict | None = None
        self._last_update_utc: datetime | None = None
        self._dirty = False
        self._first_cycle = True

    # ------------------------------------------------------------------
    # Speicher
    # ------------------------------------------------------------------
    async def async_load(self) -> None:
        """Gespeicherte Statistik laden und alte Abschnittslisten verdichten."""
        if self._store is None:
            return
        try:
            stored = await self._store.async_load()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Keine gespeicherte Einspeise-Statistik gefunden")
            return
        if not stored or not isinstance(stored, dict):
            return

        self._daily = stored.get("daily", {}) or {}
        self._current_session = stored.get("current_session")

        # Eine offene Sitzung aus der Zeit vor einem längeren Ausfall würde
        # sonst Monate später mit absurder Dauer geschlossen (Risiko 4 im
        # UMBAU-FAHRPLAN). Sie wird deshalb beim Laden verworfen, wenn ihr
        # Starttag nicht heute oder gestern ist.
        if self._current_session is not None:
            heute = _now().date()
            try:
                start_tag = date.fromisoformat(self._current_session.get("date", ""))
                veraltet = (heute - start_tag).days > 1
            except (TypeError, ValueError):
                veraltet = True
            if veraltet:
                _LOGGER.info(
                    "Einspeise-Statistik: offene Sitzung vom %s verworfen",
                    self._current_session.get("date"),
                )
                self._current_session = None
                self._dirty = True

        self._verdichte_alte_tage()

    def _verdichte_alte_tage(self) -> None:
        """Abschnittslisten alter Tage löschen — die Summen bleiben."""
        grenze = (
            date.today() - timedelta(days=STATS_COMPACT_AFTER_DAYS)
        ).isoformat()
        for tag, tages_daten in self._daily.items():
            if tag >= grenze or not isinstance(tages_daten, dict):
                continue
            for key in STATS_KEYS:
                daten = tages_daten.get(key)
                if isinstance(daten, dict) and "sessions" in daten:
                    del daten["sessions"]
                    self._dirty = True

    async def async_flush(self) -> None:
        """Auf die Platte schreiben, wenn sich etwas geändert hat."""
        if not self._dirty or self._store is None:
            return
        self._dirty = False
        try:
            await self._store.async_save({
                "version": 1,
                "current_session": self._current_session,
                "daily": self._daily,
            })
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Einspeise-Statistik nicht speicherbar: %s", err)

    # ------------------------------------------------------------------
    # Takt
    # ------------------------------------------------------------------
    async def async_update(self, status: dict | None, mode: str, now_utc: datetime) -> None:
        """Im Guard-Takt aufgerufen: Sitzung führen und Energie aufsummieren.

        Gezählt wird nur, was die Steuerung wirklich tut: eine Entladung im
        Modus Ein. Im Anzeige-Modus schreibt der Executor nichts an den
        Wechselrichter — was dann ins Netz geht, ist nicht sein Werk und
        gehört nicht in diese Reihe.
        """
        aktiv = bool(
            mode == MODE_EIN
            and status
            and status.get("active_kind") == "discharge"
        )
        stats_key = STATS_KEY_ENTLADUNG if aktiv else None

        now_local = _as_local(now_utc)
        heute = now_local.strftime("%Y-%m-%d")

        # Erster Takt nach dem Start: nur die Uhr stellen. Eine wieder
        # eingelesene offene Sitzung wird hier bewusst NICHT geschlossen — der
        # Modus-Schalter ist beim Boot oft noch nicht hydratisiert, das ergäbe
        # ein falsches Sitzungsende.
        if self._first_cycle:
            self._first_cycle = False
            self._last_update_utc = now_utc
            return

        elapsed = 0.0
        if self._last_update_utc is not None:
            elapsed = (now_utc - self._last_update_utc).total_seconds()
        self._last_update_utc = now_utc

        export_kw = self._lies_einspeisung()
        laufend = (
            self._current_session.get("state") if self._current_session else None
        )

        if stats_key is None:
            self._schliesse_sitzung(now_local)
        elif stats_key == laufend:
            self._summiere(export_kw, elapsed)
        else:
            self._schliesse_sitzung(now_local)
            self._oeffne_sitzung(stats_key, now_local, heute)
            self._summiere(export_kw, elapsed)

    def _lies_einspeisung(self) -> float:
        """Einspeiseleistung in kW, nie negativ (Bezug zählt hier nicht)."""
        from .power_readings import read_power_kw

        roh = read_power_kw(self._hass, self._grid_sensor_id)
        if roh is None:
            return 0.0
        return max(roh * self._grid_sign, 0.0)

    # ------------------------------------------------------------------
    # Sitzungen
    # ------------------------------------------------------------------
    def _oeffne_sitzung(self, stats_key: str, now_local: datetime, heute: str) -> None:
        self._current_session = {
            "state": stats_key,
            "start_utc": now_local.astimezone(timezone.utc).isoformat(),
            "start_local": now_local.strftime("%H:%M"),
            "date": heute,
            "accumulated_kwh": 0.0,
        }
        self._dirty = True

    def _summiere(self, export_kw: float, elapsed: float) -> None:
        if self._current_session is None:
            return
        if elapsed <= 0 or elapsed > _MAX_ELAPSED_SECONDS:
            return
        kwh = export_kw * elapsed / 3600.0
        if kwh > 0:
            self._current_session["accumulated_kwh"] += kwh
            self._dirty = True

    def _schliesse_sitzung(self, now_local: datetime) -> None:
        """Laufende Sitzung beenden und in den Tag einbuchen.

        Eine Sitzung bleibt an ihrem **Starttag** hängen, auch wenn sie über
        Mitternacht läuft — sonst wäre eine Nachtentladung auf zwei Tage
        verteilt und keiner der beiden Werte wäre eine Aussage.
        """
        if self._current_session is None:
            return

        sitzung = self._current_session
        self._current_session = None
        key = sitzung.get("state") or STATS_KEY_ENTLADUNG
        tag = sitzung.get("date") or now_local.strftime("%Y-%m-%d")

        try:
            start_utc = datetime.fromisoformat(sitzung["start_utc"])
            dauer_s = (
                now_local.astimezone(timezone.utc) - start_utc
            ).total_seconds()
        except (ValueError, KeyError, TypeError):
            dauer_s = 0.0

        dauer_min = round(dauer_s / 60.0)
        kwh = round(sitzung.get("accumulated_kwh", 0.0), 3)

        # Kurzer Ausschlag ohne Ertrag: verwerfen.
        if dauer_s < _MICRO_SESSION_SECONDS and kwh < 0.01:
            self._dirty = True
            return

        if tag not in self._daily or not isinstance(self._daily.get(tag), dict):
            self._daily[tag] = _leerer_tag()
        tages_daten = self._daily[tag]
        if not isinstance(tages_daten.get(key), dict):
            tages_daten[key] = _leerer_schluessel()
        daten = tages_daten[key]
        if "sessions" not in daten:
            daten["sessions"] = []

        eintrag = {
            "start": sitzung.get("start_local", "?"),
            "end": now_local.strftime("%H:%M"),
            "kwh": kwh,
            "duration_min": dauer_min,
        }

        if dauer_s < _MICRO_SESSION_SECONDS and daten["sessions"] and kwh >= 0.01:
            vorher = daten["sessions"][-1]
            vorher["end"] = eintrag["end"]
            vorher["kwh"] = round(vorher.get("kwh", 0.0) + kwh, 3)
            vorher["duration_min"] = vorher.get("duration_min", 0) + dauer_min
        else:
            daten["sessions"].append(eintrag)

        daten["total_kwh"] = round(daten.get("total_kwh", 0.0) + kwh, 3)
        daten["total_duration_min"] = daten.get("total_duration_min", 0) + dauer_min
        daten["count"] = daten.get("count", 0) + 1
        self._dirty = True

    # ------------------------------------------------------------------
    # Abfragen (Sensor und WebSocket)
    # ------------------------------------------------------------------
    def get_today_kwh(self, stats_key: str) -> float:
        """Heutige Summe samt laufender Sitzung."""
        heute = _now().strftime("%Y-%m-%d")
        summe = 0.0
        # Ein Tageseintrag aus einer beschädigten Datei darf den Sensor nicht
        # reißen — er würde dann dauerhaft „unavailable" zeigen.
        tages_daten = self._daily.get(heute)
        daten = tages_daten.get(stats_key) if isinstance(tages_daten, dict) else None
        if isinstance(daten, dict):
            summe += daten.get("total_kwh", 0.0)

        if (
            self._current_session
            and self._current_session.get("state") == stats_key
            and self._current_session.get("date") == heute
        ):
            summe += self._current_session.get("accumulated_kwh", 0.0)
        return round(summe, 3)

    def get_daily_stats(
        self, start_date: str | None = None, end_date: str | None = None
    ) -> dict:
        """Tageswerte im Zeitraum, aufsteigend sortiert (Grenzen inklusive)."""
        ergebnis = {}
        for tag, daten in sorted(self._daily.items()):
            if start_date and tag < start_date:
                continue
            if end_date and tag > end_date:
                continue
            ergebnis[tag] = daten
        return ergebnis

    def get_summary(self, days: int | None = None) -> dict:
        """Summen über einen Zeitraum. ``days=None`` heißt: alles."""
        jetzt = _now()
        start = (
            (jetzt - timedelta(days=days - 1)).strftime("%Y-%m-%d")
            if days is not None
            else None
        )

        summe: dict[str, dict] = {
            key: {"kwh": 0.0, "count": 0, "duration_min": 0} for key in STATS_KEYS
        }

        for tag, tages_daten in self._daily.items():
            if start and tag < start:
                continue
            if not isinstance(tages_daten, dict):
                continue
            for key in STATS_KEYS:
                daten = tages_daten.get(key) or {}
                summe[key]["kwh"] += daten.get("total_kwh", 0.0)
                summe[key]["count"] += daten.get("count", 0)
                summe[key]["duration_min"] += daten.get("total_duration_min", 0)

        if self._current_session:
            key = self._current_session.get("state")
            tag = self._current_session.get("date")
            if key in summe and (start is None or (tag and tag >= start)):
                summe[key]["kwh"] += self._current_session.get("accumulated_kwh", 0.0)

        for key in STATS_KEYS:
            summe[key]["kwh"] = round(summe[key]["kwh"], 2)
        return summe
