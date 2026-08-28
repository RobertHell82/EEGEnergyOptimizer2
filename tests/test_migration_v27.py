"""Tests für die v27-Migration: der Maximum-Ladestand hat keinen Schalter mehr.

Der Zustand steckt allein im Wert (100 = bis voll laden). War der Schalter
aus, blieb ein früher eingestellter Wert bewusst gespeichert, wirkte aber
nicht — die Migration stellt ihn dann auf 100, sonst begänne er nach dem
Update plötzlich zu wirken. Der Schalter-Schlüssel wird entfernt statt
liegengelassen.
"""

from unittest.mock import MagicMock

import pytest


def _v27_call(hass):
    """Den v27-spezifischen async_update_entry-Call herausfiltern."""
    return next(
        c for c in hass.config_entries.async_update_entry.call_args_list
        if c.kwargs.get("version") == 27
    )


def _migrate(entry_data, version=26):
    """async_migrate_entry mit einem Mock-Entry laufen lassen."""
    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    entry = MagicMock()
    entry.version = version
    entry.data = entry_data
    return hass, entry


@pytest.mark.asyncio
async def test_aktiver_deckel_behaelt_seinen_wert():
    """Schalter an + Wert 90: der Wert bleibt, nur der Schalter verschwindet."""
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass, entry = _migrate({
        "inverter_type": "huawei_sun2000",
        "schedule_max_soc_enabled": True,
        "schedule_max_soc_pct": 90,
    })

    await async_migrate_entry(hass, entry)

    _args, kwargs = _v27_call(hass)
    new_data = kwargs.get("data") or _args[1]
    assert "schedule_max_soc_enabled" not in new_data
    assert new_data["schedule_max_soc_pct"] == 90


@pytest.mark.asyncio
async def test_abgeschalteter_deckel_wird_neutralisiert():
    """Schalter aus + gespeicherter Wert: der Wert darf nicht plötzlich wirken."""
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass, entry = _migrate({
        "inverter_type": "huawei_sun2000",
        "schedule_max_soc_enabled": False,
        "schedule_max_soc_pct": 90,
    })

    await async_migrate_entry(hass, entry)

    _args, kwargs = _v27_call(hass)
    new_data = kwargs.get("data") or _args[1]
    assert "schedule_max_soc_enabled" not in new_data
    assert new_data["schedule_max_soc_pct"] == 100


@pytest.mark.asyncio
async def test_v27_ohne_die_schluessel_ist_harmlos():
    """Anlage, die den Deckel nie angefasst hat: nur die Version zieht mit."""
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass, entry = _migrate({"inverter_type": "huawei_sun2000"})

    await async_migrate_entry(hass, entry)

    _args, kwargs = _v27_call(hass)
    new_data = kwargs.get("data") or _args[1]
    assert new_data == {"inverter_type": "huawei_sun2000"}
