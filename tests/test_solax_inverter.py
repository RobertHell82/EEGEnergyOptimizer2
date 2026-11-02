"""Tests for SolaX Gen4+ inverter implementation.

Phase 12: async_set_charge_limit blockiert das Laden nun via
battery_charge_max_current=0 statt Mode-1-Idle. async_stop_forcible
restoriert den Originalwert aus einem Store. async_set_discharge bleibt
unverändert auf Mode 1 mit negativer active_power.
"""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.eeg_energy_optimizer.const import CONF_BATTERY_SOC_SENSOR
from custom_components.eeg_energy_optimizer.inverter.base import InverterBase
from custom_components.eeg_energy_optimizer.inverter.solax import (
    DISCHARGE_FLOOR_MARGIN_PCT,
    MODE1_AUTOREPEAT_S,
    MODE1_DAUER_S,
    MODE1_KEEPALIVE_S,
    NETZLADEN_MIN_KW,
    SOLAX_DOMAIN,
    SOLAX_ENTITY_DEFAULTS,
    SolaXInverter,
    SolaXStateStore,
)


SELECT_ENTITY = SOLAX_ENTITY_DEFAULTS["remotecontrol_power_control"]
ACTIVE_POWER_ENTITY = SOLAX_ENTITY_DEFAULTS["remotecontrol_active_power"]
DURATION_ENTITY = SOLAX_ENTITY_DEFAULTS["remotecontrol_duration"]
AUTOREPEAT_ENTITY = SOLAX_ENTITY_DEFAULTS["remotecontrol_autorepeat_duration"]
TRIGGER_ENTITY = SOLAX_ENTITY_DEFAULTS["remotecontrol_trigger"]
MAX_CURRENT_ENTITY = SOLAX_ENTITY_DEFAULTS["battery_charge_max_current"]
FLOOR_ENTITY = "number.solax_inverter_selfuse_discharge_min_soc"


def _states(werte: dict[str, str], attribute: dict[str, dict] | None = None):
    """State-Getter, der je Entity einen eigenen Wert liefert."""
    attribute = attribute or {}

    def _get(entity_id):
        if entity_id not in werte:
            return None
        st = MagicMock()
        st.state = werte[entity_id]
        st.attributes = attribute.get(entity_id, {})
        return st

    return _get


def _calls_by_entity(mock_hass) -> dict[str, dict]:
    """Index recorded service calls by their entity_id for easier assertion."""
    out: dict[str, dict] = {}
    for c in mock_hass.services.async_call.call_args_list:
        payload = c.args[2] if len(c.args) > 2 else {}
        eid = payload.get("entity_id")
        if eid:
            out[eid] = payload
    return out


class _NoopStore:
    """In-memory Store-Ersatz für Tests, die Storage nicht prüfen."""

    def __init__(self):
        self.saved: list[dict] = []
        self._data: dict = {}

    async def async_load(self):
        return self._data

    async def async_save(self, data):
        self._data = dict(data)
        self.saved.append(dict(data))


def _install_noop_store(inv: SolaXInverter) -> _NoopStore:
    noop = _NoopStore()
    if inv._state_store is not None:
        inv._state_store._store = noop
        inv._state_store._data = {}
        inv._state_store._loaded = False
    return noop


@pytest.fixture
def solax_config():
    return {}


@pytest.fixture
def inverter(mock_hass, solax_config):
    inv = SolaXInverter(mock_hass, solax_config)
    _install_noop_store(inv)
    return inv


class TestSolaXInverterBase:
    """Verify SolaXInverter inherits from InverterBase."""

    def test_is_instance_of_inverter_base(self, inverter):
        assert isinstance(inverter, InverterBase)

    def test_is_subclass_of_inverter_base(self):
        assert issubclass(SolaXInverter, InverterBase)


class TestAsyncSetChargeLimit:
    """Charge-Block ist nun battery_charge_max_current-basiert (Phase 12)."""

    async def test_block_charging_sets_max_current_to_zero(self, inverter, mock_hass):
        """power_kw=0 → battery_charge_max_current=0 + remotecontrol_power_control=Disabled.

        NEU in Phase 12: KEIN Mode-1-Idle mehr — nur Charge blockieren, Self-Use
        läuft weiter und entlädt bei Bedarf.
        """
        mock_state = MagicMock()
        mock_state.state = "30.0"
        mock_state.attributes = {"max": 30}
        mock_hass.states.get = MagicMock(return_value=mock_state)

        result = await inverter.async_set_charge_limit(0)
        assert result is True

        payloads = _calls_by_entity(mock_hass)
        assert payloads[MAX_CURRENT_ENTITY]["value"] == 0

        select_calls = [
            c for c in mock_hass.services.async_call.call_args_list
            if c.args[0] == "select" and c.args[1] == "select_option"
        ]
        assert any(
            c.args[2].get("option") == "Disabled" for c in select_calls
        ), "remotecontrol_power_control must be set to Disabled"
        assert not any(
            c.args[2].get("option") == "Enabled Battery Control" for c in select_calls
        ), "Mode 1 must NOT be activated for charge block"

    async def test_partial_charge_writes_amps_to_max_current(self, inverter, mock_hass):
        """power_kw=3.0 → battery_charge_max_current in Ampere (kW über Spannung → A).

        Bei 400V Default-Fallback: 3000W / 400V = 7.5 A.
        """
        voltage_state = MagicMock()
        voltage_state.state = "400.0"
        voltage_state.attributes = {}

        max_current_state = MagicMock()
        max_current_state.state = "30.0"
        max_current_state.attributes = {"max": 30}

        def states_get(entity_id):
            if "voltage" in entity_id:
                return voltage_state
            return max_current_state

        mock_hass.states.get = MagicMock(side_effect=states_get)

        result = await inverter.async_set_charge_limit(3.0)
        assert result is True

        payloads = _calls_by_entity(mock_hass)
        written = payloads[MAX_CURRENT_ENTITY]["value"]
        assert written == pytest.approx(7.5, rel=0.01)

    async def test_partial_charge_clamped_to_hardware_max(self, inverter, mock_hass):
        """Falls kW/V die Hardware-Max-Stromstärke überschreitet, wird auf max geclampt."""
        voltage_state = MagicMock()
        voltage_state.state = "400.0"
        voltage_state.attributes = {}

        max_current_state = MagicMock()
        max_current_state.state = "30.0"
        max_current_state.attributes = {"max": 30}

        def states_get(entity_id):
            if "voltage" in entity_id:
                return voltage_state
            return max_current_state

        mock_hass.states.get = MagicMock(side_effect=states_get)

        # 20 kW / 400 V = 50 A → muss auf 30 A geclampt werden
        await inverter.async_set_charge_limit(20.0)
        payloads = _calls_by_entity(mock_hass)
        assert payloads[MAX_CURRENT_ENTITY]["value"] == pytest.approx(30.0, rel=0.01)

    async def test_returns_false_on_exception(self, inverter, mock_hass):
        mock_hass.services.async_call = AsyncMock(side_effect=Exception("boom"))
        result = await inverter.async_set_charge_limit(0)
        assert result is False

    async def test_caches_original_on_first_call(self, inverter, mock_hass):
        """Erster Eingriff mit State > 0 cached den Wert im Store."""
        save_calls = []

        class FakeStore:
            async def async_load(self):
                return {}

            async def async_save(self, data):
                save_calls.append(dict(data))

        inverter._state_store._store = FakeStore()
        inverter._state_store._loaded = False
        inverter._state_store._data = {}

        mock_state = MagicMock()
        mock_state.state = "25.0"
        mock_state.attributes = {"max": 30}
        mock_hass.states.get = MagicMock(return_value=mock_state)

        await inverter.async_set_charge_limit(0)
        assert len(save_calls) == 1
        assert save_calls[0]["battery_charge_max_current_original"] == 25.0

    async def test_skips_cache_when_state_is_zero(self, inverter, mock_hass):
        """Reboot-Schutz: aktueller State 0 darf bestehenden Cache nicht überschreiben."""
        save_calls = []

        class FakeStore:
            async def async_load(self):
                return {"battery_charge_max_current_original": 30.0}

            async def async_save(self, data):
                save_calls.append(dict(data))

        inverter._state_store._store = FakeStore()
        inverter._state_store._loaded = False
        inverter._state_store._data = {}

        mock_state = MagicMock()
        mock_state.state = "0.0"
        mock_state.attributes = {"max": 30}
        mock_hass.states.get = MagicMock(return_value=mock_state)

        await inverter._state_store.async_load()
        assert inverter._state_store.original_current == 30.0

        await inverter.async_set_charge_limit(0)
        # Kein save_call mit 0 als Original-Wert
        assert all(
            c.get("battery_charge_max_current_original") != 0 for c in save_calls
        )


