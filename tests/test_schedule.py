"""Tests für die Fahrplan-Anbindung (schedule.py).

Deckt die beiden Teile getrennt ab: das Sammeln der Eingangsdaten aus Home
Assistant (mit Mock-hass) und die Rechnung selbst (echtes pandas, echtes
HiGHS). Damit ist belegt, dass Verbrauchsprofil, PV-Prognose und
Batteriezustand richtig in Haralds ``opt()`` ankommen.
"""

import dataclasses
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.eeg_energy_optimizer import schedule as sched

TZ = timezone(timedelta(hours=2))  # Sommerzeit Wien, ausreichend für die Tests
NOW = datetime(2026, 8, 24, 5, 7, tzinfo=TZ)  # Montag, krumme Minute


def _profile_coordinator(watts_per_hour: float = 400.0):
    """Coordinator-Attrappe mit flachem Verbrauchsprofil.

    ``hourly_for()`` ist der Weg, den schedule.py nimmt (Gruppen statt
    Wochentage — nur der Coordinator kennt Feiertage); ``hourly_avg`` bleibt
    gesetzt, weil dort noch die "Profil vorhanden?"-Prüfung hängt.
    """
    coordinator = MagicMock()
    coordinator.bucket_avg = {
        bucket: {hour: watts_per_hour for hour in range(24)}
        for bucket in ("wt", "we")
    }
    coordinator.hourly_avg = {
        day: {hour: watts_per_hour for hour in range(24)}
        for day in ("mo", "di", "mi", "do", "fr", "sa", "so")
    }
    coordinator.hourly_for = lambda stamp: watts_per_hour
    return coordinator


def _wh_hours(start: datetime, hours: int = 48) -> dict[str, float]:
    """Stündliche PV-Prognose wie async_get_solar_forecast sie liefert."""
    result = {}
    cursor = start.replace(minute=0, second=0, microsecond=0)
    for offset in range(hours):
        stamp = cursor + timedelta(hours=offset)
        # Tagesgang: 6–20 Uhr, Spitze mittags bei 6000 Wh
        hour = stamp.hour
        wh = 0.0 if hour < 6 or hour > 20 else 6000.0 * (1 - abs(13 - hour) / 7)
        result[stamp.isoformat()] = round(max(0.0, wh), 1)
    return result


# ---------------------------------------------------------------------------
# Bausteine
# ---------------------------------------------------------------------------


def test_zeitpunkte_beginnen_exakt_am_start():
    stamps = sched._grid_timestamps(NOW, hours=4)

    assert stamps[0] == NOW                      # 05:07 bleibt erhalten
    assert stamps[1] == NOW.replace(minute=30)   # dann ins Halbstundenraster
    assert all(b > a for a, b in zip(stamps, stamps[1:]))
    assert all(t.minute in (0, 30) for t in stamps[1:])
    assert stamps[-1] <= NOW + timedelta(hours=4)


def test_verbrauchsprofil_wird_zu_kilowatt():
    stamps = sched._grid_timestamps(NOW, hours=3)
    values = sched._consumption_from_profile(_profile_coordinator(750.0), stamps)

    assert values == [0.75] * len(stamps)


def test_fehlendes_verbrauchsprofil_gibt_none():
    leer = MagicMock()
    leer.hourly_avg = {}
    assert sched._consumption_from_profile(leer, [NOW]) is None


def test_pv_prognose_wird_zu_kilowatt():
    stamps = sched._grid_timestamps(NOW, hours=6)
    values = sched._production_from_wh(_wh_hours(NOW), stamps)

    assert len(values) == len(stamps)
    # 05:07 fällt in die Stunde 05:00 — vor Sonnenaufgang, also 0
    assert values[0] == 0.0
    # Mittags muss Leistung anliegen
    mittag = next(i for i, s in enumerate(stamps) if s.hour == 11)
    assert values[mittag] > 1.0


def test_pv_prognose_matcht_ueber_zeitpunkt_nicht_ueber_darstellung():
    """Prognose in UTC, Zeitpunkte lokal — muss trotzdem zusammenfinden."""
    stamps = sched._grid_timestamps(NOW, hours=3)
    lokal = _wh_hours(NOW)
    in_utc = {
        datetime.fromisoformat(k).astimezone(timezone.utc).isoformat(): v
        for k, v in lokal.items()
    }

    assert sched._production_from_wh(in_utc, stamps) == sched._production_from_wh(
        lokal, stamps
    )


# ---------------------------------------------------------------------------
# Sammeln
# ---------------------------------------------------------------------------


def _hass_with(config: dict, coordinator=None, forecast=True):
    hass = MagicMock()
    hass.data = {
        sched.DOMAIN: {
            "entry1": {
                "config": config,
                "coordinator": coordinator or _profile_coordinator(),
                "inverter": None,
            }
        }
    }

    def state_for(entity_id):
        if entity_id == "sensor.soc":
            state = MagicMock()
            state.state = "40"
            return state
        return None

    hass.states.get.side_effect = state_for
    hass.states.async_all.return_value = []   # kein Solcast, also Rückfallweg
    return hass


BASE_CONFIG = {
    "battery_soc_sensor": "sensor.soc",
    "battery_capacity_kwh": 12.5,
    "discharge_power_kw": 5.0,
    "forecast_source": "solcast_solar",
}


async def test_inputs_werden_vollstaendig_gesammelt():
    hass = _hass_with(BASE_CONFIG)

    with (
        patch.object(sched, "_now_local", return_value=NOW),
        patch.object(
            sched, "_async_solar_forecast_wh", AsyncMock(return_value=_wh_hours(NOW))
        ),
    ):
        inputs, problem = await sched.async_collect_inputs(hass, "entry1")

    assert problem is None
    assert inputs is not None
    # Start ist die aktuelle Minute — der Ladestand gilt jetzt, nicht vor
    # einer Viertelstunde
    assert inputs.start == NOW.replace(second=0, microsecond=0)
    assert inputs.time_res_s == 900
    # SOC 40 % von 12,5 kWh → 7,5 kWh freie Kapazität
    assert inputs.battery_free_kwh == pytest.approx(7.5)
    assert inputs.battery_capacity_kwh == 12.5
    assert inputs.battery_power_limit_kw == 5.0
    assert len(inputs.consumption_kw) == len(inputs.timestamps)
    assert len(inputs.production_kw) == len(inputs.timestamps)


