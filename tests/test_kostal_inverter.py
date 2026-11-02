"""Tests for Kostal Plenticore inverter implementation (proprietary Modbus TCP).

Focus areas mirror the driver's architecture decisions:
  - Float32 word-swap encoding (CDAB, Kostal factory default)
  - Register semantics: 1034 setpoint (positive = discharge), 1038 charge limit
  - Watchdog keepalive: active command survives, rewrites with ±1 W jitter
  - Stuck-register guards on mode switches and stop
  - Snapshot/restore of the pre-block max charge power
"""

import asyncio
import struct
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.eeg_energy_optimizer.const import CONF_DISCHARGE_POWER_KW

from custom_components.eeg_energy_optimizer.inverter.base import InverterBase
from custom_components.eeg_energy_optimizer.inverter.kostal import (
    REG_MAX_CHARGE_BATTERY,
    KEEPALIVE_INTERVAL,
    KOSTAL_UNIT_ID,
    KostalInverter,
    REG_BATTERY_SETPOINT,
    REG_MAX_CHARGE_POWER,
    REG_MAX_DISCHARGE_BATTERY,
    REG_MAX_DISCHARGE_POWER,
    REG_SOC,
    float_to_registers,
    registers_to_float,
    registers_to_string,
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
def kostal_config():
    return {
        "kostal_modbus_host": "192.168.1.50",
        "kostal_modbus_port": 1502,
    }


@pytest.fixture
def mock_modbus_client():
    """Mocked AsyncModbusTcpClient with success responses by default."""
    client = MagicMock()
    client.connected = True
    client.write_registers = AsyncMock(return_value=_ok_response())
    # Default read: 0.0 as word-swapped float (harmless for snapshot logic)
    client.read_holding_registers = AsyncMock(
        return_value=_ok_response(float_to_registers(0.0))
    )
    client.connect = AsyncMock(return_value=True)
    client.close = MagicMock()
    return client


@pytest.fixture
def inverter(mock_hass, kostal_config, mock_modbus_client):
    inv = KostalInverter(mock_hass, kostal_config)
    inv._client = mock_modbus_client
    return inv


def _written(client, address):
    """Return the list of register-value lists written to `address`."""
    calls = []
    for call in client.write_registers.await_args_list:
        kwargs = call.kwargs
        if kwargs.get("address") == address:
            calls.append(kwargs.get("values"))
    return calls


class TestKostalInverterBase:
    def test_is_instance_of_inverter_base(self, inverter):
        assert isinstance(inverter, InverterBase)

    def test_is_subclass_of_inverter_base(self):
        assert issubclass(KostalInverter, InverterBase)


class TestConfigParsing:
    def test_host_from_config(self, mock_hass):
        inv = KostalInverter(mock_hass, {"kostal_modbus_host": "10.0.0.7"})
        assert inv._host == "10.0.0.7"

    def test_default_port_is_1502(self, mock_hass):
        inv = KostalInverter(mock_hass, {})
        assert inv._port == 1502

    def test_custom_port(self, mock_hass):
        inv = KostalInverter(mock_hass, {"kostal_modbus_port": 502})
        assert inv._port == 502

    def test_unit_id_is_71(self, mock_hass):
        inv = KostalInverter(mock_hass, {})
        assert inv._slave_id == KOSTAL_UNIT_ID == 71

    def test_available_iff_host_configured(self, mock_hass):
        assert KostalInverter(mock_hass, {"kostal_modbus_host": "x"}).is_available
        assert not KostalInverter(mock_hass, {}).is_available


class TestFloatEncoding:
    """Kostal factory default: Float32 with word swap (CDAB).

    IEEE754 1.0 = 0x3F800000 (words AB=0x3F80, CD=0x0000) → low word first.
    """

    def test_one_point_zero(self):
        assert float_to_registers(1.0) == [0x0000, 0x3F80]

    def test_zero(self):
        assert float_to_registers(0.0) == [0x0000, 0x0000]

    def test_roundtrip(self):
        for value in (0.0, 1.0, -1.0, 3000.0, 16600.0, 0.5, -2750.25):
            regs = float_to_registers(value)
            assert registers_to_float(regs) == pytest.approx(value, rel=1e-6)

    def test_matches_struct_reference(self):
        # Reference encoding: pack big-endian, swap 16-bit words
        value = 5500.0
        raw = struct.pack(">f", value)
        expected = [
            (raw[2] << 8) | raw[3],
            (raw[0] << 8) | raw[1],
        ]
        assert float_to_registers(value) == expected

    def test_string_decoding(self):
        # "PLENTICORE" as 2 chars per register, null-terminated
        text = "PLENTICORE plus 5.5"
        padded = text + "\x00" * (32 - len(text))
        regs = [
            (ord(padded[i]) << 8) | ord(padded[i + 1])
            for i in range(0, 32, 2)
        ]
        assert registers_to_string(regs) == text


class TestChargeLimit:
    async def test_block_charging_writes_zero_to_1038(self, inverter, mock_modbus_client):
        ok = await inverter.async_set_charge_limit(0)
        assert ok is True
        writes = _written(mock_modbus_client, REG_MAX_CHARGE_POWER)
        assert writes == [float_to_registers(0.0)]
        assert inverter._active == ("charge_limit", 0.0)
        await inverter.async_disconnect()

    async def test_partial_limit_writes_watts(self, inverter, mock_modbus_client):
        ok = await inverter.async_set_charge_limit(2.5)
        assert ok is True
        writes = _written(mock_modbus_client, REG_MAX_CHARGE_POWER)
        assert writes == [float_to_registers(2500.0)]
        await inverter.async_disconnect()

    async def test_snapshots_previous_max_charge_power(self, inverter, mock_modbus_client):
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response(float_to_registers(5600.0))
        )
        await inverter.async_set_charge_limit(0)
        assert inverter._max_charge_pre_block == pytest.approx(5600.0)
        await inverter.async_disconnect()

    async def test_write_failure_returns_false(self, inverter, mock_modbus_client):
        mock_modbus_client.write_registers = AsyncMock(return_value=_err_response())
        ok = await inverter.async_set_charge_limit(0)
        assert ok is False
        assert inverter._active is None
        await inverter.async_disconnect()

    async def test_starts_keepalive_task(self, inverter):
        await inverter.async_set_charge_limit(0)
        assert inverter._keepalive_task is not None
        assert not inverter._keepalive_task.done()
        await inverter.async_disconnect()


class TestDischarge:
    async def test_discharge_writes_positive_setpoint(self, inverter, mock_modbus_client):
        ok = await inverter.async_set_discharge(3.0, target_soc=25.0)
        assert ok is True
        writes = _written(mock_modbus_client, REG_BATTERY_SETPOINT)
        # Positive value = discharge (Kostal convention)
        assert writes == [float_to_registers(3000.0)]
        assert inverter._active == ("discharge", 3000.0)
        await inverter.async_disconnect()

    async def test_target_soc_accepted_but_not_written(self, inverter, mock_modbus_client):
        """Kostal has no hardware target-SOC register — optimizer supervises."""
        await inverter.async_set_discharge(2.0, target_soc=30.0)
        for call in mock_modbus_client.write_registers.await_args_list:
            assert call.kwargs.get("address") in (
                REG_BATTERY_SETPOINT, REG_MAX_CHARGE_POWER,
            )
        await inverter.async_disconnect()

    async def test_write_failure_returns_false(self, inverter, mock_modbus_client):
        mock_modbus_client.write_registers = AsyncMock(return_value=_err_response())
        ok = await inverter.async_set_discharge(3.0)
        assert ok is False
        assert inverter._active is None
        await inverter.async_disconnect()


