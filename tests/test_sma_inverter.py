"""Tests for the SMA inverter implementation (CmpBMS 6-parameter Modbus).

Focus areas mirror the driver's architecture decisions:
  - U32/S32 big-endian encoding (SMA standard word order)
  - Complete-block semantics: every command writes OpMod + the contiguous
    power block 40793–40802 in exactly two FC16 calls
  - Register semantics: BatChaMaxW=0 blocks charging, GridWSpt positive =
    forced export, neutral block on stop
  - Watchdog keepalive: active block survives and is rewritten
  - Flash-wear guard: no write address outside the CmpBMS set
"""

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.eeg_energy_optimizer.const import CONF_BATTERY_SOC_SENSOR
from custom_components.eeg_energy_optimizer.inverter.base import InverterBase
from custom_components.eeg_energy_optimizer.inverter.sma import (
    DEFAULT_POWER_LIMIT_W,
    KEEPALIVE_INTERVAL,
    OPMOD_CHARGE,
    OPMOD_DEFAULT,
    REG_BAT_CHA_MIN_W,
    REG_BATTERY_SOC,
    REG_CMPBMS_OPMOD,
    SMA_UNIT_ID,
    SMAInverter,
    CmpBmsBlock,
    registers_to_s32,
    registers_to_u32,
    s32_to_registers,
    u32_to_registers,
)


def _ok_response(registers=None):
    res = MagicMock()
    res.isError = MagicMock(return_value=False)
    if registers is not None:
        res.registers = registers
    return res


def _err_response():
    res = MagicMock()
    res.isError = MagicMock(return_value=True)
    return res


@pytest.fixture
def sma_config():
    return {
        "sma_modbus_host": "192.168.1.60",
        "sma_modbus_port": 502,
    }


@pytest.fixture
def mock_modbus_client():
    """Mocked AsyncModbusTcpClient with success responses by default."""
    client = MagicMock()
    client.connected = True
    client.write_registers = AsyncMock(return_value=_ok_response())
    client.read_holding_registers = AsyncMock(
        return_value=_ok_response(u32_to_registers(0))
    )
    client.connect = AsyncMock(return_value=True)
    client.close = MagicMock()
    return client


@pytest.fixture
def inverter(mock_hass, sma_config, mock_modbus_client):
    inv = SMAInverter(mock_hass, sma_config)
    inv._client = mock_modbus_client
    return inv


def _writes(client):
    """Return list of (address, values) tuples for all register writes."""
    return [
        (c.kwargs.get("address"), c.kwargs.get("values"))
        for c in client.write_registers.await_args_list
    ]


def _last_block(client):
    """Decode the last complete CmpBMS block written to the client."""
    opmod = None
    power = None
    for address, values in _writes(client):
        if address == REG_CMPBMS_OPMOD:
            opmod = registers_to_u32(values)
        elif address == REG_BAT_CHA_MIN_W:
            power = values
    assert opmod is not None and power is not None
    assert len(power) == 10
    return CmpBmsBlock(
        op_mod=opmod,
        cha_min_w=registers_to_u32(power[0:2]),
        cha_max_w=registers_to_u32(power[2:4]),
        dsch_min_w=registers_to_u32(power[4:6]),
        dsch_max_w=registers_to_u32(power[6:8]),
        grid_w_spt=registers_to_s32(power[8:10]),
    )


class TestSMAInverterBase:
    def test_is_instance_of_inverter_base(self, inverter):
        assert isinstance(inverter, InverterBase)

    def test_is_subclass_of_inverter_base(self):
        assert issubclass(SMAInverter, InverterBase)


class TestConfigParsing:
    def test_host_from_config(self, mock_hass):
        inv = SMAInverter(mock_hass, {"sma_modbus_host": "10.0.0.9"})
        assert inv._host == "10.0.0.9"

    def test_default_port_is_502(self, mock_hass):
        inv = SMAInverter(mock_hass, {})
        assert inv._port == 502

    def test_custom_port(self, mock_hass):
        inv = SMAInverter(mock_hass, {"sma_modbus_port": 1502})
        assert inv._port == 1502

    def test_unit_id_is_3(self, mock_hass):
        inv = SMAInverter(mock_hass, {})
        assert inv._slave_id == SMA_UNIT_ID == 3

    def test_available_iff_host_configured(self, mock_hass):
        assert SMAInverter(mock_hass, {"sma_modbus_host": "x"}).is_available
        assert not SMAInverter(mock_hass, {}).is_available