async def test_kapazitaets_sensor_schlaegt_den_fixwert():
    """Der Sensor gewinnt gegen den manuell eingetragenen Wert.

    Regression: Der Fahrplan las ausschliesslich ``battery_capacity_kwh`` und
    rechnete deshalb mit dem Wert, der beim Setup eingetragen wurde, solange
    der Kapazitaets-Sensor noch ``unknown`` war (an einer 15-kWh-Anlage waren
    das 10 kWh). Wh werden dabei zu kWh normalisiert.
    """
    config = dict(BASE_CONFIG)
    config["battery_capacity_sensor"] = "sensor.kapazitaet"

    hass = _hass_with(config)
    original = hass.states.get.side_effect

    def state_for(entity_id):
        if entity_id == "sensor.kapazitaet":
            state = MagicMock()
            state.state = "15000"
            state.attributes = {"unit_of_measurement": "Wh"}
            return state
        return original(entity_id)

    hass.states.get.side_effect = state_for

    with (
        patch.object(sched, "_now_local", return_value=NOW),
        patch.object(
            sched, "_async_solar_forecast_wh", AsyncMock(return_value=_wh_hours(NOW))
        ),
    ):
        inputs, problem = await sched.async_collect_inputs(hass, "entry1")

    assert problem is None
    assert inputs.battery_capacity_kwh == pytest.approx(15.0)
    # SOC 40 % von 15 kWh -> 9 kWh frei (mit dem Fixwert waeren es 7,5)
    assert inputs.battery_free_kwh == pytest.approx(9.0)


def _puffer_inputs(**abweichungen):
    """ScheduleInputs mit runden Werten — 10 kWh Kapazität, 5 kWh frei."""
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
    )
    basis.update(abweichungen)
    return sched.ScheduleInputs(**basis)


def test_mindest_ladestand_kuerzt_die_nutzbare_kapazitaet():
    """10 % Puffer heißt: das Modell sieht 90 % der Kapazität.

    So entsteht die harte Untergrenze — opt() zählt in „freier Platz bis voll",
    eine kleinere Kapazität schneidet unten ab, und zwar in jedem Slot. Der Weg
    über die Reserve wurde gemessen und verworfen: siehe
    ``test_mindest_ladestand_ist_eine_harte_untergrenze``.
    """
    c = sched.HAConfig(_puffer_inputs(min_soc_pct=10.0))

    assert c.battery_capacity == pytest.approx(9.0)      # 10 kWh - 10 %
    assert c.battery_free == pytest.approx(5.0)          # unverändert, passt noch
    # Keine getrennte Reserve mehr — der Ladestand ist die Sicherheitsreserve
    assert c.max_blackout_reserve == 0.0

    c0 = sched.HAConfig(_puffer_inputs(min_soc_pct=0.0))
    assert c0.battery_capacity == pytest.approx(10.0)


def test_ladestand_unter_dem_puffer_bleibt_rechenbar():
    """Steht die Batterie unter dem Puffer, wird von „leer" aus gerechnet."""
    c = sched.HAConfig(_puffer_inputs(min_soc_pct=50.0, battery_free_kwh=9.5, soc_pct=5.0))

    assert c.battery_capacity == pytest.approx(5.0)
    assert c.battery_free == pytest.approx(5.0)   # geklemmt, nicht 9,5


def test_mindest_ladestand_wird_gelesen_wie_eingetragen():
    assert sched._min_soc_pct({}) == sched.DEFAULT_MIN_SOC_PCT
    assert sched._min_soc_pct({"schedule_min_soc_pct": ""}) == sched.DEFAULT_MIN_SOC_PCT
    assert sched._min_soc_pct({"schedule_min_soc_pct": "keine Zahl"}) == sched.DEFAULT_MIN_SOC_PCT
    assert sched._min_soc_pct({"schedule_min_soc_pct": 0}) == 0.0
    assert sched._min_soc_pct({"schedule_min_soc_pct": "15"}) == 15.0
    assert sched._min_soc_pct({"schedule_min_soc_pct": -3}) == 0.0
    # Gekappt bei 30 %: darüber bliebe zu wenig nutzbarer Bereich
    assert sched._min_soc_pct({"schedule_min_soc_pct": 99}) == 30.0
    assert sched._min_soc_pct({"schedule_min_soc_pct": 30}) == 30.0


async def test_ohne_pv_prognose_kein_fahrplan():
    hass = _hass_with(BASE_CONFIG)

    with (
        patch.object(sched, "_now_local", return_value=NOW),
        patch.object(sched, "_async_solar_forecast_wh", AsyncMock(return_value=None)),
    ):
        inputs, problem = await sched.async_collect_inputs(hass, "entry1")

    assert inputs is None
    assert "PV-Prognose" in problem


async def test_ohne_batteriedaten_kein_fahrplan():
    config = {**BASE_CONFIG, "battery_soc_sensor": "sensor.fehlt"}
    hass = _hass_with(config)

    with (
        patch.object(sched, "_now_local", return_value=NOW),
        patch.object(
            sched, "_async_solar_forecast_wh", AsyncMock(return_value=_wh_hours(NOW))
        ),
    ):
        inputs, problem = await sched.async_collect_inputs(hass, "entry1")

    assert inputs is None
    assert "Ladestand" in problem


# ---------------------------------------------------------------------------
# Rechnen
# ---------------------------------------------------------------------------


def _inputs_for_solve(horizon_hours: int = 36) -> sched.ScheduleInputs:
    stamps = sched._grid_timestamps(NOW, hours=horizon_hours)
    return sched.ScheduleInputs(
        start=NOW.replace(minute=0),
        time_res_s=900,
        timestamps=stamps,
        consumption_kw=sched._consumption_from_profile(_profile_coordinator(600), stamps),
        production_kw=sched._production_from_wh(_wh_hours(NOW, horizon_hours + 2), stamps),
        min_production_kw=None,
        worst_case_factor=0.6,
        battery_free_kwh=7.5,
        battery_capacity_kwh=12.5,
        battery_power_limit_kw=5.0,
        soc_pct=40.0,
        ac_limit_kw=10.0,
        feedin_limit_kw=9.5,
        feedin_price=0.0973,
        feedin_price_night=None,
        night_start_hour=22,
        night_end_hour=6,
        consumption_price=0.2620,
        battery_cost=0.01,
        forecast_source="solcast_solar",
    )


def test_mindest_ladestand_ist_eine_harte_untergrenze():
    """Der ganze Weg durch den Solver: kein Slot plant unter den Puffer.

    Gegenprobe zur verworfenen Variante: als vorausschauende Reserve
    (``max_blackout_reserve``) blieb der tiefste geplante Ladestand
    unverändert bei 30,8 % — bei 0 wie bei 30 % Vorgabe —, weil ``bor`` die
    Füllung freigibt, sobald kein Defizit mehr in Sicht ist. Als fehlende
    Kapazität greift die Grenze dagegen in jedem Slot.
    """
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")

    ohne = sched.solve(_inputs_for_solve())
    tiefster_ohne = min(s["soc"] for s in ohne["slots"])

    mit = sched.solve(dataclasses.replace(_inputs_for_solve(), min_soc_pct=35.0))
    tiefster_mit = min(s["soc"] for s in mit["slots"])

    assert mit["min_soc_pct"] == 35.0
    # Rundung des Prozentwerts im Payload: eine Zehntelstelle Toleranz
    assert tiefster_mit >= 34.9, f"Puffer verletzt: {tiefster_mit} %"
    assert tiefster_mit > tiefster_ohne, (
        f"Puffer wirkungslos: ohne {tiefster_ohne} %, mit {tiefster_mit} %"
    )


DEFAULT_MIN_SOC = sched.DEFAULT_MIN_SOC_PCT   # 10 %, wie an einer echten Anlage


