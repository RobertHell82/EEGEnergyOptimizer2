"""SolaX Gen4+ inverter control via HA solax_modbus integration.

Uses Mode 1 Remote Control with two-phase write model:
  Phase 1: Set parameters via number.set_value / select.select_option (DATA_LOCAL)
  Phase 2: Press trigger button to write all params to Modbus registers

Entity prefix varies by installation — resolved from config or SOLAX_ENTITY_DEFAULTS.
All power values converted from InverterBase kW to SolaX Watts.

**Der Entladeboden (`selfuse_discharge_min_soc`) ist ein harter Riegel.** Der
Wechselrichter stoppt die Batterieentladung dort — auch mitten in einer über
Mode 1 befohlenen Zwangsentladung. Der Befehl läuft weiter, die Batterie
liefert nur 0,00 kW. Belegt an einer Anlage am 26.08.2026: Befehl 5 kW,
Gerätewert 30 %, geplantes Ziel 25 % → 40 Sekunden Einspeisung, dann 1 h 47
Stillstand, während das Haus 2,6 kW aus dem Netz zog.

Der Treiber behandelt ihn deshalb wie Fronius seinen MinRsvPct:

- ``get_backup_reserve_soc_pct`` meldet den **Ruhewert** an den Fahrplan, der
  dadurch gar nicht erst tiefer plant, als das Gerät zulässt.
- ``async_set_discharge`` senkt ihn für die Dauer der Entladung auf
  Ziel-SOC − ``DISCHARGE_FLOOR_MARGIN_PCT`` ab, damit der Wechselrichter nicht
  kurz VOR dem Ziel bremst (Geräte-SOC und HA-Sensor weichen leicht ab).
- ``async_stop_forcible`` schreibt den gesicherten Wert zurück.

Der Vorwert liegt im ``SolaXStateStore`` und überlebt damit einen Neustart
mitten in der Entladung. Ohne Restore bliebe eine abgesenkte Reserve dauerhaft
stehen: Der Wechselrichter würde die Batterie auch im Automatikbetrieb tiefer
entladen, als der Betreiber es eingestellt hat.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from .base import InverterBase

try:
    from homeassistant.helpers.storage import Store
except ImportError:  # pragma: no cover — test environment
    Store = None  # type: ignore

_LOGGER = logging.getLogger(__name__)

SOLAX_DOMAIN = "solax_modbus"

# Sicherheitsabstand zwischen dem geschriebenen Entladeboden und dem geplanten
# Ziel-Ladestand. Läge der Boden exakt auf dem Ziel, stoppte der
# Wechselrichter die Entladung selbst, sobald sein interner SOC den Wert
# erreicht — und weil Geräte-SOC und HA-Sensor um ein bis zwei Prozent
# auseinanderliegen können, käme der Fahrplan an seinem Ziel nie an. Beenden
# soll immer der Fahrplan; der Boden ist nur das Netz darunter (gleiche
# Begründung wie _MINRSV_SAFETY_MARGIN_PCT im Fronius-Treiber).
DISCHARGE_FLOOR_MARGIN_PCT = 5.0

# Entity Key -> Default Entity ID Mapping
SOLAX_ENTITY_DEFAULTS = {
    "remotecontrol_power_control": "select.solax_inverter_remotecontrol_power_control",
    "remotecontrol_active_power": "number.solax_inverter_remotecontrol_active_power",
    "remotecontrol_autorepeat_duration": "number.solax_inverter_remotecontrol_autorepeat_duration",
    "remotecontrol_duration": "number.solax_inverter_remotecontrol_duration",
    "remotecontrol_trigger": "button.solax_inverter_remotecontrol_trigger",
    "battery_charge_max_current": "number.solax_inverter_battery_charge_max_current",
    "selfuse_discharge_min_soc": "number.solax_inverter_selfuse_discharge_min_soc",
}

# Suffix-Muster je Steuer-Entity. Neuere solax_modbus-Versionen (≥2025.x)
# hängen Mode-Suffixe an die Entity-Namen an (remotecontrol_power_control_mode_1,
# remotecontrol_trigger_mode_1_7, …), ältere nutzen die Basisnamen. Die
# Mode-Bereiche in den Suffixen (z. B. _mode_1_7) können sich zwischen
# Versionen ändern, daher generisches [0-9_]+. *_direct-Varianten
# (Direktregister-Schreibmodus) werden bewusst NICHT gematcht — der Treiber
# nutzt das Zwei-Phasen-Modell (Parameter setzen + Trigger).
SOLAX_CONTROL_ENTITY_PATTERNS: dict[str, tuple[str, re.Pattern]] = {
    "remotecontrol_power_control": ("select", re.compile(r"remotecontrol_power_control(_mode_1)?$")),
    "remotecontrol_active_power": ("number", re.compile(r"remotecontrol_active_power(_mode_1)?$")),
    "remotecontrol_autorepeat_duration": ("number", re.compile(r"remotecontrol_autorepeat_duration(_mode_[0-9_]+)?$")),
    "remotecontrol_duration": ("number", re.compile(r"remotecontrol_duration(_mode_[0-9_]+)?$")),
    "remotecontrol_trigger": ("button", re.compile(r"remotecontrol_trigger(_mode_[0-9_]+)?$")),
    "battery_charge_max_current": ("number", re.compile(r"battery_charge_max_current$")),
    "selfuse_discharge_min_soc": ("number", re.compile(r"selfuse_discharge_min_soc$")),
}


def find_solax_control_entity(hass: Any, config_key: str) -> str | None:
    """Suche die SolaX-Steuer-Entity per Suffix-Scan (versionstolerant).

    Liefert None, wenn keine passende Entity existiert. Bei mehreren Treffern
    (Übergangsinstallationen mit alter + neuer Benennung) gewinnt der kürzeste
    Name — das ist der Basisname ohne Mode-Suffix.
    """
    domain, pattern = SOLAX_CONTROL_ENTITY_PATTERNS[config_key]
    try:
        matches = [
            state.entity_id
            for state in hass.states.async_all(domain)
            if pattern.search(state.entity_id.split(".", 1)[1])
        ]
    except TypeError:  # Test-Umgebung ohne echte State-Machine
        return None
    if not matches:
        return None
    return min(matches, key=len)


class SolaXStateStore:
    """Persistiert Vorwerte, die der Treiber überschreibt, über HA-Reboots.

    Zwei Einträge: der Original-Wert von ``battery_charge_max_current`` (beim
    ersten async_set_charge_limit mit State > 0 befüllt) und der Entladeboden
    ``selfuse_discharge_min_soc`` (beim ersten async_set_discharge). Beide
    liest ``async_stop_forcible`` wieder aus. Ohne Persistenz gingen sie bei
    einem Neustart mitten im Eingriff verloren, und die abgesenkten Werte
    blieben dauerhaft im Wechselrichter stehen.
    """

    STORAGE_KEY = "eeg_energy_optimizer.solax_state"
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

    async def async_save_original_current(self, amps: float) -> None:
        self._data["battery_charge_max_current_original"] = float(amps)
        if self._store is not None:
            await self._store.async_save(self._data)

    @property
    def original_current(self) -> float | None:
        val = self._data.get("battery_charge_max_current_original")
        return float(val) if val is not None else None

    async def async_save_original_discharge_floor(self, pct: float) -> None:
        self._data["selfuse_discharge_min_soc_original"] = float(pct)
        if self._store is not None:
            await self._store.async_save(self._data)

    async def async_clear_original_discharge_floor(self) -> None:
        if self._data.pop("selfuse_discharge_min_soc_original", None) is not None:
            if self._store is not None:
                await self._store.async_save(self._data)

    @property
    def original_discharge_floor(self) -> float | None:
        val = self._data.get("selfuse_discharge_min_soc_original")
        return float(val) if val is not None else None


class SolaXInverter(InverterBase):
    """SolaX Gen4+ inverter control via solax_modbus HA integration."""

    def __init__(self, hass: Any, config: dict) -> None:
        super().__init__(hass, config)
        self._state_store = SolaXStateStore(hass)
        # Zuletzt im Ruhezustand gelesener Entladeboden (%). Während einer
        # Entladung steht im Gerät unser abgesenkter Wert — als Geräte-Reserve
        # gemeldet würde er dem Fahrplan eine Grenze vorgaukeln, die es ohne
        # unseren Eingriff nicht gibt.
        self._discharge_floor_idle: float | None = None
        self._discharge_active: bool = False

    def _resolve_entity(self, config_key: str) -> str:
        """Löse die Steuer-Entity auf: Config → Suffix-Scan → Default.

        Der konfigurierte Wert gilt nur, solange die Entity auch existiert —
        nach einem solax_modbus-Update mit umbenannten Entities (Mode-Suffixe)
        greift sonst automatisch der Suffix-Scan.
        """
        configured = self._config.get(f"solax_{config_key}")
        if configured and self._hass.states.get(configured) is not None:
            return configured
        found = find_solax_control_entity(self._hass, config_key)
        if found:
            return found
        return configured or SOLAX_ENTITY_DEFAULTS[config_key]

    async def _set_number(self, config_key: str, value: float) -> None:
        """Set a number entity value. Resolves entity from config, scan or defaults."""
        await self._hass.services.async_call(
            "number", "set_value",
            {"entity_id": self._resolve_entity(config_key), "value": value},
            blocking=True,
        )

    async def _set_select(self, config_key: str, option: str) -> None:
        """Set a select entity option. Resolves entity from config, scan or defaults."""
        await self._hass.services.async_call(
            "select", "select_option",
            {"entity_id": self._resolve_entity(config_key), "option": option},
            blocking=True,
        )

    async def _press_trigger(self) -> None:
        """Press the remote control trigger button to execute Modbus write."""
        await self._hass.services.async_call(
            "button", "press",
            {"entity_id": self._resolve_entity("remotecontrol_trigger")},
            blocking=True,
        )

    async def async_set_charge_limit(self, power_kw: float) -> bool:
        """Set battery charge limit. power_kw=0 blocks charging only (NOT discharge).

        Uses battery_charge_max_current entity (Ampere) — analog zu Huawei
        batterien_maximale_ladeleistung und Fronius StorCtl_Mod Bit 0 + InWRte=0.
        Self-Use-Mode der SolaX läuft im Hintergrund weiter; Discharge bleibt
        möglich bis selfuse_discharge_min_soc.

        Der Originalwert wird beim ersten Eingriff in einen Store persistiert
        (siehe SolaXStateStore). Bei Reboot mitten im Block (aktueller State = 0)
        wird der Cache NICHT überschrieben.
        """
        try:
            await self._ensure_original_cached()

            if power_kw == 0:
                await self._set_number("battery_charge_max_current", 0)
            else:
                voltage = self._read_battery_voltage_or_default()
                max_a = self._read_max_charge_current_attribute() or 30.0
                amps = max(0.0, min(power_kw * 1000.0 / voltage, max_a))
                await self._set_number("battery_charge_max_current", amps)

            # Sicherstellen, dass Mode 1 NICHT aktiv ist (Migration für
            # Bestands-Setups, die noch im alten Mode-1-Idle stehen).
            await self._set_select("remotecontrol_power_control", "Disabled")
            return True
        except Exception:
            _LOGGER.exception("SolaX: Failed to set charge limit")
            return False

    async def _ensure_original_cached(self) -> None:
        """Cache aktuellen battery_charge_max_current in Store, falls > 0 und noch nicht gespeichert."""
        if self._state_store is None:
            return
        await self._state_store.async_load()
        if self._state_store.original_current is not None:
            return
        current = self._read_current_charge_max_current()
        if current is not None and current > 0:
            await self._state_store.async_save_original_current(current)

    def _read_current_charge_max_current(self) -> float | None:
        """Liest aktuellen State von battery_charge_max_current Entity. Liefert None wenn nicht verfügbar."""
        state = self._hass.states.get(self._resolve_entity("battery_charge_max_current"))
        if state is None:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _read_max_charge_current_attribute(self) -> float | None:
        """Liest attributes.max des battery_charge_max_current Entities (Hardware-Maximum)."""
        state = self._hass.states.get(self._resolve_entity("battery_charge_max_current"))
        if state is None:
            return None
        max_val = state.attributes.get("max")
        try:
            return float(max_val) if max_val is not None else None
        except (TypeError, ValueError):
            return None

    def _read_battery_voltage_or_default(self) -> float:
        """Liest aktuelle Batteriespannung. Fallback: 400 V (typischer SolaX-Hochvolt-Bereich)."""
        state = self._hass.states.get("sensor.solax_inverter_battery_voltage_charge")
        if state is not None:
            try:
                v = float(state.state)
                if v > 50:
                    return v
            except (TypeError, ValueError):
                pass
        return 400.0

    async def async_set_discharge(
        self, power_kw: float, target_soc: float | None = None
    ) -> bool:
        """Start forced battery discharge at given power.

        Uses "Enabled Battery Control" with negative active_power.
        """
        try:
            # Entladeboden absenken, BEVOR der Befehl läuft — sonst stoppt der
            # Wechselrichter an seinem eigenen Wert, ohne das zu melden.
            if target_soc is not None:
                await self._senke_entladeboden(float(target_soc))

            power_w = -abs(int(power_kw * 1000))
            await self._set_select("remotecontrol_power_control", "Enabled Battery Control")
            await self._set_number("remotecontrol_active_power", power_w)
            await self._set_number("remotecontrol_duration", 300)
            await self._set_number("remotecontrol_autorepeat_duration", 60)
            await self._press_trigger()
            self._discharge_active = True
            return True
        except Exception:
            _LOGGER.exception("SolaX: Failed to set discharge")
            return False

    async def async_stop_forcible(self) -> bool:
        """Stop forced charge/discharge, return to automatic mode.

        Beendet Mode 1 (Disabled + active_power=0 + autorepeat=0 + trigger)
        UND restoriert battery_charge_max_current aus dem Store. Fallback wenn
        Store leer: attributes.max des Entities (typisch 30 A).
        """
        try:
            await self._set_select("remotecontrol_power_control", "Disabled")
            await self._set_number("remotecontrol_active_power", 0)
            await self._set_number("remotecontrol_duration", 20)
            await self._set_number("remotecontrol_autorepeat_duration", 0)
            await self._press_trigger()

            original = await self._resolve_original_charge_current()
            if original is not None:
                await self._set_number("battery_charge_max_current", original)

            await self._restauriere_entladeboden()
            self._discharge_active = False
            return True
        except Exception:
            _LOGGER.exception("SolaX: Failed to stop forcible mode")
            return False

    async def _resolve_original_charge_current(self) -> float | None:
        """Liefert gecachten Originalwert; Fallback ist attributes.max des Entities."""
        if self._state_store is not None:
            await self._state_store.async_load()
            original = self._state_store.original_current
            if original is not None:
                return original
        return self._read_max_charge_current_attribute()

    # ------------------------------------------------------------------
    # Entladeboden (selfuse_discharge_min_soc)
    # ------------------------------------------------------------------
    def _lies_entladeboden(self) -> float | None:
        """Aktuellen Entladeboden in Prozent, oder None wenn nicht lesbar."""
        state = self._hass.states.get(self._resolve_entity("selfuse_discharge_min_soc"))
        if state is None:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    async def _senke_entladeboden(self, target_soc: float) -> None:
        """Boden auf Ziel-SOC − Marge absenken, Vorwert sichern.

        Der Vorwert wird nur beim ERSTEN Eingriff gesichert: bei jedem
        weiteren Aufruf steht im Gerät bereits unser abgesenkter Wert, und ihn
        als "Original" zu speichern hieße, den echten zu verlieren. Nach einem
        Neustart mitten in der Entladung liefert der Store ihn zurück.

        Ein höherer Boden wird nie geschrieben: Liegt der Gerätewert schon
        tief genug, bleibt er unangetastet — wir senken nur, wo es nötig ist.
        """
        neu = max(target_soc - DISCHARGE_FLOOR_MARGIN_PCT, 0.0)
        aktuell = self._lies_entladeboden()

        await self._state_store.async_load()
        if self._state_store.original_discharge_floor is None and aktuell is not None:
            await self._state_store.async_save_original_discharge_floor(aktuell)
            self._discharge_floor_idle = aktuell

        if aktuell is not None and aktuell <= neu:
            return
        try:
            await self._set_number("selfuse_discharge_min_soc", neu)
            _LOGGER.info(
                "SolaX: Entladeboden %s %% → %.0f %% (Ziel-SOC %.0f %%)",
                "?" if aktuell is None else f"{aktuell:.0f}", neu, target_soc,
            )
        except Exception:
            # Nicht kritisch für den Entladebefehl selbst — der läuft auch so
            # an, endet dann eben früher am Gerätewert.
            _LOGGER.warning(
                "SolaX: Entladeboden nicht setzbar — die Entladung stoppt "
                "möglicherweise vorzeitig am Gerätewert",
                exc_info=True,
            )

    async def _restauriere_entladeboden(self) -> None:
        """Gesicherten Entladeboden zurückschreiben und den Eintrag löschen."""
        await self._state_store.async_load()
        original = self._state_store.original_discharge_floor
        if original is None:
            return
        try:
            await self._set_number("selfuse_discharge_min_soc", original)
            self._discharge_floor_idle = original
            await self._state_store.async_clear_original_discharge_floor()
            _LOGGER.info("SolaX: Entladeboden auf %.0f %% zurückgesetzt", original)
        except Exception:
            # Eintrag bewusst behalten: der nächste Stopp versucht es erneut.
            _LOGGER.warning(
                "SolaX: Entladeboden nicht zurücksetzbar (%.0f %%) — bleibt "
                "gespeichert für den nächsten Versuch", original, exc_info=True,
            )

    # ------------------------------------------------------------------
    # Fahrplan-Steuerschnittstelle (Schedule-Executor)
    # ------------------------------------------------------------------
    @property
    def supports_schedule_control(self) -> bool:
        """Der Fahrplan-Executor darf diesen Wechselrichter stellen."""
        return True

    async def async_get_charge_limit_kw(self) -> float | None:
        """Aktuell gesetztes Ladelimit in kW (Ampere × Batteriespannung).

        SolaX begrenzt die Ladung über einen STROM, der Fahrplan rechnet in
        Leistung — umgerechnet wird mit derselben Spannung wie beim Setzen,
        damit Hin- und Rückweg zusammenpassen. Die Spannung schwankt mit dem
        Ladestand; für Guard 1 ist das unkritisch, weil er ohnehin
        schrittweise nachführt und nicht auf den exakten Wert angewiesen ist.
        """
        amps = self._read_current_charge_max_current()
        if amps is None:
            return None
        return amps * self._read_battery_voltage_or_default() / 1000.0

    def get_charge_limit_max_kw(self) -> float | None:
        """Hardware-Maximum des Ladelimits in kW (attributes.max × Spannung)."""
        max_a = self._read_max_charge_current_attribute()
        if max_a is None:
            return None
        return max_a * self._read_battery_voltage_or_default() / 1000.0

    def get_max_discharge_power_kw(self) -> float | None:
        """Maximale Entladeleistung in kW aus dem Maximum der Mode-1-Entität.

        ``remotecontrol_active_power`` nimmt Watt und trägt das zulässige
        Maximum als Attribut — das ist die Grenze, die Guard 2 einhalten muss.
        """
        state = self._hass.states.get(self._resolve_entity("remotecontrol_active_power"))
        if state is None:
            return None
        max_w = state.attributes.get("max")
        try:
            return abs(float(max_w)) / 1000.0 if max_w is not None else None
        except (TypeError, ValueError):
            return None

    def get_backup_reserve_soc_pct(self) -> float | None:
        """Entladeboden des Geräts in Prozent — Untergrenze für den Fahrplan.

        Gemeldet wird der RUHEWERT: läuft gerade eine Entladung, ist das der
        beim Start gesicherte Vorwert, sonst der aktuell gelesene. Sonst
        meldeten wir dem Fahrplan unseren eigenen abgesenkten Boden als
        Gerätegrenze zurück und er plante mit jeder Entladung tiefer.
        """
        if self._discharge_active:
            gesichert = self._state_store.original_discharge_floor
            if gesichert is not None:
                return gesichert
            return self._discharge_floor_idle
        aktuell = self._lies_entladeboden()
        if aktuell is not None:
            self._discharge_floor_idle = aktuell
        return aktuell

    def get_control_entities(self) -> list[dict]:
        """Stellgrößen für die Transparenz-Ansicht im Panel."""
        rows: list[dict] = []
        for config_key, label, rolle in (
            ("battery_charge_max_current", "Ladestrom max", "charge_limit"),
            ("remotecontrol_active_power", "Entladeleistung (Mode 1)", "discharge_limit"),
            ("remotecontrol_power_control", "Betriebsmodus", "mode"),
            ("selfuse_discharge_min_soc", "Entladeboden", "backup_soc"),
        ):
            entity_id = self._resolve_entity(config_key)
            if entity_id and self._hass.states.get(entity_id) is not None:
                rows.append({"label": label, "entity_id": entity_id, "role": rolle})
        return rows

    @property
    def is_available(self) -> bool:
        """Whether the SolaX Modbus integration is loaded and available."""
        entries = self._hass.config_entries.async_entries(SOLAX_DOMAIN)
        return any(entry.state.value == "loaded" for entry in entries)
