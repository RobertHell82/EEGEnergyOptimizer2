"""Tests für den HiGHS-Adapter unter Haralds opt().

Der wichtigste Test ist `test_gleiche_loesung_wie_glpk`: er lädt opt_highs
zweimal — einmal wie ausgeliefert (HiGHS), einmal mit auf optlang
zurückgedrehtem Import — und vergleicht die Fahrpläne Spalte für Spalte.
Damit ist belegt, dass der Solver-Wechsel die Ergebnisse nicht verändert,
Dual-Werte inklusive. Der Test überspringt sich, wo optlang fehlt (auf
Alpine/musl ist es nicht installierbar — genau der Grund für den Adapter).

Die chamo-Module werden absichtlich flach über den Ordnerpfad geladen, nicht
über das Integrations-Package: so hängt der Test nicht an den
Home-Assistant-Stubs.
"""

import pathlib
import sys

import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")
pytest.importorskip("highspy")

CHAMO_DIR = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "eeg_energy_optimizer"
    / "chamo"
)
if str(CHAMO_DIR) not in sys.path:
    sys.path.insert(0, str(CHAMO_DIR))

import config_dummy  # noqa: E402
import highs_adapter  # noqa: E402
import opt_highs  # noqa: E402

TZ = "Europe/Vienna"
START = pd.Timestamp("2026-08-24 05:00", tz=TZ)
SLOTS = 36 * 4  # 36 Stunden im 15-Minuten-Raster
TIME_RES = 900
P2E = TIME_RES / 3600


def _index() -> pd.DatetimeIndex:
    return pd.date_range(START, periods=SLOTS, freq="15min")


def _pv() -> pd.Series:
    """PV-Tagesgang: Sinus-Halbwelle 06:00–20:00, Spitze 8 kW."""
    index = _index()
    hours = index.hour + index.minute / 60
    return pd.Series(8.0 * np.clip(np.sin((hours - 6) / 14 * np.pi), 0, None) ** 1.3, index=index)


def _load() -> pd.Series:
    """Hausverbrauch: 0,3 kW Grundlast plus Morgen- und Abendspitze."""
    index = _index()
    hours = index.hour + index.minute / 60
    morning = 1.4 * np.exp(-(((hours - 7.5) / 1.2) ** 2))
    evening = 2.2 * np.exp(-(((hours - 19.0) / 1.6) ** 2))
    return pd.Series(0.3 + morning + evening, index=index)


class _Forecast:
    def __init__(self) -> None:
        self._pv = _pv()

    def production(self, start_time):
        return self._pv.loc[start_time:]

    def min_production(self, start_time):
        # Worst Case: 60 % des Erwartungswerts, entspricht grob Solcast p10
        return self.production(start_time) * 0.6


class _Config(config_dummy.Config):
    """Synthetische Anlage: 12,5 kWh Speicher, 8 kWp, 10 kW Wechselrichter."""

    battery_capacity = 12.5
    battery_free = 8.0
    battery_power_limit = 5.0
    ac_limit = 10.0
    max_blackout_reserve = 0.0  # Blackout-Vorsorge aus

    def __init__(self) -> None:
        super().__init__(time_res=TIME_RES)
        self.forecast = _Forecast()
        self._consumption = _load()

    def consumption(self, start_time):
        return self._consumption.loc[start_time:]


def _load_with_optlang():
    """Lädt opt_highs mit auf optlang/GLPK zurückgedrehtem Import."""
    import types

    source = (CHAMO_DIR / "opt_highs.py").read_text(encoding="utf-8")
    adapter_import = (
        "try:\n\tfrom .highs_adapter import Constraint, Model, Objective, Variable\n"
        "except ImportError:  # Skriptbetrieb ohne Package-Kontext\n"
        "\tfrom highs_adapter import Constraint, Model, Objective, Variable"
    )
    assert adapter_import in source, (
        "Der Adapter-Import in opt_highs.py sieht anders aus als erwartet — "
        "dieser Test muss mitgezogen werden."
    )
    source = source.replace(
        adapter_import,
        "from optlang.glpk_interface import Constraint, Model, Objective, Variable",
    )
    module = types.ModuleType("opt_glpk")
    exec(compile(source, "opt_glpk", "exec"), module.__dict__)  # noqa: S102
    return module