class TestAsyncSetDischarge:
    """Discharge bleibt auf Mode 1 mit negativer active_power (regression guard)."""

    async def test_discharge_uses_negative_active_power(self, inverter, mock_hass):
        """power_kw=3.0 → active_power=-3000."""
        result = await inverter.async_set_discharge(3.0)
        assert result is True

        calls = mock_hass.services.async_call.call_args_list
        assert len(calls) == 5

        assert calls[0].args == (
            "select",
            "select_option",
            {"entity_id": SELECT_ENTITY, "option": "Enabled Battery Control"},
        )
        assert calls[1].args == (
            "number",
            "set_value",
            {"entity_id": ACTIVE_POWER_ENTITY, "value": -3000},
        )
        assert calls[2].args == (
            "number",
            "set_value",
            {"entity_id": DURATION_ENTITY, "value": 300},
        )
        assert calls[3].args == (
            "number",
            "set_value",
            {"entity_id": AUTOREPEAT_ENTITY, "value": 600},
        )
        assert calls[4].args == (
            "button",
            "press",
            {"entity_id": TRIGGER_ENTITY},
        )

    async def test_target_soc_senkt_den_entladeboden(self, inverter, mock_hass):
        """Ohne Absenken stoppt der Wechselrichter an seinem eigenen Boden —
        der Entladebefehl laeuft weiter, die Batterie liefert 0,00 kW."""
        mock_hass.states.get = MagicMock(
            side_effect=_states(_mode1_vorhanden(**{FLOOR_ENTITY: "30.0"}))
        )
        result = await inverter.async_set_discharge(2.0, target_soc=20)
        assert result is True

        payloads = _calls_by_entity(mock_hass)
        assert payloads[FLOOR_ENTITY]["value"] == 20 - DISCHARGE_FLOOR_MARGIN_PCT

    async def test_ohne_target_soc_bleibt_der_boden_unangetastet(
        self, inverter, mock_hass
    ):
        """Kein Ziel, kein Eingriff — sonst senkten wir ins Blaue."""
        mock_hass.states.get = MagicMock(
            side_effect=_states(_mode1_vorhanden(**{FLOOR_ENTITY: "30.0"}))
        )
        await inverter.async_set_discharge(2.0)
        assert FLOOR_ENTITY not in _calls_by_entity(mock_hass)

    async def test_tiefer_boden_wird_nicht_angehoben(self, inverter, mock_hass):
        """Liegt der Geraetewert schon tief genug, bleibt er stehen — wir
        senken nur, wo es noetig ist."""
        mock_hass.states.get = MagicMock(
            side_effect=_states({FLOOR_ENTITY: "5.0"})
        )
        await inverter.async_set_discharge(2.0, target_soc=20)
        assert FLOOR_ENTITY not in _calls_by_entity(mock_hass)

    async def test_boden_wird_im_store_gesichert(self, inverter, mock_hass):
        noop = _install_noop_store(inverter)
        mock_hass.states.get = MagicMock(
            side_effect=_states(_mode1_vorhanden(**{FLOOR_ENTITY: "30.0"}))
        )
        await inverter.async_set_discharge(2.0, target_soc=20)
        assert any(
            d.get("selfuse_discharge_min_soc_original") == 30.0 for d in noop.saved
        )

    async def test_zweiter_aufruf_ueberschreibt_den_vorwert_nicht(
        self, inverter, mock_hass
    ):
        """Beim zweiten Aufruf steht unser abgesenkter Wert im Geraet — ihn als
        Original zu sichern hiesse, den echten zu verlieren."""
        _install_noop_store(inverter)
        mock_hass.states.get = MagicMock(
            side_effect=_states(_mode1_vorhanden(**{FLOOR_ENTITY: "30.0"}))
        )
        await inverter.async_set_discharge(2.0, target_soc=20)
        mock_hass.states.get = MagicMock(
            side_effect=_states(_mode1_vorhanden(**{FLOOR_ENTITY: "15.0"}))
        )
        await inverter.async_set_discharge(2.0, target_soc=20)
        assert inverter._state_store.original_discharge_floor == 30.0

    async def test_positive_input_still_emits_negative_power(self, inverter, mock_hass):
        """Positive power input is still encoded as negative discharge."""
        await inverter.async_set_discharge(2.0)
        payloads = _calls_by_entity(mock_hass)
        assert payloads[ACTIVE_POWER_ENTITY]["value"] == -2000

    async def test_returns_false_on_exception(self, inverter, mock_hass):
        mock_hass.services.async_call = AsyncMock(side_effect=Exception("boom"))
        result = await inverter.async_set_discharge(3.0)
        assert result is False


class TestAsyncStopForcible:
    """Stop beendet Mode 1 und restoriert battery_charge_max_current (Phase 12)."""

    async def test_stop_forcible_disables_mode_one(self, inverter, mock_hass):
        """Disabled-Sequenz + duration=20 + autorepeat=0 + trigger."""
        # mock_hass.states.get liefert per Default MagicMock; attributes.max
        # parst dann nicht zu float, sodass kein Restore-Aufruf erfolgt.
        result = await inverter.async_stop_forcible()
        assert result is True

        calls = mock_hass.services.async_call.call_args_list
        # 5 Mode-1-Calls + ggf. 1 Restore-Call (hier ohne State → 5)
        assert calls[0].args == (
            "select",
            "select_option",
            {"entity_id": SELECT_ENTITY, "option": "Disabled"},
        )
        assert calls[1].args == (
            "number",
            "set_value",
            {"entity_id": ACTIVE_POWER_ENTITY, "value": 0},
        )
        assert calls[2].args == (
            "number",
            "set_value",
            {"entity_id": DURATION_ENTITY, "value": 20},
        )
        assert calls[3].args == (
            "number",
            "set_value",
            {"entity_id": AUTOREPEAT_ENTITY, "value": 0},
        )
        assert calls[4].args == (
            "button",
            "press",
            {"entity_id": TRIGGER_ENTITY},
        )

    async def test_returns_false_on_exception(self, inverter, mock_hass):
        mock_hass.services.async_call = AsyncMock(side_effect=Exception("boom"))
        result = await inverter.async_stop_forcible()
        assert result is False

    async def test_stop_forcible_restores_original_from_store(self, inverter, mock_hass):
        """stop_forcible restoriert gecachten battery_charge_max_current."""

        class FakeStore:
            async def async_load(self):
                return {"battery_charge_max_current_original": 25.0}

            async def async_save(self, data):
                pass

        inverter._state_store._store = FakeStore()
        inverter._state_store._loaded = False
        inverter._state_store._data = {}

        await inverter.async_stop_forcible()

        payloads = _calls_by_entity(mock_hass)
        assert payloads[MAX_CURRENT_ENTITY]["value"] == 25.0

    async def test_stop_forcible_uses_max_fallback_when_no_cache(self, inverter, mock_hass):
        """Wenn Store leer, nutzt stop_forcible attributes.max."""

        class FakeStore:
            async def async_load(self):
                return {}

            async def async_save(self, data):
                pass

        inverter._state_store._store = FakeStore()
        inverter._state_store._loaded = False
        inverter._state_store._data = {}

        mock_state = MagicMock()
        mock_state.state = "0.0"
        mock_state.attributes = {"max": 30}
        mock_hass.states.get = MagicMock(return_value=mock_state)

        await inverter.async_stop_forcible()
        payloads = _calls_by_entity(mock_hass)
        assert payloads[MAX_CURRENT_ENTITY]["value"] == 30.0


