"""Tests für die Prognosetage an die EEG-Statistik (prognose_telemetrie.py).

* Der Payload trägt Tagessummen und Reihen beider Quellen samt der eigenen
  Prognose ohne Kalibrierung, gerechnet wie die Karte im Panel.
* Nur abgeschlossene Tage gehen hinaus — ein laufender Tag würde als gemeldet
  vermerkt und käme nie vollständig nach.
"""

from datetime import timedelta
from unittest.mock import patch

import pytest

from custom_components.eeg_energy_optimizer import prognosevergleich as pv
from custom_components.eeg_energy_optimizer.prognose_telemetrie import (
    PAKET_TAGE,
    PrognoseVersand,
    tag_payload,
)

from .test_prognosevergleich import HEUTE, JETZT, _lokal, _reihe, _tag_mit, _vergleich  # noqa: F401


@pytest.fixture(autouse=True)
def _zeit():
    with patch.object(pv, "_utcnow", return_value=JETZT), patch.object(pv, "_lokal", _lokal):
        yield


class _Reporter:
    is_enabled = True
    is_configured = True

    def __init__(self):
        self.pakete: list[list[dict]] = []

    async def send_forecast_days(self, tage):
        self.pakete.append(tage)


def _versand() -> PrognoseVersand:
    v = PrognoseVersand(None, "test")
    v._store = None
    return v


def test_payload_tagessummen_und_reihen():
    tag = _tag_mit(fremd=2.0, eigen=1.5, gemessen=1.8, datum="2026-09-25")
    tag["eigen_roh"] = _reihe(tag["slots"], 12, 36, 1.2)
    p = tag_payload("2026-09-25", tag)

    # 24 Slots à 30 min
    assert p["actual_kwh"] == pytest.approx(21.6)
    assert p["foreign_kwh"] == pytest.approx(24.0)
    assert p["own_kwh"] == pytest.approx(18.0)
    assert p["own_raw_kwh"] == pytest.approx(14.4)
    assert p["foreign_mae_kw"] == pytest.approx(0.2)
    assert p["own_mae_kw"] == pytest.approx(0.3)
    assert p["own_raw_mae_kw"] == pytest.approx(0.6)
    assert p["foreign_source"] == "solcast_solar"
    assert p["complete"] is True and p["late"] is False
    assert p["slot_start"] == tag["slots"][0] and p["slot_minutes"] == 30
    reihen = p["series"]
    assert all(len(reihen[k]) == len(tag["slots"]) for k in ("actual", "foreign", "own", "own_raw"))
    assert reihen["own"][12] == 1.5 and reihen["own"][0] is None


def test_payload_ohne_fremdquelle():
    tag = _tag_mit(fremd=2.0, eigen=1.5, gemessen=1.8, datum="2026-09-25")
    tag["fremd"] = None
    tag["fremd_name"] = None
    p = tag_payload("2026-09-25", tag)

    assert p["foreign_kwh"] is None and p["foreign_source"] is None
    assert p["series"]["foreign"] is None
    assert p["own_raw_kwh"] is None and p["series"]["own_raw"] is None


def test_nur_abgeschlossene_tage():
    v = _vergleich()
    gestern = "2026-09-26"
    vorgestern = "2026-09-25"
    v._tage[HEUTE] = _tag_mit(2.0, 1.5, 1.8, HEUTE)
    v._tage[gestern] = _tag_mit(2.0, 1.5, 1.8, gestern) | {"vollstaendig": False}
    v._tage[vorgestern] = _tag_mit(2.0, 1.5, 1.8, vorgestern) | {"vollstaendig": False}
    v._tage["2026-09-24"] = _tag_mit(2.0, 1.5, 1.8, "2026-09-24")

    # Heute läuft, gestern fehlt noch Messung; vorgestern ist alt genug.
    assert sorted(v.abgeschlossene_tage()) == ["2026-09-24", vorgestern]


async def test_versand_in_paketen_und_nur_einmal():
    v = _vergleich()
    from datetime import date

    erster = date(2026, 5, 1)
    for i in range(120):
        d = (erster + timedelta(days=i)).isoformat()
        v._tage[d] = _tag_mit(2.0, 1.5, 1.8, d)
    reporter = _Reporter()
    versand = _versand()

    assert await versand.async_melden(reporter, "inst-1", v) == 120
    assert [len(p) for p in reporter.pakete] == [PAKET_TAGE, PAKET_TAGE, 20]
    assert await versand.async_melden(reporter, "inst-1", v) == 0
    # Neue Kennung (Daten gelöscht) → alles noch einmal.
    assert await versand.async_melden(reporter, "inst-2", v) == 120


async def test_ohne_vergleich_nichts():
    assert await _versand().async_melden(_Reporter(), "inst-1", None) == 0
