"""Tests für die Reload-Entscheidung des Config-Update-Listeners.

Geänderte Sensor-Zuordnungen oder Inverter-Anbindungen erfordern einen
vollen Reload (Plattform-Entities und Inverter/Provider cachen ihre Config
bei der Konstruktion); reine Einstellungs-Änderungen nehmen den
Hot-Reload-Pfad, damit der Optimizer-Tageszustand erhalten bleibt.
Beta-Befund 19.08.2026: Nach PV-Sensor-Wechsel nutzte der Optimizer den
neuen Sensor, das Dashboard bis zum HA-Neustart den alten.
"""
from __future__ import annotations

from custom_components.eeg_energy_optimizer import _requires_full_reload


BASE = {
    "inverter_type": "kostal_plenticore",
    "pv_power_sensor": "sensor.a_solar_power",
    "battery_soc_sensor": "sensor.a_battery_soc",
    "kostal_modbus_host": "192.168.1.50",
    "discharge_power_kw": 5.0,
    "min_soc": 10,
}


def test_sensor_mapping_change_requires_reload():
    new = {**BASE, "pv_power_sensor": "sensor.a_sum_power_of_all_pv_dc_inputs"}
    assert _requires_full_reload(BASE, new) is True


def test_modbus_host_change_requires_reload():
    new = {**BASE, "kostal_modbus_host": "192.168.1.51"}
    assert _requires_full_reload(BASE, new) is True


def test_inverter_type_change_requires_reload():
    new = {**BASE, "inverter_type": "fronius_gen24"}
    assert _requires_full_reload(BASE, new) is True


def test_added_second_pv_sensor_requires_reload():
    new = {**BASE, "pv_power_sensor_2": "sensor.b_sum_power_of_all_pv_dc_inputs"}
    assert _requires_full_reload(BASE, new) is True


def test_forecast_entity_change_requires_reload():
    old = {**BASE, "forecast_remaining_entity": "sensor.solcast_rest"}
    new = {**BASE, "forecast_remaining_entity": "sensor.other_rest"}
    assert _requires_full_reload(old, new) is True


def test_settings_only_change_uses_hot_reload():
    new = {**BASE, "discharge_power_kw": 4.0, "min_soc": 15}
    assert _requires_full_reload(BASE, new) is False


def test_identical_config_uses_hot_reload():
    assert _requires_full_reload(BASE, dict(BASE)) is False


async def test_hot_reload_holt_fremddaten_und_rechnet_neu():
    """Nach dem Speichern sofort abrufen und rechnen, nicht erst im Takt.

    Ansfelden, 02.10.2026: Wechsel von fester Quote auf PeakShare-Prognose —
    der Plan rechnete eine halbe Stunde mit einem drei Wochen alten Cache.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.eeg_energy_optimizer import (
        DOMAIN,
        _async_update_listener,
    )

    nachlauf = AsyncMock()
    tasks = []
    hass = MagicMock()
    hass.async_create_task = lambda coro: tasks.append(coro)
    data = {
        "platforms_loaded": True,
        "config": dict(BASE),
        "executor": MagicMock(),
        "nach_einstellungen": nachlauf,
    }
    hass.data = {DOMAIN: {"e1": data}}
    entry = SimpleNamespace(
        entry_id="e1",
        data={**BASE, "eeg_demand_source": "peakshare"},
        options={},
    )

    await _async_update_listener(hass, entry)

    assert len(tasks) == 1
    await tasks[0]
    nachlauf.assert_awaited_once()
