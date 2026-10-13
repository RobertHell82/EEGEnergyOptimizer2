"""„Batterie aus dem Netz laden" (Beta, nur Huawei).

Entscheidung 12.10.2026: Das Ladeziel darf mit Netzstrom erreicht werden,
aber nur in Slots, in denen Netzstrom billiger ist als der Strom, den die
Batterie danach ersetzt (die Nacht bis zur nächsten PV-Zeit) — gerechnet je
kWh, nach Wandlungsverlusten, Alterung und Mindestvorteil.

Was diese Tests festhalten:

* ohne den Schalter ist alles wie vorher (Modell, Preise, Plan),
* an einem Tag, an dem die PV die Batterie füllt, wird nichts gekauft,
* gekauft wird nur in den billigeren Slots vor dem Ende der PV-Zeit,
  höchstens mit der eingestellten Leistung und nie auf geschätzte Preise,
* Executor und Huawei-Treiber setzen das als Zwangsladung um und beenden
  sie, bevor ein anderer Befehl kommt.
"""

import dataclasses
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from custom_components.eeg_energy_optimizer import schedule as sched
from custom_components.eeg_energy_optimizer import schedule_executor as sx
from custom_components.eeg_energy_optimizer.const import MODE_EIN
from custom_components.eeg_energy_optimizer.netzentgelt import Netzgebuehr
from custom_components.eeg_energy_optimizer.sensor import fahrplan_kurzstatus

from .test_ladeziel import _inputs_gruenbach
from .test_schedule_executor import NOW, _make_executor, _messwerte, _slot, _state

# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------


class _Treiber:
    def __init__(self, kann: bool) -> None:
        self.supports_grid_charge = kann


def test_ohne_schalter_kein_netzladen():
    assert sched.netzladen_max_kw({}, _Treiber(True)) == 0.0
    assert sched.netzladen_max_kw({"schedule_netzladen": False}, _Treiber(True)) == 0.0


def test_ohne_treiber_faehigkeit_kein_netzladen():
    config = {"schedule_netzladen": True, "discharge_power_kw": 5.0}
    assert sched.netzladen_max_kw(config, _Treiber(False)) == 0.0
    assert sched.netzladen_max_kw(config, object()) == 0.0


def test_netzladeleistung_ist_die_batterieleistung():
    config = {"schedule_netzladen": True, "discharge_power_kw": 5.0,
              "schedule_netzladen_max_kw": 3}
    assert sched.netzladen_max_kw(config, _Treiber(True)) == 5.0


@pytest.mark.parametrize("roh,erwartet", [(None, 0.0), ("", 0.0), (0, 0.0), (-2, 0.0),
                                          ("x", 0.0), (7, 7.0), (12.5, 12.5)])
def test_netzgrenze_leer_heisst_keine(roh, erwartet):
    """Das Feld deckelt den ganzen Netzbezug — auch über der Batterieleistung."""
    assert sched.netzladen_netzgrenze_kw({"schedule_netzladen_max_kw": roh}) == erwartet


@pytest.mark.parametrize("roh,erwartet", [
    (None, sched.DEFAULT_NETZLADEN_VORTEIL), ("", sched.DEFAULT_NETZLADEN_VORTEIL),
    (0, 0.0), (0.05, 0.05), (5, sched.MAX_NETZLADEN_VORTEIL), (-1, 0.0), ("x", 0.02),
])
def test_mindestvorteil(roh, erwartet):
    assert sched.netzladen_vorteil({"schedule_netzladen_vorteil": roh}) == erwartet


# ---------------------------------------------------------------------------
# Bezugspreise
# ---------------------------------------------------------------------------

