"""Tests für die v28-Migration: Bezugspreis in zwei Teilen.

Der Bezugspreis wird seit v28 als Arbeitspreis + Netzgebühr eingegeben, der
SNAP ist ein Haken statt eines zweiten Preises, der Nachtpreis samt Fenster
ist entfallen. Die Migration muss den Fahrplan unverändert lassen: Gesamt-
und SNAP-Preis müssen aus den neuen Teilen exakt wieder herauskommen.
"""

from unittest.mock import MagicMock

import pytest


def _v28_call(hass):
    """Den v28-spezifischen async_update_entry-Call herausfiltern."""
    return next(
        c for c in hass.config_entries.async_update_entry.call_args_list
        if c.kwargs.get("version") == 28
    )


async def _migriere(data: dict) -> dict:
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    entry = MagicMock()
    entry.version = 27
    entry.data = data
    await async_migrate_entry(hass, entry)
    _args, kwargs = _v28_call(hass)
    return kwargs.get("data") or _args[1]


@pytest.mark.asyncio
async def test_gesamtpreis_mit_snap_wird_in_arbeitspreis_und_netzgebuehr_zerlegt():
    """Alt: 25 ct gesamt, 23,74 ct im SNAP-Fenster (= 20 % von 6,3 ct Netz).
    Neu: Arbeitspreis 18,7 ct + Netzgebühr 6,3 ct, SNAP an — dieselben Preise."""
    new_data = await _migriere({
        "inverter_type": "huawei_sun2000",
        "schedule_consumption_price": 0.25,
        "schedule_consumption_price_snap": 0.2374,
        "schedule_consumption_price_night": 0.18,
        "schedule_consumption_night_start": "22:00",
        "schedule_consumption_night_end": "06:00",
    })

    assert new_data["schedule_network_fee"] == pytest.approx(0.063)
    assert new_data["schedule_energy_price"] == pytest.approx(0.187)
    assert new_data["schedule_snap_enabled"] is True
    # Rückrechnung: Gesamt- und SNAP-Preis wie vorher
    gesamt = new_data["schedule_energy_price"] + new_data["schedule_network_fee"]
    assert gesamt == pytest.approx(0.25)
    assert gesamt - new_data["schedule_network_fee"] * 0.20 == pytest.approx(0.2374)
    for alt in (
        "schedule_consumption_price", "schedule_consumption_price_snap",
        "schedule_consumption_price_night", "schedule_consumption_night_start",
        "schedule_consumption_night_end",
    ):
        assert alt not in new_data


@pytest.mark.asyncio
async def test_gesamtpreis_ohne_snap_wird_zum_arbeitspreis():
    new_data = await _migriere({
        "inverter_type": "fronius_gen24",
        "schedule_consumption_price": 0.26,
        "schedule_consumption_price_snap": 0,
    })
    assert new_data["schedule_energy_price"] == pytest.approx(0.26)
    assert new_data["schedule_network_fee"] == 0.0
    assert new_data["schedule_snap_enabled"] is False
    assert "schedule_consumption_price" not in new_data


@pytest.mark.asyncio
async def test_snap_preis_ueber_dem_gesamtpreis_ist_kein_snap():
    """Ein SNAP-Preis, der nicht unter dem Gesamtpreis liegt, war nie ein
    Rabatt — er wird verworfen statt eine negative Netzgebühr zu erzeugen."""
    new_data = await _migriere({
        "schedule_consumption_price": 0.25,
        "schedule_consumption_price_snap": 0.25,
    })
    assert new_data["schedule_energy_price"] == pytest.approx(0.25)
    assert new_data["schedule_network_fee"] == 0.0
    assert new_data["schedule_snap_enabled"] is False


@pytest.mark.asyncio
async def test_ohne_bezugspreis_bleibt_die_konfiguration_ohne_preis():
    """Wer nie einen Bezugspreis eingetragen hat (Einspeisung + grid_fee als
    Vorgabe), bekommt auch keinen erfundenen — nur die Nachtschlüssel gehen."""
    new_data = await _migriere({
        "inverter_type": "huawei_sun2000",
        "schedule_consumption_price_night": 0.18,
    })
    assert "schedule_energy_price" not in new_data
    assert "schedule_network_fee" not in new_data
    assert "schedule_consumption_price_night" not in new_data


@pytest.mark.asyncio
async def test_bereits_zerlegte_preise_bleiben_unangetastet():
    new_data = await _migriere({
        "schedule_energy_price": 0.19,
        "schedule_network_fee": 0.06,
        "schedule_snap_enabled": True,
        "schedule_consumption_price": 0.99,
    })
    assert new_data["schedule_energy_price"] == 0.19
    assert new_data["schedule_network_fee"] == 0.06
    assert new_data["schedule_snap_enabled"] is True
    assert "schedule_consumption_price" not in new_data


@pytest.mark.asyncio
async def test_already_v28_no_migration():
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    entry = MagicMock()
    entry.version = 28
    entry.data = {"schedule_consumption_price": 0.25}

    await async_migrate_entry(hass, entry)

    versions = [
        c.kwargs.get("version")
        for c in hass.config_entries.async_update_entry.call_args_list
    ]
    assert 28 not in versions
