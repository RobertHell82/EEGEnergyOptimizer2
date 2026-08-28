"""Tests für die v25-Migration: Schlüssel des verschiebbaren Überschussabschlags.

Der Abschlag bei Gemeinschaftsüberschuss bleibt und hängt wieder allein an der
Tarifdifferenz. Der Regler daneben ist entfallen, seine beiden Schlüssel
werden aus der Konfiguration entfernt statt liegengelassen — ein Wert, den
keine Codestelle mehr liest, ist eine Falle für die nächste Suche.
"""

from unittest.mock import MagicMock

import pytest

OBSOLETE_KEYS = ("peakshare_surplus_override", "peakshare_surplus_delta")


def _v25_call(hass):
    """Den v25-spezifischen async_update_entry-Call herausfiltern."""
    return next(
        c for c in hass.config_entries.async_update_entry.call_args_list
        if c.kwargs.get("version") == 25
    )


def _migrate(entry_data, version=24):
    """async_migrate_entry mit einem Mock-Entry laufen lassen."""
    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    entry = MagicMock()
    entry.version = version
    entry.data = entry_data
    return hass, entry


@pytest.mark.asyncio
async def test_v24_to_v25_removes_obsolete_keys():
    """Beide Schlüssel verschwinden, die Gemeinschaft selbst bleibt unberührt."""
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass, entry = _migrate({
        "inverter_type": "huawei_sun2000",
        "peakshare_community": "BEG",
        "peakshare_price": 0.122,
        "peakshare_surplus_override": True,
        "peakshare_surplus_delta": 0.02,
    })

    await async_migrate_entry(hass, entry)

    _args, kwargs = _v25_call(hass)
    new_data = kwargs.get("data") or _args[1]
    for key in OBSOLETE_KEYS:
        assert key not in new_data, f"{key} hätte entfernt werden müssen"
    assert new_data["peakshare_community"] == "BEG"
    assert new_data["peakshare_price"] == 0.122


@pytest.mark.asyncio
async def test_v25_ohne_die_schluessel_ist_harmlos():
    """Anlage, die den Regler nie eingeschaltet hat: nur die Version zieht mit."""
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass, entry = _migrate({"inverter_type": "huawei_sun2000"})

    await async_migrate_entry(hass, entry)

    _args, kwargs = _v25_call(hass)
    new_data = kwargs.get("data") or _args[1]
    assert new_data == {"inverter_type": "huawei_sun2000"}