class TestIsAvailable:
    """is_available depends on whether solax_modbus has a loaded config entry."""

    def test_available_when_loaded(self, mock_hass, solax_config):
        entry = MagicMock()
        entry.state.value = "loaded"
        mock_hass.config_entries.async_entries = MagicMock(return_value=[entry])
        inv = SolaXInverter(mock_hass, solax_config)
        assert inv.is_available is True

    def test_unavailable_when_not_loaded(self, mock_hass, solax_config):
        entry = MagicMock()
        entry.state.value = "setup_error"
        mock_hass.config_entries.async_entries = MagicMock(return_value=[entry])
        inv = SolaXInverter(mock_hass, solax_config)
        assert inv.is_available is False

    def test_unavailable_when_no_entries(self, mock_hass, solax_config):
        mock_hass.config_entries.async_entries = MagicMock(return_value=[])
        inv = SolaXInverter(mock_hass, solax_config)
        assert inv.is_available is False


class TestEntityResolution:
    """Entity IDs may be overridden via solax_<key> config keys; otherwise defaults apply."""

    async def test_uses_config_override_on_discharge(self, mock_hass):
        """Discharge respektiert solax_<key>-Overrides (5-Write-Pattern)."""
        config = {
            "solax_remotecontrol_power_control": "select.custom_power_control",
            "solax_remotecontrol_active_power": "number.custom_active_power",
            "solax_remotecontrol_duration": "number.custom_duration",
            "solax_remotecontrol_autorepeat_duration": "number.custom_autorepeat",
            "solax_remotecontrol_trigger": "button.custom_trigger",
        }
        inv = SolaXInverter(mock_hass, config)
        _install_noop_store(inv)
        await inv.async_set_discharge(1.0)

        calls = mock_hass.services.async_call.call_args_list
        assert calls[0].args[2]["entity_id"] == "select.custom_power_control"
        assert calls[1].args[2]["entity_id"] == "number.custom_active_power"
        assert calls[2].args[2]["entity_id"] == "number.custom_duration"
        assert calls[3].args[2]["entity_id"] == "number.custom_autorepeat"
        assert calls[4].args[2]["entity_id"] == "button.custom_trigger"

    async def test_charge_block_respects_battery_charge_max_current_override(self, mock_hass):
        """Phase 12: solax_battery_charge_max_current-Override wird respektiert."""
        config = {
            "solax_battery_charge_max_current": "number.custom_max_current",
            "solax_remotecontrol_power_control": "select.custom_power_control",
        }
        inv = SolaXInverter(mock_hass, config)
        _install_noop_store(inv)
        await inv.async_set_charge_limit(0)

        payloads = _calls_by_entity(mock_hass)
        assert "number.custom_max_current" in payloads
        assert payloads["number.custom_max_current"]["value"] == 0
        assert payloads["select.custom_power_control"]["option"] == "Disabled"

    async def test_uses_defaults_when_no_config_on_discharge(self, mock_hass):
        """Without overrides, all entity IDs come from SOLAX_ENTITY_DEFAULTS."""
        inv = SolaXInverter(mock_hass, {})
        _install_noop_store(inv)
        await inv.async_set_discharge(1.0)

        calls = mock_hass.services.async_call.call_args_list
        assert calls[0].args[2]["entity_id"] == SELECT_ENTITY
        assert calls[1].args[2]["entity_id"] == ACTIVE_POWER_ENTITY
        assert calls[2].args[2]["entity_id"] == DURATION_ENTITY
        assert calls[3].args[2]["entity_id"] == AUTOREPEAT_ENTITY
        assert calls[4].args[2]["entity_id"] == TRIGGER_ENTITY


class TestKWToAmpsConversion:
    """kW→A conversion für battery_charge_max_current (Phase 12)."""

    async def test_fractional_kw_charge(self, inverter, mock_hass):
        """2.5 kW Charge @ 400V Default → 6.25 A auf battery_charge_max_current."""
        # states.get → None → keine kaputten MagicMock-Floats; max_a-Fallback = 30 A
        mock_hass.states.get = MagicMock(return_value=None)
        await inverter.async_set_charge_limit(2.5)
        payloads = _calls_by_entity(mock_hass)
        assert payloads[MAX_CURRENT_ENTITY]["value"] == pytest.approx(6.25, rel=0.01)

    async def test_small_charge_value(self, inverter, mock_hass):
        """0.1 kW @ 400V Default → 0.25 A."""
        mock_hass.states.get = MagicMock(return_value=None)
        await inverter.async_set_charge_limit(0.1)
        payloads = _calls_by_entity(mock_hass)
        assert payloads[MAX_CURRENT_ENTITY]["value"] == pytest.approx(0.25, rel=0.01)


class TestKWToWConversionDischarge:
    """Discharge bleibt kW→W (Mode-1-Pfad)."""

    async def test_fractional_kw_discharge(self, inverter, mock_hass):
        """1.5 kW Discharge → −1500 W."""
        await inverter.async_set_discharge(1.5)
        payloads = _calls_by_entity(mock_hass)
        assert payloads[ACTIVE_POWER_ENTITY]["value"] == -1500


class TestSolaXStateStore:
    """SolaXStateStore-Klasse persistiert den Original-Wert."""

    async def test_load_empty_store_yields_none(self, mock_hass):
        store = SolaXStateStore(mock_hass)
        store._store = _NoopStore()
        await store.async_load()
        assert store.original_current is None

    async def test_save_persists_value(self, mock_hass):
        store = SolaXStateStore(mock_hass)
        noop = _NoopStore()
        store._store = noop
        await store.async_load()
        await store.async_save_original_current(27.5)
        assert noop.saved[-1]["battery_charge_max_current_original"] == 27.5
        assert store.original_current == 27.5

    async def test_load_returns_existing_value(self, mock_hass):
        store = SolaXStateStore(mock_hass)
        noop = _NoopStore()
        noop._data = {"battery_charge_max_current_original": 30.0}
        store._store = noop
        await store.async_load()
        assert store.original_current == 30.0


# ---------------------------------------------------------------------------
# Versionstolerante Entity-Auflösung (solax_modbus ≥2025.x Mode-Suffixe)
# ---------------------------------------------------------------------------

class _FakeState:
    def __init__(self, entity_id: str):
        self.entity_id = entity_id


# Entity-Landschaft einer realen Anlage mit neuer solax_modbus-Benennung
# (Mode-Suffixe + *_direct-Varianten, beobachtet auf X3-Hybrid Gen4, 2026-08).
NEW_STYLE_ENTITIES = {
    "select": [
        "select.solax_inverter_remotecontrol_power_control_mode_1",
        "select.solax_inverter_remotecontrol_power_control_mode_mode_8_9",
        "select.solax_inverter_remotecontrol_power_control_mode_mode_8_9_direct",
        "select.solax_inverter_modbus_power_control_direct",
    ],
    "number": [
        "number.solax_inverter_remotecontrol_active_power_mode_1",
        "number.solax_inverter_remotecontrol_active_power_mode_1_direct",
        "number.solax_inverter_remotecontrol_autorepeat_duration_mode_1_9",
        "number.solax_inverter_remotecontrol_duration_mode_1_8",
        "number.solax_inverter_battery_charge_max_current",
        "number.solax_inverter_selfuse_discharge_min_soc",
    ],
    "button": [
        "button.solax_inverter_remotecontrol_trigger_mode_1_7",
        "button.solax_inverter_powercontrolmode_trigger_mode_8_9",
    ],
}

OLD_STYLE_ENTITIES = {
    "select": ["select.solax_remotecontrol_power_control"],
    "number": [
        "number.solax_remotecontrol_active_power",
        "number.solax_remotecontrol_autorepeat_duration",
        "number.solax_remotecontrol_duration",
        "number.solax_battery_charge_max_current",
        "number.solax_selfuse_discharge_min_soc",
    ],
    "button": ["button.solax_remotecontrol_trigger"],
}


