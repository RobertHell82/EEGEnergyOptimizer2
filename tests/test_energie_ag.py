"""Tests für den Einspeisetarif der Energie AG (energie_ag.py).

Die Vergütung ist der Referenzmarktwert Photovoltaik § 13 EAG minus einem
Abschlag; veröffentlicht wird der Referenzmarktwert von der E-Control.
Geprüft wird das Zerlegen der Seite mit einem echten Auszug vom 14.09.2026,
die Rechnung gegen die zwölf Monate der Preisgrafik aus dem Preisblatt
(Stand September 2026) und das Verhalten, wenn die Quelle ausfällt.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from custom_components.eeg_energy_optimizer import energie_ag as eag

# Auszug aus e-control.at/referenzmarktwert vom 14.09.2026 — mit den
# Eigenheiten, die das Zerlegen schwer machen: „im" klebt am <strong>, die
# Werte tragen ein &nbsp; vor der Einheit, und zwei andere Technologien
# stehen zwischen Monatsangabe und Photovoltaik.
ECHTE_SEITE = (
    "<p>Dementsprechend lag der aktuelle Referenzmarktwert im<strong>&nbsp;"
    "August 2026</strong>:</p> <ul> "
    "<li>für Wasserkraftanlagen bei <b>15,19&nbsp;</b>Cent/kWh</li> "
    "<li>für Windkraftanlagen bei <b>15,32&nbsp;</b>Cent/kWh</li> "
    "<li>für Photovoltaikanlagen bei <strong>9,42&nbsp;</strong>Cent/kWh</li> "
    "</ul>"
)

# Die Preisgrafik des Preisblatts „Team Sonne Float" (Stand September 2026):
# Monat → (Referenzmarktwert lt. E-Control, Float, Loyal Float) in ct/kWh.
PREISBLATT = {
    (2025, 9): (4.84, 3.34, 3.34),
    (2025, 10): (8.85, 7.35, 7.35),
    (2025, 11): (10.24, 8.74, 8.74),
    (2025, 12): (11.26, 9.76, 9.76),
    (2026, 1): (13.95, 12.45, 12.45),
    (2026, 2): (8.16, 6.66, 6.66),
    (2026, 3): (5.94, 4.44, 4.44),
    # Der Monat, an dem die Mindestvergütung den Unterschied macht.
    (2026, 4): (1.70, 0.20, 2.00),
    (2026, 5): (3.76, 2.26, 2.26),
    (2026, 6): (5.55, 4.05, 4.05),
    (2026, 7): (6.85, 5.35, 5.35),
    (2026, 8): (9.42, 7.92, 7.92),
}


# ---------------------------------------------------------------------------
# Die Seite zerlegen
# ---------------------------------------------------------------------------


def test_echte_seite_wird_gelesen():
    assert eag.parse_referenzmarktwert(ECHTE_SEITE) == (2026, 8, 0.0942)


def test_monat_und_wert_muessen_beide_da_sein():
    """Eine halbe Angabe ist schlimmer als keine — dann bleibt der alte Wert."""
    assert eag.parse_referenzmarktwert(None) is None
    assert eag.parse_referenzmarktwert("") is None
    # Monat ohne Photovoltaik-Zeile
    assert eag.parse_referenzmarktwert(
        "<p>Referenzmarktwert im August 2026:</p><li>für Windkraftanlagen bei 15,32 Cent/kWh</li>"
    ) is None
    # Photovoltaik ohne Monatsangabe
    assert eag.parse_referenzmarktwert(
        "<li>für Photovoltaikanlagen bei 9,42 Cent/kWh</li>"
    ) is None


def test_photovoltaik_wird_hinter_dem_monat_gesucht():
    """Über dem Absatz steht die Verfahrenserklärung, darunter die Historie —
    beide nennen Photovoltaik. Gelten darf nur der Wert danach."""
    seite = (
        "<p>Die Berechnung für Photovoltaikanlagen bei 99,99 Cent/kWh ist in "
        "§ 13 EAG geregelt.</p>"
        + ECHTE_SEITE
        + "<p>Historie: für Photovoltaikanlagen bei 1,11 Cent/kWh</p>"
    )
    assert eag.parse_referenzmarktwert(seite) == (2026, 8, 0.0942)


@pytest.mark.parametrize(
    "name,nummer",
    [("Jänner", 1), ("Januar", 1), ("Feber", 2), ("März", 3), ("Dezember", 12)],
)
def test_oesterreichische_monatsnamen(name, nummer):
    seite = (
        f"<p>Referenzmarktwert im <strong>{name} 2026</strong>:</p>"
        "<li>für Photovoltaikanlagen bei <b>9,42</b> Cent/kWh</li>"
    )
    assert eag.parse_referenzmarktwert(seite) == (2026, nummer, 0.0942)


# ---------------------------------------------------------------------------
# Die Rechnung — gegen das Preisblatt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("monat", sorted(PREISBLATT))
def test_rechnung_trifft_das_preisblatt(monat):
    """Alle zwölf Monate der Preisgrafik, auf den Cent genau."""
    referenzwert, erwartet_float, erwartet_loyal = PREISBLATT[monat]
    rmw = referenzwert / 100.0
    assert eag.tarif_aus_referenzwert(
        rmw, eag.DEFAULT_ABSCHLAG_EUR, eag.VARIANTE_FLOAT
    ) == pytest.approx(erwartet_float / 100.0, abs=1e-9)
    assert eag.tarif_aus_referenzwert(
        rmw, eag.DEFAULT_ABSCHLAG_EUR, eag.VARIANTE_LOYAL
    ) == pytest.approx(erwartet_loyal / 100.0, abs=1e-9)


def test_float_faellt_nie_unter_null():
    """„Der Preis sinkt nie unter 0 Cent/kWh" (Preisblatt)."""
    assert eag.tarif_aus_referenzwert(0.005, 0.015, eag.VARIANTE_FLOAT) == 0.0


