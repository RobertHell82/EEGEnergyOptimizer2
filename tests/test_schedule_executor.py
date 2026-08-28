"""Tests für den Fahrplan-Executor (schedule_executor.py).

Deckt beide Ebenen getrennt ab: ``plan_action()`` als reine Übersetzung
Slot → Absicht, und ``ScheduleExecutor.async_guard_cycle()`` mit Guards,
Totbändern, Not-Aus, Grace Period und Failsafe. Alle Messwerte werden über
die power_readings-Helfer gepatcht — geschrieben wird ausschließlich über
das InverterBase-API (Mock aus conftest).
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from custom_components.eeg_energy_optimizer import schedule_executor as sx
from custom_components.eeg_energy_optimizer.const import (
    MODE_AUS,
    MODE_EIN,
    MODE_TEST,
)
from custom_components.eeg_energy_optimizer.schedule_executor import (
    PlanAction,
    ScheduleExecutor,
    plan_action,
    _voll_ab,
)

TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 8, 24, 19, 0, tzinfo=TZ)

CFG_BASE = {"discharge_power_kw": 5.0}
CFG_LIMIT = {
    **CFG_BASE,
    "grid_export_limit_enabled": True,
    "grid_export_limit_kw": 4.0,
}


def _slot(offset_min: int, battery_p=0.0, grid_p=0.0, soc=50.0, consumption=0.6):
    return {
        "t": (NOW + timedelta(minutes=offset_min)).isoformat(),
        "battery_p": battery_p,
        "grid_p": grid_p,
        "soc": soc,
        "consumption": consumption,
    }


def _state(*slots, last_run=NOW, available=True):
    """ScheduleRunner.to_dict()-Attrappe."""
    payload = {
        "available": available,
        "error": None if available else "Rechenfehler",
        "last_run": last_run.isoformat() if last_run else None,
    }
    if available:
        payload["slots"] = list(slots)
    return payload


def _make_executor(mock_hass, mock_inverter, config=None):
    ex = ScheduleExecutor(mock_hass, "entry1", config or dict(CFG_BASE), mock_inverter)
    # Grace Period für die Tests hinter uns lassen — eigene Tests setzen
    # _created_at gezielt zurück.
    ex._created_at = NOW - timedelta(minutes=10)
    return ex


@contextmanager
def _messwerte(export=None, haus=None, pv=None):
    """Patcht die drei Messwert-Helfer im Executor-Namensraum."""
    with (
        patch.object(sx, "compute_grid_export_kw", return_value=export),
        patch.object(sx, "compute_house_load_kw", return_value=haus),
        patch.object(sx, "compute_pv_now_kw", return_value=pv),
    ):
        yield


# ---------------------------------------------------------------------------
# plan_action: Slot → Absicht
# ---------------------------------------------------------------------------


def test_laden_wird_ladelimit():
    """battery_p negativ (chamo: laden) → Ladelimit auf die Planleistung."""
    action = plan_action({"slots": [_slot(0, battery_p=-2.4)]}, NOW)
    assert action == PlanAction(
        "charge_limit", power_kw=2.4, slot_t=_slot(0)["t"], consumption_kw=0.6
    )


def test_kein_laden_geplant_blockiert_das_laden():
    """battery_p ≈ 0 heißt: PV-Überschuss soll ins Netz (Morgen-Einspeisung).
    Freigeben wäre falsch — der Automatikmodus würde die Batterie laden."""
    action = plan_action({"slots": [_slot(0, battery_p=0.0, grid_p=3.0)]}, NOW)
    assert action.kind == "charge_limit"
    assert action.power_kw == 0.0

    # LP-Rauschen zählt als 0
    noise = plan_action({"slots": [_slot(0, battery_p=1e-9, grid_p=3.0)]}, NOW)
    assert noise.kind == "charge_limit"
    assert noise.power_kw == 0.0


def test_einspeisung_aus_der_batterie_wird_entladung():
    """battery_p > 0 und grid_p > 0 → Entladung; Ziel-SOC ist der Wert des
    LAUFENDEN Slots (Zustand am Slot-Ende), nicht der des Folge-Slots."""
    slots = [
        _slot(0, battery_p=2.6, grid_p=2.0, soc=43.0),
        _slot(15, battery_p=2.6, grid_p=2.0, soc=38.0),
    ]
    action = plan_action({"slots": slots}, NOW + timedelta(minutes=5))
    assert action.kind == "discharge"
    assert action.power_kw == pytest.approx(2.0)   # grid_p, nicht battery_p
    assert action.target_soc == 43.0               # laufender Slot, nicht 38


def test_eigenverbrauch_entladung_wird_freigabe():
    """battery_p > 0, grid_p ≤ 0: Entladung nur für den Hausverbrauch —
    das macht der Wechselrichter im Automatikmodus selbst."""
    action = plan_action({"slots": [_slot(0, battery_p=0.5, grid_p=-0.1)]}, NOW)
    assert action.kind == "release"


def test_ohne_slot_keine_absicht():
    assert plan_action(None, NOW) is None
    assert plan_action({"slots": []}, NOW) is None
    # Fahrplan beginnt erst in der Zukunft
    assert plan_action({"slots": [_slot(30)]}, NOW) is None


# ---------------------------------------------------------------------------
# Guard-Lauf: Grundverhalten
# ---------------------------------------------------------------------------


async def test_nicht_unterstuetzter_treiber_schreibt_nie(mock_hass, mock_inverter):
    mock_inverter.supports_schedule_control = False
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte(haus=0.8):
        await ex.async_guard_cycle(_state(_slot(0, battery_p=2.6, grid_p=2.0)), MODE_EIN, now=NOW)

    mock_inverter.async_set_discharge.assert_not_called()
    mock_inverter.async_set_charge_limit.assert_not_called()
    mock_inverter.async_stop_forcible.assert_not_called()
    assert "nicht gesteuert" in ex.last_status
    # async_release ist für nicht gesteuerte Treiber ein No-Op
    assert await ex.async_release() is True
    mock_inverter.async_stop_forcible.assert_not_called()


async def test_grace_period_schreibt_nicht(mock_hass, mock_inverter):
    ex = _make_executor(mock_hass, mock_inverter)
    ex._created_at = NOW - timedelta(seconds=30)   # Neustart vor 30 s

    with _messwerte(haus=0.8):
        await ex.async_guard_cycle(_state(_slot(0, battery_p=-2.0)), MODE_EIN, now=NOW)

    mock_inverter.async_set_charge_limit.assert_not_called()
    assert "Startphase" in ex.last_status


async def test_anzeige_modus_schreibt_nicht(mock_hass, mock_inverter):
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte(haus=0.8):
        await ex.async_guard_cycle(_state(_slot(0, battery_p=-2.0)), MODE_TEST, now=NOW)

    mock_inverter.async_set_charge_limit.assert_not_called()
    mock_inverter.async_set_discharge.assert_not_called()
    # Die Absicht wird trotzdem bestimmt — fürs Dashboard
    assert ex.last_action is not None
    assert ex.last_action.kind == "charge_limit"
    assert ex.last_status.startswith("Aus")


async def test_wechsel_ein_zu_test_gibt_einmalig_frei(mock_hass, mock_inverter):
    """Sonst bleibt das letzte Ladelimit im Wechselrichter stehen."""
    ex = _make_executor(mock_hass, mock_inverter)
    plan = _state(_slot(0, battery_p=-2.0))

    with _messwerte():
        await ex.async_guard_cycle(plan, MODE_EIN, now=NOW)
        assert mock_inverter.async_set_charge_limit.call_count == 1

        await ex.async_guard_cycle(plan, MODE_TEST, now=NOW + timedelta(seconds=30))
        assert mock_inverter.async_stop_forcible.call_count == 1

        await ex.async_guard_cycle(plan, MODE_TEST, now=NOW + timedelta(seconds=60))
        assert mock_inverter.async_stop_forcible.call_count == 1  # nur einmal


async def test_wechselrichter_nicht_verfuegbar_schreibt_nicht(mock_hass, mock_inverter):
    mock_inverter.is_available = False
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte():
        await ex.async_guard_cycle(_state(_slot(0, battery_p=-2.0)), MODE_EIN, now=NOW)

    mock_inverter.async_set_charge_limit.assert_not_called()
    assert "nicht verfügbar" in ex.last_status


# ---------------------------------------------------------------------------
# Ladelimit: Fahrplanwert + Totband
# ---------------------------------------------------------------------------


async def test_ladelimit_ohne_einspeisegrenze_ist_der_fahrplanwert(mock_hass, mock_inverter):
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte(export=3.95):   # klebt zwar — aber Grenze nicht aktiviert
        await ex.async_guard_cycle(_state(_slot(0, battery_p=-2.0)), MODE_EIN, now=NOW)

    mock_inverter.async_set_charge_limit.assert_called_once_with(2.0)
    # Guard 1 wird ohne aktivierte Grenze gar nicht erst gerechnet
    mock_inverter.async_get_charge_limit_kw.assert_not_called()


async def test_ladelimit_totband_verhindert_wiederholtes_schreiben(mock_hass, mock_inverter):
    ex = _make_executor(mock_hass, mock_inverter)
    plan = _state(_slot(0, battery_p=-2.0))

    with _messwerte():
        await ex.async_guard_cycle(plan, MODE_EIN, now=NOW)
        await ex.async_guard_cycle(plan, MODE_EIN, now=NOW + timedelta(seconds=30))
        # 2,1 kW liegt innerhalb des 0,2-kW-Totbands um die geschriebenen 2,0
        await ex.async_guard_cycle(
            _state(_slot(0, battery_p=-2.1)), MODE_EIN, now=NOW + timedelta(seconds=60)
        )

    assert mock_inverter.async_set_charge_limit.call_count == 1


async def test_ladelimit_aenderung_ueber_dem_totband_schreibt(mock_hass, mock_inverter):
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte():
        await ex.async_guard_cycle(_state(_slot(0, battery_p=-2.0)), MODE_EIN, now=NOW)
        await ex.async_guard_cycle(
            _state(_slot(0, battery_p=-2.5)), MODE_EIN, now=NOW + timedelta(seconds=30)
        )

    assert mock_inverter.async_set_charge_limit.call_count == 2
    assert mock_inverter.async_set_charge_limit.call_args.args == (2.5,)


# ---------------------------------------------------------------------------
# Guard 1: Anheben, Rücknahme, totes Band, Clamp
# ---------------------------------------------------------------------------


async def test_guard1_hebt_an_wenn_export_am_limit_klebt(mock_hass, mock_inverter):
    """aktuell == Plan → + Schritt (2,0 → 2,5)."""
    mock_inverter.async_get_charge_limit_kw.return_value = 2.0
    ex = _make_executor(mock_hass, mock_inverter, dict(CFG_LIMIT))

    with _messwerte(export=3.95):   # ±0,1-kW-Band um 4,0
        await ex.async_guard_cycle(_state(_slot(0, battery_p=-2.0)), MODE_EIN, now=NOW)

    mock_inverter.async_set_charge_limit.assert_called_once_with(2.5)
    assert "Guard 1" in ex.last_status


async def test_guard1_plan_ueber_aktuell_setzt_den_planwert(mock_hass, mock_inverter):
    """Plan > aktuell → Planwert (nicht aktuell + Schritt)."""
    mock_inverter.async_get_charge_limit_kw.return_value = 1.0
    ex = _make_executor(mock_hass, mock_inverter, dict(CFG_LIMIT))

    with _messwerte(export=4.0):
        await ex.async_guard_cycle(_state(_slot(0, battery_p=-3.0)), MODE_EIN, now=NOW)

    mock_inverter.async_set_charge_limit.assert_called_once_with(3.0)


async def test_guard1_clampt_am_hardware_maximum(mock_hass, mock_inverter):
    mock_inverter.async_get_charge_limit_kw.return_value = 4.8
    mock_inverter.get_charge_limit_max_kw.return_value = 5.0
    ex = _make_executor(mock_hass, mock_inverter, dict(CFG_LIMIT))

    with _messwerte(export=4.05):
        await ex.async_guard_cycle(_state(_slot(0, battery_p=-2.0)), MODE_EIN, now=NOW)

    mock_inverter.async_set_charge_limit.assert_called_once_with(5.0)


async def test_guard1_ruecknahme_richtung_fahrplan_nie_darunter(mock_hass, mock_inverter):
    """Export deutlich unter der Grenze → pro Lauf ein Schritt zurück."""
    mock_inverter.async_get_charge_limit_kw.return_value = 3.0
    ex = _make_executor(mock_hass, mock_inverter, dict(CFG_LIMIT))

    with _messwerte(export=3.5):   # unter 4,0 − 0,3
        await ex.async_guard_cycle(_state(_slot(0, battery_p=-2.0)), MODE_EIN, now=NOW)
    mock_inverter.async_set_charge_limit.assert_called_once_with(2.5)

    # nie unter den Fahrplanwert: aktuell 2,4 → Ziel 2,0 (nicht 1,9)
    mock_inverter.async_set_charge_limit.reset_mock()
    mock_inverter.async_get_charge_limit_kw.return_value = 2.4
    with _messwerte(export=3.5):
        await ex.async_guard_cycle(
            _state(_slot(0, battery_p=-2.0)), MODE_EIN, now=NOW + timedelta(seconds=30)
        )
    mock_inverter.async_set_charge_limit.assert_called_once_with(2.0)


async def test_guard1_totes_band_aendert_nichts(mock_hass, mock_inverter):
    """Zwischen Grenze − 0,3 und Grenze − 0,1 wird weder angehoben noch
    zurückgenommen — das asymmetrische tote Band verhindert Pendeln."""
    mock_inverter.async_get_charge_limit_kw.return_value = 2.0
    ex = _make_executor(mock_hass, mock_inverter, dict(CFG_LIMIT))

    with _messwerte(export=4.0):    # anheben → 2,5 geschrieben
        await ex.async_guard_cycle(_state(_slot(0, battery_p=-2.0)), MODE_EIN, now=NOW)
    mock_inverter.async_get_charge_limit_kw.return_value = 2.5

    with _messwerte(export=3.8):    # totes Band (3,7 … 3,9)
        await ex.async_guard_cycle(
            _state(_slot(0, battery_p=-2.0)), MODE_EIN, now=NOW + timedelta(seconds=30)
        )

    assert mock_inverter.async_set_charge_limit.call_count == 1   # nur der erste Lauf


async def test_guard1_ohne_netzmesswert_faellt_auf_den_plan(mock_hass, mock_inverter):
    mock_inverter.async_get_charge_limit_kw.return_value = 3.0
    ex = _make_executor(mock_hass, mock_inverter, dict(CFG_LIMIT))

    with _messwerte(export=None):
        await ex.async_guard_cycle(_state(_slot(0, battery_p=-2.0)), MODE_EIN, now=NOW)

    mock_inverter.async_set_charge_limit.assert_called_once_with(2.0)


# ---------------------------------------------------------------------------
# Guard 2: Entlade-Nachführung
# ---------------------------------------------------------------------------


async def test_entladung_ist_plan_einspeisung_plus_gemessene_hauslast(mock_hass, mock_inverter):
    """grid_p 2,0 + Hauslast 0,8 → 2,8 kW auf den Ziel-SOC des Slots."""
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte(haus=0.8):
        await ex.async_guard_cycle(
            _state(_slot(0, battery_p=2.6, grid_p=2.0, soc=43.0)), MODE_EIN, now=NOW
        )

    mock_inverter.async_set_discharge.assert_called_once_with(
        pytest.approx(2.8), target_soc=43.0
    )
    assert "Entladung" in ex.last_status


async def test_entladung_zieht_laufende_pv_ab(mock_hass, mock_inverter):
    """Liefert die PV noch 0,5 kW, muss die Batterie nur den Rest geben —
    sonst käme mehr am Netzanschluss an als geplant."""
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte(haus=0.8, pv=0.5):
        await ex.async_guard_cycle(
            _state(_slot(0, battery_p=2.6, grid_p=2.0, soc=43.0)), MODE_EIN, now=NOW
        )

    mock_inverter.async_set_discharge.assert_called_once_with(
        pytest.approx(2.3), target_soc=43.0
    )


async def test_entladung_hauslast_fallback_auf_die_prognose(mock_hass, mock_inverter):
    """Hauslast-Messwert nicht lesbar → slot['consumption'] (fail-open)."""
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte(haus=None):
        await ex.async_guard_cycle(
            _state(_slot(0, battery_p=2.6, grid_p=2.0, soc=43.0, consumption=0.6)),
            MODE_EIN,
            now=NOW,
        )

    mock_inverter.async_set_discharge.assert_called_once_with(
        pytest.approx(2.6), target_soc=43.0
    )


async def test_entladung_clampt_an_der_maximalen_entladeleistung(mock_hass, mock_inverter):
    mock_inverter.get_max_discharge_power_kw.return_value = 2.5
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte(haus=1.5):
        await ex.async_guard_cycle(
            _state(_slot(0, battery_p=3.0, grid_p=2.0, soc=40.0)), MODE_EIN, now=NOW
        )

    mock_inverter.async_set_discharge.assert_called_once_with(
        pytest.approx(2.5), target_soc=40.0
    )


async def test_entladung_totbaender_fuer_leistung_und_ziel_soc(mock_hass, mock_inverter):
    ex = _make_executor(mock_hass, mock_inverter)
    plan = _state(_slot(0, battery_p=2.6, grid_p=2.0, soc=43.0))

    with _messwerte(haus=0.8):
        await ex.async_guard_cycle(plan, MODE_EIN, now=NOW)
    # Hauslast schwankt um 0,1 kW, Ziel-SOC um 0,5 Punkte → kein Schreiben
    with _messwerte(haus=0.9):
        await ex.async_guard_cycle(
            _state(_slot(0, battery_p=2.6, grid_p=2.0, soc=42.5)),
            MODE_EIN,
            now=NOW + timedelta(seconds=30),
        )
    assert mock_inverter.async_set_discharge.call_count == 1

    # Hauslast springt um 0,7 kW → nachführen
    with _messwerte(haus=1.5):
        await ex.async_guard_cycle(plan, MODE_EIN, now=NOW + timedelta(seconds=60))
    assert mock_inverter.async_set_discharge.call_count == 2
    assert mock_inverter.async_set_discharge.call_args.args[0] == pytest.approx(3.5)


async def test_ziel_soc_aenderung_ab_einem_prozentpunkt_schreibt(mock_hass, mock_inverter):
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte(haus=0.8):
        await ex.async_guard_cycle(
            _state(_slot(0, battery_p=2.6, grid_p=2.0, soc=43.0)), MODE_EIN, now=NOW
        )
        await ex.async_guard_cycle(
            _state(_slot(0, battery_p=2.6, grid_p=2.0, soc=41.0)),
            MODE_EIN,
            now=NOW + timedelta(seconds=30),
        )

    assert mock_inverter.async_set_discharge.call_count == 2
    assert mock_inverter.async_set_discharge.call_args.kwargs["target_soc"] == 41.0


async def test_pv_deckt_den_plan_keine_erzwungene_entladung(mock_hass, mock_inverter):
    """Rechnerische Entladeleistung ≈ 0 → Freigabe statt Mini-Entladung."""
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte(haus=0.5, pv=3.0):
        await ex.async_guard_cycle(
            _state(_slot(0, battery_p=2.6, grid_p=2.0, soc=43.0)), MODE_EIN, now=NOW
        )

    mock_inverter.async_set_discharge.assert_not_called()
    mock_inverter.async_stop_forcible.assert_called_once()


async def test_wechsel_von_entladung_auf_laden_stoppt_forcible(mock_hass, mock_inverter):
    """Wechsel von Einspeisung auf keine Einspeisung → async_stop_forcible()."""
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte(haus=0.8):
        await ex.async_guard_cycle(
            _state(_slot(0, battery_p=2.6, grid_p=2.0, soc=43.0)), MODE_EIN, now=NOW
        )
        await ex.async_guard_cycle(
            _state(_slot(0, battery_p=-1.5)), MODE_EIN, now=NOW + timedelta(seconds=30)
        )

    mock_inverter.async_stop_forcible.assert_called_once()
    mock_inverter.async_set_charge_limit.assert_called_once_with(1.5)


async def test_wechsel_von_entladung_auf_freigabe_stoppt_forcible(mock_hass, mock_inverter):
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte(haus=0.8):
        await ex.async_guard_cycle(
            _state(_slot(0, battery_p=2.6, grid_p=2.0, soc=43.0)), MODE_EIN, now=NOW
        )
        await ex.async_guard_cycle(
            _state(_slot(0, battery_p=0.4, grid_p=-0.2)),
            MODE_EIN,
            now=NOW + timedelta(seconds=30),
        )

    mock_inverter.async_stop_forcible.assert_called_once()
    assert "Normalbetrieb" in ex.last_status


# ---------------------------------------------------------------------------
# Not-Aus: anhaltender Netzbezug während einer Entladung
# ---------------------------------------------------------------------------

_DISCHARGE_SLOTS = (
    _slot(0, battery_p=2.6, grid_p=2.0, soc=43.0),
    _slot(15, battery_p=2.6, grid_p=2.0, soc=38.0),
)


async def test_notaus_nach_drei_laeufen_mit_netzbezug(mock_hass, mock_inverter):
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte(haus=0.8):
        await ex.async_guard_cycle(_state(*_DISCHARGE_SLOTS), MODE_EIN, now=NOW)
    assert mock_inverter.async_set_discharge.call_count == 1

    # Drei Guard-Läufe mit > 1 kW Netzbezug (Vorzeichen: negativ = Bezug)
    for sekunden in (30, 60, 90):
        with _messwerte(export=-1.5, haus=0.8):
            await ex.async_guard_cycle(
                _state(*_DISCHARGE_SLOTS),
                MODE_EIN,
                now=NOW + timedelta(seconds=sekunden),
                )

    mock_inverter.async_stop_forcible.assert_called_once()
    assert "Not-Aus" in ex.last_status

    # Gleicher Slot → keine neue Entladung
    with _messwerte(export=0.0, haus=0.8):
        await ex.async_guard_cycle(
            _state(*_DISCHARGE_SLOTS), MODE_EIN, now=NOW + timedelta(seconds=120)
        )
    assert mock_inverter.async_set_discharge.call_count == 1
    assert "gesperrt" in ex.last_status


async def test_notaus_haelt_auch_wenn_der_plan_kurz_fehlt(mock_hass, mock_inverter):
    """Der Not-Aus überwacht bewusst auch ohne Fahrplan — dann gab es aber
    keinen Slot, an den sich die Sperre hängen konnte.

    Zwei Löcher, beide hier abgedeckt: die Sperre wurde auf ``None`` gesetzt
    (und traf damit nie einen Slot), und ein einzelner Lauf ohne Plan hob
    eine bestehende Sperre sogar auf. Die Entladung startete danach sofort
    wieder in denselben Netzbezug — genau in den Zustand, den der Not-Aus
    gerade beendet hatte.
    """
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte(haus=0.8):
        await ex.async_guard_cycle(_state(*_DISCHARGE_SLOTS), MODE_EIN, now=NOW)
    assert mock_inverter.async_set_discharge.call_count == 1

    # Netzbezug — und beim dritten Lauf fehlt der Fahrplan kurz.
    for sekunden, zustand in (
        (30, _state(*_DISCHARGE_SLOTS)),
        (60, _state(*_DISCHARGE_SLOTS)),
        (90, {"available": False, "error": "Verbrauchsprofil noch nicht geladen"}),
    ):
        with _messwerte(export=-1.5, haus=0.8):
            await ex.async_guard_cycle(
                zustand, MODE_EIN, now=NOW + timedelta(seconds=sekunden)
            )

    mock_inverter.async_stop_forcible.assert_called()

    # Plan ist zurück, Slot unverändert: die Entladung darf NICHT sofort
    # wieder starten.
    with _messwerte(export=0.0, haus=0.8):
        await ex.async_guard_cycle(
            _state(*_DISCHARGE_SLOTS), MODE_EIN, now=NOW + timedelta(seconds=120)
        )
    assert mock_inverter.async_set_discharge.call_count == 1, (
        "Entladung nach Not-Aus ohne Plan sofort wieder gestartet"
    )
    assert "gesperrt" in ex.last_status


async def test_notaus_zaehler_reset_bei_normalem_netz(mock_hass, mock_inverter):
    """Kurzzeitiger Bezug (2 Läufe), dann wieder normal → kein Not-Aus."""
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte(haus=0.8):
        await ex.async_guard_cycle(_state(*_DISCHARGE_SLOTS), MODE_EIN, now=NOW)

    for sekunden, export in ((30, -1.5), (60, -1.5), (90, 0.3), (120, -1.5), (150, -1.5)):
        with _messwerte(export=export, haus=0.8):
            await ex.async_guard_cycle(
                _state(*_DISCHARGE_SLOTS),
                MODE_EIN,
                now=NOW + timedelta(seconds=sekunden),
            )

    mock_inverter.async_stop_forcible.assert_not_called()


async def test_notaus_sperre_endet_mit_dem_slotwechsel(mock_hass, mock_inverter):
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte(haus=0.8):
        await ex.async_guard_cycle(_state(*_DISCHARGE_SLOTS), MODE_EIN, now=NOW)
    for sekunden in (30, 60, 90):
        with _messwerte(export=-1.5, haus=0.8):
            await ex.async_guard_cycle(
                _state(*_DISCHARGE_SLOTS), MODE_EIN, now=NOW + timedelta(seconds=sekunden)
            )
    assert mock_inverter.async_stop_forcible.call_count == 1

    # Nächster Slot (19:15) → Sperre fällt, Entladung startet wieder
    # (last_run mitziehen — der Runner rechnet ja minütlich weiter)
    spaeter = NOW + timedelta(minutes=15, seconds=30)
    with _messwerte(export=0.0, haus=0.8):
        await ex.async_guard_cycle(
            _state(*_DISCHARGE_SLOTS, last_run=spaeter), MODE_EIN, now=spaeter
        )
    assert mock_inverter.async_set_discharge.call_count == 2


# ---------------------------------------------------------------------------
# Failsafe: Fahrplan fehlt, fehlerhaft oder veraltet
# ---------------------------------------------------------------------------


async def test_failsafe_gibt_einmalig_frei(mock_hass, mock_inverter):
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte():
        await ex.async_guard_cycle(_state(_slot(0, battery_p=-2.0)), MODE_EIN, now=NOW)
        assert mock_inverter.async_set_charge_limit.call_count == 1

        # Runner fällt aus: 16 Minuten kein brauchbarer Fahrplan
        spaeter = NOW + timedelta(minutes=16)
        await ex.async_guard_cycle(_state(available=False), MODE_EIN, now=spaeter)
        assert mock_inverter.async_stop_forcible.call_count == 1
        assert "Failsafe" in ex.last_status

        # Bleibt einmalig — kein wiederholtes Freigeben
        await ex.async_guard_cycle(
            _state(available=False), MODE_EIN, now=spaeter + timedelta(seconds=30)
        )
        assert mock_inverter.async_stop_forcible.call_count == 1

        # Fahrplan kommt zurück → normale Steuerung
        frisch = spaeter + timedelta(minutes=1)
        await ex.async_guard_cycle(
            _state(_slot(0, battery_p=-2.0), last_run=frisch), MODE_EIN, now=frisch
        )
        assert mock_inverter.async_set_charge_limit.call_count == 2


async def test_eingefrorener_runner_wird_erkannt(mock_hass, mock_inverter):
    """Ein toter Runner lässt sein letztes Ergebnis „verfügbar" in hass.data
    stehen — die Slots reichen 48 h weit. Der Executor muss die Frische am
    last_run-Zeitstempel messen, nicht an der Verfügbarkeit."""
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte():
        await ex.async_guard_cycle(_state(_slot(0, battery_p=-2.0)), MODE_EIN, now=NOW)

        eingefroren = _state(
            _slot(0, battery_p=-2.0), _slot(15, battery_p=-2.0),
            last_run=NOW - timedelta(minutes=20),
        )
        await ex.async_guard_cycle(eingefroren, MODE_EIN, now=NOW + timedelta(seconds=30))

    mock_inverter.async_stop_forcible.assert_called_once()
    assert "Failsafe" in ex.last_status


async def test_kurzzeitig_fehlender_fahrplan_haelt_den_zustand(mock_hass, mock_inverter):
    """Ein einzelner Rechenfehler des Runners darf nicht sofort freigeben —
    der nächste Lauf in einer Minute wird ihn meist beheben."""
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte():
        await ex.async_guard_cycle(_state(_slot(0, battery_p=-2.0)), MODE_EIN, now=NOW)
        await ex.async_guard_cycle(
            _state(available=False), MODE_EIN, now=NOW + timedelta(seconds=60)
        )

    mock_inverter.async_stop_forcible.assert_not_called()
    assert "kurzzeitig" in ex.last_status


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


async def test_status_beschreibt_den_letzten_lauf(mock_hass, mock_inverter):
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte(haus=0.8):
        await ex.async_guard_cycle(
            _state(_slot(0, battery_p=2.6, grid_p=2.0, soc=43.0)), MODE_EIN, now=NOW
        )

    status = ex.status()
    assert status["supported"] is True
    assert status["mode"] == MODE_EIN
    assert status["active_kind"] == "discharge"
    assert status["written_discharge_kw"] == pytest.approx(2.8)
    assert status["written_target_soc"] == 43.0
    assert status["plan_action"]["kind"] == "discharge"
    assert status["plan_action"]["power_kw"] == pytest.approx(2.0)
    assert status["last_write_ok"] is True
    assert status["write_failures"] == 0


async def test_schreibfehler_werden_gezaehlt(mock_hass, mock_inverter):
    mock_inverter.async_set_charge_limit.return_value = False
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte():
        await ex.async_guard_cycle(_state(_slot(0, battery_p=-2.0)), MODE_EIN, now=NOW)

    assert ex.status()["write_failures"] == 1
    assert ex.status()["last_write_ok"] is False
    assert "Schreibfehler" in ex.last_status


# ---------------------------------------------------------------------------
# Volle Batterie: kein Eingriff statt Ladelimit 0
# ---------------------------------------------------------------------------


def test_volle_batterie_ohne_ladeplan_greift_nicht_ein():
    """Bei vollem Akku ist „nicht laden" Platzmangel, keine Blockierabsicht.

    Ladelimit 0 würde nichts bewirken (kein Platz) und nach einem Neustart
    im Weg stehen. Erwartet wird die Freigabe auf den Standardwert.
    """
    action = plan_action({"slots": [_slot(0, battery_p=0.0, grid_p=3.0, soc=100.0)]}, NOW)
    assert action.kind == "release"
    assert action.reason == "Normalbetrieb (Batterie voll)"


def test_fast_volle_batterie_greift_nicht_ein():
    """Die Schwelle liegt bei 99 % — ein Prozentpunkt Rest lohnt kein Limit."""
    action = plan_action({"slots": [_slot(0, battery_p=0.0, grid_p=3.0, soc=99.0)]}, NOW)
    assert action.kind == "release"


def test_batterie_mit_platz_blockiert_weiter():
    """Unter der Schwelle bleibt es bei Ladelimit 0 — sonst wäre die
    Morgen-Einspeisung wirkungslos, weil der Automatikmodus lädt."""
    action = plan_action({"slots": [_slot(0, battery_p=0.0, grid_p=3.0, soc=98.0)]}, NOW)
    assert action.kind == "charge_limit"
    assert action.power_kw == 0.0


def test_volle_batterie_wandert_mit_dem_ladedeckel():
    """Mit Deckel 90 % ist 90 % voll — sonst greift die Erkennung nie mehr.

    Der Regressionsfall: eine feste 99-%-Schwelle bliebe bei einem Deckel von
    90 % dauerhaft unerreicht. Die Anlage stünde immer unter „Ladelimit 0"
    statt im Automatikmodus, mit entsprechend vielen Registerschreibvorgängen
    (bei SolarEdge zählt genau die der Register-Writes-Sensor).
    """
    plan = {"slots": [_slot(0, battery_p=0.0, grid_p=3.0, soc=90.0)], "max_soc_pct": 90.0}
    action = plan_action(plan, NOW)
    assert action.kind == "release"
    assert action.reason == "Normalbetrieb (Batterie voll)"

    # Der ABSTAND bleibt derselbe wie ohne Deckel: das letzte Prozent.
    knapp = {"slots": [_slot(0, battery_p=0.0, grid_p=3.0, soc=89.0)], "max_soc_pct": 90.0}
    assert plan_action(knapp, NOW).kind == "release"

    # Darunter hat die Batterie echten Platz — weiter blockieren.
    platz = {"slots": [_slot(0, battery_p=0.0, grid_p=3.0, soc=88.0)], "max_soc_pct": 90.0}
    action = plan_action(platz, NOW)
    assert action.kind == "charge_limit"
    assert action.power_kw == 0.0


def test_volle_batterie_schwelle_ohne_deckel_unveraendert():
    """Fehlt der Deckel im Plan (Altplan, Archiv), gilt weiter 99 %."""
    assert _voll_ab({}) == 99.0
    assert _voll_ab(None) == 99.0
    assert _voll_ab({"max_soc_pct": 100.0}) == 99.0
    assert _voll_ab({"max_soc_pct": 90.0}) == 89.0
    # Unplausible Werte dürfen die Schwelle nicht nach oben schieben und damit
    # die Erkennung abschalten.
    assert _voll_ab({"max_soc_pct": "kaputt"}) == 99.0
    assert _voll_ab({"max_soc_pct": 140.0}) == 99.0
    assert _voll_ab({"max_soc_pct": 0.0}) == 99.0


def test_fehlender_soc_blockiert_weiter():
    """Ohne SOC-Angabe im Slot bleibt es beim sicheren Verhalten (blockieren)."""
    slot = _slot(0, battery_p=0.0, grid_p=3.0)
    slot["soc"] = None
    action = plan_action({"slots": [slot]}, NOW)
    assert action.kind == "charge_limit"
    assert action.power_kw == 0.0


async def test_volle_batterie_gibt_frei_und_meldet_grund(mock_hass, mock_inverter):
    """Der Grund landet im Status — nicht mehr „Laden blockiert"."""
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte(haus=0.5):
        await ex.async_guard_cycle(
            _state(_slot(0, battery_p=0.0, grid_p=3.0, soc=100.0)), MODE_EIN, now=NOW
        )

    mock_inverter.async_stop_forcible.assert_called_once()
    mock_inverter.async_set_charge_limit.assert_not_called()
    assert "Batterie voll" in ex.last_status


