"""Sensor platform for EEG Energy Optimizer.

Sensoren:
  1.   Verbrauchsprofil                    (slow, Stundenmittel je Wochentag)
  2-8. Tagesverbrauchsprognose heute..Tag 6 (fast)
  9.   Prognose bis Sonnenaufgang          (fast)
  10.  Batterie fehlende Energie           (fast)
  11.  PV-Prognose heute / 12. morgen      (fast)
  13.  Hausverbrauch / 14. PV-Leistung / 15. Netzleistung / 16. Batterieleistung
  17.  Register-Schreibvorgänge
  18.  Fahrplan-Status                     (30-s-Guard-Lauf; unique_id des
       früheren Entscheidungs-Sensors, damit Entität + Historie bleiben)
  +    Fahrplan Batterieleistung / Netzleistung (Plan-Werte des laufenden Slots)
  +    Combined-Sensoren (Paar-Setups, Multi-Battery)
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .const import (
    CONF_BATTERY_POWER_CHARGE_SENSOR,
    CONF_BATTERY_POWER_DISCHARGE_SENSOR,
    CONF_BATTERY_POWER_SENSOR,
    CONF_BATTERY_POWER_SENSOR_2,
    CONF_FORECAST_REMAINING_ENTITY,
    CONF_FORECAST_SOURCE,
    CONF_FORECAST_TOMORROW_ENTITY,
    CONF_GRID_POWER_EXPORT_SENSOR,
    CONF_GRID_POWER_IMPORT_SENSOR,
    CONF_GRID_POWER_SENSOR,
    CONF_INVERTER_TYPE,
    CONF_LOOKBACK_WEEKS,
    CONF_PV_POWER_SENSOR,
    CONF_PV_POWER_SENSOR_2,
    COMBINED_BATTERY_CAPACITY_SENSOR_ID,
    COMBINED_BATTERY_POWER_SENSOR_ID,
    COMBINED_BATTERY_SOC_SENSOR_ID,
    COMBINED_GRID_POWER_SENSOR_ID,
    CONSUMPTION_SENSOR,
    DEFAULT_LOOKBACK_WEEKS,
    DEFAULT_UPDATE_INTERVAL_FAST,
    DEFAULT_UPDATE_INTERVAL_SLOW,
    DOMAIN,
    FORECAST_SOURCE_SOLCAST,
    INVERTER_SIGN_CONVENTIONS,
    WEEKDAY_KEYS,
)
from .coordinator import ConsumptionCoordinator
from .forecast_provider import (
    ForecastSolarProvider,
    SolcastProvider,
)

_LOGGER = logging.getLogger(__name__)

# Timezone/time utilities - imported at module level for easy test patching
try:
    from homeassistant.util import dt as dt_util

    _now = dt_util.now
    _as_local = dt_util.as_local
except ImportError:
    _now = lambda: datetime.now(tz=timezone.utc)  # noqa: E731
    _as_local = lambda dt: dt  # noqa: E731

# HA imports guarded for test environment
try:
    from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
    from homeassistant.const import UnitOfEnergy, UnitOfPower
    from homeassistant.helpers.device_registry import DeviceEntryType
    from homeassistant.helpers.entity import DeviceInfo
    from homeassistant.helpers.event import async_track_time_interval
except ImportError:
    # Stubs for test environment without full HA
    class SensorEntity:  # type: ignore[no-redef]
        """Stub."""

        _attr_has_entity_name: bool = False
        _attr_name: str = ""
        _attr_unique_id: str = ""
        _attr_native_value: Any = None
        _attr_native_unit_of_measurement: str | None = None
        _attr_device_class: str | None = None
        _attr_icon: str | None = None
        _attr_suggested_display_precision: int | None = None
        _attr_device_info: Any = None
        _attr_extra_state_attributes: dict = {}

        @property
        def native_value(self) -> Any:
            return self._attr_native_value

        @property
        def extra_state_attributes(self) -> dict:
            return self._attr_extra_state_attributes

        async def async_update(self) -> None:
            pass

        def async_write_ha_state(self) -> None:
            pass

    class SensorDeviceClass:  # type: ignore[no-redef]
        ENERGY = "energy"
        POWER = "power"
        BATTERY = "battery"

    class SensorStateClass:  # type: ignore[no-redef]
        MEASUREMENT = "measurement"
        TOTAL = "total"

    class UnitOfEnergy:  # type: ignore[no-redef]
        KILO_WATT_HOUR = "kWh"

    class UnitOfPower:  # type: ignore[no-redef]
        KILO_WATT = "kW"

    class DeviceEntryType:  # type: ignore[no-redef]
        SERVICE = "service"

    class DeviceInfo(dict):  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)

    async_track_time_interval = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_float(hass: Any, entity_id: str) -> float | None:
    """Read a float value from an entity state.

    Returns None for missing, unavailable, unknown, or non-numeric states.
    """
    state = hass.states.get(entity_id)
    if state is None:
        return None
    if state.state in ("unknown", "unavailable", ""):
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None


def _read_power_kw(hass: Any, entity_id: str) -> float | None:
    """Read a power sensor value and normalize to kW.

    Thin wrapper über power_readings.read_power_kw — eine Quelle der Wahrheit
    für Unit-Erkennung (W/kW/MW + Aliase, case-insensitive).
    """
    from .power_readings import read_power_kw
    return read_power_kw(hass, entity_id)


def _device_info(entry_id: str) -> DeviceInfo:
    """Return shared DeviceInfo for all sensors of this integration."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="EEG Energy Optimizer",
        manufacturer="Custom",
        model="EEG Energy Optimizer",
        entry_type=DeviceEntryType.SERVICE,
    )


# ---------------------------------------------------------------------------
# Sensor 1: Verbrauchsprofil (slow)
# ---------------------------------------------------------------------------

