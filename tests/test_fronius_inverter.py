"""Tests for Fronius Gen24 inverter implementation (SunSpec Model 124 Modbus TCP)."""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.eeg_energy_optimizer.inverter.base import InverterBase
from custom_components.eeg_energy_optimizer.inverter.fronius import (
    FroniusInverter,
    _MINRSV_SAFETY_MARGIN_PCT,
    _OFFSET_INWRTE,
    _OFFSET_MINRSVPCT,
    _OFFSET_OUTWRTE,
    _OFFSET_RVRTTMS,
    _OFFSET_STORCTL_MOD,
    _OFFSET_WCHAMAX,
    _OFFSET_WINTMS,
    _KEEPALIVE_INTERVAL_SECONDS,
    _RVRTTMS_SECONDS,
    _SF_DEFAULT_INOUTWRTE,
    _SF_DEFAULT_WCHAMAX,
    _SUNSPEC_END_MARKER,
    _SUNSPEC_ID_WORD0,
    _SUNSPEC_ID_WORD1,
    _SUNSPEC_MODEL_124,
    _WCHAMAX_SANITY_LIMIT,
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
def fronius_config():
    return {
        "fronius_modbus_host": "192.168.1.100",
        "fronius_modbus_port": 502,
    }


@pytest.fixture
def mock_modbus_client():
    """Mocked AsyncModbusTcpClient with success responses by default."""
    client = MagicMock()
    client.connected = True
    client.write_register = AsyncMock(return_value=_ok_response())
    client.read_holding_registers = AsyncMock(return_value=_ok_response([0]))
    client.connect = AsyncMock(return_value=True)
    client.close = MagicMock()
    return client


class _NoopStore:
    """In-memory Store-Ersatz für Tests, die Storage nicht prüfen."""

    def __init__(self):
        self.saved: list[dict] = []
        self._data: dict = {}

    async def async_load(self):
        return self._data

    async def async_save(self, data):
        self._data = dict(data)
        self.saved.append(dict(data))


def _install_noop_store(inv: FroniusInverter) -> _NoopStore:
    noop = _NoopStore()
    inv._state_store._store = noop
    inv._state_store._data = {}
    inv._state_store._loaded = False
    return noop


@pytest.fixture
def inverter(mock_hass, fronius_config, mock_modbus_client):
    """FroniusInverter with mocked client and pre-discovered Model 124 base."""
    inv = FroniusInverter(mock_hass, fronius_config)
    inv._client = mock_modbus_client
    inv._model124_base = 40070
    inv._wchamax = 5000  # 5 kW residential battery
    inv._wchamax_date = date.today().isoformat()
    _install_noop_store(inv)
    return inv


class TestFroniusInverterBase:
    """Verify FroniusInverter conforms to the InverterBase contract."""

    def test_is_instance_of_inverter_base(self, inverter):
        assert isinstance(inverter, InverterBase)

    def test_is_subclass_of_inverter_base(self):
        assert issubclass(FroniusInverter, InverterBase)


class TestConfigParsing:
    """Host and port are read from config (panel-driven)."""

    def test_host_from_config(self, mock_hass):
        inv = FroniusInverter(mock_hass, {"fronius_modbus_host": "10.0.0.5"})
        assert inv._host == "10.0.0.5"

    def test_default_port(self, mock_hass):
        inv = FroniusInverter(mock_hass, {})
        assert inv._port == 502

    def test_custom_port(self, mock_hass):
        inv = FroniusInverter(mock_hass, {"fronius_modbus_port": 1502})
        assert inv._port == 1502

    def test_string_port_coerced(self, mock_hass):
        """Port may arrive as a string from JSON-loaded config — must coerce."""
        inv = FroniusInverter(mock_hass, {"fronius_modbus_port": "503"})
        assert inv._port == 503


class TestAsyncSetChargeLimit:
    """Block / partial charge limit via InWRte (offset 13) + StorCtl_Mod (offset 3)."""

    async def test_block_charging_writes_inwrte_then_mode(self, inverter, mock_modbus_client):
        """power_kw=0 → InWRte=0, WinTms=0, RvrtTms=fallback, then StorCtl_Mod=1.

        Order matters: if mode flips on with a stale 100% rate value, the
        inverter would silently keep charging. WinTms must be 0 so the limit
        takes effect immediately; RvrtTms carries the fallback time that
        ends the block by itself if Home Assistant stops talking.
        """
        result = await inverter.async_set_charge_limit(0)
        assert result is True

        calls = mock_modbus_client.write_register.call_args_list
        assert len(calls) == 4
        base = inverter._model124_base
        # 1. InWRte = 0
        assert (calls[0].kwargs["address"], calls[0].kwargs["value"]) == (base + _OFFSET_INWRTE, 0)
        # 2. WinTms = 0 (immediate effect)
        assert (calls[1].kwargs["address"], calls[1].kwargs["value"]) == (base + _OFFSET_WINTMS, 0)
        # 3. RvrtTms = fallback time (inverter-side failsafe)
        assert (calls[2].kwargs["address"], calls[2].kwargs["value"]) == (
            base + _OFFSET_RVRTTMS, _RVRTTMS_SECONDS
        )
        # 4. StorCtl_Mod = 1 (Charge Limit active)
        assert (calls[3].kwargs["address"], calls[3].kwargs["value"]) == (base + _OFFSET_STORCTL_MOD, 1)

    async def test_partial_charge_uses_wchamax_percentage(
        self, inverter, mock_modbus_client
    ):
        """power_kw=2.5 with WChaMax=5000W → 50% → 5000 (SF -2 = 100×percent)."""
        result = await inverter.async_set_charge_limit(2.5)
        assert result is True
        calls = mock_modbus_client.write_register.call_args_list
        assert calls[0].kwargs["value"] == 5000  # 50% in SF -2 encoding

    async def test_partial_charge_clamped_to_100pct(self, inverter, mock_modbus_client):
        """power exceeding WChaMax is clamped to 100% (10000)."""
        result = await inverter.async_set_charge_limit(10.0)  # WChaMax=5kW
        assert result is True
        calls = mock_modbus_client.write_register.call_args_list
        assert calls[0].kwargs["value"] == 10000

    async def test_returns_false_on_write_error(self, inverter, mock_modbus_client):
        mock_modbus_client.write_register = AsyncMock(return_value=_err_response())
        result = await inverter.async_set_charge_limit(0)
        assert result is False

    async def test_returns_false_on_exception(self, inverter, mock_modbus_client):
        mock_modbus_client.write_register = AsyncMock(side_effect=Exception("boom"))
        result = await inverter.async_set_charge_limit(0)
        assert result is False
        # Client closed on error so next call reconnects
        assert inverter._client is None

    async def test_returns_false_when_wchamax_unknown(
        self, inverter, mock_modbus_client
    ):
        """Without a known WChaMax the percentage scaling cannot be computed."""
        inverter._wchamax = None
        inverter._wchamax_date = None
        # Re-read returns out-of-range value → still rejected
        bad = _ok_response([_WCHAMAX_SANITY_LIMIT + 1])
        mock_modbus_client.read_holding_registers = AsyncMock(return_value=bad)
        result = await inverter.async_set_charge_limit(0)
        assert result is False


class TestAsyncSetDischarge:
    """Force discharge via InWRte=0, OutWRte=%, optional MinRsvPct, then StorCtl_Mod=3."""

    async def test_discharge_with_target_soc_writes_full_sequence(
        self, inverter, mock_modbus_client
    ):
        """power_kw=2.5, target_soc=15 → InWRte=-5000 (forced discharge),
        OutWRte=+5000, MinRsvPct=1000 (Ziel − 5 % Sicherheitsabstand),
        WinTms=0, RvrtTms=fallback, StorCtl_Mod=3.

        InWRte is the negative of the discharge percent (in two's-complement
        16-bit), per Fronius Modbus manual example 6 — see _set_discharge_locked.
        MinRsvPct liegt bewusst UNTER dem Ziel-SOC (_MINRSV_SAFETY_MARGIN_PCT),
        damit der Optimizer die Entladung an seiner Austrittsschwelle beendet
        und der Fronius nicht vorher am Floor einfriert ("Minimum SOC").
        """
        # Pre-discharge MinRsvPct read returns 500 (5%)
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response([500])
        )
        result = await inverter.async_set_discharge(2.5, target_soc=15)
        assert result is True

        calls = mock_modbus_client.write_register.call_args_list
        assert len(calls) == 6
        base = inverter._model124_base
        # -5000 as unsigned 16-bit = 60536
        assert (calls[0].kwargs["address"], calls[0].kwargs["value"]) == (base + _OFFSET_INWRTE, (-5000) & 0xFFFF)
        assert (calls[1].kwargs["address"], calls[1].kwargs["value"]) == (base + _OFFSET_OUTWRTE, 5000)
        assert (calls[2].kwargs["address"], calls[2].kwargs["value"]) == (
            base + _OFFSET_MINRSVPCT,
            int((15 - _MINRSV_SAFETY_MARGIN_PCT) * 100),  # 1000 = 10%
        )
        assert (calls[3].kwargs["address"], calls[3].kwargs["value"]) == (base + _OFFSET_WINTMS, 0)
        assert (calls[4].kwargs["address"], calls[4].kwargs["value"]) == (
            base + _OFFSET_RVRTTMS, _RVRTTMS_SECONDS
        )
        assert (calls[5].kwargs["address"], calls[5].kwargs["value"]) == (base + _OFFSET_STORCTL_MOD, 3)
        # Pre-discharge MinRsvPct cached for later restore
        assert inverter._minrsvpct_pre_discharge == 500

    async def test_discharge_without_target_soc_skips_minrsvpct(
        self, inverter, mock_modbus_client
    ):
        result = await inverter.async_set_discharge(2.5)
        assert result is True
        calls = mock_modbus_client.write_register.call_args_list
        assert len(calls) == 5
        base = inverter._model124_base
        offsets = [c.kwargs["address"] - base for c in calls]
        assert _OFFSET_MINRSVPCT not in offsets
        assert inverter._minrsvpct_pre_discharge is None

    async def test_discharge_clamps_to_100pct(self, inverter, mock_modbus_client):
        """Power exceeding WChaMax clamps to 100% (10000)."""
        result = await inverter.async_set_discharge(10.0)  # WChaMax=5kW
        assert result is True
        calls = mock_modbus_client.write_register.call_args_list
        # OutWRte is the second write
        assert calls[1].kwargs["value"] == 10000

    async def test_storctl_mod_written_last(self, inverter, mock_modbus_client):
        """Mode bit must flip on AFTER all rate registers are in place."""
        await inverter.async_set_discharge(2.5)
        calls = mock_modbus_client.write_register.call_args_list
        assert calls[-1].kwargs["address"] == inverter._model124_base + _OFFSET_STORCTL_MOD
        assert calls[-1].kwargs["value"] == 3

    async def test_returns_false_on_exception(self, inverter, mock_modbus_client):
        mock_modbus_client.write_register = AsyncMock(side_effect=Exception("boom"))
        result = await inverter.async_set_discharge(2.5)
        assert result is False

    async def test_minrsvpct_snapshot_failure_is_non_critical(
        self, inverter, mock_modbus_client
    ):
        """Failure to snapshot MinRsvPct must not abort the discharge."""
        mock_modbus_client.read_holding_registers = AsyncMock(
            side_effect=Exception("read failed")
        )
        result = await inverter.async_set_discharge(2.5, target_soc=15)
        assert result is True
        # Snapshot stays None — restore on stop will be skipped
        assert inverter._minrsvpct_pre_discharge is None


