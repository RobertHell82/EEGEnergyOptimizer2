"""Tests für den Planungshorizont bei kurzer PV-Prognose.

Solcast liefert über die Tagessensoren eine Woche, Forecast.Solar je nach
Zugang nur bis zum Ende des morgigen Tages. Der Horizont zählt aber ab jetzt,
also fehlt abends der halbe übernächste Tag — und fehlende Stunden kamen als
0 kW an. Für ``opt()`` ist das kein "unbekannt", sondern "hier scheint
garantiert keine Sonne": der Plan hielt die Batterie für den vermeintlich
dunklen Tag zurück.

Geprüft wird, dass der Horizont genau am Prognoseende endet — auch nicht
eine Stunde weiter. Die Nacht danach mitzunehmen ist naheliegend und
gemessen schädlich, siehe ``_horizont_aus_wh_hours``.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.eeg_energy_optimizer import schedule as sched

TZ = timezone(timedelta(hours=2))  # Sommerzeit Wien
SONNENAUFGANG = 6
SONNENUNTERGANG = 20


def _wh_bis(start: datetime, ende: datetime) -> dict[str, float]:
    """Stündliche Prognose von ``start`` bis ``ende`` (exklusiv).

    Wie ``async_get_solar_forecast`` sie liefert: Tagesgang 6–20 Uhr, die
    Nachtstunden stehen mit 0.0 drin.
    """
    result: dict[str, float] = {}
    cursor = start.replace(minute=0, second=0, microsecond=0)
    while cursor < ende:
        if cursor.hour < SONNENAUFGANG or cursor.hour > SONNENUNTERGANG:
            wh = 0.0
        else:
            wh = 6000.0 * (1 - abs(13 - cursor.hour) / 8)
        result[cursor.isoformat()] = round(max(0.0, wh), 1)
        cursor += timedelta(hours=1)
    return result


def _forecast_solar_reichweite(jetzt: datetime) -> dict[str, float]:
    """Was Forecast.Solar liefert: heute und morgen, dann ist Schluss."""
    heute = jetzt.replace(hour=0, minute=0, second=0, microsecond=0)
    return _wh_bis(heute, heute + timedelta(days=2))


# ---------------------------------------------------------------------------
# Die Rechnung selbst
# ---------------------------------------------------------------------------


def test_volle_prognose_behaelt_die_achtundvierzig_stunden():
    """Reicht die Prognose weit genug, bleibt alles wie bisher."""
    jetzt = datetime(2026, 8, 24, 20, 7, tzinfo=TZ)
    wh = _wh_bis(jetzt, jetzt + timedelta(days=4))

    assert sched._horizont_aus_wh_hours(wh, jetzt) == sched.DEFAULT_HORIZON_HOURS


def test_abends_endet_der_horizont_am_prognoseende():
    """Der Fall, um den es geht: 20:07, Prognose nur bis morgen 23:59.

    Ohne Kürzung wären 48 h geplant und die letzten 20 h davon erfunden.
    """
    jetzt = datetime(2026, 8, 24, 20, 7, tzinfo=TZ)          # Mo
    wh = _forecast_solar_reichweite(jetzt)                    # bis Di 23:59

    horizont = sched._horizont_aus_wh_hours(wh, jetzt)

    assert horizont == 27
    assert (jetzt + timedelta(hours=horizont)).day == 25      # noch Dienstag


def test_morgens_bleibt_fast_alles_erhalten():
    """Früh am Tag kostet die Kürzung kaum etwas — das Loch ist dann klein."""
    jetzt = datetime(2026, 8, 24, 6, 30, tzinfo=TZ)
    wh = _forecast_solar_reichweite(jetzt)

    # Prognoseende Mi 00:00, also 41,5 h → 41
    assert sched._horizont_aus_wh_hours(wh, jetzt) == 41


def test_die_nacht_nach_der_prognose_zaehlt_nicht_mit():
    """Naheliegend und gemessen falsch — deshalb festgehalten.

    Nachts ist die 0 zwar eine Tatsache, aber der letzte Slot ist auf halben
    Ladestand festgenagelt (opt_highs.py). Liegt dieser Nagel hinter einer
    Nacht, muss der Plan sie mit Reserve durchqueren. Gemessen fiel der
    Export der ersten 24 h von 16,25 auf 12,98 kWh (PV x0,5) bzw. von 2,01
    auf 0,00 kWh (PV x0,25), sobald die Nacht mitgeplant wurde.
    """
    jetzt = datetime(2026, 8, 24, 20, 7, tzinfo=TZ)
    wh = _forecast_solar_reichweite(jetzt)

    horizont = sched._horizont_aus_wh_hours(wh, jetzt)
    ende = jetzt + timedelta(hours=horizont)

    prognose_ende = datetime(2026, 8, 26, 0, 0, tzinfo=TZ)
    assert ende < prognose_ende


def test_prognose_ohne_nachtstunden_endet_am_letzten_wert():
    """Manche Quellen liefern nur die Stunden mit Produktion.

    Dann endet die Reihe morgen um 20:00 — und dort endet auch der Plan.
    """
    jetzt = datetime(2026, 8, 24, 20, 7, tzinfo=TZ)
    voll = _forecast_solar_reichweite(jetzt)
    nur_sonne = {k: v for k, v in voll.items() if v > 0.0}

    horizont = sched._horizont_aus_wh_hours(nur_sonne, jetzt)

    ende = jetzt + timedelta(hours=horizont)
    assert ende.day == 25 and ende.hour <= SONNENUNTERGANG


def test_runde_startminute_legt_keinen_slot_hinter_die_prognose():
    """Randfall: geht die Rechnung exakt auf, liegt ein Slot auf der Grenze.

    Um 20:00 wären es genau 28 Stunden bis Mi 00:00 — der letzte Slot läge
    dann auf einer Stunde, für die es keinen Prognosewert mehr gibt.
    """
    jetzt = datetime(2026, 8, 24, 20, 0, tzinfo=TZ)
    wh = _forecast_solar_reichweite(jetzt)

    horizont = sched._horizont_aus_wh_hours(wh, jetzt)

    assert horizont == 27
    assert (jetzt + timedelta(hours=horizont)) < datetime(
        2026, 8, 26, 0, 0, tzinfo=TZ
    )


def test_veraltete_prognose_ergibt_null():
    """Hängt die Prognose-Integration, liegt alles in der Vergangenheit."""
    jetzt = datetime(2026, 8, 24, 20, 7, tzinfo=TZ)
    alt = _wh_bis(jetzt - timedelta(days=3), jetzt - timedelta(days=1))

    assert sched._horizont_aus_wh_hours(alt, jetzt) == 0


def test_horizont_matcht_ueber_zeitpunkt_nicht_ueber_darstellung():
    """Prognose in UTC, Planung lokal — gleiches Ergebnis."""
    jetzt = datetime(2026, 8, 24, 20, 7, tzinfo=TZ)
    lokal = _forecast_solar_reichweite(jetzt)
    in_utc = {
        datetime.fromisoformat(k).astimezone(timezone.utc).isoformat(): v
        for k, v in lokal.items()
    }

    assert sched._horizont_aus_wh_hours(in_utc, jetzt) == sched._horizont_aus_wh_hours(
        lokal, jetzt
    )


def test_unlesbare_zeitstempel_werden_uebergangen():
    """Ein kaputter Schlüssel darf den ganzen Horizont nicht kippen."""
    jetzt = datetime(2026, 8, 24, 20, 7, tzinfo=TZ)
    wh = dict(_forecast_solar_reichweite(jetzt))
    wh["kein Zeitstempel"] = 42.0
    wh["2026-08-25T12:00:00"] = 99.0   # ohne Zeitzone → nicht vergleichbar

    assert sched._horizont_aus_wh_hours(wh, jetzt) == 27


# ---------------------------------------------------------------------------
# Zusammenspiel mit dem Sammeln
# ---------------------------------------------------------------------------


def _profile_coordinator(watts_per_hour: float = 400.0):
    coordinator = MagicMock()
    coordinator.hourly_avg = {
        day: {hour: watts_per_hour for hour in range(24)}
        for day in ("mo", "di", "mi", "do", "fr", "sa", "so")
    }
    coordinator.hourly_for = lambda stamp: watts_per_hour
    return coordinator


def _hass():
    hass = MagicMock()
    hass.data = {
        sched.DOMAIN: {
            "entry1": {
                "config": {
                    "battery_soc_sensor": "sensor.soc",
                    "battery_capacity_kwh": 12.5,
                    "discharge_power_kw": 5.0,
                    "forecast_source": "forecast_solar",
                },
                "coordinator": _profile_coordinator(),
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
    hass.states.async_all.return_value = []   # kein Solcast → Rückfallweg
    return hass


async def test_kein_slot_liegt_jenseits_der_prognose():
    """Die eigentliche Zusage: der Plan erfindet keine dunklen Stunden mehr.

    Vor der Änderung lagen 20 der 48 Stunden jenseits der Prognose und kamen
    als 0 kW an.
    """
    jetzt = datetime(2026, 8, 24, 20, 7, tzinfo=TZ)
    wh = _forecast_solar_reichweite(jetzt)
    prognose_ende = max(datetime.fromisoformat(k) for k in wh) + timedelta(hours=1)

    with (
        patch.object(sched, "_now_local", return_value=jetzt),
        patch.object(sched, "_async_solar_forecast_wh", AsyncMock(return_value=wh)),
    ):
        inputs, problem = await sched.async_collect_inputs(_hass(), "entry1")

    assert problem is None
    assert inputs.timestamps[-1] < prognose_ende
    assert len(inputs.production_kw) == len(inputs.timestamps)


async def test_horizont_steht_in_der_quellenangabe():
    """Die Reichweite gehört in die Quellenangabe, nicht nur ins Log.

    ``forecast_source`` wandert über ``solve()`` ins Plan-Dict und damit in
    den WebSocket-Zustand. Das Panel liest das Feld heute noch nicht — die
    Stelle im Wizard zeigt die *Konfiguration*, nicht den gerechneten Plan.
    Wer den verkürzten Horizont sichtbar machen will, hat die Zahl damit
    bereits an der Hand.
    """
    jetzt = datetime(2026, 8, 24, 20, 7, tzinfo=TZ)

    with (
        patch.object(sched, "_now_local", return_value=jetzt),
        patch.object(
            sched,
            "_async_solar_forecast_wh",
            AsyncMock(return_value=_forecast_solar_reichweite(jetzt)),
        ),
    ):
        inputs, _ = await sched.async_collect_inputs(_hass(), "entry1")

    assert "27 h" in inputs.forecast_source


async def test_veraltete_prognose_gibt_klartext_statt_leerem_plan():
    """Lieber kein Plan (Failsafe greift) als ein Plan aus Nullen."""
    jetzt = datetime(2026, 8, 24, 20, 7, tzinfo=TZ)
    alt = _wh_bis(jetzt - timedelta(days=3), jetzt - timedelta(days=1))

    with (
        patch.object(sched, "_now_local", return_value=jetzt),
        patch.object(sched, "_async_solar_forecast_wh", AsyncMock(return_value=alt)),
    ):
        inputs, problem = await sched.async_collect_inputs(_hass(), "entry1")

    assert inputs is None
    assert "Vergangenheit" in problem
