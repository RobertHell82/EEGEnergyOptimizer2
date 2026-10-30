"""Tests für den Victron-ESS-Treiber (HA-Integration ``victron_gx``, MQTT).

Die Entitäten und Werte stammen von einer echten Anlage (gelesen am
29.10.2026: drei Multiplus-II dreiphasig, Pytes-Batterie, ET340-Netzzähler).
Eine kleine Anlagen-Attrappe nimmt die ``number.set_value``-Aufrufe an und
setzt den Zustand, wie es die Integration nach dem MQTT-Echo täte — so
prüfen die Tests nicht nur, WAS geschrieben wird, sondern auch, dass die
Freigabe den Zustand vor dem Eingriff wiederherstellt.
"""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.eeg_energy_optimizer.const import (
    CONF_BATTERY_POWER_SENSOR,
    CONF_BATTERY_SOC_SENSOR,
    CONF_GRID_POWER_SENSOR,
    CONF_INVERTER_TYPE,
    CONF_PV_POWER_SENSOR,
    INVERTER_TYPE_VICTRON,
)
from custom_components.eeg_energy_optimizer.inverter import INVERTER_TYPES, create_inverter
from custom_components.eeg_energy_optimizer.inverter.victron import (
    VictronInverter,
    find_victron_entity,
    victron_pruefung,
    victron_sensoren,
)
from custom_components.eeg_energy_optimizer.power_readings import (
    compute_house_load_kw,
    resolve_sign,
)

INST = "c0619abb7cad"

SOLLWERT = "number.gx_device_ac_leistungssollwert"
LADESTROM = "number.gx_device_ess_maximaler_ladestrom"
SPANNUNG = "sensor.gx_device_dc_batterie_spannung"
BMS = "sensor.pytes_battery_id_512_maximal_zulassiger_ladestrom"
MIN_SOC = "number.gx_device_ess_mindest_soc"
AKTIV_SOC = "sensor.gx_device_aktiver_soc_grenzwert"
WR_GRENZE = "number.gx_device_ess_maximale_wechselrichterleistung"
ESS = "select.gx_device_ess_modus"
DESS = "select.gx_device_dess_modus"
DVCC = "switch.gx_device_dvcc"
UEBERSCHUSS = "switch.gx_device_dc_gekoppelte_pv_uberschusseinspeisung"
EINSPEISUNG = "number.gx_device_ess_maximale_einspeiseleistung"
DC_PV = "sensor.gx_device_pv_leistung"
SOC = "sensor.gx_device_dc_batterie_ladestand"
BATTERIE = "sensor.gx_device_dc_batterie_leistung"
NETZ = "sensor.grid_meter_id_31_leistung"
NETZ_L1 = "sensor.gx_device_netzleistung_l1"
GEPLANT = "switch.gx_device_ess_batterylife_ladeplan_{}_aktiviert"

# entity_id → (unique_id ohne Installations-Präfix, Zustand, Attribute)
ANLAGE = {
    SOLLWERT: ("system_0_system_ac_power_set_point", "0", {"min": -1000000, "max": 1000000}),
    LADESTROM: ("system_0_system_ess_max_charge_current", "125", {"min": -1, "max": 10000}),
    SPANNUNG: ("system_0_system_dc_battery_voltage", "52.6", {}),
    BMS: ("battery_512_battery_max_charge_current", "80.0", {}),
    MIN_SOC: ("system_0_system_ess_min_soc_limit", "20", {"min": 0, "max": 100}),
    AKTIV_SOC: ("system_0_system_control_active_soc_limit", "20.0", {}),
    WR_GRENZE: ("system_0_system_ess_max_inverter_power_limit", "-1", {"min": -1, "max": 1000000}),
    ESS: ("system_0_system_ess_batterylife_state", "optimized_battery_life", {}),
    DESS: ("system_0_system_settings_dess_mode", "off", {}),
    DVCC: ("system_0_system_dvcc", "on", {}),
    UEBERSCHUSS: ("system_0_system_settings_overvoltage_feedin", "on", {}),
    EINSPEISUNG: ("system_0_system_ess_max_feed_in_power", "-1", {"min": -1, "max": 1000000}),
    DC_PV: ("system_0_system_dc_pv_power", "0.0", {"unit_of_measurement": "W"}),
    SOC: ("system_0_system_dc_battery_soc", "91.0", {"unit_of_measurement": "%"}),
    BATTERIE: ("system_0_system_dc_battery_power", "-7262.0", {"unit_of_measurement": "W"}),
    NETZ: ("grid_31_grid_power", "114.8", {"unit_of_measurement": "W"}),
    NETZ_L1: ("system_0_system_grid_power_l1", "1442.4", {"unit_of_measurement": "W"}),
    **{
        GEPLANT.format(n): (f"system_0_system_ess_schedule_charge_{n}_enabled", "off", {})
        for n in range(5)
    },
}


class Anlage:
    """Zustände + Registry einer victron_gx-Installation; set_value wirkt."""

    def __init__(self, hass, **abweichend):
        self.werte = {eid: [st, dict(attr)] for eid, (_, st, attr) in ANLAGE.items()}
        for eid, st in abweichend.items():
            if st is None:
                self.werte.pop(eid, None)
            else:
                self.werte[eid][0] = st
        self.registry = SimpleNamespace(entities={
            eid: SimpleNamespace(
                entity_id=eid,
                platform="victron_gx",
                domain=eid.split(".")[0],
                unique_id=f"{INST}_{uid}",
            )
            for eid, (uid, _, _) in ANLAGE.items()
            if eid in self.werte
        })
        self.schreibfehler = False
        hass.states.get.side_effect = self.get
        hass.services.async_call = AsyncMock(side_effect=self.call)

    def get(self, entity_id):
        if entity_id not in self.werte:
            return None
        st, attr = self.werte[entity_id]
        return SimpleNamespace(state=st, attributes=attr)

    async def call(self, domain, service, data, blocking=False):
        if self.schreibfehler:
            raise RuntimeError("MQTT getrennt")
        assert (domain, service) == ("number", "set_value")
        self.werte[data["entity_id"]][0] = str(data["value"])

    def wert(self, entity_id) -> float:
        return float(self.werte[entity_id][0])

    def setze(self, entity_id, wert) -> None:
        self.werte[entity_id][0] = str(wert)


class _Store:
    def __init__(self):
        self.data = None
        self.saved = []

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = dict(data)
        self.saved.append(dict(data))


