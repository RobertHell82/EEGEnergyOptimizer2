"""Entladedeckel der Spitzenkappung in den Treibern, die ihn können.

Fronius (OutWRte mit StorCtl_Mod Bit 1), SMA (BatDschMaxW im CmpBMS-Block),
Huawei (Number-Entität „Maximale Entladeleistung"), SolaX und Kostal
(Register 1040, Beta). Jeder Treiber muss
dreierlei richtig machen: den Deckel neben einem Ladelimit halten, ihn ohne
anderen Modus als eigenen Zustand führen (mit Watchdog-Keepalive bei den
Modbus-Treibern) und ihn bei Freigabe und Zwangsentladung loswerden.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.eeg_energy_optimizer.inverter.base import InverterBase
from custom_components.eeg_energy_optimizer.inverter.fronius import (
    FroniusInverter,
    _cache_stunde,
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
    inv._wchamax_date = _cache_stunde()
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


# ---------------------------------------------------------------------------
# Sigenergy
# ---------------------------------------------------------------------------

from custom_components.eeg_energy_optimizer.inverter.sigenergy import (  # noqa: E402
    MODE_SELF_CONSUMPTION as SIGEN_SC,
    SIGEN_ENTITY_DEFAULTS,
    SigenergyInverter,
)

SIGEN_SWITCH = SIGEN_ENTITY_DEFAULTS["remote_ems_switch"]
SIGEN_MODE = SIGEN_ENTITY_DEFAULTS["remote_ems_mode"]
SIGEN_LADEN = SIGEN_ENTITY_DEFAULTS["ess_max_charging_limit"]
SIGEN_ENTLADEN = SIGEN_ENTITY_DEFAULTS["ess_max_discharging_limit"]
SIGEN_NENN_LADEN = SIGEN_ENTITY_DEFAULTS["ess_rated_charging_power"]
SIGEN_NENN_ENTLADEN = SIGEN_ENTITY_DEFAULTS["ess_rated_discharging_power"]


def _sigen(mock_hass, **zustand):
    werte = {
        SIGEN_SWITCH: "on", SIGEN_MODE: SIGEN_SC,
        SIGEN_NENN_LADEN: "20.0", SIGEN_NENN_ENTLADEN: "21.6", **zustand,
    }
    kw = {"unit_of_measurement": "kW"}
    mock_hass.states.get = MagicMock(side_effect=lambda e: (
        SimpleNamespace(state=werte[e], attributes=kw if "rated" in e else {})
        if e in werte else None
    ))
    mock_hass.states.async_all = MagicMock(return_value=[])
    return SigenergyInverter(mock_hass, {}), werte


def _sigen_calls(mock_hass):
    return [(c.args[1], c.args[2]) for c in mock_hass.services.async_call.await_args_list]


async def test_sigen_deckel_null_wird_ein_hauch(mock_hass):
    """0 lesen Sigenergy-Geräte laut Maintainer oft als „nicht gesetzt" —
    0,01 kW ist praktisch Halten. Der Modus steht schon, also kein Schreiben
    des Modus (jedes wartet bis zu 60 s)."""
    inv, _ = _sigen(mock_hass)
    assert inv.supports_discharge_limit is True
    assert await inv.async_set_discharge_limit(0.0) is True
    assert _sigen_calls(mock_hass) == [("set_value", {"entity_id": SIGEN_ENTLADEN, "value": 0.01})]


async def test_sigen_deckel_aus_der_freigabe_schaltet_remote_ems_ein(mock_hass):
    """Aus der Freigabe (Remote EMS aus): einschalten, erst den Deckel, dann
    den Eigenverbrauch — falls das Gerät noch in einem Befehl stand."""
    inv, werte = _sigen(mock_hass, **{SIGEN_SWITCH: "off", SIGEN_MODE: "unavailable"})

    async def _aufruf(domain, service, data, **kw):
        if service == "turn_on":
            werte[SIGEN_SWITCH] = "on"
            werte[SIGEN_MODE] = "Command Discharging (ESS First)"

    mock_hass.services.async_call = AsyncMock(side_effect=_aufruf)
    assert await inv.async_set_discharge_limit(1.0) is True
    assert _sigen_calls(mock_hass) == [
        ("turn_on", {"entity_id": SIGEN_SWITCH}),
        ("set_value", {"entity_id": SIGEN_ENTLADEN, "value": 1.0}),
        ("select_option", {"entity_id": SIGEN_MODE, "option": SIGEN_SC}),
    ]


async def test_sigen_aufheben_ohne_deckel_tut_nichts(mock_hass):
    inv, _ = _sigen(mock_hass)
    assert await inv.async_set_discharge_limit(None) is True
    mock_hass.services.async_call.assert_not_awaited()


async def test_sigen_szenario_b_deckel_nach_absturz_remote_ems_von_hand_aus(mock_hass):
    """HA stürzt mit Deckel 0,01 kW ab, der Nutzer schaltet Remote EMS von
    Hand aus. 40034 wirkt global weiter — ohne Gedächtnis hob ``None``
    früher nichts auf, die Batterie entlud nie mehr. Jetzt entscheidet der
    gelesene Wert; Remote EMS bleibt aus."""
    inv, _ = _sigen(mock_hass, **{
        SIGEN_SWITCH: "off", SIGEN_MODE: "unavailable", SIGEN_LADEN: "20.0", SIGEN_ENTLADEN: "0.01",
    })
    assert await inv.async_set_discharge_limit(None) is True
    assert _sigen_calls(mock_hass) == [("set_value", {"entity_id": SIGEN_ENTLADEN, "value": 21.6})]
    # Dasselbe über die Freigabe des Executors.
    mock_hass.services.async_call.reset_mock()
    assert await inv.async_stop_forcible() is True
    assert _sigen_calls(mock_hass) == [("set_value", {"entity_id": SIGEN_ENTLADEN, "value": 21.6})]


async def test_sigen_deckel_ohne_gedaechtnis_bei_remote_ems_an_gibt_frei(mock_hass):
    """Nach einem Neustart steht Remote EMS noch an, 40034 tief: das ist
    unser Rest von vorher — ganze Freigabe."""
    inv, _ = _sigen(mock_hass, **{SIGEN_LADEN: "20.0", SIGEN_ENTLADEN: "0.01"})
    assert await inv.async_set_discharge_limit(None) is True
    assert ("turn_off", {"entity_id": SIGEN_SWITCH}) in _sigen_calls(mock_hass)
    assert ("set_value", {"entity_id": SIGEN_ENTLADEN, "value": 21.6}) in _sigen_calls(mock_hass)


async def test_sigen_aufheben_ohne_gedaechtnis_limit_auf_nennwert_nichts(mock_hass):
    inv, _ = _sigen(mock_hass, **{SIGEN_ENTLADEN: "21.6"})
    assert await inv.async_set_discharge_limit(None) is True
    mock_hass.services.async_call.assert_not_awaited()


async def test_sigen_aufheben_ohne_anderen_modus_gibt_frei(mock_hass):
    """Der Deckel war das Einzige, wofür Remote EMS an war: ganze Freigabe."""
    inv, _ = _sigen(mock_hass)
    await inv.async_set_discharge_limit(0.0)
    mock_hass.services.async_call.reset_mock()
    assert await inv.async_set_discharge_limit(None) is True
    assert _sigen_calls(mock_hass) == [
        ("select_option", {"entity_id": SIGEN_MODE, "option": SIGEN_SC}),
        ("set_value", {"entity_id": SIGEN_LADEN, "value": 20.0}),
        ("set_value", {"entity_id": SIGEN_ENTLADEN, "value": 21.6}),
        ("turn_off", {"entity_id": SIGEN_SWITCH}),
    ]
    assert inv._entladedeckel_kw is None


async def test_sigen_deckel_neben_ladelimit(mock_hass):
    """Ein laufendes Ladelimit (40032) bleibt unberührt, ein späteres
    Ladelimit nimmt den Deckel mit, Aufheben setzt nur 40034 zurück."""
    inv, _ = _sigen(mock_hass)
    await inv.async_set_charge_limit(2.0)
    mock_hass.services.async_call.reset_mock()
    assert await inv.async_set_discharge_limit(0.5) is True
    assert _sigen_calls(mock_hass) == [("set_value", {"entity_id": SIGEN_ENTLADEN, "value": 0.5})]
    mock_hass.services.async_call.reset_mock()
    await inv.async_set_charge_limit(1.0)
    assert _sigen_calls(mock_hass)[-1] == ("set_value", {"entity_id": SIGEN_ENTLADEN, "value": 0.5})
    mock_hass.services.async_call.reset_mock()
    assert await inv.async_set_discharge_limit(None) is True
    assert _sigen_calls(mock_hass) == [("set_value", {"entity_id": SIGEN_ENTLADEN, "value": 21.6})]


async def test_sigen_stopp_und_entladung_vergessen_den_deckel(mock_hass):
    inv, _ = _sigen(mock_hass)
    await inv.async_set_discharge_limit(0.0)
    assert await inv.async_stop_forcible() is True
    assert inv._entladedeckel_kw is None
    mock_hass.services.async_call.reset_mock()
    assert await inv.async_set_discharge_limit(None) is True
    mock_hass.services.async_call.assert_not_awaited()
    # Während einer Entladung gehört 40034 ihr: nur merken, nicht schreiben.
    await inv.async_set_discharge_limit(0.0)
    await inv.async_set_discharge(2.0, 30.0)
    assert inv._entladedeckel_kw is None
    mock_hass.services.async_call.reset_mock()
    assert await inv.async_set_discharge_limit(1.0) is True
    mock_hass.services.async_call.assert_not_awaited()


async def test_sigen_dienstfehler_meldet_false_und_bleibt_aufhebbar(mock_hass):
    inv, _ = _sigen(mock_hass)
    mock_hass.services.async_call = AsyncMock(side_effect=RuntimeError("modbus"))
    assert await inv.async_set_discharge_limit(0.0) is False
    assert inv.last_write_error == "RuntimeError"
    # Halb geschrieben ist unbekannt: Das Aufheben findet noch etwas zu tun.
    mock_hass.services.async_call = AsyncMock(return_value=None)
    assert await inv.async_set_discharge_limit(None) is True
    assert ("turn_off", {"entity_id": SIGEN_SWITCH}) in _sigen_calls(mock_hass)


async def test_sigen_nan_wird_abgelehnt(mock_hass):
    inv, _ = _sigen(mock_hass)
    assert await inv.async_set_discharge_limit(float("nan")) is False
    mock_hass.services.async_call.assert_not_awaited()


# ---------------------------------------------------------------------------
# Kostal (Register 1040, Beta) — die ausführlichen Fälle stehen in
# test_kostal_inverter.py; hier die drei Pflichten jedes Deckel-Treibers.
# ---------------------------------------------------------------------------


@pytest.fixture
def kostal(mock_hass):
    from custom_components.eeg_energy_optimizer.inverter.kostal import (
        REG_MAX_CHARGE_POWER,
        REG_MAX_DISCHARGE_POWER,
        KostalInverter,
        float_to_registers,
    )

    vorwerte = {REG_MAX_CHARGE_POWER: 6000.0, REG_MAX_DISCHARGE_POWER: 5000.0}

    async def _read(address, count, **kwargs):
        return _ok(float_to_registers(vorwerte.get(address, 0.0)))

    client = MagicMock()
    client.connected = True
    client.write_registers = AsyncMock(return_value=_ok())
    client.read_holding_registers = AsyncMock(side_effect=_read)
    client.connect = AsyncMock(return_value=True)
    client.close = MagicMock()
    inv = KostalInverter(mock_hass, {"kostal_modbus_host": "1.2.3.4"})
    inv._client = client
    yield inv, client
    inv._cancel_keepalive()


def _kostal_werte(client, address):
    from custom_components.eeg_energy_optimizer.inverter.kostal import (
        registers_to_float,
    )
    return [
        round(registers_to_float(c.kwargs["values"]))
        for c in client.write_registers.await_args_list
        if c.kwargs.get("address") == address
    ]


async def test_kostal_deckel_neben_ladelimit(kostal):
    from custom_components.eeg_energy_optimizer.inverter.kostal import (
        REG_MAX_CHARGE_POWER,
        REG_MAX_DISCHARGE_POWER,
    )
    inv, client = kostal
    assert inv.supports_discharge_limit is True
    assert await inv.async_set_charge_limit(2.0) is True
    assert await inv.async_set_discharge_limit(0.0) is True
    # Getrennte Register: Das Ladelimit bleibt, der Deckel kommt dazu.
    assert inv._active == ("charge_limit", 2000.0)
    assert inv._deckel_w == 0.0
    assert _kostal_werte(client, REG_MAX_CHARGE_POWER) == [2000]
    assert _kostal_werte(client, REG_MAX_DISCHARGE_POWER) == [0]


async def test_kostal_freigabe_hebt_deckel_auf(kostal):
    from custom_components.eeg_energy_optimizer.inverter.kostal import (
        REG_MAX_DISCHARGE_POWER,
    )
    inv, client = kostal
    await inv.async_set_discharge_limit(1.5)
    assert await inv.async_stop_forcible() is True
    assert _kostal_werte(client, REG_MAX_DISCHARGE_POWER) == [1500, 5000]
    assert inv._deckel_w is None


async def test_kostal_zwangsentladung_hebt_deckel_auf(kostal):
    from custom_components.eeg_energy_optimizer.inverter.kostal import (
        REG_BATTERY_SETPOINT,
        REG_MAX_DISCHARGE_POWER,
    )
    inv, client = kostal
    await inv.async_set_discharge_limit(0.0)
    client.write_registers.reset_mock()
    assert await inv.async_set_discharge(3.0) is True
    adressen = [c.kwargs["address"] for c in client.write_registers.await_args_list]
    assert adressen == [REG_MAX_DISCHARGE_POWER, REG_BATTERY_SETPOINT]
    assert _kostal_werte(client, REG_MAX_DISCHARGE_POWER) == [5000]


async def test_kostal_aufheben_ohne_deckel_ist_noop(kostal):
    inv, client = kostal
    assert await inv.async_set_discharge_limit(None) is True
    assert client.write_registers.await_count == 0


async def test_kostal_stehende_batterie_ist_keine_luft(kostal):
    """Nach 1034 = 0 steht die Batterie bis zum Watchdog-Rückfall; ein
    Deckel wird nur vorgemerkt und meldet trotzdem True. Der Executor liest
    ``getattr(inverter, "batterie_gesperrt", False)`` und rechnet dann keine
    Luft — sonst schaltete die Verbraucher-Stufe nichts ab."""
    inv, client = kostal
    assert getattr(inv, "batterie_gesperrt", False) is False
    await inv.async_set_discharge(3.0)
    await inv.async_stop_forcible()
    client.write_registers.reset_mock()
    assert await inv.async_set_discharge_limit(0.0) is True
    assert client.write_registers.await_count == 0
    assert getattr(inv, "batterie_gesperrt", False) is True


# ---------------------------------------------------------------------------
# Victron — „Max. Wechselrichterleistung" (Settings/CGwacs/MaxDischargePower)
# ---------------------------------------------------------------------------
# Der Deckel gilt für den ganzen DC→AC-Wandler, DC-gekoppelte PV läuft mit
# durch. Geschrieben wird deshalb (Deckel + DC-PV) × 1,05 + 50 W, auf 50 W
# aufgerundet, und ein Takt führt es an der DC-PV nach.

from .test_victron_inverter import (  # noqa: E402
    DC_PV,
    LADESTROM,
    SOLLWERT,
    WR_GRENZE,
    Anlage,
    _Store,
    _schreibvorgaenge,
    _treiber,
    anlage,  # noqa: F401 — Fixture
    registry_patch,  # noqa: F401 — Fixture
    soc_beobachter,  # noqa: F401 — Fixture
)


def _deckel_schreibvorgaenge(hass):
    return [w for e, w in _schreibvorgaenge(hass) if e == WR_GRENZE]


async def test_victron_halten_schreibt_nur_dc_pv_und_verluste(mock_hass, anlage, soc_beobachter):
    anlage.setze(DC_PV, "2000")
    inv = _treiber(mock_hass)
    assert inv.supports_discharge_limit is True
    assert await inv.async_set_discharge_limit(0.0) is True
    # 2000 × 1,05 + 50 = 2150 W
    assert _deckel_schreibvorgaenge(mock_hass) == [2150.0]
    assert inv._state_store.vorwert("entladedeckel") == -1.0
    assert len(soc_beobachter.takte) == 1


async def test_victron_nachts_halten(mock_hass, anlage, soc_beobachter):
    """Von −1 auf 50 W ist ein Schreiben, kein Totband — auch wenn die
    Zahlen nur 51 auseinander liegen."""
    inv = _treiber(mock_hass)
    assert await inv.async_set_discharge_limit(0.0) is True
    assert anlage.wert(WR_GRENZE) == 50.0


async def test_victron_deckel_plus_pv(mock_hass, anlage, soc_beobachter):
    anlage.setze(DC_PV, "1000")
    inv = _treiber(mock_hass)
    assert await inv.async_set_discharge_limit(1.5) is True
    # (1500 + 1000) × 1,05 + 50 = 2675 → 2700 W
    assert anlage.wert(WR_GRENZE) == 2700.0


async def test_victron_vorwert_des_betreibers_bleibt_obergrenze(mock_hass, anlage, soc_beobachter):
    anlage.setze(WR_GRENZE, "3000")
    anlage.setze(DC_PV, "4000")
    inv = _treiber(mock_hass)
    assert await inv.async_set_discharge_limit(1.0) is True
    assert anlage.wert(WR_GRENZE) == 3000.0
    assert await inv.async_set_discharge_limit(None) is True
    assert anlage.wert(WR_GRENZE) == 3000.0


async def test_victron_aufheben_stellt_minus_eins_wieder_her(mock_hass, anlage, soc_beobachter):
    inv = _treiber(mock_hass)
    await inv.async_set_discharge_limit(0.0)
    assert await inv.async_set_discharge_limit(None) is True
    assert anlage.wert(WR_GRENZE) == -1.0
    assert inv._state_store.vorwert("entladedeckel") is None
    soc_beobachter.takt_abmelder.assert_called_once()
    assert inv._entladedeckel_kw is None


async def test_victron_aufheben_ohne_eingriff_schreibt_nichts(mock_hass, anlage):
    inv = _treiber(mock_hass)
    assert await inv.async_set_discharge_limit(None) is True
    mock_hass.services.async_call.assert_not_called()


async def test_victron_neustart_im_halten_sichert_die_eigene_null_nicht(
    mock_hass, anlage, soc_beobachter
):
    """Der Vorwert steht im Store; nach dem Neustart steht im Gerät unser
    Halten (50 W) — es darf nicht zum Vorwert werden."""
    store = _Store()
    inv = _treiber(mock_hass, store=store)
    await inv.async_set_discharge_limit(0.0)
    assert anlage.wert(WR_GRENZE) == 50.0

    neu = _treiber(mock_hass, store=store)
    assert await neu.async_set_discharge_limit(0.0) is True
    assert neu._state_store.vorwert("entladedeckel") == -1.0
    assert await neu.async_set_discharge_limit(None) is True
    assert anlage.wert(WR_GRENZE) == -1.0
    assert store.data == {}

    # Und ohne Store: Nach dem Aufheben ist er leer, das Echo von −1 hängt,
    # die Entität zeigt noch unser 50-W-Halten. Der nächste Deckel darf es
    # nicht sichern — der Schutz kommt dann aus der Sitzung allein.
    assert await neu.async_set_discharge_limit(0.0) is True
    anlage.echo_verzoegert = True
    assert await neu.async_set_discharge_limit(None) is True
    assert anlage.wert(WR_GRENZE) == 50.0  # Echo steht aus
    assert store.data == {}
    assert await neu.async_set_discharge_limit(0.0) is True
    assert neu._state_store.vorwert("entladedeckel") == -1.0
    anlage.echo()
    assert await neu.async_set_discharge_limit(None) is True
    assert anlage.wert(WR_GRENZE) == -1.0


async def test_victron_freigabe_und_neuer_deckel_im_selben_lauf(mock_hass, anlage, soc_beobachter):
    """Befund aus dem Review: Executor wechselt charge_limit → release, die
    Freigabe hebt den Deckel auf (−1), im selben Lauf kommt ein neuer Deckel —
    das MQTT-Echo zeigt noch den alten (2150 W). Als „Betreiberwert"
    gesichert, schriebe danach jede Freigabe 2150 statt −1."""
    anlage.setze(DC_PV, "2000")
    inv = _treiber(mock_hass)
    await inv.async_set_charge_limit(0.0)
    assert await inv.async_set_discharge_limit(0.0) is True
    assert anlage.wert(WR_GRENZE) == 2150.0
    anlage.echo_verzoegert = True
    assert await inv.async_stop_forcible() is True
    assert await inv.async_set_discharge_limit(0.5) is True
    assert inv._state_store.vorwert("entladedeckel") == -1.0
    anlage.echo()
    assert await inv.async_stop_forcible() is True
    assert anlage.wert(WR_GRENZE) == -1.0
    assert anlage.wert(LADESTROM) == 125.0


async def test_victron_ohne_entitaet_kein_deckel(mock_hass, registry_patch):
    a = Anlage(mock_hass, **{WR_GRENZE: None})
    registry_patch.return_value = a.registry
    inv = _treiber(mock_hass)
    assert inv.supports_discharge_limit is False
    assert await inv.async_set_discharge_limit(0.0) is False
    assert inv.last_write_error == "Entität entladedeckel nicht gefunden"


async def test_victron_deaktivierte_entitaet_kann_keinen_deckel(mock_hass, anlage):
    anlage.registry.entities[WR_GRENZE].disabled_by = "integration"
    assert _treiber(mock_hass).supports_discharge_limit is False


async def test_victron_ersatzbereich_null_bis_hundert_deckelt_nicht(mock_hass, anlage):
    """Ohne Bereich aus MQTT setzt HA 0–100: −1 käme nie zurück, und jeder
    Deckel über 100 W wäre abgeschnitten."""
    anlage.werte[WR_GRENZE][1] = {"min": 0, "max": 100}
    inv = _treiber(mock_hass)
    assert await inv.async_set_discharge_limit(0.0) is False
    assert inv.last_write_error == "Entladedeckel −1 nicht schreibbar"
    mock_hass.services.async_call.assert_not_called()
    assert inv._state_store.vorwert("entladedeckel") is None


async def test_victron_minus_eins_unschreibbar_beim_aufheben_scheitert_laut(
    mock_hass, anlage, soc_beobachter
):
    """Bereich schrumpft nach dem Eingriff: nicht das Maximum (100 W, „Wandler
    fast aus") schreiben wie beim Ladestrom, sondern scheitern und den
    Vorwert für den nächsten Versuch behalten."""
    inv = _treiber(mock_hass)
    await inv.async_set_discharge_limit(0.0)
    anlage.werte[WR_GRENZE][1] = {"min": 0, "max": 100}
    mock_hass.services.async_call.reset_mock()
    assert await inv.async_set_discharge_limit(None) is False
    assert inv.last_write_error == "Entladedeckel −1 nicht schreibbar"
    mock_hass.services.async_call.assert_not_called()
    assert inv._state_store.vorwert("entladedeckel") == -1.0


async def test_victron_dc_pv_unbekannt_zaehlt_null(mock_hass, anlage, soc_beobachter, caplog):
    """Nachts schlafen die Laderegler womöglich ohne Wert — die Batterie-Stufe
    darf dann nicht ausfallen. Mit 0 W PV ist der Deckel knapper, greift aber."""
    anlage.setze(DC_PV, "unavailable")
    inv = _treiber(mock_hass)
    with caplog.at_level("DEBUG"):
        assert await inv.async_set_discharge_limit(1.0) is True
    assert _deckel_schreibvorgaenge(mock_hass) == [1100.0]  # 1000 × 1,05 + 50
    assert "DC-PV-Leistung unbekannt" in caplog.text
    anlage.setze(DC_PV, "unknown")
    assert await inv.async_set_discharge_limit(0.0) is True
    assert anlage.wert(WR_GRENZE) == 50.0


async def test_victron_ohne_laderegler_zaehlt_keine_pv(mock_hass, registry_patch, soc_beobachter):
    a = Anlage(mock_hass, **{DC_PV: None})
    registry_patch.return_value = a.registry
    inv = _treiber(mock_hass)
    assert await inv.async_set_discharge_limit(1.0) is True
    assert a.wert(WR_GRENZE) == 1100.0  # 1000 × 1,05 + 50


async def test_victron_stopp_hebt_den_deckel_auf(mock_hass, anlage, soc_beobachter):
    inv = _treiber(mock_hass)
    await inv.async_set_discharge_limit(0.0)
    assert await inv.async_stop_forcible() is True
    assert anlage.wert(WR_GRENZE) == -1.0
    soc_beobachter.takt_abmelder.assert_called_once()
    assert inv._entladedeckel_kw is None


async def test_victron_entladung_hebt_den_deckel_selbst_auf(mock_hass, anlage, soc_beobachter):
    inv = _treiber(mock_hass)
    await inv.async_set_discharge_limit(0.0)
    assert await inv.async_set_discharge(2.0, 30) is True
    assert anlage.wert(WR_GRENZE) == -1.0
    assert anlage.wert(SOLLWERT) == -2000.0
    # Während der Entladung wird kein Deckel geschrieben — und das ist kein
    # stilles True: Der Executor hielte „Deckel steht" sonst im Totband fest.
    mock_hass.services.async_call.reset_mock()
    assert await inv.async_set_discharge_limit(0.0) is False
    assert inv.last_write_error == "Entladung läuft — kein Entladedeckel"
    mock_hass.services.async_call.assert_not_called()


async def test_victron_entladung_trotz_klemmendem_deckel(mock_hass, anlage, soc_beobachter):
    """Wie im Executor: Scheitert das Aufheben, wird trotzdem entladen."""
    inv = _treiber(mock_hass)
    await inv.async_set_discharge_limit(0.0)
    anlage.werte[WR_GRENZE][1] = {"min": 0, "max": 100}
    assert await inv.async_set_discharge(2.0, 30) is True
    assert anlage.wert(SOLLWERT) == -2000.0
    assert anlage.wert(WR_GRENZE) == 50.0
    assert inv.last_write_error.startswith("Entladedeckel nicht aufgehoben")
    assert inv._state_store.vorwert("entladedeckel") == -1.0


async def test_victron_freigabe_scheitert_nicht_am_deckel(mock_hass, anlage, soc_beobachter, caplog):
    """Bereich ohne −1 nach einem Update: Sollwert und Ladestrom sind zurück,
    die Freigabe gilt — der Deckel-Vorwert bleibt für einen späteren Versuch."""
    inv = _treiber(mock_hass)
    await inv.async_set_charge_limit(0.0)
    await inv.async_set_discharge_limit(0.0)
    anlage.werte[WR_GRENZE][1] = {"min": 0, "max": 100}
    assert inv.supports_discharge_limit is False
    with caplog.at_level("WARNING"):
        assert await inv.async_stop_forcible() is True
    assert anlage.wert(LADESTROM) == 125.0
    assert inv.last_write_error == "Entladedeckel nicht aufgehoben: Entladedeckel −1 nicht schreibbar"
    assert inv._state_store.vorwert("entladedeckel") == -1.0
    assert "Entladedeckel" in caplog.text
    # Bereich wieder da: Die nächste Freigabe holt es nach.
    anlage.werte[WR_GRENZE][1] = {"min": -1, "max": 1000000}
    assert await inv.async_stop_forcible() is True
    assert inv.last_write_error is None
    assert anlage.wert(WR_GRENZE) == -1.0


async def test_victron_freigabe_ohne_deckel_entitaet(mock_hass, anlage, soc_beobachter):
    """Entität zur Laufzeit verschwunden: die Freigabe gilt trotzdem."""
    inv = _treiber(mock_hass)
    await inv.async_set_discharge_limit(0.0)
    await inv.async_set_charge_limit(0.0)
    del anlage.registry.entities[WR_GRENZE]
    assert await inv.async_stop_forcible() is True
    assert anlage.wert(LADESTROM) == 125.0
    assert inv.last_write_error == "Entladedeckel nicht aufgehoben: Entität entladedeckel nicht gefunden"
    assert inv._state_store.vorwert("entladedeckel") == -1.0


async def test_victron_deckel_neben_ladelimit(mock_hass, anlage, soc_beobachter):
    """Ladelimit (DVCC) und Deckel sind verschiedene Stellgrößen."""
    inv = _treiber(mock_hass)
    await inv.async_set_charge_limit(0.0)
    await inv.async_set_discharge_limit(0.0)
    await inv.async_set_charge_limit(1.0)
    assert anlage.wert(WR_GRENZE) == 50.0
    assert inv._entladedeckel_kw == 0.0


async def test_victron_nachfuehrung_folgt_der_dc_pv(mock_hass, anlage, soc_beobachter):
    anlage.setze(DC_PV, "1000")
    inv = _treiber(mock_hass)
    await inv.async_set_discharge_limit(0.0)
    assert anlage.wert(WR_GRENZE) == 1100.0
    [(takt, intervall)] = soc_beobachter.takte
    assert intervall.total_seconds() == 30
    anlage.setze(DC_PV, "1150")  # 1257,5 → 1300 W: genau 200 W daneben, schreibt
    await takt(None)
    assert _deckel_schreibvorgaenge(mock_hass) == [1100.0, 1300.0]
    anlage.setze(DC_PV, "1250")  # → 1400 W, nur 100 W daneben
    await takt(None)
    assert _deckel_schreibvorgaenge(mock_hass) == [1100.0, 1300.0]
    anlage.setze(DC_PV, "3000")  # → 3200 W
    await takt(None)
    assert anlage.wert(WR_GRENZE) == 3200.0
    # Ohne Messung halten.
    anlage.setze(DC_PV, "unavailable")
    await takt(None)
    assert anlage.wert(WR_GRENZE) == 3200.0


class _Uhr:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


async def test_victron_fallende_pv_senkt_erst_nach_vier_takten(mock_hass, anlage, soc_beobachter):
    """Asymmetrisch: Steigen sofort (sonst Netzbezug), Fallen erst, wenn der
    Überhang 4 Takte steht — ein zu hoher Deckel kostet kurz nur etwas
    Reserve, jedes Schreiben dagegen den Einstellungsspeicher."""
    uhr = _Uhr()
    anlage.setze(DC_PV, "3000")
    inv = _treiber(mock_hass)
    inv._uhr = uhr
    await inv.async_set_discharge_limit(0.0)
    assert anlage.wert(WR_GRENZE) == 3200.0
    [(takt, _)] = soc_beobachter.takte
    anlage.setze(DC_PV, "1000")  # → 1100 W
    for _ in range(3):
        uhr.t += 30
        await takt(None)
    assert _deckel_schreibvorgaenge(mock_hass) == [3200.0]
    # Eine Wolke geht, die PV ist wieder da: der Zähler beginnt neu.
    anlage.setze(DC_PV, "3000")
    uhr.t += 30
    await takt(None)
    anlage.setze(DC_PV, "1000")
    for _ in range(3):
        uhr.t += 30
        await takt(None)
    assert _deckel_schreibvorgaenge(mock_hass) == [3200.0]
    uhr.t += 30
    await takt(None)
    assert _deckel_schreibvorgaenge(mock_hass) == [3200.0, 1100.0]
    # Steigen dagegen sofort.
    anlage.setze(DC_PV, "2000")
    uhr.t += 30
    await takt(None)
    assert _deckel_schreibvorgaenge(mock_hass) == [3200.0, 1100.0, 2150.0]


async def test_victron_schreibvorrat_begrenzt_die_nachfuehrung(mock_hass, anlage, soc_beobachter):
    """Steigt die PV jeden Takt um 300 W, schriebe die Nachführung zweimal je
    Minute. Der Vorrat (5, einer je Minute nach) hält das im Mittel bei
    höchstens einem je Minute."""
    uhr = _Uhr()
    inv = _treiber(mock_hass)
    inv._uhr = uhr
    await inv.async_set_discharge_limit(0.0)  # Befehl: nimmt einen
    [(takt, _)] = soc_beobachter.takte
    pv = 0
    for _ in range(40):  # 20 Minuten
        pv += 300
        anlage.setze(DC_PV, str(pv))
        uhr.t += 30
        await takt(None)
    schreiben = len(_deckel_schreibvorgaenge(mock_hass))
    # 5 auf Vorrat + 20 nachgefüllt, der Befehl am Anfang eingeschlossen
    assert schreiben <= 1 + 5 + 20
    assert schreiben >= 20
    # Ein neuer Befehl des Executors geht trotz leerem Vorrat durch.
    vorher = schreiben
    assert await inv.async_set_discharge_limit(2.0) is True
    assert len(_deckel_schreibvorgaenge(mock_hass)) == vorher + 1


async def test_victron_takt_nach_dem_aufheben_schreibt_nichts(mock_hass, anlage, soc_beobachter):
    inv = _treiber(mock_hass)
    await inv.async_set_discharge_limit(0.0)
    [(takt, _)] = soc_beobachter.takte
    await inv.async_set_discharge_limit(None)
    vorher = len(_schreibvorgaenge(mock_hass))
    anlage.setze(DC_PV, "5000")
    await takt(None)
    assert len(_schreibvorgaenge(mock_hass)) == vorher


async def test_victron_nur_ein_takt(mock_hass, anlage, soc_beobachter):
    inv = _treiber(mock_hass)
    await inv.async_set_discharge_limit(0.0)
    await inv.async_set_discharge_limit(1.0)
    assert len(soc_beobachter.takte) == 1