def _install_states(mock_hass, entities: dict) -> None:
    all_ids = {eid for ids in entities.values() for eid in ids}
    mock_hass.states.async_all = MagicMock(
        side_effect=lambda domain: [_FakeState(e) for e in entities.get(domain, [])]
    )
    mock_hass.states.get = MagicMock(
        side_effect=lambda eid: _FakeState(eid) if eid in all_ids else None
    )


class TestFindSolaxControlEntity:
    """find_solax_control_entity matcht alte und neue solax_modbus-Benennung."""

    def test_new_style_entities_resolved(self, mock_hass):
        from custom_components.eeg_energy_optimizer.inverter.solax import (
            find_solax_control_entity,
        )
        _install_states(mock_hass, NEW_STYLE_ENTITIES)
        expected = {
            "remotecontrol_power_control": "select.solax_inverter_remotecontrol_power_control_mode_1",
            "remotecontrol_active_power": "number.solax_inverter_remotecontrol_active_power_mode_1",
            "remotecontrol_autorepeat_duration": "number.solax_inverter_remotecontrol_autorepeat_duration_mode_1_9",
            "remotecontrol_duration": "number.solax_inverter_remotecontrol_duration_mode_1_8",
            "remotecontrol_trigger": "button.solax_inverter_remotecontrol_trigger_mode_1_7",
            "battery_charge_max_current": "number.solax_inverter_battery_charge_max_current",
            "selfuse_discharge_min_soc": "number.solax_inverter_selfuse_discharge_min_soc",
        }
        for key, entity_id in expected.items():
            assert find_solax_control_entity(mock_hass, key) == entity_id, key

    def test_old_style_entities_resolved(self, mock_hass):
        from custom_components.eeg_energy_optimizer.inverter.solax import (
            find_solax_control_entity,
        )
        _install_states(mock_hass, OLD_STYLE_ENTITIES)
        assert (
            find_solax_control_entity(mock_hass, "remotecontrol_power_control")
            == "select.solax_remotecontrol_power_control"
        )
        assert (
            find_solax_control_entity(mock_hass, "remotecontrol_trigger")
            == "button.solax_remotecontrol_trigger"
        )

    def test_direct_and_mode89_variants_not_matched(self, mock_hass):
        from custom_components.eeg_energy_optimizer.inverter.solax import (
            find_solax_control_entity,
        )
        _install_states(mock_hass, {
            "select": [
                "select.solax_inverter_remotecontrol_power_control_mode_mode_8_9",
                "select.solax_inverter_remotecontrol_power_control_mode_mode_8_9_direct",
            ],
            "number": ["number.solax_inverter_remotecontrol_active_power_mode_1_direct"],
        })
        assert find_solax_control_entity(mock_hass, "remotecontrol_power_control") is None
        assert find_solax_control_entity(mock_hass, "remotecontrol_active_power") is None

    def test_no_states_yields_none(self, mock_hass):
        from custom_components.eeg_energy_optimizer.inverter.solax import (
            find_solax_control_entity,
        )
        _install_states(mock_hass, {})
        assert find_solax_control_entity(mock_hass, "remotecontrol_trigger") is None


class TestSolaXEntityResolution:
    """_resolve_entity: Config → Suffix-Scan → Default."""

    def test_configured_entity_wins_when_it_exists(self, mock_hass):
        entities = {
            **NEW_STYLE_ENTITIES,
            "number": NEW_STYLE_ENTITIES["number"] + ["number.custom_active_power"],
        }
        _install_states(mock_hass, entities)
        inv = SolaXInverter(
            mock_hass, {"solax_remotecontrol_active_power": "number.custom_active_power"}
        )
        assert inv._resolve_entity("remotecontrol_active_power") == "number.custom_active_power"

    def test_stale_config_falls_back_to_scan(self, mock_hass):
        """Nach solax_modbus-Update zeigt die Config auf eine umbenannte Entity."""
        _install_states(mock_hass, NEW_STYLE_ENTITIES)
        inv = SolaXInverter(
            mock_hass,
            {"solax_remotecontrol_active_power": "number.solax_inverter_remotecontrol_active_power"},
        )
        assert (
            inv._resolve_entity("remotecontrol_active_power")
            == "number.solax_inverter_remotecontrol_active_power_mode_1"
        )

    def test_no_match_falls_back_to_default(self, mock_hass):
        _install_states(mock_hass, {})
        inv = SolaXInverter(mock_hass, {})
        assert (
            inv._resolve_entity("remotecontrol_trigger")
            == SOLAX_ENTITY_DEFAULTS["remotecontrol_trigger"]
        )

    async def test_discharge_targets_new_style_entities(self, mock_hass):
        _install_states(mock_hass, NEW_STYLE_ENTITIES)
        inv = SolaXInverter(mock_hass, {})
        _install_noop_store(inv)
        assert await inv.async_set_discharge(3.0) is True
        payloads = _calls_by_entity(mock_hass)
        assert payloads["select.solax_inverter_remotecontrol_power_control_mode_1"]["option"] == "Enabled Battery Control"
        assert payloads["number.solax_inverter_remotecontrol_active_power_mode_1"]["value"] == -3000
        assert payloads["number.solax_inverter_remotecontrol_duration_mode_1_8"]["value"] == 300
        assert payloads["number.solax_inverter_remotecontrol_autorepeat_duration_mode_1_9"]["value"] == 600
        assert "button.solax_inverter_remotecontrol_trigger_mode_1_7" in payloads


class TestFindSolaxPrefix:
    """_find_solax_prefix (websocket_api) leitet den Prefix aus alter wie neuer Benennung ab."""

    def test_prefix_from_new_style_name(self, mock_hass):
        from custom_components.eeg_energy_optimizer.websocket_api import _find_solax_prefix
        _install_states(mock_hass, NEW_STYLE_ENTITIES)
        assert _find_solax_prefix(mock_hass) == "solax_inverter_"

    def test_prefix_from_old_style_name(self, mock_hass):
        from custom_components.eeg_energy_optimizer.websocket_api import _find_solax_prefix
        _install_states(mock_hass, OLD_STYLE_ENTITIES)
        assert _find_solax_prefix(mock_hass) == "solax_"

    def test_prefix_none_without_entities(self, mock_hass):
        from custom_components.eeg_energy_optimizer.websocket_api import _find_solax_prefix
        _install_states(mock_hass, {})
        assert _find_solax_prefix(mock_hass) is None


class TestEntladebodenRestore:
    """Der abgesenkte Boden muss zurueck — sonst entlaedt der Wechselrichter
    auch im Automatikbetrieb tiefer, als der Betreiber eingestellt hat."""

    async def test_stop_restauriert_den_boden(self, inverter, mock_hass):
        _install_noop_store(inverter)
        mock_hass.states.get = MagicMock(side_effect=_states(_mode1_vorhanden(**{FLOOR_ENTITY: "30.0"})))
        await inverter.async_set_discharge(2.0, target_soc=20)

        mock_hass.services.async_call.reset_mock()
        mock_hass.states.get = MagicMock(side_effect=_states({FLOOR_ENTITY: "15.0"}))
        await inverter.async_stop_forcible()

        assert _calls_by_entity(mock_hass)[FLOOR_ENTITY]["value"] == 30.0

    async def test_store_wird_nach_restore_geleert(self, inverter, mock_hass):
        _install_noop_store(inverter)
        mock_hass.states.get = MagicMock(side_effect=_states(_mode1_vorhanden(**{FLOOR_ENTITY: "30.0"})))
        await inverter.async_set_discharge(2.0, target_soc=20)
        await inverter.async_stop_forcible()
        assert inverter._state_store.original_discharge_floor is None

    async def test_stop_ohne_vorwert_schreibt_nichts(self, inverter, mock_hass):
        """Ein Stopp ohne vorherige Entladung darf den Boden nicht anfassen."""
        _install_noop_store(inverter)
        mock_hass.states.get = MagicMock(side_effect=_states({FLOOR_ENTITY: "30.0"}))
        await inverter.async_stop_forcible()
        assert FLOOR_ENTITY not in _calls_by_entity(mock_hass)