class TestModeSwitch:
    async def test_discharge_to_charge_limit_zeroes_setpoint(self, inverter, mock_modbus_client):
        """Stuck-register guard: old discharge setpoint is explicitly reset."""
        await inverter.async_set_discharge(3.0)
        mock_modbus_client.write_registers.reset_mock()

        await inverter.async_set_charge_limit(0)
        setpoint_writes = _written(mock_modbus_client, REG_BATTERY_SETPOINT)
        assert setpoint_writes == [float_to_registers(0.0)]
        assert inverter._active == ("charge_limit", 0.0)
        await inverter.async_disconnect()

    async def test_charge_limit_to_discharge_restores_snapshot(self, inverter, mock_modbus_client):
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response(float_to_registers(4000.0))
        )
        await inverter.async_set_charge_limit(0)
        mock_modbus_client.write_registers.reset_mock()

        await inverter.async_set_discharge(2.0)
        restore_writes = _written(mock_modbus_client, REG_MAX_CHARGE_POWER)
        assert restore_writes == [float_to_registers(4000.0)]
        assert inverter._max_charge_pre_block is None
        await inverter.async_disconnect()


class TestStopForcible:
    async def test_stop_zeroes_setpoint_and_cancels_keepalive(self, inverter, mock_modbus_client):
        await inverter.async_set_discharge(3.0)
        task = inverter._keepalive_task
        mock_modbus_client.write_registers.reset_mock()

        ok = await inverter.async_stop_forcible()
        assert ok is True
        assert inverter._active is None
        assert inverter._keepalive_task is None
        # give the cancelled task a tick to unwind
        await asyncio.sleep(0)
        assert task.cancelled() or task.done()
        setpoint_writes = _written(mock_modbus_client, REG_BATTERY_SETPOINT)
        assert setpoint_writes == [float_to_registers(0.0)]

    async def test_stop_restores_max_charge_power(self, inverter, mock_modbus_client):
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response(float_to_registers(6300.0))
        )
        await inverter.async_set_charge_limit(0)
        mock_modbus_client.write_registers.reset_mock()

        ok = await inverter.async_stop_forcible()
        assert ok is True
        restore_writes = _written(mock_modbus_client, REG_MAX_CHARGE_POWER)
        assert restore_writes == [float_to_registers(6300.0)]
        assert inverter._max_charge_pre_block is None

    async def test_stop_returns_false_when_restore_fails(self, inverter, mock_modbus_client):
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response(float_to_registers(6300.0))
        )
        await inverter.async_set_charge_limit(0)

        # Setpoint write succeeds, restore write fails → False so the
        # optimizer retries next cycle.
        async def _write(address, values, **kwargs):
            if address == REG_MAX_CHARGE_POWER:
                return _err_response()
            return _ok_response()

        mock_modbus_client.write_registers = AsyncMock(side_effect=_write)
        ok = await inverter.async_stop_forcible()
        assert ok is False
        assert inverter._max_charge_pre_block == pytest.approx(6300.0)

    async def test_stop_without_active_command_is_safe(self, inverter):
        ok = await inverter.async_stop_forcible()
        assert ok is True


class TestWatchdogRueckfall:
    """Ein 1034-Sollwert haelt, solange irgendein Steuerregister geschrieben
    wird — auch der 1038-Keepalive eines Ladelimits fuettert den Watchdog.
    Ansfelden 25.09.2026: nach 2 min Normalbetrieb stand die Batterie trotz
    Ladelimit 4,2 kW den ganzen Vormittag auf 30 W."""

    SENSOR = "sensor.kostal_battery_power"

    @pytest.fixture
    def uhr(self, monkeypatch):
        from datetime import datetime, timezone
        t = {"jetzt": datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)}
        monkeypatch.setattr(
            "custom_components.eeg_energy_optimizer.inverter.kostal._jetzt",
            lambda: t["jetzt"],
        )
        return t

    @pytest.fixture
    def inv(self, mock_hass, kostal_config, mock_modbus_client, uhr):
        mock_hass.states.get = MagicMock(return_value=None)
        inv = KostalInverter(
            mock_hass, {**kostal_config, "battery_power_sensor": self.SENSOR}
        )
        inv._client = mock_modbus_client
        return inv

    def _messung(self, inv, watt, um):
        state = MagicMock()
        state.state = str(watt)
        state.attributes = {"unit_of_measurement": "W"}
        state.last_reported = um
        state.last_updated = um
        inv._hass.states.get = MagicMock(return_value=state)

    async def test_freigabe_ohne_entladung_schreibt_keinen_sollwert(
        self, inv, mock_modbus_client
    ):
        await inv.async_set_charge_limit(1.0)
        await inv.async_stop_forcible()
        assert _written(mock_modbus_client, REG_BATTERY_SETPOINT) == []
        assert inv._sollwert_seit is None

    async def test_ladelimit_nach_kurzer_freigabe_wird_geschrieben(
        self, inv, mock_modbus_client
    ):
        """Der Ablauf vom 25.09.: Limit → 2 min Normalbetrieb → Limit."""
        await inv.async_set_charge_limit(2.0)
        await inv.async_stop_forcible()
        mock_modbus_client.write_registers.reset_mock()
        await inv.async_set_charge_limit(4.2)
        assert _written(mock_modbus_client, REG_MAX_CHARGE_POWER) == [
            float_to_registers(4200.0)
        ]
        await inv.async_disconnect()

    async def test_ladelimit_nach_entladung_wartet(
        self, inv, mock_modbus_client, uhr
    ):
        await inv.async_set_discharge(3.0)
        await inv.async_stop_forcible()
        mock_modbus_client.write_registers.reset_mock()

        ok = await inv.async_set_charge_limit(4.2)
        assert ok is True
        assert inv._active == ("charge_limit", 4200.0)
        assert mock_modbus_client.write_registers.await_count == 0
        await inv.async_disconnect()

    async def test_bewegung_gibt_die_register_frei(self, inv, uhr):
        from datetime import timedelta
        await inv.async_set_discharge(3.0)
        await inv.async_stop_forcible()
        start = uhr["jetzt"]

        # Gehaltener 0-W-Sollwert: 30 W sind keine Bewegung.
        uhr["jetzt"] = start + timedelta(seconds=60)
        self._messung(inv, 30, uhr["jetzt"])
        assert inv._rueckfall_offen() is True

        # Interne Automatik laedt: frei.
        self._messung(inv, -2100, uhr["jetzt"])
        assert inv._rueckfall_offen() is False
        assert inv._sollwert_seit is None

    async def test_messung_kurz_nach_dem_schreiben_zaehlt_nicht(self, inv, uhr):
        """Direkt nach dem 0-W-Befehl zeigt der Sensor noch die Entladung."""
        from datetime import timedelta
        await inv.async_set_discharge(3.0)
        await inv.async_stop_forcible()
        uhr["jetzt"] += timedelta(seconds=60)
        self._messung(inv, 3000, inv._sollwert_seit + timedelta(seconds=5))
        assert inv._rueckfall_offen() is True

    async def test_ohne_sensor_nach_hoechstdauer_frei(self, inv, uhr):
        from datetime import timedelta
        from custom_components.eeg_energy_optimizer.inverter.kostal import (
            RUECKFALL_MAX_S,
        )
        await inv.async_set_discharge(3.0)
        await inv.async_stop_forcible()
        uhr["jetzt"] += timedelta(seconds=RUECKFALL_MAX_S - 1)
        assert inv._rueckfall_offen() is True
        uhr["jetzt"] += timedelta(seconds=2)
        assert inv._rueckfall_offen() is False

    async def test_keepalive_schreibt_vorgemerktes_limit_nach_rueckfall(
        self, inv, mock_modbus_client, uhr, monkeypatch
    ):
        from datetime import timedelta
        monkeypatch.setattr(
            "custom_components.eeg_energy_optimizer.inverter.kostal.RUECKFALL_ABFRAGE_S",
            0.01,
        )
        await inv.async_set_discharge(3.0)
        await inv.async_set_charge_limit(4.2)
        mock_modbus_client.write_registers.reset_mock()

        await asyncio.sleep(0.1)
        assert _written(mock_modbus_client, REG_MAX_CHARGE_POWER) == []

        uhr["jetzt"] += timedelta(seconds=60)
        self._messung(inv, -1500, uhr["jetzt"])
        await asyncio.sleep(0.5)
        await inv.async_disconnect()
        werte = {
            round(registers_to_float(w))
            for w in _written(mock_modbus_client, REG_MAX_CHARGE_POWER)
        }
        assert werte and werte <= {4200, 4201}
        # Der Sollwert wird dabei nicht angefasst.
        assert _written(mock_modbus_client, REG_BATTERY_SETPOINT) == []

    async def test_entladung_ist_waehrend_des_wartens_erlaubt(
        self, inv, mock_modbus_client
    ):
        await inv.async_set_discharge(3.0)
        await inv.async_stop_forcible()
        mock_modbus_client.write_registers.reset_mock()
        assert await inv.async_set_discharge(2.0) is True
        assert _written(mock_modbus_client, REG_BATTERY_SETPOINT) == [
            float_to_registers(2000.0)
        ]
        await inv.async_disconnect()


