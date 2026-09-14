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
    for tag, kwh in _waerme_je_tag(table).items():
        assert kwh <= 20.0 + 1e-6, f"{tag}: {kwh:.2f} kWh über dem Tagesbudget"


def _waerme_je_tag(table):
    """Wärme in kWh, nach Kalendertag getrennt."""
    heater = np.asarray(table["heater"], dtype=float) * AC_EFF * P2E
    tage = {}
    for stempel, wert in zip(table.index, heater):
        tage[stempel.date()] = tage.get(stempel.date(), 0.0) + float(wert)
    return tage


def test_budget_begrenzt_die_waerme_je_tag():
    """Ein halb voller Puffer nimmt weniger auf — und zwar an JEDEM Tag.

    Die Schranke gilt bewusst je Kalendertag: Der Puffer kühlt über Nacht
    aus und wird leergezapft, am nächsten Tag ist wieder Platz. Mit einer
    einzigen Schranke über den ganzen Horizont sparte das Modell die
    Kapazität für den sonnigsten Tag auf und ließ die Wärme heute liegen,
    obwohl sie bis dahin ohnehin verloren geht.
    """
    knapp = opt_highs.opt(_heizstab(budget_kwh=5.0), START)
    je_tag = _waerme_je_tag(knapp)
    assert je_tag, "der Plan reicht über mindestens einen Tag"
    for tag, kwh in je_tag.items():
        assert kwh <= 5.0 + 1e-6, f"{tag}: {kwh:.2f} kWh über dem Tagesbudget"
    # Am ersten Tag wird das Budget auch wirklich genutzt, statt auf einen
    # späteren Tag zu warten.
    erster = min(je_tag)
    assert je_tag[erster] == pytest.approx(5.0, abs=0.2), (
        "Bei diesem Wärmewert lohnt sich das Tagesbudget sofort"
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


# ---------------------------------------------------------------------------
# Die Batterie speist den Heizstab nie
#
# Gruenbach, 14.09.2026: Der Plan sah 3,38 kW Waerme UND 2,02 kW
# Batterieentladung im selben Slot vor — der Ladestand fiel darin von 82,7 %
# auf 51,5 %, waehrend der Heizstab lief. Formal ging die PV in den Puffer
# und die Batterie deckte das Haus; netto wanderte gespeicherte Energie in
# die Waerme. Das Modell rechnete sich das schoen, weil es die gespeicherte
# kWh nur mit der Einspeiseverguetung bewertete (8,81 ct gegen 12 ct
# Waermewert) — bei zu hoher PV-Prognose kostet dieselbe kWh abends aber den
# vollen Bezugspreis (20,13 ct). Betreiberentscheid: niemals.
#
# Das Szenario ist der Anlage nachgebaut (Batterie fast voll, PV-Rest heute
# klein, morgen viel, kleine Grundlast, Waermewert ueber der Einspeisung)
# und wurde OHNE die Schranke gegengeprueft: dort entlaedt die Batterie beim
# Heizen mit 1,89 kW, und die Waerme liegt 1,75 kW ueber dem Ueberschuss.
# ---------------------------------------------------------------------------

GB_START = pd.Timestamp("2026-09-14 15:00", tz=TZ)


def _gb_index():
    return pd.date_range(GB_START, periods=SLOTS, freq="15min")


def _gb_pv():
    idx = _gb_index()
    h = idx.hour + idx.minute / 60
    return pd.Series(9.0 * np.clip(np.sin((h - 6) / 14 * np.pi), 0, None) ** 1.3, index=idx)


def _gb_load():
    idx = _gb_index()
    h = idx.hour + idx.minute / 60
    return pd.Series(0.3 + 1.5 * np.exp(-(((h - 19.0) / 1.6) ** 2)), index=idx)


class _GruenbachConfig(config_dummy.Config):
    battery_capacity = 10.0
    battery_power_limit = 5.0
    ac_limit = 12.0
    max_blackout_reserve = 0.0
    battery_free = 1.7          # 83 % voll
    heizstab_max_kw = 6.0
    heizstab_waermewert = 0.12  # ueber der Einspeisung, unter dem Bezug
    heizstab_budget_kwh = 20.0

    def __init__(self) -> None:
        super().__init__(time_res=TIME_RES)
        self._pv = _gb_pv()
        self._load = _gb_load()
        self.forecast = type("F", (), {
            "production": lambda s, t0: self._pv.loc[t0:],
            "min_production": lambda s, t0: self._pv.loc[t0:] * 0.6,
        })()

    def consumption(self, start_time):
        return self._load.loc[start_time:]

    def feedin_limit(self, start_time):
        return 4.0

    def feedin_price(self, start_time):
        return 0.0888

    def consumption_price(self, start_time):
        return 0.2013   # 12 ct Arbeitspreis + 8,13 ct Netz Linz


def _gb_ueberschuss(table):
    pv = _gb_pv().reindex(table.index).to_numpy(dtype=float)
    last = _gb_load().reindex(table.index).to_numpy(dtype=float)
    return np.clip(pv - last / AC_EFF, 0.0, None)


def test_keine_batterieentladung_waehrend_geheizt_wird():
    table = opt_highs.opt(_GruenbachConfig(), GB_START)
    heater = np.asarray(table["heater"], dtype=float)
    battery_p = np.asarray(table["battery_p"], dtype=float)   # positiv = entladen

    assert heater.sum() > 0, "Der Test braucht Slots mit geplanter Waerme"
    entladung = battery_p[heater > 1e-6]
    assert (entladung <= 1e-6).all(), (
        f"Waehrend geheizt wird, entlaedt die Batterie mit bis zu {entladung.max():.2f} kW"
    )


def test_waerme_nie_ueber_dem_ueberschuss_der_pv():
    """Die Schranke selbst: PV minus Hausverbrauch ist die Obergrenze."""
    table = opt_highs.opt(_GruenbachConfig(), GB_START)
    heater = np.asarray(table["heater"], dtype=float)
    zu_viel = heater - _gb_ueberschuss(table)
    assert (zu_viel <= 1e-6).all(), (
        f"Waerme liegt bis zu {zu_viel.max():.2f} kW ueber dem PV-Ueberschuss — "
        "die Differenz kommt aus der Batterie"
    )


def test_ohne_pv_keine_waerme():
    """Nachts ist jede Waerme zwangslaeufig Batterie- oder Netzstrom."""
    table = opt_highs.opt(_GruenbachConfig(), GB_START)
    heater = np.asarray(table["heater"], dtype=float)
    pv = _gb_pv().reindex(table.index).to_numpy(dtype=float)
    assert (heater[pv <= 1e-9] <= 1e-6).all()


def test_heizstab_laeuft_bei_echtem_ueberschuss_weiter():
    """Gegenprobe: Die Schranke darf den Heizstab nicht stilllegen.

    Ohne Schranke plant dasselbe Szenario 38,5 kWh Waerme, mit ihr 35,1 —
    der Unterschied ist genau der Anteil aus der Batterie. Der Rest muss
    bleiben, sonst waere die Schranke zu streng.
    """
    table = opt_highs.opt(_GruenbachConfig(), GB_START)
    waerme_kwh = float(np.asarray(table["heater"], dtype=float).sum()) * AC_EFF * P2E
    assert waerme_kwh > 25.0, f"Nur {waerme_kwh:.1f} kWh Waerme — die Schranke ist zu streng"
