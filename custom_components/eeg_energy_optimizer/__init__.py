"""EEG Energy Optimizer integration for Home Assistant."""

from __future__ import annotations

import collections
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import logging

from .power_readings import (
    compute_battery_now_kw,
    compute_grid_export_kw,
    compute_house_load_kw,
    compute_pv_now_kw,
    resolve_battery_capacity_kwh,
)
from .const import (
    DOMAIN,
    MODE_AUS,
    MODE_EIN,
    CONF_BATTERY_CAPACITY_SENSOR,
    CONF_BATTERY_SOC_SENSOR,
    CONF_FORECAST_SOURCE,
    CONF_INVERTER_TYPE,
    CONF_PV_PEAK_KWP,
    CONF_PV_POWER_SENSOR,
    CONF_PV_POWER_SENSOR_2,
    CONF_BATTERY_POWER_SENSOR,
    CONF_BATTERY_POWER_SENSOR_2,
    CONF_BATTERY_POWER_CHARGE_SENSOR,
    CONF_BATTERY_POWER_DISCHARGE_SENSOR,
    CONF_GRID_POWER_SENSOR,
    CONF_GRID_POWER_EXPORT_SENSOR,
    CONF_GRID_POWER_IMPORT_SENSOR,
    CONF_LOOKBACK_WEEKS,
    CONF_TELEMETRY_ENABLED,
    COMBINED_BATTERY_CAPACITY_SENSOR_ID,
    COMBINED_BATTERY_POWER_SENSOR_ID,
    COMBINED_BATTERY_SOC_SENSOR_ID,
    COMBINED_GRID_POWER_SENSOR_ID,
    CONSUMPTION_SENSOR,
    DEFAULT_LOOKBACK_WEEKS,
    FAILURE_DEDUP_WINDOW_S,
    FAILURE_PERSISTENT_DEDUP_WINDOW_S,
    FORECAST_NONE_STREAK_THRESHOLD,
    INVERTER_SIGN_CONVENTIONS,
    SENSOR_UNAVAIL_THRESHOLD_S,
    TELEMETRY_PROFILE_HEARTBEAT_S,
    TELEMETRY_SETTINGS_KEYS,
    TELEMETRY_SNAPSHOT_INTERVAL_MIN,
    TELEMETRY_STEUERUNG,
)
from .inverter import create_inverter
from .schedule_executor import ScheduleExecutor
from .telemetry import TelemetryReporter
from .telemetry_buffer import TelemetryBuffer
from .websocket_api import async_register_websocket_commands

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

try:
    from homeassistant.helpers.event import (
        async_call_later,
        async_track_time_change,
        async_track_time_interval,
    )
except ImportError:
    async_track_time_interval = None  # type: ignore[assignment]
    async_track_time_change = None  # type: ignore[assignment]
    async_call_later = None  # type: ignore[assignment]

try:
    from homeassistant.util import dt as dt_util
except ImportError:  # pragma: no cover — only triggered outside HA
    dt_util = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Phase 8 — geteilte Module-Level-Helfer (W-2 / W-3 / W-6 / I-4)
#
# Diese drei Helfer sind die *einzigen* Stellen, an denen ihre jeweilige
# Aufgabe erledigt wird. Sowohl _async_update_listener als auch
# websocket_api.py::ws_telemetry_enable importieren _build_telemetry_profile
# direkt aus diesem Modul, damit Profil-Shape und integration_started_at-
# Resolver garantiert identisch sind.
# ---------------------------------------------------------------------------


def _resolve_integration_started_at(entry, identity_registered_at):
    """W-3 — einziger Resolver für profile.integration_started_at.

    Reihenfolge:
      1. entry.created_at (HA 2024.x+) → UTC ISO
      2. identity_registered_at (von TelemetryBuffer.set_identity)
      3. None
    """
    created_at = getattr(entry, "created_at", None)
    if created_at is not None:
        try:
            # Bevorzugt UTC-Konvertierung über astimezone — funktioniert für
            # alle datetime-Instanzen unabhängig vom HA dt_util-Stub im Test.
            if hasattr(created_at, "astimezone"):
                return created_at.astimezone(timezone.utc).isoformat()
            if dt_util is not None:
                result = dt_util.as_utc(created_at).isoformat()
                if isinstance(result, str):
                    return result
            return str(created_at)
        except Exception:  # pragma: no cover
            pass
    return identity_registered_at


# Eine Quelle der Wahrheit: dieselbe Auflösung nutzt der Fahrplan
# (schedule.async_collect_inputs). Der Alias haelt die bestehenden
# Aufrufstellen und Tests hier unveraendert.
_resolve_battery_capacity_kwh = resolve_battery_capacity_kwh


def _resolve_pv_peak_kwp(config) -> float | None:
    """Liest die optionale Anlagen-Spitzenleistung (kWp) aus der Config.

    Wird ins Profile-Payload mitgesendet, damit das Backend serverseitige
    Sanity-Caps (z. B. ``predicted_pv_kwh ≤ 2 × pv_peak_kwp``) anwenden kann.
    Nicht gesetzt → ``None`` (Backend nimmt dann keine Caps an).
    """
    raw = config.get(CONF_PV_PEAK_KWP)
    if raw in (None, ""):
        return None
    try:
        val = float(raw)
    except (ValueError, TypeError):
        return None
    return val if val > 0 else None


_APP_VERSION_CACHE: str | None = None


def _read_manifest_version_sync() -> str:
    """Synchroner Manifest-Read — NUR aus einem Executor-Thread aufrufen.

    Wird von _load_app_version via async_add_executor_job verwendet, damit
    das Lesen der manifest.json niemals den HA-Event-Loop blockiert.
    """
    try:
        import json as _json
        import pathlib as _pathlib
        manifest = _json.loads(
            (_pathlib.Path(__file__).parent / "manifest.json").read_text()
        )
        return manifest.get("version", "") or ""
    except Exception:  # pragma: no cover
        return ""


async def _load_app_version(hass) -> str:
    """Lädt die Integrations-Version einmalig in den Modul-Cache.

    Idempotent: bei wiederholtem Aufruf wird der Cache zurückgegeben, ohne
    erneuten Disk-IO. Ein Cache-Miss läuft im Executor (kein Event-Loop-Block).
    """
    global _APP_VERSION_CACHE
    if _APP_VERSION_CACHE is None:
        try:
            _APP_VERSION_CACHE = await hass.async_add_executor_job(
                _read_manifest_version_sync
            )
        except Exception:  # pragma: no cover — defensive
            _APP_VERSION_CACHE = ""
    return _APP_VERSION_CACHE or ""


def _cached_app_version() -> str:
    """Gibt die gecachte Version zurück. Leerstring solange nicht geladen.

    Sync-Caller (z.B. _build_telemetry_profile) müssen ein Pre-Load über
    _load_app_version sicherstellen — der Boot-Pfad in async_setup_entry
    erledigt das vor dem ersten Telemetrie-Send.
    """
    return _APP_VERSION_CACHE or ""


def _build_telemetry_profile(hass, entry, identity_registered_at):
    """I-4 / W-3 — einziger Profil-Builder.

    Wird von BEIDEN Pfaden genutzt:
      - _async_update_listener (Settings-Change → reporter.update_profile)
      - websocket_api.ws_telemetry_enable (Initial-Register → reporter.register)

    Reporter._shape_profile wendet die Whitelist defensiv erneut an, aber die
    Wahrheit lebt hier.
    """
    try:
        from homeassistant.const import __version__ as HA_VERSION
    except ImportError:  # pragma: no cover — Test-Umgebung ohne HA
        HA_VERSION = None

    # HA-Konvention: data + options gemerged
    _data = getattr(entry, "data", {}) or {}
    _options = getattr(entry, "options", {}) or {}
    config = {**_data, **_options}
    app_version = _cached_app_version() or None

    settings = {k: config.get(k) for k in TELEMETRY_SETTINGS_KEYS if k in config}
    # Steuerungs-Kennung — kein Konfigurationswert, sondern eine Eigenschaft
    # dieses Builds. Die produktive Integration mit der Zustands-Heuristik
    # sendet sie nicht; im Backend bedeutet ihr Fehlen "heuristik". Deshalb
    # braucht die bestehende Flotte kein Update, um unterscheidbar zu bleiben.
    # Muss in TELEMETRY_SETTINGS_KEYS stehen, sonst filtert _shape_profile sie
    # wieder heraus.
    settings["steuerung"] = TELEMETRY_STEUERUNG

    return {
        "integration_started_at": _resolve_integration_started_at(
            entry, identity_registered_at
        ),
        "app_version": app_version,
        "ha_version": HA_VERSION,
        "inverter_type": config.get(CONF_INVERTER_TYPE),
        "battery_capacity_kwh": _resolve_battery_capacity_kwh(hass, config),
        "pv_peak_kwp": _resolve_pv_peak_kwp(config),
        "forecast_provider": config.get(CONF_FORECAST_SOURCE),
        "country_iso": getattr(hass.config, "country", None),
        "settings": settings,
    }


def _now_utc() -> datetime:
    """Helper für deterministische UTC-now (Telemetrie-Timestamps)."""
    return datetime.now(tz=timezone.utc)


def _snapshot_state(status: dict) -> str:
    """Zustand des Executors als stabile Kennung für die Telemetrie.

    ``status["status"]`` ist deutscher Anzeigetext und als Auswertungsschlüssel
    untauglich. Gebildet wird die Kennung deshalb aus den Maschinen-Feldern,
    von spezifisch nach allgemein:

    ``unsupported`` (Treiber wird nicht gesteuert) → ``emergency`` (Not-Aus
    sperrt die Entladung) → ``failsafe`` (kein brauchbarer Plan, Wechselrichter
    freigegeben) → die laufende Stellgröße (``charge_limit`` / ``discharge`` /
    ``release``) → ``normal``.
    """
    if status.get("supported") is False:
        return "unsupported"
    if status.get("emergency_blocked_slot") is not None:
        return "emergency"
    if status.get("failsafe_released"):
        return "failsafe"
    kind = status.get("active_kind")
    if kind in ("charge_limit", "discharge", "release"):
        return kind
    return "normal"


def _build_snapshot_payload(
    hass, config, schedule_state, status, mode_str, soc_pct, now
):
    """Momentaufnahme für /v1/snapshot — Schema wie types.ts SnapshotPayload.

    Absichtlich unverändertes Schema: die produktive Integration schreibt in
    dieselbe Tabelle, und eine Auswertung über die ganze Flotte soll nicht
    zwischen zwei Spaltensätzen unterscheiden müssen. Drei Felder tragen unter
    dem Fahrplan eine andere Bedeutung — welche gilt, sagt die
    Steuerungs-Kennung im Profil:

    - ``state`` kommt aus dem Zustandsraum des Executors (siehe
      ``_snapshot_state``) statt aus der Zustands-Heuristik.
    - ``min_soc_dyn`` ist der Mindestladestand, mit dem der Fahrplan
      tatsächlich gerechnet hat (aus dem Ergebnis, nicht aus der
      Konfiguration — dort steht der Wert ungekappt). Ohne Plan bleibt er
      leer; einen dynamisch nachgerechneten Wert gibt es nicht mehr.
    - ``hysteresis`` bleibt leer — der Fahrplan hat keine.

    Die Leistungswerte kommen aus ``power_readings`` und damit aus derselben
    Quelle wie die Steuerung; ein nicht lesbarer Sensor wird ``None``, nicht 0.
    """
    def _kw(value):
        return None if value is None else round(float(value), 3)

    try:
        min_soc = int(round(float((schedule_state or {}).get("min_soc_pct"))))
    except (TypeError, ValueError):
        min_soc = None

    return {
        "ts": now.isoformat() if hasattr(now, "isoformat") else str(now),
        "state": _snapshot_state(status),
        "mode": mode_str,
        "soc_pct": None if soc_pct is None else int(round(float(soc_pct))),
        "pv_now_kw": _kw(compute_pv_now_kw(hass, config)),
        "consumption_now_kw": _kw(compute_house_load_kw(hass, config)),
        "grid_now_kw": _kw(compute_grid_export_kw(hass, config)),
        "battery_now_kw": _kw(compute_battery_now_kw(hass, config)),
        "min_soc_dyn": min_soc,
        "hysteresis": None,
    }


