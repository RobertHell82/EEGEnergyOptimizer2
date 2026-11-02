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

**Ein Mode-1-Befehl hält sich nicht selbst.** Die Integration wiederholt ihn
nur für ``remotecontrol_autorepeat_duration`` und schreibt seit etwa
solax_modbus 2026.10 bei deren Ablauf ausdrücklich „Disabled"
(``BUTTONREPEAT_POST``). Der Executor schreibt nur bei einer Änderung — mit
den früheren 60 s endete eine Entladung also nach einer Minute, auch wenn der
Plan sie für eine Stunde vorsah. Der Treiber löst den Befehl deshalb selbst
alle ``MODE1_KEEPALIVE_S`` erneut aus, solange er aktiv ist; die Wiederholung
der Integration (``MODE1_AUTOREPEAT_S``) überbrückt die Zeit dazwischen mit
Abstand. ``remotecontrol_duration`` (``MODE1_DAUER_S``) bleibt der Rückfall
im Gerät: Fällt Home Assistant aus, endet der Befehl dort spätestens diese
Zeit nach dem letzten Schreiben.

**Batterie aus dem Netz laden (Beta)** ist derselbe Mode-1-Befehl mit
positiver Leistung, nur auf Gen4/Gen5 (Gen6 hat keinen Mode 1). Das Gerät
kennt dabei keinen Ziel-Ladestand; den beobachtet der Treiber selbst, siehe
``async_set_grid_charge``.

**Gen6 ohne Mode 1:** Dort bleibt nur das Ladelimit über
``battery_charge_max_current``. Zwangsentladung und Netzladung brauchen
Mode 1; fehlen dessen Entitäten, scheitern beide mit einem Grund, statt
Befehle an Entitäten zu schicken, die es nicht gibt (siehe ``_entladung``).
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from ..const import CONF_BATTERY_SOC_SENSOR
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

# Mode-1-Zeiten (Sekunden). Die Laufzeit ist der Rückfall im Gerät; die
# Wiederholung der Integration muss den Keepalive-Takt sicher überdecken — ein
# verspäteter Takt (HA ausgelastet, Modbus langsam) darf den Befehl nicht
# zwischen zwei Auslösungen enden lassen. 240 s Takt bei 600 s Wiederholung
# lassen einen verpassten Takt Luft.
MODE1_DAUER_S = 300
MODE1_AUTOREPEAT_S = 600
MODE1_KEEPALIVE_S = 240

# Netzladung: darunter ist sie eine Freigabe (die Netzgrenze lässt gerade
# nichts zu), und Mode 1 nimmt die Leistung in 100-W-Schritten.
NETZLADEN_MIN_KW = 0.05
MODE1_SCHRITT_W = 100

# Mode-1-Entitäten, ohne die es keine Netzladung gibt. Gen6 kennt Mode 1
# nicht; dort findet der Suffix-Scan sie nicht, und der Treiber bietet die
# Netzladung gar nicht erst an.
_MODE1_ENTITAETEN = (
    "remotecontrol_power_control",
    "remotecontrol_active_power",
    "remotecontrol_duration",
    "remotecontrol_autorepeat_duration",
    "remotecontrol_trigger",
)

