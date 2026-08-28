"""Zentrale Helfer für das Lesen + Normalisieren von Power-Sensoren (kW).

Eine Quelle der Wahrheit für drei Aufrufer:
  - sensor.PVLeistungSensor / HausverbrauchSensor / etc. (HA-Dashboard)
  - schedule.async_collect_inputs (Messwerte für den ersten Stützpunkt)
  - schedule_executor.ScheduleExecutor (Guard 1/2, Not-Aus)

Damit sehen Dashboard, Fahrplan und Steuerung denselben Wert — kein Drift
durch divergente Lokalkopien.
"""
from __future__ import annotations

from typing import Any

from .const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_CAPACITY_SENSOR,
    CONF_BATTERY_POWER_CHARGE_SENSOR,
    CONF_BATTERY_POWER_DISCHARGE_SENSOR,
    CONF_BATTERY_POWER_SENSOR,
    CONF_BATTERY_POWER_SENSOR_2,
    CONF_GRID_POWER_EXPORT_SENSOR,
    CONF_GRID_POWER_IMPORT_SENSOR,
    CONF_GRID_POWER_SENSOR,
    CONF_INVERTER_TYPE,
    CONF_PV_POWER_SENSOR,
    CONF_PV_POWER_SENSOR_2,
    EMMA_SENSOR_PREFIX,
    INVERTER_SIGN_CONVENTIONS,
    INVERTER_TYPE_HUAWEI,
)


def resolve_battery_capacity_kwh(hass: Any, config: dict) -> float | None:
    """Nutzbare Batteriekapazität in kWh — Sensor zuerst, dann der Fixwert.

    Reihenfolge: Kapazitäts-Sensor (mit Wh→kWh-Normalisierung) → manuell
    eingetragener Wert → None. Der Sensor gewinnt, weil der Fixwert beim
    Setup oft nur eingetragen wurde, solange der Sensor noch ``unknown``
    war (Huawei meldet die Kapazität erst nach dem ersten Poll) — er bleibt
    danach als veraltete Zahl stehen. Genau daran hat der Fahrplan mit
    10 kWh gerechnet, während die Anlage 15 kWh hatte.
    """
    cap_id = config.get(CONF_BATTERY_CAPACITY_SENSOR, "")
    if cap_id and hass is not None and hasattr(hass, "states"):
        state = hass.states.get(cap_id)
        if state is not None and state.state not in ("unknown", "unavailable", "", None):
            try:
                raw = float(state.state)
            except (ValueError, TypeError):
                raw = None
            if raw is not None:
                unit = ""
                if hasattr(state, "attributes"):
                    unit = state.attributes.get("unit_of_measurement", "") or ""
                # Ohne Einheit entscheidet die Größenordnung: eine Hausbatterie
                # mit über 1000 kWh gibt es nicht, 15000 sind also Wh.
                if unit.lower() in ("wh", "w·h") or (not unit and raw > 1000):
                    return raw / 1000.0
                return raw
    manual = config.get(CONF_BATTERY_CAPACITY_KWH)
    try:
        return float(manual) if manual is not None else None
    except (ValueError, TypeError):
        return None


def resolve_sign(inv_type: str, entity_id: str | None, kind: str) -> int:
    """Effektives Vorzeichen (+1/-1) für einen grid-/battery-Leistungssensor.

    Einzige Anwendungsstelle der Vorzeichen-Konvention — alle Aufrufer
    (Sensoren, Optimizer-Snapshot, Feed-in-Statistik) leiten ihr Vorzeichen
    hierüber ab.

    Basis ist ``INVERTER_SIGN_CONVENTIONS[inv_type][kind]`` (pro Inverter-Typ).
    Sonderfall Huawei-EMMA: Die Einspeiseleistung des EMMA-Energiemanagements
    (entity_id-Präfix ``sensor.emma…``) liefert das Netz-Vorzeichen umgekehrt
    gegenüber der SUN2000-Konvention — NUR für ``grid_sign`` wird das
    Basis-Vorzeichen invertiert. Die EMMA-Batterieleistung folgt der normalen
    SUN2000-Konvention und bleibt unverändert.

    Args:
        inv_type: Konfigurierter Inverter-Typ (CONF_INVERTER_TYPE).
        entity_id: entity_id des konkreten Sensors (kann None/leer sein).
        kind: ``"grid_sign"`` oder ``"battery_sign"``.
    """
    base = INVERTER_SIGN_CONVENTIONS.get(inv_type, {}).get(kind, 1)
    if (
        kind == "grid_sign"
        and inv_type == INVERTER_TYPE_HUAWEI
        and entity_id
        and entity_id.lower().startswith(EMMA_SENSOR_PREFIX)
    ):
        return -base
    return base


