"""Archiv der gerechneten Fahrpläne — für die nachträgliche Fehlersuche.

Warum es das gibt: der Fahrplan wird minütlich neu gerechnet und überschreibt
sich selbst. Fällt später auf, dass die Anlage gestern abend etwas Seltsames
getan hat, ist der Plan, der dazu geführt hat, längst weg. Das Archiv legt
regelmäßig eine Kopie ab und packt sie auf Wunsch samt Einstellungen und
gemessenem Verlauf in ein ZIP, das man weitergeben kann.

Aufbau eines Eintrags: der Fahrplan enthält **alle** Eingangsgrößen des
LP-Modells je Slot (PV, Verbrauch, Batteriedeckel, alle drei Preisreihen) —
zusammen mit den Kopfwerten und den archivierten Einstellungen lässt sich
``opt()`` damit nachrechnen, ohne die Anlage zu befragen.

Menge: ein Plan sind rund 45 KB roh, gzip macht daraus etwa 8 KB. Im
Viertelstundentakt sind das ~5 MB je Woche — die Aufbewahrungsfrist steht auf
sieben Tagen.

Alles Dateisystem läuft im Executor: Schreiben, Aufräumen und ZIP-Bau würden
den Event-Loop sonst spürbar blockieren.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import shutil
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Verzeichnisname unterhalb des HA-Konfigurationsordners.
ARCHIV_ORDNER = "eeg_optimizer_plaene"

# Regelmäßiger Abstand zwischen zwei Einträgen. Die Slotlänge des Fahrplans
# ist 15 Minuten — feiner zu archivieren zeigt kaum Neues, kostet aber das
# Vierfache an Platz und Schreibzugriffen.
TAKT_MINUTEN = 15

# Zusätzlich wird gespeichert, wenn sich der Plan deutlich ändert: das sind
# genau die Läufe, die man hinterher sucht. Verglichen werden die nächsten
# zwei Stunden der Batterieleistung gegen den zuletzt archivierten Plan.
ABWEICHUNG_SLOTS = 8
ABWEICHUNG_KW = 0.5
# … aber nicht öfter als alle paar Minuten: ein Plan, der dauernd um die
# Schwelle pendelt, würde sonst jeden Minutenlauf ablegen und aus 5 MB je
# Woche das Sechzehnfache machen — auf einer Speicherkarte keine Kleinigkeit.
ABWEICHUNG_MINDESTABSTAND_MIN = 5

AUFBEWAHRUNG_TAGE = 7

# Was von den Einstellungen mitarchiviert wird. Allowlist statt Sperrliste:
# in die Konfiguration können jederzeit neue Schlüssel kommen, und ein
# vergessener Ausschluss wäre eine Adresse oder ein Zugang in einer Datei,
# die weitergegeben wird.
EINSTELLUNGEN_PRAEFIXE = (
    "schedule_",
    "peakshare_",
    "battery_",
    "grid_export_limit",
    "inverter_",
    "forecast_",
    "pv_",
)
EINSTELLUNGEN_KEYS = frozenset({
    "enable_peakshare",
    "discharge_power_kw",
    "setup_complete",
})
# Auch innerhalb der Präfixe nichts mitnehmen, was eine Anlage erreichbar
# macht oder eine Person benennt.
EINSTELLUNGEN_SPERRE = ("host", "ip", "port", "token", "password", "user", "serial")


# Gemessene Reihen, die zum Plan gehören. Dieselben Sensoren zeichnet das
# Panel als Ist-Verlauf ins Diagramm; im Archiv liegen sie als Zahlen daneben.
IST_SUFFIXE = (
    "pv_leistung",
    "hausverbrauch",
    "netzleistung",
    "batterieleistung",
    "fahrplan_batterieleistung",
    "fahrplan_netzleistung",
)


async def async_ist_verlauf(
    hass: Any, entry_id: str, von: datetime, bis: datetime
) -> dict[str, Any]:
    """Gemessene Leistungen aus dem Recorder, 5-Minuten-Mittel.

    Der Recorder führt für Sensoren mit ``state_class: measurement`` ohnehin
    Kurzzeitstatistiken und hält sie zehn Tage — genau der Zeitraum, den das
    Archiv abdeckt. Die Zustandshistorie wäre feiner, aber um Größenordnungen
    umfangreicher und für einen Plan-Ist-Vergleich im 15-Minuten-Raster
    unnötig genau.
    """
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )
        from homeassistant.helpers import entity_registry as er
    except ImportError:  # Testumgebung ohne HA
        return {"fehler": "recorder nicht verfügbar", "reihen": {}}

    from .const import DOMAIN

    prefix = f"{DOMAIN}_{entry_id}_"
    registry = er.async_get(hass)
    ids: dict[str, str] = {}
    for reg_entry in er.async_entries_for_config_entry(registry, entry_id):
        unique_id = reg_entry.unique_id or ""
        if not unique_id.startswith(prefix):
            continue
        suffix = unique_id[len(prefix):]
        if suffix in IST_SUFFIXE:
            ids[suffix] = reg_entry.entity_id

    # Der Ladestand kommt aus der Quell-Integration, nicht von uns.
    eintrag = hass.config_entries.async_get_entry(entry_id)
    if eintrag is not None:
        soc = (eintrag.data or {}).get("battery_soc_sensor")
        if soc:
            ids["ladestand"] = soc

    if not ids:
        return {"fehler": "keine Sensoren gefunden", "reihen": {}}

    roh = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        von,
        bis,
        set(ids.values()),
        "5minute",
        None,
        {"mean"},
    )
    roh = roh if isinstance(roh, dict) else {}

    reihen: dict[str, list[list[Any]]] = {}
    for name, entity_id in ids.items():
        punkte = []
        for satz in roh.get(entity_id, []):
            wert = satz.get("mean")
            if wert is None:
                continue
            stempel = satz.get("start")
            if isinstance(stempel, (int, float)):
                stempel = datetime.fromtimestamp(stempel, tz=von.tzinfo).isoformat()
            punkte.append([str(stempel), round(float(wert), 4)])
        reihen[name] = punkte

    return {
        "von": von.isoformat(),
        "bis": bis.isoformat(),
        "aufloesung": "5 min (Mittelwert)",
        "entity_ids": ids,
        "reihen": reihen,
    }


def einstellungen_filtern(config: dict[str, Any]) -> dict[str, Any]:
    """Die fahrplanrelevanten Einstellungen, ohne Zugangs- und Netzdaten."""
    gefiltert: dict[str, Any] = {}
    for key, wert in config.items():
        if any(sperre in key.lower() for sperre in EINSTELLUNGEN_SPERRE):
            continue
        if key in EINSTELLUNGEN_KEYS or key.startswith(EINSTELLUNGEN_PRAEFIXE):
            gefiltert[key] = wert
    return gefiltert


def _batterie_verlauf(plan: dict[str, Any]) -> list[float]:
    """Die ersten Slots der geplanten Batterieleistung — Vergleichsgröße."""
    werte: list[float] = []
    for slot in (plan.get("slots") or [])[:ABWEICHUNG_SLOTS]:
        wert = slot.get("battery_p")
        werte.append(0.0 if wert is None else float(wert))
    return werte


def _weicht_ab(neu: list[float], alt: list[float]) -> bool:
    if not alt or not neu:
        return False
    return any(
        abs(a - b) > ABWEICHUNG_KW for a, b in zip(neu, alt)
    ) or len(neu) != len(alt)


class ScheduleArchive:
    """Legt Fahrpläne als ``.json.gz`` ab und baut daraus ein ZIP."""

    def __init__(self, hass: Any, entry_id: str, version: str = "") -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._version = version
        self._wurzel = Path(hass.config.path(ARCHIV_ORDNER))
        self._letzter_stempel: datetime | None = None
        self._letzte_batterie: list[float] = []
        self._letzter_fehler: str | None = None
        self._purge_tag: str | None = None

    # -- Schreiben --------------------------------------------------------

    async def async_maybe_store(
        self, payload: dict[str, Any], config: dict[str, Any], jetzt: datetime
    ) -> str | None:
        """Legt den Plan ab, wenn es einen Grund gibt. Gibt den Grund zurück."""
        fehler = payload.get("error")
        grund: str | None = None
        if self._letzter_stempel is None:
            grund = "start"
        elif fehler != self._letzter_fehler:
            # Der Übergang ist die Information — sowohl das Kippen in den
            # Fehler als auch die Erholung danach.
            grund = "fehler"
        elif jetzt - self._letzter_stempel >= timedelta(minutes=TAKT_MINUTEN):
            grund = "takt"
        elif (
            jetzt - self._letzter_stempel
            >= timedelta(minutes=ABWEICHUNG_MINDESTABSTAND_MIN)
            and _weicht_ab(_batterie_verlauf(payload), self._letzte_batterie)
        ):
            grund = "abweichung"

        if grund is None:
            return None

        eintrag = {
            "gespeichert": jetzt.isoformat(),
            "grund": grund,
            "version": self._version,
            "plan": payload,
            "einstellungen": einstellungen_filtern(config),
        }
        purge = self._purge_tag != jetzt.strftime("%Y-%m-%d")
        try:
            await self._hass.async_add_executor_job(
                self._schreiben, eintrag, jetzt, purge
            )
        except Exception:  # noqa: BLE001 - ein volles Dateisystem darf den Zyklus nicht kippen
            _LOGGER.warning("Fahrplan-Archiv: Schreiben fehlgeschlagen", exc_info=True)
            return None

        if purge:
            self._purge_tag = jetzt.strftime("%Y-%m-%d")
        self._letzter_stempel = jetzt
        self._letzte_batterie = _batterie_verlauf(payload)
        self._letzter_fehler = fehler
        return grund

    def _schreiben(self, eintrag: dict[str, Any], jetzt: datetime, purge: bool) -> None:
        tag = self._wurzel / jetzt.strftime("%Y-%m-%d")
        tag.mkdir(parents=True, exist_ok=True)
        ziel = tag / f"{jetzt.strftime('%H%M%S')}.json.gz"
        roh = json.dumps(eintrag, ensure_ascii=False, default=str).encode("utf-8")
        with gzip.open(ziel, "wb") as f:
            f.write(roh)
        if purge:
            self._aufraeumen(jetzt)

    def _aufraeumen(self, jetzt: datetime) -> None:
        """Tagesordner löschen, die älter sind als die Aufbewahrungsfrist."""
        grenze = (jetzt - timedelta(days=AUFBEWAHRUNG_TAGE)).strftime("%Y-%m-%d")
        if not self._wurzel.is_dir():
            return
        for ordner in self._wurzel.iterdir():
            if not ordner.is_dir() or len(ordner.name) != 10:
                continue
            if ordner.name < grenze:
                shutil.rmtree(ordner, ignore_errors=True)
                _LOGGER.debug("Fahrplan-Archiv: %s gelöscht (älter als %d Tage)",
                              ordner.name, AUFBEWAHRUNG_TAGE)

    # -- Lesen ------------------------------------------------------------

    # -- Lesen ------------------------------------------------------------

    async def async_lies_vor(
        self, ziel: datetime, hoechstens_h: float = 2.0
    ) -> dict[str, Any] | None:
        """Jüngster Eintrag, der kurz VOR ``ziel`` abgelegt wurde.

        Für die Tagesbilanz: mit ``ziel`` = Tagesbeginn liefert das den Plan,
        den die Anlage am Vorabend gerechnet hat — die Prognose, gegen die der
        Tag gemessen wird.

        ``hoechstens_h`` begrenzt, wie weit zurück gesucht wird. Ohne diese
        Grenze käme nach einem Ausfall von Home Assistant ein Tage alter Plan
        zurück und würde als "Vorabend-Prognose" ausgewertet. Lieber kein Wert
        als ein falsch beschrifteter.
        """
        return await self._hass.async_add_executor_job(
            self._lies_vor, ziel, hoechstens_h
        )

    def _lies_vor(
        self, ziel: datetime, hoechstens_h: float
    ) -> dict[str, Any] | None:
        frueh = ziel - timedelta(hours=hoechstens_h)
        treffer: tuple[datetime, Path] | None = None
        # Nur die Tagesordner, die das Fenster berühren kann.
        for tag in {frueh.strftime("%Y-%m-%d"), ziel.strftime("%Y-%m-%d")}:
            ordner = self._wurzel / tag
            if not ordner.is_dir():
                continue
            for datei in ordner.glob("*.json.gz"):
                stempel = self._stempel(datei, ziel.tzinfo)
                if stempel is None or not (frueh <= stempel < ziel):
                    continue
                if treffer is None or stempel > treffer[0]:
                    treffer = (stempel, datei)
        if treffer is None:
            return None
        try:
            with gzip.open(treffer[1], "rb") as f:
                return json.loads(f.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 - eine kaputte Datei darf nichts kippen
            _LOGGER.warning(
                "Fahrplan-Archiv: %s nicht lesbar", treffer[1].name, exc_info=True
            )
            return None

    @staticmethod
    def _stempel(datei: Path, tzinfo: Any) -> datetime | None:
        """``2026-08-25/134500.json.gz`` → datetime in derselben Zeitzone.

        Die Ordner- und Dateinamen tragen lokale Zeit (geschrieben aus
        ``_now_local``), deshalb wird die Zeitzone des Vergleichswerts
        übernommen statt geraten.
        """
        uhr = datei.stem.replace(".json", "")
        if len(uhr) != 6 or not uhr.isdigit():
            return None
        try:
            roh = datetime.strptime(f"{datei.parent.name} {uhr}", "%Y-%m-%d %H%M%S")
        except ValueError:
            return None
        return roh.replace(tzinfo=tzinfo)

    async def async_status(self) -> dict[str, Any]:
        return await self._hass.async_add_executor_job(self._status)

    def _status(self) -> dict[str, Any]:
        dateien = self._dateien()
        bytes_gesamt = sum(d.stat().st_size for d in dateien)
        return {
            "eintraege": len(dateien),
            "bytes": bytes_gesamt,
            "von": self._zeitpunkt(dateien[0]) if dateien else None,
            "bis": self._zeitpunkt(dateien[-1]) if dateien else None,
            "takt_minuten": TAKT_MINUTEN,
            "aufbewahrung_tage": AUFBEWAHRUNG_TAGE,
        }

    def _dateien(self) -> list[Path]:
        if not self._wurzel.is_dir():
            return []
        return sorted(self._wurzel.glob("*/*.json.gz"))

    @staticmethod
    def _zeitpunkt(datei: Path) -> str:
        """Aus ``2026-08-25/134500.json.gz`` wird ``2026-08-25 13:45:00``."""
        uhr = datei.stem.replace(".json", "")
        if len(uhr) == 6:
            uhr = f"{uhr[:2]}:{uhr[2:4]}:{uhr[4:]}"
        return f"{datei.parent.name} {uhr}"

    # -- Ausgabe ----------------------------------------------------------

    async def async_build_zip(self, ist_verlauf: dict[str, Any] | None = None) -> bytes:
        """Alle Einträge samt Ist-Verlauf und Lesehilfe als ZIP im Speicher."""
        return await self._hass.async_add_executor_job(self._zip_bauen, ist_verlauf)

    def _zip_bauen(self, ist_verlauf: dict[str, Any] | None) -> bytes:
        puffer = io.BytesIO()
        dateien = self._dateien()
        with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as z:
            for datei in dateien:
                # Die Einträge sind schon gzip — noch einmal komprimieren
                # bringt nichts, also unverändert übernehmen.
                z.write(datei, f"plaene/{datei.parent.name}/{datei.name}",
                        compress_type=zipfile.ZIP_STORED)
            if ist_verlauf is not None:
                z.writestr(
                    "ist_verlauf.json",
                    json.dumps(ist_verlauf, ensure_ascii=False, indent=1, default=str),
                )
            letzte = dateien[-1] if dateien else None
            if letzte is not None:
                with gzip.open(letzte, "rb") as f:
                    eintrag = json.loads(f.read().decode("utf-8"))
                z.writestr(
                    "einstellungen.json",
                    json.dumps(eintrag.get("einstellungen", {}),
                               ensure_ascii=False, indent=1, default=str),
                )
            z.writestr("LIESMICH.md", self._liesmich(dateien))
        return puffer.getvalue()

    def _liesmich(self, dateien: list[Path]) -> str:
        von = self._zeitpunkt(dateien[0]) if dateien else "—"
        bis = self._zeitpunkt(dateien[-1]) if dateien else "—"
        return f"""# Fahrplan-Archiv EEG Energy Optimizer