def _check_schedule_health(schedule_state, status, mode, config, dedup, emit):
    """Störungsbilder, die es nur mit der Fahrplan-Steuerung gibt.

    Reine Funktion: ``emit`` ist der Melder (in der Integration
    ``_emit_failure_dedup``), ``dedup`` sein Gedächtnis — beides von außen,
    damit die Verzweigungen ohne Integrations-Setup prüfbar sind.

    Vier Kennungen, gemeldet nur im Modus Ein: im Anzeige-Modus ist keine
    davon ein Problem, sondern der gewollte Zustand.

    - ``schedule_solver`` — der Fahrplan ließ sich nicht rechnen. Der Grund
      steht nur gekürzt im Kontext (bis zum ersten Doppelpunkt): das ist
      entweder unser eigener Klartext ("Verbrauchsprofil noch nicht geladen")
      oder der Name der Ausnahme. Der volle Text kann Pfade enthalten und
      bleibt deshalb lokal — dafür gibt es das Plan-Archiv auf der Anlage.
    - ``inverter_unsupported`` — kein Fehler des Geräts, aber im Modus Ein die
      Erklärung für "meine Anlage tut nichts".
    - ``schedule_stale`` — der Executor hat den Wechselrichter freigegeben,
      weil kein brauchbarer Plan mehr kam. Löst sich nicht selbst, solange der
      Runner hängt.
    - ``guard_emergency`` — anhaltender Netzbezug während einer Entladung hat
      den Not-Aus ausgelöst. Die Slot-Kennung ist Teil des Dedup-Schlüssels,
      jeder neue Slot meldet also erneut.

    Erholt sich ein Dauerzustand, wird sein Dedup-Schlüssel gelöscht: ein
    erneuter Ausfall meldet sich dann sofort und nicht erst nach sechs Stunden.
    """
    if mode != MODE_EIN:
        return

    error = (schedule_state or {}).get("error")
    if error:
        grund = str(error).split(":")[0][:80]
        emit(
            category="schedule_solver",
            severity="error",
            message_hash=grund,
            context={"grund": grund},
            dedup_window_s=FAILURE_PERSISTENT_DEDUP_WINDOW_S,
        )
    else:
        for key in [k for k in dedup if k[0] == "schedule_solver"]:
            dedup.pop(key, None)

    if status.get("supported") is False:
        emit(
            category="inverter_unsupported",
            severity="warning",
            message_hash=config.get(CONF_INVERTER_TYPE) or "unknown",
            context={"inverter_type": config.get(CONF_INVERTER_TYPE)},
            dedup_window_s=FAILURE_PERSISTENT_DEDUP_WINDOW_S,
        )
        # Ohne Steuerung sagen die beiden folgenden Prüfungen nichts.
        return

    if status.get("failsafe_released"):
        emit(
            category="schedule_stale",
            severity="error",
            message_hash="failsafe_released",
            context={"last_run": (schedule_state or {}).get("last_run")},
            dedup_window_s=FAILURE_PERSISTENT_DEDUP_WINDOW_S,
        )
    else:
        dedup.pop(("schedule_stale", "failsafe_released"), None)

    slot = status.get("emergency_blocked_slot")
    if slot is not None:
        emit(
            category="guard_emergency",
            severity="warning",
            message_hash=str(slot),
            context={
                "slot": str(slot),
                "emergency_runs": status.get("emergency_runs"),
            },
        )


PLATFORMS: list[str] = ["sensor", "select"]

