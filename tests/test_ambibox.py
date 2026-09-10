"""Tests für die Ambibox-Anbindung (ambibox/).

Zwei Ebenen: die Dekodierung der Rohregister (``modbus.py``) und die Deutung
zum Fahrzeugzustand samt Controller-Takt (``controller.py``). Der Treiber ist
in den Controller-Tests ein Mock — echte Modbus-Verbindungen gibt es hier
nicht.
"""

import struct
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.eeg_energy_optimizer.ambibox import modbus as mb
from custom_components.eeg_energy_optimizer.ambibox.controller import (
    AmbiboxController,
    ambibox_connector,
    ambibox_enabled,
    ambibox_port,
    ambibox_unit_id,
    create_ambibox,
    normalisiere_host,
)
from custom_components.eeg_energy_optimizer.const import (
    CONF_AMBIBOX_CONNECTOR,
    CONF_AMBIBOX_HOST,
    CONF_AMBIBOX_PORT,
    CONF_AMBIBOX_UNIT_ID,
    CONF_WALLBOX_TYPE,
    WALLBOX_TYPE_AMBIBOX,
)


# ---------------------------------------------------------------------------
# Hilfsmittel: einen Registerblock bauen
# ---------------------------------------------------------------------------


def _worte(fmt: str, wert) -> list[int]:
    """Einen 32-Bit-Wert in seine zwei Register zerlegen (high word zuerst)."""
    roh = struct.pack(">" + fmt, wert)
    return list(struct.unpack(">HH", roh))


def _block(werte: dict) -> list[int]:
    """Einen vollständigen Input-Block mit den angegebenen Werten.

    Schlüssel sind Offsets, Werte Tupel ``(format, zahl)`` — etwa
    ``{54: ("f", 62.5)}`` für einen Ladestand von 62,5 %.
    """
    regs = [0] * mb.INPUT_LENGTH
    for offset, (fmt, zahl) in werte.items():
        regs[offset], regs[offset + 1] = _worte(fmt, zahl)
    return regs


def _standardblock(over: dict | None = None) -> list[int]:
    """Ein angestecktes, ladendes Fahrzeug — Ausgangslage der meisten Tests."""
    werte = {
        mb.OFF_POWER_AC: ("i", 7300),
        mb.OFF_CAPACITY: ("f", 64000.0),
        mb.OFF_SOC: ("f", 62.5),
        mb.OFF_SOH: ("f", 98.5),
        mb.OFF_MAX_CHARGE_POWER: ("I", 11000),
        mb.OFF_MAX_DISCHARGE_POWER: ("I", 10000),
        mb.OFF_BATTERY_STATE: ("I", 2),  # CHARGE
        mb.OFF_CONTROL_MODE: ("I", 1),  # CONTROLLABLE
        mb.OFF_CHARGE_PROTOCOL: ("I", 5),  # ISO 15118-20
        mb.OFF_SESSION_STATE: ("I", 5),  # CHARGE_LOOP
        mb.OFF_EV_CONNECTED: ("I", 1),
        mb.OFF_SECONDS_TO_DEPARTURE: ("I", 7305),
        mb.OFF_DEPARTURE_SOC: ("f", 80.0),
        mb.OFF_SESSION_IMPORT: ("f", 4300.0),
    }
    werte.update(over or {})
    return _block(werte)


def _deute(regs):
    return AmbiboxController(None, {}, None)._deuten(regs)


# ---------------------------------------------------------------------------
# Adressraster und Dekodierung
# ---------------------------------------------------------------------------


def test_adressraster_je_ladepunkt():
    """Input-Block alle 200, Holding-Block alle 100 Register."""
    assert mb.input_base(1) == 4000
    assert mb.input_base(2) == 4200
    assert mb.input_base(10) == 5800
    assert mb.holding_base(1) == 3000
    assert mb.holding_base(2) == 3100
    assert mb.holding_base(10) == 3900


def test_block_passt_in_einen_lesevorgang():
    """104 Register bleiben unter der Modbus-Grenze von 125."""
    assert mb.INPUT_LENGTH == 104
    assert mb.INPUT_LENGTH <= 125
    # Der letzte dokumentierte Wert muss noch hineinpassen.
    assert mb.OFF_REPLUG_REQUIRED + 2 <= mb.INPUT_LENGTH


def test_dekodierung_der_datentypen():
    regs = _block({
        0: ("f", 230.5),
        2: ("i", -7281),
        4: ("I", 4294967295),
        6: ("I", 1),
    })
    assert mb.read_f32(regs, 0) == pytest.approx(230.5, abs=0.01)
    assert mb.read_i32(regs, 2) == -7281
    assert mb.read_u32(regs, 4) == 4294967295
    assert mb.read_bool(regs, 6) is True
    assert mb.read_bool(regs, 8) is False


