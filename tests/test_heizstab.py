"""Tests für den Heizstab (heizstab/controller.py).

Zwei Ebenen: die Überschuss-Regel als reine Funktion (``naechster_sollwert``)
und der Controller mit Temperatur-Hysteresen, Vorrang und Schreibpfad. Der
Treiber ist ein Mock — geschrieben wird ausschließlich über
``async_set_power``; die Werte des Geräts kommen aus dessen Cache.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.eeg_energy_optimizer.const import (
    CONF_HEIZSTAB_ENABLED,
    CONF_HEIZSTAB_HOST,
    CONF_HEIZSTAB_MAX_KW,
    CONF_HEIZSTAB_MINTEMP_C,
    CONF_HEIZSTAB_NETZBEZUG,
    CONF_HEIZSTAB_VORRANG,
    CONF_HEIZSTAB_WAERMEWERT,
    CONF_HEIZSTAB_ZIELTEMP_C,
    HEIZSTAB_KOMFORT_EXPORT_ZIEL_KW,
    HEIZSTAB_STEP_KW,
)
from custom_components.eeg_energy_optimizer.heizstab.controller import (
    HeizstabController,
    create_heizstab,
    heizstab_max_kw,
    heizstab_waermewert,
    naechster_sollwert,
)


def _cfg(**over):
    cfg = {
        CONF_HEIZSTAB_ENABLED: True,
        CONF_HEIZSTAB_HOST: "192.168.1.58",
        CONF_HEIZSTAB_MAX_KW: 6.0,
        CONF_HEIZSTAB_ZIELTEMP_C: 80.0,
        CONF_HEIZSTAB_MINTEMP_C: 0.0,
        CONF_HEIZSTAB_VORRANG: True,
        CONF_HEIZSTAB_WAERMEWERT: 0.08,
    }
    cfg.update(over)
    return cfg


def _treiber(power_w=None, temp=None, connected=True):
    t = MagicMock()
    t.last_power_w = power_w
    t.last_temperature = temp
    t.connected = connected
    t.last_error = None
    t.async_set_power = AsyncMock(return_value=True)
    t.async_read_sensors = AsyncMock()
    t.async_sync_time = AsyncMock(return_value=True)
    t.async_close = AsyncMock()
    return t


# ---------------------------------------------------------------------------
# Die Regel als reine Funktion
# ---------------------------------------------------------------------------


def test_am_limit_ein_schritt_hinauf():
    neu, grund = naechster_sollwert(1.0, export_kw=4.0, grenze_kw=4.0, max_kw=6.0, vorrang_frei=True)
    assert neu == pytest.approx(1.0 + HEIZSTAB_STEP_KW)
    assert "angehoben" in grund


def test_am_limit_nicht_ueber_das_maximum():
    neu, _ = naechster_sollwert(5.8, export_kw=4.05, grenze_kw=4.0, max_kw=6.0, vorrang_frei=True)
    assert neu == pytest.approx(6.0)
    neu, grund = naechster_sollwert(6.0, export_kw=4.0, grenze_kw=4.0, max_kw=6.0, vorrang_frei=True)
    assert neu == pytest.approx(6.0)
    assert "Maximum" in grund


def test_am_limit_ohne_vorrang_halten():
    """Batterie-Vorrang und Batterie noch nicht gesättigt → der Heizstab wartet."""
    neu, grund = naechster_sollwert(1.5, export_kw=4.0, grenze_kw=4.0, max_kw=6.0, vorrang_frei=False)
    assert neu == pytest.approx(1.5)
    assert "Batterie hat Vorrang" in grund


def test_unter_dem_limit_um_die_luecke_hinunter():
    """Export 3,2 kW bei Grenze 4 kW: Lücke 0,8 kW, in EINEM Lauf zurück."""
    neu, grund = naechster_sollwert(3.0, export_kw=3.2, grenze_kw=4.0, max_kw=6.0, vorrang_frei=True)
    assert neu == pytest.approx(2.2)
    assert "zurückgenommen" in grund


def test_netzbezug_sofort_null():
    neu, grund = naechster_sollwert(3.0, export_kw=-0.4, grenze_kw=4.0, max_kw=6.0, vorrang_frei=True)
    assert neu == 0.0
    assert "Netzbezug" in grund


def test_totes_band_haelt():
    """Zwischen Grenze − 0,3 und Grenze − 0,1: nichts tun."""
    neu, grund = naechster_sollwert(2.0, export_kw=3.8, grenze_kw=4.0, max_kw=6.0, vorrang_frei=True)
    assert neu == pytest.approx(2.0)
    assert "totes Band" in grund


def test_ohne_messwert_null():
    neu, grund = naechster_sollwert(2.0, export_kw=None, grenze_kw=4.0, max_kw=6.0, vorrang_frei=True)
    assert neu == 0.0
    assert "Messwert" in grund


def test_rampe_erreicht_das_maximum_in_zwoelf_laeufen():
    """0,5 kW je 30 s: 6 kW nach 12 Läufen (6 Minuten)."""
    soll = 0.0
    for _ in range(12):
        soll, _ = naechster_sollwert(soll, export_kw=4.0, grenze_kw=4.0, max_kw=6.0, vorrang_frei=True)
    assert soll == pytest.approx(6.0)


# ---------------------------------------------------------------------------
# Konfigurationshelfer
# ---------------------------------------------------------------------------


def test_max_kw_null_ohne_heizstab_und_vorgabe_bei_leerem_feld():
    assert heizstab_max_kw({CONF_HEIZSTAB_ENABLED: False, CONF_HEIZSTAB_MAX_KW: 6}) == 0.0
    # Leeres Panel-Zahlenfeld kommt als 0 an → Vorgabe, kein 0-kW-Heizstab.
    assert heizstab_max_kw({CONF_HEIZSTAB_ENABLED: True, CONF_HEIZSTAB_MAX_KW: 0}) == 6.0
    assert heizstab_max_kw({CONF_HEIZSTAB_ENABLED: True, CONF_HEIZSTAB_MAX_KW: 3}) == 3.0


def test_waermewert_nur_mit_heizstab():
    assert heizstab_waermewert(_cfg()) == pytest.approx(0.08)
    assert heizstab_waermewert(_cfg(**{CONF_HEIZSTAB_ENABLED: False})) == 0.0
    assert heizstab_waermewert(_cfg(**{CONF_HEIZSTAB_WAERMEWERT: -1})) == 0.0


def test_create_heizstab_none_ohne_konfiguration():
    assert create_heizstab(MagicMock(), {}) is None
    assert create_heizstab(MagicMock(), {CONF_HEIZSTAB_ENABLED: False}) is None


def test_create_heizstab_ohne_host_rechnet_aber_schreibt_nicht():
    hz = create_heizstab(MagicMock(), _cfg(**{CONF_HEIZSTAB_HOST: ""}))
    assert hz is not None
    assert hz.verfuegbar is False
    assert hz.gesaettigt is True  # nicht steuerbar zählt als gesättigt


# ---------------------------------------------------------------------------
# Controller: Temperatur-Hysteresen
# ---------------------------------------------------------------------------


def test_zieltemperatur_sperrt_mit_hysterese():
    t = _treiber(power_w=0, temp=80.0)
    hz = HeizstabController(MagicMock(), _cfg(), t)
    hz.pruefe_temperaturen()
    assert hz.ziel_erreicht is True
    soll, grund = hz.regeln(entladung=False, export_kw=4.0, grenze_kw=4.0, vorrang_frei=True)
    assert soll == 0.0 and "Zieltemperatur" in grund

    # 78 °C: noch gesperrt (Hysterese 3 K)
    t.last_temperature = 78.0
    hz.pruefe_temperaturen()
    assert hz.ziel_erreicht is True
    # 76,5 °C: wieder frei
    t.last_temperature = 76.5
    hz.pruefe_temperaturen()
    assert hz.ziel_erreicht is False
    soll, _ = hz.regeln(entladung=False, export_kw=4.0, grenze_kw=4.0, vorrang_frei=True)
    assert soll == pytest.approx(HEIZSTAB_STEP_KW)


def test_mindesttemperatur_mit_netzbezug_heizt_voll_und_schlaegt_entladung():
    t = _treiber(power_w=0, temp=38.0)
    hz = HeizstabController(
        MagicMock(),
        _cfg(**{CONF_HEIZSTAB_MINTEMP_C: 40.0, CONF_HEIZSTAB_NETZBEZUG: True}),
        t,
    )
    hz.pruefe_temperaturen()
    assert hz.komfort_aktiv is True
    assert hz.komfort_aus_netz is True
    # Auch bei laufender Entladung und ohne Einspeisung: volle Leistung.
    soll, grund = hz.regeln(entladung=True, export_kw=-2.0, grenze_kw=4.0, vorrang_frei=False)
    assert soll == pytest.approx(6.0)
    assert "auch aus dem Netz" in grund

    # Ende erst 5 K über dem Minimum.
    t.last_temperature = 44.0
    hz.pruefe_temperaturen()
    assert hz.komfort_aktiv is True
    t.last_temperature = 45.0
    hz.pruefe_temperaturen()
    assert hz.komfort_aktiv is False


def test_ohne_mindesttemperatur_kein_komfort():
    t = _treiber(power_w=0, temp=20.0)
    hz = HeizstabController(MagicMock(), _cfg(**{CONF_HEIZSTAB_MINTEMP_C: 0}), t)
    hz.pruefe_temperaturen()
    assert hz.komfort_aktiv is False


def test_ohne_fuehlerwert_kein_komfort_aber_regeln_laeuft():
    """Der Ohmpilot meldet ohne Fühler 0,0 °C — kein Messwert, kein Heizen ins Blaue."""
    t = _treiber(power_w=0, temp=0.0)
    hz = HeizstabController(MagicMock(), _cfg(**{CONF_HEIZSTAB_MINTEMP_C: 40.0}), t)
    hz.pruefe_temperaturen()
    assert hz.temperatur_c is None
    assert hz.komfort_aktiv is False
    soll, _ = hz.regeln(entladung=False, export_kw=4.0, grenze_kw=4.0, vorrang_frei=True)
    assert soll == pytest.approx(HEIZSTAB_STEP_KW)


def test_entladung_sperrt_den_heizstab():
    hz = HeizstabController(MagicMock(), _cfg(), _treiber(power_w=0, temp=50.0))
    hz.sollwert_kw = 3.0
    soll, grund = hz.regeln(entladung=True, export_kw=4.0, grenze_kw=4.0, vorrang_frei=True)
    assert soll == 0.0 and "Entladung" in grund


# ---------------------------------------------------------------------------
# Controller: Sättigung und Messwerte
# ---------------------------------------------------------------------------


def test_gesaettigt_am_maximum_und_bei_zieltemperatur():
    t = _treiber(power_w=5900, temp=60.0)
    hz = HeizstabController(MagicMock(), _cfg(), t)
    hz.pruefe_temperaturen()
    hz.sollwert_kw = 2.0
    assert hz.gesaettigt is False
    hz.sollwert_kw = 6.0
    assert hz.gesaettigt is True
    hz.sollwert_kw = 1.0
    t.last_temperature = 81.0
    hz.pruefe_temperaturen()
    assert hz.gesaettigt is True


def test_nicht_erreichbar_zaehlt_als_gesaettigt():
    t = _treiber(power_w=None, temp=None, connected=False)
    hz = HeizstabController(MagicMock(), _cfg(), t)
    assert hz.verfuegbar is False
    assert hz.gesaettigt is True


def test_leistung_und_temperatur_aus_dem_treiber_cache():
    hz = HeizstabController(MagicMock(), _cfg(), _treiber(power_w=2450, temp=61.3))
    assert hz.leistung_kw == pytest.approx(2.45)
    assert hz.temperatur_c == pytest.approx(61.3)
    st = hz.status()
    assert st["leistung_kw"] == pytest.approx(2.45)
    assert st["max_kw"] == 6.0
    assert st["vorrang_heizstab"] is True


# ---------------------------------------------------------------------------
# Controller: Schreibpfad
# ---------------------------------------------------------------------------


async def test_sollwert_aenderung_schreibt_sofort_in_watt():
    t = _treiber(power_w=0, temp=50.0)
    hz = HeizstabController(MagicMock(), _cfg(), t)
    await hz.async_set_sollwert(2.5, "Test")
    t.async_set_power.assert_awaited_once_with(2500)
    assert hz.grund == "Test"
    assert hz.last_write_ok is True


async def test_kleine_aenderung_schreibt_nicht_sofort():
    """Unter 50 W wartet der Wert auf den 30-s-Takt (Watchdog schreibt ohnehin)."""
    t = _treiber(power_w=0, temp=50.0)
    hz = HeizstabController(MagicMock(), _cfg(), t)
    hz.sollwert_kw = 2.0
    await hz.async_set_sollwert(2.02, "Test")
    t.async_set_power.assert_not_awaited()
    assert hz.sollwert_kw == pytest.approx(2.02)


async def test_watchdog_takt_schreibt_immer_den_aktuellen_wert():
    t = _treiber(power_w=0, temp=50.0)
    hz = HeizstabController(MagicMock(), _cfg(), t)
    hz.sollwert_kw = 1.5
    assert await hz.async_schreiben() is True
    t.async_set_power.assert_awaited_once_with(1500)


async def test_schreibfehler_wird_gezaehlt():
    t = _treiber(power_w=0, temp=50.0)
    t.async_set_power = AsyncMock(return_value=False)
    hz = HeizstabController(MagicMock(), _cfg(), t)
    await hz.async_set_sollwert(3.0, "Test")
    assert hz.last_write_ok is False
    assert hz.write_failures == 1


async def test_shutdown_schreibt_null_und_schliesst():
    t = _treiber(power_w=3000, temp=50.0)
    hz = HeizstabController(MagicMock(), _cfg(), t)
    hz.sollwert_kw = 3.0
    await hz.async_shutdown()
    t.async_set_power.assert_awaited_once_with(0)
    t.async_close.assert_awaited_once()
    assert hz.sollwert_kw == 0.0


async def test_lesen_ruft_listener():
    t = _treiber(power_w=1000, temp=50.0)
    hz = HeizstabController(MagicMock(), _cfg(), t)
    aufrufe = []
    entfernen = hz.add_listener(lambda: aufrufe.append(1))
    await hz.async_lesen()
    t.async_read_sensors.assert_awaited_once()
    assert aufrufe == [1]
    entfernen()
    await hz.async_lesen()
    assert aufrufe == [1]


def test_ohne_treiber_rechnet_der_controller_trotzdem():
    hz = HeizstabController(MagicMock(), _cfg(), None)
    assert hz.leistung_kw is None
    assert hz.temperatur_c is None
    soll, _ = hz.regeln(entladung=False, export_kw=4.0, grenze_kw=4.0, vorrang_frei=True)
    assert soll == pytest.approx(HEIZSTAB_STEP_KW)


# ---------------------------------------------------------------------------
# Mindesttemperatur OHNE Netzbezug (Vorgabe): Vorrang vor der Einspeisung
# ---------------------------------------------------------------------------


def _komfort_ohne_netz(temp=38.0, export=None):
    t = _treiber(power_w=0, temp=temp)
    hz = HeizstabController(MagicMock(), _cfg(**{CONF_HEIZSTAB_MINTEMP_C: 40.0}), t)
    hz.pruefe_temperaturen()
    return hz


def test_netzbezug_ist_standardmaessig_aus():
    hz = _komfort_ohne_netz()
    assert hz.netzbezug_erlaubt is False
    assert hz.komfort_aktiv is True
    assert hz.komfort_aus_netz is False


def test_komfort_ohne_netz_nimmt_die_einspeisung_unter_der_grenze():
    """Einspeisung 1,5 kW, Grenze 4 kW: normal bliebe der Heizstab aus —
    unter der Mindesttemperatur regelt er auf Einspeisung ≈ 0 und legt zu."""
    hz = _komfort_ohne_netz()
    soll, grund = hz.regeln(entladung=False, export_kw=1.5, grenze_kw=4.0, vorrang_frei=False)
    assert soll == pytest.approx(HEIZSTAB_STEP_KW)
    assert "Vorrang vor der Einspeisung" in grund


def test_komfort_ohne_netz_nie_aus_dem_netz():
    """Netzbezug 0,6 kW → Sollwert um die Lücke zurück, bis er 0 erreicht."""
    hz = _komfort_ohne_netz()
    hz.sollwert_kw = 3.0
    soll, _ = hz.regeln(entladung=False, export_kw=-0.6, grenze_kw=4.0, vorrang_frei=False)
    assert soll == pytest.approx(3.0 - (HEIZSTAB_KOMFORT_EXPORT_ZIEL_KW + 0.6))
    hz.sollwert_kw = 0.5
    soll, grund = hz.regeln(entladung=False, export_kw=-0.6, grenze_kw=4.0, vorrang_frei=False)
    assert soll == 0.0
    assert "Netzbezug" in grund


def test_komfort_ohne_netz_haelt_in_der_nullnaehe():
    """Einspeisung 0,1 kW: totes Band — kein Nachfassen, kein Netzbezug."""
    hz = _komfort_ohne_netz()
    hz.sollwert_kw = 2.0
    soll, _ = hz.regeln(entladung=False, export_kw=0.1, grenze_kw=4.0, vorrang_frei=False)
    assert soll == pytest.approx(2.0)


def test_komfort_ohne_netz_nie_aus_der_batterie():
    """Bei einer Entladung ins Netz bleibt der Heizstab aus — auch unter der
    Mindesttemperatur, denn jede Kilowattstunde käme aus der Batterie."""
    hz = _komfort_ohne_netz()
    hz.sollwert_kw = 2.0
    soll, grund = hz.regeln(entladung=True, export_kw=3.0, grenze_kw=4.0, vorrang_frei=False)
    assert soll == 0.0
    assert "Entladung" in grund


def test_komfort_ohne_netz_ignoriert_batterie_vorrang():
    """Der Vorrang-Schalter regelt den Überschuss ÜBER der Grenze; unter der
    Mindesttemperatur hat der Heizstab Vorrang vor der Einspeisung, egal wie
    der Schalter steht."""
    hz = _komfort_ohne_netz()
    soll, _ = hz.regeln(entladung=False, export_kw=2.0, grenze_kw=4.0, vorrang_frei=False)
    assert soll == pytest.approx(HEIZSTAB_STEP_KW)