class TestKeepalive:
    async def test_keepalive_rewrites_with_jitter(self, inverter, mock_modbus_client, monkeypatch):
        """The keepalive rewrites the active setpoint, alternating ±1 W."""
        monkeypatch.setattr(
            "custom_components.eeg_energy_optimizer.inverter.kostal.KEEPALIVE_INTERVAL",
            0.01,
        )
        await inverter.async_set_discharge(3.0)
        mock_modbus_client.write_registers.reset_mock()

        # allow several keepalive cycles (each write itself sleeps 200 ms)
        await asyncio.sleep(0.9)
        await inverter.async_disconnect()

        writes = _written(mock_modbus_client, REG_BATTERY_SETPOINT)
        assert len(writes) >= 2
        values = {round(registers_to_float(w)) for w in writes}
        # Alternates between 3000 and 3001 (±1 W jitter)
        assert values <= {3000, 3001}
        assert len(values) == 2

    async def test_keepalive_stops_after_stop_forcible(self, inverter, mock_modbus_client, monkeypatch):
        monkeypatch.setattr(
            "custom_components.eeg_energy_optimizer.inverter.kostal.KEEPALIVE_INTERVAL",
            0.01,
        )
        await inverter.async_set_discharge(3.0)
        await inverter.async_stop_forcible()
        mock_modbus_client.write_registers.reset_mock()

        await asyncio.sleep(0.1)
        assert mock_modbus_client.write_registers.await_count == 0

    def test_default_interval_feeds_60s_watchdog(self):
        # Recommended watchdog timeout is 60 s; evcc rewrites at timeout/2.
        # Our interval must stay safely below that even if one write is lost.
        assert KEEPALIVE_INTERVAL <= 30.0


class TestFahrplanSchnittstelle:
    """Was der ScheduleExecutor vom Treiber verlangt (base.InverterBase)."""

    def test_treiber_wird_vom_fahrplan_gesteuert(self, inverter):
        assert inverter.supports_schedule_control is True

    async def test_ladelimit_kommt_aus_register_1038(self, inverter, mock_modbus_client):
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response(float_to_registers(2500.0))
        )
        assert await inverter.async_get_charge_limit_kw() == pytest.approx(2.5)

    async def test_ladelimit_ohne_verbindung_ist_unbekannt(self, inverter, mock_modbus_client):
        mock_modbus_client.connected = False
        mock_modbus_client.connect = AsyncMock(return_value=False)
        assert await inverter.async_get_charge_limit_kw() is None

    async def test_lesefehler_liefert_none_statt_null(self, inverter, mock_modbus_client):
        """Eine 0 hiesse "Laden gesperrt" — Guard 1 wuerde daraus falsch
        weiterrechnen. Ohne Messwert muss None kommen."""
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_err_response()
        )
        assert await inverter.async_get_charge_limit_kw() is None

    async def test_hardware_grenze_wird_aus_ungestoertem_wert_gelernt(
        self, inverter, mock_modbus_client
    ):
        assert inverter.get_charge_limit_max_kw() is None
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response(float_to_registers(9000.0))
        )
        await inverter.async_get_charge_limit_kw()
        assert inverter.get_charge_limit_max_kw() == pytest.approx(9.0)

    async def test_eigenes_limit_gilt_nicht_als_hardware_grenze(
        self, inverter, mock_modbus_client
    ):
        """Sonst haelt der Treiber sein eigenes Limit fuer die Grenze des
        Geraets, und Guard 1 kaeme nie wieder darueber hinaus."""
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response(float_to_registers(9000.0))
        )
        await inverter.async_get_charge_limit_kw()
        await inverter.async_set_charge_limit(1.5)
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response(float_to_registers(1500.0))
        )
        await inverter.async_get_charge_limit_kw()
        assert inverter.get_charge_limit_max_kw() == pytest.approx(9.0)
        await inverter.async_disconnect()

    async def test_entladegrenze_kommt_aus_den_steuerwerten(
        self, inverter, mock_modbus_client
    ):
        assert inverter.get_max_discharge_power_kw() is None
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response(float_to_registers(5000.0))
        )
        await inverter.async_get_control_values()
        assert inverter.get_max_discharge_power_kw() == pytest.approx(5.0)

    async def test_steuerwerte_zeigen_die_drei_register(
        self, inverter, mock_modbus_client
    ):
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response(float_to_registers(3000.0))
        )
        werte = await inverter.async_get_control_values()
        rollen = [w["role"] for w in werte]
        assert rollen == ["forcible", "charge_limit", "discharge_limit"]
        assert all(w["unit"] == "W" for w in werte)
        # Positiver Sollwert heisst entladen — ohne den Hinweis liest sich
        # die nackte Zahl falschherum.
        assert "entladen" in werte[0]["label"]

    async def test_steuerwerte_ohne_verbindung_sind_leer(
        self, inverter, mock_modbus_client
    ):
        mock_modbus_client.connected = False
        mock_modbus_client.connect = AsyncMock(return_value=False)
        assert await inverter.async_get_control_values() == []


class TestEncodingProbe:
    """Teil-Limits unterscheiden Float32 von U32 — das wird einmal geprueft."""

    async def test_abweichung_wird_gemeldet(self, inverter, mock_modbus_client, caplog):
        # Geschrieben 2000 W, zurueck kommt etwas voellig anderes.
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response(float_to_registers(70.0))
        )
        with caplog.at_level("WARNING"):
            await inverter.async_set_charge_limit(2.0)
        assert "zurueckgelesen" in caplog.text
        await inverter.async_disconnect()

    async def test_passender_rueckleser_meldet_nichts(
        self, inverter, mock_modbus_client, caplog
    ):
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response(float_to_registers(2000.0))
        )
        with caplog.at_level("WARNING"):
            await inverter.async_set_charge_limit(2.0)
        assert "zurueckgelesen" not in caplog.text
        await inverter.async_disconnect()

    async def test_blockieren_wird_nicht_geprueft(self, inverter, mock_modbus_client):
        """Bei 0 W sind beide Kodierungen bitgleich — nichts zu pruefen."""
        await inverter.async_set_charge_limit(0.0)
        assert inverter._encoding_geprueft is False
        await inverter.async_disconnect()

    async def test_probe_laeuft_nur_einmal(self, inverter, mock_modbus_client):
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response(float_to_registers(2000.0))
        )
        await inverter.async_set_charge_limit(2.0)
        vorher = mock_modbus_client.read_holding_registers.await_count
        await inverter.async_set_charge_limit(3.0)
        # Nur noch der Snapshot-Read, keine zweite Probe.
        assert mock_modbus_client.read_holding_registers.await_count <= vorher + 1
        await inverter.async_disconnect()


# ---------------------------------------------------------------------------
# Entladedeckel (1040) und Netzladung (1034 negativ) — Beta, November 2026
# ---------------------------------------------------------------------------