def test_ladedeckel_ist_eine_harte_obergrenze():
    """Der ganze Weg durch den Solver: kein Slot plant über den Deckel.

    Spiegelbild zum Mindest-Ladestand, aber mit einem Schritt mehr: nach unten
    abschneiden genügt hier nicht, weil ``battery_free`` in opt() bei 0 endet
    und dafür kein Parameter existiert. Das Modell rechnet deshalb im
    verschobenen Fenster [Boden, Deckel] — greift der Deckel nicht bis auf die
    Zehntelstelle, ist die Verschiebung falsch gerechnet.
    """
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")

    # Mit dem üblichen Boden (10 %), sonst lädt der Plan nur auf 89,6 % und
    # ein Deckel von 90 wäre gar nicht bindend — der Test würde nichts zeigen.
    basis = dataclasses.replace(_inputs_for_solve(), min_soc_pct=DEFAULT_MIN_SOC)
    ohne = sched.solve(basis)
    assert max(s["soc"] for s in ohne["slots"]) > 90.0, "Testdaten laden nicht voll genug"

    for deckel in (90.0, 80.0, 70.0):
        plan = sched.solve(dataclasses.replace(basis, max_soc_pct=deckel))
        hoechster = max(s["soc"] for s in plan["slots"])
        assert plan["max_soc_pct"] == deckel
        # Rundung des Prozentwerts im Payload: eine Zehntelstelle Toleranz
        assert hoechster <= deckel + 0.1, f"Deckel {deckel} verletzt: {hoechster} %"
        # Und er wird auch erreicht — ein Deckel, unter dem der Plan ohnehin
        # bleibt, wäre kein Beweis.
        assert hoechster >= deckel - 0.1, f"Deckel {deckel} nicht erreicht: {hoechster} %"


def test_ladedeckel_100_aendert_nichts():
    """Die Vorgabe. Ein Update darf keinen Plan verschieben.

    Bitgleich, nicht nur ähnlich: die Koordinatenverschiebung ist bei einem
    Deckel von 100 % die Identität, und das muss sie exakt sein.
    """
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")

    ohne = sched.solve(_inputs_for_solve())
    mit = sched.solve(dataclasses.replace(_inputs_for_solve(), max_soc_pct=100.0))

    assert [s["soc"] for s in mit["slots"]] == [s["soc"] for s in ohne["slots"]]
    assert [s["battery_p"] for s in mit["slots"]] == [s["battery_p"] for s in ohne["slots"]]
    assert [s["grid_p"] for s in mit["slots"]] == [s["grid_p"] for s in ohne["slots"]]


def test_ladedeckel_und_mindestladestand_zusammen():
    """Beide Grenzen gelten gleichzeitig, das Fenster liegt dazwischen.

    Die Einstellungen sind so gekappt (Boden ≤ 30 %, Deckel ≥ 70 %), dass sie
    sich nicht kreuzen können — geprüft wird, dass die Verschiebung beide
    Enden trifft und nicht eines gegen das andere verrechnet.
    """
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")

    plan = sched.solve(dataclasses.replace(
        _inputs_for_solve(), min_soc_pct=25.0, max_soc_pct=85.0))
    werte = [s["soc"] for s in plan["slots"]]

    assert max(werte) <= 85.1, f"Deckel verletzt: {max(werte)} %"
    assert min(werte) >= 24.9, f"Boden verletzt: {min(werte)} %"
    # Der Deckel wird ausgefahren — er ist die bindende Grenze. Der Boden
    # nicht zwangsläufig: er ist eine Schranke, keine Vorgabe, und dieser
    # Plan bleibt von selbst bei 29,9 % (gemessen).
    assert max(werte) >= 84.9


def test_ladestand_ueber_dem_deckel_wird_geklemmt():
    """Batterie steht bei 95 %, Deckel ist 90 % — das Modell sieht „voll".

    Der Fall entsteht, wenn der Deckel gerade gesenkt wurde oder das Gerät
    selbst voll geladen hat. Es darf keine Zwangsentladung geben (der Deckel
    begrenzt das Laden, er wirft nichts weg) und keine widersprüchlichen
    Schranken. Der geplante Ladestand startet dabei UNTER dem wirklichen —
    das ist die konservative Richtung.
    """
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")

    hoch = dataclasses.replace(
        _inputs_for_solve(), soc_pct=95.0,
        battery_free_kwh=12.5 * 0.05, min_soc_pct=10.0, max_soc_pct=90.0)
    config = sched.HAConfig(hoch)

    # Modell sieht keinen Platz mehr, aber eine gültige Kapazität.
    assert config.battery_free == pytest.approx(0.0)
    assert config.battery_capacity == pytest.approx(12.5 * 0.80)

    plan = sched.solve(hoch)
    assert max(s["soc"] for s in plan["slots"]) <= 90.1


def test_max_soc_pct_wird_gelesen_und_gekappt():
    # Seit v27 gibt es keinen Ein/Aus-Schlüssel mehr: der Wert allein trägt
    # den Zustand, 100 (oder keine Angabe) heißt „bis voll laden".
    assert sched._max_soc_pct({}) == 100.0
    assert sched._max_soc_pct({"schedule_max_soc_pct": 90}) == 90.0
    assert sched._max_soc_pct({"schedule_max_soc_pct": ""}) == 100.0
    assert sched._max_soc_pct({"schedule_max_soc_pct": "kaputt"}) == 100.0
    # Nach unten gekappt: darunter bliebe zu wenig nutzbarer Bereich, und
    # zusammen mit der 30-%-Kappung des Bodens kreuzen sie sich nie.
    assert sched._max_soc_pct({"schedule_max_soc_pct": 20}) == sched.MIN_MAX_SOC_PCT
    assert sched._max_soc_pct({"schedule_max_soc_pct": 140}) == 100.0


def test_geleertes_deckel_feld_ist_neutral():
    """Ein geleertes Panel-Zahlenfeld kommt als 0 an.

    Gekappt würde daraus ein Deckel von 70 % — die drastischste erlaubte
    Einstellung, aus einem Versehen. Dieselbe Lehre wie beim
    Überschussabschlag: prüfen, was die Panel-Null bedeutet.
    """
    assert sched._max_soc_pct({"schedule_max_soc_pct": 0}) == 100.0
    assert sched._max_soc_pct({"schedule_max_soc_pct": -5}) == 100.0


def test_solve_liefert_fahrplan_fuer_das_panel():
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")

    result = sched.solve(_inputs_for_solve())

    assert result["slots"], "Fahrplan darf nicht leer sein"
    assert result["time_res_min"] == 15
    assert result["duration_ms"] >= 0

    erster = result["slots"][0]
    for feld in ("t", "PV", "consumption", "battery_p", "battery", "grid_p", "soc"):
        assert feld in erster, f"Feld '{feld}' fehlt im Slot"

    # SOC bleibt in physikalischen Grenzen
    soc_werte = [s["soc"] for s in result["slots"]]
    assert all(-1.0 <= v <= 101.0 for v in soc_werte), f"SOC ausserhalb: {min(soc_werte)}..{max(soc_werte)}"

    # Start-SOC muss zur freien Kapazität passen (40 % bei 12,5 kWh)
    assert result["soc_start_pct"] == pytest.approx(40.0)

    # Zeitstempel aufsteigend und im 15-Minuten-Raster
    stamps = [datetime.fromisoformat(s["t"]) for s in result["slots"]]
    abstaende = {(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])}
    assert abstaende == {900.0}, f"Unerwartete Slot-Abstände: {abstaende}"


