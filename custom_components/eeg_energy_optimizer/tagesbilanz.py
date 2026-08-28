"""Tagesbilanz — wie gut war die Prognose des abgeschlossenen Tages?

Ersetzt die Block-Outcomes der abgeschafften Zustands-Heuristik. Die alten
Ergebnisse hingen an Phasen mit Anfang und Ende; der Fahrplan hat keine, wohl
aber einen natürlichen Tagesabschluss.

**Beide Zutaten liegen schon auf der Anlage**, sie wurden nur nie verbunden:

- **Ist:** ``schedule_archive.async_ist_verlauf`` liest 5-Minuten-Mittelwerte
  aus dem Recorder (PV, Hausverbrauch, Netz, Ladestand). Der Recorder hält
  diese Kurzzeitstatistiken zehn Tage.
- **Prognose:** Das Plan-Archiv legt alle 15 Minuten den kompletten Fahrplan
  ab, inklusive PV- und Verbrauchsreihe je Slot, und hält ihn sieben Tage.
  ``ScheduleArchive.async_lies_vor`` holt daraus den Plan vom Vorabend.

Gemessen wird mit **zwei Vorläufen**, weil sich Prognose-Anbieter genau darin
unterscheiden: der Plan vom Vorabend (Vorlauf 0–24 h) und der von zwei Tagen
vorher (24–48 h). Zwei Zeilen pro Tag, gleiche Ist-Werte, verschiedene
Prognose. Der 48-Stunden-Vorlauf existiert nur, wenn die Prognose so weit
reichte — **Forecast.Solar liefert nur bis zum Ende des Folgetags**, dort
fehlt die zweite Zeile deshalb regelmäßig. Das ist kein Fehler, sondern das
Ergebnis.

Gerechnet wird über **anteilige Überlappung**: jeder Messpunkt und jeder Slot
deckt ein Intervall ab, und nur der Teil, der in das Tagesfenster fällt,
zählt. Damit sind Slot-Raster, die nicht auf Mitternacht fallen, korrekt
behandelt — und die aufsummierte Überlappung ist gleichzeitig das Maß für die
Abdeckung. Fehlt zu viel, wird die Zeile **nicht** gesendet: eine Bilanz, die
volle Prognose gegen halbe Messung stellt, verdirbt jede MAE-Auswertung
stiller, als ein fehlender Tag es täte.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Auflösung der Recorder-Kurzzeitstatistik (siehe async_ist_verlauf).
IST_AUFLOESUNG_MIN = 5

# Mindestabdeckung des Tagesfensters, ab der eine Bilanz gesendet wird.
# 0,95 lässt gut eine Stunde Lücke zu — ein Neustart samt Nachlauf, aber
# nicht einen halben Tag Sensorausfall.
MIN_ABDECKUNG = 0.95

# Prognose-Vorläufe: (event_type, Stunden vor dem Tagesbeginn, zu denen der
# ausgewertete Plan gerechnet wurde). 0 = Plan vom Vorabend.
VORLAEUFE: tuple[tuple[str, int], ...] = (
    ("fahrplan_tag", 0),
    ("fahrplan_tag_48h", 24),
)


def tagesfenster(jetzt: datetime) -> tuple[datetime, datetime]:
    """Der zuletzt abgeschlossene Kalendertag als ``[von, bis)``.

    Von Mitternacht zwölf Stunden zurück landet mittags im Vortag — auch an
    Zeitumstellungstagen, denn der Versatz beträgt höchstens eine Stunde. Die
    Normalisierung danach macht daraus dessen Mitternacht. ``timedelta(days=1)``
    wäre hier falsch: das sind exakt 24 Stunden, und an einem 23- oder
    25-stündigen Tag landet man damit um 23:00 oder 01:00 und nach dem
    ``replace`` womöglich einen Tag daneben. Ein weiter Sprung wie 36 Stunden
    wäre ebenso falsch — er landet im **Vorvortag**.

    Das Fenster ist damit korrekt 23, 24 oder 25 Stunden lang, und die
    Abdeckungsrechnung misst gegen diese echte Länge statt gegen pauschale 24.

    Gemeinsam genutzt vom Nachttimer und vom Diagnoseknopf im Panel — sonst
    könnten beide verschiedene Tage meinen.
    """
    bis = jetzt.replace(hour=0, minute=0, second=0, microsecond=0)
    von = (bis - timedelta(hours=12)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return von, bis


def _spanne_h(von: datetime, bis: datetime) -> float:
    """Zeitspanne in Stunden, über UTC gerechnet.

    Fallstrick, der eine Zeitumstellung sonst verschluckt: Python subtrahiert
    zwei aware datetimes mit **demselben** ``tzinfo``-Objekt naiv über die
    Wall Clock und ignoriert die Zone. Für den 25-stündigen Tag der
    Winterzeitumstellung käme so 24 heraus, für den 23-stündigen ebenfalls.
    Nach UTC umgerechnet ist die Differenz eindeutig — und genau diese Länge
    ist die Bezugsgröße für die Abdeckung.
    """
    return (
        bis.astimezone(timezone.utc) - von.astimezone(timezone.utc)
    ).total_seconds() / 3600.0


def _parse(stempel: Any, tzinfo: Any) -> datetime | None:
    """ISO-String oder datetime → aware datetime in der Zielzeitzone."""
    if isinstance(stempel, datetime):
        roh = stempel
    else:
        try:
            roh = datetime.fromisoformat(str(stempel).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if roh.tzinfo is None:
        return roh.replace(tzinfo=tzinfo)
    return roh


def summe_im_fenster(
    punkte: list,
    res_min: float,
    von: datetime,
    bis: datetime,
    *,
    nur_positiv: bool = False,
) -> tuple[float, float]:
    """Energie im Fenster und abgedeckte Stunden.

    ``punkte`` ist eine Liste aus ``[zeitstempel, wert]`` (Ist-Verlauf) oder
    aus Dicts mit ``t`` (Plan-Slots) — beide tragen den **Mittelwert** über
    das folgende Intervall von ``res_min`` Minuten. Bei Intervall-Mittelwerten
    ist die Rechtecksumme die exakte Integration, kein Trapez nötig.

    Gezählt wird nur der Teil eines Intervalls, der in ``[von, bis)`` fällt.
    Rückgabe: ``(kWh, abgedeckte Stunden)`` — der zweite Wert misst, wie viel
    des Fensters überhaupt Daten hatte.
    """
    kwh = 0.0
    stunden = 0.0
    res = timedelta(minutes=res_min)
    for punkt in punkte or []:
        if isinstance(punkt, dict):
            stempel, wert = punkt.get("t"), punkt.get("wert")
        else:
            try:
                stempel, wert = punkt[0], punkt[1]
            except (TypeError, IndexError):
                continue
        start = _parse(stempel, von.tzinfo)
        if start is None or wert is None:
            continue
        ende = start + res
        # Überlappung mit dem Fenster
        ab = max(start, von)
        bis_ = min(ende, bis)
        if bis_ <= ab:
            continue
        anteil_h = _spanne_h(ab, bis_)
        try:
            zahl = float(wert)
        except (TypeError, ValueError):
            continue
        if nur_positiv:
            zahl = max(0.0, zahl)
        kwh += zahl * anteil_h
        stunden += anteil_h
    return kwh, stunden


def _reihe(reihen: dict, name: str) -> list:
    werte = (reihen or {}).get(name)
    return werte if isinstance(werte, list) else []


def _im_fenster(
    punkte: list, von: datetime, bis: datetime
) -> list[tuple[datetime, float]]:
    """Lesbare Punkte einer Reihe innerhalb ``[von, bis)`` als (Zeit, Wert)."""
    treffer: list[tuple[datetime, float]] = []
    for punkt in punkte or []:
        try:
            stempel, roh = punkt[0], punkt[1]
        except (TypeError, IndexError, KeyError):
            continue
        start = _parse(stempel, von.tzinfo)
        if start is None or not (von <= start < bis):
            continue
        try:
            treffer.append((start, float(roh)))
        except (TypeError, ValueError):
            continue
    return treffer


def ist_kennzahlen(verlauf: dict, von: datetime, bis: datetime) -> dict | None:
    """Tageswerte aus dem gemessenen Verlauf. ``None`` bei zu großer Lücke.

    ``grid_export_kwh`` zählt nur die Einspeisung: die Netzleistung ist
    vorzeichenbehaftet (positiv = Einspeisung), und Bezug gegen Einspeisung
    aufzurechnen würde beides unsichtbar machen.
    """
    reihen = (verlauf or {}).get("reihen") or {}
    fenster_h = _spanne_h(von, bis)
    if fenster_h <= 0:
        return None

    pv_kwh, pv_h = summe_im_fenster(
        _reihe(reihen, "pv_leistung"), IST_AUFLOESUNG_MIN, von, bis, nur_positiv=True
    )
    cons_kwh, cons_h = summe_im_fenster(
        _reihe(reihen, "hausverbrauch"), IST_AUFLOESUNG_MIN, von, bis, nur_positiv=True
    )
    export_kwh, netz_h = summe_im_fenster(
        _reihe(reihen, "netzleistung"), IST_AUFLOESUNG_MIN, von, bis, nur_positiv=True
    )

    # Der Hausverbrauch ist die Bezugsgröße für die Abdeckung: er wird aus PV,
    # Batterie und Netz gerechnet und ist damit nur da, wenn alle drei da sind.
    abdeckung = cons_h / fenster_h
    if abdeckung < MIN_ABDECKUNG:
        _LOGGER.debug(
            "Tagesbilanz: zu wenig Messwerte (%.0f %% des Tages), keine Meldung",
            abdeckung * 100,
        )
        return None

    # Höchste Einspeiseleistung des Tages (5-Minuten-Mittel, nicht die
    # Momentanspitze — die kennt der Recorder in dieser Statistik nicht).
    spitze: float | None = None
    for start, wert in _im_fenster(_reihe(reihen, "netzleistung"), von, bis):
        if spitze is None or wert > spitze:
            spitze = wert

    # Ladestand am Anfang und am Ende des Tages, in Zeitreihenfolge.
    soc_start: int | None = None
    soc_ende: int | None = None
    for start, wert in sorted(
        _im_fenster(_reihe(reihen, "ladestand"), von, bis), key=lambda p: p[0]
    ):
        gerundet = int(round(wert))
        if soc_start is None:
            soc_start = gerundet
        soc_ende = gerundet

    return {
        "pv_kwh": round(pv_kwh, 3),
        "consumption_kwh": round(cons_kwh, 3),
        "grid_export_kwh": round(export_kwh, 3),
        "peak_power_kw": None if spitze is None else round(max(0.0, spitze), 3),
        "soc_start_pct": soc_start,
        "soc_end_pct": soc_ende,
        # Minuten mit Messwerten — macht eine Lücke in der Zeile selbst
        # sichtbar, auch wenn sie unter der Verwerfungsschwelle blieb.
        "minuten": int(round(min(cons_h, fenster_h) * 60)),
        "abdeckung": round(abdeckung, 4),
        "pv_stunden": round(pv_h, 3),
        "netz_stunden": round(netz_h, 3),
    }


def plan_kennzahlen(eintrag: dict, von: datetime, bis: datetime) -> dict | None:
    """Prognostizierte Tagessummen aus einem archivierten Plan.

    ``None``, wenn der Plan das Tagesfenster nicht (fast) vollständig deckt —
    das ist der Normalfall für den 48-Stunden-Vorlauf bei Prognose-Anbietern,
    die nur bis zum Ende des Folgetags reichen.
    """
    plan = (eintrag or {}).get("plan") or {}
    slots = plan.get("slots")
    if not isinstance(slots, list) or not slots:
        return None
    try:
        res_min = float(plan.get("time_res_min") or 15)
    except (TypeError, ValueError):
        res_min = 15.0
    if res_min <= 0:
        return None

    fenster_h = _spanne_h(von, bis)
    if fenster_h <= 0:
        return None

    pv = [{"t": s.get("t"), "wert": s.get("PV")} for s in slots if isinstance(s, dict)]
    cons = [
        {"t": s.get("t"), "wert": s.get("consumption")}
        for s in slots
        if isinstance(s, dict)
    ]

    pv_kwh, pv_h = summe_im_fenster(pv, res_min, von, bis, nur_positiv=True)
    cons_kwh, cons_h = summe_im_fenster(cons, res_min, von, bis, nur_positiv=True)

    abdeckung = min(pv_h, cons_h) / fenster_h
    if abdeckung < MIN_ABDECKUNG:
        return None

    return {
        "pv_kwh": round(pv_kwh, 3),
        "consumption_kwh": round(cons_kwh, 3),
        "abdeckung": round(abdeckung, 4),
        "gespeichert": (eintrag or {}).get("gespeichert"),
    }


def baue_outcome(
    event_type: str,
    von: datetime,
    bis: datetime,
    ist: dict,
    plan: dict | None,
) -> dict:
    """OutcomePayload nach types.ts — Schema unverändert gegenüber der
    produktiven Integration, damit beide Varianten in derselben Tabelle
    auswertbar bleiben.

    ``terminated_by`` trägt die Herkunft der Prognose statt eines
    Abbruchgrunds: bei einer Tagesbilanz gibt es keinen Abbruch, aber die
    Angabe, ob überhaupt eine Prognose vorlag, ist beim Nachsehen im
    Dashboard genau die Frage.
    """
    return {
        "event_type": event_type,
        "started_at": von.isoformat(),
        "ended_at": bis.isoformat(),
        "duration_minutes": ist.get("minuten"),
        "grid_export_kwh": ist.get("grid_export_kwh"),
        "peak_power_kw": ist.get("peak_power_kw"),
        "soc_start_pct": ist.get("soc_start_pct"),
        "soc_end_pct": ist.get("soc_end_pct"),
        "predicted_pv_kwh": None if plan is None else plan.get("pv_kwh"),
        "actual_pv_kwh": ist.get("pv_kwh"),
        "predicted_consumption_kwh": None if plan is None else plan.get("consumption_kwh"),
        "actual_consumption_kwh": ist.get("consumption_kwh"),
        "terminated_by": "tagesende" if plan is not None else "tagesende_ohne_plan",
    }


async def async_baue_tagesbilanzen(
    hass: Any,
    entry_id: str,
    archiv: Any,
    von: datetime,
    bis: datetime,
) -> list[dict]:
    """Bilanzen für das Fenster ``[von, bis)`` — eine Zeile je Vorlauf.

    Reihenfolge der Arbeit: erst der Ist-Verlauf (eine Recorder-Abfrage für
    beide Zeilen), dann je Vorlauf der passende archivierte Plan. Fehlt der
    Ist-Verlauf, entsteht gar keine Zeile — ohne Messung ist eine Bilanz
    wertlos. Fehlt nur ein Plan, wird die Zeile trotzdem gemeldet: die
    gemessenen Tageswerte sind auch ohne Prognose brauchbar, und die
    MAE-Auswertung im Backend überspringt Zeilen ohne ``predicted``.
    """
    from .schedule_archive import async_ist_verlauf

    try:
        verlauf = await async_ist_verlauf(hass, entry_id, von, bis)
    except Exception:  # noqa: BLE001 - Telemetrie darf den Zyklus nie kippen
        _LOGGER.warning("Tagesbilanz: Ist-Verlauf nicht lesbar", exc_info=True)
        return []
    if verlauf.get("fehler"):
        _LOGGER.debug("Tagesbilanz: %s", verlauf["fehler"])
        return []

    ist = ist_kennzahlen(verlauf, von, bis)
    if ist is None:
        return []

    bilanzen: list[dict] = []
    for event_type, vorlauf_h in VORLAEUFE:
        plan = None
        if archiv is not None:
            try:
                eintrag = await archiv.async_lies_vor(von - timedelta(hours=vorlauf_h))
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Tagesbilanz: Archiv für Vorlauf %d h nicht lesbar",
                    vorlauf_h, exc_info=True,
                )
                eintrag = None
            if eintrag is not None:
                plan = plan_kennzahlen(eintrag, von, bis)
        if plan is None and vorlauf_h > 0:
            # Ohne Prognose ist die zweite Zeile ein Duplikat der ersten —
            # sie trägt nur die identischen Ist-Werte. Weglassen.
            _LOGGER.debug(
                "Tagesbilanz: kein Plan mit %d h Vorlauf, der den Tag deckt",
                vorlauf_h,
            )
            continue
        bilanzen.append(baue_outcome(event_type, von, bis, ist, plan))
    return bilanzen