def _register_lesen(client, werte):
    """Lesezugriffe je Adresse beantworten; ``None`` = Lesefehler."""

    async def _read(address, count, **kwargs):
        wert = werte.get(address, 0.0)
        if wert is None:
            return _err_response()
        return _ok_response(float_to_registers(wert))

    client.read_holding_registers = AsyncMock(side_effect=_read)


def _adressen(client):
    return [c.kwargs.get("address") for c in client.write_registers.await_args_list]


def _werte(client, address):
    return [round(registers_to_float(w)) for w in _written(client, address)]


class TestEntladedeckel:
    async def test_kann_deckeln(self, inverter):
        assert inverter.supports_discharge_limit is True

    async def test_deckel_schreibt_1040_in_watt(self, inverter, mock_modbus_client):
        _register_lesen(mock_modbus_client, {REG_MAX_DISCHARGE_POWER: 5000.0})
        assert await inverter.async_set_discharge_limit(1.2) is True
        assert _werte(mock_modbus_client, REG_MAX_DISCHARGE_POWER) == [1200]
        assert inverter._max_discharge_pre_deckel == pytest.approx(5000.0)
        # 1034 bleibt unberuehrt — der Deckel ist kein Sollwert.
        assert _written(mock_modbus_client, REG_BATTERY_SETPOINT) == []
        assert inverter._sollwert_seit is None
        await inverter.async_disconnect()

    async def test_aufheben_ohne_deckel_schreibt_nichts(
        self, inverter, mock_modbus_client
    ):
        """Der Executor hebt beim Start „zur Sicherheit" auf — ein
        Schreibzugriff schaltete die externe Steuerung erst ein."""
        assert await inverter.async_set_discharge_limit(None) is True
        assert mock_modbus_client.write_registers.await_count == 0
        assert mock_modbus_client.read_holding_registers.await_count == 0

    async def test_aufheben_schreibt_den_vorwert(self, inverter, mock_modbus_client):
        _register_lesen(mock_modbus_client, {REG_MAX_DISCHARGE_POWER: 5000.0})
        await inverter.async_set_discharge_limit(0.0)
        assert await inverter.async_set_discharge_limit(None) is True
        assert _werte(mock_modbus_client, REG_MAX_DISCHARGE_POWER) == [0, 5000]
        assert inverter._deckel_beruehrt is False
        assert inverter._deckel_w is None

    async def test_ohne_vorwert_gilt_1078(self, inverter, mock_modbus_client):
        _register_lesen(
            mock_modbus_client,
            {REG_MAX_DISCHARGE_POWER: None, REG_MAX_DISCHARGE_BATTERY: 4500.0},
        )
        await inverter.async_set_discharge_limit(1.0)
        await inverter.async_set_discharge_limit(None)
        assert _werte(mock_modbus_client, REG_MAX_DISCHARGE_POWER) == [1000, 4500]

    async def test_ohne_jeden_vorwert_fehler_mit_grund(
        self, inverter, mock_modbus_client
    ):
        _register_lesen(
            mock_modbus_client,
            {REG_MAX_DISCHARGE_POWER: None, REG_MAX_DISCHARGE_BATTERY: None},
        )
        await inverter.async_set_discharge_limit(1.0)
        assert await inverter.async_set_discharge_limit(None) is False
        assert inverter.last_write_error == "Entladegrenze der Batterie unbekannt"
        assert inverter._deckel_beruehrt is True
        await inverter.async_disconnect()

    async def test_eigener_deckel_ist_keine_hardware_grenze(
        self, inverter, mock_modbus_client
    ):
        _register_lesen(mock_modbus_client, {REG_MAX_DISCHARGE_POWER: 5000.0})
        await inverter.async_set_discharge_limit(1.0)
        _register_lesen(mock_modbus_client, {REG_MAX_DISCHARGE_POWER: 1000.0})
        await inverter.async_get_control_values()
        assert inverter.get_max_discharge_power_kw() == pytest.approx(5.0)
        await inverter.async_disconnect()

    async def test_deckel_neben_ladelimit_keepalive_schreibt_beide(
        self, inverter, mock_modbus_client, monkeypatch
    ):
        monkeypatch.setattr(
            "custom_components.eeg_energy_optimizer.inverter.kostal.KEEPALIVE_INTERVAL",
            0.01,
        )
        _register_lesen(
            mock_modbus_client,
            {REG_MAX_CHARGE_POWER: 6000.0, REG_MAX_DISCHARGE_POWER: 5000.0},
        )
        await inverter.async_set_charge_limit(2.0)
        await inverter.async_set_discharge_limit(1.0)
        assert inverter._active == ("charge_limit", 2000.0)
        mock_modbus_client.write_registers.reset_mock()

        await asyncio.sleep(1.0)
        await inverter.async_disconnect()
        deckel = _werte(mock_modbus_client, REG_MAX_DISCHARGE_POWER)
        # Das Entladen der Integration nimmt den Deckel als Letztes zurueck.
        assert deckel[-1] == 5000
        assert set(_werte(mock_modbus_client, REG_MAX_CHARGE_POWER)) <= {2000, 2001}
        assert set(deckel[:-1]) <= {1000, 1001}
        assert len(_werte(mock_modbus_client, REG_MAX_CHARGE_POWER)) >= 2
        assert len(deckel[:-1]) >= 2
        assert _written(mock_modbus_client, REG_BATTERY_SETPOINT) == []

    async def test_deckel_allein_haelt_den_keepalive(
        self, inverter, mock_modbus_client, monkeypatch
    ):
        monkeypatch.setattr(
            "custom_components.eeg_energy_optimizer.inverter.kostal.KEEPALIVE_INTERVAL",
            0.01,
        )
        _register_lesen(mock_modbus_client, {REG_MAX_DISCHARGE_POWER: 5000.0})
        await inverter.async_set_discharge_limit(0.0)
        assert inverter._active is None
        mock_modbus_client.write_registers.reset_mock()
        await asyncio.sleep(0.6)
        await inverter.async_disconnect()
        deckel = _werte(mock_modbus_client, REG_MAX_DISCHARGE_POWER)
        assert deckel[-1] == 5000
        assert set(deckel[:-1]) <= {0, 1}
        assert len(deckel[:-1]) >= 2

    async def test_stopp_schreibt_1040_zurueck(self, inverter, mock_modbus_client):
        _register_lesen(mock_modbus_client, {REG_MAX_DISCHARGE_POWER: 5000.0})
        await inverter.async_set_discharge_limit(0.5)
        mock_modbus_client.write_registers.reset_mock()
        assert await inverter.async_stop_forcible() is True
        assert _werte(mock_modbus_client, REG_MAX_DISCHARGE_POWER) == [5000]
        # Kein Sollwert „zur Sicherheit" — es wurde nicht entladen.
        assert _written(mock_modbus_client, REG_BATTERY_SETPOINT) == []
        assert inverter._deckel_w is None
        assert inverter._keepalive_task is None

    async def test_entladung_hebt_den_deckel_vorher_auf(
        self, inverter, mock_modbus_client
    ):
        _register_lesen(mock_modbus_client, {REG_MAX_DISCHARGE_POWER: 5000.0})
        await inverter.async_set_discharge_limit(0.0)
        mock_modbus_client.write_registers.reset_mock()
        assert await inverter.async_set_discharge(3.0) is True
        assert _adressen(mock_modbus_client) == [
            REG_MAX_DISCHARGE_POWER, REG_BATTERY_SETPOINT,
        ]
        assert _werte(mock_modbus_client, REG_MAX_DISCHARGE_POWER) == [5000]
        assert inverter._deckel_w is None
        assert inverter._deckel_beruehrt is False
        await inverter.async_disconnect()

    async def test_deckel_waehrend_entladung_wird_nur_gemerkt(
        self, inverter, mock_modbus_client, monkeypatch
    ):
        """Gemerkt waehrend der Entladung, geschrieben erst nach ihrem Ende
        — und dort erst nach dem Watchdog-Rueckfall (Ansfelden-Regel)."""
        from datetime import datetime, timedelta, timezone
        from custom_components.eeg_energy_optimizer.inverter.kostal import (
            RUECKFALL_MAX_S,
        )
        uhr = {"jetzt": datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc)}
        monkeypatch.setattr(
            "custom_components.eeg_energy_optimizer.inverter.kostal._jetzt",
            lambda: uhr["jetzt"],
        )
        monkeypatch.setattr(
            "custom_components.eeg_energy_optimizer.inverter.kostal.RUECKFALL_ABFRAGE_S",
            0.01,
        )
        _register_lesen(
            mock_modbus_client,
            {REG_MAX_DISCHARGE_POWER: 5000.0, REG_MAX_DISCHARGE_BATTERY: 5000.0},
        )
        await inverter.async_set_discharge(3.0)
        mock_modbus_client.write_registers.reset_mock()
        assert await inverter.async_set_discharge_limit(1.0) is True
        assert inverter._deckel_w == pytest.approx(1000.0)
        assert mock_modbus_client.write_registers.await_count == 0

        # Ende der Entladung: Das Ladelimit nullt 1034, der Deckel bleibt
        # gemerkt und wartet wie das Limit auf den Rueckfall.
        await inverter.async_set_charge_limit(2.0)
        await asyncio.sleep(0.1)
        assert inverter._deckel_w == pytest.approx(1000.0)
        assert _werte(mock_modbus_client, REG_MAX_DISCHARGE_POWER) == []
        assert inverter.batterie_gesperrt is True

        uhr["jetzt"] += timedelta(seconds=RUECKFALL_MAX_S + 1)
        await asyncio.sleep(0.3)
        deckel = _werte(mock_modbus_client, REG_MAX_DISCHARGE_POWER)
        ladelimit = _werte(mock_modbus_client, REG_MAX_CHARGE_POWER)
        await inverter.async_disconnect()
        assert deckel and set(deckel) <= {1000, 1001}
        assert ladelimit and set(ladelimit) <= {2000, 2001}
        assert inverter.batterie_gesperrt is False


