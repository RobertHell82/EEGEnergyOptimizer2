"""Tests für den PeakShare-Provider (API V2).

Geprüft wird die Umformung der Antwort in das eigene Cache-Format: aus den
zwei komplementären Feldern ``deficitKwh``/``surplusKwh`` wird ein Wert mit
Vorzeichen. Ohne HTTP, ohne hass — die Rechnung, nicht der Transport.
"""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.eeg_energy_optimizer import peakshare as ps

START = datetime(2026, 8, 28, 12, 15, tzinfo=timezone.utc)


def _antwort(werte, name="BEG Musterregion", warnings=None):
    """V2-Antwort bauen. ``werte`` ist eine Liste von (defizit, ueberschuss)."""
    return {
        "generatedAt": "2026-08-28T12:00:22.452Z",
        "windowStart": START.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "windowEndExclusive": (START + timedelta(hours=48)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        ),
        "communities": [
            {
                "name": name,
                "xTenant": "CC100283",
                "sourceDays": ["2026-08-21", "2026-08-22"],
                "warnings": warnings or [],
                "intervals": [
                    {
                        "timestamp": (START + timedelta(minutes=15 * i)).strftime(
                            "%Y-%m-%dT%H:%M:%S.000Z"
                        ),
                        "deficitKwh": d,
                        "surplusKwh": u,
                    }
                    for i, (d, u) in enumerate(werte)
                ],
            }
        ],
    }


def _provider(cache=None):
    """Provider ohne ``__init__`` — kein hass, kein Store."""
    p = ps.PeakShareProvider.__new__(ps.PeakShareProvider)
    p._cache = cache
    p._cache_time = START
    p._store = None
    return p


# ---------------------------------------------------------------------------
# Struktur prüfen
# ---------------------------------------------------------------------------


def test_v2_antwort_wird_angenommen():
    assert ps._validate_api_response(_antwort([(3.284, 0), (0, 1.117)])) is True


def test_v1_antwort_wird_abgelehnt():
    """Der alte Endpunkt liefert ``hours``, nicht ``intervals``.

    Wichtig, weil V1 weiterhin erreichbar ist: eine versehentlich dorthin
    gerichtete Abfrage darf den Cache nicht mit Stundenwerten füllen.
    """
    v1 = {
        "communities": [
            {
                "name": "BEG",
                "hours": [{"timestamp": "2026-08-28T12:00:00.000Z", "deficitKwh": 5}],
            }
        ]
    }
    assert ps._validate_api_response(v1) is False


@pytest.mark.parametrize(
    "kaputt",
    [
        None,
        "kein dict",
        {},
        {"communities": "keine Liste"},
        {"communities": [{"intervals": []}]},                    # ohne name
        {"communities": [{"name": "X", "intervals": "keine Liste"}]},
        {"communities": [{"name": "X", "intervals": [{"deficitKwh": 1}]}]},  # ohne Zeit
        # Weder Defizit noch Überschuss — das ist keine V2-Antwort
        {"communities": [{"name": "X", "intervals": [{"timestamp": "2026-08-28T12:00:00Z"}]}]},
    ],
)
def test_kaputte_antworten_werden_abgelehnt(kaputt):
    assert ps._validate_api_response(kaputt) is False


# ---------------------------------------------------------------------------
# Umformung
# ---------------------------------------------------------------------------


def test_defizit_wird_positiv_ueberschuss_negativ():
    """Die Vorzeichenregel, an der alles Weitere hängt."""
    daten = ps._normalisieren(_antwort([(3.284, 0), (0, 1.117), (0, 0)]))

    salden = [i["saldoKwh"] for i in daten["communities"][0]["intervals"]]
    assert salden == [3.284, -1.117, 0.0]


def test_fehlende_felder_zaehlen_als_null():
    roh = _antwort([(1.0, 0)])
    del roh["communities"][0]["intervals"][0]["surplusKwh"]

    daten = ps._normalisieren(roh)

    assert daten["communities"][0]["intervals"][0]["saldoKwh"] == 1.0