class TestEncoding:
    """SMA standard: big-endian word order (high word first)."""

    def test_u32_encoding(self):
        # 2424 = 0x00000978 → high word 0x0000, low word 0x0978
        assert u32_to_registers(2424) == [0x0000, 0x0978]

    def test_u32_high_word(self):
        assert u32_to_registers(0x00010000) == [0x0001, 0x0000]

    def test_u32_roundtrip(self):
        for value in (0, 1, 2424, 10000, 0xFFFFFFFF):
            assert registers_to_u32(u32_to_registers(value)) == value

    def test_s32_positive_roundtrip(self):
        assert registers_to_s32(s32_to_registers(5000)) == 5000

    def test_s32_negative_roundtrip(self):
        assert registers_to_s32(s32_to_registers(-5000)) == -5000

    def test_s32_negative_two_complement(self):
        # -1 = 0xFFFFFFFF
        assert s32_to_registers(-1) == [0xFFFF, 0xFFFF]


class TestBlockComposition:
    """Every command must write the COMPLETE 6-parameter block: OpMod via
    one FC16, then 40793–40802 as ONE contiguous 10-register FC16 write
    (SMA requires all 6 within 10 s)."""

    async def test_two_writes_per_command(self, inverter, mock_modbus_client):
        await inverter.async_set_charge_limit(0)
        writes = _writes(mock_modbus_client)
        assert len(writes) == 2
        assert writes[0][0] == REG_CMPBMS_OPMOD
        assert writes[1][0] == REG_BAT_CHA_MIN_W
        assert len(writes[1][1]) == 10

    async def test_no_write_outside_cmpbms_set(
        self, inverter, mock_modbus_client
    ):
        """Flash-wear guard: only 40236 and the 40793-block are written."""
        await inverter.async_set_charge_limit(0)
        await inverter.async_set_discharge(3.0)
        await inverter.async_stop_forcible()
        for address, _values in _writes(mock_modbus_client):
            assert address in (REG_CMPBMS_OPMOD, REG_BAT_CHA_MIN_W)

    async def test_uses_unit_id_3(self, inverter, mock_modbus_client):
        await inverter.async_set_charge_limit(0)
        for c in mock_modbus_client.write_registers.await_args_list:
            slave = c.kwargs.get("slave", c.kwargs.get("device_id"))
            assert slave == SMA_UNIT_ID


class TestChargeLimit:
    async def test_block_charging(self, inverter, mock_modbus_client):
        """power_kw=0 → BatChaMaxW=0, discharge stays available, no export."""
        ok = await inverter.async_set_charge_limit(0)
        assert ok is True
        block = _last_block(mock_modbus_client)
        assert block.op_mod == OPMOD_DEFAULT
        assert block.cha_max_w == 0
        assert block.dsch_max_w == DEFAULT_POWER_LIMIT_W
        assert block.grid_w_spt == 0

    async def test_partial_charge_limit(self, inverter, mock_modbus_client):
        """power_kw=2.5 → BatChaMaxW=2500 (Einspeisebegrenzung)."""
        ok = await inverter.async_set_charge_limit(2.5)
        assert ok is True
        block = _last_block(mock_modbus_client)
        assert block.cha_max_w == 2500
        assert block.grid_w_spt == 0

    async def test_sets_active_and_keepalive(self, inverter):
        await inverter.async_set_charge_limit(0)
        assert inverter._active is not None
        assert inverter._keepalive_task is not None
        inverter._cancel_keepalive()

    async def test_write_error_returns_false(
        self, inverter, mock_modbus_client
    ):
        mock_modbus_client.write_registers = AsyncMock(
            return_value=_err_response()
        )
        ok = await inverter.async_set_charge_limit(0)
        assert ok is False
        assert inverter._active is None


