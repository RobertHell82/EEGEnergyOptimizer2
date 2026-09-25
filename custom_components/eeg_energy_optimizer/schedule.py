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
    CONF_FORECAST_REMAINING_ENTITY,
    CONF_FORECAST_SOURCE,
    CONF_FORECAST_TOMORROW_ENTITY,
    CONF_GRID_EXPORT_LIMIT_ENABLED,
    CONF_GRID_EXPORT_LIMIT_KW,
    CONF_INVERTER_AC_LIMIT_KW,
    CONF_PV_PEAK_KWP,
    DEFAULT_DISCHARGE_POWER_KW,
    DEFAULT_GRID_EXPORT_LIMIT_ENABLED,
    DEFAULT_GRID_EXPORT_LIMIT_KW,
    DOMAIN,
    FORECAST_SOURCE_SOLCAST,
    GEWINN_HORIZONT_H,
    PUFFER_BEREITSCHAFTSVERLUST_KW,
    PUFFER_UMGEBUNG_C,
    PUFFER_WH_PRO_LITER_KELVIN_EFFEKTIV,
    WASSER_WH_PRO_LITER_KELVIN,
)
from .heizstab.controller import heizstab_max_kw, heizstab_waermewert
from .netzentgelt import (  # noqa: F401 — Schlüssel hier mit-exportiert
    CONF_SCHEDULE_NETWORK_FEE,
    CONF_SCHEDULE_NETZBEREICH,
    SNAP_RABATT,
    Netzgebuehr,
    Tariftabelle,
    netzgebuehr_fuer,
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
# Woher der Basistarif kommt: Handeingabe, OeMAG (oemag.py — der zuletzt
# veröffentlichte Monat oder die Hochrechnung des laufenden Monats aus
# oemag_schaetzung.py), die Strombörse (spot.py, aWATTar-API), der feste
# Monatstarif aWATTar SUNNY (awattar_sunny.py) oder die Energie AG
# (energie_ag.py — ebenfalls in zwei Spielarten, veröffentlicht und
# hochgerechnet).
CONF_SCHEDULE_FEEDIN_SOURCE = "schedule_feedin_source"
FEEDIN_SOURCE_OEMAG = "oemag"
FEEDIN_SOURCE_OEMAG_ESTIMATE = "oemag_estimate"
FEEDIN_SOURCE_SPOT = "spot"
FEEDIN_SOURCE_AWATTAR_SUNNY = "awattar_sunny"
FEEDIN_SOURCE_ENERGIE_AG = "energie_ag"
FEEDIN_SOURCE_ENERGIE_AG_ESTIMATE = "energie_ag_estimate"
DEFAULT_SCHEDULE_FEEDIN_SOURCE = "manual"
# Energie AG „Team Sonne Float": Preisvariante und Abschlag. Die Variante
# entscheidet über die Mindestvergütung von 2 ct (nur mit Stromliefervertrag
# bei der Energie AG Vertrieb), der Abschlag ist VPI-wertgesichert und
# deshalb einstellbar statt fest. Siehe energie_ag.py.
CONF_ENERGIE_AG_VARIANTE = "energie_ag_variante"
DEFAULT_ENERGIE_AG_VARIANTE = "float"
CONF_ENERGIE_AG_ABSCHLAG = "energie_ag_abschlag"
# aWATTar SUNNY führt seit dem 25.02.2026 zwei Preisspalten — Verträge bis
# zu diesem Tag („alt") und danach („neu"). Welche gilt, sagt das
# Vertragsdatum des Nutzers; siehe awattar_sunny.py.
CONF_AWATTAR_SUNNY_VERTRAG = "awattar_sunny_vertrag"
DEFAULT_AWATTAR_SUNNY_VERTRAG = "neu"
# Abschlag des Vermarkters auf den Börsenpreis (€/kWh, im Panel in Cent).
# 0 oder leer heißt: der volle Spotpreis. Negativ wäre ein Aufschlag.
CONF_SPOT_FEEDIN_FEE = "spot_feedin_fee"
# Prozentualer Abschlag auf den BETRAG des Börsenpreises (0–100). aWATTar
# SUNNY Spot 60min zieht 19 % auf |Preis| ab — ein negativer Preis wird
# dadurch negativer, nicht kleiner. Cent- und Prozentabschlag wirken
# zusammen; wer nur einen hat, lässt den anderen leer.
CONF_SPOT_FEEDIN_FEE_PCT = "spot_feedin_fee_pct"
CONF_SCHEDULE_NIGHT_START = "schedule_night_start"
CONF_SCHEDULE_NIGHT_END = "schedule_night_end"
# Eigenes Nachtfenster der Gemeinschaften: EEG/BEG-Verträge können ein
# anderes Fenster haben als der Einspeisevertrag der Standardvergütung.
# Leer heißt: wie das Standard-Fenster — Bestandsanlagen ändern sich nicht.
CONF_PEAKSHARE_NIGHT_START = "peakshare_night_start"
CONF_PEAKSHARE_NIGHT_END = "peakshare_night_end"
# Bezugspreis in zwei Teilen (beide €/kWh inkl. MwSt): der Arbeitspreis der
# Energie und das Netznutzungsentgelt je Kilowattstunde. Zusammen sind sie
# der Preis, den eine Kilowattstunde aus dem Netz kostet — getrennt werden
# sie nur, weil SNAP und WiNAP allein den Netzanteil senken. Die Netzgebühr
# kommt aus der Verordnung (netzentgelt.py, Schlüssel CONF_SCHEDULE_NETZBEREICH)
# oder von Hand (CONF_SCHEDULE_NETWORK_FEE). Der Altschlüssel
# CONF_SCHEDULE_CONSUMPTION_PRICE (ein Gesamtpreis) wird weiter gelesen,
# falls eine Konfiguration die Migration v28 nicht durchlaufen hat.
CONF_SCHEDULE_ENERGY_PRICE = "schedule_energy_price"
CONF_SCHEDULE_CONSUMPTION_PRICE = "schedule_consumption_price"
# Zeitvariable Netzentgelte: Sommer-Nieder-Arbeitspreis (SNAP) — seit
# 1.4.2026 ein um 20 % verringertes Netznutzungsentgelt auf der Netzebene 7,
# 1. April bis 30. September, 10 bis 16 Uhr (SNE-V 2018 idF Novelle 2026,
# § 2 Abs. 1 Z 9 und § 5 Abs. 1b) — und ab 2027 der Winter-Nieder-Arbeits-
# preis (WiNAP), 1. Oktober bis 31. März, 22 bis 4 Uhr des Folgetags (SNE-G-V
# § 7 Abs. 4). Zeiträume, Uhrzeiten und Rabatt stehen in der Verordnung und
# sind deshalb fest verdrahtet — konfiguriert wird nur, ob der Anschluss die
# zeitvariablen Sätze bekommt (Voraussetzung: Viertelstundenmessung). Der
# Schlüssel heißt nach dem ersten Fenster; er schaltet beide.
CONF_SCHEDULE_SNAP_ENABLED = "schedule_snap_enabled"
SNAP_MONAT_VON = 4       # 1. April
SNAP_MONAT_BIS = 9       # 30. September (einschließlich)
SNAP_STUNDE_VON = 10
SNAP_STUNDE_BIS = 16     # 16:00 gehört nicht mehr dazu
WINAP_MONAT_VON = 10     # 1. Oktober
WINAP_MONAT_BIS = 3      # 31. März (einschließlich, über den Jahreswechsel)
WINAP_STUNDE_VON = 22
WINAP_STUNDE_BIS = 4     # 04:00 gehört nicht mehr dazu
CONF_SCHEDULE_GRID_FEE = "schedule_grid_fee"
CONF_SCHEDULE_BATTERY_COST = "schedule_battery_cost"
# Mindest-Ladestand in Prozent, unter den der Fahrplan nicht planen darf.
# Anders als die Notstrom-Reserve ist das kein Vorrat für einen Ausfall,
# sondern Batterieschonung: eine Tiefentladung kostet Lebensdauer, und der
# Wechselrichter regelt in den letzten Prozent ohnehin unsauber.
CONF_SCHEDULE_MIN_SOC_PCT = "schedule_min_soc_pct"
# Nutzbarer Bereich, der zwischen Boden und Deckel mindestens bleiben muss.
# Darunter hätte der Fahrplan nichts mehr zu entscheiden.
SOC_BAND_MIN_PCT = 20

# Maximum-Ladestand in Prozent: darüber plant der Fahrplan nicht. Gegenstück
# zum Mindest-Ladestand — manche Zellchemien altern nahe der Vollladung
# schneller, weshalb manche Betreiber ihre Batterie bewusst nie ganz füllen.
# 100 heißt: bis voll laden (Vorgabe). Der frühere Ein/Aus-Schlüssel
# ``schedule_max_soc_enabled`` ist entfallen (Migration v27): der Zustand
# steckt allein im Wert, 100 ist der Aus-Zustand.
CONF_SCHEDULE_MAX_SOC_PCT = "schedule_max_soc_pct"
# Sicherheitspuffer in Prozent auf die PROGNOSEN: Der Verbrauch geht um
# diesen Anteil erhöht ins Modell, die PV-Erzeugung um denselben Anteil
# verringert. Der Fahrplan rechnet damit mit einem knapperen Tag, als die
# Prognose hergibt — er lädt eher und entlädt zurückhaltender.
#
# Vorgabe 0: Ein Aufschlag ist kein besserer Schätzer, sondern eine
# Verschiebung des Erwartungswerts, die in der Hälfte der Fälle in die
# falsche Richtung zeigt, und er verschiebt Einspeisung aus den
# Bedarfsstunden der Gemeinschaft in die Batterie — genau gegen den Zweck
# der Optimierung. Wer bewusst konservativer fahren will, stellt ihn ein;
# von allein tut das niemandem etwas.
CONF_SCHEDULE_SICHERHEITSPUFFER_PCT = "schedule_sicherheitspuffer_pct"
DEFAULT_SICHERHEITSPUFFER_PCT = 0.0
# Obergrenze. Darüber hätte die Prognose keine Aussagekraft mehr: Bei 50 %
# plant das Modell mit der halben Sonne und dem anderthalbfachen Verbrauch.
MAX_SICHERHEITSPUFFER_PCT = 50.0
# Untergrenze der Einstellung. Zusammen mit SOC_BAND_MIN_PCT bleiben immer
# mindestens 20 Prozentpunkte nutzbarer Bereich — Boden und Deckel können
# sich also nie kreuzen, egal wie beides eingestellt ist.
MIN_MAX_SOC_PCT = 70
# Was der Mindest-Ladestand höchstens sein darf, wenn der Deckel so tief
# steht wie erlaubt. Bei höherem Deckel ist mehr möglich — wie viel, sagt
# `max_min_soc_pct()`. Bis 2.1.1-dev22 war dieser Wert die Kappung für jede
# Anlage: Wer bei Deckel 100 einen Boden von 60 % wollte, bekam stumm 50
# (gemeldet am 14.09.2026 — eingestellt 60 %, angezeigt und wirksam 50 %).
MAX_MIN_SOC_PCT = MIN_MAX_SOC_PCT - SOC_BAND_MIN_PCT

# Deckel und Boden des Einspeisepreises sind Dauerzustände, keine Ereignisse:
# Greift einer, greift er meist über Tage, denn er hängt an der Konfiguration
# (Anlage Traun: Basistarif 2 ct gegen Gemeinschaftswerte bis 10,2 ct — der
# Überschussabschlag ist das Vierfache des Basistarifs). Gemeldet bei jedem
# Lauf ergab das rund 1.400 Warnungen am Tag, 20.000 in zwei Wochen; die
# Meldung verlor damit genau die Aufmerksamkeit, für die sie gedacht war.
#
# Gemeldet wird deshalb wie im Aktivitätsprotokoll: einmal, wenn der Zustand
# einsetzt, danach höchstens alle sechs Stunden — und sobald er sich löst,
# wird der Merker gelöscht, damit ein erneutes Auftreten sofort wieder
# auffällt.
PREISHINWEIS_WIEDERHOLUNG_S = 6 * 3600
_preishinweise: dict[str, float] = {}


def _preishinweis_faellig(kennung: str, greift: bool) -> bool:
    """Steuert, ob ein Dauerhinweis zum Preis jetzt ins Log darf.

    ``greift=False`` meldet den Zustand als beendet und löscht den Merker —
    der nächste Eintritt wird dann sofort wieder protokolliert.

    Der Zustand ist modulweit, nicht an ``HAConfig`` gebunden: Für jeden
    Planlauf entsteht eine neue Instanz, ein Gedächtnis auf ihr hätte also
    nie gegriffen.
    """
    if not greift:
        _preishinweise.pop(kennung, None)
        return False

    jetzt = time.monotonic()
    zuletzt = _preishinweise.get(kennung)
    if zuletzt is not None and jetzt - zuletzt < PREISHINWEIS_WIEDERHOLUNG_S:
        return False
    _preishinweise[kennung] = jetzt
    return True


# Wie lange ein zuletzt gelesenes Paar aus Ladestand und Kapazität einen
# Sensorausfall überbrücken darf. Die Modbus-Verbindung zum Wechselrichter
# setzt regelmäßig für eine halbe bis anderthalb Minuten aus (Anlage Traun,
# 07.-21.09.2026: 92 Timeouts, davon 45 am Batterie-Koordinator) — dabei
# stehen Ladestand UND Kapazität gleichzeitig auf "unavailable", und ohne
# Puffer fiel der Planlauf dieser Minute ersatzlos aus.
#
# Fünf Minuten sind großzügig genug für jeden beobachteten Aussetzer und
# trotzdem ehrlich: Die Kapazität ist ohnehin konstant, und der Ladestand
# bewegt sich in dieser Zeit selbst bei voller Leistung um wenige Prozent —
# weniger Fehler, als eine ausgefallene Planung anrichtet. Danach greift
# wieder der Abbruch: Ein dauerhaft toter Sensor soll auffallen und nicht
# stillschweigend mit einem alten Wert weitergefahren werden.
BATTERIE_PUFFER_MAX_S = 300

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
    # Bezugspreis im SNAP-Fenster (Sommer, 10–16 Uhr) und im WiNAP-Fenster
    # (Winter, 22–4 Uhr); None = Fenster gibt es nicht. Abgeleitet aus
    # Arbeitspreis + verbilligter Netzgebühr; die Fenstergrenzen kommen aus
    # der Verordnung, nicht aus der Konfiguration.
    consumption_price_snap: float | None = None
    consumption_price_winap: float | None = None
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
    # eeg_bonus dagegen ist die Fiktion, mit der GESTEUERT wird. Im
    # Quotenmodus (eeg_price.DEMAND_SOURCE_QUOTE) tragen die Einträge
    # zusätzlich "quote_tag"/"quote_nacht" (0..1): dann gilt die Quote statt
    # des Saldos als Maß dafür, was die Gemeinschaft aufnimmt.
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
    # Heizstab als bewertete Senke (heizstab/): maximale Leistung in kW
    # (0 = kein Heizstab) und der Wert einer Kilowattstunde Wärme in €/kWh
    # (0 = unbewertet). Mit beidem plus Budget wird der Heizstab im LP zur
    # echten Alternative zur Einspeisung; fehlt eines, bekommt er wie früher
    # nur die Spalte ``discard`` nachgelagert (siehe ``_heizstab_plan_kw``).
    heizstab_max_kw: float = 0.0
    heizstab_waermewert: float = 0.0
    # Wärme, die der Puffer noch aufnehmen kann (kWh). Nur mit diesem Wert
    # wird der Heizstab im LP zur bewerteten Senke — sonst bekommt er wie
    # bisher nur, was ohnehin abgeregelt würde. 0 = nicht einplanen.
    heizstab_budget_kwh: float = 0.0
    # Für die Temperaturprognose im Diagramm (nur Anzeige, nicht im LP —
    # siehe _puffer_temperatur_verlauf): Puffervolumen, gemessene Temperatur
    # beim Lauf und Maximaltemperatur. Fehlt eines, gibt es keine Kurve.
    heizstab_puffer_liter: float = 0.0
    heizstab_temp_c: float | None = None
    heizstab_maxtemp_c: float = 0.0


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
        """Worst-Case-Pfad: p10 der Prognose, nach unten begrenzt.

        Diese Reihe steuert im LP ausschließlich die Notstrom-Reserve
        (``opt_highs.py`` bildet daraus ``residual`` und ``bor``): je tiefer
        sie liegt, desto mehr Energie muss der Fahrplan vorhalten.

        Deshalb die Untergrenze. Solcasts p10 ist ein 10-%-Quantil, dessen
        Streuung mit dem Prognosehorizont wächst — gemessen an der
        Testanlage am 08.09.2026: Tag 1 noch 87 % der Erwartung, Tag 2 44 %,
        Tag 3 28 %, Tag 4 14 %. Für die späteren Tage ist das keine
        Wetteraussage mehr, sondern die Unsicherheit der Prognose selbst. Die
        Reserve schaut aber 18 Stunden voraus und verlangte daraus einen
        Mindest-Ladestand, der in der Nacht davor nur mit NETZBEZUG zu halten
        war: 5,08 kWh für 1,17 Euro gekauft, während die Energie im Akku lag.
        Strom kaufen kann nie der Zweck einer Reserve sein — sie soll den
        Speicher gegen den Verkauf ins Netz schützen, nicht gegen den eigenen
        Verbrauch.

        ``worst_case_factor`` ist derselbe Wert, mit dem eine Quelle ohne
        p10-Pfad (Forecast.Solar) skaliert wird — beide Quellen rechnen damit
        jetzt mit derselben Annahme: schlechtestenfalls 60 % der Erwartung.
        Der Reserve-Mechanismus selbst bleibt unberührt, auch sein
        Vorschaufenster.
        """
        erwartung = self.production(start_time)
        if self._inputs.min_production_kw is None:
            return erwartung * self._inputs.worst_case_factor
        if self._min_series is None:
            import pandas as pd

            self._min_series = pd.Series(
                self._inputs.min_production_kw,
                index=pd.DatetimeIndex(self._inputs.timestamps),
            )
        return self._min_series.loc[start_time:].clip(
            lower=erwartung * self._inputs.worst_case_factor
        )


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
        # Heizstab als bewertete Senke (siehe opt_highs.opt). Alle drei Werte
        # zusammen entscheiden, ob das LP ihn überhaupt einplant.
        self.heizstab_max_kw = inputs.heizstab_max_kw
        self.heizstab_waermewert = inputs.heizstab_waermewert
        self.heizstab_budget_kwh = inputs.heizstab_budget_kwh
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
        self._consumption_price_series = None

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

            serie = pd.Series(
                self._inputs.consumption_kw,
                index=pd.DatetimeIndex(self._inputs.timestamps),
            )
            # Gekappt auf die AC-Grenze. opt() begrenzt die Einspeisung je
            # Slot auf ``ac_limit − consumption`` und setzt damit still
            # voraus, dass die Hauslast unter der AC-Grenzleistung des
            # Wechselrichters bleibt. Liegt eine Profilstunde darüber — eine
            # 11-kW-Wallbox an einem 10-kW-Gerät reicht —, wird die Schranke
            # negativ, und der Adapter bricht vor dem Solver ab: kein Plan,
            # nach 15 Minuten Failsafe, bei jedem Lauf, solange die Stunde im
            # Horizont liegt (Fronius-Anlage, 18.09.2026: 23-kW-Stunde im
            # Wochenendprofil aus zwei Werten, „grid_p_pos_84 hat lb=0 >
            # ub=-1.78125" — Samstag 18:45, halb zwischen Grundlast und
            # Spitze interpoliert).
            #
            # Die Kappung ist exakt, nicht nur pragmatisch: Was über der
            # AC-Grenze liegt, kann der Wechselrichter ohnehin nicht liefern,
            # es kommt in jedem Fall aus dem Netz. Das ist ein konstanter
            # Kostenanteil, der keine Entscheidung verändert. Und die
            # Bedeutung der Schranke — Wechselrichterausgang höchstens
            # AC-Grenze — bleibt erhalten: dc_p·η = Hauslast + Export − Bezug
            # erreicht auch mit gekappter Hauslast höchstens ac_limit. Die
            # Referenzsimulation (simuliere_standardbetrieb) schützt dieselbe
            # Formel schon mit max(0, …); hier ist der Pfad ins LP.
            if self.ac_limit and self.ac_limit > 0:
                serie = serie.clip(upper=float(self.ac_limit))
            self._consumption_series = serie
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
            if _preishinweis_faellig("deckel", bool(gedeckelt)):
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
            if _preishinweis_faellig("boden", bool(angehoben)):
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
        """Skalar ohne Zeitfenster, sonst eine Reihe je Zeitpunkt.

        Pandas nimmt beides — der Skalar wird über alle Slots gestreckt.
        Mit SNAP oder WiNAP muss es eine Reihe sein, sonst plant das LP im
        Fenster gegen einen Preis, den es dort gar nicht gibt.
        """
        if (
            self._inputs.consumption_price_snap is None
            and getattr(self._inputs, "consumption_price_winap", None) is None
        ):
            return self._inputs.consumption_price
        if self._consumption_price_series is None:
            import pandas as pd

            index = pd.DatetimeIndex(self._inputs.timestamps)
            self._consumption_price_series = pd.Series(
                [bezugspreis_zu(self._inputs, stamp) for stamp in index],
                index=index,
            )
        return self._consumption_price_series.loc[start_time:]

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


def ist_im_snap_fenster(stamp: datetime) -> bool:
    """Liegt der Zeitpunkt im Sommer-Mittagsfenster der Verordnung?

    1. April bis 30. September, 10:00 bis 16:00 — beides einschließlich
    Startgrenze, 16:00 selbst gehört nicht mehr dazu.
    """
    return (
        SNAP_MONAT_VON <= stamp.month <= SNAP_MONAT_BIS
        and SNAP_STUNDE_VON <= stamp.hour < SNAP_STUNDE_BIS
    )


def ist_im_winap_fenster(stamp: datetime) -> bool:
    """Liegt der Zeitpunkt im Winter-Nachtfenster der Verordnung?

    1. Oktober bis 31. März, jeweils 22:00 bis 04:00 des Folgetags. Die
    Nacht gehört zum Tag, an dem sie beginnt: 31.3. 22:00 bis 1.4. 04:00
    zählt noch dazu, 1.10. 00:00 bis 04:00 noch nicht.
    """
    if stamp.hour >= WINAP_STUNDE_VON:
        tag = stamp
    elif stamp.hour < WINAP_STUNDE_BIS:
        tag = stamp - timedelta(days=1)
    else:
        return False
    return tag.month >= WINAP_MONAT_VON or tag.month <= WINAP_MONAT_BIS


def bezugspreis_zu(inputs: ScheduleInputs, stamp: datetime) -> float:
    """Bezugspreis, der zu diesem Zeitpunkt gilt (€/kWh).

    Im SNAP-Fenster der Sommer-Mittagspreis, im WiNAP-Fenster der Winter-
    Nachtpreis, sonst der eine Bezugspreis. Die Fenster überschneiden sich
    nicht (Sommer/Winter). Ohne Zeitfenster ist es rund um die Uhr derselbe.
    """
    snap = getattr(inputs, "consumption_price_snap", None)
    if snap is not None and ist_im_snap_fenster(stamp):
        return float(snap)
    winap = getattr(inputs, "consumption_price_winap", None)
    if winap is not None and ist_im_winap_fenster(stamp):
        return float(winap)
    return inputs.consumption_price


def _preis_oder_none(wert: Any) -> float | None:
    """Ein Panel-Zahlenfeld lesen: leer, 0 oder Unsinn heißt „nicht gesetzt".

    Ein leeres Zahlenfeld kommt als 0 an, und 0 hieße bei einem Preis
    „gratis" — das darf kein Feld unbemerkt bewirken.
    """
    try:
        zahl = float(wert) if wert not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return zahl if zahl is not None and zahl > 0 else None


def bezugspreis_gesamt(
    config: dict, netz: Netzgebuehr | None = None
) -> float | None:
    """Bezugspreis aus der Konfiguration (€/kWh) — oder None, wenn keiner da ist.

    Seit v28 in zwei Teilen: Arbeitspreis der Energie plus Netzgebühr
    (``netz``, siehe netzentgelt.netzgebuehr_fuer; ohne sie zählt nur der
    Arbeitspreis). Der alte Gesamtpreis (``schedule_consumption_price``)
    gilt weiter, solange kein Arbeitspreis eingetragen ist — für
    Konfigurationen, die die Migration nicht durchlaufen haben.
    """
    energie = _preis_oder_none(config.get(CONF_SCHEDULE_ENERGY_PRICE))
    if energie is not None:
        return energie + (netz.ap if netz is not None else 0.0)
    return _preis_oder_none(config.get(CONF_SCHEDULE_CONSUMPTION_PRICE))


def bezugspreise_aus_config(
    config: dict, fallback: float, netz: Netzgebuehr | None = None
) -> tuple[float, float | None, float | None]:
    """Bezugspreis, SNAP-Preis und WiNAP-Preis (alle €/kWh) aus der Konfiguration.

    Ohne Angabe gilt ``fallback`` (Einspeisung plus grid_fee, wie bei
    Harald). Die Fensterpreise entstehen nur, wenn der Haken gesetzt ist
    UND eine Netzgebühr mit dem jeweiligen Satz da ist — ohne Netzanteil
    gibt es nichts zu rabattieren, und eine Reihe, die nichts unterscheidet,
    muss das Modell nicht tragen. Der WiNAP kommt erst mit der Verordnung
    ab 2027 (Spalte in der Tabelle); bis dahin bleibt er None.
    """
    energie = _preis_oder_none(config.get(CONF_SCHEDULE_ENERGY_PRICE))
    bezug = bezugspreis_gesamt(config, netz)
    if bezug is None:
        bezug = fallback
    snap = winap = None
    if energie is not None and netz is not None and config.get(CONF_SCHEDULE_SNAP_ENABLED):
        if netz.snap is not None and netz.snap < netz.ap:
            snap = energie + netz.snap
        if netz.winap is not None and netz.winap < netz.ap:
            winap = energie + netz.winap
    return bezug, snap, winap


def max_min_soc_pct(config: dict) -> float:
    """Wie hoch der Mindest-Ladestand an DIESER Anlage sein darf.

    Der Deckel abzüglich des nutzbaren Bandes: Bei Deckel 100 (Vorgabe) sind
    das 80 %, beim tiefsten erlaubten Deckel 70 % noch 50 %. Die Grenze soll
    nur verhindern, dass Boden und Deckel sich kreuzen — wie viel Reserve
    sinnvoll ist, entscheidet die Anlagengröße, nicht die Integration.
    """
    return max(0.0, _max_soc_pct(config) - SOC_BAND_MIN_PCT)


def _min_soc_pct(config: dict) -> float:
    """Mindest-Ladestand in Prozent — 0 heißt „bis leer planen erlaubt".

    Eine 0 ist eine Aussage, nur ein fehlender oder unlesbarer Wert nimmt die
    Vorgabe. Gekappt bei ``max_min_soc_pct()``, also am eingestellten Deckel.
    """
    raw = config.get(CONF_SCHEDULE_MIN_SOC_PCT)
    if raw is None or raw == "":
        return DEFAULT_MIN_SOC_PCT
    try:
        return max(0.0, min(max_min_soc_pct(config), float(raw)))
    except (TypeError, ValueError):
        return DEFAULT_MIN_SOC_PCT


def _sicherheitspuffer_pct(config: dict) -> float:
    """Sicherheitspuffer in Prozent — 0 heißt „Prognose unverändert".

    Gekappt bei ``MAX_SICHERHEITSPUFFER_PCT``; ein unlesbarer Wert nimmt die
    Vorgabe, damit ein Tippfehler in der Konfiguration nicht den ganzen
    Fahrplan verzieht.
    """
    raw = config.get(CONF_SCHEDULE_SICHERHEITSPUFFER_PCT)
    if raw is None or raw == "":
        return DEFAULT_SICHERHEITSPUFFER_PCT
    try:
        return max(0.0, min(MAX_SICHERHEITSPUFFER_PCT, float(raw)))
    except (TypeError, ValueError):
        return DEFAULT_SICHERHEITSPUFFER_PCT


def _puffer_anwenden(
    puffer_pct: float,
    consumption: list[float],
    production: list[float],
    min_production: list[float] | None,
) -> tuple[list[float], list[float], list[float] | None]:
    """Verbrauch anheben, Erzeugung absenken — beides um denselben Anteil.

    Wirkt ausschließlich auf die PROGNOSE. Der erste Stützpunkt wird beim
    Aufrufer anschließend mit den Messwerten überschrieben und bleibt damit
    unangetastet: Ein Sicherheitsaufschlag auf eine Messung wäre keine
    Vorsicht, sondern ein Fehler — der gefahrene Slot ist der einzige, über
    den es nichts zu mutmaßen gibt.

    ``min_production`` (Solcasts p10-Pfad) sinkt mit. Bliebe er stehen,
    läge die Untergrenze der Erzeugung über ihrem Erwartungswert.
    """
    if puffer_pct <= 0:
        return consumption, production, min_production
    hoch = 1.0 + puffer_pct / 100.0
    runter = 1.0 - puffer_pct / 100.0
    return (
        [round(v * hoch, 4) for v in consumption],
        [round(v * runter, 4) for v in production],
        None if min_production is None else [round(v * runter, 4) for v in min_production],
    )


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


def _batteriewerte_mit_puffer(
    data: dict,
    soc: float | None,
    capacity: float | None,
    now: datetime,
) -> tuple[float | None, float | None, float | None]:
    """Überbrückt kurze Sensorausfälle mit dem zuletzt gelesenen Paar.

    Rückgabe: ``(soc, capacity, alter_s)`` — ``alter_s`` ist None, solange
    die Sensoren selbst antworten, und sonst das Alter der benutzten Werte.

    Gepuffert wird nur ein **vollständiges** Paar: Ladestand und Kapazität
    stammen dann aus demselben Augenblick, und ``battery_free`` (Kapazität ×
    freier Anteil) bleibt in sich stimmig. Fehlt beim nächsten Lauf nur einer
    von beiden, wird auch nur dieser ergänzt.

    Der Puffer lebt in ``hass.data`` und stirbt mit dem Neuladen der
    Integration — nach einem Neustart wird also nicht aus einer alten
    Sitzung weitergerechnet. Ein Zeitsprung rückwärts (negatives Alter)
    verwirft ihn ebenfalls.
    """
    if soc is not None and capacity:
        data["batterie_puffer"] = {
            "soc": float(soc),
            "capacity": float(capacity),
            "zeit": now,
        }
        return soc, capacity, None

    puffer = data.get("batterie_puffer")
    if not puffer:
        return soc, capacity, None

    alter = (now - puffer["zeit"]).total_seconds()
    if alter < 0 or alter > BATTERIE_PUFFER_MAX_S:
        return soc, capacity, None

    if soc is None:
        soc = puffer["soc"]
    if not capacity:
        capacity = puffer["capacity"]
    return soc, capacity, alter


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


def _solcast_entity_ids(
    hass: HomeAssistant, config: dict[str, Any]
) -> set[str] | None:
    """Die Prognose-Entities DIESER Anlage — None heißt „nicht zuzuordnen".

    Anker sind die im Assistenten gewählten Prognose-Sensoren: Sie stehen in
    der Konfiguration, es muss also kein Entity-Name erraten werden. Über die
    Entity-Registry führt der Sensor zu seinem Config-Entry und damit zu allen
    Tagessensoren derselben Solcast-Installation.

    Nötig, sobald mehr als eine Solcast-Integration in der Instanz läuft
    (zweite Anlage, zweites Konto): Deren Tagessensoren tragen dasselbe
    Zeitraster, und ohne Zuordnung gewinnt schlicht der zuletzt gelesene —
    der Fahrplan führe dann die fremde Anlage.
    """
    try:
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(hass)
    except Exception:  # noqa: BLE001 — ohne Registry bleibt die Attributsuche
        return None
    for key in (CONF_FORECAST_TOMORROW_ENTITY, CONF_FORECAST_REMAINING_ENTITY):
        entity_id = str(config.get(key) or "").strip()
        if not entity_id:
            continue
        try:
            eintrag = registry.async_get(entity_id)
            if eintrag is None or not eintrag.config_entry_id:
                continue
            geschwister = er.async_entries_for_config_entry(
                registry, eintrag.config_entry_id
            )
        except Exception:  # noqa: BLE001
            continue
        ids = {e.entity_id for e in geschwister}
        if ids:
            return ids
    return None


def _solcast_detailed(
    hass: HomeAssistant, config: dict[str, Any] | None = None
) -> dict[datetime, tuple[float, float]]:
    """Halbstundenwerte aus den Solcast-Tagessensoren sammeln.

    Solcast hängt an jeden Tagessensor ein Attribut ``detailedForecast`` mit
    48 Einträgen der Form ``{period_start, pv_estimate, pv_estimate10,
    pv_estimate90}`` — Leistung in kW. Über sieben Tagessensoren ergibt das
    eine Woche Vorausschau samt Worst-Case-Pfad.

    Gelesen werden nur die Sensoren der eigenen Solcast-Installation
    (``_solcast_entity_ids``). Lässt sie sich nicht bestimmen, bleibt die
    Suche über das Attribut — sie kommt ohne Entity-Namen aus, die ja
    lokalisiert sind (``prognose_heute`` gegen ``forecast_today``). Fallen
    dabei zwei Quellen auf denselben Zeitpunkt, steht das als Warnung im
    Protokoll: Dann ist die Reihe nicht mehr eindeutig.
    """
    erlaubt = _solcast_entity_ids(hass, config or {})
    werte: dict[datetime, tuple[float, float]] = {}
    herkunft: dict[datetime, str] = {}
    fremde: set[str] = set()
    for state in hass.states.async_all("sensor"):
        detailed = state.attributes.get("detailedForecast")
        if not isinstance(detailed, list):
            continue
        if erlaubt is not None and state.entity_id not in erlaubt:
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
            alt = herkunft.get(stamp)
            if alt is not None and alt != state.entity_id:
                fremde.add(state.entity_id)
                fremde.add(alt)
            werte[stamp] = (erwartung, p10)
            herkunft[stamp] = state.entity_id
    if fremde:
        _LOGGER.warning(
            "PV-Prognose: mehrere Quellen liefern denselben Zeitraum (%s) — "
            "die Reihe ist nicht eindeutig. Prüfe im Assistenten, ob die "
            "gewählten Prognose-Sensoren zu dieser Anlage gehören.",
            ", ".join(sorted(fremde)),
        )
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
    # Feste Abnahmequote statt Prognose: dann braucht es keinen Saldo, der
    # Mischpreis kommt aus der Konfiguration (eeg_price.quoten_aufschlag_reihe).
    quotenmodus = eeg_price.bedarfsquelle(config) == eeg_price.DEMAND_SOURCE_QUOTE
    if not gemeinschaften or (bedarf is None and not quotenmodus):
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

    if quotenmodus:
        return eeg_price.quoten_aufschlag_reihe(
            gemeinschaften, stamps, basis, ist_nacht_eeg
        )
    return eeg_price.aufschlag_reihe(
        gemeinschaften, bedarf or {}, stamps, basis, ist_nacht_eeg
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

    # Auf das Slot-Raster abgerundet, nicht auf die Minute.
    #
    # Bis 2.1.1-dev35 stand hier die laufende Minute, mit der Begründung, der
    # Ladestand solle "nowish" gelten und nicht bis zu eine Viertelstunde in
    # der Vergangenheit. Die Absicht war richtig, sie wurde nur nie wirksam:
    # ``opt()`` resampled alle Reihen auf ``time_res`` (15 min), und ein
    # Stützpunkt neben diesem Raster fällt dabei heraus. Der erste LP-Slot
    # bekam seinen Wert dann per Rückwärtsfüllung aus dem nächsten
    # Rasterpunkt — also aus dem Verbrauchsprofil statt aus der Messung, die
    # zwanzig Zeilen weiter unten eigens dafür gesetzt wird.
    #
    # Getroffen hat es 14 von 15 Läufen: Nur wer zufällig auf :00, :15, :30
    # oder :45 startete, plante den gefahrenen Slot mit dem gemessenen
    # Hausverbrauch (Anlage Traun, 20.09.2026: real 9-12 kW am Abend gegen
    # 2,6 kW aus dem Profil). Sichtbar wurde es daran, dass von 29 Läufen mit
    # einer Hauslast über der AC-Grenze genau die zwei am Solver scheiterten,
    # die auf der Viertelstunde lagen.
    #
    # Das Raster ist außerdem versionsfest: pandas 3 behält den Stützpunkt
    # neben dem Raster, pandas 2.3 (der Stand im HA-Container) verwirft ihn.
    # Liegt start auf dem Raster, rechnen beide gleich — und der erste Slot
    # des Plans lag ohnehin schon immer auf der Viertelstunde, denn der
    # Ergebnisindex kommt aus genau diesem Resample.
    now = _now_local()
    start = now.replace(second=0, microsecond=0)
    start -= timedelta(minutes=start.minute % time_res_min)

    source = str(
        config.get(CONF_FORECAST_SOURCE, FORECAST_SOURCE_SOLCAST)
        or FORECAST_SOURCE_SOLCAST
    ).lower()

    # Die Prognose kommt vor dem Zeitraster, denn sie bestimmt, wie weit
    # überhaupt geplant werden darf.
    # Erste Wahl: Solcast-Halbstundenwerte, die bringen einen echten p10 mit.
    detailed = _solcast_detailed(hass, config)
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

    # Sicherheitspuffer auf die Prognose, bevor die Messung den ersten
    # Stützpunkt übernimmt: Verbrauch hoch, Erzeugung runter. Vorgabe 0,
    # dann passiert hier nichts (siehe CONF_SCHEDULE_SICHERHEITSPUFFER_PCT).
    puffer_pct = _sicherheitspuffer_pct(config)
    consumption, production, min_production = _puffer_anwenden(
        puffer_pct, consumption, production, min_production
    )

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
    # True melden (Huawei Master/Slave), warf das
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

    # Kurze Aussetzer der Wechselrichter-Verbindung überbrücken: Ladestand
    # und Kapazität fallen zusammen aus (beide hängen am selben Modbus-
    # Koordinator), und ohne Puffer fiel dann der ganze Planlauf aus.
    soc, capacity, puffer_alter = _batteriewerte_mit_puffer(data, soc, capacity, now)
    if puffer_alter is not None:
        _LOGGER.info(
            "Batteriewerte aus dem Puffer (%.0f s alt) — Ladestand oder "
            "Kapazität gerade nicht lesbar",
            puffer_alter,
        )

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
    # Zwei Spielarten: der zuletzt VERÖFFENTLICHTE Monat (läuft dem laufenden
    # immer einen Monat hinterher) oder die HOCHRECHNUNG des laufenden Monats
    # (oemag_schaetzung.py). Ohne Hochrechnung gilt auch dort der
    # veröffentlichte Wert, ohne den die Handeingabe.
    quelle_basis = str(
        config.get(CONF_SCHEDULE_FEEDIN_SOURCE, DEFAULT_SCHEDULE_FEEDIN_SOURCE)
        or DEFAULT_SCHEDULE_FEEDIN_SOURCE
    ).lower()
    if quelle_basis in (FEEDIN_SOURCE_OEMAG, FEEDIN_SOURCE_OEMAG_ESTIMATE):
        # Die OeMAG kennt keinen Nachtsatz — ein gespeicherter Wert aus der
        # Handeingabe würde Tag und Nacht aus verschiedenen Quellen mischen.
        feedin_nacht = None
        oemag_preis = None
        if quelle_basis == FEEDIN_SOURCE_OEMAG_ESTIMATE:
            schaetzer = data.get("oemag_schaetzung")
            oemag_preis = schaetzer.preis if schaetzer is not None else None
            if not oemag_preis:
                _LOGGER.debug(
                    "OeMAG-Hochrechnung nicht verfügbar, es gilt der veröffentlichte Monat"
                )
        if not oemag_preis:
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

    # Fester Monatstarif aWATTar SUNNY (awattar_sunny.py): wie die OeMAG ein
    # Skalar ohne Tagesstruktur und ohne Nachtsatz. Die Vertragsvariante
    # kommt aus der Konfiguration und wird bei jeder Abfrage übergeben — so
    # wirkt ein Wechsel in den Einstellungen sofort, ohne Neuaufbau des
    # Anbieters. Ohne Wert gilt auch hier die Handeingabe.
    if quelle_basis == FEEDIN_SOURCE_AWATTAR_SUNNY:
        feedin_nacht = None
        sunny = data.get("awattar_sunny")
        vertrag = str(
            config.get(CONF_AWATTAR_SUNNY_VERTRAG) or DEFAULT_AWATTAR_SUNNY_VERTRAG
        ).lower()
        sunny_preis = sunny.preis_fuer(vertrag) if sunny is not None else None
        if sunny_preis is not None:
            feedin_tag = float(sunny_preis)
        else:
            _LOGGER.debug(
                "aWATTar-SUNNY-Tarif nicht verfügbar, es gilt die Handeingabe (%.5f €/kWh)",
                feedin_tag,
            )

    # Energie AG „Team Sonne Float" (energie_ag.py): Referenzmarktwert
    # Photovoltaik § 13 EAG minus Abschlag — wie die OeMAG ein Monatswert
    # ohne Tagesstruktur und ohne Nachtsatz. Variante und Abschlag kommen bei
    # jeder Abfrage aus der Konfiguration, damit eine Änderung in den
    # Einstellungen sofort wirkt. Zwei Spielarten wie bei der OeMAG: der
    # zuletzt VERÖFFENTLICHTE Monat oder die HOCHRECHNUNG des laufenden
    # (derselbe Schätzer, dessen Rohwert genau dieser Referenzmarktwert ist).
    if quelle_basis in (FEEDIN_SOURCE_ENERGIE_AG, FEEDIN_SOURCE_ENERGIE_AG_ESTIMATE):
        feedin_nacht = None
        energie_ag = data.get("energie_ag")
        variante = str(
            config.get(CONF_ENERGIE_AG_VARIANTE) or DEFAULT_ENERGIE_AG_VARIANTE
        ).lower()
        abschlag = config.get(CONF_ENERGIE_AG_ABSCHLAG)
        eag_preis = None
        if quelle_basis == FEEDIN_SOURCE_ENERGIE_AG_ESTIMATE and energie_ag is not None:
            eag_preis = energie_ag.preis_geschaetzt(variante, abschlag)
            if eag_preis is None:
                _LOGGER.debug(
                    "Energie-AG-Hochrechnung nicht verfügbar, es gilt der "
                    "veröffentlichte Monat"
                )
        if eag_preis is None and energie_ag is not None:
            eag_preis = energie_ag.preis_fuer(variante, abschlag)
        if eag_preis is not None:
            feedin_tag = float(eag_preis)
        else:
            _LOGGER.debug(
                "Energie-AG-Tarif nicht verfügbar, es gilt die Handeingabe (%.5f €/kWh)",
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
            try:
                fee_pct = float(config.get(CONF_SPOT_FEEDIN_FEE_PCT) or 0) / 100.0
            except (TypeError, ValueError):
                fee_pct = 0.0
            # Der Prozentabschlag geht vom BETRAG ab (aWATTar SUNNY Spot
            # 60min: 19 % auf |Preis|) — bei negativem Börsenpreis wird die
            # Einspeisung dadurch noch teurer, genau wie im Tarif.
            feedin_reihe = [p - fee - abs(p) * fee_pct for p in roh_reihe]
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
    # Bezugspreis = Arbeitspreis + Netzgebühr; im SNAP- (und ab 2027 im
    # WiNAP-)Fenster gilt der verbilligte Netzsatz. Die Netzgebühr kommt
    # aus der Verordnung (Netzbereich, RIS-Tabelle des Providers) oder von
    # Hand. Ohne Angabe wie bei Harald Einspeisung plus grid_fee.
    netz_provider = data.get("netzentgelt")
    netz = netzgebuehr_fuer(
        config, netz_provider.tabelle if netz_provider is not None else None
    )
    bezug, bezug_snap, bezug_winap = bezugspreise_aus_config(
        config,
        feedin_tag + float(config.get(CONF_SCHEDULE_GRID_FEE, DEFAULT_GRID_FEE)),
        netz,
    )

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
    # Mit fester Abnahmequote gibt es keine Bedarfsprognose — PeakShare wird
    # dann gar nicht gefragt (die Gemeinschaft ist dort ohnehin unbekannt).
    if eeg_price.bedarfsquelle(config) == eeg_price.DEMAND_SOURCE_QUOTE:
        eeg_bedarf = None
    else:
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
        consumption_price_snap=bezug_snap,
        consumption_price_winap=bezug_winap,
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
        heizstab_max_kw=heizstab_max_kw(config),
        heizstab_waermewert=heizstab_waermewert(config),
        # Aufnahmefähigkeit des Puffers — bei jedem Lauf frisch aus der
        # gemessenen Temperatur. Sie schrumpft, während der Puffer warm
        # wird, und gibt damit die Abendentladung von selbst wieder frei.
        heizstab_budget_kwh=float(
            getattr(data.get("heizstab"), "puffer_budget_kwh", 0.0) or 0.0
        ),
        heizstab_puffer_liter=float(
            getattr(data.get("heizstab"), "puffer_liter", 0.0) or 0.0
        ),
        heizstab_temp_c=getattr(data.get("heizstab"), "temperatur_c", None),
        heizstab_maxtemp_c=float(
            getattr(data.get("heizstab"), "maxtemp_c", 0.0) or 0.0
        ),
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


def _gewinn_slotzahl(inputs: ScheduleInputs, vorhanden: int) -> int:
    """Wie viele Slots in das Bewertungsfenster des Gewinns fallen.

    Mindestens einer, hoechstens alle — ein kurzer Horizont wird nicht
    kuenstlich verlaengert, ein langer nur bis GEWINN_HORIZONT_H bewertet.
    """
    if vorhanden <= 0:
        return 0
    res_s = int(getattr(inputs, "time_res_s", 0) or 0)
    if res_s <= 0:
        return vorhanden
    passt = int(round(GEWINN_HORIZONT_H * 3600.0 / res_s))
    return max(1, min(vorhanden, passt))


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
        # Geplante Heizstab-Leistung. Mit Wärmewert und Pufferbudget hat
        # das LP sie selbst bestimmt (Spalte ``heater``) — dann gilt dessen
        # Wert. Ohne beides bleibt es bei der Nachbearbeitung: Was abgeregelt
        # würde, nimmt der Heizstab bis zu seiner Maximalleistung.
        slot["heizstab"] = _heizstab_plan_kw(
            slot.get("discard"), inputs, row.get("heater")
        )
        slots.append(slot)
    # Puffertemperatur je Slot — Nachrechnung aus der geplanten Wärme, das
    # LP bleibt davon unberührt.
    _puffer_temperatur_verlauf(slots, inputs)

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
    if slots and slots[0].get("puffer_temp_c") is not None:
        # Nur mit Kurve: die Marke „Maximaltemperatur" im Ladestandsfeld und
        # der Startwert, von dem die Prognose ausgeht.
        result["puffer_maxtemp_c"] = inputs.heizstab_maxtemp_c
        result["puffer_temp_start_c"] = inputs.heizstab_temp_c

    # Gewinnberechnung: was bringt die Optimierung gegenüber dem
    # Standardbetrieb desselben Geräts? Ein Fehler hier darf den Fahrplan
    # nicht kosten — er ist die Steuerung, der Vergleich nur Anzeige.
    try:
        referenz = simuliere_standardbetrieb(
            slots, inputs, ziel_soc_pct=slots[-1]["soc"] if slots else None
        )
        result["referenz_slots"] = referenz
        # Bewertet wird nur das vordere Stueck des Horizonts (siehe
        # GEWINN_HORIZONT_H); gezeichnet wird weiterhin alles. Die Referenz
        # dafuer wird eigens gerechnet, mit dem Plan-Ladestand am Schnitt als
        # Ziel: Nur mit gleichem Endstand kuerzt sich der Randeffekt heraus
        # (siehe simuliere_standardbetrieb). Ein Schnitt durch die lange
        # Referenz haette ihn zurueckgeholt — sie steht dort typischerweise
        # voller da als der Plan.
        gewinn_slots = slots[:_gewinn_slotzahl(inputs, len(slots))]
        if gewinn_slots is slots or len(gewinn_slots) == len(slots):
            gewinn_referenz = referenz
        else:
            gewinn_referenz = simuliere_standardbetrieb(
                gewinn_slots, inputs, ziel_soc_pct=gewinn_slots[-1]["soc"]
            )
        mit = bewerte_geldfluesse(gewinn_slots, inputs)
        ohne = bewerte_geldfluesse(gewinn_referenz, inputs)
        result["gewinn"] = {
            "mit": mit,
            "ohne": ohne,
            "vorteil": round(mit["summe"] - ohne["summe"], 4),
            "horizont_h": round(len(gewinn_slots) * inputs.time_res_s / 3600.0, 1),
            # Womit der Endbestand bewertet wird — fürs ehrliche Beschriften.
            "endbestand_tarif": round(endbestand_satz(inputs), 5),
        }
    except Exception:  # noqa: BLE001 - Vergleich ist Anzeige, kein Aktor
        _LOGGER.exception("Gewinnberechnung fehlgeschlagen — der Fahrplan bleibt gültig")

    return result


# ---------------------------------------------------------------------------
# Gewinnberechnung (Executor): Standardbetrieb als Referenz, echte Geldflüsse
# ---------------------------------------------------------------------------


def _heizstab_plan_kw(
    discard_kw: float | None,
    inputs: ScheduleInputs,
    heater_dc_kw: float | None = None,
) -> float:
    """Heizstab-Leistung (AC, kW) für einen Slot.

    Zwei Wege, je nachdem, ob das LP den Heizstab eingeplant hat:

    * ``heater_dc_kw`` gesetzt — das Modell hat selbst entschieden, wie viel
      Wärme es sich leisten will (Wärmewert gegen Einspeisung, begrenzt durch
      das Pufferbudget). Dieser Wert gilt.
    * sonst — Nachbearbeitung wie bisher: Was abgeregelt würde, nimmt der
      Heizstab bis zu seiner Maximalleistung. Das ist auch der Weg für den
      Referenz-Fahrplan, der ohne LP auskommt.

    ``discard`` und ``heater`` sind DC-Leistung vor dem Wechselrichter; am
    Heizstab kommt sie hinter dem Wirkungsgrad an. Ohne Heizstab 0.
    """
    if inputs.heizstab_max_kw <= 0:
        return 0.0
    if heater_dc_kw is not None and heater_dc_kw > 0:
        return round(
            min(float(heater_dc_kw) * HAConfig.ac_efficiency, inputs.heizstab_max_kw), 4
        )
    if discard_kw is None or discard_kw <= 0:
        return 0.0
    return round(min(discard_kw * HAConfig.ac_efficiency, inputs.heizstab_max_kw), 4)


def _puffer_temperatur_verlauf(
    slots: list[dict[str, Any]], inputs: ScheduleInputs
) -> None:
    """Prognose der Puffertemperatur je Slot — Anzeige, kein Teil des LP.

    Das Modell kennt keinen Wärmezustand, nur die Heizstab-Leistung je Slot
    und das Tagesbudget in kWh. Die Temperatur ist daraus abgeleitet:
    gemessene Temperatur beim Lauf, fortgeschrieben mit der geplanten
    Wärme je Slot, gedeckelt an der Maximaltemperatur. Zwei Verluste machen
    die Kurve bewusst konservativ:

    * Heizen rechnet mit der EFFEKTIVEN Wärmekapazität (Wasser plus
      HEIZSTAB_WAERMEVERLUST_PCT) — dieselbe Zahl wie das Pufferbudget des
      Controllers. Deshalb erreicht die Kurve die Maximaltemperatur genau
      dann, wenn das Budget verheizt ist, und nicht früher.
    * Ein fester Bereitschaftsverlust an die Umgebung
      (PUFFER_BEREITSCHAFTSVERLUST_KW) zieht sie jede Viertelstunde ein
      wenig herunter — über Nacht sichtbar, nie unter PUFFER_UMGEBUNG_C.
      Dieser Verlust ist reine Wasserwärme, die den Puffer verlässt, und
      rechnet deshalb mit der physikalischen Konstante.

    Nicht abgebildet ist die Zapfung (Warmwasser, Heizkreis) — das steht
    so auch in der Beschriftung. Ohne Volumen, Temperatur oder Heizstab
    bleibt der Schlüssel weg; das Panel zeichnet die Kurve nur, wenn er da
    ist. Liegt die gemessene Temperatur schon über der Maximaltemperatur,
    springt die Kurve nicht auf den Deckel — der gilt erst fürs Heizen.
    """
    liter = float(inputs.heizstab_puffer_liter or 0.0)
    temp = inputs.heizstab_temp_c
    maxtemp = float(inputs.heizstab_maxtemp_c or 0.0)
    if inputs.heizstab_max_kw <= 0 or liter <= 0 or temp is None or maxtemp <= 0:
        return
    dt_h = inputs.time_res_s / 3600.0
    k_heiz = 1000.0 / (liter * PUFFER_WH_PRO_LITER_KELVIN_EFFEKTIV)   # K je kWh am Heizstab
    k_verlust = 1000.0 / (liter * WASSER_WH_PRO_LITER_KELVIN)          # K je kWh Verlust
    verlust_k = PUFFER_BEREITSCHAFTSVERLUST_KW * dt_h * k_verlust
    t = float(temp)
    deckel = max(maxtemp, t)
    for slot in slots:
        heiz_kwh = max(0.0, float(slot.get("heizstab") or 0.0)) * dt_h
        t = min(deckel, t + heiz_kwh * k_heiz)
        # Bereitschaftsverlust — aber nie unter die Umgebung, und ein Puffer,
        # der schon kälter ist, wird davon nicht noch kälter gerechnet.
        t = max(t - verlust_k, min(t, PUFFER_UMGEBUNG_C))
        slot["puffer_temp_c"] = round(t, 1)


def _batterie_verluste_kw(p_kw: float, kapazitaet_kwh: float) -> float:
    """Innenwiderstandsverluste in kW zu einer Batterieleistung — Haralds Modell.

    ``opt_highs.py`` kann keinen echten Innenwiderstand (quadratisch) linear
    abbilden und nimmt zwei Stufen: bis 0,1 C ist (Ent-)Laden verlustfrei,
    zwischen 0,1 C und 0,2 C gehen ``battery_resistance`` (4 %) der Leistung
    darüber verloren, oberhalb von 0,2 C das Doppelte. „C" ist die Kapazität
    in kWh als Leistung in kW gelesen. Die Verluste gehen vom DC-Bus ab, die
    Batterie selbst sieht die volle Leistung — beim Laden kommt also weniger
    von der PV an, beim Entladen weniger beim Haus. Richtung ist egal.
    """
    r = HAConfig.battery_resistance
    stufe = kapazitaet_kwh / 10.0
    p = abs(p_kw)
    hoch1 = min(max(p - stufe, 0.0), stufe)
    hoch2 = max(p - 2.0 * stufe, 0.0)
    return r * hoch1 + 2.0 * r * hoch2


def _ladeleistung_aus_dc(angebot_kw: float, kapazitaet_kwh: float) -> float:
    """Umkehrung von ``_batterie_verluste_kw`` fürs Laden: die Batterieleistung
    p, für die ``p + Verluste(p)`` genau das DC-Angebot ausschöpft."""
    r = HAConfig.battery_resistance
    stufe = kapazitaet_kwh / 10.0
    if angebot_kw <= stufe:
        return max(angebot_kw, 0.0)
    if angebot_kw <= stufe * (2.0 + r):          # bis p = 0,2 C
        return (angebot_kw + r * stufe) / (1.0 + r)
    return (angebot_kw + 3.0 * r * stufe) / (1.0 + 2.0 * r)


def _entladeleistung_fuer_dc(bedarf_kw: float, kapazitaet_kwh: float) -> float:
    """Umkehrung fürs Entladen: die Batterieleistung p, für die
    ``p − Verluste(p)`` genau den DC-Bedarf deckt."""
    r = HAConfig.battery_resistance
    stufe = kapazitaet_kwh / 10.0
    if bedarf_kw <= stufe:
        return max(bedarf_kw, 0.0)
    if bedarf_kw <= stufe * (2.0 - r):           # bis p = 0,2 C
        return (bedarf_kw - r * stufe) / (1.0 - r)
    return (bedarf_kw - 3.0 * r * stufe) / (1.0 - 2.0 * r)


def _reservekurve(
    slots: list[dict[str, Any]],
    inputs: ScheduleInputs,
    ziel_inhalt: float,
    boden: float,
    deckel: float,
    dt_h: float,
) -> list[float]:
    """Mindest-Ladestand je Slot, damit am Ende ``ziel_inhalt`` im Speicher steht.

    Rückwärts gerechnet: im letzten Slot gilt das Ziel, in jedem Slot davor
    abzüglich dessen, was dieser Slot selbst noch aus PV-Überschuss nachladen
    könnte. Solange die Sonne den Endstand liefern kann, bleibt die Kurve
    deshalb auf dem Boden — die Referenz entlädt dann so frei wie bisher, und
    die Auflage greift erst dort, wo auch das LP einfriert: in der letzten
    Nacht vor dem Horizontende.

    Die Vorausschau macht die Referenz NICHT klüger: sie darf damit nur die
    Auflage erfüllen, nicht besser wirtschaften. Entladen wird weiterhin
    stumpf gegen die Hauslast, nie gegen einen Preis.
    """
    eff = HAConfig.ac_efficiency
    kapazitaet = inputs.battery_capacity_kwh
    reserve = [boden] * len(slots)
    bedarf = min(ziel_inhalt, deckel)
    for index in range(len(slots) - 1, -1, -1):
        reserve[index] = max(boden, bedarf)
        pv = slots[index].get("PV") or 0.0
        verbrauch = slots[index].get("consumption") or 0.0
        ueberschuss = max(0.0, pv - verbrauch / eff)
        bedarf -= (
            min(
                _ladeleistung_aus_dc(ueberschuss, kapazitaet),
                inputs.battery_power_limit_kw,
            )
            * dt_h
        )
        if bedarf <= boden:
            # Weiter vorne bindet die Auflage nicht mehr — der Rest der Kurve
            # steht schon auf dem Boden.
            break
    return reserve


def _deckelkurve(
    slots: list[dict[str, Any]],
    inputs: ScheduleInputs,
    ziel_inhalt: float,
    deckel: float,
    dt_h: float,
) -> list[float]:
    """Höchst-Ladestand je Slot, damit am Ende nicht MEHR als ``ziel_inhalt`` steht.

    Das Gegenstück zu ``_reservekurve`` und aus demselben Grund nötig: die
    Endauflage des LP ist eine Gleichung, keine Untergrenze. Der Fahrplan darf
    am Horizontende nicht voller sein als vorgegeben und verkauft alles
    darüber; die Referenz ohne diese Grenze lädt mit voller Leistung weiter
    und endet — gemessen an der Testanlage am 08.09. mittags — bei 98 %
    gegen 57 %. Dann kippt die Schieflage nur auf die andere Seite: die
    Referenz bekommt 9,56 kWh gutgeschrieben, die der Fahrplan zum
    Einspeisetarif abgegeben hat.

    Rückwärts wie die Reservekurve, nur mit umgekehrtem Vorzeichen: was ein
    Slot noch gegen die Hauslast entladen kann, durfte davor mehr im Speicher
    liegen. Reicht die Hauslast bis zum Ende, um vom Ladedeckel auf das Ziel
    zu kommen, bindet die Grenze gar nicht.
    """
    eff = HAConfig.ac_efficiency
    kapazitaet = inputs.battery_capacity_kwh
    grenze = [deckel] * len(slots)
    erlaubt = ziel_inhalt
    for index in range(len(slots) - 1, -1, -1):
        grenze[index] = min(deckel, erlaubt)
        pv = slots[index].get("PV") or 0.0
        verbrauch = slots[index].get("consumption") or 0.0
        defizit = max(0.0, verbrauch / eff - pv)
        erlaubt += (
            min(
                _entladeleistung_fuer_dc(defizit, kapazitaet),
                inputs.battery_power_limit_kw,
            )
            * dt_h
        )
        if erlaubt >= deckel:
            # Weiter vorne bindet die Grenze nicht mehr — der Rest der Kurve
            # steht schon auf dem Ladedeckel.
            break
    return grenze


def simuliere_standardbetrieb(
    slots: list[dict[str, Any]],
    inputs: ScheduleInputs,
    ziel_soc_pct: float | None = None,
) -> list[dict[str, Any]]:
    """Was ein Standard-Wechselrichter aus denselben Prognosen machen würde.

    Eigenverbrauchs-Logik, wie sie jedes Gerät ab Werk fährt: PV-Überschuss
    lädt zuerst die Batterie, erst der Rest wird eingespeist; ein Defizit
    entlädt die Batterie bis zum Mindest-Ladestand, erst der Rest kommt aus
    dem Netz. Eine einfache Slot-Schleife über die Spalten des gerechneten
    Fahrplans (PV, consumption) — bewusst KEIN zweiter LP-Lauf: das
    Standardgerät schaut nicht voraus, genau das ist der Unterschied.

    Dieselbe Physik wie im Modell, in allen drei Punkten:

    * PV und Batterie sind DC, Hauslast und Netz AC, dazwischen liegt
      ``ac_efficiency``. Ohne den Wirkungsgrad bekäme die Referenz 5 % mehr
      Energie, als das Modell je liefern kann.
    * Innenwiderstand nach Haralds Stufenmodell (``_batterie_verluste_kw``):
      über 0,1 C kostet (Ent-)Laden Verluste. Der Fahrplan lädt deshalb
      gern langsam; die Referenz lädt mit dem vollen Überschuss und zahlt
      dafür — ohne diese Verluste bekam sie bis zu 8 % geschenkt, und der
      ausgewiesene Vorteil war um genau das zu klein.
    * Der Ladedeckel ``max_soc_pct`` gilt auch hier. Er ist eine Vorgabe des
      Nutzers zum Schutz der Batterie, keine Entscheidung des Fahrplans —
      hielte nur der Fahrplan ihn ein, würde ihm angelastet, was der Nutzer
      gewollt hat. Steht die Batterie schon darüber, lädt sie nicht weiter
      (und wird nicht entladen, wie im Modell). Mit 100 % ist es „bis voll".

    Vorzeichen wie im Fahrplan (Haralds Konvention): ``grid_p`` positiv =
    Einspeisung, ``battery_p`` positiv = Entladen; ``battery_p`` ist die
    Leistung an der Batterie, die Verluste liegen davor auf dem DC-Bus.

    ``ziel_soc_pct`` ist die vierte gemeinsame Grenze: der Ladestand, den der
    Fahrplan am Horizontende erreicht. Das LP hat dort keine Wahl
    (``battery_free[-1]`` ist in ``opt_highs.py`` eine Konstante, keine
    Variable) — und zwar eine GLEICHUNG, weshalb die Auflage in beide
    Richtungen gilt: ``_reservekurve`` verhindert, dass die Referenz darunter
    endet, ``_deckelkurve``, dass sie darüber endet. Ohne die Untergrenze
    fährt sie die Batterie leer, während der Fahrplan die letzte Nacht aus dem
    Netz deckt, weil er die Reserve halten muss (1,33 von 1,73 Euro
    ausgewiesenem „Verlust" an der Testanlage); ohne die Obergrenze hortet sie
    bis 98 %, während der Fahrplan alles über seiner Vorgabe verkauft
    (0,72 Euro in die andere Richtung). Beides ist derselbe Randeffekt, und
    nur mit gleichem Endstand kürzt er sich vollständig heraus — dann ist die
    Endbestands-Gutschrift auf beiden Seiten gleich groß, unabhängig davon,
    mit welchem Satz sie bewertet wird.

    Beide Kurven binden so spät wie möglich, greifen also nur am Horizontrand.
    Ohne Angabe bleibt es beim bisherigen Verhalten (Tagesbilanz: dort ist der
    Endstand gemessen und nicht erzwungen).
    """
    dt_h = inputs.time_res_s / 3600.0
    eff = HAConfig.ac_efficiency
    kapazitaet = inputs.battery_capacity_kwh
    inhalt = kapazitaet * max(0.0, min(100.0, inputs.soc_pct or 0.0)) / 100.0
    boden = kapazitaet * max(0.0, min(100.0, inputs.min_soc_pct)) / 100.0
    deckel = kapazitaet * max(0.0, min(100.0, inputs.max_soc_pct)) / 100.0

    reserve = [boden] * len(slots)
    obergrenze = [deckel] * len(slots)
    if ziel_soc_pct is not None:
        ziel_inhalt = kapazitaet * max(0.0, min(100.0, ziel_soc_pct)) / 100.0
        if ziel_inhalt > boden:
            reserve = _reservekurve(slots, inputs, ziel_inhalt, boden, deckel, dt_h)
        if ziel_inhalt < deckel:
            obergrenze = _deckelkurve(slots, inputs, ziel_inhalt, deckel, dt_h)

    referenz: list[dict[str, Any]] = []
    for index, slot in enumerate(slots):
        pv = slot.get("PV") or 0.0
        verbrauch = slot.get("consumption") or 0.0
        # DC-Leistung, die die Hauslast hinter dem Wirkungsgrad deckt.
        bedarf_dc = verbrauch / eff
        if pv >= bedarf_dc:
            ueberschuss = pv - bedarf_dc
            # Der Überschuss muss Ladeleistung UND Verluste tragen.
            laden = min(
                _ladeleistung_aus_dc(ueberschuss, kapazitaet),
                inputs.battery_power_limit_kw,
                max(0.0, obergrenze[index] - inhalt) / dt_h,
            )
            verlust = _batterie_verluste_kw(laden, kapazitaet)
            # Was über die Einspeisegrenze hinausgeht, regelt das Gerät ab —
            # dieselbe Schranke wie im LP (feedin_limit, AC-Grenze abzüglich
            # Hauslast).
            grenze = max(
                0.0, min(inputs.feedin_limit_kw, inputs.ac_limit_kw - verbrauch)
            )
            export = min(max(0.0, ueberschuss - laden - verlust) * eff, grenze)
            inhalt += laden * dt_h
            batterie_p = -laden
            netz_p = export
            # Was weder Batterie noch Netz nehmen, regelt das Gerät ab — bei
            # einem Heizstab landet es dort (dieselbe Regel wie im Fahrplan).
            abgeregelt = max(0.0, ueberschuss - laden - verlust - export / eff)
        else:
            abgeregelt = 0.0
            defizit = bedarf_dc - pv
            # Die Batterie muss mehr liefern, als beim Haus ankommt.
            entladen = min(
                _entladeleistung_fuer_dc(defizit, kapazitaet),
                inputs.battery_power_limit_kw,
                max(0.0, inhalt - reserve[index]) / dt_h,
            )
            geliefert = entladen - _batterie_verluste_kw(entladen, kapazitaet)
            inhalt -= entladen * dt_h
            batterie_p = entladen
            netz_p = -max(0.0, defizit - geliefert) * eff
        referenz.append(
            {
                "t": slot["t"],
                "grid_p": round(netz_p, 4),
                "battery_p": round(batterie_p, 4),
                "soc": round(100.0 * inhalt / kapazitaet, 1),
                "discard": round(abgeregelt, 4),
                "heizstab": _heizstab_plan_kw(abgeregelt, inputs),
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


def endbestand_satz(inputs: ScheduleInputs) -> float:
    """Was eine am Ende gespeicherte Kilowattstunde wert ist, in €/kWh.

    Die Restenergie liegt noch DC in der Batterie. Bis sie Geld wird, geht
    der Wandlungsverlust ab (``ac_efficiency``), und ihre Entladung kostet
    dieselbe Alterung wie jede andere — genau so bewertet der Rest der
    Funktion jede gelieferte Kilowattstunde. Zum vollen Basistarif
    gutgeschrieben war die Referenz systematisch bevorteilt: sie lädt bis
    voll, endet meist voller als der Fahrplan, und die Differenz bekam sie
    verlustfrei angerechnet. An der Anlage Traun waren das um die 5 kWh
    Unterschied, also rund 7 Cent je Tag zu Lasten der Optimierung.

    Bewusst der Basistarif und nicht der Bezugspreis: ob die Energie später
    Bezug vermeidet oder eingespeist wird, weiß keiner — der kleinere Wert
    ist die ehrliche Untergrenze für beide Seiten gleichermaßen.
    """
    return inputs.feedin_price * HAConfig.ac_efficiency - inputs.battery_cost


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
    wie in der Preisfunktion). Einzige Ausnahme: die feste Abnahmequote
    (``quote_tag``/``quote_nacht`` im Tarif — Quotenmodus für Gemeinschaften
    ohne PeakShare, siehe eeg_price.py). Sie ist eine erklärte Annahme des
    Nutzers aus seiner EEG-Abrechnung, keine Prognose: aufgenommen wird dann
    Anteil × Quote der Einspeisung, Tag oder Nacht. Bewusst NICHT die interne Preisfunktion
    (``eeg_bonus`` samt Deckel, Boden und Normierung) — deren Gewichtung
    und Überschussabschlag sind Steuer-Fiktionen, hier zählt, was fließt.

    Dazu: Bezug = Netzbezug × Bezugspreis; Alterung = entladene Energie ×
    Alterungskosten (wie in Haralds Zielfunktion zählt die Entladung — so
    kostet jeder Zyklus einmal, nicht doppelt); Endbestands-Gutschrift =
    Restenergie über dem Mindest-Ladestand × ``endbestand_satz``: Basistarif
    abzüglich Wandlungsverlust und Alterung, siehe dort. Ohne die Gutschrift
    verglichen wir ungleiche Endzustände: die Pläne enden mit verschiedenem
    Ladestand, und Haralds Modell nagelt den letzten Slot ohnehin auf halbe
    Kapazität.

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
    # Wärme: was der Heizstab aufnimmt, bewertet mit dem konfigurierten
    # Wärmewert (heizstab/). Ohne Wärmewert zählt die Energie, aber kein Geld.
    heizstab_kwh = 0.0
    soc_ende: float | None = None
    for slot, basis in zip(slots, basis_je_slot):
        grid = slot.get("grid_p") or 0.0
        bat = slot.get("battery_p") or 0.0
        stamp = datetime.fromisoformat(slot["t"])
        heizstab_kwh += max(0.0, float(slot.get("heizstab") or 0.0)) * dt_h
        if grid > 0:
            export_kwh = grid * dt_h
            export_gesamt_kwh += export_kwh
            viertel = int(stamp.timestamp() // 900)
            eeg_nacht = _ist_im_nachtfenster(stamp.hour, eeg_von, eeg_bis)
            unzugeteilt = export_kwh
            for tarif in tarife:
                angeboten = tarif["anteil"] * export_kwh
                quote = tarif.get("quote_nacht" if eeg_nacht else "quote_tag")
                if quote is not None:
                    # Quotenmodus: die erklärte Abnahmequote statt des Saldos.
                    aufgenommen = angeboten * min(1.0, max(0.0, float(quote)))
                else:
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
            bezug += -grid * dt_h * bezugspreis_zu(inputs, stamp)
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
    satz = endbestand_satz(inputs)
    endbestand = rest_kwh * satz
    waerme = heizstab_kwh * max(
        0.0, float(getattr(inputs, "heizstab_waermewert", 0.0) or 0.0)
    )

    return {
        "erloes": round(erloes, 4),
        "bezug": round(bezug, 4),
        "alterung": round(alterung, 4),
        "endbestand": round(endbestand, 4),
        "endbestand_satz": round(satz, 5),
        "rest_kwh": round(rest_kwh, 2),
        # Wie viel der Einspeisung wirklich zum Gemeinschaftssatz vergütet
        # wurde — macht im Panel sichtbar, wo der Zeitvorteil herkommt.
        "eeg_kwh": round(eeg_kwh, 2),
        "export_kwh": round(export_gesamt_kwh, 2),
        # Heizstab: aufgenommene Energie und ihr Wert als Wärme.
        "heizstab_kwh": round(heizstab_kwh, 2),
        "waerme": round(waerme, 4),
        "summe": round(erloes - bezug - alterung + endbestand + waerme, 4),
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