class TestMinRsvPctSafetyMargin:
    """Der SOC-Floor liegt _MINRSV_SAFETY_MARGIN_PCT unter dem Ziel-SOC.

    Regression für den Nacht-Entladungs-Deadlock: Floor == Ziel-SOC ließ den
    Fronius die Entladung selbst stoppen ("Minimum SOC"), der SOC konnte die
    Optimizer-Austrittsschwelle (Ziel − 2 %) nie erreichen, der erzwungene
    Entlademodus blieb die ganze Nacht aktiv und das Haus zog aus dem Netz.
    """

    async def test_floor_is_margin_below_target(self, inverter, mock_modbus_client):
        await inverter.async_set_discharge(2.5, target_soc=70)
        calls = mock_modbus_client.write_register.call_args_list
        base = inverter._model124_base
        minrsv_writes = [
            c.kwargs["value"]
            for c in calls
            if c.kwargs["address"] == base + _OFFSET_MINRSVPCT
        ]
        assert minrsv_writes == [int((70 - _MINRSV_SAFETY_MARGIN_PCT) * 100)]

    async def test_floor_clamped_to_zero(self, inverter, mock_modbus_client):
        """target_soc unterhalb des Abstands darf keinen negativen Floor ergeben."""
        await inverter.async_set_discharge(2.5, target_soc=3)
        calls = mock_modbus_client.write_register.call_args_list
        base = inverter._model124_base
        minrsv_writes = [
            c.kwargs["value"]
            for c in calls
            if c.kwargs["address"] == base + _OFFSET_MINRSVPCT
        ]
        assert minrsv_writes == [0]

    def test_margin_exceeds_historic_exit_hysteresis(self):
        """Der Abstand muss größer bleiben als die historische Austritts-
        Hysterese der alten Zustands-Heuristik (RESERVE_EXIT_HYSTERESIS_PCT
        = 2 %, mit dem Umbau auf den Fahrplan entfallen). Der Fronius-Floor
        selbst bleibt unverändert — ein zu kleiner Abstand würde denselben
        Deadlock erzeugen, sobald ein Aufrufer knapp über dem Floor stoppt."""
        assert _MINRSV_SAFETY_MARGIN_PCT > 2