def test_haconfig_bietet_das_api_von_config_dummy():
    """Wenn Harald das Config-API erweitert, soll das hier auffallen."""
    pytest.importorskip("pandas")

    from custom_components.eeg_energy_optimizer.chamo import config_dummy

    haconfig = sched.HAConfig(_inputs_for_solve(horizon_hours=4))
    fehlend = [
        name
        for name in dir(config_dummy.Config)
        if not name.startswith("__") and not hasattr(haconfig, name)
    ]
    assert not fehlend, f"HAConfig fehlen Teile des chamo-API: {fehlend}"


# ---------------------------------------------------------------------------
# Solcast-Halbstundenwerte (echtes Format aus einer laufenden Anlage)
# ---------------------------------------------------------------------------


def _solcast_state(tag: datetime, werte: list[tuple[str, float, float]]):
    """Sensor-Attrappe mit detailedForecast im Format von solcast_solar."""
    state = MagicMock()
    state.attributes = {
        "detailedForecast": [
            {
                "period_start": tag.replace(
                    hour=int(uhrzeit[:2]), minute=int(uhrzeit[3:]), second=0, microsecond=0
                ).isoformat(),
                "pv_estimate": est,
                "pv_estimate10": p10,
                "pv_estimate90": est * 1.05,
            }
            for uhrzeit, est, p10 in werte
        ]
    }
    return state


SOLCAST_TAG = [
    ("05:00", 0.0, 0.0),
    ("05:30", 0.12, 0.05),
    ("06:00", 0.48, 0.21),
    ("06:30", 1.10, 0.52),
    ("07:00", 1.95, 0.98),
    ("07:30", 2.80, 1.42),
]


def test_solcast_detailed_wird_eingelesen():
    hass = MagicMock()
    hass.states.async_all.return_value = [
        MagicMock(attributes={"friendly_name": "irgendwas"}),  # ohne Prognose
        _solcast_state(NOW, SOLCAST_TAG),
    ]

    detailed = sched._solcast_detailed(hass)

    assert len(detailed) == len(SOLCAST_TAG)
    sechs_uhr = NOW.replace(hour=6, minute=0, second=0, microsecond=0)
    assert detailed[sechs_uhr] == (0.48, 0.21)


def test_worst_case_kommt_aus_p10_nicht_aus_faktor():
    hass = MagicMock()
    hass.states.async_all.return_value = [_solcast_state(NOW, SOLCAST_TAG)]
    stamps = sched._grid_timestamps(NOW, hours=2)

    erwartung, p10 = sched._production_from_detailed(sched._solcast_detailed(hass), stamps)

    assert len(erwartung) == len(p10) == len(stamps)
    # p10 liegt überall unter dem Erwartungswert, wo überhaupt Leistung anliegt
    assert all(a <= b for a, b in zip(p10, erwartung))
    assert any(a < b for a, b in zip(p10, erwartung))
    # und es ist wirklich der p10 der Prognose, nicht 60 % davon
    index_0700 = next(i for i, t in enumerate(stamps) if (t.hour, t.minute) == (7, 0))
    assert p10[index_0700] == pytest.approx(0.98)


async def test_solcast_hat_vorrang_vor_der_energy_plattform():
    hass = _hass_with(BASE_CONFIG)
    hass.states.async_all.return_value = [_solcast_state(NOW, SOLCAST_TAG)]
    energy = AsyncMock(return_value=_wh_hours(NOW))

    with (
        patch.object(sched, "_now_local", return_value=NOW),
        patch.object(sched, "_async_solar_forecast_wh", energy),
    ):
        inputs, problem = await sched.async_collect_inputs(hass, "entry1")

    assert problem is None
    assert inputs.min_production_kw is not None, "p10 muss übernommen werden"
    assert "detailedForecast" in inputs.forecast_source
    energy.assert_not_awaited()


# ---------------------------------------------------------------------------
# Grenzleistungen
# ---------------------------------------------------------------------------


async def _collect(config: dict):
    hass = _hass_with(config)
    with (
        patch.object(sched, "_now_local", return_value=NOW),
        patch.object(
            sched, "_async_solar_forecast_wh", AsyncMock(return_value=_wh_hours(NOW))
        ),
    ):
        return await sched.async_collect_inputs(hass, "entry1")


async def test_ac_grenze_hat_drei_stufen():
    """Konfigurierter Parameter, dann Altschlüssel, dann PV-Spitzenleistung."""
    inputs, _ = await _collect({**BASE_CONFIG, "pv_peak_kwp": 8.4})
    assert inputs.ac_limit_kw == pytest.approx(8.4)

    inputs, _ = await _collect(
        {**BASE_CONFIG, "pv_peak_kwp": 8.4, "schedule_ac_limit_kw": 6.0}
    )
    assert inputs.ac_limit_kw == pytest.approx(6.0)

    inputs, _ = await _collect(
        {
            **BASE_CONFIG,
            "pv_peak_kwp": 8.4,
            "schedule_ac_limit_kw": 6.0,
            "inverter_ac_limit_kw": 10.0,
        }
    )
    assert inputs.ac_limit_kw == pytest.approx(10.0)

    # Leeres Feld im Panel wird zu 0 — darf nicht als Grenze gelten
    inputs, _ = await _collect(
        {**BASE_CONFIG, "pv_peak_kwp": 8.4, "inverter_ac_limit_kw": 0}
    )
    assert inputs.ac_limit_kw == pytest.approx(8.4)


async def test_einspeisegrenze_gilt_nur_wenn_eingeschaltet():
    """Der konfigurierte Wert darf den Fahrplan nicht fesseln, solange die
    Einspeisegrenze aus ist — sonst deckelt er den Export auf 4 kW."""
    aus, _ = await _collect(
        {**BASE_CONFIG, "pv_peak_kwp": 10.0, "grid_export_limit_kw": 4.0}
    )
    assert aus.feedin_limit_kw == pytest.approx(9.5)

    ein, _ = await _collect(
        {
            **BASE_CONFIG,
            "pv_peak_kwp": 10.0,
            "grid_export_limit_kw": 4.0,
            "grid_export_limit_enabled": True,
        }
    )
    assert ein.feedin_limit_kw == pytest.approx(4.0)


async def test_alter_einspeisebegrenzungs_schluessel_wirkt_nicht_mehr():
    """enable_feedin_limit gehörte zum alten Einspeisebegrenzungs-Regler und
    bleibt in entry.data liegen (Rückwechsel-Garantie) — den Fahrplan darf
    er nicht mehr beeinflussen."""
    inputs, _ = await _collect(
        {
            **BASE_CONFIG,
            "pv_peak_kwp": 10.0,
            "enable_feedin_limit": True,
            "feedin_limit_kw": 3.0,
        }
    )
    assert inputs.feedin_limit_kw == pytest.approx(9.5)