class VerbrauchsprofilSensor(SensorEntity):
    """Exposes hourly averages per weekday as attributes for dashboard charts."""

    _attr_has_entity_name = True
    _attr_name = "Verbrauchsprofil"
    _attr_native_unit_of_measurement = "kWh"
    _attr_icon = "mdi:chart-bell-curve-cumulative"
    _attr_suggested_display_precision = 1

    def __init__(
        self, hass: Any, entry: Any, coordinator: ConsumptionCoordinator
    ) -> None:
        self.hass = hass
        self._coordinator = coordinator
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_verbrauchsprofil"
        self._attr_device_info = _device_info(entry.entry_id)
        self._attr_native_value: float | None = None
        self._attr_extra_state_attributes: dict[str, Any] = {}

        # Nacht-Beginn für die Tag/Nacht-Aufteilung im Dashboard-Diagramm.
        # Historisch der Slot-A-Start der alten Entladelogik; der Schlüssel
        # bleibt in entry.data erhalten (Rückwechsel-Garantie), gesteuert wird
        # damit nichts mehr. Default 20:00.
        a_start = entry.data.get("discharge_a_start_time", "20:00")
        try:
            self._discharge_start_h = int(a_start.split(":")[0])
        except (ValueError, AttributeError):
            self._discharge_start_h = 20

    @staticmethod
    def _calc_night_kwh(
        day_idx: int,
        hourly_avg: dict[str, dict[int, float]],
        start_h: int,
        end_decimal: float,
    ) -> float:
        """Sum kWh from hour `start_h` on day_idx to `end_decimal` on day_idx+1.

        Mirrors optimizer._gather_snapshot's overnight period:
        discharge_start (hour) on day X → sunrise+1h (decimal) on day X+1.
        """
        next_day = WEEKDAY_KEYS[(day_idx + 1) % 7]
        today = WEEKDAY_KEYS[day_idx]

        total = 0.0
        for h in range(start_h, 24):
            total += hourly_avg.get(today, {}).get(h, 0.0) / 1000.0

        full_end = int(end_decimal)
        for h in range(0, full_end):
            total += hourly_avg.get(next_day, {}).get(h, 0.0) / 1000.0

        fraction = end_decimal - full_end
        if fraction > 0 and full_end < 24:
            total += hourly_avg.get(next_day, {}).get(full_end, 0.0) / 1000.0 * fraction

        return total

    async def async_update(self) -> None:
        avg = self._coordinator.hourly_avg
        if not avg:
            return

        sunrise_hour = 6
        sunrise_minute = 0
        sunset_hour = 20
        try:
            sun_state = self.hass.states.get("sun.sun")
            if sun_state is not None:
                nr = sun_state.attributes.get("next_rising")
                ns = sun_state.attributes.get("next_setting")
                if nr:
                    sr = _as_local(datetime.fromisoformat(str(nr)))
                    sunrise_hour = sr.hour
                    sunrise_minute = sr.minute
                if ns:
                    sunset_hour = _as_local(datetime.fromisoformat(str(ns))).hour
        except Exception:
            pass

        # Night window mirrors optimizer: discharge_start_h → sunrise + 1h.
        # end_decimal can exceed 24 (e.g. sunrise 23:30 + 1h = 24.5) — clamp.
        night_end_decimal = sunrise_hour + sunrise_minute / 60.0 + 1.0
        if night_end_decimal > 24.0:
            night_end_decimal = 24.0

        attrs: dict[str, Any] = {}
        day_totals: list[float] = []

        for day_idx, day in enumerate(WEEKDAY_KEYS):
            hours_data = avg.get(day, {})
            watts = [round(hours_data.get(h, 0.0)) for h in range(24)]
            kwh = sum(w / 1000.0 for w in watts)
            nacht_kwh = self._calc_night_kwh(
                day_idx, avg, self._discharge_start_h, night_end_decimal
            )
            tag_kwh = max(kwh - nacht_kwh, 0.0)
            day_totals.append(kwh)

            attrs[f"{day}_watts"] = watts
            attrs[f"{day}_kwh"] = round(kwh, 1)
            attrs[f"{day}_tag_kwh"] = round(tag_kwh, 1)
            attrs[f"{day}_nacht_kwh"] = round(nacht_kwh, 1)

        # State: average daily total across all weekdays
        self._attr_native_value = round(sum(day_totals) / len(day_totals), 1) if day_totals else None

        attrs["stunden"] = [f"{h:02d}:00" for h in range(24)]
        attrs["sunrise_hour"] = sunrise_hour
        attrs["sunrise_minute"] = sunrise_minute
        attrs["sunset_hour"] = sunset_hour
        attrs["discharge_start_hour"] = self._discharge_start_h
        attrs["night_end_decimal"] = round(night_end_decimal, 2)
        attrs["grundlage"] = (
            f"Durchschnitt {self._coordinator.stats_count} Datenpunkte"
        )
        attrs["stats_count"] = self._coordinator.stats_count
        attrs["lookback_weeks"] = self._coordinator.lookback_weeks
        attrs["last_refresh"] = self._coordinator.last_update_iso
        attrs["last_duration_ms"] = self._coordinator.last_duration_ms
        self._attr_extra_state_attributes = attrs


# ---------------------------------------------------------------------------
# Sensors 2-8: Tagesverbrauchsprognose (fast) - 7 instances
# ---------------------------------------------------------------------------

