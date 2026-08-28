"""Tests for v20 migration: Feature "Einspeisebegrenzung optimieren".

Additive, sichere Migration: enable_feedin_limit=False (Feature aus) und ein
Vorbelegungswert feedin_limit_kw. Bestehende Installationen bleiben unverändert.
"""

from unittest.mock import MagicMock

import pytest


def _v20_call(hass):
    """Den v20-spezifischen async_update_entry-Call herausfiltern."""
    return next(
        c for c in hass.config_entries.async_update_entry.call_args_list
        if c.kwargs.get("version") == 20
    )


@pytest.mark.asyncio
async def test_v19_to_v20_sets_feedin_defaults():
    """v19-Entry bekommt enable_feedin_limit=False + feedin_limit_kw-Default."""
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    entry = MagicMock()
    entry.version = 19
    entry.data = {"inverter_type": "huawei_sun2000"}

    await async_migrate_entry(hass, entry)

    _args, kwargs = _v20_call(hass)
    new_data = kwargs.get("data") or _args[1]
    assert new_data["enable_feedin_limit"] is False
    # Literal statt Konstante: die Feature-Konstanten sind mit der alten
    # Einspeisebegrenzung entfallen, die Migration bleibt historisch exakt.
    assert new_data["feedin_limit_kw"] == 4.0
    assert kwargs.get("version") == 20


@pytest.mark.asyncio
async def test_v20_preserves_existing_values():
    """Bereits gesetzte feedin-Werte werden nicht überschrieben (setdefault)."""
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    entry = MagicMock()
    entry.version = 19
    entry.data = {
        "inverter_type": "fronius_gen24",
        "enable_feedin_limit": True,
        "feedin_limit_kw": 7.5,
    }

    await async_migrate_entry(hass, entry)

    _args, kwargs = _v20_call(hass)
    new_data = kwargs.get("data") or _args[1]
    assert new_data["enable_feedin_limit"] is True
    assert new_data["feedin_limit_kw"] == 7.5


@pytest.mark.asyncio
async def test_already_v20_no_feedin_migration():
    """Schon v20: die v20-Migration läuft nicht erneut."""
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    entry = MagicMock()
    entry.version = 20
    entry.data = {"inverter_type": "huawei_sun2000"}

    await async_migrate_entry(hass, entry)

    # Kein v20-Call. Spätere Schritte der Kette (v21) laufen sehr wohl —
    # geprüft wird hier nur, dass v20 sich nicht wiederholt.
    versions = [
        c.kwargs.get("version")
        for c in hass.config_entries.async_update_entry.call_args_list
    ]
    assert 20 not in versions
