"""Prognose der Puffertemperatur im Fahrplan — Nachrechnung, kein Teil des LP.

Die Kurve folgt der geplanten Heizstab-Wärme je Slot mit der EFFEKTIVEN
Wärmekapazität (Wasser plus Verlustaufschlag, dieselbe Zahl wie das
Pufferbudget des Controllers) und fällt durch einen festen
Bereitschaftsverlust langsam ab. Wichtigster Test: Wird genau das Budget
verheizt, endet die Kurve an der Maximaltemperatur — nur der
Bereitschaftsverlust der Laufzeit fehlt.
"""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.eeg_energy_optimizer import schedule as sched
from custom_components.eeg_energy_optimizer.const import (
    PUFFER_BEREITSCHAFTSVERLUST_KW,
    PUFFER_UMGEBUNG_C,
    PUFFER_WH_PRO_LITER_KELVIN_EFFEKTIV,
    WASSER_WH_PRO_LITER_KELVIN,
)

TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 8, 24, 5, 0, tzinfo=TZ)
LITER = 600.0
DT_H = 0.25
# Kelvin je kWh am Heizstab bzw. je kWh Verlust — dieselben Formeln wie im Code.
K_HEIZ = 1000.0 / (LITER * PUFFER_WH_PRO_LITER_KELVIN_EFFEKTIV)
K_VERLUST = 1000.0 / (LITER * WASSER_WH_PRO_LITER_KELVIN)
VERLUST_K = PUFFER_BEREITSCHAFTSVERLUST_KW * DT_H * K_VERLUST


def _inputs(**abweichungen):
    basis = dict(
        start=NOW,
        time_res_s=900,
        timestamps=[NOW],
        consumption_kw=[1.0],
        production_kw=[0.0],
        min_production_kw=None,
        worst_case_factor=0.6,
        battery_free_kwh=5.0,
        battery_capacity_kwh=10.0,
        battery_power_limit_kw=5.0,
        soc_pct=50.0,
        ac_limit_kw=8.0,
        feedin_limit_kw=7.5,
        feedin_price=0.082,
        feedin_price_night=0.102,
        night_start_hour=22,
        night_end_hour=6,
        consumption_price=0.25,
        battery_cost=0.0,
        heizstab_max_kw=6.0,
        heizstab_puffer_liter=LITER,
        heizstab_temp_c=45.0,
        heizstab_maxtemp_c=80.0,
    )
    basis.update(abweichungen)
    return sched.ScheduleInputs(**basis)


def _slots(heizstab_kw, n):
    return [
        {"t": (NOW + timedelta(minutes=15 * i)).isoformat(), "heizstab": heizstab_kw}
        for i in range(n)
    ]


@pytest.mark.parametrize(
    "abweichung",
    [
        {"heizstab_puffer_liter": 0.0},
        {"heizstab_temp_c": None},
        {"heizstab_maxtemp_c": 0.0},
        {"heizstab_max_kw": 0.0},
    ],
)
def test_ohne_volumen_temperatur_oder_heizstab_keine_kurve(abweichung):
    """Fehlt eine Angabe, bleibt der Schlüssel weg — das Panel zeichnet dann
    nichts, statt eine Kurve aus Annahmen zu zeigen."""
    slots = _slots(3.0, 4)
    sched._puffer_temperatur_verlauf(slots, _inputs(**abweichung))
    assert all("puffer_temp_c" not in s for s in slots)


def test_heizen_hebt_mit_effektiver_waermekapazitaet():
    """6 kW über eine Stunde = 6 kWh: bei 600 L knapp 7,8 K statt 8,6 K ohne
    Aufschlag — abzüglich vier Viertelstunden Bereitschaftsverlust."""
    slots = _slots(6.0, 4)
    sched._puffer_temperatur_verlauf(slots, _inputs())
    erwartet = 45.0 + 6.0 * K_HEIZ - 4 * VERLUST_K
    assert slots[-1]["puffer_temp_c"] == pytest.approx(erwartet, abs=0.06)
    # Ohne Aufschlag läge sie höher — der Aufschlag wirkt wirklich.
    assert slots[-1]["puffer_temp_c"] < 45.0 + 6.0 / (LITER * WASSER_WH_PRO_LITER_KELVIN / 1000.0)
    # Monoton steigend, solange geheizt wird.
    werte = [s["puffer_temp_c"] for s in slots]
    assert werte == sorted(werte)


def test_deckel_an_der_maximaltemperatur():
    """Der Heizstab stoppt am Maximum — die Kurve auch."""
    slots = _slots(6.0, 12)
    sched._puffer_temperatur_verlauf(slots, _inputs(heizstab_temp_c=78.0))
    assert max(s["puffer_temp_c"] for s in slots) <= 80.0
    assert slots[-1]["puffer_temp_c"] == pytest.approx(80.0, abs=0.1)


def test_budget_verheizt_endet_an_der_maximaltemperatur():
    """Konsistenz mit dem Controller: Genau das Pufferbudget (effektive
    Kapazität, 45 → 80 °C) über sechs Stunden verheizt, landet die Kurve am
    Maximum — es fehlt nur der Bereitschaftsverlust dieser sechs Stunden."""
    budget_kwh = LITER * (80.0 - 45.0) * PUFFER_WH_PRO_LITER_KELVIN_EFFEKTIV / 1000.0
    n = 24
    slots = _slots(budget_kwh / (n * DT_H), n)
    sched._puffer_temperatur_verlauf(slots, _inputs())
    ende = slots[-1]["puffer_temp_c"]
    assert ende == pytest.approx(80.0 - n * VERLUST_K, abs=0.1)
    assert 78.5 < ende <= 80.0


def test_bereitschaftsverlust_laesst_die_kurve_ueber_nacht_fallen():
    """Ohne Heizen sinkt die Temperatur stetig — bei 600 L gut 4 K am Tag."""
    slots = _slots(0.0, 96)
    sched._puffer_temperatur_verlauf(slots, _inputs(heizstab_temp_c=60.0))
    werte = [s["puffer_temp_c"] for s in slots]
    assert werte == sorted(werte, reverse=True)
    assert 60.0 - werte[-1] == pytest.approx(96 * VERLUST_K, abs=0.1)
    assert 3.5 < 60.0 - werte[-1] < 4.5


def test_nie_unter_die_umgebungstemperatur():
    slots = _slots(0.0, 96)
    sched._puffer_temperatur_verlauf(slots, _inputs(heizstab_temp_c=PUFFER_UMGEBUNG_C + 0.3))
    assert min(s["puffer_temp_c"] for s in slots) >= PUFFER_UMGEBUNG_C
    # Ein Puffer, der schon kälter als die Umgebung ist, wird nicht kälter gerechnet.
    slots = _slots(0.0, 8)
    sched._puffer_temperatur_verlauf(slots, _inputs(heizstab_temp_c=12.0))
    assert all(s["puffer_temp_c"] == 12.0 for s in slots)


def test_start_ueber_dem_maximum_springt_nicht_auf_den_deckel():
    """85 °C gemessen bei 80 °C Maximum: die Kurve beginnt bei 85 und kühlt
    ab — der Deckel gilt fürs Heizen, nicht für den Messwert."""
    slots = _slots(6.0, 4)
    sched._puffer_temperatur_verlauf(slots, _inputs(heizstab_temp_c=85.0))
    assert slots[0]["puffer_temp_c"] == pytest.approx(85.0 - VERLUST_K, abs=0.06)
    assert all(s["puffer_temp_c"] <= 85.0 for s in slots)