async def async_backfill_hausverbrauch_stats(
    hass: HomeAssistant, config: dict
) -> None:
    """Backfill Hausverbrauch statistics from source sensors on every startup.

    Calculates historical Hausverbrauch = max(PV - Battery - Grid, 0) per hour
    from the 3 source sensors and imports them into the HA recorder so that the
    ConsumptionCoordinator can build a consumption profile immediately.

    Runs on every startup — async_import_statistics overwrites existing data
    for the same timestamps, so config changes (e.g. adding a second PV sensor)
    are automatically reflected without manual intervention.

    Silently returns on any error to never block integration startup.
    """
    try:
        from datetime import timezone
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
            async_import_statistics,
        )
        from homeassistant.components.recorder.models import (
            StatisticMetaData,
            StatisticData,
        )
        # StatisticMeanType ist neueres HA — vor 2026.x lebt mean_type als
        # kwarg von async_import_statistics. Wir versuchen den modernen Pfad
        # und fallen sonst auf das Legacy-Verhalten zurück.
        try:
            from homeassistant.components.recorder.models import StatisticMeanType
        except ImportError:  # pragma: no cover — alte HA-Versionen
            StatisticMeanType = None  # type: ignore[assignment]

        now = datetime.now(tz=timezone.utc)
        recorder_instance = get_instance(hass)

        # --- Read source sensor IDs from config ---
        pv_id = config.get(CONF_PV_POWER_SENSOR, "")
        pv2_id = config.get(CONF_PV_POWER_SENSOR_2, "")
        # Battery: either a single signed sensor OR a charge / discharge pair
        battery_charge_id = config.get(CONF_BATTERY_POWER_CHARGE_SENSOR, "")
        battery_discharge_id = config.get(CONF_BATTERY_POWER_DISCHARGE_SENSOR, "")
        has_battery_pair = bool(battery_charge_id and battery_discharge_id)
        battery_single_id = config.get(CONF_BATTERY_POWER_SENSOR, "")
        # Grid: either a single signed sensor OR an export / import pair
        grid_export_id = config.get(CONF_GRID_POWER_EXPORT_SENSOR, "")
        grid_import_id = config.get(CONF_GRID_POWER_IMPORT_SENSOR, "")
        has_grid_pair = bool(grid_export_id and grid_import_id)
        grid_single_id = config.get(CONF_GRID_POWER_SENSOR, "")

        # Decide which entity IDs to actually load history for
        battery_source_ids = (
            [battery_charge_id, battery_discharge_id]
            if has_battery_pair
            else [battery_single_id] if battery_single_id else []
        )
        grid_source_ids = (
            [grid_export_id, grid_import_id]
            if has_grid_pair
            else [grid_single_id] if grid_single_id else []
        )

        if not pv_id or not battery_source_ids or not grid_source_ids:
            _LOGGER.warning(
                "Hausverbrauch backfill skipped — sensor IDs not configured "
                "(PV=%s, Battery=%s, Grid=%s)",
                pv_id or "(empty)",
                battery_source_ids or "(empty)",
                grid_source_ids or "(empty)",
            )
            return

        # Sign conventions per inverter type. For pair configs the synthetic
        # sensor is already canonical, so the convention is identity (1, 1)
        # for those — encoded in INVERTER_SIGN_CONVENTIONS for fronius_gen24.
        # resolve_backfill_signs berücksichtigt zusätzlich Huawei-EMMA-Sensoren
        # (invertiertes Vorzeichen) — MUSS identisch zu den Live-Pfaden sein,
        # sonst überschreibt der Backfill bei jedem Start die korrekt
        # aufgezeichnete Statistik mit falsch berechneten Werten.
        from .power_readings import resolve_backfill_signs
        inv_type = config.get(CONF_INVERTER_TYPE, "")
        signs = INVERTER_SIGN_CONVENTIONS.get(inv_type, {})
        battery_sign, grid_sign = resolve_backfill_signs(config)
        pv_includes_battery = signs.get("pv_includes_battery", False)

        lookback_weeks = config.get(CONF_LOOKBACK_WEEKS, DEFAULT_LOOKBACK_WEEKS)
        start_time = now - timedelta(weeks=lookback_weeks)

        # --- Determine unit conversion factors for source sensors ---
        # Statistics are stored in the sensor's native unit.
        # If a sensor reports in W, we must divide by 1000 to get kW.
        def _unit_factor(entity_id: str) -> float:
            """Return 0.001 if sensor reports in W, else 1.0 (assumes kW)."""
            state = hass.states.get(entity_id)
            if state and hasattr(state, "attributes"):
                unit = (state.attributes.get("unit_of_measurement") or "").strip()
                if unit == "W":
                    return 0.001
            return 1.0

        pv_factor = _unit_factor(pv_id)
        pv2_factor = _unit_factor(pv2_id) if pv2_id else 1.0
        battery_factors = {eid: _unit_factor(eid) for eid in battery_source_ids}
        grid_factors = {eid: _unit_factor(eid) for eid in grid_source_ids}

        _LOGGER.debug(
            "Backfill unit factors: PV=%.3f, PV2=%.3f, Battery=%s, Grid=%s",
            pv_factor, pv2_factor, battery_factors, grid_factors,
        )

        # --- Load mean statistics for all source sensors ---
        sensor_ids = {pv_id, *battery_source_ids, *grid_source_ids}
        if pv2_id:
            sensor_ids.add(pv2_id)

        result = await recorder_instance.async_add_executor_job(
            statistics_during_period,
            hass,
            start_time,
            now,
            sensor_ids,
            "hour",
            None,
            {"mean"},
        )

        pv_entries = result.get(pv_id, [])
        pv2_entries = result.get(pv2_id, []) if pv2_id else []

        # --- Index entries by start timestamp, converting to kW ---
        def _index_by_start(entries: list[dict], factor: float = 1.0) -> dict[float, float]:
            indexed: dict[float, float] = {}
            for e in entries:
                ts = e.get("start") or e.get("start_ts")
                mean = e.get("mean")
                if ts is None or mean is None:
                    continue
                if isinstance(ts, str):
                    ts_float = datetime.fromisoformat(ts).timestamp()
                else:
                    ts_float = float(ts)
                indexed[ts_float] = mean * factor
            return indexed

        # Battery / grid pair-or-single: produce a signed kW series per metric.
        # When a pair is configured: signed = pos − neg (canonical), then *
        # sign convention from INVERTER_SIGN_CONVENTIONS (identity for Fronius).
        # When a single sensor is configured: raw * sign convention as before.
        def _combine_pair_signed(
            entries_pos: list[dict], entries_neg: list[dict],
            factor_pos: float, factor_neg: float,
            sign: int,
        ) -> dict[float, float]:
            pos = _index_by_start(entries_pos, factor_pos)
            neg = _index_by_start(entries_neg, factor_neg)
            keys = set(pos.keys()) | set(neg.keys())
            return {ts: (pos.get(ts, 0.0) - neg.get(ts, 0.0)) * sign for ts in keys}

        def _single_signed(entries: list[dict], factor: float, sign: int) -> dict[float, float]:
            return {ts: v * sign for ts, v in _index_by_start(entries, factor).items()}

        any_battery_entries = any(result.get(eid) for eid in battery_source_ids)
        any_grid_entries = any(result.get(eid) for eid in grid_source_ids)
        use_history_fallback = (
            not pv_entries or not any_battery_entries or not any_grid_entries
        )

        if use_history_fallback:
            _LOGGER.info(
                "Backfill: no long-term statistics for source sensors "
                "(PV=%d, Battery=%s, Grid=%s), trying state history fallback",
                len(pv_entries),
                {eid: len(result.get(eid, [])) for eid in battery_source_ids},
                {eid: len(result.get(eid, [])) for eid in grid_source_ids},
            )
            # --- Fallback: read short-term state history and aggregate hourly ---
            from homeassistant.components.recorder.history import (
                get_significant_states,
            )

            def _load_history():
                return get_significant_states(
                    hass, start_time, now, list(sensor_ids),
                    significant_changes_only=False,
                )

            history = await recorder_instance.async_add_executor_job(_load_history)

            def _history_to_hourly_means(
                states: list,
            ) -> dict[float, float]:
                """Aggregate state history entries into hourly means."""
                from collections import defaultdict
                hourly: dict[float, list[float]] = defaultdict(list)
                for state in states:
                    try:
                        val = float(state.state)
                    except (ValueError, TypeError):
                        continue
                    # Truncate to hour
                    ts = state.last_updated.replace(
                        minute=0, second=0, microsecond=0
                    )
                    hour_ts = ts.timestamp()
                    hourly[hour_ts].append(val)
                result: dict[float, float] = {}
                for hour_ts, values in hourly.items():
                    result[hour_ts] = sum(values) / len(values)
                return result

            pv_by_ts = _history_to_hourly_means(history.get(pv_id, []))
            pv2_by_ts = _history_to_hourly_means(
                history.get(pv2_id, [])
            ) if pv2_id else {}

            def _apply_factor(by_ts: dict[float, float], factor: float) -> dict[float, float]:
                if factor == 1.0:
                    return by_ts
                return {ts: v * factor for ts, v in by_ts.items()}

            pv_by_ts = _apply_factor(pv_by_ts, pv_factor)
            pv2_by_ts = _apply_factor(pv2_by_ts, pv2_factor) if pv2_by_ts else {}

            if has_battery_pair:
                pos_h = _apply_factor(
                    _history_to_hourly_means(history.get(battery_charge_id, [])),
                    battery_factors[battery_charge_id],
                )
                neg_h = _apply_factor(
                    _history_to_hourly_means(history.get(battery_discharge_id, [])),
                    battery_factors[battery_discharge_id],
                )
                keys = set(pos_h) | set(neg_h)
                battery_by_ts = {
                    ts: (pos_h.get(ts, 0.0) - neg_h.get(ts, 0.0)) * battery_sign
                    for ts in keys
                }
            else:
                bat_h = _apply_factor(
                    _history_to_hourly_means(history.get(battery_single_id, [])),
                    battery_factors[battery_single_id],
                )
                battery_by_ts = {ts: v * battery_sign for ts, v in bat_h.items()}

            if has_grid_pair:
                pos_h = _apply_factor(
                    _history_to_hourly_means(history.get(grid_export_id, [])),
                    grid_factors[grid_export_id],
                )
                neg_h = _apply_factor(
                    _history_to_hourly_means(history.get(grid_import_id, [])),
                    grid_factors[grid_import_id],
                )
                keys = set(pos_h) | set(neg_h)
                grid_by_ts = {
                    ts: (pos_h.get(ts, 0.0) - neg_h.get(ts, 0.0)) * grid_sign
                    for ts in keys
                }
            else:
                grid_h = _apply_factor(
                    _history_to_hourly_means(history.get(grid_single_id, [])),
                    grid_factors[grid_single_id],
                )
                grid_by_ts = {ts: v * grid_sign for ts, v in grid_h.items()}

            if not pv_by_ts or not battery_by_ts or not grid_by_ts:
                _LOGGER.warning(
                    "Hausverbrauch backfill skipped — no state history "
                    "(PV=%d, Battery=%d, Grid=%d hours)",
                    len(pv_by_ts), len(battery_by_ts), len(grid_by_ts),
                )
                return

            _LOGGER.info(
                "Backfill: loaded state history "
                "(PV=%d, Battery=%d, Grid=%d hours)",
                len(pv_by_ts), len(battery_by_ts), len(grid_by_ts),
            )
        else:
            pv_by_ts = _index_by_start(pv_entries, pv_factor)
            pv2_by_ts = _index_by_start(pv2_entries, pv2_factor) if pv2_entries else {}

            if has_battery_pair:
                battery_by_ts = _combine_pair_signed(
                    result.get(battery_charge_id, []),
                    result.get(battery_discharge_id, []),
                    battery_factors[battery_charge_id],
                    battery_factors[battery_discharge_id],
                    battery_sign,
                )
            else:
                battery_by_ts = _single_signed(
                    result.get(battery_single_id, []),
                    battery_factors[battery_single_id],
                    battery_sign,
                )

            if has_grid_pair:
                grid_by_ts = _combine_pair_signed(
                    result.get(grid_export_id, []),
                    result.get(grid_import_id, []),
                    grid_factors[grid_export_id],
                    grid_factors[grid_import_id],
                    grid_sign,
                )
            else:
                grid_by_ts = _single_signed(
                    result.get(grid_single_id, []),
                    grid_factors[grid_single_id],
                    grid_sign,
                )

        # --- Calculate Hausverbrauch for each hour where all 3 have data ---
        common_timestamps = sorted(
            set(pv_by_ts.keys()) & set(battery_by_ts.keys()) & set(grid_by_ts.keys())
        )

        if not common_timestamps:
            _LOGGER.warning("Hausverbrauch backfill skipped — no overlapping timestamps")
            return

        # battery_by_ts / grid_by_ts are already in canonical signed kW
        # (positive = charging / positive = export). No further sign flip.
        statistics: list[StatisticData] = []
        battery_stats: list[StatisticData] = []
        grid_stats: list[StatisticData] = []
        skipped = 0
        for ts in common_timestamps:
            pv = pv_by_ts[ts] + pv2_by_ts.get(ts, 0.0)
            bat = battery_by_ts[ts]
            # SolarEdge: PV sensor includes battery discharge → correct
            # Don't clamp — negative from conversion losses needed for accuracy
            if pv_includes_battery:
                pv = pv + bat
            grid = grid_by_ts[ts]
            hausverbrauch = max(pv - bat - grid, 0.0)
            # Discard unrealistic values (wrong signs in historical data)
            if hausverbrauch > 50.0:
                skipped += 1
                continue
            value = round(hausverbrauch, 3)
            hour_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            statistics.append(
                StatisticData(start=hour_dt, mean=value, state=value)
            )
            # Synthetic combined sensors get their own historical statistics
            # so the Energy Dashboard / charts can show them just like a
            # native sensor with full history.
            if has_battery_pair:
                battery_stats.append(
                    StatisticData(start=hour_dt, mean=round(bat, 3), state=round(bat, 3))
                )
            if has_grid_pair:
                grid_stats.append(
                    StatisticData(start=hour_dt, mean=round(grid, 3), state=round(grid, 3))
                )
        if skipped:
            _LOGGER.info("Backfill: skipped %d entries > 50 kW (unrealistic)", skipped)

        # --- Import statistics ---
        def _import(stat_id: str, name: str, data: list[StatisticData]) -> None:
            if not data:
                return
            # Neuere HA-Versionen (2026.x+) erwarten mean_type als Feld in
            # StatisticMetaData. Auf älteren Versionen lebt es als kwarg.
            # HA 2026.11: unit_class wird Pflicht — "power" passt zur Einheit
            # kW (analog Energie/Volume/...). Ohne dieses Feld loggt
            # homeassistant.helpers.frame eine Deprecation-Warnung.
            meta_kwargs = {
                "has_mean": True,
                "has_sum": False,
                "name": name,
                "source": "recorder",
                "statistic_id": stat_id,
                "unit_of_measurement": "kW",
                "unit_class": "power",
            }
            if StatisticMeanType is not None:
                meta_kwargs["mean_type"] = StatisticMeanType.ARITHMETIC
            try:
                meta = StatisticMetaData(**meta_kwargs)
            except TypeError:
                # Älteres HA ohne unit_class- und/oder mean_type-Felder.
                # Schrittweise abwerfen, bis StatisticMetaData die Kwargs
                # akzeptiert (Legacy-Kompatibilität).
                meta_kwargs.pop("unit_class", None)
                try:
                    meta = StatisticMetaData(**meta_kwargs)
                except TypeError:
                    meta_kwargs.pop("mean_type", None)
                    meta = StatisticMetaData(**meta_kwargs)
            try:
                async_import_statistics(hass, meta, data)
            except TypeError:
                # Theoretisch unerreichbar — wenn StatisticMetaData den
                # mean_type aufgenommen hat, akzeptiert async_import_statistics
                # ihn nicht mehr als kwarg. Defensiver Fallback auf Legacy-API.
                async_import_statistics(hass, meta, data, mean_type="arithmetic")

        _import(
            CONSUMPTION_SENSOR,
            "EEG Energy Optimizer Hausverbrauch",
            statistics,
        )
        if has_battery_pair:
            _import(
                COMBINED_BATTERY_POWER_SENSOR_ID,
                "EEG Energy Optimizer Batterieleistung",
                battery_stats,
            )
        if has_grid_pair:
            _import(
                COMBINED_GRID_POWER_SENSOR_ID,
                "EEG Energy Optimizer Netzleistung",
                grid_stats,
            )

        start_date = datetime.fromtimestamp(
            common_timestamps[0], tz=timezone.utc
        ).strftime("%Y-%m-%d")
        end_date = datetime.fromtimestamp(
            common_timestamps[-1], tz=timezone.utc
        ).strftime("%Y-%m-%d")
        extra = ""
        if has_battery_pair or has_grid_pair:
            extra = (
                f" (+ {len(battery_stats)} Batterie / {len(grid_stats)} Netz "
                "synthetic statistics)"
            )
        _LOGGER.info(
            "Backfilled %d hourly statistics for Hausverbrauch from %s to %s%s",
            len(statistics),
            start_date,
            end_date,
            extra,
        )

    except Exception:
        _LOGGER.exception("Hausverbrauch backfill failed (non-critical)")