class TestScheduleControlInterface:
    """Der Executor steuert nur Treiber, die die Schnittstelle anbieten."""

    def test_supports_schedule_control(self, inverter):
        assert inverter.supports_schedule_control is True

    async def test_ladelimit_wird_aus_ampere_umgerechnet(self, inverter, mock_hass):
        """SolaX begrenzt ueber Strom, der Fahrplan rechnet in Leistung."""
        mock_hass.states.get = MagicMock(
            side_effect=_states({
                MAX_CURRENT_ENTITY: "10.0",
                "sensor.solax_inverter_battery_voltage_charge": "400.0",
            })
        )
        assert await inverter.async_get_charge_limit_kw() == 4.0

    async def test_ladelimit_none_wenn_nicht_lesbar(self, inverter, mock_hass):
        mock_hass.states.get = MagicMock(side_effect=_states({}))
        assert await inverter.async_get_charge_limit_kw() is None

    def test_ladelimit_maximum_aus_attribut(self, inverter, mock_hass):
        mock_hass.states.get = MagicMock(
            side_effect=_states(
                {
                    MAX_CURRENT_ENTITY: "10.0",
                    "sensor.solax_inverter_battery_voltage_charge": "400.0",
                },
                {MAX_CURRENT_ENTITY: {"max": 30}},
            )
        )
        assert inverter.get_charge_limit_max_kw() == 12.0

    def test_max_entladeleistung_aus_mode1_attribut(self, inverter, mock_hass):
        """Guard 2 darf die Grenze der Mode-1-Entitaet nicht ueberschreiten."""
        mock_hass.states.get = MagicMock(
            side_effect=_states(
                {ACTIVE_POWER_ENTITY: "0"},
                {ACTIVE_POWER_ENTITY: {"max": 5000}},
            )
        )
        assert inverter.get_max_discharge_power_kw() == 5.0

    def test_entladeboden_als_planungsgrenze(self, inverter, mock_hass):
        mock_hass.states.get = MagicMock(side_effect=_states({FLOOR_ENTITY: "30.0"}))
        assert inverter.get_backup_reserve_soc_pct() == 30.0

    async def test_eigener_boden_wird_nicht_als_geraetegrenze_gemeldet(
        self, inverter, mock_hass
    ):
        """Waehrend der Entladung steht unser abgesenkter Wert im Geraet.
        Gemeldet werden muss der Vorwert, sonst plant der Fahrplan mit jeder
        Entladung tiefer."""
        _install_noop_store(inverter)
        mock_hass.states.get = MagicMock(side_effect=_states(_mode1_vorhanden(**{FLOOR_ENTITY: "30.0"})))
        await inverter.async_set_discharge(2.0, target_soc=20)

        mock_hass.states.get = MagicMock(side_effect=_states({FLOOR_ENTITY: "15.0"}))
        assert inverter.get_backup_reserve_soc_pct() == 30.0

    def test_control_entities_nur_vorhandene(self, inverter, mock_hass):
        mock_hass.states.get = MagicMock(
            side_effect=_states({MAX_CURRENT_ENTITY: "10.0", FLOOR_ENTITY: "30.0"})
        )
        rollen = {r["role"] for r in inverter.get_control_entities()}
        assert rollen == {"charge_limit", "backup_soc"}


class TestEntladebodenNachFehlschlag:
    """Unser abgesenkter Boden darf nie als Gerätegrenze durchgehen.

    Gegenstück zum Fronius-Fall vom 13.09.2026: Dort sperrte eine
    hängengebliebene Reserve die Batterie. Hier wäre die Wirkung umgekehrt —
    ein hängengebliebener Boden lässt den Wechselrichter im Automatikbetrieb
    tiefer entladen, als der Betreiber eingestellt hat.
    """

    def test_gesicherter_vorwert_schlaegt_den_gelesenen_boden(self, inverter):
        """Auch ohne laufende Entladung gilt der gesicherte Vorwert."""
        inverter._state_store._data = {"selfuse_discharge_min_soc_original": 20.0}
        inverter._state_store._loaded = True
        inverter._discharge_active = False          # Entladung kam nie zustande

        assert inverter.get_backup_reserve_soc_pct() == pytest.approx(20.0)

    async def test_fehlgeschlagene_entladung_stellt_den_boden_zurueck(
        self, inverter, mock_hass
    ):
        """Scheitert das Setzen, darf kein abgesenkter Boden stehen bleiben."""
        inverter._state_store._data = {"selfuse_discharge_min_soc_original": 20.0}
        inverter._state_store._loaded = True

        zurueckgeschrieben: list[float] = []

        async def _set_number(key, wert):
            if key == "selfuse_discharge_min_soc":
                zurueckgeschrieben.append(wert)
                return
            raise RuntimeError("Gerät antwortet nicht")

        inverter._set_number = _set_number
        inverter._senke_entladeboden = AsyncMock()

        ok = await inverter.async_set_discharge(1.5, target_soc=40.0)

        assert ok is False
        assert 20.0 in zurueckgeschrieben, "Vorwert zurückgeschrieben"


@pytest.fixture
def takt():
    """``async_track_time_interval`` abfangen: liefert Rückruf und Abmelder."""
    takte = []
    abmelder = MagicMock()

    def _intervall(hass, callback, intervall):
        takte.append((callback, intervall))
        return abmelder

    beobachter = []
    soc_abmelder = MagicMock()

    def _track(hass, entities, callback):
        beobachter.append((list(entities), callback))
        return soc_abmelder

    modul = ModuleType("homeassistant.helpers.event")
    modul.async_track_time_interval = _intervall
    modul.async_track_state_change_event = _track
    # Andere Testmodule legen unter homeassistant.core einen MagicMock ab,
    # dessen ``callback`` den Rückruf verschlucken würde — hier eines, das
    # ihn nur markiert, wie das echte (Muster: test_victron_inverter.py).
    core = ModuleType("homeassistant.core")

    def _callback(func):
        func._hass_callback = True
        return func

    core.callback = _callback
    with patch.dict(
        sys.modules, {"homeassistant.helpers.event": modul, "homeassistant.core": core}
    ):
        yield SimpleNamespace(
            takte=takte, abmelder=abmelder,
            beobachter=beobachter, soc_abmelder=soc_abmelder,
        )


def _mode1_schreibvorgaenge(mock_hass) -> list[tuple]:
    return [c.args for c in mock_hass.services.async_call.call_args_list]


