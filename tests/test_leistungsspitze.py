"""Netzbezug je Viertelstunde und Bezugsspitze des Monats (leistungsspitze.py)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.eeg_energy_optimizer.leistungsspitze import (
    ABTASTUNG_S,
    HALTEN_MAX_S,
    Leistungsspitze,
    _runde_kw,
    viertelstunde_von,
)

UTC = timezone.utc
# 25.09.2026 18:00 Ortszeit (MESZ, UTC+2).
T0 = datetime(2026, 9, 25, 16, 0, tzinfo=UTC)


def _neu() -> Leistungsspitze:
    return Leistungsspitze(None, "test", {})


def _min(m: float) -> datetime:
    return T0 + timedelta(minutes=m)


def _halte(ls: Leistungsspitze, von: datetime, bis: datetime, kw: float | None) -> None:
    """Abtastung wie im Betrieb: alle ABTASTUNG_S ein Stützpunkt, bis < ``bis``."""
    t = von
    while t < bis:
        ls.messwert(t, kw)
        t += timedelta(seconds=ABTASTUNG_S)


def test_gleichmaessiger_bezug_ergibt_seinen_wert():
    ls = _neu()
    _halte(ls, _min(0), _min(15), 3.0)
    ls.grenze(_min(15))
    assert ls.letzte_viertelstunde["kw"] == 3.0
    assert ls.letzte_viertelstunde["vollstaendig"] is True
    assert ls.spitze["kw"] == 3.0


def test_kurze_spitze_zaehlt_nur_anteilig():
    # 3 Minuten Wasserkocher (2 kW über 0,5 kW Grundlast) — zählt als Mittel.
    ls = _neu()
    _halte(ls, _min(0), _min(5), 0.5)
    _halte(ls, _min(5), _min(8), 2.5)
    _halte(ls, _min(8), _min(15), 0.5)
    ls.grenze(_min(15))
    assert ls.letzte_viertelstunde["kw"] == pytest.approx(0.9)


def test_einspeisung_verrechnet_sich_nicht_mit_bezug():
    # Halbe Viertelstunde 4 kW Bezug, halbe Einspeisung (als Bezug 0): Der
    # Zähler führt beide Richtungen getrennt → 2 kW, nicht 0.
    ls = _neu()
    _halte(ls, _min(0), _min(7.5), 4.0)
    _halte(ls, _min(7.5), _min(15), 0.0)
    ls.grenze(_min(15))
    assert ls.letzte_viertelstunde["kw"] == 2.0


def test_negativer_wert_zaehlt_als_null():
    ls = _neu()
    _halte(ls, _min(0), _min(15), -3.0)
    ls.grenze(_min(15))
    assert ls.letzte_viertelstunde["kw"] == 0.0


def test_stuetzpunkt_ueber_die_grenze_wird_aufgeteilt():
    # Ohne Zeitgeber: der nächste Stützpunkt schließt die Viertelstunde ab
    # und teilt den gehaltenen Wert auf beide auf.
    ls = _neu()
    _halte(ls, _min(10), _min(14.5), 6.0)
    ls.messwert(_min(15.5), 0.0)
    assert ls.letzte_viertelstunde["start"] == _min(0).isoformat()
    # 10–15 min mit 6 kW = 0,5 kWh → 2 kW, aber nur 5 von 15 min gemessen.
    assert ls.letzte_viertelstunde["kw"] == 2.0
    assert ls.letzte_viertelstunde["vollstaendig"] is False
    # 15–15,5 min trägt noch den gehaltenen Wert: 0,05 kWh → 0,2 kW.
    assert ls.laufend(_min(15.5))["bisher_kw"] == pytest.approx(0.2)


def test_monatsspitze_bleibt_das_maximum():
    ls = _neu()
    for i, kw in enumerate([5.0, 2.0, 7.0, 1.0]):
        _halte(ls, _min(15 * i), _min(15 * (i + 1)), kw)
    ls.grenze(_min(60))
    assert ls.spitze["kw"] == 7.0
    assert ls.spitze["start"] == _min(30).isoformat()
    assert ls.letzte_viertelstunde["kw"] == 1.0


def test_unlesbarer_sensor_zaehlt_nichts():
    ls = _neu()
    _halte(ls, _min(0), _min(5), 4.0)
    _halte(ls, _min(5), _min(15), None)
    ls.grenze(_min(15))
    # 5 min mit 4 kW = 0,333 kWh → 1,33 kW als Untergrenze.
    assert ls.letzte_viertelstunde["kw"] == 1.33
    assert ls.letzte_viertelstunde["vollstaendig"] is False


def test_viertelstunde_ohne_messung_ergibt_keinen_eintrag():
    ls = _neu()
    _halte(ls, _min(0), _min(15), None)
    ls.grenze(_min(15))
    assert ls.letzte_viertelstunde is None
    assert ls.spitze is None


def test_ohne_stuetzpunkte_gilt_ein_wert_nur_begrenzt():
    # Kommen gar keine Stützpunkte mehr (Abtastung steht), darf der letzte
    # Wert nicht die ganze Viertelstunde tragen.
    ls = _neu()
    ls.messwert(_min(0), 8.0)
    ls.grenze(_min(15))
    assert ls.letzte_viertelstunde["kw"] == _runde_kw(8.0 * HALTEN_MAX_S / 900)
    assert ls.letzte_viertelstunde["vollstaendig"] is False


def test_monatswechsel_setzt_zurueck_und_archiviert():
    ls = _neu()
    # 30.09. 23:30 Ortszeit = 21:30 UTC.
    ende_sept = datetime(2026, 9, 30, 21, 30, tzinfo=UTC)
    _halte(ls, ende_sept, ende_sept + timedelta(minutes=15), 6.0)
    _halte(ls, ende_sept + timedelta(minutes=15), ende_sept + timedelta(minutes=30), 2.0)
    assert ls.monat == "2026-09"
    ls.grenze(ende_sept + timedelta(minutes=30))  # 00:00 Ortszeit, 1. Oktober
    assert ls.monat == "2026-10"
    assert ls.spitze is None
    # Die Viertelstunde 23:45–00:00 gehört noch zum September.
    assert ls.verlauf["2026-09"]["kw"] == 6.0
    assert ls.letzte_viertelstunde["kw"] == 2.0


def test_viertelstunden_liegen_auf_der_wanduhr():
    t = datetime(2026, 9, 25, 16, 7, 42, tzinfo=UTC)
    assert viertelstunde_von(t) == datetime(2026, 9, 25, 16, 0, tzinfo=UTC)


def test_kaufmaennisch_gerundet():
    assert _runde_kw(2.345) == 2.35
    assert _runde_kw(2.344) == 2.34


def test_laufend_bisher_und_hochrechnung():
    ls = _neu()
    _halte(ls, _min(0), _min(3), 0.0)
    _halte(ls, _min(3), _min(5), 8.0)
    laufend = ls.laufend(_min(5))
    # bisher: 2 min × 8 kW = 0,267 kWh → 1,07 kW (steigt nur).
    assert laufend["bisher_kw"] == pytest.approx(1.067, abs=0.001)
    # Hochrechnung: 8 kW bis zur Grenze → (0,267 + 10/60 · 8) / 0,25.
    assert laufend["hochrechnung_kw"] == pytest.approx(6.4, abs=0.001)


def test_speicherstand_uebersteht_neuladen_in_derselben_viertelstunde():
    ls = _neu()
    _halte(ls, _min(0), _min(15), 4.0)
    _halte(ls, _min(15), _min(20), 2.0)
    ls.grenze(_min(20))
    daten = ls._als_dict()

    neu = _neu()
    neu.aus_dict(daten, _min(21))
    assert neu.spitze["kw"] == 4.0
    _halte(neu, _min(21), _min(30), 2.0)
    neu.grenze(_min(30))
    # 15–20 mit 2 kW vor dem Neuladen, 21–30 danach; die Minute dazwischen fehlt.
    assert neu.letzte_viertelstunde["kw"] == pytest.approx(_runde_kw(2.0 * 14 / 15))
    assert neu.letzte_viertelstunde["vollstaendig"] is False


def test_speicherstand_aus_dem_vormonat_wird_archiviert():
    ls = _neu()
    _halte(ls, _min(0), _min(15), 5.0)
    ls.grenze(_min(15))
    daten = ls._als_dict()

    neu = _neu()
    neu.aus_dict(daten, datetime(2026, 10, 2, 8, 0, tzinfo=UTC))
    assert neu.monat == "2026-10"
    assert neu.spitze is None
    assert neu.verlauf["2026-09"]["kw"] == 5.0


def test_beobachter_hoert_den_abschluss():
    ls = _neu()
    aufrufe = []
    ls.add_listener(lambda: aufrufe.append(1))
    _halte(ls, _min(0), _min(15), 1.0)
    assert aufrufe == []
    ls.grenze(_min(15))
    assert aufrufe == [1]


# ---------------------------------------------------------------------------
# Frische der Quelle
# ---------------------------------------------------------------------------


def _hass_mit_netz(wert_w: str, zuletzt: datetime) -> MagicMock:
    hass = MagicMock()
    state = SimpleNamespace(
        state=wert_w,
        attributes={"unit_of_measurement": "W"},
        last_reported=zuletzt,
        last_updated=zuletzt,
    )
    hass.states.get = MagicMock(return_value=state)
    return hass


def test_frischer_zustand_liefert_den_bezug():
    hass = _hass_mit_netz("-2500", _min(0))
    ls = Leistungsspitze(hass, "t", {"grid_power_sensor": "sensor.netz", "inverter_type": "huawei_sun2000"})
    ls._store = None
    # Huawei: Netz positiv = Einspeisung → −2500 W sind 2,5 kW Bezug.
    assert ls._bezug_jetzt_kw(_min(0.5)) == pytest.approx(2.5)


def test_eingefrorener_zustand_zaehlt_nicht():
    hass = _hass_mit_netz("-2500", _min(0))
    ls = Leistungsspitze(hass, "t", {"grid_power_sensor": "sensor.netz", "inverter_type": "huawei_sun2000"})
    ls._store = None
    assert ls._bezug_jetzt_kw(_min(0) + timedelta(seconds=HALTEN_MAX_S + 1)) is None


# ---------------------------------------------------------------------------
# Sensoren
# ---------------------------------------------------------------------------


async def test_sensoren_zeigen_abgeschlossenen_wert_und_monatsspitze():
    from custom_components.eeg_energy_optimizer.sensor import (
        BezugsspitzeMonatSensor,
        NetzbezugViertelstundeSensor,
    )

    ls = _neu()
    _halte(ls, _min(0), _min(15), 6.0)
    _halte(ls, _min(15), _min(30), 2.0)
    ls.grenze(_min(30))
    entry = SimpleNamespace(entry_id="e1")

    vs = NetzbezugViertelstundeSensor(None, entry, ls)
    await vs.async_update()
    assert vs.native_value == 2.0
    assert vs.extra_state_attributes["vollstaendig"] is True

    ms = BezugsspitzeMonatSensor(None, entry, ls)
    await ms.async_update()
    assert ms.native_value == 6.0
    assert ms.extra_state_attributes["monat"] == "2026-09"
    assert ms.extra_state_attributes["verlauf"] == {}


async def test_sensoren_ohne_messung_sind_unbekannt():
    from custom_components.eeg_energy_optimizer.sensor import (
        BezugsspitzeMonatSensor,
        NetzbezugViertelstundeSensor,
    )

    ls = _neu()
    entry = SimpleNamespace(entry_id="e1")
    vs = NetzbezugViertelstundeSensor(None, entry, ls)
    ms = BezugsspitzeMonatSensor(None, entry, ls)
    await vs.async_update()
    await ms.async_update()
    assert vs.native_value is None
    assert ms.native_value is None