def test_dekodierung_ausserhalb_des_blocks_ist_none():
    """Ein zu kurzer Block liefert None statt einer IndexError-Kaskade."""
    kurz = [0, 0]
    assert mb.read_f32(kurz, 4) is None
    assert mb.read_i32(kurz, 4) is None
    assert mb.read_u32(kurz, 4) is None
    assert mb.read_bool(kurz, 4) is None


def test_nan_gilt_nicht_als_messwert():
    """NaN heißt „unbekannt" — sonst stünde „nan %" in der Anzeige."""
    regs = _block({0: ("f", float("nan"))})
    assert mb.read_f32(regs, 0) is None


# ---------------------------------------------------------------------------
# Deutung zum Fahrzeugzustand
# ---------------------------------------------------------------------------


def test_angestecktes_fahrzeug_wird_gedeutet():
    z = _deute(_standardblock())
    assert z.verbunden is True
    assert z.soc_pct == pytest.approx(62.5)
    assert z.kapazitaet_kwh == pytest.approx(64.0)
    # Energie im Akku = Kapazität × Ladestand
    assert z.energie_kwh == pytest.approx(40.0)
    assert z.session_text == "Lädt"
    assert z.protokoll_text == "ISO 15118-20"
    assert z.bidirektional_faehig is True
    assert z.steuerbar is True
    assert z.control_mode_text == "Voll steuerbar"
    assert z.max_charge_kw == pytest.approx(11.0)
    assert z.max_discharge_kw == pytest.approx(10.0)
    assert z.abfahrt_in_s == 7305
    assert z.abfahrt_soc_pct == pytest.approx(80.0)
    assert z.session_geladen_kwh == pytest.approx(4.3)


def test_richtung_kommt_aus_dem_batteriezustand_nicht_aus_dem_vorzeichen():
    """Die Vorzeichenkonvention der Ambibox ist ungeklärt.

    Deshalb wird die Leistung als Betrag geführt und die Richtung aus dem
    dokumentierten Batteriezustand abgeleitet — dieselbe negative Leistung
    heißt beim Laden „laden" und beim Entladen „entladen".
    """
    laden = _deute(_standardblock({
        mb.OFF_POWER_AC: ("i", -7300),
        mb.OFF_BATTERY_STATE: ("I", 2),
    }))
    assert laden.richtung == "laden"
    assert laden.leistung_kw == pytest.approx(7.3)

    entladen = _deute(_standardblock({
        mb.OFF_POWER_AC: ("i", -7300),
        mb.OFF_BATTERY_STATE: ("I", 3),
    }))
    assert entladen.richtung == "entladen"
    assert entladen.leistung_kw == pytest.approx(7.3)


def test_restleistung_im_ruhezustand_wird_zu_null():
    """Ohne Ladevorgang ist eine Kleinstleistung Rauschen der Elektronik."""
    z = _deute(_standardblock({
        mb.OFF_POWER_AC: ("i", 12),
        mb.OFF_BATTERY_STATE: ("I", 1),  # IDLE
    }))
    assert z.richtung == "aus"
    assert z.leistung_kw == 0.0


def test_kein_fahrzeug_angesteckt():
    z = _deute(_block({
        mb.OFF_EV_CONNECTED: ("I", 0),
        mb.OFF_SESSION_STATE: ("I", 8),  # STOPPED
    }))
    assert z.verbunden is False
    # Der Sitzungszustand der Box interessiert nicht, wenn nichts dranhängt.
    assert z.session_text == "Kein Fahrzeug angesteckt"


def test_replug_wird_gemeldet():
    z = _deute(_standardblock({mb.OFF_REPLUG_REQUIRED: ("I", 1)}))
    assert z.replug_required is True


def test_fehler_flagset_wird_aufgeloest():
    """Eine Zahl, mehrere Fehler — Beispiel aus dem Herstellerdokument."""
    z = _deute(_standardblock({
        # Bit 3 (Steckertemperatur) + Bit 5 (Not-Aus) = 40
        mb.OFF_EVCHARGER_ERROR: ("I", 40),
    }))
    assert "Steckertemperatur" in z.fehler
    assert "Not-Aus" in z.fehler
    assert len(z.fehler) == 2


def test_fehler_aus_beiden_flagsets():
    z = _deute(_standardblock({
        mb.OFF_EVCHARGER_ERROR: ("I", 1 << 0),  # EV_COMMUNICATION
        mb.OFF_BATTERY_ERROR: ("I", 1 << 5),  # TEMPERATURE_OVER
    }))
    assert "Kommunikation mit dem Fahrzeug" in z.fehler
    assert "Temperatur zu hoch" in z.fehler


