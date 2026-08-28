"""Tests für die Gruppierung des Verbrauchsprofils (Werktag / Wochenende+Feiertag).

Der Umbau von sieben Wochentagen auf zwei Gruppen hat genau einen Zweck: mehr
Stützwerte pro Stunde, damit ein Einzelereignis (E-Auto-Ladung an einem
Dienstag) die Prognose nicht mehr verzerrt. Diese Suite belegt beides — die
Gruppenbildung selbst und die Wirkung des getrimmten Mittels.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.eeg_energy_optimizer.coordinator import ConsumptionCoordinator

# CET (UTC+1) wie in test_coordinator.py — Tests patchen _as_local darauf
CET = timezone(timedelta(hours=1))

# Vier Rückblickwochen (Default DEFAULT_LOOKBACK_WEEKS): Mo 16.02. – So 15.03.2026
MONDAYS = [
    datetime(2026, 2, 16, tzinfo=CET),
    datetime(2026, 2, 23, tzinfo=CET),
    datetime(2026, 3, 2, tzinfo=CET),
    datetime(2026, 3, 9, tzinfo=CET),
]

# Feiertag, der auf einen Werktag fällt: Mariä Empfängnis, Di 08.12.2026 (AT).
# Die Woche 07.–13.12. enthält sonst keinen weiteren AT-Feiertag.
FEIERTAG_WOCHE_MONTAG = datetime(2026, 12, 7, tzinfo=CET)
FEIERTAG_DI = datetime(2026, 12, 8, tzinfo=CET)
NORMALER_DI = datetime(2026, 12, 15, tzinfo=CET)


def _entry(dt_local, watts):
    """Recorder-Statistikeintrag mit UTC-Zeitstempel (mean in kW-Konvention)."""
    return {
        "start": dt_local.astimezone(timezone.utc).timestamp(),
        "mean": watts,
    }


def _as_local_cet(dt_obj):
    return dt_obj.astimezone(CET)


def _patch_recorder(stats_data):
    """Recorder und Zeitzone patchen; liefert die drei Contextmanager."""
    mock_stats = AsyncMock(return_value=stats_data)
    mock_recorder = MagicMock()
    mock_recorder.async_add_executor_job = mock_stats
    mock_get_instance = MagicMock(return_value=mock_recorder)

    return (
        patch(
            "custom_components.eeg_energy_optimizer.coordinator.statistics_during_period",
            new=MagicMock(),
        ),
        patch(
            "custom_components.eeg_energy_optimizer.coordinator.get_instance",
            new=mock_get_instance,
        ),
        patch(
            "custom_components.eeg_energy_optimizer.coordinator._as_local",
            new=_as_local_cet,
        ),
    )


def _hass(country=None):
    """Mock-hass mit explizit gesetztem Ländercode."""
    hass = MagicMock()
    hass.data = {}
    hass.config.country = country
    return hass


def _week_entries(monday, werktag_w, wochenende_w, sonderfaelle=None):
    """Eine Woche Stundenstatistik, flach je Gruppe.

    sonderfaelle: {(iso_datum, stunde): watt} überschreibt einzelne Stunden.
    """
    sonderfaelle = sonderfaelle or {}
    entries = []
    for offset in range(7):
        day = monday + timedelta(days=offset)
        default_w = wochenende_w if day.weekday() >= 5 else werktag_w
        for hour in range(24):
            watts = sonderfaelle.get((day.date().isoformat(), hour), default_w)
            if watts is None:  # None = dieser Tag/diese Stunde liefert nichts
                continue
            entries.append(_entry(day.replace(hour=hour), watts))
    return entries


def _four_weeks(werktag_w=400.0, wochenende_w=900.0, sonderfaelle=None):
    """Vier Wochen Statistik → 20 Werktag- und 8 Wochenend-Stützwerte je Stunde."""
    entries = []
    for monday in MONDAYS:
        entries.extend(_week_entries(monday, werktag_w, wochenende_w, sonderfaelle))
    return {"sensor.consumption": entries}


async def _load(hass, stats_data, lookback_weeks=4):
    """Coordinator mit den gegebenen Statistiken befüllen."""
    coordinator = ConsumptionCoordinator(hass, "sensor.consumption", lookback_weeks)
    patch_sdp, patch_gi, patch_tz = _patch_recorder(stats_data)
    with patch_sdp, patch_gi, patch_tz:
        await coordinator.async_update()
    return coordinator


# ---------------------------------------------------------------------------
# (a) Mo–Fr sind eine Gruppe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_werktage_teilen_sich_eine_gruppe():
    """Ein hoher Dienstagswert hebt alle Werktage gleich — und nur verdünnt.

    Bewusst alle vier Dienstage erhöht: ein einzelner Ausreißer würde vom
    getrimmten Mittel komplett entfernt (das prüft der Test darunter). Hier
    geht es um die Verdünnung, die schon aus der Gruppierung folgt.
    """
    dienstage = ["2026-02-17", "2026-02-24", "2026-03-03", "2026-03-10"]
    stats = _four_weeks(sonderfaelle={(d, 18): 2400.0 for d in dienstage})

    coordinator = await _load(_hass(), stats)

    # 20 Werktag-Stützwerte: 4×2400 + 16×400 = 16000, größter (2400) fällt weg
    # → 13600 / 19 = 715.79 W
    erwartet = 13600 / 19
    for tag in ("mo", "di", "mi", "do", "fr"):
        assert coordinator.hourly_avg[tag][18] == pytest.approx(erwartet)

    # Tagesscharf hätte der Dienstag hier 2400 W getragen — die Gruppierung
    # verdünnt das auf knapp ein Drittel des Weges von 400 auf 2400.
    assert erwartet < 800.0
    # Wochenende bleibt unberührt
    assert coordinator.hourly_avg["sa"][18] == pytest.approx(900.0)


# ---------------------------------------------------------------------------
# (b) Der Ausreißer-Fall, um den es eigentlich geht
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_einmalige_wallbox_ladung_verschwindet_komplett():
    """4 Wochen, 20 Werktag-Werte: 19×400 W + 1×11000 W → 400 W.

    Die einmalige E-Auto-Ladung ist der größte Wert und wird getrimmt; übrig
    bleiben 19×400 W = 7600 W / 19 = exakt 400 W.
    """
    stats = _four_weeks(sonderfaelle={("2026-02-17", 19): 11000.0})

    coordinator = await _load(_hass(), stats)

    assert coordinator.bucket_avg["wt"][19] == pytest.approx(400.0)
    assert coordinator.hourly_avg["di"][19] == pytest.approx(400.0)

    # Gegenprobe: ohne Trimmung wären es (7600 + 11000) / 20 = 930 W gewesen
    assert coordinator.bucket_avg["wt"][19] < 500.0


# ---------------------------------------------------------------------------
# (c) Sa/So sind die zweite, unabhängige Gruppe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wochenende_ist_unabhaengige_gruppe():
    """Erhöhte Samstage verändern die we-Gruppe und lassen wt unberührt."""
    samstage = ["2026-02-21", "2026-02-28", "2026-03-07", "2026-03-14"]
    stats = _four_weeks(sonderfaelle={(d, 12): 5000.0 for d in samstage})

    coordinator = await _load(_hass(), stats)

    # 8 Wochenend-Stützwerte: 4×5000 + 4×900 = 23600, größter (5000) weg
    # → 18600 / 7 = 2657.14 W
    assert coordinator.bucket_avg["we"][12] == pytest.approx(18600 / 7)
    assert coordinator.hourly_avg["sa"][12] == pytest.approx(18600 / 7)
    assert coordinator.hourly_avg["so"][12] == pytest.approx(18600 / 7)

    # Werktage bleiben bei 400 W — keine Vermischung der Gruppen
    assert coordinator.bucket_avg["wt"][12] == pytest.approx(400.0)


# ---------------------------------------------------------------------------
# (d) Feiertag an einem Werktag zählt als Wochenende
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feiertag_wird_beim_einsortieren_zur_we_gruppe():
    """Der Dienstag 08.12. (AT-Feiertag) landet in der we-Gruppe.

    Eine Woche reicht als Beleg und hält die Rechnung trimmfrei: wt bekommt
    4 Werte (Mo, Mi, Do, Fr), we bekommt 3 (Sa, So, Feiertags-Di).
    """
    entries = _week_entries(
        FEIERTAG_WOCHE_MONTAG,
        werktag_w=400.0,
        wochenende_w=900.0,
        sonderfaelle={("2026-12-08", hour): 5000.0 for hour in range(24)},
    )

    coordinator = await _load(_hass("AT"), {"sensor.consumption": entries})

    # wt bleibt sauber bei 400 W — die 5000 W sind nicht dort gelandet
    assert coordinator.bucket_avg["wt"][12] == pytest.approx(400.0)
    # we hat 3 Werte (900, 900, 5000) → 6800 / 3 = 2266.67 W
    assert coordinator.bucket_avg["we"][12] == pytest.approx(6800 / 3)


def test_feiertag_wird_in_hourly_for_und_calculate_period_beruecksichtigt():
    """Prognose am Feiertag muss die we-Werte nehmen, nicht die des Dienstags."""
    coordinator = ConsumptionCoordinator(_hass("AT"), "sensor.consumption", 4)
    coordinator.bucket_avg = {
        "wt": {h: 400.0 for h in range(24)},
        "we": {h: 900.0 for h in range(24)},
    }

    assert coordinator.bucket_for(FEIERTAG_DI.replace(hour=10)) == "we"
    assert coordinator.bucket_for(NORMALER_DI.replace(hour=10)) == "wt"

    assert coordinator.hourly_for(FEIERTAG_DI.replace(hour=10)) == 900.0
    assert coordinator.hourly_for(NORMALER_DI.replace(hour=10)) == 400.0

    feiertag = coordinator.calculate_period(
        FEIERTAG_DI.replace(hour=10), FEIERTAG_DI.replace(hour=12)
    )
    normal = coordinator.calculate_period(
        NORMALER_DI.replace(hour=10), NORMALER_DI.replace(hour=12)
    )

    assert feiertag["verbrauch_kwh"] == pytest.approx(1.8)
    assert normal["verbrauch_kwh"] == pytest.approx(0.8)
    # Das Stundenprofil weist die Gruppe aus, der Wochentag bleibt "di"
    assert [e["gruppe"] for e in feiertag["stundenprofil"]] == ["we", "we"]
    assert [e["wochentag"] for e in feiertag["stundenprofil"]] == ["di", "di"]


# ---------------------------------------------------------------------------
# (e) Ohne Ländercode gibt es keine Feiertage — und keine Exception
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("country", [None, "", "   ", "XX", "ZZZ", 42, ["AT"]])
def test_ohne_gueltiges_land_keine_feiertage(country):
    """Leerer, unbekannter oder unsinniger Ländercode: nur Sa/So sind "we"."""
    coordinator = ConsumptionCoordinator(_hass(country), "sensor.consumption", 4)
    coordinator.bucket_avg = {
        "wt": {h: 400.0 for h in range(24)},
        "we": {h: 900.0 for h in range(24)},
    }

    # 08.12.2026 ist ein Dienstag → ohne Feiertagskalender ein Werktag
    assert coordinator.bucket_for(FEIERTAG_DI.replace(hour=10)) == "wt"
    assert coordinator.hourly_for(FEIERTAG_DI.replace(hour=10)) == 400.0
    # Sa/So funktionieren unabhängig vom Kalender
    assert coordinator.bucket_for(datetime(2026, 12, 12, 10, tzinfo=CET)) == "we"


def test_ohne_config_attribut_keine_exception():
    """hass ohne .config (theoretischer Fall) darf die Prognose nicht kippen."""
    hass = MagicMock()
    del hass.config
    coordinator = ConsumptionCoordinator(hass, "sensor.consumption", 4)
    coordinator.bucket_avg = {"wt": {10: 400.0}, "we": {10: 900.0}}

    assert coordinator.hourly_for(FEIERTAG_DI.replace(hour=10)) == 400.0


def test_ohne_holidays_paket_keine_exception():
    """Fehlt das Paket, läuft alles weiter — eben ohne Feiertagserkennung."""
    with patch("custom_components.eeg_energy_optimizer.coordinator._holidays_lib", new=None):
        coordinator = ConsumptionCoordinator(_hass("AT"), "sensor.consumption", 4)
        coordinator.bucket_avg = {"wt": {10: 400.0}, "we": {10: 900.0}}

        assert coordinator.bucket_for(FEIERTAG_DI.replace(hour=10)) == "wt"
        assert coordinator.hourly_for(FEIERTAG_DI.replace(hour=10)) == 400.0


@pytest.mark.asyncio
async def test_voller_refresh_ohne_land_laeuft_durch():
    """Ende-zu-Ende ohne Ländercode: Profil wird gefüllt, nichts kracht."""
    coordinator = await _load(_hass(None), _four_weeks())

    assert coordinator.bucket_avg["wt"][12] == pytest.approx(400.0)
    assert coordinator.bucket_avg["we"][12] == pytest.approx(900.0)
    assert coordinator.stats_count > 0


# ---------------------------------------------------------------------------
# (f) Getrimmtes Mittel greift erst ab 5 Werten
# ---------------------------------------------------------------------------


def test_trimmung_erst_ab_fuenf_werten():
    """Bei 4 Werten normaler Mittelwert, ab 5 fällt der größte weg."""
    vier = [400.0, 400.0, 400.0, 4000.0]
    fuenf = [400.0, 400.0, 400.0, 400.0, 4000.0]

    # 5200 / 4 = 1300 — der Ausreißer bleibt drin
    assert ConsumptionCoordinator._aggregate(vier) == pytest.approx(1300.0)
    # 1600 / 4 = 400 — der Ausreißer ist weg
    assert ConsumptionCoordinator._aggregate(fuenf) == pytest.approx(400.0)


def test_trimmung_bei_einem_einzigen_wert():
    """Ein Stützwert darf nicht durch (n-1) geteilt werden."""
    assert ConsumptionCoordinator._aggregate([1234.0]) == pytest.approx(1234.0)


@pytest.mark.asyncio
async def test_trimmung_greift_ab_fuenf_ende_zu_ende():
    """Zwei Wochen Wochenende = 4 Werte → keine Trimmung; drei Wochen = 6 → doch."""
    zwei_wochen = []
    for monday in MONDAYS[:2]:
        zwei_wochen.extend(
            _week_entries(
                monday,
                werktag_w=400.0,
                wochenende_w=900.0,
                sonderfaelle={("2026-02-21", 12): 5000.0},
            )
        )
    coordinator = await _load(_hass(), {"sensor.consumption": zwei_wochen})
    # 4 Werte: 3×900 + 5000 = 7700 / 4 = 1925 W (ungetrimmt)
    assert coordinator.bucket_avg["we"][12] == pytest.approx(7700 / 4)

    drei_wochen = []
    for monday in MONDAYS[:3]:
        drei_wochen.extend(
            _week_entries(
                monday,
                werktag_w=400.0,
                wochenende_w=900.0,
                sonderfaelle={("2026-02-21", 12): 5000.0},
            )
        )
    coordinator = await _load(_hass(), {"sensor.consumption": drei_wochen})
    # 6 Werte: 5×900 + 5000, größter weg → 4500 / 5 = 900 W
    assert coordinator.bucket_avg["we"][12] == pytest.approx(900.0)


# ---------------------------------------------------------------------------
# (g) hourly_avg bleibt panel-kompatibel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hourly_avg_behaelt_sieben_schluessel():
    """Panel-Diagramm und Sensor-Attribute hängen am alten Schema."""
    coordinator = await _load(_hass(), _four_weeks())

    assert set(coordinator.hourly_avg) == {"mo", "di", "mi", "do", "fr", "sa", "so"}
    for day, hours in coordinator.hourly_avg.items():
        assert set(hours) == set(range(24)), f"{day} unvollständig"

    werktage = [coordinator.hourly_avg[d] for d in ("mo", "di", "mi", "do", "fr")]
    assert all(tag == werktage[0] for tag in werktage)
    assert coordinator.hourly_avg["sa"] == coordinator.hourly_avg["so"]
    # Und die beiden Gruppen sind wirklich verschieden
    assert coordinator.hourly_avg["mo"] != coordinator.hourly_avg["sa"]


@pytest.mark.asyncio
async def test_ausgefaechertes_profil_ist_von_der_gruppe_entkoppelt():
    """Die Wochentagssicht ist eine Kopie — Mutation darf nicht zurückschlagen."""
    coordinator = await _load(_hass(), _four_weeks())

    coordinator.hourly_avg["mo"][12] = 12345.0
    assert coordinator.bucket_avg["wt"][12] == pytest.approx(400.0)
    assert coordinator.hourly_avg["di"][12] == pytest.approx(400.0)