class TestMode1Keepalive:
    """Ein Mode-1-Befehl hält sich nicht selbst.

    Ab solax_modbus ~2026.10 schreibt die Integration bei Ablauf der
    Wiederholung ausdrücklich „Disabled". Der Executor schreibt nur bei einer
    Änderung — ohne eigenen Takt endete jede Entladung nach der
    Wiederholungszeit, mit den früheren 60 s also nach einer Minute.
    """

    def test_zeiten_passen_zusammen(self):
        """Die Wiederholung der Integration muss den Takt überdecken, sonst
        endet der Befehl zwischen zwei Auslösungen."""
        assert MODE1_KEEPALIVE_S < MODE1_AUTOREPEAT_S
        assert MODE1_KEEPALIVE_S < MODE1_DAUER_S

    async def test_entladung_meldet_den_takt_an(self, inverter, mock_hass, takt):
        assert await inverter.async_set_discharge(2.0) is True
        assert len(takt.takte) == 1
        assert takt.takte[0][1].total_seconds() == MODE1_KEEPALIVE_S

    async def test_takt_loest_denselben_befehl_erneut_aus(self, inverter, mock_hass, takt):
        await inverter.async_set_discharge(2.0)
        mock_hass.services.async_call.reset_mock()

        await takt.takte[0][0](None)

        assert _mode1_schreibvorgaenge(mock_hass) == [
            ("select", "select_option",
             {"entity_id": SELECT_ENTITY, "option": "Enabled Battery Control"}),
            ("number", "set_value", {"entity_id": ACTIVE_POWER_ENTITY, "value": -2000}),
            ("number", "set_value", {"entity_id": DURATION_ENTITY, "value": MODE1_DAUER_S}),
            ("number", "set_value", {"entity_id": AUTOREPEAT_ENTITY, "value": MODE1_AUTOREPEAT_S}),
            ("button", "press", {"entity_id": TRIGGER_ENTITY}),
        ]

    async def test_neuer_befehl_meldet_keinen_zweiten_takt_an(self, inverter, mock_hass, takt):
        """Eine geänderte Leistung erneuert den Takt nicht doppelt — sie wird
        sein neuer Inhalt."""
        await inverter.async_set_discharge(2.0)
        await inverter.async_set_discharge(3.0)
        mock_hass.services.async_call.reset_mock()
        # Erster Befehl beendet den Takt beim Start des zweiten, also zwei
        # Anmeldungen, aber nur die zweite ist aktiv.
        assert takt.abmelder.call_count == 1
        await takt.takte[-1][0](None)
        assert _calls_by_entity(mock_hass)[ACTIVE_POWER_ENTITY]["value"] == -3000

    async def test_stopp_beendet_den_takt(self, inverter, mock_hass, takt):
        await inverter.async_set_discharge(2.0)
        assert await inverter.async_stop_forcible() is True
        takt.abmelder.assert_called_once()

        mock_hass.services.async_call.reset_mock()
        await takt.takte[0][0](None)    # ein Takt, der schon unterwegs war
        assert mock_hass.services.async_call.call_count == 0

    async def test_gescheiterter_stopp_beendet_den_takt_trotzdem(
        self, inverter, mock_hass, takt
    ):
        """Was wir beenden wollen, darf niemand mehr erneuern — er läuft im
        Gerät dann höchstens noch bis zum Rückfall aus."""
        await inverter.async_set_discharge(2.0)
        mock_hass.services.async_call = AsyncMock(side_effect=Exception("boom"))
        assert await inverter.async_stop_forcible() is False
        takt.abmelder.assert_called_once()
        assert inverter._mode1_watt is None

    async def test_ladelimit_beendet_den_takt(self, inverter, mock_hass, takt):
        mock_hass.states.get = MagicMock(side_effect=_states(_mode1_vorhanden()))
        await inverter.async_set_discharge(2.0)
        await inverter.async_set_charge_limit(0)
        takt.abmelder.assert_called_once()
        assert inverter._mode1_watt is None

    async def test_gescheiterte_entladung_meldet_keinen_takt_an(
        self, inverter, mock_hass, takt
    ):
        mock_hass.services.async_call = AsyncMock(side_effect=Exception("boom"))
        assert await inverter.async_set_discharge(2.0) is False
        assert takt.takte == []

    async def test_entladen_der_integration_meldet_ab(self, inverter, mock_hass, takt):
        await inverter.async_set_discharge(2.0)
        await inverter.async_disconnect()
        takt.abmelder.assert_called_once()
        assert inverter._mode1_watt is None

    async def test_fehler_im_takt_wirft_nicht(self, inverter, mock_hass, takt):
        """Der nächste Takt versucht es wieder; bis zum Ablauf der
        Wiederholung läuft der Befehl im Gerät ohnehin weiter."""
        await inverter.async_set_discharge(2.0)
        mock_hass.services.async_call = AsyncMock(side_effect=Exception("boom"))
        await takt.takte[0][0](None)
        assert inverter._mode1_watt == -2000
        takt.abmelder.assert_not_called()


SOC_SENSOR = "sensor.batterie_soc"


def _netzlade_treiber(mock_hass, werte: dict[str, str], attribute: dict | None = None):
    """Treiber mit SOC-Sensor und vorgegebenen Zuständen."""
    mock_hass.states.get = MagicMock(side_effect=_states(werte, attribute))
    inv = SolaXInverter(mock_hass, {CONF_BATTERY_SOC_SENSOR: SOC_SENSOR})
    _install_noop_store(inv)
    return inv


def _mode1_vorhanden(**extra) -> dict[str, str]:
    werte = {
        SELECT_ENTITY: "Disabled",
        ACTIVE_POWER_ENTITY: "0",
        DURATION_ENTITY: "20",
        AUTOREPEAT_ENTITY: "0",
        TRIGGER_ENTITY: "unknown",
    }
    werte.update(extra)
    return werte