# ---------------------------------------------------------------------------
# Freigabe-Absicherung
# ---------------------------------------------------------------------------


async def test_neustart_im_anzeige_modus_holt_freigabe_nach(mock_hass, mock_inverter):
    """Ein Limit aus der Vorsession käme im Anzeige-Modus sonst nie zurück.

    Beim ersten Lauf ist _last_mode None — die Wechsel-Erkennung Ein→Test
    greift dann nicht, und der Lauf steigt vor dem Schreiben aus.
    """
    ex = _make_executor(mock_hass, mock_inverter)

    with _messwerte():
        await ex.async_guard_cycle(_state(_slot(0, battery_p=-2.0)), MODE_TEST, now=NOW)

    mock_inverter.async_stop_forcible.assert_called_once()
    mock_inverter.async_set_charge_limit.assert_not_called()
    assert ex._display_release_pending is False


async def test_nachgeholte_freigabe_wartet_auf_startphase(mock_hass, mock_inverter):
    """In der Startphase sind die Wechselrichter-Entitäten evtl. nicht geladen."""
    ex = _make_executor(mock_hass, mock_inverter)
    ex._created_at = NOW  # Grace Period läuft noch

    with _messwerte():
        await ex.async_guard_cycle(_state(_slot(0, battery_p=-2.0)), MODE_TEST, now=NOW)

    mock_inverter.async_stop_forcible.assert_not_called()
    assert ex._display_release_pending is True


