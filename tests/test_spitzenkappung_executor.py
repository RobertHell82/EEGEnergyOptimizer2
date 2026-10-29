"""Spitzenkappung im Executor: der Entladedeckel in der Spitzenreserve.

Geprüft wird das Zusammenspiel mit dem Guard-Lauf — wann der Deckel
geschrieben, nachgeführt und aufgehoben wird, dass er vor jeder erzwungenen
Entladung weg ist und dass jede Freigabe ihn mitnimmt. Die Rechnung selbst
prüft test_spitzenreserve.py.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from custom_components.eeg_energy_optimizer import schedule_executor as sx
from custom_components.eeg_energy_optimizer.const import DOMAIN, MODE_AUS, MODE_EIN
from custom_components.eeg_energy_optimizer.schedule_executor import ScheduleExecutor

TZ = timezone(timedelta(hours=1))
NOW = datetime(2026, 10, 20, 21, 0, tzinfo=TZ)
SOC_SENSOR = "sensor.batterie_soc"

CFG = {
    "discharge_power_kw": 5.0,
    "battery_soc_sensor": SOC_SENSOR,
    "spitzenkappung_enabled": True,
    "spitzenkappung_grenze_kw": 5.0,
}


def _slot(offset_min=0, battery_p=0.5, grid_p=0.0, soc=40.0):
    return {
        "t": (NOW + timedelta(minutes=offset_min)).isoformat(),
        "battery_p": battery_p,
        "grid_p": grid_p,
        "soc": soc,
        "consumption": 0.6,
        "heizstab": 0.0,
    }


def _state(*slots, reserve_pct=20.0, basis=10.0, mit_reserve=True):
    payload = {
        "available": True,
        "error": None,
        "last_run": NOW.isoformat(),
        "slots": list(slots) or [_slot()],
    }
    if mit_reserve:
        payload["min_soc_basis_pct"] = basis
        payload["min_soc_pct"] = basis + reserve_pct
        payload["spitzenreserve"] = {
            "kurve": [
                [(NOW + timedelta(minutes=15 * i)).isoformat(), reserve_pct]
                for i in range(-1, 8)
            ],
            "min_pct": 0.0,
            "max_pct": 30.0,
            "plan_pct": reserve_pct,
        }
    return payload


@pytest.fixture
def inverter(mock_inverter):
    mock_inverter.supports_discharge_limit = True
    mock_inverter.get_max_discharge_power_kw = MagicMock(return_value=5.0)
    return mock_inverter


def _hass(mock_hass, soc=25.0, spitze_kw=None, laufend=None):
    def get(eid):
        if eid == SOC_SENSOR and soc is not None:
            return SimpleNamespace(state=str(soc), attributes={})
        return None

    mock_hass.states.get = MagicMock(side_effect=get)
    ls = SimpleNamespace(
        spitze={"kw": spitze_kw} if spitze_kw is not None else None,
        laufend=lambda _t: laufend,
    )
    mock_hass.data = {DOMAIN: {"entry1": {"leistungsspitze": ls}}}
    return mock_hass


def _executor(hass, inverter, config=None, offen=False):
    ex = ScheduleExecutor(hass, "entry1", dict(config or CFG), inverter)
    ex._created_at = NOW - timedelta(minutes=10)
    ex._start_release_pending = False
    ex._entladedeckel_offen = offen
    return ex


@contextmanager
def _messung(export=0.0, batt=0.0):
    """export: positiv = Einspeisung; batt: positiv = Laden."""
    with (
        patch.object(sx, "compute_grid_export_kw", return_value=export),
        patch.object(sx, "compute_battery_now_kw", return_value=batt),
        patch.object(sx, "compute_house_load_kw", return_value=0.6),
        patch.object(sx, "compute_pv_now_kw", return_value=0.0),
    ):
        yield


async def test_in_der_reserve_haelt_die_batterie(mock_hass, inverter):
    """Ladestand 25 % ≤ 10 % + 20 %: Grundlast 1 kW kommt aus dem Netz —
    Deckel 0."""
    ex = _executor(_hass(mock_hass, soc=25.0), inverter)
    with _messung(export=-1.0, batt=0.0):
        await ex.async_guard_cycle(_state(), MODE_EIN, now=NOW)
    inverter.async_set_discharge_limit.assert_awaited_once_with(0.0)
    assert ex._written_entladedeckel_kw == 0.0
    assert "Spitzenkappung" in ex.last_status
    assert ex.spitzenkappung["aktiv"] is True
    assert ex.spitzenkappung["schwelle_pct"] == 30.0


async def test_spitze_in_der_reserve_wird_gedeckt(mock_hass, inverter):
    """8 kW Bedarf (6 kW Netz + 2 kW Batterie), erlaubt 4,7 kW → die
    Batterie deckt 3,3 kW / Wirkungsgrad."""
    ex = _executor(_hass(mock_hass, soc=25.0), inverter)
    with _messung(export=-6.0, batt=-2.0):
        await ex.async_guard_cycle(_state(), MODE_EIN, now=NOW)
    (deckel,), _ = inverter.async_set_discharge_limit.await_args
    assert deckel == pytest.approx(3.47, abs=0.01)


async def test_monatsspitze_hebt_die_grenze(mock_hass, inverter):
    """Liegt die Monatsspitze schon bei 9 kW, kostet bis dahin nichts mehr:
    8 kW Bedarf → die Batterie hält."""
    ex = _executor(_hass(mock_hass, soc=25.0, spitze_kw=9.0), inverter)
    with _messung(export=-8.0, batt=0.0):
        await ex.async_guard_cycle(_state(), MODE_EIN, now=NOW)
    inverter.async_set_discharge_limit.assert_awaited_once_with(0.0)
    assert ex.spitzenkappung["grenze_kw"] == 9.0


async def test_totband_verhindert_dauerschreiben(mock_hass, inverter):
    ex = _executor(_hass(mock_hass, soc=25.0), inverter)
    with _messung(export=-6.0, batt=-2.0):
        await ex.async_guard_cycle(_state(), MODE_EIN, now=NOW)
    with _messung(export=-6.1, batt=-2.0):
        await ex.async_guard_cycle(_state(), MODE_EIN, now=NOW + timedelta(seconds=30))
    assert inverter.async_set_discharge_limit.await_count == 1
    # Von Spitze zu Halten wird immer geschrieben, auch unter dem Totband.
    ex._written_entladedeckel_kw = 0.15
    with _messung(export=-1.0, batt=0.0):
        await ex.async_guard_cycle(_state(), MODE_EIN, now=NOW + timedelta(seconds=60))
    assert inverter.async_set_discharge_limit.await_args == call(0.0)


async def test_ueber_der_schwelle_wird_der_deckel_aufgehoben(mock_hass, inverter):
    ex = _executor(_hass(mock_hass, soc=25.0), inverter)
    with _messung(export=-1.0, batt=0.0):
        await ex.async_guard_cycle(_state(), MODE_EIN, now=NOW)
    _hass(mock_hass, soc=40.0)
    with _messung(export=-1.0, batt=0.0):
        await ex.async_guard_cycle(_state(), MODE_EIN, now=NOW + timedelta(seconds=30))
    assert inverter.async_set_discharge_limit.await_args == call(None)
    assert ex._written_entladedeckel_kw is None
    assert ex.spitzenkappung["aktiv"] is False


async def test_vor_einer_erzwungenen_entladung_ist_der_deckel_weg(mock_hass, inverter):
    """Eine geplante Einspeisung liefe gegen den Deckel — er wird zuerst
    aufgehoben, dann wird entladen."""
    ex = _executor(_hass(mock_hass, soc=25.0), inverter)
    with _messung(export=-1.0, batt=0.0):
        await ex.async_guard_cycle(_state(), MODE_EIN, now=NOW)
    manager = MagicMock()
    manager.attach_mock(inverter.async_set_discharge_limit, "deckel")
    manager.attach_mock(inverter.async_set_discharge, "entladen")
    entladung = _state(_slot(battery_p=3.0, grid_p=2.0, soc=35.0))
    with _messung(export=-1.0, batt=0.0):
        await ex.async_guard_cycle(entladung, MODE_EIN, now=NOW + timedelta(seconds=30))
    namen = [c[0] for c in manager.mock_calls]
    assert namen == ["deckel", "entladen"]
    assert manager.mock_calls[0] == call.deckel(None)
    assert ex.spitzenkappung is None


async def test_scheitert_das_aufheben_wird_trotzdem_entladen(mock_hass, inverter):
    """Eine klemmende Entität darf keine geplante Einspeisung sperren: Bei
    Fronius/SMA ersetzt der Befehl den Deckel, bei Huawei meldet Guard 3 eine
    wirkungslose Entladung. Der Deckel bleibt als unbekannt vorgemerkt."""
    ex = _executor(_hass(mock_hass, soc=25.0), inverter)
    ex._written_entladedeckel_kw = 0.0
    inverter.async_set_discharge_limit = AsyncMock(return_value=False)
    entladung = _state(_slot(battery_p=3.0, grid_p=2.0, soc=35.0))
    with _messung():
        await ex.async_guard_cycle(entladung, MODE_EIN, now=NOW)
    inverter.async_set_discharge.assert_awaited_once()
    assert ex._entladedeckel_offen is True
    assert ex.hat_eingriff is True


async def test_modus_aus_hebt_den_deckel_auf(mock_hass, inverter):
    ex = _executor(_hass(mock_hass, soc=25.0), inverter)
    with _messung(export=-1.0, batt=0.0):
        await ex.async_guard_cycle(_state(), MODE_EIN, now=NOW)
    assert ex._active_kind == "release"
    with _messung():
        await ex.async_guard_cycle(_state(), MODE_AUS, now=NOW + timedelta(seconds=30))
    assert inverter.async_set_discharge_limit.await_args == call(None)
    assert ex._written_entladedeckel_kw is None
    assert ex.hat_eingriff is False


async def test_freigabe_nimmt_den_deckel_mit(mock_hass, inverter):
    ex = _executor(_hass(mock_hass, soc=25.0), inverter)
    ex._written_entladedeckel_kw = 0.0
    assert ex.hat_eingriff is True
    assert await ex.async_release() is True
    assert inverter.async_set_discharge_limit.await_args == call(None)
    # Scheitert das Aufheben, gilt der Stopp trotzdem — der Deckel bleibt
    # als unbekannt vorgemerkt und wird gebremst erneut aufgehoben.
    ex._written_entladedeckel_kw = 0.0
    inverter.async_set_discharge_limit = AsyncMock(return_value=False)
    assert await ex.async_release() is True
    assert ex._entladedeckel_offen is True
    assert ex.hat_eingriff is True


async def test_gescheitertes_setzen_wird_bei_aus_aufgehoben(mock_hass, inverter):
    """Huawei mit zwei Geräten: eines bekommt den Deckel, das andere nicht —
    der Executor weiß nicht, was steht, und hebt im Modus Aus sicher auf."""
    ex = _executor(_hass(mock_hass, soc=25.0), inverter)
    inverter.async_set_discharge_limit = AsyncMock(return_value=False)
    with _messung(export=-1.0):
        await ex.async_guard_cycle(_state(), MODE_EIN, now=NOW)
    assert ex._entladedeckel_offen is True
    inverter.async_set_discharge_limit = AsyncMock(return_value=True)
    ex._deckel_warten = 0
    with _messung():
        await ex.async_guard_cycle(_state(), MODE_AUS, now=NOW + timedelta(seconds=30))
    assert inverter.async_set_discharge_limit.await_args == call(None)
    assert ex._entladedeckel_offen is False


async def test_nach_gestopptem_stopp_wird_der_deckel_neu_geschrieben(mock_hass, inverter):
    """Fronius und SMA vergessen den Deckel beim Stopp einer Entladung. Der
    Executor darf danach nicht glauben, er stünde noch."""
    ex = _executor(_hass(mock_hass, soc=25.0), inverter)
    ex._active_kind = "discharge"
    ex._written_entladedeckel_kw = 0.0
    laden = _state(_slot(battery_p=-1.0, grid_p=1.0, soc=26.0))
    with _messung(export=-1.0, batt=0.0):
        await ex.async_guard_cycle(laden, MODE_EIN, now=NOW)
    assert inverter.async_set_discharge_limit.await_args == call(0.0)
    assert ex._entladedeckel_offen is False


async def test_kein_deckel_wenn_der_stopp_scheitert(mock_hass, inverter):
    ex = _executor(_hass(mock_hass, soc=25.0), inverter)
    ex._active_kind = "discharge"
    inverter.async_stop_forcible = AsyncMock(return_value=False)
    laden = _state(_slot(battery_p=-1.0, grid_p=1.0, soc=26.0))
    with _messung(export=-1.0):
        await ex.async_guard_cycle(laden, MODE_EIN, now=NOW)
    inverter.async_set_discharge_limit.assert_not_awaited()


async def test_nach_dem_start_wird_ein_unbekannter_deckel_aufgehoben(mock_hass, inverter):
    """Ein Huawei behält die Number-Entität über einen Absturz — mit
    eingeschalteter Funktion hebt die erste Freigabe sie auf."""
    ex = ScheduleExecutor(_hass(mock_hass, soc=60.0), "entry1", dict(CFG), inverter)
    assert ex._entladedeckel_offen is True
    ex._created_at = NOW - timedelta(minutes=10)
    with _messung():
        await ex.async_guard_cycle(_state(), MODE_EIN, now=NOW)
    assert call(None) in inverter.async_set_discharge_limit.await_args_list
    assert ex._entladedeckel_offen is False


async def test_ohne_funktion_kein_deckel(mock_hass, inverter):
    cfg = {**CFG, "spitzenkappung_enabled": False}
    ex = _executor(_hass(mock_hass, soc=12.0), inverter, cfg)
    assert ex._entladedeckel_offen is False
    with _messung(export=-8.0):
        await ex.async_guard_cycle(_state(), MODE_EIN, now=NOW)
    inverter.async_set_discharge_limit.assert_not_awaited()
    assert ex.spitzenkappung is None


async def test_treiber_ohne_deckel_bleibt_unberuehrt(mock_hass, mock_inverter):
    ex = _executor(_hass(mock_hass, soc=12.0), mock_inverter)
    with _messung(export=-8.0):
        await ex.async_guard_cycle(_state(), MODE_EIN, now=NOW)
    mock_inverter.async_set_discharge_limit.assert_not_awaited()


async def test_ausgeschaltet_wird_ein_stehender_deckel_aufgehoben(mock_hass, inverter):
    ex = _executor(_hass(mock_hass, soc=25.0), inverter)
    with _messung(export=-1.0):
        await ex.async_guard_cycle(_state(), MODE_EIN, now=NOW)
    ex.update_config({**CFG, "spitzenkappung_enabled": False})
    with _messung(export=-1.0):
        await ex.async_guard_cycle(_state(), MODE_EIN, now=NOW + timedelta(seconds=30))
    assert inverter.async_set_discharge_limit.await_args == call(None)


async def test_plan_ohne_reserve_hebt_den_deckel_auf(mock_hass, inverter):
    ex = _executor(_hass(mock_hass, soc=25.0), inverter)
    ex._written_entladedeckel_kw = 0.0
    with _messung():
        await ex.async_guard_cycle(_state(mit_reserve=False), MODE_EIN, now=NOW)
    assert inverter.async_set_discharge_limit.await_args == call(None)


async def test_ohne_messung_bleibt_der_deckel_stehen(mock_hass, inverter):
    ex = _executor(_hass(mock_hass, soc=25.0), inverter)
    # Laufender Betrieb: freigegeben, Deckel steht. (Ein Wechsel in die
    # Freigabe nähme ihn über async_release ohnehin mit.)
    ex._active_kind = "release"
    ex._written_entladedeckel_kw = 1.0
    ex._kappung_aktiv = True
    with _messung(export=None, batt=None):
        await ex.async_guard_cycle(_state(), MODE_EIN, now=NOW)
    inverter.async_set_discharge_limit.assert_not_awaited()
    assert ex._written_entladedeckel_kw == 1.0


async def test_schreibfehler_bremst(mock_hass, inverter):
    ex = _executor(_hass(mock_hass, soc=25.0), inverter)
    inverter.async_set_discharge_limit = AsyncMock(return_value=False)
    for i in range(4):
        with _messung(export=-1.0):
            await ex.async_guard_cycle(_state(), MODE_EIN, now=NOW + timedelta(seconds=30 * i))
    # Lauf 1 schreibt, Lauf 2 wartet (1), Lauf 3 schreibt, Lauf 4 wartet.
    assert inverter.async_set_discharge_limit.await_count == 2
    assert ex.write_failures == 2


async def test_ladelimit_und_deckel_gemeinsam(mock_hass, inverter):
    """Ein Slot mit Ladelimit (PV-Überschuss für die Einspeisung) und
    Ladestand in der Reserve: beides wird geschrieben."""
    ex = _executor(_hass(mock_hass, soc=25.0), inverter)
    laden = _state(_slot(battery_p=-1.0, grid_p=1.0, soc=26.0))
    with _messung(export=1.0, batt=1.0):
        await ex.async_guard_cycle(laden, MODE_EIN, now=NOW)
    inverter.async_set_charge_limit.assert_awaited_once()
    inverter.async_set_discharge_limit.assert_awaited_once_with(0.0)


def test_batterie_luft(mock_hass, inverter):
    ex = _executor(_hass(mock_hass, soc=50.0), inverter)
    # Ohne Lauf der Batterie-Stufe zählt nichts.
    with _messung(batt=-2.0):
        assert ex.batterie_luft_kw() == 0.0
    ex.spitzenkappung = {"basis_pct": 10.0, "aktiv": False}
    with _messung(batt=-2.0):
        assert ex.batterie_luft_kw() == pytest.approx(3.0)
    with _messung(batt=1.5):
        assert ex.batterie_luft_kw() == pytest.approx(6.5)
    # Am Mindest-Ladestand bleibt nur, was gerade geladen wird.
    _hass(mock_hass, soc=10.2)
    with _messung(batt=1.5):
        assert ex.batterie_luft_kw() == pytest.approx(1.5)
    with _messung(batt=None):
        assert ex.batterie_luft_kw() == 0.0


def test_batterie_luft_an_der_ac_grenze(mock_hass, inverter):
    """Gen24 mit 5 kW: 3 kW PV und 2 kW Entladung — der Wechselrichter gibt
    schon alles ab, was er kann. Luft gibt es keine, auch wenn die Batterie
    mehr könnte."""
    ex = _executor(_hass(mock_hass, soc=50.0), inverter, {**CFG, "inverter_ac_limit_kw": 5.0})
    ex.spitzenkappung = {"basis_pct": 10.0, "aktiv": False}
    with (
        patch.object(sx, "compute_battery_now_kw", return_value=-2.0),
        patch.object(sx, "compute_pv_now_kw", return_value=3.0),
    ):
        assert ex.batterie_luft_kw() == 0.0


def test_ohne_deckel_treiber_keine_luft(mock_hass, mock_inverter):
    """Kostal, SolaX, Sigenergy: Warum die Batterie trotz Netzbezug nicht
    mehr gibt, weiß niemand — die Verbraucherstufe rechnet ohne sie."""
    ex = _executor(_hass(mock_hass, soc=50.0), mock_inverter)
    ex.spitzenkappung = {"basis_pct": 10.0, "aktiv": False}
    with _messung(batt=-1.0):
        assert ex.batterie_luft_kw() == 0.0


async def test_waehrend_netzladung_keine_luft(mock_hass, inverter):
    inverter.supports_grid_charge = True
    ex = _executor(_hass(mock_hass, soc=50.0), inverter)
    laden = _state({**_slot(battery_p=-3.0, grid_p=0.0), "netzladung": 3.0})
    with _messung(batt=3.0):
        await ex.async_guard_cycle(laden, MODE_EIN, now=NOW)
        assert ex.batterie_luft_kw() == 0.0



async def test_aus_hebt_nur_den_deckel_auf_ohne_dauerstopp(mock_hass, inverter):
    """Steht im Modus Aus nur noch der Deckel aus, wird nicht jede halbe
    Minute der ganze Stopp wiederholt — nur der Deckel, gebremst."""
    ex = _executor(_hass(mock_hass, soc=25.0), inverter, offen=True)
    ex._active_kind = "release"
    ex._last_mode = "Aus"
    inverter.async_set_discharge_limit = AsyncMock(return_value=False)
    for i in range(10):
        with _messung():
            await ex.async_guard_cycle(_state(), MODE_AUS, now=NOW + timedelta(seconds=30 * i))
    inverter.async_stop_forcible.assert_not_awaited()
    assert 1 <= inverter.async_set_discharge_limit.await_count <= 4


async def test_aus_in_der_startphase_hebt_noch_nicht_auf(mock_hass, inverter):
    ex = _executor(_hass(mock_hass, soc=25.0), inverter, offen=True)
    ex._created_at = NOW
    ex._active_kind = "release"
    ex._last_mode = "Aus"
    with _messung():
        await ex.async_guard_cycle(_state(), MODE_AUS, now=NOW + timedelta(seconds=10))
    inverter.async_set_discharge_limit.assert_not_awaited()


async def test_freigabe_hebt_ungebremst_auf(mock_hass, inverter):
    """Beim Entladen der Integration läuft die Freigabe — eine Wartezeit aus
    einem früheren Fehlschlag darf den Huawei-Deckel dort nicht retten."""
    ex = _executor(_hass(mock_hass, soc=25.0), inverter)
    ex._written_entladedeckel_kw = 0.0
    ex._deckel_warten = 8
    assert await ex.async_release() is True
    assert inverter.async_set_discharge_limit.await_args == call(None)
    assert ex._entladedeckel_offen is False


async def test_deckel_pendelt_nicht_an_der_schwelle(mock_hass, inverter):
    """Bedarf so, dass der Deckel um 0,2 kW schwankt: Wer hält, deckt erst ab
    0,4 kW — kein Schreiben je Lauf."""
    ex = _executor(_hass(mock_hass, soc=25.0), inverter)
    ex._active_kind = "release"
    ex._written_entladedeckel_kw = 0.0
    for i, export in enumerate([-4.92, -4.88, -4.93, -4.89]):
        with _messung(export=export, batt=0.0):
            await ex.async_guard_cycle(_state(), MODE_EIN, now=NOW + timedelta(seconds=30 * i))
    inverter.async_set_discharge_limit.assert_not_awaited()


async def test_schwelle_wie_im_plan_gedeckelt(mock_hass, inverter):
    """Mindest-Ladestand 70 % + 30 % Reserve: Der Plan stoppt bei 80 %
    (Deckel 100 − nutzbares Band), der Executor auch."""
    cfg = {**CFG, "schedule_min_soc_pct": 70}
    ex = _executor(_hass(mock_hass, soc=85.0), inverter, cfg)
    with _messung(export=-1.0):
        await ex.async_guard_cycle(_state(basis=70.0, reserve_pct=30.0), MODE_EIN, now=NOW)
    assert ex.spitzenkappung["schwelle_pct"] == 80.0
    assert ex.spitzenkappung["aktiv"] is False
