"""Tests für das Fahrplan-Archiv.

Ohne Home Assistant: das Archiv bekommt ein Objekt mit ``config.path`` und
einem ``async_add_executor_job``, das die Funktion direkt aufruft.
"""

from __future__ import annotations

import gzip
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from custom_components.eeg_energy_optimizer import schedule_archive as sa

pytestmark = pytest.mark.asyncio


class _Config:
    def __init__(self, wurzel: Path) -> None:
        self._wurzel = wurzel

    def path(self, *teile: str) -> str:
        return str(self._wurzel.joinpath(*teile))


class _Hass:
    def __init__(self, wurzel: Path) -> None:
        self.config = _Config(wurzel)

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _plan(batterie: float = 1.0, fehler: str | None = None) -> dict:
    """Ein Fahrplan, wie ScheduleRunner.to_dict() ihn liefert."""
    start = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    return {
        "available": fehler is None,
        "error": fehler,
        "last_run": start.isoformat(),
        "is_running": False,
        "time_res_min": 15,
        "start": start.isoformat(),
        "slots": [
            {
                "t": (start + timedelta(minutes=15 * i)).isoformat(),
                "battery_p": batterie,
                "grid_p": 0.5,
                "feedin_price": 0.082,
                "soc": 50.0,
            }
            for i in range(20)
        ],
    }


CONFIG = {
    "schedule_feedin_price": 0.082,
    "peakshare_community": "Pucking",
    "battery_capacity_kwh": 15,
    "modbus_host": "192.168.1.50",
    "telemetry_token": "geheim",
    "battery_soc_sensor": "sensor.soc",
}


# ---------------------------------------------------------------------------
# Wann gespeichert wird
# ---------------------------------------------------------------------------


async def test_erster_lauf_wird_immer_abgelegt(tmp_path):
    archiv = sa.ScheduleArchive(_Hass(tmp_path), "abc", "1.0-test")
    jetzt = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

    assert await archiv.async_maybe_store(_plan(), CONFIG, jetzt) == "start"
    assert len(list(tmp_path.glob(f"{sa.ARCHIV_ORDNER}/*/*.json.gz"))) == 1


async def test_zwischen_zwei_takten_wird_nichts_abgelegt(tmp_path):
    """Der Fahrplan wird minütlich gerechnet — abgelegt wird viertelstündlich."""
    archiv = sa.ScheduleArchive(_Hass(tmp_path), "abc")
    jetzt = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    await archiv.async_maybe_store(_plan(), CONFIG, jetzt)

    for minute in range(1, sa.TAKT_MINUTEN):
        grund = await archiv.async_maybe_store(
            _plan(), CONFIG, jetzt + timedelta(minutes=minute)
        )
        assert grund is None, f"Minute {minute} hätte nicht schreiben dürfen"

    grund = await archiv.async_maybe_store(
        _plan(), CONFIG, jetzt + timedelta(minutes=sa.TAKT_MINUTEN)
    )
    assert grund == "takt"
    assert len(list(tmp_path.glob(f"{sa.ARCHIV_ORDNER}/*/*.json.gz"))) == 2


async def test_deutliche_planaenderung_wird_sofort_abgelegt(tmp_path):
    """Genau die Läufe, die man hinterher sucht, dürfen nicht durchrutschen."""
    archiv = sa.ScheduleArchive(_Hass(tmp_path), "abc")
    jetzt = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    await archiv.async_maybe_store(_plan(batterie=1.0), CONFIG, jetzt)

    spaeter = jetzt + timedelta(minutes=sa.ABWEICHUNG_MINDESTABSTAND_MIN)

    # Kleine Änderung: nicht der Rede wert
    kaum = await archiv.async_maybe_store(
        _plan(batterie=1.0 + sa.ABWEICHUNG_KW / 2), CONFIG, spaeter
    )
    assert kaum is None

    deutlich = await archiv.async_maybe_store(
        _plan(batterie=1.0 + sa.ABWEICHUNG_KW * 3), CONFIG, spaeter
    )
    assert deutlich == "abweichung"


