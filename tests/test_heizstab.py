"""Tests für den Heizstab (heizstab/controller.py).

Zwei Ebenen: die Überschuss-Regel als reine Funktion (``naechster_sollwert``)
und der Controller mit Temperatur-Hysteresen, Vorrang und Schreibpfad. Der
Treiber ist ein Mock — geschrieben wird ausschließlich über
``async_set_power``; die Werte des Geräts kommen aus dessen Cache.
"""

from types import SimpleNamespace
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
    CONF_HEIZSTAB_MAXTEMP_C,
    HEIZSTAB_KOMFORT_EXPORT_ZIEL_KW,
    HEIZSTAB_KONFLIKT_MINUTEN,
    HEIZSTAB_PLAN_EXPORT_ZIEL_KW,
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
        CONF_HEIZSTAB_MAXTEMP_C: 80.0,
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


def test_maximaltemperatur_sperrt_mit_hysterese():
    t = _treiber(power_w=0, temp=80.0)
    hz = HeizstabController(MagicMock(), _cfg(), t)
    hz.pruefe_temperaturen()
    assert hz.max_erreicht is True
    soll, grund = hz.regeln(entladung=False, export_kw=4.0, grenze_kw=4.0, vorrang_frei=True)
    assert soll == 0.0 and "Maximaltemperatur" in grund

    # 78 °C: noch gesperrt (Hysterese 3 K)
    t.last_temperature = 78.0
    hz.pruefe_temperaturen()
    assert hz.max_erreicht is True
    # 76,5 °C: wieder frei
    t.last_temperature = 76.5
    hz.pruefe_temperaturen()
    assert hz.max_erreicht is False
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


def test_gesaettigt_am_maximum_und_bei_maximaltemperatur():
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


# ---------------------------------------------------------------------------
# Wärme zählt unabhängig von der Puffertemperatur
# ---------------------------------------------------------------------------


def test_status_kennt_keine_zweite_heizquelle_mehr():
    """Die Unterscheidung Ersatz-/Zusatzwärme ist entfallen — jede
    Kilowattstunde in den Puffer zählt zum Wärmewert. Der Status darf die
    alten Schlüssel nicht wieder mitschleppen."""
    hz = HeizstabController(MagicMock(), _cfg(), _treiber(power_w=3000, temp=78.0))
    status = hz.status()
    assert "alt_temp_c" not in status
    assert "zusatzwaerme" not in status
    assert status["waermewert"] == hz.waermewert
    assert not hasattr(hz, "zusatzwaerme")




# ---------------------------------------------------------------------------
# Fahrplan-Betrieb: die geplante Wärme ist die Vorgabe
# ---------------------------------------------------------------------------


def _geplant(**cfg):
    """Controller ohne Temperatur-Sonderfall — der Puffer liegt im Mittelfeld."""
    return HeizstabController(MagicMock(), _cfg(**cfg), _treiber(temp=60.0))


def test_plan_heizt_auch_weit_unter_der_einspeisegrenze():
    """Einspeisung 2 kW bei Grenze 4 kW: ohne Plan bliebe der Heizstab aus.

    Der Slot sieht 3 kW Wärme vor — das LP hat die Kilowattstunde dem Puffer
    zugeschlagen, weil sie dort mehr bringt. Also wird getastet.
    """
    hz = _geplant()
    soll, grund = hz.regeln(
        entladung=False, export_kw=2.0, grenze_kw=4.0, vorrang_frei=False, plan_kw=3.0
    )
    assert soll == pytest.approx(HEIZSTAB_STEP_KW)
    assert "Fahrplan" in grund


def test_plan_ist_die_obergrenze():
    """Über die geplante Leistung hinaus wird nicht getastet — sonst nähme der
    Heizstab auch die Einspeisung, die der Plan bewusst ins Netz schickt."""
    hz = _geplant()
    hz.sollwert_kw = 2.0
    soll, grund = hz.regeln(
        entladung=False, export_kw=3.0, grenze_kw=4.0, vorrang_frei=False, plan_kw=2.0
    )
    assert soll == pytest.approx(2.0)
    assert "am Maximum" in grund


def test_plan_ueber_der_geraeteleistung_wird_gedeckelt():
    hz = _geplant()
    hz.sollwert_kw = 5.8
    soll, _ = hz.regeln(
        entladung=False, export_kw=3.0, grenze_kw=4.0, vorrang_frei=False, plan_kw=9.0
    )
    assert soll == pytest.approx(6.0)


