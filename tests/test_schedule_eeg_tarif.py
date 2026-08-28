"""Tests für den Weg der Tarife in den Fahrplan.

Hier hängt zusammen, was in eeg_price.py und oemag.py einzeln geprüft ist:
Basistarif aus Handeingabe oder OeMAG, Bedarfsprognose je Gemeinschaft, und am
Ende eine Aufschlagsreihe in den ``ScheduleInputs``.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.eeg_energy_optimizer import schedule as sched

TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 8, 25, 5, 7, tzinfo=TZ)   # Montag, krumme Minute

BASE_CONFIG = {
    "battery_soc_sensor": "sensor.soc",
    "battery_capacity_kwh": 12.5,
    "pv_peak_kwp": 8.0,
    "schedule_feedin_price": 0.082,
    "schedule_consumption_price": 0.25,
    "discharge_power_kw": 5.0,
    "enable_peakshare": True,
    "peakshare_community": "BEG",
    "peakshare_share_pct": 100,
    "peakshare_price": 0.102,
    "peakshare_weight": 0,
    "schedule_night_start": "22:00",
    "schedule_night_end": "06:00",
}


def _profile_coordinator(watt=400.0):
    c = MagicMock()
    c.hourly_avg = {"werktag": {h: watt for h in range(24)}}
    c.hourly_for.return_value = watt
    return c


def _wh_hours(start, stunden=60):
    return {
        (start + timedelta(hours=i)).replace(minute=0, second=0, microsecond=0).isoformat():
        (900.0 if 8 <= (start + timedelta(hours=i)).hour <= 16 else 0.0)
        for i in range(stunden)
    }


class _Peakshare:
    """Provider-Attrappe: liefert je Gemeinschaft dieselben Viertelstunden.

    Der Wert einer Ortsstunde gilt fuer alle vier Viertelstunden darin — so
    bleiben die Aussagen der Tests dieselben wie zur Zeit der Stundenwerte.
    """

    def __init__(self, werte_je_ortsstunde, namen=("BEG",)):
        self.werte = werte_je_ortsstunde
        self.namen = namen
        self.abrufe: list[str] = []

    def get_intervals(self, name):
        self.abrufe.append(name)
        if name not in self.namen:
            return []
        basis = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
        intervalle = []
        for tag in range(3):        # drei Tage, damit 48 h gedeckt sind
            for h, v in self.werte.items():
                for viertel in range(4):
                    stempel = basis + timedelta(
                        days=tag, hours=h, minutes=15 * viertel
                    )
                    intervalle.append({
                        "timestamp": stempel.astimezone(timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "saldoKwh": float(v),
                    })
        return intervalle

    def get_warnings(self, name):
        return []


class _Oemag:
    def __init__(self, preis):
        self.preis = preis


def _hass(config, peakshare=None, oemag=None):
    hass = MagicMock()
    hass.data = {
        sched.DOMAIN: {
            "entry1": {
                "config": config,
                "coordinator": _profile_coordinator(),
                "inverter": None,
                "peakshare": peakshare,
                "oemag": oemag,
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
    hass.states.async_all.return_value = []
    return hass


async def _collect(config, peakshare=None, oemag=None):
    hass = _hass(config, peakshare, oemag)
    with (
        patch.object(sched, "_now_local", return_value=NOW),
        patch.object(
            sched, "_async_solar_forecast_wh", AsyncMock(return_value=_wh_hours(NOW))
        ),
    ):
        return await sched.async_collect_inputs(hass, "entry1")


# ---------------------------------------------------------------------------
# Aufschlag landet in den Inputs
# ---------------------------------------------------------------------------


async def test_aufschlag_landet_in_den_inputs():
    """Bedarf um 18 und 19 Uhr — dort und nur dort ein Aufschlag."""
    ps = _Peakshare({18: 100.0, 19: 50.0})
    inputs, problem = await _collect(BASE_CONFIG, peakshare=ps)

    assert problem is None
    assert inputs.eeg_bonus is not None
    paare = list(zip(inputs.timestamps, inputs.eeg_bonus))

    # Volle Differenz zur Spitzenstunde: 10,2 − 8,2 = 2,0 ct bei 100 % Anteil
    um18 = [b for t, b in paare if t.hour == 18]
    assert um18 and all(b == pytest.approx(0.020) for b in um18)
    um19 = [b for t, b in paare if t.hour == 19]
    assert um19 and all(b == pytest.approx(0.010) for b in um19)
    assert all(b == 0.0 for t, b in paare if t.hour not in (18, 19))

    # Kein Horizont mehr noetig: V2 liefert 48 Stunden am Stueck, die
    # Wiederholung des Tagesverlaufs ist damit entfallen.
    assert ps.abrufe == ["BEG"]


async def test_ohne_anteil_kein_aufschlag():
    """Anteil 0 ist die Vorgabe: dann bleibt alles wie vorher."""
    ps = _Peakshare({18: 100.0})
    inputs, _ = await _collect(
        {**BASE_CONFIG, "peakshare_share_pct": 0}, peakshare=ps
    )

    assert inputs.eeg_bonus is None
    assert not ps.abrufe, "ohne Anteil darf gar nicht abgefragt werden"


async def test_ohne_provider_kein_absturz():
    inputs, problem = await _collect(BASE_CONFIG, peakshare=None)
    assert problem is None and inputs.eeg_bonus is None


async def test_zwei_gemeinschaften_werden_beide_abgefragt():
    ps = _Peakshare({18: 100.0}, namen=("BEG", "EEG Pucking"))
    config = {
        **BASE_CONFIG,
        "peakshare_share_pct": 60,
        "peakshare_community_2": "EEG Pucking",
        "peakshare_share_pct_2": 40,
        "peakshare_price_2": 0.102,
        "peakshare_weight_2": 0.01,
    }
    inputs, _ = await _collect(config, peakshare=ps)

    assert ps.abrufe == ["BEG", "EEG Pucking"]
    # 60 % · 2,0 ct + 40 % · 3,0 ct = 1,2 + 1,2 = 2,4 ct
    assert max(inputs.eeg_bonus) == pytest.approx(0.024)
    assert len(inputs.eeg_details) == 2
    assert inputs.eeg_details[1]["max_aufschlag_ct"] == pytest.approx(1.2)


async def test_nachtsatz_wird_gegen_den_tagwert_der_basis_gerechnet():
    """Bedarf um 18 und 23 Uhr; 23 liegt im Nachtfenster (22–06).

    Bezugspunkt ist zu jeder Stunde die Standardvergütung — und die kennt
    keinen Nachtsatz (weder OeMAG noch die üblichen Verträge). Der
    Nachtvorteil der Gemeinschaft zählt deshalb voll gegen den Tagwert.
    """
    ps = _Peakshare({18: 100.0, 23: 100.0})
    inputs, _ = await _collect(
        {**BASE_CONFIG, "peakshare_price_night": 0.142}, peakshare=ps
    )

    paare = list(zip(inputs.timestamps, inputs.eeg_bonus))
    tags = [b for t, b in paare if t.hour == 18]
    nachts = [b for t, b in paare if t.hour == 23]

    assert all(b == pytest.approx(0.020) for b in tags)     # 10,2 − 8,2
    assert all(b == pytest.approx(0.060) for b in nachts)   # 14,2 − 8,2


async def test_nachtsatz_der_basis_ist_nachts_der_bezugspunkt():
    """Hat die Standardvergütung einen Nachtsatz, steht die Gemeinschaft
    nachts gegen diesen — verglichen wird immer, was zum selben Zeitpunkt
    gilt (seit 1.5.42 bietet das Panel den Nachtsatz wieder an)."""
    ps = _Peakshare({23: 100.0})
    inputs, _ = await _collect(
        {
            **BASE_CONFIG,
            "schedule_feedin_price_night": 0.102,
            "peakshare_price_night": 0.142,
        },
        peakshare=ps,
    )

    assert inputs.feedin_price_night == pytest.approx(0.102)
    nachts = [b for t, b in zip(inputs.timestamps, inputs.eeg_bonus) if t.hour == 23]
    assert nachts and all(b == pytest.approx(0.040) for b in nachts)   # 14,2 − 10,2


async def test_ohne_eigenen_nachtsatz_gilt_rund_um_die_uhr_derselbe_aufschlag():
    """Ohne jeden Nachtsatz (Basis wie Gemeinschaft) ist der Aufschlag
    zeitunabhängig — Tag steht gegen Tag, und nachts ändert sich nichts."""
    ps = _Peakshare({12: 100.0, 23: 100.0})
    inputs, _ = await _collect(BASE_CONFIG, peakshare=ps)

    paare = list(zip(inputs.timestamps, inputs.eeg_bonus))
    assert all(b == pytest.approx(0.020) for t, b in paare if t.hour == 12)
    assert all(b == pytest.approx(0.020) for t, b in paare if t.hour == 23)


# ---------------------------------------------------------------------------
# Basistarif aus der OeMAG
# ---------------------------------------------------------------------------


async def test_oemag_ersetzt_den_basistarif():
    inputs, _ = await _collect(
        {**BASE_CONFIG, "schedule_feedin_source": "oemag"},
        peakshare=_Peakshare({18: 100.0}),
        oemag=_Oemag(0.06146),
    )

    assert inputs.feedin_price == pytest.approx(0.06146)
    # Differenz zur Gemeinschaft wächst entsprechend: 10,2 − 6,146 = 4,054 ct
    assert max(inputs.eeg_bonus) == pytest.approx(0.04054)


async def test_oemag_kennt_keinen_nachtsatz():
    """Ein konfigurierter Nachttarif würde Tag und Nacht aus verschiedenen
    Quellen mischen — bei OeMAG entfällt er deshalb."""
    inputs, _ = await _collect(
        {
            **BASE_CONFIG,
            "schedule_feedin_source": "oemag",
            "schedule_feedin_price_night": 0.102,
        },
        oemag=_Oemag(0.06146),
    )

    assert inputs.feedin_price_night is None


async def test_ohne_oemag_wert_gilt_die_handeingabe():
    """Website nicht erreichbar und kein gespeicherter Wert: der Fahrplan muss
    trotzdem rechnen."""
    inputs, problem = await _collect(
        {**BASE_CONFIG, "schedule_feedin_source": "oemag"},
        oemag=_Oemag(None),
    )

    assert problem is None
    assert inputs.feedin_price == pytest.approx(0.082)


async def test_handeingabe_bleibt_die_vorgabe():
    """Ohne gesetzte Quelle darf ein Update nichts verändern."""
    inputs, _ = await _collect(BASE_CONFIG, oemag=_Oemag(0.06146))
    assert inputs.feedin_price == pytest.approx(0.082)


async def test_gemeinschaft_hat_eigenes_nachtfenster():
    """EEG/BEG-Verträge können ein anderes Nachtfenster haben als der
    Einspeisevertrag der Standardvergütung (Nutzerwunsch 27.08.).

    Drei Stunden, drei Kombinationen: 21 Uhr liegt nur im
    Gemeinschafts-Fenster (20–05), 23 Uhr in beiden, 5 Uhr nur im
    Standard-Fenster (22–06). Der Aufschlag ist jeweils die Differenz des
    dort gültigen Gemeinschaftssatzes zum dort gültigen Basistarif.
    """
    ps = _Peakshare({21: 50.0, 23: 50.0, 5: 50.0})
    config = {
        **BASE_CONFIG,
        "schedule_feedin_price_night": 0.06,   # Standard-Nachtsatz, 22–06
        "peakshare_price_night": 0.122,        # Gemeinschafts-Nachtsatz
        "peakshare_night_start": "20:00",      # eigenes Fenster 20–05
        "peakshare_night_end": "05:00",
    }
    inputs, problem = await _collect(config, peakshare=ps)

    assert problem is None
    assert inputs.eeg_night_start_hour == 20
    assert inputs.eeg_night_end_hour == 5
    paare = list(zip(inputs.timestamps, inputs.eeg_bonus))

    # 21 Uhr: Gemeinschaft schon nachts (0,122), Basis noch am Tag (0,082).
    um21 = [b for t, b in paare if t.hour == 21]
    assert um21 and all(b == pytest.approx(0.122 - 0.082) for b in um21)
    # 23 Uhr: beide nachts — 0,122 gegen den Standard-Nachtsatz 0,06.
    um23 = [b for t, b in paare if t.hour == 23]
    assert um23 and all(b == pytest.approx(0.122 - 0.06) for b in um23)
    # 5 Uhr: Gemeinschaft wieder am Tagsatz (0,102), Basis noch nachts (0,06).
    um5 = [b for t, b in paare if t.hour == 5]
    assert um5 and all(b == pytest.approx(0.102 - 0.06) for b in um5)


async def test_leeres_gemeinschafts_nachtfenster_faellt_auf_standard():
    """Ohne eigene Angabe gilt das Standard-Fenster — Bestandsanlagen
    verhalten sich nach dem Update exakt wie vorher."""
    ps = _Peakshare({18: 100.0})
    inputs, problem = await _collect(BASE_CONFIG, peakshare=ps)

    assert problem is None
    assert inputs.eeg_night_start_hour == 22   # wie schedule_night_start
    assert inputs.eeg_night_end_hour == 6
