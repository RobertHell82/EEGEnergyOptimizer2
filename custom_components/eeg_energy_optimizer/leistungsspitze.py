"""Netzbezug je Viertelstunde und die Bezugsspitze des laufenden Monats.

Ab 1.1.2027 zahlt jeder Entnehmer auf Netzebene 7 einen Leistungspreis
(SNE-G-V § 6, Begutachtungsentwurf 30.06.2026): verrechnet wird der höchste
Viertelstunden-Mittelwert des Netzbezugs im Kalendermonat, kaufmännisch auf
zwei Nachkommastellen gerundet. Dieses Modul misst genau diese Größe mit —
es steuert nichts. Es ist die Grundlage, um überhaupt zu sehen, wann die
Spitzen entstehen und was sie kosten.

**Was gezählt wird.** Bezugs-ENERGIE je fester Viertelstunde (:00, :15,
:30, :45), geteilt durch 0,25 h. Einspeisung zählt nicht und verrechnet sich
auch nicht mit dem Bezug derselben Viertelstunde: Der Smart Meter führt
beide Richtungen in getrennten Registern. Deshalb wird ``max(0, −Netz)``
integriert, nicht die vorzeichenbehaftete Netzleistung gemittelt — zehn
Minuten Einspeisung würden eine Kochspitze sonst glattrechnen.

**Wie gezählt wird.** Jede Zustandsänderung des Netzsensors ist ein
Stützpunkt, dazu eine Abtastung alle ``ABTASTUNG_S``; zwischen zwei
Stützpunkten gilt der letzte Wert. Ein Zeitgeber genau auf der
Viertelstundengrenze schließt die Viertelstunde ab. Zwei Grenzen gegen
erfundene Energie:

* Ein nicht lesbarer Sensor zählt nichts, bis wieder ein Wert kommt.
* Ein Wert zählt nur, solange die Quelle ihn frisch schreibt
  (``last_reported`` jünger als ``HALTEN_MAX_S``), und ein Stützpunkt gilt
  höchstens so lange. Eine hängende Modbus-Verbindung lässt den letzten
  Zustand stehen, ohne ``unavailable`` zu melden — ohne diese Grenze würde
  eine eingefrorene Kochspitze zur Monatsspitze. Die Abtastung ist nötig,
  weil Home Assistant bei gleichem Wert keine Änderung meldet: Ohne sie
  wäre ein stehender Wert von einem eingefrorenen nicht zu unterscheiden.

Eine Viertelstunde mit Lücken ist eine UNTERGRENZE: Es fehlt Energie, keine
kommt hinzu. Sie zählt deshalb trotzdem zur Monatsspitze (was sie zeigt, war
mindestens so), trägt aber ``vollstaendig = False``.

**Genauigkeit.** Gemessen wird am Netzsensor des Wechselrichters, nicht am
Zähler des Netzbetreibers. Beide messen am selben Punkt und saldieren über
die Phasen; der Unterschied ist die Abtastung (Huawei ~5–30 s, Paar-Setups
im Minutentakt). Der Viertelstundenwert ist deshalb mit dem Smart-Meter-
Portal des Netzbetreibers direkt vergleichbar — das ist der Grund, warum
der Viertelstunden-Sensor den ABGESCHLOSSENEN Wert zeigt und nicht den
laufenden.

**Viertelstunden in UTC.** Die Grenzen werden in UTC abgerundet. Für den
DACH-Raum (ganzstündiger Versatz) liegen sie damit auf denselben
Wanduhr-Viertelstunden, und die doppelte Stunde im Herbst ergibt keine
Kollision. Nur die Zuordnung zum MONAT folgt der Ortszeit.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable

from .const import CONF_GRID_POWER_SENSOR, CONF_INVERTER_TYPE, DOMAIN

try:  # pragma: no cover - im Test nicht vorhanden
    from homeassistant.helpers.storage import Store
except ImportError:  # pragma: no cover
    Store = None  # type: ignore[assignment]

try:  # pragma: no cover - im Test nicht vorhanden
    from homeassistant.util import dt as dt_util
except ImportError:  # pragma: no cover
    dt_util = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

VIERTELSTUNDE = timedelta(minutes=15)
_VIERTELSTUNDE_S = 900.0
# Wie lange ein Messwert ohne Nachfolger gilt, und wie alt der Zustand der
# Quelle höchstens sein darf (siehe Moduldoku).
HALTEN_MAX_S = 120.0
# Abtastung zusätzlich zu den Zustandsänderungen.
ABTASTUNG_S = 10
# Ab diesem Anteil gemessener Zeit gilt eine Viertelstunde als vollständig.
# Nicht 100 %: Der erste Stützpunkt nach einem Neustart oder einer Lücke
# kommt nie genau auf der Grenze.
VOLLSTAENDIG_AB = 0.95
# Wie viele abgeschlossene Monate im Verlauf bleiben.
VERLAUF_MONATE = 24
_SPEICHER_VERZOEGERUNG_S = 10


def _endlich(wert: Any) -> float:
    """Gespeicherte Zahl, 0 für fehlend oder nicht endlich (json schreibt NaN)."""
    try:
        zahl = float(wert or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return zahl if math.isfinite(zahl) else 0.0


def _lokal(t: datetime) -> datetime:
    if dt_util is not None:
        lokal = dt_util.as_local(t)
        if isinstance(lokal, datetime):  # im Test ist dt_util ein Mock
            return lokal
    from zoneinfo import ZoneInfo

    return t.astimezone(ZoneInfo("Europe/Vienna"))


def viertelstunde_von(t: datetime) -> datetime:
    """Beginn der Viertelstunde, in der ``t`` liegt (UTC)."""
    u = t.astimezone(timezone.utc)
    return u.replace(minute=u.minute // 15 * 15, second=0, microsecond=0)


def monat_von(t: datetime) -> str:
    """Kalendermonat in Ortszeit als ``JJJJ-MM``."""
    return _lokal(t).strftime("%Y-%m")


# Was die Spitze kostet — RICHTWERTE, keine Tarife. Die Beträge je
# Netzbereich kommen erst mit der Tarifverordnung (SNE-T-V) gegen Ende 2026;
# bis dahin gibt es nur die „illustrative Überlegung" der E-Control
# (Fachveranstaltung 14.07.2026, Folien 5 und 7), bevölkerungsgewichtete
# Österreich-Durchschnitte, netto wie jedes Netzentgelt:
#
# * Endstufe (etwa ab 2030): 33,82 €/kW im Jahr bis 10 kW, 67,64 €/kW für
#   den Teil darüber — die HÖCHSTEN derzeit bekannten Sätze.
# * Start 2027: etwa die Hälfte, rund 19 €/kW im Jahr (je Netzbereich 15 bis
#   26), eine Stufe über 10 kW ohne bekannten Betrag.
#
# Verrechnet wird die Monatsspitze, mindestens 2 kW (SNE-G-V § 6, Entwurf).
# TODO 2027: durch die Sätze der SNE-T-V je Netzbereich ersetzen
# (netzentgelt.py liest die Verordnung ohnehin).
LEISTUNGSPREIS_QUELLE = "E-Control, Richtwerte vom 14.07.2026"
LEISTUNGSPREIS_MINDEST_KW = 2.0
LEISTUNGSPREIS_STUFE_KW = 10.0
LEISTUNGSPREIS_ENDSTUFE_EUR_KW_JAHR = (33.82, 67.64)
LEISTUNGSPREIS_START_EUR_KW_JAHR = 19.0
LEISTUNGSPREIS_START_SPANNE = (15.0, 26.0)
LEISTUNGSPREIS_UST = 1.20


def netzkosten_monat(kw: float | None) -> dict[str, Any] | None:
    """Leistungspreis für eine Monatsspitze, € je Monat inkl. USt.

    ``endstufe_eur`` mit den höchsten bekannten Sätzen, ``start_eur`` mit dem
    Durchschnitt zum Start 2027 (ohne Stufe — ihr Betrag ist nicht bekannt).
    """
    if kw is None:
        return None
    verrechnet = max(float(kw), LEISTUNGSPREIS_MINDEST_KW)
    bis, darueber = LEISTUNGSPREIS_ENDSTUFE_EUR_KW_JAHR
    endstufe = (
        min(verrechnet, LEISTUNGSPREIS_STUFE_KW) * bis
        + max(0.0, verrechnet - LEISTUNGSPREIS_STUFE_KW) * darueber
    )
    start = verrechnet * LEISTUNGSPREIS_START_EUR_KW_JAHR
    # Die Sätze gehen je MONAT und brutto hinaus: dieselbe Einheit wie der
    # Betrag daneben — Jahressätze neben einem Monatsbetrag verwirren.
    je_monat = lambda satz: round(satz / 12 * LEISTUNGSPREIS_UST, 2)  # noqa: E731
    return {
        "verrechnet_kw": round(verrechnet, 2),
        "endstufe_eur": round(endstufe / 12 * LEISTUNGSPREIS_UST, 2),
        "start_eur": round(start / 12 * LEISTUNGSPREIS_UST, 2),
        "satz_bis_eur_kw_monat": je_monat(bis),
        "satz_darueber_eur_kw_monat": je_monat(darueber),
        "stufe_kw": LEISTUNGSPREIS_STUFE_KW,
        "start_satz_eur_kw_monat": je_monat(LEISTUNGSPREIS_START_EUR_KW_JAHR),
        "start_spanne_eur_kw_monat": [je_monat(x) for x in LEISTUNGSPREIS_START_SPANNE],
        "quelle": LEISTUNGSPREIS_QUELLE,
    }


def _runde_kw(kw: float) -> float:
    """Kaufmännisch auf zwei Nachkommastellen — wie der Netzbetreiber."""
    return float(Decimal(str(kw)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class Leistungsspitze:
    """Integriert den Netzbezug je Viertelstunde und führt das Monatsmaximum.

    Die Rechnung (``messwert`` / ``grenze``) nimmt Zeitpunkte als Argument
    und ist ohne Home Assistant testbar. ``async_start`` hängt sie an den
    Netzsensor und an den Viertelstunden-Zeitgeber.
    """

    def __init__(self, hass: Any, entry_id: str, config: dict) -> None:
        self._hass = hass
        self._config = config
        self._store: Any = (
            Store(hass, 1, f"{DOMAIN}_{entry_id}_leistungsspitze")
            if Store is not None and hass is not None
            else None
        )
        self._listeners: list[Callable[[], None]] = []

        # Laufende Viertelstunde.
        self._q_start: datetime | None = None
        self._energie_kwh = 0.0
        self._abgedeckt_s = 0.0
        # Letzter Stützpunkt: Zeitpunkt und Bezug in kW (None = nicht lesbar).
        self._letzte_t: datetime | None = None
        self._letzte_kw: float | None = None
        # Bis wann der letzte Wert gelten darf (HALTEN_MAX_S nach seinem
        # Eintreffen) — unabhängig davon, dass _letzte_t beim Integrieren
        # weiterwandert.
        self._gilt_bis: datetime | None = None

        # Zuletzt abgeschlossene Viertelstunde.
        self._letzte_viertelstunde: dict[str, Any] | None = None
        # Monat.
        self._monat: str | None = None
        self._spitze: dict[str, Any] | None = None
        self._verlauf: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Rechnung
    # ------------------------------------------------------------------
    def messwert(self, t: datetime, bezug_kw: float | None) -> None:
        """Neuer Stützpunkt: ab ``t`` gilt ``bezug_kw`` (None = unbekannt)."""
        self._vorruecken(t)
        if bezug_kw is not None:
            bezug_kw = float(bezug_kw)
            # max(0, nan) ergäbe 0 — ein unlesbarer Wert ist aber unbekannt.
            bezug_kw = max(0.0, bezug_kw) if math.isfinite(bezug_kw) else None
        self._letzte_t = t
        self._letzte_kw = bezug_kw
        self._gilt_bis = t + timedelta(seconds=HALTEN_MAX_S)

    def grenze(self, t: datetime) -> None:
        """Zeitgeber: bis ``t`` integrieren, erreichte Grenzen abschließen."""
        self._vorruecken(t)

    def _vorruecken(self, t: datetime) -> None:
        if self._q_start is None:
            self._q_start = viertelstunde_von(t)
            self._pruefe_monat(self._q_start)
        if self._letzte_t is not None and t < self._letzte_t:
            return  # verspäteter Stützpunkt — die Zeit läuft nicht rückwärts
        while t >= self._q_start + VIERTELSTUNDE:
            ende = self._q_start + VIERTELSTUNDE
            self._integriere_bis(ende)
            self._schliesse_ab()
            self._q_start = ende
            self._pruefe_monat(ende)
        self._integriere_bis(t)

    def _integriere_bis(self, t: datetime) -> None:
        if self._letzte_t is None:
            return
        if self._letzte_kw is not None and self._gilt_bis is not None:
            bis = min(t, self._gilt_bis)
            dauer = (bis - self._letzte_t).total_seconds()
            if dauer > 0:
                self._energie_kwh += self._letzte_kw * dauer / 3600.0
                self._abgedeckt_s += dauer
        self._letzte_t = max(self._letzte_t, t)

    def _schliesse_ab(self) -> None:
        assert self._q_start is not None
        kw = _runde_kw(self._energie_kwh / 0.25)
        anteil = min(self._abgedeckt_s / _VIERTELSTUNDE_S, 1.0)
        if self._abgedeckt_s <= 0:
            # Gar nichts gemessen (Sensor aus, Neustart vor der Grenze):
            # keine Viertelstunde, nicht einmal eine 0.
            self._energie_kwh = 0.0
            self._abgedeckt_s = 0.0
            return
        eintrag = {
            "start": self._q_start.isoformat(),
            "kw": kw,
            "energie_kwh": round(self._energie_kwh, 4),
            "abdeckung": round(anteil, 3),
            "vollstaendig": anteil >= VOLLSTAENDIG_AB,
        }
        self._letzte_viertelstunde = eintrag
        self._pruefe_monat(self._q_start)
        if self._spitze is None or kw > self._spitze["kw"]:
            self._spitze = dict(eintrag)
        self._energie_kwh = 0.0
        self._abgedeckt_s = 0.0
        self._geaendert()

    def _pruefe_monat(self, q_start: datetime) -> None:
        monat = monat_von(q_start)
        if monat == self._monat:
            return
        if self._monat is not None and self._spitze is not None:
            self._verlauf[self._monat] = dict(self._spitze)
            for alt in sorted(self._verlauf)[:-VERLAUF_MONATE]:
                del self._verlauf[alt]
        if self._monat is not None:
            self._spitze = None
            self._geaendert()
        self._monat = monat

    # ------------------------------------------------------------------
    # Lesen
    # ------------------------------------------------------------------
    @property
    def letzte_viertelstunde(self) -> dict[str, Any] | None:
        return self._letzte_viertelstunde

    @property
    def monat(self) -> str | None:
        return self._monat

    @property
    def spitze(self) -> dict[str, Any] | None:
        return self._spitze

    @property
    def verlauf(self) -> dict[str, dict[str, Any]]:
        return dict(self._verlauf)

    def laufend(self, jetzt: datetime) -> dict[str, Any] | None:
        """Stand der laufenden Viertelstunde, ohne sie zu verändern.

        ``bisher_kw`` ist, was sie schon sicher zählt (Energie bis jetzt
        durch 0,25 h) — der Wert steigt nur. ``hochrechnung_kw`` nimmt an,
        dass der aktuelle Bezug bis zur Grenze anhält.
        """
        if (
            self._q_start is None
            or not isinstance(jetzt, datetime)
            or jetzt < self._q_start
        ):
            return None
        energie = self._energie_kwh
        kw_jetzt = None
        if (
            self._letzte_t is not None
            and self._letzte_kw is not None
            and self._gilt_bis is not None
            and jetzt >= self._letzte_t
        ):
            bis = min(jetzt, self._gilt_bis, self._q_start + VIERTELSTUNDE)
            energie += self._letzte_kw * max((bis - self._letzte_t).total_seconds(), 0) / 3600.0
            if jetzt < self._gilt_bis:
                kw_jetzt = self._letzte_kw
        rest_s = max((self._q_start + VIERTELSTUNDE - jetzt).total_seconds(), 0.0)
        ergebnis: dict[str, Any] = {
            "start": self._q_start.isoformat(),
            "bisher_kw": round(energie / 0.25, 3),
            "hochrechnung_kw": None,
            # Für die Spitzenkappung: aktueller Bezug (None = nicht frisch)
            # und was von der Viertelstunde noch übrig ist.
            "bezug_kw": kw_jetzt,
            "rest_s": round(rest_s, 1),
        }
        if kw_jetzt is not None:
            ergebnis["hochrechnung_kw"] = round(
                (energie + kw_jetzt * rest_s / 3600.0) / 0.25, 3
            )
        return ergebnis

    # ------------------------------------------------------------------
    # Beobachter
    # ------------------------------------------------------------------
    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)

        def _entfernen() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return _entfernen

    def _geaendert(self) -> None:
        if self._store is not None:
            self._store.async_delay_save(self._als_dict, _SPEICHER_VERZOEGERUNG_S)
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Leistungsspitze: Beobachter fehlgeschlagen")

    # ------------------------------------------------------------------
    # Speicher
    # ------------------------------------------------------------------
    def _als_dict(self) -> dict[str, Any]:
        return {
            "monat": self._monat,
            "spitze": self._spitze,
            "verlauf": self._verlauf,
            "letzte_viertelstunde": self._letzte_viertelstunde,
            # Die angebrochene Viertelstunde übersteht so ein Neuladen der
            # Integration (Flush beim Entladen). Nach einem echten Neustart
            # fehlt die Zeit dazwischen — die Abdeckung zeigt es.
            "laufend": (
                {
                    "start": self._q_start.isoformat(),
                    "energie_kwh": self._energie_kwh,
                    "abgedeckt_s": self._abgedeckt_s,
                }
                if self._q_start is not None
                else None
            ),
        }

    def aus_dict(self, daten: dict[str, Any], jetzt: datetime) -> None:
        """Gespeicherten Stand übernehmen (``jetzt`` entscheidet, was noch gilt)."""
        self._monat = daten.get("monat")
        self._spitze = daten.get("spitze")
        self._verlauf = dict(daten.get("verlauf") or {})
        self._letzte_viertelstunde = daten.get("letzte_viertelstunde")
        laufend = daten.get("laufend") or {}
        try:
            start = datetime.fromisoformat(laufend["start"])
        except (KeyError, TypeError, ValueError):
            start = None
        if start is not None and start == viertelstunde_von(jetzt):
            self._q_start = start
            self._energie_kwh = _endlich(laufend.get("energie_kwh"))
            self._abgedeckt_s = _endlich(laufend.get("abgedeckt_s"))
        # Monatswechsel während der Ausfallzeit.
        self._pruefe_monat(viertelstunde_von(jetzt))

    async def async_load(self) -> None:
        if self._store is None:
            return
        try:
            daten = await self._store.async_load()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Leistungsspitze: kein gespeicherter Stand")
            return
        if isinstance(daten, dict):
            self.aus_dict(daten, _jetzt_utc())

    async def async_flush(self) -> None:
        if self._store is None:
            return
        if self._q_start is not None:
            self._vorruecken(_jetzt_utc())
        await self._store.async_save(self._als_dict())

    # ------------------------------------------------------------------
    # Anbindung an Home Assistant
    # ------------------------------------------------------------------
    def _bezug_jetzt_kw(self, jetzt: datetime) -> float | None:
        """Aktueller Bezug in kW, oder None, wenn der Wert nicht frisch ist.

        Frisch heißt: die Quelle hat den Zustand in den letzten
        ``HALTEN_MAX_S`` geschrieben. ``last_reported`` wandert auch dann
        weiter, wenn der Wert gleich bleibt — ein stehender Wert ist also
        gemessen, ein eingefrorener nicht.
        """
        from .power_readings import read_power_kw, resolve_sign

        grid_id = self._config.get(CONF_GRID_POWER_SENSOR, "")
        if not grid_id:
            return None
        # Eigene, knappere Grenze als die Live-Pfade (siehe HALTEN_MAX_S),
        # gegen denselben Zeitpunkt wie die Integration.
        netz = read_power_kw(
            self._hass, grid_id, max_alter_s=HALTEN_MAX_S, jetzt=jetzt
        )
        if netz is None:
            return None
        netz *= resolve_sign(self._config.get(CONF_INVERTER_TYPE, ""), grid_id, "grid_sign")
        return max(0.0, -netz)

    def async_start(self, entry: Any) -> None:
        """Netzsensor, Abtastung und Viertelstunden-Zeitgeber abonnieren."""
        try:
            from homeassistant.core import callback
            from homeassistant.helpers.event import (
                async_track_state_change_event,
                async_track_time_change,
                async_track_time_interval,
            )
        except ImportError:  # pragma: no cover - nur außerhalb von HA
            return

        grid_id = self._config.get(CONF_GRID_POWER_SENSOR, "")
        if not grid_id:
            _LOGGER.debug("Leistungsspitze: kein Netzsensor konfiguriert")
            return

        # @callback ist Pflicht: Eine schlichte Funktion führt HA im
        # Executor-Thread aus. Dort liefen Store-Speichern und der
        # Viertelstunden-Push der Sensoren (hass.async_create_task) aus einem
        # fremden Thread — HA wies ihn ab, „Beobachter fehlgeschlagen" bei
        # jedem Viertelstunden-Abschluss (Grünbach, 25.09.2026).
        @callback
        def _stuetzpunkt(_arg: Any = None) -> None:
            jetzt = _jetzt_utc()
            self.messwert(jetzt, self._bezug_jetzt_kw(jetzt))

        @callback
        def _takt(_now: Any) -> None:
            self.grenze(_jetzt_utc())

        # Jede Änderung sofort (genauer Zeitpunkt des Sprungs), dazu eine
        # Abtastung, die einen stehenden Wert bestätigt und einen
        # eingefrorenen erkennt.
        entry.async_on_unload(
            async_track_state_change_event(self._hass, [grid_id], _stuetzpunkt)
        )
        entry.async_on_unload(
            async_track_time_interval(
                self._hass, _stuetzpunkt, timedelta(seconds=ABTASTUNG_S)
            )
        )
        entry.async_on_unload(
            async_track_time_change(
                self._hass, _takt, minute=[0, 15, 30, 45], second=0
            )
        )
        _stuetzpunkt()


def _jetzt_utc() -> datetime:
    if dt_util is not None:
        jetzt = dt_util.utcnow()
        if isinstance(jetzt, datetime):
            return jetzt
    return datetime.now(tz=timezone.utc)