def test_plan_weicht_bei_netzbezug_zurueck():
    """Die Prognose sagt, wie viel erlaubt ist — die Messung, wie viel da ist.
    Bei Bezug fällt der Sollwert um die volle Lücke, statt Netzstrom zu ziehen."""
    hz = _geplant()
    hz.sollwert_kw = 3.0
    soll, _ = hz.regeln(
        entladung=False, export_kw=-0.5, grenze_kw=4.0, vorrang_frei=False, plan_kw=3.0
    )
    assert soll == pytest.approx(3.0 - (HEIZSTAB_PLAN_EXPORT_ZIEL_KW + 0.5))
    hz.sollwert_kw = 0.4
    soll, grund = hz.regeln(
        entladung=False, export_kw=-0.5, grenze_kw=4.0, vorrang_frei=False, plan_kw=3.0
    )
    assert soll == 0.0
    assert "Netzbezug" in grund


def test_plan_ignoriert_den_anteil_an_der_ueberschussteilung():
    """Die Aufteilung zwischen Batterie und Heizstab steckt schon im Plan —
    ein zweites Mal gedeckelt würde sie doppelt wirken."""
    hz = _geplant()
    hz.sollwert_kw = 1.0
    soll, _ = hz.regeln(
        entladung=False, export_kw=3.0, grenze_kw=4.0, vorrang_frei=False,
        deckel_kw=0.5, plan_kw=3.0,
    )
    assert soll == pytest.approx(1.0 + HEIZSTAB_STEP_KW)


def test_ohne_plan_gilt_weiter_die_einspeisegrenze():
    """Ungeplanter Überschuss (PV über Prognose) fängt die alte Regel auf."""
    hz = _geplant()
    hz.sollwert_kw = 1.0
    soll, grund = hz.regeln(
        entladung=False, export_kw=2.0, grenze_kw=4.0, vorrang_frei=True, plan_kw=0.0
    )
    assert soll == 0.0
    assert "Einspeisung unter der Grenze" in grund


def test_maximaltemperatur_schlaegt_den_plan():
    hz = HeizstabController(MagicMock(), _cfg(), _treiber(temp=81.0))
    hz.pruefe_temperaturen()
    soll, grund = hz.regeln(
        entladung=False, export_kw=3.0, grenze_kw=4.0, vorrang_frei=True, plan_kw=3.0
    )
    assert soll == 0.0
    assert "Maximaltemperatur" in grund


def test_entladung_schlaegt_den_plan():
    hz = _geplant()
    hz.sollwert_kw = 2.0
    soll, grund = hz.regeln(
        entladung=True, export_kw=3.0, grenze_kw=4.0, vorrang_frei=True, plan_kw=3.0
    )
    assert soll == 0.0
    assert "Entladung" in grund


# ---------------------------------------------------------------------------
# Host-Normalisierung: eine URL im Feld darf die Verbindung nicht verhindern
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("roh,erwartet", [
    ("192.168.100.58", "192.168.100.58"),
    ("  192.168.100.58  ", "192.168.100.58"),
    ("http://192.168.100.58/", "192.168.100.58"),
    ("https://ohmpilot.local", "ohmpilot.local"),
    ("http://192.168.100.58:80/status", "192.168.100.58"),
    ("192.168.100.58:502", "192.168.100.58"),
    ("ohmpilot.fritz.box/", "ohmpilot.fritz.box"),
    ("http://user:pw@192.168.100.58/", "192.168.100.58"),
    ("[fd00::1]", "fd00::1"),
    ("", ""),
    (None, ""),
])
def test_host_wird_auf_die_reine_adresse_gekuerzt(roh, erwartet):
    from custom_components.eeg_energy_optimizer.heizstab.controller import normalisiere_host
    assert normalisiere_host(roh) == erwartet


def test_create_heizstab_nimmt_auch_eine_url():
    hz = create_heizstab(MagicMock(), _cfg(**{CONF_HEIZSTAB_HOST: "http://192.168.100.58/"}))
    assert hz is not None
    assert hz.status()["host"] == "192.168.100.58"


# ---------------------------------------------------------------------------
# Fremdsteuerung: Das Gerät folgt dem Sollwert nicht
#
# Der Ohmpilot kennt keine Zugriffsrechte — wer zuletzt auf das Register
# schreibt, gewinnt. An der Anlage Grünbach lief die Vorgänger-Integration
# weiter und überschrieb unsere 0 im Sekundentakt; sichtbar war das nur als
# Sägezahn in der Ist-Leistung, während die Karte „Heizstab aus" behauptete.
# ---------------------------------------------------------------------------


