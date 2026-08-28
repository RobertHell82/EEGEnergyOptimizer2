"""Tests for ConsumptionCoordinator."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.eeg_energy_optimizer.coordinator import ConsumptionCoordinator


def _make_stat_entry(dt_local, mean_watts):
    """Create a statistics entry with a UTC timestamp."""
    return {
        "start": dt_local.astimezone(timezone.utc).timestamp(),
        "mean": mean_watts,
    }


def _generate_week_stats(base_monday, patterns):
    """Generate a week of hourly stats.

    patterns: dict mapping weekday index (0=Mon) to a function hour -> watts.
    """
    entries = []
    for day_offset in range(7):
        day = base_monday + timedelta(days=day_offset)
        weekday_idx = day.weekday()
        pattern_fn = patterns.get(weekday_idx, lambda h: 300.0)
        for hour in range(24):
            dt_local = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            entries.append(_make_stat_entry(dt_local, pattern_fn(hour)))
    return entries


# CET timezone (UTC+1) for Austrian tests
CET = timezone(timedelta(hours=1))


@pytest.fixture
def mock_hass():
    """Create mock hass for coordinator tests.

    country ist bewusst None: die Feiertagserkennung bleibt in diesen Tests
    ausgeschaltet, damit nur die Werktag/Wochenende-Trennung geprüft wird.
    Feiertage haben ihre eigene Suite (test_consumption_grouping.py).
    """
    hass = MagicMock()
    hass.data = {}
    hass.config.country = None
    return hass


@pytest.fixture
def two_weeks_stats():
    """Generate 2 weeks of statistics with a flat weekday/weekend split.

    Werktage tragen alle dasselbe Muster, Sa/So ein zweites — so bleiben die
    Erwartungswerte auch nach der Gruppierung exakt nachrechenbar.
    """
    # Week 1: Mon 2026-03-16 (a Monday in CET)
    base_w1 = datetime(2026, 3, 16, tzinfo=CET)
    # Week 2: Mon 2026-03-09
    base_w2 = datetime(2026, 3, 9, tzinfo=CET)

    workday = lambda h: 400.0 + h * 10   # noqa: E731 - Mo-Fr: 400-630W
    weekend = lambda h: 500.0 + h * 15   # noqa: E731 - Sa/So: 500-845W

    patterns = {
        0: workday,
        1: workday,
        2: workday,
        3: workday,
        4: workday,
        5: weekend,
        6: weekend,
    }

    entries = _generate_week_stats(base_w1, patterns) + _generate_week_stats(base_w2, patterns)
    return {"sensor.consumption": entries}


def _as_local_cet(dt_obj):
    """Convert a datetime to CET (UTC+1) for test purposes."""
    return dt_obj.astimezone(CET)


def _patch_recorder(stats_data):
    """Return context managers to patch recorder and timezone functions."""
    mock_stats = AsyncMock(return_value=stats_data)
    mock_get_instance = MagicMock()
    mock_recorder = MagicMock()
    mock_recorder.async_add_executor_job = mock_stats
    mock_get_instance.return_value = mock_recorder

    return (
        patch(
            "custom_components.eeg_energy_optimizer.coordinator.statistics_during_period",
            new=MagicMock(),
        ),
        patch(
            "custom_components.eeg_energy_optimizer.coordinator.get_instance",
            new=mock_get_instance,
        ),
        patch(
            "custom_components.eeg_energy_optimizer.coordinator._as_local",
            new=_as_local_cet,
        ),
        mock_stats,
        mock_recorder,
    )


class TestBucketGrouping:
    """Aggregiert wird über zwei Gruppen (wt/we), nicht über sieben Wochentage."""

    @pytest.mark.asyncio
    async def test_bucket_grouping(self, mock_hass, two_weeks_stats):
        coordinator = ConsumptionCoordinator(mock_hass, "sensor.consumption", 8)

        patch_sdp, patch_gi, patch_tz, mock_stats, mock_recorder = _patch_recorder(two_weeks_stats)
        with patch_sdp, patch_gi, patch_tz:
            await coordinator.async_update()

        # Quelle der Wahrheit sind die zwei Gruppen
        assert set(coordinator.bucket_avg.keys()) == {"wt", "we"}

        # Werktag Stunde 0 → 400W (Muster 400 + 0*10)
        assert abs(coordinator.bucket_avg["wt"][0] - 400.0) < 1.0
        # Werktag Stunde 20 → 600W (400 + 20*10)
        assert abs(coordinator.bucket_avg["wt"][20] - 600.0) < 1.0
        # Wochenende Stunde 12 → 680W (500 + 12*15)
        assert abs(coordinator.bucket_avg["we"][12] - 680.0) < 1.0

        assert coordinator.stats_count > 0

    @pytest.mark.asyncio
    async def test_hourly_avg_bleibt_siebentaegig(self, mock_hass, two_weeks_stats):
        """Panel-Kompatibilität: hourly_avg behält Schema und Werte je Gruppe."""
        coordinator = ConsumptionCoordinator(mock_hass, "sensor.consumption", 8)

        patch_sdp, patch_gi, patch_tz, mock_stats, mock_recorder = _patch_recorder(two_weeks_stats)
        with patch_sdp, patch_gi, patch_tz:
            await coordinator.async_update()

        assert set(coordinator.hourly_avg.keys()) == {"mo", "di", "mi", "do", "fr", "sa", "so"}
        for day in ["mo", "di", "mi", "do", "fr", "sa", "so"]:
            assert len(coordinator.hourly_avg[day]) == 24, f"{day} missing hours"

        # mo-fr tragen den wt-Wert, sa+so den we-Wert
        for day in ["mo", "di", "mi", "do", "fr"]:
            assert coordinator.hourly_avg[day] == coordinator.bucket_avg["wt"]
        for day in ["sa", "so"]:
            assert coordinator.hourly_avg[day] == coordinator.bucket_avg["we"]

    @pytest.mark.asyncio
    async def test_ein_werktag_hebt_alle_werktage(self, mock_hass, two_weeks_stats):
        """Ein Ausreißer an einem Dienstag darf nicht nur den Dienstag treffen.

        Er landet in der Werktagsgruppe und damit gleichmäßig auf mo-fr — und
        weil dort viele Stützwerte liegen, nur mit kleinem Gewicht.
        """
        entries = list(two_weeks_stats["sensor.consumption"])
        # Dienstag 2026-03-17, 18 Uhr: einmalige Wallbox-Ladung
        entries.append(
            _make_stat_entry(datetime(2026, 3, 17, 18, tzinfo=CET), 11000.0)
        )
        coordinator = ConsumptionCoordinator(mock_hass, "sensor.consumption", 8)

        patch_sdp, patch_gi, patch_tz, mock_stats, mock_recorder = _patch_recorder(
            {"sensor.consumption": entries}
        )
        with patch_sdp, patch_gi, patch_tz:
            await coordinator.async_update()

        # Alle Werktage sehen denselben Wert - keine Tagesschärfe mehr
        werktagswerte = {coordinator.hourly_avg[d][18] for d in ("mo", "di", "mi", "do", "fr")}
        assert len(werktagswerte) == 1
        # Wochenende bleibt unberührt: 500 + 18*15 = 770W
        assert abs(coordinator.hourly_avg["sa"][18] - 770.0) < 1.0


class TestCalculatePeriod:
    """Test consumption period calculations."""

    @pytest.mark.asyncio
    async def test_calculate_period_full_hours(self, mock_hass, two_weeks_stats):
        coordinator = ConsumptionCoordinator(mock_hass, "sensor.consumption", 8)

        patch_sdp, patch_gi, patch_tz, mock_stats, mock_recorder = _patch_recorder(two_weeks_stats)
        with patch_sdp, patch_gi, patch_tz:
            await coordinator.async_update()

        # Monday 08:00-10:00 -> 2 full hours: mo[8] + mo[9]
        start = datetime(2026, 3, 16, 8, 0, tzinfo=CET)  # Monday
        end = datetime(2026, 3, 16, 10, 0, tzinfo=CET)

        result = coordinator.calculate_period(start, end)
        mo_8 = coordinator.hourly_avg["mo"][8]  # 400 + 80 = 480W
        mo_9 = coordinator.hourly_avg["mo"][9]  # 400 + 90 = 490W
        expected_kwh = (mo_8 + mo_9) / 1000.0

        assert abs(result["verbrauch_kwh"] - expected_kwh) < 0.01
        assert result["stunden"] == 2.0

    @pytest.mark.asyncio
    async def test_calculate_period_partial_hours(self, mock_hass, two_weeks_stats):
        coordinator = ConsumptionCoordinator(mock_hass, "sensor.consumption", 8)

        patch_sdp, patch_gi, patch_tz, mock_stats, mock_recorder = _patch_recorder(two_weeks_stats)
        with patch_sdp, patch_gi, patch_tz:
            await coordinator.async_update()

        # Monday 08:30-10:00 -> 0.5h of hour 8 + 1.0h of hour 9
        start = datetime(2026, 3, 16, 8, 30, tzinfo=CET)
        end = datetime(2026, 3, 16, 10, 0, tzinfo=CET)

        result = coordinator.calculate_period(start, end)
        mo_8 = coordinator.hourly_avg["mo"][8]
        mo_9 = coordinator.hourly_avg["mo"][9]
        expected_kwh = (0.5 * mo_8 + 1.0 * mo_9) / 1000.0

        assert abs(result["verbrauch_kwh"] - expected_kwh) < 0.01
        assert result["stunden"] == 1.5

    @pytest.mark.asyncio
    async def test_calculate_period_cross_midnight(self, mock_hass, two_weeks_stats):
        coordinator = ConsumptionCoordinator(mock_hass, "sensor.consumption", 8)

        patch_sdp, patch_gi, patch_tz, mock_stats, mock_recorder = _patch_recorder(two_weeks_stats)
        with patch_sdp, patch_gi, patch_tz:
            await coordinator.async_update()

        # Sunday 23:00 to Monday 01:00 -> so[23] + mo[0]
        start = datetime(2026, 3, 15, 23, 0, tzinfo=CET)  # Sunday
        end = datetime(2026, 3, 16, 1, 0, tzinfo=CET)  # Monday

        result = coordinator.calculate_period(start, end)
        so_23 = coordinator.hourly_avg["so"][23]
        mo_0 = coordinator.hourly_avg["mo"][0]
        expected_kwh = (so_23 + mo_0) / 1000.0

        assert abs(result["verbrauch_kwh"] - expected_kwh) < 0.01
        assert result["stunden"] == 2.0

    def test_calculate_period_empty_returns_zero(self, mock_hass):
        coordinator = ConsumptionCoordinator(mock_hass, "sensor.consumption", 8)
        # No data loaded, hourly_avg is empty

        start = datetime(2026, 3, 16, 8, 0, tzinfo=CET)
        end = datetime(2026, 3, 16, 10, 0, tzinfo=CET)

        result = coordinator.calculate_period(start, end)
        assert result["verbrauch_kwh"] == 0.0

    def test_calculate_period_end_before_start(self, mock_hass):
        coordinator = ConsumptionCoordinator(mock_hass, "sensor.consumption", 8)

        start = datetime(2026, 3, 16, 10, 0, tzinfo=CET)
        end = datetime(2026, 3, 16, 8, 0, tzinfo=CET)

        result = coordinator.calculate_period(start, end)
        assert result["verbrauch_kwh"] == 0.0
        assert result["stunden"] == 0.0


def _entries_for_weekdays(base_monday, weekday_watts):
    """Statistikeinträge einer Woche, aber nur für die genannten Wochentage.

    weekday_watts: {weekday_index: watts} — nicht genannte Tage liefern
    absichtlich keine Daten, damit der Gruppen-Fallback greifen muss.
    """
    entries = []
    for day_offset in range(7):
        day = base_monday + timedelta(days=day_offset)
        watts = weekday_watts.get(day.weekday())
        if watts is None:
            continue
        for hour in range(24):
            dt_local = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            entries.append(_make_stat_entry(dt_local, watts))
    return {"sensor.consumption": entries}


class TestFallbackChain:
    """Fallback greift jetzt auf Gruppenebene: wt ↔ we."""

    @pytest.mark.asyncio
    async def test_wochenende_faellt_auf_werktag_zurueck(self, mock_hass):
        """Ohne Sa/So-Daten übernimmt die we-Gruppe die Werktagswerte."""
        base = datetime(2026, 3, 16, tzinfo=CET)  # Montag
        stats_data = _entries_for_weekdays(base, {0: 400.0, 1: 400.0, 2: 400.0, 3: 400.0, 4: 400.0})

        coordinator = ConsumptionCoordinator(mock_hass, "sensor.consumption", 8)
        patch_sdp, patch_gi, patch_tz, mock_stats, mock_recorder = _patch_recorder(stats_data)
        with patch_sdp, patch_gi, patch_tz:
            await coordinator.async_update()

        assert abs(coordinator.bucket_avg["we"][12] - 400.0) < 1.0
        assert abs(coordinator.hourly_avg["sa"][12] - 400.0) < 1.0
        assert abs(coordinator.hourly_avg["so"][12] - 400.0) < 1.0

    @pytest.mark.asyncio
    async def test_werktag_faellt_auf_wochenende_zurueck(self, mock_hass):
        """Umgekehrt genauso: ohne Mo-Fr-Daten zieht wt die we-Werte."""
        base = datetime(2026, 3, 16, tzinfo=CET)  # Montag
        stats_data = _entries_for_weekdays(base, {5: 500.0, 6: 500.0})

        coordinator = ConsumptionCoordinator(mock_hass, "sensor.consumption", 8)
        patch_sdp, patch_gi, patch_tz, mock_stats, mock_recorder = _patch_recorder(stats_data)
        with patch_sdp, patch_gi, patch_tz:
            await coordinator.async_update()

        assert abs(coordinator.bucket_avg["wt"][12] - 500.0) < 1.0
        assert abs(coordinator.hourly_avg["mo"][12] - 500.0) < 1.0

    @pytest.mark.asyncio
    async def test_ohne_daten_bleibt_null(self, mock_hass):
        """Greift keine Gruppe, ist der Wert 0.0 — nicht None, nicht Fehler."""
        base = datetime(2026, 3, 16, tzinfo=CET)
        # Nur Stunde 12 hat Daten (Montag), alle anderen Stunden sind leer
        entries = [_make_stat_entry(base.replace(hour=12), 400.0)]

        coordinator = ConsumptionCoordinator(mock_hass, "sensor.consumption", 8)
        patch_sdp, patch_gi, patch_tz, mock_stats, mock_recorder = _patch_recorder(
            {"sensor.consumption": entries}
        )
        with patch_sdp, patch_gi, patch_tz:
            await coordinator.async_update()

        assert abs(coordinator.bucket_avg["wt"][12] - 400.0) < 1.0
        assert coordinator.bucket_avg["wt"][3] == 0.0
        assert coordinator.bucket_avg["we"][3] == 0.0


class TestEmptyStatistics:
    """Test behavior with no statistics data."""

    @pytest.mark.asyncio
    async def test_empty_statistics(self, mock_hass):
        coordinator = ConsumptionCoordinator(mock_hass, "sensor.consumption", 8)

        stats_data = {"sensor.consumption": []}
        patch_sdp, patch_gi, patch_tz, mock_stats, mock_recorder = _patch_recorder(stats_data)
        with patch_sdp, patch_gi, patch_tz:
            await coordinator.async_update()

        assert coordinator.stats_count == 0
        # All hours should be 0.0
        for day in ["mo", "di", "mi", "do", "fr", "sa", "so"]:
            for hour in range(24):
                assert coordinator.hourly_avg[day][hour] == 0.0

    @pytest.mark.asyncio
    async def test_missing_sensor_in_stats(self, mock_hass):
        coordinator = ConsumptionCoordinator(mock_hass, "sensor.consumption", 8)

        stats_data = {}  # No data at all
        patch_sdp, patch_gi, patch_tz, mock_stats, mock_recorder = _patch_recorder(stats_data)
        with patch_sdp, patch_gi, patch_tz:
            await coordinator.async_update()

        assert coordinator.stats_count == 0