async def test_notstromparameter_wirken_nicht_mehr():
    """Ein Regler statt drei: die getrennte Reserve ist entfallen. Die Felder
    gibt es nicht mehr, und HAConfig setzt den Reserve-DECKEL fest auf null —
    ein Altwert in der Konfiguration kann also nicht weiterwirken.

    Das Vorschau-FENSTER ist davon unberührt: es steht fest auf 18 Stunden
    (siehe test_vorschaufenster_steht_auf_18_stunden) und lässt sich ebenfalls
    nicht mehr konfigurieren.
    """
    inputs, _ = await _collect({**BASE_CONFIG, "schedule_min_soc_pct": 10})
    assert not hasattr(inputs, "blackout_reserve_kwh")
    assert not hasattr(inputs, "blackout_hours")
    assert inputs.min_soc_pct == pytest.approx(10.0)

    inputs, _ = await _collect({
        **BASE_CONFIG,
        "schedule_min_soc_pct": 10,
        "schedule_blackout_reserve_kwh": 9.0,
        "schedule_blackout_hours": 3,
    })
    assert inputs.min_soc_pct == pytest.approx(10.0)
    hacfg = sched.HAConfig(inputs)
    assert hacfg.max_blackout_reserve == pytest.approx(0.0)
    # Der Altwert 3 h wirkt NICHT — das Fenster ist hartkodiert.
    assert hacfg.blackout_time == "18h"


async def test_vorschaufenster_steht_auf_18_stunden():
    """Das Fenster der dynamischen Reserve ist 18 h, nicht ein Slot.

    Bis 1.5.27 stand hier ein einziger Slot, begründet damit, die Reserve
    falle „in jedem Slot auf null". Das galt nur für den sonnigen Tag, an dem
    es geprüft worden war. An wechselhaften Tagen hält der Fahrplan mit dem
    18-Stunden-Fenster deutlich mehr im Speicher — bei unverändertem Erlös
    und Netzbezug (Messung an echten Anlagendaten, siehe schedule.py).
    """
    inputs, _ = await _collect(BASE_CONFIG)
    hacfg = sched.HAConfig(inputs)

    assert hacfg.blackout_time == sched.BLACKOUT_LOOKAHEAD == "18h"
    assert hacfg.blackout_time != f"{int(inputs.time_res_s)}s"


async def test_kombinierter_batteriezustand_wird_wirklich_gelesen():
    """``has_combined_battery_state`` ist eine PROPERTY, keine Methode.

    Bis 1.5.50 stand in async_collect_inputs ein Aufruf mit Klammern. Bei
    genau den Treibern, die True melden (SolarEdge, Huawei Master/Slave),
    warf das „'bool' object is not callable"; der except-Zweig schluckte den
    Fehler still, und statt des kapazitätsgewichteten Zustands landete immer
    der Sensor-Fallback im Fahrplan. Der Test baut die Attrappe deshalb wie
    die echten Treiber — Property, nicht Methode.
    """
    class _Treiber:
        has_combined_battery_state = True    # Property-Wert, kein Callable

        def get_combined_battery_state(self):
            return 61.0, 24.0                 # zwei Batterien, gewichtet

    hass = _hass_with(BASE_CONFIG)
    hass.data[sched.DOMAIN]["entry1"]["inverter"] = _Treiber()
    with (
        patch.object(sched, "_now_local", return_value=NOW),
        patch.object(
            sched, "_async_solar_forecast_wh", AsyncMock(return_value=_wh_hours(NOW))
        ),
    ):
        inputs, problem = await sched.async_collect_inputs(hass, "entry1")

    assert problem is None
    # Der kombinierte Zustand gewinnt gegen den Sensor (40 % / 12,5 kWh).
    assert inputs.soc_pct == pytest.approx(61.0)
    assert inputs.battery_capacity_kwh == pytest.approx(24.0)


async def test_backup_ladestand_des_geraets_hebt_die_untergrenze():
    """Der Wechselrichter hält seinen Backup-Ladestand hardwareseitig zurück —
    planen wir darunter, verweigert das Gerät. Der höhere Wert gewinnt."""
    # Gerät reserviert 20 %, konfiguriert sind 8 %
    inverter = MagicMock()
    inverter.has_combined_battery_state = None
    inverter.get_backup_reserve_soc_pct = MagicMock(return_value=20.0)

    hass = _hass_with({**BASE_CONFIG, "schedule_min_soc_pct": 8})
    hass.data[sched.DOMAIN]["entry1"]["inverter"] = inverter
    with (
        patch.object(sched, "_now_local", return_value=NOW),
        patch.object(
            sched, "_async_solar_forecast_wh", AsyncMock(return_value=_wh_hours(NOW))
        ),
    ):
        inputs, problem = await sched.async_collect_inputs(hass, "entry1")

    assert problem is None
    assert inputs.min_soc_pct == pytest.approx(20.0)

    # Konfigurierter Ladestand über dem des Geräts → Konfiguration gewinnt
    inverter.get_backup_reserve_soc_pct = MagicMock(return_value=4.0)
    hass = _hass_with({**BASE_CONFIG, "schedule_min_soc_pct": 8})
    hass.data[sched.DOMAIN]["entry1"]["inverter"] = inverter
    with (
        patch.object(sched, "_now_local", return_value=NOW),
        patch.object(
            sched, "_async_solar_forecast_wh", AsyncMock(return_value=_wh_hours(NOW))
        ),
    ):
        inputs, _ = await sched.async_collect_inputs(hass, "entry1")
    assert inputs.min_soc_pct == pytest.approx(8.0)
    # Und in keinem Fall entsteht daraus eine getrennte Reserve
    assert sched.HAConfig(inputs).max_blackout_reserve == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Preise: zwei Einspeisetarife und Bezugspreis
# ---------------------------------------------------------------------------


def test_nachtfenster_geht_ueber_mitternacht():
    assert sched._ist_im_nachtfenster(23, 22, 6)
    assert sched._ist_im_nachtfenster(3, 22, 6)
    assert not sched._ist_im_nachtfenster(12, 22, 6)
    assert not sched._ist_im_nachtfenster(6, 22, 6)
    # Fenster innerhalb eines Tages
    assert sched._ist_im_nachtfenster(19, 18, 22)
    assert not sched._ist_im_nachtfenster(23, 18, 22)
    # Leeres Fenster
    assert not sched._ist_im_nachtfenster(5, 6, 6)


def test_zeitangaben_werden_tolerant_gelesen():
    assert sched._stunde_aus_zeit("22:00", 0) == 22
    assert sched._stunde_aus_zeit("6:30", 0) == 6
    assert sched._stunde_aus_zeit(20, 0) == 20
    assert sched._stunde_aus_zeit("", 22) == 22
    assert sched._stunde_aus_zeit(None, 22) == 22
    assert sched._stunde_aus_zeit("Unsinn", 22) == 22


def test_ein_tarif_bleibt_ein_skalar():
    pytest.importorskip("pandas")
    config = sched.HAConfig(_inputs_for_solve(horizon_hours=6))
    assert config.feedin_price(NOW) == pytest.approx(0.0973)