async def test_fremdsteuerung_erst_nach_der_karenzzeit(monkeypatch):
    """Sollwert 0, Gerät zieht 2,4 kW — gemeldet wird erst nach Minuten."""
    from custom_components.eeg_energy_optimizer.heizstab import controller as mod

    jetzt = [1000.0]
    monkeypatch.setattr(mod.time, "time", lambda: jetzt[0])
    t = _treiber(power_w=2400, temp=60.0)
    hz = HeizstabController(MagicMock(), _cfg(), t)
    hz.sollwert_kw = 0.0

    await hz.async_lesen()
    assert hz.fremdsteuerung is False       # Karenz laeuft erst an

    jetzt[0] += HEIZSTAB_KONFLIKT_MINUTEN * 60 - 1
    await hz.async_lesen()
    assert hz.fremdsteuerung is False

    jetzt[0] += 2
    await hz.async_lesen()
    assert hz.fremdsteuerung is True
    assert hz.status()["fremdsteuerung"] is True


async def test_kein_verdacht_wenn_das_geraet_folgt(monkeypatch):
    from custom_components.eeg_energy_optimizer.heizstab import controller as mod

    jetzt = [1000.0]
    monkeypatch.setattr(mod.time, "time", lambda: jetzt[0])
    t = _treiber(power_w=2400, temp=60.0)
    hz = HeizstabController(MagicMock(), _cfg(), t)
    hz.sollwert_kw = 2.5                     # Ist knapp darunter — passt
    jetzt[0] += HEIZSTAB_KONFLIKT_MINUTEN * 60 + 10
    await hz.async_lesen()
    assert hz.fremdsteuerung is False


async def test_regelrauschen_bleibt_in_der_toleranz(monkeypatch):
    """Ein paar Watt über dem Sollwert sind Regelabweichung, kein Konflikt."""
    from custom_components.eeg_energy_optimizer.heizstab import controller as mod

    jetzt = [1000.0]
    monkeypatch.setattr(mod.time, "time", lambda: jetzt[0])
    t = _treiber(power_w=2600, temp=60.0)    # 2,6 kW bei Sollwert 2,5
    hz = HeizstabController(MagicMock(), _cfg(), t)
    hz.sollwert_kw = 2.5
    await hz.async_lesen()
    jetzt[0] += HEIZSTAB_KONFLIKT_MINUTEN * 60 + 10
    await hz.async_lesen()
    assert hz.fremdsteuerung is False


async def test_neuer_sollwert_setzt_die_beobachtung_zurueck(monkeypatch):
    """Nach dem Herunterregeln darf das Geraet erst einmal nachziehen."""
    from custom_components.eeg_energy_optimizer.heizstab import controller as mod

    jetzt = [1000.0]
    monkeypatch.setattr(mod.time, "time", lambda: jetzt[0])
    t = _treiber(power_w=5000, temp=60.0)
    hz = HeizstabController(MagicMock(), _cfg(), t)
    hz.sollwert_kw = 0.0
    await hz.async_lesen()
    jetzt[0] += HEIZSTAB_KONFLIKT_MINUTEN * 60 + 10
    await hz.async_lesen()
    assert hz.fremdsteuerung is True

    await hz.async_set_sollwert(5.0, "Ueberschuss")   # Sollwert hoch: passt wieder
    assert hz.fremdsteuerung is False


async def test_ohne_messwert_kein_verdacht(monkeypatch):
    """Antwortet der Ohmpilot nicht, meldet `verfuegbar` das — nicht diese Regel."""
    from custom_components.eeg_energy_optimizer.heizstab import controller as mod

    jetzt = [1000.0]
    monkeypatch.setattr(mod.time, "time", lambda: jetzt[0])
    t = _treiber(power_w=None, temp=None, connected=False)
    hz = HeizstabController(MagicMock(), _cfg(), t)
    hz.sollwert_kw = 0.0
    jetzt[0] += HEIZSTAB_KONFLIKT_MINUTEN * 60 + 10
    await hz.async_lesen()
    assert hz.fremdsteuerung is False


# ---------------------------------------------------------------------------
# Pufferbudget und Sperre durch eine zweite Wärmequelle
# ---------------------------------------------------------------------------


def _hass_mit(zustaende: dict):
    hass = MagicMock()
    hass.states.get.side_effect = lambda e: (
        SimpleNamespace(state=zustaende[e]) if e in zustaende else None
    )
    return hass