class TestDeckelNachSollwert:
    """Ansfelden-Regel auch fuer 1040: Nach einem 1034-Sollwert haelt jeder
    Schreibzugriff ihn am Leben — der Deckel wartet wie das Ladelimit."""

    SENSOR = "sensor.kostal_battery_power"

    @pytest.fixture
    def uhr(self, monkeypatch):
        from datetime import datetime, timezone
        t = {"jetzt": datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc)}
        monkeypatch.setattr(
            "custom_components.eeg_energy_optimizer.inverter.kostal._jetzt",
            lambda: t["jetzt"],
        )
        return t

    @pytest.fixture
    def inv(self, mock_hass, kostal_config, mock_modbus_client, uhr):
        mock_hass.states.get = MagicMock(return_value=None)
        inv = KostalInverter(
            mock_hass, {**kostal_config, "battery_power_sensor": self.SENSOR}
        )
        inv._client = mock_modbus_client
        _register_lesen(
            mock_modbus_client, {REG_MAX_DISCHARGE_POWER: 5000.0, REG_SOC: 40.0}
        )
        return inv

    def _messung(self, inv, watt, um):
        state = MagicMock()
        state.state = str(watt)
        state.attributes = {"unit_of_measurement": "W"}
        state.last_reported = um
        state.last_updated = um
        inv._hass.states.get = MagicMock(return_value=state)

    async def test_deckel_nach_entladung_wartet_auf_rueckfall(
        self, inv, mock_modbus_client, uhr, monkeypatch
    ):
        from datetime import timedelta
        monkeypatch.setattr(
            "custom_components.eeg_energy_optimizer.inverter.kostal.RUECKFALL_ABFRAGE_S",
            0.01,
        )
        await inv.async_set_discharge(3.0)
        await inv.async_stop_forcible()
        mock_modbus_client.write_registers.reset_mock()

        assert await inv.async_set_discharge_limit(0.5) is True
        assert inv._deckel_w == pytest.approx(500.0)
        await asyncio.sleep(0.1)
        assert mock_modbus_client.write_registers.await_count == 0

        # Interne Automatik entlaedt wieder: frei.
        uhr["jetzt"] += timedelta(seconds=60)
        self._messung(inv, 1500, uhr["jetzt"])
        await asyncio.sleep(0.5)
        assert inv.batterie_gesperrt is False
        await inv.async_disconnect()
        deckel = _werte(mock_modbus_client, REG_MAX_DISCHARGE_POWER)
        assert deckel[-1] == 5000
        assert deckel[:-1]
        assert set(deckel[:-1]) <= {500, 501}
        assert _written(mock_modbus_client, REG_BATTERY_SETPOINT) == []

    async def test_deckel_nach_netzladung_wartet(self, inv, mock_modbus_client):
        await inv.async_set_grid_charge(2.0, target_soc=80.0)
        await inv.async_stop_forcible()
        mock_modbus_client.write_registers.reset_mock()
        assert await inv.async_set_discharge_limit(0.0) is True
        assert mock_modbus_client.write_registers.await_count == 0
        await inv.async_disconnect()