def test_zwei_tarife_werden_zur_zeitreihe():
    """8,2 ct tags, 10,2 ct nachts — genau Roberts Tarifmodell."""
    pytest.importorskip("pandas")

    inputs = _inputs_for_solve(horizon_hours=24)
    inputs.feedin_price = 0.082
    inputs.feedin_price_night = 0.102
    inputs.night_start_hour = 22
    inputs.night_end_hour = 6

    reihe = sched.HAConfig(inputs).feedin_price(inputs.start)

    werte = {stamp.hour: preis for stamp, preis in reihe.items()}
    assert werte[23] == pytest.approx(0.102)
    assert werte[3] == pytest.approx(0.102)
    assert werte[12] == pytest.approx(0.082)


def test_gleiche_tarife_bleiben_skalar():
    """Kein Umweg über eine Zeitreihe, wenn Tag und Nacht gleich sind."""
    pytest.importorskip("pandas")
    inputs = _inputs_for_solve(horizon_hours=6)
    inputs.feedin_price_night = inputs.feedin_price
    assert sched.HAConfig(inputs).feedin_price(inputs.start) == pytest.approx(
        inputs.feedin_price
    )


async def test_bezugspreis_direkt_oder_aus_grid_fee():
    # Ohne Angabe: Einspeisung + grid_fee, wie bei Harald
    inputs, _ = await _collect({**BASE_CONFIG, "schedule_feedin_price": 0.082})
    assert inputs.consumption_price == pytest.approx(0.082 + 0.1647)

    # Direkt gesetzt hat Vorrang
    inputs, _ = await _collect(
        {
            **BASE_CONFIG,
            "schedule_feedin_price": 0.082,
            "schedule_consumption_price": 0.22,
        }
    )
    assert inputs.consumption_price == pytest.approx(0.22)


async def test_standardverguetung_nachtsatz_wird_gelesen():
    """Der Nachtsatz der Standardvergütung wirkt bei Quelle „Fester Wert".

    Seit 1.5.42 bietet das Panel ihn wieder an — mancher Einspeisevertrag
    vergütet nachts anders, auch ganz ohne Gemeinschaft. Das Nachtfenster
    bestimmt, wann er gilt.
    """
    inputs, _ = await _collect(
        {
            **BASE_CONFIG,
            "schedule_feedin_price": 0.082,
            "schedule_feedin_price_night": 0.102,
            "schedule_night_start": "21:30",
            "schedule_night_end": "05:00",
        }
    )
    assert inputs.feedin_price == pytest.approx(0.082)
    assert inputs.feedin_price_night == pytest.approx(0.102)
    assert inputs.night_start_hour == 21
    assert inputs.night_end_hour == 5


def test_nachttarif_verschiebt_energie_in_die_nacht():
    """Der Nachtbonus muss im Fahrplan tatsächlich ankommen."""
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")

    def nacht_export(nachtpreis):
        inputs = _inputs_for_solve()
        inputs.feedin_price = 0.082
        inputs.feedin_price_night = nachtpreis
        inputs.consumption_price = 0.247
        slots = sched.solve(inputs)["slots"]
        return sum(
            s["grid_p"]
            for s in slots
            if s["grid_p"] > 0
            and sched._ist_im_nachtfenster(datetime.fromisoformat(s["t"]).hour, 22, 6)
        ) * 0.25

    ohne = nacht_export(0.082)
    mit = nacht_export(0.102)
    assert mit > ohne + 1.0, f"Nachtbonus wirkt nicht: {ohne:.1f} -> {mit:.1f} kWh"


async def test_leere_zahlenfelder_fallen_auf_defaults_zurueck():
    """Das Panel schickt für ein leeres Zahlenfeld eine 0."""
    inputs, problem = await _collect(
        {
            **BASE_CONFIG,
            "schedule_time_res_min": 0,    # wird ignoriert, Auflösung ist fix
            "schedule_horizon_hours": 0,   # wird ignoriert, Horizont ist fix
            "schedule_feedin_price_night": 0,
            "schedule_consumption_price": 0,
            "schedule_feedin_price": 0,
            "discharge_power_kw": 0,
        }
    )

    assert problem is None
    assert inputs.time_res_s == 900               # nicht 0
    assert len(inputs.timestamps) > 24            # Horizont nicht leer
    assert inputs.feedin_price_night is None      # kein Nachttarif
    assert inputs.consumption_price == pytest.approx(0.082 + 0.1647)
    # Tagestarif 0 wäre einspeisefeindlich, Leistungsgrenze 0 legte die
    # Batterie still — beide fallen auf den Default zurück.
    assert inputs.feedin_price == pytest.approx(0.082)
    assert inputs.battery_power_limit_kw == pytest.approx(5.0)


async def test_defaults_bringen_einen_tarif_und_das_nachtfenster():
    """Ohne jede Preis-Konfiguration gelten 8,2 ct rund um die Uhr, 22-06 Uhr."""
    inputs, problem = await _collect(BASE_CONFIG)

    assert problem is None
    assert inputs.feedin_price == pytest.approx(0.082)
    assert inputs.feedin_price_night is None
    # Vorgabe des Nachtfensters seit 1.5.54: 20:00-06:00 (Nutzerentscheid,
    # deckt die Abendspitze der Gemeinschaften mit ab).
    assert inputs.night_start_hour == 20
    assert inputs.night_end_hour == 6
    assert inputs.consumption_price == pytest.approx(0.2467)


# ---------------------------------------------------------------------------
# Erster Stützpunkt: Messwerte statt Prognose
# ---------------------------------------------------------------------------


async def test_erster_stuetzpunkt_nutzt_messwerte():
    """PV und Hauslast des ersten Punkts kommen aus der Messung — für die
    nächsten Minuten ist die aktuelle Messung der beste Schätzer, und nur der
    erste Slot wird gefahren."""
    hass = _hass_with(BASE_CONFIG)

    with (
        patch.object(sched, "_now_local", return_value=NOW),
        patch.object(
            sched, "_async_solar_forecast_wh", AsyncMock(return_value=_wh_hours(NOW))
        ),
        patch.object(sched, "compute_pv_now_kw", return_value=2.345),
        patch.object(sched, "compute_house_load_kw", return_value=0.789),
    ):
        inputs, problem = await sched.async_collect_inputs(hass, "entry1")

    assert problem is None
    assert inputs.production_kw[0] == pytest.approx(2.345)
    assert inputs.consumption_kw[0] == pytest.approx(0.789)
    # Die späteren Stützpunkte bleiben Prognose (Profil: 400 W überall)
    assert inputs.consumption_kw[1] == pytest.approx(0.4)
    # 05:07 → nächster Stützpunkt 05:30, vor Sonnenaufgang also 0
    assert inputs.production_kw[1] == pytest.approx(0.0)


