"""Tests für die Telemetrie-Verdrahtung in __init__.py.

Nach dem Umbau "Fahrplan als einziger Aktor" laufen nur noch Profile und
Failures — State-Changes, Snapshots und Outcomes sind mit der Zustands-
Heuristik entfallen (UMBAU-FAHRPLAN.md, Risiko 5). Entsprechend testet diese
Datei: die Profil-Helfer, die Migrationen und den Failure-Callback des
Fahrplan-Executors (Schreibfehler → /v1/failure).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.eeg_energy_optimizer.const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_FORECAST_SOURCE,
    CONF_INVERTER_TYPE,
    CONF_TELEMETRY_ENABLED,
    MODE_EIN,
)
from custom_components.eeg_energy_optimizer.schedule_executor import ScheduleExecutor


# ---------------------------------------------------------------------------
# _resolve_integration_started_at preferences (W-3)
# ---------------------------------------------------------------------------
def test_resolve_integration_started_at_prefers_entry_created_at():
    from custom_components.eeg_energy_optimizer import _resolve_integration_started_at

    created = datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)
    entry = SimpleNamespace(created_at=created)
    result = _resolve_integration_started_at(entry, "2026-01-01T00:00:00+00:00")
    # Prefer entry.created_at -> ISO form (UTC)
    assert isinstance(result, str)
    assert result.startswith("2026-01-15T10:00:00")

    # Fallback when entry has no created_at
    entry2 = SimpleNamespace()
    result2 = _resolve_integration_started_at(entry2, "2026-01-01T00:00:00+00:00")
    assert result2 == "2026-01-01T00:00:00+00:00"

    # Neither -> None
    entry3 = SimpleNamespace()
    assert _resolve_integration_started_at(entry3, None) is None


# ---------------------------------------------------------------------------
# _build_telemetry_profile single source of truth (I-4 / W-3)
# ---------------------------------------------------------------------------
def test_profile_helper_single_source_of_truth():
    from custom_components.eeg_energy_optimizer import _build_telemetry_profile

    hass = MagicMock()
    hass.config = SimpleNamespace(country="AT")
    entry = SimpleNamespace(
        data={
            CONF_INVERTER_TYPE: "huawei_sun2000",
            CONF_BATTERY_CAPACITY_KWH: 10,
            CONF_FORECAST_SOURCE: "solcast_solar",
            "schedule_min_soc_pct": 15,
            "battery_soc_sensor": "sensor.foo",  # not whitelisted
        },
        options={},
        created_at=None,
    )
    p1 = _build_telemetry_profile(hass, entry, identity_registered_at="2026-01-01T00:00:00+00:00")
    p2 = _build_telemetry_profile(hass, entry, identity_registered_at="2026-01-01T00:00:00+00:00")
    assert p1 == p2
    assert p1["country_iso"] == "AT"
    assert p1["inverter_type"] == "huawei_sun2000"
    assert p1["battery_capacity_kwh"] == 10
    assert p1["forecast_provider"] == "solcast_solar"
    # settings filtered to whitelist
    assert p1["settings"].get("schedule_min_soc_pct") == 15
    assert "battery_soc_sensor" not in p1["settings"]


# ---------------------------------------------------------------------------
# Executor failure_callback wiring (W-4)
# ---------------------------------------------------------------------------
#
# Die Treiber fangen ihre Exceptions selbst und liefern False — der Executor
# meldet den Fehlschlag deshalb als Aktions-String (charge_limit / discharge /
# release) an den in __init__.py verdrahteten Callback.

TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 8, 24, 19, 0, tzinfo=TZ)


def _slot(battery_p, grid_p=0.0, soc=50.0):
    return {
        "t": NOW.isoformat(),
        "battery_p": battery_p,
        "grid_p": grid_p,
        "soc": soc,
        "consumption": 0.6,
    }


def _plan(*slots):
    return {
        "available": True,
        "error": None,
        "last_run": NOW.isoformat(),
        "slots": list(slots),
    }


def _executor(mock_hass, mock_inverter, callback):
    ex = ScheduleExecutor(
        mock_hass, "entry1", {"discharge_power_kw": 5.0}, mock_inverter,
        failure_callback=callback,
    )
    ex._created_at = NOW - timedelta(minutes=10)  # Grace Period vorbei
    return ex


def _no_measurements():
    import custom_components.eeg_energy_optimizer.schedule_executor as sx

    return (
        patch.object(sx, "compute_grid_export_kw", return_value=None),
        patch.object(sx, "compute_house_load_kw", return_value=0.8),
        patch.object(sx, "compute_pv_now_kw", return_value=None),
    )


async def test_executor_failure_callback_on_charge_limit_error(mock_hass, mock_inverter):
    mock_inverter.async_set_charge_limit.return_value = False
    callback = MagicMock()
    ex = _executor(mock_hass, mock_inverter, callback)

    p1, p2, p3 = _no_measurements()
    with p1, p2, p3:
        await ex.async_guard_cycle(_plan(_slot(battery_p=-2.0)), MODE_EIN, now=NOW)

    callback.assert_called_once_with("charge_limit")


async def test_executor_failure_callback_on_discharge_error(mock_hass, mock_inverter):
    mock_inverter.async_set_discharge.return_value = False
    callback = MagicMock()
    ex = _executor(mock_hass, mock_inverter, callback)

    p1, p2, p3 = _no_measurements()
    with p1, p2, p3:
        await ex.async_guard_cycle(
            _plan(_slot(battery_p=2.6, grid_p=2.0, soc=43.0)), MODE_EIN, now=NOW
        )

    callback.assert_called_once_with("discharge")


async def test_executor_failure_callback_on_release_error(mock_hass, mock_inverter):
    mock_inverter.async_stop_forcible.return_value = False
    callback = MagicMock()
    ex = _executor(mock_hass, mock_inverter, callback)

    p1, p2, p3 = _no_measurements()
    with p1, p2, p3:
        # battery_p > 0, grid_p <= 0 → Freigabe (Eigenverbrauchs-Entladung)
        await ex.async_guard_cycle(
            _plan(_slot(battery_p=0.5, grid_p=-0.1)), MODE_EIN, now=NOW
        )

    callback.assert_called_once_with("release")


async def test_executor_failure_callback_default_none(mock_hass, mock_inverter):
    """failure_callback defaults to None — kein Callback, kein Fehler."""
    mock_inverter.async_set_charge_limit.return_value = False
    ex = ScheduleExecutor(mock_hass, "entry1", {}, mock_inverter)
    ex._created_at = NOW - timedelta(minutes=10)

    p1, p2, p3 = _no_measurements()
    with p1, p2, p3:
        await ex.async_guard_cycle(_plan(_slot(battery_p=-2.0)), MODE_EIN, now=NOW)

    assert ex.write_failures == 1  # gezählt, aber kein Callback nötig


async def test_executor_failure_callback_never_raises(mock_hass, mock_inverter):
    """Ein werfender Callback darf den Guard-Lauf nicht abbrechen."""
    mock_inverter.async_set_charge_limit.return_value = False
    callback = MagicMock(side_effect=RuntimeError("boom"))
    ex = _executor(mock_hass, mock_inverter, callback)

    p1, p2, p3 = _no_measurements()
    with p1, p2, p3:
        await ex.async_guard_cycle(_plan(_slot(battery_p=-2.0)), MODE_EIN, now=NOW)

    assert callback.call_count == 1
    assert "Schreibfehler" in ex.last_status


# ---------------------------------------------------------------------------
# v12 → v13 migration adds telemetry_enabled
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_v12_to_v13_migration_adds_telemetry_enabled():
    from custom_components.eeg_energy_optimizer import async_migrate_entry

    hass = MagicMock()

    # Capture what async_update_entry was called with
    captured: dict = {}

    def _update(entry, *, data=None, version=None):
        captured["data"] = data
        captured["version"] = version
        # Mutate entry so subsequent migration steps see the new state
        entry.data = data
        entry.version = version

    hass.config_entries.async_update_entry = MagicMock(side_effect=_update)

    entry = SimpleNamespace(version=12, data={"some": "value"})
    ok = await async_migrate_entry(hass, entry)
    assert ok is True
    # v13 invariant: telemetry_enabled muss False als sicherer Default gesetzt sein.
    assert entry.data.get(CONF_TELEMETRY_ENABLED) is False
    assert entry.version >= 13


# ---------------------------------------------------------------------------
# pv_peak_kwp im Profile-Payload
# ---------------------------------------------------------------------------
def test_resolve_pv_peak_kwp_reads_config_value():
    """Wenn ``CONF_PV_PEAK_KWP`` gesetzt ist, gibt der Resolver einen float
    zurück. Leer / fehlend / 0 / negativ → None."""
    from custom_components.eeg_energy_optimizer import _resolve_pv_peak_kwp

    assert _resolve_pv_peak_kwp({"pv_peak_kwp": 9.9}) == 9.9
    assert _resolve_pv_peak_kwp({"pv_peak_kwp": "12.5"}) == 12.5
    assert _resolve_pv_peak_kwp({"pv_peak_kwp": None}) is None
    assert _resolve_pv_peak_kwp({"pv_peak_kwp": ""}) is None
    assert _resolve_pv_peak_kwp({"pv_peak_kwp": 0}) is None
    assert _resolve_pv_peak_kwp({"pv_peak_kwp": -1.0}) is None
    assert _resolve_pv_peak_kwp({"pv_peak_kwp": "abc"}) is None
    assert _resolve_pv_peak_kwp({}) is None


# ---------------------------------------------------------------------------
# Steuerungs-Kennung im Profil — das Backend wertet beide Varianten parallel
# aus, die produktive Integration sendet den Schlüssel nicht.
# ---------------------------------------------------------------------------
def test_profil_traegt_steuerungskennung():
    from custom_components.eeg_energy_optimizer import _build_telemetry_profile
    from custom_components.eeg_energy_optimizer.const import TELEMETRY_STEUERUNG

    hass = MagicMock()
    hass.config = SimpleNamespace(country="AT")
    entry = SimpleNamespace(data={CONF_INVERTER_TYPE: "huawei_sun2000"},
                            options={}, created_at=None)

    profile = _build_telemetry_profile(hass, entry, identity_registered_at=None)
    assert profile["settings"]["steuerung"] == TELEMETRY_STEUERUNG == "fahrplan"


def test_profil_whitelist_laesst_fahrplan_parameter_durch():
    """Die Zielfunktion des Fahrplans muss im Profil ankommen.

    Ohne Tarife sieht das Backend das Ergebnis einer Optimierung, deren
    Zielfunktion es nicht kennt. Sensor-Entitäten dürfen weiterhin nicht
    durchkommen.
    """
    from custom_components.eeg_energy_optimizer import _build_telemetry_profile

    hass = MagicMock()
    hass.config = SimpleNamespace(country="AT")
    entry = SimpleNamespace(
        data={
            CONF_INVERTER_TYPE: "huawei_sun2000",
            "schedule_feedin_source": "oemag",
            "schedule_feedin_price": 0.082,
            "schedule_consumption_price": 0.25,
            "schedule_grid_fee": 0.03,
            "schedule_battery_cost": 0.05,
            "schedule_night_start": "22:00",
            "schedule_night_end": "06:00",
            "schedule_ac_limit_kw": 8.8,
            "grid_export_limit_kw": 4.0,
            # Darf NICHT gesendet werden:
            "battery_soc_sensor": "sensor.geheim",
            "modbus_host": "192.168.1.50",
            # Wirkt nicht mehr, verschiebt nur eine Diagrammlinie:
            "discharge_a_start_time": "20:00",
        },
        options={},
        created_at=None,
    )

    settings = _build_telemetry_profile(
        hass, entry, identity_registered_at=None
    )["settings"]

    for key in (
        "schedule_feedin_source", "schedule_feedin_price",
        "schedule_consumption_price", "schedule_grid_fee",
        "schedule_battery_cost", "schedule_night_start",
        "schedule_night_end", "schedule_ac_limit_kw", "grid_export_limit_kw",
    ):
        assert key in settings, f"{key} fehlt im Profil"
    assert settings["schedule_feedin_price"] == 0.082
    for key in ("battery_soc_sensor", "modbus_host", "discharge_a_start_time"):
        assert key not in settings


# ---------------------------------------------------------------------------
# _snapshot_state — Zustandskennung aus den Maschinenfeldern des Executors
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status,erwartet",
    [
        # Reihenfolge: spezifisch vor allgemein.
        ({"supported": False, "active_kind": "discharge"}, "unsupported"),
        ({"emergency_blocked_slot": "2026-08-27T21:00", "active_kind": "discharge"},
         "emergency"),
        ({"failsafe_released": True, "active_kind": "charge_limit"}, "failsafe"),
        ({"active_kind": "charge_limit"}, "charge_limit"),
        ({"active_kind": "discharge"}, "discharge"),
        ({"active_kind": "release"}, "release"),
        ({"active_kind": None}, "normal"),
        ({}, "normal"),
        # supported=True darf nicht als "unsupported" gelesen werden.
        ({"supported": True}, "normal"),
    ],
)
def test_snapshot_state_reihenfolge(status, erwartet):
    from custom_components.eeg_energy_optimizer import _snapshot_state

    assert _snapshot_state(status) == erwartet


# ---------------------------------------------------------------------------
# _build_snapshot_payload — Schema unverändert gegenüber types.ts
# ---------------------------------------------------------------------------
def _snapshot_hass(werte: dict):
    """hass-Mock, dessen states.get die übergebenen Rohwerte liefert."""
    def _state(value):
        st = MagicMock()
        st.state = value
        st.attributes = {"unit_of_measurement": "kW"}
        return st

    states = {eid: _state(v) for eid, v in werte.items()}
    hass = MagicMock()
    hass.states.get = MagicMock(side_effect=lambda eid: states.get(eid))
    return hass


_SNAPSHOT_CONFIG = {
    CONF_INVERTER_TYPE: "huawei_sun2000",
    "pv_power_sensor": "sensor.pv",
    "battery_power_sensor": "sensor.bat",
    "grid_power_sensor": "sensor.grid",
}


def test_snapshot_payload_schema_und_werte():
    from custom_components.eeg_energy_optimizer import _build_snapshot_payload

    hass = _snapshot_hass({
        "sensor.pv": "5.0",
        "sensor.bat": "2.0",     # positiv = laden (Huawei)
        "sensor.grid": "1.0",    # positiv = Einspeisung (Huawei)
    })
    now = datetime(2026, 8, 27, 18, 30, tzinfo=timezone.utc)

    payload = _build_snapshot_payload(
        hass, _SNAPSHOT_CONFIG,
        {"min_soc_pct": 10.0},
        {"active_kind": "charge_limit", "supported": True},
        "ein", 64.4, now,
    )

    # Genau die Felder aus types.ts SnapshotPayload — nicht mehr, nicht weniger.
    assert set(payload) == {
        "ts", "state", "mode", "soc_pct", "pv_now_kw", "consumption_now_kw",
        "grid_now_kw", "battery_now_kw", "min_soc_dyn", "hysteresis",
    }
    assert payload["ts"] == "2026-08-27T18:30:00+00:00"
    assert payload["state"] == "charge_limit"
    assert payload["mode"] == "ein"
    assert payload["soc_pct"] == 64        # ganzzahlig, wie die Spalte
    assert payload["pv_now_kw"] == 5.0
    assert payload["battery_now_kw"] == 2.0
    assert payload["grid_now_kw"] == 1.0
    assert payload["consumption_now_kw"] == 2.0   # 5 − 2 − 1
    assert payload["min_soc_dyn"] == 10
    assert payload["hysteresis"] is None          # der Fahrplan hat keine


def test_snapshot_payload_unlesbare_sensoren_werden_none_nicht_null():
    """Ein fehlender Sensor ist keine 0 kW — sonst rechnet die Auswertung falsch."""
    from custom_components.eeg_energy_optimizer import _build_snapshot_payload

    hass = _snapshot_hass({})
    payload = _build_snapshot_payload(
        hass, _SNAPSHOT_CONFIG, None, {}, "test", None,
        datetime(2026, 8, 27, 3, 0, tzinfo=timezone.utc),
    )
    assert payload["battery_now_kw"] is None
    assert payload["grid_now_kw"] is None
    assert payload["consumption_now_kw"] is None
    assert payload["soc_pct"] is None
    assert payload["min_soc_dyn"] is None      # ohne Plan kein Mindestwert
    assert payload["state"] == "normal"
    assert payload["mode"] == "test"


def test_snapshot_min_soc_kommt_aus_dem_plan_nicht_aus_der_konfiguration():
    """Gerechnet wird mit dem gekappten Wert — der steht nur im Ergebnis."""
    from custom_components.eeg_energy_optimizer import _build_snapshot_payload

    hass = _snapshot_hass({})
    config = dict(_SNAPSHOT_CONFIG, schedule_min_soc_pct=90)  # ungekappt
    payload = _build_snapshot_payload(
        hass, config, {"min_soc_pct": 30.0}, {}, "ein", 50,
        datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc),
    )
    assert payload["min_soc_dyn"] == 30


# ---------------------------------------------------------------------------
# _check_schedule_health — Störungsbilder des Fahrplans
# ---------------------------------------------------------------------------
def _sammler():
    """Melder-Ersatz: sammelt die Aufrufe, statt sie zu senden."""
    calls = []

    def emit(**kwargs):
        calls.append(kwargs)

    return calls, emit


def test_schedule_health_meldet_solver_fehler_nur_gekuerzt():
    from custom_components.eeg_energy_optimizer import _check_schedule_health
    from custom_components.eeg_energy_optimizer.const import (
        FAILURE_PERSISTENT_DEDUP_WINDOW_S,
    )

    calls, emit = _sammler()
    _check_schedule_health(
        {"error": "ValueError: /config/eeg_optimizer_plaene/2026-08-27.json fehlt"},
        {"supported": True}, MODE_EIN, {CONF_INVERTER_TYPE: "huawei_sun2000"},
        {}, emit,
    )
    assert len(calls) == 1
    meldung = calls[0]
    assert meldung["category"] == "schedule_solver"
    assert meldung["message_hash"] == "ValueError"
    # Der Pfad darf das Gerät nicht verlassen.
    assert "/config/" not in str(meldung["context"])
    assert meldung["dedup_window_s"] == FAILURE_PERSISTENT_DEDUP_WINDOW_S


def test_schedule_health_eigener_klartext_bleibt_lesbar():
    from custom_components.eeg_energy_optimizer import _check_schedule_health

    calls, emit = _sammler()
    _check_schedule_health(
        {"error": "Verbrauchsprofil noch nicht geladen"},
        {"supported": True}, MODE_EIN, {}, {}, emit,
    )
    assert calls[0]["context"]["grund"] == "Verbrauchsprofil noch nicht geladen"


def test_schedule_health_erholung_loescht_den_dedup_schluessel():
    """Nach einem geglückten Lauf meldet der nächste Fehler sofort wieder."""
    from custom_components.eeg_energy_optimizer import _check_schedule_health

    dedup = {("schedule_solver", "ValueError"): datetime.now(timezone.utc),
             ("sensor_unavailable", "pv_power"): datetime.now(timezone.utc)}
    calls, emit = _sammler()
    _check_schedule_health(
        {"error": None, "available": True}, {"supported": True},
        MODE_EIN, {}, dedup, emit,
    )
    assert ("schedule_solver", "ValueError") not in dedup
    # Fremde Kategorien bleiben unberührt.
    assert ("sensor_unavailable", "pv_power") in dedup
    assert calls == []


def test_schedule_health_nicht_gesteuerter_treiber_stoppt_folgepruefungen():
    """Ohne Steuerung sagen Failsafe und Not-Aus nichts — nur eine Meldung."""
    from custom_components.eeg_energy_optimizer import _check_schedule_health

    calls, emit = _sammler()
    _check_schedule_health(
        {"error": None},
        {"supported": False, "failsafe_released": True,
         "emergency_blocked_slot": "2026-08-27T21:00"},
        MODE_EIN, {CONF_INVERTER_TYPE: "sma_smart_energy"}, {}, emit,
    )
    assert [c["category"] for c in calls] == ["inverter_unsupported"]
    assert calls[0]["message_hash"] == "sma_smart_energy"


def test_schedule_health_meldet_veralteten_plan_und_not_aus():
    from custom_components.eeg_energy_optimizer import _check_schedule_health

    calls, emit = _sammler()
    _check_schedule_health(
        {"error": None, "last_run": "2026-08-27T20:00:00+02:00"},
        {"supported": True, "failsafe_released": True,
         "emergency_blocked_slot": "2026-08-27T21:00", "emergency_runs": 3},
        MODE_EIN, {}, {}, emit,
    )
    kategorien = [c["category"] for c in calls]
    assert kategorien == ["schedule_stale", "guard_emergency"]
    assert calls[0]["context"]["last_run"] == "2026-08-27T20:00:00+02:00"
    assert calls[1]["message_hash"] == "2026-08-27T21:00"
    assert calls[1]["context"]["emergency_runs"] == 3


def test_schedule_health_failsafe_erholung_loescht_schluessel():
    from custom_components.eeg_energy_optimizer import _check_schedule_health

    dedup = {("schedule_stale", "failsafe_released"): datetime.now(timezone.utc)}
    calls, emit = _sammler()
    _check_schedule_health(
        {"error": None}, {"supported": True, "failsafe_released": False},
        MODE_EIN, {}, dedup, emit,
    )
    assert dedup == {}
    assert calls == []


def test_schedule_health_schweigt_im_anzeige_modus():
    """Im Modus Test ist keiner dieser Zustände ein Problem."""
    from custom_components.eeg_energy_optimizer import _check_schedule_health
    from custom_components.eeg_energy_optimizer.const import MODE_TEST

    calls, emit = _sammler()
    _check_schedule_health(
        {"error": "ValueError: kaputt"},
        {"supported": False, "failsafe_released": True,
         "emergency_blocked_slot": "x"},
        MODE_TEST, {}, {}, emit,
    )
    assert calls == []