class TestMinRsvPctStorePersistence:
    """Pre-Discharge-MinRsvPct überlebt HA-Neustarts via FroniusStateStore."""

    async def test_snapshot_saved_to_store(self, inverter, mock_modbus_client):
        """Erster Entladestart persistiert den gelesenen Vorwert im Store."""
        noop = _install_noop_store(inverter)
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response([500])
        )
        await inverter.async_set_discharge(2.5, target_soc=15)
        assert noop.saved == [{"minrsvpct_original": 500}]

    async def test_snapshot_from_store_after_restart(
        self, inverter, mock_modbus_client
    ):
        """Nach HA-Neustart mitten in der Entladung: Store-Wert hat Vorrang
        vor dem Register-Read — das Register enthält bereits unseren
        abgesenkten Floor (Snapshot-Vergiftung vermeiden)."""
        noop = _install_noop_store(inverter)
        noop._data = {"minrsvpct_original": 500}
        await inverter.async_set_discharge(2.5, target_soc=15)
        # Vorwert aus dem Store übernommen, kein Register-Read nötig
        assert inverter._minrsvpct_pre_discharge == 500
        mock_modbus_client.read_holding_registers.assert_not_called()

    async def test_stop_forcible_restores_from_store_after_restart(
        self, inverter, mock_modbus_client
    ):
        """Neustart-Szenario: RAM-Cache leer, Store hält den Vorwert —
        stop_forcible restauriert aus dem Store und löscht ihn danach."""
        noop = _install_noop_store(inverter)
        noop._data = {"minrsvpct_original": 500}
        assert inverter._minrsvpct_pre_discharge is None

        result = await inverter.async_stop_forcible()
        assert result is True
        calls = mock_modbus_client.write_register.call_args_list
        assert (calls[3].kwargs["address"], calls[3].kwargs["value"]) == (
            inverter._model124_base + _OFFSET_MINRSVPCT,
            500,
        )
        # Store-Eintrag nach erfolgreichem Restore gelöscht
        assert noop.saved[-1] == {}

    async def test_store_cleared_after_successful_restore(
        self, inverter, mock_modbus_client
    ):
        """Regulärer Zyklus: Snapshot → Restore → Store wieder leer."""
        noop = _install_noop_store(inverter)
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response([500])
        )
        await inverter.async_set_discharge(2.5, target_soc=15)
        assert noop._data.get("minrsvpct_original") == 500

        await inverter.async_stop_forcible()
        assert "minrsvpct_original" not in noop._data
        assert inverter._minrsvpct_pre_discharge is None


