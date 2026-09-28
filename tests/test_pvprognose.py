"""Tests für die eigene PV-Prognose (pvprognose/).

Grundlage sind zwei echte Open-Meteo-Antworten vom 27.09.2026 (Standort
48,2 / 14,3, Neigung 30°, einmal Süd, einmal Ost, zwei Tage im
15-Minuten-Raster) in ``tests/fixtures``. Geprüft werden die Umrechnung des
Azimuts und die URL, das Zerlegen der Antwort, die Physik des Modells, die
Lage der Werte auf dem Slot-Raster, Tagessummen in Ortszeit, der Provider
(Frische, Fehlerfall, Alter) und der Weg in den Fahrplan.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.eeg_energy_optimizer.pvprognose import modell, openmeteo
from custom_components.eeg_energy_optimizer.pvprognose import provider as prov

FIXTURES = Path(__file__).parent / "fixtures"
CEST = timezone(timedelta(hours=2))
UTC = timezone.utc


def _antwort(name: str) -> dict:
    return json.loads((FIXTURES / f"openmeteo_{name}_2tage.json").read_text(encoding="utf-8"))


def _lokal(dt: datetime) -> datetime:
    return dt.astimezone(CEST)


def _utc(tag: int, stunde: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, tag, stunde, minute, tzinfo=UTC)


SUED = modell.Flaeche("Süd", 5.0, 30.0, 180.0)
OST = modell.Flaeche("Ost", 3.0, 30.0, 90.0)


# ---------------------------------------------------------------------------
# Open-Meteo: Azimut, URL, Antwort
# ---------------------------------------------------------------------------


def test_azimut_kompass_nach_openmeteo():
    """Kompass 0 = Nord → Open-Meteo 0 = Süd, Ost negativ, West positiv."""
    assert openmeteo.azimut_openmeteo(180) == 0.0
    assert openmeteo.azimut_openmeteo(90) == -90.0
    assert openmeteo.azimut_openmeteo(270) == 90.0
    assert abs(openmeteo.azimut_openmeteo(0)) == 180.0
    assert openmeteo.azimut_openmeteo(135) == -45.0
    assert openmeteo.azimut_openmeteo(225) == 45.0
    # Werte über 360 werden gefaltet
    assert openmeteo.azimut_openmeteo(450) == -90.0


def test_url_traegt_neigung_azimut_und_utc():
    url = openmeteo.baue_url(48.2, 14.3, 30, 90)
    assert url.startswith(openmeteo.API_URL + "?")
    assert "latitude=48.2000" in url
    assert "longitude=14.3000" in url
    assert "tilt=30.0" in url
    assert "azimuth=-90.0" in url
    assert "timezone=UTC" in url
    assert "forecast_days=7" in url
    assert "minutely_15=global_tilted_irradiance,temperature_2m" in url
    assert "models=icon_seamless,ecmwf_ifs025,meteofrance_seamless" in url


def test_mittel_ueber_die_modelle():
    """Je Zeitpunkt zählen die Modelle mit Wert — endet eines (Météo-France
    nach gut vier Tagen), mitteln die übrigen weiter; hat keines einen Wert,
    ist die Strahlung 0 und die Temperatur unbekannt."""
    reihe = openmeteo.parse_antwort(
        {
            "minutely_15": {
                "time": ["2026-09-28T07:00", "2026-09-28T07:15", "2026-09-28T07:30"],
                "global_tilted_irradiance_icon_seamless": [333.7, 350.0, None],
                "temperature_2m_icon_seamless": [8.0, 9.0, None],
                "global_tilted_irradiance_ecmwf_ifs025": [409.0, 380.0, None],
                "temperature_2m_ecmwf_ifs025": [10.0, 11.0, None],
                "global_tilted_irradiance_meteofrance_seamless": [423.7, None, None],
                "temperature_2m_meteofrance_seamless": [12.0, None, None],
            }
        }
    )
    assert reihe.gti_w_m2[0] == pytest.approx((333.7 + 409.0 + 423.7) / 3)
    assert reihe.temp_c[0] == pytest.approx(10.0)
    assert reihe.gti_w_m2[1] == pytest.approx(365.0)
    assert reihe.temp_c[1] == pytest.approx(10.0)
    assert reihe.gti_w_m2[2] == 0.0
    assert reihe.temp_c[2] is None


def test_antwort_wird_zerlegt():
    reihe = openmeteo.parse_antwort(_antwort("sued"))

    assert len(reihe) == 192
    assert reihe.ende[0] == _utc(27, 0, 0)
    assert reihe.ende[-1] == _utc(28, 23, 45)
    i = reihe.ende.index(_utc(27, 8, 15))
    assert reihe.gti_w_m2[i] == pytest.approx(513.2)
    assert reihe.temp_c[i] == pytest.approx(12.7)


def test_antwort_fehler_und_luecken():
    with pytest.raises(ValueError, match="Nacht"):
        openmeteo.parse_antwort({"error": True, "reason": "Nacht ist kein Wetter"})
    with pytest.raises(ValueError):
        openmeteo.parse_antwort({"hourly": {"time": ["2026-09-27T00:00"]}})
    with pytest.raises(ValueError):
        openmeteo.parse_antwort("kein json")
    # null-Werte: Strahlung 0, Temperatur unbekannt
    reihe = openmeteo.parse_antwort(
        {
            "minutely_15": {
                "time": ["2026-09-27T08:00", "2026-09-27T08:15"],
                "global_tilted_irradiance": [None, 100.0],
                "temperature_2m": [None, 10.0],
            }
        }
    )
    assert reihe.gti_w_m2 == [0.0, 100.0]
    assert reihe.temp_c == [None, 10.0]


# ---------------------------------------------------------------------------
# Modell: Physik
# ---------------------------------------------------------------------------


def test_stc_bedingungen_geben_die_nennleistung():
    """1000 W/m² bei Zelltemperatur 25 °C — Luft −5 °C, denn die Zelle
    liegt 30 K über der Luft."""
    assert modell.dc_kw(1000.0, -5.0, 5.0) == pytest.approx(5.0)


def test_waerme_kostet_leistung():
    kalt = modell.dc_kw(800.0, 5.0, 5.0)
    warm = modell.dc_kw(800.0, 30.0, 5.0)
    assert warm < kalt
    # Zelle = Luft + 24 K (0,03 K·m²/W · 800 W/m²): 29 K bzw. 54 K → gegen
    # 25 °C sind das 4 K und 29 K Abweichung bei −0,4 %/K.
    assert warm / kalt == pytest.approx((1 - 0.004 * 29) / (1 - 0.004 * 4), rel=1e-6)


def test_dc_wert_aus_der_fixture():
    """513,2 W/m² bei 12,7 °C auf 5 kWp — von Hand nachgerechnet."""
    erwartet = 5.0 * 0.5132 * (1 + (-0.004) * (12.7 + 0.03 * 513.2 - 25.0))
    assert modell.dc_kw(513.2, 12.7, 5.0) == pytest.approx(erwartet, rel=1e-6)
    assert erwartet == pytest.approx(2.534, abs=1e-3)


def test_ohne_licht_oder_ohne_module_nichts():
    assert modell.dc_kw(0.0, 20.0, 5.0) == 0.0
    assert modell.dc_kw(500.0, 20.0, 0.0) == 0.0
    assert modell.dc_kw(-5.0, 20.0, 5.0) == 0.0


def test_fehlende_temperatur_nimmt_die_vorgabe():
    assert modell.dc_kw(500.0, None, 5.0) == pytest.approx(
        modell.dc_kw(500.0, modell.TEMP_VORGABE_C, 5.0)
    )


# ---------------------------------------------------------------------------
# Modell: Flächen, Verluste, Deckel, Raster
# ---------------------------------------------------------------------------


def _paare():
    return [
        (SUED, openmeteo.parse_antwort(_antwort("sued"))),
        (OST, openmeteo.parse_antwort(_antwort("ost"))),
    ]


def test_flaechen_addieren_sich_und_der_deckel_greift():
    frei = modell.leistungsreihe(_paare(), 14.0, None)
    gedeckelt = modell.leistungsreihe(_paare(), 14.0, 4.0)

    assert len(frei) == len(gedeckelt) == 192
    assert max(frei.kw) > 4.0
    assert max(gedeckelt.kw) == pytest.approx(4.0)
    # Unter dem Deckel sind beide identisch
    for a, b in zip(frei.kw, gedeckelt.kw):
        if a < 4.0:
            assert a == b


def test_verluste_skalieren_linear():
    ohne = modell.leistungsreihe(_paare(), 0.0, None)
    mit = modell.leistungsreihe(_paare(), 14.0, None)
    i = ohne.ende.index(_utc(27, 8, 15))
    assert mit.kw[i] == pytest.approx(ohne.kw[i] * 0.86, rel=1e-3)


def test_ost_hat_ihr_maximum_vor_sued():
    """Die Fixture selbst beweist die Azimut-Konvention: Ost morgens."""
    sued = modell.leistungsreihe([(SUED, openmeteo.parse_antwort(_antwort("sued")))], 0.0, None)
    ost = modell.leistungsreihe([(OST, openmeteo.parse_antwort(_antwort("ost")))], 0.0, None)
    spitze_sued = sued.ende[sued.kw.index(max(sued.kw[:96]))]
    spitze_ost = ost.ende[ost.kw.index(max(ost.kw[:96]))]
    assert spitze_ost < spitze_sued
    assert spitze_ost == _utc(27, 9, 15)
    assert spitze_sued == _utc(27, 11, 0)


def test_nur_gemeinsame_zeitpunkte_zaehlen():
    """Fehlt einer Fläche das Ende der Reihe, darf sie dort nicht als 0 kW
    in die Summe eingehen — die Reihe endet, wo die kürzeste endet."""
    paare = _paare()
    kurz = paare[1][1]
    kurz.ende, kurz.gti_w_m2, kurz.temp_c = kurz.ende[:100], kurz.gti_w_m2[:100], kurz.temp_c[:100]
    reihe = modell.leistungsreihe(paare, 14.0, None)
    assert len(reihe) == 100


def test_slot_anfang_ist_intervall_ende_minus_15_minuten():
    reihe = modell.leistungsreihe(_paare(), 14.0, None)
    slots = modell.slot_anfaenge(reihe)
    i = reihe.ende.index(_utc(27, 8, 15))
    assert slots[_utc(27, 8, 0)] == reihe.kw[i]
    assert _utc(28, 23, 45) not in slots  # letztes Ende → Anfang 23:30
    assert _utc(28, 23, 30) in slots


def test_halbstunden_mitteln_zwei_viertelstunden():
    reihe = modell.leistungsreihe(_paare(), 14.0, None)
    halb = modell.halbstunden(reihe)
    kw = dict(zip(reihe.ende, reihe.kw))

    assert all(t.minute in (0, 30) for t in halb)
    assert halb[_utc(27, 8, 0)] == pytest.approx(
        (kw[_utc(27, 8, 15)] + kw[_utc(27, 8, 30)]) / 2, abs=1e-4
    )
    assert halb[_utc(27, 8, 30)] == pytest.approx(
        (kw[_utc(27, 8, 45)] + kw[_utc(27, 9, 0)]) / 2, abs=1e-4
    )
    # 192 Viertelstunden ab 26.09. 23:45 (Slot-Anfang des ersten Endes) bis
    # 28.09. 23:30: eine angeschnittene halbe Stunde vorn, eine hinten.
    assert len(halb) == 97
    assert min(halb) == _utc(26, 23, 30)
    assert max(halb) == _utc(28, 23, 30)


def test_energie_rechnet_angeschnittene_slots_anteilig():
    reihe = modell.Leistungsreihe(
        ende=[_utc(27, 8, 15), _utc(27, 8, 30)], kw=[1.0, 1.0]
    )
    # zwei volle Slots = 0,5 kWh
    assert modell.energie_kwh(reihe, _utc(27, 8, 0), _utc(27, 8, 30)) == pytest.approx(0.5)
    # halber erster Slot
    assert modell.energie_kwh(reihe, _utc(27, 8, 7), _utc(27, 8, 15)) == pytest.approx(
        1.0 * 8 / 60, abs=1e-3
    )
    assert modell.energie_kwh(reihe, _utc(27, 9, 0), _utc(27, 10, 0)) == 0.0
    assert modell.energie_kwh(reihe, _utc(27, 9, 0), _utc(27, 8, 0)) == 0.0


def test_tagessummen_in_ortszeit():
    reihe = modell.leistungsreihe(_paare(), 14.0, None)
    jetzt = _utc(27, 10, 0)  # 12:00 MESZ

    tage = modell.tagessummen(reihe, jetzt, _lokal)

    assert len(tage) == 7
    # Der lokale Tag 27.09. reicht von 26.09. 22:00 UTC bis 27.09. 22:00 UTC —
    # die Fixture beginnt erst am 27.09. 00:00 UTC, davor ist es Nacht.
    von_hand = modell.energie_kwh(reihe, _utc(27, 0, 0), _utc(27, 22, 0))
    assert tage[0] == pytest.approx(von_hand, abs=1e-3)
    assert tage[0] > 20.0  # 8 kWp an einem Septembertag
    assert tage[1] > 0.0
    assert tage[2:] == [0.0] * 5  # die Fixture hat nur zwei Tage

    rest = modell.rest_heute_kwh(reihe, jetzt, _lokal)
    assert 0 < rest < tage[0]
    assert rest == pytest.approx(modell.energie_kwh(reihe, jetzt, _utc(27, 22, 0)), abs=1e-3)


# ---------------------------------------------------------------------------
# Konfiguration: Prüfung und Lesen der Flächen
# ---------------------------------------------------------------------------


def test_pruefe_flaechen_normiert():
    sauber, fehler = modell.pruefe_flaechen(
        [
            {"name": "  Dach Süd  ", "kwp": "5.5", "neigung": 35, "azimut": 450},
            {"kwp": 3},
        ]
    )
    assert fehler is None
    assert sauber[0] == {
        "name": "Dach Süd", "kwp": 5.5, "neigung": 35.0, "azimut": 90.0, "max_kw": None,
    }
    assert sauber[1] == {
        "name": "Fläche 2", "kwp": 3.0, "neigung": 30.0, "azimut": 180.0, "max_kw": None,
    }


def test_grenze_je_flaeche_wird_gepruft_und_angewendet():
    """Zwei Wechselrichter: das Süddach mit 10 kWp an einem 8-kW-Gerät wird
    für sich abgeschnitten, nicht erst in der Summe."""
    sauber, fehler = modell.pruefe_flaechen([{"kwp": 10, "max_kw": "8"}, {"kwp": 6, "max_kw": ""}])
    assert fehler is None
    assert sauber[0]["max_kw"] == 8.0 and sauber[1]["max_kw"] is None
    for roh in ([{"kwp": 10, "max_kw": "acht"}], [{"kwp": 10, "max_kw": 0.01}]):
        _, fehler = modell.pruefe_flaechen(roh)
        assert fehler and "Grenze" in fehler

    # Ende September bringt das 10-kWp-Süddach der Fixture gut 6 kW —
    # die Grenze liegt deshalb darunter, sonst griffe sie nicht.
    sued_frei = modell.Flaeche("Süd", 10.0, 30.0, 180.0)
    sued_5kw = modell.Flaeche("Süd", 10.0, 30.0, 180.0, max_kw=5.0)
    wetter = openmeteo.parse_antwort(_antwort("sued"))
    frei = modell.leistungsreihe([(sued_frei, wetter)], 14.0, None)
    begrenzt = modell.leistungsreihe([(sued_5kw, wetter)], 14.0, None)
    assert 6.0 < max(frei.kw) < 7.0
    assert max(begrenzt.kw) == pytest.approx(5.0)
    for a, b in zip(frei.kw, begrenzt.kw):
        assert b == pytest.approx(min(a, 5.0))
    # Die Summengrenze wirkt zusätzlich: 5 kW je Fläche, 8 kW gesamt.
    ost = openmeteo.parse_antwort(_antwort("ost"))
    beide = modell.leistungsreihe(
        [(sued_5kw, wetter), (modell.Flaeche("Ost", 10.0, 30.0, 90.0, max_kw=5.0), ost)],
        14.0,
        8.0,
    )
    assert max(beide.kw) == pytest.approx(8.0)
    assert modell.flaechen_aus_config({"pv_flaechen": [{"kwp": 5, "max_kw": "x"}]})[0].max_kw is None
    assert modell.flaechen_aus_config({"pv_flaechen": [{"kwp": 5, "max_kw": 4}]})[0].max_kw == 4.0


@pytest.mark.parametrize(
    "roh",
    [
        [],
        None,
        [{"neigung": 30}],
        [{"kwp": 0}],
        [{"kwp": 5, "neigung": 95}],
        [{"kwp": 5, "neigung": "steil"}],
        ["kein dict"],
        [{"kwp": 1}] * 9,
    ],
)
def test_pruefe_flaechen_weist_ab(roh):
    sauber, fehler = modell.pruefe_flaechen(roh)
    assert sauber == []
    assert fehler


def test_flaechen_aus_config_uebergeht_kaputtes():
    flaechen = modell.flaechen_aus_config(
        {
            "pv_flaechen": [
                {"name": "Süd", "kwp": 5, "neigung": 30, "azimut": 180},
                {"kwp": "x"},
                {"kwp": 0},
                "unsinn",
                {"kwp": 2, "azimut": 540},
            ]
        }
    )
    assert [f.name for f in flaechen] == ["Süd", "Fläche 5"]
    assert flaechen[1].azimut == 180.0
    assert modell.flaechen_aus_config({}) == []
    assert modell.flaechen_aus_config({"pv_flaechen": "nix"}) == []


def test_verluste_aus_config():
    assert modell.verluste_aus_config({}) == 14.0
    assert modell.verluste_aus_config({"pv_verluste_pct": ""}) == 14.0
    assert modell.verluste_aus_config({"pv_verluste_pct": "8"}) == 8.0
    assert modell.verluste_aus_config({"pv_verluste_pct": 99}) == 60.0


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

JETZT = _utc(27, 10, 0)
CONFIG = {
    "pv_flaechen": [
        {"name": "Süd", "kwp": 5, "neigung": 30, "azimut": 180},
        {"name": "Ost", "kwp": 3, "neigung": 30, "azimut": 90},
    ],
    "pv_verluste_pct": 14,
    "inverter_ac_limit_kw": 6,
}


def _hass():
    hass = MagicMock()
    hass.config.latitude = 48.2
    hass.config.longitude = 14.3
    return hass


async def _wetter_nach_url(session, url):
    """Attrappe für hole_wetter: Süd oder Ost, je nach Azimut in der URL."""
    if "azimuth=0.0" in url:
        return openmeteo.parse_antwort(_antwort("sued"))
    if "azimuth=-90.0" in url:
        return openmeteo.parse_antwort(_antwort("ost"))
    raise AssertionError(f"unerwartete URL {url}")


def _provider(config=CONFIG):
    with patch.object(prov, "Store", None):
        return prov.PvPrognoseProvider(_hass(), "entry1", config)


def _abruf_patches(hole=None):
    return (
        patch.object(prov, "async_get_clientsession", MagicMock(return_value=MagicMock())),
        patch.object(prov, "hole_wetter", AsyncMock(side_effect=hole or _wetter_nach_url)),
        patch.object(prov, "_utcnow", return_value=JETZT),
        patch.object(prov, "_lokal", _lokal),
    )


async def test_fetch_rechnet_reihe_und_status():
    p = _provider()
    a, b, c, d = _abruf_patches()
    with a, b as hole, c, d:
        assert await p.async_fetch(force=True) is True
        assert hole.await_count == 2  # eine Anfrage je Fläche
        status = p.status(JETZT)

    assert p.hat_daten
    assert status["werte"] == 192
    assert status["kwp_gesamt"] == 8.0
    assert status["fehler"] is None
    assert status["alter_minuten"] == 0
    assert status["standort"] == {"breite": 48.2, "laenge": 14.3}
    assert len(status["tage_kwh"]) == 7
    assert status["rest_heute_kwh"] == pytest.approx(p.rest_heute_kwh(JETZT))
    assert status["morgen_kwh"] == status["tage_kwh"][1]
    assert status["spitze_kw"] <= 6.0
    halb = p.halbstunden(JETZT)
    assert halb and all(t.minute in (0, 30) for t in halb)


async def test_frische_verhindert_den_zweiten_abruf():
    p = _provider()
    a, b, c, d = _abruf_patches()
    with a, b as hole, c, d:
        assert await p.async_fetch(force=True) is True
        assert await p.async_fetch() is False
        assert hole.await_count == 2
        # Nach der Frist holt er wieder
        with patch.object(prov, "_utcnow", return_value=JETZT + timedelta(seconds=prov.FRISCH_S + 1)):
            assert await p.async_fetch() is True
        assert hole.await_count == 4


async def test_fehler_behaelt_die_alte_reihe():
    p = _provider()
    a, b, c, d = _abruf_patches()
    with a, b, c, d:
        await p.async_fetch(force=True)
    alt = p.status(JETZT)["tage_kwh"]

    async def kaputt(session, url):
        raise RuntimeError("HTTP 500")

    a, b, c, d = _abruf_patches(kaputt)
    with a, b, c, d:
        assert await p.async_fetch(force=True) is False
        status = p.status(JETZT)

    assert p.hat_daten
    assert status["fehler"] == "HTTP 500"
    assert status["tage_kwh"] == alt


async def test_ohne_flaechen_oder_standort_kein_abruf():
    p = _provider({})
    a, b, c, d = _abruf_patches()
    with a, b as hole, c, d:
        assert await p.async_fetch(force=True) is False
        assert hole.await_count == 0
    assert "Fläche" in p.status(JETZT)["fehler"]

    p = _provider()
    p._hass.config.latitude = None  # nicht gesetzt → kein float
    a, b, c, d = _abruf_patches()
    with a, b as hole, c, d:
        assert await p.async_fetch(force=True) is False
        assert hole.await_count == 0
    assert "Standort" in p.status(JETZT)["fehler"]


async def test_reihe_verfaellt_nach_48_stunden():
    p = _provider()
    a, b, c, d = _abruf_patches()
    with a, b, c, d:
        await p.async_fetch(force=True)

    spaeter = JETZT + timedelta(hours=49)
    assert p.reihe(JETZT + timedelta(hours=47)) is not None
    assert p.reihe(spaeter) is None
    assert p.halbstunden(spaeter) == {}
    assert p.tage_kwh(spaeter) is None
    status = p.status(spaeter)
    assert status["veraltet"] is True
    assert status["tage_kwh"] is None


def test_update_config_liest_die_anlage():
    p = _provider()
    assert [f.name for f in p.flaechen] == ["Süd", "Ost"]
    assert p.kwp_gesamt == 8.0
    p.update_config({"pv_flaechen": [{"kwp": 2}], "inverter_ac_limit_kw": "x"})
    assert p.kwp_gesamt == 2.0
    assert p._ac_limit_kw is None


async def test_berechne_einmalig_speichert_nichts():
    hass = _hass()
    a, b, c, d = _abruf_patches()
    with a, b, c, d:
        ergebnis = await prov.berechne_einmalig(hass, CONFIG["pv_flaechen"], 14, 6)

    assert ergebnis["kwp_gesamt"] == 8.0
    assert len(ergebnis["tage_kwh"]) == 7
    assert ergebnis["tage_kwh"][0] > 20.0
    assert ergebnis["standort"] == {"breite": 48.2, "laenge": 14.3}
    assert ergebnis["spitze_kw"] <= 6.0
    assert ergebnis["flaechen"][0]["name"] == "Süd"


async def test_berechne_einmalig_meldet_eingabe_und_standortfehler():
    hass = _hass()
    a, b, c, d = _abruf_patches()
    with a, b, c, d:
        with pytest.raises(ValueError, match="Fläche"):
            await prov.berechne_einmalig(hass, [], 14, None)
        hass.config.latitude = None
        with pytest.raises(ValueError, match="Standort"):
            await prov.berechne_einmalig(hass, CONFIG["pv_flaechen"], 14, None)


# ---------------------------------------------------------------------------
# Weg in den Fahrplan und in die Sensoren
# ---------------------------------------------------------------------------


def _fake_prognose(halb: dict, fehler: str | None = None, alter_s: float = 60.0):
    fake = MagicMock()
    fake.halbstunden.return_value = halb
    fake.status.return_value = {"fehler": fehler, "veraltet": False, "quelle": "open_meteo"}
    fake.alter_s.return_value = alter_s
    return fake


async def test_fahrplan_nimmt_die_eigene_prognose_ohne_p10():
    from tests import test_schedule as ts
    from custom_components.eeg_energy_optimizer import schedule as sched

    start = ts.NOW.replace(minute=0, second=0, microsecond=0)
    halb = {start + timedelta(minutes=30 * k): round(0.5 + k * 0.01, 4) for k in range(96)}
    hass = ts._hass_with({**ts.BASE_CONFIG, "forecast_source": "eigen"})
    hass.data[sched.DOMAIN]["entry1"]["pvprognose"] = _fake_prognose(halb)

    with patch.object(sched, "_now_local", return_value=ts.NOW):
        inputs, problem = await sched.async_collect_inputs(hass, "entry1")

    assert problem is None
    assert inputs.min_production_kw is None
    # Ab dem zweiten Stützpunkt steht die Prognose, der erste ist die Messung
    # (hier nicht lesbar → Prognose bleibt)
    assert inputs.production_kw[1] == pytest.approx(halb[start + timedelta(minutes=30)])
    assert inputs.production_kw[10] == pytest.approx(halb[start + timedelta(minutes=300)])
    assert len(inputs.production_kw) == len(inputs.timestamps)


async def test_fahrplan_ohne_wetterdaten_nennt_den_grund():
    from tests import test_schedule as ts
    from custom_components.eeg_energy_optimizer import schedule as sched

    hass = ts._hass_with({**ts.BASE_CONFIG, "forecast_source": "eigen"})
    hass.data[sched.DOMAIN]["entry1"]["pvprognose"] = _fake_prognose({}, fehler="HTTP 500")

    with patch.object(sched, "_now_local", return_value=ts.NOW):
        inputs, problem = await sched.async_collect_inputs(hass, "entry1")

    assert inputs is None
    assert "HTTP 500" in problem

    del hass.data[sched.DOMAIN]["entry1"]["pvprognose"]
    with patch.object(sched, "_now_local", return_value=ts.NOW):
        inputs, problem = await sched.async_collect_inputs(hass, "entry1")
    assert inputs is None
    assert "nicht geladen" in problem


async def test_sensor_traegt_tagessummen_und_stand():
    from custom_components.eeg_energy_optimizer.forecast_provider import EigenProvider
    from custom_components.eeg_energy_optimizer.sensor import (
        PVForecastTodaySensor,
        PVForecastTomorrowSensor,
    )

    prognose = MagicMock()
    prognose.rest_heute_kwh.return_value = 12.5
    prognose.morgen_kwh.return_value = 30.1
    prognose.tage_kwh.return_value = [25.0, 30.1, 18.0, 0.0, 0.0, 0.0, 0.0]
    prognose.status.return_value = {
        "quelle": "open_meteo", "geholt": "2026-09-27T10:00:00+00:00",
        "alter_minuten": 3, "fehler": None,
    }
    provider = EigenProvider(MagicMock(), prognose)
    entry = MagicMock()
    entry.entry_id = "entry1"

    heute = PVForecastTodaySensor(MagicMock(), entry, provider)
    morgen = PVForecastTomorrowSensor(MagicMock(), entry, provider)
    await heute.async_update()
    await morgen.async_update()

    assert heute.native_value == 12.5
    assert morgen.native_value == 30.1
    attrs = heute.extra_state_attributes
    assert attrs["heute_gesamt_kwh"] == 25.0
    assert attrs["tage_kwh"][1] == 30.1
    assert attrs["quelle"] == "open_meteo"
    assert attrs["alter_minuten"] == 3


async def test_solcast_sensor_bleibt_ohne_attribute():
    """Der alte Weg darf sich nicht ändern: kein tage_kwh am Provider → keine Attribute."""
    from custom_components.eeg_energy_optimizer.forecast_provider import PVForecast
    from custom_components.eeg_energy_optimizer.sensor import PVForecastTodaySensor

    provider = MagicMock(spec=["get_forecast"])
    provider.get_forecast.return_value = PVForecast(remaining_today_kwh=4.0, tomorrow_kwh=5.0)
    entry = MagicMock()
    entry.entry_id = "entry1"
    sensor = PVForecastTodaySensor(MagicMock(), entry, provider)
    await sensor.async_update()
    assert sensor.native_value == 4.0
    assert sensor.extra_state_attributes == {}


# ---------------------------------------------------------------------------
# Sensoren der eigenen Prognose
# ---------------------------------------------------------------------------


async def test_leistung_jetzt_ist_die_laufende_viertelstunde():
    p = _provider()
    a, b, c, d = _abruf_patches()
    with a, b, c, d:
        await p.async_fetch(force=True)
    reihe = p.reihe(JETZT)
    # JETZT = 10:00 UTC liegt im Slot 10:00–10:15, Intervall-Ende 10:15
    erwartet = reihe.kw[reihe.ende.index(_utc(27, 10, 15))]
    assert p.leistung_jetzt_kw(JETZT) == erwartet
    assert p.leistung_jetzt_kw(JETZT + timedelta(minutes=14)) == erwartet
    assert p.leistung_jetzt_kw(_utc(30, 12, 0)) is None  # hinter der Reihe


def _sensor_umgebung(config):
    from custom_components.eeg_energy_optimizer import sensor as sen

    prognose = MagicMock()
    prognose.leistung_jetzt_kw.return_value = 3.2
    prognose.rest_heute_kwh.return_value = 12.0
    prognose.tage_kwh.return_value = [25.0, 30.0, 18.0, 17.0, 16.0, 15.0, 14.0]
    prognose.halbstunden.return_value = {_utc(27, 10, 30): 3.4, _utc(27, 10, 0): 3.1}
    prognose.status.return_value = {"geholt": "x", "alter_minuten": 4, "fehler": None, "kwp_gesamt": 8.0}
    entry = MagicMock()
    entry.entry_id = "entry1"
    return sen, sen.eigene_prognose_sensoren(MagicMock(), entry, config, {"pvprognose": prognose})


async def test_neun_sensoren_mit_eigener_quelle_oder_vergleich():
    _, keine = _sensor_umgebung({"forecast_source": "solcast_solar"})
    assert keine == []
    _, als_quelle = _sensor_umgebung({"forecast_source": "eigen"})
    _, als_vergleich = _sensor_umgebung({"forecast_source": "solcast_solar", "pv_prognose_vergleich": True})
    assert len(als_quelle) == len(als_vergleich) == 9
    ids = [s._attr_unique_id for s in als_quelle]
    assert len(set(ids)) == 9
    assert all(i.startswith("eeg_energy_optimizer_entry1_eigene_prognose_") for i in ids)


async def test_sensorwerte_und_attribute():
    _, sensoren = _sensor_umgebung({"forecast_source": "eigen"})
    nach_art = {s._art: s for s in sensoren}
    for s in sensoren:
        await s.async_update()
    assert nach_art["leistung"].native_value == 3.2
    assert nach_art["leistung"]._attr_native_unit_of_measurement == "kW"
    assert nach_art["rest_heute"].native_value == 12.0
    assert nach_art["heute"].native_value == 25.0
    assert nach_art["morgen"].native_value == 30.0
    assert nach_art["tag_3"].native_value == 18.0  # übermorgen, wie bei Solcast
    assert nach_art["tag_7"].native_value == 14.0
    assert nach_art["tag_7"]._attr_name == "Eigene PV-Prognose Tag 7"
    halb = nach_art["leistung"].extra_state_attributes["prognose_halbstunden"]
    assert [h["kw"] for h in halb] == [3.1, 3.4]  # zeitlich sortiert
    assert "prognose_halbstunden" in nach_art["leistung"]._unrecorded_attributes
    # Kein detailedForecast: die Solcast-Suche darf diese Sensoren nie sehen
    assert all("detailedForecast" not in s.extra_state_attributes for s in sensoren)
    assert nach_art["heute"].extra_state_attributes["alter_minuten"] == 4


async def test_solcast_suche_ignoriert_die_eigenen_sensoren():
    from custom_components.eeg_energy_optimizer import schedule as sched

    _, sensoren = _sensor_umgebung({"forecast_source": "eigen"})
    for s in sensoren:
        await s.async_update()
    hass = MagicMock()
    hass.states.async_all.return_value = [
        MagicMock(entity_id=f"sensor.x{i}", attributes=s.extra_state_attributes)
        for i, s in enumerate(sensoren)
    ]
    assert sched._solcast_detailed(hass) == {}


async def test_ohne_daten_bleiben_die_sensoren_leer():
    _, sensoren = _sensor_umgebung({"forecast_source": "eigen"})
    p = sensoren[0]._prognose
    p.leistung_jetzt_kw.return_value = None
    p.rest_heute_kwh.return_value = None
    p.tage_kwh.return_value = None
    p.halbstunden.return_value = {}
    for s in sensoren:
        await s.async_update()
        assert s.native_value is None