def _budget_cfg(**over):
    cfg = _cfg(
        heizstab_puffer_liter=600,
        heizstab_maxtemp_c=80.0,
    )
    cfg.update(over)
    return cfg


def test_budget_aus_volumen_und_temperatur():
    """1,163 Wh je Liter und Kelvin — 600 L von 45 auf 80 °C sind 24,4 kWh."""
    controller = HeizstabController(MagicMock(), _budget_cfg(), _treiber(temp=45.0))
    controller._treiber.last_temperature = 45.0
    assert controller.puffer_budget_kwh == pytest.approx(24.423, abs=0.01)


def test_budget_schrumpft_mit_steigender_temperatur():
    """Der Sommerfall: Je wärmer der Puffer, desto weniger ist der Wärmeweg
    wert — bis die Abendentladung wieder die bessere Verwendung ist."""
    werte = {}
    for temp in (45.0, 70.0, 78.0, 80.0, 85.0):
        controller = HeizstabController(MagicMock(), _budget_cfg(), _treiber(temp=temp))
        werte[temp] = controller.puffer_budget_kwh
    assert werte[45.0] > werte[70.0] > werte[78.0] > 0
    assert werte[80.0] == 0.0, "Maximaltemperatur erreicht → nichts mehr aufzunehmen"
    assert werte[85.0] == 0.0, "Über der Maximaltemperatur kein negatives Budget"


@pytest.mark.parametrize("fehlend", ["volumen", "temperatur"])
def test_ohne_angabe_kein_budget(fehlend):
    """Fehlt Volumen oder Messwert, wird nicht geraten: 0 heißt „nicht
    einplanen", und der Fahrplan bleibt beim bisherigen Verhalten."""
    cfg = _budget_cfg()
    temp = 45.0
    if fehlend == "volumen":
        cfg["heizstab_puffer_liter"] = 0
    else:
        temp = None
    controller = HeizstabController(MagicMock(), cfg, _treiber(temp=temp))
    assert controller.puffer_budget_kwh == 0.0


def test_zweite_waermequelle_sperrt_den_heizstab():
    """Läuft der Holzvergaser, hat der Heizstab am Puffer nichts zu suchen —
    und zwar vor jeder anderen Regel, auch vor der Mindesttemperatur."""
    cfg = _budget_cfg(
        heizstab_sperr_entity="switch.holzvergaser",
        heizstab_mintemp_c=50.0,   # Komfortheizen wäre sonst fällig
        heizstab_netzbezug=True,
    )
    hass = _hass_mit({"switch.holzvergaser": "on"})
    controller = HeizstabController(hass, cfg, _treiber(temp=40.0))

    assert controller.gesperrt is True
    soll, grund = controller.regeln(
        entladung=False, export_kw=5.0, grenze_kw=4.0, vorrang_frei=True
    )
    assert soll == 0.0
    assert "andere Wärmequelle" in grund
    # Und der Fahrplan rechnet nicht mit einem Weg, den es gerade nicht gibt.
    assert controller.puffer_budget_kwh == 0.0


def test_ausgeschaltete_waermequelle_gibt_frei():
    cfg = _budget_cfg(heizstab_sperr_entity="switch.holzvergaser")
    controller = HeizstabController(
        _hass_mit({"switch.holzvergaser": "off"}), cfg, _treiber(temp=45.0)
    )
    assert controller.gesperrt is False
    assert controller.puffer_budget_kwh > 0


def test_unerreichbare_sperrentitaet_gilt_als_frei():
    """Ein ausgefallener Sensor darf den Heizstab nicht unbemerkt für Wochen
    stilllegen. Läuft er versehentlich mit, bremst ihn die Maximaltemperatur."""
    cfg = _budget_cfg(heizstab_sperr_entity="switch.gibt_es_nicht")
    controller = HeizstabController(_hass_mit({}), cfg, _treiber(temp=45.0))
    assert controller.gesperrt is False

    fuer_unavailable = HeizstabController(
        _hass_mit({"switch.holzvergaser": "unavailable"}),
        _budget_cfg(heizstab_sperr_entity="switch.holzvergaser"),
        _treiber(temp=45.0),
    )
    assert fuer_unavailable.gesperrt is False


def test_ohne_sperrentitaet_keine_sperre():
    controller = HeizstabController(MagicMock(), _budget_cfg(), _treiber(temp=45.0))
    assert controller.gesperrt is False
