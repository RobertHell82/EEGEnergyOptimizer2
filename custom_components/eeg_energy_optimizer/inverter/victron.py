"""Victron ESS (Multiplus/Quattro + GX-Gerät) über die HA-Integration ``victron_gx``.

Die Integration ist seit Home Assistant 2026.5 eingebaut (Domain
``victron_gx``, Bibliothek ``victron-mqtt``) und spricht MQTT mit dem
GX-Gerät (Cerbo GX, Venus GX, Ekrano GX). Sie bietet die ESS-Stellgrößen als
Standardentitäten an, geschrieben wird per ``number.set_value`` — dasselbe
Muster wie bei SolaX und Sigenergy, kein eigenes Modbus, kein Keepalive.

Die drei Absichten des Fahrplan-Executors werden so abgebildet:

  Ladelimit X kW  → DVCC „Max. Ladestrom" = X / Batteriespannung
                    (``Settings/SystemSetup/MaxChargeCurrent``)
  Entladung X kW  → ESS-Netzsollwert = −X (``Settings/CGwacs/AcPowerSetPoint``)
  Freigabe        → beide Werte zurück auf den Stand vor unserem Eingriff

**Netzsollwert statt Batterieleistung.** Der ESS-Regelkreis im GX-Gerät
regelt den Netzanschlusspunkt auf den Sollwert und legt die Hauslast selbst
obendrauf — wie SMA (``GridWSpt``). Deshalb ``discharge_is_grid_setpoint =
True``: der Executor übergibt die geplante EINSPEISUNG, nicht die
Batterieleistung. Negativ heißt Einspeisung.

**Ladelimit über DVCC, nicht über „ESS max charge power".** Die ESS-Grenze
``Settings/CGwacs/MaxChargePower`` begrenzt nur das Laden durch den
Multiplus (AC → DC). Hängt die PV an einem MPPT-Laderegler (DC-gekoppelt),
lädt sie an dieser Grenze vorbei. Der DVCC-Ladestrom gilt für das ganze
System, Multiplus und Laderegler zusammen. Damit ein begrenztes Laden bei
DC-gekoppelter PV den Überschuss ins Netz schickt statt ihn abzuregeln, muss
am GX „DC-gekoppelte PV — Überschuss einspeisen" eingeschaltet sein; der
Einrichtungsassistent prüft das (``victron_pruefung``).

**Vorwerte.** Beide Stellgrößen sind gespeicherte Einstellungen des
GX-Geräts, die der Betreiber selbst gesetzt hat (Netzsollwert oft 0 oder
+50 W, Ladestrom oft eine Schutzgrenze für Batterie oder Verkabelung). Der
Wert vor dem ERSTEN Eingriff wird deshalb im ``VictronStateStore``
gesichert und bei der Freigabe zurückgeschrieben — überlebt einen Neustart
mitten im Eingriff, wie beim SolaX-Treiber.

**Kein geräteseitiges Failsafe.** Die Einstellungen haben keinen Watchdog.
Bleibt Home Assistant mitten in einer Entladung stehen, regelt das GX-Gerät
weiter auf den negativen Sollwert, bis zum ESS-Mindest-SOC. Der
ESS-Modus 3 („External control", ``Hub4/L1/AcPowerSetpoint`` mit
60-s-Watchdog) hätte einen, schaltet aber die ganze ESS-Regelung auf
Fremdsteuerung um — dann müsste der Optimizer auch den Eigenverbrauch
regeln. Das ist bewusst nicht gewählt. Zwei Netze darunter:

- Der Treiber beobachtet den Ladestand während einer Entladung und nimmt
  den Sollwert selbst zurück, sobald der Ziel-SOC erreicht ist — das GX-Gerät
  kennt keinen Ziel-SOC für einen Netzsollwert.
- Nach jedem Start gibt der Executor zuerst frei (``START_RELEASE``).

**Wertebereich des Ladestroms.** Ältere Stände der Integration liefern für
``MaxChargeCurrent`` keinen Wertebereich, Home Assistant setzt dann 0–100 A
und −1 („Ladestrom nicht begrenzen") ist nicht schreibbar. Stand der
Ladestrom vor unserem Eingriff auf −1, schreibt die Freigabe dann das
Maximum der Entität zurück — die Einstellung des Betreibers wäre still
geändert. Der Assistent lässt diesen Fall deshalb nicht durch. Mit HA 2026.10
reicht der Bereich von −1 bis 10000 A (Anlage gelesen am 29.10.2026), dann
wird −1 unverändert zurückgeschrieben.

**Gelesen an einer echten Anlage** (29.10.2026, drei Multiplus-II 48/5000
dreiphasig, ESS „Optimiert mit BatteryLife", Phasenausgleich an, drei
MPPT-Laderegler, Pytes-Batterie, Netzzähler ET340): Batterie entlädt
7,26 kW → ``Dc/Battery/Power`` −7262 W, Netzzähler gesamt +115 W bei
L1 +1486 / L2 −783 / L3 −589 W. Die Vorzeichen oben stimmen damit. Der
Rückfall auf ``system_grid_power_l1`` für den Netzsensor ist deshalb
gestrichen: Auf einer dreiphasigen Anlage hätte er 1,4 kW Bezug gemeldet,
wo der Zähler fast null misst.

**Am Gerät zu verifizieren (Feldtest):**

- Begrenzt DVCC-Ladestrom 0 A das Laden vollständig, und speisen die
  Laderegler den Überschuss dann ein?
- Folgt der Multiplus dem negativen Netzsollwert mit voller Leistung, und
  greift die ESS-Grenze „Max. Einspeiseleistung" dabei wie erwartet?
- Wie schnell kommen Messwerte über MQTT an (Integration: „auto", je Metrik)?

Vorzeichen (Venus-OS-D-Bus, von der Bibliothek roh durchgereicht):
``Dc/Battery/Power`` positiv = Laden — wie bei uns; Netzleistung
(``grid/…/Ac/Power``, ``Ac/Grid/L1/Power``) positiv = BEZUG — gegen unsere
Konvention, daher ``grid_sign = -1`` in ``INVERTER_SIGN_CONVENTIONS``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from .base import InverterBase
from ..const import CONF_BATTERY_SOC_SENSOR

try:
    from homeassistant.helpers.storage import Store
except ImportError:  # pragma: no cover — Testumgebung
    Store = None  # type: ignore

_LOGGER = logging.getLogger(__name__)

VICTRON_DOMAIN = "victron_gx"

# Metrik-Kennungen (``short_id``) der Bibliothek victron-mqtt. Die
# Integration baut daraus die unique_id ``<installation>_<gerätetyp>_<id>_<short_id>``
# und den Übersetzungsschlüssel des Namens. Die Entity-IDs dagegen hängen am
# Gerätenamen (Modell, Sprache, Umbenennung) — deshalb wird über die
# unique_id in der Entity-Registry aufgelöst, nie über Namensmuster.
# Venus OS kennt fünf Zeitfenster für „Geplantes Laden" (ESS).
GEPLANTES_LADEN_FENSTER = 5

VICTRON_METRIKEN: dict[str, tuple[str, str]] = {
    # Stellgrößen
    "netzsollwert": ("number", "system_ac_power_set_point"),
    "ladestrom": ("number", "system_ess_max_charge_current"),
    # Lesewerte für den Fahrplan
    "min_soc": ("number", "system_ess_min_soc_limit"),
    "aktive_soc_grenze": ("sensor", "system_control_active_soc_limit"),
    "batteriespannung": ("sensor", "system_dc_battery_voltage"),
    "bms_max_ladestrom": ("sensor", "battery_max_charge_current"),
    # Nur für die Prüfung im Einrichtungsassistenten
    "ess_modus": ("select", "system_ess_batterylife_state"),
    "dess_modus": ("select", "system_settings_dess_mode"),
    "dvcc": ("switch", "system_dvcc"),
    "dc_ueberschuss_einspeisen": ("switch", "system_settings_overvoltage_feedin"),
    "max_einspeiseleistung": ("number", "system_ess_max_feed_in_power"),
    **{
        f"geplantes_laden_{n}": ("switch", f"system_ess_schedule_charge_{n}_enabled")
        for n in range(GEPLANTES_LADEN_FENSTER)
    },
    "dc_pv_leistung": ("sensor", "system_dc_pv_power"),
}

# Ohne diese beiden kann der Treiber nichts stellen.
VICTRON_PFLICHT: tuple[str, ...] = ("netzsollwert", "ladestrom")

# Was der Treiber zum Stellen braucht — deaktiviert ist es so gut wie fehlend.
VICTRON_NAMEN: dict[str, str] = {
    "netzsollwert": "ESS-Netzsollwert",
    "ladestrom": "DVCC-Ladestrom",
    "batteriespannung": "Batteriespannung",
}

# Messwerte für die Sensor-Zuordnung im Assistenten (Konfig-Schlüssel →
# short_id-Kandidaten, erster Treffer gewinnt). Netz: nur der Netzzähler,
# er liefert die Summe aller Phasen. Kein Rückfall auf eine einzelne Phase
# (``system_grid_power_l1``) — dreiphasig wäre das falsch (Modulkopf); ohne
# Zähler wählt der Betreiber den Netzsensor selbst. Aus demselben Grund wird
# PV an einem AC-Wechselrichter nicht erkannt: Das GX meldet sie je Phase
# (``system_pv_on_output_power_l1`` …), eine Summe gibt es nicht.
VICTRON_SENSOREN: dict[str, tuple[str, ...]] = {
    "battery_soc_sensor": ("system_dc_battery_soc",),
    "pv_power_sensor": ("system_dc_pv_power",),
    "grid_power_sensor": ("grid_power",),
    "battery_power_sensor": ("system_dc_battery_power",),
}

# Optionen der Auswahl-Entitäten (``VictronEnum.id`` — die Integration nimmt
# die Enum-Kennung als Option, nicht den Anzeigetext).
ESS_MODI_OK = ("optimized_battery_life", "optimized_no_battery_life")
DESS_AUS = "off"


class VictronFehler(Exception):
    """Ein Grund, der als ``last_write_error`` taugt (kurz, ohne Zahlen)."""

# Toleranzen, unterhalb derer ein Wert als „schon gesetzt" gilt. Beide
# Stellgrößen sind gespeicherte Einstellungen — jeder Schreibvorgang landet
# im Einstellungsspeicher des GX-Geräts, deshalb kein Schreiben ohne Not.
NETZSOLLWERT_TOLERANZ_W = 10.0  # Schrittweite der Entität
LADESTROM_TOLERANZ_A = 0.5


def _short_id_muster(short_id: str) -> re.Pattern:
    """unique_id endet auf ``_<geräte-id>_<short_id>``.

    Die Geräte-ID direkt davor verhindert, dass ``grid_power`` auch
    ``…_system_0_system_grid_power_l1`` oder einen PV-Wechselrichter mit
    gleichlautendem Ende trifft.
    """
    return re.compile(rf"_\d+_{re.escape(short_id)}$")


def _victron_eintrag(hass: Any, domain: str, short_id: str) -> Any:
    """Registry-Eintrag zur Metrik, oder None.

    Findet auch deaktivierte Entitäten (``disabled_by``) — die Prüfung im
    Assistenten meldet sie. Bei mehreren Treffern (zwei GX-Geräte, mehrere
    Netzzähler) gewinnt die niedrigste Geräte-ID — bei Victron ist das
    Gerät 0 bzw. der erste Zähler.
    """
    try:
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
        eintraege = list(registry.entities.values())
    except Exception:  # noqa: BLE001 — ohne Registry keine Aussage (Tests)
        return None
    muster = _short_id_muster(short_id)
    treffer = []
    for eintrag in eintraege:
        if getattr(eintrag, "platform", None) != VICTRON_DOMAIN:
            continue
        if getattr(eintrag, "domain", None) != domain:
            continue
        unique_id = str(getattr(eintrag, "unique_id", "") or "")
        m = muster.search(unique_id)
        if m is None:
            continue
        geraet = int(m.group(0).split("_")[1])
        treffer.append((geraet, eintrag.entity_id, eintrag))
    if not treffer:
        return None
    return min(treffer, key=lambda t: (t[0], t[1]))[2]


def find_victron_entity(hass: Any, domain: str, short_id: str) -> str | None:
    """Entity-ID zur Metrik aus der Entity-Registry, oder None."""
    eintrag = _victron_eintrag(hass, domain, short_id)
    return eintrag.entity_id if eintrag is not None else None


def victron_sensoren(hass: Any) -> dict[str, str]:
    """Messwert-Sensoren für die Zuordnung im Assistenten (Konfig-Schlüssel → Entity)."""
    sensoren: dict[str, str] = {}
    for conf_key, kandidaten in VICTRON_SENSOREN.items():
        for short_id in kandidaten:
            entity_id = find_victron_entity(hass, "sensor", short_id)
            if entity_id:
                sensoren[conf_key] = entity_id
                break
    return sensoren


def _zustand(hass: Any, entity_id: str | None) -> str | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    raw = str(state.state)
    if raw in ("unavailable", "unknown", ""):
        return None
    return raw


def _zahl(hass: Any, entity_id: str | None) -> float | None:
    raw = _zustand(hass, entity_id)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _attribut_zahl(hass: Any, entity_id: str | None, name: str) -> float | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    try:
        return float(state.attributes.get(name))
    except (TypeError, ValueError):
        return None


def victron_pruefung(hass: Any) -> dict[str, Any]:
    """Was am GX-Gerät stimmen muss, damit der Treiber wirkt.

    ``entities``: Schlüssel → Entity-ID (nur gefundene). ``fehlt``: Namen der
    Pflicht-Stellgrößen ohne Entität. ``probleme``: Einstellungen, mit denen
    die Steuerung nicht oder falsch wirkt — der Assistent lässt dann nicht
    weiter. ``hinweise``: Einstellungen, die funktionieren, aber eine Folge
    haben, die der Betreiber kennen soll.
    """
    entities: dict[str, str] = {}
    deaktiviert: list[str] = []
    for key, (domain, short_id) in VICTRON_METRIKEN.items():
        eintrag = _victron_eintrag(hass, domain, short_id)
        if eintrag is None:
            continue
        entities[key] = eintrag.entity_id
        if key in VICTRON_NAMEN and getattr(eintrag, "disabled_by", None):
            deaktiviert.append(VICTRON_NAMEN[key])

    fehlt = [VICTRON_NAMEN[k] for k in VICTRON_PFLICHT if k not in entities]

    probleme: list[str] = []
    hinweise: list[str] = []
    if deaktiviert:
        probleme.append(
            "In Home Assistant deaktiviert: " + ", ".join(deaktiviert)
            + " — bitte unter Geräte & Dienste → Victron GX aktivieren"
        )

    ess = _zustand(hass, entities.get("ess_modus"))
    if ess is not None and ess not in ESS_MODI_OK:
        probleme.append(
            "ESS-Modus ist nicht „Optimiert“ — der Netzsollwert wirkt nur "
            "im Modus „Optimiert (mit/ohne BatteryLife)“"
        )
    dess = _zustand(hass, entities.get("dess_modus"))
    if dess is not None and dess != DESS_AUS:
        probleme.append(
            "Dynamic ESS ist eingeschaltet — es überschreibt den Netzsollwert "
            "und muss aus sein"
        )
    dvcc = _zustand(hass, entities.get("dvcc"))
    if dvcc == "off":
        probleme.append("DVCC ist aus — ohne DVCC wirkt das Ladelimit nicht")

    dc_pv = _zahl(hass, entities.get("dc_pv_leistung"))
    ueberschuss = _zustand(hass, entities.get("dc_ueberschuss_einspeisen"))
    if dc_pv is not None and ueberschuss == "off":
        hinweise.append(
            "„DC-gekoppelte PV — Überschuss einspeisen“ ist aus: Ein "
            "Ladelimit regelt die Laderegler ab, statt den Überschuss "
            "einzuspeisen"
        )
    geplant = [
        str(n + 1)
        for n in range(GEPLANTES_LADEN_FENSTER)
        if _zustand(hass, entities.get(f"geplantes_laden_{n}")) == "on"
    ]
    if geplant:
        probleme.append(
            "ESS „Geplantes Laden“ ist eingeschaltet (Zeitfenster "
            + ", ".join(geplant)
            + ") — es lädt zu festen Zeiten aus dem Netz und hält die "
            "Batterie dort gegen jede Entladung; der Fahrplan weiß davon "
            "nichts. Bitte am GX-Gerät unter ESS → Geplantes Laden ausschalten"
        )
    ladestrom = _zahl(hass, entities.get("ladestrom"))
    ladestrom_min = _attribut_zahl(hass, entities.get("ladestrom"), "min")
    if (
        ladestrom is not None
        and ladestrom < 0
        and ladestrom_min is not None
        and ladestrom < ladestrom_min
    ):
        probleme.append(
            "DVCC-Ladestrom steht auf −1 (nicht begrenzt), diese Version der "
            "Integration kann −1 aber nicht zurückschreiben. Bitte am "
            "GX-Gerät unter DVCC einen festen Höchststrom eintragen (z. B. den "
            "Ladestrom des BMS) oder Home Assistant aktualisieren"
        )
    einspeisung = _zahl(hass, entities.get("max_einspeiseleistung"))
    if einspeisung is not None and 0 <= einspeisung < 100:
        probleme.append(
            "Die ESS-Einspeisegrenze steht auf 0 W — Einspeisen aus der "
            "Batterie ist damit gesperrt"
        )
    return {
        "entities": entities,
        "fehlt": fehlt,
        "probleme": probleme,
        "hinweise": hinweise,
    }


def _grund(exc: Exception) -> str:
    return str(exc) if isinstance(exc, VictronFehler) else type(exc).__name__


class VictronStateStore:
    """Vorwerte der beiden Stellgrößen, über Neustarts hinweg.

    Gesichert wird beim ERSTEN Eingriff in eine Stellgröße — bei jedem
    weiteren steht im Gerät bereits unser Wert, ihn als Vorwert zu sichern
    hieße, den echten zu verlieren. Die Freigabe schreibt zurück und löscht.
    """

    STORAGE_KEY = "eeg_energy_optimizer.victron_state"
    STORAGE_VERSION = 1

    def __init__(self, hass: Any) -> None:
        self._store = (
            Store(hass, self.STORAGE_VERSION, self.STORAGE_KEY)
            if Store is not None
            else None
        )
        self._data: dict[str, float] = {}
        self._loaded = False

    async def async_load(self) -> None:
        if self._loaded:
            return
        if self._store is not None:
            data = await self._store.async_load()
            self._data = data if isinstance(data, dict) else {}
        self._loaded = True

    def vorwert(self, key: str) -> float | None:
        wert = self._data.get(key)
        return float(wert) if wert is not None else None

    async def async_sichern(self, key: str, wert: float) -> None:
        self._data[key] = float(wert)
        if self._store is not None:
            await self._store.async_save(self._data)

    async def async_loeschen(self, key: str) -> None:
        if self._data.pop(key, None) is not None and self._store is not None:
            await self._store.async_save(self._data)


class VictronInverter(InverterBase):
    """Victron ESS über die HA-Integration ``victron_gx`` (MQTT)."""

    def __init__(self, hass: Any, config: dict) -> None:
        super().__init__(hass, config)
        self._state_store = VictronStateStore(hass)
        # Ziel-SOC der laufenden Entladung und der Abmelder des
        # SOC-Beobachters — siehe _beobachte_ziel_soc().
        self._ziel_soc: float | None = None
        self._ziel_abmelden: Any = None
        # Executor (30-s-Lauf) und SOC-Beobachter schreiben nebenläufig; ein
        # Schreiben über MQTT dauert. Ohne Sperre könnte die Zielrücknahme
        # zwischen Sichern und Schreiben eines Sollwerts fallen — der Wert
        # käme danach an, unbeobachtet und ohne Vorwert.
        self._sperre = asyncio.Lock()
        # Zuletzt selbst geschriebener Wert je Stellgröße: Der Zustand der
        # Entität kommt per MQTT verzögert zurück.
        self._geschrieben: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Entitäten
    # ------------------------------------------------------------------
    def _entity(self, key: str) -> str | None:
        """Entität zur Stellgröße aus der Registry, None, wenn nicht auffindbar.

        Bewusst ohne gespeicherte Entity-ID: Nach einem Neuanlegen der
        Integration oder einer Umbenennung greift die Registry von selbst.
        """
        domain, short_id = VICTRON_METRIKEN[key]
        return find_victron_entity(self._hass, domain, short_id)

    def _lies(self, key: str) -> float | None:
        return _zahl(self._hass, self._entity(key))

    def _attribut(self, key: str, name: str) -> float | None:
        return _attribut_zahl(self._hass, self._entity(key), name)

    async def _schreibe(self, key: str, wert: float) -> None:
        entity_id = self._entity(key)
        if not entity_id:
            raise VictronFehler(f"Entität {key} nicht gefunden")
        await self._hass.services.async_call(
            "number", "set_value",
            {"entity_id": entity_id, "value": wert},
            blocking=True,
        )
        self._geschrieben[key] = float(wert)

    async def _setze(self, key: str, wert: float, toleranz: float) -> None:
        """Wert schreiben, Vorwert beim ersten Eingriff sichern.

        Steht der Wert schon im Gerät — und haben wir nicht gerade etwas
        anderes geschrieben, dessen Echo noch aussteht —, wird nicht
        geschrieben (siehe ``NETZSOLLWERT_TOLERANZ_W``). Ist der aktuelle Wert
        beim ersten Eingriff unlesbar, wird gar nicht geschrieben: ohne
        Vorwert könnte die Freigabe nichts zurücknehmen, ein Netzsollwert
        bliebe über jeden Neustart hinweg stehen.
        """
        await self._state_store.async_load()
        aktuell = self._lies(key)
        if self._state_store.vorwert(key) is None:
            if aktuell is None:
                raise VictronFehler(f"Vorwert {key} unlesbar")
            await self._state_store.async_sichern(key, aktuell)
        zuletzt = self._geschrieben.get(key)
        if (
            aktuell is not None
            and abs(aktuell - wert) < toleranz
            and (zuletzt is None or abs(zuletzt - wert) < toleranz)
        ):
            return
        await self._schreibe(key, wert)

    async def _zuruecksetzen(self, key: str, toleranz: float) -> None:
        """Gesicherten Vorwert zurückschreiben und den Eintrag löschen.

        Liegt der Vorwert außerhalb des Bereichs der Entität (DVCC-Ladestrom
        −1, siehe Modulkopf), wird die nächste Grenze geschrieben. Schlägt
        das Schreiben fehl, bleibt der Eintrag für den nächsten Versuch.
        """
        await self._state_store.async_load()
        vorwert = self._state_store.vorwert(key)
        if vorwert is None:
            return
        ziel = vorwert
        minimum = self._attribut(key, "min")
        maximum = self._attribut(key, "max")
        if minimum is not None and ziel < minimum:
            ziel = maximum if (key == "ladestrom" and maximum is not None) else minimum
        if maximum is not None and ziel > maximum:
            ziel = maximum
        aktuell = self._lies(key)
        zuletzt = self._geschrieben.get(key)
        if (
            aktuell is None
            or abs(aktuell - ziel) >= toleranz
            or (zuletzt is not None and abs(zuletzt - ziel) >= toleranz)
        ):
            await self._schreibe(key, ziel)
        await self._state_store.async_loeschen(key)

    def _batteriespannung(self) -> float | None:
        spannung = self._lies("batteriespannung")
        if spannung is None or spannung <= 0:
            return None
        return spannung

    # ------------------------------------------------------------------
    # Ziel-SOC der Entladung
    # ------------------------------------------------------------------
    def _soc(self) -> float | None:
        return _zahl(self._hass, self._config.get(CONF_BATTERY_SOC_SENSOR))

    def _am_ziel(self) -> bool:
        soc = self._soc()
        return self._ziel_soc is not None and soc is not None and soc <= self._ziel_soc

    def _beobachte_ziel_soc(self, target_soc: float | None) -> None:
        """Den Ladestand während der Entladung beobachten.

        Das GX-Gerät kennt keinen Ziel-SOC für einen Netzsollwert — es
        regelt, bis der ESS-Mindest-SOC greift. Der Executor schreibt nur bei
        einer Änderung; zwischen zwei Befehlen hielte niemand an. Deshalb
        meldet sich der Treiber selbst am SOC-Sensor an und nimmt den
        Sollwert zurück, sobald das Ziel erreicht ist. Ohne Ziel aus dem
        Plan gilt die Reserve des Geräts (ESS-Mindest-SOC / BatteryLife) —
        ganz ohne Ziel liefe die Entladung unbeobachtet.
        """
        if target_soc is None:
            target_soc = self.get_backup_reserve_soc_pct()
        self._ziel_soc = target_soc
        if target_soc is None or self._ziel_abmelden is not None:
            return
        soc_sensor = self._config.get(CONF_BATTERY_SOC_SENSOR)
        if not soc_sensor:
            return
        try:
            from homeassistant.helpers.event import async_track_state_change_event
        except ImportError:  # pragma: no cover — Testumgebung
            return

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
        if self._ziel_soc is None:
            return
        ziel = self._ziel_soc
        self._beobachtung_beenden()
        try:
            await self._zuruecksetzen("netzsollwert", NETZSOLLWERT_TOLERANZ_W)
            _LOGGER.info("Victron: Ziel-SOC %.0f %% erreicht — Netzsollwert zurückgenommen", ziel)
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Victron: Netzsollwert am Ziel-SOC nicht zurücknehmbar — "
                "die nächste Freigabe versucht es erneut", exc_info=True,
            )

    # ------------------------------------------------------------------
    # InverterBase
    # ------------------------------------------------------------------
    async def async_set_charge_limit(self, power_kw: float) -> bool:
        """Laden auf ``power_kw`` begrenzen; 0 sperrt das Laden.

        Zuerst wird ein eigener Netzsollwert zurückgenommen — sonst liefe
        eine vorherige Entladung unter dem neuen Limit weiter.
        """
        if self._nicht_endlich(power_kw):
            return False
        async with self._sperre:
            return await self._ladelimit(power_kw)

    async def _ladelimit(self, power_kw: float) -> bool:
        try:
            # Die Entladung zuerst zurücknehmen — auch wenn die Spannung
            # gleich fehlt: ein stehender Netzsollwert ist der teurere Fehler.
            self._beobachtung_beenden()
            await self._zuruecksetzen("netzsollwert", NETZSOLLWERT_TOLERANZ_W)
            spannung = self._batteriespannung()
            if spannung is None:
                # Keine Spannung, kein Strom: die Umrechnung zu raten hieße,
                # bei einem 12- oder 24-V-System das Vier- oder Doppelte zu
                # schreiben.
                return self._fehler("Batteriespannung unbekannt")
            ampere = max(0.0, float(power_kw)) * 1000.0 / spannung
            maximum = self._attribut("ladestrom", "max")
            if maximum is not None:
                ampere = min(ampere, maximum)
            await self._setze("ladestrom", round(ampere, 1), LADESTROM_TOLERANZ_A)
            return self._erfolg()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("Victron: Ladelimit konnte nicht gesetzt werden")
            return self._fehler(_grund(exc))

    async def async_set_discharge(
        self, power_kw: float, target_soc: float | None = None
    ) -> bool:
        """Einspeisung ``power_kw`` als Netzsollwert, bis ``target_soc``.

        ``power_kw`` ist die geplante Einspeisung (``discharge_is_grid_setpoint``).
        Ein eigenes Ladelimit wird vorher zurückgenommen. Steht der Ladestand
        schon am Ziel, wird nicht entladen — der Befehl gilt trotzdem als
        ausgeführt, er ist erfüllt.
        """
        if self._nicht_endlich(power_kw, target_soc):
            return False
        async with self._sperre:
            return await self._entladung(power_kw, target_soc)

    async def _entladung(self, power_kw: float, target_soc: float | None) -> bool:
        try:
            await self._zuruecksetzen("ladestrom", LADESTROM_TOLERANZ_A)
            self._beobachte_ziel_soc(target_soc)
            if self._am_ziel():
                await self._ziel_erreicht()
                return self._erfolg()
            watt = -round(max(0.0, abs(float(power_kw))) * 1000.0 / NETZSOLLWERT_TOLERANZ_W) * NETZSOLLWERT_TOLERANZ_W
            minimum = self._attribut("netzsollwert", "min")
            if minimum is not None:
                watt = max(watt, minimum)
            await self._setze("netzsollwert", watt, NETZSOLLWERT_TOLERANZ_W)
            return self._erfolg()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("Victron: Entladung konnte nicht gestartet werden")
            return self._fehler(_grund(exc))

    async def async_stop_forcible(self) -> bool:
        """Freigeben: Netzsollwert, dann Ladestrom auf die Vorwerte.

        Der Sollwert zuerst — er ist der Eingriff, der Energie bewegt.
        Ohne gesicherten Vorwert gibt es nichts zurückzunehmen.
        """
        async with self._sperre:
            self._beobachtung_beenden()
            try:
                await self._zuruecksetzen("netzsollwert", NETZSOLLWERT_TOLERANZ_W)
                await self._zuruecksetzen("ladestrom", LADESTROM_TOLERANZ_A)
                return self._erfolg()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("Victron: Freigabe fehlgeschlagen")
                return self._fehler(_grund(exc))

    @property
    def is_available(self) -> bool:
        entries = self._hass.config_entries.async_entries(VICTRON_DOMAIN)
        return any(entry.state.value == "loaded" for entry in entries)

    # ------------------------------------------------------------------
    # Fahrplan-Steuerschnittstelle (Schedule-Executor)
    # ------------------------------------------------------------------
    @property
    def supports_schedule_control(self) -> bool:
        return True

    @property
    def discharge_is_grid_setpoint(self) -> bool:
        """ESS regelt den Netzanschlusspunkt, nicht die Batterie (siehe Modulkopf)."""
        return True

    async def async_get_charge_limit_kw(self) -> float | None:
        """Gesetzter DVCC-Ladestrom × Batteriespannung, −1 (unbegrenzt) → None."""
        ampere = self._lies("ladestrom")
        spannung = self._batteriespannung()
        if ampere is None or ampere < 0 or spannung is None:
            return None
        return ampere * spannung / 1000.0

    def get_charge_limit_max_kw(self) -> float | None:
        """Kleinere Grenze aus Entitätsbereich und BMS-Ladestrom, in kW."""
        spannung = self._batteriespannung()
        if spannung is None:
            return None
        grenzen = [
            a for a in (self._attribut("ladestrom", "max"), self._lies("bms_max_ladestrom"))
            if a is not None and a > 0
        ]
        return min(grenzen) * spannung / 1000.0 if grenzen else None

    # get_max_discharge_power_kw bleibt beim Default (None): Die ESS-Grenze
    # „Max. Wechselrichterleistung" gilt je nach Venus-OS-Stand je Phase —
    # als Gesamtgrenze genommen, deckelte Guard 2 eine dreiphasige Anlage
    # auf ein Drittel. Unbelegt; die Grenze setzt das Gerät ohnehin selbst.

    def get_backup_reserve_soc_pct(self) -> float | None:
        """Höhere aus ESS-Mindest-SOC und aktiver SOC-Grenze (BatteryLife)."""
        werte = [
            v for v in (self._lies("min_soc"), self._lies("aktive_soc_grenze"))
            if v is not None
        ]
        return max(werte) if werte else None

    def get_control_entities(self) -> list[dict]:
        rows: list[dict] = []
        for key, label, rolle in (
            ("netzsollwert", "ESS-Netzsollwert", "forcible"),
            ("ladestrom", "DVCC-Ladestrom", "charge_limit"),
            ("min_soc", "ESS-Mindest-SOC", "backup_soc"),
            ("ess_modus", "ESS-Modus", "mode"),
        ):
            entity_id = self._entity(key)
            if entity_id and self._hass.states.get(entity_id) is not None:
                rows.append({"label": label, "entity_id": entity_id, "role": rolle})
        return rows