TZ = timezone(timedelta(hours=2))
STAMPS = [datetime(2026, 10, 12, 10, 0, tzinfo=TZ) + timedelta(minutes=30 * i) for i in range(4)]
# Netzgebühr brutto: 6 ct Netznutzung, 4 ct Verlust + Abgaben.
NETZ = Netzgebuehr(
    ap=0.10, snap=None, winap=None, bereich="ooe", stand=None, quelle=None,
    netznutzung=0.06, netzverlust=0.01, elektrizitaetsabgabe=0.02, foerderbeitrag=0.01,
)


def _reihe(config, netz=NETZ, spot=None, fest=None, bedarf=None, max_kw=4.0):
    return sched.bezugsreihe_netzladen(
        config, STAMPS, 0.25, None, None, netz, spot, fest, bedarf,
        [0.5] * 4, [0.0] * 4, max_kw,
    )


def test_ohne_spot_und_eeg_bleibt_der_bezugspreis():
    assert _reihe({}) == (None, None)


def test_ohne_netzgebuehr_keine_reihe():
    """Ohne Netzgebühr wäre Spot- oder EEG-Bezug reine Energie und viel zu billig."""
    config = {"schedule_bezug_quelle": "spot"}
    assert _reihe(config, netz=None, spot=[0.05] * 4) == (None, None)


def test_spot_bezug_mit_aufschlag_und_mwst():
    config = {
        "schedule_bezug_quelle": "spot",
        "spot_bezug_aufschlag": 0.015,
        "spot_bezug_aufschlag_pct": 3,
    }
    reihe, fest = _reihe(config, spot=[0.10, -0.02, 0.10, 0.10], fest=[True, True, False, True])
    # (0,10 + 0,015 + 0,003) × 1,2 + 0,10 Netz
    assert reihe[0] == pytest.approx(0.118 * 1.2 + 0.10)
    # negativer Börsenpreis: Prozent vom Betrag kommt dazu
    assert reihe[1] == pytest.approx((-0.02 + 0.015 + 0.0006) * 1.2 + 0.10)
    assert fest == [True, True, False, True]


