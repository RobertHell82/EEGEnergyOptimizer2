"""Der Heizstab als bewertete Senke im Fahrplanmodell.

Kern der Sache: Ohne Wärmewert ist ``discard`` dem Modell nichts wert — es
regelt ab, wenn es muss, und der Heizstab bekommt den Rest geschenkt. Mit
Wärmewert wird daraus eine echte Alternative zur Einspeisung, und eine
Abendentladung rechnet sich nur noch, wenn sie mehr bringt als die Wärme,
die morgen aus derselben Kilowattstunde würde.

Der wichtigste Test ist ``test_ohne_heizstab_identisch``: Die Erweiterung
darf bei den Anlagen ohne Heizstab — also fast allen — nichts verschieben.

Die chamo-Module werden wie in ``test_chamo_highs_adapter.py`` flach über den
Ordnerpfad geladen, damit die Tests nicht an den HA-Stubs hängen.
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
import opt_highs  # noqa: E402

TZ = "Europe/Vienna"
START = pd.Timestamp("2026-08-24 05:00", tz=TZ)
SLOTS = 36 * 4
TIME_RES = 900
P2E = TIME_RES / 3600
AC_EFF = 0.95


def _index() -> pd.DatetimeIndex:
    return pd.date_range(START, periods=SLOTS, freq="15min")


def _pv() -> pd.Series:
    index = _index()
    hours = index.hour + index.minute / 60
    return pd.Series(8.0 * np.clip(np.sin((hours - 6) / 14 * np.pi), 0, None) ** 1.3, index=index)


def _load() -> pd.Series:
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
        return self.production(start_time) * 0.6


class _Config(config_dummy.Config):
    """Synthetische Anlage, dieselbe wie im Adapter-Test.

    Die Einspeisegrenze liegt bewusst niedrig: Nur dann gibt es Überschuss,
    den weder Batterie noch Netz aufnehmen — die Lage, für die der Heizstab
    gedacht ist.
    """

    battery_capacity = 12.5
    battery_free = 8.0
    battery_power_limit = 5.0
    ac_limit = 10.0
    max_blackout_reserve = 0.0

    def __init__(self, *, max_kw=0.0, waermewert=0.0, budget_kwh=0.0, feedin_limit=4.0) -> None:
        super().__init__(time_res=TIME_RES)
        self.forecast = _Forecast()
        self._consumption = _load()
        self._feedin_limit = feedin_limit
        self.heizstab_max_kw = max_kw
        self.heizstab_waermewert = waermewert
        self.heizstab_budget_kwh = budget_kwh

    def consumption(self, start_time):
        return self._consumption.loc[start_time:]

    def feedin_limit(self, start_time):
        return self._feedin_limit


def _heizstab(max_kw=6.0, waermewert=0.18, budget_kwh=20.0, **kw):
    return _Config(max_kw=max_kw, waermewert=waermewert, budget_kwh=budget_kwh, **kw)


# ---------------------------------------------------------------------------
# Regression: ohne Heizstab darf sich nichts ändern
# ---------------------------------------------------------------------------


def test_ohne_heizstab_identisch():
    """Eine Anlage ohne Heizstab muss denselben Fahrplan bekommen wie vorher.

    Geprüft wird gegen eine Konfiguration, die die Attribute gar nicht kennt
    — so wie ``config_dummy.Config`` und jeder Aufruf aus dem Skriptbetrieb.
    Beide Wege müssen Spalte für Spalte dasselbe liefern.
    """
    ohne_attribute = _Config()
    del ohne_attribute.heizstab_max_kw
    del ohne_attribute.heizstab_waermewert
    del ohne_attribute.heizstab_budget_kwh

    a = opt_highs.opt(ohne_attribute, START)
    b = opt_highs.opt(_Config(), START)

    for spalte in ("grid_p", "battery_p", "battery", "discard", "ac_price", "bat_price"):
        assert np.allclose(
            a[spalte].astype(float), b[spalte].astype(float), atol=1e-9
        ), f"Spalte {spalte} weicht ab"


@pytest.mark.parametrize("max_kw,waermewert,budget", [
    (0.0, 0.18, 20.0),   # kein Heizstab
    (6.0, 0.0, 20.0),    # kein Wärmewert → Wärme ist dem Nutzer nichts wert
    (6.0, 0.18, 0.0),    # kein Budget → Puffer voll oder Volumen unbekannt
])
def test_unvollstaendige_angaben_planen_keinen_heizstab(max_kw, waermewert, budget):
    """Erst alle drei Angaben zusammen machen den Heizstab planbar.

    Fehlt eine, fällt das Modell auf das bisherige Verhalten zurück — ein
    unbegrenzt bewerteter Heizstab würde sonst jede Entladung blockieren.
    """
    table = opt_highs.opt(
        _Config(max_kw=max_kw, waermewert=waermewert, budget_kwh=budget), START
    )
    assert float(np.asarray(table["heater"], dtype=float).sum()) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Die Senke selbst
# ---------------------------------------------------------------------------


def test_heizstab_bekommt_ueberschuss_und_haelt_die_grenzen():
    table = opt_highs.opt(_heizstab(), START)
    heater = np.asarray(table["heater"], dtype=float)
    discard = np.asarray(table["discard"], dtype=float)

    assert heater.sum() > 0, "Bei knapper Einspeisegrenze muss Wärme geplant werden"
    # Nie mehr als abgeregelt wird …
    assert (heater <= discard + 1e-6).all()
    # … nie mehr als der Heizstab aufnimmt (AC-seitig) …
    assert (heater * AC_EFF <= 6.0 + 1e-6).all()
    # … und über den Horizont nicht mehr, als der Puffer fasst.
    assert heater.sum() * AC_EFF * P2E <= 20.0 + 1e-6


def test_budget_begrenzt_die_gesamtwaerme():
    """Ein halb voller Puffer nimmt weniger auf — und zwar genau so viel."""
    knapp = opt_highs.opt(_heizstab(budget_kwh=5.0), START)
    waerme_kwh = float(np.asarray(knapp["heater"], dtype=float).sum()) * AC_EFF * P2E
    assert waerme_kwh <= 5.0 + 1e-6
    assert waerme_kwh == pytest.approx(5.0, abs=0.2), (
        "Bei diesem Wärmewert lohnt sich das Budget vollständig"
    )


def test_leistungsgrenze_wirkt_je_slot():
    """Mehr als die Nennleistung passt nicht durch — auch nicht kurzzeitig."""
    table = opt_highs.opt(_heizstab(max_kw=2.0), START)
    heater = np.asarray(table["heater"], dtype=float)
    assert (heater * AC_EFF <= 2.0 + 1e-6).all()


# ---------------------------------------------------------------------------
# Der eigentliche Zweck: die Abendentladung
# ---------------------------------------------------------------------------


def test_hoher_waermewert_haelt_energie_zurueck():
    """Der Fall aus Grünbach: Wärme ist 18 ct wert, Einspeisung 9,7 ct.

    Dann darf das Modell die Batterie abends nicht leeren, nur um für den
    halben Preis einzuspeisen — die Energie wird morgen im Puffer mehr wert.
    Geprüft wird am Ladestand am Ende des Horizonts: Mit Wärmewert bleibt
    mehr im Speicher.
    """
    ohne = opt_highs.opt(_Config(), START)
    mit = opt_highs.opt(_heizstab(), START)

    # 'battery' ist die FREIE Kapazität — weniger frei heißt voller.
    frei_ohne = float(ohne["battery"].astype(float).iloc[-1])
    frei_mit = float(mit["battery"].astype(float).iloc[-1])
    assert frei_mit <= frei_ohne + 1e-6

    # Und es wird insgesamt nicht mehr ins Netz gedrückt als vorher.
    export_ohne = float(ohne["grid_p"].astype(float).clip(lower=0).sum())
    export_mit = float(mit["grid_p"].astype(float).clip(lower=0).sum())
    assert export_mit <= export_ohne + 1e-6


def test_voller_puffer_gibt_die_einspeisung_wieder_frei():
    """Sommerfall: Ist der Puffer warm, schrumpft das Budget gegen null —
    dann gilt wieder die alte Rechnung, und es wird eingespeist."""
    voll = opt_highs.opt(_heizstab(budget_kwh=0.0), START)
    ohne = opt_highs.opt(_Config(), START)
    assert np.allclose(
        voll["grid_p"].astype(float), ohne["grid_p"].astype(float), atol=1e-9
    )


def test_niedriger_waermewert_aendert_nichts_am_export():
    """Ist die Wärme weniger wert als die Einspeisung, bleibt es beim Netz."""
    billig = opt_highs.opt(_heizstab(waermewert=0.02), START)
    ohne = opt_highs.opt(_Config(), START)
    export_billig = float(billig["grid_p"].astype(float).clip(lower=0).sum())
    export_ohne = float(ohne["grid_p"].astype(float).clip(lower=0).sum())
    # Die Einspeisung darf nicht zugunsten der billigen Wärme aufgegeben werden.
    assert export_billig == pytest.approx(export_ohne, abs=1e-6)
