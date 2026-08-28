"""Tests for EEG Energy Optimizer sensor platform."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.eeg_energy_optimizer.const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_CAPACITY_SENSOR,
    CONF_BATTERY_SOC_SENSOR,
    CONF_FORECAST_REMAINING_ENTITY,
    CONF_FORECAST_SOURCE,
    CONF_FORECAST_TOMORROW_ENTITY,
    CONF_LOOKBACK_WEEKS,
    DOMAIN,
    FORECAST_SOURCE_SOLCAST,
    WEEKDAY_KEYS,
)
from custom_components.eeg_energy_optimizer.forecast_provider import PVForecast


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(value, attributes=None):
    """Create a mock entity state."""
    state = MagicMock()
    state.state = str(value) if value is not None else "unavailable"
    state.attributes = attributes or {}
    return state


def _make_coordinator(hourly_avg=None, stats_count=100):
    """Create a mock ConsumptionCoordinator."""
    coord = MagicMock()
    coord.hourly_avg = hourly_avg or {
        day: {h: 500.0 for h in range(24)} for day in WEEKDAY_KEYS
    }
    coord.stats_count = stats_count
    coord.async_update = AsyncMock()
    coord.calculate_period = MagicMock(return_value={
        "verbrauch_kwh": 6.0,
        "stunden": 12.0,
        "stundenprofil": [],
    })
    return coord


def _make_provider(remaining=12.5, tomorrow=25.0):
    """Create a mock ForecastProvider."""
    provider = MagicMock()
    provider.get_forecast.return_value = PVForecast(
        remaining_today_kwh=remaining,
        tomorrow_kwh=tomorrow,
    )
    return provider


# ---------------------------------------------------------------------------
# Battery Missing Energy Sensor
# ---------------------------------------------------------------------------

class TestPVForecastSensors:
    """Tests for PVForecastTodaySensor and PVForecastTomorrowSensor."""

    def _make_today_sensor(self, hass, entry, provider):
        from custom_components.eeg_energy_optimizer.sensor import PVForecastTodaySensor
        return PVForecastTodaySensor(hass, entry, provider)

    def _make_tomorrow_sensor(self, hass, entry, provider):
        from custom_components.eeg_energy_optimizer.sensor import PVForecastTomorrowSensor
        return PVForecastTomorrowSensor(hass, entry, provider)

    @pytest.mark.asyncio
    async def test_pv_forecast_today(self, mock_hass):
        """Provider returns 12.5 -> sensor value 12.5."""
        entry = MagicMock()
        entry.entry_id = "test_entry"
        provider = _make_provider(remaining=12.5, tomorrow=25.0)

        sensor = self._make_today_sensor(mock_hass, entry, provider)
        await sensor.async_update()
        assert sensor.native_value == 12.5

    @pytest.mark.asyncio
    async def test_pv_forecast_tomorrow(self, mock_hass):
        """Provider returns 25.0 for tomorrow -> sensor value 25.0."""
        entry = MagicMock()
        entry.entry_id = "test_entry"
        provider = _make_provider(remaining=12.5, tomorrow=25.0)

        sensor = self._make_tomorrow_sensor(mock_hass, entry, provider)
        await sensor.async_update()
        assert sensor.native_value == 25.0

    @pytest.mark.asyncio
    async def test_pv_forecast_unavailable(self, mock_hass):
        """Provider returns None -> sensor value None."""
        entry = MagicMock()
        entry.entry_id = "test_entry"
        provider = _make_provider(remaining=None, tomorrow=None)

        sensor = self._make_today_sensor(mock_hass, entry, provider)
        await sensor.async_update()
        assert sensor.native_value is None

        sensor2 = self._make_tomorrow_sensor(mock_hass, entry, provider)
        await sensor2.async_update()
        assert sensor2.native_value is None


# ---------------------------------------------------------------------------
# Daily Forecast Sensor
# ---------------------------------------------------------------------------

class TestDailyForecastSensor:
    """Tests for DailyForecastSensor."""

    def _make_sensor(self, hass, entry, coordinator, day_offset):
        from custom_components.eeg_energy_optimizer.sensor import DailyForecastSensor
        return DailyForecastSensor(hass, entry, coordinator, day_offset)

    @pytest.mark.asyncio
    async def test_daily_forecast_today(self, mock_hass):
        """Day_offset=0: calculate_period called for remaining-day AND full-day total."""
        entry = MagicMock()
        entry.entry_id = "test_entry"
        coord = _make_coordinator()
        coord.calculate_period.return_value = {
            "verbrauch_kwh": 8.5,
            "stunden": 10.0,
            "stundenprofil": [],
        }

        sensor = self._make_sensor(mock_hass, entry, coord, 0)

        fixed_now = datetime(2026, 3, 21, 14, 0, 0, tzinfo=timezone.utc)
        with patch("custom_components.eeg_energy_optimizer.sensor._now", return_value=fixed_now):
            await sensor.async_update()

        assert sensor.native_value == 8.5
        # Two calls: 1) now → midnight+1d (remaining), 2) midnight → midnight+1d (total)
        assert coord.calculate_period.call_count == 2

        first_call = coord.calculate_period.call_args_list[0][0]
        assert first_call[0] == fixed_now
        assert first_call[1].hour == 0
        assert first_call[1].day == 22

        second_call = coord.calculate_period.call_args_list[1][0]
        assert second_call[0].hour == 0
        assert second_call[0].day == 21
        assert second_call[1].hour == 0
        assert second_call[1].day == 22

        # tagesverbrauch_gesamt_kwh attribute exposed for the dashboard chart
        assert "tagesverbrauch_gesamt_kwh" in sensor.extra_state_attributes

    @pytest.mark.asyncio
    async def test_daily_forecast_tomorrow(self, mock_hass):
        """Day_offset=1: calculate_period called for full tomorrow."""
        entry = MagicMock()
        entry.entry_id = "test_entry"
        coord = _make_coordinator()
        coord.calculate_period.return_value = {
            "verbrauch_kwh": 12.0,
            "stunden": 24.0,
            "stundenprofil": [],
        }

        sensor = self._make_sensor(mock_hass, entry, coord, 1)

        fixed_now = datetime(2026, 3, 21, 14, 0, 0, tzinfo=timezone.utc)
        with patch("custom_components.eeg_energy_optimizer.sensor._now", return_value=fixed_now):
            await sensor.async_update()

        assert sensor.native_value == 12.0
        call_args = coord.calculate_period.call_args[0]
        # Start should be midnight tomorrow, end should be midnight day after
        assert call_args[0].day == 22
        assert call_args[0].hour == 0
        assert call_args[1].day == 23
        assert call_args[1].hour == 0


# ---------------------------------------------------------------------------
# Verbrauchsprofil Sensor
# ---------------------------------------------------------------------------

class TestVerbrauchsprofilSensor:
    """Tests for VerbrauchsprofilSensor."""

    def _make_sensor(self, hass, entry, coordinator):
        from custom_components.eeg_energy_optimizer.sensor import VerbrauchsprofilSensor
        return VerbrauchsprofilSensor(hass, entry, coordinator)

    @pytest.mark.asyncio
    async def test_verbrauchsprofil_attributes(self, mock_hass):
        """Verify sensor exposes mo_watts, di_watts, etc. as attributes."""
        entry = MagicMock()
        entry.entry_id = "test_entry"
        # Test pinnt das Verhalten gegen den historischen Default discharge_start=20:00.
        # Aktueller Default ist 01:00 (Migration v14) — der hier explizite Wert
        # macht den Test unabhängig vom Default und prüft Nacht = 20:00 → sunrise+1h.
        entry.data = {"discharge_start_time": "20:00"}
        hourly_avg = {
            day: {h: 400.0 + h * 10.0 for h in range(24)}
            for day in WEEKDAY_KEYS
        }
        coord = _make_coordinator(hourly_avg=hourly_avg, stats_count=200)
        # No sun.sun → driver falls back to default day window (6..20)
        mock_hass.states.get = MagicMock(return_value=None)

        sensor = self._make_sensor(mock_hass, entry, coord)
        await sensor.async_update()

        attrs = sensor.extra_state_attributes
        # Hourly arrays + day totals per weekday
        for day in WEEKDAY_KEYS:
            assert f"{day}_watts" in attrs, f"Missing {day}_watts"
            assert f"{day}_kwh" in attrs, f"Missing {day}_kwh"
            assert f"{day}_tag_kwh" in attrs, f"Missing {day}_tag_kwh"
            assert f"{day}_nacht_kwh" in attrs, f"Missing {day}_nacht_kwh"
            assert len(attrs[f"{day}_watts"]) == 24
            # Tag + Nacht must add up to the day total (within rounding)
            total = attrs[f"{day}_kwh"]
            split_sum = attrs[f"{day}_tag_kwh"] + attrs[f"{day}_nacht_kwh"]
            assert abs(total - split_sum) <= 0.2

        assert "stunden" in attrs
        assert len(attrs["stunden"]) == 24
        assert attrs["stunden"][0] == "00:00"
        # Sunrise / sunset hours are exposed for the chart legend
        assert attrs["sunrise_hour"] == 6
        assert attrs["sunset_hour"] == 20
        assert "stats_count" in attrs
        assert "grundlage" in attrs

    @pytest.mark.asyncio
    async def test_verbrauchsprofil_uses_sun_state_for_day_window(self, mock_hass):
        """Day window adapts to actual sunrise/sunset times from sun.sun."""
        from datetime import datetime, timezone

        entry = MagicMock()
        entry.entry_id = "test_entry"
        # Test pinnt das Verhalten gegen den historischen Default discharge_start=20:00.
        entry.data = {"discharge_start_time": "20:00"}
        # Constant 1000 W per hour → 1 kWh per hour, 24 kWh per day
        hourly_avg = {day: {h: 1000.0 for h in range(24)} for day in WEEKDAY_KEYS}
        coord = _make_coordinator(hourly_avg=hourly_avg, stats_count=200)

        sun_state = _make_state(
            "above_horizon",
            {
                "next_rising": "2026-04-27T05:00:00+00:00",
                "next_setting": "2026-04-27T19:00:00+00:00",
            },
        )
        mock_hass.states.get = MagicMock(
            side_effect=lambda eid: sun_state if eid == "sun.sun" else None
        )

        sensor = self._make_sensor(mock_hass, entry, coord)
        # _as_local is a MagicMock in the test environment — keep it identity
        with patch(
            "custom_components.eeg_energy_optimizer.sensor._as_local",
            side_effect=lambda dt: dt,
        ):
            await sensor.async_update()

        attrs = sensor.extra_state_attributes
        assert attrs["sunrise_hour"] == 5
        assert attrs["sunset_hour"] == 19
        # Night mirrors optimizer: discharge_start (default 20:00) → sunrise+1h next day.
        # With sunrise 05:00, night_end = 06:00.
        # Hours 20–23 today (4h × 1 kWh) + hours 0–5 next day (6h × 1 kWh) = 10 kWh
        # Day = 24 - 10 = 14 kWh
        assert attrs["mo_nacht_kwh"] == 10.0
        assert attrs["mo_tag_kwh"] == 14.0
        assert attrs["discharge_start_hour"] == 20
        assert attrs["night_end_decimal"] == 6.0


# ---------------------------------------------------------------------------
# Sunrise Forecast Sensor
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Entladung ins Netz heute — Rückkehr mit unveränderter Kennung
# ---------------------------------------------------------------------------
class TestEntladungInsNetzSensor:
    """Die ``unique_id`` ist der ganze Punkt an dieser Rückkehr.

    Sie entscheidet, ob die Langzeitstatistik der Anlage weiterläuft oder ob
    eine zweite, leere Reihe daneben beginnt. Ein Tippfehler hier wäre nicht
    reparabel, sobald die neue Kennung einmal in einer Datenbank steht.
    """

    @staticmethod
    def _entry():
        from types import SimpleNamespace

        return SimpleNamespace(entry_id="abc123", data={}, options={})

    def test_unique_id_ist_die_alte(self):
        from custom_components.eeg_energy_optimizer.sensor import (
            EntladungInsNetzSensor,
        )

        sensor = EntladungInsNetzSensor(MagicMock(), self._entry())
        # Gelesen wird das Attribut, nicht die Property: die
        # Entity-Basisklasse von Home Assistant ist in der Testumgebung ein
        # Stub und leitet _attr_unique_id nicht weiter.
        # Wortgleich mit dem früheren Sensor "Nacht-Entladung Energie heute".
        assert (
            sensor._attr_unique_id
            == "eeg_energy_optimizer_abc123_feedin_evening_heute"
        )

    def test_anzeigename_ist_neu(self):
        """Der Name darf sich ändern, die Kennung nicht — der Fahrplan entlädt
        auch tagsüber, „Nacht-Entladung" wäre falsch."""
        from custom_components.eeg_energy_optimizer.sensor import (
            EntladungInsNetzSensor,
        )

        sensor = EntladungInsNetzSensor(MagicMock(), self._entry())
        assert sensor._attr_name == "Entladung ins Netz heute"

    @pytest.mark.asyncio
    async def test_wert_und_attribute_kommen_aus_der_statistik(self):
        from custom_components.eeg_energy_optimizer.const import DOMAIN
        from custom_components.eeg_energy_optimizer.sensor import (
            EntladungInsNetzSensor,
        )
        from custom_components.eeg_energy_optimizer.statistics import (
            UMGESTELLT_AM,
            ZAEHLWEISE,
        )

        stats = MagicMock()
        stats.get_today_kwh = MagicMock(return_value=7.4567)
        hass = MagicMock()
        hass.data = {DOMAIN: {"abc123": {"feedin_stats": stats}}}

        sensor = EntladungInsNetzSensor(hass, self._entry())
        await sensor.async_update()

        assert sensor.native_value == pytest.approx(7.457)
        stats.get_today_kwh.assert_called_once_with("evening")
        attrs = sensor.extra_state_attributes
        # Der Bedeutungswechsel steht in der Entität selbst, nicht nur im
        # Changelog — dort findet ihn auch, wer die Reihe später auswertet.
        assert attrs["zaehlweise"] == ZAEHLWEISE
        assert attrs["umgestellt_am"] == UMGESTELLT_AM
        # last_reset gehört zu state_class TOTAL, sonst deutet das
        # Energie-Dashboard den täglichen Rücksprung als Zählerdefekt.
        assert "last_reset" in attrs

    @pytest.mark.asyncio
    async def test_ohne_statistik_bleibt_der_wert_stehen(self):
        from custom_components.eeg_energy_optimizer.const import DOMAIN
        from custom_components.eeg_energy_optimizer.sensor import (
            EntladungInsNetzSensor,
        )

        hass = MagicMock()
        hass.data = {DOMAIN: {"abc123": {}}}
        sensor = EntladungInsNetzSensor(hass, self._entry())
        await sensor.async_update()
        assert sensor.native_value == 0.0