class DailyForecastSensor(SensorEntity):
    """Daily consumption forecast sensor. 7 instances (day_offset 0-6)."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_icon = "mdi:lightning-bolt"
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        hass: Any,
        entry: Any,
        coordinator: ConsumptionCoordinator,
        day_offset: int,
    ) -> None:
        self.hass = hass
        self._coordinator = coordinator
        self._day_offset = day_offset

        if day_offset == 0:
            self._attr_name = "Tagesverbrauchsprognose heute"
            suffix = "tagesverbrauch_heute"
        elif day_offset == 1:
            self._attr_name = "Tagesverbrauchsprognose morgen"
            suffix = "tagesverbrauch_morgen"
        else:
            self._attr_name = f"Tagesverbrauchsprognose Tag {day_offset}"
            suffix = f"tagesverbrauch_tag_{day_offset}"

        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{suffix}"
        self._attr_device_info = _device_info(entry.entry_id)
        self._attr_native_value: float | None = None
        self._attr_extra_state_attributes: dict[str, Any] = {}

    async def async_update(self) -> None:
        now = _now()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        if self._day_offset == 0:
            # Today: remaining from now to end of day
            start = now
            end = midnight + timedelta(days=1)
        else:
            # Future days: full 24h
            target_date = now.date() + timedelta(days=self._day_offset)
            start = now.replace(
                year=target_date.year,
                month=target_date.month,
                day=target_date.day,
                hour=0, minute=0, second=0, microsecond=0,
            )
            end = start + timedelta(days=1)

        result = self._coordinator.calculate_period(start, end)
        self._attr_native_value = round(result["verbrauch_kwh"], 2)
        attrs = {"stunden": round(result["stunden"], 1)}

        if self._day_offset == 0:
            # Full day total (midnight to midnight) for dashboard chart
            full_day = self._coordinator.calculate_period(midnight, midnight + timedelta(days=1))
            attrs["tagesverbrauch_gesamt_kwh"] = round(full_day["verbrauch_kwh"], 2)

        self._attr_extra_state_attributes = attrs


# ---------------------------------------------------------------------------
# Sensor 9: Prognose bis Sonnenaufgang (fast)
# ---------------------------------------------------------------------------

class PVForecastTodaySensor(SensorEntity):
    """PV forecast remaining today from forecast provider."""

    _attr_has_entity_name = True
    _attr_name = "PV-Prognose heute"
    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_icon = "mdi:solar-power"
    _attr_suggested_display_precision = 1

    def __init__(self, hass: Any, entry: Any, provider: Any) -> None:
        self.hass = hass
        self._provider = provider
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_pv_prognose_heute"
        self._attr_device_info = _device_info(entry.entry_id)
        self._attr_native_value: float | None = None

    async def async_update(self) -> None:
        forecast = self._provider.get_forecast()
        self._attr_native_value = forecast.remaining_today_kwh


# ---------------------------------------------------------------------------
# Sensor 12: PV-Prognose morgen (fast)
# ---------------------------------------------------------------------------

class PVForecastTomorrowSensor(SensorEntity):
    """PV forecast for tomorrow from forecast provider."""

    _attr_has_entity_name = True
    _attr_name = "PV-Prognose morgen"
    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_icon = "mdi:solar-power"
    _attr_suggested_display_precision = 1

    def __init__(self, hass: Any, entry: Any, provider: Any) -> None:
        self.hass = hass
        self._provider = provider
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_pv_prognose_morgen"
        self._attr_device_info = _device_info(entry.entry_id)
        self._attr_native_value: float | None = None

    async def async_update(self) -> None:
        forecast = self._provider.get_forecast()
        self._attr_native_value = forecast.tomorrow_kwh


# ---------------------------------------------------------------------------
# Sensor 13: Hausverbrauch (fast, calculated house consumption)
# ---------------------------------------------------------------------------

class HausverbrauchSensor(SensorEntity):
    """Calculates actual house consumption from PV input, battery, and grid power.

    Formula: Hausverbrauch = PV-Eingangsleistung - Batterie-Lade/Entladeleistung - Netz-Wirkleistung
    (battery positive = charging, negative = discharging; grid positive = export, negative = import)
    Result clamped to >= 0.
    state_class=MEASUREMENT so HA recorder stores mean statistics.
    """

    _attr_has_entity_name = True
    _attr_name = "Hausverbrauch"
    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:home-lightning-bolt"
    _attr_suggested_display_precision = 2

    def __init__(self, hass: Any, entry: Any, config: dict) -> None:
        self.hass = hass
        self._pv_sensor_id = config.get(CONF_PV_POWER_SENSOR, "")
        self._pv_sensor_2_id = config.get(CONF_PV_POWER_SENSOR_2, "")
        self._battery_power_sensor_id = config.get(CONF_BATTERY_POWER_SENSOR, "")
        # Optional second battery (Huawei Master/Slave) — explicit signed sensor.
        self._battery_power_2_sensor_id = config.get(CONF_BATTERY_POWER_SENSOR_2, "")
        self._grid_sensor_id = config.get(CONF_GRID_POWER_SENSOR, "")
        # Sign conventions differ per inverter type (defined in const.py);
        # resolve_sign berücksichtigt zusätzlich Huawei-EMMA-Sensoren.
        from .power_readings import resolve_sign
        inv_type = config.get(CONF_INVERTER_TYPE, "")
        signs = INVERTER_SIGN_CONVENTIONS.get(inv_type, {})
        self._battery_sign = resolve_sign(inv_type, self._battery_power_sensor_id, "battery_sign")
        self._grid_sign = resolve_sign(inv_type, self._grid_sensor_id, "grid_sign")
        self._pv_includes_battery = signs.get("pv_includes_battery", False)
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_hausverbrauch"
        self._attr_device_info = _device_info(entry.entry_id)
        self._attr_native_value: float | None = None
        self._attr_extra_state_attributes: dict[str, Any] = {}

    async def async_update(self) -> None:
        pv_power = _read_power_kw(self.hass, self._pv_sensor_id)
        battery_power = _read_power_kw(self.hass, self._battery_power_sensor_id)
        grid_power = _read_power_kw(self.hass, self._grid_sensor_id)

        # PV sensor unavailable at night (inverter offline) → PV = 0 kW
        if pv_power is None:
            pv_power = 0.0

        if battery_power is None or grid_power is None:
            self._attr_native_value = None
            hints = []
            if battery_power is None:
                hints.append(f"Batterie-Sensor ({self._battery_power_sensor_id}) nicht verfügbar")
            if grid_power is None:
                hints.append(f"Netz-Sensor ({self._grid_sensor_id}) nicht verfügbar")
            self._attr_extra_state_attributes = {"hinweis": ", ".join(hints)}
            return

        # Sum optional second PV sensor (e.g. SolaX generator inverter via Meter 2)
        if self._pv_sensor_2_id:
            pv2 = _read_power_kw(self.hass, self._pv_sensor_2_id)
            if pv2 is not None:
                pv_power += pv2

        # SolarEdge: ac_power includes battery discharge → correct to get real PV
        # PV_real = ac_power + battery_raw (positive=charge, negative=discharge)
        # Don't clamp here — small negative from conversion losses is expected
        # and needed for accurate Hausverbrauch (formula simplifies to ac_power - grid)
        if self._pv_includes_battery and battery_power is not None:
            pv_power = pv_power + battery_power

        # Second battery (Huawei Master/Slave): add the raw signed power of the
        # slave battery so PV − Battery − Grid reflects the whole system. Done
        # after the SolarEdge correction so it never affects that path (the key
        # is only set for Huawei multi-inverter setups).
        if self._battery_power_2_sensor_id:
            bat2 = _read_power_kw(self.hass, self._battery_power_2_sensor_id)
            if bat2 is not None:
                battery_power += bat2

        # Normalize signs: positive=charging / positive=export
        battery_power *= self._battery_sign
        grid_power *= self._grid_sign

        # PV input - battery power - grid power
        # battery positive = charging, negative = discharging
        # grid positive = export, negative = import
        # All values normalized to kW by _read_power_kw
        hausverbrauch = max(pv_power - battery_power - grid_power, 0.0)
        self._attr_native_value = round(hausverbrauch, 3)
        attrs = {
            "pv_leistung_kw": round(max(pv_power, 0.0), 3),
            "batterie_leistung_kw": round(battery_power, 3),
            "netz_leistung_kw": round(grid_power, 3),
        }
        if self._pv_sensor_2_id:
            pv2_val = _read_power_kw(self.hass, self._pv_sensor_2_id)
            if pv2_val is not None:
                attrs["pv_leistung_2_kw"] = round(pv2_val, 3)
        self._attr_extra_state_attributes = attrs


# ---------------------------------------------------------------------------
# Sensor 14: PV-Gesamtleistung (fast, normalized total PV)
# ---------------------------------------------------------------------------

class PVLeistungSensor(SensorEntity):
    """Total PV production from all inverters, normalized to real PV output.

    Sums pv_power_sensor + optional pv_power_sensor_2 and applies the
    pv_includes_battery correction per inverter (SolarEdge: ac_power includes
    battery discharge, so we must subtract each inverter's battery to get real PV).
    Result clamped to >= 0.
    """

    _attr_has_entity_name = True
    _attr_name = "PV-Leistung"
    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:solar-power-variant"
    _attr_suggested_display_precision = 2

    def __init__(self, hass: Any, entry: Any, config: dict) -> None:
        self.hass = hass
        # Config-Snapshot für compute_pv_now_kw — derselbe Helper rechnet
        # auch im Telemetrie-Pfad (optimizer._current_power_readings).
        self._config = config
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_pv_leistung"
        self._attr_device_info = _device_info(entry.entry_id)
        self._attr_native_value: float | None = None

    async def async_update(self) -> None:
        from .power_readings import compute_pv_now_kw
        pv = compute_pv_now_kw(self.hass, self._config)
        # Bei kompletter Sensor-Unverfügbarkeit zeigt der Sensor 0 (statt
        # "unavailable") — historisches Verhalten der Integration.
        self._attr_native_value = round(pv if pv is not None else 0.0, 3)


# ---------------------------------------------------------------------------
# Sensor 15: Netzleistung (fast, normalized grid power)
# ---------------------------------------------------------------------------

class NetzleistungSensor(SensorEntity):
    """Normalized grid power: positive = export (Einspeisung), negative = import (Bezug).

    Reads grid_power_sensor and applies the inverter-specific grid_sign convention
    so the value is always: positive = feed-in, negative = consumption from grid.
    """

    _attr_has_entity_name = True
    _attr_name = "Netzleistung"
    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:transmission-tower"
    _attr_suggested_display_precision = 2

    def __init__(self, hass: Any, entry: Any, config: dict) -> None:
        self.hass = hass
        self._grid_sensor_id = config.get(CONF_GRID_POWER_SENSOR, "")
        from .power_readings import resolve_sign
        inv_type = config.get(CONF_INVERTER_TYPE, "")
        self._grid_sign = resolve_sign(inv_type, self._grid_sensor_id, "grid_sign")
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_netzleistung"
        self._attr_device_info = _device_info(entry.entry_id)
        self._attr_native_value: float | None = None

    async def async_update(self) -> None:
        grid = _read_power_kw(self.hass, self._grid_sensor_id)
        if grid is None:
            self._attr_native_value = None
            return
        self._attr_native_value = round(grid * self._grid_sign, 3)


# ---------------------------------------------------------------------------
# Sensor 16: Batterieleistung (fast, normalized battery power)
# ---------------------------------------------------------------------------

class BatterieleistungSensor(SensorEntity):
    """Normalized total battery power: positive = charging, negative = discharging.

    Sums battery power from all inverters and applies the inverter-specific
    battery_sign convention. For multi-inverter SolarEdge, the second battery
    is derived from the second PV sensor prefix.
    """

    _attr_has_entity_name = True
    _attr_name = "Batterieleistung"
    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery-charging"
    _attr_suggested_display_precision = 2

    def __init__(self, hass: Any, entry: Any, config: dict) -> None:
        self.hass = hass
        self._battery_sensor_id = config.get(CONF_BATTERY_POWER_SENSOR, "")
        from .power_readings import resolve_sign
        inv_type = config.get(CONF_INVERTER_TYPE, "")
        self._battery_sign = resolve_sign(inv_type, self._battery_sensor_id, "battery_sign")
        # Second battery (multi-inverter). Explicit config wins (e.g. Huawei
        # Master/Slave, where the second battery has its own signed sensor);
        # otherwise derive it from the second PV sensor prefix (SolarEdge:
        # ac_power → b1_dc_power).
        self._battery_2_sensor_id = config.get(CONF_BATTERY_POWER_SENSOR_2, "")
        if not self._battery_2_sensor_id:
            pv2_id = config.get(CONF_PV_POWER_SENSOR_2, "")
            if pv2_id and "ac_power" in pv2_id:
                self._battery_2_sensor_id = pv2_id.replace("ac_power", "b1_dc_power")
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_batterieleistung"
        self._attr_device_info = _device_info(entry.entry_id)
        self._attr_native_value: float | None = None

    async def async_update(self) -> None:
        bat = _read_power_kw(self.hass, self._battery_sensor_id)
        if bat is None:
            self._attr_native_value = None
            return
        total = bat * self._battery_sign
        if self._battery_2_sensor_id:
            bat2 = _read_power_kw(self.hass, self._battery_2_sensor_id)
            if bat2 is not None:
                total += bat2 * self._battery_sign
        self._attr_native_value = round(total, 3)


# ---------------------------------------------------------------------------
# Sensor 17: Register-Schreibvorgänge (inverter write counter)
# ---------------------------------------------------------------------------

class RegisterWritesSensor(SensorEntity):
    """Counts Modbus/service register writes to the inverter.

    Tracks NVRAM-relevant writes for SolarEdge (and potentially other
    inverters in the future). Uses state_class=total_increasing so HA
    tracks the cumulative total across restarts via long-term statistics.
    The sensor reads the counter from the inverter object every fast update.
    """

    _attr_has_entity_name = True
    _attr_name = "Register-Schreibvorgänge"
    _attr_native_unit_of_measurement = "Writes"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:database-edit-outline"

    def __init__(self, hass: Any, entry: Any, inverter: Any) -> None:
        self.hass = hass
        self._inverter = inverter
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_register_writes"
        self._attr_device_info = _device_info(entry.entry_id)
        self._attr_native_value: int = 0

    async def async_update(self) -> None:
        if self._inverter is not None:
            self._attr_native_value = self._inverter.register_writes


# ---------------------------------------------------------------------------
# Sensor 18: Fahrplan-Status (30-s-Guard-Lauf)
# ---------------------------------------------------------------------------


def fahrplan_kurzstatus(status: dict) -> str:
    """Kompakter Zustand der Fahrplan-Steuerung aus ScheduleExecutor.status().

    Modul-Funktion, damit Sensor-State und Aktivitätsprotokoll garantiert
    dieselbe Kurzform verwenden.
    """
    if not status.get("supported"):
        return "Nur Anzeige"
    if status.get("mode") != "Ein":
        return "Anzeige-Modus"
    kind = status.get("active_kind")
    if kind == "charge_limit":
        limit = status.get("written_charge_limit_kw")
        if limit is None:
            return "Laden begrenzt"
        if limit <= 0.05:
            return "Laden blockiert"
        return f"Laden begrenzt auf {limit:.1f} kW"
    if kind == "discharge":
        # Leitgröße ist die geplante Einspeisung, nicht der Batterie-Sollwert:
        # Der Sollwert enthält die Hauslast (Plan + Haus − PV) und liegt damit
        # immer über dem, was in der Gemeinschaft ankommt. Wer „1,0 kW" liest
        # und 0,6 kW im Netz sieht, sucht sonst einen Fehler, der keiner ist.
        action = status.get("plan_action") or {}
        einspeisung = action.get("power_kw")
        soc = status.get("written_target_soc")
        soc_text = f" bis {soc:.0f} %" if soc is not None else ""
        if einspeisung is None:
            power = status.get("written_discharge_kw") or 0.0
            return f"Entladung {power:.2f} kW{soc_text}"
        return f"Einspeisung {einspeisung:.2f} kW{soc_text}"
    return "Normalbetrieb"


class FahrplanStatusSensor(SensorEntity):
    """Zustand der Fahrplan-Steuerung — Nachfolger des Entscheidungs-Sensors.

    Bewusst dieselbe unique_id wie der frühere EntscheidungsSensor, damit
    die Entität und ihre Verlaufshistorie erhalten bleiben. Automationen,
    die alte Attribute lasen (markdown, morning_*, discharge_*), brechen —
    dokumentiert im CHANGELOG (Schritt 7 des Umbaus).

    State: kompakte Kurzform ("Laden begrenzt auf 2,0 kW", "Entladung 2,8 kW
    bis 43 %", "Normalbetrieb", "Anzeige-Modus"). Aktualisiert vom
    30-Sekunden-Guard-Lauf in __init__.py.
    """

    _attr_has_entity_name = True
    _attr_name = "Fahrplan-Status"
    _attr_icon = "mdi:robot"

    def __init__(self, entry_id: str) -> None:
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_entscheidung"
        self._attr_device_info = _device_info(entry_id)
        self._attr_native_value: str | None = None
        self._attr_extra_state_attributes: dict[str, Any] = {}

    def update_from_executor(self, status: dict) -> str:
        """Sensor aus ScheduleExecutor.status() aktualisieren.

        Gibt die Kurzform zurück, damit der Guard-Lauf sie fürs
        Aktivitätsprotokoll (Statuswechsel-Erkennung) weiterverwenden kann.
        """
        kurz = fahrplan_kurzstatus(status)
        self._attr_native_value = kurz
        action = status.get("plan_action") or {}
        self._attr_extra_state_attributes = {
            "status": status.get("status"),
            "modus": status.get("mode"),
            "gesteuert": bool(status.get("supported")),
            "aktiv": status.get("active_kind"),
            "ladelimit_kw": status.get("written_charge_limit_kw"),
            "entladeleistung_kw": status.get("written_discharge_kw"),
            "ziel_soc": status.get("written_target_soc"),
            "plan_aktion": action.get("kind"),
            "plan_leistung_kw": action.get("power_kw"),
            "plan_ziel_soc": action.get("target_soc"),
            "plan_slot": action.get("slot"),
            "plan_grund": action.get("reason"),
            "failsafe": bool(status.get("failsafe_released")),
            "notaus_gesperrt": status.get("emergency_blocked_slot") is not None,
            "schreibfehler": status.get("write_failures"),
            "letzter_schreibversuch_ok": status.get("last_write_ok"),
            "letzte_aktualisierung": status.get("last_run"),
        }
        self.async_write_ha_state()
        return kurz


# ---------------------------------------------------------------------------
# Pair-Sensor Helpers (Fronius and similar split-sensor inverters)
# ---------------------------------------------------------------------------

class BatteryPowerCombinedSensor(SensorEntity):
    """Synthetic signed battery power for inverters that expose only a charge /
    discharge pair of always-positive sensors (e.g. Fronius via SolarNet).

    Output: positive = charging, negative = discharging (canonical).
    Setup must register this sensor whenever both pair config keys are set,
    and point CONF_BATTERY_POWER_SENSOR at it so all downstream consumers
    (Hausverbrauch, Netzleistung-Watchdog, statistics, optimizer) see the
    same single source of truth.

    Object-id is forced to the constant in const.py so backfill writes the
    same statistic_id the live updates produce.
    """

    _attr_has_entity_name = False
    _attr_name = "Batterieleistung"
    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery-charging"
    _attr_suggested_display_precision = 3

    def __init__(self, hass: Any, entry: Any, config: dict) -> None:
        self.hass = hass
        self._charge_id = config.get(CONF_BATTERY_POWER_CHARGE_SENSOR, "")
        self._discharge_id = config.get(CONF_BATTERY_POWER_DISCHARGE_SENSOR, "")
        # Pin the exact entity_id. Using suggested_object_id alone is not
        # enough — when the entity is bound to a device, HA still prefixes
        # the device slug, producing a doubled "eeg_energy_optimizer_..."
        # path that mismatches the canonical ID written to config.
        self.entity_id = COMBINED_BATTERY_POWER_SENSOR_ID
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_battery_power_combined"
        self._attr_device_info = _device_info(entry.entry_id)
        self._attr_native_value: float | None = None

    async def async_update(self) -> None:
        c = _read_power_kw(self.hass, self._charge_id)
        d = _read_power_kw(self.hass, self._discharge_id)
        if c is None and d is None:
            self._attr_native_value = None
            return
        self._attr_native_value = round((c or 0.0) - (d or 0.0), 3)


class GridPowerCombinedSensor(SensorEntity):
    """Synthetic signed grid power from an export / import pair.

    Output: positive = export (Einspeisung), negative = import (Bezug).
    """

    _attr_has_entity_name = False
    _attr_name = "Netzleistung"
    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:transmission-tower"
    _attr_suggested_display_precision = 3

    def __init__(self, hass: Any, entry: Any, config: dict) -> None:
        self.hass = hass
        self._export_id = config.get(CONF_GRID_POWER_EXPORT_SENSOR, "")
        self._import_id = config.get(CONF_GRID_POWER_IMPORT_SENSOR, "")
        # See BatteryPowerCombinedSensor.__init__ for why entity_id is pinned.
        self.entity_id = COMBINED_GRID_POWER_SENSOR_ID
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_grid_power_combined"
        self._attr_device_info = _device_info(entry.entry_id)
        self._attr_native_value: float | None = None

    async def async_update(self) -> None:
        e = _read_power_kw(self.hass, self._export_id)
        i = _read_power_kw(self.hass, self._import_id)
        if e is None and i is None:
            self._attr_native_value = None
            return
        self._attr_native_value = round((e or 0.0) - (i or 0.0), 3)


class CombinedBatterySocSensor(SensorEntity):
    """Capacity-weighted SOC across all batteries managed by the inverter.

    Wird nur registriert, wenn der Driver get_combined_battery_state() einen
    Wert liefert (aktuell: SolarEdge mit ≥ 2 Invertern). Spiegelt exakt den
    Wert, den der Optimizer intern nutzt — damit das UI nicht "44 %" zeigt,
    während der Optimizer mit "34.6 %" rechnet.
    """

    _attr_has_entity_name = True
    _attr_name = "Batterie-Ladestand kombiniert"
    _attr_native_unit_of_measurement = "%"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_icon = "mdi:battery-sync"
    _attr_suggested_display_precision = 1

    def __init__(self, hass: Any, entry: Any, inverter: Any) -> None:
        self.hass = hass
        self._inverter = inverter
        # Pin entity_id so wizard + frontend can reference it as a constant.
        self.entity_id = COMBINED_BATTERY_SOC_SENSOR_ID
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_combined_soc"
        self._attr_device_info = _device_info(entry.entry_id)
        self._attr_native_value: float | None = None
        self._attr_extra_state_attributes: dict[str, Any] = {}

    async def async_update(self) -> None:
        try:
            soc, cap = self._inverter.get_combined_battery_state()
        except Exception:
            soc, cap = (None, None)
        if soc is None:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {
                "hinweis": "Combined-SOC nicht verfügbar",
            }
            return
        self._attr_native_value = round(soc, 1)
        self._attr_extra_state_attributes = {
            "kombinierte_kapazitaet_kwh": round(cap, 2) if cap is not None else None,
            "berechnung": "Σ(SOC_i × kapazität_i) / Σ(kapazität_i)",
        }


class CombinedBatteryCapacitySensor(SensorEntity):
    """Total nominal battery capacity across all inverters (kWh).

    Wird nur registriert, wenn Driver get_combined_battery_state() liefert.
    """

    _attr_has_entity_name = True
    _attr_name = "Batterie-Kapazität kombiniert"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_icon = "mdi:battery-high"
    _attr_suggested_display_precision = 2

    def __init__(self, hass: Any, entry: Any, inverter: Any) -> None:
        self.hass = hass
        self._inverter = inverter
        self.entity_id = COMBINED_BATTERY_CAPACITY_SENSOR_ID
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_combined_capacity"
        self._attr_device_info = _device_info(entry.entry_id)
        self._attr_native_value: float | None = None

    async def async_update(self) -> None:
        try:
            _soc, cap = self._inverter.get_combined_battery_state()
        except Exception:
            cap = None
        self._attr_native_value = round(cap, 2) if cap is not None else None


def _inverter_has_combined_state(inverter: Any) -> bool:
    """Whether to create the combined SOC/capacity sensors for this driver.

    Prefers the STRUCTURAL ``has_combined_battery_state`` property — that way
    the sensors are created even when the source integration's entities are not
    yet populated at setup time (huawei_solar can take >10s). The sensors then
    fill in on the next update once the source values appear. Falls back to the
    legacy value-based probe only for drivers without the property.
    """
    if inverter is None:
        return False
    if hasattr(inverter, "has_combined_battery_state"):
        try:
            return bool(inverter.has_combined_battery_state)
        except Exception:
            pass
    if not hasattr(inverter, "get_combined_battery_state"):
        return False
    try:
        soc, cap = inverter.get_combined_battery_state()
    except Exception:
        return False
    return soc is not None or cap is not None


def _has_battery_pair(config: dict) -> bool:
    return bool(
        config.get(CONF_BATTERY_POWER_CHARGE_SENSOR)
        and config.get(CONF_BATTERY_POWER_DISCHARGE_SENSOR)
    )


def _has_grid_pair(config: dict) -> bool:
    return bool(
        config.get(CONF_GRID_POWER_EXPORT_SENSOR)
        and config.get(CONF_GRID_POWER_IMPORT_SENSOR)
    )


# ---------------------------------------------------------------------------
# Einspeise-Statistik
# ---------------------------------------------------------------------------


class EntladungInsNetzSensor(SensorEntity):
    """Heute ins Netz eingespeiste Energie während gesteuerter Entladungen.

    Bewusst dieselbe ``unique_id`` wie der frühere Sensor „Nacht-Entladung
    Energie heute" (``..._feedin_evening_heute``): Entität und
    Langzeitstatistik laufen damit weiter. Was gezählt wird, hat sich mit dem
    Fahrplan aber geändert — vorher ein Zustand mit Zeitfenster, jetzt die
    tatsächlich gestellte Entladung. Deshalb der neue Anzeigename und die
    Attribute ``zaehlweise`` und ``umgestellt_am``: der Sprung in der Reihe
    soll auffindbar sein, nicht verborgen. Siehe ``statistics.py``.
    """

    _attr_has_entity_name = True
    _attr_name = "Entladung ins Netz heute"
    _attr_icon = "mdi:transmission-tower-export"
    _attr_native_unit_of_measurement = "kWh"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    def __init__(self, hass: Any, entry: Any) -> None:
        self.hass = hass
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_feedin_evening_heute"
        self._attr_device_info = _device_info(entry.entry_id)
        self._attr_native_value: float | None = 0.0
        self._attr_extra_state_attributes: dict[str, Any] = {}

    async def async_update(self) -> None:
        from .statistics import STATS_KEY_ENTLADUNG, UMGESTELLT_AM, ZAEHLWEISE

        stats = self.hass.data.get(DOMAIN, {}).get(self._entry_id, {}).get(
            "feedin_stats"
        )
        if stats is None:
            return
        self._attr_native_value = round(stats.get_today_kwh(STATS_KEY_ENTLADUNG), 3)
        # last_reset gehört zu state_class TOTAL — ohne ihn deutet das
        # Energie-Dashboard den täglichen Rücksprung auf 0 als Zählerdefekt.
        mitternacht = _now_local().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        self._attr_extra_state_attributes = {
            "last_reset": mitternacht.isoformat(),
            "zaehlweise": ZAEHLWEISE,
            "umgestellt_am": UMGESTELLT_AM,
        }


# ---------------------------------------------------------------------------
# Fahrplan (chamo-Prototyp)
# ---------------------------------------------------------------------------


try:  # Zeitquelle auf Modulebene, damit Tests sie ersetzen können
    from homeassistant.util import dt as _dt_util

    def _now_local() -> Any:
        return _dt_util.now()

except ImportError:  # Testumgebung ohne HA

    def _now_local() -> Any:
        from datetime import datetime

        return datetime.now()


def _aktueller_slot(hass: Any, entry_id: str) -> tuple[dict | None, dict | None]:
    """Liefert (Slot für jetzt, Fahrplan-Zustand) aus dem ScheduleRunner.

    Der Slot-Lookup selbst liegt in ``schedule.slot_for`` — derselbe Helfer,
    den auch der Executor nutzt, damit Anzeige und Steuerung nie
    unterschiedliche Slots sehen.
    """
    runner = hass.data.get(DOMAIN, {}).get(entry_id, {}).get("schedule")
    if runner is None:
        return None, None
    zustand = runner.to_dict()
    if not zustand.get("available"):
        return None, zustand

    from .schedule import slot_for

    return slot_for(zustand.get("slots"), _now_local()), zustand


class FahrplanBatterieleistungSensor(SensorEntity):
    """Geplante Batterieleistung des laufenden Fahrplan-Slots.

    Vorzeichen wie beim Ist-Sensor Batterieleistung: positiv = laden,
    negativ = entladen. Der Fahrplan selbst rechnet umgekehrt (Haralds
    Konvention: positiv = entladen), das wird hier gedreht — nur so lassen
    sich Plan und Ist im selben Diagramm übereinanderlegen.
    """

    _attr_has_entity_name = True
    _attr_name = "Fahrplan Batterieleistung"
    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery-clock"
    _attr_suggested_display_precision = 2

    def __init__(self, hass: Any, entry: Any) -> None:
        self.hass = hass
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_fahrplan_batterieleistung"
        self._attr_device_info = _device_info(entry.entry_id)
        self._attr_native_value: float | None = None
        self._attr_extra_state_attributes: dict[str, Any] = {}

    async def async_update(self) -> None:
        slot, zustand = _aktueller_slot(self.hass, self._entry_id)

        if slot is None:
            self._attr_native_value = None
            hinweis = "Fahrplan-Modul nicht aktiv"
            if zustand:
                hinweis = zustand.get("error") or "Kein Slot für die aktuelle Zeit"
            self._attr_extra_state_attributes = {"hinweis": hinweis}
            return

        battery_p = slot.get("battery_p")
        self._attr_native_value = None if battery_p is None else round(-battery_p, 3)
        self._attr_extra_state_attributes = {
            "slot": slot["t"][11:16],
            "ziel_soc_pct": slot.get("soc"),
            "netzleistung_kw": slot.get("grid_p"),
            "einspeisepreis_ct": _ct(zustand, slot),
            "batteriewert_ct": _ct_wert(slot.get("bat_price")),
            "berechnet_um": (zustand.get("last_run") or "")[11:19],
            "rechenzeit_ms": zustand.get("duration_ms"),
        }


class FahrplanNetzleistungSensor(SensorEntity):
    """Geplante Netzleistung des laufenden Slots — positiv = Einspeisung.

    Gleiche Vorzeichenkonvention wie der Ist-Sensor Netzleistung.
    """

    _attr_has_entity_name = True
    _attr_name = "Fahrplan Netzleistung"
    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:transmission-tower-export"
    _attr_suggested_display_precision = 2

    def __init__(self, hass: Any, entry: Any) -> None:
        self.hass = hass
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_fahrplan_netzleistung"
        self._attr_device_info = _device_info(entry.entry_id)
        self._attr_native_value: float | None = None
        self._attr_extra_state_attributes: dict[str, Any] = {}

    async def async_update(self) -> None:
        slot, zustand = _aktueller_slot(self.hass, self._entry_id)

        if slot is None:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {
                "hinweis": (zustand or {}).get("error") or "Kein Fahrplan"
            }
            return

        grid_p = slot.get("grid_p")
        self._attr_native_value = None if grid_p is None else round(grid_p, 3)
        self._attr_extra_state_attributes = {
            "slot": slot["t"][11:16],
            "pv_kw": slot.get("PV"),
            "verbrauch_kw": slot.get("consumption"),
        }


def _ct(zustand: dict, slot: dict) -> float | None:
    """Einspeisepreis dieses Slots in Cent, falls im Fahrplan enthalten."""
    preis = slot.get("feedin_price")
    return None if preis is None else round(preis * 100, 2)


def _ct_wert(wert: float | None) -> float | None:
    """Schattenpreis in Cent je kWh — was eine kWh im Speicher gerade wert ist."""
    return None if wert is None else round(wert * 100, 2)


# ---------------------------------------------------------------------------
# Platform setup
# ---------------------------------------------------------------------------

async def async_setup_entry(
    hass: Any,
    entry: Any,
    async_add_entities: Any,
) -> None:
    """Set up sensor platform for EEG Energy Optimizer."""
    data = hass.data[DOMAIN][entry.entry_id]
    config = data["config"]

    # Backfill Hausverbrauch statistics before first coordinator load
    # Start backfill in background — don't block startup
    from .__init__ import async_backfill_hausverbrauch_stats

    lookback_weeks = config.get(CONF_LOOKBACK_WEEKS, DEFAULT_LOOKBACK_WEEKS)
    coordinator = ConsumptionCoordinator(hass, CONSUMPTION_SENSOR, lookback_weeks)
    # Single initial load — backfill runs in background and refreshes after
    await coordinator.async_update()

    async def _backfill_then_refresh():
        await async_backfill_hausverbrauch_stats(hass, config)
        # Zwei Latenzquellen haben das Panel bisher minutenlang
        # "Verbrauchsdaten werden berechnet..." anzeigen lassen:
        # (1) async_import_statistics läuft asynchron über die Recorder-
        #     Queue — direkt nach dem Backfill ist der Import oft noch
        #     nicht lesbar (auf großen Instanzen bis ~1 min).
        # (2) Der Profil-SENSOR (dessen stats_count das Panel prüft) wurde
        #     nur vom Slow-Timer aktualisiert — Default alle 15 Minuten.
        # Daher: kurz nachfassen, bis der Coordinator Daten sieht, und
        # dabei die Profil-Sensoren direkt aktualisieren. Gibt es gar
        # keine Sensor-Historie (fabrikneue Instanz), läuft die Schleife
        # nach ~2 min leer aus — das Profil füllt sich dann mit der Zeit.
        for delay in (0, 5, 10, 20, 30, 60):
            if delay:
                await asyncio.sleep(delay)
            refresh = data.get("refresh_consumption_profile")
            if refresh is not None:
                # Aktualisiert Coordinator + Profil-/Prognose-Sensoren
                # inkl. State-Write (Panel-Hinweis verschwindet sofort).
                await refresh()
            else:
                # Sensoren noch nicht registriert (Setup läuft noch) —
                # nur den Coordinator laden.
                await coordinator.async_update()
            if coordinator.stats_count > 0:
                break

    hass.async_create_task(_backfill_then_refresh())

    # Create forecast provider
    source = config.get(CONF_FORECAST_SOURCE, FORECAST_SOURCE_SOLCAST)
    remaining_id = config.get(CONF_FORECAST_REMAINING_ENTITY, "")
    tomorrow_id = config.get(CONF_FORECAST_TOMORROW_ENTITY, "")

    if source == FORECAST_SOURCE_SOLCAST:
        provider = SolcastProvider(hass, remaining_id, tomorrow_id)
    else:
        provider = ForecastSolarProvider(hass, remaining_id, tomorrow_id)

    # Store for other components
    data["coordinator"] = coordinator
    data["provider"] = provider

    # Create sensors
    profil_sensor = VerbrauchsprofilSensor(hass, entry, coordinator)

    daily_sensors = [
        DailyForecastSensor(hass, entry, coordinator, day_offset=d)
        for d in range(7)
    ]

    pv_today_sensor = PVForecastTodaySensor(hass, entry, provider)
    pv_tomorrow_sensor = PVForecastTomorrowSensor(hass, entry, provider)
    hausverbrauch_sensor = HausverbrauchSensor(hass, entry, config)
    pv_leistung_sensor = PVLeistungSensor(hass, entry, config)
    netzleistung_sensor = NetzleistungSensor(hass, entry, config)
    batterieleistung_sensor = BatterieleistungSensor(hass, entry, config)

    # Combined-pair sensors (Fronius and similar). Created only when both
    # pair config keys are present, so single-sensor setups (Huawei, SolaX,
    # SolarEdge) get no extra entities.
    combined_battery_sensor = (
        BatteryPowerCombinedSensor(hass, entry, config)
        if _has_battery_pair(config) else None
    )
    combined_grid_sensor = (
        GridPowerCombinedSensor(hass, entry, config)
        if _has_grid_pair(config) else None
    )

    # Register writes sensor — reads counter from inverter object
    inverter = data.get("inverter")
    register_writes_sensor = RegisterWritesSensor(hass, entry, inverter)

    # Combined SOC/Capacity sensors — only created when the driver actually
    # provides them (multi-battery setups, currently only SolarEdge i1+i2+…).
    # Single-battery drivers (Huawei, Fronius, SolaX) return (None, None)
    # from get_combined_battery_state() → no extra entities.
    if _inverter_has_combined_state(inverter):
        combined_soc_sensor = CombinedBatterySocSensor(hass, entry, inverter)
        combined_capacity_sensor = CombinedBatteryCapacitySensor(
            hass, entry, inverter
        )
    else:
        combined_soc_sensor = None
        combined_capacity_sensor = None

    # Sensor 18: Fahrplan-Status (updated by the 30-s guard cycle, not by
    # fast/slow timers). Key "decision_sensor" bleibt — der Guard-Lauf in
    # __init__.py und Tests greifen darüber zu.
    decision_sensor = FahrplanStatusSensor(entry.entry_id)
    data["decision_sensor"] = decision_sensor

    slow_sensors: list[SensorEntity] = [profil_sensor]
    fast_sensors: list[SensorEntity] = (
        daily_sensors
        + [pv_today_sensor, pv_tomorrow_sensor,
           hausverbrauch_sensor, pv_leistung_sensor, netzleistung_sensor,
           batterieleistung_sensor, register_writes_sensor]
        + ([combined_battery_sensor] if combined_battery_sensor else [])
        + ([combined_grid_sensor] if combined_grid_sensor else [])
        + ([combined_soc_sensor] if combined_soc_sensor else [])
        + ([combined_capacity_sensor] if combined_capacity_sensor else [])
        # Fahrplan-Prototyp: Plan-Werte im selben Takt wie die Ist-Werte,
        # damit Plan und Ist in der Recorder-Historie vergleichbar sind.
        + [
            FahrplanBatterieleistungSensor(hass, entry),
            FahrplanNetzleistungSensor(hass, entry),
            EntladungInsNetzSensor(hass, entry),
        ]
    )

    async_add_entities(slow_sensors + fast_sensors + [decision_sensor], False)

    # Dual update timers — festverdrahtet, keine Konfig-Schlüssel mehr (v26)
    slow_interval = DEFAULT_UPDATE_INTERVAL_SLOW
    fast_interval = DEFAULT_UPDATE_INTERVAL_FAST

    async def _slow_update(_now_dt: Any = None) -> None:
        await coordinator.async_update()
        for sensor in slow_sensors:
            await sensor.async_update()
            sensor.async_write_ha_state()

    async def _fast_update(_now_dt: Any = None) -> None:
        for sensor in fast_sensors:
            await sensor.async_update()
            sensor.async_write_ha_state()

    # Manueller Refresh des Verbrauchsprofils (vom Panel via WebSocket aufgerufen).
    # Aktualisiert Coordinator + alle profilabhängigen Sensoren (Profil,
    # Tagesprognosen). Wird über einen Lock serialisiert, damit kein
    # paralleler Slow-Timer-Lauf reinpfuscht.
    refresh_lock = asyncio.Lock()
    profile_dependent_sensors: list[SensorEntity] = [profil_sensor] + daily_sensors

    async def _refresh_consumption_profile() -> None:
        async with refresh_lock:
            await coordinator.async_update()
            for sensor in profile_dependent_sensors:
                await sensor.async_update()
                sensor.async_write_ha_state()

    data["refresh_consumption_profile"] = _refresh_consumption_profile
    data["consumption_refresh_lock"] = refresh_lock

    if async_track_time_interval is not None:
        unsub_slow = async_track_time_interval(
            hass, _slow_update, timedelta(minutes=slow_interval)
        )
        unsub_fast = async_track_time_interval(
            hass, _fast_update, timedelta(minutes=fast_interval)
        )
        entry.async_on_unload(unsub_slow)
        entry.async_on_unload(unsub_fast)

        # Initial sensor update in background — don't block setup
        # Optimizer timer will keep them current every 30s anyway
        async def _initial_sensor_update():
            for sensor in slow_sensors + fast_sensors:
                await sensor.async_update()
                sensor.async_write_ha_state()

        hass.async_create_task(_initial_sensor_update())