class TestNetzladung:
    @pytest.fixture
    def lesen(self, mock_modbus_client):
        werte = {
            REG_SOC: 50.0,
            REG_MAX_CHARGE_POWER: 6000.0,
            REG_MAX_DISCHARGE_POWER: 5000.0,
        }
        _register_lesen(mock_modbus_client, werte)
        return werte

    async def test_kann_aus_dem_netz_laden(self, inverter):
        assert inverter.supports_grid_charge is True

    async def test_1034_negativ(self, inverter, mock_modbus_client, lesen):
        assert await inverter.async_set_grid_charge(2.0, target_soc=80.0) is True
        assert _werte(mock_modbus_client, REG_BATTERY_SETPOINT) == [-2000]
        assert inverter._active == ("grid_charge", 2000.0)
        assert inverter._netz_ziel_pct == pytest.approx(80.0)
        await inverter.async_disconnect()

    async def test_ladelimit_vorher_zurueck(self, inverter, mock_modbus_client, lesen):
        """1038 = 0 blockierte die Ladung — erst der Vorwert, dann 1034."""
        await inverter.async_set_charge_limit(0.0)
        mock_modbus_client.write_registers.reset_mock()
        assert await inverter.async_set_grid_charge(2.0, target_soc=80.0) is True
        assert _adressen(mock_modbus_client) == [
            REG_MAX_CHARGE_POWER, REG_BATTERY_SETPOINT,
        ]
        assert _werte(mock_modbus_client, REG_MAX_CHARGE_POWER) == [6000]
        assert inverter._max_charge_pre_block is None
        await inverter.async_disconnect()

    async def test_deckel_vorher_zurueck(self, inverter, mock_modbus_client, lesen):
        await inverter.async_set_discharge_limit(0.0)
        mock_modbus_client.write_registers.reset_mock()
        await inverter.async_set_grid_charge(2.0)
        assert _adressen(mock_modbus_client) == [
            REG_MAX_DISCHARGE_POWER, REG_BATTERY_SETPOINT,
        ]
        assert _werte(mock_modbus_client, REG_MAX_DISCHARGE_POWER) == [5000]
        assert inverter._deckel_w is None
        await inverter.async_disconnect()

    async def test_am_ziel_nur_freigabe(self, inverter, mock_modbus_client, lesen):
        lesen[REG_SOC] = 85.0
        assert await inverter.async_set_grid_charge(2.0, target_soc=80.0) is True
        # Lief keine Netzladung, gibt es auch keinen Sollwert zu nullen.
        assert _written(mock_modbus_client, REG_BATTERY_SETPOINT) == []
        assert inverter._active is None

    async def test_kleine_leistung_ist_freigabe(
        self, inverter, mock_modbus_client, lesen
    ):
        await inverter.async_set_grid_charge(2.0)
        mock_modbus_client.write_registers.reset_mock()
        assert await inverter.async_set_grid_charge(0.01) is True
        assert _werte(mock_modbus_client, REG_BATTERY_SETPOINT) == [0]
        assert inverter._active is None

    async def test_keepalive_haelt_am_ziel_an_und_wartet_dann(
        self, inverter, mock_modbus_client, lesen, monkeypatch
    ):
        monkeypatch.setattr(
            "custom_components.eeg_energy_optimizer.inverter.kostal.KEEPALIVE_INTERVAL",
            0.01,
        )
        await inverter.async_set_grid_charge(2.0, target_soc=80.0)
        await asyncio.sleep(0.5)
        # Unterwegs mit Jitter nachgeschrieben, immer negativ.
        assert set(_werte(mock_modbus_client, REG_BATTERY_SETPOINT)) <= {-2000, -2001}

        mock_modbus_client.write_registers.reset_mock()
        lesen[REG_SOC] = 80.5
        await asyncio.sleep(0.5)
        assert _werte(mock_modbus_client, REG_BATTERY_SETPOINT) == [0]
        assert inverter._active is None
        # Danach gilt die Ansfelden-Regel: ein Ladelimit wird nur vorgemerkt.
        assert inverter._sollwert_seit is not None
        mock_modbus_client.write_registers.reset_mock()
        assert await inverter.async_set_charge_limit(3.0) is True
        assert mock_modbus_client.write_registers.await_count == 0
        await inverter.async_disconnect()

    async def test_stopp_nach_netzladung_nullt_1034(
        self, inverter, mock_modbus_client, lesen
    ):
        await inverter.async_set_grid_charge(2.0, target_soc=80.0)
        mock_modbus_client.write_registers.reset_mock()
        assert await inverter.async_stop_forcible() is True
        assert _werte(mock_modbus_client, REG_BATTERY_SETPOINT) == [0]
        assert inverter._active is None

    async def test_ladelimit_nach_netzladung_nullt_1034(
        self, inverter, mock_modbus_client, lesen
    ):
        await inverter.async_set_grid_charge(2.0)
        mock_modbus_client.write_registers.reset_mock()
        assert await inverter.async_set_charge_limit(3.0) is True
        assert _werte(mock_modbus_client, REG_BATTERY_SETPOINT) == [0]
        # 1038 wartet auf den Rueckfall.
        assert _written(mock_modbus_client, REG_MAX_CHARGE_POWER) == []
        await inverter.async_disconnect()

    async def test_gradientensperre_hat_eigenen_grund(
        self, inverter, mock_modbus_client, lesen
    ):
        async def _write(address, values, **kwargs):
            if address == REG_BATTERY_SETPOINT:
                return _err_response()
            return _ok_response()

        mock_modbus_client.write_registers = AsyncMock(side_effect=_write)
        assert await inverter.async_set_grid_charge(2.0) is False
        assert "Gradient" in inverter.last_write_error
        # Ohne Zahlen — der Grund wird zum message_hash der Telemetrie.
        assert "2000" not in inverter.last_write_error
        assert inverter._active is None
        # Abgelehnt heisst: Es gilt sicher kein Sollwert — 1038/1040 frei.
        assert inverter._sollwert_seit is None
        assert inverter.batterie_gesperrt is False

    async def test_entladung_scheitert_an_der_sperre_mit_demselben_grund(
        self, inverter, mock_modbus_client
    ):
        mock_modbus_client.write_registers = AsyncMock(return_value=_err_response())
        assert await inverter.async_set_discharge(3.0) is False
        assert "Gradient" in inverter.last_write_error

    async def test_nan_wird_nicht_geschrieben(self, inverter, mock_modbus_client):
        assert await inverter.async_set_grid_charge(float("nan")) is False
        assert mock_modbus_client.write_registers.await_count == 0


# ---------------------------------------------------------------------------
# Review November 2026: Rueckfall, Ziel-Hysterese, Vorwert
# ---------------------------------------------------------------------------

_K = "custom_components.eeg_energy_optimizer.inverter.kostal"
_SENSOR = "sensor.kostal_battery_power"


@pytest.fixture
def uhr(monkeypatch):
    from datetime import datetime, timezone
    t = {"jetzt": datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc)}
    monkeypatch.setattr(f"{_K}._jetzt", lambda: t["jetzt"])
    return t


@pytest.fixture
def inv(mock_hass, kostal_config, mock_modbus_client, uhr):
    """Kostal mit Batteriesensor (ohne Messung) und lesbaren Registern."""
    mock_hass.states.get = MagicMock(return_value=None)
    inv = KostalInverter(
        mock_hass, {**kostal_config, "battery_power_sensor": _SENSOR}
    )
    inv._client = mock_modbus_client
    return inv


@pytest.fixture
def register(mock_modbus_client):
    werte = {
        REG_SOC: 50.0,
        REG_MAX_CHARGE_POWER: 6000.0,
        REG_MAX_DISCHARGE_POWER: 5000.0,
        REG_MAX_DISCHARGE_BATTERY: 5000.0,
    }
    _register_lesen(mock_modbus_client, werte)
    return werte


def _messung(inv, watt, um):
    state = MagicMock()
    state.state = str(watt)
    state.attributes = {"unit_of_measurement": "W"}
    state.last_reported = um
    state.last_updated = um
    inv._hass.states.get = MagicMock(return_value=state)


class TestBatterieGesperrt:
    """Nach 1034 = 0 steht die Batterie bis zum Rueckfall. Der Executor
    darf sie dann nicht als Luft der Spitzenkappung rechnen — er fragt
    ``getattr(inverter, "batterie_gesperrt", False)``."""

    async def test_ohne_sollwert_nicht_gesperrt(self, inv, register):
        assert inv.batterie_gesperrt is False
        await inv.async_set_charge_limit(2.0)
        await inv.async_set_discharge_limit(1.0)
        assert inv.batterie_gesperrt is False
        await inv.async_disconnect()

    async def test_nach_entladung_gesperrt_bis_zur_bewegung(
        self, inv, register, uhr
    ):
        from datetime import timedelta
        await inv.async_set_discharge(3.0)
        # Waehrend des eigenen Befehls weiss es der Executor selbst.
        assert inv.batterie_gesperrt is False
        await inv.async_stop_forcible()
        assert inv.batterie_gesperrt is True
        # Der vorgemerkte Deckel meldet Erfolg — die Batterie steht trotzdem.
        assert await inv.async_set_discharge_limit(1.0) is True
        assert inv.batterie_gesperrt is True

        uhr["jetzt"] += timedelta(seconds=60)
        _messung(inv, 1200, uhr["jetzt"])
        assert inv.batterie_gesperrt is False
        await inv.async_disconnect()

    async def test_nach_netzladung_am_ziel_gesperrt(self, inv, register):
        await inv.async_set_grid_charge(2.0, target_soc=80.0)
        register[REG_SOC] = 80.0
        await inv.async_set_grid_charge(2.0, target_soc=80.0)
        assert inv._active is None
        assert inv.batterie_gesperrt is True
        await inv.async_disconnect()