@pytest.fixture
def registry_patch():
    patcher = patch("homeassistant.helpers.entity_registry.async_get")
    yield patcher.start()
    patcher.stop()


@pytest.fixture
def anlage(mock_hass, registry_patch):
    a = Anlage(mock_hass)
    registry_patch.return_value = a.registry
    return a


@pytest.fixture
def soc_beobachter():
    """``async_track_state_change_event`` abfangen: liefert die Rückrufe."""
    rueckrufe = []
    abmelder = MagicMock()

    def _track(hass, entities, callback):
        rueckrufe.append((list(entities), callback))
        return abmelder

    takte = []
    takt_abmelder = MagicMock()

    def _intervall(hass, callback, intervall):
        takte.append((callback, intervall))
        return takt_abmelder

    modul = ModuleType("homeassistant.helpers.event")
    modul.async_track_state_change_event = _track
    modul.async_track_time_interval = _intervall
    with patch.dict(sys.modules, {"homeassistant.helpers.event": modul}):
        yield SimpleNamespace(
            rueckrufe=rueckrufe, abmelder=abmelder, takte=takte, takt_abmelder=takt_abmelder
        )


def _treiber(hass, store=None, **config):
    inv = VictronInverter(hass, {CONF_BATTERY_SOC_SENSOR: SOC, **config})
    inv._state_store._store = store if store is not None else _Store()
    inv._state_store._data = {}
    inv._state_store._loaded = False
    return inv


def _schreibvorgaenge(hass):
    return [(c.args[2]["entity_id"], c.args[2]["value"]) for c in hass.services.async_call.call_args_list]


# ----------------------------------------------------------------------
# Registrierung und Grundeigenschaften
# ----------------------------------------------------------------------
class TestRegistrierung:
    def test_factory_kennt_victron(self, mock_hass):
        assert INVERTER_TYPES[INVERTER_TYPE_VICTRON] is VictronInverter
        inv = create_inverter(INVERTER_TYPE_VICTRON, mock_hass, {})
        assert isinstance(inv, VictronInverter)

    def test_steuerbar_mit_netzsollwert(self, mock_hass):
        inv = VictronInverter(mock_hass, {})
        assert inv.supports_schedule_control is True
        assert inv.discharge_is_grid_setpoint is True
        assert inv.supports_grid_charge is True
        assert inv.supports_discharge_limit is False

    def test_verfuegbar_nur_mit_geladener_integration(self, mock_hass):
        inv = VictronInverter(mock_hass, {})
        assert inv.is_available is False
        geladen = SimpleNamespace(state=SimpleNamespace(value="loaded"))
        fehler = SimpleNamespace(state=SimpleNamespace(value="setup_retry"))
        mock_hass.config_entries.async_entries.return_value = [fehler]
        assert inv.is_available is False
        mock_hass.config_entries.async_entries.return_value = [fehler, geladen]
        assert inv.is_available is True
        mock_hass.config_entries.async_entries.assert_called_with("victron_gx")


# ----------------------------------------------------------------------
# Auflösung über die Entity-Registry
# ----------------------------------------------------------------------
class TestRegistry:
    def test_aufloesung_ueber_unique_id(self, mock_hass, anlage):
        assert find_victron_entity(mock_hass, "number", "system_ac_power_set_point") == SOLLWERT
        assert find_victron_entity(mock_hass, "sensor", "battery_max_charge_current") == BMS

    def test_grid_power_trifft_nicht_die_phase(self, mock_hass, anlage):
        """``grid_power`` darf weder ``system_grid_power_l1`` noch
        ``grid_power_l1`` des Zählers treffen."""
        anlage.registry.entities["sensor.zaehler_l1"] = SimpleNamespace(
            entity_id="sensor.zaehler_l1", platform="victron_gx", domain="sensor",
            unique_id=f"{INST}_grid_31_grid_power_l1",
        )
        assert find_victron_entity(mock_hass, "sensor", "grid_power") == NETZ

    def test_domain_und_plattform_muessen_passen(self, mock_hass, anlage):
        assert find_victron_entity(mock_hass, "sensor", "system_ac_power_set_point") is None
        anlage.registry.entities["number.fremd"] = SimpleNamespace(
            entity_id="number.fremd", platform="andere", domain="number",
            unique_id="x_system_0_system_ess_min_soc_limit",
        )
        assert find_victron_entity(mock_hass, "number", "system_ess_min_soc_limit") == MIN_SOC

    def test_niedrigste_geraete_id_gewinnt(self, mock_hass, anlage):
        anlage.registry.entities["sensor.zaehler_2"] = SimpleNamespace(
            entity_id="sensor.zaehler_2", platform="victron_gx", domain="sensor",
            unique_id=f"{INST}_grid_40_grid_power",
        )
        anlage.registry.entities["sensor.zaehler_0"] = SimpleNamespace(
            entity_id="sensor.zaehler_0", platform="victron_gx", domain="sensor",
            unique_id=f"{INST}_grid_7_grid_power",
        )
        assert find_victron_entity(mock_hass, "sensor", "grid_power") == "sensor.zaehler_0"

    def test_ohne_registry_keine_aussage(self, mock_hass, registry_patch):
        registry_patch.side_effect = RuntimeError("keine Registry")
        assert find_victron_entity(mock_hass, "number", "system_ac_power_set_point") is None

    def test_umbenannte_entity_wird_gefunden(self, mock_hass, anlage):
        """Keine gespeicherte Entity-ID: eine Umbenennung greift sofort."""
        inv = _treiber(mock_hass)
        assert inv._entity("netzsollwert") == SOLLWERT
        eintrag = anlage.registry.entities.pop(SOLLWERT)
        eintrag.entity_id = "number.mein_sollwert"
        anlage.registry.entities["number.mein_sollwert"] = eintrag
        assert inv._entity("netzsollwert") == "number.mein_sollwert"