class TestAsyncStopForcible:
    """Stop writes StorCtl_Mod=0, then InWRte/OutWRte=10000, plus optional MinRsvPct restore."""

    async def test_stop_forcible_basic_sequence(self, inverter, mock_modbus_client):
        """Without cached MinRsvPct: 3 writes — mode off, charge 100%, discharge 100%."""
        result = await inverter.async_stop_forcible()
        assert result is True
        calls = mock_modbus_client.write_register.call_args_list
        assert len(calls) == 3
        base = inverter._model124_base
        assert (calls[0].kwargs["address"], calls[0].kwargs["value"]) == (base + _OFFSET_STORCTL_MOD, 0)
        assert (calls[1].kwargs["address"], calls[1].kwargs["value"]) == (base + _OFFSET_INWRTE, 10000)
        assert (calls[2].kwargs["address"], calls[2].kwargs["value"]) == (base + _OFFSET_OUTWRTE, 10000)

    async def test_stop_forcible_restores_minrsvpct(self, inverter, mock_modbus_client):
        """A cached pre-discharge reserve is restored on stop, then cleared."""
        inverter._minrsvpct_pre_discharge = 500  # 5%
        result = await inverter.async_stop_forcible()
        assert result is True
        calls = mock_modbus_client.write_register.call_args_list
        assert len(calls) == 4
        assert (calls[3].kwargs["address"], calls[3].kwargs["value"]) == (
            inverter._model124_base + _OFFSET_MINRSVPCT,
            500,
        )
        assert inverter._minrsvpct_pre_discharge is None

    async def test_stop_forcible_keeps_minrsvpct_on_failed_restore(
        self, inverter, mock_modbus_client
    ):
        """Failed restore keeps the cached value and returns False.

        False → der Optimizer markiert den Stop nicht als erledigt und
        wiederholt ihn im nächsten Zyklus (statt die erhöhte Reserve bis
        zum nächsten Zustandswechsel stehen zu lassen).
        """
        inverter._minrsvpct_pre_discharge = 500
        responses = [
            _ok_response(),    # StorCtl_Mod=0
            _ok_response(),    # InWRte=10000
            _ok_response(),    # OutWRte=10000
            _err_response(),   # MinRsvPct restore — fails
        ]
        mock_modbus_client.write_register = AsyncMock(side_effect=responses)
        result = await inverter.async_stop_forcible()
        assert result is False
        # Cache retained for retry on the next stop_forcible
        assert inverter._minrsvpct_pre_discharge == 500

    async def test_returns_false_on_exception(self, inverter, mock_modbus_client):
        mock_modbus_client.write_register = AsyncMock(side_effect=Exception("boom"))
        result = await inverter.async_stop_forcible()
        assert result is False


class TestIsAvailable:
    """is_available reflects whether a host is configured.

    Fronius opens the Modbus TCP connection lazily, so checking the live
    socket state would falsely report unavailable before the first
    operation runs. As long as a host is configured, the inverter is
    considered available — the actual TCP probe happens in
    _ensure_connected when an operation runs.
    """

    def test_available_when_host_configured(self, inverter, mock_modbus_client):
        mock_modbus_client.connected = True
        assert inverter.is_available is True

    def test_available_even_when_socket_disconnected(self, inverter, mock_modbus_client):
        """Disconnected socket still reports available — reconnect happens lazily."""
        mock_modbus_client.connected = False
        assert inverter.is_available is True

    def test_available_without_client(self, mock_hass, fronius_config):
        """No client yet, but host configured → available (lazy connect)."""
        inv = FroniusInverter(mock_hass, fronius_config)
        assert inv.is_available is True

    def test_unavailable_when_no_host(self, mock_hass):
        """Without a configured host, the inverter cannot be reached."""
        inv = FroniusInverter(mock_hass, {})
        assert inv.is_available is False


class TestAsyncDisconnect:
    """async_disconnect is called from async_unload_entry to free the TCP socket."""

    async def test_disconnect_closes_client(self, inverter, mock_modbus_client):
        await inverter.async_disconnect()
        mock_modbus_client.close.assert_called_once()
        assert inverter._client is None

    async def test_disconnect_when_no_client(self, mock_hass, fronius_config):
        inv = FroniusInverter(mock_hass, fronius_config)
        # Must not raise even though no client was ever created
        await inv.async_disconnect()


class TestRegisterWriteCounter:
    """Each successful Modbus write increments register_writes."""

    async def test_counter_increments_per_write(self, inverter, mock_modbus_client):
        before = inverter.register_writes
        await inverter.async_set_charge_limit(0)
        # set_charge_limit writes 4 registers: InWRte, WinTms, RvrtTms, StorCtl_Mod
        assert inverter.register_writes == before + 4

    async def test_failed_write_does_not_increment(
        self, inverter, mock_modbus_client
    ):
        before = inverter.register_writes
        mock_modbus_client.write_register = AsyncMock(return_value=_err_response())
        await inverter.async_set_charge_limit(0)
        assert inverter.register_writes == before


class TestWChaMaxSanityCheck:
    """Implausible WChaMax values are rejected so percentage scaling stays correct."""

    async def test_zero_wchamax_rejected(self, inverter, mock_modbus_client):
        inverter._wchamax = None
        inverter._wchamax_date = None
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response([0])
        )
        result = await inverter._read_wchamax()
        assert result is None
        assert inverter._wchamax is None  # not cached

    async def test_oversized_wchamax_rejected(self, inverter, mock_modbus_client):
        inverter._wchamax = None
        inverter._wchamax_date = None
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response([_WCHAMAX_SANITY_LIMIT + 1])
        )
        result = await inverter._read_wchamax()
        assert result is None
        assert inverter._wchamax is None

    async def test_plausible_wchamax_cached(self, inverter, mock_modbus_client):
        inverter._wchamax = None
        inverter._wchamax_date = None
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response([4500])
        )
        result = await inverter._read_wchamax()
        assert result == 4500
        assert inverter._wchamax == 4500
        assert inverter._wchamax_date == date.today().isoformat()

    async def test_wchamax_cache_returns_without_io(
        self, inverter, mock_modbus_client
    ):
        """Cached WChaMax is reused on the same day — no Modbus reads issued."""
        inverter._wchamax = 4500
        inverter._wchamax_date = date.today().isoformat()
        mock_modbus_client.read_holding_registers = AsyncMock()
        result = await inverter._read_wchamax()
        assert result == 4500
        mock_modbus_client.read_holding_registers.assert_not_called()


