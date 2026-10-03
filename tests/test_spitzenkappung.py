"""Spitzenkappung: Verbraucher abschalten, bevor die Bezugsspitze steigt."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.eeg_energy_optimizer.spitzenkappung import (
    BERUHIGUNG_S,
    MIN_AUS_S,
    Spitzenkappung,
    Verbraucher,
    VerbraucherZustand,
    entscheide,
    grenze_aus_config,
    schwelle_kw,
    verbraucher_aus_config,
)
from custom_components.eeg_energy_optimizer.websocket_api import _pruefe_verbraucher

UTC = timezone.utc
T0 = datetime(2026, 10, 2, 16, 0, tzinfo=UTC)

AUTO = Verbraucher("Tesla", "sensor.tesla_ladeleistung", "switch.tesla_laden")
WP = Verbraucher("Wärmepumpe", "sensor.wp_leistung", "switch.wp")


def _laufend(hochrechnung, bezug, rest_s=600.0):
    return {"hochrechnung_kw": hochrechnung, "bezug_kw": bezug, "rest_s": rest_s}


def _an(kw):
    return VerbraucherZustand(kw, True)


# ----------------------------------------------------------------------
# Konfiguration
# ----------------------------------------------------------------------
def test_schwelle_ist_grenzwert_oder_schon_erreichte_spitze():
    assert schwelle_kw(5.0, None) == 5.0
    assert schwelle_kw(5.0, 3.2) == 5.0
    # Schon einmal drüber gewesen: bis dorthin kostet es nichts mehr.
    assert schwelle_kw(5.0, 7.4) == 7.4


def test_grenze_wird_auf_2_bis_20_kw_geklemmt():
    assert grenze_aus_config({"spitzenkappung_grenze_kw": 1}) == 2.0
    assert grenze_aus_config({"spitzenkappung_grenze_kw": 50}) == 20.0
    assert grenze_aus_config({"spitzenkappung_grenze_kw": 6.5}) == 6.5
    assert grenze_aus_config({}) == 5.0


def test_verbraucherliste_nur_gueltige_schalter_ohne_doppelte():
    liste = verbraucher_aus_config({"spitzenkappung_verbraucher": [
        {"name": "Tesla", "leistung_entity": "sensor.a", "schalter_entity": "switch.a"},
        {"name": "doppelt", "leistung_entity": "sensor.b", "schalter_entity": "switch.a"},
        {"name": "Licht", "leistung_entity": "sensor.c", "schalter_entity": "light.c"},
        {"name": "", "leistung_entity": "sensor.d", "schalter_entity": "input_boolean.d"},
        {"name": "halb", "leistung_entity": "", "schalter_entity": "switch.e"},
    ]})
    assert [(v.name, v.schalter_entity) for v in liste] == [
        ("Tesla", "switch.a"), ("Verbraucher 4", "input_boolean.d"),
    ]


def test_save_config_prueft_die_verbraucherliste():
    gut = [{"name": "Tesla", "leistung_entity": "sensor.a", "schalter_entity": "switch.a"}]
    assert _pruefe_verbraucher(gut) is None
    assert _pruefe_verbraucher([]) is None
    assert _pruefe_verbraucher("x") is not None
    assert _pruefe_verbraucher([{"schalter_entity": "lock.haustuer"}]) is not None
    assert _pruefe_verbraucher([{"leistung_entity": "switch.a"}]) is not None
    assert _pruefe_verbraucher(gut * 11) is not None


# ----------------------------------------------------------------------
# Entscheidung
# ----------------------------------------------------------------------
def _entscheide(laufend, zustaende, abgeschaltet=None, letztes=None, schwelle=5.0,
                verbraucher=(AUTO, WP), jetzt=T0):
    return entscheide(
        jetzt=jetzt, laufend=laufend, schwelle=schwelle,
        verbraucher=list(verbraucher), zustaende=zustaende,
        abgeschaltet=abgeschaltet or {}, letztes_abschalten=letztes,
    )


def test_unter_der_schwelle_wird_nichts_geschaltet():
    assert _entscheide(_laufend(4.8, 4.8), {AUTO.schalter_entity: _an(3.0)}) == []


def test_ohne_frische_messung_wird_nichts_geschaltet():
    zust = {AUTO.schalter_entity: _an(11.0)}
    assert _entscheide(None, zust) == []
    assert _entscheide(_laufend(None, None), zust) == []
    # Auch kein Wiedereinschalten ins Blaue.
    ab = {AUTO.schalter_entity: {"seit": T0 - timedelta(hours=1), "leistung_kw": 11.0}}
    assert _entscheide(_laufend(None, None), {AUTO.schalter_entity: VerbraucherZustand(0, False)}, ab) == []


def test_ueber_der_schwelle_wird_der_erste_aktive_abgeschaltet():
    aktionen = _entscheide(
        _laufend(9.0, 12.0),
        {AUTO.schalter_entity: _an(11.0), WP.schalter_entity: _an(2.0)},
    )
    # Das Auto allein reicht (12 − 11 kW über die restlichen 10 min).
    assert [(a.schalter_entity, a.einschalten) for a in aktionen] == [(AUTO.schalter_entity, False)]


def test_reicht_einer_nicht_folgt_der_naechste_im_selben_takt():
    aktionen = _entscheide(
        _laufend(12.0, 12.0),
        {AUTO.schalter_entity: _an(3.0), WP.schalter_entity: _an(4.0)},
    )
    assert [a.schalter_entity for a in aktionen] == [AUTO.schalter_entity, WP.schalter_entity]


def test_inaktive_und_ausgeschaltete_verbraucher_bleiben_unberuehrt():
    aktionen = _entscheide(
        _laufend(9.0, 9.0),
        {AUTO.schalter_entity: _an(0.05), WP.schalter_entity: VerbraucherZustand(3.0, False)},
    )
    assert aktionen == []


def test_nach_dem_abschalten_wird_auf_die_messung_gewartet():
    # Die Hochrechnung steht noch über der Schwelle, weil das Auto langsam
    # herunterregelt — nach 20 s darf nicht gleich die Wärmepumpe folgen.
    ab = {AUTO.schalter_entity: {"seit": T0 - timedelta(seconds=20), "leistung_kw": 3.0}}
    zust = {AUTO.schalter_entity: VerbraucherZustand(0.0, False), WP.schalter_entity: _an(4.0)}
    assert _entscheide(_laufend(8.0, 8.0), zust, ab, letztes=T0 - timedelta(seconds=20)) == []
    later = T0 + timedelta(seconds=BERUHIGUNG_S)
    aktionen = _entscheide(_laufend(8.0, 8.0), zust, ab, letztes=T0 - timedelta(seconds=20), jetzt=later)
    assert [a.schalter_entity for a in aktionen] == [WP.schalter_entity]


def test_was_noch_abklingt_zaehlt_nicht_doppelt():
    # Das Auto ist aus, meldet aber noch 6 kW: Ohne Abzug schiene die
    # Viertelstunde über der Schwelle, und die Wärmepumpe ginge mit.
    ab = {AUTO.schalter_entity: {"seit": T0 - timedelta(minutes=2), "leistung_kw": 11.0}}
    zust = {AUTO.schalter_entity: VerbraucherZustand(6.0, False), WP.schalter_entity: _an(2.0)}
    assert _entscheide(_laufend(8.0, 8.0, rest_s=900), zust, ab) == []


def test_wieder_ein_erst_nach_mindestzeit_und_mit_platz_fuer_eine_volle_viertelstunde():
    zust = {AUTO.schalter_entity: VerbraucherZustand(0.0, False)}
    kurz = {AUTO.schalter_entity: {"seit": T0 - timedelta(seconds=MIN_AUS_S - 10), "leistung_kw": 3.0}}
    assert _entscheide(_laufend(1.0, 1.0), zust, kurz) == []

    lang = {AUTO.schalter_entity: {"seit": T0 - timedelta(seconds=MIN_AUS_S), "leistung_kw": 3.0}}
    aktionen = _entscheide(_laufend(1.0, 1.0), zust, lang)
    assert [(a.schalter_entity, a.einschalten) for a in aktionen] == [(AUTO.schalter_entity, True)]

    # Spät in der Viertelstunde passte das Auto rechnerisch noch hinein
    # (wenig Rest), eine volle aber nicht — dann bleibt es aus.
    assert _entscheide(_laufend(2.0, 3.0, rest_s=60), zust, lang) == []


def test_wieder_ein_in_umgekehrter_reihenfolge_und_einer_je_takt():
    seit = T0 - timedelta(hours=1)
    ab = {
        AUTO.schalter_entity: {"seit": seit, "leistung_kw": 3.0},
        WP.schalter_entity: {"seit": seit, "leistung_kw": 1.0},
    }
    zust = {
        AUTO.schalter_entity: VerbraucherZustand(0.0, False),
        WP.schalter_entity: VerbraucherZustand(0.0, False),
    }
    aktionen = _entscheide(_laufend(0.5, 0.5), zust, ab)
    assert [a.schalter_entity for a in aktionen] == [WP.schalter_entity]
    # Passt der Spätere nicht, darf der Frühere nicht an ihm vorbei.
    ab[WP.schalter_entity]["leistung_kw"] = 9.0
    assert _entscheide(_laufend(0.5, 0.5), zust, ab) == []


# ----------------------------------------------------------------------
# Verdrahtung
# ----------------------------------------------------------------------
class _Hass:
    def __init__(self, zustaende):
        self._z = zustaende
        self.states = SimpleNamespace(get=self._get)
        self.aufrufe = []
        self.services = SimpleNamespace(async_call=AsyncMock(side_effect=self._call))

    def _get(self, entity):
        wert = self._z.get(entity)
        if wert is None:
            return None
        return SimpleNamespace(state=str(wert), attributes={"unit_of_measurement": "kW"})

    async def _call(self, domaene, dienst, daten, blocking=False):
        entity = daten["entity_id"]
        self.aufrufe.append((entity, dienst))
        self._z[entity] = "on" if dienst == "turn_on" else "off"


class _Spitze:
    def __init__(self, laufend, spitze=None):
        self.werte = laufend
        self.spitze = spitze

    def laufend(self, _jetzt):
        return self.werte


def _kappung(hass, spitze, erlaubt=True, **config):
    cfg = {
        "spitzenkappung_enabled": True,
        "spitzenkappung_grenze_kw": 5.0,
        "spitzenkappung_verbraucher": [
            {"name": "Tesla", "leistung_entity": AUTO.leistung_entity, "schalter_entity": AUTO.schalter_entity},
        ],
        **config,
    }
    log = []
    k = Spitzenkappung(hass, "test", lambda: cfg, spitze, lambda: erlaubt[0] if isinstance(erlaubt, list) else erlaubt,
                       lambda z, g: log.append(z))
    return k, cfg, log


async def test_takt_schaltet_ab_und_spaeter_wieder_ein():
    hass = _Hass({AUTO.leistung_entity: 3.0, AUTO.schalter_entity: "on"})
    spitze = _Spitze(_laufend(6.0, 6.0))
    k, _cfg, log = _kappung(hass, spitze)

    await k.async_takt(T0)
    assert hass.aufrufe == [(AUTO.schalter_entity, "turn_off")]
    assert k.status()["abgeschaltet"][0]["name"] == "Tesla"
    assert log == ["Spitzenkappung: Tesla aus"]

    hass._z[AUTO.leistung_entity] = 0.0
    # 2,5 kW Bezug + 3 kW Auto passen nicht unter 5 kW − Hysterese.
    spitze.werte = _laufend(2.5, 2.5)
    await k.async_takt(T0 + timedelta(seconds=MIN_AUS_S))
    assert len(hass.aufrufe) == 1
    spitze.werte = _laufend(1.5, 1.5)
    await k.async_takt(T0 + timedelta(seconds=MIN_AUS_S + 10))
    assert hass.aufrufe[-1] == (AUTO.schalter_entity, "turn_on")
    assert k.status()["abgeschaltet"] == []


async def test_von_hand_eingeschaltet_ist_nicht_mehr_unser_eingriff():
    hass = _Hass({AUTO.leistung_entity: 11.0, AUTO.schalter_entity: "on"})
    spitze = _Spitze(_laufend(13.0, 13.0))
    k, _cfg, _log = _kappung(hass, spitze)
    await k.async_takt(T0)
    # Jemand schaltet in der App wieder ein — die Wache nimmt es zur
    # Kenntnis, schaltet aber bei fortbestehender Spitze erneut ab.
    hass._z[AUTO.schalter_entity] = "on"
    await k.async_takt(T0 + timedelta(seconds=BERUHIGUNG_S))
    assert hass.aufrufe.count((AUTO.schalter_entity, "turn_off")) == 2


async def test_modus_aus_gibt_alles_zurueck():
    hass = _Hass({AUTO.leistung_entity: 11.0, AUTO.schalter_entity: "on"})
    erlaubt = [True]
    k, _cfg, _log = _kappung(hass, _Spitze(_laufend(13.0, 13.0)), erlaubt=erlaubt)
    await k.async_takt(T0)
    erlaubt[0] = False
    await k.async_takt(T0 + timedelta(seconds=10))
    assert hass.aufrufe[-1] == (AUTO.schalter_entity, "turn_on")
    assert k.status()["abgeschaltet"] == []


async def test_funktion_aus_gibt_alles_zurueck_und_schaltet_nichts():
    hass = _Hass({AUTO.leistung_entity: 11.0, AUTO.schalter_entity: "on"})
    k, cfg, _log = _kappung(hass, _Spitze(_laufend(13.0, 13.0)))
    await k.async_takt(T0)
    cfg["spitzenkappung_enabled"] = False
    await k.async_takt(T0 + timedelta(seconds=10))
    await k.async_takt(T0 + timedelta(seconds=20))
    assert hass.aufrufe == [(AUTO.schalter_entity, "turn_off"), (AUTO.schalter_entity, "turn_on")]


async def test_aus_der_liste_entfernt_wird_zurueckgegeben():
    hass = _Hass({AUTO.leistung_entity: 11.0, AUTO.schalter_entity: "on"})
    k, cfg, _log = _kappung(hass, _Spitze(_laufend(13.0, 13.0)))
    await k.async_takt(T0)
    cfg["spitzenkappung_verbraucher"] = []
    await k.async_takt(T0 + timedelta(seconds=10))
    assert hass.aufrufe[-1] == (AUTO.schalter_entity, "turn_on")


async def test_monatsspitze_hebt_die_schwelle():
    hass = _Hass({AUTO.leistung_entity: 11.0, AUTO.schalter_entity: "on"})
    k, _cfg, _log = _kappung(hass, _Spitze(_laufend(13.0, 13.0), spitze={"kw": 14.0}))
    await k.async_takt(T0)
    assert hass.aufrufe == []
    assert k.status()["schwelle_kw"] == 14.0


async def test_fehlgeschlagener_dienst_wird_gebremst():
    hass = _Hass({AUTO.leistung_entity: 11.0, AUTO.schalter_entity: "on"})
    hass.services.async_call = AsyncMock(side_effect=RuntimeError("Fahrzeug schläft"))
    k, _cfg, _log = _kappung(hass, _Spitze(_laufend(13.0, 13.0)))
    await k.async_takt(T0)
    await k.async_takt(T0 + timedelta(seconds=10))
    assert hass.services.async_call.await_count == 1
    assert "Fahrzeug schläft" in k.status()["fehler"]
    assert k.status()["abgeschaltet"] == []


async def test_stand_uebersteht_neustart_und_entladen_gibt_zurueck():
    hass = _Hass({AUTO.leistung_entity: 11.0, AUTO.schalter_entity: "on"})
    k, cfg, _log = _kappung(hass, _Spitze(_laufend(13.0, 13.0)))
    await k.async_takt(T0)
    gespeichert = k._als_dict()

    k2 = Spitzenkappung(hass, "test", lambda: cfg, _Spitze(_laufend(13.0, 13.0)), lambda: True)
    k2.aus_dict(gespeichert)
    assert k2.status()["abgeschaltet"][0]["entity"] == AUTO.schalter_entity
    await k2.async_shutdown()
    assert hass.aufrufe[-1] == (AUTO.schalter_entity, "turn_on")
