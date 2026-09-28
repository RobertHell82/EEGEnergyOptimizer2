"""Prognosevergleich — zwei PV-Prognosen und die Messung, festgehalten je Tag.

Die Frage „ist die eigene Berechnung so gut wie Solcast?" lässt sich nur mit
Zahlen von der eigenen Anlage beantworten. Beide Prognosen ändern sich
laufend, am Abend weiß niemand mehr, was sie morgens gesagt haben — also
wird **einmal am Morgen festgehalten** (ab ``FESTHALTEN_AB_STUNDE`` Uhr
Ortszeit, dem ersten Fremddaten-Takt danach), was jede Quelle für den
laufenden Kalendertag vorhersagt: Halbstundenmittel der **Fremdquelle**
(Solcast über ``detailedForecast``, sonst Forecast.Solar über die
Energy-Plattform) und der **eigenen Berechnung** (``pvprognose/``). Die
Messung kommt dazu, sobald sie da ist: 5-Minuten-Mittel des PV-Sensors aus
dem Recorder (``schedule_archive.async_ist_verlauf``), zu Halbstunden
verdichtet, bei jedem Takt nachgezogen — der laufende Tag zeigt so im Panel
schon seinen bisherigen Verlauf.

Aufgehoben werden ``TAGE_AUFBEWAHRUNG`` Tage — gut ein Jahr, weil die
Kalibrierung der eigenen Prognose (``pvprognose/kalibrierung.py``) aus
genau diesen Tagen lernt und jeden Sonnenstand einmal gesehen haben soll.
Ausgewertet werden die letzten ``TAGE_AUSWERTUNG``: je Quelle Tagessumme,
Abweichung und mittlerer Fehler über die Tagstunden, sowie der **empirische
p10**: das 10-%-Quantil des Verhältnisses gemessen zu prognostiziert über
die vollständigen Tage — ab ``P10_MIN_TAGE`` Tagen, denn ein Quantil aus
fünf Werten ist Zufall. Ein Jahr darin würde Sommer und Winter mischen.
``schedule.py`` nimmt den p10 für die eigene Prognose, wenn kein Solcast-p10
zum Leihen da ist.

Zwei Speicher: Ein Jahr Tage sind einige Megabyte, und der Takt schreibt
alle 30 Minuten. Die abgeschlossenen Tage (``_ist_archiv``) stehen deshalb
im Archiv-Store, der nur geschrieben wird, wenn ein Tag hineinwandert —
einmal am Tag; der laufende Store hält nur, was sich noch ändert.

Neben den beiden Prognosen hält ein Tag fest, was die Kalibrierung braucht:
``eigen_roh`` (die eigene Prognose OHNE Kalibrierung — aus der kalibrierten
lernte sie ihre eigene Korrektur nach), ``eigen_kennung`` (Anlage und
Wettermodelle, mit denen sie gerechnet wurde) und zur Messung die
Einspeisung (``netz``) und den Ladestand (``soc``), an denen eine Abregelung
zu erkennen ist.

Was der Vergleich NICHT ist: eine Steuerung. Er liest, rechnet und zeigt —
und gibt der eigenen Prognose die Lerndaten. Aufgezeichnet wird, wenn der
Schalter ``pv_prognose_vergleich`` an ist ODER die eigene Prognose steuert
(sonst lernte sie nie).

Zeitraster: Slot-Anfänge als UTC-ISO-Strings, 30 Minuten, von lokaler
Mitternacht bis lokaler Mitternacht — 48 Slots, an den Umstellungstagen
46 oder 50. In UTC gerechnet, weil Wanduhr plus ``timedelta`` an der
Umstellung doppelte oder fehlende Zeiten erzeugt (siehe ``_grid_timestamps``
in schedule.py).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .const import CONF_PV_PROGNOSE_VERGLEICH, DOMAIN  # noqa: F401 — Schlüssel mit-exportiert

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.helpers.storage import Store
    from homeassistant.util import dt as dt_util

    _utcnow = dt_util.utcnow
    _lokal = dt_util.as_local
except ImportError:  # Testumgebung
    _utcnow = lambda: datetime.now(tz=timezone.utc)  # noqa: E731
    _lokal = lambda dt: dt.astimezone()  # noqa: E731
    Store = None  # type: ignore[assignment,misc]

TAGE_AUFBEWAHRUNG = 400
TAGE_AUSWERTUNG = 30
# Ein Tag wandert ins Archiv, wenn er vollständig ist oder so alt, dass keine
# Messung mehr nachkommt.
ARCHIV_AB_TAGEN = 2
FESTHALTEN_AB_STUNDE = 5
# Wird eine Prognose erst nach dieser Stunde festgehalten (Home Assistant lief
# um 5 Uhr nicht), hat sie den halben Tag schon gesehen: Der Tag heißt dann
# „spät", steht in der Tabelle, zählt aber weder in der Zusammenfassung noch
# im empirischen p10 — dort stünde er als unverdient gute Prognose.
FESTHALTEN_SPAET_STUNDE = 8
SLOT_MIN = 30
# Der empirische p10 braucht eine Grundlage: unter 14 vollständigen Tagen
# bleibt er aus und die Reserve rechnet mit dem festen Faktor.
P10_MIN_TAGE = 14
P10_QUANTIL = 0.10
# Ein Tag zählt für Abweichung und p10 nur, wenn die Messung bis kurz vor
# Mitternacht reicht und die Prognose überhaupt Energie versprach.
MESSUNG_VOLLSTAENDIG_TOLERANZ_S = 45 * 60
P10_MIN_PROGNOSE_KWH = 1.0
# Tagstunden: Slots, in denen eine der beiden Reihen mehr als das hier zeigt.
TAG_SCHWELLE_KW = 0.05

QUELLE_EIGEN = "eigen"
QUELLE_FREMD = "fremd"

_SLOT = timedelta(minutes=SLOT_MIN)


def vergleich_aktiv(config: dict[str, Any]) -> bool:
    return bool(config.get(CONF_PV_PROGNOSE_VERGLEICH))


def aufzeichnung_noetig(config: dict[str, Any]) -> bool:
    """Vergleich eingeschaltet — oder die eigene Prognose steuert und braucht
    die Tage für ihre Kalibrierung."""
    from .const import CONF_FORECAST_SOURCE, FORECAST_SOURCE_EIGEN

    return vergleich_aktiv(config) or (
        str(config.get(CONF_FORECAST_SOURCE) or "").lower() == FORECAST_SOURCE_EIGEN
    )


def tagesslots(datum: str) -> list[datetime]:
    """Slot-Anfänge (UTC) des lokalen Kalendertags ``datum``."""
    tag = date.fromisoformat(datum)
    start_lokal = _lokal(_utcnow()).replace(
        year=tag.year, month=tag.month, day=tag.day, hour=0, minute=0, second=0, microsecond=0
    )
    ende_lokal = start_lokal + timedelta(days=1)
    # Über UTC schreiten — die Wanduhr hat an der Umstellung Lücken/Doppel.
    start = start_lokal.astimezone(timezone.utc)
    ende = ende_lokal.astimezone(timezone.utc)
    slots: list[datetime] = []
    t = start
    while t < ende:
        slots.append(t)
        t += _SLOT
    return slots


def reihe_exakt(werte: dict[datetime, Any], slots: list[datetime]) -> dict[str, float]:
    """Werte auf die Slots legen — nur exakte Zeitpunkte, nichts verschmiert."""
    nach_epoche: dict[float, Any] = {}
    for stamp, wert in werte.items():
        try:
            nach_epoche[stamp.timestamp()] = wert
        except (AttributeError, TypeError):
            continue
    ergebnis: dict[str, float] = {}
    for slot in slots:
        wert = nach_epoche.get(slot.timestamp())
        if wert is None:
            continue
        if isinstance(wert, (tuple, list)):
            wert = wert[0]
        try:
            ergebnis[slot.isoformat()] = round(max(0.0, float(wert)), 4)
        except (TypeError, ValueError):
            continue
    return ergebnis


def reihe_aus_stunden(wh_hours: dict[str, float], slots: list[datetime]) -> dict[str, float]:
    """Wh je Stunde (Forecast.Solar) → kW je Halbstunde, beide Hälften gleich."""
    nach_stunde: dict[float, float] = {}
    for roh, wert in (wh_hours or {}).items():
        try:
            stamp = datetime.fromisoformat(str(roh))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            continue
        stunde = stamp.replace(minute=0, second=0, microsecond=0)
        nach_stunde[stunde.timestamp()] = float(wert) / 1000.0
    ergebnis: dict[str, float] = {}
    for slot in slots:
        stunde = slot - timedelta(minutes=slot.minute, seconds=slot.second)
        wert = nach_stunde.get(stunde.timestamp())
        if wert is None:
            continue
        ergebnis[slot.isoformat()] = round(max(0.0, wert), 4)
    return ergebnis


def messung_aus_punkten(
    punkte: list[list[Any]], slots: list[datetime]
) -> tuple[dict[str, float], datetime | None]:
    """5-Minuten-Mittel des Recorders → Halbstundenmittel, plus Ende der Messung."""
    gruppen: dict[str, list[float]] = {}
    letzter: datetime | None = None
    if not slots:
        return {}, None
    erster_slot = slots[0]
    for punkt in punkte or []:
        try:
            stamp = datetime.fromisoformat(str(punkt[0]))
            wert = float(punkt[1])
        except (TypeError, ValueError, IndexError):
            continue
        if stamp.tzinfo is None:
            continue
        if stamp < erster_slot:
            continue
        # Slot über den Abstand zum ersten Slot — kein Wanduhr-Rechnen.
        index = int((stamp - erster_slot).total_seconds() // (SLOT_MIN * 60))
        if index < 0 or index >= len(slots):
            continue
        gruppen.setdefault(slots[index].isoformat(), []).append(max(0.0, wert))
        ende = stamp + timedelta(minutes=5)
        if letzter is None or ende > letzter:
            letzter = ende
    return {k: round(sum(v) / len(v), 4) for k, v in gruppen.items()}, letzter


def _energie_kwh(reihe: dict[str, float] | None, nur: set[str] | None = None) -> float:
    if not reihe:
        return 0.0
    return round(
        sum(v for k, v in reihe.items() if nur is None or k in nur) * SLOT_MIN / 60.0, 3
    )


def _quantil(werte: list[float], q: float) -> float:
    sortiert = sorted(werte)
    if not sortiert:
        return 0.0
    pos = (len(sortiert) - 1) * q
    unten = int(pos)
    oben = min(unten + 1, len(sortiert) - 1)
    rest = pos - unten
    return sortiert[unten] + (sortiert[oben] - sortiert[unten]) * rest


def statistik_tag(tag: dict[str, Any]) -> dict[str, Any]:
    """Kennzahlen eines Tages je Quelle — gegen die Messung, soweit vorhanden."""
    gemessen: dict[str, float] = tag.get("gemessen") or {}
    slots: list[str] = tag.get("slots") or []
    vollstaendig = bool(tag.get("vollstaendig"))
    gemessen_kwh = _energie_kwh(gemessen)
    ergebnis: dict[str, Any] = {
        "datum": tag.get("datum"),
        "fremd_name": tag.get("fremd_name"),
        "festgehalten": tag.get("festgehalten"),
        "gemessen_bis": tag.get("gemessen_bis"),
        "vollstaendig": vollstaendig,
        "spaet": bool(tag.get("spaet")),
        "festgehalten_fremd": tag.get("festgehalten_fremd"),
        "festgehalten_eigen": tag.get("festgehalten_eigen"),
        "gemessen_kwh": gemessen_kwh,
        "quellen": {},
    }
    for quelle in (QUELLE_FREMD, QUELLE_EIGEN):
        reihe: dict[str, float] | None = tag.get(quelle)
        if not reihe:
            continue
        gemeinsam = [s for s in slots if s in reihe and s in gemessen]
        tagstunden = [
            s for s in gemeinsam
            if reihe[s] > TAG_SCHWELLE_KW or gemessen[s] > TAG_SCHWELLE_KW
        ]
        mae = (
            round(sum(abs(reihe[s] - gemessen[s]) for s in tagstunden) / len(tagstunden), 3)
            if tagstunden
            else None
        )
        prognose_kwh = _energie_kwh(reihe)
        # Bis hierher: Prognose nur über die Slots, die auch gemessen sind —
        # sonst stünde ein halber Tag Messung gegen einen ganzen Tag Prognose.
        prognose_bisher_kwh = _energie_kwh(reihe, set(gemeinsam))
        abweichung = round(prognose_kwh - gemessen_kwh, 3) if vollstaendig else None
        abweichung_pct = (
            round(abweichung / gemessen_kwh * 100.0, 1)
            if abweichung is not None and gemessen_kwh > 0.5
            else None
        )
        ergebnis["quellen"][quelle] = {
            "prognose_kwh": prognose_kwh,
            "prognose_bisher_kwh": prognose_bisher_kwh,
            "abweichung_kwh": abweichung,
            "abweichung_pct": abweichung_pct,
            "mae_kw": mae,
            "slots": len(gemeinsam),
        }
    return ergebnis


class Prognosevergleich:
    """Hält die Tagesaufzeichnungen und rechnet die Kennzahlen daraus."""

    def __init__(self, hass: Any, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store = None
        self._archiv_store = None
        if Store is not None:
            self._store = Store(hass, 1, f"{DOMAIN}_{entry_id}_prognosevergleich")
            self._archiv_store = Store(hass, 1, f"{DOMAIN}_{entry_id}_prognosevergleich_archiv")
        self._tage: dict[str, dict[str, Any]] = {}
        # Welche Tage das Archiv zuletzt geschrieben hat — nur bei Änderung neu.
        self._archiv_stand: frozenset[str] = frozenset()
        self._letzter_fehler: str | None = None

    # -- Speicher -------------------------------------------------------

    async def async_load(self) -> None:
        if self._store is None:
            return
        for store, archiv in ((self._archiv_store, True), (self._store, False)):
            if store is None:
                continue
            try:
                stored = await store.async_load()
            except Exception:  # noqa: BLE001
                continue
            if isinstance(stored, dict) and isinstance(stored.get("tage"), dict):
                tage = {str(k): v for k, v in stored["tage"].items() if isinstance(v, dict)}
                self._tage.update(tage)
                if archiv:
                    self._archiv_stand = frozenset(tage)

    def _ist_archiv(self, datum: str, jetzt: datetime) -> bool:
        tag = self._tage.get(datum) or {}
        grenze = (_lokal(jetzt).date() - timedelta(days=ARCHIV_AB_TAGEN)).isoformat()
        heute = _lokal(jetzt).date().isoformat()
        return datum < heute and (bool(tag.get("vollstaendig")) or datum <= grenze)

    async def async_save(self, jetzt: datetime | None = None) -> None:
        if self._store is None:
            return
        jetzt = jetzt or _utcnow()
        archiv = {d: t for d, t in self._tage.items() if self._ist_archiv(d, jetzt)}
        laufend = {d: t for d, t in self._tage.items() if d not in archiv}
        try:
            if self._archiv_store is not None and frozenset(archiv) != self._archiv_stand:
                await self._archiv_store.async_save({"tage": archiv})
                self._archiv_stand = frozenset(archiv)
            await self._store.async_save({"tage": laufend})
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Prognosevergleich konnte nicht gespeichert werden")

    # -- Schreiben ------------------------------------------------------

    def _tag(self, datum: str) -> dict[str, Any]:
        tag = self._tage.get(datum)
        if tag is None:
            tag = {
                "datum": datum,
                "slots": [s.isoformat() for s in tagesslots(datum)],
                "fremd_name": None,
                "fremd": None,
                "eigen": None,
                "gemessen": {},
                "gemessen_bis": None,
                "vollstaendig": False,
                "festgehalten": None,
            }
            self._tage[datum] = tag
        return tag

    def festhalten(
        self,
        datum: str,
        jetzt: datetime,
        fremd_name: str | None,
        fremd: dict[str, float] | None,
        eigen: dict[str, float] | None,
        eigen_roh: dict[str, float] | None = None,
        eigen_kennung: str | None = None,
    ) -> bool:
        """Prognosen des Tages eintragen — je Quelle nur einmal. True bei Neuem.

        Ohne eine einzige Reihe entsteht kein Tag: ein leerer Eintrag stünde
        sonst als „Tag ohne Prognose" in der Übersicht, obwohl nur die
        Quellen noch nicht da waren.
        """
        if not fremd and not eigen:
            return False
        tag = self._tag(datum)
        neu = False
        # Je Quelle ein eigener Zeitpunkt: kommt eine Quelle erst Stunden
        # später dazu, muss das an genau dieser Quelle sichtbar sein.
        spaet = _lokal(jetzt).hour >= FESTHALTEN_SPAET_STUNDE
        if fremd and tag.get("fremd") is None:
            tag["fremd"] = fremd
            tag["fremd_name"] = fremd_name
            tag["festgehalten_fremd"] = jetzt.isoformat()
            neu = True
        if eigen and tag.get("eigen") is None:
            tag["eigen"] = eigen
            tag["festgehalten_eigen"] = jetzt.isoformat()
            neu = True
        # Die Lerndaten der Kalibrierung dürfen auch später kommen: eine
        # Prognose vom Mittag ist für den Faktor so gut wie eine vom Morgen
        # (Wetterzufall filtert die Kalibrierung ohnehin), nur der Vergleich
        # mit Solcast wäre unfair — und der liest ``eigen``, nicht das hier.
        if eigen_roh and eigen_kennung and tag.get("eigen_roh") is None:
            tag["eigen_roh"] = eigen_roh
            tag["eigen_kennung"] = eigen_kennung
            neu = True
        if neu:
            if not tag.get("festgehalten"):
                tag["festgehalten"] = jetzt.isoformat()
            if spaet:
                tag["spaet"] = True
        return neu

    def messung_eintragen(
        self,
        datum: str,
        gemessen: dict[str, float],
        bis: datetime | None,
        netz: dict[str, float] | None = None,
        soc: dict[str, float] | None = None,
    ) -> None:
        if not gemessen:
            return
        tag = self._tag(datum)
        tag["gemessen"] = gemessen
        if netz is not None:
            tag["netz"] = netz
        if soc is not None:
            tag["soc"] = soc
        tag["gemessen_bis"] = bis.isoformat() if bis else None
        if bis is not None and tag["slots"]:
            ende = datetime.fromisoformat(tag["slots"][-1]) + _SLOT
            tag["vollstaendig"] = (ende - bis).total_seconds() <= MESSUNG_VOLLSTAENDIG_TOLERANZ_S
        else:
            tag["vollstaendig"] = False

    def aufraeumen(self, jetzt: datetime) -> None:
        grenze = (_lokal(jetzt).date() - timedelta(days=TAGE_AUFBEWAHRUNG)).isoformat()
        for datum in [d for d in self._tage if d < grenze]:
            del self._tage[datum]

    # -- Lesen ----------------------------------------------------------

    def tage(self) -> list[str]:
        return sorted(self._tage)

    def tag(self, datum: str) -> dict[str, Any] | None:
        return self._tage.get(datum)

    def hat_prognosen(self, datum: str) -> tuple[bool, bool]:
        tag = self._tage.get(datum) or {}
        return tag.get("fremd") is not None, tag.get("eigen") is not None

    def lerntage(self) -> list[dict[str, Any]]:
        """Alle Tage für die Kalibrierung — sie filtert selbst."""
        return list(self._tage.values())

    def _auswertung(self) -> list[dict[str, Any]]:
        """Die letzten ``TAGE_AUSWERTUNG`` Tage — Grundlage aller Kennzahlen."""
        return [self._tage[d] for d in sorted(self._tage)[-TAGE_AUSWERTUNG:]]

    def uebersicht(self) -> list[dict[str, Any]]:
        """Kennzahlen der ausgewerteten Tage, neueste zuerst."""
        return [statistik_tag(t) for t in reversed(self._auswertung())]

    def _tagesverhaeltnisse(self, quelle: str) -> list[float]:
        werte: list[float] = []
        for tag in self._auswertung():
            if not tag.get("vollstaendig") or tag.get("spaet"):
                continue
            reihe = tag.get(quelle)
            if not reihe:
                continue
            prognose = _energie_kwh(reihe)
            if prognose < P10_MIN_PROGNOSE_KWH:
                continue
            werte.append(_energie_kwh(tag.get("gemessen")) / prognose)
        return werte

    def p10_faktor(self, quelle: str) -> tuple[float | None, int]:
        """(Faktor, Anzahl Tage) — Faktor None unter ``P10_MIN_TAGE``."""
        werte = self._tagesverhaeltnisse(quelle)
        if len(werte) < P10_MIN_TAGE:
            return None, len(werte)
        faktor = _quantil(werte, P10_QUANTIL)
        return round(min(1.0, max(0.2, faktor)), 3), len(werte)

    def zusammenfassung(self) -> dict[str, Any]:
        """Über alle vollständigen Tage: Bias, Fehler, wer öfter näher lag."""
        tage = [
            statistik_tag(t) for t in self._auswertung()
            if t.get("vollstaendig") and not t.get("spaet")
        ]
        spaet = sum(1 for t in self._auswertung() if t.get("vollstaendig") and t.get("spaet"))
        ergebnis: dict[str, Any] = {
            "tage": len(tage), "spaet_ausgelassen": spaet, "quellen": {}, "naeher": {},
        }
        for quelle in (QUELLE_FREMD, QUELLE_EIGEN):
            abw = [t["quellen"][quelle]["abweichung_kwh"] for t in tage if quelle in t["quellen"]]
            abw = [a for a in abw if a is not None]
            gemessen = [t["gemessen_kwh"] for t in tage if quelle in t["quellen"]]
            maes = [t["quellen"][quelle]["mae_kw"] for t in tage if quelle in t["quellen"]]
            maes = [m for m in maes if m is not None]
            if not abw:
                continue
            summe_gemessen = sum(gemessen)
            ergebnis["quellen"][quelle] = {
                "tage": len(abw),
                "bias_kwh": round(sum(abw) / len(abw), 3),
                "bias_pct": round(sum(abw) / summe_gemessen * 100.0, 1) if summe_gemessen > 0 else None,
                "mae_kwh": round(sum(abs(a) for a in abw) / len(abw), 3),
                "mae_pct": round(sum(abs(a) for a in abw) / summe_gemessen * 100.0, 1) if summe_gemessen > 0 else None,
                "mae_kw": round(sum(maes) / len(maes), 3) if maes else None,
            }
            faktor, n = self.p10_faktor(quelle)
            ergebnis["quellen"][quelle]["p10_faktor"] = faktor
            ergebnis["quellen"][quelle]["p10_tage"] = n
        beide = [
            t for t in tage
            if QUELLE_FREMD in t["quellen"] and QUELLE_EIGEN in t["quellen"]
            and t["quellen"][QUELLE_FREMD]["abweichung_kwh"] is not None
            and t["quellen"][QUELLE_EIGEN]["abweichung_kwh"] is not None
        ]
        if beide:
            fremd_naeher = sum(
                1 for t in beide
                if abs(t["quellen"][QUELLE_FREMD]["abweichung_kwh"])
                < abs(t["quellen"][QUELLE_EIGEN]["abweichung_kwh"])
            )
            ergebnis["naeher"] = {
                "tage": len(beide),
                "fremd": fremd_naeher,
                "eigen": len(beide) - fremd_naeher,
            }
        return ergebnis

    def status(self, datum: str | None = None) -> dict[str, Any]:
        """Alles fürs Panel: Übersicht, Zusammenfassung, ein Tag im Detail."""
        tage = self.tage()
        gewaehlt = datum if datum in self._tage else (tage[-1] if tage else None)
        tag = self._tage.get(gewaehlt) if gewaehlt else None
        return {
            "tage": tage,
            "uebersicht": self.uebersicht(),
            "zusammenfassung": self.zusammenfassung(),
            "tag": tag,
            "tag_statistik": statistik_tag(tag) if tag else None,
            "p10_min_tage": P10_MIN_TAGE,
            "fehler": self._letzter_fehler,
        }

    # -- Der Takt -------------------------------------------------------

    async def async_tick(
        self, config: dict[str, Any], jetzt: datetime | None = None
    ) -> None:
        """Alle 30 Minuten: morgens festhalten, Messung nachziehen, aufräumen.

        Fehler einer Quelle halten die anderen nicht auf — und nichts hier
        darf den Fremddaten-Takt kippen, der ruft ohnehin mit try/except.
        """
        from . import schedule as sched

        jetzt = jetzt or _utcnow()
        lokal = _lokal(jetzt)
        heute = lokal.date().isoformat()
        gestern = (lokal.date() - timedelta(days=1)).isoformat()
        geaendert = False

        provider = (self._hass.data.get(DOMAIN, {}).get(self._entry_id) or {}).get("pvprognose")
        if lokal.hour >= FESTHALTEN_AB_STUNDE:
            hat_fremd, hat_eigen = self.hat_prognosen(heute)
            hat_roh = (self._tage.get(heute) or {}).get("eigen_roh") is not None
            if not (hat_fremd and hat_eigen and hat_roh):
                slots = tagesslots(heute)
                fremd_name: str | None = None
                fremd: dict[str, float] | None = None
                eigen: dict[str, float] | None = None
                eigen_roh: dict[str, float] | None = None
                kennung: str | None = None
                if not hat_fremd:
                    try:
                        detailed = sched._solcast_detailed(self._hass, config)
                        if detailed:
                            fremd = reihe_exakt(detailed, slots)
                            fremd_name = "solcast_solar"
                        else:
                            wh = await sched._async_solar_forecast_wh(self._hass, "forecast_solar")
                            if wh:
                                fremd = reihe_aus_stunden(wh, slots)
                                fremd_name = "forecast_solar"
                    except Exception as err:  # noqa: BLE001
                        self._letzter_fehler = f"Fremdprognose: {err}"
                if provider is not None:
                    try:
                        if not hat_eigen:
                            eigen = reihe_exakt(provider.halbstunden(jetzt), slots)
                        if not hat_roh:
                            eigen_roh = reihe_exakt(provider.halbstunden(jetzt, roh=True), slots)
                            kennung = provider.modellkennung()
                    except Exception as err:  # noqa: BLE001
                        self._letzter_fehler = f"Eigene Prognose: {err}"
                if self.festhalten(
                    heute, jetzt, fremd_name, fremd or None, eigen or None,
                    eigen_roh or None, kennung if isinstance(kennung, str) else None,
                ):
                    geaendert = True

        # Messung: heute laufend, gestern bis es vollständig ist.
        for datum in (gestern, heute):
            tag = self._tage.get(datum)
            if tag is None or tag.get("vollstaendig"):
                continue
            slots = [datetime.fromisoformat(s) for s in tag.get("slots") or []]
            if not slots:
                continue
            try:
                from .schedule_archive import async_ist_verlauf

                verlauf = await async_ist_verlauf(
                    self._hass, self._entry_id, slots[0], slots[-1] + _SLOT
                )
                reihen = verlauf.get("reihen") or {}
            except Exception as err:  # noqa: BLE001
                self._letzter_fehler = f"Messung: {err}"
                continue
            gemessen, bis = messung_aus_punkten(reihen.get("pv_leistung") or [], slots)
            if gemessen:
                # Netzleistung positiv = Einspeisung; messung_aus_punkten
                # schneidet unter 0 ab, übrig bleibt genau der Export.
                netz, _ = messung_aus_punkten(reihen.get("netzleistung") or [], slots)
                soc, _ = messung_aus_punkten(reihen.get("ladestand") or [], slots)
                self.messung_eintragen(datum, gemessen, bis, netz, soc)
                geaendert = True

        vorher = len(self._tage)
        self.aufraeumen(jetzt)
        if geaendert or len(self._tage) != vorher:
            await self.async_save(jetzt)
        if geaendert and provider is not None:
            try:
                provider.kalibrieren(self.lerntage())
            except Exception:  # noqa: BLE001 — die Kalibrierung darf den Takt nicht kippen
                _LOGGER.debug("Kalibrierung der eigenen Prognose fehlgeschlagen", exc_info=True)