PANEL_FRONTEND_URL = "/eeg_optimizer_panel"
PANEL_ICON = "mdi:battery-charging-high"
PANEL_TITLE = "EEG Energy Optimizer"
PANEL_URL_PATH = "eeg-optimizer"


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry from older versions."""
    if entry.version < 3:
        new_data = {**entry.data}
        # Add Phase 3 defaults for missing keys
        new_data.setdefault("ueberschuss_schwelle", 1.25)
        new_data.setdefault("morning_end_time", "10:00")
        new_data.setdefault("discharge_start_time", "20:00")
        new_data.setdefault("discharge_power_kw", 3.0)
        new_data.setdefault("min_soc", 10)
        new_data.setdefault("safety_buffer_pct", 25)
        hass.config_entries.async_update_entry(entry, data=new_data, version=3)

    if entry.version < 4:
        new_data = {**entry.data}
        new_data.setdefault("setup_complete", False)
        hass.config_entries.async_update_entry(entry, data=new_data, version=4)

    if entry.version < 5:
        new_data = {**entry.data}
        new_data.setdefault("enable_morning_delay", True)
        new_data.setdefault("enable_night_discharge", True)
        # überschuss_schwelle no longer used — safety_buffer_pct replaces it
        new_data.pop("ueberschuss_schwelle", None)
        hass.config_entries.async_update_entry(entry, data=new_data, version=5)

    if entry.version < 6:
        new_data = {**entry.data}
        new_data.setdefault("grid_power_sensor", "sensor.power_meter_wirkleistung")
        hass.config_entries.async_update_entry(entry, data=new_data, version=6)

    if entry.version < 7:
        new_data = {**entry.data}
        new_data.setdefault("battery_power_sensor", "sensor.batteries_lade_entladeleistung")
        hass.config_entries.async_update_entry(entry, data=new_data, version=7)

    if entry.version < 8:
        new_data = {**entry.data}
        # Switch default consumption sensor to own Hausverbrauch sensor
        if new_data.get("consumption_sensor") == "sensor.power_meter_verbrauch":
            new_data["consumption_sensor"] = "sensor.eeg_energy_optimizer_hausverbrauch"
        hass.config_entries.async_update_entry(entry, data=new_data, version=8)

    if entry.version < 9:
        new_data = {**entry.data}
        new_data.pop("consumption_sensor", None)
        hass.config_entries.async_update_entry(entry, data=new_data, version=9)

    if entry.version < 10:
        new_data = {**entry.data}
        # Preserve existing expert behavior: if expert_mode was on,
        # enable both new features to maintain current dashboard
        is_expert = new_data.get("expert_mode", False)
        new_data.setdefault("enable_simulation", is_expert)
        new_data.setdefault("enable_manual_control", is_expert)
        hass.config_entries.async_update_entry(entry, data=new_data, version=10)

    if entry.version < 11:
        # v11 only bumps the schema version to mark Fronius support — no
        # data backfill needed because fronius_modbus_host/port are written
        # by the wizard when (and only when) the user actually selects
        # Fronius. Existing Huawei/SolaX/SolarEdge entries get the bump
        # without their data dict being touched.
        hass.config_entries.async_update_entry(entry, data=entry.data, version=11)

    if entry.version < 12:
        new_data = {**entry.data}
        new_data.setdefault("enable_peakshare", True)
        new_data.setdefault("peakshare_community", "BEG")
        # Don't change existing discharge_power_kw — only default for new installs is 5.0
        hass.config_entries.async_update_entry(entry, data=new_data, version=12)

    if entry.version < 13:
        # v13 vereint zwei Migrations-Intents (gemeinsam gedraftet):
        #   1. Pair-sensor support (Fronius) — schema-only, pair keys werden
        #      vom Wizard/Auto-Detect geschrieben, wenn der User tatsächlich
        #      ein SolarNet split-sensor Setup hat.
        #   2. Phase 8 Telemetrie (D-02): CONF_TELEMETRY_ENABLED=False als
        #      sicherer Default für alle existierenden Installationen.
        new_data = {**entry.data}
        new_data.setdefault(CONF_TELEMETRY_ENABLED, False)
        hass.config_entries.async_update_entry(entry, data=new_data, version=13)

    if entry.version < 14:
        # v14 — Abend-Entladestart auf 01:00 vereinheitlichen.
        # Hard-Migration: ALLE bestehenden Entries werden auf "01:00" gesetzt,
        # unabhängig vom bisherigen Wert. Begründung:
        #   - In beiden Modi (Fixed + PeakShare) ist discharge_start_time jetzt
        #     der frühestmögliche Entladestart (PeakShare nutzt ihn als Sliding-
        #     Window-Untergrenze). Späterer Start = präzisere Verbrauchsprognose
        #     für den Restbedarf der Nacht = höhere realisierte Einspeisung.
        #   - Der zuvor empfohlene Default 20:00 produzierte zu konservative
        #     min_soc_dyn-Werte und damit kürzere Fenster.
        # User kann den Wert jederzeit im Wizard wieder ändern.
        new_data = {**entry.data}
        new_data["discharge_start_time"] = "01:00"
        hass.config_entries.async_update_entry(entry, data=new_data, version=14)

    if entry.version < 15:
        # v15 — Phase 11: Dual-Window-Entladung
        # Additive Migration: setzt neue Slot-Konfigurations-Keys mit Defaults.
        # Default-Wechsel (D-04, intendiert) — Bestands-Anlagen (nicht
        # SolarEdge) erhalten Dual-Window automatisch beim Update. Mitigation:
        # Pro-Slot-Hysterese und PV-Tomorrow-Garantie verhindern aggressive
        # Erstaktivierung; CHANGELOG dokumentiert die Verhaltensänderung
        # prominent ("Verhaltensänderung beim Update").
        # SolarEdge-Sonderfall (D-03): NVRAM-Verschleiß erlaubt nur einen
        # Slot pro Tag → enable_dual_discharge=False, enable_slot_a=True,
        # enable_slot_b=False. Defense-in-depth in 11-03 (Save-Path) und
        # 11-02 (Runtime-Erzwingung).
        # setdefault statt Hard-Set respektiert vorhandene User-Werte (T-11-01-01).
        new_data = {**entry.data}
        inverter_type = new_data.get("inverter_type", "")
        is_solaredge = inverter_type == "solaredge_storedge"
        if is_solaredge:
            new_data.setdefault("enable_dual_discharge", False)
            new_data.setdefault("enable_slot_a", True)
            new_data.setdefault("enable_slot_b", False)
        else:
            new_data.setdefault("enable_dual_discharge", True)
            new_data.setdefault("enable_slot_a", True)
            new_data.setdefault("enable_slot_b", True)
        new_data.setdefault("discharge_a_start_time", "20:00")
        new_data.setdefault("discharge_b_start_time", "03:00")
        new_data.setdefault("discharge_b_end_cap", "07:00")
        hass.config_entries.async_update_entry(entry, data=new_data, version=15)

    if entry.version < 16:
        # v16 — Phase 12: Dual-Window-Master-Toggle entfernt, Slot-A/B sind
        # die einzige Discharge-Logik. discharge_start_time + enable_dual_discharge
        # werden aus der Config entfernt (Optimizer-Code liest sie nicht mehr).
        # SolarEdge-Sonderfall: bisheriger discharge_start_time wird auf den
        # passenden Slot übertragen, damit das gewohnte Zeitfenster erhalten
        # bleibt. start < 12:00 → Slot B (Morgen-Entladung), sonst Slot A.
        new_data = {**entry.data}
        inv_type = new_data.get("inverter_type", "")
        is_solaredge = inv_type == "solaredge_storedge"
        old_start = new_data.get("discharge_start_time", "")
        if is_solaredge and old_start:
            try:
                old_h = int(str(old_start).split(":")[0])
                if old_h < 12:
                    new_data["enable_slot_a"] = False
                    new_data["enable_slot_b"] = True
                    new_data["discharge_b_start_time"] = old_start
                else:
                    new_data["enable_slot_a"] = True
                    new_data["enable_slot_b"] = False
                    new_data["discharge_a_start_time"] = old_start
            except (ValueError, AttributeError):
                pass
        new_data.pop("discharge_start_time", None)
        new_data.pop("enable_dual_discharge", None)
        hass.config_entries.async_update_entry(entry, data=new_data, version=16)

    if entry.version < 17:
        # v17 — Slot-B-Reserve entfernt. discharge_a_reserve_pct wird aus der
        # Config gestrichen; Slot A entlädt immer bis min_soc_dyn, Slot B
        # nutzt den verbleibenden SOC oberhalb min_soc als Budget.
        new_data = {**entry.data}
        new_data.pop("discharge_a_reserve_pct", None)
        hass.config_entries.async_update_entry(entry, data=new_data, version=17)

    if entry.version < 18:
        # v18 — Phase 12: SolaX Charge-Block via battery_charge_max_current.
        # Neuer Entity-Override-Key für das Lade-Limit (analog zu den
        # existierenden solax_remotecontrol_*-Keys). Bestehende Anlagen
        # bekommen den Default-Entity-Pfad.
        new_data = {**entry.data}
        new_data.setdefault(
            "solax_battery_charge_max_current",
            "number.solax_inverter_battery_charge_max_current",
        )
        hass.config_entries.async_update_entry(entry, data=new_data, version=18)

    if entry.version < 19:
        # v19 — SolarEdge: Auto-Switch auf Driver-side Combined-SOC/Capacity.
        # Bei SolarEdge liefert jeder Modbus-Inverter nur den SOC seiner
        # eigenen Batterie. Der Optimizer-Snapshot überstimmt das jetzt via
        # InverterBase.get_combined_battery_state(), aber das Frontend liest
        # den SOC weiterhin direkt aus battery_soc_sensor. Diese Migration
        # zeigt beide Sensor-Felder auf die neuen synthetischen Combined-
        # Entities um, sodass UI und Optimizer denselben Wert sehen — auch
        # bei Single-Inverter-SolarEdge (Combined liefert dann nur i1's SOC).
        # Andere Inverter (Huawei, Fronius, SolaX): unverändert.
        new_data = {**entry.data}
        if new_data.get("inverter_type") == "solaredge_storedge":
            new_data["battery_soc_sensor"] = COMBINED_BATTERY_SOC_SENSOR_ID
            new_data["battery_capacity_sensor"] = COMBINED_BATTERY_CAPACITY_SENSOR_ID
            # Manueller Capacity-Fallback ist nicht mehr nötig — Driver
            # summiert die echten Sensorwerte. Setze ihn aber nicht zurück,
            # damit der User seine Konfiguration nachvollziehen kann.
        hass.config_entries.async_update_entry(entry, data=new_data, version=19)

    if entry.version < 20:
        # v20 — Feature "Einspeisebegrenzung optimieren" (Huawei/Fronius).
        # Additive, sichere Migration: Feature standardmäßig AUS, damit sich
        # bestehende Installationen nicht verändern. Der Nutzer aktiviert es
        # bewusst im Wizard/Settings. feedin_limit_kw dient nur als sinnvoller
        # Vorbelegungswert und ist ohne aktives Feature wirkungslos.
        # Literale statt Konstanten: die Feature-Konstanten sind mit der
        # Zustands-Heuristik entfallen, die Migration bleibt historisch exakt.
        new_data = {**entry.data}
        new_data.setdefault("enable_feedin_limit", False)
        new_data.setdefault("feedin_limit_kw", 4.0)
        hass.config_entries.async_update_entry(entry, data=new_data, version=20)

    if entry.version < 21:
        # v21 — Altschlüssel der abgeschafften Zustands-Heuristik entfernen.
        # Seit dem Fahrplan-Umbau ist der LP-Fahrplan der einzige Aktor; diese
        # Schlüssel wurden von keiner Codestelle mehr gelesen und lagen nur noch
        # als eingefrorene Migrationswerte in der Konfiguration. Sie zu
        # entfernen macht sichtbar, was die Anlage tatsächlich steuert.
        # Literale statt Konstanten: die zugehörigen Konstanten sind mit der
        # Heuristik entfallen, die Migration bleibt historisch exakt.
        # discharge_a_start_time bleibt bewusst erhalten — sensor.py nutzt ihn
        # als Tag/Nacht-Trenner der Verbrauchsprofil-Anzeige.
        new_data = {**entry.data}
        for obsolete_key in (
            "min_soc",
            "safety_buffer_pct",
            "morning_end_time",
            "enable_morning_delay",
            "enable_night_discharge",
            "enable_slot_a",
            "enable_slot_b",
            "discharge_b_start_time",
            "discharge_b_end_cap",
            "enable_feedin_limit",
            "feedin_limit_kw",
            "enable_simulation",
            "enable_manual_control",
        ):
            new_data.pop(obsolete_key, None)
        hass.config_entries.async_update_entry(entry, data=new_data, version=21)

    if entry.version < 22:
        # v22 — Mittagsabschlag („Batterie früher voll laden").
        #
        # Verhaltensänderung beim Update, bewusst: das Feature ist mit 20 %
        # und 10:00–14:00 AN. Grund ist die Messung, die dazu geführt hat —
        # ohne den Abschlag wird die Batterie an sonnigen Tagen erst am
        # späten Nachmittag voll (gemessen 16:30 statt 13:45), weil frühes
        # Laden für das LP wertlos ist, solange der Tag rechnerisch reicht.
        # Der Erlösverlust ist mit rund 2 ct pro 48 h vernachlässigbar, an
        # trüben Tagen ändert sich nichts. Wer es nicht will, stellt den
        # Abschlag im Panel auf 0.
        #
        # Literale statt Konstanten: die Werte gehören zu diesem
        # Migrationsschritt und dürfen sich nicht mitverschieben, wenn
        # später eine andere Vorgabe sinnvoll wird.
        new_data = {**entry.data}
        new_data.setdefault("schedule_midday_discount_pct", 20.0)
        new_data.setdefault("schedule_midday_discount_start", "10:00")
        new_data.setdefault("schedule_midday_discount_end", "14:00")
        hass.config_entries.async_update_entry(entry, data=new_data, version=22)

    if entry.version < 23:
        # v23 — peakshare_kind entfernen.
        #
        # Der Schlüssel unterschied EEG von BEG und stand in der
        # Konfiguration echter Anlagen, wurde aber von keiner Codestelle
        # gelesen: weder Panel noch Fahrplan noch Preisfunktion fragen ihn
        # ab, auch nicht dynamisch zusammengebaut (geprüft). Die Art der
        # Gemeinschaft steckt faktisch in den Werten — eine BEG bekommt
        # keine Netzgebührenersparnis, also 0 als Gewichtung.
        #
        # v21 hat ihn übersehen, weil er nicht auf der damaligen Liste stand.
        new_data = {**entry.data}
        new_data.pop("peakshare_kind", None)
        new_data.pop("peakshare_kind_2", None)
        hass.config_entries.async_update_entry(entry, data=new_data, version=23)

    if entry.version < 24:
        # v24 — Mittagsabschlag entfernt, der Gemeinschaftsüberschuss tritt
        # an seine Stelle.
        #
        # Das Fenster 10:00–14:00 war geraten und für jede Anlage dasselbe.
        # Seit PeakShare V2 auch den Überschuss der Gemeinschaft liefert, gibt
        # es dieselbe Aussage gemessen: hat die Gemeinschaft Überschuss,
        # findet eingespeister Strom dort keinen Abnehmer und ist weniger wert
        # als in einer Bedarfsstunde. Das senkt den Preis am Mittag von selbst
        # — mit dem Zeitprofil der jeweiligen Gemeinschaft statt einer festen
        # Uhrzeit, und ohne einen Regler, den niemand kalibrieren kann.
        #
        # Die drei Schlüssel werden entfernt statt ignoriert: sie stehen in
        # der Konfiguration echter Anlagen, und ein liegengebliebener Wert,
        # den keine Codestelle mehr liest, ist eine Falle für die nächste
        # Suche. v22 bleibt historisch unverändert stehen und setzt sie
        # weiterhin — die Kette beschreibt, was damals galt, nicht was heute
        # gilt.
        new_data = {**entry.data}
        for obsolete_key in (
            "schedule_midday_discount_pct",
            "schedule_midday_discount_start",
            "schedule_midday_discount_end",
        ):
            new_data.pop(obsolete_key, None)
        hass.config_entries.async_update_entry(entry, data=new_data, version=24)

    if entry.version < 25:
        # v25 — der verschiebbare Überschussabschlag ist wieder entfallen.
        #
        # Der Abschlag selbst bleibt: hat die Gemeinschaft Überschuss, senkt
        # die Optimierung den gerechneten Einspeisepreis, und zwar um die
        # Tarifdifferenz — dieselbe Zahl wie der Aufschlag bei Bedarf. Was
        # entfällt, ist der Regler daneben. Gibt es keine Gemeinschaft, gibt
        # es weiterhin keinen Abschlag; auch dafür braucht es keine
        # Einstellung.
        #
        # Beide Schlüssel werden entfernt statt ignoriert — begründet wie in
        # v24: ein Wert, den keine Codestelle mehr liest, ist eine Falle für
        # die nächste Suche.
        new_data = {**entry.data}
        for obsolete_key in (
            "peakshare_surplus_override",
            "peakshare_surplus_delta",
        ):
            new_data.pop(obsolete_key, None)
        hass.config_entries.async_update_entry(entry, data=new_data, version=25)

    if entry.version < 26:
        # v26 — die Update-Takte sind festverdrahtet (1 min Messwerte,
        # 15 min Verbrauchsprofil).
        #
        # Das schnelle Intervall stand auf dem erlaubten Minimum, das
        # zugleich die Vorgabe war — ein Regler ohne sinnvolle Stellung.
        # Das langsame betraf ein Profil, das sich nur über Wochen ändert;
        # kein bekannter Grund, je davon abzuweichen. Die Schlüssel werden
        # entfernt statt ignoriert — begründet wie in v24: ein Wert, den
        # keine Codestelle mehr liest, ist eine Falle für die nächste Suche.
        new_data = {**entry.data}
        for obsolete_key in (
            "update_interval_fast_min",
            "update_interval_slow_min",
        ):
            new_data.pop(obsolete_key, None)
        hass.config_entries.async_update_entry(entry, data=new_data, version=26)

    if entry.version < 27:
        # v27 — der Ein/Aus-Schalter des Maximum-Ladestands (früher
        # „Ladedeckel") ist entfallen: der Zustand steckt allein im Wert,
        # 100 heißt „bis voll laden" und ist die Vorgabe.
        #
        # War der Schalter aus, blieb ein früher eingestellter Wert bewusst
        # gespeichert, wirkte aber nicht. Ohne diese Korrektur begänne er
        # nach dem Update plötzlich zu wirken — deshalb wird er dann auf 100
        # gestellt. Der Schalter-Schlüssel selbst wird entfernt statt
        # ignoriert, begründet wie in v24.
        new_data = {**entry.data}
        enabled = new_data.pop("schedule_max_soc_enabled", False)
        if not enabled and "schedule_max_soc_pct" in new_data:
            new_data["schedule_max_soc_pct"] = 100
        hass.config_entries.async_update_entry(entry, data=new_data, version=27)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EEG Energy Optimizer from a config entry."""
    from homeassistant.components.frontend import (
        async_register_built_in_panel,
        async_remove_panel,
    )
    from homeassistant.components.http import StaticPathConfig

    hass.data.setdefault(DOMAIN, {})
    config = {**entry.data, **entry.options}
    setup_complete = config.get("setup_complete", False)

    # Cache-Invalidate: Module-State bleibt bei Config-Entry-Reload bestehen,
    # daher würde der nach HACS-Update geänderte manifest.json-Wert nicht
    # gelesen werden, bevor HA komplett neu startet. Beim Setup-Aufruf den
    # Cache zurücksetzen, damit _load_app_version frisch von Disk liest.
    global _APP_VERSION_CACHE
    _APP_VERSION_CACHE = None

    # Register WebSocket commands (always — panel needs them even before setup)
    async_register_websocket_commands(hass)

    # Register frontend panel (always — user needs panel to complete setup)
    # Skip if already registered (e.g. during config entry reload)
    frontend_path = str(Path(__file__).parent / "frontend")
    if not hass.data.get(f"{DOMAIN}_static_registered"):
        try:
            await hass.http.async_register_static_paths(
                [StaticPathConfig(PANEL_FRONTEND_URL, frontend_path, cache_headers=False)]
            )
            hass.data[f"{DOMAIN}_static_registered"] = True
        except Exception:
            hass.data[f"{DOMAIN}_static_registered"] = True  # Already registered

    # Read version from manifest for cache-busting query parameter.
    # Cached in module state to avoid blocking disk IO on every panel load
    # (HA 2026.x detects this as a blocking_call_inside_event_loop offense
    # and the warmer-than-expected manifest.json access measurably stalls
    # the loop on slow storage).
    panel_version = await _load_app_version(hass) or "0"

    # Always re-register panel to update cache-busting version in js_url
    if PANEL_URL_PATH in hass.data.get("frontend_panels", {}):
        async_remove_panel(hass, PANEL_URL_PATH)
    async_register_built_in_panel(
        hass,
        component_name="custom",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        frontend_url_path=PANEL_URL_PATH,
        config={
            "_panel_custom": {
                "name": "eeg-optimizer-panel",
                "embed_iframe": False,
                "trust_external": False,
                "js_url": f"{PANEL_FRONTEND_URL}/eeg-optimizer-panel.js?v={panel_version}",
            }
        },
        require_admin=False,
    )

    hass.data[DOMAIN][entry.entry_id] = {
        "config": config,
        "inverter": None,
        "platforms_loaded": False,
    }

    # If setup not complete, register panel only — skip platforms and optimizer
    if not setup_complete:
        entry.async_on_unload(entry.add_update_listener(_async_update_listener))
        return True

    # Full setup: inverter, platforms, Fahrplan-Executor
    try:
        inverter = create_inverter(config.get("inverter_type", ""), hass, config)
    except ValueError as err:
        _LOGGER.error("Failed to create inverter: %s", err)
        from homeassistant.components.persistent_notification import async_create
        async_create(
            hass,
            f"EEG Energy Optimizer: Wechselrichter konnte nicht erstellt werden — {err}",
            title="EEG Energy Optimizer Fehler",
            notification_id="eeg_inverter_error",
        )
        return False
    hass.data[DOMAIN][entry.entry_id]["inverter"] = inverter

    # Restore persisted register write counter
    from homeassistant.helpers.storage import Store as _Store
    writes_store = _Store(hass, 1, f"{DOMAIN}_{entry.entry_id}_register_writes")
    try:
        stored_writes = await writes_store.async_load()
        if stored_writes and isinstance(stored_writes, int):
            inverter.register_writes = stored_writes
            _LOGGER.debug("Restored register write counter: %d", stored_writes)
    except Exception:
        pass
    hass.data[DOMAIN][entry.entry_id]["writes_store"] = writes_store

    # Migration: earlier builds of the synthetic Fronius pair sensors used
    # suggested_object_id without pinning entity_id. HA prefixed the device
    # slug anyway, producing IDs like
    #   sensor.eeg_energy_optimizer_eeg_energy_optimizer_battery_power
    # which do not match the canonical IDs the rest of the integration
    # writes into config (CONF_BATTERY_POWER_SENSOR / CONF_GRID_POWER_SENSOR
    # → COMBINED_*_SENSOR_ID). Result: Hausverbrauch / Netzleistung /
    # Batterieleistung read from a non-existent entity → "unknown".
    # Rename the legacy registry entries back to canonical before the
    # platforms are forwarded so the new sensor classes attach cleanly.
    try:
        from homeassistant.helpers import entity_registry as er
        ent_reg = er.async_get(hass)
        for unique_id, canonical in (
            (f"{DOMAIN}_{entry.entry_id}_battery_power_combined", COMBINED_BATTERY_POWER_SENSOR_ID),
            (f"{DOMAIN}_{entry.entry_id}_grid_power_combined", COMBINED_GRID_POWER_SENSOR_ID),
        ):
            existing = ent_reg.async_get_entity_id("sensor", DOMAIN, unique_id)
            if existing and existing != canonical:
                # Free the canonical slot if a stale entity squats on it
                blocker = ent_reg.async_get(canonical)
                if blocker and blocker.unique_id != unique_id:
                    ent_reg.async_update_entity(
                        canonical, new_entity_id=f"{canonical}_legacy"
                    )
                ent_reg.async_update_entity(existing, new_entity_id=canonical)
                _LOGGER.info(
                    "Renamed combined sensor %s -> %s", existing, canonical
                )
    except Exception:
        _LOGGER.exception("Combined-sensor entity_id migration failed (non-fatal)")

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    hass.data[DOMAIN][entry.entry_id]["platforms_loaded"] = True

    # After platforms are set up, coordinator/provider/select are available
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data.get("coordinator")
    provider = data.get("provider")

    # PeakShare: Bedarfsprognose der Gemeinschaften. Seit der EEG-Preisfunktion
    # keine reine Anzeige mehr — der Fahrplan rechnet damit.
    from .peakshare import PeakShareProvider
    peakshare_provider = PeakShareProvider(hass, entry.entry_id)
    await peakshare_provider.async_load()
    await peakshare_provider.async_fetch()
    data["peakshare"] = peakshare_provider

    # OeMAG: monatlicher Einspeisetarif, falls als Basistarif gewählt. Wird
    # immer geholt, damit das Panel den Wert auch zeigen kann, bevor jemand
    # umschaltet.
    from .oemag import OemagProvider
    oemag_provider = OemagProvider(hass, entry.entry_id)
    await oemag_provider.async_load()
    data["oemag"] = oemag_provider
    # Nicht im Setup abrufen: eine langsame Website würde den Start des
    # Integrationsaufbaus verzögern. Der erste Lauf kommt als Task.
    hass.async_create_task(oemag_provider.async_fetch())

    # Spotpreis (EPEX Day-Ahead über aWATTar), falls als Basistarif gewählt.
    # Immer geladen (fürs Panel), aber nur automatisch abgerufen, wenn die
    # Quelle wirklich „spot" ist — kein Dauerverkehr für Nutzer, die die
    # Börse nie gewählt haben. Der „Jetzt holen"-Knopf holt per WS trotzdem.
    from .spot import SpotProvider
    spot_provider = SpotProvider(
        hass, entry.entry_id, market=str(config.get("spot_market_area") or "at")
    )
    await spot_provider.async_load()
    data["spot"] = spot_provider
    if str(config.get("schedule_feedin_source") or "manual").lower() == "spot":
        hass.async_create_task(spot_provider.async_fetch())

    if coordinator and provider:
        # ----------------------------------------------------------
        # Phase 8: Telemetry Reporter Lifecycle (D-04 .. D-06)
        # Profile und Failures laufen weiter; State-Changes, Snapshots und
        # Outcomes sind mit der Zustands-Heuristik entfallen (ihre Semantik
        # war zustandsgebunden — siehe UMBAU-FAHRPLAN.md, Risiko 5).
        # ----------------------------------------------------------
        telemetry_buffer = TelemetryBuffer(hass)
        await telemetry_buffer.load()
        reporter = TelemetryReporter(hass, telemetry_buffer)
        data["telemetry_buffer"] = telemetry_buffer
        data["telemetry_reporter"] = reporter
        # (category, message_hash) -> last-emit datetime (UTC)
        data["telemetry_failure_dedup"] = {}
        data["telemetry_forecast_none_streak"] = 0
        # sensor_role -> datetime|None (None = aktuell verfügbar)
        data["telemetry_sensor_unavail_since"] = {}
        # Momentaufnahmen: gesammelt im Guard-Takt, gesendet im Flush-Timer.
        data["telemetry_snapshot_queue"] = []
        # Halbstunden-Raster (Stunde × 2 + Minute // 30) der letzten Ablage.
        # Über das Raster statt über einen eigenen Timer, damit die Zeitpunkte
        # zwischen Anlagen vergleichbar bleiben und ein Neustart den Takt nicht
        # verschiebt.
        data["telemetry_snapshot_slot"] = None

        # ----------------------------------------------------------
        # Telemetrie-Failure-Helper (closures über data + reporter)
        # ----------------------------------------------------------
        def _emit_failure_dedup(
            *, category, severity, message_hash, context,
            dedup_window_s=FAILURE_DEDUP_WINDOW_S,
        ):
            """Störung melden, höchstens einmal je Fenster und Kennung.

            Dauerzustände übergeben ``FAILURE_PERSISTENT_DEDUP_WINDOW_S`` (6 h):
            sie heilen nicht von selbst, und stündlich dieselbe Meldung wäre nur
            Lärm. Transiente Fehler behalten das Stundenfenster.
            """
            key = (category, message_hash)
            last = data["telemetry_failure_dedup"].get(key)
            now_ts = _now_utc()
            if last is not None and (now_ts - last).total_seconds() < dedup_window_s:
                return
            data["telemetry_failure_dedup"][key] = now_ts
            payload = {
                "ts": now_ts.isoformat(),
                "category": category,
                "severity": severity,
                "message_hash": message_hash,
                "context": context,
            }
            try:
                hass.async_create_task(reporter.send_failure(payload))
            except Exception:  # pragma: no cover — defensive
                _LOGGER.exception("Telemetry: failed to schedule send_failure")

        def _executor_failure_callback(action):
            """W-4 — Schreibfehler des Fahrplan-Executors → /v1/failure (D-16).

            Die Treiber fangen ihre Exceptions selbst und liefern False —
            deshalb kommt hier nur noch die fehlgeschlagene Aktion an
            (charge_limit / discharge / release), kein Exception-Objekt.
            """
            _emit_failure_dedup(
                category="inverter_write",
                severity="error",
                message_hash=f"executor_{action}",
                context={
                    "inverter_type": config.get(CONF_INVERTER_TYPE),
                    "action": action,
                },
            )

        def _check_sensor_unavailability():
            """D-16 — 10-min Watchdog auf 5 essenzielle Sensoren."""
            roles = {
                "battery_soc": config.get(CONF_BATTERY_SOC_SENSOR, ""),
                "pv_power": config.get(CONF_PV_POWER_SENSOR, ""),
                "grid_power": config.get(CONF_GRID_POWER_SENSOR, ""),
                "battery_power": config.get(CONF_BATTERY_POWER_SENSOR, ""),
                "hausverbrauch": CONSUMPTION_SENSOR,
            }
            now_ts = _now_utc()
            for role, eid in roles.items():
                if not eid:
                    data["telemetry_sensor_unavail_since"][role] = None
                    continue
                state = hass.states.get(eid)
                unavailable = (
                    state is None
                    or getattr(state, "state", None) in ("unknown", "unavailable", "")
                )
                since = data["telemetry_sensor_unavail_since"].get(role)
                if unavailable:
                    if since is None:
                        data["telemetry_sensor_unavail_since"][role] = now_ts
                    elif (now_ts - since).total_seconds() >= SENSOR_UNAVAIL_THRESHOLD_S:
                        _emit_failure_dedup(
                            category="sensor_unavailable",
                            severity="warning",
                            message_hash=role,
                            context={"sensor_role": role, "entity_id": eid},
                            dedup_window_s=FAILURE_PERSISTENT_DEDUP_WINDOW_S,
                        )
                else:
                    data["telemetry_sensor_unavail_since"][role] = None
                    # Zustands-Semantik: nach einer Erholung meldet sich ein
                    # erneuter Ausfall sofort, nicht erst nach Fensterablauf.
                    data["telemetry_failure_dedup"].pop(
                        ("sensor_unavailable", role), None
                    )

        def _check_forecast_streak(forecast):
            """D-16 — 3 None-Forecasts in Folge → Failure (1 h Dedup)."""
            try:
                remaining = forecast.remaining_today_kwh
                tomorrow = forecast.tomorrow_kwh
            except AttributeError:
                return
            if remaining is None and tomorrow is None:
                data["telemetry_forecast_none_streak"] += 1
                if data["telemetry_forecast_none_streak"] >= FORECAST_NONE_STREAK_THRESHOLD:
                    _emit_failure_dedup(
                        category="forecast_provider",
                        severity="warning",
                        message_hash="all_none",
                        context={
                            "forecast_source": config.get(CONF_FORECAST_SOURCE),
                        },
                        dedup_window_s=FAILURE_PERSISTENT_DEDUP_WINDOW_S,
                    )
            else:
                data["telemetry_forecast_none_streak"] = 0
                data["telemetry_failure_dedup"].pop(
                    ("forecast_provider", "all_none"), None
                )

        def _collect_snapshot(schedule_state, status, mode, soc_pct):
            """Alle 30 Minuten eine Momentaufnahme in die Warteschlange legen.

            Kein eigener Timer: der Guard-Takt kommt ohnehin alle 30 Sekunden
            vorbei, und das Halbstunden-Raster hält die Zeitpunkte über alle
            Anlagen vergleichbar — auch über einen Neustart hinweg.
            """
            now_ts = _now_utc()
            slot = now_ts.hour * 2 + now_ts.minute // TELEMETRY_SNAPSHOT_INTERVAL_MIN
            if slot == data.get("telemetry_snapshot_slot"):
                return
            data["telemetry_snapshot_slot"] = slot
            mode_str = "ein" if mode == MODE_EIN else "test"
            payload = _build_snapshot_payload(
                hass, config, schedule_state, status, mode_str, soc_pct, now_ts,
            )
            queue = data["telemetry_snapshot_queue"]
            queue.append(payload)
            # Obergrenze: bei dauerhaft unerreichbarem Backend darf die
            # Warteschlange nicht wachsen. 100 Einträge sind gut zwei Tage.
            if len(queue) > 100:
                del queue[: len(queue) - 100]

        # ----------------------------------------------------------
        # Fahrplan-Executor — der einzige Aktor. Rechnet nicht selbst,
        # sondern hält den zuletzt gerechneten Fahrplan (ScheduleRunner)
        # alle 30 Sekunden gegen die Messwerte und steuert nach.
        # ----------------------------------------------------------
        executor = ScheduleExecutor(
            hass, entry.entry_id, config, inverter,
            failure_callback=_executor_failure_callback,
        )
        data["executor"] = executor

        # ----------------------------------------------------------
        # Einspeise-Statistik: zählt, was während einer gesteuerten
        # Entladung ins Netz geht. Speicherformat und Sensor-Kennung sind
        # dieselben wie vor dem Umbau, die Quelle ist der Executor statt
        # eines Zustands — Einzelheiten in statistics.py.
        # ----------------------------------------------------------
        from .statistics import FeedinStatistics

        feedin_stats = FeedinStatistics(hass, entry.entry_id, config)
        await feedin_stats.async_load()
        data["feedin_stats"] = feedin_stats

        # Telemetrie-Hooks im Closure-Scope für späteren Zugriff (Tests)
        data["_check_sensor_unavailability"] = _check_sensor_unavailability
        data["_check_forecast_streak"] = _check_forecast_streak
        data["_collect_snapshot"] = _collect_snapshot
        data["_emit_failure_dedup"] = _emit_failure_dedup

        # Activity log: persistent ring buffer (last 5000 entries)
        from homeassistant.helpers.storage import Store
        ACTIVITY_STORE_KEY = f"{DOMAIN}_{entry.entry_id}_activity"
        activity_store = Store(hass, 1, ACTIVITY_STORE_KEY)
        activity_log = collections.deque(maxlen=5000)
        data["activity_log"] = activity_log
        activity_dirty = [False]

        # Load persisted entries
        try:
            stored = await activity_store.async_load()
            if stored and isinstance(stored, list):
                activity_log.extend(stored)
                _LOGGER.debug("Loaded %d activity log entries", len(stored))
        except Exception:
            _LOGGER.debug("No persisted activity log found")

        async def _save_activity_log():
            """Mark log as dirty — actual save happens at end of each cycle."""
            activity_dirty[0] = True

        async def _flush_activity_log():
            """Persist activity log to disk if dirty."""
            if not activity_dirty[0]:
                return
            activity_dirty[0] = False
            try:
                await activity_store.async_save(list(activity_log))
            except Exception as err:
                _LOGGER.warning("Failed to save activity log: %s", err)

        prev_zustand = [None]  # mutable container for closure
        last_heartbeat_hour = [None]  # track last logged hour
        first_cycle = [True]  # skip logging on first cycle (sensors not ready)

        def _read_soc():
            """Batterie-Ladestand fürs Aktivitätsprotokoll (None wenn nicht lesbar)."""
            state = hass.states.get(config.get(CONF_BATTERY_SOC_SENSOR, ""))
            if state is None or state.state in ("unknown", "unavailable", ""):
                return None
            try:
                return round(float(state.state), 1)
            except (ValueError, TypeError):
                return None

        def _log_activity(zustand, reason, status, mode):
            """Append an activity entry and fire a HA event."""
            entry_data = {
                "timestamp": _now_utc().isoformat(),
                "zustand": zustand,
                "reason": reason,
                "status": status.get("status"),
                "soc": _read_soc(),
                "plan": status.get("plan_action"),
                "schreibfehler": status.get("write_failures"),
                "ausführung": mode == MODE_EIN,
            }
            activity_log.append(entry_data)
            hass.bus.async_fire("eeg_optimizer_activity", entry_data)
            hass.async_create_task(_save_activity_log())

        async def _guard_cycle(_now=None):
            """30-Sekunden-Takt: Fahrplan gegen die Messwerte halten und steuern.

            Der Executor entscheidet selbst, ob geschrieben wird (Modus Ein,
            Grace Period, Failsafe, Totbänder) — hier passiert nur Verdrahtung:
            Modus aus dem Select, Fahrplan-Zustand aus dem Runner, Status in
            den Sensor, Aktivitätslog, Telemetrie-Watchdogs, Log-Flush.
            """
            select = data.get("select")
            mode = select._attr_current_option if select else MODE_AUS
            current_executor = data.get("executor")
            if current_executor is None:
                return
            runner = data.get("schedule")
            schedule_state = runner.to_dict() if runner is not None else None

            await current_executor.async_guard_cycle(schedule_state, mode)
            status = current_executor.status()

            status_sensor = data.get("decision_sensor")
            if status_sensor is not None:
                zustand = status_sensor.update_from_executor(status)
            else:
                zustand = status.get("status") or ""

            # ----------------------------------------------------------
            # Telemetrie-Watchdogs (D-16) — Sensor-Unavailability + Forecast
            # ----------------------------------------------------------
            cfg_enabled = config.get(CONF_TELEMETRY_ENABLED, False)
            telemetry_active = (
                cfg_enabled
                and reporter.is_configured
                and telemetry_buffer.identity_known()
            )
            if telemetry_active and not first_cycle[0] and mode != MODE_AUS:
                try:
                    _check_sensor_unavailability()
                except Exception:  # pragma: no cover
                    _LOGGER.exception("Telemetry: sensor watchdog failed")
                try:
                    current_provider = data.get("provider")
                    if current_provider is not None:
                        _check_forecast_streak(current_provider.get_forecast())
                except Exception:  # pragma: no cover
                    _LOGGER.exception("Telemetry: forecast watchdog failed")
                try:
                    _check_schedule_health(
                        schedule_state, status, mode, config,
                        data["telemetry_failure_dedup"], _emit_failure_dedup,
                    )
                except Exception:  # pragma: no cover
                    _LOGGER.exception("Telemetry: schedule watchdog failed")
                try:
                    _collect_snapshot(schedule_state, status, mode, _read_soc())
                except Exception:  # pragma: no cover
                    _LOGGER.exception("Telemetry: snapshot collect failed")

            # Profile-Drift-Self-Heal: Wenn der Kapazitäts-Sensor beim Boot
            # noch unknown war, hat _boot_telemetry_send den manuellen
            # Fallback gesendet (z.B. 10 kWh). Sobald der Sensor jetzt
            # einen anderen Wert liefert, gleichen wir das Backend-Profil
            # einmalig nach. Läuft nur, wenn überhaupt schon ein Profil
            # gesendet wurde (Schlüssel im data-Dict vorhanden).
            if (
                telemetry_active
                and "telemetry_last_profile_capacity_kwh" in data
            ):
                try:
                    live_cap = _resolve_battery_capacity_kwh(hass, config)
                except Exception:  # pragma: no cover — defensive
                    live_cap = None
                if live_cap != data["telemetry_last_profile_capacity_kwh"]:
                    # Sofort markieren, damit ein laufender Cycle nicht
                    # mehrfach denselben Re-Send queued.
                    data["telemetry_last_profile_capacity_kwh"] = live_cap

                    async def _resend_profile_for_capacity_drift():
                        try:
                            ident = telemetry_buffer.get_identity() or {}
                            profile = _build_telemetry_profile(
                                hass, entry,
                                identity_registered_at=ident.get("registered_at"),
                            )
                            await reporter.update_profile(profile)
                            # Authoritative: was tatsächlich gesendet wurde.
                            data["telemetry_last_profile_capacity_kwh"] = (
                                profile.get("battery_capacity_kwh")
                            )
                        except Exception:  # pragma: no cover
                            _LOGGER.exception(
                                "Telemetry: capacity drift profile "
                                "re-send failed",
                            )
                    hass.async_create_task(
                        _resend_profile_for_capacity_drift()
                    )

            # Aktivitätsprotokoll: Statuswechsel + Stunden-Heartbeat.
            # Erster Zyklus wird übersprungen — Sensoren liefern noch nichts.
            if first_cycle[0]:
                first_cycle[0] = False
                prev_zustand[0] = zustand
            elif zustand != prev_zustand[0]:
                _log_activity(zustand, status.get("status") or zustand, status, mode)
                prev_zustand[0] = zustand
            else:
                from datetime import datetime as dt
                current_hour = dt.now().hour
                if current_hour != last_heartbeat_hour[0]:
                    _log_activity(zustand, "Heartbeat", status, mode)
                    last_heartbeat_hour[0] = current_hour

            # Einspeise-Statistik führen. Nach dem Aktivitätsprotokoll, weil
            # sie den Executor-Status dieses Laufs braucht, und vor dem
            # Schreiben, damit beide Dateien im selben Takt landen.
            try:
                await feedin_stats.async_update(status, mode, _now_utc())
                await feedin_stats.async_flush()
            except Exception:  # pragma: no cover — Statistik darf nie kippen
                _LOGGER.exception("Einspeise-Statistik: Takt fehlgeschlagen")

            # Persist activity log to disk if changed
            await _flush_activity_log()

            # Persist register write counter (only if changed)
            _ws = data.get("writes_store")
            if _ws and inverter.register_writes > 0:
                try:
                    await _ws.async_save(inverter.register_writes)
                except Exception:
                    pass

        data["_run_cycle"] = _guard_cycle

        if async_track_time_interval is not None:
            unsub = async_track_time_interval(
                hass, _guard_cycle, timedelta(seconds=30)
            )
            entry.async_on_unload(unsub)

            # Run initial cycle immediately — sensors are already populated
            # by the synchronous slow+fast update in async_setup_entry
            await _guard_cycle()

            # ----------------------------------------------------------
            # Fahrplan-Runner (chamo/): rechnet jede Minute einen LP-Fahrplan.
            # Der Guard-Lauf oben setzt den laufenden Slot am Wechselrichter
            # durch; ohne frischen Fahrplan greift dort der Failsafe.
            # ----------------------------------------------------------
            from .schedule import DEFAULT_INTERVAL_MIN, ScheduleRunner

            schedule_runner = ScheduleRunner(hass, entry.entry_id)
            data["schedule"] = schedule_runner

            # Archiv der gerechneten Fahrpläne: der Plan überschreibt sich
            # minütlich selbst, und ohne Kopie ist die Erklärung für ein
            # Verhalten von gestern abend hinterher nicht mehr auffindbar.
            from .schedule_archive import ScheduleArchive

            schedule_archive = ScheduleArchive(
                hass, entry.entry_id, await _load_app_version(hass)
            )
            data["schedule_archive"] = schedule_archive

            from .schedule_archive_view import async_register_archive_view

            async_register_archive_view(hass)

            async def _schedule_cycle(_now=None):
                await schedule_runner.async_run()
                try:
                    await schedule_archive.async_maybe_store(
                        schedule_runner.to_dict(),
                        {**entry.data, **entry.options},
                        dt_util.now(),
                    )
                except Exception:  # noqa: BLE001 - Archivieren darf den Takt nie kippen
                    _LOGGER.debug("Fahrplan-Archiv übersprungen", exc_info=True)

            # Rechnen ist nicht abschaltbar: ohne Fahrplan zeigt das Panel
            # nichts und die Steuerung fällt in den Failsafe. Ob überhaupt
            # gestellt wird, entscheidet allein der Modus-Schalter (Ein/Test).
            takt = DEFAULT_INTERVAL_MIN
            unsub_schedule = async_track_time_interval(
                hass, _schedule_cycle, timedelta(minutes=takt)
            )
            entry.async_on_unload(unsub_schedule)

            # ----------------------------------------------------------
            # Fremddaten auffrischen: Bedarfsprognose und OeMAG-Tarif. Beide
            # Provider haben eine eigene Frist (6 bzw. 12 Stunden) und gehen
            # nur dann ins Netz — der halbstündige Takt ist bloß der Anlass.
            # Vorher wurde die Bedarfsprognose nur beim Start geholt; als
            # Grundlage der Preisfunktion wäre sie damit nach einem Tag stumm.
            # ----------------------------------------------------------
            async def _fremddaten_cycle(_now=None):
                namen = ["peakshare", "oemag"]
                # Die Börse nur abfragen, wenn sie der gewählte Basistarif ist.
                cfg = data.get("config") or {}
                if str(
                    cfg.get("schedule_feedin_source") or "manual"
                ).lower() == "spot":
                    namen.append("spot")
                for name in namen:
                    quelle = data.get(name)
                    if quelle is None:
                        continue
                    try:
                        await quelle.async_fetch()
                    except Exception:
                        _LOGGER.debug("%s: Abruf fehlgeschlagen", name, exc_info=True)

            unsub_fremddaten = async_track_time_interval(
                hass, _fremddaten_cycle, timedelta(minutes=30)
            )
            entry.async_on_unload(unsub_fremddaten)
            # Erster Lauf als Task: beim Boot sind PV-Prognose und
            # Batteriesensoren oft noch nicht da, das Setup soll nicht warten.
            hass.async_create_task(_schedule_cycle())

            # ----------------------------------------------------------
            # Phase 8: Flush-Timer (60 min) — draint den persistenten
            # Telemetrie-Buffer (Events aus Backend-Down-Phasen). Der alte
            # 30-min-Snapshot-Timer ist mit der Snapshot-Telemetrie entfallen.
            # ----------------------------------------------------------
            cfg_enabled = config.get(CONF_TELEMETRY_ENABLED, False)
            if cfg_enabled and reporter.is_configured:
                async def _telemetry_flush(_now=None):
                    if not (
                        reporter.is_configured and telemetry_buffer.identity_known()
                    ):
                        return

                    # Momentaufnahmen als Sammelpaket. Die Warteschlange wird
                    # vor dem Senden geleert: schlägt der Versand fehl, sind
                    # die Zeilen verloren (kein Puffer für Listen, siehe
                    # TelemetryReporter.send_snapshot_batch) — sie erneut
                    # mitzuschleppen würde die nächsten Pakete immer größer
                    # machen, ohne dass jemand die Lücke später vermisst.
                    queue = data.get("telemetry_snapshot_queue") or []
                    if queue:
                        data["telemetry_snapshot_queue"] = []
                        try:
                            await reporter.send_snapshot_batch(queue)
                        except Exception:  # pragma: no cover
                            _LOGGER.exception("Telemetry: snapshot batch failed")

                    try:
                        await reporter.flush_buffer()
                    except Exception:  # pragma: no cover
                        _LOGGER.exception("Telemetry: buffer flush failed")

                    # Herzschlag. Das Backend führt ``last_seen_at`` nur bei
                    # authentifizierten Ereignissen nach und löscht
                    # Installationen, die 90 Tage stumm waren. Eine gesunde
                    # Anlage im Modus Aus sendet weder Momentaufnahmen noch
                    # Störungen — ohne diesen Fallback verschwindet sie aus dem
                    # Bestand. Ein Profil-Update ist idempotent (COALESCE) und
                    # kostet einmal am Tag einen Request.
                    last_ok = reporter.last_success_at
                    if not last_ok:
                        return
                    try:
                        age_s = (
                            _now_utc() - datetime.fromisoformat(last_ok)
                        ).total_seconds()
                    except (TypeError, ValueError):  # pragma: no cover
                        return
                    if age_s < TELEMETRY_PROFILE_HEARTBEAT_S:
                        return
                    try:
                        ident = telemetry_buffer.get_identity() or {}
                        await reporter.update_profile(
                            _build_telemetry_profile(
                                hass, entry,
                                identity_registered_at=ident.get("registered_at"),
                            )
                        )
                    except Exception:  # pragma: no cover
                        _LOGGER.exception("Telemetry: heartbeat profile failed")

                try:
                    unsub_flush = async_track_time_interval(
                        hass, _telemetry_flush, timedelta(minutes=60),
                    )
                    entry.async_on_unload(unsub_flush)
                except Exception:  # pragma: no cover
                    _LOGGER.exception("Telemetry: failed to register flush timer")

                # ----------------------------------------------------------
                # Tagesbilanz — die Prognose des abgeschlossenen Tages gegen
                # die Messung (siehe tagesbilanz.py). Ersetzt die
                # Block-Outcomes der Zustands-Heuristik, deren Phasen es nicht
                # mehr gibt.
                #
                # 00:15 und nicht 00:00: der Recorder verdichtet seine
                # Kurzzeitstatistik erst nach Ablauf des Intervalls, um Punkt
                # Mitternacht fehlt das letzte Fünf-Minuten-Bündel des Vortags
                # noch.
                #
                # Läuft unabhängig vom Modus, anders als die Momentaufnahmen:
                # PV- und Verbrauchsprognose gelten auch für eine Anlage, die
                # gerade nicht gesteuert wird — für die Prognosegüte ist so ein
                # Tag sogar der unverfälschtere.
                #
                # Verpasste Tage werden nicht nachgeholt. Läuft Home Assistant
                # um 00:15 nicht, fehlt der Tag in der Auswertung; über dreißig
                # Tage MAE fällt das nicht auf, und die Alternative wäre ein
                # persistenter Merker für den letzten gemeldeten Tag.
                # ----------------------------------------------------------
                async def _tagesbilanz_cycle(now=None):
                    if not (
                        reporter.is_configured and telemetry_buffer.identity_known()
                    ):
                        return
                    from .tagesbilanz import (
                        async_baue_tagesbilanzen, tagesfenster,
                    )

                    # Fenster über tagesfenster(), nicht selbst gerechnet: die
                    # Zeitumstellung macht diese Rechnung tückischer, als sie
                    # aussieht (siehe Docstring dort).
                    von, bis = tagesfenster(now or dt_util.now())
                    try:
                        bilanzen = await async_baue_tagesbilanzen(
                            hass, entry.entry_id,
                            data.get("schedule_archive"), von, bis,
                        )
                    except Exception:  # pragma: no cover
                        _LOGGER.exception("Tagesbilanz: Aufbau fehlgeschlagen")
                        return []
                    for bilanz in bilanzen:
                        try:
                            await reporter.send_outcome(bilanz)
                        except Exception:  # pragma: no cover
                            _LOGGER.exception("Tagesbilanz: Senden fehlgeschlagen")
                    if bilanzen:
                        _LOGGER.info(
                            "Tagesbilanz %s gesendet (%d Zeile(n))",
                            von.strftime("%Y-%m-%d"), len(bilanzen),
                        )
                    return bilanzen

                data["_tagesbilanz_cycle"] = _tagesbilanz_cycle

                try:
                    unsub_bilanz = async_track_time_change(
                        hass, _tagesbilanz_cycle, hour=0, minute=15, second=0,
                    )
                    entry.async_on_unload(unsub_bilanz)
                except Exception:  # pragma: no cover
                    _LOGGER.exception("Telemetry: failed to register balance timer")

                # Boot-Send: Profile-Update + Buffer-Drain.
                # WICHTIG — Delay von 180 s: Beim HA-Start sind Modbus-/
                # Cloud-Sensoren (z.B. sensor.batterien_akkukapazitat,
                # PV-Forecasts) häufig noch unknown/unavailable. Würden wir
                # sofort senden, ginge der Profile-Resolver auf den manuellen
                # Wizard-Default zurück (z.B. 10 kWh statt der echten 15 kWh
                # vom Huawei-Sensor) und das Backend bekäme dauerhaft den
                # falschen Wert. 3 min reichen für 1–2 Modbus-Polls.
                # Defence-in-Depth: Falls der Sensor auch nach 180 s noch
                # nicht da ist, fängt die Drift-Detection im Guard-Cycle
                # die spätere Aktualisierung ab.
                _BOOT_TELEMETRY_DELAY_S = 180

                if telemetry_buffer.identity_known():
                    async def _boot_telemetry_send(_now=None):
                        try:
                            identity = telemetry_buffer.get_identity() or {}
                            profile = _build_telemetry_profile(
                                hass, entry,
                                identity_registered_at=identity.get("registered_at"),
                            )
                            await reporter.update_profile(profile)
                            data["telemetry_last_profile_capacity_kwh"] = (
                                profile.get("battery_capacity_kwh")
                            )
                            await reporter.flush_buffer()
                        except Exception:  # pragma: no cover
                            _LOGGER.exception("Telemetry boot send failed")
                    if async_call_later is not None:
                        unsub_boot = async_call_later(
                            hass, _BOOT_TELEMETRY_DELAY_S, _boot_telemetry_send,
                        )
                        entry.async_on_unload(unsub_boot)
                    else:  # pragma: no cover — Fallback ohne HA-Helper
                        hass.async_create_task(_boot_telemetry_send())
                else:
                    # Default-on Opt-Out: neue Installationen werden mit
                    # cfg_enabled=True angelegt (config_flow.py). Damit das Flag
                    # auch wirkt, registrieren wir hier einmalig im Hintergrund.
                    # Bestehende Installationen mit explizit gewähltem False
                    # landen nicht in diesem Block, weil cfg_enabled bereits
                    # oben gefiltert hat.
                    async def _auto_register(_now=None):
                        try:
                            profile = _build_telemetry_profile(
                                hass, entry, identity_registered_at=None,
                            )
                            ok = await reporter.register(profile)
                            if ok:
                                data["telemetry_last_profile_capacity_kwh"] = (
                                    profile.get("battery_capacity_kwh")
                                )
                        except Exception:  # pragma: no cover
                            _LOGGER.exception("Telemetry auto-register failed")
                    if async_call_later is not None:
                        unsub_reg = async_call_later(
                            hass, _BOOT_TELEMETRY_DELAY_S, _auto_register,
                        )
                        entry.async_on_unload(unsub_reg)
                    else:  # pragma: no cover — Fallback ohne HA-Helper
                        hass.async_create_task(_auto_register())
    else:
        missing = []
        if not coordinator:
            missing.append("Verbrauchsprofil (coordinator)")
        if not provider:
            missing.append("PV-Prognose (provider)")
        _LOGGER.error(
            "EEG Energy Optimizer: Optimizer konnte nicht gestartet werden — "
            "fehlende Komponenten: %s",
            ", ".join(missing),
        )
        from homeassistant.components.persistent_notification import async_create
        async_create(
            hass,
            f"EEG Energy Optimizer konnte nicht vollstaendig starten. "
            f"Fehlende Komponenten: {', '.join(missing)}. "
            f"Bitte Setup-Wizard erneut durchlaufen.",
            title="EEG Energy Optimizer Warnung",
            notification_id="eeg_init_warning",
        )

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


