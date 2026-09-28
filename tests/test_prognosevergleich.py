"""Tests für den Prognosevergleich (prognosevergleich.py).

Zeitraster des lokalen Kalendertags, das Ablegen beider Prognosen (je Quelle
nur einmal), die Verdichtung der Recorder-Messung, die Kennzahlen je Tag und
über alle Tage, der empirische p10 mit seiner Mindestanzahl, das Aufräumen
und der halbstündige Takt mit Attrappen für Solcast, eigene Prognose und
Recorder.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.eeg_energy_optimizer import prognosevergleich as pv
from custom_components.eeg_energy_optimizer import schedule as sched

CEST = timezone(timedelta(hours=2))
UTC = timezone.utc
JETZT = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)  # 10:00 MESZ
HEUTE = "2026-09-27"


def _lokal(dt: datetime) -> datetime:
    return dt.astimezone(CEST)


@pytest.fixture(autouse=True)
def _zeit():
    with patch.object(pv, "_utcnow", return_value=JETZT), patch.object(pv, "_lokal", _lokal):
        yield


def _vergleich() -> pv.Prognosevergleich:
    with patch.object(pv, "Store", None):
        return pv.Prognosevergleich(MagicMock(), "entry1")


def _reihe(slots: list[str], von: int, bis: int, wert: float) -> dict[str, float]:
    """Konstante Leistung in den Slots [von, bis), sonst nichts."""
    return {s: wert for i, s in enumerate(slots) if von <= i < bis}


def _tag_mit(fremd: float, eigen: float, gemessen: float, datum: str = HEUTE) -> dict:
    slots = [s.isoformat() for s in pv.tagesslots(datum)]
    ende = datetime.fromisoformat(slots[-1]) + timedelta(minutes=30)
    return {
        "datum": datum,
        "slots": slots,
        "fremd_name": "solcast_solar",
        "fremd": _reihe(slots, 12, 36, fremd),
        "eigen": _reihe(slots, 12, 36, eigen),
        "gemessen": _reihe(slots, 12, 36, gemessen),
        "gemessen_bis": ende.isoformat(),
        "vollstaendig": True,
        "festgehalten": JETZT.isoformat(),
    }


# ---------------------------------------------------------------------------
# Zeitraster und Reihen
# ---------------------------------------------------------------------------


def test_tagesslots_decken_den_lokalen_tag():
    slots = pv.tagesslots(HEUTE)
    assert len(slots) == 48
    assert slots[0] == datetime(2026, 9, 26, 22, 0, tzinfo=UTC)
    assert slots[-1] == datetime(2026, 9, 27, 21, 30, tzinfo=UTC)
    assert all(s.minute in (0, 30) for s in slots)


def test_tagesslots_an_der_zeitumstellung():
    try:
        from zoneinfo import ZoneInfo

        wien = ZoneInfo("Europe/Vienna")
    except Exception:  # noqa: BLE001 — ohne tzdata kein Test
        pytest.skip("keine Zeitzonendatenbank")
    with patch.object(pv, "_lokal", lambda dt: dt.astimezone(wien)):
        assert len(pv.tagesslots("2026-10-25")) == 50  # Rückstellung: 25 Stunden
        assert len(pv.tagesslots("2026-03-29")) == 46  # Vorstellung: 23 Stunden
        assert len(pv.tagesslots("2026-07-01")) == 48


def test_reihe_exakt_nimmt_nur_treffer():
    slots = pv.tagesslots(HEUTE)
    werte = {
        slots[10]: (1.5, 0.7),          # Solcast-Tupel: Erwartung zählt
        slots[11]: 2.0,
        slots[12] + timedelta(minutes=7): 9.9,  # neben dem Raster: weg
        slots[13]: -0.3,                # negativ → 0
    }
    reihe = pv.reihe_exakt(werte, slots)
    assert reihe == {
        slots[10].isoformat(): 1.5,
        slots[11].isoformat(): 2.0,
        slots[13].isoformat(): 0.0,
    }


def test_reihe_aus_stunden_teilt_die_stunde():
    slots = pv.tagesslots(HEUTE)
    stunde = slots[20].replace(minute=0)
    reihe = pv.reihe_aus_stunden({stunde.isoformat(): 1000.0}, slots)
    assert reihe[stunde.isoformat()] == 1.0
    assert reihe[(stunde + timedelta(minutes=30)).isoformat()] == 1.0
    assert len(reihe) == 2


def test_messung_aus_punkten_mittelt_je_halbstunde():
    slots = pv.tagesslots(HEUTE)
    basis = slots[20]
    punkte = [[(basis + timedelta(minutes=5 * i)).isoformat(), 1.0 + i] for i in range(9)]
    punkte.append([(slots[0] - timedelta(minutes=5)).isoformat(), 99.0])  # Vortag: weg
    gemessen, bis = pv.messung_aus_punkten(punkte, slots)
    assert gemessen[basis.isoformat()] == pytest.approx((1 + 2 + 3 + 4 + 5 + 6) / 6)
    assert gemessen[slots[21].isoformat()] == pytest.approx((7 + 8 + 9) / 3)
    assert bis == basis + timedelta(minutes=45)
    assert pv.messung_aus_punkten([], slots) == ({}, None)


# ---------------------------------------------------------------------------
# Ablegen
# ---------------------------------------------------------------------------


def test_festhalten_je_quelle_nur_einmal():
    v = _vergleich()
    slots = [s.isoformat() for s in pv.tagesslots(HEUTE)]
    erste = _reihe(slots, 12, 36, 1.0)
    assert v.festhalten(HEUTE, JETZT, "solcast_solar", erste, None) is True
    assert v.hat_prognosen(HEUTE) == (True, False)
    # Zweiter Versuch mit anderen Werten ändert nichts
    assert v.festhalten(HEUTE, JETZT, "solcast_solar", _reihe(slots, 12, 36, 5.0), None) is False
    assert v.tag(HEUTE)["fremd"] == erste
    # Die eigene Reihe darf später nachkommen
    spaeter = JETZT + timedelta(minutes=30)
    assert v.festhalten(HEUTE, spaeter, None, None, _reihe(slots, 12, 36, 0.8)) is True
    assert v.hat_prognosen(HEUTE) == (True, True)
    assert v.tag(HEUTE)["festgehalten"] == JETZT.isoformat()  # der erste Zeitpunkt bleibt


def test_messung_und_vollstaendigkeit():
    v = _vergleich()
    slots = pv.tagesslots(HEUTE)
    v.festhalten(HEUTE, JETZT, "solcast_solar", {slots[12].isoformat(): 1.0}, None)
    v.messung_eintragen(HEUTE, {slots[12].isoformat(): 0.9}, slots[13])
    assert v.tag(HEUTE)["vollstaendig"] is False
    v.messung_eintragen(HEUTE, {slots[12].isoformat(): 0.9}, slots[-1] + timedelta(minutes=10))
    assert v.tag(HEUTE)["vollstaendig"] is True
    v.messung_eintragen(HEUTE, {}, None)  # leer ändert nichts
    assert v.tag(HEUTE)["vollstaendig"] is True


# ---------------------------------------------------------------------------
# Kennzahlen
# ---------------------------------------------------------------------------


def test_statistik_eines_tages():
    st = pv.statistik_tag(_tag_mit(fremd=1.0, eigen=0.8, gemessen=0.9))
    assert st["gemessen_kwh"] == pytest.approx(10.8)
    f, e = st["quellen"]["fremd"], st["quellen"]["eigen"]
    assert f["prognose_kwh"] == pytest.approx(12.0)
    assert f["abweichung_kwh"] == pytest.approx(1.2)
    assert f["abweichung_pct"] == pytest.approx(11.1, abs=0.05)
    assert f["mae_kw"] == pytest.approx(0.1)
    assert e["abweichung_kwh"] == pytest.approx(-1.2)
    assert e["mae_kw"] == pytest.approx(0.1)
    assert f["slots"] == 24


def test_unvollstaendiger_tag_hat_keine_abweichung():
    tag = _tag_mit(1.0, 0.8, 0.9)
    tag["vollstaendig"] = False
    st = pv.statistik_tag(tag)
    assert st["quellen"]["fremd"]["abweichung_kwh"] is None
    assert st["quellen"]["fremd"]["mae_kw"] == pytest.approx(0.1)  # Verlauf zählt trotzdem
    assert st["quellen"]["fremd"]["prognose_bisher_kwh"] == pytest.approx(12.0)


def test_zusammenfassung_zaehlt_wer_naeher_lag():
    v = _vergleich()
    v._tage["2026-09-25"] = _tag_mit(1.0, 0.7, 0.9, "2026-09-25")   # fremd näher
    v._tage["2026-09-26"] = _tag_mit(1.3, 1.0, 0.9, "2026-09-26")   # eigen näher
    z = v.zusammenfassung()
    assert z["tage"] == 2
    assert z["naeher"] == {"tage": 2, "fremd": 1, "eigen": 1}
    fremd = z["quellen"]["fremd"]
    assert fremd["tage"] == 2
    assert fremd["bias_kwh"] == pytest.approx((1.2 + 4.8) / 2)
    assert fremd["mae_kwh"] == pytest.approx(3.0)
    assert fremd["p10_faktor"] is None and fremd["p10_tage"] == 2


def test_p10_faktor_braucht_vierzehn_tage():
    v = _vergleich()
    for i in range(13):
        datum = (datetime(2026, 9, 1) + timedelta(days=i)).date().isoformat()
        v._tage[datum] = _tag_mit(1.0, 1.0, 0.9, datum)
    assert v.p10_faktor(pv.QUELLE_EIGEN) == (None, 13)

    # 14 weitere Tage mit Verhältnissen 0,5 … 1,15. Sortiert stehen vorn
    # 0,5 / 0,55 / 0,6 / 0,7 …, das 10-%-Quantil von 27 Werten liegt bei
    # Position 2,6 — zwischen 0,6 und 0,7, also 0,66.
    for i, mess in enumerate([0.5, 0.55, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 1.0, 1.0, 1.05, 1.1, 1.1, 1.15]):
        datum = (datetime(2026, 8, 1) + timedelta(days=i)).date().isoformat()
        v._tage[datum] = _tag_mit(1.0, 1.0, mess, datum)
    faktor, n = v.p10_faktor(pv.QUELLE_EIGEN)
    assert n == 27
    werte = sorted(v._tagesverhaeltnisse(pv.QUELLE_EIGEN))
    assert faktor == pytest.approx(round(pv._quantil(werte, 0.1), 3))
    assert faktor == pytest.approx(0.66, abs=1e-3)


def test_p10_faktor_ist_begrenzt_und_ignoriert_leere_tage():
    v = _vergleich()
    for i in range(14):
        datum = (datetime(2026, 9, 1) + timedelta(days=i)).date().isoformat()
        v._tage[datum] = _tag_mit(1.0, 1.0, 0.05, datum)   # 5 % → geklemmt auf 0,2
    leer = _tag_mit(0.01, 0.01, 0.0, "2026-09-20")          # Prognose unter 1 kWh: zählt nicht
    v._tage["2026-09-20"] = leer
    faktor, n = v.p10_faktor(pv.QUELLE_FREMD)
    assert n == 14
    assert faktor == 0.2


def test_aufraeumen_haelt_gut_ein_jahr():
    """Die Kalibrierung lernt aus einem Jahr; ausgewertet werden 30 Tage."""
    v = _vergleich()
    alt = (JETZT.date() - timedelta(days=pv.TAGE_AUFBEWAHRUNG + 1)).isoformat()
    frisch = (JETZT.date() - timedelta(days=pv.TAGE_AUFBEWAHRUNG - 1)).isoformat()
    v._tage[alt] = _tag_mit(1, 1, 1, alt)
    v._tage[frisch] = _tag_mit(1, 1, 1, frisch)
    v.aufraeumen(JETZT)
    assert v.tage() == [frisch]
    assert pv.TAGE_AUFBEWAHRUNG >= 366


def test_auswertung_nur_ueber_die_letzten_dreissig_tage():
    v = _vergleich()
    for i in range(40):
        d = (JETZT.date() - timedelta(days=i + 1)).isoformat()
        v._tage[d] = _tag_mit(1.0, 0.7, 0.9, d)
    assert len(v.uebersicht()) == pv.TAGE_AUSWERTUNG
    assert v.zusammenfassung()["tage"] <= pv.TAGE_AUSWERTUNG
    assert len(v.lerntage()) == 40


async def test_abgeschlossene_tage_gehen_ins_archiv():
    """Das Archiv wird nur geschrieben, wenn ein Tag hineinwandert."""
    v = _vergleich()
    v._store = MagicMock(async_save=AsyncMock())
    v._archiv_store = MagicMock(async_save=AsyncMock())
    gestern = (JETZT.date() - timedelta(days=1)).isoformat()
    v._tage[gestern] = _tag_mit(1.0, 0.7, 0.9, gestern)
    v._tage[gestern]["vollstaendig"] = True
    v._tage[HEUTE] = _tag_mit(1.0, 0.7, 0.9, HEUTE)
    await v.async_save(JETZT)
    assert list(v._archiv_store.async_save.await_args.args[0]["tage"]) == [gestern]
    assert list(v._store.async_save.await_args.args[0]["tage"]) == [HEUTE]
    await v.async_save(JETZT)
    assert v._archiv_store.async_save.await_count == 1
    assert v._store.async_save.await_count == 2


def test_status_liefert_uebersicht_und_gewaehlten_tag():
    v = _vergleich()
    v._tage["2026-09-25"] = _tag_mit(1.0, 0.7, 0.9, "2026-09-25")
    v._tage["2026-09-26"] = _tag_mit(1.3, 1.0, 0.9, "2026-09-26")
    st = v.status()
    assert st["tage"] == ["2026-09-25", "2026-09-26"]
    assert st["tag"]["datum"] == "2026-09-26"
    assert [u["datum"] for u in st["uebersicht"]] == ["2026-09-26", "2026-09-25"]
    assert v.status("2026-09-25")["tag"]["datum"] == "2026-09-25"
    assert v.status("1999-01-01")["tag"]["datum"] == "2026-09-26"
    assert st["p10_min_tage"] == 14


# ---------------------------------------------------------------------------
# Der Takt
# ---------------------------------------------------------------------------


def _hass_mit_provider(halb: dict) -> MagicMock:
    hass = MagicMock()
    provider = MagicMock()
    provider.halbstunden.return_value = halb
    hass.data = {sched.DOMAIN: {"entry1": {"pvprognose": provider}}}
    return hass


async def test_tick_haelt_morgens_fest_und_zieht_die_messung_nach():
    slots = pv.tagesslots(HEUTE)
    solcast = {slots[i]: (1.0, 0.5) for i in range(12, 36)}
    eigen = {slots[i]: 0.8 for i in range(12, 36)}
    punkte = [[(slots[20] + timedelta(minutes=5 * i)).isoformat(), 0.9] for i in range(12)]
    hass = _hass_mit_provider(eigen)
    with patch.object(pv, "Store", None):
        v = pv.Prognosevergleich(hass, "entry1")

    ist = AsyncMock(return_value={"reihen": {"pv_leistung": punkte}})
    with (
        patch.object(sched, "_solcast_detailed", return_value=solcast),
        patch("custom_components.eeg_energy_optimizer.schedule_archive.async_ist_verlauf", ist),
    ):
        await v.async_tick({}, JETZT)
        tag = v.tag(HEUTE)
        assert tag["fremd_name"] == "solcast_solar"
        assert tag["fremd"][slots[12].isoformat()] == 1.0
        assert tag["eigen"][slots[12].isoformat()] == 0.8
        assert tag["gemessen"][slots[20].isoformat()] == 0.9
        assert tag["vollstaendig"] is False
        festgehalten = tag["festgehalten"]

        # Zweiter Takt: Prognosen bleiben, die Messung wächst
        punkte.append([(slots[-1] + timedelta(minutes=25)).isoformat(), 0.0])
        hass.data[sched.DOMAIN]["entry1"]["pvprognose"].halbstunden.return_value = {
            s: 5.0 for s in slots
        }
        await v.async_tick({}, JETZT + timedelta(hours=12))
        tag = v.tag(HEUTE)
        assert tag["festgehalten"] == festgehalten
        assert tag["eigen"][slots[12].isoformat()] == 0.8
        assert tag["vollstaendig"] is True
    # Der Recorder wurde für heute (und gestern gibt es nicht) gefragt
    assert ist.await_count == 2


async def test_tick_vor_fuenf_uhr_haelt_nichts_fest():
    hass = _hass_mit_provider({})
    with patch.object(pv, "Store", None):
        v = pv.Prognosevergleich(hass, "entry1")
    frueh = datetime(2026, 9, 27, 2, 30, tzinfo=UTC)  # 04:30 MESZ
    with patch.object(sched, "_solcast_detailed", return_value={}):
        await v.async_tick({}, frueh)
    assert v.tage() == []


async def test_tick_faellt_auf_forecast_solar_zurueck():
    slots = pv.tagesslots(HEUTE)
    stunde = slots[20].replace(minute=0)
    hass = _hass_mit_provider({})
    with patch.object(pv, "Store", None):
        v = pv.Prognosevergleich(hass, "entry1")
    with (
        patch.object(sched, "_solcast_detailed", return_value={}),
        patch.object(sched, "_async_solar_forecast_wh", AsyncMock(return_value={stunde.isoformat(): 1500.0})),
        patch(
            "custom_components.eeg_energy_optimizer.schedule_archive.async_ist_verlauf",
            AsyncMock(return_value={"reihen": {}}),
        ),
    ):
        await v.async_tick({}, JETZT)
    tag = v.tag(HEUTE)
    assert tag["fremd_name"] == "forecast_solar"
    assert tag["fremd"][stunde.isoformat()] == 1.5
    assert tag["eigen"] is None  # leere eigene Reihe wird nicht als „festgehalten" gewertet


async def test_tick_ueberlebt_fehler_der_quellen():
    hass = _hass_mit_provider({})
    hass.data[sched.DOMAIN]["entry1"]["pvprognose"].halbstunden.side_effect = RuntimeError("kaputt")
    with patch.object(pv, "Store", None):
        v = pv.Prognosevergleich(hass, "entry1")
    with (
        patch.object(sched, "_solcast_detailed", side_effect=RuntimeError("Solcast weg")),
        patch(
            "custom_components.eeg_energy_optimizer.schedule_archive.async_ist_verlauf",
            AsyncMock(side_effect=RuntimeError("Recorder weg")),
        ),
    ):
        await v.async_tick({}, JETZT)
    assert v.tage() == []
    assert "kaputt" in v.status()["fehler"]


# ---------------------------------------------------------------------------
# p10 im Fahrplan: geliehen, empirisch, keiner
# ---------------------------------------------------------------------------


def _fahrplan_hass(config_extra: dict, halb: dict, solcast: list, vergleich=None):
    from tests import test_schedule as ts

    hass = ts._hass_with({**ts.BASE_CONFIG, "forecast_source": "eigen", **config_extra})
    prognose = MagicMock()
    prognose.halbstunden.return_value = halb
    prognose.status.return_value = {"fehler": None, "veraltet": False}
    prognose.alter_s.return_value = 60.0
    hass.data[sched.DOMAIN]["entry1"]["pvprognose"] = prognose
    if vergleich is not None:
        hass.data[sched.DOMAIN]["entry1"]["prognosevergleich"] = vergleich
    hass.states.async_all.return_value = solcast
    return hass, ts.NOW


async def test_p10_wird_von_solcast_geliehen():
    from tests.test_schedule import _solcast_state

    from tests import test_schedule as ts

    start = ts.NOW.replace(minute=0, second=0, microsecond=0)
    halb = {start + timedelta(minutes=30 * k): 2.0 for k in range(96)}
    # Solcast sagt p10 = halbe Erwartung in der ersten Stunde nach start
    solcast_werte = [
        (f"{(start + timedelta(minutes=30 * k)).hour:02d}:{(start + timedelta(minutes=30 * k)).minute:02d}", 4.0, 2.0)
        for k in range(2)
    ]
    hass, now = _fahrplan_hass({"pv_prognose_vergleich": True}, halb, [_solcast_state(start, solcast_werte)])
    with patch.object(sched, "_now_local", return_value=now):
        inputs, problem = await sched.async_collect_inputs(hass, "entry1")
    assert problem is None
    assert inputs.min_production_kw is not None
    assert inputs.min_production_kw[1] == pytest.approx(1.0)   # 2,0 × (2,0/4,0)
    assert "geliehen" in inputs.forecast_source


async def test_p10_kommt_empirisch_ohne_solcast():
    from tests import test_schedule as ts

    start = ts.NOW.replace(minute=0, second=0, microsecond=0)
    halb = {start + timedelta(minutes=30 * k): 2.0 for k in range(96)}
    vergleich = MagicMock()
    vergleich.p10_faktor.return_value = (0.7, 20)
    hass, now = _fahrplan_hass({"pv_prognose_vergleich": True}, halb, [], vergleich)
    with patch.object(sched, "_now_local", return_value=now):
        inputs, problem = await sched.async_collect_inputs(hass, "entry1")
    assert problem is None
    assert inputs.min_production_kw[1] == pytest.approx(1.4)
    assert "empirisch aus 20 Tagen" in inputs.forecast_source


async def test_ohne_vergleich_kein_p10():
    from tests import test_schedule as ts
    from tests.test_schedule import _solcast_state

    start = ts.NOW.replace(minute=0, second=0, microsecond=0)
    halb = {start + timedelta(minutes=30 * k): 2.0 for k in range(96)}
    vergleich = MagicMock()
    vergleich.p10_faktor.return_value = (0.7, 20)
    # Solcast installiert und Vergleichsdaten da — aber der Schalter ist aus
    hass, now = _fahrplan_hass({}, halb, [_solcast_state(start, [("05:00", 4.0, 2.0)])], vergleich)
    with patch.object(sched, "_now_local", return_value=now):
        inputs, problem = await sched.async_collect_inputs(hass, "entry1")
    assert problem is None
    assert inputs.min_production_kw is None
    assert "Worst-Case-Faktor" in inputs.forecast_source


# ---------------------------------------------------------------------------
# Spät festgehaltene Tage und Telemetrie
# ---------------------------------------------------------------------------


def test_spaet_festgehaltener_tag_zaehlt_nicht():
    v = _vergleich()
    slots = [s.isoformat() for s in pv.tagesslots(HEUTE)]
    # 14:00 MESZ: Home Assistant lief am Morgen nicht
    mittag = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    assert v.festhalten(HEUTE, mittag, "solcast_solar", _reihe(slots, 12, 36, 1.0), None)
    tag = v.tag(HEUTE)
    assert tag["spaet"] is True
    assert tag["festgehalten_fremd"] == mittag.isoformat()
    assert pv.statistik_tag(tag)["spaet"] is True

    # Ein pünktlicher und ein später vollständiger Tag
    v._tage["2026-09-25"] = _tag_mit(1.0, 1.0, 0.9, "2026-09-25")
    spaet = _tag_mit(2.0, 2.0, 0.9, "2026-09-26")
    spaet["spaet"] = True
    v._tage["2026-09-26"] = spaet
    z = v.zusammenfassung()
    assert z["tage"] == 1
    assert z["spaet_ausgelassen"] == 1
    assert z["quellen"]["fremd"]["bias_kwh"] == pytest.approx(1.2)
    assert len(v._tagesverhaeltnisse(pv.QUELLE_FREMD)) == 1


def test_pünktlich_festgehalten_ist_nicht_spaet():
    v = _vergleich()
    slots = [s.isoformat() for s in pv.tagesslots(HEUTE)]
    frueh = datetime(2026, 9, 27, 3, 30, tzinfo=UTC)  # 05:30 MESZ
    v.festhalten(HEUTE, frueh, None, None, _reihe(slots, 12, 36, 1.0))
    assert not v.tag(HEUTE).get("spaet")
    assert v.tag(HEUTE)["festgehalten_eigen"] == frueh.isoformat()


def test_telemetrie_meldet_vergleich_und_flaechen_ohne_liste():
    from types import SimpleNamespace

    from custom_components.eeg_energy_optimizer import _build_telemetry_profile
    from custom_components.eeg_energy_optimizer.const import TELEMETRY_SETTINGS_KEYS

    entry = SimpleNamespace(
        entry_id="e1",
        data={
            "forecast_source": "eigen",
            "pv_prognose_vergleich": True,
            "pv_verluste_pct": 12,
            "pv_flaechen": [{"kwp": 10, "azimut": 180}, {"kwp": 6.5, "azimut": 90}],
        },
        options={},
    )
    profil = _build_telemetry_profile(MagicMock(), entry, None)
    settings = profil["settings"]
    assert settings["pv_prognose_vergleich"] is True
    assert settings["pv_verluste_pct"] == 12
    assert settings["pv_flaechen_anzahl"] == 2
    assert settings["pv_flaechen_kwp"] == 16.5
    assert "pv_flaechen" not in settings
    assert "pv_flaechen" not in TELEMETRY_SETTINGS_KEYS


def test_messung_aus_punkten_maximum_und_nan():
    """Das Maximum je Halbstunde erkennt eine Viertelstunde an der
    Exportgrenze, die im Mittel verschwindet; NaN fällt weg."""
    slots = pv.tagesslots(HEUTE)
    basis = slots[20]
    werte = [4.0, 4.0, 4.0, 1.0, 1.0, float("nan")]
    punkte = [[(basis + timedelta(minutes=5 * i)).isoformat(), w] for i, w in enumerate(werte)]
    punkte.append([(basis + timedelta(minutes=30)).isoformat(), "inf"])
    mittel, _ = pv.messung_aus_punkten(punkte, slots)
    maximum, _ = pv.messung_aus_punkten(punkte, slots, maximum=True)
    assert mittel[basis.isoformat()] == pytest.approx(14.0 / 5)
    assert maximum[basis.isoformat()] == pytest.approx(4.0)
    assert slots[21].isoformat() not in mittel


async def test_tick_speichert_die_maxima_fuer_die_kalibrierung():
    slots = pv.tagesslots(HEUTE)
    solcast = {slots[i]: (1.0, 0.5) for i in range(12, 36)}
    eigen = {slots[i]: 0.8 for i in range(12, 36)}

    def _punkte(werte):
        return [[(slots[20] + timedelta(minutes=5 * i)).isoformat(), w] for i, w in enumerate(werte)]

    reihen = {
        "pv_leistung": _punkte([5.0, 5.0, 5.0, 2.0, 2.0, 2.0]),
        "netzleistung": _punkte([4.0, 4.0, 4.0, 1.0, 1.0, 1.0]),
        "batterieleistung": _punkte([3.0, 3.0, 3.0, -1.0, -1.0, -1.0]),
    }
    hass = _hass_mit_provider(eigen)
    with patch.object(pv, "Store", None):
        v = pv.Prognosevergleich(hass, "entry1")
    ist = AsyncMock(return_value={"reihen": reihen})
    with (
        patch.object(sched, "_solcast_detailed", return_value=solcast),
        patch("custom_components.eeg_energy_optimizer.schedule_archive.async_ist_verlauf", ist),
    ):
        await v.async_tick({}, JETZT)
    tag = v.tag(HEUTE)
    k = slots[20].isoformat()
    assert tag["netz"][k] == pytest.approx(2.5)
    assert tag["netz_max"][k] == pytest.approx(4.0)
    assert tag["gemessen_max"][k] == pytest.approx(5.0)
    assert tag["batterie_max"][k] == pytest.approx(3.0)