def test_eeg_bezug_nur_soweit_die_gemeinschaft_ueberschuss_hat():
    config = {
        "peakshare_community": "Sonnenhof",
        "peakshare_share_pct": 100,
        "peakshare_bezug_price": 0.08,
        "peakshare_bezug_ebene": "lokal",
    }
    v0 = int(STAMPS[0].timestamp() // 900)
    v1 = int(STAMPS[1].timestamp() // 900)
    # Höchstens bezogen: (0,5 kW Haus + 4 kW Netzladen) × 0,25 h = 1,125 kWh
    bedarf = {"Sonnenhof": {v0: -5.0, v1: -0.5625}}
    reihe, fest = _reihe(config, bedarf=bedarf)
    eeg = 0.08 + 0.06 * (1 - 0.57) + 0.01   # nur Netzverlust, Abgaben entfallen
    assert reihe[0] == pytest.approx(eeg)                       # ganz aus der EEG
    assert reihe[1] == pytest.approx(0.5 * eeg + 0.5 * 0.25)    # halb
    assert reihe[2] == pytest.approx(0.25)                      # kein Überschuss
    assert fest is None


def test_eeg_bezug_ohne_snap_rabatt():
    """Für Gemeinschaftsmengen gilt der SNAP nicht — der Lieferantenteil
    bekommt ihn, der EEG-Teil rechnet mit dem vollen Netznutzungsentgelt."""
    sommer = [datetime(2026, 6, 12, 11, 0, tzinfo=TZ)]
    netz = dataclasses.replace(NETZ, snap=0.088)   # Netznutzung 4,8 statt 6 ct
    config = {"peakshare_community": "Sonnenhof", "peakshare_share_pct": 100, "peakshare_bezug_price": 0.08}
    v = int(sommer[0].timestamp() // 900)
    reihe, _ = sched.bezugsreihe_netzladen(
        config, sommer, 0.25, 0.238, None, netz, None, None,
        {"Sonnenhof": {v: -10.0}}, [0.5], [0.0], 4.0,
    )
    assert reihe[0] == pytest.approx(0.08 + 0.06 * (1 - 0.57) + 0.01)


def test_eeg_bezug_ohne_saldo_zaehlt_nicht():
    """Quotenmodus (kein Saldo) oder unbekannte Gemeinschaft: kein EEG-Bezug."""
    config = {"peakshare_community": "Sonnenhof", "peakshare_share_pct": 100, "peakshare_bezug_price": 0.08}
    assert _reihe(config, bedarf=None) == (None, None)
    assert _reihe(config, bedarf={"Andere": {}}) == (None, None)


def test_eeg_bezug_wie_die_einspeisung_nur_der_anteil():
    """Der Aufteilungsschlüssel gilt für den Bezug wie für die Einspeisung:
    Traun 12.10.2026 — BEG 1 %, EEG Pucking 99 %. Auch mit reichlich
    Überschuss kommt aus der BEG höchstens 1 % des Bezugs."""
    config = {
        "peakshare_community": "BEG", "peakshare_share_pct": 1,
        "peakshare_bezug_price": 0.05, "peakshare_bezug_ebene": "beg",
        "peakshare_community_2": "Pucking", "peakshare_share_pct_2": 99,
        "peakshare_bezug_price_2": 0.08, "peakshare_bezug_ebene_2": "lokal",
    }
    v0 = int(STAMPS[0].timestamp() // 900)
    v1 = int(STAMPS[1].timestamp() // 900)
    bedarf = {"BEG": {v0: -50.0, v1: -50.0}, "Pucking": {v0: -50.0}}
    reihe, _ = _reihe(config, bedarf=bedarf)
    beg = 0.05 + NETZ.ap                              # volle Netzgebühr samt Abgaben
    eeg = 0.08 + 0.06 * (1 - 0.57) + 0.01
    assert reihe[0] == pytest.approx(0.01 * beg + 0.99 * eeg)
    assert reihe[1] == pytest.approx(0.01 * beg + 0.99 * 0.25)  # Pucking ohne Überschuss


def test_eeg_bezug_anteil_null_zaehlt_nicht():
    config = {"peakshare_community": "Sonnenhof", "peakshare_share_pct": 0, "peakshare_bezug_price": 0.01}
    assert sched.eeg_bezug_aus_config(config) == []


def test_beg_zahlt_die_volle_netzgebuehr():
    config = {
        "peakshare_community": "BEG", "peakshare_share_pct": 100,
        "peakshare_bezug_price": 0.08, "peakshare_bezug_ebene": "beg",
    }
    v0 = int(STAMPS[0].timestamp() // 900)
    reihe, _ = _reihe(config, bedarf={"BEG": {v0: -50.0}})
    assert reihe[0] == pytest.approx(0.08 + NETZ.ap)


def test_bezugspreis_zu_liest_die_reihe():
    inputs = _inputs_gruenbach()
    reihe = [0.1 + i / 1000 for i in range(len(inputs.timestamps))]
    inputs = dataclasses.replace(inputs, consumption_price_series=reihe)
    t = inputs.timestamps[5]
    assert sched.bezugspreis_zu(inputs, t) == pytest.approx(reihe[5])
    assert sched.bezugspreis_zu(inputs, t + timedelta(minutes=1)) == pytest.approx(reihe[5])
    assert sched._tiefster_bezugspreis(inputs) == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Im Modell (Grünbach-Plan vom 04.10.2026, Start 20:00, 16,6 kWh)
# ---------------------------------------------------------------------------

pandas_und_highs = pytest.mark.usefixtures()


def _mittag_billig(inputs, mittag):
    return [mittag if 10 <= t.hour < 16 else inputs.consumption_price for t in inputs.timestamps]


def _inputs(pv=0.3, soc=25.0, mittag=0.15, max_kw=4.0, ladeziel=100.0, fest=None):
    inputs = _inputs_gruenbach(ladeziel_pct=ladeziel, pv_faktor=pv)
    kapazitaet = inputs.battery_capacity_kwh
    return dataclasses.replace(
        inputs,
        soc_pct=soc,
        battery_free_kwh=kapazitaet * (1 - soc / 100),
        consumption_price_series=_mittag_billig(inputs, mittag),
        consumption_price_fest=fest,
        netzladen_max_kw=max_kw,
    )


def _netzladung_kwh(plan) -> float:
    return sum(s.get("netzladung", 0.0) for s in plan["slots"]) * 0.25


@pytest.fixture(autouse=False)
def lp():
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")


def test_aus_ist_identisch(lp):
    """Leistung 0: der Haken liefert None, das Modell ist das ohne Netzladen."""
    pd = pytest.importorskip("pandas")
    from custom_components.eeg_energy_optimizer.chamo import opt_highs

    class OhneHaken(sched.HAConfig):
        netzladen = None

    inputs = _inputs(max_kw=0.0)
    a = opt_highs.opt(sched.HAConfig(inputs), inputs.start)
    b = opt_highs.opt(OhneHaken(inputs), inputs.start)
    for spalte in ("grid_p", "battery_p", "battery", "battery_ub", "discard"):
        assert pd.Series(a[spalte]).astype(float).round(9).equals(
            pd.Series(b[spalte]).astype(float).round(9)
        ), spalte
    assert "netzladung" not in sched.solve(inputs)["slots"][0]


def test_sonniger_tag_kauft_nichts(lp):
    assert _netzladung_kwh(sched.solve(_inputs(pv=1.0))) == 0.0


def test_trueber_tag_mit_billigem_mittag_wird_voll(lp):
    plan = sched.solve(_inputs(pv=0.3))
    assert _netzladung_kwh(plan) > 3.0
    for termin in plan["ladeziel"]["termine"]:
        assert termin["geplant_pct"] >= 99.5, termin
    ohne = sched.solve(_inputs(pv=0.3, max_kw=0.0))
    assert min(t["geplant_pct"] for t in ohne["ladeziel"]["termine"]) < 95.0


def test_gekauft_wird_nur_in_billigen_slots_vor_dem_pv_ende(lp):
    plan = sched.solve(_inputs(pv=0.3))
    termine = [datetime.fromisoformat(t["t"]) for t in plan["ladeziel"]["termine"]]
    for slot in plan["slots"]:
        if slot.get("netzladung", 0.0) <= 0:
            continue
        t = datetime.fromisoformat(slot["t"])
        assert 10 <= t.hour < 16, slot["t"]
        assert any(t <= termin and t.date() == termin.date() for termin in termine), slot["t"]


def test_nicht_billiger_als_die_nacht_kauft_nichts(lp):
    """Mittags 24 ct gegen 20 ct nachts — kein Kauf, auch mit Ladeziel."""
    assert _netzladung_kwh(sched.solve(_inputs(pv=0.1, mittag=0.24))) == 0.0


def test_mindestvorteil_verhindert_knappe_kaeufe(lp):
    """18 ct: nach η² (0,9025 × 20,1 ct = 18,2 ct) und 1 ct Alterung bleibt
    kein Vorteil von 2 ct."""
    assert _netzladung_kwh(sched.solve(_inputs(pv=0.1, mittag=0.17))) == 0.0


def test_netzladung_hoechstens_die_eingestellte_leistung(lp):
    plan = sched.solve(_inputs(pv=0.1, max_kw=1.0))
    assert max(s.get("netzladung", 0.0) for s in plan["slots"]) <= 1.0 + 1e-6
    assert _netzladung_kwh(plan) > 0


def test_netzgrenze_gilt_fuer_haus_und_batterie(lp):
    """Mit 2 kW Grenze bleibt der geplante Netzbezug in jedem Netzlade-Slot
    bei höchstens 2 kW (oder der Hauslast, wenn die allein darüber liegt) —
    die Batterie lädt um die Hauslast langsamer."""
    ohne = sched.solve(_inputs(pv=0.1))
    plan = sched.solve(dataclasses.replace(_inputs(pv=0.1), netzladen_netzgrenze_kw=2.0))
    geladen = [s for s in plan["slots"] if s.get("netzladung", 0.0) > 0]
    assert geladen
    for slot in geladen:
        bezug = -float(slot["grid_p"])
        assert bezug <= max(2.0, float(slot["consumption"])) + 1e-6, slot
    assert max(-float(s["grid_p"]) for s in ohne["slots"] if s.get("netzladung", 0.0) > 0) > 2.0


def test_hauslast_ueber_der_netzgrenze_kauft_nichts(lp):
    inputs = _inputs(pv=0.1)
    plan = sched.solve(dataclasses.replace(inputs, netzladen_netzgrenze_kw=0.05))
    assert _netzladung_kwh(plan) == pytest.approx(0.0, abs=1e-6)


def test_geschaetzte_preise_tragen_keinen_kauf(lp):
    inputs = _inputs(pv=0.3)
    fest = [False] * len(inputs.timestamps)
    plan = sched.solve(dataclasses.replace(inputs, consumption_price_fest=fest))
    assert _netzladung_kwh(plan) == 0.0


def test_ladeziel_bleibt_ohne_netzladen_bei_der_sonne(lp):
    """Gegenprobe: Ohne Leistung wird das Ladeziel nicht mit Netzstrom erreicht."""
    plan = sched.solve(_inputs(pv=0.3, max_kw=0.0))
    assert all(s.get("netzladung", 0.0) == 0.0 for s in plan["slots"])


@pytest.mark.parametrize("pv", [0.0, 0.05])
def test_dunkler_horizont_bleibt_loesbar(lp, pv):
    plan = sched.solve(_inputs(pv=pv))
    assert plan["slots"]


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


def _netzslot(offset_min=0, battery_p=-3.0, netzladung=2.5, soc=60.0):
    slot = _slot(offset_min, battery_p=battery_p, grid_p=-2.6, soc=soc)
    slot["netzladung"] = netzladung
    slot["PV"] = 0.5
    return slot


def test_plan_action_netzladung():
    action = sx.plan_action(_state(_netzslot()), NOW)
    assert action.kind == "grid_charge"
    assert action.power_kw == pytest.approx(3.0)
    assert action.target_soc == 60.0


def test_plan_action_rauschen_ist_keine_netzladung():
    action = sx.plan_action(_state(_netzslot(netzladung=0.05)), NOW)
    assert action.kind != "grid_charge"


async def test_executor_schreibt_die_netzladung(mock_hass, mock_inverter):
    mock_inverter.supports_grid_charge = True
    ex = _make_executor(mock_hass, mock_inverter)
    with _messwerte(haus=0.6, pv=0.5, export=-2.6):
        await ex.async_guard_cycle(_state(_netzslot()), MODE_EIN, now=NOW)
    mock_inverter.async_set_grid_charge.assert_awaited_once_with(3.0, target_soc=60.0)
    assert ex._active_kind == "grid_charge"
    assert "Netzladung 3.00 kW bis 60 %" in ex.last_status
    assert fahrplan_kurzstatus({**ex.status(), "mode": "Ein"}) == "Netzladung 3.00 kW bis 60 %"

    # Gleicher Slot, gleiche Werte → nichts neu schreiben (Totband).
    with _messwerte(haus=0.6, pv=0.5, export=-2.6):
        await ex.async_guard_cycle(_state(_netzslot()), MODE_EIN, now=NOW + timedelta(seconds=30))
    assert mock_inverter.async_set_grid_charge.await_count == 1


async def test_netzladung_gedeckelt_auf_die_ladeleistung(mock_hass, mock_inverter):
    mock_inverter.supports_grid_charge = True
    mock_inverter.get_charge_limit_max_kw = MagicMock(return_value=2.0)
    ex = _make_executor(mock_hass, mock_inverter)
    with _messwerte(haus=0.6, pv=0.5):
        await ex.async_guard_cycle(_state(_netzslot()), MODE_EIN, now=NOW)
    mock_inverter.async_set_grid_charge.assert_awaited_once_with(2.0, target_soc=60.0)


async def test_netzgrenze_fuehrt_die_ladeleistung_an_der_messung_nach(mock_hass, mock_inverter):
    """Grenze 3 kW, Haus 1,5 kW, PV 0,5 kW → Batterie ≤ 2 kW, nicht die
    geplanten 3 kW. Geht der Herd aus, steigt sie wieder auf den Plan."""
    mock_inverter.supports_grid_charge = True
    config = {**_make_executor(mock_hass, mock_inverter)._config,
              "schedule_netzladen_max_kw": 3.0}
    ex = _make_executor(mock_hass, mock_inverter, config)
    with _messwerte(haus=1.5, pv=0.5):
        await ex.async_guard_cycle(_state(_netzslot()), MODE_EIN, now=NOW)
    mock_inverter.async_set_grid_charge.assert_awaited_once_with(2.0, target_soc=60.0)
    assert "Netzbezug ≤ 3.0 kW" in ex.last_status

    with _messwerte(haus=0.3, pv=0.5):
        await ex.async_guard_cycle(_state(_netzslot()), MODE_EIN, now=NOW + timedelta(seconds=30))
    assert mock_inverter.async_set_grid_charge.await_args.args[0] == pytest.approx(3.0)


async def test_hauslast_ueber_der_netzgrenze_laedt_mit_null(mock_hass, mock_inverter):
    mock_inverter.supports_grid_charge = True
    config = {**_make_executor(mock_hass, mock_inverter)._config,
              "schedule_netzladen_max_kw": 2.0}
    ex = _make_executor(mock_hass, mock_inverter, config)
    with _messwerte(haus=4.0, pv=0.5):
        await ex.async_guard_cycle(_state(_netzslot()), MODE_EIN, now=NOW)
    mock_inverter.async_set_grid_charge.assert_awaited_once_with(0.0, target_soc=60.0)


async def test_treiber_ohne_netzladen_bekommt_das_ladelimit(mock_hass, mock_inverter):
    ex = _make_executor(mock_hass, mock_inverter)
    with _messwerte(haus=0.6, pv=0.5):
        await ex.async_guard_cycle(_state(_netzslot()), MODE_EIN, now=NOW)
    mock_inverter.async_set_grid_charge.assert_not_called()
    mock_inverter.async_set_charge_limit.assert_awaited_once()


async def test_wechsel_auf_ladelimit_stoppt_die_netzladung_zuerst(mock_hass, mock_inverter):
    mock_inverter.supports_grid_charge = True
    ex = _make_executor(mock_hass, mock_inverter)
    with _messwerte(haus=0.6, pv=0.5):
        await ex.async_guard_cycle(_state(_netzslot()), MODE_EIN, now=NOW)
    mock_inverter.async_stop_forcible.reset_mock()
    with _messwerte(haus=0.6, pv=3.0, export=1.0):
        await ex.async_guard_cycle(
            _state(_slot(15, battery_p=-1.0), last_run=NOW + timedelta(minutes=15)),
            MODE_EIN, now=NOW + timedelta(minutes=15),
        )
    mock_inverter.async_stop_forcible.assert_awaited_once()
    mock_inverter.async_set_charge_limit.assert_awaited_once()
    assert ex._active_kind == "charge_limit"


async def test_gescheiterter_stopp_bleibt_in_der_netzladung(mock_hass, mock_inverter):
    mock_inverter.supports_grid_charge = True
    ex = _make_executor(mock_hass, mock_inverter)
    with _messwerte(haus=0.6, pv=0.5):
        await ex.async_guard_cycle(_state(_netzslot()), MODE_EIN, now=NOW)
    mock_inverter.async_stop_forcible.return_value = False
    with _messwerte(haus=0.6, pv=3.0, export=1.0):
        await ex.async_guard_cycle(
            _state(_slot(15, battery_p=-1.0), last_run=NOW + timedelta(minutes=15)),
            MODE_EIN, now=NOW + timedelta(minutes=15),
        )
    mock_inverter.async_set_charge_limit.assert_not_called()
    assert ex._active_kind == "grid_charge"
    assert "Netzladung konnte nicht gestoppt werden" in ex.last_status


async def test_modus_aus_beendet_die_netzladung(mock_hass, mock_inverter):
    from custom_components.eeg_energy_optimizer.const import MODE_AUS

    mock_inverter.supports_grid_charge = True
    ex = _make_executor(mock_hass, mock_inverter)
    with _messwerte(haus=0.6, pv=0.5):
        await ex.async_guard_cycle(_state(_netzslot()), MODE_EIN, now=NOW)
    mock_inverter.async_stop_forcible.reset_mock()
    with _messwerte(haus=0.6, pv=0.5):
        await ex.async_guard_cycle(_state(_netzslot()), MODE_AUS, now=NOW + timedelta(seconds=30))
    mock_inverter.async_stop_forcible.assert_awaited_once()
    assert ex._active_kind == "release"


# ---------------------------------------------------------------------------
# Huawei
# ---------------------------------------------------------------------------


async def test_huawei_netzladung(mock_hass):
    from custom_components.eeg_energy_optimizer.inverter.huawei import (
        HUAWEI_DOMAIN,
        MAX_CHARGE_POWER_CANDIDATES,
        HuaweiInverter,
    )

    entity = MAX_CHARGE_POWER_CANDIDATES[0]
    zustand = MagicMock()
    zustand.attributes = {"max": 5000.0}
    mock_hass.states.get = MagicMock(side_effect=lambda eid: zustand if eid == entity else None)
    inv = HuaweiInverter(mock_hass, {"huawei_device_id": "dev"})
    assert inv.supports_grid_charge is True

    assert await inv.async_set_grid_charge(3.2, target_soc=61.3) is True
    aufrufe = mock_hass.services.async_call.await_args_list
    # Erst das Ladelimit aufs Maximum, dann die Zwangsladung.
    assert aufrufe[0].args[:2] == ("number", "set_value")
    assert aufrufe[0].args[2] == {"entity_id": entity, "value": 5000.0}
    assert aufrufe[1].args[:2] == (HUAWEI_DOMAIN, "forcible_charge_soc")
    assert aufrufe[1].args[2] == {"device_id": "dev", "power": "3200", "target_soc": 62}


async def test_huawei_netzladung_fehler_mit_grund(mock_hass):
    from custom_components.eeg_energy_optimizer.inverter.huawei import HuaweiInverter

    mock_hass.states.get = MagicMock(return_value=None)
    mock_hass.states.async_all = MagicMock(return_value=[])

    async def scheitert(domain, service, *args, **kwargs):
        if service == "forcible_charge_soc":
            raise RuntimeError("nein")

    mock_hass.services.async_call.side_effect = scheitert
    inv = HuaweiInverter(mock_hass, {"huawei_device_id": "dev"})
    assert await inv.async_set_grid_charge(2.0, target_soc=80) is False
    assert inv.last_write_error == "forcible_charge_soc: RuntimeError"


async def test_basis_treiber_kann_kein_netzladen():
    from custom_components.eeg_energy_optimizer.inverter.base import InverterBase

    class Minimal(InverterBase):
        async def async_set_charge_limit(self, power_kw):
            return True

        async def async_set_discharge(self, power_kw, target_soc=None):
            return True

        async def async_stop_forcible(self):
            return True

        @property
        def is_available(self):
            return True

    inv = Minimal(MagicMock(), {})
    assert inv.supports_grid_charge is False
    assert await inv.async_set_grid_charge(1.0, 80) is False
    assert inv.last_write_error
