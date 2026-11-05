"""Diagnose der Ansteuerung (diagnose.py).

Die reinen Bewertungsfunktionen direkt, den Ablauf gegen eine simulierte
Anlage: Ein Treiber-Double hält seinen Zustand (Automatik, Ladelimit,
Zwangsentladung, Netzladung, Deckel), die Simulation rechnet daraus die
Batterie- und Netzleistung, und eine Testuhr lässt die Beobachtungszeiten
ohne Warten verstreichen. Kaputte Treiber werden über Schalter simuliert:
quittiert ohne Wirkung, abgelehnt, falsch zurückgelesen, Ausnahme.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.eeg_energy_optimizer import diagnose as d
from custom_components.eeg_energy_optimizer.const import MODE_AUS, MODE_EIN


# ----------------------------------------------------------------------
# Reine Funktionen
# ----------------------------------------------------------------------
class TestUrteil:
    def test_ohne_bewertbare_proben_nicht_pruefbar(self):
        assert d.urteil([None, None]) == d.NICHT_PRUEFBAR
        assert d.urteil([]) == d.NICHT_PRUEFBAR

    def test_letzte_vier_erfuellt(self):
        assert d.urteil([False, False, True, True, True, True]) == d.ERFUELLT

    def test_nie_erfuellt(self):
        assert d.urteil([False, None, False]) == d.NICHT_ERFUELLT

    def test_kippt_ist_unklar(self):
        assert d.urteil([True, True, True, False]) == d.UNEINDEUTIG
        assert d.urteil([False, True, False, True]) == d.UNEINDEUTIG

    def test_none_zaehlt_nicht(self):
        assert d.urteil([False, True, None, True, None]) == d.UNEINDEUTIG
        assert d.urteil([True, None, True]) == d.ERFUELLT


class TestFruehFertig:
    def test_nicht_vor_mindestzeit(self):
        assert not d.frueh_fertig([True] * 10, d.BEOBACHTUNG_MIN_S - 1)

    def test_drei_in_folge(self):
        assert d.frueh_fertig([False, True, True, True], d.BEOBACHTUNG_MIN_S)

    def test_unterbrochen(self):
        assert not d.frueh_fertig([True, True, False, True], d.BEOBACHTUNG_MIN_S)

    def test_none_ueberspringen(self):
        assert d.frueh_fertig([True, None, True, None, True], d.BEOBACHTUNG_MIN_S)


class TestSchrittErgebnis:
    def test_nichts_geschrieben(self):
        assert d.schritt_ergebnis(None, None, None) == d.UEBERSPRUNGEN

    def test_abgelehnt(self):
        assert d.schritt_ergebnis(False, None, d.ERFUELLT) == d.FEHLER

    def test_ruecklesen_abweichung(self):
        assert d.schritt_ergebnis(True, "abweichung", d.ERFUELLT) == d.FEHLER

    def test_ruecklesen_nicht_lesbar_ist_kein_fehler(self):
        assert d.schritt_ergebnis(True, "nicht_lesbar", d.ERFUELLT) == d.OK

    def test_wirkungslos(self):
        assert d.schritt_ergebnis(True, "ok", d.NICHT_ERFUELLT) == d.FEHLER

    def test_unklar(self):
        assert d.schritt_ergebnis(True, "ok", d.UNEINDEUTIG) == d.UNKLAR

    def test_nicht_pruefbar_ist_teilweise(self):
        assert d.schritt_ergebnis(True, "ok", d.NICHT_PRUEFBAR) == d.TEILWEISE
        assert d.schritt_ergebnis(True, None, None) == d.TEILWEISE

    def test_alles_gut(self):
        assert d.schritt_ergebnis(True, "ok", d.ERFUELLT) == d.OK


def test_gesamt_ohne_ausnahme():
    assert d.gesamt_ergebnis([d.OK, d.TEILWEISE]) == d.TEILWEISE
    assert d.gesamt_ergebnis([d.UEBERSPRUNGEN]) == d.UEBERSPRUNGEN
    assert d.gesamt_ergebnis([d.OK, d.UEBERSPRUNGEN]) == d.OK


class TestBereichErgebnis:
    def test_schlimmster_zaehlt(self):
        assert d.bereich_ergebnis([d.OK, d.TEILWEISE, d.FEHLER]) == d.FEHLER
        assert d.bereich_ergebnis([d.TEILWEISE, d.ABGEBROCHEN]) == d.ABGEBROCHEN
        assert d.bereich_ergebnis([d.OK, d.UNKLAR, d.TEILWEISE]) == d.UNKLAR

    def test_nur_uebersprungen(self):
        assert d.bereich_ergebnis([d.UEBERSPRUNGEN]) == d.UEBERSPRUNGEN
        assert d.bereich_ergebnis([]) == d.UEBERSPRUNGEN

    def test_uebersprungen_verdeckt_nichts(self):
        assert d.bereich_ergebnis([d.UEBERSPRUNGEN, d.OK]) == d.OK

    def test_teilweise_neben_ok_ist_ok(self):
        assert d.bereich_ergebnis([d.OK, d.TEILWEISE]) == d.OK
        assert d.bereich_ergebnis([d.TEILWEISE, d.TEILWEISE]) == d.TEILWEISE
        assert d.bereich_ergebnis([d.OK, d.TEILWEISE, d.UNKLAR]) == d.UNKLAR


class TestPruefungen:
    def test_wirkung_wie_guard3(self):
        assert d.wirkung_erfuellt(1.0, 0.5)
        assert d.wirkung_erfuellt(1.0, 0.75)      # Lücke < 0,3
        assert not d.wirkung_erfuellt(1.0, 0.4)

    def test_laden_hoechstens(self):
        p = d.pruef_laden_hoechstens(0.0)
        assert p({"batt": 0.1})
        assert not p({"batt": 1.0})
        assert p({"batt": -2.0})                  # Entladen ist kein Laden
        assert p({"batt": None}) is None

    def test_entladen_mindestens(self):
        p = d.pruef_entladen_mindestens(1.0)
        assert p({"batt": -1.0})
        assert not p({"batt": 0.5})

    def test_einspeisen_mindestens(self):
        p = d.pruef_einspeisen_mindestens(1.0)
        assert p({"netz": 0.9})
        assert not p({"netz": -0.5})
        assert p({"netz": None}) is None

    def test_keine_zwangsentladung(self):
        assert d.pruef_keine_zwangsentladung({"batt": -1.0, "netz": 0.0})   # deckt das Haus
        assert d.pruef_keine_zwangsentladung({"batt": 0.0, "netz": 3.0})    # PV-Einspeisung
        assert not d.pruef_keine_zwangsentladung({"batt": -1.0, "netz": 0.8})

    def test_keine_netzladung(self):
        assert d.pruef_keine_netzladung({"batt": 2.0, "netz": 0.0})
        assert not d.pruef_keine_netzladung({"batt": 1.0, "netz": -1.0})

    def test_ruecklesen(self):
        assert d.ruecklesen_bewerten(0.0, 0.05) == "ok"
        assert d.ruecklesen_bewerten(0.0, 0.5) == "abweichung"
        assert d.ruecklesen_bewerten(5.0, 4.6) == "ok"           # 10 %
        assert d.ruecklesen_bewerten(1.0, None) == "nicht_lesbar"

    def test_median(self):
        assert d.median([3.0, 1.0, 2.0]) == 2.0
        assert d.median([1.0, 2.0]) == 1.5
        assert d.median([None, float("nan")]) is None


def test_fuer_telemetrie_ohne_ausnahmetexte_und_adressen():
    protokoll = {
        "abbruch_grund": "Interner Fehler: ConnectionError: 192.168.1.20:502 weg",
        "abbruch_ausnahme": "ConnectionError",
        "bereiche": {"netzladung": {"ergebnis": "fehler",
                                    "text": "Befehl abgelehnt: ConnectionError: 10.0.0.5:1502"}},
        "schritte": [
            {"titel": "Netzladung", "fehler": "ConnectionError: Modbus 10.0.0.5:1502",
             "ausnahme": "ConnectionError", "text": "Befehl abgelehnt: ConnectionError: Modbus 10.0.0.5:1502"},
            {"titel": "Ladelimit", "fehler": "Register abgelehnt", "text": "Befehl abgelehnt: Register abgelehnt"},
        ],
    }
    aus = d.fuer_telemetrie(protokoll)
    assert aus["abbruch_grund"] == "Interner Fehler: ConnectionError"
    assert "abbruch_ausnahme" in aus
    assert aus["schritte"][0]["fehler"] == "ConnectionError"
    assert "10.0.0.5" not in str(aus)
    assert "192.168" not in str(aus)
    assert "<adresse>" in aus["bereiche"]["netzladung"]["text"]
    # Treibertexte ohne Adresse bleiben.
    assert aus["schritte"][1]["fehler"] == "Register abgelehnt"
    # Lokal bleibt alles (fürs Panel-Protokoll).
    assert "10.0.0.5" in protokoll["schritte"][0]["fehler"]


def test_fuer_telemetrie_ohne_entitaeten_und_einstellungen():
    protokoll = {
        "einstellungen": {"schedule_min_soc_pct": 10},
        "stellwerte_vorher": [{"label": "Limit", "entity_id": "number.x", "value": "5"}],
        "stellwerte_nachher": [{"label": "Limit", "entity_id": "number.x", "value": "5"}],
        "schritte": [{"titel": "a", "stellwerte": [{"entity_id": "number.y", "value": 1}]}],
    }
    aus = d.fuer_telemetrie(protokoll)
    assert "einstellungen" not in aus
    assert aus["stellwerte_vorher"] == [{"label": "Limit", "value": "5"}]
    assert "entity_id" not in aus["schritte"][0]["stellwerte"][0]
    # Das Original bleibt unverändert (das Panel lädt es herunter).
    assert protokoll["stellwerte_vorher"][0]["entity_id"] == "number.x"


# ----------------------------------------------------------------------
# Simulierte Anlage
# ----------------------------------------------------------------------
class Uhr:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    async def schlafen(self, s):
        self.t += s
        await asyncio.sleep(0)


class FakeInverter:
    """Treiber-Double mit Zustand. Schalter simulieren kaputte Wege."""

    def __init__(self, *, netz_sollwert=False, deckel=True, netzladung=True,
                 zwangsentladung=True):
        self.modus = "auto"
        self.ladelimit = None
        self.entladung = 0.0
        self.netzladung = 0.0
        self.deckel = None
        self.supports_schedule_control = True
        self.supports_discharge_limit = deckel
        self.supports_grid_charge = netzladung
        self.supports_forced_discharge = zwangsentladung
        self.discharge_is_grid_setpoint = netz_sollwert
        self.is_available = True
        self.last_write_error = None
        self.befehle: list[tuple] = []
        # Schalter
        self.entladung_wirkungslos = False
        self.ladelimit_abgelehnt = False
        self.ladelimit_falsch_gelesen = False
        self.deckel_wirkungslos = False
        self.netzladung_wirft = False
        self.stopp_abgelehnt = False
        self.ladelimit_nur_null = False      # Wert ≠ 0 wird ignoriert, 0 bleibt
        self.deckel_nur_null = False         # Deckel ≠ 0 und Aufheben ignoriert
        self.entladung_nur_ladestopp = False # Zwangsmodus sperrt nur das Laden
        self.ruecklesen_verzoegert = 0       # so viele Lesungen noch der alte Wert
        self.ladelimit_vom_geraet_gelesen = True
        self.batterie_gesperrt = False
        self.hinweis = None

    async def async_set_charge_limit(self, kw):
        self.befehle.append(("ladelimit", kw))
        if self.ladelimit_abgelehnt:
            self.last_write_error = "Register abgelehnt"
            return False
        if self.ladelimit_nur_null and kw != 0 and self.ladelimit == 0:
            return True
        self._alt_limit = self.ladelimit
        self.ladelimit = kw
        if self.hinweis:
            self.last_write_error = self.hinweis
        return True

    async def async_get_charge_limit_kw(self):
        if self.ruecklesen_verzoegert > 0:
            self.ruecklesen_verzoegert -= 1
            alt = getattr(self, "_alt_limit", None)
            return 5.0 if alt is None else alt
        if self.ladelimit is None:
            return 5.0
        return self.ladelimit + (0.8 if self.ladelimit_falsch_gelesen else 0.0)

    def get_charge_limit_max_kw(self):
        return 5.0

    def get_max_discharge_power_kw(self):
        return 5.0

    def get_backup_reserve_soc_pct(self):
        return None

    def get_combined_battery_state(self):
        return (None, None)

    async def async_set_discharge(self, kw, ziel=None):
        self.befehle.append(("entladung", kw, ziel))
        self.modus = "entladung"
        self.entladung = kw
        return True

    async def async_set_grid_charge(self, kw, ziel=None):
        self.befehle.append(("netzladung", kw, ziel))
        if self.netzladung_wirft:
            raise ConnectionError("Modbus weg")
        self.modus = "netzladung"
        self.netzladung = kw
        return True

    async def async_set_discharge_limit(self, kw):
        self.befehle.append(("deckel", kw))
        if self.deckel_nur_null and self.deckel == 0.0 and kw != 0.0:
            return True
        self.deckel = kw
        return True

    async def async_stop_forcible(self):
        self.befehle.append(("stopp",))
        if self.stopp_abgelehnt:
            self.last_write_error = "Stopp abgelehnt"
            return False
        self.modus = "auto"
        self.ladelimit = None
        self.entladung = 0.0
        self.netzladung = 0.0
        return True

    async def async_get_control_values(self):
        return [{"label": "Modus", "value": self.modus, "unit": None, "role": "mode"}]

    def get_control_entities(self):
        return [{"label": "Ladelimit", "entity_id": "number.ladelimit", "role": "charge_limit"}]


class Anlage:
    """Rechnet aus Treiberzustand, PV und Haus die Messwerte."""

    def __init__(self, inv, *, pv, haus, soc):
        self.inv, self.pv, self.haus, self.soc = inv, pv, haus, soc
        self.messen_wirft = False

    def batterie(self):
        inv = self.inv
        if inv.modus == "entladung" and inv.entladung_nur_ladestopp:
            ueberschuss = self.pv - self.haus
            return min(ueberschuss, 0.0)
        if inv.modus == "entladung" and not inv.entladung_wirkungslos:
            if inv.discharge_is_grid_setpoint:
                return -(inv.entladung + self.haus - self.pv)
            return -inv.entladung
        if inv.modus == "netzladung":
            return inv.netzladung
        ueberschuss = self.pv - self.haus
        if ueberschuss >= 0:
            grenze = 5.0 if inv.ladelimit is None else inv.ladelimit
            return min(ueberschuss, grenze)
        bedarf = -ueberschuss
        if inv.deckel is not None and not inv.deckel_wirkungslos:
            bedarf = min(bedarf, inv.deckel)
        return -bedarf

    def __call__(self):
        if self.messen_wirft:
            raise RuntimeError("Sensor kaputt")
        b = self.batterie()
        return {"soc": self.soc, "pv": self.pv, "batt": b,
                "netz": self.pv - self.haus - b, "haus": self.haus}


class FakeSelect:
    def __init__(self, modus):
        self._attr_current_option = modus
        self.verlauf = [modus]

    async def async_select_option(self, modus):
        self._attr_current_option = modus
        self.verlauf.append(modus)


class FakeExecutor:
    def __init__(self, inv):
        self.inv = inv
        self.fremdeingriff = 0
        self.freigaben = 0

    def nach_fremdeingriff(self):
        self.fremdeingriff += 1

    async def async_release(self):
        self.freigaben += 1
        ok = await self.inv.async_stop_forcible()
        if self.inv.supports_discharge_limit:
            await self.inv.async_set_discharge_limit(None)
        return ok


class FakeStore:
    def __init__(self, inhalt=None):
        self.inhalt = inhalt
        self.geschrieben = []

    async def async_load(self):
        return self.inhalt

    async def async_save(self, daten):
        self.inhalt = daten
        self.geschrieben.append(daten)


def _states():
    zustand = SimpleNamespace(state="5.0", attributes={"unit_of_measurement": "kW"})
    return SimpleNamespace(get=lambda eid: zustand)


def _aufbau(*, pv, haus, soc, modus=MODE_EIN, config=None, **inv_args):
    inv = FakeInverter(**inv_args)
    anlage = Anlage(inv, pv=pv, haus=haus, soc=soc)
    uhr = Uhr()
    data = {
        "config": {"inverter_type": "huawei", "schedule_min_soc_pct": 10,
                   "schedule_max_soc_pct": 100, **(config or {})},
        "inverter": inv,
        "executor": FakeExecutor(inv),
        "select": FakeSelect(modus),
        "_run_cycle": AsyncMock(),
    }
    hass = SimpleNamespace(states=_states())
    gemeldet = []
    diag = d.AnsteuerungsDiagnose(
        hass, "e1", data, messen=anlage, schlafen=uhr.schlafen, uhr=uhr,
        bei_ende=gemeldet.append,
    )
    diag._store = FakeStore()
    return diag, inv, anlage, data, uhr, gemeldet


async def _laufen(diag):
    ok, meldung = diag.async_start()
    assert ok, meldung
    await diag._task
    return diag._letztes


def _bereich(protokoll, name):
    return protokoll["bereiche"][name]["ergebnis"]


def _schritte(protokoll, bereich):
    return [s for s in protokoll["schritte"] if s["bereich"] == bereich]


# ----------------------------------------------------------------------
# Abläufe
# ----------------------------------------------------------------------
class TestMittag:
    """PV-Überschuss: Ladelimit prüfbar, Deckel und Netzladung nicht."""

    @pytest.mark.asyncio
    async def test_ganzer_lauf(self):
        diag, inv, anlage, data, uhr, gemeldet = _aufbau(pv=5.0, haus=1.0, soc=50)
        p = await _laufen(diag)
        assert p["abbruch_grund"] is None
        assert _bereich(p, "vorpruefung") == d.OK
        assert _bereich(p, "ladelimit") == d.OK
        assert _bereich(p, "entladung") == d.OK
        assert _bereich(p, "freigabe") == d.OK
        # Keine Entladung im Automatikbetrieb → Deckel nicht prüfbar.
        assert _bereich(p, "deckel") == d.TEILWEISE
        # Die Batterie lädt schon aus PV → Netzladung nicht unterscheidbar.
        assert _bereich(p, "netzladung") == d.TEILWEISE
        assert p["ergebnis"] == d.TEILWEISE
        # Modus: Aus während des Tests, danach wieder Ein.
        assert data["select"].verlauf == [MODE_EIN, MODE_AUS, MODE_EIN]
        assert p["modus_wiederhergestellt"] is True
        assert data["diagnose_laeuft"] is False
        assert diag.laeuft is False
        # Abschluss über den Executor, der vorher vom Fremdeingriff erfährt.
        assert data["executor"].fremdeingriff == 1
        assert data["executor"].freigaben == 1
        assert inv.modus == "auto" and inv.deckel is None and inv.ladelimit is None
        # Telemetrie-Haken mit dem Protokoll gerufen.
        assert gemeldet == [p]

    @pytest.mark.asyncio
    async def test_netz_sollwert_treiber_misst_am_netz(self):
        diag, inv, anlage, *_ = _aufbau(pv=5.0, haus=1.0, soc=50, netz_sollwert=True)
        p = await _laufen(diag)
        s = _schritte(p, "entladung")[0]
        # Netz-Sollwert mittags: vorherige Einspeisung (0) + was die Batterie
        # lud (4 kW) + 1 kW — so viel geht nur mit Entladung.
        assert s["befehl"]["power_kw"] == 5.0
        assert s["ergebnis"] == d.OK

    @pytest.mark.asyncio
    async def test_abends_sollwert_mit_hausbedarf(self):
        """Batterie-Treiber: Haus 2 kW + 1 kW Einspeisung = 3 kW Sollwert."""
        diag, inv, *_ = _aufbau(pv=0.0, haus=2.0, soc=60)
        p = await _laufen(diag)
        s = _schritte(p, "entladung")[0]
        assert s["befehl"]["power_kw"] == 3.0
        assert s["ergebnis"] == d.OK
        diag, inv, *_ = _aufbau(pv=0.0, haus=2.0, soc=60, netz_sollwert=True)
        p = await _laufen(diag)
        assert _schritte(p, "entladung")[0]["befehl"]["power_kw"] == 1.0
        assert _schritte(p, "entladung")[0]["ergebnis"] == d.OK

    @pytest.mark.asyncio
    async def test_batterie_zu_schwach_fuer_einspeisung(self):
        diag, inv, *_ = _aufbau(pv=0.0, haus=4.8, soc=60)
        p = await _laufen(diag)
        s = _schritte(p, "entladung")[0]
        assert s["ergebnis"] == d.UEBERSPRUNGEN
        assert not any(b[0] == "entladung" for b in inv.befehle)

    @pytest.mark.asyncio
    async def test_bereiche_werden_gespeichert(self):
        diag, *_ = _aufbau(pv=5.0, haus=1.0, soc=50)
        await _laufen(diag)
        gespeichert = diag._store.inhalt
        assert gespeichert["laeuft_seit"] is None
        assert gespeichert["bereiche"]["ladelimit"]["ergebnis"] == d.OK
        assert gespeichert["bereiche"]["ladelimit"]["zeit"]
        # Während des Laufs stand ``laeuft_seit`` im Speicher.
        assert diag._store.geschrieben[0]["laeuft_seit"]


class TestAbend:
    """Kein PV, Haus zieht: Deckel und Netzladung prüfbar, Ladelimit nicht."""

    @pytest.mark.asyncio
    async def test_ganzer_lauf(self):
        diag, inv, *_ = _aufbau(pv=0.0, haus=2.0, soc=60)
        p = await _laufen(diag)
        assert _bereich(p, "ladelimit") == d.TEILWEISE
        assert _bereich(p, "entladung") == d.OK
        assert _bereich(p, "deckel") == d.OK
        assert _bereich(p, "netzladung") == d.OK
        assert _bereich(p, "freigabe") == d.OK
        texte = [s["voraussetzung"] for s in _schritte(p, "ladelimit") if s["voraussetzung"]]
        assert any("Mittagszeit" in t for t in texte)

    @pytest.mark.asyncio
    async def test_teilweise_ueberschreibt_kein_ok(self):
        """Mittags bestanden, abends nur teilweise → das OK bleibt stehen."""
        diag, inv, anlage, *_ = _aufbau(pv=5.0, haus=1.0, soc=50)
        await _laufen(diag)
        assert diag._bereiche["ladelimit"]["ergebnis"] == d.OK
        anlage.pv, anlage.haus = 0.0, 2.0
        await _laufen(diag)
        assert diag._bereiche["ladelimit"]["ergebnis"] == d.OK
        assert diag._bereiche["ladelimit"]["zuletzt_teilweise"]
        assert diag._bereiche["deckel"]["ergebnis"] == d.OK
        assert diag.nie_vollstaendig is False

    @pytest.mark.asyncio
    async def test_netzladung_begrenzt_durch_spitzenkappung(self):
        diag, inv, *_ = _aufbau(
            pv=0.0, haus=2.0, soc=60,
            config={"spitzenkappung_enabled": True, "spitzenkappung_grenze_kw": 2.5},
        )
        p = await _laufen(diag)
        s = _schritte(p, "netzladung")[0]
        # 2,5 − 0,3 − 2,0 = 0,2 kW Luft → zu wenig, übersprungen.
        assert s["ergebnis"] == d.UEBERSPRUNGEN
        assert "Spitzenkappungs-Grenze" in s["voraussetzung"]
        assert not any(b[0] == "netzladung" for b in inv.befehle)


class TestKaputteTreiber:
    @pytest.mark.asyncio
    async def test_entladung_quittiert_ohne_wirkung(self):
        diag, inv, *_ = _aufbau(pv=0.0, haus=2.0, soc=60)
        inv.entladung_wirkungslos = True
        p = await _laufen(diag)
        assert _bereich(p, "entladung") == d.FEHLER
        s = _schritte(p, "entladung")[0]
        assert s["befehl_ok"] is True
        assert s["wirkung"] == d.NICHT_ERFUELLT
        assert "keine Wirkung" in s["text"]
        befunde = d.befunde(p)
        assert {"bereich": "entladung", "art": "wirkungslos"}.items() <= befunde[0].items()
        assert befunde[0]["batt_median"] is not None

    @pytest.mark.asyncio
    async def test_ladelimit_abgelehnt(self):
        diag, inv, *_ = _aufbau(pv=5.0, haus=1.0, soc=50)
        inv.ladelimit_abgelehnt = True
        p = await _laufen(diag)
        assert _bereich(p, "ladelimit") == d.FEHLER
        s = _schritte(p, "ladelimit")[0]
        assert s["fehler"] == "Register abgelehnt"
        assert "Register abgelehnt" in s["text"]
        arten = {b["art"] for b in d.befunde(p)}
        assert "abgelehnt (Register abgelehnt)" in arten

    @pytest.mark.asyncio
    async def test_ladelimit_falsch_zurueckgelesen(self):
        diag, inv, *_ = _aufbau(pv=5.0, haus=1.0, soc=50)
        inv.ladelimit_falsch_gelesen = True
        p = await _laufen(diag)
        s = _schritte(p, "ladelimit")[0]
        assert s["ruecklesen"] == "abweichung"
        assert s["ergebnis"] == d.FEHLER
        assert "ruecklesen" in {b["art"] for b in d.befunde(p)}

    @pytest.mark.asyncio
    async def test_deckel_wirkungslos(self):
        diag, inv, *_ = _aufbau(pv=0.0, haus=2.0, soc=60)
        inv.deckel_wirkungslos = True
        p = await _laufen(diag)
        assert _bereich(p, "deckel") == d.FEHLER
        # Das Aufheben wird trotzdem geschrieben.
        assert ("deckel", None) in inv.befehle

    @pytest.mark.asyncio
    async def test_ausnahme_im_treiber(self):
        diag, inv, *_ = _aufbau(pv=0.0, haus=2.0, soc=60)
        inv.netzladung_wirft = True
        p = await _laufen(diag)
        s = _schritte(p, "netzladung")[0]
        assert s["befehl_ok"] is False
        assert s["fehler"].startswith("ConnectionError")
        # Zahlenfreie Art: nur der Ausnahme-Typ.
        assert "abgelehnt (ConnectionError)" in {b["art"] for b in d.befunde(p)}
        # Der Test lief weiter bis zum Abschluss.
        assert p["abbruch_grund"] is None

    @pytest.mark.asyncio
    async def test_freigabe_scheitert_modus_bleibt_aus(self):
        diag, inv, anlage, data, *_ = _aufbau(pv=5.0, haus=1.0, soc=50)
        inv.stopp_abgelehnt = True
        p = await _laufen(diag)
        assert _bereich(p, "freigabe") == d.FEHLER
        abschluss = p["schritte"][-1]
        assert abschluss["titel"].startswith("Abschluss")
        assert abschluss["ergebnis"] == d.FEHLER
        assert p["modus_wiederhergestellt"] is False
        assert data["select"]._attr_current_option == MODE_AUS

    @pytest.mark.asyncio
    async def test_nicht_erreichbar_bricht_ab_und_gibt_frei(self):
        diag, inv, anlage, data, *_ = _aufbau(pv=5.0, haus=1.0, soc=50)
        inv.is_available = False
        p = await _laufen(diag)
        assert p["ergebnis"] == d.ABGEBROCHEN
        assert "nicht erreichbar" in p["abbruch_grund"]
        assert _bereich(p, "vorpruefung") == d.FEHLER
        assert data["executor"].freigaben == 1

    @pytest.mark.asyncio
    async def test_messwerte_fehlen(self):
        diag, inv, anlage, *_ = _aufbau(pv=5.0, haus=1.0, soc=None)
        p = await _laufen(diag)
        assert p["ergebnis"] == d.ABGEBROCHEN
        assert "Ladestand" in p["abbruch_grund"]
        # Kein einziger Schreibbefehl außer der Abschluss-Freigabe.
        assert all(b[0] in ("stopp", "deckel") for b in inv.befehle)

    @pytest.mark.asyncio
    async def test_interner_fehler_gibt_frei(self):
        diag, inv, anlage, data, *_ = _aufbau(pv=5.0, haus=1.0, soc=50)
        anlage.messen_wirft = True
        p = await _laufen(diag)
        assert p["ergebnis"] == d.ABGEBROCHEN
        assert p["abbruch_grund"].startswith("Interner Fehler")
        assert data["executor"].freigaben == 1
        assert data["diagnose_laeuft"] is False
        assert any(b["art"] == "intern" for b in d.befunde(p))


class TestVoraussetzungen:
    @pytest.mark.asyncio
    async def test_ladestand_niedrig_keine_entladung(self):
        diag, inv, *_ = _aufbau(pv=0.0, haus=2.0, soc=12)
        p = await _laufen(diag)
        s = _schritte(p, "entladung")[0]
        assert s["ergebnis"] == d.UEBERSPRUNGEN
        assert not any(b[0] == "entladung" for b in inv.befehle)

    @pytest.mark.asyncio
    async def test_backup_soc_hebt_die_grenze(self):
        diag, inv, *_ = _aufbau(pv=0.0, haus=2.0, soc=30)
        inv.get_backup_reserve_soc_pct = lambda: 28.0
        p = await _laufen(diag)
        assert _schritte(p, "entladung")[0]["ergebnis"] == d.UEBERSPRUNGEN

    @pytest.mark.asyncio
    async def test_voll_keine_netzladung(self):
        diag, inv, *_ = _aufbau(pv=0.0, haus=2.0, soc=97)
        p = await _laufen(diag)
        assert _schritte(p, "netzladung")[0]["ergebnis"] == d.UEBERSPRUNGEN
        assert not any(b[0] == "netzladung" for b in inv.befehle)

    @pytest.mark.asyncio
    async def test_ziel_soc_der_entladung(self):
        diag, inv, *_ = _aufbau(pv=0.0, haus=2.0, soc=60)
        await _laufen(diag)
        entladung = next(b for b in inv.befehle if b[0] == "entladung")
        assert entladung[2] == 57
        netz = next(b for b in inv.befehle if b[0] == "netzladung")
        assert netz[2] == 63

    @pytest.mark.asyncio
    async def test_treiber_ohne_faehigkeiten(self):
        diag, inv, *_ = _aufbau(
            pv=0.0, haus=2.0, soc=60, deckel=False, netzladung=False, zwangsentladung=False,
        )
        p = await _laufen(diag)
        for bereich in ("entladung", "deckel", "netzladung"):
            assert _bereich(p, bereich) == d.UEBERSPRUNGEN
        assert not any(b[0] in ("entladung", "deckel", "netzladung") for b in inv.befehle)

    @pytest.mark.asyncio
    async def test_uebersprungen_ueberschreibt_kein_ergebnis(self):
        diag, inv, anlage, *_ = _aufbau(pv=0.0, haus=2.0, soc=60)
        await _laufen(diag)
        assert diag._bereiche["entladung"]["ergebnis"] == d.OK
        anlage.soc = 11
        await _laufen(diag)
        assert diag._bereiche["entladung"]["ergebnis"] == d.OK


class TestAbbruch:
    @pytest.mark.asyncio
    async def test_nutzer_bricht_ab(self):
        diag, inv, anlage, data, uhr, _ = _aufbau(pv=0.0, haus=2.0, soc=60)
        diag.async_start()
        # Laufen lassen, bis die Zwangsentladung beobachtet wird.
        for _ in range(2000):
            await asyncio.sleep(0)
            if inv.modus == "entladung":
                break
        assert inv.modus == "entladung"
        assert diag.abbrechen()
        await diag._task
        p = diag._letztes
        assert p["ergebnis"] == d.ABGEBROCHEN
        assert p["abbruch_grund"] == "Vom Nutzer abgebrochen"
        # Der laufende Schritt ist abgeschlossen, keiner bleibt ohne Ergebnis.
        assert all(s["ergebnis"] for s in p["schritte"])
        assert inv.modus == "auto"
        assert data["executor"].freigaben == 1
        # Abgebrochene Bereiche überschreiben kein älteres Ergebnis — hier
        # gab es keins, also steht die Entladung als abgebrochen.
        assert diag._bereiche["entladung"]["ergebnis"] == d.ABGEBROCHEN

    @pytest.mark.asyncio
    async def test_abbruch_im_deckel_hebt_ihn_auf(self):
        diag, inv, anlage, data, uhr, _ = _aufbau(pv=0.0, haus=2.0, soc=60)
        diag.async_start()
        for _ in range(5000):
            await asyncio.sleep(0)
            if inv.deckel == 0.0:
                break
        assert inv.deckel == 0.0
        diag.abbrechen()
        await diag._task
        p = diag._letztes
        assert all(s["ergebnis"] for s in p["schritte"])
        titel = [s["titel"] for s in _schritte(p, "deckel")]
        assert "Entladedeckel aufheben" in titel
        assert inv.deckel is None

    @pytest.mark.asyncio
    async def test_modus_ein_waehrend_des_tests(self):
        diag, inv, anlage, data, uhr, _ = _aufbau(pv=0.0, haus=2.0, soc=60)
        diag.async_start()
        for _ in range(500):
            await asyncio.sleep(0)
            if data["diagnose_laeuft"]:
                break
        data["select"]._attr_current_option = MODE_EIN
        await diag._task
        p = diag._letztes
        assert p["ergebnis"] == d.ABGEBROCHEN
        assert "Ein gestellt" in p["abbruch_grund"]
        assert p["modus_wiederhergestellt"] is False

    @pytest.mark.asyncio
    async def test_stilllegen_wartet_auf_freigabe(self):
        diag, inv, anlage, data, uhr, gemeldet = _aufbau(pv=0.0, haus=2.0, soc=60)
        diag.async_start()
        for _ in range(500):
            await asyncio.sleep(0)
            if data["diagnose_laeuft"]:
                break
        data["stopping"] = True
        await diag.async_stilllegen(5.0)
        assert diag.laeuft is False
        p = diag._letztes
        assert p["ergebnis"] == d.ABGEBROCHEN
        assert data["executor"].freigaben == 1
        # Beim Entladen weder Modus zurück noch Guard-Lauf hinterher.
        assert p["modus_wiederhergestellt"] is False

    @pytest.mark.asyncio
    async def test_zweiter_start_abgelehnt(self):
        diag, *_ = _aufbau(pv=0.0, haus=2.0, soc=60)
        assert diag.async_start()[0]
        ok, meldung = diag.async_start()
        assert not ok and "läuft bereits" in meldung
        diag.abbrechen()
        await diag._task

    @pytest.mark.asyncio
    async def test_nicht_gesteuerter_treiber(self):
        diag, inv, *_ = _aufbau(pv=0.0, haus=2.0, soc=60)
        inv.supports_schedule_control = False
        ok, meldung = diag.async_start()
        assert not ok and "nicht gesteuert" in meldung

    @pytest.mark.asyncio
    async def test_modus_aus_bleibt_aus(self):
        diag, inv, anlage, data, *_ = _aufbau(pv=0.0, haus=2.0, soc=60, modus=MODE_AUS)
        p = await _laufen(diag)
        assert data["select"].verlauf == [MODE_AUS]
        assert p["modus_wiederhergestellt"] is False

    @pytest.mark.asyncio
    async def test_guard_lauf_vor_und_nach_dem_test(self):
        diag, inv, anlage, data, *_ = _aufbau(pv=0.0, haus=2.0, soc=60)
        await _laufen(diag)
        assert data["_run_cycle"].await_count == 2


class TestStatusUndSpeicher:
    @pytest.mark.asyncio
    async def test_neustart_mitten_im_test(self):
        diag, *_ = _aufbau(pv=0.0, haus=2.0, soc=60)
        diag._store = FakeStore({
            "bereiche": {"ladelimit": {"ergebnis": "ok", "zeit": "2026-11-05T12:00"}},
            "letztes": None,
            "laeuft_seit": "2026-11-05T18:00:00+01:00",
        })
        await diag.async_load()
        assert diag._letztes["ergebnis"] == d.ABGEBROCHEN
        assert "Neustart" in diag._letztes["abbruch_grund"]
        assert diag._bereiche["ladelimit"]["ergebnis"] == "ok"
        assert diag._store.inhalt["laeuft_seit"] is None

    @pytest.mark.asyncio
    async def test_kaputter_speicher(self):
        diag, *_ = _aufbau(pv=0.0, haus=2.0, soc=60)
        diag._store = FakeStore({"bereiche": "quatsch", "letztes": 3,
                                 "laeuft_seit": None})
        await diag.async_load()
        assert diag._bereiche == {}
        assert diag._letztes is None

    def test_nie_vollstaendig(self):
        diag, *_ = _aufbau(pv=0.0, haus=2.0, soc=60)
        assert diag.nie_vollstaendig
        diag._bereiche = {b: {"ergebnis": d.OK} for b in d.BEREICHE}
        assert not diag.nie_vollstaendig
        diag._bereiche["deckel"] = {"ergebnis": d.TEILWEISE}
        assert not diag.nie_vollstaendig
        # Übersprungen mangels Lage: nie geprüft.
        diag._bereiche["deckel"] = {"ergebnis": d.UEBERSPRUNGEN}
        assert diag.nie_vollstaendig
        # Übersprungen mangels Fähigkeit: nichts zu prüfen.
        diag._bereiche["deckel"] = {"ergebnis": d.UEBERSPRUNGEN, "ohne_faehigkeit": True}
        assert not diag.nie_vollstaendig
        diag._bereiche["deckel"] = {"ergebnis": d.FEHLER}
        assert diag.nie_vollstaendig

    @pytest.mark.asyncio
    async def test_ohne_faehigkeit_wird_gemerkt(self):
        diag, inv, *_ = _aufbau(pv=0.0, haus=2.0, soc=12, deckel=False)
        await _laufen(diag)
        assert diag._bereiche["deckel"]["ohne_faehigkeit"] is True
        # Entladung mangels Ladestand übersprungen → nicht als Fähigkeit.
        assert "ohne_faehigkeit" not in diag._bereiche["entladung"]
        assert diag.nie_vollstaendig

    @pytest.mark.asyncio
    async def test_status_live_und_protokoll(self):
        diag, inv, anlage, data, *_ = _aufbau(pv=0.0, haus=2.0, soc=60)
        diag.async_start()
        for _ in range(500):
            await asyncio.sleep(0)
            if diag._schritt is not None and diag._schritt["bereich"] == "ladelimit":
                break
        live = diag.status()
        assert live["laeuft"] is True
        assert live["schritt"]["bereich"] == "ladelimit"
        assert "proben" not in live["schritt"]
        assert live["probe"] is not None
        assert list(live["bereiche"]) == list(d.BEREICHE)
        diag.abbrechen()
        await diag._task
        voll = diag.status(mit_protokoll=True)
        assert "proben" in voll["letztes"]["schritte"][0]
        kurz = diag.status()
        assert "proben" not in kurz["letztes"]["schritte"][0]
        assert voll["letztes"]["stellwerte_vorher"][1]["entity_id"] == "number.ladelimit"


# ----------------------------------------------------------------------
# Executor: nach dem Fremdeingriff hebt die Freigabe auch den Deckel auf
# ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_executor_nach_fremdeingriff_hebt_deckel_auf(mock_hass, mock_inverter):
    from custom_components.eeg_energy_optimizer.schedule_executor import ScheduleExecutor

    mock_inverter.supports_discharge_limit = True
    mock_inverter.async_set_discharge_limit = AsyncMock(return_value=True)
    ex = ScheduleExecutor(mock_hass, "e1", {}, mock_inverter)
    # Ohne Spitzenkappung glaubt der Executor, es stehe kein Deckel.
    assert ex._entladedeckel_offen is False
    ex.nach_fremdeingriff()
    assert ex.hat_eingriff
    assert await ex.async_release()
    mock_inverter.async_set_discharge_limit.assert_awaited_with(None)
    assert ex._active_kind == "release"


# ----------------------------------------------------------------------
# Befunde aus dem Review (05.11.2026): Zustände, die eine Prüfung vorher
# schon erfüllten, Kostal-Sperre, MQTT-Verzögerung, Abbruch durch HA.
# ----------------------------------------------------------------------
class TestReviewBefunde:
    @pytest.mark.asyncio
    async def test_ladelimit_1kw_ignoriert_faellt_auf(self):
        diag, inv, *_ = _aufbau(pv=5.0, haus=1.0, soc=50)
        inv.ladelimit_nur_null = True
        inv.ladelimit_vom_geraet_gelesen = False   # Rücklesen allein fände es nicht
        p = await _laufen(diag)
        s = [x for x in _schritte(p, "ladelimit") if x["titel"].startswith("Ladelimit 1")][0]
        assert s["wirkung"] == d.NICHT_ERFUELLT
        assert _bereich(p, "ladelimit") == d.FEHLER

    @pytest.mark.asyncio
    async def test_deckel_05_und_aufheben_ignoriert(self):
        diag, inv, *_ = _aufbau(pv=0.0, haus=2.0, soc=60)
        inv.deckel_nur_null = True
        p = await _laufen(diag)
        titel = {x["titel"]: x for x in _schritte(p, "deckel")}
        assert titel["Entladedeckel 0.5 kW"]["wirkung"] == d.NICHT_ERFUELLT
        assert titel["Entladedeckel aufheben"]["wirkung"] == d.NICHT_ERFUELLT
        assert _bereich(p, "deckel") == d.FEHLER

    @pytest.mark.asyncio
    async def test_aufheben_bei_kleiner_entladung_nicht_pruefbar(self):
        diag, inv, *_ = _aufbau(pv=0.0, haus=0.8, soc=60)
        p = await _laufen(diag)
        titel = {x["titel"]: x for x in _schritte(p, "deckel")}
        assert titel["Entladedeckel 0 kW (halten)"]["ergebnis"] == d.OK
        assert titel["Entladedeckel aufheben"]["wirkung"] == d.NICHT_PRUEFBAR
        assert _bereich(p, "deckel") == d.OK

    @pytest.mark.asyncio
    async def test_mittags_nur_ladestopp_ist_keine_entladung(self):
        diag, inv, *_ = _aufbau(pv=5.0, haus=1.0, soc=50)
        inv.entladung_nur_ladestopp = True
        p = await _laufen(diag)
        s = _schritte(p, "entladung")[0]
        assert s["wirkung"] == d.NICHT_ERFUELLT
        # Die Freigabe danach ist nicht prüfbar, nicht geschenkt.
        f = [x for x in _schritte(p, "freigabe") if "Zwangsentladung" in x["titel"]][0]
        assert f["wirkung"] == d.NICHT_PRUEFBAR

    @pytest.mark.asyncio
    async def test_einspeisegrenze_ohne_luft(self):
        diag, inv, *_ = _aufbau(
            pv=5.0, haus=1.0, soc=95,   # Batterie fast voll, lädt kaum → speist ein
            config={"grid_export_limit_enabled": True, "grid_export_limit_kw": 4.0},
        )
        inv.ladelimit = None
        # Volle Batterie simulieren: lädt nicht, alles ins Netz.
        anlage = diag._messen_fn
        anlage.pv, anlage.haus = 5.0, 1.0
        inv.ladelimit = 0.0
        p = await _laufen(diag)
        s = _schritte(p, "entladung")[0]
        assert s["ergebnis"] == d.UEBERSPRUNGEN
        assert "Einspeisegrenze" in s["voraussetzung"]

    @pytest.mark.asyncio
    async def test_rueckgelesen_nicht_vom_geraet(self):
        diag, inv, *_ = _aufbau(pv=5.0, haus=1.0, soc=50)
        inv.ladelimit_vom_geraet_gelesen = False
        p = await _laufen(diag)
        s = _schritte(p, "ladelimit")[0]
        assert s["ruecklesen"] == "nicht_lesbar"
        assert s["ruecklesen_hinweis"]

    @pytest.mark.asyncio
    async def test_rueckgelesen_verzoegert_wird_abgewartet(self):
        diag, inv, *_ = _aufbau(pv=5.0, haus=1.0, soc=50)
        inv.ruecklesen_verzoegert = 3
        p = await _laufen(diag)
        s = _schritte(p, "ladelimit")[0]
        assert s["ruecklesen"] == "ok"
        assert s["ergebnis"] == d.OK

    @pytest.mark.asyncio
    async def test_hinweis_bei_teilerfolg(self):
        diag, inv, *_ = _aufbau(pv=5.0, haus=1.0, soc=50)
        inv.hinweis = "Deckel nicht aufgehoben"
        p = await _laufen(diag)
        assert _schritte(p, "ladelimit")[0]["hinweis"] == "Deckel nicht aufgehoben"

    @pytest.mark.asyncio
    async def test_kostal_sperre_wird_abgewartet(self):
        diag, inv, anlage, data, uhr, _ = _aufbau(pv=0.0, haus=2.0, soc=60)
        start = uhr.t
        inv.batterie_gesperrt = True

        async def freigeben_nach():
            while uhr.t < start + 200:
                await asyncio.sleep(0)
            inv.batterie_gesperrt = False

        aufgabe = asyncio.ensure_future(freigeben_nach())
        p = await _laufen(diag)
        await aufgabe
        warten = [x for x in p["schritte"] if x["titel"].startswith("Warten auf den Watchdog")]
        assert warten and warten[0]["ergebnis"] == d.OK
        assert _bereich(p, "entladung") == d.OK

    @pytest.mark.asyncio
    async def test_kostal_sperre_ohne_ende(self):
        diag, inv, *_ = _aufbau(pv=0.0, haus=2.0, soc=60)
        inv.batterie_gesperrt = True
        p = await _laufen(diag)
        assert _bereich(p, "ladelimit") == d.UEBERSPRUNGEN
        assert "Watchdog" in p["bereiche"]["ladelimit"]["text"]
        # Übersprungen mangels Lage → zählt als nie geprüft.
        assert diag.nie_vollstaendig
        assert not any(b[0] in ("ladelimit", "entladung", "netzladung") for b in inv.befehle)

    @pytest.mark.asyncio
    async def test_freigabe_ohne_geprueften_schritt_ist_teilweise(self):
        """Ausgangslage und Abschluss prüfen nichts — sie machen kein „ok"."""
        diag, inv, *_ = _aufbau(pv=0.0, haus=2.0, soc=60)
        inv.entladung_wirkungslos = True
        p = await _laufen(diag)
        # Ladelimit-Freigabe nicht prüfbar (keine Ladung), Entladung wirkungslos
        # → deren Freigabe nicht prüfbar; Netzladung wirkt → ihre Freigabe ok.
        assert _bereich(p, "freigabe") == d.OK
        diag2, inv2, *_ = _aufbau(pv=0.0, haus=2.0, soc=60, netzladung=False)
        inv2.entladung_wirkungslos = True
        p2 = await _laufen(diag2)
        assert _bereich(p2, "freigabe") == d.TEILWEISE

    @pytest.mark.asyncio
    async def test_abbruch_durch_ha_gibt_frei_und_bricht_weiter_ab(self):
        diag, inv, anlage, data, uhr, gemeldet = _aufbau(pv=0.0, haus=2.0, soc=60)
        diag.async_start()
        for _ in range(3000):
            await asyncio.sleep(0)
            if inv.modus == "entladung":
                break
        assert inv.modus == "entladung"
        diag._task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await diag._task
        await diag._abschluss_task
        p = diag._letztes
        assert p["ergebnis"] == d.ABGEBROCHEN
        assert "Home Assistant" in p["abbruch_grund"]
        assert data["executor"].freigaben == 1
        assert inv.modus == "auto"
        assert p["modus_wiederhergestellt"] is False
        assert data["select"]._attr_current_option == MODE_AUS

    @pytest.mark.asyncio
    async def test_stilllegen_nach_abbruch_wirft_nicht(self):
        diag, inv, anlage, data, uhr, _ = _aufbau(pv=0.0, haus=2.0, soc=60)
        diag.async_start()
        for _ in range(500):
            await asyncio.sleep(0)
            if data.get("diagnose_laeuft"):
                break
        diag._task.cancel()
        await asyncio.sleep(0)
        data["stopping"] = True
        await diag.async_stilllegen(5.0)   # darf nicht CancelledError werfen
        assert data["executor"].freigaben == 1

    @pytest.mark.asyncio
    async def test_stilllegen_haengt_markiert_fremdeingriff(self):
        diag, inv, anlage, data, uhr, _ = _aufbau(pv=0.0, haus=2.0, soc=60)
        haengt = asyncio.Event()

        async def ewig(*_a):
            await haengt.wait()
            return True

        inv.async_set_charge_limit = ewig
        diag.async_start()
        for _ in range(3000):
            await asyncio.sleep(0)
            if diag._schritt and diag._schritt["bereich"] == "ladelimit" and diag._schritt.get("befehl"):
                break
        await diag.async_stilllegen(0.05)
        assert data["executor"].fremdeingriff >= 1
        assert data["guard_lauf_offen"] is True
        haengt.set()
        await diag._task

    @pytest.mark.asyncio
    async def test_neustart_informiert_executor_und_schaltet_aus(self):
        diag, inv, anlage, data, *_ = _aufbau(pv=0.0, haus=2.0, soc=60, modus=MODE_EIN)
        diag._store = FakeStore({"bereiche": {}, "letztes": None,
                                 "laeuft_seit": "2026-11-05T18:00:00+01:00"})
        await diag.async_load()
        assert data["executor"].fremdeingriff == 1
        assert data["select"]._attr_current_option == MODE_AUS


