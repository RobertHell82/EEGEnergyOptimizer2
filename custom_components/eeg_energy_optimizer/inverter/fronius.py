"""Fronius Gen24 inverter control via direct Modbus TCP (SunSpec Model 124).

Uses pymodbus AsyncModbusTcpClient for direct register writes.
Sensors are read via the native HA Fronius Integration (Solar API).

SunSpec Model 124 register offsets (relative to the discovered data base
address, i.e. after the 2-register ID/length header):
  +0  WChaMax              uint16(WChaMax_SF)  R   Max battery power in W
  +3  StorCtl_Mod          bitfield16 RW  Control mode (Bit 0=Charge, Bit 1=Discharge)
  +5  MinRsvPct            uint16(MinRsvPct_SF) RW  Min reserve %
  +10 OutWRte              int16(InOutWRte_SF)  RW  Discharge rate % of WChaMax
  +11 InWRte               int16(InOutWRte_SF)  RW  Charge rate % of WChaMax
  +12 InOutWRte_WinTms     uint16  RW  Window time before rate becomes effective (s)
  +13 InOutWRte_RvrtTms    uint16  RW  Fallback time (s, 0 = no revert)
  +16 WChaMax_SF           sunssf  R
  +19 MinRsvPct_SF         sunssf  R
  +23 InOutWRte_SF         sunssf  R

Scale factors are read from the device instead of assumed: every raw
register value maps to its physical value as ``value = raw * 10**SF``.
The SunSpec defaults on a Gen24 are SF 0 (W) / -2 (percent), but that is
firmware- and model-dependent - see _read_scale_factors().

Failsafe: InOutWRte_RvrtTms is the inverter's own communication watchdog.
Fronius: "Determines how long the operating mode will remain active. The
timer is restarted with every Modbus message received. If no new Modbus
message was received during the fallback time (= RvrtTms), the operating
mode is automatically ended and the operating mode with the next highest
priority becomes active." The driver arms it with _RVRTTMS_SECONDS and
feeds it from a keepalive task - same shape as the Kostal and SMA drivers.
This matters because the executor deduplicates: an unchanged plan value is
not rewritten (deadbands), so without the keepalive no Modbus message would
reach the inverter for hours. If Home Assistant dies mid-block or
mid-discharge, the inverter returns to its own battery management instead
of staying frozen in the forced mode.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

from .base import InverterBase

try:
    from homeassistant.helpers.storage import Store
except ImportError:  # pragma: no cover — test environment
    Store = None  # type: ignore

_LOGGER = logging.getLogger(__name__)


def _slave_kw(client: Any, slave_id: int) -> dict:
    """Return the slave/device-id kwarg matching the active pymodbus API.

    pymodbus 3.9+ renamed the ``slave`` parameter on
    ``ModbusClientMixin.read_holding_registers`` (and friends) to
    ``device_id``. Older releases still expect ``slave``. We probe the
    callable's signature once and pick whichever it accepts so the same
    code runs on both old and new HA installations.
    """
    import inspect
    try:
        sig = inspect.signature(client.read_holding_registers)
        if "device_id" in sig.parameters:
            return {"device_id": slave_id}
    except (TypeError, ValueError):
        pass
    return {"slave": slave_id}


def _to_signed16(raw: int) -> int:
    """Interpret a raw holding register as a signed 16-bit value."""
    return raw - 0x10000 if raw >= 0x8000 else raw


def _scale_to_register(value: float, sf: int) -> int:
    """Convert a physical value into its raw register representation.

    SunSpec defines ``value = raw * 10**SF``, so the inverse is
    ``raw = value / 10**SF`` - e.g. 100 % at SF -2 becomes 10000.
    """
    return int(round(value / (10 ** sf)))


# SunSpec Model 124 register offsets (relative to model base address)
_OFFSET_WCHAMAX = 0
_OFFSET_STORCTL_MOD = 3
_OFFSET_MINRSVPCT = 5
_OFFSET_OUTWRTE = 10
_OFFSET_INWRTE = 11
_OFFSET_WINTMS = 12
_OFFSET_RVRTTMS = 13

# Scale-factor registers (sunssf, int16). Offsets per the SunSpec model 124
# definition minus the 2-register ID/length header.
_OFFSET_WCHAMAX_SF = 16
_OFFSET_MINRSVPCT_SF = 19
_OFFSET_INOUTWRTE_SF = 23

# Model 124 data length in registers (26 total minus the 2-register header).
# A device reporting less than this does not expose the scale-factor block,
# so the driver keeps its documented defaults instead of reading garbage.
_MODEL124_DATA_LENGTH = 24

# Scale-factor fallbacks - the SunSpec defaults every Gen24 seen so far
# reports. Used when the register block is missing or implausible.
_SF_DEFAULT_WCHAMAX = 0
_SF_DEFAULT_MINRSVPCT = -2
_SF_DEFAULT_INOUTWRTE = -2

# A sunssf outside this range is not a scale factor but a misread register
# (wrong model layout, corrupted response). Applying it would shift every
# power/percent value by orders of magnitude.
_SF_PLAUSIBLE_RANGE = range(-10, 11)

# SunSpec identification
_SUNSPEC_ID_WORD0 = 0x5375  # "Su"
_SUNSPEC_ID_WORD1 = 0x6E53  # "nS"
_SUNSPEC_START = 40000
_SUNSPEC_MODEL_124 = 124
_SUNSPEC_END_MARKER = 0xFFFF
_SUNSPEC_MAX_ITERATIONS = 128

# Fallback time written to InOutWRte_RvrtTms (seconds, Fronius range
# 0...28800). After this long without a Modbus message the inverter ends the
# forced mode by itself - the failsafe for a dead Home Assistant. Long
# enough that a few missed keepalives do not drop the block mid-morning,
# short enough that a crashed HA does not leave the battery frozen for
# hours. Also comfortably above the executor's startup grace period, so a
# reload never lets the mode lapse.
_RVRTTMS_SECONDS = 300

# Keepalive interval. 5x margin before the fallback time lapses, matching
# the SMA driver - a single slow or lost write must never expire the
# watchdog while the schedule still wants the mode.
_KEEPALIVE_INTERVAL_SECONDS = 60

# Sicherheitsabstand zwischen dem als MinRsvPct geschriebenen SOC-Floor und
# dem Ziel-SOC des Optimizers. Der Optimizer beendet die Entladung erst bei
# Ziel-SOC − RESERVE_EXIT_HYSTERESIS_PCT (2 %, Schmitt-Trigger). Läge der
# Floor exakt auf dem Ziel-SOC, stoppt der Fronius die Entladung selbst
# (Anzeige "Minimum SOC"), der SOC kann die Austrittsschwelle nie erreichen
# und der erzwungene Entlademodus bleibt stehen — Batterie eingefroren, das
# Haus zieht die restliche Nacht aus dem Netz. Mit 5 % Abstand (> 2 %
# Hysterese + ~2 % Abweichung zwischen Fronius-internem SOC und HA-Sensor)
# beendet immer der Optimizer die Entladung; der Floor bleibt reines
# Sicherheitsnetz für den Fall, dass HA während der Entladung ausfällt.
_MINRSV_SAFETY_MARGIN_PCT = 5.0

# Sanity bound for WChaMax (max battery power in W). No residential Fronius
# Gen24 + battery setup exceeds this; a larger value almost certainly comes
# from a corrupted Modbus response and would compress every charge/discharge
# percentage calculation toward zero, making the battery appear inert.
_WCHAMAX_SANITY_LIMIT = 25000


class FroniusStateStore:
    """Persistiert den Pre-Discharge-MinRsvPct über HA-Neustarts.

    Beim ersten async_set_discharge einer Entladung wird der originale
    MinRsvPct (Rohwert, SF -2) gesichert; async_stop_forcible stellt ihn
    wieder her und löscht den Eintrag. Ohne Persistenz ginge der Snapshot
    bei einem HA-Neustart mitten in der Entladung verloren — die neue
    Instanz hätte keinen Vorwert, und die erhöhte Reserve bliebe dauerhaft
    im Wechselrichter stehen (Batterie im Automatikbetrieb nur noch bis
    zum alten Ziel-SOC nutzbar). Muster analog SolaXStateStore (Phase 12).
    """

    STORAGE_KEY = "eeg_energy_optimizer.fronius_state"
    STORAGE_VERSION = 1

    def __init__(self, hass: Any) -> None:
        self._store = (
            Store(hass, self.STORAGE_VERSION, self.STORAGE_KEY)
            if Store is not None
            else None
        )
        self._data: dict = {}
        self._loaded = False

    async def async_load(self) -> None:
        if self._loaded:
            return
        if self._store is None:
            self._loaded = True
            return
        data = await self._store.async_load()
        self._data = data if isinstance(data, dict) else {}
        self._loaded = True

    async def async_save_original_minrsvpct(self, raw: int) -> None:
        self._data["minrsvpct_original"] = int(raw)
        if self._store is not None:
            await self._store.async_save(self._data)

    async def async_clear_original_minrsvpct(self) -> None:
        if self._data.pop("minrsvpct_original", None) is not None:
            if self._store is not None:
                await self._store.async_save(self._data)

    @property
    def original_minrsvpct(self) -> int | None:
        val = self._data.get("minrsvpct_original")
        try:
            return int(val) if val is not None else None
        except (TypeError, ValueError):
            return None


class FroniusInverter(InverterBase):
    """Fronius Gen24 battery control via direct Modbus TCP (SunSpec Model 124)."""

    def __init__(self, hass: Any, config: dict) -> None:
        super().__init__(hass, config)
        self._host: str = config.get("fronius_modbus_host", "")
        self._port: int = int(config.get("fronius_modbus_port", 502))
        self._client: Any = None  # AsyncModbusTcpClient (lazy)
        self._model124_base: int | None = None
        self._model124_length: int | None = None
        self._wchamax: int | None = None
        self._wchamax_date: str | None = None  # date string for daily cache
        self._slave_id: int = 1  # Fronius default Modbus unit ID
        # Scale factors, read from the device on first use (see
        # _read_scale_factors). Defaults keep the documented SunSpec
        # behaviour until the real values are known.
        self._sf_wchamax: int = _SF_DEFAULT_WCHAMAX
        self._sf_minrsvpct: int = _SF_DEFAULT_MINRSVPCT
        self._sf_inoutwrte: int = _SF_DEFAULT_INOUTWRTE
        self._sf_loaded: bool = False
        # Active forced mode the keepalive rewrites until stop:
        #   {"kind": "charge_limit", "power_kw": float}
        #   {"kind": "discharge", "power_kw": float, "target_soc": float|None}
        self._active_command: dict | None = None
        self._keepalive_task: asyncio.Task | None = None
        # Cached MinRsvPct value (raw register, SF -2) read before
        # async_set_discharge() overwrites it. Restored by
        # async_stop_forcible() so we do not leave the inverter with
        # an elevated reserve in automatic mode. Zusätzlich im
        # FroniusStateStore persistiert, damit der Vorwert einen
        # HA-Neustart mitten in der Entladung überlebt.
        self._minrsvpct_pre_discharge: int | None = None
        self._state_store = FroniusStateStore(hass)
        # Serializes Modbus operations. Der 30-Sekunden-Lauf der Steuerung und
        # der Verbindungstest aus dem Panel können ihre Mehr-Register-Sequenzen
        # sonst verschränken und den Wechselrichter halb gesetzt zurücklassen.
        # Other inverter drivers rely on HA service-call serialization;
        # the direct Modbus TCP path here has no such guarantee.
        self._lock = asyncio.Lock()

    def _close_client(self) -> None:
        """Close and discard the Modbus TCP client.

        Use this instead of `self._client = None` so the underlying socket
        is released immediately rather than waiting for the GC. pymodbus
        AsyncModbusTcpClient.close() is synchronous (it just tears the
        transport down).
        """
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                _LOGGER.debug("Fronius: error closing Modbus client")
            self._client = None

    async def _ensure_connected(self) -> bool:
        """Ensure Modbus TCP connection is established.

        Creates a new AsyncModbusTcpClient if needed and attempts connection
        with up to 3 retries (200ms delay between attempts).
        Returns True if connected, False on failure.
        """
        if self._client is not None and self._client.connected:
            return True

        try:
            from pymodbus.client import AsyncModbusTcpClient
        except ImportError:
            _LOGGER.error("Fronius: pymodbus not installed")
            return False

        for attempt in range(3):
            try:
                if self._client is None or not self._client.connected:
                    # Close any stale client before replacing it
                    self._close_client()
                    self._client = AsyncModbusTcpClient(
                        self._host, port=self._port
                    )
                    await self._client.connect()
                if self._client.connected:
                    _LOGGER.debug(
                        "Fronius: Modbus TCP connected to %s:%s (attempt %d)",
                        self._host, self._port, attempt + 1,
                    )
                    return True
            except Exception:
                _LOGGER.debug(
                    "Fronius: connection attempt %d failed", attempt + 1
                )
                self._close_client()
            if attempt < 2:
                await asyncio.sleep(0.2)

        _LOGGER.error(
            "Fronius: failed to connect to %s:%s after 3 attempts",
            self._host, self._port,
        )
        return False

    async def _discover_model124(self) -> bool:
        """SunSpec Model Discovery: scan from register 40000 to find Model 124.

        Reads the SunSpec identification header, then iterates through the
        model table until Model 124 is found or the end marker is reached.
        """
        if self._client is None or not self._client.connected:
            return False

        try:
            # Verify SunSpec ID at registers 40000-40001
            result = await self._client.read_holding_registers(
                address=_SUNSPEC_START, count=2,
                **_slave_kw(self._client, self._slave_id),
            )
            if result.isError():
                _LOGGER.error("Fronius: failed to read SunSpec ID at %d", _SUNSPEC_START)
                return False

            word0, word1 = result.registers[0], result.registers[1]
            if word0 != _SUNSPEC_ID_WORD0 or word1 != _SUNSPEC_ID_WORD1:
                _LOGGER.error(
                    "Fronius: invalid SunSpec ID at %d: 0x%04X 0x%04X (expected 0x%04X 0x%04X)",
                    _SUNSPEC_START, word0, word1, _SUNSPEC_ID_WORD0, _SUNSPEC_ID_WORD1,
                )
                return False

            _LOGGER.debug("Fronius: SunSpec ID verified at %d", _SUNSPEC_START)

            # Iterate through model table starting at 40002
            address = _SUNSPEC_START + 2
            for i in range(_SUNSPEC_MAX_ITERATIONS):
                result = await self._client.read_holding_registers(
                    address=address, count=2,
                    **_slave_kw(self._client, self._slave_id),
                )
                if result.isError():
                    _LOGGER.error(
                        "Fronius: failed to read model header at %d", address
                    )
                    return False

                model_id = result.registers[0]
                length = result.registers[1]

                _LOGGER.debug(
                    "Fronius: model %d (length %d) at address %d",
                    model_id, length, address,
                )

                if model_id == _SUNSPEC_END_MARKER:
                    _LOGGER.warning(
                        "Fronius: SunSpec end marker reached at %d, Model 124 not found",
                        address,
                    )
                    return False

                if model_id == _SUNSPEC_MODEL_124:
                    # Model 124 data starts after the 2-register header
                    self._model124_base = address + 2
                    self._model124_length = length
                    _LOGGER.info(
                        "Fronius: SunSpec Model 124 found, data base address = %d "
                        "(length %d)",
                        self._model124_base, length,
                    )
                    return True

                # Advance past header (2 regs) + model data (length regs)
                address += length + 2

        except Exception:
            _LOGGER.exception("Fronius: error during SunSpec Model Discovery")
            self._close_client()
            return False

        return False

    async def _ensure_model124(self) -> bool:
        """Ensure Modbus connection is alive and Model 124 base address is known.

        Connection check must happen before the cache check — otherwise the
        cached base address keeps the driver from reconnecting after a
        Modbus TCP drop, and every subsequent read/write fails silently.
        """
        if not await self._ensure_connected():
            return False
        if self._model124_base is not None:
            return True
        return await self._discover_model124()

    async def _read_scale_factors(self) -> None:
        """Read the Model 124 scale-factor block once and cache it.

        The rate and reserve registers are percentages and the battery
        power is watts, but *how* a raw register maps to those units is
        declared by the device, not by us: value = raw * 10**SF. Assuming
        SF -2 / 0 happens to match every Gen24 seen so far, yet a device
        reporting WChaMax_SF = 1 (values in 10 W steps) would make every
        percentage calculation off by a factor of ten - the battery would
        look inert or the block would silently allow full charging.

        Failure is never fatal: the documented SunSpec defaults stay in
        place and the driver behaves exactly as it did before. Scale
        factors are immutable per device, so this runs once per session.
        """
        if self._sf_loaded or self._model124_base is None:
            return

        # A short model does not carry the scale-factor block at all -
        # reading past its end would return whatever the next model holds.
        if (self._model124_length or 0) < _MODEL124_DATA_LENGTH:
            _LOGGER.warning(
                "Fronius: Model 124 reports length %s (<%d) - keeping default "
                "scale factors (WChaMax=%d, MinRsvPct=%d, InOutWRte=%d)",
                self._model124_length, _MODEL124_DATA_LENGTH,
                _SF_DEFAULT_WCHAMAX, _SF_DEFAULT_MINRSVPCT, _SF_DEFAULT_INOUTWRTE,
            )
            self._sf_loaded = True
            return

        try:
            # One block read covers WChaMax_SF (+16) through InOutWRte_SF (+23).
            count = _OFFSET_INOUTWRTE_SF - _OFFSET_WCHAMAX_SF + 1
            result = await self._client.read_holding_registers(
                address=self._model124_base + _OFFSET_WCHAMAX_SF, count=count,
                **_slave_kw(self._client, self._slave_id),
            )
            if result.isError():
                _LOGGER.warning(
                    "Fronius: failed to read scale factors - keeping defaults"
                )
                self._sf_loaded = True
                return

            regs = result.registers

            def _pick(offset: int, default: int, name: str) -> int:
                value = _to_signed16(regs[offset - _OFFSET_WCHAMAX_SF])
                if value not in _SF_PLAUSIBLE_RANGE:
                    _LOGGER.warning(
                        "Fronius: %s = %d is not a plausible scale factor - "
                        "using default %d",
                        name, value, default,
                    )
                    return default
                return value

            self._sf_wchamax = _pick(
                _OFFSET_WCHAMAX_SF, _SF_DEFAULT_WCHAMAX, "WChaMax_SF"
            )
            self._sf_minrsvpct = _pick(
                _OFFSET_MINRSVPCT_SF, _SF_DEFAULT_MINRSVPCT, "MinRsvPct_SF"
            )
            self._sf_inoutwrte = _pick(
                _OFFSET_INOUTWRTE_SF, _SF_DEFAULT_INOUTWRTE, "InOutWRte_SF"
            )
            self._sf_loaded = True
            _LOGGER.info(
                "Fronius: scale factors read - WChaMax_SF=%d, MinRsvPct_SF=%d, "
                "InOutWRte_SF=%d",
                self._sf_wchamax, self._sf_minrsvpct, self._sf_inoutwrte,
            )
        except Exception:
            _LOGGER.exception(
                "Fronius: error reading scale factors - keeping defaults"
            )
            self._sf_loaded = True

    def _rate_register(self, percent: float) -> int:
        """Encode a percentage for InWRte/OutWRte as an int16 register value.

        Negative percentages (the lower bound of a forced discharge) are
        written in two's complement, since Modbus holding registers are
        unsigned 16-bit.
        """
        raw = _scale_to_register(percent, self._sf_inoutwrte)
        if raw > 32767 or raw < -32768:
            clamped = max(min(raw, 32767), -32768)
            _LOGGER.warning(
                "Fronius: rate %.2f%% encodes to %d with SF %d - clamped to %d",
                percent, raw, self._sf_inoutwrte, clamped,
            )
            raw = clamped
        return raw & 0xFFFF

    async def _read_wchamax(self) -> int | None:
        """Read WChaMax (max battery power in W) from Model 124 offset +0.

        Cached for the current day — only re-read once per day. The raw
        register is scaled by WChaMax_SF before any plausibility check, so
        the sanity limit always applies to real watts.
        """
        today = date.today().isoformat()
        if self._wchamax is not None and self._wchamax_date == today:
            return self._wchamax

        if not await self._ensure_model124():
            return None

        try:
            await self._read_scale_factors()

            result = await self._client.read_holding_registers(
                address=self._model124_base + _OFFSET_WCHAMAX, count=1,
                **_slave_kw(self._client, self._slave_id),
            )
            if result.isError():
                _LOGGER.error("Fronius: failed to read WChaMax")
                return None

            raw = int(round(result.registers[0] * (10 ** self._sf_wchamax)))
            if raw == 0 or raw > _WCHAMAX_SANITY_LIMIT:
                # Implausible value — likely a corrupted Modbus response or
                # wrong SunSpec model layout. Don't cache, force a re-read on
                # the next cycle. Zero is also handled by callers as "unknown".
                _LOGGER.warning(
                    "Fronius: WChaMax=%d W outside plausible range (1..%d) — ignoring",
                    raw, _WCHAMAX_SANITY_LIMIT,
                )
                return None

            self._wchamax = raw
            self._wchamax_date = today
            _LOGGER.info("Fronius: WChaMax = %d W", self._wchamax)
            return self._wchamax

        except Exception:
            _LOGGER.exception("Fronius: error reading WChaMax")
            self._close_client()
            return None

    async def _write_register(self, offset: int, value: int) -> bool:
        """Write a single register at Model 124 base + offset.

        Increments register_writes counter and adds 200ms pause after write.
        """
        if self._model124_base is None:
            _LOGGER.error("Fronius: Model 124 base address not discovered")
            return False

        address = self._model124_base + offset
        try:
            result = await self._client.write_register(
                address=address, value=value,
                **_slave_kw(self._client, self._slave_id),
            )
            if result.isError():
                _LOGGER.error(
                    "Fronius: write error at register %d (value=%d)", address, value
                )
                return False
            self.register_writes += 1
            await asyncio.sleep(0.2)
            _LOGGER.debug(
                "Fronius: wrote register %d = %d", address, value
            )
            return True
        except Exception:
            _LOGGER.exception(
                "Fronius: exception writing register %d (value=%d)", address, value
            )
            self._close_client()
            return False

    # ------------------------------------------------------------------
    # Keepalive (feeding the InOutWRte_RvrtTms watchdog)
    # ------------------------------------------------------------------

    def _start_keepalive(self) -> None:
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    def _cancel_keepalive(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None

    async def _keepalive_loop(self) -> None:
        """Rewrite the active forced mode until it is stopped.

        The executor only writes when the plan value actually changes (it
        deduplicates via deadbands), so without this loop no Modbus message
        would reach the inverter for hours and the fallback timer would end
        the mode while the schedule still wants it. A failed rewrite is
        logged and retried next interval — with a 5× margin before RvrtTms
        lapses, one lost write is harmless.
        """
        try:
            while True:
                await asyncio.sleep(_KEEPALIVE_INTERVAL_SECONDS)
                if self._active_command is None:
                    return
                async with self._lock:
                    # Re-read under the lock: a stop may have landed while
                    # we were waiting for it.
                    command = self._active_command
                    if command is None:
                        return
                    ok = await self._write_active_command(command)
                if not ok:
                    _LOGGER.warning(
                        "Fronius: keepalive rewrite failed (%s) — will retry "
                        "next interval; the inverter falls back to its own "
                        "battery management after %d s without a message",
                        command.get("kind"), _RVRTTMS_SECONDS,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Fronius: keepalive loop crashed")

    async def _write_active_command(self, command: dict) -> bool:
        """Write the full register sequence for the given forced mode.

        Used for both the initial command and every keepalive rewrite, so a
        rewrite can never leave a half-updated register set behind. Caller
        must hold the lock.
        """
        if command["kind"] == "charge_limit":
            return await self._write_charge_limit(command["power_kw"])
        return await self._write_discharge(
            command["power_kw"], command.get("target_soc")
        )

    async def async_set_charge_limit(self, power_kw: float) -> bool:
        """Set battery charge limit / block charging.

        power_kw=0: Block charging (Ladesperre)
          - StorCtl_Mod = 1 (Bit 0: Charge Limit active)
          - InWRte = 0 (0% charge = no charging)

        power_kw>0: Partial charge limit
          - StorCtl_Mod = 1
          - InWRte = percent of WChaMax

        Arms the inverter's fallback timer and starts the keepalive, so a
        crashed Home Assistant cannot leave charging blocked indefinitely.
        """
        async with self._lock:
            return await self._set_charge_limit_locked(power_kw)

    async def _set_charge_limit_locked(self, power_kw: float) -> bool:
        ok = await self._write_charge_limit(power_kw)
        if ok:
            self._active_command = {"kind": "charge_limit", "power_kw": power_kw}
            self._start_keepalive()
        return ok

    async def _write_charge_limit(self, power_kw: float) -> bool:
        try:
            if not await self._ensure_model124():
                return False

            wchamax = await self._read_wchamax()
            if wchamax is None or wchamax == 0:
                _LOGGER.error("Fronius: cannot set charge limit — WChaMax unknown or zero")
                return False

            # Write the rate register BEFORE activating the limit mode.
            # If StorCtl_Mod=1 succeeded first and InWRte then failed, the
            # inverter would enter Charge-Limit mode with the previously
            # cached InWRte (possibly 10000 = 100%) and silently fail to
            # block charging. Setting the rate first guarantees that when
            # the mode bit flips, the desired rate is already in place.
            if power_kw == 0:
                # Block charging completely
                percent = 0.0
            else:
                # Partial charge limit
                percent = min(power_kw * 1000 / wchamax, 1.0) * 100.0
            if not await self._write_register(
                _OFFSET_INWRTE, self._rate_register(percent)
            ):
                return False

            # WinTms = 0: the limit takes effect immediately instead of
            # being spread over a randomised window. (Historically this
            # driver also forced RvrtTms to 0 — that was a workaround for
            # writing to the wrong offsets, and it removed the only
            # inverter-side failsafe there is.)
            if not await self._write_register(_OFFSET_WINTMS, 0):
                return False
            # RvrtTms = fallback time: the inverter's own watchdog. It ends
            # the forced mode if no Modbus message arrives for this long,
            # which is exactly what we want when HA dies — see the module
            # docstring. The keepalive keeps feeding it while the schedule
            # still wants the block.
            if not await self._write_register(_OFFSET_RVRTTMS, _RVRTTMS_SECONDS):
                return False

            # StorCtl_Mod = 1 (Charge Limit active) — activates InWRte set above
            if not await self._write_register(_OFFSET_STORCTL_MOD, 1):
                return False

            _LOGGER.info(
                "Fronius: charge limit set (power_kw=%.2f, WChaMax=%d W)",
                power_kw, wchamax,
            )
            return True

        except Exception:
            _LOGGER.exception("Fronius: failed to set charge limit")
            self._close_client()
            return False

    async def async_set_discharge(
        self, power_kw: float, target_soc: float | None = None
    ) -> bool:
        """Force battery discharge into the grid at the given power.

        Sets InWRte=-percent (lower bound), OutWRte=+percent (upper bound),
        and StorCtl_Mod=3 (both limits active). Per Fronius Modbus manual
        example 6, this collapses the [InWRte, OutWRte] power window onto a
        single point, enforcing discharge at the requested rate independent
        of house consumption — what the EEG evening feed-in needs.

        Optionally sets MinRsvPct as a SOC floor; the previous MinRsvPct is
        snapshotted on first call so async_stop_forcible() can restore it.
        """
        async with self._lock:
            return await self._set_discharge_locked(power_kw, target_soc)

    async def _set_discharge_locked(
        self, power_kw: float, target_soc: float | None
    ) -> bool:
        ok = await self._write_discharge(power_kw, target_soc)
        if ok:
            self._active_command = {
                "kind": "discharge",
                "power_kw": power_kw,
                "target_soc": target_soc,
            }
            self._start_keepalive()
        return ok

    async def _write_discharge(
        self, power_kw: float, target_soc: float | None
    ) -> bool:
        try:
            if not await self._ensure_model124():
                return False

            wchamax = await self._read_wchamax()
            if wchamax is None or wchamax == 0:
                _LOGGER.error("Fronius: cannot set discharge — WChaMax unknown or zero")
                return False

            percent = min(power_kw * 1000 / wchamax, 1.0) * 100.0

            # Write the rate registers BEFORE activating the limit mode so
            # that a partial failure can never leave the inverter with the
            # discharge mode active but stale rate values from a previous
            # operation. See ME-03 in REVIEW.md / set_charge_limit comment.

            # Force-discharge into the grid (independent of house consumption):
            # per Fronius Modbus manual example 6, "Discharging with 50% of
            # nominal power" requires OutWRte=+50%, InWRte=-50%, StorCtl_Mod=3,
            # which yields the window [-WChaMax/2, -WChaMax/2] — i.e. an
            # exactly enforced discharge. With InWRte=0 the inverter would
            # only discharge what the house actually consumes, defeating the
            # EEG evening feed-in purpose.
            #
            # _rate_register() applies InOutWRte_SF and encodes the negative
            # lower bound as two's complement (Modbus holding registers are
            # unsigned 16-bit).
            if not await self._write_register(
                _OFFSET_INWRTE, self._rate_register(-percent)
            ):
                return False

            # OutWRte = +percent (upper discharge bound, mirrors InWRte's
            # absolute value so the [InWRte, OutWRte] window collapses onto
            # the desired forced discharge point)
            if not await self._write_register(
                _OFFSET_OUTWRTE, self._rate_register(percent)
            ):
                return False

            # Optional: set MinRsvPct for SOC floor (SF -2, e.g. 1500 = 15%)
            if target_soc is not None:
                # Snapshot the current MinRsvPct so async_stop_forcible() can
                # restore the user's configured reserve. Fronius has no
                # auto-revert, so without this snapshot the elevated reserve
                # would persist into automatic mode. Der Store hat Vorrang
                # vor dem Register-Read: nach einem HA-Neustart mitten in der
                # Entladung enthält das Register bereits unseren abgesenkten
                # Floor — nur der Store hält den echten Vorwert
                # (Snapshot-Vergiftung vermeiden).
                if self._minrsvpct_pre_discharge is None:
                    await self._state_store.async_load()
                    stored = self._state_store.original_minrsvpct
                    if stored is not None:
                        self._minrsvpct_pre_discharge = stored
                        _LOGGER.debug(
                            "Fronius: pre-discharge MinRsvPct=%d aus Store übernommen",
                            stored,
                        )
                    else:
                        try:
                            result = await self._client.read_holding_registers(
                                address=self._model124_base + _OFFSET_MINRSVPCT,
                                count=1,
                                **_slave_kw(self._client, self._slave_id),
                            )
                            if not result.isError():
                                self._minrsvpct_pre_discharge = result.registers[0]
                                await self._state_store.async_save_original_minrsvpct(
                                    result.registers[0]
                                )
                                _LOGGER.debug(
                                    "Fronius: cached pre-discharge MinRsvPct=%d",
                                    self._minrsvpct_pre_discharge,
                                )
                        except Exception:
                            _LOGGER.debug(
                                "Fronius: could not snapshot MinRsvPct — will skip restore"
                            )

                # Floor mit Sicherheitsabstand unter dem Ziel-SOC schreiben —
                # Begründung siehe _MINRSV_SAFETY_MARGIN_PCT.
                min_rsv = _scale_to_register(
                    max(target_soc - _MINRSV_SAFETY_MARGIN_PCT, 0.0),
                    self._sf_minrsvpct,
                )
                if not await self._write_register(_OFFSET_MINRSVPCT, min_rsv):
                    _LOGGER.warning("Fronius: failed to set MinRsvPct (non-critical)")

            # WinTms = 0 (effective immediately), RvrtTms = fallback time
            # (the inverter's watchdog) — see _write_charge_limit.
            if not await self._write_register(_OFFSET_WINTMS, 0):
                return False
            if not await self._write_register(_OFFSET_RVRTTMS, _RVRTTMS_SECONDS):
                return False

            # StorCtl_Mod = 3 (Bits 0+1: Charge + Discharge Limit active) —
            # written LAST so that all rate/reserve registers are already in
            # place when the mode bits flip on. Prevents partial-failure
            # states like "discharge mode active with stale rate values".
            if not await self._write_register(_OFFSET_STORCTL_MOD, 3):
                return False

            _LOGGER.info(
                "Fronius: discharge set (power_kw=%.2f, percent=%.1f, WChaMax=%d W)",
                power_kw, percent, wchamax,
            )
            return True

        except Exception:
            _LOGGER.exception("Fronius: failed to set discharge")
            self._close_client()
            return False

    async def async_stop_forcible(self) -> bool:
        """Stop forced charge/discharge, return to automatic mode.

        Restores StorCtl_Mod=0 and both rate registers to 100 %, and stops
        the keepalive — from here on the inverter's fallback timer would
        end the mode anyway, so a failed write can no longer strand it.
        """
        async with self._lock:
            return await self._stop_forcible_locked()

    async def _stop_forcible_locked(self) -> bool:
        # Drop the active command first: a keepalive rewrite racing this
        # stop would otherwise re-arm the mode we are about to clear.
        self._active_command = None
        self._cancel_keepalive()
        try:
            if not await self._ensure_model124():
                return False

            # The 100 % rates below are encoded with InOutWRte_SF, which may
            # not have been read yet — a stop can be the very first command
            # of a session (e.g. handing the battery back after a restart).
            await self._read_scale_factors()

            # StorCtl_Mod = 0 (no limits active)
            if not await self._write_register(_OFFSET_STORCTL_MOD, 0):
                return False

            # InWRte = 100% charge allowed
            if not await self._write_register(
                _OFFSET_INWRTE, self._rate_register(100.0)
            ):
                return False

            # OutWRte = 100% discharge allowed
            if not await self._write_register(
                _OFFSET_OUTWRTE, self._rate_register(100.0)
            ):
                return False

            # Restore MinRsvPct if async_set_discharge() raised it earlier.
            # Fronius has no auto-revert: leaving the reserve elevated
            # would prevent the inverter from using the battery down to
            # the user's configured level in automatic mode. Quelle ist der
            # RAM-Cache oder — nach einem HA-Neustart mitten in der
            # Entladung — der persistierte Store-Wert.
            restored = self._minrsvpct_pre_discharge
            if restored is None:
                await self._state_store.async_load()
                restored = self._state_store.original_minrsvpct
            if restored is not None:
                if await self._write_register(_OFFSET_MINRSVPCT, restored):
                    _LOGGER.info(
                        "Fronius: restored MinRsvPct to %d (pre-discharge value)",
                        restored,
                    )
                    self._minrsvpct_pre_discharge = None
                    await self._state_store.async_clear_original_minrsvpct()
                else:
                    _LOGGER.warning(
                        "Fronius: failed to restore MinRsvPct=%d — keeping cached value for retry",
                        restored,
                    )
                    # False, damit der Optimizer stop_forcible im nächsten
                    # Zyklus wiederholt — sonst bliebe die erhöhte Reserve
                    # bis zum nächsten Zustandswechsel stehen.
                    return False

            _LOGGER.info("Fronius: stopped forcible mode — automatic operation restored")
            return True

        except Exception:
            _LOGGER.exception("Fronius: failed to stop forcible mode")
            self._close_client()
            return False

    # ------------------------------------------------------------------
    # Fahrplan-Steuerschnittstelle (Schedule-Executor)
    # ------------------------------------------------------------------
    @property
    def supports_schedule_control(self) -> bool:
        """Der Fahrplan-Executor darf den Gen24 stellen."""
        return True

    async def async_get_charge_limit_kw(self) -> float | None:
        """Aktuell wirksames Ladelimit in kW (InWRte × WChaMax).

        Guard 1 rechnet vom Rückgabewert aus weiter, deshalb muss er das
        WIRKSAME Limit sein: ist Bit 0 von StorCtl_Mod nicht gesetzt,
        begrenzt der Wechselrichter gar nicht — dann gilt die volle
        Ladeleistung, nicht der Restinhalt von InWRte. Während einer
        erzwungenen Entladung steht InWRte negativ; das wirksame Ladelimit
        ist dann 0.
        """
        async with self._lock:
            if not await self._ensure_model124():
                return None
            wchamax = await self._read_wchamax()
            if not wchamax:
                return None
            try:
                count = _OFFSET_INWRTE - _OFFSET_STORCTL_MOD + 1
                result = await self._client.read_holding_registers(
                    address=self._model124_base + _OFFSET_STORCTL_MOD,
                    count=count,
                    **_slave_kw(self._client, self._slave_id),
                )
                if result.isError():
                    _LOGGER.warning("Fronius: failed to read StorCtl_Mod/InWRte")
                    return None
                storctl = result.registers[0]
                if not storctl & 0b1:
                    return wchamax / 1000.0
                percent = _to_signed16(
                    result.registers[_OFFSET_INWRTE - _OFFSET_STORCTL_MOD]
                ) * (10 ** self._sf_inoutwrte)
                return max(percent, 0.0) / 100.0 * wchamax / 1000.0
            except Exception:
                _LOGGER.exception("Fronius: error reading charge limit")
                self._close_client()
                return None

    def get_charge_limit_max_kw(self) -> float | None:
        """Hardware-Maximum des Ladelimits in kW — WChaMax.

        Synchron, also nur aus dem Tagescache: vor dem ersten Schreibbefehl
        ist der Wert unbekannt (None) und Guard 1 hebt ungeclampt an, was
        der Wechselrichter selbst abfängt.
        """
        return None if self._wchamax is None else self._wchamax / 1000.0

    def get_max_discharge_power_kw(self) -> float | None:
        """Maximale Entladeleistung in kW — ebenfalls WChaMax.

        Bei Fronius ist WChaMax die Bezugsgröße für BEIDE Raten: laut
        Modbus-Doku "feeds back the baseline value for the registers
        OutWRte and InWRte". OutWRte = 100 % entspricht damit WChaMax, das
        ist die Obergrenze, die Guard 2 nicht überschreiten darf.
        """
        return None if self._wchamax is None else self._wchamax / 1000.0

    async def async_get_control_values(self) -> list[dict]:
        """Stellgrößen für die Transparenz-Ansicht — direkt aus den Registern.

        Der Gen24 wird nicht über HA-Entitäten gestellt, sondern über
        Modbus; get_control_entities() bleibt deshalb leer und die Werte
        kommen von hier. Fehlschläge liefern eine leere Liste statt zu
        werfen — die Ansicht ist Diagnose, kein Steuerpfad.
        """
        async with self._lock:
            if not await self._ensure_model124():
                return []
            wchamax = await self._read_wchamax()
            try:
                count = _OFFSET_INWRTE - _OFFSET_STORCTL_MOD + 1
                result = await self._client.read_holding_registers(
                    address=self._model124_base + _OFFSET_STORCTL_MOD,
                    count=count,
                    **_slave_kw(self._client, self._slave_id),
                )
                if result.isError():
                    return []
                regs = result.registers
            except Exception:
                _LOGGER.exception("Fronius: error reading control values")
                self._close_client()
                return []

        def _at(offset: int) -> int:
            return regs[offset - _OFFSET_STORCTL_MOD]

        storctl = _at(_OFFSET_STORCTL_MOD)
        mode_text = {
            0: "Automatik (keine Limits)",
            1: "Ladelimit aktiv",
            2: "Entladelimit aktiv",
            3: "Lade- + Entladelimit aktiv",
        }.get(storctl, f"StorCtl_Mod {storctl}")

        rows: list[dict] = [
            {"label": "Betriebsmodus (StorCtl_Mod)", "value": mode_text, "role": "mode"},
        ]

        in_pct = _to_signed16(_at(_OFFSET_INWRTE)) * (10 ** self._sf_inoutwrte)
        out_pct = _to_signed16(_at(_OFFSET_OUTWRTE)) * (10 ** self._sf_inoutwrte)
        if wchamax:
            rows.append({
                "label": "Ladeleistung max (InWRte)",
                "value": round(max(in_pct, 0.0) / 100.0 * wchamax / 1000.0, 2),
                "unit": "kW",
                "role": "charge_limit",
            })
            rows.append({
                "label": "Entladeleistung max (OutWRte)",
                "value": round(abs(out_pct) / 100.0 * wchamax / 1000.0, 2),
                "unit": "kW",
                "role": "discharge_limit",
            })
        else:
            rows.append({
                "label": "Ladeleistung max (InWRte)",
                "value": round(in_pct, 1), "unit": "%", "role": "charge_limit",
            })
            rows.append({
                "label": "Entladeleistung max (OutWRte)",
                "value": round(out_pct, 1), "unit": "%", "role": "discharge_limit",
            })

        rows.append({
            "label": "Mindest-Ladestand (MinRsvPct)",
            "value": round(
                _at(_OFFSET_MINRSVPCT) * (10 ** self._sf_minrsvpct), 1
            ),
            "unit": "%",
            "role": "backup_soc",
        })
        return rows

    @property
    def is_available(self) -> bool:
        """Whether the inverter is reachable.

        Fronius is driven over a direct Modbus TCP connection that we open
        lazily, so checking ``self._client.connected`` would falsely report
        unavailable before the first operation runs (e.g. when the user's
        first action after setup is a manual control click). As long as a
        host is configured, treat the inverter as available — the real
        TCP probe happens inside _ensure_connected when an operation runs.
        """
        return bool(self._host)

    async def async_disconnect(self) -> None:
        """Disconnect Modbus TCP client for cleanup (called on entry unload).

        Stops the keepalive but deliberately does NOT clear the forced
        mode: an unload during a reload would briefly hand the battery
        back. If the entry is gone for good, the inverter's fallback timer
        ends the mode on its own after _RVRTTMS_SECONDS.
        """
        self._cancel_keepalive()
        self._close_client()