def test_unbekanntes_fehlerbit_wird_nicht_verschluckt():
    """Ein Bit ohne Namen darf nicht als „kein Fehler" durchgehen."""
    z = _deute(_standardblock({mb.OFF_EVCHARGER_ERROR: ("I", 1 << 20)}))
    assert z.fehler
    assert "Unbekannter Fehlercode" in z.fehler[0]


def test_nicht_steuerbares_fahrzeug():
    z = _deute(_standardblock({
        mb.OFF_CONTROL_MODE: ("I", 0),
        mb.OFF_CHARGE_PROTOCOL: ("I", 2),  # DIN 70121
    }))
    assert z.steuerbar is False
    assert z.control_mode_text == "Nicht steuerbar"
    assert z.bidirektional_faehig is False
    assert z.protokoll_text == "DIN 70121"


def test_unbekannter_sitzungszustand_bleibt_lesbar():
    z = _deute(_standardblock({mb.OFF_SESSION_STATE: ("I", 42)}))
    assert z.session_text == "Zustand 42"


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------


def test_wallbox_typ_schaltet_die_anbindung():
    assert ambibox_enabled({CONF_WALLBOX_TYPE: WALLBOX_TYPE_AMBIBOX}) is True
    assert ambibox_enabled({CONF_WALLBOX_TYPE: "Ambibox"}) is True  # Groß/klein egal
    assert ambibox_enabled({CONF_WALLBOX_TYPE: ""}) is False
    assert ambibox_enabled({}) is False


@pytest.mark.parametrize("eingabe,erwartet", [
    ("192.168.1.70", "192.168.1.70"),
    ("http://192.168.1.70/", "192.168.1.70"),
    ("https://ambibox.local/status", "ambibox.local"),
    ("192.168.1.70:502", "192.168.1.70"),
    ("  192.168.1.70  ", "192.168.1.70"),
    ("", ""),
    (None, ""),
])
def test_adresse_wird_aus_einer_url_geschaelt(eingabe, erwartet):
    """Die Ambibox hat ein Webinterface — die URL liegt nahe, pymodbus kann
    damit nichts anfangen."""
    assert normalisiere_host(eingabe) == erwartet


def test_ungueltige_werte_fallen_auf_die_vorgabe_zurueck():
    assert ambibox_port({CONF_AMBIBOX_PORT: "quatsch"}) == 502
    assert ambibox_unit_id({CONF_AMBIBOX_UNIT_ID: None}) == 1
    assert ambibox_connector({CONF_AMBIBOX_CONNECTOR: 99}) == 1
    assert ambibox_connector({CONF_AMBIBOX_CONNECTOR: 3}) == 3


def test_create_ambibox_ohne_wallbox_ist_none():
    assert create_ambibox(MagicMock(), {}) is None


def test_create_ambibox_ohne_adresse_liefert_controller_ohne_treiber():
    """Kein Host heißt: Sensoren existieren und stehen auf „nicht verfügbar" —
    die Integration darf daran nicht scheitern."""
    controller = create_ambibox(MagicMock(), {CONF_WALLBOX_TYPE: WALLBOX_TYPE_AMBIBOX})
    assert controller is not None
    assert controller.verfuegbar is False


def test_create_ambibox_baut_den_treiber_mit_dem_ladepunkt():
    controller = create_ambibox(MagicMock(), {
        CONF_WALLBOX_TYPE: WALLBOX_TYPE_AMBIBOX,
        CONF_AMBIBOX_HOST: "http://192.168.1.70/",
        CONF_AMBIBOX_CONNECTOR: 2,
    })
    treiber = controller._treiber
    assert treiber is not None
    assert treiber.host == "192.168.1.70"
    assert treiber.input_base == 4200
    assert treiber.holding_base == 3100


# ---------------------------------------------------------------------------
# Controller-Takt
# ---------------------------------------------------------------------------


def _treiber(regs=None):
    t = MagicMock()
    t.async_read_block = AsyncMock(return_value=regs)
    t.async_close = AsyncMock()
    t.read_failures = 0 if regs is not None else 1
    t.last_error = None
    return t


async def test_lesen_fuellt_den_zustand_und_meldet():
    treiber = _treiber(_standardblock())
    controller = AmbiboxController(MagicMock(), {}, treiber)
    gemeldet = []
    controller.add_listener(lambda: gemeldet.append(1))

    await controller.async_lesen()

    assert controller.verfuegbar is True
    assert controller.zustand.soc_pct == pytest.approx(62.5)
    assert gemeldet == [1]


