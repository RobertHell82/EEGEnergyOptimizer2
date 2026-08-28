"""Tests für die v26-Migration: Update-Takte sind festverdrahtet.

Das schnelle Intervall stand auf dem erlaubten Minimum, das zugleich die
Vorgabe war — ein Regler ohne sinnvolle Stellung. Das langsame betraf ein
Profil, das sich nur über Wochen ändert. Beide Schlüssel werden aus der
Konfiguration entfernt statt liegengelassen — ein Wert, den keine Codestelle
mehr liest, ist eine Falle für die nächste Suche.
"""

from unittest.mock import MagicMock

import pytest

OBSOLETE_KEYS = ("update_interval_fast_min", "update_interval_slow_min")


def _v26_call(hass):
    """Den v26-spezifischen async_update_entry-Call herausfiltern."""
    return next(
        c for c in hass.config_entries.async_update_entry.call_args_list
        if c.kwargs.get("version") == 26
    )


def _migrate(entry_data, version=25):
    """async_migrate_entry mit einem Mock-Entry laufen lassen."""
    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    entry = MagicMock()
    entry.version = version
    entry.data = entry_data
    return hass, entry


@pytest.mark.asyncio
async def test_v25_to_v26_removes_interval_keys():
    """Beide Takt-Schlüssel verschwinden, der Rest bleibt unberührt."""
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass, entry = _migrate({
        "inverter_type": "huawei_sun2000",
        "lookback_weeks": 4,
        "update_interval_fast_min": 1,
        "update_interval_slow_min": 15,
    })

    await async_migrate_entry(hass, entry)

    _args, kwargs = _v26_call(hass)
    new_data = kwargs.get("data") or _args[1]
    for key in OBSOLETE_KEYS:
        assert key not in new_data, f"{key} hätte entfernt werden müssen"
    assert new_data["lookback_weeks"] == 4


@pytest.mark.asyncio
async def test_v26_ohne_die_schluessel_ist_harmlos():
    """Anlage ohne die Schlüssel: nur die Version zieht mit."""
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass, entry = _migrate({"inverter_type": "huawei_sun2000"})

    await async_migrate_entry(hass, entry)

    _args, kwargs = _v26_call(hass)
    new_data = kwargs.get("data") or _args[1]
    assert new_data == {"inverter_type": "huawei_sun2000"}