# ----------------------------------------------------------------------
# Sensorzuordnung und Prüfung im Assistenten
# ----------------------------------------------------------------------
class TestAssistent:
    def test_sensoren_der_echten_anlage(self, mock_hass, anlage):
        assert victron_sensoren(mock_hass) == {
            "battery_soc_sensor": SOC,
            "pv_power_sensor": DC_PV,
            "grid_power_sensor": NETZ,
            "battery_power_sensor": BATTERIE,
        }

    def test_kein_rueckfall_auf_eine_phase(self, mock_hass, registry_patch):
        """Dreiphasig misst L1 allein 1,4 kW Bezug, der Zähler gesamt 0,1 kW —
        ohne Zähler bleibt der Netzsensor leer, der Betreiber wählt selbst."""
        a = Anlage(mock_hass, **{NETZ: None})
        registry_patch.return_value = a.registry
        assert "grid_power_sensor" not in victron_sensoren(mock_hass)

    def test_echte_anlage_ist_bis_auf_geplantes_laden_in_ordnung(self, mock_hass, anlage):
        p = victron_pruefung(mock_hass)
        assert p["fehlt"] == []
        assert p["probleme"] == []
        assert p["hinweise"] == []
        assert p["entities"]["netzsollwert"] == SOLLWERT
        assert p["entities"]["ladestrom"] == LADESTROM

    def test_geplantes_laden_ist_eine_warnung(self, mock_hass, registry_patch):
        a = Anlage(mock_hass, **{GEPLANT.format(0): "on", GEPLANT.format(2): "on"})
        registry_patch.return_value = a.registry
        p = victron_pruefung(mock_hass)
        assert p["probleme"] == []
        assert len(p["warnungen"]) == 1
        assert "Geplantes Laden" in p["warnungen"][0]
        assert "Zeitfenster 1, 3" in p["warnungen"][0]

    @pytest.mark.parametrize(
        "entity, wert, stichwort",
        [
            (ESS, "keep_batteries_charged", "ESS-Modus"),
            (ESS, "external_control", "ESS-Modus"),
            (DESS, "auto_vrm", "Dynamic ESS"),
            (DVCC, "off", "DVCC"),
            (EINSPEISUNG, "0", "Einspeisegrenze"),
        ],
    )
    def test_probleme(self, mock_hass, registry_patch, entity, wert, stichwort):
        a = Anlage(mock_hass, **{entity: wert})
        registry_patch.return_value = a.registry
        p = victron_pruefung(mock_hass)
        assert len(p["probleme"]) == 1
        assert stichwort in p["probleme"][0]

    def test_ohne_batterylife_ist_auch_recht(self, mock_hass, registry_patch):
        a = Anlage(mock_hass, **{ESS: "optimized_no_battery_life"})
        registry_patch.return_value = a.registry
        assert victron_pruefung(mock_hass)["probleme"] == []

    def test_unbekannter_zustand_ist_kein_problem(self, mock_hass, registry_patch):
        a = Anlage(mock_hass, **{ESS: "unavailable", DVCC: "unknown"})
        registry_patch.return_value = a.registry
        assert victron_pruefung(mock_hass)["probleme"] == []

    def test_dc_ueberschuss_aus_ist_ein_hinweis(self, mock_hass, registry_patch):
        a = Anlage(mock_hass, **{UEBERSCHUSS: "off"})
        registry_patch.return_value = a.registry
        p = victron_pruefung(mock_hass)
        assert p["probleme"] == []
        assert len(p["hinweise"]) == 1 and "Überschuss" in p["hinweise"][0]

    def test_ladestrom_minus_eins_nur_problem_wenn_nicht_schreibbar(self, mock_hass, registry_patch):
        """Ältere Integration (0–100 A): −1 wäre nach der ersten Freigabe
        still durch 100 A ersetzt — das lässt der Assistent nicht durch."""
        a = Anlage(mock_hass, **{LADESTROM: "-1"})
        registry_patch.return_value = a.registry
        assert victron_pruefung(mock_hass)["probleme"] == []
        a.werte[LADESTROM][1] = {"min": 0, "max": 100}
        p = victron_pruefung(mock_hass)
        assert len(p["probleme"]) == 1 and "−1" in p["probleme"][0]

    def test_deaktivierte_stellgroesse_ist_ein_problem(self, mock_hass, anlage):
        anlage.registry.entities[LADESTROM].disabled_by = "integration"
        anlage.werte.pop(LADESTROM)
        p = victron_pruefung(mock_hass)
        assert p["fehlt"] == []
        assert p["probleme"] == [
            "In Home Assistant deaktiviert: DVCC-Ladestrom — bitte unter "
            "Geräte & Dienste → Victron GX aktivieren"
        ]

    def test_fehlende_stellgroessen(self, mock_hass, registry_patch):
        a = Anlage(mock_hass, **{SOLLWERT: None, LADESTROM: None})
        registry_patch.return_value = a.registry
        assert victron_pruefung(mock_hass)["fehlt"] == ["ESS-Netzsollwert", "DVCC-Ladestrom"]


