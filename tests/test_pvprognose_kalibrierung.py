"""Tests für die Kalibrierung der eigenen PV-Prognose (pvprognose/kalibrierung.py).

Geprüft werden der Sonnenstand, das Lernen aus Tagen des Prognosevergleichs
samt seiner Filter (Kennung, Abregelung, Wettertage), die Interpolation und
der Weg durch Modell und Provider — dort vor allem, dass die ROHE Reihe
unverändert bleibt, denn aus ihr wird gelernt.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from custom_components.eeg_energy_optimizer.pvprognose import kalibrierung as kal
from custom_components.eeg_energy_optimizer.pvprognose import modell, openmeteo
from custom_components.eeg_energy_optimizer.pvprognose import provider as prov

UTC = timezone.utc
TRAUN = (48.226, 14.22)
KENNUNG = "k1"


def _utc(tag: int, stunde: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, tag, stunde, minute, tzinfo=UTC)


def _tag(tag: int, faktor: float, prognose_kw: float = 3.0, **extra) -> dict:
    """Ein Tag: rohe Prognose konstant von 06:00 bis 15:00 UTC, gemessen × faktor."""
    slots = [_utc(tag, 6) + timedelta(minutes=30 * i) for i in range(18)]
    roh = {s.isoformat(): prognose_kw for s in slots}
    return {
        "datum": f"2026-09-{tag:02d}",
        "eigen_roh": roh,
        "eigen_kennung": KENNUNG,
        "gemessen": {k: v * faktor for k, v in roh.items()},
        **extra,
    }


# ---------------------------------------------------------------------------
# Sonnenstand
# ---------------------------------------------------------------------------


def test_sonnenstand_mittag_und_morgen():
    # Wahrer Mittag in Traun Ende September gegen 10:52 UTC: Süd, Höhe ≈ 90 − 48,2 − 2
    azimut, hoehe = kal.sonnenstand(_utc(28, 10, 52), *TRAUN)
    assert azimut == pytest.approx(180.0, abs=2.0)
    assert hoehe == pytest.approx(39.8, abs=1.0)
    # Morgens im Osten, knapp über dem Horizont
    azimut, hoehe = kal.sonnenstand(_utc(28, 5, 30), *TRAUN)
    assert 90.0 < azimut < 110.0
    assert 0.0 < hoehe < 8.0
    # Nachts unter dem Horizont
    assert kal.sonnenstand(_utc(28, 0, 0), *TRAUN)[1] < 0


# ---------------------------------------------------------------------------
# Lernen
# ---------------------------------------------------------------------------


def test_ohne_daten_ueberall_eins():
    leer = kal.lerne([], *TRAUN, KENNUNG, None, None)
    assert not leer.aktiv
    assert leer.faktor(_utc(28, 8)) == 1.0


def test_lernt_den_systematischen_fehler_und_schrumpft_zu_eins():
    """Die Messung liegt 25 % über der Prognose: der Faktor wandert dahin,
    mit einem Tag nur ein Stück, mit vielen fast ganz."""
    ein_tag = kal.lerne([_tag(20, 1.25)], *TRAUN, KENNUNG, None, None)
    viele = kal.lerne([_tag(d, 1.25) for d in range(1, 29)], *TRAUN, KENNUNG, None, None)
    t = _utc(28, 9)
    assert 1.0 < ein_tag.faktor(t) < viele.faktor(t) <= 1.25
    assert viele.faktor(t) > 1.15  # jedes Feld sieht nur wenige der 28 Tage
    assert viele.tage == 28
    # Nachts und an nie gesehenen Sonnenständen: 1
    assert viele.faktor(_utc(28, 1)) == 1.0
    assert viele.faktor(_utc(28, 17, 30)) == 1.0


def test_fremde_kennung_zaehlt_nicht():
    """Andere Flächen oder Wettermodelle: der alte Fehler gilt nicht mehr."""
    alt = _tag(20, 1.4)
    alt["eigen_kennung"] = "vorher"
    ohne = _tag(21, 1.4)
    del ohne["eigen_kennung"]
    k = kal.lerne([alt, ohne], *TRAUN, KENNUNG, None, None)
    assert not k.aktiv


def test_wettertag_faellt_raus():
    """Regen statt Sonne sagt nichts über die Anlage."""
    k = kal.lerne([_tag(20, 0.3)], *TRAUN, KENNUNG, None, None)
    assert not k.aktiv
    assert k.ausgelassen_tage == 1


def test_abregelung_an_der_exportgrenze_zaehlt_nicht():
    """Einspeisung an der Grenze: die Messung zeigt, was abgenommen wurde,
    nicht, was die Module konnten."""
    tag = _tag(20, 0.8)
    tag["netz"] = {k: 3.9 for k in tag["eigen_roh"]}
    k = kal.lerne([tag], *TRAUN, KENNUNG, 4.0, None)
    assert not k.aktiv
    assert k.ausgelassen_abregelung == 18
    # Unter der Grenze gilt die Halbstunde
    tag["netz"] = {k: 2.0 for k in tag["eigen_roh"]}
    assert kal.lerne([tag], *TRAUN, KENNUNG, 4.0, None).aktiv


def test_nulleinspeisung_nur_mit_voller_batterie_abgeregelt():
    tag = _tag(20, 0.8)
    tag["netz"] = {k: 0.0 for k in tag["eigen_roh"]}
    tag["soc"] = {k: 60.0 for k in tag["eigen_roh"]}
    assert kal.lerne([tag], *TRAUN, KENNUNG, 0.0, None).aktiv
    tag["soc"] = {k: 100.0 for k in tag["eigen_roh"]}
    assert not kal.lerne([tag], *TRAUN, KENNUNG, 0.0, None).aktiv


def test_nahe_der_ac_grenze_zaehlt_nicht():
    k = kal.lerne([_tag(20, 1.0, prognose_kw=7.8)], *TRAUN, KENNUNG, None, 8.0)
    assert not k.aktiv


def test_faktor_gedeckelt():
    viele = kal.lerne([_tag(d, 1.6) for d in range(1, 29)], *TRAUN, KENNUNG, None, None)
    assert viele.status()["faktor_max"] <= kal.FAKTOR_MAX


# ---------------------------------------------------------------------------
# Modell und Provider
# ---------------------------------------------------------------------------


def _wetter(gti: float) -> openmeteo.Wetterreihe:
    ende = [_utc(28, 9) + timedelta(minutes=15 * i) for i in range(4)]
    return openmeteo.Wetterreihe(ende=ende, gti_w_m2=[gti] * 4, temp_c=[20.0] * 4)


def test_faktor_wirkt_vor_dem_deckel():
    flaeche = modell.Flaeche("Süd", 10.0, 30.0, 180.0)
    paare = [(flaeche, _wetter(700.0))]
    roh = modell.leistungsreihe(paare, 10.0, 8.0)
    hoch = modell.leistungsreihe(paare, 10.0, 8.0, lambda t: 1.5)
    runter = modell.leistungsreihe(paare, 10.0, 8.0, lambda t: 0.5)
    assert roh.kw[0] < 8.0
    assert hoch.kw[0] == 8.0  # nach oben korrigiert, dann gedeckelt
    assert runter.kw[0] == pytest.approx(roh.kw[0] * 0.5, rel=1e-3)


def _hass():
    hass = MagicMock()
    hass.config.latitude = TRAUN[0]
    hass.config.longitude = TRAUN[1]
    return hass


CONFIG = {
    "pv_flaechen": [{"name": "Traun", "kwp": 7.65, "neigung": 33, "azimut": 150}],
    "pv_verluste_pct": 10,
    "inverter_ac_limit_kw": 8,
}


def _provider():
    with patch.object(prov, "Store", None):
        p = prov.PvPrognoseProvider(_hass(), "entry1", CONFIG)
    p._paare = [(p.flaechen[0], _wetter(500.0))]
    p._geholt = _utc(28, 9)
    p._neu_rechnen()
    return p


def test_provider_liefert_kalibriert_und_roh():
    p = _provider()
    jetzt = _utc(28, 9, 30)
    vorher = p.halbstunden(jetzt)
    tage = [_tag(d, 1.25) for d in range(1, 29)]
    for t in tage:
        t["eigen_kennung"] = p.modellkennung()
    p.kalibrieren(tage)
    assert p.kalibrierung_status()["aktiv"]
    nachher = p.halbstunden(jetzt)
    roh = p.halbstunden(jetzt, roh=True)
    assert roh == vorher
    k = next(iter(nachher))
    assert nachher[k] > roh[k] * 1.15
    assert p.status(jetzt)["kalibrierung"]["tage"] == 28


def test_modellkennung_haengt_an_anlage_und_wettermodellen():
    p = _provider()
    k1 = p.modellkennung()
    assert "icon_seamless" in k1
    p.update_config({**CONFIG, "pv_verluste_pct": 12})
    assert p.modellkennung() != k1


async def test_wetter_ueberlebt_den_neustart():
    """Mit dem gespeicherten Wetter greift eine Kalibrierung ohne Abruf."""
    gespeichert = {}

    class _Store:
        def __init__(self, *a, **k):
            pass

        async def async_save(self, daten):
            gespeichert.update(daten)

        async def async_load(self):
            return gespeichert

    with patch.object(prov, "Store", _Store):
        p = prov.PvPrognoseProvider(_hass(), "entry1", CONFIG)
        p._paare = [(p.flaechen[0], _wetter(500.0))]
        p._geholt = _utc(28, 9)
        p._neu_rechnen()
        await p._async_save()
        neu = prov.PvPrognoseProvider(_hass(), "entry1", CONFIG)
        await neu.async_load()
    assert len(neu._paare) == 1
    assert neu._paare[0][1].gti_w_m2 == [500.0] * 4
