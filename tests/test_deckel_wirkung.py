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


# ---------------------------------------------------------------------------
# Gegenrichtung: Deckel erlaubt Entladung, Netz über der Grenze, die
# Batterie gibt nichts — ``deckel_richtung = "steht"``.
# ---------------------------------------------------------------------------


def _steht_laeufe(ex, n, *, batterie_kw=0.0, export_kw=-6.0, soc=25.0):
    ex._batterie_soc_pct = lambda: soc
    with (
        patch.object(sx, "compute_battery_now_kw", return_value=batterie_kw),
        patch.object(sx, "compute_grid_export_kw", return_value=export_kw),
    ):
        for _ in range(n):
            ex._deckel_pruefen()


@pytest.fixture
def steht(ex):
    ex._written_entladedeckel_kw = 1.2
    ex.spitzenkappung = {"aktiv": True, "grenze_kw": 5.0, "basis_pct": 10.0,
                         "reserve_pct": 20.0, "schwelle_pct": 30.0}
    return ex


def test_batterie_steht_trotz_deckel(steht):
    _steht_laeufe(steht, GUARD_WIRKUNG_RUNS - 1)
    assert steht.status()["deckel_wirkungslos"] is False
    _steht_laeufe(steht, 1)
    status = steht.status()
    assert status["deckel_wirkungslos"] is True
    assert status["deckel_richtung"] == "steht"
    assert status["deckel_ist_kw"] == 0.0
    # Gibt sie wieder, ist der Befund weg.
    _steht_laeufe(steht, 1, batterie_kw=-1.0)
    assert steht.status()["deckel_wirkungslos"] is False
    assert steht.status()["deckel_richtung"] is None


def test_zu_viel_hat_seine_eigene_richtung(ex):
    _laeufe(ex, -2.0, GUARD_WIRKUNG_RUNS)
    assert ex.status()["deckel_richtung"] == "zu_viel"


@pytest.mark.parametrize(
    "aenderung",
    [
        {"deckel": 0.4},                 # unter 0,5 kW ist Halten gemeint
        {"export_kw": -4.0},             # Netz unter der Grenze: nichts zu decken
        {"batterie_kw": -0.2},           # sie gibt etwas
        {"soc": 10.5},                   # am Mindest-Ladestand hält sie zu Recht
        {"gesperrt": True},              # Kostal bis zum Watchdog-Rückfall
    ],
)
def test_steht_nur_wenn_alles_zutrifft(steht, aenderung):
    if "deckel" in aenderung:
        steht._written_entladedeckel_kw = aenderung["deckel"]
    if aenderung.get("gesperrt"):
        steht._inverter.batterie_gesperrt = True
    kw = {k: v for k, v in aenderung.items() if k in ("export_kw", "batterie_kw", "soc")}
    _steht_laeufe(steht, GUARD_WIRKUNG_RUNS * 2, **kw)
    assert steht.status()["deckel_wirkungslos"] is False


async def test_freigabe_loescht_den_befund(steht):
    """Failsafe, Not-Aus und Aus kehren vor ``_deckel_pruefen`` zurück —
    die Freigabe muss den Befund selbst mitnehmen."""
    _steht_laeufe(steht, GUARD_WIRKUNG_RUNS)
    assert steht.status()["deckel_wirkungslos"] is True
    await steht.async_release()
    status = steht.status()
    assert status["deckel_wirkungslos"] is False
    assert status["deckel_richtung"] is None
    assert status["deckel_ist_kw"] is None


async def test_aufgehobener_deckel_loescht_den_befund(ex):
    ex._inverter.supports_discharge_limit = True
    _laeufe(ex, -2.0, GUARD_WIRKUNG_RUNS)
    assert ex.status()["deckel_wirkungslos"] is True
    assert await ex._entladedeckel_aufheben(bremse=False) is True
    assert ex.status()["deckel_wirkungslos"] is False


def test_richtungswechsel_stellt_flag_und_richtung_um(steht):
    """Erst entlädt sie zu viel, dann steht sie: Der Befund folgt der neuen
    Richtung, statt beim alten stehen zu bleiben (eigene Telemetrie-Kennung)."""
    with patch.object(sx, "compute_grid_export_kw", return_value=-6.0):
        _laeufe(steht, -4.0, GUARD_WIRKUNG_RUNS)
    assert steht.status()["deckel_richtung"] == "zu_viel"
    _steht_laeufe(steht, GUARD_WIRKUNG_RUNS)
    status = steht.status()
    assert status["deckel_wirkungslos"] is True
    assert status["deckel_richtung"] == "steht"
    assert status["deckel_ist_kw"] == 0.0
    # Und zurück.
    with patch.object(sx, "compute_grid_export_kw", return_value=-6.0):
        _laeufe(steht, -4.0, GUARD_WIRKUNG_RUNS)
    status = steht.status()
    assert status["deckel_wirkungslos"] is True
    assert status["deckel_richtung"] == "zu_viel"
    assert status["deckel_ist_kw"] == 4.0