class TestNetzladung:
    """Batterie aus dem Netz laden (Beta): Mode 1 mit positiver Leistung.

    Am Gerät nicht geprüft — die Tests halten fest, was der Treiber
    schreibt, nicht, was die SolaX daraus macht.
    """

    def test_angeboten_mit_mode1_entitaeten(self, mock_hass):
        inv = _netzlade_treiber(mock_hass, _mode1_vorhanden())
        assert inv.supports_grid_charge is True

    def test_nicht_angeboten_ohne_mode1(self, mock_hass):
        """Gen6 hat keinen Mode 1 — der Plan darf keine Netzladung vorsehen,
        die nie ankäme."""
        werte = _mode1_vorhanden()
        del werte[TRIGGER_ENTITY]
        inv = _netzlade_treiber(mock_hass, werte)
        assert inv.supports_grid_charge is False

    def test_zwangsentladung_nur_mit_mode1(self, mock_hass):
        """Gen6 ohne Mode 1: keine Zwangsentladung — der Executor gibt dann
        frei, statt jede geplante Einspeisung scheitern zu lassen (Schreib-
        bremse, tägliche inverter_write-Telemetrie)."""
        mit = _netzlade_treiber(mock_hass, _mode1_vorhanden())
        assert mit.supports_forced_discharge is True
        werte = _mode1_vorhanden()
        del werte[TRIGGER_ENTITY]
        ohne = _netzlade_treiber(mock_hass, werte)
        assert ohne.supports_forced_discharge is False

    async def test_werte_und_reihenfolge(self, mock_hass, takt):
        inv = _netzlade_treiber(
            mock_hass,
            _mode1_vorhanden(**{SOC_SENSOR: "40", MAX_CURRENT_ENTITY: "25"}),
            {ACTIVE_POWER_ENTITY: {"max": 10000}},
        )
        assert await inv.async_set_grid_charge(3.04, target_soc=80) is True
        assert _mode1_schreibvorgaenge(mock_hass) == [
            ("select", "select_option",
             {"entity_id": SELECT_ENTITY, "option": "Enabled Battery Control"}),
            ("number", "set_value", {"entity_id": ACTIVE_POWER_ENTITY, "value": 3000}),
            ("number", "set_value", {"entity_id": DURATION_ENTITY, "value": MODE1_DAUER_S}),
            ("number", "set_value", {"entity_id": AUTOREPEAT_ENTITY, "value": MODE1_AUTOREPEAT_S}),
            ("button", "press", {"entity_id": TRIGGER_ENTITY}),
        ]
        assert inv.last_write_error is None

    async def test_leistung_am_maximum_gedeckelt(self, mock_hass, takt):
        inv = _netzlade_treiber(
            mock_hass,
            _mode1_vorhanden(**{SOC_SENSOR: "40", MAX_CURRENT_ENTITY: "25"}),
            {ACTIVE_POWER_ENTITY: {"max": 5000}},
        )
        await inv.async_set_grid_charge(8.0, target_soc=80)
        assert _calls_by_entity(mock_hass)[ACTIVE_POWER_ENTITY]["value"] == 5000

    async def test_import_limit_wird_nicht_geschrieben(self, mock_hass, takt):
        """Die Netzgrenze führt der Executor über die Leistung nach."""
        inv = _netzlade_treiber(mock_hass, _mode1_vorhanden(**{SOC_SENSOR: "40"}))
        await inv.async_set_grid_charge(2.0, target_soc=80)
        assert not any("import_limit" in eid for eid in _calls_by_entity(mock_hass))

    async def test_ladestrom_vorher_zurueck(self, mock_hass, takt):
        """Ein Ladelimit von 0 A aus dem Slot davor deckelte sonst auch die
        Mode-1-Ladung."""
        inv = _netzlade_treiber(
            mock_hass, _mode1_vorhanden(**{SOC_SENSOR: "40", MAX_CURRENT_ENTITY: "0"}),
            {MAX_CURRENT_ENTITY: {"max": 30}},
        )
        inv._state_store._data = {"battery_charge_max_current_original": 25.0}
        inv._state_store._loaded = True

        await inv.async_set_grid_charge(2.0, target_soc=80)

        aufrufe = _mode1_schreibvorgaenge(mock_hass)
        assert aufrufe[0] == (
            "number", "set_value", {"entity_id": MAX_CURRENT_ENTITY, "value": 25.0}
        )
        assert aufrufe[1][2]["option"] == "Enabled Battery Control"

    async def test_ladestrom_ohne_vorwert_auf_maximum(self, mock_hass, takt):
        inv = _netzlade_treiber(
            mock_hass, _mode1_vorhanden(**{SOC_SENSOR: "40", MAX_CURRENT_ENTITY: "0"}),
            {MAX_CURRENT_ENTITY: {"max": 30}},
        )
        await inv.async_set_grid_charge(2.0, target_soc=80)
        assert _calls_by_entity(mock_hass)[MAX_CURRENT_ENTITY]["value"] == 30.0

    async def test_eigener_ladestrom_des_betreibers_bleibt(self, mock_hass, takt):
        """Ohne gesicherten Vorwert hat der Treiber nichts verstellt — ein Wert
        über 0 ist die Einstellung des Betreibers."""
        inv = _netzlade_treiber(
            mock_hass, _mode1_vorhanden(**{SOC_SENSOR: "40", MAX_CURRENT_ENTITY: "18"}),
            {MAX_CURRENT_ENTITY: {"max": 30}},
        )
        await inv.async_set_grid_charge(2.0, target_soc=80)
        assert MAX_CURRENT_ENTITY not in _calls_by_entity(mock_hass)

    async def test_ziel_schon_erreicht_gibt_frei(self, mock_hass, takt):
        inv = _netzlade_treiber(mock_hass, _mode1_vorhanden(**{SOC_SENSOR: "80"}))
        assert await inv.async_set_grid_charge(2.0, target_soc=80) is True
        payloads = _calls_by_entity(mock_hass)
        assert payloads[SELECT_ENTITY]["option"] == "Disabled"
        assert payloads[ACTIVE_POWER_ENTITY]["value"] == 0
        assert takt.takte == []
        assert takt.beobachter == []

    async def test_unter_mindestleistung_wie_freigabe(self, mock_hass, takt):
        """0 W in Mode 1 hielte die Batterie fest — auch gegen PV-Überschuss."""
        inv = _netzlade_treiber(mock_hass, _mode1_vorhanden(**{SOC_SENSOR: "40"}))
        assert await inv.async_set_grid_charge(NETZLADEN_MIN_KW / 2, target_soc=80) is True
        payloads = _calls_by_entity(mock_hass)
        assert payloads[SELECT_ENTITY]["option"] == "Disabled"
        assert takt.takte == []

    async def test_nicht_endlich(self, mock_hass, takt):
        inv = _netzlade_treiber(mock_hass, _mode1_vorhanden(**{SOC_SENSOR: "40"}))
        assert await inv.async_set_grid_charge(float("nan"), target_soc=80) is False
        assert mock_hass.services.async_call.call_count == 0
        assert inv.last_write_error == "Sollwert nicht endlich"

    async def test_soc_beobachter_stoppt_am_ziel(self, mock_hass, takt):
        werte = _mode1_vorhanden(**{SOC_SENSOR: "60"})
        inv = _netzlade_treiber(mock_hass, werte)
        mock_hass.async_create_task = MagicMock()
        await inv.async_set_grid_charge(2.0, target_soc=80)
        assert takt.beobachter[0][0] == [SOC_SENSOR]

        # Ohne @callback führte HA den Rückruf im Executor-Thread aus, und
        # ``async_create_task`` von dort weist HA ab — der Stopp am Ziel
        # käme nie, Mode 1 lüde bis zum nächsten Takt weiter aus dem Netz.
        assert getattr(takt.beobachter[0][1], "_hass_callback", False) is True

        werte[SOC_SENSOR] = "70"
        takt.beobachter[0][1](None)
        mock_hass.async_create_task.assert_not_called()

        werte[SOC_SENSOR] = "80"
        mock_hass.services.async_call.reset_mock()
        takt.beobachter[0][1](None)
        await mock_hass.async_create_task.call_args.args[0]

        assert _calls_by_entity(mock_hass)[SELECT_ENTITY]["option"] == "Disabled"
        takt.soc_abmelder.assert_called_once()
        takt.abmelder.assert_called_once()
        assert inv._mode1_watt is None

    async def test_ohne_ziel_bis_100(self, mock_hass, takt):
        inv = _netzlade_treiber(mock_hass, _mode1_vorhanden(**{SOC_SENSOR: "60"}))
        await inv.async_set_grid_charge(2.0)
        assert inv._ziel_soc == 100.0

    async def test_keepalive_erneuert_und_endet_am_ziel(self, mock_hass, takt):
        werte = _mode1_vorhanden(**{SOC_SENSOR: "60"})
        inv = _netzlade_treiber(mock_hass, werte)
        await inv.async_set_grid_charge(2.0, target_soc=80)
        assert len(takt.takte) == 1

        mock_hass.services.async_call.reset_mock()
        await takt.takte[0][0](None)
        assert _calls_by_entity(mock_hass)[ACTIVE_POWER_ENTITY]["value"] == 2000

        # Am Ziel ändert sich der SOC womöglich nicht mehr — der Takt sieht
        # es trotzdem und gibt frei, statt weiter zu laden.
        werte[SOC_SENSOR] = "81"
        mock_hass.services.async_call.reset_mock()
        await takt.takte[0][0](None)
        payloads = _calls_by_entity(mock_hass)
        assert payloads[SELECT_ENTITY]["option"] == "Disabled"
        assert payloads[ACTIVE_POWER_ENTITY]["value"] == 0
        takt.abmelder.assert_called_once()

    async def test_gescheiterter_stopp_am_ziel_wird_wiederholt(self, mock_hass, takt):
        werte = _mode1_vorhanden(**{SOC_SENSOR: "60"})
        inv = _netzlade_treiber(mock_hass, werte)
        await inv.async_set_grid_charge(2.0, target_soc=80)

        werte[SOC_SENSOR] = "80"
        mock_hass.services.async_call = AsyncMock(side_effect=Exception("boom"))
        await takt.takte[0][0](None)
        takt.abmelder.assert_not_called()           # Takt bleibt als Wiederholung
        assert inv._mode1_watt is None              # lädt aber nicht mehr

        mock_hass.services.async_call = AsyncMock(return_value=None)
        await takt.takte[0][0](None)
        assert _calls_by_entity(mock_hass)[SELECT_ENTITY]["option"] == "Disabled"
        takt.abmelder.assert_called_once()

    async def test_stopp_beendet_netzladung(self, mock_hass, takt):
        inv = _netzlade_treiber(mock_hass, _mode1_vorhanden(**{SOC_SENSOR: "60"}))
        await inv.async_set_grid_charge(2.0, target_soc=80)
        assert await inv.async_stop_forcible() is True
        takt.abmelder.assert_called_once()
        takt.soc_abmelder.assert_called_once()
        assert inv._ziel_soc is None

    async def test_fehlerpfad(self, mock_hass, takt):
        """Scheitert der Trigger, bleibt nichts halb stehen: Die Freigabe-Folge
        läuft, kein Takt, kein Beobachter, ein Grund für die Telemetrie."""
        inv = _netzlade_treiber(mock_hass, _mode1_vorhanden(**{SOC_SENSOR: "60"}))
        erster = {"trigger": True}

        async def _call(domain, service, daten, blocking=True):
            if daten.get("entity_id") == TRIGGER_ENTITY and erster["trigger"]:
                erster["trigger"] = False
                raise RuntimeError("Modbus")

        mock_hass.services.async_call = AsyncMock(side_effect=_call)

        assert await inv.async_set_grid_charge(2.0, target_soc=80) is False
        assert inv.last_write_error == "Netzladung: RuntimeError"
        assert takt.takte == []
        assert takt.beobachter == []
        geschrieben = [c.args[2] for c in mock_hass.services.async_call.call_args_list]
        assert {"entity_id": SELECT_ENTITY, "option": "Disabled"} in geschrieben

    async def test_gescheiterter_stopp_wird_auch_unter_dem_ziel_wiederholt(
        self, mock_hass, takt
    ):
        """Fällt der SOC nach dem gescheiterten Stopp wieder unter das Ziel
        (die Hauslast zieht mit), sähe der Takt ohne eigenes Flag weder ein
        Ziel noch einen Befehl — und Mode 1 lüde weiter aus dem Netz."""
        werte = _mode1_vorhanden(**{SOC_SENSOR: "60"})
        inv = _netzlade_treiber(mock_hass, werte)
        await inv.async_set_grid_charge(2.0, target_soc=80)

        werte[SOC_SENSOR] = "80"
        mock_hass.services.async_call = AsyncMock(side_effect=Exception("boom"))
        await takt.takte[0][0](None)
        assert inv._stopp_offen is True

        werte[SOC_SENSOR] = "78"
        mock_hass.services.async_call = AsyncMock(return_value=None)
        await takt.takte[0][0](None)
        payloads = _calls_by_entity(mock_hass)
        assert payloads[SELECT_ENTITY]["option"] == "Disabled"
        assert payloads[ACTIVE_POWER_ENTITY]["value"] == 0
        takt.abmelder.assert_called_once()
        assert inv._stopp_offen is False

    async def test_dauerhaft_scheiternder_stopp_nur_einmal_mit_stacktrace(
        self, mock_hass, takt, caplog
    ):
        """Der Takt wiederholt einen gescheiterten Stopp alle 30 s. Nur der
        erste Fehlschlag kommt mit Stacktrace, danach eine knappe Warnung."""
        werte = _mode1_vorhanden(**{SOC_SENSOR: "60"})
        inv = _netzlade_treiber(mock_hass, werte)
        await inv.async_set_grid_charge(2.0, target_soc=80)
        werte[SOC_SENSOR] = "80"
        mock_hass.services.async_call = AsyncMock(side_effect=Exception("boom"))
        caplog.clear()
        for _ in range(3):
            await takt.takte[0][0](None)
        mit_stack = [r for r in caplog.records if r.exc_info]
        assert len(mit_stack) == 1
        assert "Stopp erneut fehlgeschlagen" in caplog.text
        assert inv._stopp_offen is True

    async def test_neuer_befehl_erledigt_den_offenen_stopp(self, mock_hass, takt):
        werte = _mode1_vorhanden(**{SOC_SENSOR: "60"})
        inv = _netzlade_treiber(mock_hass, werte)
        await inv.async_set_grid_charge(2.0, target_soc=80)
        werte[SOC_SENSOR] = "80"
        mock_hass.services.async_call = AsyncMock(side_effect=Exception("boom"))
        await takt.takte[0][0](None)

        mock_hass.services.async_call = AsyncMock(return_value=None)
        werte[SOC_SENSOR] = "70"
        assert await inv.async_set_discharge(1.0) is True
        assert inv._stopp_offen is False
        mock_hass.services.async_call.reset_mock()
        await takt.takte[-1][0](None)
        assert _calls_by_entity(mock_hass)[ACTIVE_POWER_ENTITY]["value"] == -1000

    async def test_unlesbarer_soc_warnt_einmal_je_befehl(self, mock_hass, takt, caplog):
        """Ohne SOC kein Stopp am Ziel — das soll im Log stehen, aber nicht
        alle vier Minuten."""
        werte = _mode1_vorhanden(**{SOC_SENSOR: "unavailable"})
        inv = _netzlade_treiber(mock_hass, werte)

        def _warnungen():
            return [r for r in caplog.records if "Ladestand nicht lesbar" in r.getMessage()]

        assert await inv.async_set_grid_charge(2.0, target_soc=80) is True
        await takt.takte[0][0](None)
        await takt.takte[0][0](None)
        assert len(_warnungen()) == 1

        await inv.async_set_grid_charge(3.0, target_soc=80)
        assert len(_warnungen()) == 2

    async def test_soc_erst_im_takt_unlesbar_warnt(self, mock_hass, takt, caplog):
        werte = _mode1_vorhanden(**{SOC_SENSOR: "60"})
        inv = _netzlade_treiber(mock_hass, werte)
        await inv.async_set_grid_charge(2.0, target_soc=80)
        assert not [r for r in caplog.records if "Ladestand nicht lesbar" in r.getMessage()]

        werte[SOC_SENSOR] = "unavailable"
        await takt.takte[0][0](None)
        assert [r for r in caplog.records if "Ladestand nicht lesbar" in r.getMessage()]


