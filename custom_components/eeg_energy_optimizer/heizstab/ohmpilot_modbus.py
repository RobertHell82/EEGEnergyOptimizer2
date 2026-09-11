"""Direkte Modbus-TCP-Steuerung des Fronius Ohmpilot.

Schreibt einen stufenlosen Leistungs-Sollwert (0 bis max_power W) per Modbus
TCP direkt an den Ohmpilot, ohne Umweg über den Fronius Gen24.

Watchdog: Der Ohmpilot schaltet nach 50 s ohne neuen Sollwert automatisch ab.
Empfohlenes Schreibintervall: alle 25 bis 30 Sekunden.

Voraussetzung: Der Ohmpilot ist vom Gen24 ENTKOPPELT (Kopplung im
Gen24-Webinterface gelöst oder anderes Subnetz). Sonst schreiben zwei
Master auf dasselbe Register — und der Gen24 regelt den Überschuss auf
Einspeisung null, was jede EEG-Einspeisung auffrisst.

Herkunft: HA_Optimierung_Gruenbach, ``energieoptimierung/ohmpilot_modbus.py``
(Registerwerte dort am echten Gerät verifiziert). Geändert für die
Integration: pymodbus wird erst beim Import dieses Moduls gebraucht, der
Import ist gegen die Testumgebung abgesichert.
"""

from __future__ import annotations

import asyncio
import logging
import time

try:
    from pymodbus.client import AsyncModbusTcpClient
    from pymodbus.exceptions import ModbusException
except ImportError:  # pragma: no cover — Testumgebung ohne pymodbus
    AsyncModbusTcpClient = None  # type: ignore[assignment,misc]

    class ModbusException(Exception):  # type: ignore[no-redef]
        """Platzhalter, damit die except-Zweige unten gültig bleiben."""

_LOGGER = logging.getLogger(__name__)

# Register-Adressen (pymodbus 0-basiert)
# Empirisch verifizierte Werte am echten Gerät (Fronius Ohmpilot, Slave 1):
# - Temperatur: int16 single register an 40808, Skala 0.1°C (749 = 74.9°C)
# - Leistung: int32 (low/high word) an 40800-40801 (40799 las nur 0 obwohl
#   Heizstab tatsächlich 5695 W zog → +1-Symmetrie zu Setpoint/Temperatur)
# - Setpoint: int32 an 40599-40600 (40598 wirft "Illegal Data Address")
REG_POWER_SETPOINT = 40599    # int32, Leistungs-Sollwert in Watt
REG_POWER_ACTUAL = 40800      # int32, aktuelle Leistungsaufnahme
REG_TEMPERATURE = 40808       # int16, aktuelle Wassertemperatur in 0.1°C
REG_UNIX_TIME = 40400         # int32, Unix-Timestamp

SLAVE_ID = 1