# ----------------------------------------------------------------------
# Ladelimit
# ----------------------------------------------------------------------
class TestLadelimit:
    async def test_kw_wird_ampere(self, mock_hass, anlage):
        inv = _treiber(mock_hass)
        assert await inv.async_set_charge_limit(2.0) is True
        # 2000 W / 52,6 V = 38,02 A
        assert _schreibvorgaenge(mock_hass) == [(LADESTROM, 38.0)]
        assert inv._state_store.vorwert("ladestrom") == 125.0

    async def test_null_sperrt_das_laden(self, mock_hass, anlage):
        inv = _treiber(mock_hass)
        assert await inv.async_set_charge_limit(0) is True
        assert anlage.wert(LADESTROM) == 0.0

    async def test_negatives_limit_wird_null(self, mock_hass, anlage):
        inv = _treiber(mock_hass)
        assert await inv.async_set_charge_limit(-1.0) is True
        assert anlage.wert(LADESTROM) == 0.0

    async def test_auf_das_maximum_der_entitaet_begrenzt(self, mock_hass, anlage):
        anlage.werte[LADESTROM][1] = {"min": 0, "max": 100}
        inv = _treiber(mock_hass)
        assert await inv.async_set_charge_limit(50.0) is True
        assert anlage.wert(LADESTROM) == 100.0

    async def test_ohne_spannung_kein_schreiben(self, mock_hass, anlage):
        anlage.setze(SPANNUNG, "unavailable")
        inv = _treiber(mock_hass)
        assert await inv.async_set_charge_limit(2.0) is False
        assert inv.last_write_error == "Batteriespannung unbekannt"
        mock_hass.services.async_call.assert_not_called()

    async def test_ohne_spannung_wird_die_entladung_trotzdem_zurueckgenommen(
        self, mock_hass, anlage, soc_beobachter
    ):
        inv = _treiber(mock_hass)
        await inv.async_set_discharge(3.0, 30)
        anlage.setze(SPANNUNG, "unavailable")
        assert await inv.async_set_charge_limit(1.0) is False
        assert anlage.wert(SOLLWERT) == 0.0
        assert inv._state_store.vorwert("netzsollwert") is None

    async def test_unlesbarer_vorwert_verhindert_das_schreiben(self, mock_hass, anlage):
        """Ohne Vorwert könnte die Freigabe nichts zurücknehmen — dann lieber
        gar nicht eingreifen; der Executor versucht es gebremst wieder."""
        anlage.setze(LADESTROM, "unavailable")
        inv = _treiber(mock_hass)
        assert await inv.async_set_charge_limit(0) is False
        assert inv.last_write_error == "Vorwert ladestrom unlesbar"
        mock_hass.services.async_call.assert_not_called()

    async def test_nach_dem_ersten_eingriff_genuegt_der_gesicherte_vorwert(self, mock_hass, anlage):
        inv = _treiber(mock_hass)
        await inv.async_set_charge_limit(2.0)
        anlage.setze(LADESTROM, "unavailable")
        assert await inv.async_set_charge_limit(1.0) is True
        assert anlage.wert(LADESTROM) == 19.0

    async def test_nan_wird_abgelehnt(self, mock_hass, anlage):
        inv = _treiber(mock_hass)
        assert await inv.async_set_charge_limit(float("nan")) is False
        mock_hass.services.async_call.assert_not_called()

    async def test_gleicher_wert_wird_nicht_geschrieben(self, mock_hass, anlage):
        """Einstellungen landen im Speicher des GX — nicht ohne Not schreiben."""
        inv = _treiber(mock_hass)
        await inv.async_set_charge_limit(2.0)
        await inv.async_set_charge_limit(2.01)
        assert len(_schreibvorgaenge(mock_hass)) == 1

    async def test_vorwert_wird_nur_beim_ersten_eingriff_gesichert(self, mock_hass, anlage):
        inv = _treiber(mock_hass)
        await inv.async_set_charge_limit(2.0)
        await inv.async_set_charge_limit(1.0)
        assert inv._state_store.vorwert("ladestrom") == 125.0

    async def test_nimmt_eine_eigene_entladung_zurueck(self, mock_hass, anlage, soc_beobachter):
        inv = _treiber(mock_hass)
        await inv.async_set_discharge(3.0, 30)
        assert anlage.wert(SOLLWERT) == -3000.0
        assert await inv.async_set_charge_limit(1.0) is True
        assert anlage.wert(SOLLWERT) == 0.0
        soc_beobachter.abmelder.assert_called_once()

    async def test_schreibfehler(self, mock_hass, anlage):
        anlage.schreibfehler = True
        inv = _treiber(mock_hass)
        assert await inv.async_set_charge_limit(2.0) is False
        assert inv.last_write_error == "RuntimeError"


# ----------------------------------------------------------------------
# Entladung über den Netzsollwert
# ----------------------------------------------------------------------
class TestEntladung:
    async def test_einspeisung_wird_negativer_sollwert(self, mock_hass, anlage, soc_beobachter):
        inv = _treiber(mock_hass)
        assert await inv.async_set_discharge(2.346, 30) is True
        assert _schreibvorgaenge(mock_hass) == [(SOLLWERT, -2350)]
        assert inv._state_store.vorwert("netzsollwert") == 0.0

    async def test_vorzeichen_der_eingabe_egal(self, mock_hass, anlage, soc_beobachter):
        inv = _treiber(mock_hass)
        await inv.async_set_discharge(-1.0, 30)
        assert anlage.wert(SOLLWERT) == -1000.0

    async def test_auf_das_minimum_der_entitaet_begrenzt(self, mock_hass, anlage, soc_beobachter):
        anlage.werte[SOLLWERT][1] = {"min": -10000, "max": 10000}
        inv = _treiber(mock_hass)
        await inv.async_set_discharge(15.0, 30)
        assert anlage.wert(SOLLWERT) == -10000.0

    async def test_nimmt_ein_eigenes_ladelimit_zurueck(self, mock_hass, anlage, soc_beobachter):
        inv = _treiber(mock_hass)
        await inv.async_set_charge_limit(0)
        assert anlage.wert(LADESTROM) == 0.0
        await inv.async_set_discharge(2.0, 30)
        assert anlage.wert(LADESTROM) == 125.0
        assert inv._state_store.vorwert("ladestrom") is None

    async def test_am_ziel_wird_nicht_entladen(self, mock_hass, anlage, soc_beobachter):
        anlage.setze(SOC, "30")
        inv = _treiber(mock_hass)
        assert await inv.async_set_discharge(2.0, 30) is True
        mock_hass.services.async_call.assert_not_called()

    async def test_ziel_soc_nimmt_den_sollwert_zurueck(self, mock_hass, anlage, soc_beobachter):
        """Das GX kennt keinen Ziel-SOC — der Treiber hört selbst auf."""
        inv = _treiber(mock_hass)
        await inv.async_set_discharge(3.0, 40)
        assert anlage.wert(SOLLWERT) == -3000.0
        [(entities, rueckruf)] = soc_beobachter.rueckrufe
        assert entities == [SOC]

        aufgaben = []
        mock_hass.async_create_task = MagicMock(side_effect=aufgaben.append)
        anlage.setze(SOC, "45")
        rueckruf(None)
        assert aufgaben == []
        anlage.setze(SOC, "40")
        rueckruf(None)
        assert len(aufgaben) == 1
        await aufgaben[0]
        assert anlage.wert(SOLLWERT) == 0.0
        assert inv._state_store.vorwert("netzsollwert") is None
        soc_beobachter.abmelder.assert_called_once()

    async def test_nur_ein_beobachter_bei_neuem_befehl(self, mock_hass, anlage, soc_beobachter):
        inv = _treiber(mock_hass)
        await inv.async_set_discharge(3.0, 40)
        await inv.async_set_discharge(2.0, 35)
        assert len(soc_beobachter.rueckrufe) == 1
        assert inv._ziel_soc == 35
        assert anlage.wert(SOLLWERT) == -2000.0
        assert inv._state_store.vorwert("netzsollwert") == 0.0

    async def test_schreibfehler(self, mock_hass, anlage, soc_beobachter):
        anlage.schreibfehler = True
        inv = _treiber(mock_hass)
        assert await inv.async_set_discharge(2.0, 30) is False
        assert inv.last_write_error == "RuntimeError"

    async def test_ohne_ziel_gilt_die_reserve_des_geraets(self, mock_hass, anlage, soc_beobachter):
        """Ohne Ziel-SOC aus dem Plan liefe die Entladung unbeobachtet bis zum
        ESS-Minimum — weiter, auch wenn Home Assistant danach ausfällt."""
        anlage.setze(AKTIV_SOC, "25")
        inv = _treiber(mock_hass)
        await inv.async_set_discharge(2.0, None)
        assert inv._ziel_soc == 25.0
        assert len(soc_beobachter.rueckrufe) == 1

    async def test_echo_steht_noch_aus(self, mock_hass, anlage, soc_beobachter):
        """Der Zustand kommt per MQTT verzögert: Zeigt die Entität noch den
        alten Wert, zählt der zuletzt geschriebene."""
        anlage.setze(SOLLWERT, "50")
        inv = _treiber(mock_hass)
        await inv.async_set_discharge(3.0, 30)
        anlage.setze(SOLLWERT, "50")  # Echo von −3000 noch nicht da
        await inv.async_stop_forcible()
        assert _schreibvorgaenge(mock_hass)[-1] == (SOLLWERT, 50.0)

    async def test_zielruecknahme_wartet_auf_einen_laufenden_befehl(
        self, mock_hass, anlage, soc_beobachter
    ):
        """Wettlauf: Der SOC meldet das Ziel, während ein neuer Befehl noch
        schreibt. Die Rücknahme darf nicht dazwischenfallen und prüft danach
        erneut — der neue Befehl hat ein tieferes Ziel."""
        import asyncio

        inv = _treiber(mock_hass)
        await inv.async_set_discharge(3.0, 40)
        [(_, rueckruf)] = soc_beobachter.rueckrufe

        freigabe = asyncio.Event()
        original = anlage.call

        async def langsam(domain, service, data, blocking=False):
            await freigabe.wait()
            await original(domain, service, data, blocking)

        mock_hass.services.async_call = AsyncMock(side_effect=langsam)
        aufgaben = []
        mock_hass.async_create_task = MagicMock(
            side_effect=lambda c: aufgaben.append(asyncio.ensure_future(c))
        )
        befehl = asyncio.ensure_future(inv.async_set_discharge(2.0, 30))
        await asyncio.sleep(0)
        anlage.setze(SOC, "40")
        rueckruf(None)  # altes Ziel 40 — der Befehl setzt gleich 30
        await asyncio.sleep(0)
        freigabe.set()
        assert await befehl is True
        await asyncio.gather(*aufgaben)
        assert anlage.wert(SOLLWERT) == -2000.0
        assert inv._state_store.vorwert("netzsollwert") == 0.0