# Entity Key -> Default Entity ID Mapping
SOLAX_ENTITY_DEFAULTS = {
    "remotecontrol_power_control": "select.solax_inverter_remotecontrol_power_control",
    "remotecontrol_active_power": "number.solax_inverter_remotecontrol_active_power",
    "remotecontrol_autorepeat_duration": "number.solax_inverter_remotecontrol_autorepeat_duration",
    "remotecontrol_duration": "number.solax_inverter_remotecontrol_duration",
    "remotecontrol_trigger": "button.solax_inverter_remotecontrol_trigger",
    "battery_charge_max_current": "number.solax_inverter_battery_charge_max_current",
    "battery_discharge_max_current": "number.solax_inverter_battery_discharge_max_current",
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
    "battery_discharge_max_current": ("number", re.compile(r"battery_discharge_max_current$")),
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

    async def async_save_original_discharge_current(self, amps: float) -> None:
        self._data["battery_discharge_max_current_original"] = float(amps)
        if self._store is not None:
            await self._store.async_save(self._data)

    async def async_clear_original_discharge_current(self) -> None:
        if self._data.pop("battery_discharge_max_current_original", None) is not None:
            if self._store is not None:
                await self._store.async_save(self._data)

    @property
    def original_discharge_current(self) -> float | None:
        val = self._data.get("battery_discharge_max_current_original")
        return float(val) if val is not None else None

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
        # Laufender Mode-1-Befehl (W, negativ = Entladung), den der
        # Keepalive-Takt erneuert, und dessen Abmelder. None = kein Befehl.
        self._mode1_watt: int | None = None
        self._keepalive_abmelden: Any = None
        # Ziel-SOC der laufenden Netzladung und der Abmelder des
        # SOC-Beobachters — siehe _beobachte_ziel_soc().
        self._ziel_soc: float | None = None
        self._ziel_abmelden: Any = None
        # Ein Stopp am Ziel-SOC ist gescheitert und noch nicht nachgeholt.
        # Eigenes Flag, weil der Takt ihn sonst nur wiederholte, solange der
        # SOC am Ziel steht — fällt er danach darunter (die Hauslast zieht
        # mit), sähe der Takt weder ein Ziel noch einen Befehl, und Mode 1
        # kaufte bis zum Ablauf der Wiederholung weiter Netzstrom.
        self._stopp_offen: bool = False
        # Unlesbarer SOC während einer Netzladung: nur einmal je Befehl
        # warnen, nicht in jedem Takt.
        self._soc_warnung_gegeben: bool = False
        # Executor (30-s-Lauf) und Keepalive-Takt schreiben nebenläufig. Ohne
        # Sperre könnte ein Takt zwischen die Schritte eines Stopps fallen und
        # Mode 1 danach wieder einschalten — unbeobachtet, bis der Rückfall im
        # Gerät greift.
        self._sperre = asyncio.Lock()

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
        if self._nicht_endlich(power_kw):
            return False
        async with self._sperre:
            return await self._ladelimit(power_kw)

    async def _ladelimit(self, power_kw: float) -> bool:
        # Ein Ladelimit ist kein Mode-1-Befehl: Der Takt darf nichts mehr
        # erneuern, sonst schaltete er Mode 1 gleich wieder ein.
        self._keepalive_beenden()
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
            # _erfolg() statt True: Erst das räumt einen Grund aus einem
            # früheren Fehlschlag ab, sonst trüge die Telemetrie ihn weiter.
            return self._erfolg()
        except Exception as exc:
            _LOGGER.exception("SolaX: Failed to set charge limit")
            return self._fehler(type(exc).__name__)

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

        Uses "Enabled Battery Control" with negative active_power. Der Befehl
        läuft, bis ein anderer kommt — der Keepalive-Takt hält ihn am Leben
        (siehe Modulkopf).
        """
        if self._nicht_endlich(power_kw, target_soc):
            return False
        async with self._sperre:
            return await self._entladung(power_kw, target_soc)

    async def _entladung(self, power_kw: float, target_soc: float | None) -> bool:
        # Ohne Mode-1-Entitäten (Gen6) ginge jeder Schreibversuch an die
        # Standard-IDs, die es nicht gibt. HA weist das nicht ab, es warnt
        # nur — der Treiber meldete Erfolg, der Executor hielte die Entladung
        # für befohlen, und erst Guard 3 merkte nach drei Minuten, dass
        # nichts kommt. Ob Gen6 eine Zwangsentladung anders kann, ist am
        # Gerät nicht belegt; bis dahin lieber ehrlich scheitern.
        if not self._mode1_vorhanden():
            return self._fehler("Mode 1 nicht verfügbar")
        self._keepalive_beenden()
        try:
            # Entladeboden absenken, BEVOR der Befehl läuft — sonst stoppt der
            # Wechselrichter an seinem eigenen Wert, ohne das zu melden.
            if target_soc is not None:
                await self._senke_entladeboden(float(target_soc))

            power_w = -abs(int(power_kw * 1000))
            await self._mode1_schreiben(power_w)
            self._discharge_active = True
            self._starte_keepalive(power_w)
            return self._erfolg()
        except Exception as exc:
            _LOGGER.exception("SolaX: Failed to set discharge")
            # Der Takt ist schon abgemeldet. Bliebe ein früherer Mode-1-Befehl
            # (andere Leistung, oder eine Netzladung) im Gerät stehen, liefe
            # er ohne Takt und ohne Ziel bis zum Ablauf der Wiederholung
            # weiter — und das über dem zurückgesetzten Boden hinweg, den er
            # nicht kennt. Darum wie bei der Netzladung: alles freigeben.
            try:
                await self._stopp_schreiben()
            except Exception:  # noqa: BLE001 — der Fehlerpfad darf nie werfen
                _LOGGER.debug("SolaX: Stopp nach gescheiterter Entladung", exc_info=True)
            # Der Stopp schreibt den Boden erst am Ende zurück; scheiterte er
            # vorher, muss der Boden trotzdem weg: Sonst entlädt der
            # Wechselrichter im Automatikbetrieb tiefer, als der
            # Anlagenbetreiber eingestellt hat. Hat der Stopp ihn schon
            # zurückgeschrieben, ist kein Vorwert mehr gesichert, und hier
            # passiert nichts.
            try:
                await self._restauriere_entladeboden()
            except Exception:  # noqa: BLE001 — der Fehlerpfad darf nie werfen
                _LOGGER.debug("SolaX: Entladeboden nach Fehlschlag nicht "
                              "zurücksetzbar", exc_info=True)
            return self._fehler(type(exc).__name__)

    async def async_stop_forcible(self) -> bool:
        """Stop forced charge/discharge, return to automatic mode.

        Beendet Mode 1 (Disabled + active_power=0 + autorepeat=0 + trigger)
        UND restoriert battery_charge_max_current aus dem Store. Fallback wenn
        Store leer: attributes.max des Entities (typisch 30 A).

        Der Keepalive-Takt endet zuerst, auch wenn der Stopp danach scheitert:
        Einen Befehl, den wir beenden wollen, darf niemand mehr erneuern. Er
        läuft im Gerät dann höchstens noch bis zum Rückfall aus.
        """
        async with self._sperre:
            return await self._stopp()

    async def _stopp(self) -> bool:
        self._keepalive_beenden()
        return await self._stopp_schreiben()

    async def _stopp_schreiben(self) -> bool:
        """Die Schreibfolge der Freigabe — ohne den Takt anzufassen.

        Getrennt, weil die Netzladung am Ziel-SOC den Takt bei einem
        Fehlschlag behalten will: Er ist dann die Wiederholung des Stopps.
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
            return self._erfolg()
        except Exception as exc:
            if self._stopp_offen:
                # Die Wiederholung aus dem Takt (alle 30 s): Der Stacktrace
                # steht schon vom ersten Fehlschlag im Log. Bei einem Gerät,
                # das länger weg ist, füllte er es sonst im Halbminutentakt.
                _LOGGER.warning(
                    "SolaX: Stopp erneut fehlgeschlagen (%s: %s)",
                    type(exc).__name__, exc,
                )
            else:
                _LOGGER.exception("SolaX: Failed to stop forcible mode")
            return self._fehler(type(exc).__name__)

    # ------------------------------------------------------------------
    # Mode 1 schreiben und wachhalten
    # ------------------------------------------------------------------
    async def _mode1_schreiben(self, power_w: int) -> None:
        """Mode 1 „Enabled Battery Control" mit ``power_w`` setzen und auslösen.

        Dieselbe Folge für den ersten Befehl und jeden Takt: Die Parameter
        liegen nur lokal in der Integration (DATA_LOCAL) und sind nach deren
        Neuladen wieder auf den Standardwerten — ein bloßer Trigger schriebe
        dann diese ins Gerät.
        """
        await self._set_select("remotecontrol_power_control", "Enabled Battery Control")
        await self._set_number("remotecontrol_active_power", power_w)
        await self._set_number("remotecontrol_duration", MODE1_DAUER_S)
        await self._set_number("remotecontrol_autorepeat_duration", MODE1_AUTOREPEAT_S)
        await self._press_trigger()

    def _starte_keepalive(self, power_w: int) -> None:
        """Den laufenden Befehl merken und den Takt anmelden (nur einmal)."""
        self._mode1_watt = power_w
        if self._keepalive_abmelden is not None:
            return
        try:
            from datetime import timedelta

            from homeassistant.helpers.event import async_track_time_interval
        except ImportError:  # pragma: no cover — Testumgebung
            return

        async def _takt(_jetzt: Any) -> None:
            await self._keepalive_takt()

        self._keepalive_abmelden = async_track_time_interval(
            self._hass, _takt, timedelta(seconds=MODE1_KEEPALIVE_S)
        )

    def _keepalive_beenden(self) -> None:
        """Takt und SOC-Beobachtung beenden — jeder neue Befehl tut das.

        Auch ein offener Stopp am Ziel erledigt sich damit: Der neue Befehl
        ersetzt den alten Mode-1-Befehl, oder er ist selbst ein Stopp.
        """
        self._mode1_watt = None
        self._stopp_offen = False
        self._beobachtung_beenden()
        if self._keepalive_abmelden is not None:
            try:
                self._keepalive_abmelden()
            except Exception:  # noqa: BLE001 — Abmelden darf nie werfen
                pass
            self._keepalive_abmelden = None

    async def _keepalive_takt(self) -> None:
        """Ein Takt: den laufenden Befehl unverändert erneut auslösen.

        Unter der Sperre und erst nach erneuter Prüfung — ein Stopp kann
        zwischen Anmeldung und Takt gekommen sein. Ein Fehlschlag wird nur
        protokolliert: Der nächste Takt versucht es wieder, und bis zum Ablauf
        der Wiederholung läuft der Befehl ohnehin weiter.
        """
        async with self._sperre:
            # Netzladung am Ziel: zurücknehmen statt erneuern — auch als
            # Wiederholung eines gescheiterten Stopps, denn am Ziel ändert
            # sich der SOC womöglich nicht mehr, und der Beobachter bliebe
            # stumm. Ein offener Stopp gilt auch, wenn der SOC inzwischen
            # wieder unter dem Ziel liegt.
            if self._stopp_offen or self._am_ziel():
                await self._ziel_erreicht()
                return
            if self._mode1_watt is None:
                return
            if self._ziel_soc is not None:
                self._warne_soc_unlesbar()
            try:
                await self._mode1_schreiben(self._mode1_watt)
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "SolaX: Mode-1-Befehl nicht erneuert — nächster Versuch "
                    "im nächsten Takt", exc_info=True,
                )

    async def async_disconnect(self) -> None:
        """Beim Entladen der Integration: den Takt abmelden.

        Den Befehl selbst nimmt vorher die Freigabe des Executors zurück
        (``async_stop_forcible``); hier geht es nur darum, dass kein Takt
        eines entladenen Eintrags weiterläuft.
        """
        self._keepalive_beenden()

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
    # Batterie aus dem Netz laden (Beta)
    # ------------------------------------------------------------------
    def _mode1_vorhanden(self) -> bool:
        """Sind alle Mode-1-Entitäten auffindbar? Auf Gen6 nicht."""
        return all(
            self._hass.states.get(self._resolve_entity(key)) is not None
            for key in _MODE1_ENTITAETEN
        )

    @property
    def supports_forced_discharge(self) -> bool:
        """Kann der Treiber zwangsentladen? Nur mit Mode 1 — auf Gen6 nicht.

        Die Zwangsentladung läuft über Mode 1 („Enabled Battery Control" mit
        negativer Leistung). Gen6 hat ihn nicht: Jede geplante Einspeisung
        scheiterte dort mit „Mode 1 nicht verfügbar", die Schreibbremse
        griffe, und die Telemetrie meldete jeden Tag ``inverter_write`` für
        etwas, das der Treiber gar nicht kann. Mit False gibt der Executor
        statt einer Zwangsentladung frei — der Plan rechnet zwar weiter mit
        der Einspeisung, die Batterie bleibt aber im Automatikbetrieb.
        """
        return self._mode1_vorhanden()

    @property
    def supports_grid_charge(self) -> bool:
        """„Batterie aus dem Netz laden" (Beta) — Mode 1 mit positiver Leistung.

        Nur, wenn die Mode-1-Entitäten auffindbar sind. Gen6 hat keinen
        Mode 1; dort bliebe ein Netzladebefehl ohne Wirkung, und der Plan
        kaufte Strom für eine Ladung, die nie kommt.
        """
        return self._mode1_vorhanden()

    async def async_set_grid_charge(
        self, power_kw: float, target_soc: float | None = None
    ) -> bool:
        """Batterie mit ``power_kw`` laden, PV zuerst, den Rest aus dem Netz.

        Mode 1 „Enabled Battery Control" mit POSITIVER ``active_power``: Die
        Batterie lädt genau diese Leistung; die Integration rechnet die PV
        heraus (``ap_target = target − pv_power``) und holt den Rest aus dem
        Netz, die Hauslast ebenso. Dieselbe Schreibfolge wie die Entladung,
        derselbe Keepalive-Takt.

        Zwei Dinge erledigt der Treiber selbst:

        * **Ladestrom vorher zurück.** Ein stehendes Ladelimit
          (``battery_charge_max_current``, etwa 0 A aus dem Slot davor)
          deckelt vermutlich auch die Mode-1-Ladung. Zurück auf den
          gesicherten Vorwert, ohne ihn auf das Maximum des Geräts.
        * **Ziel-SOC.** Das Gerät hört bei keinem Ladestand auf, der Executor
          schreibt nur bei einer Änderung — zwischen zwei Befehlen hielte
          niemand an. Der Treiber beobachtet den SOC-Sensor und gibt am Ziel
          frei wie ``async_stop_forcible``. Ohne Ziel lädt er bis 100 %.
          **Ist der SOC nicht lesbar** (kein Sensor konfiguriert, Sensor
          ``unavailable``), gibt es keinen Stopp am Ziel: Die Ladung läuft,
          bis der Executor einen anderen Befehl schreibt, im schlimmsten
          Fall bis zum Ablauf der Wiederholung im Gerät. Der Treiber warnt
          dann einmal je Befehl im Log.

        ``remotecontrol_import_limit`` bleibt unberührt: Die Netzgrenze führt
        der Executor in jedem Lauf über die Leistung nach.

        Am Gerät nicht geprüft (Stand 02.11.2026) — das ist der Weg, den
        die Integration dokumentiert.
        """
        if self._nicht_endlich(power_kw, target_soc):
            return False
        async with self._sperre:
            return await self._netzladung(power_kw, target_soc)

    async def _netzladung(self, power_kw: float, target_soc: float | None) -> bool:
        ziel = 100.0 if target_soc is None else min(100.0, float(target_soc))
        leistung = max(0.0, float(power_kw))
        soc = self._soc()
        if leistung < NETZLADEN_MIN_KW or (soc is not None and soc >= ziel):
            # Nichts aus dem Netz (die Netzgrenze lässt gerade nichts zu)
            # oder das Ziel steht schon: wie eine Freigabe. Ein Mode-1-Befehl
            # mit 0 W hielte die Batterie fest — auch gegen PV-Überschuss.
            return self._erfolg() if await self._stopp() else False
        self._keepalive_beenden()
        try:
            await self._ladestrom_freigeben()
            power_w = self._ladeleistung_w(leistung)
            await self._mode1_schreiben(power_w)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("SolaX: Netzladung konnte nicht gestartet werden")
            # Nichts halb stehen lassen: Der Executor hält den Befehl für
            # gescheitert und schreibt bei unverändertem Plan womöglich nichts
            # anderes mehr.
            try:
                await self._stopp_schreiben()
            except Exception:  # noqa: BLE001 — der Fehlerpfad darf nie werfen
                _LOGGER.debug("SolaX: Stopp nach gescheiterter Netzladung", exc_info=True)
            return self._fehler(f"Netzladung: {type(exc).__name__}")
        self._discharge_active = False
        self._beobachte_ziel_soc(ziel)
        self._starte_keepalive(power_w)
        self._warne_soc_unlesbar()
        return self._erfolg()

    def _ladeleistung_w(self, leistung_kw: float) -> int:
        """kW → W in 100-W-Schritten, gedeckelt am Maximum der Entität.

        Mindestens ein Schritt: Unter ``NETZLADEN_MIN_KW`` kommt man hier
        nicht an, und 0 W wäre kein Laden, sondern ein Festhalten.
        """
        watt = max(MODE1_SCHRITT_W, round(leistung_kw * 1000.0 / MODE1_SCHRITT_W) * MODE1_SCHRITT_W)
        state = self._hass.states.get(self._resolve_entity("remotecontrol_active_power"))
        try:
            maximum = abs(float(state.attributes.get("max"))) if state is not None else None
        except (TypeError, ValueError):
            maximum = None
        if maximum is not None:
            watt = min(watt, int(maximum // MODE1_SCHRITT_W) * MODE1_SCHRITT_W)
        return int(watt)

    async def _ladestrom_freigeben(self) -> None:
        """Ein eigenes Ladelimit zurücknehmen, bevor Mode 1 laden soll.

        Ziel ist der gesicherte Vorwert. Ohne ihn hat der Treiber das Limit
        nie verstellt — ein Wert über 0 ist dann die Einstellung des
        Betreibers und bleibt. Nur 0 A ohne Vorwert (Neustart mitten im
        Sperren, Store verloren) geht auf das Maximum der Entität.
        """
        await self._state_store.async_load()
        aktuell = self._read_current_charge_max_current()
        ziel = self._state_store.original_current
        if ziel is None:
            if aktuell is not None and aktuell > 0:
                return
            ziel = self._read_max_charge_current_attribute()
        if ziel is None:
            _LOGGER.warning(
                "SolaX: Ladestrom vor der Netzladung nicht zurücksetzbar — "
                "Vorwert und Maximum unbekannt"
            )
            return
        if aktuell is not None and abs(aktuell - ziel) < 0.05:
            return
        await self._set_number("battery_charge_max_current", ziel)

    # ------------------------------------------------------------------
    # Ziel-SOC der Netzladung
    # ------------------------------------------------------------------
    def _soc(self) -> float | None:
        sensor = self._config.get(CONF_BATTERY_SOC_SENSOR)
        if not sensor:
            return None
        state = self._hass.states.get(sensor)
        if state is None:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _am_ziel(self) -> bool:
        if self._ziel_soc is None:
            return False
        soc = self._soc()
        return soc is not None and soc >= self._ziel_soc

    def _warne_soc_unlesbar(self) -> None:
        """Ohne lesbaren SOC kein Stopp am Ziel — einmal je Befehl sagen."""
        if self._soc_warnung_gegeben or self._soc() is not None:
            return
        self._soc_warnung_gegeben = True
        _LOGGER.warning(
            "SolaX: Ladestand nicht lesbar (%s) — die Netzladung endet nicht "
            "am Ziel-SOC %.0f %%, sondern erst mit dem nächsten Befehl",
            self._config.get(CONF_BATTERY_SOC_SENSOR) or "kein Sensor",
            self._ziel_soc or 0.0,
        )

    def _beobachte_ziel_soc(self, ziel: float) -> None:
        """Am SOC-Sensor anmelden und am Ziel freigeben (Muster: Victron)."""
        self._ziel_soc = ziel
        self._soc_warnung_gegeben = False
        if self._ziel_abmelden is not None:
            return
        sensor = self._config.get(CONF_BATTERY_SOC_SENSOR)
        if not sensor:
            return
        try:
            from homeassistant.helpers.event import async_track_state_change_event
        except ImportError:  # pragma: no cover — Testumgebung
            return
        try:
            from homeassistant.core import callback
        except ImportError:  # pragma: no cover — Testumgebung
            def callback(func: Any) -> Any:
                func._hass_callback = True
                return func

        # @callback ist Pflicht: Eine schlichte Funktion führt HA im
        # Executor-Thread aus, und ``hass.async_create_task`` aus einem
        # fremden Thread weist HA ab — die Rücknahme am Ziel-SOC liefe nie,
        # Mode 1 lüde bis zum nächsten Takt (bis 240 s) über das Ziel hinaus
        # aus dem Netz. Dasselbe Muster wie im Victron-Treiber und in
        # leistungsspitze.py (Grünbach, 25.09.2026).
        @callback
        def _soc_geaendert(_event: Any) -> None:
            if self._am_ziel():
                self._hass.async_create_task(self._ziel_erreicht_gesperrt())

        self._ziel_abmelden = async_track_state_change_event(
            self._hass, [sensor], _soc_geaendert
        )

    def _beobachtung_beenden(self) -> None:
        self._ziel_soc = None
        if self._ziel_abmelden is not None:
            try:
                self._ziel_abmelden()
            except Exception:  # noqa: BLE001 — Abmelden darf nie werfen
                pass
            self._ziel_abmelden = None

    async def _ziel_erreicht_gesperrt(self) -> None:
        """Aus dem SOC-Beobachter: unter der Sperre und erst nach erneuter
        Prüfung — ein Befehl dazwischen kann ein neues Ziel gesetzt haben."""
        async with self._sperre:
            if self._am_ziel():
                await self._ziel_erreicht()

    async def _ziel_erreicht(self) -> None:
        """Am Ziel freigeben wie ``async_stop_forcible``.

        Scheitert der Stopp, bleiben Beobachtung und Takt stehen — der Takt
        erneuert dann nicht mehr den Ladebefehl, sondern wiederholt den
        Stopp (``_stopp_offen``), gleich wo der SOC inzwischen steht. Ein
        stehender Mode-1-Befehl kaufte sonst bis zum Ablauf der
        Wiederholung weiter Netzstrom.
        """
        ziel = self._ziel_soc
        self._mode1_watt = None
        if await self._stopp_schreiben():
            self._keepalive_beenden()
            _LOGGER.info("SolaX: Ziel-SOC %.0f %% erreicht — Netzladung beendet", ziel or 0.0)
        else:
            self._stopp_offen = True
            _LOGGER.warning(
                "SolaX: Netzladung am Ziel-SOC nicht beendet — wird wiederholt"
            )

    # ------------------------------------------------------------------
    # Fahrplan-Steuerschnittstelle (Schedule-Executor)
    # ------------------------------------------------------------------
    @property
    def supports_schedule_control(self) -> bool:
        """Der Fahrplan-Executor darf diesen Wechselrichter stellen."""
        return True

    # ------------------------------------------------------------------
    # Entladedeckel (Spitzenkappung)
    # ------------------------------------------------------------------
    @property
    def supports_discharge_limit(self) -> bool:
        """Über ``battery_discharge_max_current`` — das Gegenstück zum Ladelimit.

        Wie beim Laden ein STROM in Ampere, umgerechnet mit der
        Batteriespannung. Der Self-Use-Modus läuft weiter und deckt das Haus
        bis zu diesem Strom; 0 A heißt, die Batterie gibt nichts ab. Am Gerät
        noch nicht geprüft (Stand 29.10.2026) — bis dahin ist das der Weg,
        den die Entität verspricht.
        """
        return True

    def _read_discharge_current(self) -> tuple[float | None, float | None]:
        """(aktueller Wert, Maximum) der Entladestrom-Entität in A."""
        state = self._hass.states.get(self._resolve_entity("battery_discharge_max_current"))
        if state is None:
            return None, None
        try:
            wert = float(state.state)
        except (TypeError, ValueError):
            wert = None
        try:
            maximum = float(state.attributes.get("max"))
        except (TypeError, ValueError):
            maximum = None
        return wert, maximum

    async def async_set_discharge_limit(self, power_kw: float | None) -> bool:
        """Entladestrom deckeln; ``None`` stellt den gesicherten Wert wieder her.

        Der Originalwert wird beim ersten Deckel gesichert (Store, überlebt
        einen Neustart) — ein bewusst niedriger eingestellter Entladestrom des
        Betreibers bleibt so erhalten, statt beim Aufheben auf das Maximum zu
        springen. Ein Wert von 0 beim Sichern ist schon unser eigener Deckel
        (Neustart mitten im Halten) und wird nicht gesichert.
        """
        if power_kw is not None and self._nicht_endlich(power_kw):
            return False
        try:
            await self._state_store.async_load()
            aktuell, maximum = self._read_discharge_current()
            if aktuell is None and maximum is None:
                return self._fehler("Entladestrom-Entität nicht verfügbar")
            if power_kw is None:
                ziel = self._state_store.original_discharge_current
                if ziel is None:
                    ziel = maximum
                if ziel is None:
                    return self._fehler("Entladestrom: Maximum unbekannt")
                await self._set_number("battery_discharge_max_current", ziel)
                await self._state_store.async_clear_original_discharge_current()
                return self._erfolg()
            if (
                self._state_store.original_discharge_current is None
                and aktuell is not None
                and aktuell > 0
            ):
                await self._state_store.async_save_original_discharge_current(aktuell)
            amps = max(0.0, float(power_kw) * 1000.0 / self._read_battery_voltage_or_default())
            if maximum is not None:
                amps = min(amps, maximum)
            await self._set_number("battery_discharge_max_current", round(amps, 1))
            return self._erfolg()
        except Exception as exc:
            _LOGGER.exception("SolaX: Entladedeckel fehlgeschlagen")
            return self._fehler(type(exc).__name__)

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
        # Ein gesicherter Vorwert ist der Beweis, dass WIR den Boden verstellt
        # haben — unabhängig davon, ob die Entladung zustande kam. Am Flag
        # allein hing es bis 2.1.1-dev20: Es wird erst nach dem letzten
        # Befehl gesetzt, der abgesenkte Boden steht aber schon vorher im
        # Gerät. Bricht etwas dazwischen ab, meldeten wir dem Fahrplan unseren
        # eigenen Boden als Gerätegrenze und er plante mit jeder Entladung
        # tiefer. Beim Fronius ging dieselbe Konstruktion am 13.09.2026
        # andersherum schief (angehobene Reserve sperrte die Batterie).
        gesichert = self._state_store.original_discharge_floor
        if gesichert is not None:
            return gesichert
        if self._discharge_active:
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
            # Ein Wert für beide Richtungen: negativ entlädt, positiv lädt
            # (Netzladung).
            ("remotecontrol_active_power", "Mode-1-Leistung (− Entladen, + Laden)", "discharge_limit"),
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