# Config-Schlüssel, deren Änderung einen VOLLEN Reload erfordert: Die
# Plattform-Entities (PV-Leistung, Hausverbrauch, Netz-/Batterieleistung,
# Prognosen) sowie Inverter- und Forecast-Provider-Objekte cachen ihre
# Config bei der Konstruktion. Der Hot-Reload unten tauscht nur den
# Optimizer — nach einer geänderten Sensor-Zuordnung lasen die Sensoren
# sonst bis zum nächsten HA-Neustart die alten Entity-IDs (Beta-Befund
# 19.08.2026: Optimizer nutzte den neuen PV-Sensor, Dashboard den alten).
_RELOAD_CONFIG_KEYS = frozenset({
    CONF_INVERTER_TYPE,
    CONF_BATTERY_SOC_SENSOR,
    CONF_BATTERY_CAPACITY_SENSOR,
    CONF_PV_POWER_SENSOR,
    CONF_PV_POWER_SENSOR_2,
    CONF_BATTERY_POWER_SENSOR,
    CONF_BATTERY_POWER_SENSOR_2,
    CONF_GRID_POWER_SENSOR,
    CONF_BATTERY_POWER_CHARGE_SENSOR,
    CONF_BATTERY_POWER_DISCHARGE_SENSOR,
    CONF_GRID_POWER_EXPORT_SENSOR,
    CONF_GRID_POWER_IMPORT_SENSOR,
})
# Präfixe decken Inverter-Anbindung (Modbus-Hosts/Ports, Geräte-IDs,
# Steuer-Entities) und Forecast-Quellen ab, ohne jeden Key einzeln zu pflegen.
_RELOAD_CONFIG_PREFIXES = (
    "fronius_", "huawei_", "kostal_", "sma_", "solaredge_", "solax_",
    "forecast_",
)


