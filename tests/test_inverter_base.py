"""Tests for InverterBase ABC contract (INF-01)."""

import pytest

from custom_components.eeg_energy_optimizer.inverter.base import InverterBase


class TestInverterBaseABC:
    """Verify the abstract base class enforces all required methods."""

    def test_cannot_instantiate_base_directly(self, mock_hass):
        """InverterBase cannot be instantiated directly."""
        with pytest.raises(TypeError):
            InverterBase(mock_hass, {})

    def test_incomplete_subclass_missing_charge_limit(self, mock_hass):
        """A subclass missing async_set_charge_limit raises TypeError."""

        class Incomplete(InverterBase):
            async def async_set_discharge(self, power_kw, target_soc=None):
                return True

            async def async_stop_forcible(self):
                return True

            @property
            def is_available(self):
                return True

        with pytest.raises(TypeError):
            Incomplete(mock_hass, {})

    def test_incomplete_subclass_missing_discharge(self, mock_hass):
        """A subclass missing async_set_discharge raises TypeError."""

        class Incomplete(InverterBase):
            async def async_set_charge_limit(self, power_kw):
                return True

            async def async_stop_forcible(self):
                return True

            @property
            def is_available(self):
                return True

        with pytest.raises(TypeError):
            Incomplete(mock_hass, {})

    def test_incomplete_subclass_missing_stop(self, mock_hass):
        """A subclass missing async_stop_forcible raises TypeError."""

        class Incomplete(InverterBase):
            async def async_set_charge_limit(self, power_kw):
                return True

            async def async_set_discharge(self, power_kw, target_soc=None):
                return True

            @property
            def is_available(self):
                return True

        with pytest.raises(TypeError):
            Incomplete(mock_hass, {})

    def test_incomplete_subclass_missing_is_available(self, mock_hass):
        """A subclass missing is_available raises TypeError."""

        class Incomplete(InverterBase):
            async def async_set_charge_limit(self, power_kw):
                return True

            async def async_set_discharge(self, power_kw, target_soc=None):
                return True

            async def async_stop_forcible(self):
                return True

        with pytest.raises(TypeError):
            Incomplete(mock_hass, {})

    def test_complete_subclass_instantiates(self, mock_hass):
        """A complete subclass implementing all 4 members can be instantiated."""

        class Complete(InverterBase):
            async def async_set_charge_limit(self, power_kw):
                return True

            async def async_set_discharge(self, power_kw, target_soc=None):
                return True

            async def async_stop_forcible(self):
                return True

            @property
            def is_available(self):
                return True

        inverter = Complete(mock_hass, {"test": "config"})
        assert isinstance(inverter, InverterBase)
        assert inverter._hass is mock_hass
        assert inverter._config == {"test": "config"}


class TestScheduleControlInterface:
    """Fahrplan-Steuerschnittstelle: Defaults für nicht gesteuerte Treiber.

    Treiber ohne eigene Umsetzung rechnen und zeigen an, steuern aber nicht:
    supports_schedule_control=False, alle Lesewege liefern None.
    """

    def _complete(self, mock_hass):
        class Complete(InverterBase):
            async def async_set_charge_limit(self, power_kw):
                return True

            async def async_set_discharge(self, power_kw, target_soc=None):
                return True

            async def async_stop_forcible(self):
                return True

            @property
            def is_available(self):
                return True

        return Complete(mock_hass, {})

    def test_schedule_control_defaults_to_false(self, mock_hass):
        assert self._complete(mock_hass).supports_schedule_control is False

    async def test_read_paths_default_to_none(self, mock_hass):
        inv = self._complete(mock_hass)
        assert await inv.async_get_charge_limit_kw() is None
        assert inv.get_charge_limit_max_kw() is None
        assert inv.get_max_discharge_power_kw() is None
        assert inv.get_backup_reserve_soc_pct() is None


class TestNichtEndlich:
    """NaN/Inf wird vor jedem Schreiben abgewiesen, mit Grund."""

    def _inv(self, mock_hass):
        class Voll(InverterBase):
            async def async_set_charge_limit(self, power_kw):
                return True

            async def async_set_discharge(self, power_kw, target_soc=None):
                return True

            async def async_stop_forcible(self):
                return True

            @property
            def is_available(self):
                return True

        return Voll(mock_hass, {})

    @pytest.mark.parametrize("werte", [
        (float("nan"),), (float("inf"),), (2.0, float("-inf")), ("x",),
    ])
    def test_nicht_endlich_wird_erkannt(self, mock_hass, werte):
        inv = self._inv(mock_hass)
        assert inv._nicht_endlich(*werte) is True
        assert inv.last_write_error == "Sollwert nicht endlich"

    def test_endlich_und_none_sind_erlaubt(self, mock_hass):
        inv = self._inv(mock_hass)
        assert inv._nicht_endlich(2.5, None, 0) is False
        assert inv.last_write_error is None


@pytest.mark.parametrize("aufruf", [
    ("async_set_charge_limit", (float("nan"),)),
    ("async_set_discharge", (float("inf"), 20)),
    ("async_set_discharge", (2.0, float("nan"))),
])
async def test_huawei_schreibt_keinen_nicht_endlichen_sollwert(mock_hass, aufruf):
    from custom_components.eeg_energy_optimizer.inverter.huawei import HuaweiInverter

    inv = HuaweiInverter(mock_hass, {"huawei_device_id": "dev"})
    name, args = aufruf
    assert await getattr(inv, name)(*args) is False
    mock_hass.services.async_call.assert_not_called()
