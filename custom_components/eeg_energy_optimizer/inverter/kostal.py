"""Kostal Plenticore inverter control via direct Modbus TCP.

Uses pymodbus AsyncModbusTcpClient for direct register writes against the
proprietary Kostal battery-control registers (NOT SunSpec Model 124 — Kostal
does not implement the storage-control model; evcc confirms this by writing
the same proprietary registers). Sensors are read via the native HA
`kostal_plenticore` integration (REST).

KOSTAL Modbus spec Rev. 2.9, chapter 3.4 (identical for G1/G2/G3):
  Port 1502, Unit-ID 71, Float32 with word swap (factory default
  "little-endian" byte order = CDAB word order), FC 0x03 / 0x10.

  1034  Battery charge power (DC) setpoint  Float RW
        positive = DISCHARGE, negative = charge — actively into/from grid
  1038  Battery max. charge power limit     Float RW (0 = charging blocked,
        discharge for house consumption stays available)
  1040  Battery max. discharge power limit  Float RW (not written by us)
  210   Act. state of charge (%)            Float R
  1068  Battery work capacity (Wh)          Float R
  1080  Battery management mode             U16   R
        0 = internal, 1 = external digital I/O, 2 = external Modbus TCP

Central architectural difference to Fronius: Kostal expects the setpoint to
be REWRITTEN CYCLICALLY (watchdog, timeout configurable in the service menu,
recommendation 60 s). On timeout the inverter falls back to its internal
automatic battery management — a built-in failsafe if HA dies mid-discharge.
The driver therefore runs its own keepalive task that rewrites the active
setpoint every KEEPALIVE_INTERVAL seconds, decoupled from the optimizer's
"only write on state change" deduplication.

The control registers are volatile (RAM, spec ch. 3.3) — cyclic writing is
harmless (no NVRAM wear, unlike SolarEdge). There is no hardware target-SOC
register: the optimizer supervises the SOC every 30 s and stops the
discharge itself; the watchdog covers the HA-crash case.

VERIFY AT DEVICE (before first production release):
  1. Does a positive 1034 setpoint really feed into the grid beyond house
     consumption? (multiple community confirmations, one dissent)
  2. AT grid-connection ramp (~10 min, TOR Erzeuger): are writes to 1034
     rejected with an exception or silently ignored? (spec footnote 7)
  3. Watchdog/fallback behavior of the concrete firmware; whether identical
     rewrites are accepted (some firmwares need a ±1 W jitter — implemented
     unconditionally below, harmless either way)
  4. Float vs U32 encoding of 1038/1040 (evcc writes float; an older
     community register list documents U32 — for the 0-W blocking value
     both encodings are identical, so only Einspeisebegrenzung would care)
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any

from .base import InverterBase

_LOGGER = logging.getLogger(__name__)


def _slave_kw(client: Any, slave_id: int) -> dict:
    """Return the slave/device-id kwarg matching the active pymodbus API.

    pymodbus 3.9+ renamed the ``slave`` parameter on
    ``ModbusClientMixin.read_holding_registers`` (and friends) to
    ``device_id``. Older releases still expect ``slave``. Same probing
    approach as the Fronius driver so the code runs on both.
    """
    import inspect
    try:
        sig = inspect.signature(client.read_holding_registers)
        if "device_id" in sig.parameters:
            return {"device_id": slave_id}
    except (TypeError, ValueError):
        pass
    return {"slave": slave_id}


# Kostal Modbus endpoint (spec ch. 3.1)
KOSTAL_UNIT_ID = 71

# Control registers (spec ch. 3.4)
REG_BATTERY_SETPOINT = 1034   # Float W — positive = discharge
REG_MAX_CHARGE_POWER = 1038   # Float W — 0 = block charging
REG_MAX_DISCHARGE_POWER = 1040  # Float W — read-only for us

# Read registers used by driver/probe
REG_SOC = 210                 # Float %
REG_BATTERY_CAPACITY = 1068   # Float Wh
REG_BATTERY_MGMT_MODE = 1080  # U16: 0=internal, 1=ext. digital I/O, 2=ext. Modbus
REG_PRODUCTNAME = 768         # String, 32 registers
REG_POWER_CLASS = 800         # String, 32 registers

BATTERY_MGMT_EXTERNAL_MODBUS = 2

# Keepalive rewrite interval. evcc rewrites at watchdog-timeout/2 with the
# recommended 60 s timeout → 30 s. We use 25 s for extra margin: a single
# lost/slow write must not let the watchdog lapse mid-state.
KEEPALIVE_INTERVAL = 25.0

# Some firmwares reportedly ignore identical rewrites for watchdog purposes.
# Alternate the written value by ±1 W between keepalive cycles — electrically
# irrelevant, but guarantees every write is a register change.
_JITTER_W = 1.0


def float_to_registers(value: float) -> list[int]:
    """Encode Float32 in Kostal's factory-default word order (CDAB).

    Bytes within each 16-bit register travel big-endian on the Modbus wire;
    Kostal's "little-endian" setting swaps the WORD order: the low word is
    transmitted first. IEEE754 bytes A B C D therefore map to
    registers [C<<8|D, A<<8|B].
    """
    a, b, c, d = struct.pack(">f", value)
    return [(c << 8) | d, (a << 8) | b]


def registers_to_float(regs: list[int]) -> float:
    """Decode Float32 from Kostal word-swapped register order (CDAB)."""
    low, high = regs[0], regs[1]
    raw = bytes([(high >> 8) & 0xFF, high & 0xFF, (low >> 8) & 0xFF, low & 0xFF])
    return struct.unpack(">f", raw)[0]


def registers_to_string(regs: list[int]) -> str:
    """Decode a Kostal string register block (2 ASCII chars per register)."""
    chars: list[str] = []
    for reg in regs:
        chars.append(chr((reg >> 8) & 0xFF))
        chars.append(chr(reg & 0xFF))
    return "".join(chars).split("\x00", 1)[0].strip()


class KostalInverter(InverterBase):
    """Kostal Plenticore battery control via direct Modbus TCP.

    Active commands are held in ``self._active`` and rewritten by the
    keepalive task until async_stop_forcible() clears them.
    """

    def __init__(self, hass: Any, config: dict) -> None:
        super().__init__(hass, config)
        self._host: str = config.get("kostal_modbus_host", "")
        self._port: int = int(config.get("kostal_modbus_port", 1502))
        self._client: Any = None  # AsyncModbusTcpClient (lazy)
        self._slave_id: int = KOSTAL_UNIT_ID
        # Active command the keepalive rewrites:
        #   ("discharge", watts) | ("charge_limit", watts) | None
        self._active: tuple[str, float] | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._jitter_toggle: bool = False
        # Snapshot of the max-charge-power register before we first wrote it,
        # restored on stop. RAM-only on purpose: the register is volatile —
        # after an HA restart the watchdog has already returned the inverter
        # to internal management, which ignores the stale value; the next
        # mode entry rewrites all relevant registers anyway.
        self._max_charge_pre_block: float | None = None
        # Hardware-Grenzen aus den Registern, sobald sie ungestoert lesbar
        # waren (Guard 1 und Guard 2 clampen darauf). None = noch unbekannt,
        # dann heben die Guards ungeclampt an und das Geraet faengt es ab.
        self._max_charge_hw: float | None = None
        self._max_discharge_hw: float | None = None
        # Einmalige Rueckleseprobe beim ersten Teil-Ladelimit, siehe
        # _pruefe_encoding().
        self._encoding_geprueft: bool = False
        # Serializes Modbus operations between the 30-second optimizer
        # cycle, manual WebSocket commands, and the keepalive task. Same
        # rationale as the Fronius driver: the direct Modbus TCP path has
        # no HA service-call serialization.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Connection handling (mirrors the Fronius driver)
    # ------------------------------------------------------------------

    def _close_client(self) -> None:
        """Close and discard the Modbus TCP client (releases the socket)."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                _LOGGER.debug("Kostal: error closing Modbus client")
            self._client = None

    async def _ensure_connected(self) -> bool:
        """Ensure Modbus TCP connection is established (3 attempts)."""
        if self._client is not None and self._client.connected:
            return True

        try:
            from pymodbus.client import AsyncModbusTcpClient
        except ImportError:
            _LOGGER.error("Kostal: pymodbus not installed")
            return False

        for attempt in range(3):
            try:
                if self._client is None or not self._client.connected:
                    self._close_client()
                    self._client = AsyncModbusTcpClient(
                        self._host, port=self._port
                    )
                    await self._client.connect()
                if self._client.connected:
                    _LOGGER.debug(
                        "Kostal: Modbus TCP connected to %s:%s (attempt %d)",
                        self._host, self._port, attempt + 1,
                    )
                    return True
            except Exception:
                _LOGGER.debug(
                    "Kostal: connection attempt %d failed", attempt + 1
                )
                self._close_client()
            if attempt < 2:
                await asyncio.sleep(0.2)

        _LOGGER.error(
            "Kostal: failed to connect to %s:%s after 3 attempts",
            self._host, self._port,
        )
        return False

    # ------------------------------------------------------------------
    # Register primitives
    # ------------------------------------------------------------------

    async def _write_float(self, address: int, value: float) -> bool:
        """Write a Float32 (word-swapped) via FC16, count writes, 200ms pause."""
        try:
            result = await self._client.write_registers(
                address=address, values=float_to_registers(value),
                **_slave_kw(self._client, self._slave_id),
            )
            if result.isError():
                _LOGGER.error(
                    "Kostal: write error at register %d (value=%.1f)",
                    address, value,
                )
                return False
            self.register_writes += 1
            await asyncio.sleep(0.2)
            _LOGGER.debug("Kostal: wrote register %d = %.1f", address, value)
            return True
        except Exception:
            _LOGGER.exception(
                "Kostal: exception writing register %d (value=%.1f)",
                address, value,
            )
            self._close_client()
            return False

    async def _read_float(self, address: int) -> float | None:
        """Read a Float32 (word-swapped) holding register pair."""
        try:
            result = await self._client.read_holding_registers(
                address=address, count=2,
                **_slave_kw(self._client, self._slave_id),
            )
            if result.isError():
                _LOGGER.error("Kostal: read error at register %d", address)
                return None
            return registers_to_float(result.registers)
        except Exception:
            _LOGGER.exception("Kostal: exception reading register %d", address)
            self._close_client()
            return None

    # ------------------------------------------------------------------
    # Keepalive (watchdog feeding)
    # ------------------------------------------------------------------

    def _start_keepalive(self) -> None:
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    def _cancel_keepalive(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None

    async def _keepalive_loop(self) -> None:
        """Rewrite the active setpoint until cancelled.

        Failures are logged but tolerated: the AT grid-connection ramp (spec
        footnote 7) can reject writes to 1034 for ~10 minutes after grid
        reconnection — cyclic writing heals this by itself, and a watchdog
        lapse in the meantime only means temporary internal automatic mode.
        """
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                async with self._lock:
                    active = self._active
                    if active is None:
                        return
                    if not await self._ensure_connected():
                        _LOGGER.warning(
                            "Kostal: keepalive write skipped — not connected"
                        )
                        continue
                    kind, watts = active
                    self._jitter_toggle = not self._jitter_toggle
                    jittered = watts + (_JITTER_W if self._jitter_toggle else 0.0)
                    if kind == "discharge":
                        ok = await self._write_float(
                            REG_BATTERY_SETPOINT, jittered
                        )
                    else:  # charge_limit
                        ok = await self._write_float(
                            REG_MAX_CHARGE_POWER, jittered
                        )
                    if not ok:
                        _LOGGER.warning(
                            "Kostal: keepalive rewrite failed (%s, %.0f W) — "
                            "will retry next interval; watchdog may fall back "
                            "to internal mode until then",
                            kind, watts,
                        )
        except asyncio.CancelledError:
            pass
        except Exception:
            _LOGGER.exception("Kostal: keepalive loop crashed")

    # ------------------------------------------------------------------
    # InverterBase implementation
    # ------------------------------------------------------------------

    async def async_set_charge_limit(self, power_kw: float) -> bool:
        """Set battery max charge power / block charging.

        power_kw=0: block charging (Morgen-Einspeisung) — register 1038 = 0.
        Discharge for house consumption stays available (Kostal semantics).
        power_kw>0: partial charge limit (Einspeisebegrenzung).
        The keepalive rewrites the value until stop.
        """
        async with self._lock:
            return await self._set_charge_limit_locked(power_kw)

    async def _set_charge_limit_locked(self, power_kw: float) -> bool:
        try:
            if not await self._ensure_connected():
                return False

            # Stuck-register guard: when switching from a discharge, reset
            # the setpoint explicitly instead of trusting the watchdog
            # timeout (community-reported bug: old setpoints survive a mode
            # change on some firmwares).
            if self._active is not None and self._active[0] == "discharge":
                if not await self._write_float(REG_BATTERY_SETPOINT, 0.0):
                    return False

            # Snapshot the pre-block max charge power once, so stop can
            # restore it. Read failure is non-critical: without a snapshot
            # the watchdog fallback restores internal management anyway.
            if self._max_charge_pre_block is None:
                current = await self._read_float(REG_MAX_CHARGE_POWER)
                if current is not None and current > 0:
                    self._max_charge_pre_block = current
                    self._max_charge_hw = current
                    _LOGGER.debug(
                        "Kostal: cached pre-block max charge power %.0f W",
                        current,
                    )

            watts = max(power_kw, 0.0) * 1000.0
            if not await self._write_float(REG_MAX_CHARGE_POWER, watts):
                return False
            if watts > 0:
                await self._pruefe_encoding(watts)

            self._active = ("charge_limit", watts)
            self._start_keepalive()
            _LOGGER.info(
                "Kostal: charge limit set (%.2f kW) — keepalive active",
                power_kw,
            )
            return True

        except Exception:
            _LOGGER.exception("Kostal: failed to set charge limit")
            self._close_client()
            return False

    async def async_set_discharge(
        self, power_kw: float, target_soc: float | None = None
    ) -> bool:
        """Force battery discharge at the given power (register 1034, positive).

        Kostal has no hardware target-SOC register: `target_soc` is accepted
        for interface compatibility but enforcement happens in the optimizer,
        which re-evaluates the SOC every 30 s and calls async_stop_forcible().
        The watchdog fallback to internal automatic covers the HA-crash case.
        """
        async with self._lock:
            return await self._set_discharge_locked(power_kw)

    async def _set_discharge_locked(self, power_kw: float) -> bool:
        try:
            if not await self._ensure_connected():
                return False

            # Stuck-register guard: leaving a charge block behind while
            # discharging is functionally harmless (charging is not wanted
            # during discharge), but restore it anyway so a later stop or
            # watchdog lapse never resumes with a stale 0-W charge limit.
            if (
                self._active is not None
                and self._active[0] == "charge_limit"
                and self._max_charge_pre_block is not None
            ):
                if await self._write_float(
                    REG_MAX_CHARGE_POWER, self._max_charge_pre_block
                ):
                    self._max_charge_pre_block = None

            watts = max(power_kw, 0.0) * 1000.0
            if not await self._write_float(REG_BATTERY_SETPOINT, watts):
                return False

            self._active = ("discharge", watts)
            self._start_keepalive()
            _LOGGER.info(
                "Kostal: discharge set (%.2f kW) — keepalive active", power_kw
            )
            return True

        except Exception:
            _LOGGER.exception("Kostal: failed to set discharge")
            self._close_client()
            return False

    async def async_stop_forcible(self) -> bool:
        """Stop forced charge/discharge, return to automatic mode.

        Explicitly zeroes the discharge setpoint and restores the max charge
        power (stuck-register guard), then stops feeding the watchdog — the
        inverter falls back to internal automatic management on timeout.
        """
        async with self._lock:
            return await self._stop_forcible_locked()

    async def _stop_forcible_locked(self) -> bool:
        # Clear the active command FIRST so a failed write below cannot
        # race with a concurrent keepalive rewrite of the old setpoint.
        self._active = None
        self._cancel_keepalive()
        try:
            if not await self._ensure_connected():
                return False

            if not await self._write_float(REG_BATTERY_SETPOINT, 0.0):
                return False

            if self._max_charge_pre_block is not None:
                if await self._write_float(
                    REG_MAX_CHARGE_POWER, self._max_charge_pre_block
                ):
                    _LOGGER.info(
                        "Kostal: restored max charge power to %.0f W",
                        self._max_charge_pre_block,
                    )
                    self._max_charge_pre_block = None
                else:
                    # False → optimizer retries stop next cycle. The register
                    # is volatile, so even a persistent failure resolves once
                    # the watchdog lapses back to internal management.
                    _LOGGER.warning(
                        "Kostal: failed to restore max charge power — retrying"
                    )
                    return False

            _LOGGER.info(
                "Kostal: stopped forcible mode — watchdog will hand back "
                "to internal automatic management"
            )
            return True

        except Exception:
            _LOGGER.exception("Kostal: failed to stop forcible mode")
            self._close_client()
            return False

    async def _pruefe_encoding(self, geschrieben_w: float) -> None:
        """Einmal pruefen, ob das Geraet das geschriebene Teil-Limit annimmt.

        Fuer den Blockierwert 0 sind Float32 und U32 bitgleich — ein
        Teil-Limit unterscheidet sie. Die Registerlisten der Community
        widersprechen sich an dieser Stelle (evcc schreibt Float, eine
        aeltere Liste dokumentiert U32), und der Fahrplan schreibt staendig
        Teil-Limits. Deshalb wird der erste solche Wert zurueckgelesen: Passt
        er nicht, steht es im Log, statt dass die Anlage still mit einem
        unsinnigen Limit laedt.

        Nur Diagnose — der Schreibvorgang gilt trotzdem als erfolgreich,
        denn die Abweichung kann auch am Geraet liegen (eigene Deckelung,
        Rundung auf ganze 100 W).
        """
        if self._encoding_geprueft:
            return
        self._encoding_geprueft = True
        gelesen = await self._read_float(REG_MAX_CHARGE_POWER)
        if gelesen is None:
            return
        # Grosszuegig: 10 % oder 100 W Toleranz decken Rundung und eigene
        # Deckelung ab; ein falsches Encoding liegt um Groessenordnungen daneben.
        toleranz = max(100.0, geschrieben_w * 0.1)
        if abs(gelesen - geschrieben_w) > toleranz:
            _LOGGER.warning(
                "Kostal: Ladelimit %.0f W geschrieben, aber %.0f W "
                "zurueckgelesen — moeglicherweise erwartet diese Firmware "
                "eine andere Kodierung von Register %d, oder das Geraet "
                "deckelt selbst. Das Blockieren des Ladens (0 W) ist davon "
                "nicht betroffen.",
                geschrieben_w, gelesen, REG_MAX_CHARGE_POWER,
            )
        else:
            _LOGGER.debug(
                "Kostal: Ladelimit verifiziert (%.0f W geschrieben, "
                "%.0f W gelesen)", geschrieben_w, gelesen,
            )

    def _merke_hardware_grenze(self, max_charge_w: float | None) -> None:
        """Ungestoerten 1038-Wert als Hardware-Maximum merken.

        Nur solange wir nicht selbst begrenzen: sonst hielte der Treiber sein
        eigenes Limit fuer die Grenze des Geraets, und Guard 1 kaeme nie
        wieder darueber hinaus.
        """
        if max_charge_w is None or max_charge_w <= 0:
            return
        if self._active is not None and self._active[0] == "charge_limit":
            return
        self._max_charge_hw = max_charge_w

    # ------------------------------------------------------------------
    # Fahrplan-Steuerschnittstelle (Schedule-Executor)
    # ------------------------------------------------------------------

    @property
    def supports_schedule_control(self) -> bool:
        """Der Fahrplan-Executor darf den Plenticore stellen."""
        return True

    async def async_get_charge_limit_kw(self) -> float | None:
        """Aktuell wirksames Ladelimit in kW — Register 1038.

        Anders als bei SunSpec gibt es kein Modus-Bit, das die Begrenzung
        erst scharf schaltet: 1038 wirkt immer. Der gelesene Wert ist damit
        unmittelbar das, wovon Guard 1 weiterrechnet — waehrend einer
        Entladung ebenso, dort steht der Sollwert in 1034 und 1038 bleibt
        die Ladegrenze.
        """
        async with self._lock:
            if not await self._ensure_connected():
                return None
            watts = await self._read_float(REG_MAX_CHARGE_POWER)
            if watts is None:
                return None
            self._merke_hardware_grenze(watts)
            return max(watts, 0.0) / 1000.0

    def get_charge_limit_max_kw(self) -> float | None:
        """Hardware-Maximum des Ladelimits in kW, aus dem Cache.

        Synchron, also nur aus dem, was ein frueherer Lauf ungestoert
        gelesen hat. Vor dem ersten Lesen None — Guard 1 hebt dann
        ungeclampt an, und das Geraet deckelt selbst.
        """
        return None if self._max_charge_hw is None else self._max_charge_hw / 1000.0

    def get_max_discharge_power_kw(self) -> float | None:
        """Maximale Entladeleistung in kW — Register 1040, aus dem Cache.

        Gefuellt von async_get_control_values(); das Register schreiben wir
        nie, es ist die Grenze, die Guard 2 nicht ueberschreiten darf.
        """
        return (
            None if self._max_discharge_hw is None
            else self._max_discharge_hw / 1000.0
        )

    async def async_get_control_values(self) -> list[dict]:
        """Stellgroessen fuer die Transparenz-Ansicht — direkt aus den Registern.

        Der Plenticore wird nicht ueber HA-Entitaeten gestellt, sondern per
        Modbus; get_control_entities() bleibt deshalb leer. Fehlschlaege
        liefern eine leere Liste statt zu werfen — die Ansicht ist Diagnose,
        kein Steuerpfad.
        """
        async with self._lock:
            if not await self._ensure_connected():
                return []
            setpoint = await self._read_float(REG_BATTERY_SETPOINT)
            max_charge = await self._read_float(REG_MAX_CHARGE_POWER)
            max_discharge = await self._read_float(REG_MAX_DISCHARGE_POWER)

        self._merke_hardware_grenze(max_charge)
        if max_discharge is not None and max_discharge > 0:
            self._max_discharge_hw = max_discharge

        def _w(wert: float | None) -> Any:
            return None if wert is None else round(wert)

        # Vorzeichen des Sollwerts ist die halbe Information: positiv heisst
        # entladen, negativ laden — ohne den Hinweis liest sich eine nackte
        # Zahl falschherum.
        richtung = ""
        if setpoint is not None:
            if setpoint > 1.0:
                richtung = " (entladen)"
            elif setpoint < -1.0:
                richtung = " (laden)"
        return [
            {
                "label": f"Batterie-Sollwert (Register {REG_BATTERY_SETPOINT}){richtung}",
                "value": _w(setpoint),
                "unit": "W",
                "role": "forcible",
            },
            {
                "label": f"Max. Ladeleistung (Register {REG_MAX_CHARGE_POWER})",
                "value": _w(max_charge),
                "unit": "W",
                "role": "charge_limit",
            },
            {
                "label": f"Max. Entladeleistung (Register {REG_MAX_DISCHARGE_POWER})",
                "value": _w(max_discharge),
                "unit": "W",
                "role": "discharge_limit",
            },
        ]

    @property
    def is_available(self) -> bool:
        """Whether the inverter is reachable.

        Connection is opened lazily (same rationale as Fronius): as long as
        a host is configured, report available — the real TCP probe happens
        inside _ensure_connected when an operation runs.
        """
        return bool(self._host)

    async def async_disconnect(self) -> None:
        """Stop keepalive and disconnect (called on entry unload)."""
        self._cancel_keepalive()
        self._active = None
        self._close_client()