async def test_zappelnder_plan_schreibt_nicht_jede_minute(tmp_path):
    """Ein Plan, der um die Schwelle pendelt, darf das Archiv nicht fluten."""
    archiv = sa.ScheduleArchive(_Hass(tmp_path), "abc")
    jetzt = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    await archiv.async_maybe_store(_plan(batterie=1.0), CONFIG, jetzt)

    for minute in range(1, sa.ABWEICHUNG_MINDESTABSTAND_MIN):
        grund = await archiv.async_maybe_store(
            _plan(batterie=1.0 + sa.ABWEICHUNG_KW * 3 * minute),
            CONFIG,
            jetzt + timedelta(minutes=minute),
        )
        assert grund is None, f"Minute {minute} hätte warten müssen"

    grund = await archiv.async_maybe_store(
        _plan(batterie=9.0), CONFIG,
        jetzt + timedelta(minutes=sa.ABWEICHUNG_MINDESTABSTAND_MIN),
    )
    assert grund == "abweichung"


async def test_fehler_und_erholung_werden_festgehalten(tmp_path):
    archiv = sa.ScheduleArchive(_Hass(tmp_path), "abc")
    jetzt = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    await archiv.async_maybe_store(_plan(), CONFIG, jetzt)

    kaputt = await archiv.async_maybe_store(
        _plan(fehler="ValueError: keine Prognose"), CONFIG, jetzt + timedelta(minutes=1)
    )
    assert kaputt == "fehler"

    # Auch der Weg zurück ist eine Information
    heil = await archiv.async_maybe_store(_plan(), CONFIG, jetzt + timedelta(minutes=2))
    assert heil == "fehler"


# ---------------------------------------------------------------------------
# Was gespeichert wird
# ---------------------------------------------------------------------------


async def test_zugangsdaten_landen_nicht_im_archiv(tmp_path):
    """Das ZIP wird weitergegeben — Adressen und Zugänge dürfen nicht mit."""
    archiv = sa.ScheduleArchive(_Hass(tmp_path), "abc", "1.0-test")
    await archiv.async_maybe_store(
        _plan(), CONFIG, datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    )

    datei = next(iter(tmp_path.glob(f"{sa.ARCHIV_ORDNER}/*/*.json.gz")))
    with gzip.open(datei, "rb") as f:
        eintrag = json.loads(f.read().decode("utf-8"))

    einstellungen = eintrag["einstellungen"]
    assert einstellungen["schedule_feedin_price"] == 0.082
    assert einstellungen["peakshare_community"] == "Pucking"
    assert "modbus_host" not in einstellungen
    assert "telemetry_token" not in einstellungen
    # Auch der Sensorname trägt „sensor" im Wert, nicht im Schlüssel — er darf
    # bleiben, die Sperre greift nur auf Schlüssel mit Zugangsbezug.
    assert einstellungen["battery_capacity_kwh"] == 15
    assert eintrag["plan"]["slots"][0]["feedin_price"] == 0.082
    assert eintrag["version"] == "1.0-test"


@pytest.mark.filterwarnings("ignore")
async def test_einstellungen_filtern_kennt_die_sperren():
    gefiltert = sa.einstellungen_filtern({
        "schedule_battery_cost": 0.01,
        "inverter_host": "10.0.0.5",
        "inverter_type": "huawei_sun2000",
        "peakshare_api_token": "abc",
        "irgendwas_anderes": 1,
    })
    assert gefiltert == {
        "schedule_battery_cost": 0.01,
        "inverter_type": "huawei_sun2000",
    }


# ---------------------------------------------------------------------------
# Aufbewahrung und Ausgabe
# ---------------------------------------------------------------------------


async def test_alte_tage_werden_geloescht(tmp_path):
    archiv = sa.ScheduleArchive(_Hass(tmp_path), "abc")
    wurzel = tmp_path / sa.ARCHIV_ORDNER
    jetzt = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

    # Zwei Tage anlegen: einer innerhalb der Frist, einer weit darüber
    for tage in (2, sa.AUFBEWAHRUNG_TAGE + 3):
        tag = wurzel / (jetzt - timedelta(days=tage)).strftime("%Y-%m-%d")
        tag.mkdir(parents=True)
        with gzip.open(tag / "120000.json.gz", "wb") as f:
            f.write(b"{}")

    await archiv.async_maybe_store(_plan(), CONFIG, jetzt)

    tage_da = sorted(p.name for p in wurzel.iterdir() if p.is_dir())
    assert (jetzt - timedelta(days=2)).strftime("%Y-%m-%d") in tage_da
    assert (jetzt - timedelta(days=sa.AUFBEWAHRUNG_TAGE + 3)).strftime("%Y-%m-%d") not in tage_da