async def test_nachgeholte_freigabe_wird_wiederholt(mock_hass, mock_inverter):
    """Schlägt sie fehl, muss der nächste Lauf es erneut versuchen."""
    ex = _make_executor(mock_hass, mock_inverter)
    mock_inverter.async_stop_forcible.return_value = False

    with _messwerte():
        await ex.async_guard_cycle(_state(_slot(0, battery_p=-2.0)), MODE_TEST, now=NOW)
    assert ex._display_release_pending is True

    mock_inverter.async_stop_forcible.return_value = True
    with _messwerte():
        await ex.async_guard_cycle(
            _state(_slot(0, battery_p=-2.0)), MODE_TEST, now=NOW + timedelta(seconds=30)
        )
    assert ex._display_release_pending is False


async def test_freigabe_merkt_sich_fehlschlag_nicht_als_erfolg(mock_hass, mock_inverter):
    """Sonst steigt _apply_release künftig früh aus und das Limit bleibt stehen."""
    ex = _make_executor(mock_hass, mock_inverter)
    mock_inverter.async_stop_forcible.return_value = False

    ok = await ex.async_release()

    assert ok is False
    assert ex._active_kind is None
    assert ex.write_failures == 1


async def test_freigabe_wird_nach_fehlschlag_erneut_versucht(mock_hass, mock_inverter):
    """Zwei Läufe mit Absicht „release": nach einem Fehlschlag noch ein Versuch."""
    ex = _make_executor(mock_hass, mock_inverter)
    plan = _state(_slot(0, battery_p=1.5, grid_p=0.0))  # Entladung nur fürs Haus
    mock_inverter.async_stop_forcible.return_value = False

    with _messwerte(haus=1.5):
        await ex.async_guard_cycle(plan, MODE_EIN, now=NOW)
    assert mock_inverter.async_stop_forcible.call_count == 1

    mock_inverter.async_stop_forcible.return_value = True
    with _messwerte(haus=1.5):
        await ex.async_guard_cycle(plan, MODE_EIN, now=NOW + timedelta(seconds=30))
    assert mock_inverter.async_stop_forcible.call_count == 2
    assert ex._active_kind == "release"