async def test_abriss_setzt_verfuegbarkeit_zurueck():
    """Fährt das Auto weg oder fällt die Box aus, darf der letzte Ladestand
    nicht als aktueller Wert stehen bleiben."""
    treiber = _treiber(_standardblock())
    controller = AmbiboxController(MagicMock(), {}, treiber)
    await controller.async_lesen()
    assert controller.verfuegbar is True

    treiber.async_read_block = AsyncMock(return_value=None)
    treiber.read_failures = 1
    gemeldet = []
    controller.add_listener(lambda: gemeldet.append(1))
    await controller.async_lesen()

    assert controller.verfuegbar is False
    assert gemeldet == [1]


async def test_ohne_treiber_passiert_nichts():
    controller = AmbiboxController(MagicMock(), {}, None)
    await controller.async_lesen()  # darf nicht werfen
    assert controller.verfuegbar is False


async def test_fehler_eines_zuhoerers_kippt_den_takt_nicht():
    treiber = _treiber(_standardblock())
    controller = AmbiboxController(MagicMock(), {}, treiber)
    controller.add_listener(MagicMock(side_effect=RuntimeError("kaputt")))
    zweiter = []
    controller.add_listener(lambda: zweiter.append(1))

    await controller.async_lesen()

    assert zweiter == [1]
    assert controller.zustand.verbunden is True


def test_status_enthaelt_anbindung_und_fahrzeug():
    config = {
        CONF_WALLBOX_TYPE: WALLBOX_TYPE_AMBIBOX,
        CONF_AMBIBOX_HOST: "192.168.1.70",
        CONF_AMBIBOX_CONNECTOR: 2,
    }
    controller = AmbiboxController(MagicMock(), config, _treiber(_standardblock()))
    status = controller.status()
    assert status["enabled"] is True
    assert status["host"] == "192.168.1.70"
    assert status["connector"] == 2
    assert status["port"] == 502
    assert "soc_pct" in status and "richtung" in status


async def test_shutdown_schliesst_die_verbindung():
    treiber = _treiber(_standardblock())
    controller = AmbiboxController(MagicMock(), {}, treiber)
    await controller.async_shutdown()
    treiber.async_close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Sensoren
# ---------------------------------------------------------------------------


def _sensor_controller(regs=None, verfuegbar=True):
    controller = AmbiboxController(MagicMock(), {}, _treiber(regs))
    if regs is not None:
        controller._zustand = controller._deuten(regs)
        controller._gelesen = verfuegbar
    return controller


def _entry():
    entry = MagicMock()
    entry.entry_id = "test-entry"
    return entry


def test_ladeleistung_traegt_das_vorzeichen_der_hausbatterie():
    """Laden positiv, Rückspeisen negativ — dieselbe Leserichtung wie bei der
    Hausbatterie, obwohl die Ambibox nur einen Betrag liefert."""
    from custom_components.eeg_energy_optimizer.sensor import AutoLeistungSensor

    laden = AutoLeistungSensor(
        MagicMock(), _entry(),
        _sensor_controller(_standardblock({mb.OFF_BATTERY_STATE: ("I", 2)})),
    )
    assert laden.native_value == pytest.approx(7.3)

    entladen = AutoLeistungSensor(
        MagicMock(), _entry(),
        _sensor_controller(_standardblock({mb.OFF_BATTERY_STATE: ("I", 3)})),
    )
    assert entladen.native_value == pytest.approx(-7.3)


def test_status_sensor_meldet_unerreichbare_wallbox():
    """Der Statussensor bleibt verfügbar — „nicht erreichbar" ist selbst eine
    Aussage, die die Karte zeigen können muss."""
    from custom_components.eeg_energy_optimizer.sensor import AutoStatusSensor

    sensor = AutoStatusSensor(MagicMock(), _entry(), _sensor_controller(None))
    assert sensor.available is True
    assert sensor.native_value == "Ambibox nicht erreichbar"


def test_ladestand_ohne_fahrzeug_ist_nicht_verfuegbar():
    """Ohne angestecktes Auto meldet die Box den Wert des letzten Fahrzeugs —
    das ist ab dem Abstecken eine Behauptung, kein Messwert."""
    from custom_components.eeg_energy_optimizer.sensor import AutoLadestandSensor

    ohne = AutoLadestandSensor(
        MagicMock(), _entry(),
        _sensor_controller(_block({mb.OFF_EV_CONNECTED: ("I", 0), mb.OFF_SOC: ("f", 62.5)})),
    )
    assert ohne.available is False

    mit = AutoLadestandSensor(MagicMock(), _entry(), _sensor_controller(_standardblock()))
    assert mit.available is True
    assert mit.native_value == pytest.approx(62.5)
