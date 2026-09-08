"""Sigenergy SigenStor — Steuerung über die HA-Integration ``sigen``.

Die HACS-Integration `TypQxQ/Sigenergy-Local-Modbus <https://github.com/TypQxQ/Sigenergy-Local-Modbus>`_
(Domain ``sigen``) bietet alle Stellgrößen des Remote-EMS als
Standardentitäten der Anlage („Sigen Plant"):

- ``switch`` „Remote EMS (Controlled by Home Assistant)"  → Register 40029
- ``select`` „Remote EMS Control Mode"                     → Register 40031
- ``number`` „ESS Max Charging Limit" (kW)                 → Register 40032
- ``number`` „ESS Max Discharging Limit" (kW)              → Register 40034

Geschrieben wird per ``switch.turn_on`` / ``select.select_option`` /
``number.set_value`` — dasselbe Muster wie beim SolaX-Treiber, kein eigenes
Modbus, kein Keepalive, keine Skalierungsfaktoren. Die drei Absichten des
Fahrplan-Executors werden so abgebildet:

  Ladelimit X kW  → Modus „Command Charging (PV First)", Ladelimit = X
                    (0 kW sperrt das Laden; Register 40032 wirkt nur in den
                    Lademodi 3/4, deshalb der Moduswechsel)
  Entladung X kW  → Modus „Command Discharging (ESS First)", Entladelimit = X
  Freigabe        → Modus „Maximum Self Consumption", danach Remote EMS AUS —
                    das Gerät läuft wieder in seinem eigenen EMS-Arbeitsmodus.

Zwei Eigenheiten der Integration bestimmen den Ablauf:

1. **Alle Steuerentitäten sind ab Werk deaktiviert**
   (``entity_registry_enabled_default=False``, an der Testanlage 189 von 294
   Entitäten). Sie müssen einmalig in der Entity-Registry aktiviert werden —
   der Einrichtungsassistent prüft das über ``sigen_control_entity_status``
   und benennt, was fehlt.
2. **Die Modus-Auswahl ist nur verfügbar, solange der Remote-EMS-Schalter an
   ist** (``available_fn`` der Integration prüft Register 40029 == 1). Home
   Assistant überspringt Service-Aufrufe an nicht verfügbare Entitäten
   stillschweigend (``entity_service_call`` filtert auf ``entity.available``)
   — ein ``select_option`` direkt nach dem Einschalten ginge ins Leere, ohne
   Fehler. Der Treiber schaltet deshalb zuerst ein und wartet, bis die
   Auswahl verfügbar ist; die Integration stößt nach jedem Schreibvorgang
   sofort einen Refresh an, das dauert typisch unter einer Sekunde.

**Kein geräteseitiges Failsafe.** Die Sigenergy-Modbus-Spezifikation (V2.7)
kennt weder Watchdog noch Rückfallzeit — anders als Fronius (``RvrtTms``),
Kostal und SMA. Bleibt Home Assistant mitten in einem Befehl stehen, läuft
er am Gerät weiter. Deshalb ist die Freigabe hier das AUSSCHALTEN des Remote
EMS und nicht nur ein Moduswechsel, und deshalb gibt die Integration beim
Entladen und beim Umschalten auf „Aus" immer aktiv frei.

**Am Gerät zu verifizieren (Feldtest):**

- Sperrt „Command Charging (PV First)" mit Limit 0 wirklich nur das Laden
  und die Hauslast kommt weiter aus der Batterie — oder entlädt der Speicher
  in diesem Modus gar nicht (dann käme sie aus dem Netz; Alternative wäre
  „Command Discharging (PV First)" mit vollem Entladelimit)?
- Liefert „Command Discharging (ESS First)" ins Netz oder deckt es nur die
  Hauslast?
- Stoppt der Entlade-Cut-Off (Register 40048, ``ESS Discharge Cut-Off State
  of Charge``) eine befohlene Entladung wie der Entladeboden bei SolaX? Der
  Treiber liest ihn als Untergrenze für den Fahrplan, schreibt ihn aber nicht.

Vorzeichen der Sensoren (Register 30037 / 30005, von der Integration roh
durchgereicht): Batterieleistung positiv = laden — wie bei uns, an der
Testanlage bestätigt (−0,696 kW bei ``binary_sensor…battery_discharging``
= on); Netzleistung positiv = BEZUG — gegen unsere Konvention, daher
``grid_sign = -1`` in ``INVERTER_SIGN_CONVENTIONS``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from .base import InverterBase
from ..power_readings import read_power_kw

_LOGGER = logging.getLogger(__name__)

SIGEN_DOMAIN = "sigen"

# Options-Strings der Modus-Auswahl, wörtlich aus select.py der Integration.
MODE_SELF_CONSUMPTION = "Maximum Self Consumption"
MODE_COMMAND_CHARGING_PV_FIRST = "Command Charging (PV First)"
MODE_COMMAND_DISCHARGING_ESS_FIRST = "Command Discharging (ESS First)"

# Wie lange nach dem Einschalten des Remote EMS auf die Modus-Auswahl
# gewartet wird. Die Integration refresht direkt nach dem Schreiben; die
# Grenze fängt nur den Fall ab, dass das Gerät den Schalter nicht annimmt.
REMOTE_EMS_READY_TIMEOUT_S = 10.0
REMOTE_EMS_POLL_INTERVAL_S = 0.5

# Entity-Key -> Default-Entity-ID. Die IDs entstehen in der Integration aus
# Gerätename („Sigen Plant") + Entitätsname; wer das Gerät umbenennt, bekommt
# andere IDs — dafür gibt es den Suffix-Scan und die Registry-Auflösung.
SIGEN_ENTITY_DEFAULTS: dict[str, str] = {
    "remote_ems_switch": "switch.sigen_plant_remote_ems_controlled_by_home_assistant",
    "remote_ems_mode": "select.sigen_plant_remote_ems_control_mode",
    "ess_max_charging_limit": "number.sigen_plant_ess_max_charging_limit",
    "ess_max_discharging_limit": "number.sigen_plant_ess_max_discharging_limit",
    "ess_discharge_cut_off_soc": "number.sigen_plant_ess_discharge_cut_off_state_of_charge",
    "ess_backup_soc": "number.sigen_plant_ess_backup_state_of_charge",
    "ess_rated_charging_power": "sensor.sigen_plant_ess_rated_charging_power",
    "ess_rated_discharging_power": "sensor.sigen_plant_ess_rated_discharging_power",
}

# Suffix-Muster je Entity (Object-ID ohne Domain). Die Anlagen-Entitäten
# unterscheiden sich von den Wechselrichter-Entitäten im Wortlaut
# („ess_rated_charging_power" gegen „ess_rated_charge_power"), sodass die
# Muster nicht versehentlich ein Gerät zweiter Ebene treffen.
SIGEN_CONTROL_ENTITY_PATTERNS: dict[str, tuple[str, re.Pattern]] = {
    "remote_ems_switch": ("switch", re.compile(r"remote_ems_controlled_by_home_assistant$")),
    "remote_ems_mode": ("select", re.compile(r"remote_ems_control_mode$")),
    "ess_max_charging_limit": ("number", re.compile(r"ess_max_charging_limit$")),
    "ess_max_discharging_limit": ("number", re.compile(r"ess_max_discharging_limit$")),
    "ess_discharge_cut_off_soc": ("number", re.compile(r"ess_discharge_cut_off_state_of_charge$")),
    "ess_backup_soc": ("number", re.compile(r"ess_backup_state_of_charge$")),
    "ess_rated_charging_power": ("sensor", re.compile(r"ess_rated_charging_power$")),
    "ess_rated_discharging_power": ("sensor", re.compile(r"ess_rated_discharging_power$")),
}

# Originalnamen in der Entity-Registry (``original_name``) — stabil auch dann,
# wenn der Nutzer Entity-IDs oder Anzeigenamen geändert hat. Nur über die
# Registry sind auch DEAKTIVIERTE Entitäten sichtbar; die State-Machine kennt
# sie nicht.
SIGEN_CONTROL_ORIGINAL_NAMES: dict[str, tuple[str, str]] = {
    "remote_ems_switch": ("switch", "Remote EMS (Controlled by Home Assistant)"),
    "remote_ems_mode": ("select", "Remote EMS Control Mode"),
    "ess_max_charging_limit": ("number", "ESS Max Charging Limit"),
    "ess_max_discharging_limit": ("number", "ESS Max Discharging Limit"),
    "ess_discharge_cut_off_soc": ("number", "ESS Discharge Cut-Off State of Charge"),
    "ess_backup_soc": ("number", "ESS Backup State of Charge"),
}

# Ohne diese vier kann der Treiber nichts stellen. Die beiden SOC-Grenzen sind
# nur Lesewerte für den Fahrplan und dürfen fehlen.
SIGEN_REQUIRED_CONTROLS: tuple[str, ...] = (
    "remote_ems_switch",
    "remote_ems_mode",
    "ess_max_charging_limit",
    "ess_max_discharging_limit",
)


def find_sigen_control_entity(hass: Any, config_key: str) -> str | None:
    """Suche die Sigenergy-Entity per Suffix-Scan über die State-Machine.

    Findet nur AKTIVIERTE Entitäten (deaktivierte haben keinen State). Bei
    mehreren Treffern gewinnt der kürzeste Name — an einer Anlage mit
    mehreren Wechselrichtern ist das die Anlagen-Entität ohne Zähl-Suffix.
    """
    domain, pattern = SIGEN_CONTROL_ENTITY_PATTERNS[config_key]
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


def sigen_control_entity_status(hass: Any) -> dict[str, dict[str, Any]]:
    """Zustand der Steuerentitäten aus der Entity-Registry.

    Je Schlüssel ``{"name", "entity_id", "enabled"}``: ``entity_id`` None,
    wenn die Integration die Entität nicht angelegt hat; ``enabled`` False,
    wenn sie existiert, aber deaktiviert ist — der Normalfall nach der
    Installation. Leeres Dict, wenn keine Registry erreichbar ist (Tests).
    """
    try:
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        entries = list(registry.entities.values())
    except Exception:  # noqa: BLE001 — ohne Registry keine Aussage
        return {}

    status: dict[str, dict[str, Any]] = {}
    for key, (domain, name) in SIGEN_CONTROL_ORIGINAL_NAMES.items():
        treffer = None
        for entry in entries:
            if getattr(entry, "platform", None) != SIGEN_DOMAIN:
                continue
            if getattr(entry, "domain", None) != domain:
                continue
            if getattr(entry, "original_name", None) != name:
                continue
            treffer = entry
            break
        status[key] = {
            "name": name,
            "entity_id": getattr(treffer, "entity_id", None) if treffer else None,
            "enabled": (getattr(treffer, "disabled_by", None) is None) if treffer else None,
        }
    return status


class SigenergyInverter(InverterBase):
    """Sigenergy SigenStor über die HA-Integration ``sigen`` (Remote EMS)."""

    def __init__(self, hass: Any, config: dict) -> None:
        super().__init__(hass, config)
        # Zuletzt von uns gesetzter Modus — für die Transparenz-Ansicht und
        # damit die Freigabe weiß, ob überhaupt etwas zurückzunehmen ist.
        self._active_mode: str | None = None

    # ------------------------------------------------------------------
    # Entitäten
    # ------------------------------------------------------------------
    def _resolve_entity(self, config_key: str) -> str:
        """Config → Suffix-Scan → Default (Reihenfolge wie beim SolaX-Treiber).

        Der konfigurierte Wert gilt nur, solange die Entität existiert — nach
        einer Umbenennung in der Integration greift sonst der Scan.
        """
        configured = self._config.get(f"sigen_{config_key}")
        if configured and self._hass.states.get(configured) is not None:
            return configured
        found = find_sigen_control_entity(self._hass, config_key)
        if found:
            return found
        return configured or SIGEN_ENTITY_DEFAULTS[config_key]

    def _state_str(self, entity_id: str) -> str | None:
        state = self._hass.states.get(entity_id)
        if state is None:
            return None
        return str(state.state)

    def _state_float(self, entity_id: str) -> float | None:
        raw = self._state_str(entity_id)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _ist_verfuegbar(raw: str | None) -> bool:
        return raw is not None and raw not in ("unavailable", "unknown", "")

    async def _set_number(self, config_key: str, value: float) -> None:
        await self._hass.services.async_call(
            "number", "set_value",
            {"entity_id": self._resolve_entity(config_key), "value": round(float(value), 3)},
            blocking=True,
        )

    async def _set_mode(self, option: str) -> None:
        await self._hass.services.async_call(
            "select", "select_option",
            {"entity_id": self._resolve_entity("remote_ems_mode"), "option": option},
            blocking=True,
        )
        self._active_mode = option

    async def _switch(self, on: bool) -> None:
        await self._hass.services.async_call(
            "switch", "turn_on" if on else "turn_off",
            {"entity_id": self._resolve_entity("remote_ems_switch")},
            blocking=True,
        )

    async def _ensure_remote_ems(self) -> None:
        """Remote EMS einschalten und warten, bis die Modus-Auswahl da ist.

        Ohne das Warten ginge der folgende ``select_option`` verloren: Home
        Assistant ruft Services auf nicht verfügbaren Entitäten gar nicht auf
        (siehe Modul-Docstring). Ist der Schalter schon an und die Auswahl
        verfügbar, passiert nichts.
        """
        select_id = self._resolve_entity("remote_ems_mode")
        schalter = self._state_str(self._resolve_entity("remote_ems_switch"))
        if schalter != "on":
            await self._switch(True)
        if self._ist_verfuegbar(self._state_str(select_id)):
            return
        gewartet = 0.0
        while gewartet < REMOTE_EMS_READY_TIMEOUT_S:
            await asyncio.sleep(REMOTE_EMS_POLL_INTERVAL_S)
            gewartet += REMOTE_EMS_POLL_INTERVAL_S
            if self._ist_verfuegbar(self._state_str(select_id)):
                return
        # Nicht abbrechen: der Aufruf danach schlägt sichtbar fehl oder wird
        # übersprungen — beides landet im Log, und der Executor zählt den
        # Schreibfehler.
        _LOGGER.warning(
            "Sigenergy: Modus-Auswahl %s nach %.0f s noch nicht verfügbar — "
            "ist der Schalter „Remote EMS“ aktiviert und das Gerät erreichbar?",
            select_id, REMOTE_EMS_READY_TIMEOUT_S,
        )

    # ------------------------------------------------------------------
    # InverterBase
    # ------------------------------------------------------------------
    async def async_set_charge_limit(self, power_kw: float) -> bool:
        """Laden auf ``power_kw`` begrenzen; 0 sperrt das Laden.

        Reihenfolge: erst das Limit, dann der Modus — sonst liefe der
        Lademodus einen Moment mit einem alten, höheren Limit.
        """
        try:
            await self._ensure_remote_ems()
            await self._set_number("ess_max_charging_limit", max(0.0, float(power_kw)))
            await self._set_mode(MODE_COMMAND_CHARGING_PV_FIRST)
            return True
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Sigenergy: Ladelimit konnte nicht gesetzt werden")
            return False

    async def async_set_discharge(
        self, power_kw: float, target_soc: float | None = None
    ) -> bool:
        """Erzwungene Entladung mit ``power_kw``.

        ``target_soc`` setzt der Executor selbst durch (er stoppt am Ziel);
        das Gerät bekommt keinen SOC — den Cut-Off (40048) schreiben wir
        bewusst nicht, solange sein Verhalten am Gerät nicht geprüft ist.
        """
        try:
            await self._ensure_remote_ems()
            await self._set_number("ess_max_discharging_limit", max(0.0, abs(float(power_kw))))
            await self._set_mode(MODE_COMMAND_DISCHARGING_ESS_FIRST)
            return True
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Sigenergy: Entladung konnte nicht gestartet werden")
            return False

    async def async_stop_forcible(self) -> bool:
        """Freigeben: Eigenverbrauchsmodus setzen, dann Remote EMS ausschalten.

        Der Moduswechsel zuerst: schlägt das Ausschalten fehl, steht das
        Gerät wenigstens im Eigenverbrauch statt in einem Befehl. Ist der
        Schalter schon aus, ist die Auswahl nicht verfügbar — dann gibt es
        nichts zurückzunehmen.
        """
        try:
            schalter = self._state_str(self._resolve_entity("remote_ems_switch"))
            if schalter != "off":
                if self._ist_verfuegbar(self._state_str(self._resolve_entity("remote_ems_mode"))):
                    await self._set_mode(MODE_SELF_CONSUMPTION)
                await self._switch(False)
            self._active_mode = None
            return True
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Sigenergy: Freigabe fehlgeschlagen")
            return False

    @property
    def is_available(self) -> bool:
        entries = self._hass.config_entries.async_entries(SIGEN_DOMAIN)
        return any(entry.state.value == "loaded" for entry in entries)

    # ------------------------------------------------------------------
    # Fahrplan-Steuerschnittstelle (Schedule-Executor)
    # ------------------------------------------------------------------
    @property
    def supports_schedule_control(self) -> bool:
        return True

    async def async_get_charge_limit_kw(self) -> float | None:
        """Gesetztes Ladelimit — nur im Lademodus eine Aussage.

        Im Eigenverbrauch oder beim Entladen ist Register 40032 wirkungslos;
        sein Wert wäre dann kein Limit. None lässt den Executor auf den
        zuletzt geschriebenen Wert bzw. den Planwert zurückfallen.
        """
        if self._state_str(self._resolve_entity("remote_ems_mode")) != MODE_COMMAND_CHARGING_PV_FIRST:
            return None
        return self._state_float(self._resolve_entity("ess_max_charging_limit"))

    def get_charge_limit_max_kw(self) -> float | None:
        """Nennladeleistung des Speichers (``ESS Rated Charging Power``)."""
        return read_power_kw(self._hass, self._resolve_entity("ess_rated_charging_power"))

    def get_max_discharge_power_kw(self) -> float | None:
        """Nennentladeleistung des Speichers (``ESS Rated Discharging Power``)."""
        return read_power_kw(self._hass, self._resolve_entity("ess_rated_discharging_power"))

    def get_backup_reserve_soc_pct(self) -> float | None:
        """Höhere der beiden Gerätegrenzen: Backup-SOC und Entlade-Cut-Off.

        Beide Entitäten sind ab Werk deaktiviert — dann None, und der
        Fahrplan rechnet allein mit dem konfigurierten Mindest-Ladestand.
        """
        werte = [
            v for v in (
                self._state_float(self._resolve_entity("ess_backup_soc")),
                self._state_float(self._resolve_entity("ess_discharge_cut_off_soc")),
            )
            if v is not None
        ]
        return max(werte) if werte else None

    def get_control_entities(self) -> list[dict]:
        rows: list[dict] = []
        for config_key, label, rolle in (
            ("remote_ems_switch", "Remote EMS", "mode"),
            ("remote_ems_mode", "Betriebsmodus", "mode"),
            ("ess_max_charging_limit", "Ladelimit", "charge_limit"),
            ("ess_max_discharging_limit", "Entladelimit", "discharge_limit"),
            ("ess_discharge_cut_off_soc", "Entlade-Cut-Off", "backup_soc"),
        ):
            entity_id = self._resolve_entity(config_key)
            if entity_id and self._hass.states.get(entity_id) is not None:
                rows.append({"label": label, "entity_id": entity_id, "role": rolle})
        return rows
