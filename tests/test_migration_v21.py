"""Tests für die v21-Migration: Altschlüssel der Zustands-Heuristik entfernen.

Seit dem Fahrplan-Umbau ist der LP-Fahrplan der einzige Aktor. Die 13
Schlüssel der alten Heuristik lagen nur noch als eingefrorene Migrationswerte
in der Konfiguration und werden von keiner Codestelle mehr gelesen.
discharge_a_start_time bleibt erhalten — sensor.py nutzt ihn als Tag/Nacht-
Trenner der Verbrauchsprofil-Anzeige.
"""

from unittest.mock import MagicMock

import pytest

OBSOLETE_KEYS = (
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
)


def _v21_call(hass):
    """Den v21-spezifischen async_update_entry-Call herausfiltern."""
    return next(
        c for c in hass.config_entries.async_update_entry.call_args_list
        if c.kwargs.get("version") == 21
    )


def _migrate(entry_data, version=20):
    """async_migrate_entry mit einem Mock-Entry laufen lassen."""
    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    entry = MagicMock()
    entry.version = version
    entry.data = entry_data
    return hass, entry


@pytest.mark.asyncio
async def test_v20_to_v21_removes_obsolete_keys():
    """Alle 13 Altschlüssel verschwinden aus entry.data."""
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass, entry = _migrate({
        "inverter_type": "huawei_sun2000",
        **{k: "irgendwas" for k in OBSOLETE_KEYS},
    })

    await async_migrate_entry(hass, entry)

    _args, kwargs = _v21_call(hass)
    new_data = kwargs.get("data") or _args[1]
    for key in OBSOLETE_KEYS:
        assert key not in new_data, f"{key} hätte entfernt werden müssen"
    assert kwargs.get("version") == 21


@pytest.mark.asyncio
async def test_v21_keeps_discharge_a_start_time():
    """discharge_a_start_time bleibt — sensor.py liest ihn weiterhin."""
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass, entry = _migrate({
        "inverter_type": "huawei_sun2000",
        "discharge_a_start_time": "21:30",
        "min_soc": 10,
        "enable_slot_a": True,
    })

    await async_migrate_entry(hass, entry)

    _args, kwargs = _v21_call(hass)
    new_data = kwargs.get("data") or _args[1]
    assert new_data["discharge_a_start_time"] == "21:30"
    assert "min_soc" not in new_data
    assert "enable_slot_a" not in new_data


@pytest.mark.asyncio
async def test_v21_keeps_active_schedule_config():
    """Die tatsächlich wirkende Fahrplan-Konfiguration bleibt unangetastet."""
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    active = {
        "inverter_type": "huawei_sun2000",
        "schedule_min_soc_pct": 12,
        "grid_export_limit_enabled": True,
        "grid_export_limit_kw": 6.5,
        "discharge_power_kw": 5.0,
        "enable_peakshare": True,
        "peakshare_community": "BEG",
        "setup_complete": True,
    }
    hass, entry = _migrate({**active, "min_soc": 10, "safety_buffer_pct": 25})

    await async_migrate_entry(hass, entry)

    _args, kwargs = _v21_call(hass)
    new_data = kwargs.get("data") or _args[1]
    for key, value in active.items():
        assert new_data[key] == value


@pytest.mark.asyncio
async def test_v21_tolerates_missing_keys():
    """Ein Entry ohne Altschlüssel migriert fehlerfrei (pop mit Default)."""
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass, entry = _migrate({"inverter_type": "fronius_gen24"})

    await async_migrate_entry(hass, entry)

    _args, kwargs = _v21_call(hass)
    new_data = kwargs.get("data") or _args[1]
    assert new_data == {"inverter_type": "fronius_gen24"}
    # v22 kommt danach und ergänzt eigene Schlüssel — hier zählt nur,
    # dass v21 selbst nichts hinzufügt.


@pytest.mark.asyncio
async def test_already_v21_no_migration():
    """Schon v21: die v21-Migration läuft nicht erneut.

    Spätere Schritte der Kette laufen sehr wohl — geprüft wird hier nur,
    dass sich v21 nicht wiederholt.
    """
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass, entry = _migrate({"inverter_type": "huawei_sun2000"}, version=21)

    await async_migrate_entry(hass, entry)

    versions = [
        c.kwargs.get("version")
        for c in hass.config_entries.async_update_entry.call_args_list
    ]
    assert 21 not in versions


@pytest.mark.asyncio
async def test_old_entry_runs_full_chain_through_v21():
    """Ein sehr alter Entry durchläuft die volle Kette und passiert v21."""
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass, entry = _migrate({"inverter_type": "huawei_sun2000"}, version=2)

    await async_migrate_entry(hass, entry)

    versions = [
        c.kwargs.get("version")
        for c in hass.config_entries.async_update_entry.call_args_list
    ]
    # Lückenlos aufsteigend ab 3 — kein Schritt wird übersprungen.
    assert versions == sorted(versions)
    assert 21 in versions
    assert versions[0] == 3


# ---------------------------------------------------------------------------
# v23 — peakshare_kind entfernen
# ---------------------------------------------------------------------------


def _v23_call(hass):
    return next(
        c for c in hass.config_entries.async_update_entry.call_args_list
        if c.kwargs.get("version") == 23
    )


@pytest.mark.asyncio
async def test_v23_entfernt_peakshare_kind():
    """Der Schlüssel wird von keiner Codestelle gelesen — er fliegt raus."""
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass, entry = _migrate({
        "inverter_type": "huawei_sun2000",
        "peakshare_kind": "eeg",
        "peakshare_kind_2": "beg",
        "peakshare_community": "Pucking",
        "peakshare_share_pct": 50,
    }, version=22)

    await async_migrate_entry(hass, entry)

    _args, kwargs = _v23_call(hass)
    new_data = kwargs.get("data") or _args[1]
    assert "peakshare_kind" not in new_data
    assert "peakshare_kind_2" not in new_data
    # Die wirkenden Gemeinschafts-Schlüssel bleiben unangetastet.
    assert new_data["peakshare_community"] == "Pucking"
    assert new_data["peakshare_share_pct"] == 50


@pytest.mark.asyncio
async def test_v23_ohne_die_schluessel_ist_harmlos():
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass, entry = _migrate({"inverter_type": "fronius_gen24"}, version=22)

    await async_migrate_entry(hass, entry)

    _args, kwargs = _v23_call(hass)
    new_data = kwargs.get("data") or _args[1]
    assert new_data == {"inverter_type": "fronius_gen24"}