async def test_status_zaehlt_und_datiert(tmp_path):
    archiv = sa.ScheduleArchive(_Hass(tmp_path), "abc")
    jetzt = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    await archiv.async_maybe_store(_plan(), CONFIG, jetzt)
    await archiv.async_maybe_store(
        _plan(), CONFIG, jetzt + timedelta(minutes=sa.TAKT_MINUTEN)
    )

    status = await archiv.async_status()
    assert status["eintraege"] == 2
    assert status["bytes"] > 0
    assert status["von"] == "2026-08-25 12:00:00"
    assert status["bis"] == "2026-08-25 12:15:00"
    assert status["aufbewahrung_tage"] == sa.AUFBEWAHRUNG_TAGE


async def test_zip_enthaelt_plaene_verlauf_und_lesehilfe(tmp_path):
    archiv = sa.ScheduleArchive(_Hass(tmp_path), "abc", "1.0-test")
    jetzt = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    await archiv.async_maybe_store(_plan(), CONFIG, jetzt)

    daten = await archiv.async_build_zip({"reihen": {"pv_leistung": [["t", 1.0]]}})
    with zipfile.ZipFile(io.BytesIO(daten)) as z:
        namen = z.namelist()
        assert any(n.startswith("plaene/") and n.endswith(".json.gz") for n in namen)
        assert "ist_verlauf.json" in namen
        assert "einstellungen.json" in namen
        assert "LIESMICH.md" in namen

        verlauf = json.loads(z.read("ist_verlauf.json").decode("utf-8"))
        assert verlauf["reihen"]["pv_leistung"] == [["t", 1.0]]
        # Der Plan bleibt im ZIP lesbar, ohne ihn erst auspacken zu müssen
        plan_name = next(n for n in namen if n.endswith(".json.gz"))
        eintrag = json.loads(gzip.decompress(z.read(plan_name)).decode("utf-8"))
        assert eintrag["plan"]["slots"][0]["battery_p"] == 1.0
        assert "feedin_price" in z.read("LIESMICH.md").decode("utf-8")


async def test_leeres_archiv_liefert_trotzdem_ein_zip(tmp_path):
    """Der Knopf ist zwar aus, aber ein Aufruf darf nicht scheitern."""
    archiv = sa.ScheduleArchive(_Hass(tmp_path), "abc")
    daten = await archiv.async_build_zip(None)
    with zipfile.ZipFile(io.BytesIO(daten)) as z:
        assert z.namelist() == ["LIESMICH.md"]


# ---------------------------------------------------------------------------
# async_lies_vor — Plan vom Vorabend für die Tagesbilanz
# ---------------------------------------------------------------------------
async def test_lies_vor_nimmt_den_juengsten_eintrag_vor_dem_ziel(tmp_path):
    archiv = sa.ScheduleArchive(_Hass(tmp_path), "abc")
    tz = timezone(timedelta(hours=2))
    # Drei Pläne am Vorabend, einer nach Mitternacht.
    for stunde, minute, marke in (
        (22, 45, "zu frueh"),
        (23, 30, "auch zu frueh"),
        (23, 45, "der richtige"),
    ):
        await archiv.async_maybe_store(
            {**_plan(), "marke": marke}, {},
            datetime(2026, 8, 26, stunde, minute, tzinfo=tz),
        )
        archiv._letzter_stempel = None  # Takt-Sperre für den Test umgehen
    await archiv.async_maybe_store(
        {**_plan(), "marke": "schon der neue Tag"}, {},
        datetime(2026, 8, 27, 0, 5, tzinfo=tz),
    )

    ziel = datetime(2026, 8, 27, 0, 0, tzinfo=tz)
    eintrag = await archiv.async_lies_vor(ziel)
    assert eintrag is not None
    assert eintrag["plan"]["marke"] == "der richtige"


async def test_lies_vor_ignoriert_zu_alte_eintraege(tmp_path):
    """Nach einem Ausfall darf kein Tage alter Plan als Vorabend gelten."""
    archiv = sa.ScheduleArchive(_Hass(tmp_path), "abc")
    tz = timezone(timedelta(hours=2))
    await archiv.async_maybe_store(
        _plan(), {}, datetime(2026, 8, 26, 12, 0, tzinfo=tz)
    )
    ziel = datetime(2026, 8, 27, 0, 0, tzinfo=tz)
    # 12 Stunden alt — außerhalb des Zwei-Stunden-Fensters.
    assert await archiv.async_lies_vor(ziel) is None
    # Mit weitem Fenster wird er gefunden.
    assert await archiv.async_lies_vor(ziel, hoechstens_h=24) is not None


