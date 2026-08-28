"""Tests für den Spotpreis-Provider (aWATTar) und die Fortschreibung."""

from datetime import datetime, timedelta, timezone

from custom_components.eeg_energy_optimizer.spot import (
    parse_marketdata,
    reihe_fuer,
)


def _eintrag(start: datetime, stunden: float, preis_mwh: float) -> dict:
    ende = start + timedelta(hours=stunden)
    return {
        "start_timestamp": int(start.timestamp() * 1000),
        "end_timestamp": int(ende.timestamp() * 1000),
        "marketprice": preis_mwh,
        "unit": "Eur/MWh",
    }


START = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)


def test_parse_fuellt_viertelstunden_und_rechnet_mwh_um():
    payload = {"data": [_eintrag(START, 1, 82.5)]}
    preise = parse_marketdata(payload)
    # Ein Stundeneintrag füllt vier Viertelstunden, Eur/MWh → €/kWh.
    assert len(preise) == 4
    assert all(p == 0.0825 for p in preise.values())
    basis = int(START.timestamp() // 900)
    assert set(preise) == {basis, basis + 1, basis + 2, basis + 3}


def test_parse_laesst_negative_preise_negativ():
    payload = {"data": [_eintrag(START, 1, -12.0)]}
    preise = parse_marketdata(payload)
    assert all(p == -0.012 for p in preise.values())


def test_parse_uebersteht_muell():
    assert parse_marketdata(None) == {}
    assert parse_marketdata({"data": [{"kaputt": 1}, "unsinn", None]}) == {}


def test_reihe_exakt_ohne_fortschreibung():
    payload = {"data": [_eintrag(START + timedelta(hours=h), 1, 100 + h) for h in range(24)]}
    preise = parse_marketdata(payload)
    stamps = [START + timedelta(hours=h) for h in range(24)]
    werte, fortgeschrieben = reihe_fuer(preise, stamps)
    assert fortgeschrieben == 0
    assert werte[0] == 0.100
    assert werte[23] == 0.123


def test_fehlende_slots_kommen_vom_vortag():
    # Nur Tag 1 hat Preise; Tag 2 wird angefragt → jeder Slot vom Vortag.
    payload = {"data": [_eintrag(START + timedelta(hours=h), 1, 100 + h) for h in range(24)]}
    preise = parse_marketdata(payload)
    stamps = [START + timedelta(hours=24 + h) for h in range(24)]
    werte, fortgeschrieben = reihe_fuer(preise, stamps)
    assert fortgeschrieben == 24
    assert werte[0] == 0.100          # 00:00 von gestern
    assert werte[23] == 0.123         # 23:00 von gestern


def test_fortschreibung_kaskadiert_ueber_mehrere_tage():
    payload = {"data": [_eintrag(START, 1, 80.0)]}
    preise = parse_marketdata(payload)
    # Drei Tage später, gleiche Uhrzeit: t−24h und t−48h fehlen, t−72h trifft.
    werte, fortgeschrieben = reihe_fuer(preise, [START + timedelta(days=3)])
    assert fortgeschrieben == 1
    assert werte == [0.080]


def test_ohne_daten_gilt_die_handeingabe():
    assert reihe_fuer({}, [START]) == (None, 0)


async def test_fetch_fragt_start_und_ende_an(monkeypatch):
    """Der ``end``-Parameter ist Pflicht, kein Feinschliff.

    Mit nur ``start`` liefert die aWATTar-API genau 24 Stunden AB start —
    bei start = jetzt − 48 h also ausschließlich Vergangenheit, nie den
    aktuellen oder morgigen Preis (live gemessen, 27.08.2026). Genau so sah
    der Fehler aus: „Jetzt holen" lief fehlerfrei durch, und der Status
    blieb trotzdem bei „Noch keine Preise geholt", weil preis_jetzt() für
    die laufende Viertelstunde nichts fand.
    """
    import sys
    import types

    from custom_components.eeg_energy_optimizer import spot as spot_mod

    aufgerufen: dict = {}

    class _Antwort:
        status = 200

        async def json(self):
            jetzt = datetime.now(tz=timezone.utc).replace(minute=0, second=0, microsecond=0)
            return {"data": [_eintrag(jetzt, 1, 90.0)]}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Session:
        def get(self, url, params=None, headers=None, timeout=None):
            aufgerufen["params"] = params
            return _Antwort()

    monkeypatch.setitem(
        sys.modules, "aiohttp",
        types.SimpleNamespace(ClientTimeout=lambda total=None: None),
    )
    monkeypatch.setattr(spot_mod, "async_get_clientsession", lambda hass: _Session())

    provider = spot_mod.SpotProvider(hass=None, entry_id="e1")
    await provider.async_fetch(force=True)

    params = aufgerufen["params"]
    assert "start" in params and "end" in params
    jetzt_ms = datetime.now(tz=timezone.utc).timestamp() * 1000
    assert int(params["start"]) < jetzt_ms < int(params["end"]), (
        "das Abruffenster muss JETZT einschließen — sonst kommen nur "
        "Vergangenheitspreise an"
    )
    # Und die geholten Preise decken die laufende Viertelstunde.
    assert provider.preis_jetzt() == 0.090
