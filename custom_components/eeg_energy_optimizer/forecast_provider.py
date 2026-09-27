"""PV forecast provider abstraction for EEG Energy Optimizer.

Supports reading PV production forecasts from Solcast Solar and
Forecast.Solar HA integrations via entity state reads.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


@dataclass
class PVForecast:
    """PV forecast data container."""

    remaining_today_kwh: float | None
    tomorrow_kwh: float | None


def _read_float(hass: HomeAssistant, entity_id: str) -> float | None:
    """Read a float value from an entity state.

    Returns None for missing, unavailable, unknown, or non-numeric states.
    """
    state = hass.states.get(entity_id)
    if state is None:
        return None
    if state.state in ("unknown", "unavailable", ""):
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None


class ForecastProvider(ABC):
    """Abstract base class for PV forecast providers."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    @abstractmethod
    def get_forecast(self) -> PVForecast:
        """Return current PV forecast. Must be overridden."""
        raise NotImplementedError


class SolcastProvider(ForecastProvider):
    """Read PV forecasts from Solcast Solar HA integration."""

    def __init__(
        self, hass: HomeAssistant, remaining_entity: str, tomorrow_entity: str
    ) -> None:
        super().__init__(hass)
        self._remaining_id = remaining_entity
        self._tomorrow_id = tomorrow_entity

    def get_forecast(self) -> PVForecast:
        """Return PV forecast from Solcast entity states."""
        return PVForecast(
            remaining_today_kwh=_read_float(self._hass, self._remaining_id),
            tomorrow_kwh=_read_float(self._hass, self._tomorrow_id),
        )


class ForecastSolarProvider(ForecastProvider):
    """Read PV forecasts from Forecast.Solar HA integration."""

    def __init__(
        self, hass: HomeAssistant, remaining_entity: str, tomorrow_entity: str
    ) -> None:
        super().__init__(hass)
        self._remaining_id = remaining_entity
        self._tomorrow_id = tomorrow_entity

    def get_forecast(self) -> PVForecast:
        """Return PV forecast from Forecast.Solar entity states."""
        return PVForecast(
            remaining_today_kwh=_read_float(self._hass, self._remaining_id),
            tomorrow_kwh=_read_float(self._hass, self._tomorrow_id),
        )


class EigenProvider(ForecastProvider):
    """Eigene Berechnung (pvprognose/): Werte kommen aus dem PvPrognoseProvider.

    Kein Entity-Lesen — die Reihe liegt im Speicher. Neben den beiden
    Tageswerten liefert der Provider auch die sieben Tagessummen fürs
    Wochendiagramm (``tage_kwh``) und seinen Stand (``status``), die der
    Sensor „PV-Prognose heute" als Attribute weitergibt.
    """

    def __init__(self, hass: HomeAssistant, prognose: Any) -> None:
        super().__init__(hass)
        self._prognose = prognose

    def get_forecast(self) -> PVForecast:
        return PVForecast(
            remaining_today_kwh=self._prognose.rest_heute_kwh(),
            tomorrow_kwh=self._prognose.morgen_kwh(),
        )

    def tage_kwh(self) -> list[float] | None:
        return self._prognose.tage_kwh()

    def status(self) -> dict[str, Any]:
        return self._prognose.status()
