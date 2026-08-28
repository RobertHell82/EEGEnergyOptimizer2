"""Abstract base class for inverter battery control."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class InverterBase(ABC):
    """Abstract base class for inverter battery control.

    All inverter implementations must inherit from this class and implement
    the three write methods plus the is_available property.
    """

    def __init__(self, hass: Any, config: dict) -> None:
        """Initialize the inverter base.

        Args:
            hass: Home Assistant instance.
            config: Integration configuration dictionary.
        """
        self._hass = hass
        self._config = config
        self.register_writes: int = 0

    @abstractmethod
    async def async_set_charge_limit(self, power_kw: float) -> bool:
        """Set battery charge limit in kW.

        Instructs the inverter to charge the battery at up to power_kw.
        Returns True on success, False on failure.
        """

    @abstractmethod
    async def async_set_discharge(
        self, power_kw: float, target_soc: float | None = None
    ) -> bool:
        """Set battery discharge at given power in kW.

        Optional target_soc (0-100) as SOC floor for discharge.
        Returns True on success, False on failure.
        """

    @abstractmethod
    async def async_stop_forcible(self) -> bool:
        """Stop any forced charge/discharge, return to automatic mode.

        Returns True on success, False on failure.
        """

    @property
    @abstractmethod
    def is_available(self) -> bool:
        """Whether the inverter connection/service is available."""

    # ------------------------------------------------------------------
    # Optional: Fahrplan-Steuerschnittstelle (Schedule-Executor).
    # ------------------------------------------------------------------
    # Der ScheduleExecutor steuert nur Treiber, die diese Schnittstelle
    # vollständig anbieten (derzeit Huawei). Alle anderen Treiber bleiben im
    # Code und im UI, rechnen und zeigen an, steuern aber nicht — dafür
    # genügen die Defaults: supports_schedule_control=False und None-Rückgaben.

    @property
    def supports_schedule_control(self) -> bool:
        """Whether the schedule executor may control this inverter.

        Default False: der Fahrplan wird für diesen Treiber nur angezeigt.
        Ein Treiber, der True liefert, muss auch die drei Lese-Methoden
        unten implementieren — der Executor verlässt sich darauf.
        """
        return False

    async def async_get_charge_limit_kw(self) -> float | None:
        """Currently set battery charge limit in kW, or None if unreadable.

        Guard 1 (Ladelimit anheben, wenn die Einspeisung am Limit klebt)
        rechnet vom aktuell gesetzten Limit aus weiter — bei aktiver
        Abregelung ist die gemessene PV-Leistung bereits beschnitten, ein
        Neuberechnen aus PV − Hauslast − Grenze fiele systematisch zu klein
        aus. Default None: kein Leseweg vorhanden.
        """
        return None

    def get_charge_limit_max_kw(self) -> float | None:
        """Hardware maximum of the charge limit in kW, or None if unknown.

        Obergrenze für Guard 1 — höher anheben als die Hardware erlaubt
        schlägt beim Schreiben fehl. Default None: unbekannt (Guard hebt
        dann ungeclampt an).
        """
        return None

    def get_max_discharge_power_kw(self) -> float | None:
        """Hardware maximum discharge power in kW, or None if unknown.

        Obergrenze für Guard 2 (Entlade-Nachführung): geplante Einspeisung
        plus gemessene Hauslast darf die Entladeleistung der Batterie nicht
        übersteigen. Default None: unbekannt.
        """
        return None

    def get_control_entities(self) -> list[dict]:
        """Stellgrößen dieses Treibers für die Transparenz-Ansicht im Panel.

        Jeder Eintrag: ``{"label": str, "entity_id": str, "role": str}``.
        ``role`` ordnet die Zeile einer Steuerfunktion zu (``charge_limit``,
        ``discharge_limit``, ``forcible``, ``mode``, ``backup_soc``) — das
        Panel stellt daneben, welchen Wert wir zuletzt geschrieben haben.

        Default leer: Treiber ohne Fahrplan-Steuerung haben nichts zu zeigen.
        """
        return []

    def get_backup_reserve_soc_pct(self) -> float | None:
        """Notstrom-Ladestand (%), den das Gerät hardwareseitig zurückhält.

        Fließt als Untergrenze in den Fahrplan ein (der höhere Wert aus
        konfigurierter Blackout-Reserve und Geräte-Reserve gewinnt) — sonst
        plant der Fahrplan Entladungen, die das Gerät verweigert, und Plan
        und Ist laufen dauerhaft auseinander. Default None: unbekannt.
        """
        return None

    # ------------------------------------------------------------------
    # Optional: combined battery state for multi-inverter setups.
    # ------------------------------------------------------------------
    # Bei Multi-Inverter-Setups (aktuell nur SolarEdge mit i1+i2+…) liefert
    # jede Modbus-Integration nur den SOC einer einzelnen Batterie. Der
    # Optimizer braucht aber den kapazitätsgewichteten Gesamt-SOC und die
    # Gesamtkapazität — sonst entlädt er gegen einen falschen Maßstab
    # ("44 % SOC" bei i1 obwohl gewichtet nur 34.6 %).
    #
    # Default: (None, None) → der Driver hat keine Combined-Sicht (Huawei,
    # Fronius, SolaX: Single-Battery), Optimizer fällt auf den Config-Sensor
    # battery_soc_sensor + manual capacity zurück. Driver-Override liefert
    # ein Tupel ⇒ Optimizer überstimmt damit Config-Werte automatisch.
    def get_combined_battery_state(self) -> tuple[float | None, float | None]:
        """Return (combined_soc_pct, combined_capacity_kwh) or (None, None).

        Override in Multi-Battery-Drivers (z. B. SolarEdge) to provide a
        capacity-weighted SOC and the summed nominal capacity. Default
        (None, None) signals: no driver-side combination available — caller
        falls back to the configured battery_soc_sensor / battery_capacity_kwh.
        """
        return (None, None)

    @property
    def has_combined_battery_state(self) -> bool:
        """Whether this driver provides a combined battery state — STRUCTURAL.

        Decides whether the combined SOC/capacity sensors get created at setup.
        Must be structural (config-based), NOT value-based: the source
        integration's entities may not be populated yet at setup time (e.g.
        huawei_solar can take >10s to expose its sensors). A value-based check
        would then skip sensor creation permanently. Default: False (single-
        battery drivers). Override in multi-battery drivers.
        """
        return False
