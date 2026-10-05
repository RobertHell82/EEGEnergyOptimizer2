"""„Vormittags bevorzugt netzdienlich" (schedule_netzdienlich).

Anlass: Wunsch eines Betreibers, 05.10.2026 — der Fahrplan lud morgens früh
und langsam, gewünscht war: vormittags ins Netz, mittags mit voller Leistung
in die Batterie, soweit sich das ausgeht.

Was diese Tests festhalten:

* ausgeschaltet ändert sich nichts (``test_aus_ist_identisch``),
* den Bonus bekommt nur PV-Strom, nie die Batterie — ohne die Einspeisegrenze
  auf den Überschuss entlud das LP vormittags ins Netz (Arbitrage),
* kein zusätzlicher Netzbezug, und mit Ladeziel bleibt der Abend gleich voll,
* der Bonus ist Steuerung, kein Geld: die Gewinnbewertung sieht ihn nicht.
"""

from dataclasses import replace

import pytest

from custom_components.eeg_energy_optimizer import schedule as sched

from tests.test_ladeziel import _inputs_gruenbach

TAG = "2026-10-05"  # der Vormittag nach dem Grünbach-Plan vom 04.10., 20:00


def _an(inputs: sched.ScheduleInputs, bonus: float = 0.05, bis: int = 11) -> sched.ScheduleInputs:
    return replace(inputs, netzdienlich_bis_stunde=bis, netzdienlich_bonus=bonus)


def _vormittag_mit_ueberschuss(slots: list[dict], bis: int = 11) -> list[dict]:
    return [
        s for s in slots
        if s["t"].startswith(TAG) and int(s["t"][11:13]) < bis
        and s["PV"] * sched.HAConfig.ac_efficiency > s["consumption"]
    ]


def _netzbezug_kwh(slots: list[dict]) -> float:
    return sum(max(-s["grid_p"], 0.0) for s in slots) * 0.25


# ---------------------------------------------------------------------------
# Einstellung lesen
# ---------------------------------------------------------------------------


def test_vorgabe_aus():
    assert sched._netzdienlich({}) == (None, 0.0)
    assert sched._netzdienlich({sched.CONF_SCHEDULE_NETZDIENLICH: False}) == (None, 0.0)


def test_eingeschaltet_mit_vorgaben():
    assert sched._netzdienlich({sched.CONF_SCHEDULE_NETZDIENLICH: True}) == (
        sched.DEFAULT_NETZDIENLICH_BIS_STUNDE, sched.DEFAULT_NETZDIENLICH_BONUS
    )


@pytest.mark.parametrize("bis,bonus,erwartet", [
    ("10:00", 0.03, (10, 0.03)),
    ("12", 0.01, (12, 0.01)),
    ("00:00", 0.05, (1, 0.05)),       # „bis 0 Uhr" wäre nie
    ("11:00", 0.5, (11, sched.MAX_NETZDIENLICH_BONUS)),
    ("11:00", "abc", (11, sched.DEFAULT_NETZDIENLICH_BONUS)),
    ("11:00", "", (11, sched.DEFAULT_NETZDIENLICH_BONUS)),
    ("11:00", 0, (None, 0.0)),        # geleertes Feld → ohne Wirkung
    ("11:00", -0.02, (None, 0.0)),
])
def test_eingaben_geklemmt(bis, bonus, erwartet):
    config = {
        sched.CONF_SCHEDULE_NETZDIENLICH: True,
        sched.CONF_SCHEDULE_NETZDIENLICH_BIS: bis,
        sched.CONF_SCHEDULE_NETZDIENLICH_BONUS: bonus,
    }
    assert sched._netzdienlich(config) == erwartet


# ---------------------------------------------------------------------------
# Im Modell
# ---------------------------------------------------------------------------