class OhmpilotModbus:
    """Async Modbus TCP Client fuer den Ohmpilot."""

    def __init__(self, host: str, port: int = 502, max_power: int = 6000) -> None:
        self._host = host
        self._port = port
        self._max_power = max_power
        self._client: AsyncModbusTcpClient | None = None
        self._connected = False
        # Serialisiert Read/Write-Zugriffe auf den TCP-Socket
        # (drei Timer in __init__.py teilen sich den Client)
        self._lock = asyncio.Lock()
        # Gecachte Messwerte (aktualisiert durch async_read_sensors)
        self.last_power_w: int | None = None
        self.last_temperature: float | None = None
        # Diagnose: letzter Modbus-Fehler (landet im Status des Controllers)
        self.last_error: str | None = None

    def _record_error(self, where: str, exc: Exception | str) -> None:
        """Erfasst einen Modbus-Fehler für Diagnose-Sensor-Attribute."""
        msg = f"{where}: {type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else f"{where}: {exc}"
        self.last_error = msg[:300]
        _LOGGER.warning("Ohmpilot Modbus Fehler [%s]: %s", where, msg)

    @property
    def connected(self) -> bool:
        return self._connected and self._client is not None and self._client.connected

    @property
    def host(self) -> str:
        return self._host

    async def async_connect(self) -> bool:
        """Verbindung zum Ohmpilot herstellen."""
        try:
            if self._client is not None and self._client.connected:
                return True
            if AsyncModbusTcpClient is None:
                self._record_error("connect", "pymodbus nicht installiert")
                return False
            self._client = AsyncModbusTcpClient(self._host, port=self._port, timeout=5)
            result = await self._client.connect()
            self._connected = result
            if result:
                _LOGGER.info("Ohmpilot Modbus verbunden: %s:%s", self._host, self._port)
                self.last_error = None  # Verbindung wieder ok
            else:
                self._record_error("connect", f"Verbindung fehlgeschlagen {self._host}:{self._port}")
            return result
        except Exception as exc:
            self._record_error("connect", exc)
            self._connected = False
            return False

    def _modbus_kwargs(self) -> dict:
        """Slave/Device-ID-Kwarg für aktuelle pymodbus-API.

        pymodbus 3.7+ benannte 'slave' in 'device_id' um. Wir versuchen
        zuerst 'slave', cachen den Erfolg, und fallen bei TypeError auf
        'device_id' zurück.
        """
        if not hasattr(self, "_slave_kwarg"):
            self._slave_kwarg = "slave"
        return {self._slave_kwarg: SLAVE_ID}

    async def _modbus_read(self, address: int, count: int):
        """Read mit automatischem slave/device_id-Fallback."""
        try:
            return await self._client.read_holding_registers(
                address=address, count=count, **self._modbus_kwargs()
            )
        except TypeError as exc:
            if "slave" in str(exc) and self._slave_kwarg == "slave":
                self._slave_kwarg = "device_id"
                _LOGGER.info("pymodbus: Fallback auf device_id-Kwarg")
                return await self._client.read_holding_registers(
                    address=address, count=count, **self._modbus_kwargs()
                )
            raise

    async def _modbus_write(self, address: int, values: list[int]):
        """Write mit automatischem slave/device_id-Fallback."""
        try:
            return await self._client.write_registers(
                address=address, values=values, **self._modbus_kwargs()
            )
        except TypeError as exc:
            if "slave" in str(exc) and self._slave_kwarg == "slave":
                self._slave_kwarg = "device_id"
                _LOGGER.info("pymodbus: Fallback auf device_id-Kwarg")
                return await self._client.write_registers(
                    address=address, values=values, **self._modbus_kwargs()
                )
            raise

    async def async_close(self) -> None:
        """Verbindung trennen."""
        async with self._lock:
            if self._client is not None:
                self._client.close()
                self._connected = False
                _LOGGER.info("Ohmpilot Modbus getrennt")

    async def async_set_power(self, watts: int) -> bool:
        """Leistungs-Sollwert in Watt setzen (0 bis max_power).

        Muss alle 25-30 Sekunden aufgerufen werden (Watchdog: 50s).
        """
        watts = max(0, min(watts, self._max_power))

        async with self._lock:
            if not await self._ensure_connected():
                return False

            try:
                # Big-Endian Word-Order (SunSpec): high word zuerst, low word zweiter.
                # Empirisch verifiziert: setpoint=4500 als [0x0000, 0x1194] wird vom
                # Ohmpilot als 4500 W gelesen; [0x1194, 0x0000] dagegen als 0x11940000
                # (~294 Mio W) interpretiert, was den Heizstab auf Hardware-Max clampt.
                low_word = watts & 0xFFFF
                high_word = (watts >> 16) & 0xFFFF
                result = await self._modbus_write(REG_POWER_SETPOINT, [high_word, low_word])
                if result.isError():
                    self._record_error("set_power", str(result))
                    return False
                _LOGGER.debug("Ohmpilot Sollwert: %d W", watts)
                return True
            except ModbusException as exc:
                self._record_error("set_power.modbus", exc)
                self._connected = False
                return False
            except Exception as exc:
                self._record_error("set_power", exc)
                self._connected = False
                return False

    async def async_read_power(self) -> int | None:
        """Aktuelle Leistungsaufnahme in Watt lesen."""
        async with self._lock:
            if not await self._ensure_connected():
                return None

            try:
                result = await self._modbus_read(REG_POWER_ACTUAL, 2)
                if result.isError():
                    self._record_error("read_power", str(result))
                    return None
                # Big-Endian Word-Order (SunSpec): high word zuerst, low word zweiter.
                # Empirisch verifiziert: 5752 W kommt als [0x0000, 0x1678] zurück.
                return (result.registers[0] << 16) | result.registers[1]
            except Exception as exc:
                self._record_error("read_power", exc)
                self._connected = False
                return None

    async def async_read_temperature(self) -> float | None:
        """Aktuelle Wassertemperatur lesen."""
        async with self._lock:
            if not await self._ensure_connected():
                return None

            try:
                # int16 single register, Skala 0.1°C (empirisch verifiziert)
                result = await self._modbus_read(REG_TEMPERATURE, 1)
                if result.isError():
                    self._record_error("read_temperature", str(result))
                    return None
                raw = result.registers[0]
                return raw / 10.0
            except Exception as exc:
                self._record_error("read_temperature", exc)
                self._connected = False
                return None

    async def async_sync_time(self) -> bool:
        """Unix-Zeit synchronisieren (alle 6h aufrufen)."""
        async with self._lock:
            if not await self._ensure_connected():
                return False

            try:
                unix_time = int(time.time())
                low_word = unix_time & 0xFFFF
                high_word = (unix_time >> 16) & 0xFFFF
                # Big-Endian Word-Order wie bei REG_POWER_SETPOINT.
                result = await self._modbus_write(REG_UNIX_TIME, [high_word, low_word])
                if result.isError():
                    self._record_error("sync_time", str(result))
                    return False
                _LOGGER.info("Ohmpilot Zeit synchronisiert: %d", unix_time)
                return True
            except Exception as exc:
                self._record_error("sync_time", exc)
                self._connected = False
                return False

    async def async_read_sensors(self) -> None:
        """Leistung und Temperatur lesen und cachen (fuer HA-Sensoren).

        Schluckt jede Exception damit der periodische Read-Cycle in __init__.py
        nicht abbricht. Fehler werden über _record_error in last_error
        festgehalten und sind via Sensor-Attribute sichtbar.
        """
        try:
            self.last_power_w = await self.async_read_power()
        except Exception as exc:
            self._record_error("read_sensors.power", exc)
            self.last_power_w = None
        try:
            self.last_temperature = await self.async_read_temperature()
        except Exception as exc:
            self._record_error("read_sensors.temperature", exc)
            self.last_temperature = None

    async def _ensure_connected(self) -> bool:
        """Verbindung pruefen und bei Bedarf neu aufbauen."""
        if self.connected:
            return True
        _LOGGER.debug("Ohmpilot Modbus Reconnect...")
        return await self.async_connect()