def resolve_backfill_signs(config: dict) -> tuple[int, int]:
    """Vorzeichen ``(battery_sign, grid_sign)`` für den Statistik-Backfill.

    Identische Vorzeichen-Logik wie die Live-Pfade (``resolve_sign`` inkl.
    Huawei-EMMA-Erkennung), erweitert um Paar-Konfigurationen: Ein Lade-/
    Entlade- bzw. Export-/Import-Paar wird im Backfill per Konstruktion
    kanonisch kombiniert (``pos − neg``) — dort gilt das Basis-Vorzeichen
    des Inverter-Typs (Identität für Fronius; EMMA liefert keine Paare).

    Historie: Der Backfill nutzte INVERTER_SIGN_CONVENTIONS direkt und
    übersprang damit die EMMA-Inversion — bei EMMA-Anlagen überschrieb er
    so bei jedem HA-Start die Hausverbrauch-Statistik mit falsch (invertiert)
    berechneten Werten. Dieser Helper hält Backfill und Live-Sensoren
    zwangsweise konsistent.
    """
    inv_type = config.get(CONF_INVERTER_TYPE, "")
    signs = INVERTER_SIGN_CONVENTIONS.get(inv_type, {})

    has_battery_pair = bool(
        config.get(CONF_BATTERY_POWER_CHARGE_SENSOR)
        and config.get(CONF_BATTERY_POWER_DISCHARGE_SENSOR)
    )
    has_grid_pair = bool(
        config.get(CONF_GRID_POWER_EXPORT_SENSOR)
        and config.get(CONF_GRID_POWER_IMPORT_SENSOR)
    )

    battery_sign = (
        signs.get("battery_sign", 1)
        if has_battery_pair
        else resolve_sign(
            inv_type, config.get(CONF_BATTERY_POWER_SENSOR, ""), "battery_sign"
        )
    )
    grid_sign = (
        signs.get("grid_sign", 1)
        if has_grid_pair
        else resolve_sign(
            inv_type, config.get(CONF_GRID_POWER_SENSOR, ""), "grid_sign"
        )
    )
    return battery_sign, grid_sign


# Bekannte Einheiten-Aliase, alle in der KEY in lowercase. Deckt die in HA-
# Sensoren beobachteten Schreibweisen ab — bewusst defensiv, weil HA-Custom-
# Integrationen selten den `homeassistant.const.UnitOfPower`-Constraint nutzen.
_UNIT_FACTORS_TO_KW: dict[str, float] = {
    # → kW
    "kw": 1.0,
    "kilowatt": 1.0,
    "kilowatts": 1.0,
    # → W
    "w": 0.001,
    "watt": 0.001,
    "watts": 0.001,
    # → MW (selten in PV/Hausanlagen, aber gerne in Industriesensoren)
    "mw": 1000.0,
    "megawatt": 1000.0,
    "megawatts": 1000.0,
}


def read_power_kw(hass: Any, entity_id: str) -> float | None:
    """Liest einen Power-Sensor und normalisiert auf kW.

    Returns None für nicht konfigurierte / nicht verfügbare Sensoren —
    NICHT 0.0, weil das Backend zwischen "0 W" und "konnte nicht gelesen
    werden" unterscheidet.

    Einheiten-Erkennung ist case-insensitive und akzeptiert die gängigen
    Aliase (W/Watt/Watts, kW/kilowatt, MW/Megawatt). Eine fehlende oder
    unbekannte Einheit wird konservativ als kW interpretiert (Default-
    Verhalten der HA-Integration vor diesem Refactoring).
    """
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    raw_state = state.state
    if raw_state in (None, "unknown", "unavailable", ""):
        return None
    try:
        val = float(raw_state)
    except (ValueError, TypeError):
        return None

    attrs = getattr(state, "attributes", None) or {}
    unit_raw = attrs.get("unit_of_measurement") if hasattr(attrs, "get") else None
    unit = (unit_raw or "").strip().lower()

    factor = _UNIT_FACTORS_TO_KW.get(unit)
    if factor is None:
        # Unbekannt oder leer → Sensor wird so behandelt, als sei er bereits
        # in kW. Das ist die historische Default-Annahme der Integration.
        return val
    return val * factor