class TestDischarge:
    async def test_grid_setpoint_positive_export(
        self, inverter, mock_modbus_client
    ):
        """Discharge 3 kW → GridWSpt=+3000 (positive = export), charging
        blocked, discharge limit open."""
        ok = await inverter.async_set_discharge(3.0)
        assert ok is True
        block = _last_block(mock_modbus_client)
        assert block.op_mod == OPMOD_DEFAULT
        assert block.grid_w_spt == 3000
        assert block.cha_max_w == 0
        assert block.dsch_max_w == DEFAULT_POWER_LIMIT_W

    async def test_target_soc_accepted_but_ignored(
        self, inverter, mock_modbus_client
    ):
        """SMA has no hardware target-SOC — the optimizer supervises."""
        ok = await inverter.async_set_discharge(2.0, target_soc=30)
        assert ok is True
        block = _last_block(mock_modbus_client)
        assert block.grid_w_spt == 2000

    async def test_negative_power_clamped_to_zero(
        self, inverter, mock_modbus_client
    ):
        await inverter.async_set_discharge(-5.0)
        block = _last_block(mock_modbus_client)
        assert block.grid_w_spt == 0

    async def test_sets_active_and_keepalive(self, inverter):
        await inverter.async_set_discharge(3.0)
        assert inverter._active is not None
        inverter._cancel_keepalive()


class TestStopForcible:
    async def test_writes_neutral_block(self, inverter, mock_modbus_client):
        """Stop → default mode, full limits both directions, setpoint 0."""
        await inverter.async_set_discharge(3.0)
        ok = await inverter.async_stop_forcible()
        assert ok is True
        block = _last_block(mock_modbus_client)
        assert block.op_mod == OPMOD_DEFAULT
        assert block.cha_max_w == DEFAULT_POWER_LIMIT_W
        assert block.dsch_max_w == DEFAULT_POWER_LIMIT_W
        assert block.grid_w_spt == 0

    async def test_clears_active_and_keepalive(self, inverter):
        await inverter.async_set_discharge(3.0)
        await inverter.async_stop_forcible()
        assert inverter._active is None
        assert inverter._keepalive_task is None

    async def test_active_cleared_even_on_write_failure(
        self, inverter, mock_modbus_client
    ):
        """The active block is cleared BEFORE the write so a failed stop
        can never race a keepalive rewrite of the stale block."""
        await inverter.async_set_discharge(3.0)
        mock_modbus_client.write_registers = AsyncMock(
            return_value=_err_response()
        )
        ok = await inverter.async_stop_forcible()
        assert ok is False
        assert inverter._active is None


class TestKeepalive:
    async def test_keepalive_rewrites_active_block(
        self, inverter, mock_modbus_client, monkeypatch
    ):
        """The keepalive loop rewrites the active block after the interval."""
        monkeypatch.setattr(
            "custom_components.eeg_energy_optimizer.inverter.sma."
            "KEEPALIVE_INTERVAL",
            0.01,
        )
        await inverter.async_set_discharge(3.0)
        writes_before = len(_writes(mock_modbus_client))
        await asyncio.sleep(0.1)
        inverter._cancel_keepalive()
        writes_after = len(_writes(mock_modbus_client))
        assert writes_after > writes_before
        # Rewrites are the identical discharge block
        block = _last_block(mock_modbus_client)
        assert block.grid_w_spt == 3000

    async def test_keepalive_stops_when_inactive(
        self, inverter, monkeypatch
    ):
        monkeypatch.setattr(
            "custom_components.eeg_energy_optimizer.inverter.sma."
            "KEEPALIVE_INTERVAL",
            0.01,
        )
        await inverter.async_set_discharge(3.0)
        inverter._active = None
        await asyncio.sleep(0.05)
        assert (
            inverter._keepalive_task is None
            or inverter._keepalive_task.done()
        )

    def test_keepalive_interval_within_watchdog(self):
        """SMA requires a refresh at least every 300 s."""
        assert KEEPALIVE_INTERVAL < 300


class TestDisconnect:
    async def test_disconnect_cleans_up(self, inverter, mock_modbus_client):
        await inverter.async_set_discharge(3.0)
        await inverter.async_disconnect()
        assert inverter._active is None
        assert inverter._client is None
        mock_modbus_client.close.assert_called()


