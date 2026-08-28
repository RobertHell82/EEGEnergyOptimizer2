"""Tests für die Tagesbilanz (tagesbilanz.py).

Geprüft wird das Rechnen, nicht die Verdrahtung: die Integration über
anteilige Überlappung, die Abdeckungsschwellen (eine halbe Messung darf keine
volle Prognose gegenübergestellt bekommen) und die Regel, dass der
48-Stunden-Vorlauf ausfällt, wenn die Prognose nicht so weit reichte.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.eeg_energy_optimizer.tagesbilanz import (
    IST_AUFLOESUNG_MIN,
    MIN_ABDECKUNG,
    async_baue_tagesbilanzen,
    baue_outcome,
    ist_kennzahlen,
    plan_kennzahlen,
    summe_im_fenster,
)

TZ = timezone(timedelta(hours=2))
VON = datetime(2026, 8, 26, 0, 0, tzinfo=TZ)
BIS = datetime(2026, 8, 27, 0, 0, tzinfo=TZ)


def _reihe(start: datetime, anzahl: int, wert, res_min: int = IST_AUFLOESUNG_MIN):
    """Messreihe wie async_ist_verlauf sie liefert: [[iso, wert], ...]."""
    punkte = []
    for i in range(anzahl):
        stempel = (start + timedelta(minutes=res_min * i)).isoformat()
        punkte.append([stempel, wert(i) if callable(wert) else wert])
    return punkte


def _verlauf(**reihen):
    return {"reihen": reihen}


def _tagesverlauf(pv=2.0, hausverbrauch=1.0, netz=3.0, ladestand=None):
    """Voller Tag in 5-Minuten-Schritten (288 Punkte)."""
    reihen = {
        "pv_leistung": _reihe(VON, 288, pv),
        "hausverbrauch": _reihe(VON, 288, hausverbrauch),
        "netzleistung": _reihe(VON, 288, netz),
    }
    if ladestand is not None:
        reihen["ladestand"] = _reihe(VON, 288, ladestand)
    return _verlauf(**reihen)


def _plan(slots_anzahl=96, res_min=15, pv=4.0, consumption=1.0, start=VON):
    slots = []
    for i in range(slots_anzahl):
        slots.append({
            "t": (start + timedelta(minutes=res_min * i)).isoformat(),
            "PV": pv(i) if callable(pv) else pv,
            "consumption": consumption(i) if callable(consumption) else consumption,
        })
    return {
        "gespeichert": (start - timedelta(minutes=15)).isoformat(),
        "plan": {"slots": slots, "time_res_min": res_min},
    }


# ---------------------------------------------------------------------------
# summe_im_fenster — Integration über Intervall-Mittelwerte
# ---------------------------------------------------------------------------
class TestSummeImFenster:
    def test_rechtecksumme_ist_exakt(self):
        # 12 Punkte à 5 min mit 6 kW = eine Stunde = 6 kWh
        punkte = _reihe(VON, 12, 6.0)
        kwh, stunden = summe_im_fenster(punkte, 5, VON, BIS)
        assert kwh == pytest.approx(6.0)
        assert stunden == pytest.approx(1.0)

    def test_punkt_vor_dem_fenster_zaehlt_nur_anteilig(self):
        # Punkt beginnt 2 min vor Mitternacht, deckt also nur 3 der 5 min ab.
        punkte = [[(VON - timedelta(minutes=2)).isoformat(), 6.0]]
        kwh, stunden = summe_im_fenster(punkte, 5, VON, BIS)
        assert stunden == pytest.approx(3 / 60)
        assert kwh == pytest.approx(6.0 * 3 / 60)

    def test_punkt_ganz_vor_dem_fenster_zaehlt_nicht(self):
        punkte = [[(VON - timedelta(minutes=5)).isoformat(), 6.0]]
        assert summe_im_fenster(punkte, 5, VON, BIS) == (0.0, 0.0)

    def test_punkt_ueber_das_fensterende_wird_gekappt(self):
        punkte = [[(BIS - timedelta(minutes=5)).isoformat(), 6.0]]
        kwh, stunden = summe_im_fenster(punkte, 15, VON, BIS)
        # 15-min-Intervall, aber nur 5 min liegen noch im Tag.
        assert stunden == pytest.approx(5 / 60)
        assert kwh == pytest.approx(6.0 * 5 / 60)

    def test_nur_positiv_ignoriert_bezug(self):
        punkte = _reihe(VON, 4, lambda i: 6.0 if i % 2 == 0 else -6.0)
        kwh, stunden = summe_im_fenster(punkte, 5, VON, BIS, nur_positiv=True)
        assert kwh == pytest.approx(2 * 6.0 * 5 / 60)
        # Die Stunden zählen die vorhandenen Messwerte, nicht nur die positiven.
        assert stunden == pytest.approx(4 * 5 / 60)

    def test_unlesbare_punkte_werden_uebersprungen(self):
        punkte = [
            [VON.isoformat(), 6.0],
            [VON.isoformat(), None],
            ["kein datum", 6.0],
            [(VON + timedelta(minutes=5)).isoformat(), "keine zahl"],
        ]
        kwh, stunden = summe_im_fenster(punkte, 5, VON, BIS)
        assert kwh == pytest.approx(0.5)
        assert stunden == pytest.approx(5 / 60)

    def test_naiver_zeitstempel_bekommt_die_fensterzone(self):
        punkte = [["2026-08-26T00:00:00", 6.0]]
        kwh, _ = summe_im_fenster(punkte, 5, VON, BIS)
        assert kwh == pytest.approx(0.5)

    def test_dict_form_der_plan_slots(self):
        punkte = [{"t": VON.isoformat(), "wert": 4.0}]
        kwh, stunden = summe_im_fenster(punkte, 15, VON, BIS)
        assert kwh == pytest.approx(1.0)
        assert stunden == pytest.approx(0.25)

    def test_leere_eingabe(self):
        assert summe_im_fenster([], 5, VON, BIS) == (0.0, 0.0)
        assert summe_im_fenster(None, 5, VON, BIS) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# ist_kennzahlen
# ---------------------------------------------------------------------------
class TestIstKennzahlen:
    def test_voller_tag(self):
        ist = ist_kennzahlen(
            _tagesverlauf(pv=2.0, hausverbrauch=1.0, netz=3.0, ladestand=lambda i: 50 + i % 10),
            VON, BIS,
        )
        assert ist is not None
        assert ist["pv_kwh"] == pytest.approx(48.0)          # 2 kW × 24 h
        assert ist["consumption_kwh"] == pytest.approx(24.0)  # 1 kW × 24 h
        assert ist["grid_export_kwh"] == pytest.approx(72.0)  # 3 kW × 24 h
        assert ist["peak_power_kw"] == pytest.approx(3.0)
        assert ist["minuten"] == 1440
        assert ist["abdeckung"] == pytest.approx(1.0)

    def test_netzbezug_zaehlt_nicht_als_einspeisung(self):
        # Halber Tag Einspeisung, halber Tag Bezug.
        ist = ist_kennzahlen(
            _verlauf(
                pv_leistung=_reihe(VON, 288, 0.0),
                hausverbrauch=_reihe(VON, 288, 1.0),
                netzleistung=_reihe(VON, 288, lambda i: 4.0 if i < 144 else -4.0),
            ),
            VON, BIS,
        )
        assert ist["grid_export_kwh"] == pytest.approx(48.0)  # nur die 12 h Export
        assert ist["peak_power_kw"] == pytest.approx(4.0)

    def test_reine_bezugs_nacht_hat_keine_negative_spitze(self):
        ist = ist_kennzahlen(
            _verlauf(
                hausverbrauch=_reihe(VON, 288, 1.0),
                netzleistung=_reihe(VON, 288, -2.0),
            ),
            VON, BIS,
        )
        assert ist["grid_export_kwh"] == pytest.approx(0.0)
        # Die Spitze ist eine Einspeisespitze — ohne Einspeisung ist sie 0.
        assert ist["peak_power_kw"] == pytest.approx(0.0)

    def test_ladestand_erster_und_letzter_wert(self):
        ist = ist_kennzahlen(
            _tagesverlauf(ladestand=lambda i: 20 + i / 10), VON, BIS
        )
        assert ist["soc_start_pct"] == 20
        assert ist["soc_end_pct"] == int(round(20 + 287 / 10))

    def test_ladestand_wird_zeitlich_sortiert(self):
        """Der Recorder liefert sortiert, aber darauf verlassen wir uns nicht."""
        reihen = {
            "hausverbrauch": _reihe(VON, 288, 1.0),
            "ladestand": [
                [(VON + timedelta(hours=12)).isoformat(), 80],
                [VON.isoformat(), 30],
                [(VON + timedelta(hours=23)).isoformat(), 55],
            ],
        }
        ist = ist_kennzahlen(_verlauf(**reihen), VON, BIS)
        assert ist["soc_start_pct"] == 30
        assert ist["soc_end_pct"] == 55

    def test_ohne_ladestand_bleiben_die_felder_leer(self):
        ist = ist_kennzahlen(_tagesverlauf(), VON, BIS)
        assert ist["soc_start_pct"] is None
        assert ist["soc_end_pct"] is None

    def test_zu_grosse_luecke_verwirft_die_bilanz(self):
        # Nur 12 Stunden Hausverbrauch → 50 % Abdeckung.
        ist = ist_kennzahlen(
            _verlauf(hausverbrauch=_reihe(VON, 144, 1.0)), VON, BIS
        )
        assert ist is None

    def test_kleine_luecke_wird_toleriert_und_gemeldet(self):
        # 280 von 288 Punkten ≈ 97 % — über der Schwelle.
        anzahl = 280
        ist = ist_kennzahlen(
            _verlauf(
                hausverbrauch=_reihe(VON, anzahl, 1.0),
                pv_leistung=_reihe(VON, anzahl, 2.0),
            ),
            VON, BIS,
        )
        assert ist is not None
        assert ist["abdeckung"] == pytest.approx(anzahl * 5 / 60 / 24, abs=1e-4)
        assert ist["abdeckung"] > MIN_ABDECKUNG
        # Die Lücke ist in der Zeile sichtbar.
        assert ist["minuten"] == anzahl * 5

    def test_ohne_hausverbrauch_keine_bilanz(self):
        """Der Hausverbrauch ist die Bezugsgröße der Abdeckung."""
        assert ist_kennzahlen(_verlauf(pv_leistung=_reihe(VON, 288, 2.0)), VON, BIS) is None

    def test_leeres_fenster(self):
        assert ist_kennzahlen(_tagesverlauf(), VON, VON) is None


# ---------------------------------------------------------------------------
# plan_kennzahlen
# ---------------------------------------------------------------------------
class TestPlanKennzahlen:
    def test_kilowatt_werden_zu_kilowattstunden(self):
        plan = plan_kennzahlen(_plan(pv=4.0, consumption=1.0), VON, BIS)
        assert plan is not None
        assert plan["pv_kwh"] == pytest.approx(96.0)          # 4 kW × 24 h
        assert plan["consumption_kwh"] == pytest.approx(24.0)
        assert plan["abdeckung"] == pytest.approx(1.0)

    def test_halbstundenraster(self):
        plan = plan_kennzahlen(_plan(slots_anzahl=48, res_min=30, pv=4.0), VON, BIS)
        assert plan["pv_kwh"] == pytest.approx(96.0)

    def test_versetztes_raster_wird_anteilig_gerechnet(self):
        """Ein Plan, dessen Slots nicht auf Mitternacht fallen.

        Start 23:45 mit 30-Minuten-Raster: der erste Slot reicht bis 00:15,
        also zählen 15 seiner 30 Minuten zum neuen Tag.
        """
        start = VON - timedelta(minutes=15)
        plan = plan_kennzahlen(
            _plan(slots_anzahl=49, res_min=30, pv=4.0, start=start), VON, BIS
        )
        assert plan is not None
        # 49 Slots ab 23:45 reichen bis 00:15 des Folgetags → Tag voll gedeckt.
        assert plan["abdeckung"] == pytest.approx(1.0)
        assert plan["pv_kwh"] == pytest.approx(96.0)

    def test_unvollstaendiger_plan_liefert_nichts(self):
        """Der Regelfall für den 48-Stunden-Vorlauf bei kurzer Prognose."""
        assert plan_kennzahlen(_plan(slots_anzahl=48, res_min=15), VON, BIS) is None

    def test_luecke_mitten_im_plan_liefert_nichts(self):
        eintrag = _plan(slots_anzahl=96)
        del eintrag["plan"]["slots"][40:60]
        assert plan_kennzahlen(eintrag, VON, BIS) is None

    def test_ohne_slots_liefert_nichts(self):
        assert plan_kennzahlen({"plan": {"slots": []}}, VON, BIS) is None
        assert plan_kennzahlen({}, VON, BIS) is None
        assert plan_kennzahlen({"plan": {"slots": "kaputt"}}, VON, BIS) is None

    def test_fehlendes_raster_nimmt_fuenfzehn_minuten_an(self):
        eintrag = _plan(slots_anzahl=96)
        del eintrag["plan"]["time_res_min"]
        plan = plan_kennzahlen(eintrag, VON, BIS)
        assert plan is not None
        assert plan["pv_kwh"] == pytest.approx(96.0)


# ---------------------------------------------------------------------------
# baue_outcome — Schema nach types.ts
# ---------------------------------------------------------------------------
class TestBaueOutcome:
    def test_schema_und_felder(self):
        ist = ist_kennzahlen(_tagesverlauf(ladestand=60), VON, BIS)
        plan = plan_kennzahlen(_plan(), VON, BIS)
        payload = baue_outcome("fahrplan_tag", VON, BIS, ist, plan)

        assert set(payload) == {
            "event_type", "started_at", "ended_at", "duration_minutes",
            "grid_export_kwh", "peak_power_kw", "soc_start_pct", "soc_end_pct",
            "predicted_pv_kwh", "actual_pv_kwh",
            "predicted_consumption_kwh", "actual_consumption_kwh",
            "terminated_by",
        }
        assert payload["event_type"] == "fahrplan_tag"
        assert payload["started_at"] == VON.isoformat()
        assert payload["ended_at"] == BIS.isoformat()
        assert payload["predicted_pv_kwh"] == pytest.approx(96.0)
        assert payload["actual_pv_kwh"] == pytest.approx(48.0)
        assert payload["predicted_consumption_kwh"] == pytest.approx(24.0)
        assert payload["actual_consumption_kwh"] == pytest.approx(24.0)
        assert payload["terminated_by"] == "tagesende"

    def test_ohne_plan_bleiben_die_prognosefelder_leer(self):
        ist = ist_kennzahlen(_tagesverlauf(), VON, BIS)
        payload = baue_outcome("fahrplan_tag", VON, BIS, ist, None)
        assert payload["predicted_pv_kwh"] is None
        assert payload["predicted_consumption_kwh"] is None
        assert payload["actual_pv_kwh"] == pytest.approx(48.0)
        # Die Herkunft steht in der Zeile — im Dashboard genau die Frage.
        assert payload["terminated_by"] == "tagesende_ohne_plan"


# ---------------------------------------------------------------------------
# async_baue_tagesbilanzen — Zusammenspiel beider Quellen
# ---------------------------------------------------------------------------
def _archiv(eintraege: dict):
    """Archiv-Ersatz: Zuordnung Vorlauf-Zielzeit → Eintrag."""
    archiv = MagicMock()

    async def lies_vor(ziel, *args, **kwargs):
        return eintraege.get(ziel)

    archiv.async_lies_vor = AsyncMock(side_effect=lies_vor)
    return archiv


@pytest.mark.asyncio
async def test_beide_vorlaeufe_wenn_beide_plaene_da_sind():
    archiv = _archiv({VON: _plan(pv=4.0), VON - timedelta(hours=24): _plan(pv=5.0)})
    with patch(
        "custom_components.eeg_energy_optimizer.schedule_archive.async_ist_verlauf",
        new=AsyncMock(return_value=_tagesverlauf()),
    ):
        bilanzen = await async_baue_tagesbilanzen(
            MagicMock(), "entry", archiv, VON, BIS
        )
    assert [b["event_type"] for b in bilanzen] == ["fahrplan_tag", "fahrplan_tag_48h"]
    # Gleiche Messung, verschiedene Prognose — das ist der ganze Zweck.
    assert bilanzen[0]["actual_pv_kwh"] == bilanzen[1]["actual_pv_kwh"]
    assert bilanzen[0]["predicted_pv_kwh"] == pytest.approx(96.0)
    assert bilanzen[1]["predicted_pv_kwh"] == pytest.approx(120.0)


@pytest.mark.asyncio
async def test_kurze_prognose_laesst_die_48h_zeile_ausfallen():
    """Forecast.Solar reicht nur bis zum Ende des Folgetags."""
    archiv = _archiv({VON: _plan()})  # nur der Vorabend-Plan
    with patch(
        "custom_components.eeg_energy_optimizer.schedule_archive.async_ist_verlauf",
        new=AsyncMock(return_value=_tagesverlauf()),
    ):
        bilanzen = await async_baue_tagesbilanzen(
            MagicMock(), "entry", archiv, VON, BIS
        )
    assert [b["event_type"] for b in bilanzen] == ["fahrplan_tag"]


@pytest.mark.asyncio
async def test_ohne_jeden_plan_bleibt_die_gemessene_zeile():
    with patch(
        "custom_components.eeg_energy_optimizer.schedule_archive.async_ist_verlauf",
        new=AsyncMock(return_value=_tagesverlauf()),
    ):
        bilanzen = await async_baue_tagesbilanzen(
            MagicMock(), "entry", _archiv({}), VON, BIS
        )
    assert len(bilanzen) == 1
    assert bilanzen[0]["predicted_pv_kwh"] is None
    assert bilanzen[0]["actual_pv_kwh"] == pytest.approx(48.0)


@pytest.mark.asyncio
async def test_ohne_messung_keine_zeile():
    with patch(
        "custom_components.eeg_energy_optimizer.schedule_archive.async_ist_verlauf",
        new=AsyncMock(return_value=_verlauf()),
    ):
        bilanzen = await async_baue_tagesbilanzen(
            MagicMock(), "entry", _archiv({VON: _plan()}), VON, BIS
        )
    assert bilanzen == []


@pytest.mark.asyncio
async def test_recorder_fehler_kippt_nichts():
    with patch(
        "custom_components.eeg_energy_optimizer.schedule_archive.async_ist_verlauf",
        new=AsyncMock(return_value={"fehler": "recorder nicht verfügbar", "reihen": {}}),
    ):
        assert await async_baue_tagesbilanzen(
            MagicMock(), "entry", None, VON, BIS
        ) == []

    with patch(
        "custom_components.eeg_energy_optimizer.schedule_archive.async_ist_verlauf",
        new=AsyncMock(side_effect=RuntimeError("kaputt")),
    ):
        assert await async_baue_tagesbilanzen(
            MagicMock(), "entry", None, VON, BIS
        ) == []


@pytest.mark.asyncio
async def test_archivfehler_kostet_nur_die_prognose():
    archiv = MagicMock()
    archiv.async_lies_vor = AsyncMock(side_effect=OSError("Platte weg"))
    with patch(
        "custom_components.eeg_energy_optimizer.schedule_archive.async_ist_verlauf",
        new=AsyncMock(return_value=_tagesverlauf()),
    ):
        bilanzen = await async_baue_tagesbilanzen(
            MagicMock(), "entry", archiv, VON, BIS
        )
    assert len(bilanzen) == 1
    assert bilanzen[0]["predicted_pv_kwh"] is None
    assert bilanzen[0]["actual_pv_kwh"] == pytest.approx(48.0)


# ---------------------------------------------------------------------------
# tagesfenster — der zuletzt abgeschlossene Kalendertag
# ---------------------------------------------------------------------------
class TestTagesfenster:
    """Die Rechnung sieht harmlos aus und ist es nicht.

    Ein Sprung von 24 Stunden trifft an Zeitumstellungstagen 23:00 oder 01:00,
    ein weiter Sprung landet im Vorvortag. Beides fiele im Betrieb kaum auf —
    das Archiv hält sieben Tage, es käme immer irgendein Plan zurück, nur eben
    zum falschen Tag und mit falsch beschriftetem Vorlauf.
    """

    def test_nach_mitternacht_meint_den_vortag(self):
        from custom_components.eeg_energy_optimizer.tagesbilanz import tagesfenster

        von, bis = tagesfenster(datetime(2026, 8, 27, 0, 15, tzinfo=TZ))
        assert von == datetime(2026, 8, 26, 0, 0, tzinfo=TZ)
        assert bis == datetime(2026, 8, 27, 0, 0, tzinfo=TZ)

    def test_auch_mitten_am_tag_gilt_der_vortag(self):
        """Der Diagnoseknopf im Panel wird zu jeder Uhrzeit gedrückt."""
        from custom_components.eeg_energy_optimizer.tagesbilanz import tagesfenster

        von, bis = tagesfenster(datetime(2026, 8, 27, 14, 30, tzinfo=TZ))
        assert von == datetime(2026, 8, 26, 0, 0, tzinfo=TZ)
        assert bis == datetime(2026, 8, 27, 0, 0, tzinfo=TZ)

    def test_kurz_vor_mitternacht(self):
        from custom_components.eeg_energy_optimizer.tagesbilanz import tagesfenster

        von, bis = tagesfenster(datetime(2026, 8, 27, 23, 59, tzinfo=TZ))
        assert von == datetime(2026, 8, 26, 0, 0, tzinfo=TZ)
        assert bis == datetime(2026, 8, 27, 0, 0, tzinfo=TZ)

    def test_monatswechsel(self):
        from custom_components.eeg_energy_optimizer.tagesbilanz import tagesfenster

        von, bis = tagesfenster(datetime(2026, 9, 1, 0, 15, tzinfo=TZ))
        assert von == datetime(2026, 8, 31, 0, 0, tzinfo=TZ)
        assert bis == datetime(2026, 9, 1, 0, 0, tzinfo=TZ)


class TestTagesfensterZeitumstellung:
    """Echte Zeitzone, echte Umstellungstage."""

    @staticmethod
    def _wien():
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo("Europe/Vienna")
        except Exception:  # pragma: no cover - ohne tzdata nicht prüfbar
            pytest.skip("Zeitzonendatenbank nicht verfügbar")

    def test_ende_der_sommerzeit_ergibt_25_stunden(self):
        """25.10.2026 hat 25 Stunden — das Fenster muss sie alle umfassen."""
        from custom_components.eeg_energy_optimizer.tagesbilanz import tagesfenster

        tz = self._wien()
        von, bis = tagesfenster(datetime(2026, 10, 26, 0, 15, tzinfo=tz))
        assert von.date().isoformat() == "2026-10-25"
        assert bis.date().isoformat() == "2026-10-26"
        assert (von.hour, bis.hour) == (0, 0)
        # Ueber UTC, sonst rechnet Python die Wall Clock und die
        # Zeitumstellung verschwindet (siehe _spanne_h).
        spanne = bis.astimezone(timezone.utc) - von.astimezone(timezone.utc)
        assert spanne.total_seconds() / 3600 == pytest.approx(25.0)

    def test_beginn_der_sommerzeit_ergibt_23_stunden(self):
        """29.03.2026 hat 23 Stunden."""
        from custom_components.eeg_energy_optimizer.tagesbilanz import tagesfenster

        tz = self._wien()
        von, bis = tagesfenster(datetime(2026, 3, 30, 0, 15, tzinfo=tz))
        assert von.date().isoformat() == "2026-03-29"
        assert bis.date().isoformat() == "2026-03-30"
        assert (von.hour, bis.hour) == (0, 0)
        spanne = bis.astimezone(timezone.utc) - von.astimezone(timezone.utc)
        assert spanne.total_seconds() / 3600 == pytest.approx(23.0)

    def test_abdeckung_misst_gegen_die_echte_tageslaenge(self):
        """Ein 23-Stunden-Tag darf nicht als lückenhaft gelten.

        Würde gegen pauschale 24 Stunden gemessen, käme eine vollständige
        Messung auf 23/24 = 96 % — knapp über der Schwelle, aber die Zahl wäre
        falsch, und bei einem 25-Stunden-Tag stünde da 104 %.
        """
        from custom_components.eeg_energy_optimizer.tagesbilanz import (
            ist_kennzahlen, tagesfenster,
        )

        tz = self._wien()
        von, bis = tagesfenster(datetime(2026, 3, 30, 0, 15, tzinfo=tz))
        spanne = bis.astimezone(timezone.utc) - von.astimezone(timezone.utc)
        punkte = int(spanne.total_seconds() / 60 / IST_AUFLOESUNG_MIN)
        ist = ist_kennzahlen(
            {"reihen": {
                "hausverbrauch": _reihe(von, punkte, 1.0),
                "pv_leistung": _reihe(von, punkte, 2.0),
            }},
            von, bis,
        )
        assert ist is not None
        assert ist["abdeckung"] == pytest.approx(1.0)
        assert ist["consumption_kwh"] == pytest.approx(23.0)
