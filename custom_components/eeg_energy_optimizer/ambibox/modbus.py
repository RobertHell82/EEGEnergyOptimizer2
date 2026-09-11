"""Modbus-TCP-Zugriff auf eine Ambibox (sidOS).

Grundlage ist das Herstellerdokument „User Interface (Modbus TCP) – v1 –
sidOS – ambibox", Public release version 1.3.0. sidOS ist nicht nur die
Wallbox-Firmware, sondern ein Energiemanagement mit fünf Geräteklassen zu je
zehn Instanzen. Uns interessiert die Klasse EV Charger:

    Input Register (FC 4):   4000 + (Anschluss − 1) × 200
    Holding Register (FC 16): 3000 + (Anschluss − 1) × 100

Jeder Wert belegt zwei Register (32 bit, high word zuerst). Die Offsets
unten sind relativ zur Basisadresse.

Geschrieben wird nur auf ausdrückliche Anweisung (manueller Test im Panel).
Der Fahrplan steuert die Wallbox nicht: Das Dokument beantwortet weder, wie
lange ein Sollwert ohne Nachschreiben gilt, noch mit welchem Vorzeichen
geladen wird, noch was bei gleichzeitiger Regelung durch sidOS passiert.
Genau diese Fragen soll der manuelle Test am Gerät beantworten.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from typing import Any

try:
    from pymodbus.client import AsyncModbusTcpClient
except ImportError:  # pragma: no cover — Testumgebung ohne pymodbus
    AsyncModbusTcpClient = None  # type: ignore[assignment,misc]

_LOGGER = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Adressraster
# --------------------------------------------------------------------------
INPUT_BASE = 4000
INPUT_STRIDE = 200
HOLDING_BASE = 3000
HOLDING_STRIDE = 100
# Anschlüsse 1..10 laut Dokument.
MAX_CONNECTOR = 10

# --------------------------------------------------------------------------
# Input-Register, Offset zur Basis. 0–46 ist der generische
# Wechselrichter-Block, ab 48 die Fahrzeugbatterie, ab 80 die Ladesitzung.
# --------------------------------------------------------------------------
OFF_POWER_AC = 18            # int32  W
OFF_ENERGY_IMPORT = 26       # float  Wh
OFF_ENERGY_EXPORT = 28       # float  Wh
OFF_NUMBER_PHASES = 36       # uint32
OFF_CAPACITY = 48            # float  Wh
OFF_MIN_SOC = 50             # float  %
OFF_MAX_SOC = 52             # float  %
OFF_SOC = 54                 # float  %
OFF_SOH = 56                 # float  %
OFF_TIME_TO_FULL = 58        # uint32 s
OFF_CYCLES = 60              # uint32
OFF_MIN_CHARGE_POWER = 62    # uint32 W
OFF_MAX_CHARGE_POWER = 64    # uint32 W
OFF_MIN_DISCHARGE_POWER = 66  # uint32 W
OFF_MAX_DISCHARGE_POWER = 68  # uint32 W
OFF_BATTERY_STATE = 70       # uint32 enum
OFF_BATTERY_TEMP = 72        # float  °C
OFF_BATTERY_ERROR = 74       # uint32 Flagset
OFF_CONTROL_MODE = 76        # uint32 enum
OFF_POWER_DC_BATTERY = 78    # int32  W
OFF_CHARGE_PROTOCOL = 80     # uint32 enum
OFF_SESSION_STATE = 82       # uint32 enum
OFF_EV_CONNECTED = 84        # uint32 bool
OFF_SECONDS_TO_DEPARTURE = 86  # uint32 s
OFF_DEPARTURE_SOC = 88       # float  %
OFF_DEPARTURE_ENERGY = 90    # uint32 Wh
OFF_MIN_ENERGY_REQUEST = 92  # uint32 Wh
OFF_MAX_ENERGY_REQUEST = 94  # uint32 Wh
OFF_EVCHARGER_ERROR = 96     # uint32 Flagset
OFF_SESSION_IMPORT = 98      # float  Wh
OFF_SESSION_EXPORT = 100     # float  Wh
OFF_REPLUG_REQUIRED = 102    # uint32 bool
# Der Block endet nach Offset 102 (+2 Register) — genau 104 Register, die in
# einen einzigen Modbus-Lesevorgang passen (Grenze 125).
INPUT_LENGTH = 104

# --------------------------------------------------------------------------
# Holding-Register, Offset zur Basis.
# --------------------------------------------------------------------------
OFF_TARGET_POWER = 0   # int32  W — Vorzeichenkonvention ungeprüft
OFF_WAKE_UP = 2        # uint32, nur Wert 1
OFF_SET_SLEEP = 4      # uint32, nur Wert 1
OFF_STOP_CHARGE = 6    # uint32, nur Wert 1


def _slave_kw(client: Any, unit_id: int) -> dict:
    """Passenden Parameternamen für die aktive pymodbus-Version wählen.

    pymodbus 3.9 hat ``slave`` in ``device_id`` umbenannt; ältere Stände
    kennen nur ``slave``. Gleiche Erkennung wie im Fronius-Treiber.
    """
    import inspect
    for methode in ("read_input_registers", "write_registers"):
        try:
            sig = inspect.signature(getattr(client, methode))
        except (AttributeError, TypeError, ValueError):
            continue
        if "device_id" in sig.parameters:
            return {"device_id": unit_id}
        return {"slave": unit_id}
    return {"slave": unit_id}


def input_base(connector: int) -> int:
    """Basisadresse des Input-Blocks für einen Anschluss (1-basiert)."""
    return INPUT_BASE + (connector - 1) * INPUT_STRIDE


def holding_base(connector: int) -> int:
    """Basisadresse des Holding-Blocks für einen Anschluss (1-basiert)."""
    return HOLDING_BASE + (connector - 1) * HOLDING_STRIDE


# --------------------------------------------------------------------------
# Dekodierung: zwei Register je Wert, high word zuerst
# --------------------------------------------------------------------------


def _raw32(regs: list[int], offset: int) -> bytes | None:
    """Die vier Bytes eines 32-Bit-Werts aus dem gelesenen Block."""
    if offset < 0 or offset + 1 >= len(regs):
        return None
    try:
        return struct.pack(">HH", regs[offset] & 0xFFFF, regs[offset + 1] & 0xFFFF)
    except (struct.error, TypeError):
        return None


def read_f32(regs: list[int], offset: int) -> float | None:
    """IEEE-754-Gleitkommawert. NaN und Inf gelten als „kein Wert"."""
    raw = _raw32(regs, offset)
    if raw is None:
        return None
    value = struct.unpack(">f", raw)[0]
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return float(value)


