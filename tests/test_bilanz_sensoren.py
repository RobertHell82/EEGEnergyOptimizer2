"""Tests für die sechs Geld-Sensoren der Energiebilanz.

Der Kern: Monat und Jahr müssen den laufenden Tag mitzählen (er steht noch
nicht im Archiv), und die PV-Ersparnis muss den Optimierungs-Vorteil als
Attribut ausweisen statt ihn dazuzuzählen.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.eeg_energy_optimizer import sensor as sensor_modul
from custom_components.eeg_energy_optimizer.const import DOMAIN
from custom_components.eeg_energy_optimizer.sensor import (
    OptimierungsVorteilSensor,
    PVErsparnisSensor,
)

TZ = timezone(timedelta(hours=2))
JETZT = datetime(2026, 8, 27, 14, 30, tzinfo=TZ)


class _FakeBilanz:
    """Bilanz-Doppel mit festen Werten — hier wird der Sensor geprüft."""

    def __init__(self, heute: dict, monate: dict | None = None) -> None:
        self._heute = heute
        self._monate = monate or {}

    def heute(self, inputs=None) -> dict:
        return self._heute

    def summe(self, feld: str, monat: str | None = None, jahr: str | None = None):
        gesamt = 0.0
        for schluessel, eintrag in self._monate.items():
            if monat and schluessel != monat:
                continue
            if jahr and not schluessel.startswith(jahr):
                continue
            gesamt += float(eintrag.get(feld, 0.0) or 0.0)
        return round(gesamt, 4)


HEUTE = {
    "pv_ersparnis": 3.4567,
    "opt_vorteil": 0.4212,
    "vermieden": 2.10,
    "erloes": 1.3567,
    "eigen_kwh": 8.1,
    "export_kwh": 20.0,
    "bezug_kwh": 1.2,
    "pv_kwh": 28.0,
    "eeg_kwh": 5.0,
    "ein_anteil": 1.0,
    "ist_summe": 1.9,
    "ref_summe": 1.4788,
}


def _hass(bilanz, waehrung="EUR"):
    hass = MagicMock()
    hass.config = SimpleNamespace(currency=waehrung)
    hass.data = {DOMAIN: {"entry1": {"bilanz": bilanz, "schedule": None}}}
    return hass


def _entry():
    return SimpleNamespace(entry_id="entry1")


@pytest.fixture(autouse=True)
def _feste_uhr():
    with patch.object(sensor_modul, "_now_local", return_value=JETZT):
        yield


# ---------------------------------------------------------------------------
# Ersparnis durch PV
# ---------------------------------------------------------------------------


async def test_pv_heute_zeigt_den_tageswert():
    sensor = PVErsparnisSensor(_hass(_FakeBilanz(HEUTE)), _entry(), "heute")

    await sensor.async_update()

    assert sensor.native_value == pytest.approx(3.46)
    assert sensor.native_unit_of_measurement == "EUR"


async def test_pv_monat_zaehlt_den_laufenden_tag_dazu():
    """Sonst wäre der Monatswert den ganzen Tag über zu klein."""
    bilanz = _FakeBilanz(HEUTE, {"2026-08": {"pv_ersparnis": 40.0}})
    sensor = PVErsparnisSensor(_hass(bilanz), _entry(), "monat")

    await sensor.async_update()

    assert sensor.native_value == pytest.approx(43.46)


async def test_pv_jahr_summiert_alle_monate_plus_heute():
    bilanz = _FakeBilanz(
        HEUTE,
        {
            "2026-07": {"pv_ersparnis": 50.0},
            "2026-08": {"pv_ersparnis": 40.0},
            "2025-08": {"pv_ersparnis": 999.0},   # Vorjahr zählt nicht
        },
    )
    sensor = PVErsparnisSensor(_hass(bilanz), _entry(), "jahr")

    await sensor.async_update()

    assert sensor.native_value == pytest.approx(93.46)


async def test_optimierungsvorteil_steht_als_attribut_nicht_in_der_summe():
    """Die zentrale Falle: Der Vorteil ist IN der PV-Ersparnis enthalten."""
    sensor = PVErsparnisSensor(_hass(_FakeBilanz(HEUTE)), _entry(), "heute")

    await sensor.async_update()
    attrs = sensor.extra_state_attributes

    assert attrs["davon_optimierung"] == pytest.approx(0.4212)
    # 2,10 vermieden + 1,3567 Erlös = 3,4567 — der Vorteil ist nicht addiert.
    assert sensor.native_value == pytest.approx(
        round(attrs["vermiedener_bezug"] + attrs["einspeiseerloes"], 2)
    )
    assert "nicht dazugezaehlt" in attrs["hinweis"]


async def test_eeg_anteil_wird_transparent_ausgewiesen():
    sensor = PVErsparnisSensor(_hass(_FakeBilanz(HEUTE)), _entry(), "heute")

    await sensor.async_update()
    attrs = sensor.extra_state_attributes

    assert attrs["eeg_kwh"] == pytest.approx(5.0)
    assert attrs["eeg_anteil_pct"] == pytest.approx(25.0)
    assert "EEG-Abrechnung" in attrs["eeg_hinweis"]


async def test_ohne_einspeisung_kein_eeg_attribut():
    ohne = dict(HEUTE, export_kwh=0.0, eeg_kwh=0.0)
    sensor = PVErsparnisSensor(_hass(_FakeBilanz(ohne)), _entry(), "heute")

    await sensor.async_update()

    assert "eeg_anteil_pct" not in sensor.extra_state_attributes


async def test_monat_und_jahr_tragen_keine_tagesattribute():
    """Attribute wie last_reset gehören zum Tageswert, nicht zum Monat."""
    sensor = PVErsparnisSensor(_hass(_FakeBilanz(HEUTE)), _entry(), "monat")

    await sensor.async_update()

    assert sensor.extra_state_attributes == {}


# ---------------------------------------------------------------------------
# Ersparnis durch die Optimierung
# ---------------------------------------------------------------------------


async def test_optimierung_heute_zeigt_beide_seiten_der_differenz():
    sensor = OptimierungsVorteilSensor(_hass(_FakeBilanz(HEUTE)), _entry(), "heute")

    await sensor.async_update()
    attrs = sensor.extra_state_attributes

    assert sensor.native_value == pytest.approx(0.42)
    assert attrs["mit_optimierung"] == pytest.approx(1.9)
    assert attrs["ohne_optimierung"] == pytest.approx(1.4788)
    assert attrs["modus_ein_anteil"] == 1.0
    assert "Modellrechnung" in attrs["hinweis"]


async def test_nicht_rechenbarer_vorteil_bleibt_ohne_wert():
    """Ohne Start-Ladestand gibt es keinen Wert — keine erfundene Null."""
    ohne = dict(HEUTE, opt_vorteil=None)
    sensor = OptimierungsVorteilSensor(_hass(_FakeBilanz(ohne)), _entry(), "heute")

    await sensor.async_update()

    assert sensor.native_value is None


async def test_monatssumme_ueberspringt_tage_ohne_vorteil():
    ohne = dict(HEUTE, opt_vorteil=None)
    bilanz = _FakeBilanz(ohne, {"2026-08": {"opt_vorteil": 6.0}})
    sensor = OptimierungsVorteilSensor(_hass(bilanz), _entry(), "monat")

    await sensor.async_update()

    # Der heutige Tag steuert nichts bei, das Archiv bleibt gültig.
    assert sensor.native_value == pytest.approx(6.0)


async def test_waehrung_kommt_aus_der_ha_konfiguration():
    sensor = PVErsparnisSensor(
        _hass(_FakeBilanz(HEUTE), waehrung="CHF"), _entry(), "heute"
    )

    assert sensor.native_unit_of_measurement == "CHF"


async def test_ohne_bilanz_kein_absturz():
    hass = MagicMock()
    hass.config = SimpleNamespace(currency="EUR")
    hass.data = {DOMAIN: {"entry1": {}}}
    sensor = PVErsparnisSensor(hass, _entry(), "heute")

    await sensor.async_update()

    assert sensor.native_value is None