# ----------------------------------------------------------------------
# Freigabe
# ----------------------------------------------------------------------
class TestFreigabe:
    async def test_stellt_beide_vorwerte_wieder_her(self, mock_hass, anlage, soc_beobachter):
        anlage.setze(SOLLWERT, "50")  # Betreiber hält 50 W Bezug
        inv = _treiber(mock_hass)
        await inv.async_set_charge_limit(0)
        await inv.async_set_discharge(2.0, 30)
        mock_hass.services.async_call.reset_mock()

        assert await inv.async_stop_forcible() is True
        assert anlage.wert(SOLLWERT) == 50.0
        assert anlage.wert(LADESTROM) == 125.0
        assert inv._state_store.vorwert("netzsollwert") is None
        assert inv._state_store.vorwert("ladestrom") is None

    async def test_sollwert_vor_ladestrom(self, mock_hass, anlage, soc_beobachter):
        inv = _treiber(mock_hass)
        await inv.async_set_charge_limit(0)
        # beide Vorwerte stehen im Speicher, z. B. nach einem Neustart
        inv._state_store._data["netzsollwert"] = 0.0
        anlage.setze(SOLLWERT, "-2000")
        mock_hass.services.async_call.reset_mock()
        await inv.async_stop_forcible()
        assert [e for e, _ in _schreibvorgaenge(mock_hass)] == [SOLLWERT, LADESTROM]

    async def test_ohne_eingriff_wird_nichts_geschrieben(self, mock_hass, anlage):
        inv = _treiber(mock_hass)
        assert await inv.async_stop_forcible() is True
        mock_hass.services.async_call.assert_not_called()

    async def test_minus_eins_wird_zurueckgeschrieben(self, mock_hass, anlage):
        """HA 2026.10: Bereich −1…10000 A — „nicht begrenzen" bleibt erhalten."""
        anlage.setze(LADESTROM, "-1")
        inv = _treiber(mock_hass)
        await inv.async_set_charge_limit(1.0)
        await inv.async_stop_forcible()
        assert anlage.wert(LADESTROM) == -1.0

    async def test_minus_eins_bei_altem_bereich_wird_maximum(self, mock_hass, anlage):
        anlage.setze(LADESTROM, "-1")
        anlage.werte[LADESTROM][1] = {"min": 0, "max": 100}
        inv = _treiber(mock_hass)
        await inv.async_set_charge_limit(1.0)
        await inv.async_stop_forcible()
        assert anlage.wert(LADESTROM) == 100.0

    async def test_vorwert_ueberlebt_einen_neustart(self, mock_hass, anlage):
        store = _Store()
        inv = _treiber(mock_hass, store=store)
        await inv.async_set_charge_limit(0)
        assert store.data == {"ladestrom": 125.0}

        neu = _treiber(mock_hass, store=store)
        assert await neu.async_stop_forcible() is True
        assert anlage.wert(LADESTROM) == 125.0
        assert store.data == {}

    async def test_fehlgeschlagene_freigabe_behaelt_den_vorwert(self, mock_hass, anlage):
        inv = _treiber(mock_hass)
        await inv.async_set_charge_limit(0)
        anlage.schreibfehler = True
        assert await inv.async_stop_forcible() is False
        assert inv._state_store.vorwert("ladestrom") == 125.0
        anlage.schreibfehler = False
        assert await inv.async_stop_forcible() is True
        assert anlage.wert(LADESTROM) == 125.0

    async def test_freigabe_beendet_die_beobachtung(self, mock_hass, anlage, soc_beobachter):
        inv = _treiber(mock_hass)
        await inv.async_set_discharge(2.0, 30)
        await inv.async_stop_forcible()
        soc_beobachter.abmelder.assert_called_once()
        assert inv._ziel_soc is None


