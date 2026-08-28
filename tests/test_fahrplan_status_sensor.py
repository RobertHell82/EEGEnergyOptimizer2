"""Tests für den Fahrplan-Statussensor (Nachfolger des Entscheidungs-Sensors).

Die unique_id bleibt die des alten Entscheidungs-Sensors, damit Entität und
Verlaufshistorie erhalten bleiben. Der Sensor wird vom 30-Sekunden-Guard-Lauf
mit ScheduleExecutor.status() gefüttert.
"""

import pytest

from custom_components.eeg_energy_optimizer.const import DOMAIN
from custom_components.eeg_energy_optimizer.sensor import (
    FahrplanStatusSensor,
    fahrplan_kurzstatus,
)


def _status(**overrides):
    base = {
        "supported": True,
        "mode": "Ein",
        "status": "Laden begrenzt auf 2.00 kW (Fahrplanwert)",
        "last_run": "2026-08-24T19:00:00+02:00",
        "active_kind": "charge_limit",
        "written_charge_limit_kw": 2.0,
        "written_discharge_kw": None,
        "written_target_soc": None,
        "plan_action": {
            "kind": "charge_limit",
            "power_kw": 2.0,
            "target_soc": None,
            "slot": "2026-08-24T19:00:00+02:00",
        },
        "failsafe_released": False,
        "emergency_runs": 0,
        "emergency_blocked_slot": None,
        "write_failures": 0,
        "last_write_ok": True,
    }
    base.update(overrides)
    return base


def test_unique_id_bleibt_die_des_entscheidungs_sensors():
    """Entität + Verlaufshistorie müssen den Umbau überleben."""
    sensor = FahrplanStatusSensor("entry1")
    assert sensor._attr_unique_id == f"{DOMAIN}_entry1_entscheidung"


def test_kurzstatus_deckt_alle_zustaende_ab():
    assert fahrplan_kurzstatus(_status(supported=False)) == "Nur Anzeige"
    assert fahrplan_kurzstatus(_status(mode="Test")) == "Anzeige-Modus"
    assert fahrplan_kurzstatus(_status()) == "Laden begrenzt auf 2.0 kW"
    assert (
        fahrplan_kurzstatus(_status(written_charge_limit_kw=0.0))
        == "Laden blockiert"
    )
    # Angezeigt wird die geplante Einspeisung (2,0 kW), nicht der
    # Batterie-Sollwert (2,8 kW = Einspeisung + Hauslast − PV).
    assert (
        fahrplan_kurzstatus(
            _status(
                active_kind="discharge",
                written_discharge_kw=2.8,
                written_target_soc=43.0,
                plan_action={
                    "kind": "discharge",
                    "power_kw": 2.0,
                    "target_soc": 43.0,
                    "slot": "2026-08-24T19:00:00+02:00",
                },
            )
        )
        == "Einspeisung 2.00 kW bis 43 %"
    )
    # Ohne Plan (Slot verschwunden, Wert steht noch) bleibt der Sollwert
    assert (
        fahrplan_kurzstatus(
            _status(
                active_kind="discharge",
                written_discharge_kw=2.8,
                written_target_soc=43.0,
                plan_action=None,
            )
        )
        == "Entladung 2.80 kW bis 43 %"
    )
    assert fahrplan_kurzstatus(_status(active_kind="release")) == "Normalbetrieb"
    # Noch nichts geschrieben (Grace Period / Start) → Normalbetrieb
    assert fahrplan_kurzstatus(_status(active_kind=None)) == "Normalbetrieb"


def test_update_from_executor_setzt_state_und_attribute():
    sensor = FahrplanStatusSensor("entry1")

    kurz = sensor.update_from_executor(
        _status(
            active_kind="discharge",
            written_discharge_kw=2.8,
            written_target_soc=43.0,
            plan_action={
                "kind": "discharge",
                "power_kw": 2.0,
                "target_soc": 43.0,
                "slot": "2026-08-24T19:00:00+02:00",
            },
        )
    )

    assert kurz == "Einspeisung 2.00 kW bis 43 %"
    assert sensor.native_value == kurz
    attrs = sensor.extra_state_attributes
    assert attrs["gesteuert"] is True
    assert attrs["aktiv"] == "discharge"
    assert attrs["entladeleistung_kw"] == pytest.approx(2.8)
    assert attrs["ziel_soc"] == 43.0
    assert attrs["plan_aktion"] == "discharge"
    assert attrs["plan_leistung_kw"] == pytest.approx(2.0)
    assert attrs["failsafe"] is False
    assert attrs["notaus_gesperrt"] is False
    assert attrs["letzte_aktualisierung"] == "2026-08-24T19:00:00+02:00"


def test_notaus_und_failsafe_erscheinen_in_den_attributen():
    sensor = FahrplanStatusSensor("entry1")
    sensor.update_from_executor(
        _status(
            failsafe_released=True,
            emergency_blocked_slot="2026-08-24T19:00:00+02:00",
            write_failures=2,
            last_write_ok=False,
        )
    )
    attrs = sensor.extra_state_attributes
    assert attrs["failsafe"] is True
    assert attrs["notaus_gesperrt"] is True
    assert attrs["schreibfehler"] == 2
    assert attrs["letzter_schreibversuch_ok"] is False


def test_fehlende_plan_action_bricht_nicht():
    sensor = FahrplanStatusSensor("entry1")
    kurz = sensor.update_from_executor(
        _status(active_kind=None, plan_action=None, mode="Test")
    )
    assert kurz == "Anzeige-Modus"
    assert sensor.extra_state_attributes["plan_aktion"] is None