# ---------------------------------------------------------------------------
# Adapter allein
# ---------------------------------------------------------------------------


def test_kleines_lp_mit_bekannter_loesung():
    """max x + y unter x + 2y <= 4 und x <= 1 → x=1, y=1.5, Schattenpreis 0.5."""
    x = highs_adapter.Variable("x", lb=0, ub=1)
    y = highs_adapter.Variable("y", lb=0)
    kapazitaet = highs_adapter.Constraint(x + 2 * y, ub=4, name="kapazitaet")

    model = highs_adapter.Model()
    model.add(kapazitaet)
    model.objective = highs_adapter.Objective(x + y, direction="max")

    assert model.optimize() == "optimal"
    assert x.primal == pytest.approx(1.0)
    assert y.primal == pytest.approx(1.5)
    assert model.objective.value == pytest.approx(2.5)
    assert kapazitaet.dual == pytest.approx(0.5)


def test_konstante_im_ausdruck_wandert_in_die_schranken():
    """x + 3 >= 5 muss zu x >= 2 werden."""
    x = highs_adapter.Variable("x", lb=0)
    model = highs_adapter.Model()
    model.add(highs_adapter.Constraint(x + 3, lb=5))
    model.objective = highs_adapter.Objective(x, direction="min")

    assert model.optimize() == "optimal"
    assert x.primal == pytest.approx(2.0)


def test_unloesbares_modell_meldet_infeasible():
    x = highs_adapter.Variable("x", lb=0, ub=1)
    model = highs_adapter.Model()
    model.add(highs_adapter.Constraint(x, lb=2))
    model.objective = highs_adapter.Objective(x, direction="max")

    assert model.optimize() == "infeasible"
    assert x.primal is None


def test_nan_schranke_wird_abgefangen():
    with pytest.raises(ValueError, match="NaN"):
        highs_adapter.Variable("kaputt", lb=float("nan"))


# ---------------------------------------------------------------------------
# Adapter unter opt()
# ---------------------------------------------------------------------------


def test_fahrplan_ist_energetisch_konsistent():
    """Der Batterieverlauf muss zur geplanten Batterieleistung passen."""
    config = _Config()
    table = opt_highs.opt(config, START)

    assert len(table) == SLOTS
    assert not table[["grid_p", "battery_p", "battery"]].isna().to_numpy().any()

    # battery = freie Kapazität; sie wächst genau um die entladene Energie
    erwartet = config.battery_free + (table.battery_p * P2E).cumsum()
    assert np.allclose(table.battery.astype(float), erwartet, atol=1e-6)

    # Grenzen der Batterie eingehalten
    assert (table.battery.astype(float) <= table.battery_ub.astype(float) + 1e-6).all()
    assert (table.battery.astype(float) <= config.battery_capacity + 1e-6).all()
    assert (table.battery_p.abs() <= config.battery_power_limit + 1e-6).all()

    # Kein Netzbezug zum Laden, solange no_grid_charging gilt
    assert config.no_grid_charging
    assert (table.grid_p.astype(float) >= -1e-6).all()


def test_gleiche_loesung_wie_glpk():
    """Fahrplan über HiGHS und über GLPK müssen identisch sein."""
    pytest.importorskip(
        "optlang", reason="optlang/GLPK ist auf musl-Systemen nicht installierbar"
    )

    ueber_glpk = _load_with_optlang().opt(_Config(), START)
    ueber_highs = opt_highs.opt(_Config(), START)

    assert list(ueber_highs.columns) == list(ueber_glpk.columns)
    for column in ueber_glpk.columns:
        np.testing.assert_allclose(
            ueber_highs[column].astype(float),
            ueber_glpk[column].astype(float),
            atol=1e-6,
            err_msg=f"Spalte '{column}' weicht zwischen HiGHS und GLPK ab",
        )
