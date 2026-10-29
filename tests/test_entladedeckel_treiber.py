"""Entladedeckel der Spitzenkappung in den drei Treibern, die ihn können.

Fronius (OutWRte mit StorCtl_Mod Bit 1), SMA (BatDschMaxW im CmpBMS-Block)
und Huawei (Number-Entität „Maximale Entladeleistung"). Jeder Treiber muss
dreierlei richtig machen: den Deckel neben einem Ladelimit halten, ihn ohne
anderen Modus als eigenen Zustand führen (mit Watchdog-Keepalive bei den
Modbus-Treibern) und ihn bei Freigabe und Zwangsentladung loswerden.
"""

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.eeg_energy_optimizer.inverter.base import InverterBase
from custom_components.eeg_energy_optimizer.inverter.fronius import (
    FroniusInverter,
    _OFFSET_INWRTE,
    _OFFSET_OUTWRTE,
    _OFFSET_STORCTL_MOD,
)
from custom_components.eeg_energy_optimizer.inverter.huawei import HuaweiInverter
from custom_components.eeg_energy_optimizer.inverter.sma import (
    DEFAULT_POWER_LIMIT_W,
    REG_BAT_CHA_MIN_W,
    REG_CMPBMS_OPMOD,
    CmpBmsBlock,
    SMAInverter,
    registers_to_s32,
    registers_to_u32,
)


def _ok(registers=None):
    res = MagicMock()
    res.isError = MagicMock(return_value=False)
    if registers is not None:
        res.registers = registers
    return res


class _Minimal(InverterBase):
    async def async_set_charge_limit(self, power_kw):
        return True

    async def async_set_discharge(self, power_kw, target_soc=None):
        return True

    async def async_stop_forcible(self):
        return True

    @property
    def is_available(self):
        return True


async def test_base_kann_keinen_deckel(mock_hass):
    inv = _Minimal(mock_hass, {})
    assert inv.supports_discharge_limit is False
    assert await inv.async_set_discharge_limit(0.0) is False
    assert inv.last_write_error


# ---------------------------------------------------------------------------
# Fronius
# ---------------------------------------------------------------------------


class _NoopStore:
    async def async_load(self):
        return {}

    async def async_save(self, data):
        return None


@pytest.fixture
def fronius(mock_hass):
    client = MagicMock()
    client.connected = True
    client.write_register = AsyncMock(return_value=_ok())
    client.read_holding_registers = AsyncMock(return_value=_ok([0]))
    client.connect = AsyncMock(return_value=True)
    client.close = MagicMock()
    inv = FroniusInverter(mock_hass, {"fronius_modbus_host": "1.2.3.4", "fronius_modbus_port": 502})
    inv._client = client
    inv._model124_base = 40070
    inv._wchamax = 5000
    inv._wchamax_date = date.today().isoformat()
    inv._state_store._store = _NoopStore()
    inv._state_store._data = {}
    inv._state_store._loaded = False
    yield inv, client
    inv._cancel_keepalive()


def _register(client, base=40070):
    """Letzter geschriebener Wert je Offset."""
    werte = {}
    for c in client.write_register.call_args_list:
        werte[c.kwargs["address"] - base] = c.kwargs["value"]
    return werte


async def test_fronius_kann_den_deckel(fronius):
    inv, _ = fronius
    assert inv.supports_discharge_limit is True


async def test_fronius_deckel_allein_ist_ein_eigener_modus(fronius):
    """Ohne Ladelimit: InWRte 100 %, OutWRte = Deckel, StorCtl_Mod = 2 und
    der Keepalive füttert den Watchdog."""
    inv, client = fronius
    assert await inv.async_set_discharge_limit(1.0) is True
    reg = _register(client)
    assert reg[_OFFSET_INWRTE] == 10000
    assert reg[_OFFSET_OUTWRTE] == 2000  # 1 kW von 5 kW = 20 %
    assert reg[_OFFSET_STORCTL_MOD] == 2
    # InWRte vor OutWRte: sonst wäre ein negatives Fenster einer früheren
    # Entladung nach dem OutWRte-Schritt leer, und der Gen24 lehnt ab.
    adressen = [c.kwargs["address"] - 40070 for c in client.write_register.call_args_list]
    assert adressen.index(_OFFSET_INWRTE) < adressen.index(_OFFSET_OUTWRTE)
    assert inv._active_command == {"kind": "entladedeckel"}
    assert inv._keepalive_task is not None