class TestNetzladungZielHysterese:
    """Am Ziel angehalten, im Rueckfall ein Zehntelprozent ans Haus — und
    jede neue Leistung des Executors startete 1034 neu."""

    async def test_knapp_unter_dem_ziel_kein_neustart(
        self, inv, register, mock_modbus_client
    ):
        assert await inv.async_set_grid_charge(2.0, target_soc=80.0) is True
        register[REG_SOC] = 80.2
        assert await inv.async_set_grid_charge(2.5, target_soc=80.0) is True
        assert _werte(mock_modbus_client, REG_BATTERY_SETPOINT) == [-2000, 0]

        mock_modbus_client.write_registers.reset_mock()
        register[REG_SOC] = 79.4
        assert await inv.async_set_grid_charge(2.5, target_soc=80.0) is True
        assert _written(mock_modbus_client, REG_BATTERY_SETPOINT) == []
        assert inv._active is None

        # Erst zwei Prozent darunter gilt es wieder.
        register[REG_SOC] = 77.9
        assert await inv.async_set_grid_charge(2.5, target_soc=80.0) is True
        assert _werte(mock_modbus_client, REG_BATTERY_SETPOINT) == [-2500]
        assert inv._active == ("grid_charge", 2500.0)
        await inv.async_disconnect()

    async def test_unbekannter_ladestand_startet_nicht_neu(
        self, inv, register, mock_modbus_client
    ):
        await inv.async_set_grid_charge(2.0, target_soc=80.0)
        register[REG_SOC] = 80.0
        await inv.async_set_grid_charge(2.0, target_soc=80.0)
        mock_modbus_client.write_registers.reset_mock()
        register[REG_SOC] = None
        assert await inv.async_set_grid_charge(2.0, target_soc=80.0) is True
        assert _written(mock_modbus_client, REG_BATTERY_SETPOINT) == []
        await inv.async_disconnect()

    async def test_anderes_ziel_ist_ein_neuer_befehl(
        self, inv, register, mock_modbus_client
    ):
        await inv.async_set_grid_charge(2.0, target_soc=80.0)
        register[REG_SOC] = 80.0
        await inv.async_set_grid_charge(2.0, target_soc=80.0)
        mock_modbus_client.write_registers.reset_mock()
        register[REG_SOC] = 79.5
        assert await inv.async_set_grid_charge(2.0, target_soc=90.0) is True
        assert _werte(mock_modbus_client, REG_BATTERY_SETPOINT) == [-2000]
        await inv.async_disconnect()

    async def test_entladung_loescht_das_erreichte_ziel(
        self, inv, register, mock_modbus_client
    ):
        await inv.async_set_grid_charge(2.0, target_soc=80.0)
        register[REG_SOC] = 80.0
        await inv.async_set_grid_charge(2.0, target_soc=80.0)
        await inv.async_set_discharge(1.0)
        mock_modbus_client.write_registers.reset_mock()
        register[REG_SOC] = 79.5
        assert await inv.async_set_grid_charge(2.0, target_soc=80.0) is True
        assert _werte(mock_modbus_client, REG_BATTERY_SETPOINT) == [-2000]
        await inv.async_disconnect()

    async def test_keepalive_merkt_das_ziel(
        self, inv, register, mock_modbus_client, monkeypatch
    ):
        monkeypatch.setattr(f"{_K}.KEEPALIVE_INTERVAL", 0.01)
        await inv.async_set_grid_charge(2.0, target_soc=80.0)
        register[REG_SOC] = 80.3
        await asyncio.sleep(0.3)
        assert inv._active is None
        assert inv._netz_ziel_erreicht_pct == pytest.approx(80.0)

        mock_modbus_client.write_registers.reset_mock()
        register[REG_SOC] = 79.6
        assert await inv.async_set_grid_charge(3.0, target_soc=80.0) is True
        assert _written(mock_modbus_client, REG_BATTERY_SETPOINT) == []
        await inv.async_disconnect()


    async def test_ziel_80_auf_80_4_behaelt_die_hysterese(
        self, inv, register, mock_modbus_client
    ):
        """Review-Runde 2: Der Slot-End-SOC ändert sich bei jeder
        Neuplanung um Zehntel. 80,0 erreicht, 80,4 verlangt, SOC 79,5 —
        kein Neustart, und das Flag für den Executor bleibt stehen."""
        await inv.async_set_grid_charge(2.0, target_soc=80.0)
        register[REG_SOC] = 80.0
        await inv.async_set_grid_charge(2.0, target_soc=80.0)
        assert inv.netzladung_am_ziel is True
        mock_modbus_client.write_registers.reset_mock()
        register[REG_SOC] = 79.5
        assert await inv.async_set_grid_charge(2.0, target_soc=80.4) is True
        assert _written(mock_modbus_client, REG_BATTERY_SETPOINT) == []
        assert inv._netz_ziel_erreicht_pct == pytest.approx(80.0)
        assert inv.netzladung_am_ziel is True
        await inv.async_disconnect()

    async def test_stopp_und_ladelimit_loeschen_das_ziel(
        self, inv, register, mock_modbus_client
    ):
        """Sonst begänne die Netzladung bei unlesbarem Ladestand nie wieder."""
        await inv.async_set_grid_charge(2.0, target_soc=80.0)
        register[REG_SOC] = 80.0
        await inv.async_set_grid_charge(2.0, target_soc=80.0)
        assert inv.netzladung_am_ziel is True
        await inv.async_stop_forcible()
        assert inv.netzladung_am_ziel is False

        await inv.async_set_grid_charge(2.0, target_soc=80.0)
        assert inv.netzladung_am_ziel is True
        await inv.async_set_charge_limit(1.0)
        assert inv.netzladung_am_ziel is False
        await inv.async_disconnect()

    async def test_flag_aus_waehrend_die_netzladung_laeuft(
        self, inv, register, mock_modbus_client
    ):
        await inv.async_set_grid_charge(2.0, target_soc=80.0)
        assert inv.netzladung_am_ziel is False
        await inv.async_disconnect()

class TestVorwert1040:
    """Nach einem HA-Neustart kann ein alter Deckel in 1040 stehen."""

    async def test_1078_unlesbar_1040_unter_batterieleistung_ist_kein_vorwert(
        self, inverter, mock_modbus_client
    ):
        """Review-Runde 2: 1078 None, 1040 = 500 W — ohne den Massstab der
        Batterie wäre ein stehender alter Deckel wieder Vorwert und Grenze.
        Unter der eingestellten Batterieleistung zählt er als keins von
        beidem."""
        inverter._config[CONF_DISCHARGE_POWER_KW] = 5.0
        _register_lesen(
            mock_modbus_client,
            {REG_MAX_DISCHARGE_POWER: 500.0, REG_MAX_DISCHARGE_BATTERY: None},
        )
        await inverter.async_set_discharge_limit(1.0)
        assert inverter._max_discharge_pre_deckel is None
        assert inverter._max_discharge_hw is None

    async def test_1078_unlesbar_1040_ueber_batterieleistung_bleibt(
        self, inverter, mock_modbus_client
    ):
        inverter._config[CONF_DISCHARGE_POWER_KW] = 5.0
        _register_lesen(
            mock_modbus_client,
            {REG_MAX_DISCHARGE_POWER: 6000.0, REG_MAX_DISCHARGE_BATTERY: None},
        )
        await inverter.async_set_discharge_limit(1.0)
        assert inverter._max_discharge_pre_deckel == pytest.approx(6000.0)

    async def test_ladelimit_ersatz_nimmt_das_groessere(
        self, inverter, mock_modbus_client
    ):
        """1076 kann gedrosselt sein — das Größere aus 1076 und der
        eingestellten Leistung."""
        inverter._config[CONF_DISCHARGE_POWER_KW] = 5.0
        _register_lesen(mock_modbus_client, {REG_MAX_CHARGE_BATTERY: 1200.0})
        assert await inverter._ladelimit_ersatz_w() == pytest.approx(5000.0)

    async def test_alter_deckel_ist_kein_vorwert(
        self, inverter, mock_modbus_client
    ):
        _register_lesen(
            mock_modbus_client,
            {REG_MAX_DISCHARGE_POWER: 500.0, REG_MAX_DISCHARGE_BATTERY: 5000.0},
        )
        await inverter.async_set_discharge_limit(1.0)
        assert inverter._max_discharge_pre_deckel == pytest.approx(5000.0)
        assert inverter.get_max_discharge_power_kw() == pytest.approx(5.0)
        assert await inverter.async_set_discharge_limit(None) is True
        assert _werte(mock_modbus_client, REG_MAX_DISCHARGE_POWER) == [1000, 5000]

    async def test_hoeherer_vorwert_bleibt_grenze_ist_1078(
        self, inverter, mock_modbus_client
    ):
        _register_lesen(
            mock_modbus_client,
            {REG_MAX_DISCHARGE_POWER: 6000.0, REG_MAX_DISCHARGE_BATTERY: 5000.0},
        )
        await inverter.async_set_discharge_limit(1.0)
        assert inverter._max_discharge_pre_deckel == pytest.approx(6000.0)
        assert inverter.get_max_discharge_power_kw() == pytest.approx(5.0)
        await inverter.async_set_discharge_limit(None)
        assert _werte(mock_modbus_client, REG_MAX_DISCHARGE_POWER) == [1000, 6000]

    async def test_steuerwerte_nehmen_1078_als_grenze(
        self, inverter, mock_modbus_client
    ):
        _register_lesen(
            mock_modbus_client,
            {REG_MAX_DISCHARGE_POWER: 500.0, REG_MAX_DISCHARGE_BATTERY: 4800.0},
        )
        await inverter.async_get_control_values()
        assert inverter.get_max_discharge_power_kw() == pytest.approx(4.8)