# ----------------------------------------------------------------------
# Lesewerte für den Executor
# ----------------------------------------------------------------------
class TestLesewerte:
    async def test_gesetztes_ladelimit_in_kw(self, mock_hass, anlage):
        inv = _treiber(mock_hass)
        assert await inv.async_get_charge_limit_kw() == pytest.approx(125 * 52.6 / 1000)
        anlage.setze(LADESTROM, "-1")
        assert await inv.async_get_charge_limit_kw() is None
        anlage.setze(SPANNUNG, "unavailable")
        anlage.setze(LADESTROM, "10")
        assert await inv.async_get_charge_limit_kw() is None

    def test_ladegrenze_ist_das_bms(self, mock_hass, anlage):
        """Entität bis 10000 A, BMS 80 A → 80 A × 52,6 V."""
        inv = _treiber(mock_hass)
        assert inv.get_charge_limit_max_kw() == pytest.approx(80 * 52.6 / 1000)
        anlage.setze(BMS, "unavailable")
        assert inv.get_charge_limit_max_kw() == pytest.approx(10000 * 52.6 / 1000)

    def test_keine_entladegrenze_aus_der_ess_einstellung(self, mock_hass, anlage):
        """„Max. Wechselrichterleistung" gilt je nach Venus OS je Phase — als
        Gesamtgrenze deckelte sie eine dreiphasige Anlage auf ein Drittel."""
        anlage.setze(WR_GRENZE, "4500")
        assert _treiber(mock_hass).get_max_discharge_power_kw() is None

    def test_reserve_ist_die_hoehere_soc_grenze(self, mock_hass, anlage):
        inv = _treiber(mock_hass)
        assert inv.get_backup_reserve_soc_pct() == 20.0
        anlage.setze(AKTIV_SOC, "35")  # BatteryLife hat die Grenze angehoben
        assert inv.get_backup_reserve_soc_pct() == 35.0
        anlage.setze(MIN_SOC, "unavailable")
        anlage.setze(AKTIV_SOC, "unavailable")
        assert inv.get_backup_reserve_soc_pct() is None

    def test_steuerentitaeten_fuers_panel(self, mock_hass, anlage):
        inv = _treiber(mock_hass)
        assert [(r["entity_id"], r["role"]) for r in inv.get_control_entities()] == [
            (SOLLWERT, "forcible"),
            (LADESTROM, "charge_limit"),
            (MIN_SOC, "backup_soc"),
            (ESS, "mode"),
        ]


# ----------------------------------------------------------------------
# Vorzeichen — gemessen an der echten Anlage
# ----------------------------------------------------------------------
class TestVorzeichen:
    def test_konvention(self):
        assert resolve_sign(INVERTER_TYPE_VICTRON, BATTERIE, "battery_sign") == 1
        assert resolve_sign(INVERTER_TYPE_VICTRON, NETZ, "grid_sign") == -1

    def test_hausverbrauch_aus_anlagenwerten(self, mock_hass, anlage):
        """29.10.2026, 16 Uhr: PV 0, Batterie −7262 W (entlädt), Netzzähler
        +114,8 W (Bezug) → Hausverbrauch 7,38 kW. Die AC-Lasten lagen bei
        6,75 kW — die Differenz sind Wandlerverluste, die die Formel
        PV − Batterie − Netz zum Hausverbrauch zählt."""
        cfg = {
            CONF_INVERTER_TYPE: INVERTER_TYPE_VICTRON,
            CONF_PV_POWER_SENSOR: DC_PV,
            CONF_BATTERY_POWER_SENSOR: BATTERIE,
            CONF_GRID_POWER_SENSOR: NETZ,
        }
        assert compute_house_load_kw(mock_hass, cfg) == pytest.approx(7.3768, abs=1e-3)


# ----------------------------------------------------------------------
# Netzladung (Beta): DVCC-Deckel + positiver, nachgeführter Netzsollwert
# ----------------------------------------------------------------------
MESSUNG = {
    CONF_INVERTER_TYPE: INVERTER_TYPE_VICTRON,
    CONF_PV_POWER_SENSOR: DC_PV,
    CONF_BATTERY_POWER_SENSOR: BATTERIE,
    CONF_GRID_POWER_SENSOR: NETZ,
}


def _netzlader(hass, anlage, bezug_w=500.0, batterie_w=1000.0, soc="50"):
    """Mittags, Batterie lädt 1 kW aus der PV, Haus bezieht 0,5 kW."""
    anlage.setze(NETZ, bezug_w)
    anlage.setze(BATTERIE, batterie_w)
    anlage.setze(SOC, soc)
    return _treiber(hass, **MESSUNG)