async def test_fronius_halten_ist_outwrte_null(fronius):
    inv, client = fronius
    assert await inv.async_set_discharge_limit(0.0) is True
    assert _register(client)[_OFFSET_OUTWRTE] == 0


async def test_fronius_deckel_neben_ladelimit(fronius):
    """Ein laufendes Ladelimit bleibt, der Deckel kommt dazu: StorCtl_Mod 3."""
    inv, client = fronius
    await inv.async_set_charge_limit(2.0)
    client.write_register.reset_mock()
    assert await inv.async_set_discharge_limit(0.5) is True
    reg = _register(client)
    assert reg[_OFFSET_INWRTE] == 4000
    assert reg[_OFFSET_OUTWRTE] == 1000
    assert reg[_OFFSET_STORCTL_MOD] == 3
    assert inv._active_command["kind"] == "charge_limit"
    # Ein späteres Ladelimit nimmt den Deckel mit.
    client.write_register.reset_mock()
    await inv.async_set_charge_limit(1.0)
    assert _register(client)[_OFFSET_STORCTL_MOD] == 3
    # Aufheben neben dem Ladelimit: zurück auf StorCtl_Mod 1.
    client.write_register.reset_mock()
    assert await inv.async_set_discharge_limit(None) is True
    assert _register(client)[_OFFSET_STORCTL_MOD] == 1


async def test_fronius_aufheben_ohne_anderen_modus_gibt_frei(fronius):
    inv, client = fronius
    await inv.async_set_discharge_limit(0.0)
    client.write_register.reset_mock()
    assert await inv.async_set_discharge_limit(None) is True
    reg = _register(client)
    assert reg[_OFFSET_STORCTL_MOD] == 0
    assert reg[_OFFSET_OUTWRTE] == 10000
    assert inv._active_command is None


async def test_fronius_keepalive_schreibt_den_deckel_neu(fronius):
    inv, client = fronius
    await inv.async_set_discharge_limit(0.0)
    client.write_register.reset_mock()
    assert await inv._write_active_command(inv._active_command) is True
    assert _register(client)[_OFFSET_STORCTL_MOD] == 2


async def test_fronius_stopp_und_entladung_vergessen_den_deckel(fronius):
    inv, client = fronius
    await inv.async_set_discharge_limit(0.0)
    await inv.async_stop_forcible()
    assert inv._entladedeckel_kw is None
    await inv.async_set_discharge_limit(0.0)
    await inv.async_set_discharge(2.0, 30.0)
    assert inv._entladedeckel_kw is None
    # Während der Entladung wird ein Deckel nur gemerkt, nicht geschrieben.
    client.write_register.reset_mock()
    assert await inv.async_set_discharge_limit(1.0) is True
    client.write_register.assert_not_called()


async def test_fronius_schreibfehler_laesst_den_alten_deckel(fronius):
    inv, client = fronius
    await inv.async_set_discharge_limit(1.0)
    client.write_register = AsyncMock(return_value=MagicMock(isError=MagicMock(return_value=True)))
    assert await inv.async_set_discharge_limit(0.0) is False
    assert inv._entladedeckel_kw == 1.0


async def test_fronius_nan_wird_abgelehnt(fronius):
    inv, client = fronius
    assert await inv.async_set_discharge_limit(float("nan")) is False
    client.write_register.assert_not_called()


# ---------------------------------------------------------------------------
# SMA
# ---------------------------------------------------------------------------


@pytest.fixture
def sma(mock_hass):
    client = MagicMock()
    client.connected = True
    client.write_registers = AsyncMock(return_value=_ok())
    client.read_holding_registers = AsyncMock(return_value=_ok([0, 0]))
    client.connect = AsyncMock(return_value=True)
    client.close = MagicMock()
    inv = SMAInverter(mock_hass, {"sma_modbus_host": "1.2.3.5", "sma_modbus_port": 502})
    inv._client = client
    yield inv, client
    inv._cancel_keepalive()