def _requires_full_reload(old: dict, new: dict) -> bool:
    """True, wenn sich ein Reload-pflichtiger Config-Schlüssel geändert hat."""
    for key in set(old) | set(new):
        if key in _RELOAD_CONFIG_KEYS or key.startswith(_RELOAD_CONFIG_PREFIXES):
            if old.get(key) != new.get(key):
                return True
    return False


async def _async_update_listener(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle config entry update — hot-reload optimizer or full restart after wizard."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not data:
        return

    config = {**entry.data, **entry.options}

    if not data.get("platforms_loaded"):
        # Wizard just finished — need full reload to create platforms/sensors
        await hass.config_entries.async_reload(entry.entry_id)
        return

    # Sensor-Zuordnung / Inverter-Anbindung geändert → voller Reload, damit
    # Entities, Inverter und Provider mit der neuen Config neu entstehen.
    # Reine Einstellungs-Änderungen nehmen weiterhin den Hot-Reload-Pfad,
    # damit der Optimizer-Tageszustand (Hysterese, aktive Zustände) erhalten
    # bleibt.
    if _requires_full_reload(data.get("config") or {}, config):
        _LOGGER.info(
            "EEG Energy Optimizer: Sensor-/Inverter-Konfiguration geändert — "
            "voller Reload"
        )
        await hass.config_entries.async_reload(entry.entry_id)
        return

    # Hot-reload: der Executor behält seinen Zustand (Totbänder, Grace
    # Period, Not-Aus-Sperre, zuletzt geschriebene Werte) und bekommt nur
    # die neue Config — er liest sie bei jedem Zugriff. Der ScheduleRunner
    # liest die Config ohnehin je Rechenlauf aus hass.data.
    data["config"] = config
    executor = data.get("executor")
    if executor is not None:
        coordinator = data.get("coordinator")
        if coordinator is not None:
            # Sync lookback_weeks into coordinator if changed; trigger
            # background refresh so profile + dependent sensors reflect new window.
            new_lookback = config.get(CONF_LOOKBACK_WEEKS, DEFAULT_LOOKBACK_WEEKS)
            if getattr(coordinator, "_lookback_weeks", None) != new_lookback:
                coordinator._lookback_weeks = new_lookback
                refresh_fn = data.get("refresh_consumption_profile")
                if refresh_fn is not None:
                    hass.async_create_task(refresh_fn())

        executor.update_config(config)
        _LOGGER.info("EEG Energy Optimizer: Config hot-reloaded")

        # ----------------------------------------------------------
        # Phase 8: Profile-Update bei Settings-Change (D-17, W-3, I-4)
        # ----------------------------------------------------------
        reporter = data.get("telemetry_reporter")
        buffer = data.get("telemetry_buffer")
        if (
            reporter is not None
            and reporter.is_configured
            and config.get(CONF_TELEMETRY_ENABLED, False)
            and buffer is not None
            and buffer.identity_known()
        ):
            try:
                identity = buffer.get_identity() or {}
                profile = _build_telemetry_profile(
                    hass, entry,
                    identity_registered_at=identity.get("registered_at"),
                )
                await reporter.update_profile(profile)
                data["telemetry_last_profile_capacity_kwh"] = (
                    profile.get("battery_capacity_kwh")
                )
            except Exception:  # pragma: no cover — defensive
                _LOGGER.exception("Telemetry profile update failed")

        # Fahrplan sofort neu rechnen und Guard-Lauf ausführen, damit das
        # Dashboard die geänderten Einstellungen direkt widerspiegelt.
        runner = data.get("schedule")
        if runner is not None:
            hass.async_create_task(runner.async_run())
        cycle_fn = data.get("_run_cycle")
        if cycle_fn:
            await cycle_fn()


async def async_unload_entry(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Unload EEG Energy Optimizer config entry."""
    from homeassistant.components.frontend import async_remove_panel

    async_remove_panel(hass, PANEL_URL_PATH)

    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    platforms_loaded = data.get("platforms_loaded", False)

    if platforms_loaded:
        unload_ok = await hass.config_entries.async_unload_platforms(
            entry, PLATFORMS
        )
    else:
        unload_ok = True

    if unload_ok:
        # Offene Einspeise-Sitzung sichern, bevor der Eintrag verschwindet.
        # Ohne das fehlt die letzte angebrochene Entladung in der Tagessumme.
        feedin_stats = data.get("feedin_stats")
        if feedin_stats is not None:
            try:
                await feedin_stats.async_flush()
            except Exception:
                _LOGGER.exception(
                    "EEG Energy Optimizer: error flushing feed-in statistics"
                )
        # Fahrplan-Steuerung freigeben: erzwungene Entladung stoppen und das
        # Ladelimit zurücksetzen — sonst bleibt das letzte geschriebene Limit
        # im Wechselrichter stehen (Risiko 2 des Umbauplans).
        executor = data.get("executor")
        if executor is not None:
            try:
                await executor.async_release()
            except Exception:
                _LOGGER.exception(
                    "EEG Energy Optimizer: error releasing schedule executor on unload"
                )
        # Close inverter resources (e.g. Fronius pymodbus TCP socket)
        # before dropping the entry. Other inverters use HA-managed
        # services/entities and do not need explicit cleanup.
        inverter = data.get("inverter")
        disconnect = getattr(inverter, "async_disconnect", None)
        if disconnect is not None:
            try:
                await disconnect()
            except Exception:
                _LOGGER.exception(
                    "EEG Energy Optimizer: error disconnecting inverter on unload"
                )
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