def test_loyal_haelt_die_mindestverguetung():
    """„immer mindestens 2 Cent je kWh" — auch bei negativem Referenzwert."""
    assert eag.tarif_aus_referenzwert(-0.01, 0.015, eag.VARIANTE_LOYAL) == 0.02


def test_unbekannte_variante_gilt_als_float():
    """Die Mindestvergütung gibt es nur mit Stromliefervertrag — im Zweifel
    also die Variante ohne sie, statt einen Preis zu versprechen."""
    assert eag.tarif_aus_referenzwert(0.005, 0.015, "unsinn") == 0.0


def test_abschlag_ist_einstellbar_weil_er_wertgesichert_ist():
    assert eag._abschlag(None) == eag.DEFAULT_ABSCHLAG_EUR
    assert eag._abschlag("") == eag.DEFAULT_ABSCHLAG_EUR
    assert eag._abschlag("keine Zahl") == eag.DEFAULT_ABSCHLAG_EUR
    # Eine 0 ist eine Aussage, keine fehlende Angabe.
    assert eag._abschlag(0) == 0.0
    assert eag._abschlag(0.017) == 0.017
    assert eag._abschlag(-1) == 0.0


# ---------------------------------------------------------------------------
# Monatswahl
# ---------------------------------------------------------------------------


def test_der_laufende_monat_fehlt_immer_und_der_vormonat_gilt():
    """Der Wert eines Monats erscheint erst im Folgemonat — der Rückfall auf
    den jüngsten bekannten ist hier der Normalfall, nicht die Ausnahme."""
    werte = {202607: 0.0685, 202608: 0.0942}
    assert eag.wert_fuer(werte, 202609) == (0.0942, 202608)
    assert eag.wert_fuer(werte, 202608) == (0.0942, 202608)
    assert eag.wert_fuer({}, 202609) is None


def test_nur_spaetere_monate_bekannt():
    assert eag.wert_fuer({202608: 0.0942}, 202601) == (0.0942, 202608)


# ---------------------------------------------------------------------------
# Der Anbieter
# ---------------------------------------------------------------------------


def _provider(schaetzer=None):
    p = eag.EnergieAgProvider(object(), "entry1", schaetzer)
    p._store = None
    return p


def test_preis_aus_dem_veroeffentlichten_monat():
    p = _provider()
    p._werte = {202608: 0.0942}
    p._geholt = eag._utcnow()
    with patch.object(eag, "aktueller_schluessel", return_value=202609):
        assert p.preis_fuer(eag.VARIANTE_FLOAT) == pytest.approx(0.0792)
        assert p.preis_fuer(eag.VARIANTE_LOYAL) == pytest.approx(0.0792)
        status = p.status()
    assert status["referenzwert"] == 0.0942
    assert (status["jahr"], status["monat"]) == (2026, 8)
    assert status["float"]["preis"] == pytest.approx(0.0792)


def test_ohne_daten_kein_preis():
    p = _provider()
    assert p.preis_fuer(eag.VARIANTE_FLOAT) is None
    assert p.status()["float"] is None


def test_zu_alter_wert_gilt_nicht_mehr():
    """Nach Wochen ohne Erfolg lieber die Handeingabe als ein Fantasiewert."""
    p = _provider()
    p._werte = {202608: 0.0942}
    p._geholt = eag._utcnow() - timedelta(seconds=eag.CACHE_MAX_SECONDS + 60)
    assert p.preis_fuer(eag.VARIANTE_FLOAT) is None


def test_schaetzung_kommt_vom_oemag_schaetzer():
    """Der Rohwert der OeMAG-Hochrechnung IST der Referenzmarktwert — ein
    zweiter Abruf derselben Daten wäre reine Last (beide APIs drosseln)."""
    class _Schaetzer:
        roh = 0.0880

    p = _provider(_Schaetzer())
    assert p.preis_geschaetzt(eag.VARIANTE_FLOAT) == pytest.approx(0.0730)
    assert p.status()["schaetzung"]["float"] == pytest.approx(0.0730)


def test_ohne_schaetzer_keine_schaetzung():
    p = _provider()
    assert p.preis_geschaetzt(eag.VARIANTE_FLOAT) is None
    assert p.status()["schaetzung"] is None

    class _Leer:
        roh = None

    p2 = _provider(_Leer())
    assert p2.preis_geschaetzt(eag.VARIANTE_FLOAT) is None


def test_vormonat_bekannt_steuert_die_frist():
    """Solange der fällige Monat fehlt, wird stündlich statt zweimal täglich
    nachgesehen — er erscheint irgendwann zwischen dem 2. und dem 9."""
    p = _provider()
    with patch.object(eag, "aktueller_schluessel", return_value=202609):
        assert p._vormonat_bekannt() is False
        p._werte = {202608: 0.0942}
        assert p._vormonat_bekannt() is True
    # Jahreswechsel: der Vormonat des Jänner ist der Dezember davor.
    p._werte = {202512: 0.1126}
    with patch.object(eag, "aktueller_schluessel", return_value=202601):
        assert p._vormonat_bekannt() is True


def test_alter_wert_ueberlebt_einen_ausfall():
    """Ein Netzfehler darf den Fahrplan nicht anhalten."""
    p = _provider()
    p._werte = {202608: 0.0942}
    p._geholt = eag._utcnow()
    # async_fetch bricht ohne HA-Session früh ab; der Wert bleibt stehen.
    with patch.object(eag, "aktueller_schluessel", return_value=202609):
        assert p.preis_fuer(eag.VARIANTE_FLOAT) == pytest.approx(0.0792)