def _letzter_block(client):
    opmod = power = None
    for c in client.write_registers.await_args_list:
        if c.kwargs.get("address") == REG_CMPBMS_OPMOD:
            opmod = registers_to_u32(c.kwargs["values"])
        elif c.kwargs.get("address") == REG_BAT_CHA_MIN_W:
            power = c.kwargs["values"]
    return CmpBmsBlock(
        opmod,
        registers_to_u32(power[0:2]),
        registers_to_u32(power[2:4]),
        registers_to_u32(power[4:6]),
        registers_to_u32(power[6:8]),
        registers_to_s32(power[8:10]),
    )


async def test_sma_deckel_allein(sma):
    inv, client = sma
    assert inv.supports_discharge_limit is True
    assert await inv.async_set_discharge_limit(1.5) is True
    block = _letzter_block(client)
    assert block.dsch_max_w == 1500
    assert block.cha_max_w == DEFAULT_POWER_LIMIT_W
    assert block.grid_w_spt == 0
    assert inv._active == block
    assert inv._keepalive_task is not None


async def test_sma_deckel_neben_ladelimit_und_umgekehrt(sma):
    inv, client = sma
    await inv.async_set_charge_limit(2.0)
    await inv.async_set_discharge_limit(0.0)
    block = _letzter_block(client)
    assert (block.cha_max_w, block.dsch_max_w) == (2000, 0)
    # Ein neues Ladelimit behält den Deckel …
    await inv.async_set_charge_limit(1.0)
    block = _letzter_block(client)
    assert (block.cha_max_w, block.dsch_max_w) == (1000, 0)
    # … ein Ladelimit von genau 10 kW auch (kein Raten über den Wert).
    await inv.async_set_charge_limit(DEFAULT_POWER_LIMIT_W / 1000.0)
    await inv.async_set_discharge_limit(0.5)
    block = _letzter_block(client)
    assert (block.cha_max_w, block.dsch_max_w) == (DEFAULT_POWER_LIMIT_W, 500)
    # Aufheben neben dem Ladelimit: Ladelimit bleibt, Entladung frei.
    await inv.async_set_discharge_limit(None)
    block = _letzter_block(client)
    assert (block.cha_max_w, block.dsch_max_w) == (DEFAULT_POWER_LIMIT_W, DEFAULT_POWER_LIMIT_W)


async def test_sma_aufheben_ohne_anderen_modus_gibt_frei(sma):
    inv, client = sma
    await inv.async_set_discharge_limit(0.0)
    assert await inv.async_set_discharge_limit(None) is True
    block = _letzter_block(client)
    assert block.dsch_max_w == DEFAULT_POWER_LIMIT_W
    assert inv._active is None


async def test_sma_einspeisung_und_stopp_vergessen_den_deckel(sma):
    inv, client = sma
    await inv.async_set_discharge_limit(0.0)
    await inv.async_set_discharge(2.0, 30.0)
    assert inv._entladedeckel_w is None
    # Während der Einspeisung nur merken — der Block gehört ihr.
    client.write_registers.reset_mock()
    assert await inv.async_set_discharge_limit(1.0) is True
    client.write_registers.assert_not_awaited()
    await inv.async_stop_forcible()
    assert inv._entladedeckel_w is None
    assert inv._ladelimit_w is None


async def test_sma_schreibfehler_laesst_den_alten_deckel(sma):
    inv, client = sma
    await inv.async_set_discharge_limit(1.0)
    client.write_registers = AsyncMock(return_value=MagicMock(isError=MagicMock(return_value=True)))
    assert await inv.async_set_discharge_limit(0.0) is False
    assert inv._entladedeckel_w == 1000


# ---------------------------------------------------------------------------
# Huawei
# ---------------------------------------------------------------------------

DISCHARGE_ENTITY = "number.batteries_maximale_entladeleistung"
CHARGE_ENTITY = "number.batteries_maximale_ladeleistung"


def _huawei(mock_hass, entities, device_ids=("dev1",)):
    zustaende = {
        eid: SimpleNamespace(state=str(wert), attributes={"max": wert})
        for eid, wert in entities.items()
    }
    mock_hass.states.get = MagicMock(side_effect=lambda eid: zustaende.get(eid))
    mock_hass.states.async_all = MagicMock(
        side_effect=lambda dom=None: [
            SimpleNamespace(entity_id=eid) for eid in zustaende if eid.startswith(f"{dom}.")
        ]
    )
    inv = HuaweiInverter(mock_hass, {"huawei_device_ids": list(device_ids)})
    return inv


