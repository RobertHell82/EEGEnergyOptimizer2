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
  1040  Battery max. discharge power limit  Float RW (Entladedeckel der
        Spitzenkappung, sonst unberuehrt)
  1078  Max. discharge limit from battery   Float R  (Rueckfallwert fuer 1040)
  210   Act. state of charge (%)            Float R
  1068  Battery work capacity (Wh)          Float R
  1080  Battery management mode             U16   R
        0 = internal, 1 = external digital I/O, 2 = external Modbus TCP

Beta (November 2026, am Geraet nicht geprueft): Entladedeckel ueber 1040 und
Netzladung ueber einen negativen 1034-Sollwert. Siehe
async_set_discharge_limit() und async_set_grid_charge().

Central architectural difference to Fronius: Kostal expects the setpoint to
be REWRITTEN CYCLICALLY (watchdog, timeout configurable in the service menu,
recommendation 60 s). On timeout the inverter falls back to its internal
automatic battery management — a built-in failsafe if HA dies mid-discharge.
The driver therefore runs its own keepalive task that rewrites the active
setpoint every KEEPALIVE_INTERVAL seconds, decoupled from the optimizer's
"only write on state change" deduplication.

The control registers are volatile (RAM, spec ch. 3.3) — cyclic writing is
harmless (no NVRAM wear). There is no hardware target-SOC
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
  5. Netzladung: laedt 1034 = −1000 W nachts (ohne PV) wirklich 1 kW aus dem
     Netz, oder verlangt der Plenticore dafuer eine eigene Freigabe?
     (Ansfelden plus den 5.5er dort: nie getestet)
  6. Entladedeckel: haelt 1040 = 1000 W nachts bei > 1 kW Hauslast die
     Batterie bei 1 kW? Belegt ist nur 1040 = 0 (evcc #26168). Und: setzt
     der Watchdog-Rueckfall 1040 auf den Vorwert zurueck oder bleibt er?
  7. Watchdog-Timeout der Anlage im Servicemenue ablesen — er bestimmt, wie
     lange die Batterie nach einer Entladung/Netzladung steht.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from datetime import datetime, timezone
from typing import Any

from ..const import CONF_BATTERY_POWER_SENSOR
from ..power_readings import read_power_kw
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
REG_MAX_DISCHARGE_POWER = 1040  # Float W — Entladedeckel (Spitzenkappung)

# Read registers used by driver/probe
REG_SOC = 210                 # Float %
REG_MAX_DISCHARGE_BATTERY = 1078  # Float W — Entladegrenze, die die Batterie meldet
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

# Watchdog-Rueckfall nach einem 1034-Sollwert (Ansfelden, 22. + 25.09.2026).
# 1034 ist ein Sollwert, kein Limit: 0 W heisst "Batterie steht", und es gibt
# keinen Wert, der "zurueck zur Automatik" bedeutet — das tut nur Schweigen
# bis zum Watchdog-Timeout. Jeder Schreibzugriff auf die Steuerregister
# fuettert diesen Watchdog aber, auch der 1038-Keepalive eines Ladelimits.
# Folgte auf einen 0-W-Sollwert ein Ladelimit, hielt der Kostal den 0-W-
# Sollwert daher stundenlang fest: 1038 = 4031 W, Batterie 30 W, 9 kW
# Einspeisung. Der Timeout ist im Service-Menue einstellbar und ueber Modbus
# nicht lesbar, also wird der Rueckfall GEMESSEN statt abgewartet: Die
# Batterie bewegt sich wieder, sobald die interne Automatik uebernimmt.
# Bis dahin schreibt der Treiber kein Steuerregister ausser einer Entladung.
RUECKFALL_BEWEGUNG_KW = 0.15   # gehaltener 0-W-Sollwert: gemessen 20–45 W
RUECKFALL_BERUHIGUNG_S = 20.0  # Messwerte davor zeigen noch den alten Befehl
RUECKFALL_MAX_S = 900.0        # Sicherheitsnetz, falls der Sensor ausfaellt
RUECKFALL_ABFRAGE_S = 10.0     # Abfragetakt, solange ein Ladelimit wartet

# Unter dieser Leistung ist eine Netzladung keine — sie wird wie eine
# Freigabe behandelt (gleiche Schwelle wie im Fronius-Treiber).
NETZLADEN_MIN_KW = 0.05

# Befehle, die 1034 stellen. Solange einer davon laeuft, haelt der
# Keepalive den Sollwert ohnehin am Leben; danach gilt die Ansfelden-Regel.
_SOLLWERT_BEFEHLE = ("discharge", "grid_charge")


def _jetzt() -> datetime:
    return datetime.now(timezone.utc)


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

    Zwei Dinge haelt der Keepalive am Leben, getrennt voneinander: den
    Befehl in ``self._active`` (Ladelimit 1038, Entladung oder Netzladung
    1034) und den Entladedeckel in ``self._deckel_w`` (1040). Sie liegen in
    verschiedenen Registern und gelten nebeneinander — ein Ladelimit mit
    Deckel ist der Normalfall der Spitzenkappung am Tag.
    """

    def __init__(self, hass: Any, config: dict) -> None:
        super().__init__(hass, config)
        self._host: str = config.get("kostal_modbus_host", "")
        self._port: int = int(config.get("kostal_modbus_port", 1502))
        self._client: Any = None  # AsyncModbusTcpClient (lazy)
        self._slave_id: int = KOSTAL_UNIT_ID
        # Active command the keepalive rewrites:
        #   ("discharge", watts) | ("charge_limit", watts)
        #   | ("grid_charge", watts — Betrag, geschrieben wird −watts) | None
        self._active: tuple[str, float] | None = None
        # Ziel-Ladestand der laufenden Netzladung (Prozent).
        self._netz_ziel_pct: float | None = None
        # Entladedeckel (1040) in W, den der Keepalive schreibt; None = keiner.
        self._deckel_w: float | None = None
        # Steht in 1040 ein Wert von uns? Dann muss er zurueck, auch wenn
        # _deckel_w schon None ist. Gesetzt VOR dem Schreiben — bei einem
        # Verbindungsabbruch ist unklar, ob der Wert ankam.
        self._deckel_beruehrt: bool = False
        # 1040 vor unserem ersten Deckel; ohne ihn gilt 1078.
        self._max_discharge_pre_deckel: float | None = None
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
        # Zeitpunkt des letzten 1034-Schreibens, solange der Sollwert noch
        # gelten kann; None = interne Automatik. Siehe RUECKFALL_*.
        self._sollwert_seit: datetime | None = None
        self._batterie_sensor: str = config.get(CONF_BATTERY_POWER_SENSOR, "")
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
                if address == REG_BATTERY_SETPOINT:
                    # In Oesterreich ist 1034 nach jeder Netzzuschaltung rund
                    # 10 min gesperrt (Gradientenfall, Fussnote 7 der Kostal-
                    # Spezifikation; 1038/1040 nicht). Der haeufigste Grund
                    # einer Ablehnung — er gehoert in die Telemetrie, damit
                    # niemand einem Treiberfehler nachjagt. Ob das Geraet
                    # ablehnt oder still ignoriert, ist am Geraet nicht geprueft.
                    return self._fehler(
                        "1034 gesperrt (Gradient nach Netzzuschaltung?)"
                    )
                return self._fehler(f"Modbus lehnt Register {address} ab")
            await asyncio.sleep(0.2)
            _LOGGER.debug("Kostal: wrote register %d = %.1f", address, value)
            return True
        except Exception:
            _LOGGER.exception(
                "Kostal: exception writing register %d (value=%.1f)",
                address, value,
            )
            self._close_client()
            return self._fehler(f"Verbindungsfehler bei Register {address}")

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

    async def _schreibe_sollwert(self, watts: float) -> bool:
        """1034 schreiben und merken, dass ab jetzt ein Sollwert gilt.

        Gemerkt wird schon vor dem Ergebnis: Bei einem Verbindungsabbruch ist
        unklar, ob der Wert angekommen ist, und ein irrtuemlich angenommener
        Sollwert kostet nur die Rueckfall-Messung, ein uebersehener dagegen
        eine stehende Batterie.
        """
        self._sollwert_seit = _jetzt()
        return await self._write_float(REG_BATTERY_SETPOINT, watts)

    def _rueckfall_offen(self) -> bool:
        """Haelt der Kostal womoeglich noch einen 1034-Sollwert fest?

        Solange ja, darf kein Steuerregister geschrieben werden — jeder
        Schreibzugriff verlaengert den Watchdog und damit den Sollwert.
        Beendet wird das Warten durch eine Batterieleistung, die erst nach
        dem Schreiben gemessen wurde und nicht zum gehaltenen 0-W-Sollwert
        passt, oder nach RUECKFALL_MAX_S. Bewegt sich bis dahin nichts
        (Nacht, Batterie leer), macht der Sollwert auch keinen Unterschied.
        """
        seit = self._sollwert_seit
        if seit is None:
            return False
        dauer = (_jetzt() - seit).total_seconds()
        if dauer >= RUECKFALL_MAX_S:
            _LOGGER.info(
                "Kostal: nach %.0f s ohne Messbeleg Rueckfall zur internen "
                "Automatik angenommen", dauer,
            )
            self._sollwert_seit = None
            return False
        state = self._hass.states.get(self._batterie_sensor) if self._batterie_sensor else None
        gemessen = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
        if not isinstance(gemessen, datetime):
            return True
        if (gemessen - seit).total_seconds() < RUECKFALL_BERUHIGUNG_S:
            return True
        leistung = read_power_kw(self._hass, self._batterie_sensor)
        if leistung is None or abs(leistung) < RUECKFALL_BEWEGUNG_KW:
            return True
        _LOGGER.info(
            "Kostal: interne Automatik hat nach %.0f s uebernommen "
            "(Batterie %.2f kW) — Steuerregister wieder frei", dauer, leistung,
        )
        self._sollwert_seit = None
        return False

    def _steuert_sollwert(self) -> bool:
        """Laeuft gerade ein Befehl, der 1034 stellt (Entladung, Netzladung)?

        Dann ist der gehaltene Sollwert gewollt, und _rueckfall_offen() darf
        nicht gefragt werden: Die Batterie bewegt sich ja — wegen unseres
        Befehls, nicht wegen der internen Automatik.
        """
        return self._active is not None and self._active[0] in _SOLLWERT_BEFEHLE

    def _register_gesperrt(self) -> bool:
        """Duerfen 1038/1040 jetzt NICHT geschrieben werden (Ansfelden-Regel)?"""
        return not self._steuert_sollwert() and self._rueckfall_offen()

    async def _ladestand_pct(self) -> float | None:
        """Register 210 — der Ladestand, mit dem der Plenticore selbst rechnet."""
        soc = await self._read_float(REG_SOC)
        if soc is None or not 0.0 <= soc <= 100.0:
            return None
        return soc

    # ------------------------------------------------------------------
    # Entladedeckel (Register 1040)
    # ------------------------------------------------------------------

    async def _deckel_schreiben(self, watts: float) -> bool:
        """1040 schreiben; vor dem ersten Mal den Vorwert sichern."""
        if not self._deckel_beruehrt:
            vorher = await self._read_float(REG_MAX_DISCHARGE_POWER)
            if vorher is not None and vorher > 0:
                self._max_discharge_pre_deckel = vorher
                self._max_discharge_hw = vorher
            self._deckel_beruehrt = True
        return await self._write_float(REG_MAX_DISCHARGE_POWER, watts)

    async def _deckel_zuruecksetzen(self) -> bool:
        """1040 auf den Wert vor unserem ersten Deckel zurueckschreiben.

        Ohne gesicherten Vorwert (Lesefehler beim ersten Deckel) gilt, was
        die Batterie selbst als Entladegrenze meldet (1078). Ob der Kostal
        1040 beim Watchdog-Rueckfall von sich aus zuruecksetzt, ist nicht
        dokumentiert und am Geraet nicht geprueft — deshalb schreibt der
        Treiber ihn selbst zurueck, statt sich darauf zu verlassen.
        """
        if not self._deckel_beruehrt:
            return True
        wert = self._max_discharge_pre_deckel
        if wert is None:
            wert = await self._read_float(REG_MAX_DISCHARGE_BATTERY)
            if wert is None or wert <= 0:
                return self._fehler("Entladegrenze der Batterie unbekannt")
        if not await self._write_float(REG_MAX_DISCHARGE_POWER, wert):
            return False
        _LOGGER.info("Kostal: Entladedeckel aufgehoben (1040 = %.0f W)", wert)
        self._deckel_beruehrt = False
        self._max_discharge_pre_deckel = None
        return True

    async def _deckel_auffrischen(self, jitter: float) -> None:
        """Keepalive-Teil fuer 1040: Deckel nachschreiben oder zuruecknehmen.

        Waehrend einer Entladung oder Netzladung wird kein Deckel geschrieben
        (er bremste den Befehl), ein Zuruecksetzen schon. Nach dem Befehl
        wartet beides auf den Watchdog-Rueckfall — jeder Schreibzugriff auf
        1040 hielte sonst den letzten 1034-Sollwert fest; evcc hat gemessen,
        dass alle in der Watchdog-Periode geschriebenen Register bis zum
        Timeout gelten.
        """
        if self._steuert_sollwert():
            if self._deckel_beruehrt and self._deckel_w is None:
                await self._deckel_zuruecksetzen()
            return
        if self._rueckfall_offen():
            return
        if self._deckel_w is not None:
            if not await self._deckel_schreiben(self._deckel_w + jitter):
                _LOGGER.warning(
                    "Kostal: Entladedeckel %.0f W nicht nachgeschrieben — "
                    "naechster Versuch im naechsten Takt", self._deckel_w,
                )
        elif self._deckel_beruehrt:
            await self._deckel_zuruecksetzen()

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

    def _keepalive_neu_starten(self) -> None:
        """Neu starten, damit ein Vorgemerktes sofort im Abfragetakt misst.

        Ein laufender Entlade-Keepalive schliefe sonst noch bis zu
        KEEPALIVE_INTERVAL, bevor er den Rueckfall pruefen kann.
        """
        self._cancel_keepalive()
        self._start_keepalive()

    def _hat_arbeit(self) -> bool:
        return (
            self._active is not None
            or self._deckel_w is not None
            or self._deckel_beruehrt
        )

    def _keepalive_takt(self) -> float:
        wartet = (
            self._sollwert_seit is not None
            and not self._steuert_sollwert()
            and self._hat_arbeit()
        )
        return (
            min(RUECKFALL_ABFRAGE_S, KEEPALIVE_INTERVAL) if wartet
            else KEEPALIVE_INTERVAL
        )

    async def _befehl_auffrischen(self, jitter: float) -> None:
        """Keepalive-Teil fuer den Befehl: 1034 bzw. 1038 nachschreiben."""
        active = self._active
        if active is None:
            return
        kind, watts = active
        if kind == "charge_limit":
            # Ein vorgemerktes Ladelimit erst schreiben, wenn der Kostal den
            # alten 1034-Sollwert losgelassen hat.
            if self._rueckfall_offen():
                return
            ok = await self._write_float(REG_MAX_CHARGE_POWER, watts + jitter)
        elif kind == "grid_charge":
            # Einen Ziel-Ladestand kennt der Plenticore nicht: am Ziel selbst
            # anhalten (1034 = 0) und dann wie nach einer Entladung warten.
            ziel = 100.0 if self._netz_ziel_pct is None else self._netz_ziel_pct
            soc = await self._ladestand_pct()
            if soc is not None and soc >= ziel:
                _LOGGER.info(
                    "Kostal: Netzladung am Ziel (%.1f %% ≥ %.0f %%) — Freigabe",
                    soc, ziel,
                )
                await self._stop_forcible_locked(keepalive_stoppen=False)
                return
            ok = await self._schreibe_sollwert(-(watts + jitter))
        else:  # discharge
            ok = await self._schreibe_sollwert(watts + jitter)
        if not ok:
            _LOGGER.warning(
                "Kostal: keepalive rewrite failed (%s, %.0f W) — "
                "will retry next interval; watchdog may fall back "
                "to internal mode until then",
                kind, watts,
            )

    async def _keepalive_loop(self) -> None:
        """Rewrite the active command and the discharge cap until cancelled.

        Failures are logged but tolerated: the AT grid-connection ramp (spec
        footnote 7) can reject writes to 1034 for ~10 minutes after grid
        reconnection — cyclic writing heals this by itself, and a watchdog
        lapse in the meantime only means temporary internal automatic mode.
        """
        try:
            while True:
                await asyncio.sleep(self._keepalive_takt())
                async with self._lock:
                    if not self._hat_arbeit():
                        return
                    if not await self._ensure_connected():
                        _LOGGER.warning(
                            "Kostal: keepalive write skipped — not connected"
                        )
                        continue
                    self._jitter_toggle = not self._jitter_toggle
                    jitter = _JITTER_W if self._jitter_toggle else 0.0
                    await self._befehl_auffrischen(jitter)
                    await self._deckel_auffrischen(jitter)
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
        if self._nicht_endlich(power_kw):
            return False
        async with self._lock:
            return await self._set_charge_limit_locked(power_kw)

    async def _set_charge_limit_locked(self, power_kw: float) -> bool:
        try:
            if not await self._ensure_connected():
                return False

            # Stuck-register guard: when switching from a discharge, reset
            # the setpoint explicitly instead of trusting the watchdog
            # timeout (community-reported bug: old setpoints survive a mode
            # change on some firmwares). Damit gilt ab jetzt 0 W — das
            # Ladelimit wartet auf den Watchdog-Rueckfall (RUECKFALL_*).
            # Eine Netzladung ebenso: Ein Ladelimit allein hielte sie nicht an.
            if self._steuert_sollwert():
                if not await self._schreibe_sollwert(0.0):
                    return False
                self._active = None
                self._netz_ziel_pct = None

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
            if self._rueckfall_offen():
                # Jetzt 1038 zu schreiben hielte den 0-W-Sollwert am Leben.
                # Der Keepalive schreibt das Limit, sobald der Kostal wieder
                # selbst regelt; bis dahin steht die Batterie ohnehin.
                self._active = ("charge_limit", watts)
                self._keepalive_neu_starten()
                _LOGGER.info(
                    "Kostal: Ladelimit %.2f kW vorgemerkt — warte auf den "
                    "Watchdog-Rueckfall des Batterie-Sollwerts", power_kw,
                )
                return True
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
            return self._erfolg()

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
        if self._nicht_endlich(power_kw, target_soc):
            return False
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

            # Ein Entladedeckel bremste die Entladung — 1040 vorher zurueck.
            # Der Executor hebt ihn ohnehin vorher auf; schlaegt es hier
            # fehl, laeuft die Entladung trotzdem an (Guard 3 meldet eine
            # gebremste), und der Keepalive versucht das Zuruecksetzen erneut.
            self._deckel_w = None
            if self._deckel_beruehrt and not await self._deckel_zuruecksetzen():
                _LOGGER.warning(
                    "Kostal: Entladedeckel vor der Entladung nicht aufgehoben"
                )

            watts = max(power_kw, 0.0) * 1000.0
            if not await self._schreibe_sollwert(watts):
                return False

            self._active = ("discharge", watts)
            self._netz_ziel_pct = None
            self._start_keepalive()
            _LOGGER.info(
                "Kostal: discharge set (%.2f kW) — keepalive active", power_kw
            )
            return self._erfolg()

        except Exception:
            _LOGGER.exception("Kostal: failed to set discharge")
            self._close_client()
            return False

    async def async_stop_forcible(self) -> bool:
        """Stop forced charge/discharge, return to automatic mode.

        Explicitly zeroes the setpoint after a discharge or grid charge and
        restores the max charge power and the discharge cap (stuck-register
        guard), then stops feeding the watchdog — the inverter falls back to
        internal automatic management on timeout.
        """
        async with self._lock:
            return await self._stop_forcible_locked()

    async def _stop_forcible_locked(self, keepalive_stoppen: bool = True) -> bool:
        """Freigeben. Aufrufer haelt den Lock.

        ``keepalive_stoppen=False`` nur aus dem Keepalive selbst (Netzladung
        am Ziel): Sich selbst abzubrechen beendete die Freigabe mittendrin.
        """
        # Clear the active command FIRST so a failed write below cannot
        # race with a concurrent keepalive rewrite of the old setpoint.
        vorher = self._active
        ziel_vorher = self._netz_ziel_pct
        self._active = None
        self._netz_ziel_pct = None
        # Freigeben heisst auch: kein Deckel mehr (base.InverterBase). Der
        # Executor schreibt ihn danach neu, wenn er noch gilt.
        self._deckel_w = None
        if keepalive_stoppen:
            self._cancel_keepalive()
        try:
            if not await self._ensure_connected():
                return False

            # 1034 nur zuruecksetzen, wenn wirklich entladen oder aus dem
            # Netz geladen wurde. Ein 0-W-Sollwert "zur Sicherheit" ist kein
            # Freigeben, sondern ein Befehl, die Batterie anzuhalten — und das
            # nachfolgende Ladelimit hielte ihn am Leben (Ansfelden 25.09.2026).
            if vorher is not None and vorher[0] in _SOLLWERT_BEFEHLE:
                if not await self._schreibe_sollwert(0.0):
                    # Beim naechsten Stop-Versuch erneut zuruecksetzen.
                    self._active = vorher
                    self._netz_ziel_pct = ziel_vorher
                    return False

            # Waehrend des Rueckfalls nichts schreiben; der Schnappschuss
            # bleibt fuer den naechsten Schreibpfad erhalten.
            if self._max_charge_pre_block is not None and not self._rueckfall_offen():
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

            # Den Deckel ebenso zuruecknehmen — ausser waehrend des
            # Rueckfalls: Dann tut es der Keepalive, sobald die Batterie sich
            # wieder bewegt (er laeuft dafuer weiter bzw. neu an).
            if self._deckel_beruehrt:
                if self._rueckfall_offen():
                    if keepalive_stoppen:
                        self._keepalive_neu_starten()
                elif not await self._deckel_zuruecksetzen():
                    _LOGGER.warning(
                        "Kostal: Entladedeckel nicht aufgehoben — retrying"
                    )
                    return False

            _LOGGER.info(
                "Kostal: stopped forcible mode — watchdog will hand back "
                "to internal automatic management"
            )
            return self._erfolg()

        except Exception:
            _LOGGER.exception("Kostal: failed to stop forcible mode")
            self._close_client()
            return False

    # ------------------------------------------------------------------
    # Entladedeckel (Spitzenkappung, Beta)
    # ------------------------------------------------------------------

    @property
    def supports_discharge_limit(self) -> bool:
        """Ueber Register 1040 (max. Entladeleistung), Beta.

        Am Geraet nicht geprueft (Stand 02.11.2026): Dass 1040 = 0 die
        Entladung anhaelt, belegt nur evcc (#26168); ein Deckel ueber 0 —
        der Normalfall der Spitzenkappung — ist nirgends gemessen. Ebenso
        offen, ob der Kostal 1040 beim Watchdog-Rueckfall zuruecksetzt.
        """
        return True

    async def async_set_discharge_limit(self, power_kw: float | None) -> bool:
        """Entladedeckel in 1040 setzen (kW) oder mit ``None`` aufheben.

        1040 ist ein eigenes Register, ein Ladelimit in 1038 bleibt stehen;
        der Keepalive schreibt beide zyklisch neu (der Watchdog gilt fuer
        alle Steuerregister). Aufheben schreibt den Vorwert zurueck —
        gelesen vor dem ersten Deckel, sonst 1078. Ohne eigenen Deckel ist
        ``None`` ein No-op: Ein Schreibzugriff auf 1040 schaltete die externe
        Steuerung erst ein, und der Executor hebt beim Start „zur Sicherheit"
        auf.

        Nach einer Entladung oder Netzladung wartet der Deckel wie das
        Ladelimit auf den Watchdog-Rueckfall (Ansfelden-Regel): Jeder
        Schreibzugriff auf 1040 hielte sonst den letzten 1034-Sollwert fest.
        Bis dahin steht die Batterie ohnehin. Waehrend einer Entladung oder
        Netzladung wird der Deckel nur gemerkt — er bremste den Befehl.
        """
        if power_kw is not None and self._nicht_endlich(power_kw):
            return False
        async with self._lock:
            try:
                if power_kw is None:
                    return await self._deckel_aufheben_locked()
                return await self._deckel_setzen_locked(max(0.0, float(power_kw)))
            except Exception:
                _LOGGER.exception("Kostal: Entladedeckel fehlgeschlagen")
                self._close_client()
                return self._fehler("Ausnahme beim Entladedeckel")

    async def _deckel_setzen_locked(self, kw: float) -> bool:
        watts = kw * 1000.0
        if self._steuert_sollwert():
            self._deckel_w = watts
            return self._erfolg()
        if self._rueckfall_offen():
            self._deckel_w = watts
            self._keepalive_neu_starten()
            _LOGGER.info(
                "Kostal: Entladedeckel %.2f kW vorgemerkt — warte auf den "
                "Watchdog-Rueckfall des Batterie-Sollwerts", kw,
            )
            return True
        if not await self._ensure_connected():
            return self._fehler("Modbus nicht erreichbar")
        if not await self._deckel_schreiben(watts):
            # Was jetzt in 1040 steht, ist unklar — _deckel_beruehrt bleibt,
            # damit ein Aufheben es sicher zuruecksetzt.
            return False
        self._deckel_w = watts
        self._start_keepalive()
        _LOGGER.info("Kostal: Entladedeckel %.2f kW gesetzt (1040)", kw)
        return self._erfolg()

    async def _deckel_aufheben_locked(self) -> bool:
        self._deckel_w = None
        if not self._deckel_beruehrt:
            return self._erfolg()
        if self._register_gesperrt():
            # Zuruecksetzen erst nach dem Rueckfall; bis dahin steht die
            # Batterie, der alte Deckel bremst also nichts.
            self._keepalive_neu_starten()
            return True
        if not await self._ensure_connected():
            return self._fehler("Modbus nicht erreichbar")
        if not await self._deckel_zuruecksetzen():
            return False
        return self._erfolg()

    # ------------------------------------------------------------------
    # Netzladung (Beta): Batterie aus PV und Netz laden
    # ------------------------------------------------------------------

    @property
    def supports_grid_charge(self) -> bool:
        """Ueber einen negativen 1034-Sollwert (DC-Batterieleistung), Beta.

        Am Geraet nicht geprueft (Stand 02.11.2026): In Ansfelden lief nur
        die Entladung (1034 positiv); ein negativer Sollwert ist laut
        Spezifikation „Laden" und laut Community auch aus dem Netz — ob der
        Plenticore dafuer zusaetzlich das Laden aus dem Netz freigegeben
        haben will, ist offen. Gilt auch fuer den 5.5er dort.
        """
        return True

    async def async_set_grid_charge(
        self, power_kw: float, target_soc: float | None = None
    ) -> bool:
        """Batterie mit ``power_kw`` laden, PV zuerst, den Rest aus dem Netz.

        1034 ist die DC-Batterieleistung: −W heisst „lade mit W", woher die
        Energie kommt, ergibt sich — PV zuerst, der Rest aus dem Netz. Vorher
        gehen 1038 (ein Ladelimit von 0 blockierte die Ladung vermutlich) und
        1040 auf ihre Vorwerte zurueck.

        Einen Ziel-Ladestand kennt der Plenticore nicht. Der Treiber liest
        Register 210 bei jedem Schreiben und in jedem Keepalive und haelt am
        Ziel (ohne Ziel: 100 %) mit 1034 = 0 an; danach wartet er wie nach
        einer Entladung auf den Watchdog-Rueckfall. Unter
        ``NETZLADEN_MIN_KW`` ist es eine Freigabe.

        In Oesterreich ist 1034 nach jeder Netzzuschaltung rund 10 min
        gesperrt (Gradientenfall) — die Ablehnung kommt mit eigenem Grund
        zurueck, der Executor versucht es gebremst erneut.
        """
        if self._nicht_endlich(power_kw, target_soc):
            return False
        async with self._lock:
            return await self._netzladung_schreiben(power_kw, target_soc)

    async def _netzladung_schreiben(
        self, power_kw: float, target_soc: float | None
    ) -> bool:
        """Netzladung schreiben oder am Ziel freigeben. Aufrufer haelt den Lock."""
        try:
            if not await self._ensure_connected():
                return self._fehler("Modbus nicht erreichbar")
            ziel = 100.0 if target_soc is None else min(100.0, float(target_soc))
            leistung = max(0.0, float(power_kw))
            soc = await self._ladestand_pct()
            am_ziel = soc is not None and soc >= ziel
            if leistung < NETZLADEN_MIN_KW or am_ziel:
                if am_ziel:
                    _LOGGER.info(
                        "Kostal: Netzladung am Ziel (%.1f %% ≥ %.0f %%) — Freigabe",
                        soc, ziel,
                    )
                ok = await self._stop_forcible_locked()
                return (
                    self._erfolg() if ok
                    else self._fehler("Freigabe am Ziel fehlgeschlagen")
                )

            # Ein Ladelimit von uns muss weg, sonst laedt die Batterie nicht.
            if self._max_charge_pre_block is not None:
                if not await self._write_float(
                    REG_MAX_CHARGE_POWER, self._max_charge_pre_block
                ):
                    return False
                self._max_charge_pre_block = None
            elif self._active is not None and self._active[0] == "charge_limit":
                # Ladelimit ohne gesicherten Vorwert (Lesefehler beim ersten
                # Limit): auf das gelernte Hardware-Maximum.
                if self._max_charge_hw is None:
                    return self._fehler("Ladelimit-Vorwert unbekannt")
                if not await self._write_float(
                    REG_MAX_CHARGE_POWER, self._max_charge_hw
                ):
                    return False
            if self._active is not None and self._active[0] == "charge_limit":
                # Das Limit gilt nicht mehr — der Keepalive darf es nicht
                # wieder hineinschreiben, falls 1034 gleich scheitert.
                self._active = None

            # 1040 bremst das Laden nicht; zurueck muss er trotzdem, solange
            # 1034 ohnehin geschrieben wird (danach wartete er auf den Rueckfall).
            self._deckel_w = None
            if self._deckel_beruehrt and not await self._deckel_zuruecksetzen():
                _LOGGER.warning(
                    "Kostal: Entladedeckel vor der Netzladung nicht aufgehoben"
                )

            watts = leistung * 1000.0
            if not await self._schreibe_sollwert(-watts):
                return False

            self._active = ("grid_charge", watts)
            self._netz_ziel_pct = ziel
            self._start_keepalive()
            _LOGGER.info(
                "Kostal: Netzladung %.2f kW bis %.0f %% (1034 = %.0f W)",
                leistung, ziel, -watts,
            )
            return self._erfolg()
        except Exception:
            _LOGGER.exception("Kostal: Netzladung fehlgeschlagen")
            self._close_client()
            return self._fehler("Ausnahme beim Setzen der Netzladung")

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

        Gefuellt von async_get_control_values() und vor dem ersten
        Entladedeckel — nur aus einem 1040, in dem kein Deckel von uns steht.
        Sonst hielte der Treiber den eigenen Deckel fuer die Grenze der
        Batterie, und Guard 2 wie die Spitzenkappung rechneten mit ihm.
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
        if (
            max_discharge is not None and max_discharge > 0
            and not self._deckel_beruehrt
        ):
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
        if self._sollwert_seit is not None and not self._steuert_sollwert():
            richtung += " — warte auf Watchdog-Rückfall"
        deckel = ""
        if self._deckel_w is not None:
            deckel = " — Entladedeckel"
            if self._register_gesperrt():
                deckel += " vorgemerkt"
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
                "label": f"Max. Entladeleistung (Register {REG_MAX_DISCHARGE_POWER}){deckel}",
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
        self._netz_ziel_pct = None
        self._deckel_w = None
        self._close_client()