def test_befunde_treibertext_ganz_ausnahme_nur_typ():
    p = {"schritte": [
        {"bereich": "ladelimit", "ergebnis": d.FEHLER, "befehl_ok": False,
         "fehler": "number.set_value: ServiceNotFound"},
        {"bereich": "netzladung", "ergebnis": d.FEHLER, "befehl_ok": False,
         "fehler": "ConnectionError: 10.0.0.5:502", "ausnahme": "ConnectionError"},
    ]}
    arten = [b["art"] for b in d.befunde(p)]
    assert arten == ["abgelehnt (number.set_value: ServiceNotFound)", "abgelehnt (ConnectionError)"]


class TestNeuePruefungen:
    def test_laden_bei(self):
        p = d.pruef_laden_bei(1.0)
        assert p({"batt": 0.9})
        assert not p({"batt": 0.0})     # Limit 0 steht noch
        assert not p({"batt": 2.0})

    def test_entladen_ueber(self):
        p = d.pruef_entladen_ueber(0.75, 2.0)
        assert p({"batt": -2.0})
        assert not p({"batt": -0.7})    # Deckel steht noch

    def test_einspeisung_braucht_entladung(self):
        p = d.pruef_einspeisung_steigt(0.0, 5.0, 0.5)
        assert not p({"netz": 4.0, "batt": 0.0})   # nur Ladestopp
        assert p({"netz": 5.0, "batt": -1.0})