async def test_huawei_deckel_schreibt_die_entladeleistung(mock_hass):
    inv = _huawei(mock_hass, {CHARGE_ENTITY: 5000.0, DISCHARGE_ENTITY: 5000.0})
    assert inv.supports_discharge_limit is True
    assert await inv.async_set_discharge_limit(1.25) is True
    mock_hass.services.async_call.assert_awaited_with(
        "number", "set_value", {"entity_id": DISCHARGE_ENTITY, "value": 1250}, blocking=True
    )


async def test_huawei_aufheben_setzt_das_maximum(mock_hass):
    inv = _huawei(mock_hass, {CHARGE_ENTITY: 5000.0, DISCHARGE_ENTITY: 5000.0})
    assert await inv.async_set_discharge_limit(None) is True
    mock_hass.services.async_call.assert_awaited_with(
        "number", "set_value", {"entity_id": DISCHARGE_ENTITY, "value": 5000}, blocking=True
    )


async def test_huawei_ohne_entitaet_scheitert_laut(mock_hass):
    inv = _huawei(mock_hass, {CHARGE_ENTITY: 5000.0})
    assert await inv.async_set_discharge_limit(0.0) is False
    assert "Entladeleistung" in (inv.last_write_error or "")
    mock_hass.services.async_call.assert_not_awaited()


async def test_huawei_dienstfehler_meldet_false(mock_hass):
    inv = _huawei(mock_hass, {CHARGE_ENTITY: 5000.0, DISCHARGE_ENTITY: 5000.0})
    mock_hass.services.async_call = AsyncMock(side_effect=RuntimeError("weg"))
    assert await inv.async_set_discharge_limit(0.0) is False
    assert "RuntimeError" in (inv.last_write_error or "")


async def test_huawei_zwei_batterien_an_einer_entitaet_bekommen_die_summe(mock_hass):
    """Zwei Batteriegeräte, deren Entladeleistung am selben Wechselrichter
    hängt: einmal die Summe schreiben, nicht zweimal je einen Anteil."""
    inv = _huawei(mock_hass, {CHARGE_ENTITY: 5000.0, DISCHARGE_ENTITY: 5000.0}, ("a", "b"))
    inv._discharge_entity = MagicMock(return_value=DISCHARGE_ENTITY)
    inv._compute_discharge_distribution = MagicMock(return_value={"a": 1.0, "b": 2.0})
    assert await inv.async_set_discharge_limit(3.0) is True
    # … nie über dem Maximum der Entität (die Summe zählt sie doppelt).
    inv._compute_discharge_distribution = MagicMock(return_value={"a": 4.0, "b": 4.0})
    assert await inv.async_set_discharge_limit(8.0) is True
    assert mock_hass.services.async_call.await_args.args[2]["value"] == 5000
    mock_hass.services.async_call.reset_mock()
    inv._compute_discharge_distribution = MagicMock(return_value={"a": 1.0, "b": 2.0})
    assert await inv.async_set_discharge_limit(3.0) is True
    aufrufe = [
        c for c in mock_hass.services.async_call.await_args_list
        if c.args[2]["entity_id"] == DISCHARGE_ENTITY
    ]
    assert len(aufrufe) == 1
    assert aufrufe[0].args[2]["value"] == 3000


async def test_huawei_zwei_wechselrichter_je_ein_anteil(mock_hass):
    inv = _huawei(mock_hass, {
        CHARGE_ENTITY: 5000.0,
        "number.a_maximum_discharging_power": 5000.0,
        "number.b_maximum_discharging_power": 5000.0,
    }, ("a", "b"))
    inv._discharge_entity = MagicMock(side_effect=lambda did: f"number.{did}_maximum_discharging_power")
    inv._compute_discharge_distribution = MagicMock(return_value={"a": 1.0, "b": 2.0})
    assert await inv.async_set_discharge_limit(3.0) is True
    werte = {
        c.args[2]["entity_id"]: c.args[2]["value"]
        for c in mock_hass.services.async_call.await_args_list
    }
    assert werte == {
        "number.a_maximum_discharging_power": 1000,
        "number.b_maximum_discharging_power": 2000,
    }