async def test_erster_stuetzpunkt_ueberschreibt_auch_den_worst_case():
    """Mit Solcast-p10 muss auch min_production[0] die Messung übernehmen —
    zum Messzeitpunkt gibt es keine Prognoseunsicherheit."""
    hass = _hass_with(BASE_CONFIG)
    hass.states.async_all.return_value = [_solcast_state(NOW, SOLCAST_TAG)]

    with (
        patch.object(sched, "_now_local", return_value=NOW),
        patch.object(sched, "compute_pv_now_kw", return_value=1.5),
        patch.object(sched, "compute_house_load_kw", return_value=None),
    ):
        inputs, problem = await sched.async_collect_inputs(hass, "entry1")

    assert problem is None
    assert inputs.production_kw[0] == pytest.approx(1.5)
    assert inputs.min_production_kw[0] == pytest.approx(1.5)
    # p10 der Folge-Stützpunkte bleibt aus der Prognose (05:30 → 0.05)
    assert inputs.min_production_kw[1] == pytest.approx(0.05)


async def test_nicht_lesbare_messwerte_lassen_die_prognose_stehen():
    """Fail-open: ohne Messwert rechnet der Fahrplan wie bisher mit der
    Prognose — er darf nicht an fehlenden Power-Sensoren scheitern."""
    hass = _hass_with(BASE_CONFIG)

    with (
        patch.object(sched, "_now_local", return_value=NOW),
        patch.object(
            sched, "_async_solar_forecast_wh", AsyncMock(return_value=_wh_hours(NOW))
        ),
        patch.object(sched, "compute_pv_now_kw", return_value=None),
        patch.object(sched, "compute_house_load_kw", return_value=None),
    ):
        inputs, problem = await sched.async_collect_inputs(hass, "entry1")

    assert problem is None
    assert inputs.consumption_kw[0] == pytest.approx(0.4)   # Profilwert
    assert inputs.production_kw[0] == pytest.approx(0.0)    # Prognose 05:07


# ---------------------------------------------------------------------------
# slot_for: gemeinsamer Slot-Lookup für Sensoren und Executor
# ---------------------------------------------------------------------------


def _slots_ab(start: datetime, minuten: int = 15, anzahl: int = 4) -> list[dict]:
    return [
        {"t": (start + timedelta(minutes=i * minuten)).isoformat(), "nr": i}
        for i in range(anzahl)
    ]


def test_slot_for_nimmt_den_laufenden_slot():
    slots = _slots_ab(NOW.replace(minute=0))
    # 05:07 liegt im Slot 05:00 (Slot 0), 05:31 im Slot 05:30 (Slot 2)
    assert sched.slot_for(slots, NOW)["nr"] == 0
    assert sched.slot_for(slots, NOW.replace(minute=31))["nr"] == 2
    # Nach dem letzten Slot-Start bleibt der letzte Slot der laufende
    assert sched.slot_for(slots, NOW + timedelta(hours=3))["nr"] == 3


def test_slot_for_vor_dem_ersten_slot_gibt_none():
    slots = _slots_ab(NOW.replace(minute=30))
    assert sched.slot_for(slots, NOW) is None


def test_slot_for_uebersteht_kaputte_und_leere_slots():
    assert sched.slot_for(None, NOW) is None
    assert sched.slot_for([], NOW) is None
    slots = [{"kein_t": True}, {"t": "Unsinn"}, {"t": NOW.isoformat(), "nr": 99}]
    assert sched.slot_for(slots, NOW)["nr"] == 99


async def test_start_ist_die_aktuelle_minute():
    """Der Fahrplan wird minütlich gerechnet, also muss er auch minütlich
    beginnen — sonst gilt der Ladestand für einen Zeitpunkt in der
    Vergangenheit und der erste Slot rechnet falsch."""
    krumm = NOW.replace(minute=7, second=41, microsecond=500)

    with (
        patch.object(sched, "_now_local", return_value=krumm),
        patch.object(
            sched, "_async_solar_forecast_wh", AsyncMock(return_value=_wh_hours(NOW))
        ),
    ):
        inputs, _ = await sched.async_collect_inputs(_hass_with(BASE_CONFIG), "entry1")

    assert inputs.start == krumm.replace(second=0, microsecond=0)
    assert inputs.timestamps[0] == inputs.start


# ---------------------------------------------------------------------------
# EEG-Preisfunktion im Zusammenspiel mit dem Fahrplan
# ---------------------------------------------------------------------------


def _bonus_fuer_stunden(inputs, stunden, hoehe):
    """Aufschlag nur in den genannten Ortsstunden — wie es die Bedarfsprognose
    einer Gemeinschaft erzeugt (dort ist der Bedarf konzentriert)."""
    return [hoehe if stamp.hour in stunden else 0.0 for stamp in inputs.timestamps]


def test_feedin_price_addiert_den_eeg_aufschlag():
    pytest.importorskip("pandas")
    inputs = _inputs_for_solve(horizon_hours=6)
    bonus = _bonus_fuer_stunden(inputs, {7}, 0.03)
    config = sched.HAConfig(dataclasses.replace(inputs, eeg_bonus=bonus))

    reihe = config.feedin_price(inputs.start)

    # Ohne Bedarf der Basistarif, in der Bedarfsstunde plus Aufschlag
    assert reihe.min() == pytest.approx(inputs.feedin_price)
    assert reihe.max() == pytest.approx(inputs.feedin_price + 0.03)
    treffer = [v for stamp, v in reihe.items() if stamp.hour == 7]
    assert treffer and all(v == pytest.approx(inputs.feedin_price + 0.03) for v in treffer)


def test_feedin_price_bleibt_ohne_aufschlag_ein_skalar():
    """Kein Nachttarif, keine Gemeinschaft: dann bleibt es ein einziger Wert —
    das erspart opt() eine Zeitreihe."""
    inputs = _inputs_for_solve(horizon_hours=6)
    config = sched.HAConfig(dataclasses.replace(inputs, eeg_bonus=[0.0] * len(inputs.timestamps)))

    assert config.feedin_price(inputs.start) == pytest.approx(inputs.feedin_price)


def test_feedin_price_wird_unter_dem_bezugspreis_gedeckelt():
    """Über dem Bezugspreis kauft das LP Strom, um ihn teurer zu verkaufen
    (gemessen: 5,49 kW Kauf gegen 9,40 kW Verkauf bei 0,60 kW Hauslast)."""
    pytest.importorskip("pandas")
    inputs = _inputs_for_solve(horizon_hours=6)
    # Aufschlag absichtlich absurd hoch — so wirkt eine Fehlkonfiguration.
    bonus = _bonus_fuer_stunden(inputs, {7}, 0.5)
    config = sched.HAConfig(dataclasses.replace(inputs, eeg_bonus=bonus))

    reihe = config.feedin_price(inputs.start)

    assert reihe.max() < inputs.consumption_price
    assert reihe.max() == pytest.approx(
        inputs.consumption_price - sched.eeg_price.DECKEL_ABSTAND
    )


