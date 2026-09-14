"""Tests für die v29-Migration: der Altschlüssel heizstab_alt_temp_c geht.

Die „Temperatur der anderen Heizquelle“ ist seit 2.1.1-dev6 wirkungslos
(CHANGELOG), stand aber in Bestandskonfigurationen weiter herum — in
Grünbach mit 55. Die Migration räumt ihn weg und lässt alles andere stehen.
"""

from unittest.mock import MagicMock

import pytest


def _v29_call(hass):
    return next(
        c for c in hass.config_entries.async_update_entry.call_args_list
        if c.kwargs.get("version") == 29
    )


async def _migriere(data: dict) -> dict:
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    entry = MagicMock()
    entry.version = 28
    entry.data = data
    await async_migrate_entry(hass, entry)
    _args, kwargs = _v29_call(hass)
    return kwargs.get("data") or _args[1]


@pytest.mark.asyncio
async def test_altschluessel_wird_entfernt_rest_bleibt():
    new_data = await _migriere({
        "inverter_type": "fronius_gen24",
        "heizstab_enabled": True,
        "heizstab_maxtemp_c": 80.0,
        "heizstab_alt_temp_c": 55,
    })
    assert "heizstab_alt_temp_c" not in new_data
    assert new_data["heizstab_enabled"] is True
    assert new_data["heizstab_maxtemp_c"] == 80.0
    assert new_data["inverter_type"] == "fronius_gen24"


@pytest.mark.asyncio
async def test_ohne_altschluessel_nur_der_versionssprung():
    daten = {"inverter_type": "huawei_sun2000", "heizstab_enabled": False}
    new_data = await _migriere(dict(daten))
    assert new_data == daten