def test_aus_ist_identisch():
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")
    inputs = _inputs_gruenbach(ladeziel_pct=100.0)
    config = sched.HAConfig(inputs)
    assert config.netzdienlich_slots() is None
    assert config.feedin_limit(inputs.start) == inputs.feedin_limit_kw
    a = sched.solve(inputs)["slots"]
    b = sched.solve(replace(inputs, netzdienlich_bis_stunde=11, netzdienlich_bonus=0.0))["slots"]
    assert [s["battery_p"] for s in a] == [s["battery_p"] for s in b]


def test_bonus_nur_vormittags_und_nur_mit_ueberschuss():
    pytest.importorskip("pandas")
    inputs = _an(_inputs_gruenbach())
    maske = sched.HAConfig(inputs).netzdienlich_slots()
    for stamp, wert, pv, haus in zip(
        inputs.timestamps, maske, inputs.production_kw, inputs.consumption_kw
    ):
        if wert is not None:
            assert stamp.hour < 11
            assert pv * sched.HAConfig.ac_efficiency - haus == pytest.approx(wert)
            assert wert > 0
        elif stamp.hour < 11:
            assert pv * sched.HAConfig.ac_efficiency <= haus
    assert any(m is not None for m in maske)


def test_vormittags_einspeisen_mittags_laden():
    """Der eigentliche Wunsch, am Plan, der ihn ausgelöst hat (Grünbach)."""
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")
    ohne = sched.solve(_inputs_gruenbach(ladeziel_pct=100.0))["slots"]
    mit = sched.solve(_an(_inputs_gruenbach(ladeziel_pct=100.0)))["slots"]

    def laden(slots):
        return sum(max(-s["battery_p"], 0.0) for s in _vormittag_mit_ueberschuss(slots)) * 0.25

    def export(slots):
        return sum(max(s["grid_p"], 0.0) for s in _vormittag_mit_ueberschuss(slots)) * 0.25

    assert laden(ohne) > 2.0
    assert laden(mit) < 0.5
    assert export(mit) > export(ohne) + 2.0
    # Abends trotzdem voll — das Ladeziel bleibt der Anker.
    assert max(s["soc"] for s in mit if s["t"].startswith(TAG)) == pytest.approx(100.0, abs=0.5)
    assert _netzbezug_kwh(mit) <= _netzbezug_kwh(ohne) + 1e-6


def test_batterie_bekommt_den_bonus_nie():
    """Ohne Einspeisegrenze auf den Überschuss entlud das LP vormittags von
    39 auf 20 % ins Netz, um den Bonus mitzunehmen. Das darf nicht wieder
    passieren — auch nicht ohne Ladeziel und mit großem Bonus."""
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")
    for ladeziel in (0.0, 100.0):
        slots = sched.solve(_an(_inputs_gruenbach(ladeziel_pct=ladeziel), bonus=0.2))["slots"]
        for s in _vormittag_mit_ueberschuss(slots):
            assert s["battery_p"] <= 1e-3, (ladeziel, s["t"], s["battery_p"])


def test_kein_netzbezug_an_einem_trueben_tag():
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")
    for faktor in (0.6, 0.35):
        ohne = sched.solve(_inputs_gruenbach(ladeziel_pct=100.0, pv_faktor=faktor))["slots"]
        mit = sched.solve(_an(_inputs_gruenbach(ladeziel_pct=100.0, pv_faktor=faktor)))["slots"]
        assert _netzbezug_kwh(mit) <= _netzbezug_kwh(ohne) + 1e-6


def test_bonus_ist_kein_geld():
    """Die Gewinnbewertung rechnet mit dem echten Tarif — der Bonus ist nur
    eine Steuergröße wie der Gemeinschaftsaufschlag."""
    pytest.importorskip("pandas")
    pytest.importorskip("highspy")
    inputs = _inputs_gruenbach(ladeziel_pct=100.0)
    slots = sched.solve(inputs)["slots"]
    assert sched._basistarif_je_slot(slots, inputs) == sched._basistarif_je_slot(slots, _an(inputs))
    assert sched.bewerte_geldfluesse(slots, inputs) == sched.bewerte_geldfluesse(slots, _an(inputs))