async def test_wiederholtes_umschalten_gibt_jedes_mal_frei(mock_hass, mock_inverter):
    """Ein → Aus → Ein → Aus: auch beim zweiten Mal muessen die Steuerwerte
    zurueckgenommen werden.

    Die einmalige Freigabe haengt am Flag _display_release_pending, das nach
    dem ersten Mal verbraucht ist. Ohne zweite Bedingung blieb das zuletzt
    gesetzte Ladelimit im Wechselrichter stehen, obwohl die Optimierung aus
    war.
    """
    ex = _make_executor(mock_hass, mock_inverter)
    plan = _state(_slot(0, battery_p=-2.0))

    with _messwerte():
        await ex.async_guard_cycle(plan, MODE_EIN, now=NOW)
        await ex.async_guard_cycle(plan, MODE_AUS, now=NOW + timedelta(seconds=30))
        assert mock_inverter.async_stop_forcible.call_count == 1

        # Zurueck auf Ein — es wird wieder ein Limit gesetzt.
        await ex.async_guard_cycle(plan, MODE_EIN, now=NOW + timedelta(seconds=60))
        assert mock_inverter.async_set_charge_limit.call_count >= 1

        # Und wieder auf Aus: erneut freigeben.
        await ex.async_guard_cycle(plan, MODE_AUS, now=NOW + timedelta(seconds=90))
        assert mock_inverter.async_stop_forcible.call_count == 2, (
            "zweites Umschalten auf Aus nahm die Steuerwerte nicht zurueck"
        )