def test_negative_einzelwerte_werden_weggekappt():
    """Ein negatives ``deficitKwh`` wäre ein API-Fehler.

    Ungeprüft würde daraus ein Vorzeichendreher — und damit eine Preisumkehr:
    aus einer Bedarfsstunde würde eine Überschussstunde.
    """
    daten = ps._normalisieren(_antwort([(-5.0, 0), (0, -5.0)]))

    salden = [i["saldoKwh"] for i in daten["communities"][0]["intervals"]]
    assert salden == [0.0, 0.0]


def test_kaputte_zeitstempel_fallen_heraus():
    roh = _antwort([(1.0, 0), (2.0, 0)])
    roh["communities"][0]["intervals"][0]["timestamp"] = "kaputt"

    daten = ps._normalisieren(roh)

    assert [i["saldoKwh"] for i in daten["communities"][0]["intervals"]] == [2.0]


def test_intervalle_kommen_sortiert():
    roh = _antwort([(1.0, 0), (2.0, 0), (3.0, 0)])
    roh["communities"][0]["intervals"].reverse()

    daten = ps._normalisieren(roh)

    assert [i["saldoKwh"] for i in daten["communities"][0]["intervals"]] == [
        1.0,
        2.0,
        3.0,
    ]


def test_warnungen_und_quelltage_bleiben_erhalten():
    daten = ps._normalisieren(
        _antwort([(1.0, 0)], warnings=[ps.WARN_NO_SOURCE])
    )

    gemeinschaft = daten["communities"][0]
    assert gemeinschaft["warnings"] == [ps.WARN_NO_SOURCE]
    assert gemeinschaft["sourceDays"] == ["2026-08-21", "2026-08-22"]
    assert gemeinschaft["xTenant"] == "CC100283"


# ---------------------------------------------------------------------------
# Lesezugriffe
# ---------------------------------------------------------------------------


def test_intervalle_und_warnungen_je_gemeinschaft():
    cache = ps._normalisieren(_antwort([(1.0, 0)], warnings=[ps.WARN_STALE]))
    p = _provider(cache)

    assert len(p.get_intervals("BEG Musterregion")) == 1
    assert p.get_warnings("BEG Musterregion") == [ps.WARN_STALE]
    assert p.get_communities() == ["BEG Musterregion"]


def test_unbekannte_gemeinschaft_und_leerer_cache():
    p = _provider(ps._normalisieren(_antwort([(1.0, 0)])))

    assert p.get_intervals("gibt es nicht") == []
    assert p.get_warnings("gibt es nicht") == []
    assert _provider(None).get_intervals("BEG Musterregion") == []
    assert _provider(None).get_communities() == []


# ---------------------------------------------------------------------------
# Persistenz
# ---------------------------------------------------------------------------


def test_alter_v1_cache_wird_nicht_als_eigener_erkannt():
    """Beim Update liegt noch ein V1-Persistat auf der Platte.

    Es hat nur 24 Stunden und kennt keinen Überschuss — als Grundlage taugt
    es nicht, also wird es verworfen statt umgerechnet.
    """
    v1_persistat = {
        "communities": [
            {
                "name": "BEG",
                "hours": [{"timestamp": "2026-08-28T12:00:00.000Z", "deficitKwh": 5}],
            }
        ]
    }
    assert ps._ist_normalisiert(v1_persistat) is False


def test_eigener_cache_wird_erkannt():
    assert ps._ist_normalisiert(ps._normalisieren(_antwort([(1.0, 0)]))) is True


@pytest.mark.parametrize(
    "kaputt",
    [
        None,
        "kein dict",
        {"communities": "keine Liste"},
        {"communities": [{"intervals": [{"timestamp": "x"}]}]},   # ohne saldoKwh
    ],
)
def test_kaputte_persistate_werden_verworfen(kaputt):
    assert ps._ist_normalisiert(kaputt) is False


# ---------------------------------------------------------------------------
# Laufende Viertelstunde über einen Abruf hinweg retten
# ---------------------------------------------------------------------------


