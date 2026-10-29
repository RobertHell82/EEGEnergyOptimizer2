"""Spitzenreserve im Fahrplan: der Boden steigt — nur über HAConfig.

Haralds Modell bleibt unberührt: Die Reserve hebt ``min_soc_pct``, also die
Kapazität, die ``opt()`` als fehlend sieht. Geprüft wird, wann das passiert
(Funktion an UND Treiber mit Entladedeckel), wie weit (Maximalreserve,
Deckel des Mindest-Ladestands) und dass der Plan die Kurve für Executor und
Panel mitbringt.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.eeg_energy_optimizer import schedule as sched
from custom_components.eeg_energy_optimizer import spitzenreserve as sr

TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 8, 24, 19, 7, tzinfo=TZ)

CONFIG = {
    "battery_soc_sensor": "sensor.soc",
    "battery_capacity_kwh": 10.0,
    "discharge_power_kw": 5.0,
    "forecast_source": "solcast_solar",
    "schedule_min_soc_pct": 10,
    "spitzenkappung_enabled": True,
    "spitzenkappung_grenze_kw": 5.0,
}


def _coordinator(watt=400.0):
    c = MagicMock()
    c.bucket_avg = {b: {h: watt for h in range(24)} for b in ("wt", "we")}
    c.hourly_avg = {d: {h: watt for h in range(24)} for d in ("mo", "di", "mi", "do", "fr", "sa", "so")}
    c.hourly_for = lambda stamp: watt
    return c


def _wh_hours(start, hours=48):
    out = {}
    cursor = start.replace(minute=0, second=0, microsecond=0)
    for i in range(hours):
        t = cursor + timedelta(hours=i)
        wh = 0.0 if t.hour < 6 or t.hour > 20 else 6000.0 * (1 - abs(13 - t.hour) / 7)
        out[t.isoformat()] = round(max(0.0, wh), 1)
    return out


def _inverter(deckel=True):
    inv = MagicMock()
    inv.supports_schedule_control = True
    inv.supports_discharge_limit = deckel
    inv.get_max_discharge_power_kw = MagicMock(return_value=5.0)
    inv.get_backup_reserve_soc_pct = MagicMock(return_value=None)
    inv.has_combined_battery_state = False
    inv.get_combined_battery_state = MagicMock(return_value=(None, None))
    return inv


def _verlauf(kw_nachts: float, tage: int = 10) -> dict[str, float]:
    """Jede Nacht (19–06 Uhr) ``kw_nachts`` Nettolast, tagsüber 0."""
    verlauf = {}
    start = NOW.replace(minute=0) - timedelta(days=tage + 1)
    t = start
    while t < NOW:
        verlauf[sr.schluessel(t)] = kw_nachts if (t.hour >= 19 or t.hour < 6) else 0.0
        t += timedelta(minutes=15)
    return verlauf


def _reserve(verlauf: dict[str, float]) -> sr.Spitzenreserve:
    reserve = sr.Spitzenreserve(None, "entry1")
    reserve.verlauf = dict(verlauf)
    return reserve


def _hass(config, inverter, verlauf=None, spitze_kw=None, modus="Ein", soc="60"):
    hass = MagicMock()
    hass.data = {
        sched.DOMAIN: {
            "entry1": {
                "config": config,
                "coordinator": _coordinator(),
                "inverter": inverter,
                "spitzenreserve": _reserve(verlauf or {}),
                "select": SimpleNamespace(_attr_current_option=modus),
                "leistungsspitze": SimpleNamespace(
                    spitze={"kw": spitze_kw} if spitze_kw else None
                ),
            }
        }
    }

    def state_for(eid):
        if eid == "sensor.soc":
            return SimpleNamespace(state=soc, attributes={})
        return None

    hass.states.get.side_effect = state_for
    hass.states.async_all.return_value = []
    return hass


async def _sammeln(hass):
    with (
        patch.object(sched, "_now_local", return_value=NOW),
        patch.object(sched, "_async_solar_forecast_wh", AsyncMock(return_value=_wh_hours(NOW))),
    ):
        return await sched.async_collect_inputs(hass, "entry1")


async def test_ohne_funktion_bleibt_der_boden():
    cfg = {**CONFIG, "spitzenkappung_enabled": False}
    inputs, problem = await _sammeln(_hass(cfg, _inverter(), _verlauf(8.0)))
    assert problem is None
    assert inputs.min_soc_pct == 10.0
    assert inputs.min_soc_basis_pct is None
    assert inputs.spitzenreserve is None


async def test_treiber_ohne_deckel_bekommt_keine_reserve():
    inputs, _ = await _sammeln(_hass(CONFIG, _inverter(deckel=False), _verlauf(8.0)))
    assert inputs.min_soc_pct == 10.0
    assert inputs.spitzenreserve is None


async def test_reserve_hebt_den_boden_bis_zur_maximalreserve():
    """8 kW jede Nacht, Grenze 5 kW: 3 kW × 11 h ≈ 35 kWh über der Grenze —
    weit mehr, als 30 % von 10 kWh zulassen. Der Boden steigt um 30 Punkte."""
    inputs, problem = await _sammeln(_hass(CONFIG, _inverter(), _verlauf(8.0)))
    assert problem is None
    assert inputs.min_soc_basis_pct == 10.0
    assert inputs.min_soc_pct == pytest.approx(40.0)
    info = inputs.spitzenreserve
    assert info["plan_pct"] == 30.0
    assert info["plan_wirksam_pct"] == 30.0
    assert info["statistik"] is True
    assert info["tage"] >= sr.MIN_TAGE
    assert info["grenze_kw"] == 5.0
    # Viertelstunden über den ganzen Horizont (er folgt der Prognose).
    stempel = [datetime.fromisoformat(t) for t, _ in info["kurve"]]
    assert len(stempel) > 40 * 4
    assert all(b - a == timedelta(minutes=15) for a, b in zip(stempel, stempel[1:]))
    # Am Ende der Nacht (06:00) ist nichts mehr reserviert.
    morgen = [p for t, p in info["kurve"] if t.startswith("2026-08-25T07:")]
    assert morgen and all(p == 0.0 for p in morgen)


async def test_monatsspitze_ueber_dem_bedarf_braucht_keine_reserve():
    inputs, _ = await _sammeln(_hass(CONFIG, _inverter(), _verlauf(8.0), spitze_kw=9.0))
    assert inputs.spitzenreserve["grenze_kw"] == 9.0
    assert inputs.min_soc_pct == 10.0


async def test_ohne_statistik_gilt_die_mindestreserve():
    cfg = {**CONFIG, "spitzenkappung_reserve_min_pct": 5}
    inputs, _ = await _sammeln(_hass(cfg, _inverter(), {}))
    assert inputs.spitzenreserve["statistik"] is False
    assert inputs.min_soc_pct == pytest.approx(15.0)


async def test_boden_bleibt_unter_dem_deckel_des_mindest_ladestands():
    """Mindest-Ladestand 70 % + 30 % Reserve gingen über 80 % (Deckel 100
    minus nutzbares Band) — der Boden stoppt dort."""
    cfg = {**CONFIG, "schedule_min_soc_pct": 70}
    inputs, _ = await _sammeln(_hass(cfg, _inverter(), _verlauf(8.0), soc="95"))
    assert inputs.min_soc_basis_pct == 70.0
    assert inputs.min_soc_pct == pytest.approx(sched.max_min_soc_pct(cfg))
    assert inputs.spitzenreserve["plan_wirksam_pct"] == pytest.approx(10.0)


async def test_kaputte_statistik_kostet_keinen_plan():
    hass = _hass(CONFIG, _inverter(), _verlauf(8.0))
    with patch.object(sr, "reservekurve", side_effect=RuntimeError("kaputt")):
        inputs, problem = await _sammeln(hass)
    assert problem is None
    assert inputs.min_soc_pct == 10.0
    assert inputs.spitzenreserve is None


async def test_plan_traegt_reserve_und_basis():
    """Der gerechnete Plan bringt Kurve und Basis für Executor und Panel mit
    und plant nie unter dem gehobenen Boden."""
    inputs, _ = await _sammeln(_hass(CONFIG, _inverter(), _verlauf(8.0)))
    result = sched.solve(inputs)
    assert result["min_soc_basis_pct"] == 10.0
    assert result["min_soc_pct"] == pytest.approx(40.0)
    assert result["spitzenreserve"]["plan_pct"] == 30.0
    assert min(s["soc"] for s in result["slots"]) >= 40.0 - 0.5


async def test_im_modus_aus_keine_reserve():
    """Im Modus Aus läuft die Batterie bis zu ihrem eigenen Boden — der Plan
    (und mit ihm Referenz und Bilanz) darf keine Reserve annehmen."""
    inputs, _ = await _sammeln(_hass(CONFIG, _inverter(), _verlauf(8.0), modus="Aus"))
    assert inputs.min_soc_pct == 10.0
    assert inputs.spitzenreserve is None


async def test_boden_nie_ueber_dem_ladestand():
    """Nach einer Nacht in der Reserve steht die Batterie bei 18 %. Ein Boden
    von 40 % ließe opt() von leer rechnen und Einspeisung statt Laden
    planen — der Boden bleibt am Ladestand."""
    inputs, _ = await _sammeln(_hass(CONFIG, _inverter(), _verlauf(8.0), soc="18"))
    assert inputs.min_soc_pct == pytest.approx(18.0)
    assert inputs.spitzenreserve["plan_pct"] == 30.0
    assert inputs.spitzenreserve["plan_wirksam_pct"] == pytest.approx(8.0)


async def test_tagsueber_wird_die_abendreserve_zum_ladeziel():
    """Mittag mit Überschuss, Ladeziel aus: Die Batterie soll am Ende der
    PV-Zeit wenigstens Mindest-Ladestand + Abendreserve haben."""
    mittag = NOW.replace(hour=11, minute=7)
    cfg = {**CONFIG, "schedule_ladeziel_pct": 0}
    hass = _hass(cfg, _inverter(), _verlauf(8.0), soc="30")
    with (
        patch.object(sched, "_now_local", return_value=mittag),
        patch.object(sched, "_async_solar_forecast_wh", AsyncMock(return_value=_wh_hours(mittag))),
    ):
        inputs, problem = await sched.async_collect_inputs(hass, "entry1")
    assert problem is None
    assert inputs.spitzenreserve["im_ueberschuss"] is True
    assert inputs.ladeziel_pct == pytest.approx(40.0)
    assert inputs.min_soc_pct == pytest.approx(30.0)