class TestSunSpecDiscovery:
    """SunSpec model table walk from register 40000 finds Model 124."""

    async def test_discovers_model124_first_in_table(
        self, mock_hass, fronius_config, mock_modbus_client
    ):
        """Model 124 sits directly after the SunSpec ID."""
        responses = [
            _ok_response([_SUNSPEC_ID_WORD0, _SUNSPEC_ID_WORD1]),  # 40000
            _ok_response([_SUNSPEC_MODEL_124, 24]),                # 40002 header
        ]
        mock_modbus_client.read_holding_registers = AsyncMock(side_effect=responses)
        inv = FroniusInverter(mock_hass, fronius_config)
        inv._client = mock_modbus_client
        result = await inv._discover_model124()
        assert result is True
        # Data starts after 2-register header at 40002
        assert inv._model124_base == 40004

    async def test_aborts_on_invalid_sunspec_id(
        self, mock_hass, fronius_config, mock_modbus_client
    ):
        """Wrong magic ID at 40000 aborts immediately."""
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response([0x0000, 0x0000])
        )
        inv = FroniusInverter(mock_hass, fronius_config)
        inv._client = mock_modbus_client
        result = await inv._discover_model124()
        assert result is False
        assert inv._model124_base is None

    async def test_aborts_on_end_marker(
        self, mock_hass, fronius_config, mock_modbus_client
    ):
        """Hitting 0xFFFF before Model 124 returns False."""
        responses = [
            _ok_response([_SUNSPEC_ID_WORD0, _SUNSPEC_ID_WORD1]),
            _ok_response([1, 50]),                          # Model 1, length 50
            _ok_response([_SUNSPEC_END_MARKER, 0]),         # end of table
        ]
        mock_modbus_client.read_holding_registers = AsyncMock(side_effect=responses)
        inv = FroniusInverter(mock_hass, fronius_config)
        inv._client = mock_modbus_client
        result = await inv._discover_model124()
        assert result is False

    async def test_iterates_past_other_models(
        self, mock_hass, fronius_config, mock_modbus_client
    ):
        """Walks past Model 1 and Model 103 to find Model 124."""
        responses = [
            _ok_response([_SUNSPEC_ID_WORD0, _SUNSPEC_ID_WORD1]),
            _ok_response([1, 65]),                          # Model 1, length 65
            _ok_response([103, 50]),                        # Model 103, length 50
            _ok_response([_SUNSPEC_MODEL_124, 24]),
        ]
        mock_modbus_client.read_holding_registers = AsyncMock(side_effect=responses)
        inv = FroniusInverter(mock_hass, fronius_config)
        inv._client = mock_modbus_client
        result = await inv._discover_model124()
        assert result is True
        # 40000 (start) + 2 (ID) + (65 + 2) (model 1+header) + (50 + 2) (model 103+header) + 2 (model 124 header)
        expected_base = 40000 + 2 + (65 + 2) + (50 + 2) + 2
        assert inv._model124_base == expected_base


class TestEnsureModel124Reconnect:
    """_ensure_model124 must reconnect when the TCP socket dropped, even if base is cached."""

    async def test_reconnects_when_disconnected_with_cached_base(
        self, inverter, monkeypatch
    ):
        """Connection check happens BEFORE the cache check.

        Otherwise a stale base address would mask a dropped socket and every
        subsequent read/write would silently fail.
        """
        import sys

        # Stale client that reports as disconnected
        stale_client = MagicMock()
        stale_client.connected = False
        stale_client.close = MagicMock()
        inverter._client = stale_client

        # Fresh client that the patched constructor returns on reconnect
        fresh_client = MagicMock()
        fresh_client.connected = False
        fresh_client.read_holding_registers = AsyncMock(
            return_value=_ok_response([0])
        )

        async def fresh_connect():
            fresh_client.connected = True
            return True

        fresh_client.connect = AsyncMock(side_effect=fresh_connect)

        # The driver re-imports pymodbus.client lazily in _ensure_connected.
        # Inject a stub module so the import succeeds without the real package.
        constructor = MagicMock(return_value=fresh_client)
        pymodbus_client_mod = MagicMock()
        pymodbus_client_mod.AsyncModbusTcpClient = constructor
        pymodbus_mod = MagicMock()
        pymodbus_mod.client = pymodbus_client_mod
        monkeypatch.setitem(sys.modules, "pymodbus", pymodbus_mod)
        monkeypatch.setitem(sys.modules, "pymodbus.client", pymodbus_client_mod)

        result = await inverter._ensure_model124()
        assert result is True
        # Stale socket released, new client built
        stale_client.close.assert_called_once()
        constructor.assert_called_with(inverter._host, port=inverter._port)
        fresh_client.connect.assert_called()
        # Cached Model 124 base preserved across the reconnect
        assert inverter._model124_base == 40070


class TestLockSerialization:
    """asyncio.Lock prevents interleaved Modbus write sequences."""

    async def test_lock_serializes_concurrent_calls(
        self, inverter, mock_modbus_client
    ):
        """Two concurrent control operations produce non-interleaved write streams."""
        import asyncio

        write_order: list[int] = []
        original_write = mock_modbus_client.write_register

        async def tracked_write(*args, **kwargs):
            # pymodbus 3.9+: address and value are passed as kwargs
            write_order.append(kwargs["address"])
            await asyncio.sleep(0)  # force a scheduler yield
            return await original_write(*args, **kwargs)

        mock_modbus_client.write_register = AsyncMock(side_effect=tracked_write)

        await asyncio.gather(
            inverter.async_set_charge_limit(0),
            inverter.async_stop_forcible(),
        )

        base = inverter._model124_base
        cl_writes = [
            base + _OFFSET_INWRTE,
            base + _OFFSET_WINTMS,
            base + _OFFSET_RVRTTMS,
            base + _OFFSET_STORCTL_MOD,
        ]
        sf_writes = [
            base + _OFFSET_STORCTL_MOD,
            base + _OFFSET_INWRTE,
            base + _OFFSET_OUTWRTE,
        ]
        # The two operations must run back-to-back, never interleaved
        assert (
            write_order == cl_writes + sf_writes
            or write_order == sf_writes + cl_writes
        ), f"Lock did not serialize writes: {write_order}"


