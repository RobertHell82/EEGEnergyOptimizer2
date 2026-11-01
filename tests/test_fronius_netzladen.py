"""Fronius Gen24: Batterie aus dem Netz laden (Beta).

Geprüft gegen ein simuliertes Model-124-Registerfeld: Ladefenster auf einen
Punkt (InWRte = +x, OutWRte = −x, StorCtl_Mod = 3), Reihenfolge, damit das
Fenster nie leer ist, ChaGriSet mit gesichertem und zurückgeschriebenem
Vorwert, und die Freigabe am Ziel-Ladestand, den der Gen24 fürs Laden
selbst nicht kennt.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.eeg_energy_optimizer.inverter.fronius import (
    FroniusInverter,
    _CHAGRISET_GRID,
    _CHAGRISET_PV,
    _OFFSET_CHAGRISET,
    _OFFSET_CHASTATE,
    _OFFSET_INWRTE,
    _OFFSET_OUTWRTE,
    _OFFSET_STORCTL_MOD,
    _cache_stunde,
)

BASIS = 40070


def _antwort(registers=None, fehler=False):
    res = MagicMock()
    res.isError = MagicMock(return_value=fehler)
    if registers is not None:
        res.registers = registers
    return res


class _Gen24:
    """Model-124-Register als Feld; lehnt Schritte ab, die das Fenster leeren.

    Prozentwerte mit SF −2 (wie jeder bisher gesehene Gen24): 10000 = 100 %.
    """

    def __init__(self, soc=40.0, chagriset=_CHAGRISET_PV):
        self.reg = {o: 0 for o in range(24)}
        self.reg[_OFFSET_INWRTE] = 10000
        self.reg[_OFFSET_OUTWRTE] = 10000
        self.reg[_OFFSET_CHASTATE] = int(soc * 100)
        self.reg[_OFFSET_CHAGRISET] = chagriset
        self.schreibungen: list[tuple[int, int]] = []

    @staticmethod
    def _s(wert):
        return wert - 0x10000 if wert > 0x7FFF else wert

    async def lesen(self, address, count, **_kw):
        o = address - BASIS
        return _antwort([self.reg[o + i] for i in range(count)])

    async def schreiben(self, address, value, **_kw):
        o = address - BASIS
        neu = dict(self.reg)
        neu[o] = value
        if -self._s(neu[_OFFSET_OUTWRTE]) > self._s(neu[_OFFSET_INWRTE]):
            return _antwort(fehler=True)  # „Illegal Data Value"
        self.reg = neu
        self.schreibungen.append((o, value))
        return _antwort()

    def prozent(self, offset):
        return self._s(self.reg[offset]) / 100.0


class _Store:
    def __init__(self, daten=None):
        self._data = dict(daten or {})

    async def async_load(self):
        return dict(self._data)

    async def async_save(self, data):
        self._data = dict(data)


def _inverter(gen24, store_daten=None):
    hass = MagicMock()
    inv = FroniusInverter(hass, {"fronius_modbus_host": "192.168.1.100"})
    client = MagicMock()
    client.connected = True
    client.read_holding_registers = AsyncMock(side_effect=gen24.lesen)
    client.write_register = AsyncMock(side_effect=gen24.schreiben)
    inv._client = client
    inv._model124_base = BASIS
    inv._wchamax = 5000
    inv._wchamax_date = _cache_stunde()
    inv._sf_loaded = True
    inv._state_store._store = _Store(store_daten)
    inv._state_store._data = {}
    inv._state_store._loaded = False
    return inv


@pytest.fixture(autouse=True)
def _ohne_pause():
    with patch(
        "custom_components.eeg_energy_optimizer.inverter.fronius.asyncio.sleep",
        AsyncMock(),
    ):
        yield


def test_treiber_bietet_netzladen_an():
    inv = _inverter(_Gen24())
    assert inv.supports_grid_charge is True


async def test_ladepunkt_aus_automatik():
    """2,5 kW von 5 kW: Fenster [50 %, 50 %], Bezug erlaubt, Vorwert gesichert."""
    g = _Gen24(soc=40.0)
    inv = _inverter(g)
    assert await inv.async_set_grid_charge(2.5, target_soc=80.0) is True
    inv._cancel_keepalive()
    assert g.prozent(_OFFSET_INWRTE) == pytest.approx(50.0)
    assert g.prozent(_OFFSET_OUTWRTE) == pytest.approx(-50.0)
    assert g.reg[_OFFSET_STORCTL_MOD] == 3
    assert g.reg[_OFFSET_CHAGRISET] == _CHAGRISET_GRID
    assert inv._state_store.original_chagriset == _CHAGRISET_PV
    assert inv._active_command == {
        "kind": "grid_charge", "power_kw": 2.5, "target_soc": 80.0,
    }
    # Der Modus kommt zuletzt, wenn alle Raten schon stehen.
    assert g.schreibungen[-1] == (_OFFSET_STORCTL_MOD, 3)


@pytest.mark.parametrize("vorher, nachher", [(1.0, 4.0), (4.0, 1.0)])
async def test_ladepunkt_hoch_und_runter_ohne_leeres_fenster(vorher, nachher):
    """Steigt der Ladepunkt, erst InWRte; sinkt er, erst OutWRte — sonst
    lehnt der Gen24 den Zwischenschritt ab (Grünbach, 23.09.2026, Entladung)."""
    g = _Gen24()
    inv = _inverter(g)
    assert await inv.async_set_grid_charge(vorher, target_soc=90.0)
    assert await inv.async_set_grid_charge(nachher, target_soc=90.0)
    inv._cancel_keepalive()
    assert g.prozent(_OFFSET_INWRTE) == pytest.approx(nachher * 20.0)
    assert g.prozent(_OFFSET_OUTWRTE) == pytest.approx(-nachher * 20.0)


async def test_aus_der_entladung_heraus():
    """Von einem Entladepunkt direkt auf einen Ladepunkt: InWRte zuerst."""
    g = _Gen24()
    inv = _inverter(g)
    assert await inv.async_set_discharge(3.0, target_soc=None)
    assert await inv.async_set_grid_charge(2.0, target_soc=90.0)
    inv._cancel_keepalive()
    assert g.prozent(_OFFSET_INWRTE) == pytest.approx(40.0)
    assert g.prozent(_OFFSET_OUTWRTE) == pytest.approx(-40.0)


async def test_stopp_schreibt_chagriset_zurueck():
    g = _Gen24()
    inv = _inverter(g)
    await inv.async_set_grid_charge(2.0, target_soc=90.0)
    assert await inv.async_stop_forcible() is True
    assert g.reg[_OFFSET_STORCTL_MOD] == 0
    assert g.reg[_OFFSET_CHAGRISET] == _CHAGRISET_PV
    assert inv._state_store.original_chagriset is None
    assert inv._keepalive_task is None


async def test_vorwert_aus_dem_store_nach_neustart():
    """Neustart mitten in der Ladung: Im Register steht schon unsere 1, nur
    der Store kennt den echten Vorwert — zurück geht es auf den."""
    g = _Gen24(chagriset=_CHAGRISET_GRID)
    inv = _inverter(g, store_daten={"chagriset_original": _CHAGRISET_PV})
    await inv.async_set_grid_charge(2.0, target_soc=90.0)
    assert await inv.async_stop_forcible()
    assert g.reg[_OFFSET_CHAGRISET] == _CHAGRISET_PV


async def test_gescheitertes_zuruecksetzen_meldet_false_und_behaelt_vorwert():
    g = _Gen24()
    inv = _inverter(g)
    await inv.async_set_grid_charge(2.0, target_soc=90.0)
    normal = g.schreiben

    async def chagriset_kaputt(address, value, **kw):
        if address - BASIS == _OFFSET_CHAGRISET:
            return _antwort(fehler=True)
        return await normal(address, value, **kw)

    inv._client.write_register = AsyncMock(side_effect=chagriset_kaputt)
    assert await inv.async_stop_forcible() is False
    assert inv._state_store.original_chagriset == _CHAGRISET_PV


async def test_am_ziel_wird_nur_freigegeben():
    """Ladestand schon am Ziel: kein Ladepunkt, ChaGriSet nie angefasst."""
    g = _Gen24(soc=80.5)
    inv = _inverter(g)
    assert await inv.async_set_grid_charge(3.0, target_soc=80.0) is True
    assert g.reg[_OFFSET_STORCTL_MOD] == 0
    assert all(o != _OFFSET_CHAGRISET for o, _ in g.schreibungen)
    assert inv._active_command is None


async def test_ohne_leistung_wird_freigegeben():
    """Die Netzgrenze lässt gerade nichts zu: Freigabe statt Ladepunkt 0,
    der auch das Laden aus PV sperrte."""
    g = _Gen24(soc=40.0)
    inv = _inverter(g)
    assert await inv.async_set_grid_charge(0.0, target_soc=80.0) is True
    assert g.reg[_OFFSET_STORCTL_MOD] == 0
    assert inv._active_command is None


async def test_keepalive_gibt_am_ziel_frei():
    """Der Gen24 kennt kein Ladeziel: Der Keepalive sieht ChaState am Ziel
    und gibt frei — samt ChaGriSet, ohne sich mitten im Stopp abzubrechen."""
    g = _Gen24(soc=70.0)
    inv = _inverter(g)
    await inv.async_set_grid_charge(2.0, target_soc=80.0)
    inv._cancel_keepalive()
    g.reg[_OFFSET_CHASTATE] = 8000  # 80 %

    with patch.object(asyncio, "sleep", AsyncMock()):
        await inv._keepalive_loop()

    assert inv._active_command is None
    assert g.reg[_OFFSET_STORCTL_MOD] == 0
    assert g.reg[_OFFSET_CHAGRISET] == _CHAGRISET_PV


async def test_ohne_ziel_bis_voll():
    g = _Gen24(soc=99.0)
    inv = _inverter(g)
    assert await inv.async_set_grid_charge(2.0, target_soc=None)
    inv._cancel_keepalive()
    assert g.reg[_OFFSET_STORCTL_MOD] == 3


async def test_unbekanntes_chagriset_wird_nicht_ueberschrieben():
    """Ein Wert außer 0/1 (nicht implementiert): kein Vorwert, kein Schreiben."""
    g = _Gen24(chagriset=0xFFFF)
    inv = _inverter(g)
    assert await inv.async_set_grid_charge(2.0, target_soc=90.0) is False
    assert g.reg[_OFFSET_CHAGRISET] == 0xFFFF
    assert g.reg[_OFFSET_STORCTL_MOD] == 0
    assert inv.last_write_error == "ChaGriSet nicht unterstützt"


async def test_transparenz_zeigt_chagriset():
    g = _Gen24()
    inv = _inverter(g)
    zeilen = await inv.async_get_control_values()
    zeile = next(z for z in zeilen if z["role"] == "grid_charge")
    assert zeile["value"] == "nur PV"
