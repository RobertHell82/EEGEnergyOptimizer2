"""Fahrplan-Anbindung: HA-Daten → chamo-Optimierer → Fahrplan.

Der Optimierer in ``chamo/`` ist reine Rechnung ohne HA-Bezug. Diese Datei
ist die Brücke und hält dabei eine Trennung ein, die nicht verhandelbar ist:

* ``async_collect_inputs()`` läuft im **Event-Loop** und liest alles, was aus
  Home Assistant kommt — Verbrauchsprofil, Batteriezustand, PV-Prognose. Das
  Ergebnis ist ein pandas-freies Dataclass.
* ``ScheduleRunner._solve()`` läuft im **Executor** und rechnet nur noch. Dort
  wird pandas importiert und ``opt()`` aufgerufen; kein Zugriff auf ``hass``.

Grund: der pandas-Import und der Modellaufbau blockieren lange genug, dass HA
sie im Loop als blocking call meldet — und State-Zugriffe aus einem Thread
sind ohnehin nicht zulässig.

Gesteuert wird hier nichts: ``push()`` bleibt bewusst leer. Die Umsetzung des
Fahrplans übernimmt der ScheduleExecutor (schedule_executor.py) im
30-Sekunden-Guard-Lauf — getrennt, weil Rechnen (minütlich, Executor-Thread)
und Nachführen (30 s, Event-Loop, Messwerte) verschiedene Takte haben.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from . import eeg_price
from .const import (
    CONF_BATTERY_SOC_SENSOR,
    CONF_DISCHARGE_POWER_KW,
    CONF_FORECAST_SOURCE,
    CONF_GRID_EXPORT_LIMIT_ENABLED,
    CONF_GRID_EXPORT_LIMIT_KW,
    CONF_INVERTER_AC_LIMIT_KW,
    CONF_PV_PEAK_KWP,
    DEFAULT_DISCHARGE_POWER_KW,
    DEFAULT_GRID_EXPORT_LIMIT_ENABLED,
    DEFAULT_GRID_EXPORT_LIMIT_KW,
    DOMAIN,
    FORECAST_SOURCE_SOLCAST,
)
from .power_readings import (
    compute_house_load_kw,
    compute_pv_now_kw,
    resolve_battery_capacity_kwh,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.util import dt as dt_util

    _now_local = dt_util.now
except ImportError:  # Testumgebung ohne HA
    from datetime import timezone

    def _now_local() -> datetime:
        return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Einstellungen (additiv — die Config-Entry-Version bleibt unberührt, damit
# ein Rückwechsel auf die produktive Integration jederzeit möglich ist)
# ---------------------------------------------------------------------------

CONF_SCHEDULE_AC_LIMIT_KW = "schedule_ac_limit_kw"
CONF_SCHEDULE_FEEDIN_PRICE = "schedule_feedin_price"
# Nachtsatz der Standardvergütung — nur bei Quelle „manual" (die OeMAG kennt
# keinen Nachtsatz), 0 oder leer heißt: kein Nachttarif.
CONF_SCHEDULE_FEEDIN_PRICE_NIGHT = "schedule_feedin_price_night"
# Woher der Basistarif kommt: Handeingabe, OeMAG (oemag.py) oder die
# Strombörse (spot.py, aWATTar-API).
CONF_SCHEDULE_FEEDIN_SOURCE = "schedule_feedin_source"
FEEDIN_SOURCE_OEMAG = "oemag"
FEEDIN_SOURCE_SPOT = "spot"
DEFAULT_SCHEDULE_FEEDIN_SOURCE = "manual"
# Abschlag des Vermarkters auf den Börsenpreis (€/kWh, im Panel in Cent).
# 0 oder leer heißt: der volle Spotpreis. Negativ wäre ein Aufschlag.
CONF_SPOT_FEEDIN_FEE = "spot_feedin_fee"
CONF_SCHEDULE_NIGHT_START = "schedule_night_start"
CONF_SCHEDULE_NIGHT_END = "schedule_night_end"
# Eigenes Nachtfenster der Gemeinschaften: EEG/BEG-Verträge können ein
# anderes Fenster haben als der Einspeisevertrag der Standardvergütung.
# Leer heißt: wie das Standard-Fenster — Bestandsanlagen ändern sich nicht.
CONF_PEAKSHARE_NIGHT_START = "peakshare_night_start"
CONF_PEAKSHARE_NIGHT_END = "peakshare_night_end"
CONF_SCHEDULE_CONSUMPTION_PRICE = "schedule_consumption_price"
CONF_SCHEDULE_GRID_FEE = "schedule_grid_fee"
CONF_SCHEDULE_BATTERY_COST = "schedule_battery_cost"
# Mindest-Ladestand in Prozent, unter den der Fahrplan nicht planen darf.
# Anders als die Notstrom-Reserve ist das kein Vorrat für einen Ausfall,
# sondern Batterieschonung: eine Tiefentladung kostet Lebensdauer, und der
# Wechselrichter regelt in den letzten Prozent ohnehin unsauber.
CONF_SCHEDULE_MIN_SOC_PCT = "schedule_min_soc_pct"
# Obergrenze: darüber bleibt zu wenig nutzbarer Bereich, um eine Nacht zu
# tragen — aus Batterieschonung würde Stilllegung.
MAX_MIN_SOC_PCT = 30

# Maximum-Ladestand in Prozent: darüber plant der Fahrplan nicht. Gegenstück
# zum Mindest-Ladestand — manche Zellchemien altern nahe der Vollladung
# schneller, weshalb manche Betreiber ihre Batterie bewusst nie ganz füllen.
# 100 heißt: bis voll laden (Vorgabe). Der frühere Ein/Aus-Schlüssel
# ``schedule_max_soc_enabled`` ist entfallen (Migration v27): der Zustand
# steckt allein im Wert, 100 ist der Aus-Zustand.
CONF_SCHEDULE_MAX_SOC_PCT = "schedule_max_soc_pct"
# Untergrenze der Einstellung. Zusammen mit MAX_MIN_SOC_PCT (30) bleiben
# immer mindestens 40 Prozentpunkte nutzbarer Bereich — Boden und Deckel
# können sich also nie kreuzen, egal wie beides eingestellt ist.
MIN_MAX_SOC_PCT = 70

# Slotlänge, bewusst nicht einstellbar: 15 Minuten sind das Abrechnungsraster.
# Feiner bringt keine bessere Entscheidung, kostet aber Rechenzeit; gröber
# verwischt kurze Preis- und Lastfenster.
DEFAULT_TIME_RES_MIN = 15
# Wie oft neu gerechnet wird. Ein Lauf kostet rund 40 ms Rechenzeit im
# Executor, minütlich ist also unkritisch — und nötig, damit die Steuerung
# dem tatsächlichen Ladestand folgt statt einem 15 Minuten alten Plan.
DEFAULT_INTERVAL_MIN = 1
# Planungshorizont, bewusst nicht einstellbar: 48 Stunden sind zwei volle
# Tage — der übernächste Vormittag ist damit zu jeder Tageszeit im Blick, nicht
# nur abends. Rechenzeit ist kein Argument dagegen (gemessen 30 -> 37 ms bei
# 145 -> 193 Slots), die Grenze ist die Prognose: Solcast liefert über die
# Tagessensoren eine Woche und erreicht die 48 Stunden immer. Forecast.Solar
# reicht nur bis zum Ende des morgigen Tages — dort ist dies eine Obergrenze,
# den tatsächlichen Horizont bestimmt _horizont_aus_wh_hours().
DEFAULT_HORIZON_HOURS = 48
# Raster der Eingangs-Zeitreihen. Solcast liefert Halbstundenwerte, also
# nehmen wir die auch als Raster; opt() resampelt daraus auf time_res.
GRID_STEP_MIN = 30
# Worst-Case-Pfad, falls die Prognosequelle kein Perzentil liefert (das ist
# bei Forecast.Solar der Fall). Bei Solcast kommt pv_estimate10 zum Einsatz
# und dieser Faktor bleibt ungenutzt. Nicht einstellbar: 60 % des
# Erwartungswerts liegt in der Größenordnung, die Solcast als p10 gegen p50
# meldet — genauer wird es durch Raten am Regler nicht, und wer eine echte
# Bandbreite will, nimmt Solcast.
DEFAULT_WORST_CASE_FACTOR = 0.6
DEFAULT_AC_LIMIT_KW = 10.0
# Preise als österreichische Richtwerte (Stand 2026). Wer andere Tarife hat,
# stellt sie im Panel ein — der Fahrplan reagiert vor allem auf den Unterschied
# zwischen Tag und Nacht, nicht auf die absolute Höhe.
DEFAULT_FEEDIN_PRICE = 0.082
DEFAULT_NIGHT_START = "20:00"
DEFAULT_NIGHT_END = "06:00"
# Aufschlag vom Einspeise- auf den Bezugspreis. Der Name kommt aus Haralds
# Config und meint nicht das Netzentgelt allein, sondern die ganze Differenz
# (Energiepreisdifferenz + Netz + Abgaben). Mit dem Einspeise-Default ergibt
# das einen Bezugspreis von 24,7 ct. Wer seinen Arbeitspreis kennt, setzt ihn
# direkt über CONF_SCHEDULE_CONSUMPTION_PRICE — genauer, aber kaum wirksam:
# solange der Bezug klar über der Einspeisung liegt, ändert seine Höhe den
# Fahrplan nicht.
DEFAULT_GRID_FEE = 0.1647
DEFAULT_BATTERY_COST = 0.01
DEFAULT_MIN_SOC_PCT = 10.0
DEFAULT_MAX_SOC_PCT = 100.0     # kein Deckel
# Vorschaufenster der dynamischen Reserve (``bor`` in Haralds Modell). Nicht
# einstellbar, 18 Stunden decken jede Nacht plus Puffer ab.
#
# Was es tut: In Überschuss-Slots verlangt ``bor`` als Untergrenze so viel
# gespeicherte Energie, wie der größte kumulierte Fehlbetrag der nächsten
# 18 Stunden ausmacht — gerechnet mit dem p10-Pfad und gedeckelt auf das,
# was bis dahin überhaupt erreichbar war. In Defizit-Slots deckelt dagegen
# ``max_blackout_reserve`` (bei uns 0), dort gibt es keine Untergrenze und
# die Nachteinspeisung bleibt frei.
#
# Gemessen an den echten Anlagendaten (26.08.2026), Fenster 18 h gegen einen
# einzigen Slot, bei sonst gleicher Konfiguration:
#
#   Tag      SOC 10:00      tiefster SOC     Export/Erlös/Bezug
#   sonnig   unverändert    unverändert      unverändert
#   80 %     unverändert    unverändert      unverändert
#   40 %     49 % statt 19  21,6 statt 5,0   unverändert
#   25 %     69 % statt 21  36,1 statt 20,9  unverändert
#
# An guten Tagen ändert sich nichts, an schlechten hält der Fahrplan die
# Batterie deutlich voller — und zwar zum selben Preis: Export, Erlös und
# Netzbezug sind in jeder Wetterlage bis auf die dritte Nachkommastelle
# gleich. Es verschiebt sich nur, WANN die Energie im Speicher liegt. Damit
# ist es ein Puffer gegen Überraschungen (Auto angesteckt, Wärmepumpe zieht
# mehr), der nichts kostet.
BLACKOUT_LOOKAHEAD = "18h"


# Die Prognose-Integrationen, die async_get_solar_forecast anbieten
_FORECAST_DOMAINS = {
    "solcast": "solcast_solar",
    "solcast_solar": "solcast_solar",
    "forecast_solar": "forecast_solar",
}


@dataclass
class ScheduleInputs:
    """Alles, was der Optimierer braucht — bewusst ohne pandas und ohne hass.

    Die Zeitreihen sind stündlich ab ``start`` (erster Punkt liegt genau auf
    ``start``, danach volle Stunden). ``opt()`` resampelt selbst auf
    ``time_res_s`` und interpoliert dabei.
    """

    start: datetime
    time_res_s: int
    timestamps: list[datetime]
    consumption_kw: list[float]
    production_kw: list[float]
    # Echter p10-Pfad, wenn die Quelle einen liefert (Solcast). Sonst None,
    # dann skaliert _Forecast den Erwartungswert mit worst_case_factor.
    min_production_kw: list[float] | None
    worst_case_factor: float

    battery_free_kwh: float
    battery_capacity_kwh: float
    battery_power_limit_kw: float
    soc_pct: float | None

    ac_limit_kw: float
    feedin_limit_kw: float
    feedin_price: float
    # Einspeisepreis im Nachtfenster; None = kein zweiter Tarif
    feedin_price_night: float | None
    night_start_hour: int
    night_end_hour: int
    consumption_price: float
    battery_cost: float
    # Untergrenze in Prozent; 0 = der Fahrplan darf bis leer planen
    min_soc_pct: float = 0.0
    # Obergrenze in Prozent; 100 = der Fahrplan darf bis voll planen
    max_soc_pct: float = 100.0
    forecast_source: str = ""
    # Preisaufschlag je Zeitpunkt aus dem Bedarf der Energiegemeinschaften
    # (€/kWh, siehe eeg_price.py). Leer = keine Gemeinschaft wirkt mit.
    eeg_bonus: list[float] | None = None
    # Aufschlüsselung je Gemeinschaft — nur für Anzeige und Log.
    eeg_details: list[dict] | None = None
    # Basistarif als Zeitreihe je ``timestamps``-Eintrag (€/kWh) — gesetzt bei
    # Quelle „Spotpreis". Ersetzt dann feedin_price/feedin_price_night als
    # Grundlage; darf negativ sein (echte Börsenpreise, der Fahrplan regelt
    # dann ab statt einzuspeisen).
    feedin_price_series: list[float] | None = None
    # Wie viele Slots der Reihe vom Vortag fortgeschrieben sind (Anzeige/Log).
    feedin_series_extrapolated: int = 0
    # Die ECHTEN Vergütungssätze der Gemeinschaften (ohne Gewichtung), je
    # Eintrag {"anteil", "tag", "nacht"} — nur für die Gewinnberechnung.
    # eeg_bonus dagegen ist die Fiktion, mit der GESTEUERT wird.
    eeg_tarife: list[dict] | None = None
    # Saldo-Prognose je Gemeinschaft für die Gewinnberechnung:
    # {Name: {Epochenviertelstunde: kWh}}, positiv = Bedarf (wie peakshare.py).
    # Die Gemeinschaft nimmt je Viertelstunde nur auf, was ihr Saldo hergibt —
    # der Rest der Einspeisung fällt zum Basistarif an den Restabnehmer.
    eeg_bedarf: dict[str, dict[int, float]] | None = None
    # Nachtfenster der Gemeinschafts-Nachtsätze — EEG/BEG-Verträge können ein
    # anderes Fenster haben als die Standardvergütung (night_start/end_hour).
    # None = wie das Standard-Fenster.
    eeg_night_start_hour: int | None = None
    eeg_night_end_hour: int | None = None


class _Forecast:
    """Erzeugungsprognose in der Form, die ``opt()`` erwartet."""

    def __init__(self, inputs: ScheduleInputs) -> None:
        self._inputs = inputs
        self._series = None  # wird beim ersten Zugriff gebaut (Executor)
        self._min_series = None

    def _build(self):
        if self._series is None:
            import pandas as pd

            self._series = pd.Series(
                self._inputs.production_kw, index=pd.DatetimeIndex(self._inputs.timestamps)
            )
        return self._series

    def production(self, start_time):
        return self._build().loc[start_time:]

    def min_production(self, start_time):
        """Worst-Case-Pfad: p10 der Prognose, sonst skalierter Erwartungswert."""
        if self._inputs.min_production_kw is None:
            return self.production(start_time) * self._inputs.worst_case_factor
        if self._min_series is None:
            import pandas as pd

            self._min_series = pd.Series(
                self._inputs.min_production_kw,
                index=pd.DatetimeIndex(self._inputs.timestamps),
            )
        return self._min_series.loc[start_time:]


class HAConfig:
    """Config-Provider für ``opt()``, gefüttert aus ScheduleInputs.

    Bewusst keine Unterklasse von ``chamo.config_dummy.Config``: die
    Dummy-Klasse liest aus CSV-Dateien und ihr ``__init__`` legt einen
    DummyForecast an, den wir sofort wieder ersetzen würden. Das API ist
    identisch — wer es ändert, muss hier nachziehen.
    """

    # Von uns nicht gesteuert, aber Teil des API
    time_buffer = 110
    fullcharge_try = False
    no_grid_charging = True
    ac_efficiency = 0.95
    max_grid_cost = 0.011
    max_battery_cost = 0.01
    battery_resistance = 0.04

    def __init__(self, inputs: ScheduleInputs) -> None:
        self._inputs = inputs
        self.time_res = inputs.time_res_s
        self.forecast = _Forecast(inputs)

        # Der Mindest-Ladestand wird als *nicht vorhandene* Kapazität
        # modelliert: opt() zählt in "freier Platz bis voll", also schneidet
        # eine kleinere Kapazität genau unten ab. Das ist eine harte
        # Untergrenze, die in JEDEM Slot gilt.
        #
        # Über die Reserve (``max_blackout_reserve``) ginge es nicht: ``bor``
        # ist vorausschauend und gibt die Füllung frei, sobald in den nächsten
        # Stunden kein Defizit mehr liegt. Gemessen blieb der tiefste geplante
        # Ladestand dadurch unverändert — 30,8 % bei 0 wie bei 30 % Vorgabe,
        # die verlangte Mindestfüllung fiel in jeder Variante irgendwann auf
        # null. Deshalb ist die Kapazitätsvariante die richtige, und die
        # Reserve bleibt bei 0.
        #
        # Der Ladedeckel ist das Spiegelbild, braucht aber einen Schritt mehr:
        # nach unten abzuschneiden genügt hier nicht, weil ``battery_free``
        # von unten durch 0 begrenzt ist und opt() dafür keinen Parameter
        # kennt (``battery_free_lb`` ist dort stets <= 0). Stattdessen rechnet
        # das Modell im verschobenen Fenster [Boden, Deckel]:
        #
        #     Kapazität   = Deckel - Boden
        #     battery_free = (Kapazität_echt - Ist) - (Kapazität_echt - Deckel)
        #
        # "voll" heißt für opt() dann Deckel, "leer" heißt Boden. Damit bleibt
        # Haralds Modell unberührt — der Preis dafür ist, dass der Ladestand
        # beim Auslesen zurückgerechnet werden muss (siehe solve()).
        floor_kwh = inputs.battery_capacity_kwh * max(
            0.0, min(90.0, inputs.min_soc_pct)
        ) / 100.0
        self.deckel_kwh = inputs.battery_capacity_kwh * max(
            0.0, min(100.0, inputs.max_soc_pct)
        ) / 100.0
        self.battery_capacity = max(0.5, self.deckel_kwh - floor_kwh)
        # Zwei Klemmungen, zwei verschiedene Fälle:
        # unten — steht die Batterie schon unter dem Puffer, rechnet das
        # Modell von "leer" aus weiter, sonst wären die Schranken
        # widersprüchlich;
        # oben — steht sie über dem Deckel (Deckel gerade gesenkt, oder das
        # Gerät hat selbst voll geladen), sieht das Modell "voll". Es darf
        # dann nicht weiter laden, und mehr kann es nicht ausdrücken. Der
        # geplante Ladestand startet dadurch unter dem wirklichen; das ist
        # die konservative Richtung — geplant wird mit weniger Energie, als
        # tatsächlich da ist. Eine Zwangsentladung auf den Deckel wäre die
        # Alternative und ist bewusst nicht gewollt: der Deckel begrenzt das
        # Laden, er wirft nichts weg.
        ueber_dem_deckel = inputs.battery_capacity_kwh - self.deckel_kwh
        self.battery_free = max(
            0.0, min(inputs.battery_free_kwh - ueber_dem_deckel, self.battery_capacity)
        )
        self.battery_power_limit = inputs.battery_power_limit_kw
        self.ac_limit = inputs.ac_limit_kw
        self.battery_cost = inputs.battery_cost
        # Keine getrennte Notstrom-RESERVE (Deckel 0) — die harte Untergrenze
        # macht oben die Kapazität (Mindest-Ladestand). Das Vorschau-FENSTER
        # bleibt aber echt: mit 18 Stunden hält der Fahrplan an trüben Tagen
        # deutlich mehr im Speicher, ohne dass sich Erlös oder Netzbezug
        # ändern (Messung bei BLACKOUT_LOOKAHEAD).
        #
        # Bis 1.5.27 stand hier ein Fenster von einem Slot, mit der Begründung,
        # die Reserve falle „in jedem Slot auf null". Das stimmte nur für den
        # sonnigen Tag, an dem es geprüft worden war — an wechselhaften Tagen
        # ist der Unterschied bis zu 50 Prozentpunkte Ladestand.
        self.max_blackout_reserve = 0.0
        self.blackout_time = BLACKOUT_LOOKAHEAD

        self._consumption_series = None
        self._feedin_series = None

    @property
    def grid_fee(self) -> float:
        """Aufschlag vom Einspeise- auf den Bezugspreis.

        Gehört zu Haralds API; bei uns abgeleitet, weil wir den Bezugspreis
        direkt konfigurieren. Nicht das Netzentgelt allein — die ganze
        Differenz aus Energiepreis, Netz und Abgaben.
        """
        return self._inputs.consumption_price - self._inputs.feedin_price

    # -- Zeitreihen ----------------------------------------------------

    def consumption(self, start_time):
        if self._consumption_series is None:
            import pandas as pd

            self._consumption_series = pd.Series(
                self._inputs.consumption_kw,
                index=pd.DatetimeIndex(self._inputs.timestamps),
            )
        return self._consumption_series.loc[start_time:]

    def feedin_limit(self, start_time):
        return self._inputs.feedin_limit_kw

    def feedin_price(self, start_time):
        """Einspeisepreis: Basistarif, Nachtfenster und EEG-Aufschlag.

        Skalar nur im einfachsten Fall — sobald ein Nachttarif gilt oder eine
        Energiegemeinschaft mitwirkt, wird daraus eine Zeitreihe. Gemessen an
        einer echten Anlage genügen 2 ct Unterschied, damit der Fahrplan
        Energie in die teurere Stunde verschiebt; auf die Höhe kommt es dabei
        kaum an, auf den Verlauf sehr.

        Der Aufschlag steckt schon fertig in ``inputs.eeg_bonus`` — gerechnet
        wird er in ``async_collect_inputs``, weil dort noch Zugriff auf Home
        Assistant besteht. Hier wird nur addiert und gedeckelt.
        """
        nacht = self._inputs.feedin_price_night
        bonus = self._inputs.eeg_bonus or []
        serie = self._inputs.feedin_price_series
        # Ein Bonuseintrag kann jetzt auch negativ sein — hat die Gemeinschaft
        # Überschuss, ist die Kilowattstunde dort weniger wert. Beide
        # Richtungen machen aus dem Skalar eine Zeitreihe.
        hat_bonus = any(b for b in bonus)
        if (
            serie is None
            and (nacht is None or nacht == self._inputs.feedin_price)
            and not hat_bonus
        ):
            return self._inputs.feedin_price

        import pandas as pd

        if self._feedin_series is None:
            von = self._inputs.night_start_hour
            bis = self._inputs.night_end_hour
            index = pd.DatetimeIndex(self._inputs.timestamps)
            basis = self._inputs.feedin_price
            werte = []
            basis_je_slot = []
            for i, stamp in enumerate(index):
                if serie is not None:
                    # Börsenreihe: darf negativ sein, kein Nachtfenster.
                    preis = serie[i] if i < len(serie) else serie[-1]
                else:
                    preis = basis
                    if nacht is not None and nacht != basis and _ist_im_nachtfenster(
                        stamp.hour, von, bis
                    ):
                        preis = nacht
                basis_je_slot.append(preis)
                if i < len(bonus):
                    preis += bonus[i]
                werte.append(preis)
            # Der Deckel soll den SCHEINHANDEL verhindern (über dem
            # Bezugspreis kauft das LP Strom, um ihn im selben Slot teurer zu
            # verkaufen) — er darf aber keinen ECHTEN Börsenpreis kappen.
            # Sonst wurden an teuren Abenden 42, 35 und 25 ct für das Modell
            # ununterscheidbar, und bewerte_geldfluesse verrechnete gegen
            # einen anderen Preis als den, gegen den geplant wurde. Deshalb
            # ist die Grenze je Slot mindestens der echte Basistarif.
            deckel_je_slot = [
                max(self._inputs.consumption_price, b + eeg_price.DECKEL_ABSTAND)
                for b in basis_je_slot
            ]
            gedeckelt = 0
            hoechster = max(werte, default=0.0)
            for i, (wert, deckel) in enumerate(zip(werte, deckel_je_slot)):
                grenze = deckel - eeg_price.DECKEL_ABSTAND
                if wert > grenze:
                    werte[i] = grenze
                    gedeckelt += 1
            if gedeckelt:
                # Kein stiller Eingriff: greift der Deckel, ist die
                # Konfiguration zu erklären und nicht der Fahrplan.
                _LOGGER.warning(
                    "Einspeisepreis in %d Zeitpunkten auf den Bezugspreis gedeckelt "
                    "(höchster Wert %.3f, Bezugspreis %.3f €/kWh) — Gewichtung der "
                    "Gemeinschaften prüfen",
                    gedeckelt, hoechster, self._inputs.consumption_price,
                )
            # Boden je Slot: die FIKTION des Gemeinschafts-Abschlags darf den
            # Preis nicht unter null drücken — ein ECHT negativer Börsenpreis
            # aber schon (dann ist Abregeln richtig, nicht Einspeisen).
            untergrenzen = [min(0.0, b) for b in basis_je_slot]
            werte, angehoben, tiefster = eeg_price.mit_boden(werte, untergrenzen)
            if angehoben:
                # Ebenfalls kein stiller Eingriff: unter null wirft das LP die
                # Energie lieber weg, als sie zu verschenken.
                _LOGGER.warning(
                    "Einspeisepreis in %d Zeitpunkten auf null angehoben "
                    "(tiefster Wert %.3f €/kWh) — der Überschussabschlag der "
                    "Gemeinschaften übersteigt den Basistarif",
                    angehoben, tiefster,
                )
            self._feedin_series = pd.Series(werte, index=index)
        return self._feedin_series.loc[start_time:]

    def consumption_price(self, start_time):
        return self._inputs.consumption_price

    # -- Lebenszyklus --------------------------------------------------

    def fetch(self) -> None:
        """Absichtlich leer.

        Die Daten holt ``async_collect_inputs()`` im Event-Loop, bevor dieser
        Config-Provider überhaupt entsteht. Aus dem Executor heraus dürfen wir
        Home Assistant nicht befragen.
        """

    def push(self, timetable) -> None:
        """Absichtlich leer — gesteuert wird nicht aus dem Rechenlauf heraus.

        Das Mapping des laufenden Slots auf die Wechselrichter-Befehle macht
        der ScheduleExecutor im 30-Sekunden-Guard-Lauf (schedule_executor.py),
        mit Messwerten, Totbändern und Not-Aus — nicht dieser Executor-Thread.
        """

    def error(self) -> None:
        """Wird von ``opt()`` nicht aufgerufen; der Runner behandelt Fehler."""


# ---------------------------------------------------------------------------
# Daten sammeln (Event-Loop)
# ---------------------------------------------------------------------------


def _ist_im_nachtfenster(stunde: int, von: int, bis: int) -> bool:
    """Fenster über Mitternacht hinweg, z.B. 22 bis 6."""
    if von == bis:
        return False
    if von < bis:
        return von <= stunde < bis
    return stunde >= von or stunde < bis


def _min_soc_pct(config: dict) -> float:
    """Mindest-Ladestand in Prozent — 0 heißt „bis leer planen erlaubt".

    Eine 0 ist eine Aussage, nur ein fehlender oder unlesbarer Wert nimmt die
    Vorgabe. Gekappt bei 30 %: darüber wird aus Batterieschonung eine
    Stilllegung, weil vom nutzbaren Bereich zu wenig bleibt, um eine Nacht
    zu tragen.
    """
    raw = config.get(CONF_SCHEDULE_MIN_SOC_PCT)
    if raw is None or raw == "":
        return DEFAULT_MIN_SOC_PCT
    try:
        return max(0.0, min(MAX_MIN_SOC_PCT, float(raw)))
    except (TypeError, ValueError):
        return DEFAULT_MIN_SOC_PCT


def _max_soc_pct(config: dict) -> float:
    """Maximum-Ladestand in Prozent — 100 heißt „bis voll laden".

    Gekappt bei 70 % nach unten: darunter bliebe zu wenig nutzbarer Bereich.
    Zusammen mit der 30-%-Kappung des Mindest-Ladestands liegen Boden und
    Deckel immer mindestens 40 Punkte auseinander.
    """
    raw = config.get(CONF_SCHEDULE_MAX_SOC_PCT)
    if raw is None or raw == "":
        return DEFAULT_MAX_SOC_PCT
    try:
        wert = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_SOC_PCT
    # Eine 0 ist hier keine Angabe, sondern ein geleertes Panel-Zahlenfeld.
    # Gekappt würde daraus ein Deckel von 70 % — eine drastische Einstellung
    # aus einem Versehen. Dieselbe Lehre wie beim Überschussabschlag: prüfen,
    # was die Null bedeutet, die das Panel für ein leeres Feld schickt.
    if wert <= 0:
        return DEFAULT_MAX_SOC_PCT
    return max(MIN_MAX_SOC_PCT, min(100.0, wert))


def _stunde_aus_zeit(wert: Any, default: int) -> int:
    """Nimmt '22:00', '22' oder 22 und gibt die Stunde zurück."""
    if wert is None or wert == "":
        return default
    if isinstance(wert, (int, float)):
        return int(wert) % 24
    try:
        return int(str(wert).split(":")[0]) % 24
    except (ValueError, IndexError):
        return default


def _read_float(hass: HomeAssistant, entity_id: str | None) -> float | None:
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable", ""):
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None


def _grid_timestamps(
    start: datetime, hours: int, step_min: int = GRID_STEP_MIN
) -> list[datetime]:
    """Zeitraster ab start: erster Punkt exakt auf start, dann im Takt step_min.

    Das Raster ist unabhängig von der Prognosequelle. Der Verbrauch kommt aus
    dem Stundenprofil (Stufenfunktion), die PV-Prognose im Halbstundentakt von
    Solcast passt direkt darauf.

    **Geschritten wird in UTC, nicht auf der Wanduhr.** Ortszeit plus
    ``timedelta`` rechnet den Zeitzonen-Offset nicht mit: an der
    Frühjahrs-Umstellung entstanden so 02:00 (CET, die Stunde gibt es nicht)
    und danach 03:00 (CEST) — derselbe UTC-Zeitpunkt. Die Liste bekam
    Duplikate, und ``resample()`` in ``opt()`` brach mit "cannot reindex on
    an axis with duplicate labels" ab: kein Fahrplan, und weil der Horizont
    48 Stunden umfasst, zwei Tage lang bei jedem Lauf — bis der Failsafe den
    Wechselrichter freigab. Im Herbst gab es keinen Absturz, aber einen
    90-Minuten-Sprung, der eine Stunde Stützpunkte verschluckte (197 statt
    193 Slots). Über die Epoche geschritten stimmen beide Übergänge; die
    Stempel kommen als Ortszeit zurück, dann aber mit dem richtigen Offset.
    """
    zone = start.tzinfo
    stamps = [start]
    schritt = timedelta(minutes=step_min)
    cursor = start.replace(second=0, microsecond=0)
    cursor = cursor + timedelta(minutes=step_min - (cursor.minute % step_min))
    if zone is None:
        # Naive Zeitstempel (Tests): keine Zeitzone, keine Umstellung.
        ende = start + timedelta(hours=hours)
        while cursor <= ende:
            stamps.append(cursor)
            cursor = cursor + schritt
        return stamps

    cursor_utc = cursor.astimezone(timezone.utc)
    ende_utc = start.astimezone(timezone.utc) + timedelta(hours=hours)
    while cursor_utc <= ende_utc:
        stamps.append(cursor_utc.astimezone(zone))
        cursor_utc = cursor_utc + schritt
    return stamps


def _consumption_from_profile(coordinator: Any, stamps: list[datetime]) -> list[float] | None:
    """Verbrauchsprofil (W je Gruppe/Stunde) auf die Zeitpunkte abbilden.

    Der Wert kommt über ``hourly_for()``, nicht über ``hourly_avg``: nur der
    Coordinator weiß, ob ein Zeitpunkt in die Werktags- oder in die
    Wochenend-/Feiertagsgruppe fällt. Über den Wochentagsschlüssel würde ein
    Feiertag am Dienstag mit Werktagslast geplant.
    """
    hourly = getattr(coordinator, "hourly_avg", None)
    if not hourly:
        return None
    values: list[float] = []
    for stamp in stamps:
        watts = coordinator.hourly_for(stamp)
        if watts is None:
            return None
        values.append(round(watts / 1000.0, 4))
    return values


def _solcast_detailed(hass: HomeAssistant) -> dict[datetime, tuple[float, float]]:
    """Halbstundenwerte aus den Solcast-Tagessensoren sammeln.

    Solcast hängt an jeden Tagessensor ein Attribut ``detailedForecast`` mit
    48 Einträgen der Form ``{period_start, pv_estimate, pv_estimate10,
    pv_estimate90}`` — Leistung in kW. Über sieben Tagessensoren ergibt das
    eine Woche Vorausschau samt Worst-Case-Pfad.

    Gesucht wird über das Attribut, nicht über Entity-Namen: die sind
    lokalisiert (``prognose_heute`` gegen ``forecast_today``) und wären eine
    dauerhafte Fehlerquelle.
    """
    werte: dict[datetime, tuple[float, float]] = {}
    for state in hass.states.async_all("sensor"):
        detailed = state.attributes.get("detailedForecast")
        if not isinstance(detailed, list):
            continue
        for eintrag in detailed:
            if not isinstance(eintrag, dict):
                continue
            roh = eintrag.get("period_start")
            if not roh:
                continue
            try:
                stamp = (
                    roh
                    if isinstance(roh, datetime)
                    else datetime.fromisoformat(str(roh))
                )
            except ValueError:
                continue
            if stamp.tzinfo is None:
                continue
            try:
                erwartung = float(eintrag.get("pv_estimate") or 0.0)
                p10 = float(
                    eintrag.get("pv_estimate10", eintrag.get("pv_estimate")) or 0.0
                )
            except (TypeError, ValueError):
                continue
            werte[stamp] = (erwartung, p10)
    return werte


def _production_from_detailed(
    detailed: dict[datetime, tuple[float, float]], stamps: list[datetime]
) -> tuple[list[float], list[float]]:
    """Halbstundenwerte auf das Raster legen — Vergleich über den Zeitpunkt."""
    nach_epoche = {stamp.timestamp(): werte for stamp, werte in detailed.items()}
    sortiert = sorted(nach_epoche)

    erwartung: list[float] = []
    p10: list[float] = []
    for stamp in stamps:
        ziel = stamp.timestamp()
        treffer = nach_epoche.get(ziel)
        if treffer is None:
            # Nächstliegender Wert, der nicht in der Zukunft liegt
            passend = [t for t in sortiert if t <= ziel]
            treffer = nach_epoche[passend[-1]] if passend else (0.0, 0.0)
        erwartung.append(round(treffer[0], 4))
        p10.append(round(treffer[1], 4))
    return erwartung, p10


async def _async_solar_forecast_wh(hass: HomeAssistant, source: str) -> dict[str, float] | None:
    """Stündliche PV-Prognose über die Energy-Dashboard-Schnittstelle.

    Sowohl ``forecast_solar`` als auch ``solcast_solar`` stellen die Plattform
    ``energy`` mit ``async_get_solar_forecast()`` bereit — dasselbe, woraus das
    Energie-Dashboard seine Prognosekurve zeichnet. Rückgabe ist ein Dict
    ``{ISO-Zeitstempel: Wh in dieser Stunde}``.
    """
    domain = _FORECAST_DOMAINS.get(source)
    if domain is None:
        _LOGGER.warning("Unbekannte Prognosequelle '%s' für den Fahrplan", source)
        return None

    entries = hass.config_entries.async_entries(domain)
    if not entries:
        _LOGGER.warning("Prognose-Integration '%s' ist nicht eingerichtet", domain)
        return None

    try:
        from homeassistant.loader import async_get_integration

        integration = await async_get_integration(hass, domain)
        platform = await integration.async_get_platform("energy")
    except Exception:
        _LOGGER.exception("Energy-Plattform von '%s' nicht ladbar", domain)
        return None

    getter = getattr(platform, "async_get_solar_forecast", None)
    if getter is None:
        _LOGGER.warning("'%s' bietet kein async_get_solar_forecast", domain)
        return None

    merged: dict[str, float] = {}
    for entry in entries:
        try:
            result = await getter(hass, entry.entry_id)
        except Exception:
            _LOGGER.exception("PV-Prognose von '%s' nicht lesbar", domain)
            continue
        if not result:
            continue
        for stamp, value in (result.get("wh_hours") or {}).items():
            merged[stamp] = merged.get(stamp, 0.0) + float(value)
    return merged or None


def _production_from_wh(wh_hours: dict[str, float], stamps: list[datetime]) -> list[float]:
    """Wh-je-Stunde auf die Zeitpunkte abbilden — Wh/h entspricht kW/1000."""
    parsed: dict[datetime, float] = {}
    for raw, value in wh_hours.items():
        try:
            stamp = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if stamp.tzinfo is None:
            continue
        parsed[stamp] = float(value)

    values: list[float] = []
    for stamp in stamps:
        hour = stamp.replace(minute=0, second=0, microsecond=0)
        # Zeitzonen der Prognose können von der lokalen abweichen — über den
        # UTC-Zeitpunkt vergleichen, nicht über die Darstellung.
        match = None
        for candidate, value in parsed.items():
            if candidate.timestamp() == hour.timestamp():
                match = value
                break
        values.append(round((match or 0.0) / 1000.0, 4))
    return values


def _horizont_aus_wh_hours(wh_hours: dict[str, float], start: datetime) -> int:
    """Planungshorizont, der die Prognose nicht überschreitet.

    Forecast.Solar reicht nur bis zum Ende des morgigen Tages, der Horizont
    zählt aber ab jetzt — abends fehlt damit der halbe übernächste Tag. Was
    darüber hinausragt, kommt in ``_production_from_wh`` als 0 kW an, und für
    ``opt()`` ist das kein "unbekannt", sondern die Zusage "hier scheint
    garantiert keine Sonne". Der Plan hält die Batterie dann für den
    vermeintlich dunklen Tag zurück.

    Der Horizont endet deshalb genau dort, wo die Prognose endet.

    **Die Nacht danach mitzunehmen wäre naheliegend und ist falsch** — nicht
    nochmal versuchen. Dort ist die 0 zwar keine Annahme, sondern eine
    Tatsache, aber der letzte Slot ist in ``opt_highs.py`` hart auf halben
    Ladestand festgenagelt (``battery_free.iloc[-1] = capacity / 2``). Liegt
    dieser Nagel hinter einer Nacht, muss der Plan die Nacht mit Reserve
    durchqueren, um dort noch 50 % zu haben: die Nachtstunden verlangen
    Vorsorge, ohne Ertrag beizusteuern. Liegt er am Ende eines PV-Tages, ist
    die Batterie ohnehin voll und die Forderung kostet nichts.

    Gemessen (Start Mo 20:00, Prognose bis Di 24:00, Export der ersten
    24 Stunden, EEG-Aufschlag aktiv):

        Horizont   Plan endet    PV x0,5    PV x0,25
        27 h       Di 23:00      16,25      2,01      <- Prognoseende
        33 h       Mi 05:00      12,98      0,00      <- "bis Nachtende"
        48 h       Mi 20:00      12,98      0,00      <- vorher

    Der Export fällt monoton mit jeder Stunde jenseits der Prognose, echte
    Nachtstunden eingeschlossen.

    Rückgabe 0 heißt: die Prognose liegt vollständig in der Vergangenheit.
    """
    zeitpunkte: list[datetime] = []
    for raw in wh_hours:
        try:
            stamp = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if stamp.tzinfo is None:
            continue
        # Über den Zeitpunkt vergleichen, nicht über die Darstellung — die
        # Prognose kommt oft in UTC, geplant wird lokal.
        zeitpunkte.append(stamp.astimezone(start.tzinfo))

    if not zeitpunkte:
        return 0

    # Ein Eintrag beschreibt eine ganze Stunde, nicht einen Zeitpunkt: der
    # letzte deckt noch bis zu seinem Stundenende.
    ende = max(zeitpunkte).replace(minute=0, second=0, microsecond=0) + timedelta(
        hours=1
    )

    # Abrunden, nie aufrunden: eine angebrochene Stunde läge jenseits der
    # Prognose und brächte genau die Nullen zurück, die hier vermieden werden.
    stunden = int((ende - start).total_seconds() // 3600)
    if start + timedelta(hours=stunden) >= ende:
        # Landet der Horizont exakt auf der Grenze, liegt dort ein Slot —
        # und der hätte wieder keinen Prognosewert. Eine Stunde davor ist
        # der letzte, der noch gedeckt ist.
        stunden -= 1
    return max(0, min(stunden, DEFAULT_HORIZON_HOURS))


def _eeg_bedarf_sammeln(
    data: dict[str, Any], namen: list[str]
) -> dict[str, dict[int, float]] | None:
    """Saldo-Prognose je Gemeinschaft: ``{Name: {Viertelstunde: kWh}}``.

    EINMAL gesammelt und dann an beide Verwender gereicht — die Preisfunktion
    (Steuerung, ``_eeg_aufschlag``) und die Gewinnberechnung
    (``bewerte_geldfluesse``). Das geht im Event-Loop, weil der
    PeakShare-Provider seinen Cache im Speicher hält — kein IO, kein Netz.
    Ohne Provider oder ohne Namen ``None``: die Steuerung erzeugt dann keinen
    Aufschlag, die Bewertung fällt auf den Basistarif zurück.
    """
    provider = data.get("peakshare")
    if provider is None or not namen:
        return None
    bedarf: dict[str, dict[int, float]] = {}
    for name in namen:
        try:
            intervalle = provider.get_intervals(name)
        except Exception:  # pragma: no cover - defensiv
            _LOGGER.debug("Saldodaten für '%s' nicht lesbar", name, exc_info=True)
            intervalle = []
        bedarf[name] = eeg_price.saldo_je_intervall(intervalle)
    return bedarf


def _eeg_aufschlag(
    config: dict[str, Any],
    bedarf: dict[str, dict[int, float]] | None,
    stamps: list[datetime],
    basis_tag: float,
    basis_nacht: float | None,
    nacht_von: int,
    nacht_bis: int,
    eeg_nacht_von: int,
    eeg_nacht_bis: int,
    basis_reihe: list[float] | None = None,
) -> tuple[list[float] | None, list[dict] | None]:
    """Preisaufschlag aus dem Bedarf der Energiegemeinschaften.

    Die Rechnung selbst steht in ``eeg_price.py`` (ohne Home-Assistant-Bezug
    und einzeln getestet); die Bedarfsprognose kommt fertig gesammelt von
    ``_eeg_bedarf_sammeln`` — dieselben Daten nutzt die Gewinnberechnung.

    Zwei Nachtfenster, weil zwei Verträge: ``nacht_von/bis`` bestimmt, wann
    der Nachtsatz der STANDARDVERGÜTUNG gilt (Basisreihe), ``eeg_nacht_*``,
    wann die Gemeinschaften ihren Nachtsatz zahlen. Verglichen wird
    weiterhin, was zum selben Zeitpunkt gilt — nur eben je Vertrag.
    """
    gemeinschaften = eeg_price.gemeinschaften_aus_config(config)
    if not gemeinschaften or bedarf is None:
        return None, None

    summe = eeg_price.anteile_summe(gemeinschaften)
    if summe > 1.0001:
        # Nicht stillschweigend zurechtbiegen: der Aufteilungsschlüssel ist
        # eine vertragliche Größe, eine Summe über 100 % ist ein Eingabefehler.
        _LOGGER.warning(
            "Summe der Gemeinschafts-Anteile ist %.0f %% (höchstens 100 %% sind "
            "sinnvoll) — der Einspeisepreis wird dadurch zu hoch gewichtet",
            summe * 100,
        )

    # Beide Seiten zeitabhängig: der Basistarif kann ein Nachtfenster haben
    # oder eine Börsenreihe sein, die Gemeinschaft eigene Tag- und Nachtsätze.
    if basis_reihe is not None:
        basis: list[float] | Any = basis_reihe
    else:
        nachtwert = basis_nacht if basis_nacht else basis_tag
        basis = [
            nachtwert
            if _ist_im_nachtfenster(stamp.hour, nacht_von, nacht_bis)
            else basis_tag
            for stamp in stamps
        ]
    ist_nacht_eeg = [
        _ist_im_nachtfenster(stamp.hour, eeg_nacht_von, eeg_nacht_bis)
        for stamp in stamps
    ]

    return eeg_price.aufschlag_reihe(
        gemeinschaften, bedarf, stamps, basis, ist_nacht_eeg
    )


async def async_collect_inputs(
    hass: HomeAssistant, entry_id: str
) -> tuple[ScheduleInputs | None, str | None]:
    """Sammelt alle Eingangsdaten. Rückgabe: (Inputs, Fehlertext)."""
    data = hass.data.get(DOMAIN, {}).get(entry_id)
    if not data:
        return None, "Integration nicht geladen"

    config = dict(data.get("config") or {})
    coordinator = data.get("coordinator")
    inverter = data.get("inverter")

    # Ein leeres Zahlenfeld im Panel kommt als 0 an — das darf hier nicht
    # durchschlagen (Auflösung 0 wäre eine Division durch Null, Horizont 0
    # ein leerer Fahrplan).
    time_res_min = DEFAULT_TIME_RES_MIN

    # Auf die Minute, nicht auf das Slot-Raster: opt() verlangt, dass
    # battery_free zu start_time gilt ("nowish"). Bei einem 15-Minuten-Raster
    # wäre der Ladestand bis zu einer Viertelstunde in der Vergangenheit
    # verortet — der erste Slot ist aber genau der, den die Steuerung fährt.
    now = _now_local()
    start = now.replace(second=0, microsecond=0)

    source = str(
        config.get(CONF_FORECAST_SOURCE, FORECAST_SOURCE_SOLCAST)
        or FORECAST_SOURCE_SOLCAST
    ).lower()

    # Die Prognose kommt vor dem Zeitraster, denn sie bestimmt, wie weit
    # überhaupt geplant werden darf.
    # Erste Wahl: Solcast-Halbstundenwerte, die bringen einen echten p10 mit.
    detailed = _solcast_detailed(hass)
    wh_hours: dict[str, float] | None = None
    if detailed:
        horizon = DEFAULT_HORIZON_HOURS
        quelle = f"{source} (detailedForecast)"
    else:
        # Rückfall: Energy-Dashboard-Schnittstelle, nur Erwartungswerte —
        # und je nach Zugang nur bis zum Ende des morgigen Tages.
        wh_hours = await _async_solar_forecast_wh(hass, source)
        if not wh_hours:
            return None, "Keine PV-Prognose-Zeitreihe verfügbar"
        horizon = _horizont_aus_wh_hours(wh_hours, start)
        if horizon <= 0:
            return None, "PV-Prognose liegt vollständig in der Vergangenheit"
        if horizon < DEFAULT_HORIZON_HOURS:
            _LOGGER.debug(
                "Horizont auf %d h gekürzt — so weit reicht die Prognose (%s)",
                horizon,
                source,
            )
        quelle = f"{source} (wh_hours, {horizon} h)"

    stamps = _grid_timestamps(start, horizon)

    consumption = _consumption_from_profile(coordinator, stamps)
    if consumption is None:
        return None, "Verbrauchsprofil noch nicht geladen"

    if wh_hours is None:
        production, min_production = _production_from_detailed(detailed, stamps)
    else:
        production = _production_from_wh(wh_hours, stamps)
        min_production = None

    # Erster Stützpunkt: Messwerte statt Prognose. Für die nächsten Minuten
    # ist die aktuelle Messung der beste Schätzer, und nur der erste Slot wird
    # gefahren — die späteren Stützpunkte dienen der Vorausschau und bleiben
    # bei der Prognose (opt() interpoliert bis zum nächsten 30-Minuten-
    # Stützpunkt zurück). Nicht lesbare Messwerte lassen die Prognose stehen.
    pv_now = compute_pv_now_kw(hass, config)
    if pv_now is not None:
        production[0] = round(pv_now, 4)
        if min_production is not None:
            min_production[0] = round(pv_now, 4)
    house_load_now = compute_house_load_kw(hass, config)
    if house_load_now is not None:
        consumption[0] = round(house_load_now, 4)

    # Batterie: kombinierter Zustand bei Master/Slave, sonst die Sensoren.
    # ``has_combined_battery_state`` ist eine PROPERTY (inverter/base.py) —
    # das getattr liefert also bereits den Wahrheitswert. Bis 1.5.50 stand
    # hier ein zusätzlicher Aufruf mit Klammern: bei genau den Treibern, die
    # True melden (SolarEdge, Huawei Master/Slave), warf das
    # "'bool' object is not callable", der except-Zweig schluckte es, und der
    # kapazitätsgewichtete Zustand erreichte den Fahrplan nie. sensor.py
    # (_hat_kombinierten_batteriezustand) macht es seit jeher richtig.
    soc = capacity = None
    if inverter is not None and getattr(inverter, "has_combined_battery_state", None):
        try:
            soc, capacity = inverter.get_combined_battery_state()
        except Exception:
            _LOGGER.debug("Kombinierter Batteriezustand nicht lesbar", exc_info=True)
    if soc is None:
        soc = _read_float(hass, config.get(CONF_BATTERY_SOC_SENSOR))
    if capacity is None:
        # Sensor zuerst: der manuell eingetragene Wert ist oft der Stand vom
        # Setup-Zeitpunkt und veraltet, sobald Module ergänzt werden.
        capacity = resolve_battery_capacity_kwh(hass, config)
        capacity = float(capacity) if capacity else None

    if soc is None or not capacity:
        return None, "Batterie-Ladestand oder -Kapazität unbekannt"

    battery_free = max(0.0, capacity * (1.0 - soc / 100.0))

    # AC-Grenzleistung: konfigurierter Wert, sonst die PV-Peakleistung als Näherung.
    # Ein zu großer Wert schadet wenig (er begrenzt nur Export plus Hauslast),
    # ein zu kleiner würde den Fahrplan künstlich einschnüren.
    ac_limit = (
        config.get(CONF_INVERTER_AC_LIMIT_KW)
        or config.get(CONF_SCHEDULE_AC_LIMIT_KW)   # Altschlüssel aus dem Prototyp
        or config.get(CONF_PV_PEAK_KWP)
    )
    ac_limit = float(ac_limit) if ac_limit else DEFAULT_AC_LIMIT_KW

    # Die Einspeisegrenze gilt nur, wenn sie aktiviert ist. Sonst wäre der
    # konfigurierte Wert (Default 4 kW) eine Fessel, die es in Wirklichkeit
    # nicht gibt. Bewusst die neuen Schlüssel (grid_export_limit_*) — der
    # alte enable_feedin_limit meinte den eigenen Einspeisebegrenzungs-Regler.
    if config.get(CONF_GRID_EXPORT_LIMIT_ENABLED, DEFAULT_GRID_EXPORT_LIMIT_ENABLED):
        feedin_limit = float(
            config.get(CONF_GRID_EXPORT_LIMIT_KW, DEFAULT_GRID_EXPORT_LIMIT_KW)
            or DEFAULT_GRID_EXPORT_LIMIT_KW
        )
    else:
        feedin_limit = max(0.5, ac_limit - 0.5)

    # Notstrom-Untergrenze: unsere konfigurierte Reserve gegen den Ladestand,
    # den der Wechselrichter hardwareseitig zurückhält (Backup-Power) — der
    # höhere Wert gewinnt. Sonst plant der Fahrplan Entladungen, die das
    # Gerät verweigert, und Plan und Ist laufen dauerhaft auseinander.
    # Keine getrennte Notstromreserve mehr: der Mindest-Ladestand IST die
    # Sicherheitsreserve, und er wirkt in HAConfig als harte Untergrenze.
    # Altwerte in der Konfiguration (schedule_blackout_*) werden nicht mehr
    # gelesen und wirken daher auch nicht.
    # Der Wechselrichter hält seinen Backup-Ladestand hardwareseitig zurück.
    # Planen wir darunter, verweigert das Gerät, und Plan und Wirklichkeit
    # laufen dauerhaft auseinander — deshalb gewinnt der höhere der beiden
    # Werte als Untergrenze.
    min_soc = _min_soc_pct(config)
    backup_getter = getattr(inverter, "get_backup_reserve_soc_pct", None)
    if backup_getter is not None:
        try:
            backup_soc = float(backup_getter() or 0.0)
            if backup_soc > min_soc:
                min_soc = backup_soc
        except Exception:
            _LOGGER.debug("Backup-Ladestand des Geräts nicht lesbar", exc_info=True)

    # Preise. Der Bezugspreis lässt sich direkt setzen; ohne Angabe wird er
    # wie bei Harald aus Einspeisepreis plus grid_fee gebildet. Ein leeres
    # Panel-Zahlenfeld kommt als 0 an — beim Tagestarif fällt das auf den
    # Default zurück (ein Einspeisepreis von exakt 0 wäre eine Fessel, die
    # den ganzen Fahrplan einspeisefeindlich macht).
    feedin_tag = float(
        config.get(CONF_SCHEDULE_FEEDIN_PRICE, DEFAULT_FEEDIN_PRICE)
        or DEFAULT_FEEDIN_PRICE
    )
    # Nachtsatz der Standardvergütung (seit 1.5.42 wieder im Panel): mancher
    # Einspeisevertrag vergütet nachts anders, auch ganz ohne Gemeinschaft.
    # Ein leeres Panel-Zahlenfeld kommt als 0 an und heißt „kein Nachttarif"
    # — None lässt solve() beim Skalar bleiben. Er wirkt doppelt: als
    # Einspeisepreis im Nachtfenster und als Bezugspunkt der Preisfunktion
    # (die Gemeinschaft steht nachts gegen den Nacht-Basistarif).
    feedin_nacht = None
    try:
        nacht_raw = float(config.get(CONF_SCHEDULE_FEEDIN_PRICE_NIGHT) or 0)
        if nacht_raw > 0:
            feedin_nacht = nacht_raw
    except (TypeError, ValueError):
        feedin_nacht = None

    # Basistarif aus der OeMAG statt aus der Handeingabe. Der Wert wechselt
    # monatlich; ihn hier zu ziehen (statt im Executor) hält die Rechnung frei
    # von Netzzugriffen — geholt wird er im Hintergrund, siehe oemag.py.
    if str(
        config.get(CONF_SCHEDULE_FEEDIN_SOURCE, DEFAULT_SCHEDULE_FEEDIN_SOURCE)
        or DEFAULT_SCHEDULE_FEEDIN_SOURCE
    ).lower() == FEEDIN_SOURCE_OEMAG:
        # Die OeMAG kennt keinen Nachtsatz — ein gespeicherter Wert aus der
        # Handeingabe würde Tag und Nacht aus verschiedenen Quellen mischen.
        feedin_nacht = None
        oemag = data.get("oemag")
        oemag_preis = oemag.preis if oemag is not None else None
        if oemag_preis:
            feedin_tag = float(oemag_preis)
        else:
            # Kein Warnen im Minutentakt: der Provider meldet den Ausfall
            # einmal, das Panel zeigt Alter und Fehler dauerhaft an.
            _LOGGER.debug(
                "OeMAG-Tarif nicht verfügbar, es gilt die Handeingabe (%.5f €/kWh)",
                feedin_tag,
            )

    # Basistarif von der Strombörse (Day-Ahead, aWATTar-API): eine Zeitreihe
    # statt Tag/Nacht-Sätzen. Der Vermarkter-Abschlag geht je Slot ab, negative
    # Börsenpreise bleiben negativ (der Fahrplan regelt dann ab statt
    # einzuspeisen). Ohne jegliche Daten gilt die Handeingabe — wie bei OeMAG.
    feedin_reihe: list[float] | None = None
    reihe_fortgeschrieben = 0
    if str(
        config.get(CONF_SCHEDULE_FEEDIN_SOURCE, DEFAULT_SCHEDULE_FEEDIN_SOURCE)
        or DEFAULT_SCHEDULE_FEEDIN_SOURCE
    ).lower() == FEEDIN_SOURCE_SPOT:
        feedin_nacht = None
        spot = data.get("spot")
        roh_reihe, reihe_fortgeschrieben = (
            spot.reihe_fuer(stamps) if spot is not None else (None, 0)
        )
        if roh_reihe:
            try:
                fee = float(config.get(CONF_SPOT_FEEDIN_FEE) or 0)
            except (TypeError, ValueError):
                fee = 0.0
            feedin_reihe = [p - fee for p in roh_reihe]
            # Der Skalar bleibt als Kenngröße (Bezugspreis-Fallback, Anzeige):
            # das Mittel der Reihe ist dafür der ehrlichste Einzelwert.
            feedin_tag = sum(feedin_reihe) / len(feedin_reihe)
            if reihe_fortgeschrieben:
                _LOGGER.debug(
                    "Spotpreise: %d von %d Slots vom Vortag fortgeschrieben",
                    reihe_fortgeschrieben,
                    len(feedin_reihe),
                )
        else:
            _LOGGER.debug(
                "Keine Spotpreise verfügbar, es gilt die Handeingabe (%.5f €/kWh)",
                feedin_tag,
            )
    bezug = config.get(CONF_SCHEDULE_CONSUMPTION_PRICE)
    if bezug:
        bezug = float(bezug)
    else:
        bezug = feedin_tag + float(config.get(CONF_SCHEDULE_GRID_FEE, DEFAULT_GRID_FEE))

    # Bedarfsprognose EINMAL sammeln — für die Preisfunktion (Steuerung) und
    # die Gewinnberechnung. Die beiden Gemeinschaftslisten unterscheiden sich:
    # die Steuerung hält auch Einträge am Leben, die nur über die Gewichtung
    # wirken; für die Gewinnberechnung zählen nur echte Sätze (ohne
    # Gewichtung). Gesammelt wird über die Vereinigung der Namen.
    echte_tarife = eeg_price.echte_tarife_aus_config(config)
    alle_namen = list(dict.fromkeys(
        [g.name for g in eeg_price.gemeinschaften_aus_config(config)]
        + [t["name"] for t in echte_tarife]
    ))
    eeg_bedarf = _eeg_bedarf_sammeln(data, alle_namen)

    # Nachtfenster: das der Standardvergütung und — seit es getrennt
    # einstellbar ist — das der Gemeinschaften. Ein leeres Gemeinschafts-
    # Fenster fällt auf das Standard-Fenster zurück (Bestandsanlagen
    # verhalten sich unverändert).
    nacht_von = _stunde_aus_zeit(
        config.get(CONF_SCHEDULE_NIGHT_START), _stunde_aus_zeit(DEFAULT_NIGHT_START, 22)
    )
    nacht_bis = _stunde_aus_zeit(
        config.get(CONF_SCHEDULE_NIGHT_END), _stunde_aus_zeit(DEFAULT_NIGHT_END, 6)
    )
    eeg_nacht_von = _stunde_aus_zeit(
        config.get(CONF_PEAKSHARE_NIGHT_START) or config.get(CONF_SCHEDULE_NIGHT_START),
        _stunde_aus_zeit(DEFAULT_NIGHT_START, 22),
    )
    eeg_nacht_bis = _stunde_aus_zeit(
        config.get(CONF_PEAKSHARE_NIGHT_END) or config.get(CONF_SCHEDULE_NIGHT_END),
        _stunde_aus_zeit(DEFAULT_NIGHT_END, 6),
    )

    # Aufschlag aus dem Gemeinschaftsbedarf. Steht der Basistarif fest, kann
    # die Preisfunktion rechnen — sie braucht ihn als Bezugspunkt.
    eeg_bonus, eeg_details = _eeg_aufschlag(
        config, eeg_bedarf, stamps, feedin_tag, feedin_nacht,
        nacht_von, nacht_bis, eeg_nacht_von, eeg_nacht_bis,
        basis_reihe=feedin_reihe,
    )

    inputs = ScheduleInputs(
        start=start,
        time_res_s=time_res_min * 60,
        timestamps=stamps,
        consumption_kw=consumption,
        production_kw=production,
        min_production_kw=min_production,
        worst_case_factor=DEFAULT_WORST_CASE_FACTOR,
        battery_free_kwh=round(battery_free, 3),
        battery_capacity_kwh=float(capacity),
        # Leeres Panel-Feld (0) → Default: eine Leistungsgrenze von 0 würde
        # die Batterie im LP-Modell komplett stilllegen.
        battery_power_limit_kw=float(
            config.get(CONF_DISCHARGE_POWER_KW, DEFAULT_DISCHARGE_POWER_KW)
            or DEFAULT_DISCHARGE_POWER_KW
        ),
        soc_pct=float(soc),
        ac_limit_kw=ac_limit,
        feedin_limit_kw=feedin_limit,
        feedin_price=feedin_tag,
        feedin_price_night=feedin_nacht,
        night_start_hour=nacht_von,
        night_end_hour=nacht_bis,
        eeg_night_start_hour=eeg_nacht_von,
        eeg_night_end_hour=eeg_nacht_bis,
        consumption_price=bezug,
        # Wie bei den übrigen Fahrplan-Zahlen zählt auch hier ein leeres Feld
        # als „nicht gesetzt": das Panel speicherte leere Zahlenfelder als 0,
        # und eine 0 hieße, die Optimierung schont die Batterie überhaupt
        # nicht — an der Anlage stand genau das, ohne dass es je jemand
        # eingetragen hätte.
        battery_cost=float(
            config.get(CONF_SCHEDULE_BATTERY_COST, DEFAULT_BATTERY_COST)
            or DEFAULT_BATTERY_COST
        ),
        min_soc_pct=min_soc,
        max_soc_pct=_max_soc_pct(config),
        forecast_source=quelle,
        eeg_bonus=eeg_bonus,
        eeg_details=eeg_details,
        feedin_price_series=feedin_reihe,
        feedin_series_extrapolated=reihe_fortgeschrieben,
        # Für die Gewinnberechnung: die echten Sätze (ohne Gewichtung) und
        # der Saldo je Viertelstunde — vergütet wird nur, was die
        # Gemeinschaft laut Prognose tatsächlich aufnimmt.
        eeg_tarife=echte_tarife or None,
        eeg_bedarf=eeg_bedarf,
    )
    return inputs, None


# ---------------------------------------------------------------------------
# Rechnen (Executor)
# ---------------------------------------------------------------------------

# Spalten, die ins Panel gehen. Die Preisspalten stammen aus den Dual-Werten
# des LP und erklären, warum der Fahrplan so aussieht, wie er aussieht.
_PANEL_COLUMNS = (
    "PV",
    "consumption",
    "battery_p",
    "battery",
    "battery_ub",
    "grid_p",
    "discard",
    "bat_price",
    "ac_price",
    # Der Preis, mit dem der Fahrplan gerechnet hat — bei aktiver
    # Preisfunktion die eigentliche Erklärung für seine Form. Ohne diese
    # Spalte blieb das Sensor-Attribut einspeisepreis_ct immer leer.
    "feedin_price",
)


def solve(inputs: ScheduleInputs) -> dict[str, Any]:
    """Rechnet den Fahrplan. Läuft im Executor — hier kein hass-Zugriff."""
    from .chamo import opt_highs

    config = HAConfig(inputs)
    started = time.monotonic()
    table = opt_highs.opt(config, inputs.start)
    duration_ms = int((time.monotonic() - started) * 1000)

    slots: list[dict[str, Any]] = []
    for stamp, row in table.iterrows():
        slot: dict[str, Any] = {"t": stamp.isoformat()}
        for column in _PANEL_COLUMNS:
            value = row.get(column)
            slot[column] = None if value is None else round(float(value), 4)
        # SOC-Verlauf ist anschaulicher als die freie Kapazität. Bezugspunkt
        # ist der DECKEL, nicht die Kapazität: opt() rechnet im verschobenen
        # Fenster (siehe HAConfig), "battery_free = 0" heißt dort Deckel und
        # nicht 100 %. Ohne Deckel sind beide dasselbe, dann rechnet es wie
        # bisher. Das ist keine Anzeigekosmetik — der Executor übergibt diesen
        # Wert als Ziel-Ladestand an den Wechselrichter.
        deckel_kwh = inputs.battery_capacity_kwh * max(
            0.0, min(100.0, inputs.max_soc_pct)
        ) / 100.0
        slot["soc"] = round(
            100.0 * (deckel_kwh - float(row["battery"]))
            / inputs.battery_capacity_kwh,
            1,
        )
        slots.append(slot)

    result = {
        "slots": slots,
        "duration_ms": duration_ms,
        "time_res_min": inputs.time_res_s // 60,
        "start": inputs.start.isoformat(),
        "soc_start_pct": inputs.soc_pct,
        "battery_capacity_kwh": inputs.battery_capacity_kwh,
        "min_soc_pct": inputs.min_soc_pct,
        "max_soc_pct": inputs.max_soc_pct,
        "forecast_source": inputs.forecast_source,
    }

    # Gewinnberechnung: was bringt die Optimierung gegenüber dem
    # Standardbetrieb desselben Geräts? Ein Fehler hier darf den Fahrplan
    # nicht kosten — er ist die Steuerung, der Vergleich nur Anzeige.
    try:
        referenz = simuliere_standardbetrieb(slots, inputs)
        mit = bewerte_geldfluesse(slots, inputs)
        ohne = bewerte_geldfluesse(referenz, inputs)
        result["referenz_slots"] = referenz
        result["gewinn"] = {
            "mit": mit,
            "ohne": ohne,
            "vorteil": round(mit["summe"] - ohne["summe"], 4),
            "horizont_h": round(len(slots) * inputs.time_res_s / 3600.0, 1),
            # Womit der Endbestand bewertet wird — fürs ehrliche Beschriften.
            "endbestand_tarif": round(inputs.feedin_price, 5),
        }
    except Exception:  # noqa: BLE001 - Vergleich ist Anzeige, kein Aktor
        _LOGGER.exception("Gewinnberechnung fehlgeschlagen — der Fahrplan bleibt gültig")

    return result


# ---------------------------------------------------------------------------
# Gewinnberechnung (Executor): Standardbetrieb als Referenz, echte Geldflüsse
# ---------------------------------------------------------------------------


def simuliere_standardbetrieb(
    slots: list[dict[str, Any]], inputs: ScheduleInputs
) -> list[dict[str, Any]]:
    """Was ein Standard-Wechselrichter aus denselben Prognosen machen würde.

    Eigenverbrauchs-Logik, wie sie jedes Gerät ab Werk fährt: PV-Überschuss
    lädt zuerst die Batterie bis voll, erst der Rest wird eingespeist; ein
    Defizit entlädt die Batterie bis zum Mindest-Ladestand, erst der Rest
    kommt aus dem Netz. Eine einfache Slot-Schleife über die Spalten des
    gerechneten Fahrplans (PV, consumption) — bewusst KEIN zweiter LP-Lauf:
    das Standardgerät schaut nicht voraus, genau das ist der Unterschied.

    Dieselbe Physik wie im Modell: PV und Batterie sind DC, Hauslast und Netz
    AC, dazwischen liegt ``ac_efficiency``. Ohne den Wirkungsgrad bekäme die
    Referenz 5 % mehr Energie, als das Modell je liefern kann, und der
    Vergleich wäre systematisch schief. Weggelassen ist der Innenwiderstand
    (Zusatzverluste über 0,1C) — das begünstigt die Referenz, der
    ausgewiesene Vorteil ist also eher zu klein als zu groß.

    Vorzeichen wie im Fahrplan (Haralds Konvention): ``grid_p`` positiv =
    Einspeisung, ``battery_p`` positiv = Entladen. Der Ladedeckel
    (``max_soc_pct``) gilt hier NICHT — ein Standardgerät lädt bis voll.
    """
    dt_h = inputs.time_res_s / 3600.0
    eff = HAConfig.ac_efficiency
    kapazitaet = inputs.battery_capacity_kwh
    inhalt = kapazitaet * max(0.0, min(100.0, inputs.soc_pct or 0.0)) / 100.0
    boden = kapazitaet * max(0.0, min(100.0, inputs.min_soc_pct)) / 100.0

    referenz: list[dict[str, Any]] = []
    for slot in slots:
        pv = slot.get("PV") or 0.0
        verbrauch = slot.get("consumption") or 0.0
        # DC-Leistung, die die Hauslast hinter dem Wirkungsgrad deckt.
        bedarf_dc = verbrauch / eff
        if pv >= bedarf_dc:
            ueberschuss = pv - bedarf_dc
            laden = min(
                ueberschuss,
                inputs.battery_power_limit_kw,
                max(0.0, kapazitaet - inhalt) / dt_h,
            )
            # Was über die Einspeisegrenze hinausgeht, regelt das Gerät ab —
            # dieselbe Schranke wie im LP (feedin_limit, AC-Grenze abzüglich
            # Hauslast).
            grenze = max(
                0.0, min(inputs.feedin_limit_kw, inputs.ac_limit_kw - verbrauch)
            )
            export = min((ueberschuss - laden) * eff, grenze)
            inhalt += laden * dt_h
            batterie_p = -laden
            netz_p = export
        else:
            defizit = bedarf_dc - pv
            entladen = min(
                defizit,
                inputs.battery_power_limit_kw,
                max(0.0, inhalt - boden) / dt_h,
            )
            inhalt -= entladen * dt_h
            batterie_p = entladen
            netz_p = -(defizit - entladen) * eff
        referenz.append(
            {
                "t": slot["t"],
                "grid_p": round(netz_p, 4),
                "battery_p": round(batterie_p, 4),
                "soc": round(100.0 * inhalt / kapazitaet, 1),
            }
        )
    return referenz


def _basistarif_je_slot(
    slots: list[dict[str, Any]], inputs: ScheduleInputs
) -> list[float]:
    """Basistarif je Slot.

    Der Basistarif kommt aus der Handeingabe (mit dem Nachtfenster der
    STANDARDVERGÜTUNG — die Gemeinschaften haben ihr eigenes), von der OeMAG
    (steckt dann im Skalar) oder als Spotreihe — dort gilt je Slot der
    letzte Stützpunkt, der nicht in der Zukunft liegt (beide Listen sind
    zeitlich aufsteigend, ein Zeiger genügt).
    """
    stuetzen: list[tuple[float, float]] = []
    if inputs.feedin_price_series is not None:
        stuetzen = [
            (stamp.timestamp(), inputs.feedin_price_series[i])
            for i, stamp in enumerate(inputs.timestamps)
            if i < len(inputs.feedin_price_series)
        ]
    zeiger = 0

    ergebnis: list[float] = []
    for slot in slots:
        stamp = datetime.fromisoformat(slot["t"])
        nacht = _ist_im_nachtfenster(
            stamp.hour, inputs.night_start_hour, inputs.night_end_hour
        )
        if stuetzen:
            ziel = stamp.timestamp()
            while zeiger + 1 < len(stuetzen) and stuetzen[zeiger + 1][0] <= ziel:
                zeiger += 1
            basis = stuetzen[zeiger][1]
        elif nacht and inputs.feedin_price_night is not None:
            basis = inputs.feedin_price_night
        else:
            basis = inputs.feedin_price
        ergebnis.append(basis)
    return ergebnis


def bewerte_geldfluesse(
    slots: list[dict[str, Any]], inputs: ScheduleInputs
) -> dict[str, float]:
    """Echte Geldflüsse eines Plans — dieselbe Funktion für beide Pläne.

    Der Einspeise-Erlös folgt der realen EEG-Abrechnung je Viertelstunde:
    jeder Gemeinschaft wird ihr Anteil der Einspeisung ANGEBOTEN, vergütet
    zum Gemeinschaftssatz wird aber nur, was ihr Saldo in derselben
    Viertelstunde hergibt (positiv = Bedarf, siehe ``eeg_bedarf``) — der
    Rest fällt zum Basistarif an den Restabnehmer. Genau darin liegt der
    Zeitvorteil der Optimierung: wer einspeist, wenn die Gemeinschaft
    Bedarf hat, bekommt den Gemeinschaftssatz; der Mittagsexport des
    Standardbetriebs trifft deren Überschuss und bekommt nur den
    Basistarif. Ohne Saldodaten für einen Slot gilt der Basistarif — eine
    fehlende Prognose darf keinen erfundenen Erlös erzeugen (dieselbe Regel
    wie in der Preisfunktion). Bewusst NICHT die interne Preisfunktion
    (``eeg_bonus`` samt Deckel, Boden und Normierung) — deren Gewichtung
    und Überschussabschlag sind Steuer-Fiktionen, hier zählt, was fließt.

    Dazu: Bezug = Netzbezug × Bezugspreis; Alterung = entladene Energie ×
    Alterungskosten (wie in Haralds Zielfunktion zählt die Entladung — so
    kostet jeder Zyklus einmal, nicht doppelt); Endbestands-Gutschrift =
    Restenergie über dem Mindest-Ladestand × Basistarif (konservativ).
    Ohne sie verglichen wir ungleiche Endzustände: die Pläne enden mit
    verschiedenem Ladestand, und Haralds Modell nagelt den letzten Slot
    ohnehin auf halbe Kapazität.

    Vorzeichen wie im Fahrplan: ``grid_p`` positiv = Einspeisung,
    ``battery_p`` positiv = Entladen.
    """
    dt_h = inputs.time_res_s / 3600.0
    tarife = inputs.eeg_tarife or []
    bedarf = inputs.eeg_bedarf or {}
    basis_je_slot = _basistarif_je_slot(slots, inputs)
    # Die Gemeinschaften haben ihr eigenes Nachtfenster; ohne Angabe gilt
    # das der Standardvergütung.
    eeg_von = (
        inputs.eeg_night_start_hour
        if inputs.eeg_night_start_hour is not None
        else inputs.night_start_hour
    )
    eeg_bis = (
        inputs.eeg_night_end_hour
        if inputs.eeg_night_end_hour is not None
        else inputs.night_end_hour
    )

    erloes = bezug = alterung = 0.0
    eeg_kwh = export_gesamt_kwh = 0.0
    soc_ende: float | None = None
    for slot, basis in zip(slots, basis_je_slot):
        grid = slot.get("grid_p") or 0.0
        bat = slot.get("battery_p") or 0.0
        if grid > 0:
            export_kwh = grid * dt_h
            export_gesamt_kwh += export_kwh
            stamp = datetime.fromisoformat(slot["t"])
            viertel = int(stamp.timestamp() // 900)
            eeg_nacht = _ist_im_nachtfenster(stamp.hour, eeg_von, eeg_bis)
            unzugeteilt = export_kwh
            for tarif in tarife:
                angeboten = tarif["anteil"] * export_kwh
                saldo = (bedarf.get(tarif["name"]) or {}).get(viertel)
                aufgenommen = (
                    0.0 if saldo is None else min(angeboten, max(0.0, saldo))
                )
                satz = tarif["nacht"] if eeg_nacht else tarif["tag"]
                erloes += aufgenommen * satz + (angeboten - aufgenommen) * basis
                eeg_kwh += aufgenommen
                unzugeteilt -= angeboten
            # Restanteil (keiner Gemeinschaft zugeordnet) zum Basistarif.
            # Summieren sich die Anteile über 100 %, wird hier nichts doppelt
            # bewertet — der Fehler ist dann in der Konfiguration und wird
            # beim Sammeln der Inputs bereits als Warnung protokolliert.
            erloes += max(0.0, unzugeteilt) * basis
        else:
            bezug += -grid * dt_h * inputs.consumption_price
        if bat > 0:
            alterung += bat * dt_h * inputs.battery_cost
        if slot.get("soc") is not None:
            soc_ende = float(slot["soc"])

    rest_kwh = 0.0
    if soc_ende is not None:
        rest_kwh = max(
            0.0,
            (soc_ende - inputs.min_soc_pct) / 100.0 * inputs.battery_capacity_kwh,
        )
    endbestand = rest_kwh * inputs.feedin_price

    return {
        "erloes": round(erloes, 4),
        "bezug": round(bezug, 4),
        "alterung": round(alterung, 4),
        "endbestand": round(endbestand, 4),
        "rest_kwh": round(rest_kwh, 2),
        # Wie viel der Einspeisung wirklich zum Gemeinschaftssatz vergütet
        # wurde — macht im Panel sichtbar, wo der Zeitvorteil herkommt.
        "eeg_kwh": round(eeg_kwh, 2),
        "export_kwh": round(export_gesamt_kwh, 2),
        "summe": round(erloes - bezug - alterung + endbestand, 4),
    }


def slot_for(slots: list[dict] | None, now: datetime) -> dict | None:
    """Der Slot, der zu ``now`` läuft: der letzte, dessen Startzeit <= now ist.

    Gemeinsamer Helfer für die Fahrplan-Sensoren und den Executor — beide
    müssen denselben Slot sehen, sonst laufen Anzeige und Steuerung
    auseinander. Slots ohne parsebaren Zeitstempel werden übersprungen;
    liegt ``now`` vor dem ersten Slot (oder ist die Liste leer), kommt None.
    """
    treffer = None
    for slot in slots or []:
        try:
            stamp = datetime.fromisoformat(slot["t"])
        except (KeyError, TypeError, ValueError):
            continue
        if stamp <= now:
            treffer = slot
        else:
            break
    return treffer


class ScheduleRunner:
    """Rechnet den Fahrplan periodisch und hält das letzte Ergebnis.

    Der ScheduleExecutor liest ``to_dict()`` in jedem Guard-Lauf und setzt
    den laufenden Slot am Wechselrichter durch. ``last_run`` ist dabei die
    Frische-Referenz des Failsafes — ein eingefrorener Runner wird daran
    erkannt, nicht an der Verfügbarkeit des (alten) Ergebnisses.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self.result: dict[str, Any] | None = None
        self.error: str | None = None
        self.last_run_iso: str | None = None
        self.is_running = False
        # Die zuletzt gesammelten Inputs. Die Energiebilanz friert daraus die
        # Preise der laufenden Viertelstunde ein — über dieselben Werte, mit
        # denen der Fahrplan rechnet. Ein zweiter Preis-Pfad daneben würde bei
        # der nächsten Tarifänderung auseinanderlaufen.
        self.last_inputs: ScheduleInputs | None = None

    async def async_run(self) -> None:
        """Ein Durchlauf: sammeln im Loop, rechnen im Executor."""
        if self.is_running:
            _LOGGER.debug("Fahrplan-Lauf läuft noch, überspringe diesen Takt")
            return

        self.is_running = True
        try:
            inputs, problem = await async_collect_inputs(self._hass, self._entry_id)
            if inputs is None:
                self.error = problem
                self.result = None
                _LOGGER.info("Fahrplan nicht berechenbar: %s", problem)
                return
            self.last_inputs = inputs

            result = await self._hass.async_add_executor_job(solve, inputs)
            self.result = result
            self.error = None
            _LOGGER.info(
                "Fahrplan gerechnet: %d Slots in %d ms (Start-SOC %.0f %%)",
                len(result["slots"]),
                result["duration_ms"],
                inputs.soc_pct or 0.0,
            )
        except Exception as err:  # noqa: BLE001 - Anzeige darf nie den Zyklus killen
            self.error = f"{type(err).__name__}: {err}"
            self.result = None
            _LOGGER.exception("Fahrplan-Berechnung fehlgeschlagen")
        finally:
            self.last_run_iso = _now_local().isoformat()
            self.is_running = False

    def to_dict(self) -> dict[str, Any]:
        """Zustand für WebSocket und Sensor."""
        payload: dict[str, Any] = {
            "available": self.result is not None,
            "error": self.error,
            "last_run": self.last_run_iso,
            "is_running": self.is_running,
        }
        if self.result:
            payload.update(self.result)
        return payload