class TestFailsafeKeepalive:
    """RvrtTms-Watchdog + Keepalive: der Wechselrichter beendet den
    Zwangsmodus selbst, wenn Home Assistant nicht mehr spricht."""

    def test_keepalive_interval_leaves_margin_before_fallback(self):
        """Ein einzelner verlorener Rewrite darf den Watchdog nicht ablaufen
        lassen — sonst fiele die Steuerung sporadisch in die Automatik."""
        assert _KEEPALIVE_INTERVAL_SECONDS * 2 < _RVRTTMS_SECONDS

    def test_fallback_within_fronius_range(self):
        """Fronius akzeptiert 0…28800 s; außerhalb wird der Wert abgelehnt."""
        assert 0 < _RVRTTMS_SECONDS <= 28800

    async def test_charge_limit_starts_keepalive(self, inverter):
        await inverter.async_set_charge_limit(0)
        assert inverter._active_command == {"kind": "charge_limit", "power_kw": 0}
        assert inverter._keepalive_task is not None
        inverter._cancel_keepalive()

    async def test_discharge_starts_keepalive(self, inverter):
        await inverter.async_set_discharge(2.5, target_soc=15)
        assert inverter._active_command == {
            "kind": "discharge", "power_kw": 2.5, "target_soc": 15,
        }
        assert inverter._keepalive_task is not None
        inverter._cancel_keepalive()

    async def test_failed_command_does_not_arm_keepalive(
        self, inverter, mock_modbus_client
    ):
        """Ohne erfolgreichen Befehl gibt es nichts nachzuschreiben."""
        mock_modbus_client.write_register = AsyncMock(return_value=_err_response())
        await inverter.async_set_charge_limit(0)
        assert inverter._active_command is None
        assert inverter._keepalive_task is None

    async def test_stop_forcible_cancels_keepalive(self, inverter):
        await inverter.async_set_charge_limit(0)
        await inverter.async_stop_forcible()
        assert inverter._active_command is None
        assert inverter._keepalive_task is None

    async def test_disconnect_cancels_keepalive(self, inverter):
        await inverter.async_set_charge_limit(0)
        await inverter.async_disconnect()
        assert inverter._keepalive_task is None

    async def test_keepalive_rewrites_active_command(
        self, inverter, mock_modbus_client
    ):
        """Der Loop schreibt die komplette Sequenz erneut — nur so kann ein
        Rewrite keinen halb aktualisierten Registersatz hinterlassen."""
        import asyncio

        await inverter.async_set_charge_limit(0)
        inverter._cancel_keepalive()
        mock_modbus_client.write_register.reset_mock()

        # Keepalive-Intervall überspringen und den Loop nach einem Durchlauf
        # beenden, indem der aktive Befehl verschwindet.
        state = {"n": 0}

        async def fake_sleep(_seconds):
            state["n"] += 1
            if state["n"] > 1:
                inverter._active_command = None

        with patch.object(asyncio, "sleep", fake_sleep):
            await inverter._keepalive_loop()

        base = inverter._model124_base
        written = [
            c.kwargs["address"]
            for c in mock_modbus_client.write_register.call_args_list
        ]
        assert written == [
            base + _OFFSET_INWRTE,
            base + _OFFSET_WINTMS,
            base + _OFFSET_RVRTTMS,
            base + _OFFSET_STORCTL_MOD,
        ]

    async def test_keepalive_stops_when_command_cleared(
        self, inverter, mock_modbus_client
    ):
        """Nach einem Stop darf kein Rewrite den Modus reaktivieren."""
        import asyncio

        inverter._active_command = None
        mock_modbus_client.write_register.reset_mock()

        async def fake_sleep(_seconds):
            return None

        with patch.object(asyncio, "sleep", fake_sleep):
            await inverter._keepalive_loop()

        mock_modbus_client.write_register.assert_not_called()