class TestSollwertAblehnung:
    async def test_ablehnung_sperrt_die_register_nicht(
        self, inv, register, mock_modbus_client
    ):
        """Gradientensperre: 1034 abgelehnt, also gilt kein Sollwert — ein
        Ladelimit danach wird sofort geschrieben, nicht vorgemerkt."""
        async def _write(address, values, **kwargs):
            if address == REG_BATTERY_SETPOINT:
                return _err_response()
            return _ok_response()

        mock_modbus_client.write_registers = AsyncMock(side_effect=_write)
        assert await inv.async_set_discharge(3.0) is False
        assert inv._sollwert_seit is None
        assert await inv.async_set_charge_limit(2.0) is True
        assert _werte(mock_modbus_client, REG_MAX_CHARGE_POWER) == [2000]
        await inv.async_disconnect()

    async def test_ablehnung_laesst_einen_geltenden_sollwert_stehen(
        self, inv, register, mock_modbus_client
    ):
        await inv.async_set_discharge(3.0)
        seit = inv._sollwert_seit
        mock_modbus_client.write_registers = AsyncMock(return_value=_err_response())
        assert await inv.async_stop_forcible() is False
        assert inv._sollwert_seit == seit
        await inv.async_disconnect()

    async def test_verbindungsabbruch_merkt_den_sollwert(
        self, inv, register, mock_modbus_client
    ):
        mock_modbus_client.write_registers = AsyncMock(
            side_effect=ConnectionError("weg")
        )
        assert await inv.async_set_discharge(3.0) is False
        # Ob der Wert ankam, ist offen — lieber auf den Rueckfall warten.
        assert inv._sollwert_seit is not None


class TestLadelimitErsatz:
    """Netzladung nach einem Ladelimit ohne gesicherten 1038-Vorwert."""

    async def _limit_ohne_vorwert(self, inv, client, werte):
        werte[REG_MAX_CHARGE_POWER] = None
        _register_lesen(client, werte)
        await inv.async_set_charge_limit(0.0)
        assert inv._max_charge_pre_block is None
        assert inv._max_charge_hw is None
        client.write_registers.reset_mock()

    async def test_1076_der_batterie(self, inverter, mock_modbus_client):
        werte = {REG_SOC: 50.0, 1076: 7000.0}
        await self._limit_ohne_vorwert(inverter, mock_modbus_client, werte)
        assert await inverter.async_set_grid_charge(2.0, target_soc=80.0) is True
        assert _werte(mock_modbus_client, REG_MAX_CHARGE_POWER) == [7000]
        assert _werte(mock_modbus_client, REG_BATTERY_SETPOINT) == [-2000]
        await inverter.async_disconnect()

    async def test_eingestellte_batterieleistung(
        self, mock_hass, kostal_config, mock_modbus_client
    ):
        inv = KostalInverter(
            mock_hass, {**kostal_config, "discharge_power_kw": 4.0}
        )
        inv._client = mock_modbus_client
        werte = {REG_SOC: 50.0, 1076: None}
        await self._limit_ohne_vorwert(inv, mock_modbus_client, werte)
        assert await inv.async_set_grid_charge(2.0, target_soc=80.0) is True
        assert _werte(mock_modbus_client, REG_MAX_CHARGE_POWER) == [4000]
        await inv.async_disconnect()

    async def test_ohne_jeden_wert_fehler_mit_grund(
        self, inverter, mock_modbus_client
    ):
        werte = {REG_SOC: 50.0, 1076: None}
        await self._limit_ohne_vorwert(inverter, mock_modbus_client, werte)
        assert await inverter.async_set_grid_charge(2.0) is False
        assert inverter.last_write_error == "Ladelimit-Vorwert unbekannt"
        await inverter.async_disconnect()


class TestDisconnectDeckel:
    async def _deckel_bleibt_beruehrt(self, inv, client):
        """Deckel gesetzt, Zuruecksetzen vor der Entladung scheitert, Stopp
        im Rueckfall delegiert es an den Keepalive."""
        await inv.async_set_discharge_limit(1.0)

        async def _write(address, values, **kwargs):
            if address == REG_MAX_DISCHARGE_POWER:
                return _err_response()
            return _ok_response()

        client.write_registers = AsyncMock(side_effect=_write)
        await inv.async_set_discharge(3.0)
        assert inv._deckel_beruehrt is True
        assert await inv.async_stop_forcible() is True
        assert inv.batterie_gesperrt is True
        client.write_registers = AsyncMock(return_value=_ok_response())

    async def test_ohne_rueckfall_wird_zurueckgesetzt(
        self, inv, register, mock_modbus_client
    ):
        await inv.async_set_discharge_limit(1.0)
        mock_modbus_client.write_registers.reset_mock()
        await inv.async_disconnect()
        assert _werte(mock_modbus_client, REG_MAX_DISCHARGE_POWER) == [5000]
        assert inv._deckel_beruehrt is False

    async def test_im_rueckfall_nichts_schreiben_aber_laut(
        self, inv, register, mock_modbus_client, caplog
    ):
        await self._deckel_bleibt_beruehrt(inv, mock_modbus_client)
        await inv.async_disconnect()
        assert mock_modbus_client.write_registers.await_count == 0
        assert "bleibt beim Entladen" in caplog.text

    async def test_nach_dem_rueckfall_zurueckgesetzt(
        self, inv, register, mock_modbus_client, uhr
    ):
        from datetime import timedelta
        await self._deckel_bleibt_beruehrt(inv, mock_modbus_client)
        inv._cancel_keepalive()
        uhr["jetzt"] += timedelta(seconds=60)
        _messung(inv, -1500, uhr["jetzt"])
        await inv.async_disconnect()
        assert _werte(mock_modbus_client, REG_MAX_DISCHARGE_POWER) == [5000]
        assert _written(mock_modbus_client, REG_BATTERY_SETPOINT) == []


class TestVorgemerktIstErfolg:
    """Ein vorgemerkter Befehl ist ein angenommener — ein alter Fehlergrund
    darf nicht stehen bleiben (er landete sonst in der Telemetrie)."""

    async def test_ladelimit_und_deckel_vorgemerkt(self, inv, register):
        await inv.async_set_discharge(3.0)
        await inv.async_stop_forcible()
        inv.last_write_error = "alt"
        assert await inv.async_set_charge_limit(2.0) is True
        assert inv.last_write_error is None
        inv.last_write_error = "alt"
        assert await inv.async_set_discharge_limit(1.0) is True
        assert inv.last_write_error is None
        await inv.async_disconnect()

    async def test_aufheben_vorgemerkt(self, inv, register, mock_modbus_client):
        await TestDisconnectDeckel()._deckel_bleibt_beruehrt(inv, mock_modbus_client)
        inv.last_write_error = "alt"
        assert await inv.async_set_discharge_limit(None) is True
        assert inv.last_write_error is None
        assert mock_modbus_client.write_registers.await_count == 0
        inv._cancel_keepalive()