class TestFahrplanSchnittstelle:
    """Was der ScheduleExecutor vom Treiber verlangt (base.InverterBase)."""

    def test_treiber_wird_vom_fahrplan_gesteuert(self, inverter):
        assert inverter.supports_schedule_control is True

    def test_entladung_ist_ein_netz_sollwert(self, inverter):
        """GridWSpt regelt den Netzanschlusspunkt — der Executor gibt den
        Export vor, nicht die Batterieleistung."""
        assert inverter.discharge_is_grid_setpoint is True

    async def test_ladelimit_ohne_aktiven_block_ist_unbekannt(self, inverter):
        """Kein Block aktiv = internes Management; was in 40795 steht, sagt
        nichts über eine Begrenzung aus. Also None, nicht ein Registerwert."""
        assert await inverter.async_get_charge_limit_kw() is None

    async def test_ladelimit_kommt_aus_dem_aktiven_block(self, inverter):
        await inverter.async_set_charge_limit(2.5)
        assert await inverter.async_get_charge_limit_kw() == pytest.approx(2.5)
        await inverter.async_set_discharge(3.0)
        # Während der Entladung ist Laden gesperrt: Block trägt ChaMax 0.
        assert await inverter.async_get_charge_limit_kw() == pytest.approx(0.0)
        await inverter.async_disconnect()

    def test_hardware_grenzen_sind_unbekannt(self, inverter):
        assert inverter.get_charge_limit_max_kw() is None
        assert inverter.get_max_discharge_power_kw() is None

    async def test_steuerwerte_zeigen_die_vier_register(self, inverter, mock_modbus_client):
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response(u32_to_registers(OPMOD_DEFAULT))
        )
        werte = await inverter.async_get_control_values()
        assert [w["role"] for w in werte] == ["mode", "charge_limit", "discharge_limit", "forcible"]
        assert "Voreinstellung" in werte[0]["value"]
        # 2424 als S32 gelesen ist positiv → Einspeisung.
        assert "Einspeisung" in werte[3]["label"]

    async def test_negativer_netz_sollwert_heisst_bezug(self, inverter, mock_modbus_client):
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response(s32_to_registers(-1500))
        )
        werte = await inverter.async_get_control_values()
        assert werte[3]["value"] == -1500
        assert "Bezug" in werte[3]["label"]

    async def test_steuerwerte_ohne_verbindung_sind_leer(self, inverter, mock_modbus_client):
        mock_modbus_client.connected = False
        mock_modbus_client.connect = AsyncMock(return_value=False)
        assert await inverter.async_get_control_values() == []


# ----------------------------------------------------------------------
# Netzladung (Beta): OpMod 2289, ChaMin = ChaMax = P, Entladen gesperrt
# ----------------------------------------------------------------------

SOC_SENSOR = "sensor.sma_soc"
NORMAL_BLOCK = CmpBmsBlock(
    OPMOD_DEFAULT, 0, DEFAULT_POWER_LIMIT_W, 0, DEFAULT_POWER_LIMIT_W, 0
)


def _soc_register(client, soc):
    """30845 liefert `soc`, alle anderen Lesezugriffe 0."""

    async def _lesen(address=None, count=2, **_kw):
        wert = soc if address == REG_BATTERY_SOC else 0
        return _ok_response(u32_to_registers(wert))

    client.read_holding_registers = AsyncMock(side_effect=_lesen)


@pytest.fixture
def soc_beobachter():
    """``async_track_state_change_event`` abfangen: liefert die Rückrufe."""
    rueckrufe = []
    abmelder = MagicMock()

    def _track(hass, entities, callback):
        rueckrufe.append((list(entities), callback))
        return abmelder

    modul = ModuleType("homeassistant.helpers.event")
    modul.async_track_state_change_event = _track
    # Andere Testmodule legen unter homeassistant.core einen MagicMock ab,
    # dessen ``callback`` den Rückruf verschlucken würde — hier eines, das
    # ihn nur markiert, wie das echte.
    core = ModuleType("homeassistant.core")

    def _callback(func):
        func._hass_callback = True
        return func

    core.callback = _callback
    with patch.dict(
        sys.modules, {"homeassistant.helpers.event": modul, "homeassistant.core": core}
    ):
        yield SimpleNamespace(rueckrufe=rueckrufe, abmelder=abmelder)


async def _warten_bis(bedingung, frist_s: float = 5.0) -> None:
    schritte = int(frist_s / 0.02)
    for _ in range(schritte):
        if bedingung():
            return
        await asyncio.sleep(0.02)
    assert bedingung()


def _mit_sensor(hass, client):
    inv = SMAInverter(
        hass, {"sma_modbus_host": "x", CONF_BATTERY_SOC_SENSOR: SOC_SENSOR}
    )
    inv._client = client
    return inv


