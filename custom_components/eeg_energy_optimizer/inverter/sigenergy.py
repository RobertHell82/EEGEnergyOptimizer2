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

  Ladelimit X kW  → Ladelimit = X im Modus „Maximum Self Consumption",
                    Entladelimit auf Nennleistung
                    (siehe „Warum kein Command Charging" unten)
  Entladung X kW  → Modus „Command Discharging (ESS First)", Entladelimit = X,
                    Ladelimit auf Nennleistung
  Freigabe        → Modus „Maximum Self Consumption", beide Limits auf
                    Nennleistung, danach Remote EMS AUS — das Gerät läuft
                    wieder in seinem eigenen EMS-Arbeitsmodus.

Dazu zwei Beta-Funktionen, beide **am Gerät noch nicht geprüft**:

  Entladedeckel   → Entladelimit = X im Modus „Maximum Self Consumption"
  (Spitzenkappung)  (0 → 0,01 kW), neben einem laufenden Ladelimit
  Netzladung X kW → Ladelimit = X, Modus „Command Charging (PV First)";
                    den Ziel-SOC überwacht der Treiber selbst.

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
   sofort einen Refresh an, das dauert typisch unter einer Sekunde. Seit
   ``sigen`` 1.3.0 wartet jedes Schreiben von Schalter und Modus außerdem auf
   die Rückmeldung des Geräts, bis zu 60 s.

**Die Limits wirken global.** Laut Spezifikation V2.9 (Tab. 5-2-1) gelten
40032 und 40034 „regardless of the EMS operating mode" — in jedem Modus und
auch, wenn Remote EMS wieder aus ist. Bis 2.1.62 ging der Treiber nach V2.7
davon aus, dass sie nur in den Befehlsmodi greifen, und ließ sie bei der
Freigabe stehen: Ein Ladelimit 0 aus dem Fahrplan hätte dann das Laden auch
im eigenen EMS des Geräts weiter gesperrt, ein altes Entladelimit den
Eigenverbrauch gedeckelt. Deshalb setzt jede Freigabe beide auf die
Nennleistung zurück, BEVOR Remote EMS ausgeht, und jeder Befehl stellt das
Limit, das er selbst nicht braucht, ebenfalls auf die Nennleistung. Steht der
Schalter bei der Freigabe schon aus, liest sie beide Limits und setzt zurück,
was darunter steht, ohne Remote EMS einzuschalten: ein Ladelimit 0 aus der
Zeit bis 2.1.62, ein Deckel, nach dem jemand Remote EMS von Hand
ausgeschaltet hat. Die Limits sind auch bei aus schreibbar.

**Warum kein Command Charging.** Bis 2.1.0-dev6 wurde für ein Ladelimit in
den Modus „Command Charging (PV First)" geschaltet, weil Register 40032 laut
Spezifikation V2.7 nur in den Lademodi 3/4 wirkt. Das war ein Fehlschluss:
Dieser Modus ist ein **Ladebefehl**, kein Limit — „PV First" heißt nur „PV
zuerst nehmen, den Rest aus dem Netz". Am 11.09.2026 hat eine Anlage damit
von 06:35 bis 09:15 ihre Batterie von 8,5 % auf 22 % geladen, bei einer
PV-Leistung von 0,00 kW: 5,35 kWh aus dem Netz, während der Fahrplan „Laden
begrenzt auf 3,8 kW" anzeigte. Aus „höchstens X" war „genau X, notfalls aus
dem Netz" geworden.

Deshalb bleibt der Eigenverbrauchsmodus stehen und nur das Limit wird
geschrieben. V2.9 bestätigt, dass 40032 dort greift; ältere Firmware (vor
SPC113) tat es laut Anwenderberichten nicht. Greift es nicht, ist die Folge
ein wirkungsloses Limit — die Anlage lädt dann mit Überschuss weiter. Das ist
der richtige Ausgang eines Zweifels: keine Steuerwirkung ist harmlos,
Netzladen kostet Geld.

**Kein geräteseitiges Failsafe.** Die Sigenergy-Modbus-Spezifikation (auch
V2.9) kennt weder Watchdog noch Rückfallzeit — anders als Fronius
(``RvrtTms``), Kostal und SMA. Bleibt Home Assistant mitten in einem Befehl
stehen, läuft er am Gerät weiter. Deshalb ist die Freigabe hier das
AUSSCHALTEN des Remote EMS und nicht nur ein Moduswechsel, und deshalb gibt
die Integration beim Entladen und beim Umschalten auf „Aus" immer aktiv frei.
Am teuersten trifft das die Netzladung: Stürzt Home Assistant währenddessen
ab, lädt das Gerät weiter, bis die Batterie voll ist — aus dem Netz, soweit
die Sonne nicht reicht. Ein stehender Entladedeckel hielte die Batterie
entsprechend fest. Von Hand hilft dann nicht allein, Remote EMS
auszuschalten — die Limits wirken global weiter —, sondern zusätzlich
„ESS Max Charging Limit" und „ESS Max Discharging Limit" auf die
Nennleistung zu stellen. Läuft die Integration wieder, erledigt das ihre
nächste Freigabe von selbst.

**Am Gerät zu verifizieren (Feldtest):**

- Liefert „Command Discharging (ESS First)" ins Netz oder deckt es nur die
  Hauslast?
- Stoppt der Entlade-Cut-Off (Register 40048, ``ESS Discharge Cut-Off State
  of Charge``) eine befohlene Entladung wie der Entladeboden bei SolaX? Der
  Treiber liest ihn als Untergrenze für den Fahrplan, schreibt ihn aber nicht.
- Stehen beide Limits nach dem Ausschalten des Remote EMS wirklich auf der
  geschriebenen Nennleistung (V2.9 sagt global — an der Anlage ablesen)?
- Entladedeckel: Hält 1 kW bzw. 0 (= 0,01 kW) im Eigenverbrauch nachts die
  Entladung, und lädt die PV daneben weiter?
- Netzladung: Wohin geht der Überschuss bei „Command Charging (PV First)"
  mit 2 kW und 5 kW PV — ins Netz, ins Haus oder abgeregelt?

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
from ..const import CONF_BATTERY_SOC_SENSOR
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

# Unterhalb davon ist eine Netzladung keine: wie eine Freigabe behandelt
# (gleiche Schwelle wie beim Victron-Treiber).
NETZLADEN_MIN_KW = 0.05
# Starten erst ab Minimum + Hysterese. Die Leistung, die der Executor aus
# Netzgrenze, Hauslast und PV nachführt, pendelt am Rand gern um ein paar
# Watt — ohne Abstand schaltete jedes Pendeln Remote EMS aus und wieder ein,
# und jedes Schreiben von Schalter oder Modus wartet bis zu 60 s.
NETZLADEN_START_HYSTERESE_KW = 0.05
# Hat eine Netzladung ihr Ziel einmal erreicht, beginnt sie erst unter
# Ziel − 1 % von vorn. Sonst startete der Ladebefehl knapp unter dem Ziel bei
# jeder Leistungsänderung des Executors neu, nur um gleich wieder zu enden.
NETZLADEN_ZIEL_HYSTERESE_PCT = 1.0

# Ein Limit gilt als „auf Nennleistung", solange es höchstens so viel
# darunter liegt. Nur ein Limit UNTER der Nennleistung beschränkt etwas;
# darüber beschneidet das Gerät selbst.
LIMIT_TOLERANZ_KW = 0.1

# Kleinster geschriebener Entladedeckel. 0 lesen Sigenergy-Geräte laut
# Maintainer der Integration oft als „nicht gesetzt" — siehe
# async_set_discharge_limit().
ENTLADEDECKEL_MIN_KW = 0.01

# Limit-Entität -> Sensor mit ihrer Nennleistung. Auf diesen Wert gehen die
# Limits zurück, wenn wir sie nicht mehr brauchen (siehe „Die Limits wirken
# global" im Modulkopf).
NENNWERT_ZU_LIMIT: dict[str, str] = {
    "ess_max_charging_limit": "ess_rated_charging_power",
    "ess_max_discharging_limit": "ess_rated_discharging_power",
}


class SigenFehler(Exception):
    """Ein Schreibschritt ist nicht möglich; der Text wird ``last_write_error``."""


class _ModusNichtAngekommen(SigenFehler):
    """Der Moduswechsel ist gescheitert und steht auch nicht am Gerät."""


def _grund(exc: Exception) -> str:
    """Kurzer Fehlergrund: eigener Text, sonst der Name der Ausnahme."""
    return str(exc) if isinstance(exc, SigenFehler) else type(exc).__name__

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
        # Was wir gerade am Gerät halten. Das Gerät selbst sagt es nicht
        # eindeutig: Der Modus „Maximum Self Consumption" kann ein Ladelimit,
        # ein Entladedeckel, beides oder ein nach außen unsichtbarer Rest
        # sein. Für den Deckel entscheidet das, ob sein Aufheben Remote EMS
        # ausschaltet (nichts sonst steht) oder nur 40034 zurücksetzt.
        self._ladelimit_kw: float | None = None
        self._entladedeckel_kw: float | None = None
        self._befehl: str | None = None  # "entladung" | "netzladung"
        # Ziel-SOC der Netzladung und der Abmelder des SOC-Beobachters —
        # siehe _beobachte_ziel_soc().
        self._ziel_soc: float | None = None
        self._ziel_abmelden: Any = None
        # Executor (30-s-Lauf) und SOC-Beobachter schreiben nebenläufig, und
        # ein Schreiben von Schalter oder Modus kann bis zu 60 s dauern. Ohne
        # Sperre fiele die Rücknahme am Ziel mitten in einen neuen Befehl.
        self._sperre = asyncio.Lock()
        # Limits, deren Rücksetzen auf die Nennleistung als Folgeschritt eines
        # Befehls gescheitert ist — beim nächsten Befehl bzw. der Freigabe
        # nachgeholt (siehe _folgeschritt()).
        self._nennwert_offen: set[str] = set()
        # Ziel, an dem die laufende Netzladung schon einmal angekommen ist —
        # Grundlage der Ziel-Hysterese (NETZLADEN_ZIEL_HYSTERESE_PCT).
        self._ziel_erreicht_pct: float | None = None

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

    async def _switch(self, on: bool) -> None:
        await self._hass.services.async_call(
            "switch", "turn_on" if on else "turn_off",
            {"entity_id": self._resolve_entity("remote_ems_switch")},
            blocking=True,
        )

    def _nennwert(self, limit_key: str) -> tuple[float | None, bool]:
        """Nennleistung zum Limit und ob sie vom Nennleistungs-Sensor stammt.

        Die Nennleistungs-Sensoren können deaktiviert sein — dann bleibt nur
        das ``max`` der Zahl-Entität (laut Integration 100 kW). Ob das Gerät
        einen Wert über seiner Nennleistung selbst beschneidet oder ablehnt,
        ist nicht geprüft; siehe _limit_auf_nennwert().
        """
        nenn = read_power_kw(self._hass, self._resolve_entity(NENNWERT_ZU_LIMIT[limit_key]))
        if nenn is not None and nenn > 0:
            return nenn, True
        state = self._hass.states.get(self._resolve_entity(limit_key))
        try:
            maximum = float(state.attributes.get("max")) if state is not None else None
        except (AttributeError, TypeError, ValueError):
            maximum = None
        return (maximum if maximum is not None and maximum > 0 else None), False

    def _nennwert_kw(self, limit_key: str) -> float | None:
        return self._nennwert(limit_key)[0]

    async def _limit_auf_nennwert(self, limit_key: str) -> None:
        """Ein Limit freigeben: auf die Nennleistung schreiben.

        Unbekannte Nennleistung ist ein Fehler, kein Überspringen — ein
        stehendes Limit wirkt global weiter (Modulkopf), und das soll laut
        scheitern, nicht still.

        Ohne Nennleistungs-Sensor wird trotzdem das ``max`` der Entität
        geschrieben, nicht der stehende Wert gelassen: Der stehende Wert ist
        genau der Fehler, um den es geht (ein Ladelimit 0 sperrt das Laden
        für immer). Lehnt das Gerät das Maximum ab, steht der alte Wert
        ohnehin weiter — dann aber laut, mit einem Grund, der sagt, was zu
        tun ist: den Sensor „ESS Rated …" aktivieren.
        """
        nenn, aus_sensor = self._nennwert(limit_key)
        if nenn is None:
            raise SigenFehler("Nennleistung unbekannt")
        try:
            await self._set_number(limit_key, nenn)
        except Exception as exc:
            if aus_sensor:
                raise
            raise SigenFehler("Maximum abgelehnt, Nennleistungs-Sensor fehlt") from exc
        self._nennwert_offen.discard(limit_key)

    async def _folgeschritt(self, limit_key: str) -> str | None:
        """Das Limit, das ein Befehl nicht braucht, auf die Nennleistung.

        Läuft NACH dem Moduswechsel, der Befehl steht also schon am Gerät.
        Scheitert nur dieser Schritt, ist der Befehl deshalb nicht
        gescheitert: Meldete der Treiber ``False``, hielte der Executor an
        seiner alten Absicht fest (etwa ``release``), Not-Aus und Guard 3
        liefen nicht, und eine spätere Freigabe stiege aus — eine
        Zwangsentladung oder ein Ladebefehl liefe unbeobachtet weiter.
        Deshalb: merken, beim nächsten Befehl oder der Freigabe nachholen,
        den Grund in ``last_write_error`` und ``True``.
        """
        try:
            await self._limit_auf_nennwert(limit_key)
        except Exception as exc:  # noqa: BLE001
            self._nennwert_offen.add(limit_key)
            _LOGGER.warning(
                "Sigenergy: %s nicht auf Nennleistung (%s) — wird nachgeholt",
                limit_key, _grund(exc),
            )
            return f"Limit nicht auf Nennleistung: {_grund(exc)}"
        return None

    async def _offene_nachholen(self, *selbst_geschrieben: str) -> str | None:
        """Gescheiterte Folgeschritte früherer Befehle wiederholen — außer
        für Limits, die der laufende Befehl selbst gerade geschrieben hat."""
        self._nennwert_offen.difference_update(selbst_geschrieben)
        for key in sorted(self._nennwert_offen):
            grund = await self._folgeschritt(key)
            if grund is not None:
                return grund
        return None

    def _teilerfolg(self, grund: str | None) -> bool:
        """Befehl steht; ``grund`` nennt einen gescheiterten Nebenschritt."""
        if grund is None:
            return self._erfolg()
        self.last_write_error = grund
        return True

    def _modus_ist(self) -> str | None:
        return self._state_str(self._resolve_entity("remote_ems_mode"))

    async def _modus_wechseln(self, option: str) -> str | None:
        """Modus schreiben; bei einer Ausnahme nachsehen, was am Gerät steht.

        Seit ``sigen`` 1.3.0 wartet das Schreiben bis zu 60 s auf die
        Rückmeldung des Geräts; eine Zeitüberschreitung heißt nicht, dass der
        Modus nicht angekommen ist. Steht danach der gewünschte Modus, gilt
        der Schritt als gelungen (Rückgabe: der Grund als Hinweis) — der
        Befehl läuft ja, und nur wenn der Executor das weiß, beobachtet er
        ihn. Steht er nicht, ``_ModusNichtAngekommen``.
        """
        try:
            await self._set_mode(option)
            return None
        except Exception as exc:  # noqa: BLE001
            grund = _grund(exc)
            if self._modus_ist() == option:
                _LOGGER.warning(
                    "Sigenergy: Modus „%s“ meldete %s, steht aber am Gerät", option, grund
                )
                return f"Modus mit Fehler gesetzt: {grund}"
            _LOGGER.warning("Sigenergy: Modus „%s“ nicht gesetzt (%s)", option, grund)
            raise _ModusNichtAngekommen(grund) from exc

    async def _zurueckrollen(self) -> None:
        """Nach einem gescheiterten Moduswechsel: kein Befehl ohne Wissen
        des Executors.

        Er bekommt ``False`` und hält an seiner alten Absicht fest. Steht das
        Gerät dann womöglich in einem Befehl (anderer Modus als
        Eigenverbrauch, oder unbekannt — die Rückmeldung kann noch kommen),
        gibt der Treiber frei: Remote EMS aus beendet jeden Befehl, auch einen,
        der verspätet ankommt. Im Eigenverbrauch ist nichts zu tun.

        Scheitert auch die Freigabe — ein Gerät, das den Modus nicht annimmt,
        nimmt ihn meist auch dort nicht —, bleibt als letzter Schritt das
        Ausschalten allein. Die Limits stehen dann noch; die nächste Freigabe
        findet den Schalter aus und setzt sie zurück (_limits_bei_aus()).
        """
        if self._modus_ist() == MODE_SELF_CONSUMPTION:
            return
        if await self._freigeben():
            return
        try:
            await self._switch(False)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Sigenergy: Rücknahme nach gescheitertem Moduswechsel fehlgeschlagen (%s)",
                _grund(exc),
            )
            return
        self._befehl = None
        self._ladelimit_kw = None

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
    # Freigabe und Ziel-SOC der Netzladung
    # ------------------------------------------------------------------
    async def _freigeben(self) -> bool:
        """Eigenverbrauch, beide Limits auf Nennleistung, Remote EMS aus.

        Der Moduswechsel zuerst: schlägt danach etwas fehl, steht das Gerät
        wenigstens im Eigenverbrauch statt in einem Befehl — und erst im
        Eigenverbrauch dürfen die Limits hoch, sonst liefe eine Entladung
        oder ein Ladebefehl einen Moment mit voller Leistung. Dann die
        Limits, weil sie global wirken und nach dem Ausschalten im eigenen
        EMS des Geräts weiter gälten. Scheitert das, bleibt Remote EMS an:
        Aus brächte bei stehenden Limits nichts, und der nächste Versuch des
        Executors findet die Entitäten so vor, wie er sie braucht.

        Ist der Schalter schon aus, gibt es keinen Befehl zurückzunehmen —
        wohl aber Limits: Sie wirken auch bei ausgeschaltetem Remote EMS. Bis
        2.1.62 blieben sie bei der Freigabe stehen, ein Ladelimit 0 von damals
        sperrt das Laden bis heute; und wer nach einem Absturz von Home
        Assistant Remote EMS von Hand ausschaltet, lässt einen Entladedeckel
        von 0,01 kW stehen, die Batterie entlädt nie wieder. Deshalb liest die
        Freigabe beide und setzt zurück, was unter der Nennleistung steht
        (siehe _limits_bei_aus()). Remote EMS wird dafür NICHT eingeschaltet:
        Die Limits sind auch bei aus schreibbar, und ein Einschalten zöge den
        Modus wieder an uns.

        Erst nach einem Erfolg wird vergessen, was stand: Ein gescheitertes
        Aufheben des Deckels muss beim nächsten Mal wieder etwas zu tun finden.
        """
        try:
            schalter = self._state_str(self._resolve_entity("remote_ems_switch"))
            if schalter == "off":
                await self._limits_bei_aus()
            else:
                if self._ist_verfuegbar(self._modus_ist()):
                    await self._set_mode(MODE_SELF_CONSUMPTION)
                await self._limit_auf_nennwert("ess_max_charging_limit")
                await self._limit_auf_nennwert("ess_max_discharging_limit")
                await self._switch(False)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("Sigenergy: Freigabe fehlgeschlagen")
            return self._fehler(_grund(exc))
        self._ladelimit_kw = None
        self._entladedeckel_kw = None
        self._befehl = None
        self._nennwert_offen.clear()
        return self._erfolg()

    def _limit_zu_niedrig(self, limit_key: str) -> bool | None:
        """Steht das Limit unter der Nennleistung? None = nicht lesbar."""
        ist = self._state_float(self._resolve_entity(limit_key))
        nenn = self._nennwert_kw(limit_key)
        if ist is None or nenn is None:
            return None
        return ist < nenn - LIMIT_TOLERANZ_KW

    async def _limits_bei_aus(self) -> None:
        """Remote EMS ist aus: nur zurücksetzen, was unter der Nennleistung steht.

        Gelesen statt blind geschrieben, weil diese Freigabe auch läuft, wenn
        wir am Gerät gar nichts gestellt haben (Modus „Aus", jeder Start) —
        dann soll sie nichts schreiben. Eine nicht verfügbare Entität ist
        nicht schreibbar (Home Assistant überspränge den Aufruf still): kurz
        loggen, die Freigabe gilt trotzdem, ändern ließe sich ohnehin nichts.
        """
        for key in NENNWERT_ZU_LIMIT:
            zu_niedrig = self._limit_zu_niedrig(key)
            if zu_niedrig is None:
                _LOGGER.info(
                    "Sigenergy: %s bei ausgeschaltetem Remote EMS nicht lesbar — "
                    "nicht zurückgesetzt", self._resolve_entity(key),
                )
                continue
            if not zu_niedrig:
                continue
            _LOGGER.warning(
                "Sigenergy: %s steht bei ausgeschaltetem Remote EMS unter der "
                "Nennleistung — es wirkt global, zurück auf die Nennleistung",
                self._resolve_entity(key),
            )
            await self._limit_auf_nennwert(key)

    def _soc(self) -> float | None:
        soc_sensor = self._config.get(CONF_BATTERY_SOC_SENSOR)
        return self._state_float(soc_sensor) if soc_sensor else None

    def _am_ziel(self) -> bool:
        soc = self._soc()
        if self._ziel_soc is None or soc is None:
            return False
        return soc >= self._ziel_soc

    def _beobachte_ziel_soc(self) -> None:
        """Den Ladestand während der Netzladung selbst beobachten.

        „Command Charging" kennt keinen Ziel-SOC — das Gerät lädt, bis die
        Batterie voll ist. Der Executor schreibt nur bei einer Änderung;
        zwischen zwei Befehlen hielte niemand an. Deshalb meldet sich der
        Treiber am SOC-Sensor an und gibt frei, sobald das Ziel erreicht ist
        (Muster des Victron-Treibers).

        ``@callback`` ist Pflicht: Eine schlichte Funktion führt Home
        Assistant im Executor-Thread aus, und ``async_create_task`` aus einem
        fremden Thread weist es ab (siehe leistungsspitze.py).
        """
        if self._ziel_abmelden is not None:
            return
        soc_sensor = self._config.get(CONF_BATTERY_SOC_SENSOR)
        if not soc_sensor:
            return
        try:
            from homeassistant.helpers.event import async_track_state_change_event
        except ImportError:  # pragma: no cover — Testumgebung
            return
        try:
            from homeassistant.core import callback
        except ImportError:  # Testumgebung: nur das Event-Modul ist gestubbt
            def callback(func: Any) -> Any:
                return func

        @callback
        def _soc_geaendert(_event: Any) -> None:
            if self._am_ziel():
                self._hass.async_create_task(self._ziel_erreicht_gesperrt())

        self._ziel_abmelden = async_track_state_change_event(
            self._hass, [soc_sensor], _soc_geaendert
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
        ziel = self._ziel_soc
        if not await self._freigeben():
            # Beobachtung bleibt: Das Gerät lädt weiter, der SOC steigt, und
            # die nächste Änderung versucht es erneut.
            _LOGGER.warning(
                "Sigenergy: Netzladung am Ziel-SOC nicht beendet — wird wiederholt"
            )
            return
        self._beobachtung_beenden()
        self._ziel_erreicht_pct = ziel
        _LOGGER.info("Sigenergy: Ziel-SOC %.0f %% erreicht — Netzladung beendet", ziel or 0.0)

    # ------------------------------------------------------------------
    # InverterBase
    # ------------------------------------------------------------------
    async def async_set_charge_limit(self, power_kw: float) -> bool:
        """Laden auf ``power_kw`` begrenzen; 0 sperrt das Laden.

        Der Modus bleibt „Maximum Self Consumption". Ein Ladebefehl
        („Command Charging") wäre hier falsch: Er lädt auf den Sollwert und
        holt sich die Differenz aus dem Netz, wenn die Sonne nicht reicht —
        siehe „Warum kein Command Charging" im Modulkopf. Ein Limit darf nur
        begrenzen, nie laden.

        Reihenfolge: erst das Limit, dann der Modus — sonst liefe ein
        Moduswechsel einen Moment mit einem alten, höheren Limit. Das
        Entladelimit geht danach auf die Nennleistung: Es wirkt global, ein
        Rest aus einer vorherigen Entladung deckelte sonst den Eigenverbrauch
        (die Hauslast käme aus dem Netz). Erst NACH dem Moduswechsel — davor
        liefe die Entladung einen Moment mit voller Leistung. Steht ein
        Entladedeckel der Spitzenkappung, bleibt er stattdessen stehen: Beide
        gelten nebeneinander.

        Teilfehler: Scheitert das Entladelimit nach dem Moduswechsel, steht
        das Ladelimit trotzdem — ``True`` mit Grund (siehe _folgeschritt()).
        Scheitert der Moduswechsel und das Gerät steht womöglich noch in
        einem Befehl, wird freigegeben (siehe _zurueckrollen()).
        """
        if self._nicht_endlich(power_kw):
            return False
        async with self._sperre:
            self._beobachtung_beenden()
            self._ziel_erreicht_pct = None
            leistung = max(0.0, float(power_kw))
            try:
                await self._ensure_remote_ems()
                await self._set_number("ess_max_charging_limit", leistung)
                hinweis = await self._modus_wechseln(MODE_SELF_CONSUMPTION)
            except _ModusNichtAngekommen as exc:
                await self._zurueckrollen()
                return self._fehler(str(exc))
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("Sigenergy: Ladelimit konnte nicht gesetzt werden")
                return self._fehler(_grund(exc))
            self._befehl = None
            self._ladelimit_kw = leistung
            # 40032 gehört jetzt dem Ladelimit, 40034 schreibt dieser Befehl
            # selbst — nichts Offenes bleibt außerhalb dieses Befehls.
            self._nennwert_offen.discard("ess_max_charging_limit")
            if self._entladedeckel_kw is not None:
                try:
                    await self._set_number("ess_max_discharging_limit", self._entladedeckel_kw)
                    self._nennwert_offen.discard("ess_max_discharging_limit")
                    folge = None
                except Exception as exc:  # noqa: BLE001
                    # Der Deckel bleibt gemerkt; das nächste Ladelimit oder
                    # der nächste Deckel schreibt ihn erneut.
                    _LOGGER.warning("Sigenergy: Entladedeckel neben dem Ladelimit nicht gesetzt (%s)", _grund(exc))
                    folge = f"Entladedeckel nicht gesetzt: {_grund(exc)}"
            else:
                folge = await self._folgeschritt("ess_max_discharging_limit")
            return self._teilerfolg(hinweis or folge)

    async def async_set_discharge(
        self, power_kw: float, target_soc: float | None = None
    ) -> bool:
        """Erzwungene Entladung mit ``power_kw``.

        ``target_soc`` setzt der Executor selbst durch (er stoppt am Ziel);
        das Gerät bekommt keinen SOC — den Cut-Off (40048) schreiben wir
        bewusst nicht, solange sein Verhalten am Gerät nicht geprüft ist.

        Ein Ladelimit aus dem Slot davor (oft 0) geht nach dem Moduswechsel
        auf die Nennleistung — es wirkt global und gehört nicht zur Entladung.
        Ein Entladedeckel ist mit dem ersten Schreiben überschrieben, 40034
        gehört jetzt der Entladung.

        Steht der Entlademodus, ist die Entladung gestartet — auch wenn das
        Ladelimit danach nicht auf die Nennleistung geht oder der Modus erst
        nach einer Zeitüberschreitung ankam. Der Executor muss sie dann
        kennen, sonst laufen Not-Aus und Guard 3 nicht (siehe
        _folgeschritt() und _modus_wechseln()). Steht er nicht, gibt der
        Treiber frei, falls das Gerät womöglich in einem Befehl steht.
        """
        if self._nicht_endlich(power_kw, target_soc):
            return False
        async with self._sperre:
            self._beobachtung_beenden()
            self._ziel_erreicht_pct = None
            try:
                await self._ensure_remote_ems()
                await self._set_number("ess_max_discharging_limit", max(0.0, abs(float(power_kw))))
                self._entladedeckel_kw = None
                hinweis = await self._modus_wechseln(MODE_COMMAND_DISCHARGING_ESS_FIRST)
            except _ModusNichtAngekommen as exc:
                await self._zurueckrollen()
                return self._fehler(str(exc))
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("Sigenergy: Entladung konnte nicht gestartet werden")
                return self._fehler(_grund(exc))
            self._befehl = "entladung"
            self._ladelimit_kw = None
            self._nennwert_offen.discard("ess_max_discharging_limit")
            folge = await self._folgeschritt("ess_max_charging_limit")
            return self._teilerfolg(hinweis or folge)

    async def async_stop_forcible(self) -> bool:
        """Freigeben (siehe ``_freigeben``); eine Netzladung wird nicht mehr beobachtet."""
        async with self._sperre:
            self._beobachtung_beenden()
            self._ziel_erreicht_pct = None
            return await self._freigeben()

    @property
    def is_available(self) -> bool:
        entries = self._hass.config_entries.async_entries(SIGEN_DOMAIN)
        return any(entry.state.value == "loaded" for entry in entries)

    # ------------------------------------------------------------------
    # Netzladung (Beta)
    # ------------------------------------------------------------------
    @property
    def supports_grid_charge(self) -> bool:
        """„Batterie aus dem Netz laden" (Beta) — „Command Charging (PV First)"."""
        return True

    async def async_set_grid_charge(
        self, power_kw: float, target_soc: float | None = None
    ) -> bool:
        """Batterie mit ``power_kw`` laden, PV zuerst, den Rest aus dem Netz.

        Genau das, wovor der Modulkopf beim Ladelimit warnt, ist hier
        gewollt: „Command Charging (PV First)" mit ESS Max Charging Limit =
        Leistung hat am 11.09.2026 an der Testanlage aus dem Netz geladen.
        Erst das Limit, dann der Modus — umgekehrt liefe der Ladebefehl einen
        Moment mit einem alten, höheren Limit aus dem Netz.

        Das Gerät hört am Ziel nicht von selbst auf. Den Ladestand beobachtet
        der Treiber deshalb selbst (ohne Ziel 100 %) und gibt am Ziel frei.
        Steht er schon dort, wird nichts geschrieben; eine laufende
        Netzladung von uns wird beendet.

        **Risiko: kein Watchdog.** Stürzt Home Assistant während einer
        Netzladung ab, lädt das Gerät weiter, bis die Batterie voll ist — aus
        dem Netz, wenn die Sonne nicht reicht. Abhilfe dann von Hand: Remote
        EMS ausschalten und beide Limits auf die Nennleistung stellen.

        Hysteresen: Eine neue Netzladung beginnt erst ab Minimum + 0,05 kW,
        eine laufende endet unter dem Minimum; nach erreichtem Ziel beginnt
        sie erst unter Ziel − 1 % von vorn. Der Modus wird nur geschrieben,
        wenn er nicht schon steht — eine Leistungsänderung schreibt nur 40032.
        """
        if self._nicht_endlich(power_kw, target_soc):
            return False
        async with self._sperre:
            return await self._netzladung(power_kw, target_soc)

    async def _netzladung(self, power_kw: float, target_soc: float | None) -> bool:
        ziel = 100.0 if target_soc is None else min(100.0, float(target_soc))
        leistung = max(0.0, float(power_kw))
        laeuft = self._befehl == "netzladung"
        # Eine laufende Netzladung endet erst unter dem Minimum, eine neue
        # beginnt erst darüber plus Hysterese — siehe
        # NETZLADEN_START_HYSTERESE_KW.
        schwelle = NETZLADEN_MIN_KW if laeuft else NETZLADEN_MIN_KW + NETZLADEN_START_HYSTERESE_KW
        if leistung < schwelle:
            # Nichts aus dem Netz (die Netzgrenze lässt gerade nichts zu):
            # wie eine Freigabe. Ein Ladebefehl mit Limit 0 sperrte sonst auch
            # das Laden aus PV.
            self._beobachtung_beenden()
            return await self._freigeben()
        if self._ziel_erreicht_pct is not None and self._ziel_erreicht_pct != ziel:
            self._ziel_erreicht_pct = None  # neues Ziel, neue Netzladung
        self._ziel_soc = ziel
        soc = self._soc()
        knapp_darunter = (
            self._ziel_erreicht_pct is not None
            and soc is not None
            and soc >= ziel - NETZLADEN_ZIEL_HYSTERESE_PCT
        )
        if self._am_ziel() or knapp_darunter:
            self._beobachtung_beenden()
            self._ziel_erreicht_pct = ziel
            if laeuft:
                return await self._freigeben()
            return self._erfolg()
        # Vor dem Schreiben anmelden: Ein Moduswechsel kann in der Integration
        # bis zu 60 s auf die Rückmeldung warten und trotzdem am Gerät
        # angekommen sein. Auch nach einem Fehlschlag soll dann jemand am
        # Ziel anhalten.
        self._beobachte_ziel_soc()
        try:
            await self._ensure_remote_ems()
            await self._set_number("ess_max_charging_limit", leistung)
            # Den Modus nur schreiben, wenn er nicht schon steht: Der Executor
            # führt die Leistung jeden Lauf der Netzgrenze nach, und jedes
            # Schreiben des Modus wartet bis zu 60 s (wie beim Deckel).
            hinweis = None
            if self._modus_ist() != MODE_COMMAND_CHARGING_PV_FIRST:
                hinweis = await self._modus_wechseln(MODE_COMMAND_CHARGING_PV_FIRST)
        except _ModusNichtAngekommen as exc:
            await self._zurueckrollen()
            return self._fehler(str(exc))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("Sigenergy: Netzladung konnte nicht gestartet werden")
            return self._fehler(_grund(exc))
        self._befehl = "netzladung"
        self._ladelimit_kw = None
        self._entladedeckel_kw = None
        nachgeholt = await self._offene_nachholen("ess_max_charging_limit")
        return self._teilerfolg(hinweis or nachgeholt)

    # ------------------------------------------------------------------
    # Entladedeckel der Spitzenkappung
    # ------------------------------------------------------------------
    @property
    def supports_discharge_limit(self) -> bool:
        """Über ESS Max Discharging Limit (40034) im Eigenverbrauch."""
        return True

    async def async_set_discharge_limit(self, power_kw: float | None) -> bool:
        """Entladeleistung im Eigenverbrauch auf ``power_kw`` deckeln.

        40034 wirkt laut V2.9 in jedem Modus, also auch in „Maximum Self
        Consumption" — der Deckel ist dort das Entladelimit, neben einem
        laufenden Ladelimit (40032) und ohne es anzufassen. **Am Gerät nicht
        geprüft**, siehe docs/wechselrichter-status.md.

        0 wird als 0,01 kW geschrieben: Laut Maintainer der Integration lesen
        Sigenergy-Geräte eine 0 oft als „nicht gesetzt" und entladen dann
        frei — genau das Gegenteil von „halten". 10 W sind praktisch Halten.

        ``None`` hebt den Deckel auf: 40034 zurück auf die Nennleistung; läuft
        weder ein Ladelimit noch ein Befehl, gleich die ganze Freigabe
        (Remote EMS aus) — der Deckel war das Einzige, wofür es an war. Ohne
        gemerkten Deckel entscheidet der gelesene Wert von 40034 (siehe
        _deckel_aufheben()). Während einer Entladung
        oder Netzladung gehört 40034 dem Befehl; dann wird der Wert nur
        gemerkt (der Executor hebt den Deckel vor jedem Befehl ohnehin auf).
        """
        if power_kw is not None and self._nicht_endlich(power_kw):
            return False
        async with self._sperre:
            if power_kw is None:
                return await self._deckel_aufheben()
            deckel = max(float(power_kw), ENTLADEDECKEL_MIN_KW)
            if self._befehl is not None:
                self._entladedeckel_kw = deckel
                return self._erfolg()
            # Vor dem Schreiben merken: Scheitert es halb (Limit steht, Modus
            # nicht), muss ein späteres Aufheben noch etwas zu tun finden.
            self._entladedeckel_kw = deckel
            try:
                await self._ensure_remote_ems()
                await self._set_number("ess_max_discharging_limit", deckel)
                self._nennwert_offen.discard("ess_max_discharging_limit")
                # Den Modus nur schreiben, wenn er nicht schon steht: Der
                # Deckel folgt dem Viertelstundenbudget und ändert sich oft,
                # und jedes Schreiben des Modus wartet bis zu 60 s.
                hinweis = None
                if self._modus_ist() != MODE_SELF_CONSUMPTION:
                    hinweis = await self._modus_wechseln(MODE_SELF_CONSUMPTION)
                return self._teilerfolg(hinweis)
            except _ModusNichtAngekommen as exc:
                await self._zurueckrollen()
                return self._fehler(str(exc))
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("Sigenergy: Entladedeckel konnte nicht gesetzt werden")
                return self._fehler(_grund(exc))

    async def _deckel_aufheben(self) -> bool:
        """Den Deckel aufheben — auch einen, von dem der Treiber nichts weiß.

        Ohne Gedächtnis (nach einem Neustart von Home Assistant) entscheidet
        der gelesene Zustand: Steht 40034 unter der Nennleistung, kann es ein
        Deckel aus der Zeit vor dem Neustart sein, und der wirkt global — auch
        wenn jemand Remote EMS inzwischen von Hand ausgeschaltet hat. Früher
        meldete ``None`` hier ungeprüft Erfolg, und die Batterie blieb
        gedeckelt. Nicht lesbar heißt: nichts zu sehen, nichts zu tun.
        """
        if self._befehl is not None:
            # 40034 gehört dem laufenden Befehl, auch ohne gemerkten Deckel.
            self._entladedeckel_kw = None
            return self._erfolg()
        if self._entladedeckel_kw is None and not self._limit_zu_niedrig("ess_max_discharging_limit"):
            return self._erfolg()
        if self._ladelimit_kw is None:
            return await self._freigeben()
        try:
            await self._limit_auf_nennwert("ess_max_discharging_limit")
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("Sigenergy: Entladedeckel konnte nicht aufgehoben werden")
            return self._fehler(_grund(exc))
        self._entladedeckel_kw = None
        return self._erfolg()

    # ------------------------------------------------------------------
    # Fahrplan-Steuerschnittstelle (Schedule-Executor)
    # ------------------------------------------------------------------
    @property
    def supports_schedule_control(self) -> bool:
        return True

    async def async_get_charge_limit_kw(self) -> float | None:
        """Gesetztes Ladelimit — im Eigenverbrauchsmodus eine Aussage.

        In den Befehlsmodi gehört Register 40032 dem Befehl (bei der
        Entladung steht es auf der Nennleistung), sein Wert wäre dann kein
        Ladelimit des Fahrplans. None lässt den Executor auf den zuletzt
        geschriebenen Wert bzw. den Planwert zurückfallen.
        """
        if self._state_str(self._resolve_entity("remote_ems_mode")) != MODE_SELF_CONSUMPTION:
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
