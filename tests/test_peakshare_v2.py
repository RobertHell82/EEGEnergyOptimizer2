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
