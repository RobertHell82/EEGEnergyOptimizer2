"""Wirkungskontrolle des Entladedeckels (``ScheduleExecutor._deckel_pruefen``).

Ein „True" des Treibers heißt nur, dass das Gerät den Deckel angenommen hat.
Ob die Batterie danach wirklich weniger abgibt, sieht nur die Messung — bei
Kostal, Sigenergy und Victron ist das am Gerät noch unbelegt. Gemeldet wird
(nie gestoppt), wenn die Entladung über ``GUARD_WIRKUNG_RUNS`` Läufe deutlich
über dem Deckel liegt.
"""

from unittest.mock import patch

import pytest

from custom_components.eeg_energy_optimizer import schedule_executor as sx
from custom_components.eeg_energy_optimizer.const import GUARD_WIRKUNG_RUNS

from .test_schedule_executor import _make_executor


def _laeufe(ex, batterie_kw, n):
    with patch.object(sx, "compute_battery_now_kw", return_value=batterie_kw):
        for _ in range(n):
            ex._deckel_pruefen()


@pytest.fixture
def ex(mock_hass, mock_inverter):
    ex = _make_executor(mock_hass, mock_inverter)
    ex._active_kind = "release"
    ex._written_entladedeckel_kw = 0.0  # halten
    return ex


def test_batterie_gibt_trotz_halten_ab(ex):
    _laeufe(ex, -2.0, GUARD_WIRKUNG_RUNS - 1)
    assert ex.status()["deckel_wirkungslos"] is False
    _laeufe(ex, -2.0, 1)
    status = ex.status()
    assert status["deckel_wirkungslos"] is True
    assert status["deckel_ist_kw"] == 2.0


def test_rauschen_beim_halten_ist_kein_versagen(ex):
    _laeufe(ex, -0.25, GUARD_WIRKUNG_RUNS * 2)
    assert ex.status()["deckel_wirkungslos"] is False


def test_knapp_ueber_einem_grossen_deckel_ist_kein_versagen(ex):
    """3 kW gedeckelt, 3,5 kW gemessen: über der Lücke, aber nicht über dem
    Anderthalbfachen — Regelträgheit, kein wirkungsloser Deckel."""
    ex._written_entladedeckel_kw = 3.0
    _laeufe(ex, -3.5, GUARD_WIRKUNG_RUNS * 2)
    assert ex.status()["deckel_wirkungslos"] is False


def test_wirkt_er_wieder_ist_die_meldung_weg(ex):
    _laeufe(ex, -2.0, GUARD_WIRKUNG_RUNS)
    assert ex.status()["deckel_wirkungslos"] is True
    _laeufe(ex, 0.0, 1)
    assert ex.status()["deckel_wirkungslos"] is False


def test_ohne_deckel_oder_in_einem_befehl_wird_nicht_geprueft(ex):
    ex._written_entladedeckel_kw = None
    _laeufe(ex, -5.0, GUARD_WIRKUNG_RUNS * 2)
    assert ex.status()["deckel_wirkungslos"] is False
    ex._written_entladedeckel_kw = 0.0
    ex._active_kind = "discharge"
    _laeufe(ex, -5.0, GUARD_WIRKUNG_RUNS * 2)
    assert ex.status()["deckel_wirkungslos"] is False


def test_ohne_messung_bleibt_der_zaehler_stehen(ex):
    _laeufe(ex, -2.0, GUARD_WIRKUNG_RUNS - 1)
    _laeufe(ex, None, 3)
    _laeufe(ex, -2.0, 1)
    assert ex.status()["deckel_wirkungslos"] is True
