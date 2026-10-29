"""Spitzenreserve: wie viel Batterie die Spitzenkappung zurückhalten soll.

Im Normalbetrieb deckt die Batterie den Hausverbrauch selbst, Spitzen
inklusive — eine Spitze entsteht erst, wenn sie leer ist. Damit sie dann
noch etwas hat, hält die Spitzenkappung (schedule_executor.py) unterhalb
einer Schwelle ``Mindest-SOC + Reserve`` die Batterie für den Teil zurück,
der über der Grenze liegt; den Grundverbrauch holt das Haus dort aus dem
Netz. Dieses Modul rechnet die Reserve.

**Gerechnet, nicht geschätzt.** Für jeden Slot des Horizonts ist die
Reserve die Energie, die von dort bis zum nächsten PV-Überschuss über der
Grenze bezogen worden wäre — gemessen an den letzten ``TAGE`` Tagen zur
selben Uhrzeit, als ``QUANTIL``-Quantil über diese Tage. Je Viertelstunde
zählt ``min(max(0, Haus − PV − Grenze), Entladeleistung)``: mehr als die
Batterie abgeben kann, kann sie auch nicht kappen (dafür gibt es das
Abschalten von Verbrauchern, spitzenkappung.py).

Warum ein Quantil und kein Mittelwert: Die Reserve soll die Nacht mit der
Spitze tragen, nicht die durchschnittliche. Lädt das Auto eine Nacht pro
Woche, gäbe der Mittelwert ein Siebtel dessen, was in genau dieser Nacht
fehlt — und eine einzige verfehlte Viertelstunde setzt die Spitze für den
ganzen Monat. Nach oben begrenzt die Maximalreserve.

Warum die Kurve über die Nacht fällt: Sie zählt nur, was von JETZT bis zum
nächsten Überschuss noch kommt. Kurz vor Sonnenaufgang ist kaum noch etwas
reserviert — was übrig bleibt, hätte das Haus sonst teuer aus dem Netz
gekauft, während die PV die Batterie am Morgen wieder füllt.

Gemessen wird ``Haus − PV`` und nicht der Netzbezug: Den Netzbezug hat die
Batterie schon geformt, und was sie gekappt hat, sähe man dort nicht mehr.
Die Werte kommen aus der Kurzzeitstatistik des Recorders (5-Minuten-Mittel
der eigenen Sensoren *Hausverbrauch* und *PV-Leistung*, zehn Tage) und
werden hier als Viertelstundenmittel auf ``AUFBEWAHRUNG_TAGE`` fortgeführt —
die Statistik steht also schon am ersten Tag mit zehn Tagen bereit.

Die Rechnung (``reservekurve``) ist eine reine Funktion; die Klasse hält
den Verlauf und holt ihn stündlich nach.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

try:  # pragma: no cover - im Testlauf ohne HA
    from homeassistant.helpers.storage import Store
except ImportError:  # pragma: no cover
    Store = None  # type: ignore[assignment,misc]

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_SPITZENKAPPUNG_RESERVE_MIN_PCT = "spitzenkappung_reserve_min_pct"
CONF_SPITZENKAPPUNG_RESERVE_MAX_PCT = "spitzenkappung_reserve_max_pct"
# Vorgabe 0: Die Statistik legt die Reserve dorthin, wo die Spitzen liegen.
# Eine feste Mindestreserve hält die Batterie auch in Nächten ohne Spitze
# zurück — sinnvoll nur für etwas, das die letzten Wochen noch nicht kennen.
DEFAULT_RESERVE_MIN_PCT = 0.0
DEFAULT_RESERVE_MAX_PCT = 30.0
RESERVE_MAX_GRENZE_PCT = 80.0

TAGE = 28
QUANTIL = 0.9
# Weniger vergleichbare Tage ergeben kein Quantil, sondern einen Zufall —
# dann gilt nur die Mindestreserve.
MIN_TAGE = 7
# Ein Vergleichstag zählt nur, wenn er den Zeitraum zu diesem Anteil
# abdeckt (HA-Neustart, Recorder-Lücke).
MIN_ABDECKUNG = 0.8
AUFBEWAHRUNG_TAGE = 35
VIERTELSTUNDE = timedelta(minutes=15)
SLOT_H = 0.25
# Verluste zwischen Zelle und Zähler: um 1 kWh am Zähler zu kappen, gibt
# die Batterie etwas mehr ab.
ENTLADE_WIRKUNGSGRAD = 0.95
AKTUALISIEREN_S = 3600
_SCHLUESSEL = "%Y-%m-%dT%H:%M"


def schluessel(t_lokal: datetime) -> str:
    """Viertelstunden-Schlüssel als Wanduhrzeit (naiv, Ortszeit)."""
    return t_lokal.replace(tzinfo=None).strftime(_SCHLUESSEL)


def _zahl(config: dict, key: str, vorgabe: float) -> float:
    raw = config.get(key)
    if raw is None or raw == "":
        return vorgabe
    try:
        wert = float(raw)
    except (TypeError, ValueError):
        return vorgabe
    return wert if math.isfinite(wert) else vorgabe


def reserve_grenzen_pct(config: dict) -> tuple[float, float]:
    """(Mindest-, Maximalreserve) in Prozent der Kapazität, geklemmt.

    Die Mindestreserve liegt nie über der maximalen — sonst wäre unklar,
    welche gilt; die maximale gewinnt, sie ist die Zusage an den Benutzer.
    """
    hoch = max(0.0, min(RESERVE_MAX_GRENZE_PCT, _zahl(
        config, CONF_SPITZENKAPPUNG_RESERVE_MAX_PCT, DEFAULT_RESERVE_MAX_PCT
    )))
    tief = max(0.0, min(hoch, _zahl(
        config, CONF_SPITZENKAPPUNG_RESERVE_MIN_PCT, DEFAULT_RESERVE_MIN_PCT
    )))
    return tief, hoch


def ueberschuss_je_slot(
    slot_starts: Sequence[datetime],
    stamps: Sequence[datetime],
    pv_kw: Sequence[float],
    verbrauch_kw: Sequence[float],
) -> list[bool]:
    """Erwartet der Slot PV-Überschuss? Stufenweise aus den Stützpunkten.

    Ein Slot gilt als Überschuss, wenn die PV-Prognose über dem
    prognostizierten Verbrauch liegt — dort lädt die Batterie, und die
    Reserve muss bis dorthin reichen.
    """
    ergebnis: list[bool] = []
    j = 0
    n = min(len(stamps), len(pv_kw), len(verbrauch_kw))
    for t in slot_starts:
        while j + 1 < n and stamps[j + 1] <= t:
            j += 1
        if n == 0 or stamps[0] > t:
            ergebnis.append(False)
            continue
        ergebnis.append(float(pv_kw[j]) > float(verbrauch_kw[j]))
    return ergebnis


def _quantil(werte: list[float], q: float) -> float:
    """Quantil nach dem Rang (nearest rank) — ohne Interpolation.

    Bei 28 Tagen und 0,9 ist das der drittgrößte Wert: ein Tag, den es
    wirklich gab, nicht ein Mittel zwischen zwei Tagen.
    """
    if not werte:
        return 0.0
    sortiert = sorted(werte)
    rang = max(1, math.ceil(q * len(sortiert)))
    return sortiert[rang - 1]


def verlauf_nach_zeit(verlauf: dict[str, float]) -> dict[datetime, float]:
    """Schlüssel als naive Wanduhrzeit statt Text — für ``reservekurve``.

    Einmal je Abruf statt jede Minute: Die Kurve schlägt 28 Tage × 192
    Viertelstunden nach, und das Formatieren der Schlüssel war der teure
    Teil davon.
    """
    ergebnis: dict[datetime, float] = {}
    for k, v in verlauf.items():
        try:
            ergebnis[datetime.strptime(k, _SCHLUESSEL)] = float(v)
        except (TypeError, ValueError):
            continue
    return ergebnis


def reservekurve(
    verlauf: dict[datetime, float],
    slot_starts_lokal: Sequence[datetime],
    ueberschuss: Sequence[bool],
    grenze_kw: float,
    entlade_kw: float,
    *,
    tage: int = TAGE,
    quantil: float = QUANTIL,
    min_tage: int = MIN_TAGE,
) -> tuple[list[float | None], int]:
    """Reserve in kWh je Slot (Bedarf ab dem Slot-BEGINN) und genutzte Tage.

    ``verlauf``: naive Wanduhrzeit des Viertelstunden-Beginns
    (``verlauf_nach_zeit``) → mittlere Nettolast ``Haus − PV`` in kW. Für
    jeden Vergleichstag ``k`` wird der ganze Horizont um ``k`` Kalendertage
    zurückgeschoben (Wanduhrzeit, die Zeitumstellung verschiebt also nichts)
    und rückwärts aufsummiert: Ein Überschuss-Slot setzt die Summe auf null,
    jeder andere legt seinen Anteil über der Grenze drauf.

    Ein Vergleichstag zählt für einen Slot nur, wenn er das FENSTER ab
    diesem Slot (bis zum nächsten Überschuss) zu ``MIN_ABDECKUNG`` abdeckt —
    nicht den ganzen Horizont: Für gestern liegt die zweite Hälfte eines
    48-h-Horizonts noch in der Zukunft, und eine Lücke genau in der Nacht,
    um die es geht, darf nicht als „keine Spitze" durchgehen. Das Quantil
    je Slot über die gültigen Tage ist die Reserve; ``None``, wo weniger als
    ``min_tage`` gültig sind. Die zweite Zahl ist die Anzahl gültiger Tage
    am ersten Slot ohne Überschuss (der, mit dem der Plan rechnet).
    """
    n = len(slot_starts_lokal)
    if n == 0 or len(ueberschuss) != n:
        return [], 0
    grenze = max(0.0, float(grenze_kw))
    deckel = max(0.0, float(entlade_kw))
    naiv = [t.replace(tzinfo=None) for t in slot_starts_lokal]
    # Je Slot die Werte der gültigen Tage.
    werte: list[list[float]] = [[] for _ in range(n)]
    for k in range(1, tage + 1):
        verschiebung = timedelta(days=k)
        summe = 0.0
        vorhanden = 0
        gesamt = 0
        for i in range(n - 1, -1, -1):
            if ueberschuss[i]:
                summe, vorhanden, gesamt = 0.0, 0, 0
                werte[i].append(0.0)
                continue
            wert = verlauf.get(naiv[i] - verschiebung)
            gesamt += 1
            if wert is not None:
                vorhanden += 1
                summe += min(max(float(wert) - grenze, 0.0), deckel) * SLOT_H
            if vorhanden >= MIN_ABDECKUNG * gesamt:
                werte[i].append(summe)
    kurve: list[float | None] = []
    for liste in werte:
        if len(liste) < min_tage:
            kurve.append(None)
        else:
            kurve.append(round(_quantil(liste, quantil) / ENTLADE_WIRKUNGSGRAD, 4))
    erster = next((i for i in range(n) if not ueberschuss[i]), None)
    genutzt = len(werte[erster]) if erster is not None else len(werte[0])
    return kurve, genutzt


def kurve_in_pct(
    kurve_kwh: Sequence[float | None],
    kapazitaet_kwh: float,
    min_pct: float,
    max_pct: float,
) -> list[float]:
    """kWh je Slot → Prozent der Kapazität, zwischen Mindest- und Maximalreserve.

    Wo die Statistik nicht reicht (``None``), gilt die Mindestreserve — eine
    Reserve, die keiner gemessen hat, wird nicht erfunden.
    """
    if not kapazitaet_kwh or kapazitaet_kwh <= 0:
        return [round(min_pct, 1)] * len(kurve_kwh)
    return [
        round(min_pct, 1) if kwh is None
        else round(max(min_pct, min(max_pct, 100.0 * kwh / kapazitaet_kwh)), 1)
        for kwh in kurve_kwh
    ]


def plan_reserve_pct(
    kurve_pct: Sequence[float],
    ueberschuss: Sequence[bool],
    slot_starts_lokal: Sequence[datetime] | None = None,
) -> float | None:
    """Die Reserve, die der Plan halten soll, bevor der Ladestand sie deckelt.

    Ohne Überschuss die von jetzt — jede Minute neu gerechnet, sinkt sie
    über die Nacht mit der Kurve. Im Überschuss das Höchste bis Mitternacht:
    Das ist die Reserve, mit der der Abend beginnt, und ein kurzes Fenster
    ohne Überschuss zu Mittag (Kochen, eine Wolke in der Prognose) verdeckt
    sie nicht. Der Aufrufer begrenzt sie noch auf den aktuellen Ladestand
    (siehe ``schedule._spitzenreserve_fuer_plan``).
    """
    if not kurve_pct:
        return None
    if not ueberschuss[0]:
        return kurve_pct[0]
    if slot_starts_lokal:
        mitternacht = (slot_starts_lokal[0] + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        bis_mitternacht = [
            w for w, t in zip(kurve_pct, slot_starts_lokal) if t < mitternacht
        ]
        return max(bis_mitternacht) if bis_mitternacht else kurve_pct[0]
    return max(kurve_pct)


def reserve_zu(kurve: Sequence[Sequence[Any]] | None, jetzt: datetime) -> float | None:
    """Reserve in Prozent zum Zeitpunkt ``jetzt``, linear im laufenden Slot.

    ``kurve``: ``[[iso_start, pct], …]`` aus dem Plan. Der Wert gilt am
    Slot-Beginn; bis zum nächsten fällt er gleichmäßig — im Slot wird die
    Energie, die er braucht, ja gerade verbraucht.
    """
    if not kurve:
        return None
    punkte: list[tuple[datetime, float]] = []
    for eintrag in kurve:
        try:
            punkte.append((datetime.fromisoformat(str(eintrag[0])), float(eintrag[1])))
        except (TypeError, ValueError, IndexError):
            return None
    if jetzt < punkte[0][0]:
        return None
    for (t0, w0), (t1, w1) in zip(punkte, punkte[1:]):
        if t0 <= jetzt < t1:
            anteil = (jetzt - t0).total_seconds() / max((t1 - t0).total_seconds(), 1.0)
            return round(w0 + (w1 - w0) * anteil, 2)
    t_letzt, w_letzt = punkte[-1]
    if jetzt < t_letzt + VIERTELSTUNDE:
        return w_letzt
    return None


def viertelstunden_aus_5min(
    haus: Sequence[tuple[datetime, float]],
    pv: Sequence[tuple[datetime, float]],
) -> dict[datetime, float]:
    """5-Minuten-Mittel → Viertelstundenmittel der Nettolast ``Haus − PV``.

    Schlüssel ist der Viertelstunden-Beginn (UTC). Fehlt die PV zu einem
    Zeitpunkt, zählt sie als 0 — nachts meldet sie sich ab. Eine
    Viertelstunde braucht mindestens zwei ihrer drei Hausverbrauchswerte.
    """
    pv_je = {t: w for t, w in pv}
    summen: dict[datetime, list[float]] = {}
    for t, w in haus:
        start = t.replace(minute=t.minute - t.minute % 15, second=0, microsecond=0)
        summen.setdefault(start, []).append(float(w) - float(pv_je.get(t, 0.0)))
    return {t: sum(v) / len(v) for t, v in summen.items() if len(v) >= 2}


class Spitzenreserve:
    """Hält den Nettolast-Verlauf und holt ihn stündlich aus dem Recorder."""

    def __init__(self, hass: Any, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store: Any = (
            Store(hass, 1, f"{DOMAIN}_{entry_id}_spitzenreserve")
            if Store is not None and hass is not None
            else None
        )
        self.verlauf: dict[str, float] = {}
        self._nach_zeit: dict[datetime, float] | None = None
        self.letzter_abruf: datetime | None = None
        self.fehler: str | None = None

    async def async_load(self) -> None:
        if self._store is None:
            return
        try:
            daten = await self._store.async_load()
        except Exception:  # noqa: BLE001 — ohne Speicher lernt sie neu
            _LOGGER.debug("Spitzenreserve: Speicher nicht lesbar", exc_info=True)
            return
        if isinstance(daten, dict) and isinstance(daten.get("verlauf"), dict):
            for k, v in daten["verlauf"].items():
                try:
                    self.verlauf[str(k)] = float(v)
                except (TypeError, ValueError):
                    continue
            self._nach_zeit = None

    def verlauf_nach_zeit(self) -> dict[datetime, float]:
        """Der Verlauf mit Zeit-Schlüsseln, gebaut einmal je Änderung."""
        if self._nach_zeit is None:
            self._nach_zeit = verlauf_nach_zeit(self.verlauf)
        return self._nach_zeit

    def _aufraeumen(self, jetzt_lokal: datetime) -> None:
        grenze = schluessel(jetzt_lokal - timedelta(days=AUFBEWAHRUNG_TAGE))
        for k in [k for k in self.verlauf if k < grenze]:
            del self.verlauf[k]
        self._nach_zeit = None

    def uebernehmen(self, werte: dict[datetime, float], tz: Any) -> int:
        """Viertelstundenmittel (UTC-Schlüssel) in den Verlauf schreiben."""
        neu = 0
        for t, w in werte.items():
            if not math.isfinite(w):
                continue
            k = schluessel(t.astimezone(tz))
            if k not in self.verlauf:
                neu += 1
            self.verlauf[k] = round(float(w), 3)
        self._nach_zeit = None
        return neu

    async def async_aktualisieren(self, jetzt: datetime | None = None) -> None:
        """Abgeschlossene Viertelstunden aus dem Recorder nachholen."""
        try:
            from homeassistant.util import dt as dt_util
        except ImportError:  # pragma: no cover
            return
        jetzt = jetzt or dt_util.now()
        tz = dt_util.get_default_time_zone()
        bis = jetzt.astimezone(timezone.utc).replace(second=0, microsecond=0)
        bis -= timedelta(minutes=bis.minute % 15)
        von = bis - timedelta(days=AUFBEWAHRUNG_TAGE)
        if self.verlauf:
            # Die letzte Stunde immer neu: eine Viertelstunde, die beim
            # letzten Abruf noch unvollständig war, wird so vollständig.
            try:
                juengster = datetime.strptime(max(self.verlauf), _SCHLUESSEL)
                juengster = juengster.replace(tzinfo=tz).astimezone(timezone.utc)
                von = max(von, juengster - timedelta(hours=1))
            except ValueError:
                pass
        try:
            haus, pv = await self._async_recorder(von, bis)
        except Exception as err:  # noqa: BLE001 — Statistik ist kein Aktor
            self.fehler = f"Recorder: {type(err).__name__}"
            _LOGGER.debug("Spitzenreserve: Recorder-Abruf fehlgeschlagen", exc_info=True)
            return
        if haus is None:
            self.fehler = "Sensor Hausverbrauch nicht gefunden"
            return
        self.uebernehmen(viertelstunden_aus_5min(haus, pv or []), tz)
        self._aufraeumen(jetzt.astimezone(tz))
        self.letzter_abruf = jetzt
        self.fehler = None
        if self._store is not None:
            self._store.async_delay_save(lambda: {"verlauf": dict(self.verlauf)}, 10)

    async def _async_recorder(
        self, von: datetime, bis: datetime
    ) -> tuple[list[tuple[datetime, float]] | None, list[tuple[datetime, float]] | None]:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(self._hass)
        prefix = f"{DOMAIN}_{self._entry_id}_"
        ids: dict[str, str] = {}
        for eintrag in er.async_entries_for_config_entry(registry, self._entry_id):
            uid = eintrag.unique_id or ""
            if uid in (prefix + "hausverbrauch", prefix + "pv_leistung"):
                ids[uid[len(prefix):]] = eintrag.entity_id
        if "hausverbrauch" not in ids:
            return None, None
        # Einheit ausdrücklich in kW: Ohne Angabe liefert HA die Statistik in
        # der ANZEIGE-Einheit des Sensors (siehe power_readings, Traun
        # 28.09.2026 — dort kam die Hauslast 1000-fach zu klein an).
        roh = await get_instance(self._hass).async_add_executor_job(
            statistics_during_period,
            self._hass, von, bis, set(ids.values()), "5minute",
            {"power": "kW"}, {"mean"},
        )
        roh = roh if isinstance(roh, dict) else {}

        def reihe(name: str) -> list[tuple[datetime, float]]:
            punkte = []
            for satz in roh.get(ids.get(name, ""), []):
                wert = satz.get("mean")
                stempel = satz.get("start")
                if wert is None or stempel is None:
                    continue
                if isinstance(stempel, (int, float)):
                    stempel = datetime.fromtimestamp(stempel, tz=timezone.utc)
                elif isinstance(stempel, str):
                    stempel = datetime.fromisoformat(stempel)
                punkte.append((stempel.astimezone(timezone.utc), float(wert)))
            return punkte

        return reihe("hausverbrauch"), reihe("pv_leistung")

    def async_start(self, entry: Any) -> None:
        try:
            from homeassistant.helpers.event import (
                async_call_later,
                async_track_time_interval,
            )
        except ImportError:  # pragma: no cover
            return

        async def _abruf(_now: Any = None) -> None:
            try:
                await self.async_aktualisieren()
            except Exception:  # noqa: BLE001 — der Takt darf nie kippen
                _LOGGER.exception("Spitzenreserve: Aktualisierung fehlgeschlagen")

        # Nicht sofort: Beim Start ist der Recorder womöglich noch nicht so
        # weit, und die Sensoren sind gerade erst angelegt.
        entry.async_on_unload(async_call_later(self._hass, 120, _abruf))
        entry.async_on_unload(
            async_track_time_interval(
                self._hass, _abruf, timedelta(seconds=AKTUALISIEREN_S)
            )
        )


def tage_im_verlauf(verlauf: dict[str, float]) -> int:
    """Wie viele Kalendertage der Verlauf berührt (Anzeige „lernt noch")."""
    return len({k[:10] for k in verlauf})



# ---------------------------------------------------------------------------
# Ausführung: der Entladedeckel im laufenden Betrieb (schedule_executor.py)
# ---------------------------------------------------------------------------

# Abstand unter der Grenze, auf den geregelt wird: Der Deckel wird alle 30 s
# nachgeführt, und das Gerät braucht ein paar Sekunden, bis er wirkt.
SICHERHEIT_KW = 0.3
# Einmal in der Reserve, bleibt die Kappung, bis der Ladestand die Schwelle
# um so viel überschreitet — sonst schaltete jedes Prozent Messrauschen hin
# und her.
HYSTERESE_PCT = 1.0
# Am Mindest-Ladestand gibt die Batterie nichts mehr ab, auch keine Spitze:
# Er bleibt die harte Untergrenze, die er im Plan schon ist.
BODEN_ABSTAND_PCT = 0.5
# Ein Deckel darunter ist halten: Bei einem Bedarf knapp über dem erlaubten
# Netzbezug pendelte er sonst mit jedem Rauschen zwischen 0 und 0,1 kW — ein
# Schreibvorgang und ein Protokolleintrag je Lauf. Die 0,3 kW Sicherheit
# unter der Grenze fangen den Rest auf.
DECKEL_MIN_KW = 0.2
DECKEL_HYSTERESE_KW = 0.2
# In der letzten Minute der Viertelstunde ist das Restbudget eine Division
# durch fast null; dort gilt die Grenze selbst.
_REST_MIN_S = 60.0


def erlaubter_netzbezug_kw(grenze_kw: float, laufend: dict[str, Any] | None) -> float:
    """Wie viel Netzbezug bis zum Ende der Viertelstunde noch erlaubt ist.

    Abgerechnet wird das Mittel der Viertelstunde. War der Anfang ruhig,
    darf der Rest mehr — ein Wasserkocher, der zwei Minuten läuft, kostet
    dann keine Batterie. Ohne Stand der laufenden Viertelstunde (Sensor
    noch nicht da) gilt die Grenze selbst.
    """
    ziel = max(0.0, grenze_kw - SICHERHEIT_KW)
    if not laufend:
        return ziel
    try:
        bisher_kwh = float(laufend.get("bisher_kw") or 0.0) * SLOT_H
        rest_s = float(laufend.get("rest_s") or 0.0)
    except (TypeError, ValueError):
        return ziel
    if rest_s < _REST_MIN_S:
        return ziel
    return max(0.0, (ziel * SLOT_H - bisher_kwh) / (rest_s / 3600.0))


def entladedeckel_kw(
    *,
    soc_pct: float | None,
    basis_pct: float,
    reserve_pct: float,
    war_aktiv: bool,
    grenze_kw: float,
    laufend: dict[str, Any] | None,
    bedarf_kw: float | None,
    entlade_max_kw: float | None,
    bisher_kw: float | None = None,
) -> tuple[bool, float | None, str]:
    """(aktiv, Deckel in kW oder None, Begründung) für diesen Lauf.

    ``bedarf_kw`` ist, was das Haus gerade braucht, ohne die Batterie:
    gemessener Netzbezug plus Batterieentladung (Haus + Heizstab − PV).
    Über der Schwelle ``basis + reserve`` ist die Batterie frei (None).
    Darunter deckt sie nur, was über dem erlaubten Netzbezug liegt; 0 heißt,
    sie hält ihre Reserve. Ohne Ladestand oder ohne Bedarf bleibt der
    Deckel, wie er ist (aktiv unverändert, Deckel ``None`` mit Grund
    „unverändert") — blind freizugeben hieße, die Reserve zu verlieren.
    """
    if soc_pct is None:
        return war_aktiv, None, "unverändert (Ladestand nicht lesbar)"
    schwelle = basis_pct + max(0.0, reserve_pct)
    aktiv = soc_pct <= (schwelle + HYSTERESE_PCT if war_aktiv else schwelle)
    if not aktiv:
        return False, None, f"frei (Ladestand {soc_pct:.0f} % über {schwelle:.0f} %)"
    if soc_pct <= basis_pct + BODEN_ABSTAND_PCT:
        return True, 0.0, f"Mindest-Ladestand {basis_pct:.0f} % erreicht — Batterie hält"
    if bedarf_kw is None:
        return True, None, "unverändert (Netz- oder Batteriemessung fehlt)"
    erlaubt = erlaubter_netzbezug_kw(grenze_kw, laufend)
    deckel = max(0.0, bedarf_kw - erlaubt) / ENTLADE_WIRKUNGSGRAD
    if entlade_max_kw is not None and entlade_max_kw > 0:
        deckel = min(deckel, entlade_max_kw)
    # Hysterese am geschriebenen Zustand: Wer hält, deckt erst ab
    # DECKEL_MIN_KW + DECKEL_HYSTERESE_KW; wer deckt, hält erst unter
    # DECKEL_MIN_KW. Ohne sie wanderte das Pendeln nur an die neue Schwelle.
    schwelle_kw = DECKEL_MIN_KW + (DECKEL_HYSTERESE_KW if bisher_kw == 0 else 0.0)
    if deckel < schwelle_kw:
        return True, 0.0, (
            f"hält Reserve (Ladestand {soc_pct:.0f} % ≤ {schwelle:.0f} %, "
            f"Bedarf {bedarf_kw:.2f} kW ≤ {erlaubt:.2f} kW aus dem Netz)"
        )
    return True, round(deckel, 2), (
        f"deckt {deckel:.2f} kW über {erlaubt:.2f} kW Netzbezug "
        f"(Grenze {grenze_kw:.2f} kW, Ladestand {soc_pct:.0f} % ≤ {schwelle:.0f} %)"
    )
