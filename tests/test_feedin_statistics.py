"""Tests für die Einspeise-Statistik (statistics.py).

Zwei Dinge stehen im Mittelpunkt: dass gezählt wird, **was die Steuerung
tatsächlich tut** (Entladung im Modus Ein, nicht ein Zeitfenster), und dass
das Speicherformat mit dem von vor 1.5.1 kompatibel bleibt — daran hängt die
Langzeithistorie des Sensors.

Die Zeitquellen des Moduls werden gepatcht: in der Testumgebung ist
``homeassistant.util.dt`` ein Mock, ohne Patch käme als „heute" kein Datum
zurück, sondern ein Mock-Objekt.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.eeg_energy_optimizer.const import MODE_EIN, MODE_TEST
from custom_components.eeg_energy_optimizer.statistics import (
    _MAX_ELAPSED_SECONDS,
    STATS_KEY_ENTLADUNG,
    UMGESTELLT_AM,
    ZAEHLWEISE,
    FeedinStatistics,
)

TZ = timezone(timedelta(hours=2))
MOD = "custom_components.eeg_energy_optimizer.statistics"


def _hass(grid_kw: float = 3.0, unit: str = "kW"):
    hass = MagicMock()
    state = MagicMock()
    state.state = str(grid_kw)
    state.attributes = {"unit_of_measurement": unit}
    hass.states.get = MagicMock(return_value=state)
    return hass


def _config():
    return {
        "grid_power_sensor": "sensor.netz",
        "inverter_type": "huawei_sun2000",
    }


def _stats(hass=None):
    return FeedinStatistics(hass or _hass(), "entry-1", _config())


@contextlib.contextmanager
def _zeit(jetzt: datetime):
    """Modul-Zeitquellen festnageln: ``_now`` und ``_as_local``."""
    with patch(f"{MOD}._now", return_value=jetzt), patch(
        f"{MOD}._as_local", side_effect=lambda z: z.astimezone(TZ)
    ):
        yield


def _utc(tag: int, stunde: int, minute: int = 0, sekunde: int = 0) -> datetime:
    """UTC-Zeitpunkt; Sekunden dürfen überlaufen (30 * i in Schleifen)."""
    basis = datetime(2026, 8, tag, stunde, minute, tzinfo=timezone.utc)
    return basis + timedelta(seconds=sekunde)


def _status(kind: str | None):
    return {"active_kind": kind, "supported": True}


# ---------------------------------------------------------------------------
# Was überhaupt gezählt wird
# ---------------------------------------------------------------------------
class TestWasGezaehltWird:
    @pytest.mark.asyncio
    async def test_entladung_im_modus_ein_zaehlt(self):
        stats = _stats(_hass(grid_kw=3.0))
        with _zeit(_utc(26, 20)):
            # Erster Takt stellt nur die Uhr.
            await stats.async_update(_status("discharge"), MODE_EIN, _utc(26, 20, 0))
            await stats.async_update(_status("discharge"), MODE_EIN, _utc(26, 20, 1))
            # 3 kW über 60 s = 0,05 kWh
            assert stats.get_today_kwh(STATS_KEY_ENTLADUNG) == pytest.approx(0.05)

    @pytest.mark.asyncio
    async def test_im_anzeige_modus_wird_nicht_gezaehlt(self):
        """Im Modus Test schreibt der Executor nichts — es ist nicht sein Werk."""
        stats = _stats()
        with _zeit(_utc(26, 20)):
            await stats.async_update(_status("discharge"), MODE_TEST, _utc(26, 20, 0))
            await stats.async_update(_status("discharge"), MODE_TEST, _utc(26, 20, 1))
            assert stats.get_today_kwh(STATS_KEY_ENTLADUNG) == 0.0

    @pytest.mark.asyncio
    async def test_ladebegrenzung_ist_keine_entladung(self):
        stats = _stats()
        with _zeit(_utc(26, 12)):
            await stats.async_update(_status("charge_limit"), MODE_EIN, _utc(26, 12, 0))
            await stats.async_update(_status("charge_limit"), MODE_EIN, _utc(26, 12, 1))
            assert stats.get_today_kwh(STATS_KEY_ENTLADUNG) == 0.0

    @pytest.mark.asyncio
    async def test_ohne_status_wird_nicht_gezaehlt(self):
        stats = _stats()
        with _zeit(_utc(26, 20)):
            await stats.async_update(None, MODE_EIN, _utc(26, 20, 0))
            await stats.async_update(None, MODE_EIN, _utc(26, 20, 1))
            assert stats.get_today_kwh(STATS_KEY_ENTLADUNG) == 0.0

    @pytest.mark.asyncio
    async def test_netzbezug_zaehlt_nicht(self):
        """Während einer Entladung kann trotzdem Bezug anliegen (hohe Hauslast)."""
        stats = _stats(_hass(grid_kw=-2.0))
        with _zeit(_utc(26, 20)):
            await stats.async_update(_status("discharge"), MODE_EIN, _utc(26, 20, 0))
            await stats.async_update(_status("discharge"), MODE_EIN, _utc(26, 20, 1))
            assert stats.get_today_kwh(STATS_KEY_ENTLADUNG) == 0.0

    @pytest.mark.asyncio
    async def test_unlesbarer_netzsensor_zaehlt_null(self):
        hass = MagicMock()
        hass.states.get = MagicMock(return_value=None)
        stats = _stats(hass)
        with _zeit(_utc(26, 20)):
            await stats.async_update(_status("discharge"), MODE_EIN, _utc(26, 20, 0))
            await stats.async_update(_status("discharge"), MODE_EIN, _utc(26, 20, 1))
            assert stats.get_today_kwh(STATS_KEY_ENTLADUNG) == 0.0


# ---------------------------------------------------------------------------
# Aufsummieren
# ---------------------------------------------------------------------------
class TestAufsummieren:
    @pytest.mark.asyncio
    async def test_rechteck_ueber_mehrere_takte(self):
        stats = _stats(_hass(grid_kw=6.0))
        with _zeit(_utc(26, 21)):
            await stats.async_update(_status("discharge"), MODE_EIN, _utc(26, 21, 0))
            for i in range(1, 5):  # 4 × 30 s = 2 min
                await stats.async_update(
                    _status("discharge"), MODE_EIN, _utc(26, 21, 0, 30 * i)
                )
            # 6 kW × 2 min = 0,2 kWh
            assert stats.get_today_kwh(STATS_KEY_ENTLADUNG) == pytest.approx(0.2)

    @pytest.mark.asyncio
    async def test_zu_grosse_luecke_wird_nicht_gerechnet(self):
        """Nach einem Neustart wäre die Rechnung sonst ein Sprung."""
        stats = _stats(_hass(grid_kw=6.0))
        with _zeit(_utc(26, 21)):
            await stats.async_update(_status("discharge"), MODE_EIN, _utc(26, 21, 0))
            spaeter = _utc(26, 21, 0) + timedelta(seconds=_MAX_ELAPSED_SECONDS + 1)
            await stats.async_update(_status("discharge"), MODE_EIN, spaeter)
            assert stats.get_today_kwh(STATS_KEY_ENTLADUNG) == 0.0

    @pytest.mark.asyncio
    async def test_watt_sensor_wird_umgerechnet(self):
        stats = _stats(_hass(grid_kw=3000, unit="W"))
        with _zeit(_utc(26, 20)):
            await stats.async_update(_status("discharge"), MODE_EIN, _utc(26, 20, 0))
            await stats.async_update(_status("discharge"), MODE_EIN, _utc(26, 20, 1))
            assert stats.get_today_kwh(STATS_KEY_ENTLADUNG) == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# Sitzungen
# ---------------------------------------------------------------------------
class TestSitzungen:
    @pytest.mark.asyncio
    async def test_sitzung_wird_beim_ende_eingebucht(self):
        stats = _stats(_hass(grid_kw=6.0))
        with _zeit(_utc(26, 21)):
            await stats.async_update(_status("discharge"), MODE_EIN, _utc(26, 21, 0))
            for i in range(1, 11):  # 5 Minuten
                await stats.async_update(
                    _status("discharge"), MODE_EIN, _utc(26, 21, 0, 30 * i)
                )
            await stats.async_update(_status(None), MODE_EIN, _utc(26, 21, 5, 30))

            tage = stats.get_daily_stats()
            tag = tage["2026-08-26"][STATS_KEY_ENTLADUNG]
            assert tag["count"] == 1
            assert tag["total_kwh"] == pytest.approx(0.5, abs=0.01)
            assert tag["total_duration_min"] == 5
            assert len(tag["sessions"]) == 1
            assert tag["sessions"][0]["start"] == "23:00"  # UTC+2

    @pytest.mark.asyncio
    async def test_kurzer_ausschlag_ohne_ertrag_wird_verworfen(self):
        stats = _stats(_hass(grid_kw=0.0))
        with _zeit(_utc(26, 21)):
            await stats.async_update(_status("discharge"), MODE_EIN, _utc(26, 21, 0))
            await stats.async_update(_status("discharge"), MODE_EIN, _utc(26, 21, 0, 30))
            await stats.async_update(_status(None), MODE_EIN, _utc(26, 21, 1))
            assert stats.get_daily_stats() == {}

    @pytest.mark.asyncio
    async def test_sitzung_ueber_mitternacht_bleibt_am_starttag(self):
        """Sonst wäre eine Nachtentladung auf zwei halbe Werte verteilt."""
        stats = _stats(_hass(grid_kw=6.0))
        # Start 23:30 lokal (= 21:30 UTC), Ende 00:30 lokal am Folgetag.
        with _zeit(_utc(26, 21, 30)):
            await stats.async_update(_status("discharge"), MODE_EIN, _utc(26, 21, 30))
            await stats.async_update(_status("discharge"), MODE_EIN, _utc(26, 21, 31))
        with _zeit(_utc(26, 22, 30)):
            await stats.async_update(_status(None), MODE_EIN, _utc(26, 22, 30))

        tage = stats.get_daily_stats()
        # Der Starttag traegt die ganze Sitzung, der Folgetag bleibt leer.
        assert list(tage) == ["2026-08-26"]
        # 59 und nicht 60 Minuten: der erste Takt stellt nur die Uhr, die
        # Sitzung beginnt erst im zweiten (23:31 lokal).
        assert tage["2026-08-26"][STATS_KEY_ENTLADUNG]["total_duration_min"] == 59

    @pytest.mark.asyncio
    async def test_laufende_sitzung_zaehlt_in_der_tagessumme_mit(self):
        stats = _stats(_hass(grid_kw=6.0))
        with _zeit(_utc(26, 21)):
            await stats.async_update(_status("discharge"), MODE_EIN, _utc(26, 21, 0))
            await stats.async_update(_status("discharge"), MODE_EIN, _utc(26, 21, 1))
            # Noch nicht geschlossen, aber schon sichtbar.
            assert stats.get_today_kwh(STATS_KEY_ENTLADUNG) == pytest.approx(0.1)
            assert stats.get_daily_stats() == {}


# ---------------------------------------------------------------------------
# Speicher — Kompatibilität mit dem Format vor 1.5.1
# ---------------------------------------------------------------------------
class TestSpeicher:
    @pytest.mark.asyncio
    async def test_alte_datei_wird_gelesen_und_morgen_bleibt_stehen(self):
        stats = _stats()
        stats._store = MagicMock()
        stats._store.async_load = AsyncMock(return_value={
            "version": 1,
            "current_session": None,
            "daily": {
                "2026-08-01": {
                    "morning": {"sessions": [], "total_kwh": 4.2,
                                "total_duration_min": 90, "count": 1},
                    "evening": {"sessions": [], "total_kwh": 7.5,
                                "total_duration_min": 300, "count": 2},
                },
            },
        })
        with _zeit(_utc(26, 12)):
            await stats.async_load()
            gesamt = stats.get_summary(days=None)
        # Beide Reihen bleiben lesbar — die Morgen-Werte von damals sind Daten,
        # keine Altlast.
        assert gesamt["morning"]["kwh"] == pytest.approx(4.2)
        assert gesamt["evening"]["kwh"] == pytest.approx(7.5)
        assert gesamt["evening"]["count"] == 2

    @pytest.mark.asyncio
    async def test_geschrieben_wird_das_alte_format(self):
        stats = _stats(_hass(grid_kw=6.0))
        stats._store = MagicMock()
        stats._store.async_save = AsyncMock()
        with _zeit(_utc(26, 21)):
            await stats.async_update(_status("discharge"), MODE_EIN, _utc(26, 21, 0))
            await stats.async_update(_status("discharge"), MODE_EIN, _utc(26, 21, 1))
            await stats.async_flush()

        gespeichert = stats._store.async_save.await_args[0][0]
        assert gespeichert["version"] == 1
        assert "current_session" in gespeichert
        assert "daily" in gespeichert
        assert gespeichert["current_session"]["state"] == STATS_KEY_ENTLADUNG

    @pytest.mark.asyncio
    async def test_ohne_aenderung_wird_nicht_geschrieben(self):
        stats = _stats()
        stats._store = MagicMock()
        stats._store.async_save = AsyncMock()
        await stats.async_flush()
        stats._store.async_save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_veraltete_offene_sitzung_wird_verworfen(self):
        """Sonst wird sie Monate später mit absurder Dauer geschlossen."""
        stats = _stats()
        stats._store = MagicMock()
        stats._store.async_load = AsyncMock(return_value={
            "version": 1,
            "daily": {},
            "current_session": {
                "state": "evening", "date": "2026-05-01",
                "start_utc": "2026-05-01T20:00:00+00:00",
                "start_local": "22:00", "accumulated_kwh": 1.0,
            },
        })
        with _zeit(_utc(26, 12)):
            await stats.async_load()
        assert stats._current_session is None

    @pytest.mark.asyncio
    async def test_sitzung_von_gestern_bleibt(self):
        """Eine Nachtentladung über Mitternacht ist beim Neustart noch offen."""
        stats = _stats()
        stats._store = MagicMock()
        stats._store.async_load = AsyncMock(return_value={
            "version": 1,
            "daily": {},
            "current_session": {
                "state": "evening", "date": "2026-08-25",
                "start_utc": "2026-08-25T20:00:00+00:00",
                "start_local": "22:00", "accumulated_kwh": 1.0,
            },
        })
        with _zeit(_utc(26, 1)):
            await stats.async_load()
        assert stats._current_session is not None

    @pytest.mark.asyncio
    async def test_alte_tage_verlieren_die_abschnittsliste_nicht_die_summe(self):
        stats = _stats()
        stats._store = MagicMock()
        alt = "2020-01-01"
        stats._store.async_load = AsyncMock(return_value={
            "version": 1, "current_session": None,
            "daily": {alt: {"evening": {
                "sessions": [{"start": "22:00", "end": "23:00", "kwh": 2.0,
                              "duration_min": 60}],
                "total_kwh": 2.0, "total_duration_min": 60, "count": 1,
            }}},
        })
        with _zeit(_utc(26, 12)):
            await stats.async_load()
        eintrag = stats.get_daily_stats()[alt]["evening"]
        assert "sessions" not in eintrag
        assert eintrag["total_kwh"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Abfragen
# ---------------------------------------------------------------------------
class TestAbfragen:
    def test_summary_grenzt_den_zeitraum_ab(self):
        stats = _stats()
        stats._daily = {
            "2026-08-26": {"evening": {"total_kwh": 1.0, "count": 1,
                                       "total_duration_min": 30}},
            "2026-08-20": {"evening": {"total_kwh": 2.0, "count": 1,
                                       "total_duration_min": 30}},
            "2026-06-01": {"evening": {"total_kwh": 4.0, "count": 1,
                                       "total_duration_min": 30}},
        }
        with _zeit(datetime(2026, 8, 26, 12, tzinfo=TZ)):
            assert stats.get_summary(days=1)["evening"]["kwh"] == pytest.approx(1.0)
            assert stats.get_summary(days=7)["evening"]["kwh"] == pytest.approx(3.0)
            assert stats.get_summary(days=None)["evening"]["kwh"] == pytest.approx(7.0)

    def test_daily_stats_mit_grenzen(self):
        stats = _stats()
        stats._daily = {
            "2026-08-24": {}, "2026-08-25": {}, "2026-08-26": {},
        }
        assert list(stats.get_daily_stats(start_date="2026-08-25")) == [
            "2026-08-25", "2026-08-26",
        ]
        assert list(stats.get_daily_stats(end_date="2026-08-25")) == [
            "2026-08-24", "2026-08-25",
        ]

    def test_kaputte_tageseintraege_kippen_die_abfrage_nicht(self):
        stats = _stats()
        stats._daily = {"2026-08-26": "kein dict"}
        with _zeit(datetime(2026, 8, 26, 12, tzinfo=TZ)):
            assert stats.get_summary(days=None)["evening"]["kwh"] == 0.0
            assert stats.get_today_kwh(STATS_KEY_ENTLADUNG) == 0.0


# ---------------------------------------------------------------------------
# Offenlegung des Bedeutungswechsels
# ---------------------------------------------------------------------------
def test_zaehlweise_und_umstellungsdatum_sind_gesetzt():
    """Ändert sich die Quelle erneut, muss sich ZAEHLWEISE mit ändern.

    Der Wert landet als Sensor-Attribut in der Historie — daran erkennt eine
    Auswertung später, welche Größe eine Zeile trägt.
    """
    assert ZAEHLWEISE == "fahrplan_entladung"
    assert UMGESTELLT_AM == "2026-08-27"