class TestNetzladung:
    def test_treiber_kann_netzladen(self, inverter):
        assert inverter.supports_grid_charge is True

    async def test_block_werte(self, inverter, mock_modbus_client):
        """2289, ChaMin = ChaMax = Batterieleistung, Entladen 0, GridWSpt 0."""
        _soc_register(mock_modbus_client, 40)
        ok = await inverter.async_set_grid_charge(2.5, target_soc=80)
        assert ok is True
        block = _last_block(mock_modbus_client)
        assert block == CmpBmsBlock(OPMOD_CHARGE, 2500, 2500, 0, 0, 0)
        assert inverter._active == block
        assert inverter._keepalive_task is not None
        assert await inverter.async_get_charge_limit_kw() == pytest.approx(2.5)
        await inverter.async_disconnect()

    async def test_schreibmenge_bleibt_geschlossen(self, inverter, mock_modbus_client):
        """Keine neue Registeradresse: nur 40236 und der 40793-Block."""
        _soc_register(mock_modbus_client, 40)
        await inverter.async_set_grid_charge(3.0, target_soc=90)
        await inverter.async_set_discharge_limit(0.0)
        await inverter.async_stop_forcible()
        assert _writes(mock_modbus_client)
        for address, _values in _writes(mock_modbus_client):
            assert address in (REG_CMPBMS_OPMOD, REG_BAT_CHA_MIN_W)

    async def test_kleine_leistung_ist_freigabe(self, inverter, mock_modbus_client):
        """Unter 0,05 kW: Normal-Block, kein Keepalive — 0 W in 2289
        sperrte sonst auch das Laden aus PV."""
        _soc_register(mock_modbus_client, 40)
        ok = await inverter.async_set_grid_charge(0.04, target_soc=80)
        assert ok is True
        assert _last_block(mock_modbus_client) == NORMAL_BLOCK
        assert inverter._active is None
        assert inverter._keepalive_task is None

    async def test_soc_am_ziel_nur_freigabe(self, inverter, mock_modbus_client):
        _soc_register(mock_modbus_client, 80)
        ok = await inverter.async_set_grid_charge(2.0, target_soc=80)
        assert ok is True
        assert _last_block(mock_modbus_client) == NORMAL_BLOCK
        assert all(
            registers_to_u32(v) != OPMOD_CHARGE
            for a, v in _writes(mock_modbus_client)
            if a == REG_CMPBMS_OPMOD
        )
        assert inverter._active is None

    async def test_ohne_ziel_gilt_100_prozent(self, inverter, mock_modbus_client):
        _soc_register(mock_modbus_client, 100)
        await inverter.async_set_grid_charge(2.0)
        assert inverter._active is None
        _soc_register(mock_modbus_client, 99)
        await inverter.async_set_grid_charge(2.0)
        assert inverter._active.op_mod == OPMOD_CHARGE
        await inverter.async_disconnect()

    async def test_keepalive_stoppt_am_ziel(
        self, inverter, mock_modbus_client, monkeypatch
    ):
        monkeypatch.setattr(
            "custom_components.eeg_energy_optimizer.inverter.sma."
            "KEEPALIVE_INTERVAL",
            0.01,
        )
        _soc_register(mock_modbus_client, 50)
        await inverter.async_set_grid_charge(2.0, target_soc=80)
        vorher = len(_writes(mock_modbus_client))
        # Unter dem Ziel: der Ladeblock wird erneuert (ein Block braucht
        # mit den Pausen zwischen den Schreibzugriffen 0,4 s).
        await _warten_bis(lambda: len(_writes(mock_modbus_client)) >= vorher + 2)
        assert _last_block(mock_modbus_client).op_mod == OPMOD_CHARGE
        _soc_register(mock_modbus_client, 80)
        await _warten_bis(lambda: inverter._active is None)
        assert _last_block(mock_modbus_client) == NORMAL_BLOCK
        assert inverter._netzladung_ziel is None
        await _warten_bis(
            lambda: inverter._keepalive_task is None or inverter._keepalive_task.done()
        )
        anzahl = len(_writes(mock_modbus_client))
        await asyncio.sleep(0.1)
        assert len(_writes(mock_modbus_client)) == anzahl

    async def test_soc_sensor_beendet_die_ladung(
        self, mock_hass, mock_modbus_client, soc_beobachter
    ):
        """Zwischen zwei Keepalives: der SOC-Sensor meldet das Ziel."""
        inv = _mit_sensor(mock_hass, mock_modbus_client)
        _soc_register(mock_modbus_client, 50)
        mock_hass.states.get = MagicMock(return_value=SimpleNamespace(state="50"))
        await inv.async_set_grid_charge(2.0, target_soc=80)
        assert len(soc_beobachter.rueckrufe) == 1
        entities, rueckruf = soc_beobachter.rueckrufe[0]
        assert entities == [SOC_SENSOR]
        # Ohne @callback führte HA den Rückruf im Executor-Thread aus, und
        # ``hass.async_create_task`` würde von dort abgewiesen.
        assert getattr(rueckruf, "_hass_callback", False) is True

        aufgaben = []
        mock_hass.async_create_task = MagicMock(
            side_effect=lambda c: aufgaben.append(asyncio.ensure_future(c))
        )
        rueckruf(None)  # 50 % — nichts
        assert aufgaben == []
        mock_hass.states.get = MagicMock(return_value=SimpleNamespace(state="80.4"))
        rueckruf(None)
        await asyncio.gather(*aufgaben)
        assert _last_block(mock_modbus_client) == NORMAL_BLOCK
        assert inv._active is None
        assert inv._keepalive_task is None
        soc_beobachter.abmelder.assert_called_once()

    async def test_stopp_meldet_den_beobachter_ab(
        self, mock_hass, mock_modbus_client, soc_beobachter
    ):
        inv = _mit_sensor(mock_hass, mock_modbus_client)
        _soc_register(mock_modbus_client, 50)
        await inv.async_set_grid_charge(2.0, target_soc=80)
        # Eine Leistungsänderung meldet nicht doppelt an.
        await inv.async_set_grid_charge(1.5, target_soc=80)
        assert len(soc_beobachter.rueckrufe) == 1
        await inv.async_stop_forcible()
        soc_beobachter.abmelder.assert_called_once()
        assert inv._netzladung_ziel is None

    async def test_deckel_waehrend_netzladung_wird_nur_gemerkt(
        self, inverter, mock_modbus_client
    ):
        _soc_register(mock_modbus_client, 40)
        await inverter.async_set_grid_charge(2.0, target_soc=80)
        anzahl = len(_writes(mock_modbus_client))
        ok = await inverter.async_set_discharge_limit(0.5)
        assert ok is True
        assert len(_writes(mock_modbus_client)) == anzahl
        assert _last_block(mock_modbus_client).op_mod == OPMOD_CHARGE
        assert inverter._active.op_mod == OPMOD_CHARGE
        await inverter.async_disconnect()

    async def test_netzladung_loescht_deckel_und_ladelimit(
        self, inverter, mock_modbus_client
    ):
        _soc_register(mock_modbus_client, 40)
        await inverter.async_set_charge_limit(1.0)
        await inverter.async_set_discharge_limit(0.0)
        await inverter.async_set_grid_charge(2.0, target_soc=80)
        assert inverter._entladedeckel_w is None
        assert inverter._ladelimit_w is None
        assert _last_block(mock_modbus_client).dsch_max_w == 0
        await inverter.async_disconnect()

    async def test_stopp_schreibt_normal_block(self, inverter, mock_modbus_client):
        _soc_register(mock_modbus_client, 40)
        await inverter.async_set_grid_charge(2.0, target_soc=80)
        ok = await inverter.async_stop_forcible()
        assert ok is True
        assert _last_block(mock_modbus_client) == NORMAL_BLOCK
        assert inverter._active is None
        assert inverter._netzladung_ziel is None
        assert inverter._keepalive_task is None

    async def test_nicht_endlich_schreibt_nichts(self, inverter, mock_modbus_client):
        ok = await inverter.async_set_grid_charge(float("nan"), target_soc=80)
        assert ok is False
        assert inverter.last_write_error == "Sollwert nicht endlich"
        assert _writes(mock_modbus_client) == []

    async def test_schreibfehler_meldet_grund(self, inverter, mock_modbus_client):
        _soc_register(mock_modbus_client, 40)
        mock_modbus_client.write_registers = AsyncMock(return_value=_err_response())
        ok = await inverter.async_set_grid_charge(2.0, target_soc=80)
        assert ok is False
        assert inverter.last_write_error
        assert inverter._active is None
        assert inverter._netzladung_ziel is None

    async def test_soc_aus_sensor_wenn_register_fehlt(
        self, mock_hass, mock_modbus_client
    ):
        """30845 nicht lesbar (SMA-NaN) → der SOC-Sensor entscheidet."""
        inv = _mit_sensor(mock_hass, mock_modbus_client)
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response([0xFFFF, 0xFFFF])
        )
        mock_hass.states.get = MagicMock(return_value=SimpleNamespace(state="85"))
        ok = await inv.async_set_grid_charge(2.0, target_soc=80)
        assert ok is True
        assert inv._active is None

    async def test_gescheiterte_freigabe_am_ziel_wiederholt_der_keepalive(
        self, mock_hass, mock_modbus_client, soc_beobachter, monkeypatch
    ):
        """Scheitert die Freigabe aus dem SOC-Beobachter, bleiben Ladeblock
        und Ziel vermerkt und der Keepalive läuft weiter. Er schreibt den
        Ladeblock nicht neu, sondern wiederholt die Freigabe — sonst lüde das
        Gerät bis zum Watchdog (300 s) aus dem Netz weiter, denn der Executor
        schreibt bei gleicher Leistung nicht neu."""
        inv = _mit_sensor(mock_hass, mock_modbus_client)
        _soc_register(mock_modbus_client, 50)
        mock_hass.states.get = MagicMock(return_value=SimpleNamespace(state="50"))
        await inv.async_set_grid_charge(2.0, target_soc=80)
        _entities, rueckruf = soc_beobachter.rueckrufe[0]
        aufgaben = []
        mock_hass.async_create_task = MagicMock(
            side_effect=lambda c: aufgaben.append(asyncio.ensure_future(c))
        )

        mock_hass.states.get = MagicMock(return_value=SimpleNamespace(state="80"))
        mock_modbus_client.write_registers = AsyncMock(return_value=_err_response())
        rueckruf(None)
        await asyncio.gather(*aufgaben)
        assert inv._active is not None and inv._active.op_mod == OPMOD_CHARGE
        assert inv._netzladung_ziel == 80
        assert inv._keepalive_task is not None and not inv._keepalive_task.done()
        soc_beobachter.abmelder.assert_not_called()

        # Der nächste Keepalive-Takt: Freigabe statt Ladeblock.
        monkeypatch.setattr(
            "custom_components.eeg_energy_optimizer.inverter.sma."
            "KEEPALIVE_INTERVAL",
            0.01,
        )
        inv._cancel_keepalive()
        inv._start_keepalive()
        _soc_register(mock_modbus_client, 80)
        mock_modbus_client.write_registers = AsyncMock(return_value=_ok_response())
        await _warten_bis(lambda: inv._active is None)
        assert _last_block(mock_modbus_client) == NORMAL_BLOCK
        assert all(
            registers_to_u32(v) != OPMOD_CHARGE
            for a, v in _writes(mock_modbus_client)
            if a == REG_CMPBMS_OPMOD
        )
        assert inv._netzladung_ziel is None
        soc_beobachter.abmelder.assert_called_once()

    async def test_offene_freigabe_gilt_auch_wenn_30845_darunter_meldet(
        self, mock_hass, mock_modbus_client, soc_beobachter, monkeypatch
    ):
        """Sensor 80 %, Register 79 %: Der Beobachter erkennt das Ziel, seine
        Freigabe scheitert. Der Keepalive prüft zuerst 30845 und hielte die
        Ladung für unfertig — mit „Freigabe offen“ wiederholt er trotzdem NUR
        die Freigabe und schreibt den Ladeblock nicht neu (das fütterte den
        Watchdog, die Netzladung liefe über das Ziel weiter)."""
        inv = _mit_sensor(mock_hass, mock_modbus_client)
        _soc_register(mock_modbus_client, 50)
        mock_hass.states.get = MagicMock(return_value=SimpleNamespace(state="50"))
        await inv.async_set_grid_charge(2.0, target_soc=80)
        _entities, rueckruf = soc_beobachter.rueckrufe[0]
        aufgaben = []
        mock_hass.async_create_task = MagicMock(
            side_effect=lambda c: aufgaben.append(asyncio.ensure_future(c))
        )
        mock_hass.states.get = MagicMock(return_value=SimpleNamespace(state="80"))
        _soc_register(mock_modbus_client, 79)
        mock_modbus_client.write_registers = AsyncMock(return_value=_err_response())
        rueckruf(None)
        await asyncio.gather(*aufgaben)
        assert inv._freigabe_offen is True

        monkeypatch.setattr(
            "custom_components.eeg_energy_optimizer.inverter.sma."
            "KEEPALIVE_INTERVAL",
            0.01,
        )
        inv._cancel_keepalive()
        mock_modbus_client.write_registers = AsyncMock(return_value=_ok_response())
        inv._start_keepalive()
        await _warten_bis(lambda: inv._active is None)
        assert _last_block(mock_modbus_client) == NORMAL_BLOCK
        assert all(
            registers_to_u32(v) != OPMOD_CHARGE
            for a, v in _writes(mock_modbus_client)
            if a == REG_CMPBMS_OPMOD
        )
        assert inv._freigabe_offen is False
        assert inv._netzladung_ziel is None

    async def test_neuer_befehl_raeumt_offene_freigabe_ab(
        self, mock_hass, mock_modbus_client
    ):
        """Ein neuer Ladeblock, ein Ladelimit oder das Trennen beenden die
        offene Freigabe — sie gehörte zur alten Ladung."""
        inv = _mit_sensor(mock_hass, mock_modbus_client)
        _soc_register(mock_modbus_client, 50)
        mock_hass.states.get = MagicMock(return_value=SimpleNamespace(state="50"))
        await inv.async_set_grid_charge(2.0, target_soc=80)
        inv._freigabe_offen = True
        assert await inv.async_set_grid_charge(2.5, target_soc=90) is True
        assert inv._freigabe_offen is False
        inv._freigabe_offen = True
        assert await inv.async_set_charge_limit(1.0) is True
        assert inv._freigabe_offen is False
        inv._freigabe_offen = True
        await inv.async_disconnect()
        assert inv._freigabe_offen is False

    async def test_keepalive_verbindet_nach_lesefehler_neu(
        self, inverter, mock_modbus_client, monkeypatch, caplog
    ):
        """Ein Ausnahmefehler beim Lesen von 30845 schließt die Verbindung.
        Der Keepalive baut sie vor dem Schreiben neu auf, statt in einen
        AttributeError zu laufen und den Watchdog einen Takt lang nicht zu
        füttern."""
        lesungen = {"n": 0}

        async def _lesen(address=None, count=2, **_kw):
            if address == REG_BATTERY_SOC:
                lesungen["n"] += 1
                if lesungen["n"] == 2:  # die erste Lesung im Keepalive
                    raise ConnectionError("Verbindung weg")
            return _ok_response(u32_to_registers(50))

        mock_modbus_client.read_holding_registers = AsyncMock(side_effect=_lesen)
        neue_clients = []

        def _neuer_client(*_a, **_kw):
            neue_clients.append(1)
            return mock_modbus_client

        monkeypatch.setattr("pymodbus.client.AsyncModbusTcpClient", _neuer_client)
        monkeypatch.setattr(
            "custom_components.eeg_energy_optimizer.inverter.sma."
            "KEEPALIVE_INTERVAL",
            0.01,
        )
        await inverter.async_set_grid_charge(2.0, target_soc=80)
        vorher = len(_writes(mock_modbus_client))
        await _warten_bis(lambda: len(_writes(mock_modbus_client)) >= vorher + 2)
        await inverter.async_disconnect()
        assert lesungen["n"] >= 2
        assert neue_clients
        assert _writes(mock_modbus_client)[vorher][0] == REG_CMPBMS_OPMOD
        assert registers_to_u32(_writes(mock_modbus_client)[vorher][1]) == OPMOD_CHARGE
        assert "exception writing CmpBMS block" not in caplog.text

    async def test_verbindungsabbruch_nach_soc_lesen_vergisst_das_ziel(
        self, inverter, mock_modbus_client, monkeypatch
    ):
        """Bricht die Verbindung beim Lesen von 30845 ab und kommt nicht
        wieder, gehört das Ziel zu keiner Ladung — es wird zurückgesetzt."""
        mock_modbus_client.read_holding_registers = AsyncMock(
            side_effect=ConnectionError("Verbindung weg")
        )

        def _kein_client(*_a, **_kw):
            raise ConnectionError("Gerät nicht erreichbar")

        monkeypatch.setattr("pymodbus.client.AsyncModbusTcpClient", _kein_client)
        ok = await inverter.async_set_grid_charge(2.0, target_soc=80)
        assert ok is False
        assert inverter.last_write_error == "Modbus nicht verbunden"
        assert inverter._active is None
        assert inverter._netzladung_ziel is None
        assert _writes(mock_modbus_client) == []