class TestScaleFactors:
    """Skalierungsfaktoren werden vom Gerät gelesen statt angenommen."""

    @staticmethod
    def _sf_response(wchamax_sf, minrsv_sf, inout_sf):
        # Block +16…+23: WChaMax_SF, WChaDisChaGra_SF, VAChaMax_SF,
        # MinRsvPct_SF, ChaState_SF, StorAval_SF, InBatV_SF, InOutWRte_SF
        return _ok_response([
            wchamax_sf & 0xFFFF, 0, 0, minrsv_sf & 0xFFFF,
            0, 0, 0, inout_sf & 0xFFFF,
        ])

    async def test_reads_and_applies_scale_factors(self, inverter, mock_modbus_client):
        inverter._model124_length = 24
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=self._sf_response(0, -2, -2)
        )
        await inverter._read_scale_factors()
        assert (
            inverter._sf_wchamax,
            inverter._sf_minrsvpct,
            inverter._sf_inoutwrte,
        ) == (0, -2, -2)

    async def test_negative_scale_factors_decoded_as_signed(
        self, inverter, mock_modbus_client
    ):
        """sunssf ist int16 — 0xFFFD muss -3 ergeben, nicht 65533."""
        inverter._model124_length = 24
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=self._sf_response(1, -3, -2)
        )
        await inverter._read_scale_factors()
        assert inverter._sf_minrsvpct == -3
        assert inverter._sf_wchamax == 1

    async def test_implausible_scale_factor_falls_back_to_default(
        self, inverter, mock_modbus_client
    ):
        """Ein Fehl-Lesewert darf nicht jede Leistung um Zehnerpotenzen
        verschieben — dann lieber der dokumentierte SunSpec-Default."""
        inverter._model124_length = 24
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=self._sf_response(99, -2, -2)
        )
        await inverter._read_scale_factors()
        assert inverter._sf_wchamax == _SF_DEFAULT_WCHAMAX

    async def test_short_model_keeps_defaults_without_reading(
        self, inverter, mock_modbus_client
    ):
        """Ein zu kurzes Model 124 hat keinen SF-Block — dahinter läge das
        nächste Model, dessen Werte hier Unsinn ergäben."""
        inverter._model124_length = 16
        mock_modbus_client.read_holding_registers.reset_mock()
        await inverter._read_scale_factors()
        mock_modbus_client.read_holding_registers.assert_not_called()
        assert inverter._sf_inoutwrte == _SF_DEFAULT_INOUTWRTE
        assert inverter._sf_loaded is True

    async def test_read_error_keeps_defaults(self, inverter, mock_modbus_client):
        inverter._model124_length = 24
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_err_response()
        )
        await inverter._read_scale_factors()
        assert inverter._sf_inoutwrte == _SF_DEFAULT_INOUTWRTE

    async def test_scale_factors_read_once(self, inverter, mock_modbus_client):
        inverter._model124_length = 24
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=self._sf_response(0, -2, -2)
        )
        await inverter._read_scale_factors()
        await inverter._read_scale_factors()
        assert mock_modbus_client.read_holding_registers.call_count == 1

    async def test_wchamax_scaled_before_sanity_check(
        self, inverter, mock_modbus_client
    ):
        """WChaMax_SF=1 heißt: Rohwert in 10-W-Schritten. Ohne Skalierung
        wären 900 → 900 W statt 9000 W und jede Prozentrechnung zehnfach
        daneben."""
        inverter._wchamax = None
        inverter._wchamax_date = None
        inverter._sf_wchamax = 1
        inverter._sf_loaded = True
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response([900])
        )
        assert await inverter._read_wchamax() == 9000

    def test_rate_register_uses_inoutwrte_sf(self, inverter):
        inverter._sf_inoutwrte = -2
        assert inverter._rate_register(100.0) == 10000
        assert inverter._rate_register(50.0) == 5000
        # Negative Untergrenze als Zweierkomplement
        assert inverter._rate_register(-50.0) == (-5000) & 0xFFFF

    def test_rate_register_with_sf_zero(self, inverter):
        """SF 0 → das Register trägt ganze Prozent."""
        inverter._sf_inoutwrte = 0
        assert inverter._rate_register(100.0) == 100
        assert inverter._rate_register(-50.0) == (-50) & 0xFFFF

    def test_rate_register_clamps_to_int16(self, inverter):
        """Ein SF, mit dem 100 % nicht in int16 passt, darf nicht überlaufen."""
        inverter._sf_inoutwrte = -3
        assert inverter._rate_register(100.0) == 32767

    async def test_stop_forcible_reads_scale_factors_first(
        self, inverter, mock_modbus_client
    ):
        """Ein Stopp kann der erste Befehl einer Sitzung sein — dann muss er
        die 100-%-Raten trotzdem mit dem echten SF kodieren."""
        inverter._model124_length = 24
        inverter._sf_loaded = False
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=self._sf_response(0, 0, 0)
        )
        await inverter.async_stop_forcible()
        base = inverter._model124_base
        rate_writes = [
            c.kwargs["value"]
            for c in mock_modbus_client.write_register.call_args_list
            if c.kwargs["address"] in (base + _OFFSET_INWRTE, base + _OFFSET_OUTWRTE)
        ]
        # SF 0 → 100 % ist die 100, nicht die 10000 des SF-(-2)-Defaults
        assert rate_writes == [100, 100]

    async def test_minrsvpct_scaled_with_its_own_sf(
        self, inverter, mock_modbus_client
    ):
        """MinRsvPct hat einen eigenen SF — er darf nicht vom Raten-SF
        abgeleitet werden."""
        inverter._sf_minrsvpct = 0
        inverter._sf_loaded = True
        await inverter.async_set_discharge(2.5, target_soc=20)
        inverter._cancel_keepalive()
        base = inverter._model124_base
        minrsv_writes = [
            c.kwargs["value"]
            for c in mock_modbus_client.write_register.call_args_list
            if c.kwargs["address"] == base + _OFFSET_MINRSVPCT
        ]
        assert minrsv_writes == [int(20 - _MINRSV_SAFETY_MARGIN_PCT)]