async def test_lies_vor_ohne_archiv(tmp_path):
    archiv = sa.ScheduleArchive(_Hass(tmp_path), "abc")
    ziel = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)
    assert await archiv.async_lies_vor(ziel) is None


async def test_lies_vor_uebergeht_kaputte_dateien(tmp_path):
    archiv = sa.ScheduleArchive(_Hass(tmp_path), "abc")
    tz = timezone(timedelta(hours=2))
    await archiv.async_maybe_store(
        {**_plan(), "marke": "gut"}, {}, datetime(2026, 8, 26, 23, 30, tzinfo=tz)
    )
    # Eine jüngere, unlesbare Datei daneben.
    ordner = Path(tmp_path) / sa.ARCHIV_ORDNER / "2026-08-26"
    (ordner / "234500.json.gz").write_bytes(b"kein gzip")
    ziel = datetime(2026, 8, 27, 0, 0, tzinfo=tz)
    # Die kaputte Datei ist die jüngste — sie wird gewählt und schlägt fehl,
    # ohne den Aufrufer zu reißen.
    assert await archiv.async_lies_vor(ziel) is None
    # Eine Datei mit unbrauchbarem Namen wird gar nicht erst betrachtet.
    (ordner / "kaputt.json.gz").write_bytes(b"egal")
    assert await archiv.async_lies_vor(ziel) is None


# ---------------------------------------------------------------------------
# Eingaben und Heizstab-Einstellungen (Replay 28.09.2026)
# ---------------------------------------------------------------------------


async def test_heizstab_einstellungen_ohne_host_und_port():
    gefiltert = sa.einstellungen_filtern({
        "heizstab_enabled": True,
        "heizstab_max_kw": 6.0,
        "heizstab_waermewert": 0.12,
        "heizstab_puffer_liter": 1000,
        "heizstab_sperr_entity": "binary_sensor.holzvergaser",
        "heizstab_host": "192.168.100.58",
        "heizstab_port": 502,
    })
    assert gefiltert["heizstab_max_kw"] == 6.0
    assert gefiltert["heizstab_sperr_entity"] == "binary_sensor.holzvergaser"
    assert "heizstab_host" not in gefiltert
    assert "heizstab_port" not in gefiltert


async def test_eingaben_mit_p10_pfad_werden_abgelegt(tmp_path):
    """Ohne p10-Pfad rechnete ein Replay die Reserve mit dem Pauschalfaktor
    und lag in Traun im Median 1,35 kW neben dem echten Plan."""
    from custom_components.eeg_energy_optimizer import schedule as sched

    start = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    inputs = sched.ScheduleInputs(
        start=start, time_res_s=900, timestamps=[start],
        consumption_kw=[0.5], production_kw=[4.0],
        min_production_kw=[2.5], worst_case_factor=0.6,
        battery_free_kwh=5.0, battery_capacity_kwh=10.0,
        battery_power_limit_kw=5.0, soc_pct=50.0, ac_limit_kw=8.0,
        feedin_limit_kw=7.5, feedin_price=0.02, feedin_price_night=None,
        night_start_hour=20, night_end_hour=6, consumption_price=0.19,
        battery_cost=0.01, heizstab_budget_kwh=8.3,
        eeg_bedarf={"Pucking": {123: 1.5}},
    )
    archiv = sa.ScheduleArchive(_Hass(tmp_path), "abc")
    assert await archiv.async_maybe_store(_plan(), CONFIG, start, inputs) == "start"
    datei = next(tmp_path.glob(f"{sa.ARCHIV_ORDNER}/*/*.json.gz"))
    eintrag = json.loads(gzip.decompress(datei.read_bytes()))
    eingaben = eintrag["eingaben"]
    assert eingaben["min_production_kw"] == [2.5]
    assert eingaben["heizstab_budget_kwh"] == 8.3
    assert eingaben["start"] == start.isoformat()
    assert eingaben["eeg_bedarf"] == {"Pucking": {"123": 1.5}}
    # Was der Plan schon trägt, steht nicht doppelt drin
    assert "consumption_kw" not in eingaben and "timestamps" not in eingaben


async def test_ohne_eingaben_bleibt_der_eintrag_wie_bisher(tmp_path):
    archiv = sa.ScheduleArchive(_Hass(tmp_path), "abc")
    jetzt = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    await archiv.async_maybe_store(_plan(), CONFIG, jetzt)
    datei = next(tmp_path.glob(f"{sa.ARCHIV_ORDNER}/*/*.json.gz"))
    assert "eingaben" not in json.loads(gzip.decompress(datei.read_bytes()))
