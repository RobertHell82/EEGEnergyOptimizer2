"""Messkorrektur der PV-Prognose (messkorrektur.py) — rein, ohne hass.

Anlass: Hochnebel im November. Grünbach 04.11.2026 lag den ganzen Tag bei
35–40 % der eigenen Prognose, Traun 03.11.2026 bei 1,8 statt 13,0 kWh.
"""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.eeg_energy_optimizer import messkorrektur as mk

JETZT = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)


def _proben(minuten, gemessen, prognose, abgeregelt=False):
    puffer = []
    for m in range(minuten, -1, -1):
        mk.probe_ablegen(puffer, JETZT - timedelta(minutes=m), gemessen, prognose, abgeregelt)
    return puffer


def test_nebel_senkt_die_naechsten_stunden_abklingend():
    """Grünbach 04.11.2026, 10:00–11:00: gemessen ~1,7 kW, Prognose ~4,6 kW."""
    puffer = _proben(45, 1.7, 4.6)
    wert = mk.faktor(puffer, JETZT)
    assert wert == pytest.approx(1.7 / 4.6, abs=0.001)

    stempel = [JETZT + timedelta(minutes=15 * k) for k in range(20)]
    reihe = [4.6] * 20
    neu = mk.anwenden(reihe, stempel, JETZT, wert)
    assert neu[0] == 4.6, "der erste Stützpunkt ist schon die Messung"
    assert neu[1] == pytest.approx(4.6 * (1 + (wert - 1) * (1 - 0.25 / 3)), abs=0.001)
    assert neu[1] < neu[4] < neu[8] < 4.6
    assert neu[12:] == reihe[12:], "nach drei Stunden gilt die Prognose allein"


def test_zu_kurz_beobachtet_korrigiert_nicht():
    """Eine Wolke von zehn Minuten ist noch kein Wetter."""
    assert mk.faktor(_proben(10, 1.0, 4.0), JETZT) is None


def test_morgens_und_abends_kein_faktor():
    assert mk.faktor(_proben(45, 0.05, 0.2), JETZT) is None


def test_abgeregelte_proben_zaehlen_nur_nach_oben():
    """Volle Batterie an der Einspeisegrenze: Die Messung liegt unter dem,
    was die Sonne hergäbe — das ist kein Nebel."""
    assert mk.faktor(_proben(45, 2.0, 5.0, abgeregelt=True), JETZT) is None
    assert mk.faktor(_proben(45, 6.0, 5.0, abgeregelt=True), JETZT) == pytest.approx(1.2)


def test_faktor_ist_begrenzt():
    assert mk.faktor(_proben(45, 0.0, 3.0), JETZT) == mk.FAKTOR_MIN
    assert mk.faktor(_proben(45, 9.0, 3.0), JETZT) == mk.FAKTOR_MAX


def test_nach_oben_hoechstens_bis_zur_ac_grenze():
    stempel = [JETZT + timedelta(minutes=15 * k) for k in range(4)]
    neu = mk.anwenden([5.0] * 4, stempel, JETZT, 2.0, obergrenze_kw=8.0)
    assert max(neu[1:]) == 8.0


def test_alte_proben_fallen_aus_dem_fenster():
    puffer = _proben(45, 1.0, 4.0)
    spaeter = JETZT + timedelta(seconds=mk.FENSTER_S + 60)
    mk.probe_ablegen(puffer, spaeter, 4.0, 4.0, False)
    assert len(puffer) == 1
    assert mk.faktor(puffer, spaeter) is None


def test_im_fahrplan_sinkt_erzeugung_und_p10(monkeypatch):
    """Über ``schedule._messkorrektur_anwenden``: 45 Planläufe mit 1,7 kW
    gemessen gegen 4,6 kW Prognose. Danach rechnet der Plan die nächsten
    Stunden mit weniger Sonne, der p10-Pfad bleibt darunter, und die Quelle
    nennt den Faktor."""
    from custom_components.eeg_energy_optimizer import schedule as sched

    monkeypatch.setattr(sched, "compute_grid_export_kw", lambda hass, config: 0.0)
    data: dict = {}
    stempel = [JETZT + timedelta(minutes=15 * k) for k in range(20)]
    for m in range(45, -1, -1):
        jetzt = JETZT - timedelta(minutes=m)
        erzeugung, p10, quelle = sched._messkorrektur_anwenden(
            None, {}, data, jetzt, stempel, [4.6] * 20, [3.0] * 20,
            "solcast_solar (detailedForecast)", 1.7, 4.6, 40.0, 12.0, None,
        )
    assert erzeugung[1] < 2.0 and erzeugung[-1] == 4.6
    assert all(p <= e for p, e in zip(p10, erzeugung))
    assert quelle == "solcast_solar (detailedForecast); Messkorrektur ×0,37"


def test_im_fahrplan_ohne_abweichung_unveraendert(monkeypatch):
    from custom_components.eeg_energy_optimizer import schedule as sched

    monkeypatch.setattr(sched, "compute_grid_export_kw", lambda hass, config: 0.0)
    data: dict = {}
    stempel = [JETZT + timedelta(minutes=15 * k) for k in range(8)]
    for m in range(45, -1, -1):
        erzeugung, p10, quelle = sched._messkorrektur_anwenden(
            None, {}, data, JETZT - timedelta(minutes=m), stempel, [4.6] * 8, None,
            "eigen", 4.5, 4.6, 40.0, 12.0, None,
        )
    assert erzeugung == [4.6] * 8 and p10 is None and quelle == "eigen"
