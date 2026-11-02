"""Tests für den Sigenergy-SigenStor-Treiber (HA-Integration ``sigen``).

Der Treiber stellt über Standardentitäten der HACS-Integration
TypQxQ/Sigenergy-Local-Modbus: Schalter „Remote EMS", Auswahl
„Remote EMS Control Mode", Zahlen „ESS Max Charging/Discharging Limit".
Die Tests prüfen den Ablauf (einschalten → auf Verfügbarkeit warten →
Limit → Modus → das andere Limit auf Nennleistung), die Freigabe
(Eigenverbrauch → beide Limits auf Nennleistung → Schalter aus), die
Registry-Auflösung der ab Werk deaktivierten Steuerentitäten und die
Vorzeichenkonvention der Sensoren.
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
    INVERTER_SIGN_CONVENTIONS,
    INVERTER_TYPE_SIGENERGY,
)
from custom_components.eeg_energy_optimizer.inverter.base import InverterBase
from custom_components.eeg_energy_optimizer.inverter.sigenergy import (
    MODE_COMMAND_CHARGING_PV_FIRST,
    MODE_COMMAND_DISCHARGING_ESS_FIRST,
    MODE_SELF_CONSUMPTION,
    SIGEN_DOMAIN,
    SIGEN_ENTITY_DEFAULTS,
    SIGEN_REQUIRED_CONTROLS,
    SigenergyInverter,
    find_sigen_control_entity,
    sigen_control_entity_status,
)
from custom_components.eeg_energy_optimizer.power_readings import (
    compute_house_load_kw,
    resolve_sign,
)

SWITCH = SIGEN_ENTITY_DEFAULTS["remote_ems_switch"]
MODE = SIGEN_ENTITY_DEFAULTS["remote_ems_mode"]
CHARGE_LIMIT = SIGEN_ENTITY_DEFAULTS["ess_max_charging_limit"]
DISCHARGE_LIMIT = SIGEN_ENTITY_DEFAULTS["ess_max_discharging_limit"]
CUT_OFF = SIGEN_ENTITY_DEFAULTS["ess_discharge_cut_off_soc"]
BACKUP = SIGEN_ENTITY_DEFAULTS["ess_backup_soc"]
RATED_CHARGE = SIGEN_ENTITY_DEFAULTS["ess_rated_charging_power"]
RATED_DISCHARGE = SIGEN_ENTITY_DEFAULTS["ess_rated_discharging_power"]


def _states(werte: dict[str, str], attribute: dict[str, dict] | None = None):
    """State-Getter, der je Entity einen eigenen Wert liefert (None = fehlt)."""
    attribute = attribute or {}

    def _get(entity_id):
        if entity_id not in werte:
            return None
        st = MagicMock()
        st.state = werte[entity_id]
        st.attributes = attribute.get(entity_id, {})
        return st

    return _get


def _calls(mock_hass) -> list[tuple[str, str, dict]]:
    """(domain, service, payload) aller aufgezeichneten Service-Aufrufe, in Reihenfolge."""
    out = []
    for c in mock_hass.services.async_call.call_args_list:
        domain, service = c.args[0], c.args[1]
        payload = c.args[2] if len(c.args) > 2 else c.kwargs.get("service_data", {})
        out.append((domain, service, payload))
    return out


# Nennleistungen der Testanlage (SigenStor EC 25.0 TP) und ihre Einheit.
NENN = {RATED_CHARGE: "20.0", RATED_DISCHARGE: "21.6"}
NENN_ATTR = {RATED_CHARGE: {"unit_of_measurement": "kW"}, RATED_DISCHARGE: {"unit_of_measurement": "kW"}}


@pytest.fixture
def inverter(mock_hass):
    # Alles da, Schalter an, Auswahl verfügbar — der Normalfall im Betrieb.
    mock_hass.states.get.side_effect = _states({
        SWITCH: "on",
        MODE: MODE_SELF_CONSUMPTION,
        CHARGE_LIMIT: "20.0",
        DISCHARGE_LIMIT: "21.6",
        **NENN,
    }, NENN_ATTR)
    mock_hass.states.async_all.return_value = []
    return SigenergyInverter(mock_hass, {})


class TestBasis:
    def test_ist_inverter_base(self, inverter):
        assert isinstance(inverter, InverterBase)
        assert issubclass(SigenergyInverter, InverterBase)

    def test_fahrplan_steuerung_freigegeben(self, inverter):
        assert inverter.supports_schedule_control is True


class TestLadelimit:
    async def test_limit_dann_modus(self, inverter, mock_hass):
        """Reihenfolge: Limit VOR dem Moduswechsel — es darf nie ein Moment
        mit einem alten, höheren Limit laufen."""
        assert await inverter.async_set_charge_limit(2.5) is True

        calls = _calls(mock_hass)
        # Schalter war an, Auswahl verfügbar → kein turn_on nötig
        assert all(c[1] != "turn_on" for c in calls)
        assert calls[0] == ("number", "set_value", {"entity_id": CHARGE_LIMIT, "value": 2.5})
        assert calls[1] == ("select", "select_option", {"entity_id": MODE, "option": MODE_SELF_CONSUMPTION})
        # Das Entladelimit wirkt global (V2.9) — ein Rest aus einer Entladung
        # deckelte sonst den Eigenverbrauch. Erst nach dem Moduswechsel.
        assert calls[2] == ("number", "set_value", {"entity_id": DISCHARGE_LIMIT, "value": 21.6})
        assert len(calls) == 3

    async def test_ladelimit_schaltet_nie_in_einen_ladebefehl(self, inverter, mock_hass):
        """Die Lehre aus dem Feldbefund vom 11.09.2026.

        „Command Charging (PV First)" ist ein Ladebefehl, kein Limit: Es lädt
        auf den Sollwert und holt die Differenz aus dem Netz, wenn die Sonne
        nicht reicht. Eine Anlage lud damit bei 0,00 kW PV ihre Batterie von
        8,5 % auf 22 % — 5,35 kWh aus dem Netz, während „Laden begrenzt auf
        3,8 kW" im Panel stand. Ein Limit darf nur begrenzen, nie laden.
        """
        for kw in (0.0, 2.5, 3.8):
            mock_hass.services.async_call.reset_mock()
            await inverter.async_set_charge_limit(kw)
            modi = [c[2].get("option") for c in _calls(mock_hass) if c[1] == "select_option"]
            assert modi == [MODE_SELF_CONSUMPTION], f"{kw} kW -> {modi}"
            assert not any("Command Charging" in str(m) for m in modi)

    async def test_null_sperrt_das_laden(self, inverter, mock_hass):
        assert await inverter.async_set_charge_limit(0.0) is True
        payload = [c for c in _calls(mock_hass) if c[1] == "set_value"][0][2]
        assert payload["value"] == 0.0

    async def test_negativ_wird_zu_null(self, inverter, mock_hass):
        await inverter.async_set_charge_limit(-3.0)
        payload = [c for c in _calls(mock_hass) if c[1] == "set_value"][0][2]
        assert payload["value"] == 0.0

    async def test_kw_werden_auf_drei_stellen_gerundet(self, inverter, mock_hass):
        """Die Zahl-Entität hat Schritt 0,001 kW — mehr Stellen lehnt HA ab."""
        await inverter.async_set_charge_limit(1.23456789)
        payload = [c for c in _calls(mock_hass) if c[1] == "set_value"][0][2]
        assert payload["value"] == 1.235

    async def test_fehler_liefert_false(self, inverter, mock_hass):
        mock_hass.services.async_call = AsyncMock(side_effect=RuntimeError("modbus"))
        assert await inverter.async_set_charge_limit(1.0) is False
        assert inverter.last_write_error == "RuntimeError"

    async def test_ohne_nennleistung_das_maximum_der_entitaet(self, mock_hass):
        """Nennleistungs-Sensoren deaktiviert: zurück auf das ``max`` der
        Zahl-Entität — das Gerät beschneidet es selbst auf seine Nennleistung."""
        mock_hass.states.async_all.return_value = []
        mock_hass.states.get.side_effect = _states(
            {SWITCH: "on", MODE: MODE_SELF_CONSUMPTION, DISCHARGE_LIMIT: "3.0"},
            {DISCHARGE_LIMIT: {"max": 100.0}},
        )
        assert await SigenergyInverter(mock_hass, {}).async_set_charge_limit(1.0) is True
        assert _calls(mock_hass)[-1] == ("number", "set_value", {"entity_id": DISCHARGE_LIMIT, "value": 100.0})

    async def test_nennleistung_unbekannt_scheitert_laut(self, mock_hass):
        mock_hass.states.async_all.return_value = []
        mock_hass.states.get.side_effect = _states({SWITCH: "on", MODE: MODE_SELF_CONSUMPTION})
        inv = SigenergyInverter(mock_hass, {})
        assert await inv.async_set_charge_limit(1.0) is False
        assert inv.last_write_error == "Nennleistung unbekannt"


class TestRemoteEmsEinschalten:
    async def test_schalter_aus_wird_eingeschaltet_und_auf_auswahl_gewartet(self, mock_hass):
        """HA überspringt Service-Calls an nicht verfügbare Entitäten still.
        Der Treiber schaltet deshalb ein und wartet, bis die Auswahl
        verfügbar ist, bevor er den Modus setzt."""
        zustand = {SWITCH: "off", MODE: "unavailable", CHARGE_LIMIT: "0", **NENN}
        mock_hass.states.get.side_effect = _states(zustand, NENN_ATTR)
        mock_hass.states.async_all.return_value = []
        inv = SigenergyInverter(mock_hass, {})

        async def _turn_on(domain, service, data, **kw):
            if service == "turn_on":
                zustand[SWITCH] = "on"   # die Auswahl wird erst mit dem Refresh verfügbar

        mock_hass.services.async_call = AsyncMock(side_effect=_turn_on)

        def _refresh(*_):
            # Der Refresh der Integration kommt während des Wartens an.
            zustand[MODE] = MODE_SELF_CONSUMPTION

        with patch("custom_components.eeg_energy_optimizer.inverter.sigenergy.asyncio.sleep", new=AsyncMock(side_effect=_refresh)) as schlaf:
            assert await inv.async_set_charge_limit(1.0) is True

        calls = _calls(mock_hass)
        assert calls[0] == ("switch", "turn_on", {"entity_id": SWITCH})
        assert calls[1][1] == "set_value"
        assert calls[2] == ("select", "select_option", {"entity_id": MODE, "option": MODE_SELF_CONSUMPTION})
        # Ein Wartezyklus, weil die Auswahl erst nach dem turn_on verfügbar war
        assert schlaf.await_count == 1

    async def test_wartezeit_ist_begrenzt(self, mock_hass, caplog):
        """Nimmt das Gerät den Schalter nicht an, läuft der Treiber nicht ewig —
        er warnt und versucht den Befehl trotzdem (der Executor zählt den Fehler)."""
        mock_hass.states.get.side_effect = _states({SWITCH: "off", MODE: "unavailable"})
        mock_hass.states.async_all.return_value = []
        inv = SigenergyInverter(mock_hass, {})

        with patch("custom_components.eeg_energy_optimizer.inverter.sigenergy.asyncio.sleep", new=AsyncMock()) as schlaf:
            await inv.async_set_charge_limit(1.0)

        assert schlaf.await_count == 20  # 10 s / 0,5 s
        assert "noch nicht verfügbar" in caplog.text


class TestEntladung:
    async def test_limit_dann_entlademodus(self, inverter, mock_hass):
        assert await inverter.async_set_discharge(4.2, target_soc=30.0) is True
        calls = _calls(mock_hass)
        assert calls[0] == ("number", "set_value", {"entity_id": DISCHARGE_LIMIT, "value": 4.2})
        assert calls[1] == ("select", "select_option", {"entity_id": MODE, "option": MODE_COMMAND_DISCHARGING_ESS_FIRST})
        # Ein Ladelimit 0 aus dem Slot davor gilt global weiter — zurück auf
        # die Nennleistung, erst im Entlademodus.
        assert calls[2] == ("number", "set_value", {"entity_id": CHARGE_LIMIT, "value": 20.0})

    async def test_ziel_soc_wird_nicht_ans_geraet_geschrieben(self, inverter, mock_hass):
        """Den Cut-Off (40048) schreiben wir nicht, solange sein Verhalten am
        Gerät ungeprüft ist — das Ziel setzt der Executor durch."""
        await inverter.async_set_discharge(3.0, target_soc=25.0)
        entities = {c[2].get("entity_id") for c in _calls(mock_hass)}
        assert CUT_OFF not in entities and BACKUP not in entities

    async def test_negative_leistung_wird_betrag(self, inverter, mock_hass):
        await inverter.async_set_discharge(-3.0)
        payload = [c for c in _calls(mock_hass) if c[1] == "set_value"][0][2]
        assert payload["value"] == 3.0

    async def test_nan_schreibt_nichts(self, inverter, mock_hass):
        assert await inverter.async_set_discharge(float("nan"), 30.0) is False
        assert _calls(mock_hass) == []


class TestFreigabe:
    async def test_eigenverbrauch_limits_dann_schalter_aus(self, inverter, mock_hass):
        """Erst der Modus, dann beide Limits auf Nennleistung, dann der
        Schalter. Die Limits wirken laut V2.9 global — auch im eigenen EMS
        des Geräts nach dem Ausschalten. Ein Ladelimit 0 aus dem Fahrplan
        sperrte sonst das Laden weiter, ein Entladelimit deckelte den
        Eigenverbrauch."""
        assert await inverter.async_stop_forcible() is True
        calls = _calls(mock_hass)
        assert calls == [
            ("select", "select_option", {"entity_id": MODE, "option": MODE_SELF_CONSUMPTION}),
            ("number", "set_value", {"entity_id": CHARGE_LIMIT, "value": 20.0}),
            ("number", "set_value", {"entity_id": DISCHARGE_LIMIT, "value": 21.6}),
            ("switch", "turn_off", {"entity_id": SWITCH}),
        ]

    async def test_limit_fehlschlag_laesst_remote_ems_an(self, inverter, mock_hass):
        """Scheitert das Zurücksetzen eines Limits, bleibt der Schalter an:
        Aus brächte bei stehenden Limits nichts, und der Executor wiederholt
        die Freigabe."""
        async def _aufruf(domain, service, data, **kw):
            if domain == "number":
                raise RuntimeError("modbus")

        mock_hass.services.async_call = AsyncMock(side_effect=_aufruf)
        assert await inverter.async_stop_forcible() is False
        assert inverter.last_write_error == "RuntimeError"
        assert all(c[1] != "turn_off" for c in _calls(mock_hass))

    async def test_schalter_schon_aus_nichts_zu_tun(self, mock_hass):
        mock_hass.states.get.side_effect = _states({SWITCH: "off", MODE: "unavailable"})
        mock_hass.states.async_all.return_value = []
        inv = SigenergyInverter(mock_hass, {})
        assert await inv.async_stop_forcible() is True
        assert _calls(mock_hass) == []

    async def test_auswahl_nicht_verfuegbar_trotzdem_ausschalten(self, mock_hass):
        """Schalter an, Auswahl (noch) nicht verfügbar → kein select_option
        (ginge ins Leere), aber das Ausschalten muss passieren."""
        mock_hass.states.get.side_effect = _states({SWITCH: "on", MODE: "unavailable", **NENN}, NENN_ATTR)
        mock_hass.states.async_all.return_value = []
        inv = SigenergyInverter(mock_hass, {})
        assert await inv.async_stop_forcible() is True
        assert _calls(mock_hass) == [
            ("number", "set_value", {"entity_id": CHARGE_LIMIT, "value": 20.0}),
            ("number", "set_value", {"entity_id": DISCHARGE_LIMIT, "value": 21.6}),
            ("switch", "turn_off", {"entity_id": SWITCH}),
        ]


SOC = "sensor.sigen_plant_battery_state_of_charge"


@pytest.fixture
def soc_beobachter():
    """``async_track_state_change_event`` abfangen: liefert die Rückrufe.

    ``homeassistant.core`` wird mit ersetzt: Andere Testmodule legen dort
    einen MagicMock ab, dessen ``callback`` den Rückruf verschlucken würde.
    """
    rueckrufe = []
    abmelder = MagicMock()

    def _track(hass, entities, rueckruf):
        rueckrufe.append((list(entities), rueckruf))
        return abmelder

    event = ModuleType("homeassistant.helpers.event")
    event.async_track_state_change_event = _track
    core = ModuleType("homeassistant.core")
    core.callback = lambda func: func
    with patch.dict(sys.modules, {"homeassistant.helpers.event": event, "homeassistant.core": core}):
        yield SimpleNamespace(rueckrufe=rueckrufe, abmelder=abmelder)


def _netzlader(mock_hass, soc="50", **zustand):
    werte = {SWITCH: "on", MODE: MODE_SELF_CONSUMPTION, SOC: soc, **NENN, **zustand}
    mock_hass.states.get.side_effect = _states(werte, NENN_ATTR)
    mock_hass.states.async_all.return_value = []
    return SigenergyInverter(mock_hass, {CONF_BATTERY_SOC_SENSOR: SOC}), werte


FREIGABE = [
    ("select", "select_option", {"entity_id": MODE, "option": MODE_SELF_CONSUMPTION}),
    ("number", "set_value", {"entity_id": CHARGE_LIMIT, "value": 20.0}),
    ("number", "set_value", {"entity_id": DISCHARGE_LIMIT, "value": 21.6}),
    ("switch", "turn_off", {"entity_id": SWITCH}),
]


class TestNetzladung:
    """Beta „Batterie aus dem Netz laden": „Command Charging (PV First)" mit
    dem Ladelimit als Leistung — derselbe Modus, der am 11.09.2026 aus dem
    Netz geladen hat, diesmal gewollt. Das Gerät stoppt nicht am Ziel-SOC."""

    def test_kann_netzladen(self, inverter):
        assert inverter.supports_grid_charge is True

    async def test_limit_vor_modus(self, mock_hass, soc_beobachter):
        """Erst das Limit, dann der Ladebefehl — umgekehrt liefe er einen
        Moment mit dem alten, höheren Limit aus dem Netz."""
        inv, _ = _netzlader(mock_hass)
        assert await inv.async_set_grid_charge(3.0, 80.0) is True
        assert _calls(mock_hass) == [
            ("number", "set_value", {"entity_id": CHARGE_LIMIT, "value": 3.0}),
            ("select", "select_option", {"entity_id": MODE, "option": MODE_COMMAND_CHARGING_PV_FIRST}),
        ]
        assert inv.last_write_error is None

    async def test_ziel_erreicht_schreibt_nichts(self, mock_hass, soc_beobachter):
        inv, _ = _netzlader(mock_hass, soc="80")
        assert await inv.async_set_grid_charge(3.0, 80.0) is True
        assert _calls(mock_hass) == []
        assert soc_beobachter.rueckrufe == []

    async def test_ziel_erreicht_beendet_eine_laufende_netzladung(self, mock_hass, soc_beobachter):
        inv, werte = _netzlader(mock_hass)
        await inv.async_set_grid_charge(3.0, 80.0)
        mock_hass.services.async_call.reset_mock()
        werte[SOC] = "81"
        assert await inv.async_set_grid_charge(3.0, 80.0) is True
        assert _calls(mock_hass) == FREIGABE

    async def test_unter_dem_minimum_wie_freigabe(self, mock_hass, soc_beobachter):
        """Nichts aus dem Netz: freigeben. Ein Ladebefehl mit Limit 0
        sperrte auch das Laden aus PV."""
        inv, _ = _netzlader(mock_hass)
        assert await inv.async_set_grid_charge(0.03, 80.0) is True
        assert _calls(mock_hass) == FREIGABE
        assert soc_beobachter.rueckrufe == []

    async def test_beobachter_gibt_am_ziel_frei(self, mock_hass, soc_beobachter):
        inv, werte = _netzlader(mock_hass)
        await inv.async_set_grid_charge(3.0, 80.0)
        [(entities, rueckruf)] = soc_beobachter.rueckrufe
        assert entities == [SOC]
        mock_hass.services.async_call.reset_mock()
        werte[SOC] = "79"
        rueckruf(None)
        mock_hass.async_create_task.assert_not_called()
        werte[SOC] = "80"
        rueckruf(None)
        await mock_hass.async_create_task.call_args.args[0]
        assert _calls(mock_hass) == FREIGABE
        soc_beobachter.abmelder.assert_called_once()
        assert inv._befehl is None

    async def test_ohne_ziel_bis_voll(self, mock_hass, soc_beobachter):
        inv, _ = _netzlader(mock_hass)
        assert await inv.async_set_grid_charge(3.0, None) is True
        assert inv._ziel_soc == 100.0

    async def test_nan_schreibt_nichts(self, mock_hass, soc_beobachter):
        inv, _ = _netzlader(mock_hass)
        assert await inv.async_set_grid_charge(float("nan"), 80.0) is False
        assert await inv.async_set_grid_charge(3.0, float("inf")) is False
        assert inv.last_write_error == "Sollwert nicht endlich"
        assert _calls(mock_hass) == []

    async def test_dienstfehler_meldet_false_und_beobachtet_weiter(self, mock_hass, soc_beobachter):
        """Ein Moduswechsel kann in der Integration bis 60 s warten und
        trotzdem am Gerät angekommen sein — dann soll am Ziel jemand anhalten."""
        inv, _ = _netzlader(mock_hass)
        mock_hass.services.async_call = AsyncMock(side_effect=RuntimeError("timeout"))
        assert await inv.async_set_grid_charge(3.0, 80.0) is False
        assert inv.last_write_error == "RuntimeError"
        assert len(soc_beobachter.rueckrufe) == 1

    async def test_ladelimit_danach_beendet_die_beobachtung(self, mock_hass, soc_beobachter):
        inv, _ = _netzlader(mock_hass)
        await inv.async_set_grid_charge(3.0, 80.0)
        await inv.async_set_charge_limit(1.0)
        soc_beobachter.abmelder.assert_called_once()
        assert inv._ziel_soc is None


class TestVerfuegbarkeit:
    def test_geladen(self, inverter, mock_hass):
        entry = MagicMock()
        entry.state.value = "loaded"
        mock_hass.config_entries.async_entries.return_value = [entry]
        assert inverter.is_available is True
        mock_hass.config_entries.async_entries.assert_called_with(SIGEN_DOMAIN)

    def test_nicht_geladen(self, inverter, mock_hass):
        entry = MagicMock()
        entry.state.value = "setup_retry"
        mock_hass.config_entries.async_entries.return_value = [entry]
        assert inverter.is_available is False

    def test_ohne_eintrag(self, inverter, mock_hass):
        mock_hass.config_entries.async_entries.return_value = []
        assert inverter.is_available is False


class TestFahrplanSchnittstelle:
    async def test_ladelimit_im_eigenverbrauch_lesbar(self, mock_hass):
        """Dort setzen wir es, dort gilt es. In den Entlademodi ist Register
        40032 wirkungslos — sein Wert wäre kein Limit, und None lässt den
        Executor auf den Planwert zurückfallen."""
        mock_hass.states.async_all.return_value = []
        mock_hass.states.get.side_effect = _states({MODE: MODE_SELF_CONSUMPTION, CHARGE_LIMIT: "5.0"})
        assert await SigenergyInverter(mock_hass, {}).async_get_charge_limit_kw() == 5.0

        mock_hass.states.get.side_effect = _states({MODE: MODE_COMMAND_DISCHARGING_ESS_FIRST, CHARGE_LIMIT: "5.0"})
        assert await SigenergyInverter(mock_hass, {}).async_get_charge_limit_kw() is None

    def test_maxima_aus_den_nennleistungs_sensoren(self, mock_hass):
        mock_hass.states.async_all.return_value = []
        mock_hass.states.get.side_effect = _states(
            {RATED_CHARGE: "20.0", RATED_DISCHARGE: "21.6"},
            {RATED_CHARGE: {"unit_of_measurement": "kW"}, RATED_DISCHARGE: {"unit_of_measurement": "kW"}},
        )
        inv = SigenergyInverter(mock_hass, {})
        assert inv.get_charge_limit_max_kw() == 20.0
        assert inv.get_max_discharge_power_kw() == 21.6

    def test_maxima_unbekannt_ohne_sensoren(self, mock_hass):
        mock_hass.states.async_all.return_value = []
        mock_hass.states.get.side_effect = _states({})
        inv = SigenergyInverter(mock_hass, {})
        assert inv.get_charge_limit_max_kw() is None
        assert inv.get_max_discharge_power_kw() is None

    def test_reserve_ist_die_hoehere_geraetegrenze(self, mock_hass):
        mock_hass.states.async_all.return_value = []
        mock_hass.states.get.side_effect = _states({BACKUP: "10", CUT_OFF: "15"})
        assert SigenergyInverter(mock_hass, {}).get_backup_reserve_soc_pct() == 15.0

    def test_reserve_none_wenn_entitaeten_deaktiviert(self, mock_hass):
        """Beide Grenzen sind ab Werk deaktiviert → kein State → None; der
        Fahrplan rechnet dann allein mit dem konfigurierten Mindest-Ladestand."""
        mock_hass.states.async_all.return_value = []
        mock_hass.states.get.side_effect = _states({})
        assert SigenergyInverter(mock_hass, {}).get_backup_reserve_soc_pct() is None

    def test_control_entities_nur_vorhandene(self, inverter):
        rows = inverter.get_control_entities()
        ids = {r["entity_id"] for r in rows}
        assert {SWITCH, MODE, CHARGE_LIMIT, DISCHARGE_LIMIT} <= ids
        assert CUT_OFF not in ids  # nicht in der State-Machine → nicht gelistet
        assert {r["role"] for r in rows} <= {"mode", "charge_limit", "discharge_limit", "backup_soc"}


class _FakeState:
    def __init__(self, entity_id):
        self.entity_id = entity_id


class TestEntityAufloesung:
    def test_scan_findet_umbenannte_anlage(self, mock_hass):
        mock_hass.states.async_all.return_value = [
            _FakeState("select.meine_anlage_remote_ems_control_mode"),
            _FakeState("select.irgendwas_anderes"),
        ]
        assert find_sigen_control_entity(mock_hass, "remote_ems_mode") == "select.meine_anlage_remote_ems_control_mode"

    def test_scan_kuerzester_name_gewinnt(self, mock_hass):
        """Anlage und Wechselrichter führen ähnliche Namen — die Anlagen-
        Entität ist die ohne Zähl-Suffix und damit die kürzere."""
        mock_hass.states.async_all.return_value = [
            _FakeState("number.sigen_plant_2_ess_max_charging_limit"),
            _FakeState("number.sigen_plant_ess_max_charging_limit"),
        ]
        assert find_sigen_control_entity(mock_hass, "ess_max_charging_limit") == "number.sigen_plant_ess_max_charging_limit"

    def test_scan_ohne_treffer_none(self, mock_hass):
        mock_hass.states.async_all.return_value = []
        assert find_sigen_control_entity(mock_hass, "remote_ems_switch") is None

    def test_konfigurierte_entity_gewinnt_wenn_vorhanden(self, mock_hass):
        mock_hass.states.async_all.return_value = []
        mock_hass.states.get.side_effect = _states({"switch.custom_ems": "on"})
        inv = SigenergyInverter(mock_hass, {"sigen_remote_ems_switch": "switch.custom_ems"})
        assert inv._resolve_entity("remote_ems_switch") == "switch.custom_ems"

    def test_veraltete_config_faellt_auf_scan_zurueck(self, mock_hass):
        mock_hass.states.get.side_effect = _states({})
        mock_hass.states.async_all.return_value = [_FakeState("switch.neu_remote_ems_controlled_by_home_assistant")]
        inv = SigenergyInverter(mock_hass, {"sigen_remote_ems_switch": "switch.alt_und_weg"})
        assert inv._resolve_entity("remote_ems_switch") == "switch.neu_remote_ems_controlled_by_home_assistant"

    def test_ohne_alles_default(self, mock_hass):
        mock_hass.states.get.side_effect = _states({})
        mock_hass.states.async_all.return_value = []
        inv = SigenergyInverter(mock_hass, {})
        assert inv._resolve_entity("remote_ems_mode") == MODE


class TestRegistryStatus:
    """Die Steuerentitäten sind ab Werk deaktiviert und tauchen in der
    State-Machine nicht auf — nur die Entity-Registry kennt sie."""

    @staticmethod
    def _registry(entries):
        reg = MagicMock()
        reg.entities.values.return_value = entries
        return reg

    @staticmethod
    def _entry(entity_id, original_name, disabled_by=None, platform="sigen"):
        return SimpleNamespace(
            entity_id=entity_id, domain=entity_id.split(".")[0],
            original_name=original_name, disabled_by=disabled_by, platform=platform,
        )

    def test_deaktivierte_und_fehlende_werden_erkannt(self, mock_hass):
        entries = [
            self._entry(SWITCH, "Remote EMS (Controlled by Home Assistant)", disabled_by="integration"),
            self._entry(MODE, "Remote EMS Control Mode", disabled_by=None),
            self._entry(CHARGE_LIMIT, "ESS Max Charging Limit", disabled_by="integration"),
            # Entladelimit fehlt ganz; fremde Plattform darf nicht zählen:
            self._entry("number.fremd_ess_max_discharging_limit", "ESS Max Discharging Limit", platform="other"),
        ]
        with patch("homeassistant.helpers.entity_registry.async_get", return_value=self._registry(entries)):
            status = sigen_control_entity_status(mock_hass)

        assert status["remote_ems_switch"] == {"name": "Remote EMS (Controlled by Home Assistant)", "entity_id": SWITCH, "enabled": False}
        assert status["remote_ems_mode"]["enabled"] is True
        assert status["ess_max_charging_limit"]["enabled"] is False
        assert status["ess_max_discharging_limit"] == {"name": "ESS Max Discharging Limit", "entity_id": None, "enabled": None}

        fehlend = [status[k]["name"] for k in SIGEN_REQUIRED_CONTROLS if status[k]["enabled"] is not True]
        assert fehlend == [
            "Remote EMS (Controlled by Home Assistant)",
            "ESS Max Charging Limit",
            "ESS Max Discharging Limit",
        ]

    def test_ohne_registry_leer(self, mock_hass):
        with patch("homeassistant.helpers.entity_registry.async_get", side_effect=RuntimeError("keine Registry")):
            assert sigen_control_entity_status(mock_hass) == {}


class TestVorzeichen:
    """Register 30037 (ESS) positiv = laden — wie bei uns; Register 30005
    (Netz) positiv = Bezug — gegen unsere Konvention."""

    def test_konvention_registriert(self):
        assert INVERTER_SIGN_CONVENTIONS[INVERTER_TYPE_SIGENERGY] == {"battery_sign": 1, "grid_sign": -1}
        assert resolve_sign(INVERTER_TYPE_SIGENERGY, "sensor.sigen_plant_battery_power", "battery_sign") == 1
        assert resolve_sign(INVERTER_TYPE_SIGENERGY, "sensor.sigen_plant_grid_active_power", "grid_sign") == -1

    def test_hausverbrauch_aus_anlagenwerten(self, mock_hass):
        """Messung der Testanlage: PV 0, Batterie −0,696 kW (entlädt), Netz 0
        → Hausverbrauch 0,696 kW. Und bei Bezug (Netz roh +2,0) steigt er."""
        cfg = {
            CONF_INVERTER_TYPE: INVERTER_TYPE_SIGENERGY,
            CONF_PV_POWER_SENSOR: "sensor.sigen_plant_pv_power",
            CONF_BATTERY_POWER_SENSOR: "sensor.sigen_plant_battery_power",
            CONF_GRID_POWER_SENSOR: "sensor.sigen_plant_grid_active_power",
        }
        kw = {"unit_of_measurement": "kW"}
        mock_hass.states.get.side_effect = _states(
            {"sensor.sigen_plant_pv_power": "0.0", "sensor.sigen_plant_battery_power": "-0.696",
             "sensor.sigen_plant_grid_active_power": "0.0"},
            {"sensor.sigen_plant_pv_power": kw, "sensor.sigen_plant_battery_power": kw,
             "sensor.sigen_plant_grid_active_power": kw},
        )
        assert compute_house_load_kw(mock_hass, cfg) == pytest.approx(0.696)

        mock_hass.states.get.side_effect = _states(
            {"sensor.sigen_plant_pv_power": "0.0", "sensor.sigen_plant_battery_power": "0.0",
             "sensor.sigen_plant_grid_active_power": "2.0"},   # roh positiv = Bezug
            {"sensor.sigen_plant_pv_power": kw, "sensor.sigen_plant_battery_power": kw,
             "sensor.sigen_plant_grid_active_power": kw},
        )
        assert compute_house_load_kw(mock_hass, cfg) == pytest.approx(2.0)