async def test_huawei_unerreichbares_geraet_ist_ein_fehlschlag(mock_hass):
    """Gerät b ist gerade weg: a wird geschrieben, aber der Aufruf meldet
    False — Huawei behält den Wert, b stünde nach seiner Rückkehr still mit
    dem alten Deckel da. Der Executor versucht es dann erneut."""
    inv = _huawei(mock_hass, {
        CHARGE_ENTITY: 5000.0,
        "number.a_maximum_discharging_power": 5000.0,
    }, ("a", "b"))
    inv._discharge_entity = MagicMock(side_effect=lambda did: f"number.{did}_maximum_discharging_power")
    assert await inv.async_set_discharge_limit(None) is False
    werte = [c.args[2] for c in mock_hass.services.async_call.await_args_list]
    assert werte == [{"entity_id": "number.a_maximum_discharging_power", "value": 5000}]
    assert "nicht verfügbar" in inv.last_write_error
    mock_hass.services.async_call.reset_mock()
    inv._compute_discharge_distribution = MagicMock(return_value={"a": 1.0, "b": 2.0})
    assert await inv.async_set_discharge_limit(3.0) is False


# ---------------------------------------------------------------------------
# SolaX
# ---------------------------------------------------------------------------

from custom_components.eeg_energy_optimizer.inverter.solax import SolaXInverter  # noqa: E402

SOLAX_DSCH = "number.solax_inverter_battery_discharge_max_current"


class _SolaxStore:
    def __init__(self):
        self.daten = None

    async def async_load(self):
        return self.daten

    async def async_save(self, daten):
        self.daten = dict(daten)


def _solax(mock_hass, strom=25.0, maximum=30.0, spannung="400"):
    zustaende = {
        SOLAX_DSCH: SimpleNamespace(state=str(strom), attributes={"max": maximum}),
        "sensor.solax_inverter_battery_voltage_charge": SimpleNamespace(state=spannung, attributes={}),
    }
    mock_hass.states.get = MagicMock(side_effect=lambda e: zustaende.get(e))
    mock_hass.states.async_all = MagicMock(return_value=[])
    inv = SolaXInverter(mock_hass, {})
    inv._state_store._store = _SolaxStore()
    return inv, zustaende


def _gesetzt(mock_hass):
    return [c.args[2]["value"] for c in mock_hass.services.async_call.await_args_list
            if c.args[2].get("entity_id") == SOLAX_DSCH]


async def test_solax_deckel_als_strom_und_originalwert_zurueck(mock_hass):
    inv, zustaende = _solax(mock_hass)
    assert inv.supports_discharge_limit is True
    assert await inv.async_set_discharge_limit(2.0) is True      # 2 kW / 400 V = 5 A
    assert await inv.async_set_discharge_limit(0.0) is True
    zustaende[SOLAX_DSCH] = SimpleNamespace(state="0", attributes={"max": 30.0})
    assert await inv.async_set_discharge_limit(None) is True     # zurück auf die 25 A von vorher
    assert _gesetzt(mock_hass) == [5.0, 0.0, 25.0]
    assert inv._state_store.original_discharge_current is None


async def test_solax_neustart_im_halten_sichert_die_null_nicht(mock_hass):
    """Steht beim ersten Deckel schon 0 A (unser eigener, vor dem Neustart),
    wird nicht 0 als Original gemerkt — aufgehoben wird aufs Maximum."""
    inv, _ = _solax(mock_hass, strom=0.0)
    await inv.async_set_discharge_limit(0.0)
    await inv.async_set_discharge_limit(None)
    assert _gesetzt(mock_hass) == [0.0, 30.0]


async def test_solax_deckel_nie_ueber_dem_maximum_und_ohne_entitaet_fehler(mock_hass):
    inv, _ = _solax(mock_hass)
    await inv.async_set_discharge_limit(50.0)
    assert _gesetzt(mock_hass) == [30.0]
    leer, zustaende = _solax(mock_hass)
    zustaende.pop(SOLAX_DSCH)
    assert await leer.async_set_discharge_limit(1.0) is False
    assert "nicht verfügbar" in leer.last_write_error