def compute_pv_now_kw(hass: Any, config: dict) -> float | None:
    """Live-PV-Leistung in kW — identisch zu sensor.PVLeistungSensor.

    Wendet dieselben drei Korrekturen an, die das HA-Integration-Dashboard
    verwendet:
      1. Optionalen zweiten PV-Sensor summieren (Multi-Inverter-Setups,
         z.B. SolaX-Generator über Meter 2 oder zweiter SolarEdge-Inverter).
      2. ``pv_includes_battery``-Korrektur: bei SolarEdge enthält
         ``ac_power`` bereits die Batterie-Entladung. Echte PV =
         ac_power + battery_raw  (Entladung ist negativ → wird subtrahiert,
         Ladung ist positiv → wird zur PV addiert, da der Inverter die
         Batterie aus PV speist).
      3. Clipping auf ``>= 0`` — kleine negative Werte aus
         Wandlungsverlusten / Inverter-Eigenverbrauch werden zu 0, statt
         als Phantom-Negativ-Erzeugung ans Backend zu gehen.

    Liefert ``None`` nur dann, wenn weder primärer noch sekundärer
    PV-Sensor lesbar ist — andernfalls wird die jeweilige fehlende Quelle
    als 0 behandelt (Konsistenz mit ``PVLeistungSensor.async_update``).
    """
    inv_type = config.get(CONF_INVERTER_TYPE, "")
    signs = INVERTER_SIGN_CONVENTIONS.get(inv_type, {})
    pv_includes_battery = signs.get("pv_includes_battery", False)

    pv_id = config.get(CONF_PV_POWER_SENSOR, "")
    pv_2_id = config.get(CONF_PV_POWER_SENSOR_2, "")

    pv_raw = read_power_kw(hass, pv_id) if pv_id else None
    pv_2_raw = read_power_kw(hass, pv_2_id) if pv_2_id else None

    # Beide Quellen unverfügbar → kein Wert (Backend bekommt None, nicht 0)
    if pv_raw is None and pv_2_raw is None:
        return None

    pv_combined = (pv_raw or 0.0) + (pv_2_raw or 0.0)

    if pv_includes_battery:
        bat_id = config.get(CONF_BATTERY_POWER_SENSOR, "")
        if bat_id:
            bat_raw = read_power_kw(hass, bat_id)
            if bat_raw is not None:
                pv_combined += bat_raw
        # Multi-Inverter SolarEdge: zweiter PV-Sensor implizierte zweite Batterie
        # (gleiche Heuristik wie PVLeistungSensor.__init__: ac_power → b1_dc_power).
        if pv_2_id and "ac_power" in pv_2_id:
            bat_2_id = pv_2_id.replace("ac_power", "b1_dc_power")
            bat_2_raw = read_power_kw(hass, bat_2_id)
            if bat_2_raw is not None:
                pv_combined += bat_2_raw

    return max(pv_combined, 0.0)


def compute_grid_export_kw(hass: Any, config: dict) -> float | None:
    """Live-Netzleistung in kW — identisch zu sensor.NetzleistungSensor.

    Positiv = Einspeisung, negativ = Bezug (Vorzeichen über ``resolve_sign``
    inkl. Huawei-EMMA-Sonderfall). Liefert ``None``, wenn der Netz-Sensor
    nicht lesbar ist. Guard 1 prüft damit, ob die Einspeisung am Limit
    klebt; der Not-Aus erkennt anhaltenden Netzbezug während einer Entladung.
    """
    grid_id = config.get(CONF_GRID_POWER_SENSOR, "")
    grid = read_power_kw(hass, grid_id)
    if grid is None:
        return None
    return grid * resolve_sign(config.get(CONF_INVERTER_TYPE, ""), grid_id, "grid_sign")