class TestScheduleControlInterface:
    """Fahrplan-Steuerschnittstelle: der Executor steuert den Gen24 nur,
    wenn der Treiber sie vollständig anbietet."""

    def test_supports_schedule_control(self, inverter):
        assert inverter.supports_schedule_control is True

    def test_charge_limit_max_from_wchamax(self, inverter):
        inverter._wchamax = 5000
        assert inverter.get_charge_limit_max_kw() == 5.0

    def test_charge_limit_max_unknown_before_first_read(self, mock_hass, fronius_config):
        """Synchron gelesen — vor dem ersten Modbus-Zugriff gibt es den Wert
        noch nicht, und ein geratener Wert wäre schlimmer als None."""
        inv = FroniusInverter(mock_hass, fronius_config)
        assert inv.get_charge_limit_max_kw() is None
        assert inv.get_max_discharge_power_kw() is None

    def test_max_discharge_power_from_wchamax(self, inverter):
        """WChaMax ist bei Fronius die Bezugsgröße für BEIDE Raten — also
        auch die Obergrenze, die Guard 2 nicht überschreiten darf."""
        inverter._wchamax = 5000
        assert inverter.get_max_discharge_power_kw() == 5.0

    async def test_charge_limit_full_when_no_limit_active(
        self, inverter, mock_modbus_client
    ):
        """StorCtl_Mod ohne Bit 0: der Wechselrichter begrenzt nicht. Guard 1
        muss die volle Ladeleistung sehen, nicht den Restwert in InWRte."""
        regs = [0] * (_OFFSET_INWRTE - _OFFSET_STORCTL_MOD + 1)
        regs[0] = 0  # StorCtl_Mod
        regs[_OFFSET_INWRTE - _OFFSET_STORCTL_MOD] = 2500  # 25 % — irrelevant
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response(regs)
        )
        assert await inverter.async_get_charge_limit_kw() == 5.0

    async def test_charge_limit_scaled_when_active(self, inverter, mock_modbus_client):
        """Bit 0 gesetzt, InWRte 50 % von 5 kW → 2,5 kW."""
        regs = [0] * (_OFFSET_INWRTE - _OFFSET_STORCTL_MOD + 1)
        regs[0] = 1
        regs[_OFFSET_INWRTE - _OFFSET_STORCTL_MOD] = 5000
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response(regs)
        )
        assert await inverter.async_get_charge_limit_kw() == 2.5

    async def test_charge_limit_zero_during_forced_discharge(
        self, inverter, mock_modbus_client
    ):
        """Während der Zwangsentladung steht InWRte negativ — das wirksame
        Ladelimit ist 0, kein negativer Wert."""
        regs = [0] * (_OFFSET_INWRTE - _OFFSET_STORCTL_MOD + 1)
        regs[0] = 3
        regs[_OFFSET_INWRTE - _OFFSET_STORCTL_MOD] = (-5000) & 0xFFFF
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response(regs)
        )
        assert await inverter.async_get_charge_limit_kw() == 0.0

    async def test_charge_limit_none_on_read_error(self, inverter, mock_modbus_client):
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_err_response()
        )
        assert await inverter.async_get_charge_limit_kw() is None

    async def test_control_values_rows(self, inverter, mock_modbus_client):
        """Die Transparenz-Ansicht bekommt Modus, beide Raten und die
        Reserve — bei einem Modbus-Treiber gibt es keine Entitäten, an denen
        das Panel sie sonst ablesen könnte."""
        regs = [0] * (_OFFSET_INWRTE - _OFFSET_STORCTL_MOD + 1)
        regs[0] = 3
        regs[_OFFSET_MINRSVPCT - _OFFSET_STORCTL_MOD] = 1000  # 10 %
        regs[_OFFSET_OUTWRTE - _OFFSET_STORCTL_MOD] = 5000  # 50 %
        regs[_OFFSET_INWRTE - _OFFSET_STORCTL_MOD] = (-5000) & 0xFFFF
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response(regs)
        )
        rows = await inverter.async_get_control_values()
        by_role = {r["role"]: r for r in rows}
        assert by_role["mode"]["value"] == "Lade- + Entladelimit aktiv"
        assert by_role["charge_limit"]["value"] == 0.0
        assert by_role["discharge_limit"]["value"] == 2.5
        assert by_role["backup_soc"]["value"] == 10.0
        assert all(r.get("entity_id") is None for r in rows)

    async def test_control_values_empty_on_error(self, inverter, mock_modbus_client):
        """Die Ansicht ruft man gerade dann auf, wenn etwas klemmt — sie darf
        nie werfen."""
        mock_modbus_client.read_holding_registers = AsyncMock(
            side_effect=Exception("boom")
        )
        assert await inverter.async_get_control_values() == []

    def test_no_control_entities(self, inverter):
        """Der Gen24 wird über Modbus gestellt, nicht über HA-Entitäten."""
        assert inverter.get_control_entities() == []


class TestBackupReserve:
    """MinRsvPct als Planungs-Untergrenze — ohne sie plant der Fahrplan
    Entladungen, die das Gerät verweigert."""

    def test_unknown_before_first_read(self, mock_hass, fronius_config):
        inv = FroniusInverter(mock_hass, fronius_config)
        assert inv.get_backup_reserve_soc_pct() is None

    async def test_read_with_wchamax_block(self, inverter, mock_modbus_client):
        """Der tägliche WChaMax-Block reicht bis MinRsvPct — ein Roundtrip."""
        inverter._wchamax = None
        inverter._wchamax_date = None
        inverter._sf_loaded = True
        # WChaMax, WChaGra, WDisChaGra, StorCtl_Mod, VAChaMax, MinRsvPct
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response([5000, 0, 0, 0, 0, 1500])
        )
        await inverter._read_wchamax()
        assert inverter.get_backup_reserve_soc_pct() == 15.0

    async def test_short_response_does_not_crash(self, inverter, mock_modbus_client):
        """Antwortet das Gerät kürzer als angefragt, bleibt WChaMax gültig."""
        inverter._wchamax = None
        inverter._wchamax_date = None
        inverter._sf_loaded = True
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response([5000])
        )
        assert await inverter._read_wchamax() == 5000
        assert inverter.get_backup_reserve_soc_pct() is None

    async def test_own_floor_is_not_reported_as_device_reserve(
        self, inverter, mock_modbus_client
    ):
        """Während der Entladung steht unser abgesenkter Floor im Register.
        Gemeldet werden muss trotzdem der Vorwert — sonst wandert die
        Planungsgrenze mit unserem eigenen Eingriff mit."""
        inverter._sf_loaded = True
        inverter._minrsvpct_idle = 1500  # 15 % Ruhewert
        mock_modbus_client.read_holding_registers = AsyncMock(
            return_value=_ok_response([1500])
        )
        await inverter.async_set_discharge(2.5, target_soc=20)
        inverter._cancel_keepalive()
        # Der Treiber hat gerade MinRsvPct auf 15 % (= 20 − 5) abgesenkt …
        assert inverter._minrsvpct_pre_discharge == 1500
        # … gemeldet wird der Vorwert, nicht der Floor.
        assert inverter.get_backup_reserve_soc_pct() == 15.0

    async def test_idle_value_not_overwritten_during_discharge(self, inverter):
        """Ein Read mitten in der Entladung darf den Ruhewert nicht kippen."""
        inverter._minrsvpct_idle = 1500
        inverter._active_command = {"kind": "discharge", "power_kw": 2.5, "target_soc": 20}
        inverter._note_idle_minrsvpct(500)
        assert inverter._minrsvpct_idle == 1500

    async def test_idle_value_updated_when_not_discharging(self, inverter):
        inverter._active_command = {"kind": "charge_limit", "power_kw": 0}
        inverter._note_idle_minrsvpct(500)
        assert inverter._minrsvpct_idle == 500
