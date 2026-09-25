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
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable

from .const import CONF_GRID_POWER_SENSOR, DOMAIN

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
            bezug_kw = max(0.0, float(bezug_kw))
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
            self._energie_kwh = float(laufend.get("energie_kwh") or 0.0)
            self._abgedeckt_s = float(laufend.get("abgedeckt_s") or 0.0)
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
        from .power_readings import compute_grid_export_kw

        grid_id = self._config.get(CONF_GRID_POWER_SENSOR, "")
        state = self._hass.states.get(grid_id) if grid_id else None
        if state is None:
            return None
        zuletzt = getattr(state, "last_reported", None) or getattr(
            state, "last_updated", None
        )
        if isinstance(zuletzt, datetime) and (
            jetzt - zuletzt
        ).total_seconds() > HALTEN_MAX_S:
            return None
        netz = compute_grid_export_kw(self._hass, self._config)
        return None if netz is None else max(0.0, -netz)

    def async_start(self, entry: Any) -> None:
        """Netzsensor, Abtastung und Viertelstunden-Zeitgeber abonnieren."""
        try:
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

        def _stuetzpunkt(_arg: Any = None) -> None:
            jetzt = _jetzt_utc()
            self.messwert(jetzt, self._bezug_jetzt_kw(jetzt))

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