class TestEntladungFehlerpfade:
    """Was nach einem gescheiterten Entladebefehl im Gerät stehen bleibt."""

    async def test_ohne_mode1_scheitert_die_entladung_ehrlich(self, mock_hass):
        """Gen6: Ohne Mode-1-Entitäten gingen die Schreibvorgänge an
        Standard-IDs, die es nicht gibt. HA warnt dann nur — der Treiber
        meldete Erfolg, und erst Guard 3 merkte, dass nichts kommt."""
        werte = _mode1_vorhanden(**{FLOOR_ENTITY: "30.0"})
        del werte[TRIGGER_ENTITY]
        mock_hass.states.get = MagicMock(side_effect=_states(werte))
        inv = SolaXInverter(mock_hass, {})
        _install_noop_store(inv)

        assert await inv.async_set_discharge(2.0, target_soc=20) is False
        assert inv.last_write_error == "Mode 1 nicht verfügbar"
        mock_hass.services.async_call.assert_not_called()
        assert inv._state_store.original_discharge_floor is None

    async def test_erfolg_verwirft_den_alten_fehlergrund(self, inverter, mock_hass):
        inverter.last_write_error = "RuntimeError"
        assert await inverter.async_set_discharge(2.0) is True
        assert inverter.last_write_error is None

    async def test_ladelimit_und_stopp_verwerfen_den_alten_fehlergrund(
        self, inverter, mock_hass
    ):
        """Auch Ladelimit und Freigabe melden den Erfolg über ``_erfolg()`` —
        sonst trüge die Telemetrie einen längst behobenen Grund weiter."""
        inverter.last_write_error = "RuntimeError"
        assert await inverter.async_set_charge_limit(1.0) is True
        assert inverter.last_write_error is None
        inverter.last_write_error = "RuntimeError"
        assert await inverter.async_stop_forcible() is True
        assert inverter.last_write_error is None

    async def test_gescheiterter_neuer_befehl_stoppt_den_alten(
        self, inverter, mock_hass, takt
    ):
        """Der Takt ist beim neuen Befehl schon abgemeldet. Ohne Stopp liefe
        der alte Mode-1-Befehl ungetaktet bis zum Ablauf der Wiederholung
        weiter — über den zurückgesetzten Boden hinweg."""
        werte = _mode1_vorhanden(**{FLOOR_ENTITY: "30.0"})
        mock_hass.states.get = MagicMock(side_effect=_states(werte))
        assert await inverter.async_set_discharge(2.0, target_soc=40) is True
        werte[FLOOR_ENTITY] = "35.0"

        async def _call(domain, service, daten, blocking=True):
            if daten.get("entity_id") == ACTIVE_POWER_ENTITY and daten.get("value") == -3000:
                raise RuntimeError("Modbus")

        mock_hass.services.async_call = AsyncMock(side_effect=_call)
        assert await inverter.async_set_discharge(3.0, target_soc=20) is False
        assert inverter.last_write_error == "RuntimeError"

        geschrieben = [c.args[2] for c in mock_hass.services.async_call.call_args_list]
        assert {"entity_id": SELECT_ENTITY, "option": "Disabled"} in geschrieben
        assert {"entity_id": ACTIVE_POWER_ENTITY, "value": 0} in geschrieben
        assert {"entity_id": TRIGGER_ENTITY} in geschrieben
        assert _calls_by_entity(mock_hass)[FLOOR_ENTITY]["value"] == 30.0
        assert inverter._discharge_active is False
        assert len(takt.takte) == 1                 # kein neuer Takt
        assert inverter._mode1_watt is None