def compute_battery_now_kw(hass: Any, config: dict) -> float | None:
    """Live-Batterieleistung in kW — positiv = laden, negativ = entladen.

    Gleiche Vorzeichen- und Mehrgeräte-Behandlung wie in
    ``compute_house_load_kw``: roher Wert plus optionale zweite Batterie
    (Huawei Master/Slave), danach ``resolve_sign``. Liefert ``None``, wenn der
    Batterie-Sensor nicht lesbar ist.

    Bewusst eine eigene Funktion und kein Aufruf aus ``compute_house_load_kw``:
    dort ist der *rohe* Batteriewert zwischen zwei Schritten eingeklemmt — er
    rekonstruiert bei SolarEdge zuerst die echte PV-Leistung
    (``pv_includes_battery``) und wird erst danach um die zweite Batterie
    ergänzt und mit dem Vorzeichen multipliziert. Diese Verschränkung
    aufzulösen hieße, den Rechenweg der Steuerung anzufassen; die sechs Zeilen
    doppelt zu halten ist das kleinere Übel. Wer eines ändert, ändert beides.
    """
    inv_type = config.get(CONF_INVERTER_TYPE, "")
    bat_id = config.get(CONF_BATTERY_POWER_SENSOR, "")
    bat_2_id = config.get(CONF_BATTERY_POWER_SENSOR_2, "")

    battery_power = read_power_kw(hass, bat_id)
    if battery_power is None:
        return None

    if bat_2_id:
        bat2 = read_power_kw(hass, bat_2_id)
        if bat2 is not None:
            battery_power += bat2

    return battery_power * resolve_sign(inv_type, bat_id, "battery_sign")


def compute_house_load_kw(hass: Any, config: dict) -> float | None:
    """Live-Hauslast in kW — identisch zu sensor.HausverbrauchSensor.

    Formel: Hausverbrauch = PV − Batterie − Netz, Vorzeichen über
    ``resolve_sign`` normalisiert (Batterie positiv = laden, Netz positiv =
    Einspeisung), Ergebnis auf ≥ 0 begrenzt.

    Direkt lesbar im 30-Sekunden-Takt — der Fahrplan braucht die Hauslast als
    Messwert für den ersten Stützpunkt und der Executor für die Nachführung
    der Entladeleistung; der HA-Sensor aktualisiert dafür zu langsam.

    Liefert ``None``, wenn Batterie- oder Netz-Sensor nicht lesbar sind
    (gleiche Semantik wie der Sensor: ohne diese beiden ist die Bilanz nicht
    rechenbar). Ein nicht lesbarer PV-Sensor zählt als 0 kW — der Inverter
    ist nachts schlicht offline.
    """
    inv_type = config.get(CONF_INVERTER_TYPE, "")
    signs = INVERTER_SIGN_CONVENTIONS.get(inv_type, {})

    pv_id = config.get(CONF_PV_POWER_SENSOR, "")
    pv_2_id = config.get(CONF_PV_POWER_SENSOR_2, "")
    bat_id = config.get(CONF_BATTERY_POWER_SENSOR, "")
    bat_2_id = config.get(CONF_BATTERY_POWER_SENSOR_2, "")
    grid_id = config.get(CONF_GRID_POWER_SENSOR, "")

    pv_power = read_power_kw(hass, pv_id)
    battery_power = read_power_kw(hass, bat_id)
    grid_power = read_power_kw(hass, grid_id)

    # PV-Sensor nachts nicht verfügbar (Inverter offline) → PV = 0 kW
    if pv_power is None:
        pv_power = 0.0
    if battery_power is None or grid_power is None:
        return None

    # Optionaler zweiter PV-Sensor (z. B. SolaX-Generator über Meter 2)
    if pv_2_id:
        pv2 = read_power_kw(hass, pv_2_id)
        if pv2 is not None:
            pv_power += pv2

    # SolarEdge: ac_power enthält die Batterie-Entladung → echte PV rekonstruieren
    if signs.get("pv_includes_battery", False):
        pv_power += battery_power

    # Zweite Batterie (Huawei Master/Slave): roher, vorzeichenbehafteter Wert —
    # nach der SolarEdge-Korrektur addiert, exakt wie im HausverbrauchSensor.
    if bat_2_id:
        bat2 = read_power_kw(hass, bat_2_id)
        if bat2 is not None:
            battery_power += bat2

    battery_power *= resolve_sign(inv_type, bat_id, "battery_sign")
    grid_power *= resolve_sign(inv_type, grid_id, "grid_sign")

    return max(pv_power - battery_power - grid_power, 0.0)