Version: {self._version or "unbekannt"}
Zeitraum: {von} bis {bis} ({len(dateien)} Einträge)

## Inhalt

* `plaene/<Tag>/<HHMMSS>.json.gz` — je ein gerechneter Fahrplan, gzip-JSON.
  Gespeichert wird alle {TAKT_MINUTEN} Minuten, zusätzlich bei deutlicher
  Planänderung und bei jedem Wechsel des Fehlerzustands; das Feld `grund`
  sagt, welcher Fall zutraf (`takt`, `abweichung`, `fehler`, `start`).
* `ist_verlauf.json` — gemessene Leistungen aus dem Recorder im
  5-Minuten-Raster, je Sensor eine Reihe aus Zeitstempel und Mittelwert.
* `einstellungen.json` — die Einstellungen des jüngsten Eintrags, ohne
  Netzwerk- und Zugangsdaten.

## Ein Eintrag

    gespeichert   Zeitpunkt der Ablage (lokale Zeit)
    grund         warum dieser Lauf archiviert wurde
    version       Version der Integration
    einstellungen fahrplanrelevante Konfiguration
    plan          Ergebnis von ScheduleRunner.to_dict():
      start                 Beginn des Planungszeitraums
      time_res_min          Slotlänge in Minuten (15)
      soc_start_pct         Ladestand beim Start des Laufs
      battery_capacity_kwh  Kapazität, mit der gerechnet wurde
      min_soc_pct           harte Untergrenze des Ladestands
      forecast_source       Herkunft der PV-Prognose
      duration_ms           Rechenzeit des Solvers
      slots[]               je Slot:
        t             Zeitstempel
        PV            erwartete Erzeugung (kW)
        consumption   erwarteter Eigenverbrauch (kW)
        battery_p     geplante Batterieleistung (kW, + = entladen)
        battery       freie Kapazität (kWh)
        battery_ub    Obergrenze der freien Kapazität (kWh)
        grid_p        geplanter Netzaustausch (kW, + = Einspeisung)
        discard       abgeregelte Erzeugung (kW)
        bat_price     Grenzpreis der Batterie (€/kWh)
        ac_price      Grenzpreis am Wechselrichter (€/kWh)
        feedin_price  Einspeisepreis inkl. EEG-Aufschlag (€/kWh)
        soc           geplanter Ladestand (%)

Die Slots enthalten damit sämtliche Eingangsgrößen des LP-Modells — ein
Lauf lässt sich allein aus dieser Datei nachrechnen.
"""