def _cache_ab(start, werte, name="BEG Musterregion"):
    """Normalisierter Cache ab ``start``; ``werte`` sind Salden in kWh."""
    return {
        "communities": [
            {
                "name": name,
                "intervals": [
                    {
                        "timestamp": ps._format_stamp(start + timedelta(minutes=15 * i)),
                        "saldoKwh": w,
                    }
                    for i, w in enumerate(werte)
                ],
            }
        ]
    }


def test_laufende_viertelstunde_bleibt_nach_dem_abruf_erhalten():
    """af4bbda0, 23.09.2026: Abruf um 08:56 liefert ab 09:00.

    Ohne Übernahme fehlte der Slot ab 08:45 — sein Einspeisepreis fiel auf
    den Basistarif, und das LP lud für vier Minuten mit 1,7 kW, um dieselbe
    Energie ab 09:00 teurer zu verkaufen.
    """
    alt = _cache_ab(datetime(2026, 9, 23, 6, 30, tzinfo=timezone.utc), [20.0, 24.4, 16.5])
    neu = _cache_ab(datetime(2026, 9, 23, 7, 0, tzinfo=timezone.utc), [24.4, 16.5])
    now = datetime(2026, 9, 23, 6, 56, tzinfo=timezone.utc)

    ergebnis = ps._laufende_uebernehmen(neu, alt, now)

    werte = ergebnis["communities"][0]["intervals"]
    assert [e["timestamp"] for e in werte] == [
        "2026-09-23T06:45:00.000Z",
        "2026-09-23T07:00:00.000Z",
        "2026-09-23T07:15:00.000Z",
    ]
    assert werte[0]["saldoKwh"] == 24.4
    # Der Fahrplan greift über die Epochen-Viertelstunde zu — genau dort
    # muss der Wert jetzt liegen.
    from custom_components.eeg_energy_optimizer import eeg_price

    slot = datetime(2026, 9, 23, 8, 45, tzinfo=timezone(timedelta(hours=2)))
    assert eeg_price.saldo_je_intervall(werte)[int(slot.timestamp() // 900)] == 24.4


def test_vergangene_viertelstunden_werden_nicht_uebernommen():
    """Nur die laufende — was schon vorbei ist, braucht niemand mehr."""
    alt = _cache_ab(datetime(2026, 9, 23, 6, 0, tzinfo=timezone.utc), [1.0, 2.0, 3.0, 4.0])
    neu = _cache_ab(datetime(2026, 9, 23, 7, 0, tzinfo=timezone.utc), [5.0])
    now = datetime(2026, 9, 23, 6, 50, tzinfo=timezone.utc)

    werte = ps._laufende_uebernehmen(neu, alt, now)["communities"][0]["intervals"]

    assert [e["saldoKwh"] for e in werte] == [4.0, 5.0]


def test_neue_antwort_hat_vorrang():
    """Deckt die neue Antwort die laufende Viertelstunde selbst, gilt sie."""
    alt = _cache_ab(datetime(2026, 9, 23, 6, 45, tzinfo=timezone.utc), [9.9, 9.9])
    neu = _cache_ab(datetime(2026, 9, 23, 6, 45, tzinfo=timezone.utc), [1.0, 2.0])
    now = datetime(2026, 9, 23, 6, 50, tzinfo=timezone.utc)

    werte = ps._laufende_uebernehmen(neu, alt, now)["communities"][0]["intervals"]

    assert [e["saldoKwh"] for e in werte] == [1.0, 2.0]


def test_ohne_alten_cache_oder_fremde_gemeinschaft_bleibt_alles_wie_es_ist():
    neu = _cache_ab(datetime(2026, 9, 23, 7, 0, tzinfo=timezone.utc), [5.0])
    now = datetime(2026, 9, 23, 6, 56, tzinfo=timezone.utc)
    assert ps._laufende_uebernehmen(neu, None, now) is neu

    alt = _cache_ab(datetime(2026, 9, 23, 6, 45, tzinfo=timezone.utc), [7.0], name="Andere")
    werte = ps._laufende_uebernehmen(neu, alt, now)["communities"][0]["intervals"]
    assert [e["saldoKwh"] for e in werte] == [5.0]