def test_aufschlag_verschiebt_die_einspeisung_in_die_bedarfsstunden():
    """Der eigentliche Zweck: mit Aufschlag landet mehr Einspeisung dort, wo
    die Gemeinschaft Bedarf hat — und der Gesamtexport bleibt praktisch gleich.
    """
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")

    inputs = _inputs_for_solve(horizon_hours=36)
    bedarfsstunden = {18, 19, 20}

    def export_in_bedarfsstunden(bonus):
        ergebnis = sched.solve(dataclasses.replace(inputs, eeg_bonus=bonus))
        gesamt = sum(max(0.0, s["grid_p"]) for s in ergebnis["slots"])
        gezielt = sum(
            max(0.0, s["grid_p"])
            for s in ergebnis["slots"]
            if int(s["t"][11:13]) in bedarfsstunden
        )
        return gesamt, gezielt

    ohne_gesamt, ohne_gezielt = export_in_bedarfsstunden(None)
    mit_gesamt, mit_gezielt = export_in_bedarfsstunden(
        _bonus_fuer_stunden(inputs, bedarfsstunden, 0.02)
    )

    assert mit_gezielt > ohne_gezielt * 1.5, (
        f"Aufschlag wirkt nicht: {ohne_gezielt:.2f} -> {mit_gezielt:.2f} kW"
    )
    # Umverteilung, nicht Verzicht: der Gesamtexport darf kaum sinken.
    assert mit_gesamt > ohne_gesamt * 0.97


# ---------------------------------------------------------------------------
# Basistarif von der Strombörse (Spot)
# ---------------------------------------------------------------------------


class _FakeSpot:
    def __init__(self, werte):
        self._werte = werte

    def reihe_fuer(self, stamps):
        if self._werte is None:
            return None, 0
        return [self._werte] * len(stamps), 3


async def _collect_spot(config, spot):
    hass = _hass_with(config)
    hass.data[sched.DOMAIN]["entry1"]["spot"] = spot
    with (
        patch.object(sched, "_now_local", return_value=NOW),
        patch.object(
            sched, "_async_solar_forecast_wh", AsyncMock(return_value=_wh_hours(NOW))
        ),
    ):
        return await sched.async_collect_inputs(hass, "entry1")


async def test_spotquelle_liefert_preisreihe_mit_abschlag():
    inputs, _ = await _collect_spot(
        {
            **BASE_CONFIG,
            "schedule_feedin_source": "spot",
            "spot_feedin_fee": 0.015,
        },
        _FakeSpot(0.09),
    )
    assert inputs.feedin_price_series is not None
    assert all(p == pytest.approx(0.075) for p in inputs.feedin_price_series)
    assert inputs.feedin_series_extrapolated == 3
    # Kein Nachtsatz bei Spot; der Skalar trägt das Reihenmittel.
    assert inputs.feedin_price_night is None
    assert inputs.feedin_price == pytest.approx(0.075)


async def test_spotquelle_ohne_daten_faellt_auf_handeingabe_zurueck():
    inputs, _ = await _collect_spot(
        {
            **BASE_CONFIG,
            "schedule_feedin_source": "spot",
            "schedule_feedin_price": 0.082,
        },
        _FakeSpot(None),
    )
    assert inputs.feedin_price_series is None
    assert inputs.feedin_price == pytest.approx(0.082)


def test_negative_spotpreise_verhindern_die_einspeisung():
    """Bei echt negativem Preis speist der Fahrplan nicht ein."""
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")

    inputs = _inputs_for_solve()
    inputs.consumption_price = 0.247
    # Erste sechs Stunden negativ, danach ordentlich vergütet.
    inputs.feedin_price_series = [
        -0.02 if i < 6 else 0.09 for i in range(len(inputs.timestamps))
    ]
    slots = sched.solve(inputs)["slots"]
    negativ = [s for s in slots if s["feedin_price"] is not None and s["feedin_price"] < 0]
    assert negativ, "kein Slot mit negativem Preis im Fahrplan"
    assert all(s["grid_p"] <= 0.001 for s in negativ), (
        "der Fahrplan speist trotz negativem Börsenpreis ein"
    )

# ---------------------------------------------------------------------------
# Zeitumstellung
# ---------------------------------------------------------------------------


def _wien():
    from zoneinfo import ZoneInfo

    return ZoneInfo("Europe/Vienna")


def test_zeitraster_ueberlebt_die_fruehjahrs_umstellung():
    """Wanduhr-Arithmetik erzeugte an der Umstellung doppelte Zeitpunkte.

    Ortszeit + timedelta rechnet den Offset nicht mit: 02:00 (CET, die
    Stunde gibt es nicht) und 03:00 (CEST) sind derselbe UTC-Zeitpunkt. Die
    doppelten Labels ließen ``resample()`` in ``opt()`` abbrechen — zwei
    Tage lang (Horizont 48 h) gar kein Fahrplan, danach Failsafe.
    """
    start = datetime(2027, 3, 27, 21, 0, tzinfo=_wien())
    stamps = sched._grid_timestamps(start, hours=48)

    epochen = [s.timestamp() for s in stamps]
    assert len(set(epochen)) == len(epochen), "doppelte Zeitpunkte im Raster"
    assert epochen == sorted(epochen), "Raster nicht monoton"
    # Die Umstellungsnacht ist wirklich enthalten und überspringt 02:00–03:00.
    stunden = {s.astimezone(_wien()).strftime("%d.%m %H:%M") for s in stamps}
    assert "28.03 01:30" in stunden
    assert "28.03 02:00" not in stunden and "28.03 02:30" not in stunden
    assert "28.03 03:00" in stunden


def test_zeitraster_ueberlebt_die_herbst_umstellung():
    """Im Herbst gab es keinen Absturz, aber einen 90-Minuten-Sprung: die
    doppelte Stunde fehlte als Stützpunkt, ``opt()`` lieferte 197 statt 193
    Slots. Über die Epoche geschritten liegt zwischen allen Punkten genau
    ein Schritt."""
    start = datetime(2026, 10, 24, 22, 0, tzinfo=_wien())
    stamps = sched._grid_timestamps(start, hours=48)

    abstaende = {
        round(b.timestamp() - a.timestamp())
        for a, b in zip(stamps[1:], stamps[2:])
    }
    assert abstaende == {sched.GRID_STEP_MIN * 60}, f"Sprünge im Raster: {abstaende}"
    # Die doppelte Stunde kommt zweimal vor — als zwei echte Zeitpunkte.
    zweimal = [s for s in stamps if s.strftime("%d.%m %H:%M") == "25.10 02:00"]
    assert len(zweimal) == 2
    assert zweimal[0].utcoffset() != zweimal[1].utcoffset()


def test_solve_rechnet_ueber_die_umstellung_hinweg():
    """Der ganze Weg: an der Frühjahrs-Umstellung muss ein Plan entstehen."""
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")

    start = datetime(2027, 3, 27, 21, 0, tzinfo=_wien())
    stamps = sched._grid_timestamps(start, hours=36)
    inputs = dataclasses.replace(
        _inputs_for_solve(),
        start=start,
        timestamps=stamps,
        consumption_kw=sched._consumption_from_profile(
            _profile_coordinator(600), stamps),
        production_kw=sched._production_from_wh(_wh_hours(start, 40), stamps),
        min_production_kw=None,
    )

    result = sched.solve(inputs)

    assert result["slots"], "an der Zeitumstellung entstand kein Fahrplan"