class TestNetzladung:
    async def test_deckel_und_bezug(self, mock_hass, anlage, soc_beobachter):
        inv = _netzlader(mock_hass, anlage)
        assert await inv.async_set_grid_charge(3.0, 80) is True
        # 3 kW / 52,6 V = 57,0 A; Sollwert = 0,5 + (3 − 1) − 0,2 = 2,3 kW
        assert _schreibvorgaenge(mock_hass) == [(LADESTROM, 57.0), (SOLLWERT, 2300.0)]
        assert inv._state_store.vorwert("ladestrom") == 125.0
        assert inv._state_store.vorwert("netzsollwert") == 0.0
        assert len(soc_beobachter.takte) == 1

    async def test_ladestrom_vor_sollwert(self, mock_hass, anlage, soc_beobachter):
        """Umgekehrt zöge der Multiplus einen Takt lang mit voller
        Ladeleistung aus dem Netz."""
        inv = _netzlader(mock_hass, anlage)
        await inv.async_set_grid_charge(3.0, 80)
        assert [e for e, _ in _schreibvorgaenge(mock_hass)] == [LADESTROM, SOLLWERT]

    async def test_nachfuehrung_senkt_den_bezug_wenn_die_pv_reicht(
        self, mock_hass, anlage, soc_beobachter
    ):
        """Batterie am Deckel: Ob die PV abgeregelt wird, sieht man nicht —
        also rasch zurück (halbieren), statt 0,2 kW je Takt."""
        inv = _netzlader(mock_hass, anlage)
        await inv.async_set_grid_charge(3.0, 80)
        [(takt, _)] = soc_beobachter.takte
        anlage.setze(BATTERIE, 3000.0)
        anlage.setze(NETZ, 2300.0)
        await takt(None)
        assert anlage.wert(SOLLWERT) == 1150.0

    async def test_nachfuehrung_hebt_den_bezug_wenn_die_pv_nachlaesst(
        self, mock_hass, anlage, soc_beobachter
    ):
        inv = _netzlader(mock_hass, anlage)
        await inv.async_set_grid_charge(3.0, 80)
        [(takt, _)] = soc_beobachter.takte
        anlage.setze(BATTERIE, 2000.0)  # Wolke: 1 kW fehlt
        anlage.setze(NETZ, 2300.0)
        await takt(None)
        assert anlage.wert(SOLLWERT) == 3100.0

    async def test_kleine_abweichung_schreibt_nicht(self, mock_hass, anlage, soc_beobachter):
        inv = _netzlader(mock_hass, anlage)
        await inv.async_set_grid_charge(3.0, 80)
        [(takt, _)] = soc_beobachter.takte
        anlage.setze(BATTERIE, 2750.0)
        anlage.setze(NETZ, 2300.0)
        vorher = len(_schreibvorgaenge(mock_hass))
        await takt(None)  # Soll 2350 W, 50 W neben 2300
        assert len(_schreibvorgaenge(mock_hass)) == vorher

    async def test_nie_ein_negativer_sollwert(self, mock_hass, anlage, soc_beobachter):
        """Viel PV, Einspeisung: Die Netzladung speist nicht selbst ein."""
        inv = _netzlader(mock_hass, anlage, bezug_w=-2000.0, batterie_w=3000.0)
        await inv.async_set_grid_charge(3.0, 80)
        assert anlage.wert(SOLLWERT) == 0.0

    async def test_ohne_messung_kein_bezug(self, mock_hass, anlage, soc_beobachter):
        inv = _netzlader(mock_hass, anlage)
        anlage.setze(NETZ, "unavailable")
        anlage.setze(SOLLWERT, "50")
        assert await inv.async_set_grid_charge(3.0, 80) is True
        assert anlage.wert(SOLLWERT) == 0.0

    async def test_auf_den_bereich_des_ladestroms_begrenzt(self, mock_hass, anlage, soc_beobachter):
        anlage.werte[LADESTROM][1] = {"min": -1, "max": 50}
        inv = _netzlader(mock_hass, anlage)
        await inv.async_set_grid_charge(5.0, 80)
        assert anlage.wert(LADESTROM) == 50.0

    async def test_ohne_spannung_kein_bezug(self, mock_hass, anlage, soc_beobachter):
        inv = _netzlader(mock_hass, anlage)
        await inv.async_set_grid_charge(3.0, 80)
        anlage.setze(SPANNUNG, "unavailable")
        assert await inv.async_set_grid_charge(2.0, 80) is False
        assert inv.last_write_error == "Batteriespannung unbekannt"
        assert anlage.wert(SOLLWERT) == 0.0
        soc_beobachter.takt_abmelder.assert_called_once()

    async def test_am_ziel_wird_nicht_geladen(self, mock_hass, anlage, soc_beobachter):
        inv = _netzlader(mock_hass, anlage, soc="80")
        assert await inv.async_set_grid_charge(3.0, 80) is True
        mock_hass.services.async_call.assert_not_called()

    async def test_ziel_nimmt_beide_werte_zurueck(self, mock_hass, anlage, soc_beobachter):
        inv = _netzlader(mock_hass, anlage)
        await inv.async_set_grid_charge(3.0, 80)
        [(entities, rueckruf)] = soc_beobachter.rueckrufe
        assert entities == [SOC]
        anlage.setze(SOC, "79")
        rueckruf(None)
        mock_hass.async_create_task.assert_not_called()
        anlage.setze(SOC, "80")
        rueckruf(None)
        await mock_hass.async_create_task.call_args.args[0]
        assert anlage.wert(SOLLWERT) == 0.0
        assert anlage.wert(LADESTROM) == 125.0
        soc_beobachter.takt_abmelder.assert_called_once()

    async def test_ohne_ziel_bis_voll(self, mock_hass, anlage, soc_beobachter):
        inv = _netzlader(mock_hass, anlage)
        await inv.async_set_grid_charge(3.0, None)
        assert inv._ziel_soc == 100.0 and inv._ziel_laden is True

    async def test_entladung_danach_beendet_die_nachfuehrung(
        self, mock_hass, anlage, soc_beobachter
    ):
        inv = _netzlader(mock_hass, anlage)
        await inv.async_set_grid_charge(3.0, 80)
        await inv.async_set_discharge(2.0, 30)
        soc_beobachter.takt_abmelder.assert_called_once()
        assert anlage.wert(SOLLWERT) == -2000.0
        assert anlage.wert(LADESTROM) == 125.0
        assert inv._ziel_laden is False and inv._ziel_soc == 30

    async def test_ladelimit_danach_nimmt_den_bezug_zurueck(
        self, mock_hass, anlage, soc_beobachter
    ):
        inv = _netzlader(mock_hass, anlage)
        await inv.async_set_grid_charge(3.0, 80)
        await inv.async_set_charge_limit(0)
        assert anlage.wert(SOLLWERT) == 0.0
        assert anlage.wert(LADESTROM) == 0.0
        soc_beobachter.takt_abmelder.assert_called_once()

    async def test_freigabe_stellt_alles_wieder_her(self, mock_hass, anlage, soc_beobachter):
        inv = _netzlader(mock_hass, anlage)
        await inv.async_set_grid_charge(3.0, 80)
        assert await inv.async_stop_forcible() is True
        assert anlage.wert(SOLLWERT) == 0.0
        assert anlage.wert(LADESTROM) == 125.0
        assert inv._netzladung_kw is None

    async def test_takt_nach_der_freigabe_schreibt_nichts(
        self, mock_hass, anlage, soc_beobachter
    ):
        inv = _netzlader(mock_hass, anlage)
        await inv.async_set_grid_charge(3.0, 80)
        [(takt, _)] = soc_beobachter.takte
        await inv.async_stop_forcible()
        vorher = len(_schreibvorgaenge(mock_hass))
        await takt(None)
        assert len(_schreibvorgaenge(mock_hass)) == vorher

    async def test_neuer_befehl_nur_ein_takt(self, mock_hass, anlage, soc_beobachter):
        inv = _netzlader(mock_hass, anlage)
        await inv.async_set_grid_charge(3.0, 80)
        await inv.async_set_grid_charge(2.0, 85)
        assert len(soc_beobachter.takte) == 1
        assert len(soc_beobachter.rueckrufe) == 1
        assert anlage.wert(LADESTROM) == pytest.approx(38.0)

    async def test_schreibfehler(self, mock_hass, anlage, soc_beobachter):
        anlage.schreibfehler = True
        inv = _netzlader(mock_hass, anlage)
        assert await inv.async_set_grid_charge(3.0, 80) is False
        assert inv.last_write_error == "RuntimeError"
        assert inv._ziel_soc is None and inv._netzladung_kw is None
        soc_beobachter.abmelder.assert_called_once()
        assert soc_beobachter.takte == []

    async def test_kein_aufschaukeln_wenn_die_batterie_nicht_folgt(
        self, mock_hass, anlage, soc_beobachter
    ):
        """Absorptionsphase: Die Batterie nimmt nur 1,5 kW, das ESS hält den
        Sollwert durch Abregeln der PV. Ohne Sperre stiege er Takt für Takt,
        bis alle PV abgeregelt wäre."""
        inv = _netzlader(mock_hass, anlage)
        await inv.async_set_grid_charge(3.0, 80)  # Sollwert 2,3 kW
        [(takt, _)] = soc_beobachter.takte
        anlage.setze(BATTERIE, 1500.0)
        sollwerte = []
        for _ in range(30):
            anlage.setze(NETZ, anlage.wert(SOLLWERT))  # ESS hält den Sollwert
            await takt(None)
            sollwerte.append(anlage.wert(SOLLWERT))
        assert max(sollwerte) <= 2300.0
        assert sollwerte[0] == 500.0  # Anhebung (1,8 kW) zurückgenommen

    async def test_nach_der_sperre_wird_wieder_angehoben(self, mock_hass, anlage, soc_beobachter):
        """Folgt die Batterie wieder (Wolke statt Absorption), steigt der
        Sollwert nach der Sperre in Schritten von höchstens 1 kW."""
        from custom_components.eeg_energy_optimizer.inverter.victron import (
            NETZLADEN_SPERRE_TAKTE,
        )

        inv = _netzlader(mock_hass, anlage)
        await inv.async_set_grid_charge(3.0, 80)
        [(takt, _)] = soc_beobachter.takte
        anlage.setze(BATTERIE, 1000.0)
        anlage.setze(NETZ, 2300.0)
        await takt(None)  # nicht gefolgt → 0,5 kW, gesperrt
        for _ in range(NETZLADEN_SPERRE_TAKTE - 1):
            await takt(None)
        assert anlage.wert(SOLLWERT) == 500.0
        await takt(None)
        assert anlage.wert(SOLLWERT) == 1500.0

    async def test_takt_ohne_messung_haelt(self, mock_hass, anlage, soc_beobachter):
        inv = _netzlader(mock_hass, anlage)
        await inv.async_set_grid_charge(3.0, 80)
        [(takt, _)] = soc_beobachter.takte
        anlage.setze(NETZ, "unavailable")
        await takt(None)
        assert anlage.wert(SOLLWERT) == 2300.0

    async def test_sollwert_auf_den_bereich_begrenzt(self, mock_hass, anlage, soc_beobachter):
        anlage.werte[SOLLWERT][1] = {"min": -1000, "max": 1000}
        inv = _netzlader(mock_hass, anlage)
        await inv.async_set_grid_charge(3.0, 80)
        assert anlage.wert(SOLLWERT) == 1000.0

    async def test_null_kw_ist_eine_freigabe(self, mock_hass, anlage, soc_beobachter):
        """Die Netzgrenze lässt nichts zu: 0 A sperrte auch das Laden aus PV."""
        inv = _netzlader(mock_hass, anlage)
        await inv.async_set_grid_charge(3.0, 80)
        assert await inv.async_set_grid_charge(0.0, 80) is True
        assert anlage.wert(LADESTROM) == 125.0
        assert anlage.wert(SOLLWERT) == 0.0
        soc_beobachter.takt_abmelder.assert_called_once()

    async def test_teilfehler_beim_start_laesst_nichts_stehen(
        self, mock_hass, anlage, soc_beobachter
    ):
        """Ladestrom geschrieben, Sollwert scheitert: Der Executor hält den
        Befehl für gescheitert — der Deckel darf nicht bleiben."""
        inv = _netzlader(mock_hass, anlage)
        original = anlage.call

        async def sollwert_scheitert(domain, service, data, blocking=False):
            if data["entity_id"] == SOLLWERT and data["value"] > 0:
                raise RuntimeError("MQTT Zeitüberschreitung")
            await original(domain, service, data, blocking)

        mock_hass.services.async_call = AsyncMock(side_effect=sollwert_scheitert)
        assert await inv.async_set_grid_charge(3.0, 80) is False
        assert anlage.wert(LADESTROM) == 125.0
        assert anlage.wert(SOLLWERT) == 0.0
        assert inv._ziel_soc is None and inv._netzladung_kw is None
        assert inv._state_store.vorwert("ladestrom") is None

    async def test_unlesbarer_sollwert_verhindert_auch_den_deckel(
        self, mock_hass, anlage, soc_beobachter
    ):
        anlage.setze(SOLLWERT, "unavailable")
        inv = _netzlader(mock_hass, anlage)
        assert await inv.async_set_grid_charge(3.0, 80) is False
        assert inv.last_write_error == "Vorwert netzsollwert unlesbar"
        mock_hass.services.async_call.assert_not_called()

    async def test_fehlgeschlagene_zielruecknahme_wird_im_takt_wiederholt(
        self, mock_hass, anlage, soc_beobachter
    ):
        """Am Ziel ändert sich der SOC womöglich nicht mehr — ein stehender
        positiver Sollwert kaufte bis zum Slotwechsel weiter."""
        inv = _netzlader(mock_hass, anlage)
        await inv.async_set_grid_charge(3.0, 80)
        [(takt, _)] = soc_beobachter.takte
        [(_, rueckruf)] = soc_beobachter.rueckrufe
        anlage.setze(SOC, "80")
        anlage.schreibfehler = True
        rueckruf(None)
        await mock_hass.async_create_task.call_args.args[0]
        assert anlage.wert(SOLLWERT) == 2300.0
        soc_beobachter.takt_abmelder.assert_not_called()
        anlage.schreibfehler = False
        await takt(None)
        assert anlage.wert(SOLLWERT) == 0.0
        assert anlage.wert(LADESTROM) == 125.0
        soc_beobachter.takt_abmelder.assert_called_once()
