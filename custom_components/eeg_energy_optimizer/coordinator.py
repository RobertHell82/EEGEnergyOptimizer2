"""Consumption profile coordinator for EEG Energy Optimizer.

Lädt Stundenmittelwerte aus den Recorder-Langzeitstatistiken. Aggregiert wird
über zwei Gruppen ("wt" = Werktag, "we" = Wochenende/Feiertag) statt über die
sieben Wochentage einzeln: bei vier Rückblickwochen stehen damit ~20 statt 4
Stützwerte pro Werktagsstunde zur Verfügung. Das ist der eigentliche Grund für
die Gruppierung — mit nur vier Stützwerten schlägt eine einmalige E-Auto-Ladung
mit 25 % Gewicht in die Prognose durch, mit zwanzig nur mit 5 %, und das
getrimmte Mittel (siehe _aggregate) entfernt sie ganz.

calculate_period() liefert daraus die Verbrauchsprognose für beliebige
Zeiträume.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from .const import WEEKDAY_KEYS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Timezone conversion - imported at module level for easy test patching
try:
    from homeassistant.util import dt as dt_util

    _as_local = dt_util.as_local
    _now = dt_util.now
except ImportError:
    _as_local = lambda dt: dt  # noqa: E731
    _now = lambda: datetime.now(tz=timezone.utc)  # noqa: E731

# Feiertagskalender. Optional wie die HA-Importe: fehlt das Paket, läuft die
# Integration weiter und kennt eben nur Sa/So als Wochenende.
try:
    import holidays as _holidays_lib
except ImportError:  # pragma: no cover - Paket ist Requirement, aber nie Pflicht
    _holidays_lib = None
    _LOGGER.debug("Paket 'holidays' nicht verfügbar - Feiertage werden ignoriert")

# Lazy imports for recorder (only available at runtime in HA)
statistics_during_period = None
get_instance = None

# Gruppen des Verbrauchsprofils
BUCKET_WT = "wt"  # Mo-Fr, sofern kein Feiertag
BUCKET_WE = "we"  # Sa, So und jeder Feiertag
BUCKET_KEYS = [BUCKET_WT, BUCKET_WE]

# Fallback, wenn eine Gruppe für eine Stunde überhaupt keine Daten hat.
# Auf Gruppenebene bleibt genau ein sinnvoller Ausweichkandidat übrig.
FALLBACKS: dict[str, list[str]] = {
    BUCKET_WT: [BUCKET_WE],
    BUCKET_WE: [BUCKET_WT],
}

# Ab dieser Anzahl Stützwerte wird der größte Wert vor der Mittelung verworfen
TRIM_MIN_SAMPLES = 5


def _ensure_recorder_imports() -> None:
    """Lazy-import recorder functions (not available during tests without HA)."""
    global statistics_during_period, get_instance  # noqa: PLW0603
    if statistics_during_period is not None:
        return
    try:
        from homeassistant.components.recorder import get_instance as _gi
        from homeassistant.components.recorder.statistics import (
            statistics_during_period as _sdp,
        )

        statistics_during_period = _sdp
        get_instance = _gi
    except ImportError:
        _LOGGER.warning("Recorder not available - statistics will not be loaded")


def _build_holiday_calendar(hass: Any) -> Any:
    """Feiertagskalender für das in HA konfigurierte Land bauen.

    Bewusst komplett fehlertolerant: ein unbekannter oder leerer Ländercode
    darf die Profilberechnung nie kippen — dann gelten schlicht nur Sa/So als
    Wochenende. Subdivisions (Bundesländer) werden absichtlich ignoriert, für
    die Lastprognose ist der Unterschied irrelevant.
    """
    if _holidays_lib is None:
        return None
    try:
        country = getattr(getattr(hass, "config", None), "country", None)
        # isinstance-Prüfung statt bloßem Truthiness-Test: in Tests ist hass
        # ein MagicMock, dessen config.country ein Mock-Objekt wäre.
        if not isinstance(country, str) or not country.strip():
            return None
        return _holidays_lib.country_holidays(country.strip().upper())
    except Exception as err:  # noqa: BLE001 - jede Ursache ist hier harmlos
        _LOGGER.debug("Feiertagskalender nicht verfügbar: %s", err)
        return None


class ConsumptionCoordinator:
    """Lädt Stundenmittelwerte aus dem Recorder, gruppiert in Werktag/Wochenende."""

    def __init__(
        self,
        hass: HomeAssistant,
        consumption_sensor: str,
        lookback_weeks: int,
    ) -> None:
        self.hass = hass
        self._consumption_id = consumption_sensor
        self._lookback_weeks = lookback_weeks

        # Quelle der Wahrheit: {"wt"|"we": {hour: avg_watts}}
        self.bucket_avg: dict[str, dict[int, float]] = {}

        # Aufgefächerte Sicht {weekday: {hour: avg_watts}}. Schema unverändert,
        # weil Panel-Diagramm und Sensor-Attribute (mo_watts … so_nacht_kwh)
        # daran hängen; mo-fr tragen den "wt"-Wert, sa+so den "we"-Wert.
        self.hourly_avg: dict[str, dict[int, float]] = {}
        self.stats_count: int = 0

        # Feiertagskalender: einmal pro Refresh gebaut, nicht pro Zeitstempel
        self._holidays: Any = None
        self._holidays_loaded: bool = False

        # Status für Refresh-Anzeige im Panel
        self.is_running: bool = False
        self.last_update_iso: str | None = None
        self.last_duration_ms: int | None = None

    @property
    def lookback_weeks(self) -> int:
        return self._lookback_weeks

    # ------------------------------------------------------------------
    # Gruppenzuordnung
    # ------------------------------------------------------------------

    def _holiday_calendar(self) -> Any:
        """Kalenderobjekt liefern, beim ersten Zugriff aufbauen.

        Der Refresh baut ihn neu (Land kann sich in HA ändern); wird das Profil
        vorher schon abgefragt, greift dieser Lazy-Pfad.
        """
        if not self._holidays_loaded:
            self._holidays = _build_holiday_calendar(self.hass)
            self._holidays_loaded = True
        return self._holidays

    def _is_holiday(self, day: date) -> bool:
        """Feiertagsprüfung, die nie wirft.

        Das ``holidays``-Objekt füllt fehlende Jahre bei Bedarf selbst nach —
        deshalb funktioniert die Prüfung auch für Fahrplan-Zeitpunkte, die
        jenseits des Rückblickfensters liegen.
        """
        calendar = self._holiday_calendar()
        if calendar is None:
            return False
        try:
            return day in calendar
        except Exception:  # noqa: BLE001 - Prognose darf daran nicht scheitern
            return False

    def bucket_for(self, dt: datetime) -> str:
        """Gruppe eines lokalen Zeitpunkts: Sa/So und Feiertage sind "we"."""
        if dt.weekday() >= 5:
            return BUCKET_WE
        return BUCKET_WE if self._is_holiday(dt.date()) else BUCKET_WT

    def hourly_for(self, dt: datetime) -> float:
        """Mittlere Leistung (W) für einen konkreten lokalen Zeitpunkt.

        Der Weg über die Gruppe ist zwingend: ein Feiertag am Dienstag muss die
        "we"-Werte bekommen. Über hourly_avg["di"] käme der Werktagswert.
        """
        return self.bucket_avg.get(self.bucket_for(dt), {}).get(dt.hour, 0.0)

    # ------------------------------------------------------------------
    # Laden
    # ------------------------------------------------------------------

    async def async_update(self) -> None:
        """Reload hourly averages from recorder statistics.

        Supports two sensor types:
        - state_class=measurement (power sensors, W/kW): uses 'mean' statistics
        - state_class=total_increasing (energy sensors, kWh): uses 'sum' statistics
          and calculates hourly consumption from consecutive sum differences
        """
        self.is_running = True
        t0 = time.monotonic()
        try:
            await self._async_update_impl()
        finally:
            self.last_duration_ms = int((time.monotonic() - t0) * 1000)
            self.last_update_iso = datetime.now(timezone.utc).isoformat()
            self.is_running = False

    async def _async_update_impl(self) -> None:
        _ensure_recorder_imports()

        # Kalender einmal pro Refresh aufbauen — die Einsortierung unten fragt
        # ihn für jeden Statistikeintrag ab.
        self._holidays = _build_holiday_calendar(self.hass)
        self._holidays_loaded = True

        if get_instance is None or statistics_during_period is None:
            _LOGGER.error("Recorder imports not available, cannot load statistics")
            self._init_empty()
            return

        now = _now()
        start = now - timedelta(weeks=self._lookback_weeks)

        # Try mean first (measurement sensors like power in W/kW)
        stats = await self._async_load_statistics(start, now, {"mean"})
        entries = stats.get(self._consumption_id, [])
        has_mean = any(e.get("mean") is not None for e in entries)

        if entries and has_mean:
            _LOGGER.debug("Using 'mean' statistics for %s", self._consumption_id)
            self._process_mean_entries(entries)
            return

        # Fallback: try sum (total_increasing sensors like energy in kWh)
        stats = await self._async_load_statistics(start, now, {"sum"})
        entries = stats.get(self._consumption_id, [])
        has_sum = any(e.get("sum") is not None for e in entries)

        if entries and has_sum:
            _LOGGER.debug("Using 'sum' statistics for %s", self._consumption_id)
            self._process_sum_entries(entries)
            return

        _LOGGER.warning(
            "No consumption statistics for '%s' (tried mean and sum). Available: %s",
            self._consumption_id,
            list(stats.keys()) if stats else "none",
        )
        self._init_empty()
        self.stats_count = 0

    async def _async_load_statistics(
        self, start: datetime, end: datetime, types: set[str]
    ) -> dict[str, list[dict]]:
        """Load statistics from recorder."""
        recorder_instance = get_instance(self.hass)

        result = await recorder_instance.async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            end,
            {self._consumption_id},
            "hour",
            None,
            types,
        )

        return result if isinstance(result, dict) else {}

    @staticmethod
    def _parse_timestamp(ts: Any) -> datetime | None:
        """Parse a timestamp from recorder statistics entry."""
        if isinstance(ts, (int, float)):
            return _as_local(datetime.fromtimestamp(ts, tz=timezone.utc))
        if isinstance(ts, str):
            return _as_local(datetime.fromisoformat(ts))
        return None

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    @staticmethod
    def _new_accumulator() -> dict[str, dict[int, list[float]]]:
        """Leere Sammelstruktur je Gruppe und Stunde."""
        return {bucket: {h: [] for h in range(24)} for bucket in BUCKET_KEYS}

    @staticmethod
    def _aggregate(values: list[float]) -> float:
        """Getrimmtes Mittel: ab TRIM_MIN_SAMPLES Werten fällt der größte weg.

        Einzelereignisse (Wallbox-Ladung, Sauna) sollen den Fahrplan nicht
        prägen — sie sind kein Muster, sondern Rauschen. Ein Median wäre das
        naheliegende Mittel dagegen, würde aber die rechtsschiefe Haushaltslast
        systematisch unterschätzen und den Fahrplan damit zu knapp planen
        lassen. Nur die Spitze zu kappen trifft den Ausreißer und lässt die
        übrige Verteilung in Ruhe.

        Unter TRIM_MIN_SAMPLES Werten wird nicht getrimmt: dort wäre der
        Informationsverlust größer als der Gewinn.
        """
        if len(values) >= TRIM_MIN_SAMPLES:
            return (sum(values) - max(values)) / (len(values) - 1)
        return sum(values) / len(values)

    def _apply_fallbacks(
        self, accum: dict[str, dict[int, list[float]]]
    ) -> dict[str, dict[int, float]]:
        """Gruppenmittelwerte bilden, mit Ausweichgruppe für Datenlücken."""
        result: dict[str, dict[int, float]] = {}
        for bucket in BUCKET_KEYS:
            result[bucket] = {}
            for hour in range(24):
                values = accum[bucket][hour]
                if values:
                    result[bucket][hour] = self._aggregate(values)
                    continue
                found = False
                for fb_bucket in FALLBACKS[bucket]:
                    fb_values = accum[fb_bucket][hour]
                    if fb_values:
                        result[bucket][hour] = self._aggregate(fb_values)
                        found = True
                        break
                if not found:
                    result[bucket][hour] = 0.0
        return result

    @staticmethod
    def _fan_out(bucket_result: dict[str, dict[int, float]]) -> dict[str, dict[int, float]]:
        """Gruppenwerte auf alle sieben Wochentagsschlüssel verteilen.

        Nur für die Anzeige (Panel-Diagramm, Sensor-Attribute); gerechnet wird
        über bucket_avg bzw. hourly_for().
        """
        wt = bucket_result.get(BUCKET_WT, {})
        we = bucket_result.get(BUCKET_WE, {})
        return {
            day: dict(we if index >= 5 else wt)
            for index, day in enumerate(WEEKDAY_KEYS)
        }

    def _finalize(self, bucket_result: dict[str, dict[int, float]], count: int, mode: str) -> None:
        """Store result and log summary."""
        self.bucket_avg = bucket_result
        self.hourly_avg = self._fan_out(bucket_result)
        self.stats_count = count
        _LOGGER.info(
            "Loaded %d consumption statistics (%s). Sample wt[0]=%.0fW, we[12]=%.0fW",
            count,
            mode,
            bucket_result.get(BUCKET_WT, {}).get(0, 0),
            bucket_result.get(BUCKET_WE, {}).get(12, 0),
        )

    def _process_mean_entries(self, entries: list[dict]) -> None:
        """Process 'mean' statistics (measurement sensors, kW).

        The consumption sensor (Hausverbrauch) reports in kW.
        Values are converted to W (* 1000) for internal consistency,
        since calculate_period() expects watts.
        """
        accum = self._new_accumulator()

        corrected = 0
        discarded = 0
        for entry in entries:
            ts = entry.get("start") or entry.get("start_ts")
            mean = entry.get("mean")
            if ts is None or mean is None:
                continue

            local_dt = self._parse_timestamp(ts)
            if local_dt is None:
                continue

            if mean < 0:
                continue

            # Auto-correct old data where W was incorrectly recorded as kW
            # Household consumption above 200 kW is unrealistic and indicates
            # the value was recorded in W instead of kW — divide by 1000.
            if mean > 200.0:
                mean = mean / 1000.0
                corrected += 1

            # Discard values still unrealistic after correction (e.g. from
            # historical data with wrong sign conventions)
            if mean > 50.0:
                discarded += 1
                continue

            # Convert kW to W (consumption sensor always reports in kW)
            value = mean * 1000.0

            accum[self.bucket_for(local_dt)][local_dt.hour].append(value)

        if corrected:
            _LOGGER.info("Auto-corrected %d mean entries (W→kW)", corrected)
        if discarded:
            _LOGGER.info("Discarded %d unrealistic entries (>50 kW after correction)", discarded)

        result = self._apply_fallbacks(accum)
        self._finalize(result, len(entries), "mean")

    def _process_sum_entries(self, entries: list[dict]) -> None:
        """Process 'sum' statistics (total_increasing sensors, kWh).

        Each entry has a cumulative sum. Hourly consumption is derived from
        the difference between consecutive sums. The result is converted
        to average watts: kWh_per_hour * 1000 = W.
        """
        accum = self._new_accumulator()

        prev_sum: float | None = None
        for entry in entries:
            ts = entry.get("start") or entry.get("start_ts")
            current_sum = entry.get("sum")
            if ts is None or current_sum is None:
                prev_sum = None
                continue

            local_dt = self._parse_timestamp(ts)
            if local_dt is None:
                prev_sum = current_sum
                continue

            if prev_sum is not None:
                diff_kwh = current_sum - prev_sum
                # Skip negative diffs (meter reset) and unrealistic spikes
                if 0.0 <= diff_kwh <= 50.0:
                    # Convert kWh consumed in 1 hour to average watts
                    avg_watts = diff_kwh * 1000.0
                    accum[self.bucket_for(local_dt)][local_dt.hour].append(avg_watts)

            prev_sum = current_sum

        result = self._apply_fallbacks(accum)
        self._finalize(result, len(entries), "sum/diff")

    def _init_empty(self) -> None:
        """Beide Sichten mit Nullen füllen (Gruppen und aufgefächerte Wochentage)."""
        self.bucket_avg = {
            bucket: {h: 0.0 for h in range(24)} for bucket in BUCKET_KEYS
        }
        self.hourly_avg = self._fan_out(self.bucket_avg)

    # ------------------------------------------------------------------
    # Prognose
    # ------------------------------------------------------------------

    def calculate_period(
        self, start: datetime, end: datetime
    ) -> dict[str, Any]:
        """Calculate consumption forecast for an arbitrary time period.

        Läuft stundenweise von start bis end und holt sich den Mittelwert über
        hourly_for() — damit stimmt die Prognose auch an Feiertagen, die auf
        einen Werktag fallen. Teilstunden werden anteilig gewichtet.

        Returns dict with verbrauch_kwh, stunden, stundenprofil.
        """
        if end <= start:
            return self._empty_result()

        hours_total = (end - start).total_seconds() / 3600.0
        hourly_details: list[dict[str, Any]] = []
        total_kwh = 0.0

        current = start.replace(minute=0, second=0, microsecond=0)

        while current < end:
            hour = current.hour
            next_hour = current + timedelta(hours=1)

            slot_start = max(current, start)
            slot_end = min(next_hour, end)
            fraction = (slot_end - slot_start).total_seconds() / 3600.0

            if fraction <= 0:
                current = next_hour
                continue

            avg_watts = self.hourly_for(current)

            kwh = (avg_watts * fraction) / 1000.0
            total_kwh += kwh

            hourly_details.append({
                "stunde": f"{hour:02d}:00",
                "wochentag": WEEKDAY_KEYS[current.weekday()],
                "gruppe": self.bucket_for(current),
                "anteil": round(fraction, 2),
                "verbrauch_w": round(avg_watts),
                "kwh": round(kwh, 3),
            })

            current = next_hour

        return {
            "verbrauch_kwh": round(total_kwh, 2),
            "stunden": round(hours_total, 1),
            "stundenprofil": hourly_details,
        }

    @staticmethod
    def _empty_result() -> dict[str, Any]:
        """Return empty calculation result."""
        return {
            "verbrauch_kwh": 0.0,
            "stunden": 0.0,
            "stundenprofil": [],
        }
