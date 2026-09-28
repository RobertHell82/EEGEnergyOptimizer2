"""Guard-Sperre, Entladen und HA-Stopp (__init__.py).

Die Verdrahtung in ``async_setup_entry`` ist ohne echtes HA nicht zu fahren;
getestet werden deshalb die Modul-Helfer, über die sie läuft:
``_unter_guard_sperre`` (ein Guard-Lauf zur Zeit), ``_stilllegen`` /
``_freigeben`` (Entladen, HA-Stopp) und ``async_unload_entry`` selbst.
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.eeg_energy_optimizer as integration
from custom_components.eeg_energy_optimizer.const import DOMAIN


# ---------------------------------------------------------------------------
# Ein Guard-Lauf zur Zeit (M1)
# ---------------------------------------------------------------------------


async def test_timer_ueberspringt_waehrend_ein_lauf_laeuft():
    lock = asyncio.Lock()
    data: dict = {}
    freigabe = asyncio.Event()
    laeufe = []

    async def langsamer_lauf():
        laeufe.append("lang")
        await freigabe.wait()

    async def lauf():
        laeufe.append("timer")

    erster = asyncio.create_task(
        integration._unter_guard_sperre(data, lock, langsamer_lauf, vom_timer=False)
    )
    await asyncio.sleep(0)
    assert lock.locked()

    assert await integration._unter_guard_sperre(data, lock, lauf, vom_timer=True) is False
    freigabe.set()
    assert await erster is True
    assert laeufe == ["lang"]


async def test_direkter_aufruf_wartet_auf_den_laufenden_lauf():
    """Pause/Override: der Nutzer hat geklickt — sein Lauf findet statt,
    aber erst nach dem laufenden, nicht mittendrin."""
    lock = asyncio.Lock()
    data: dict = {}
    freigabe = asyncio.Event()
    reihenfolge = []

    async def langsamer_lauf():
        reihenfolge.append("start lang")
        await freigabe.wait()
        reihenfolge.append("ende lang")

    async def klick():
        reihenfolge.append("klick")

    erster = asyncio.create_task(
        integration._unter_guard_sperre(data, lock, langsamer_lauf, vom_timer=False)
    )
    await asyncio.sleep(0)
    zweiter = asyncio.create_task(
        integration._unter_guard_sperre(data, lock, klick, vom_timer=False)
    )
    await asyncio.sleep(0)
    freigabe.set()
    await erster
    assert await zweiter is True
    assert reihenfolge == ["start lang", "ende lang", "klick"]


async def test_nach_dem_warten_zaehlt_das_stopp_flag():
    """Begann das Entladen, während ein Aufruf auf die Sperre wartete, läuft
    er nicht mehr — sonst schriebe er nach der Freigabe einen Befehl."""
    lock = asyncio.Lock()
    data: dict = {}
    freigabe = asyncio.Event()
    lauf = AsyncMock()

    async def langsamer_lauf():
        await freigabe.wait()

    erster = asyncio.create_task(
        integration._unter_guard_sperre(data, lock, langsamer_lauf, vom_timer=False)
    )
    await asyncio.sleep(0)
    wartender = asyncio.create_task(
        integration._unter_guard_sperre(data, lock, lauf, vom_timer=False)
    )
    await asyncio.sleep(0)
    data["stopping"] = True
    freigabe.set()
    await erster
    assert await wartender is False
    lauf.assert_not_awaited()


# ---------------------------------------------------------------------------
# Stilllegen und Freigeben (M2/M3)
# ---------------------------------------------------------------------------


def _data(eingriff=True):
    executor = MagicMock()
    executor.async_release = AsyncMock(return_value=True)
    executor.hat_eingriff = eingriff
    heizstab = MagicMock()
    heizstab.async_shutdown = AsyncMock()
    ambibox = MagicMock()
    ambibox.async_shutdown = AsyncMock()
    return {
        "executor": executor,
        "heizstab": heizstab,
        "ambibox": ambibox,
        "guard_lock": asyncio.Lock(),
    }


async def test_stilllegen_setzt_das_flag_und_wartet_auf_den_lauf():
    data = _data()
    await data["guard_lock"].acquire()
    aufgabe = asyncio.create_task(integration._stilllegen(data))
    await asyncio.sleep(0)
    # Sofort still, noch bevor der laufende Lauf fertig ist.
    assert data["stopping"] is True
    data["executor"].stilllegen.assert_called_once()
    data["heizstab"].stilllegen.assert_called_once()
    assert not aufgabe.done()
    data["guard_lock"].release()
    await aufgabe


async def test_stilllegen_wartet_nicht_ewig(monkeypatch):
    monkeypatch.setattr(integration, "GUARD_STOPP_WARTEN_S", 0.01)
    data = _data()
    await data["guard_lock"].acquire()
    await integration._stilllegen(data)   # kehrt trotz gehaltener Sperre zurück
    assert data["stopping"] is True


async def test_ha_stopp_gibt_frei_und_schaltet_den_heizstab_ab():
    data = _data(eingriff=True)
    await integration._stilllegen_und_freigeben(MagicMock(), data)
    assert data["stopping"] is True
    data["executor"].async_release.assert_awaited_once()
    data["heizstab"].async_shutdown.assert_awaited_once()
    data["ambibox"].async_shutdown.assert_awaited_once()


async def test_ha_stopp_ohne_eigenen_eingriff_fasst_den_wechselrichter_nicht_an():
    """Im Modus Aus ist bereits freigegeben — ein Stopp-Befehl träfe womöglich,
    was der Nutzer selbst am Gerät eingestellt hat."""
    data = _data(eingriff=False)
    await integration._stilllegen_und_freigeben(MagicMock(), data)
    data["executor"].async_release.assert_not_awaited()
    data["heizstab"].async_shutdown.assert_awaited_once()


async def test_fehler_beim_freigeben_laesst_den_heizstab_nicht_weiterheizen():
    data = _data()
    data["executor"].async_release = AsyncMock(side_effect=RuntimeError("weg"))
    await integration._freigeben(data, nur_bei_eingriff=False)
    data["heizstab"].async_shutdown.assert_awaited_once()


# ---------------------------------------------------------------------------
# async_unload_entry
# ---------------------------------------------------------------------------


@pytest.fixture
def frontend_stub(monkeypatch):
    """``homeassistant.components.frontend`` gibt es in der Testumgebung nicht."""
    for name in ("homeassistant", "homeassistant.components"):
        if name not in sys.modules:
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    frontend = types.ModuleType("homeassistant.components.frontend")
    frontend.async_remove_panel = MagicMock()
    monkeypatch.setitem(sys.modules, "homeassistant.components.frontend", frontend)
    return frontend


@pytest.mark.parametrize("unload_ok", [True, False])
async def test_unload_gibt_immer_frei(frontend_stub, unload_ok):
    """Auch wenn die Plattformen nicht sauber entladen: stillgelegt ist der
    Eintrag dann ohnehin, ein stehender Befehl liefe unbeaufsichtigt weiter."""
    data = _data()
    data["platforms_loaded"] = True
    data["inverter"] = MagicMock(async_disconnect=AsyncMock())
    hass = MagicMock()
    hass.data = {DOMAIN: {"e1": data}}
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=unload_ok)
    entry = MagicMock(entry_id="e1")

    assert await integration.async_unload_entry(hass, entry) is unload_ok

    assert data["stopping"] is True
    data["executor"].stilllegen.assert_called_once()
    data["executor"].async_release.assert_awaited_once()
    data["heizstab"].async_shutdown.assert_awaited_once()
    if unload_ok:
        assert "e1" not in hass.data[DOMAIN]
        data["inverter"].async_disconnect.assert_awaited_once()
    else:
        # Der Eintrag bleibt stehen (passiv), die Verbindung auch.
        assert "e1" in hass.data[DOMAIN]
        data["inverter"].async_disconnect.assert_not_awaited()


async def test_unload_legt_still_bevor_die_plattformen_entladen(frontend_stub):
    """Die Timer fallen erst NACH async_unload_entry weg — in der Lücke darf
    kein Guard-Lauf mehr schreiben."""
    data = _data()
    data["platforms_loaded"] = True
    beim_entladen = {}

    async def _unload_platforms(entry, platforms):
        beim_entladen["stopping"] = data.get("stopping")
        return True

    hass = MagicMock()
    hass.data = {DOMAIN: {"e1": data}}
    hass.config_entries.async_unload_platforms = _unload_platforms
    await integration.async_unload_entry(hass, MagicMock(entry_id="e1"))
    assert beim_entladen["stopping"] is True