def read_i32(regs: list[int], offset: int) -> int | None:
    """Vorzeichenbehaftete 32-Bit-Ganzzahl."""
    raw = _raw32(regs, offset)
    return None if raw is None else int(struct.unpack(">i", raw)[0])


def read_u32(regs: list[int], offset: int) -> int | None:
    """Vorzeichenlose 32-Bit-Ganzzahl."""
    raw = _raw32(regs, offset)
    return None if raw is None else int(struct.unpack(">I", raw)[0])


def read_bool(regs: list[int], offset: int) -> bool | None:
    """Wahrheitswert — im Dokument als eigener Typ geführt, auf dem Draht
    aber dieselben zwei Register wie alles andere."""
    value = read_u32(regs, offset)
    return None if value is None else bool(value)


class AmbiboxModbus:
    """Liest den EV-Charger-Block einer Ambibox über Modbus TCP.

    Ein Aufruf von :meth:`async_read_block` holt den kompletten Block von
    104 Registern in einem Zug. Einzelabfragen je Wert wären ein Vielfaches
    an Round-Trips für dieselben Daten — und die Werte kämen aus
    verschiedenen Momenten, was bei Ladeleistung gegen Ladestand sichtbar
    wäre.
    """

    def __init__(
        self,
        host: str,
        port: int = 502,
        unit_id: int = 1,
        connector: int = 1,
    ) -> None:
        self._host = host
        self._port = int(port)
        self._unit_id = int(unit_id)
        self._connector = int(connector)
        self._client: Any = None
        self._lock = asyncio.Lock()
        self.last_error: str | None = None
        self.last_success: float | None = None
        self.read_failures = 0
        # Zahl der Schreibvorgänge — für die Diagnose interessant, weil an
        # dieser Wallbox nur auf ausdrückliche Anweisung geschrieben wird.
        self.writes = 0

    # -- Eigenschaften ----------------------------------------------------

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def unit_id(self) -> int:
        return self._unit_id

    @property
    def connector(self) -> int:
        return self._connector

    @property
    def input_base(self) -> int:
        return input_base(self._connector)

    @property
    def holding_base(self) -> int:
        return holding_base(self._connector)

    @property
    def connected(self) -> bool:
        return bool(self._client is not None and getattr(self._client, "connected", False))

    # -- Verbindung -------------------------------------------------------

    async def async_connect(self) -> bool:
        """Verbinden, wenn nötig. Ein Versuch je Aufruf — der Lesetakt
        wiederholt ohnehin, ein Retry-Sturm brächte nur Wartezeit im
        Event-Loop."""
        if self.connected:
            return True
        if AsyncModbusTcpClient is None:
            self._record_error("connect", "pymodbus ist nicht installiert")
            return False
        self._close_client()
        try:
            self._client = AsyncModbusTcpClient(self._host, port=self._port)
            await self._client.connect()
        except Exception as exc:  # noqa: BLE001 — jeder Fehler ist „nicht erreichbar"
            self._record_error("connect", exc)
            self._close_client()
            return False
        if not self.connected:
            self._record_error("connect", f"keine Verbindung zu {self._host}:{self._port}")
            return False
        _LOGGER.debug("Ambibox: verbunden mit %s:%s", self._host, self._port)
        return True

    def _close_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 — beim Aufräumen ist alles egal
                pass
            self._client = None

    async def async_close(self) -> None:
        """Verbindung schließen (Integration wird entladen)."""
        async with self._lock:
            self._close_client()

    # -- Lesen ------------------------------------------------------------

    async def async_read_block(self) -> list[int] | None:
        """Den gesamten EV-Charger-Input-Block lesen.

        Rückgabe sind die Rohregister; die Deutung übernimmt der Controller.
        None heißt: nicht erreichbar oder Modbus-Fehler.
        """
        async with self._lock:
            if not await self.async_connect():
                return None
            try:
                result = await self._client.read_input_registers(
                    address=self.input_base,
                    count=INPUT_LENGTH,
                    **_slave_kw(self._client, self._unit_id),
                )
            except Exception as exc:  # noqa: BLE001
                self._record_error("read", exc)
                # Nach einem Transportfehler ist die Verbindung meist hin —
                # der nächste Lauf baut sie neu auf.
                self._close_client()
                return None
            if result is None or result.isError():
                self._record_error("read", f"Modbus-Fehler an Adresse {self.input_base}")
                return None
            regs = list(getattr(result, "registers", []) or [])
            if len(regs) < INPUT_LENGTH:
                self._record_error(
                    "read", f"nur {len(regs)} von {INPUT_LENGTH} Registern erhalten"
                )
                return None
            self.last_error = None
            self.last_success = time.time()
            self.read_failures = 0
            return regs

    def _record_error(self, wo: str, exc: Exception | str) -> None:
        self.read_failures += 1
        self.last_error = f"{wo}: {exc}"
        # Nur der erste Fehler einer Serie ist eine Meldung wert — danach
        # steht dieselbe Zeile sonst alle 15 Sekunden im Protokoll.
        if self.read_failures == 1:
            _LOGGER.warning(
                "Ambibox %s:%s — %s", self._host, self._port, self.last_error
            )
        else:
            _LOGGER.debug(
                "Ambibox %s:%s — %s (Fehler %d in Folge)",
                self._host, self._port, self.last_error, self.read_failures,
            )

    # -- Schreiben ---------------------------------------------------------
    #
    # Nur für den manuellen Test aus dem Panel. Jeder Schreibvorgang belegt
    # zwei Register (32 bit, high word zuerst) und geht über
    # write_registers (FC 16) — dieselbe Kodierung wie beim Lesen.

    async def _async_write32(self, offset: int, roh: bytes) -> bool:
        """Einen 32-Bit-Wert in den Holding-Block schreiben."""
        async with self._lock:
            if not await self.async_connect():
                return False
            werte = list(struct.unpack(">HH", roh))
            adresse = self.holding_base + offset
            try:
                result = await self._client.write_registers(
                    address=adresse,
                    values=werte,
                    **_slave_kw(self._client, self._unit_id),
                )
            except Exception as exc:  # noqa: BLE001
                self._record_error("write", exc)
                self._close_client()
                return False
            if result is None or result.isError():
                self._record_error("write", f"Modbus-Fehler an Adresse {adresse}")
                return False
            self.writes += 1
            _LOGGER.debug(
                "Ambibox: Adresse %d = %s geschrieben", adresse, werte
            )
            return True

    async def async_write_target_power(self, watts: int) -> bool:
        """Leistungssollwert setzen (int32, Watt).

        Das Vorzeichen legt der Aufrufer fest — welche Richtung die Ambibox
        als Laden versteht, ist nicht dokumentiert und steht deshalb in der
        Konfiguration (siehe controller.ambibox_charge_sign).
        """
        return await self._async_write32(
            OFF_TARGET_POWER, struct.pack(">i", int(watts))
        )

    async def async_wake_up(self) -> bool:
        """Die Wallbox aufwecken (nur der Wert 1 ist zulässig)."""
        return await self._async_write32(OFF_WAKE_UP, struct.pack(">I", 1))

    async def async_stop_charge(self) -> bool:
        """Den Ladevorgang beenden (nur der Wert 1 ist zulässig)."""
        return await self._async_write32(OFF_STOP_CHARGE, struct.pack(">I", 1))
