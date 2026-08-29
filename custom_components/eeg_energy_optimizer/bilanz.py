"""Energiebilanz in Geld — was die PV bringt, und was davon die Optimierung ist.

Zwei Fragen, die sich grundlegend unterscheiden:

* **Ersparnis durch die PV** ist eine MESSUNG. Jede selbst verbrauchte
  Kilowattstunde hätte sonst gekauft werden müssen, jede eingespeiste bringt
  Geld. Beides ist gemessen, nichts daran ist unterstellt.
* **Ersparnis durch die Optimierung** ist eine MODELLRECHNUNG. Sie ist die
  Differenz zu einem Betrieb, den es nicht gegeben hat — den muss man
  simulieren, messen kann man ihn nicht.

Beides beruht auf derselben aufgezeichneten Reihe: 96 Viertelstunden je Tag
mit Energie, Ladestand und den Preisen, die zu dieser Viertelstunde galten.

**Warum die Preise EINGEFROREN werden statt später rekonstruiert.** Zwei
harte Gründe, beide beim Bauen geprüft:

1. ``_basistarif_je_slot`` in ``schedule.py`` sucht den letzten Stützpunkt,
   der nicht in der Zukunft liegt. Die Preisreihe des Fahrplans beginnt aber
   bei JETZT — für einen Slot von heute Vormittag fiele sie auf den ersten
   Stützpunkt zurück, also auf den aktuellen Preis. Bei Quelle Spotpreis
   wäre die Rückschau damit schlicht falsch.
2. Die Bedarfssalden der Gemeinschaften stehen in einem wandernden
   48-Stunden-Fenster. Die Viertelstunden von heute früh sind heute Abend
   nicht mehr abrufbar.

Nebeneffekt, der erwünscht ist: Wer morgen seinen Bezugspreis korrigiert,
schreibt damit nicht die Geldwerte der vergangenen Tage um.

**Bewertet wird mit ``bewerte_geldfluesse``** — derselben Funktion, die auch
Plan und Referenz der Gewinnkarte bewertet. Sie fragt nicht, woher eine
Slot-Liste kommt; die gemessene Reihe geht genauso hinein wie ein Plan. Das
ist Absicht: Eine zweite Geldlogik daneben würde bei der nächsten
Tarifänderung auseinanderlaufen.

**Zur Zeitumstellung.** Der Slot-Index kommt aus der Wanduhrzeit. Im Herbst
wird die doppelte Stunde deshalb in dieselben vier Slots addiert, im Frühjahr
bleiben vier Slots leer. Für die Geldsummen ist das folgenlos — sie sind
Summen, und es geht keine Kilowattstunde verloren oder kommt hinzu. Nur die
Zuordnung einzelner Viertelstunden ist an diesen zwei Tagen im Jahr
ungenau. Der Fahrplan darf das NICHT so machen (dort brach ``resample()`` an
doppelten Zeitstempeln), hier ist es unkritisch.

**Die Messwerte kommen aus den Sensoren der Integration**, nicht aus den
Rohsensoren des Wechselrichters. Der Hausverbrauch-Sensor trägt bereits die
Vorzeichen-Normalisierung, die SolarEdge-Korrektur (ac_power enthält die
Batterieentladung) und die Summe über mehrere Batterien. Ein zweiter Nachbau
hier wäre die klassische zweite Wahrheit.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .const import DOMAIN, MODE_EIN

try:  # pragma: no cover - im Test nicht vorhanden
    from homeassistant.helpers.storage import Store
except ImportError:  # pragma: no cover
    Store = None  # type: ignore[assignment]

try:  # pragma: no cover - im Test nicht vorhanden
    from homeassistant.util import dt as dt_util
except ImportError:  # pragma: no cover
    dt_util = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

# Ein Slot ist eine Viertelstunde — dasselbe Raster wie der Fahrplan.
SLOT_SEKUNDEN = 900
SLOTS_PRO_TAG = 96

# Tage mit voller Aufschlüsselung. Darüber hinaus bleiben die Monatssummen,
# die für „dieses Jahr" genügen — 12 Einträge je Jahr statt 365.
TAGE_ROH = 400

# Größer Sprung zwischen zwei Takten (Neustart, Schlaf) — dann wird nicht
# hochgerechnet. Lieber eine Lücke als eine erfundene Kilowattstunde.
MAX_TAKT_SEKUNDEN = 300

# Felder, die sich NICHT ueber Tage aufsummieren lassen: ein_anteil ist ein
# Anteil zwischen 0 und 1, seine Monatssumme waere 30 statt eines Anteils.
NICHT_SUMMIERBAR = {"ein_anteil"}

# Unique-ID-Endungen der Sensoren, aus denen die Bilanz liest. Sie sind
# bereits normalisiert (Vorzeichen, Multi-Batterie, SolarEdge).
_QUELLEN = {
    "pv": "pv_leistung",
    "netz": "netzleistung",
    "batterie": "batterieleistung",
    "haus": "hausverbrauch",
}


def _jetzt_lokal(now_utc: datetime) -> datetime:
    if dt_util is not None:
        return dt_util.as_local(now_utc)
    return now_utc.astimezone()


def _leerer_slot() -> dict[str, Any]:
    return {
        "pv": 0.0,          # kWh erzeugt
        "haus": 0.0,        # kWh verbraucht (Hauslast)
        "bezug": 0.0,       # kWh aus dem Netz
        "export": 0.0,      # kWh ins Netz
        "laden": 0.0,       # kWh in die Batterie
        "entladen": 0.0,    # kWh aus der Batterie
        "soc_a": None,      # Ladestand am Slot-Anfang (%)
        "soc_e": None,      # Ladestand am Slot-Ende (%)
        "basis": None,      # eingefrorener Basistarif (EUR/kWh)
        "kwp": None,        # eingefrorener Bezugspreis (EUR/kWh)
        "eeg": {},          # eingefrorene Salden je Gemeinschaft (kWh)
        "ein_s": 0.0,       # Sekunden im Modus Ein
        "s": 0.0,           # gezählte Sekunden gesamt
    }


def _leerer_tag(datum: str) -> dict[str, Any]:
    return {"datum": datum, "slots": {}}


class EnergieBilanz:
    """Zeichnet die Energiereihe auf und bewertet sie in Geld."""

    def __init__(self, hass: Any, entry_id: str, config: dict) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._config = config

        store_key = f"{DOMAIN}_{entry_id}_bilanz"
        self._store: Any = Store(hass, 1, store_key) if Store is not None else None

        self._heute: dict[str, Any] = _leerer_tag("")
        # Abgelaufene Tage, die noch auf ihre Bewertung warten (siehe
        # _schliesse_offene). Sie werden mitgespeichert — sonst ginge ein
        # wartender Tag beim naechsten Neustart verloren, und genau der
        # Neustart ist der Grund, warum er wartet.
        self._offen: list[dict[str, Any]] = []
        self._tage: dict[str, dict] = {}
        self._monate: dict[str, dict] = {}
        self._letzter_takt_utc: datetime | None = None
        self._dirty = False
        self._erster_takt = True
        # Aufgelöste Entity-IDs der Quellsensoren, einmal ermittelt.
        self._quellen: dict[str, str] | None = None

    # ------------------------------------------------------------------
    # Speicher
    # ------------------------------------------------------------------
    async def async_load(self) -> None:
        """Gespeicherte Bilanz laden."""
        if self._store is None:
            return
        try:
            stored = await self._store.async_load()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Keine gespeicherte Bilanz gefunden")
            return
        if not stored or not isinstance(stored, dict):
            return

        self._heute = stored.get("heute") or _leerer_tag("")
        self._offen = stored.get("offen") or []
        self._tage = stored.get("tage") or {}
        self._monate = stored.get("monate") or {}
        self._verdichte_alte_tage()

    def _verdichte_alte_tage(self) -> None:
        """Tage über der Aufbewahrungsfrist verwerfen — Monate bleiben."""
        grenze = (date.today() - timedelta(days=TAGE_ROH)).isoformat()
        alt = [tag for tag in self._tage if tag < grenze]
        for tag in alt:
            del self._tage[tag]
            self._dirty = True

    async def async_flush(self) -> None:
        """Schreiben, wenn sich etwas geändert hat."""
        if not self._dirty or self._store is None:
            return
        self._dirty = False
        try:
            await self._store.async_save({
                "version": 1,
                "heute": self._heute,
                "offen": self._offen,
                "tage": self._tage,
                "monate": self._monate,
            })
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Bilanz nicht speicherbar: %s", err)

    # ------------------------------------------------------------------
    # Takt
    # ------------------------------------------------------------------
    async def async_update(
        self, mode: str, now_utc: datetime, inputs: Any = None
    ) -> None:
        """Im Guard-Takt aufgerufen: Energie aufsummieren, Preise einfrieren.

        Gezählt wird IMMER, auch im Modus Aus — anders als die
        Einspeise-Statistik, die nur zählt, was die Steuerung selbst tut. Der
        Ertrag der PV entsteht schließlich unabhängig davon, ob der Fahrplan
        gerade eingreift. Der Modus wird je Slot als Sekundenanteil
        mitgeschrieben; er ist die Grundlage für die Selbstprüfung: Im Modus
        Aus fährt die Anlage Standardbetrieb, dort MUSS der ausgewiesene
        Optimierungs-Vorteil gegen null gehen.
        """
        now_local = _jetzt_lokal(now_utc)
        heute = now_local.strftime("%Y-%m-%d")

        # Tageswechsel (auch nach einem Neustart über Mitternacht hinweg).
        # Der alte Tag wird NICHT sofort bewertet: Ohne Fahrplan-Inputs
        # fehlen Tarife und Kapazität, und er läge dauerhaft ohne
        # Einspeiseerlös im Archiv. Genau das passiert nach einem Neustart
        # über Mitternacht — der erste Bilanz-Takt kommt dann vor dem ersten
        # Fahrplan-Lauf. Er wandert deshalb in die Warteschlange und wird
        # bewertet, sobald die Inputs da sind (in aller Regel eine Minute
        # später).
        if self._heute.get("datum") and self._heute["datum"] != heute:
            self._offen.append(self._heute)
            self._heute = _leerer_tag(heute)
            self._dirty = True
        if self._heute.get("datum") != heute:
            self._heute = _leerer_tag(heute)
            self._dirty = True
        self._schliesse_offene(inputs, heute)

        # Erster Takt nach dem Start: nur die Uhr stellen.
        if self._erster_takt:
            self._erster_takt = False
            self._letzter_takt_utc = now_utc
            return

        sekunden = 0.0
        if self._letzter_takt_utc is not None:
            sekunden = (now_utc - self._letzter_takt_utc).total_seconds()
        self._letzter_takt_utc = now_utc
        if sekunden <= 0 or sekunden > MAX_TAKT_SEKUNDEN:
            return

        messwerte = self._lies_messwerte()
        if messwerte is None:
            return

        index = (now_local.hour * 3600 + now_local.minute * 60) // SLOT_SEKUNDEN
        self._summiere(str(index), messwerte, sekunden, mode, inputs, now_local)
        self._dirty = True

    def _lies_messwerte(self) -> dict[str, float] | None:
        """PV, Netz, Batterie, Hauslast in kW und der Ladestand in Prozent.

        Aus den Sensoren der Integration, siehe Modul-Docstring. Fehlt einer,
        wird dieser Takt übersprungen: eine Lücke ist ehrlicher als eine mit
        Nullen aufgefüllte Reihe.
        """
        from .power_readings import read_power_kw

        quellen = self._entity_ids()
        if quellen is None:
            return None

        werte: dict[str, float] = {}
        for name, entity_id in quellen.items():
            roh = read_power_kw(self._hass, entity_id)
            if roh is None:
                # Die PV meldet sich nachts ab — das ist kein Fehler,
                # sondern 0 kW. Alle anderen Sensoren müssen liefern.
                if name == "pv":
                    roh = 0.0
                else:
                    return None
            werte[name] = roh

        soc = self._lies_soc()
        if soc is not None:
            werte["soc"] = soc
        return werte

    def _entity_ids(self) -> dict[str, str] | None:
        """Entity-IDs der Quellsensoren über die Registry auflösen (einmalig).

        Über die unique_id statt über einen geratenen Namen: Die Entitäten
        können vom Benutzer umbenannt worden sein, die unique_id nicht.
        """
        if self._quellen is not None:
            return self._quellen or None

        try:
            from homeassistant.helpers import entity_registry as er

            registry = er.async_get(self._hass)
        except Exception:  # noqa: BLE001 - ohne Registry keine Bilanz
            return None

        gefunden: dict[str, str] = {}
        for name, endung in _QUELLEN.items():
            unique_id = f"{DOMAIN}_{self._entry_id}_{endung}"
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
            if entity_id:
                gefunden[name] = entity_id

        if len(gefunden) < len(_QUELLEN):
            fehlend = sorted(set(_QUELLEN) - set(gefunden))
            _LOGGER.debug("Bilanz: Sensoren fehlen noch: %s", ", ".join(fehlend))
            # Nicht merken — die Plattformen sind beim ersten Takt
            # moeglicherweise noch nicht fertig.
            return None

        self._quellen = gefunden
        return gefunden

    def _lies_soc(self) -> float | None:
        """Ladestand in Prozent. Bei mehreren Batterien zeigt der konfigurierte
        Sensor auf den kombinierten Wert — deshalb genügt dieser eine."""
        entity_id = self._config.get("battery_soc_sensor") or ""
        if not entity_id:
            return None
        zustand = self._hass.states.get(entity_id)
        if zustand is None:
            return None
        try:
            return float(zustand.state)
        except (TypeError, ValueError):
            return None

    def _summiere(
        self,
        index: str,
        werte: dict[str, float],
        sekunden: float,
        mode: str,
        inputs: Any,
        now_local: datetime,
    ) -> None:
        """Energie dieses Takts in den Slot der laufenden Viertelstunde buchen."""
        slots = self._heute.setdefault("slots", {})
        slot = slots.get(index)
        if slot is None:
            slot = _leerer_slot()
            slots[index] = slot
            # Preise EINMAL je Slot einfrieren, beim ersten Takt darin.
            self._friere_preise_ein(slot, inputs, now_local)

        stunden = sekunden / 3600.0
        netz = werte.get("netz", 0.0)
        batterie = werte.get("batterie", 0.0)

        slot["pv"] += max(werte.get("pv", 0.0), 0.0) * stunden
        slot["haus"] += max(werte.get("haus", 0.0), 0.0) * stunden
        # Netz: positiv = Einspeisung, negativ = Bezug.
        slot["export"] += max(netz, 0.0) * stunden
        slot["bezug"] += max(-netz, 0.0) * stunden
        # Batterie: positiv = Laden, negativ = Entladen.
        slot["laden"] += max(batterie, 0.0) * stunden
        slot["entladen"] += max(-batterie, 0.0) * stunden
        slot["s"] += sekunden
        if mode == MODE_EIN:
            slot["ein_s"] += sekunden

        soc = werte.get("soc")
        if soc is not None:
            if slot["soc_a"] is None:
                slot["soc_a"] = round(soc, 1)
            slot["soc_e"] = round(soc, 1)

    def _friere_preise_ein(
        self, slot: dict[str, Any], inputs: Any, now_local: datetime
    ) -> None:
        """Basistarif, Bezugspreis und EEG-Salden dieser Viertelstunde sichern.

        Der Basistarif kommt über ``_basistarif_je_slot`` — dieselbe Funktion,
        die der Fahrplan benutzt, aufgerufen für genau einen Slot: jetzt. Weil
        die Preisreihe des Fahrplans bei jetzt beginnt, trifft sie hier exakt.
        Später ginge das nicht mehr (siehe Modul-Docstring).
        """
        if inputs is None:
            return
        try:
            from .schedule import _basistarif_je_slot

            marke = {"t": now_local.isoformat()}
            slot["basis"] = _basistarif_je_slot([marke], inputs)[0]
            slot["kwp"] = float(inputs.consumption_price)
        except Exception:  # noqa: BLE001 - ohne Preise bleibt der Slot roh
            _LOGGER.debug("Bilanz: Preise nicht einfrierbar", exc_info=True)
            return

        # Saldo je Gemeinschaft fuer die laufende Viertelstunde.
        bedarf = getattr(inputs, "eeg_bedarf", None) or {}
        viertel = int(now_local.timestamp() // SLOT_SEKUNDEN)
        salden: dict[str, float] = {}
        for name, reihe in bedarf.items():
            wert = (reihe or {}).get(viertel)
            if wert is not None:
                salden[name] = round(float(wert), 3)
        slot["eeg"] = salden

    # ------------------------------------------------------------------
    # Tagesabschluss
    # ------------------------------------------------------------------
    def _schliesse_offene(self, inputs: Any, heute: str) -> None:
        """Wartende Tage bewerten, sobald Fahrplan-Inputs vorliegen.

        Nicht ewig warten: Kommt zwei Tage lang kein Fahrplan zustande, wird
        mit dem bewertet, was da ist. Ein unbewerteter Tag im Archiv ist
        ärgerlich, eine unbegrenzt wachsende Warteschlange wäre schlimmer.
        """
        if not self._offen:
            return
        try:
            grenze = (date.fromisoformat(heute) - timedelta(days=1)).isoformat()
        except (ValueError, TypeError):
            grenze = ""

        rest: list[dict[str, Any]] = []
        for tag in self._offen:
            datum = tag.get("datum") or ""
            if inputs is None and datum >= grenze:
                rest.append(tag)
                continue
            if inputs is None:
                _LOGGER.warning(
                    "Bilanz: %s wird ohne Fahrplan-Daten abgeschlossen — "
                    "ohne Tarife bleibt der Einspeiseerlös aus",
                    datum,
                )
            self._archiviere(tag, inputs)
        self._offen = rest
        self._dirty = True

    def _archiviere(self, tag: dict[str, Any], inputs: Any) -> None:
        """Einen abgelaufenen Tag bewerten und ins Archiv legen."""
        datum = tag.get("datum")
        if not datum:
            return
        ergebnis = self.bewerte_tag(tag, inputs)
        self._tage[datum] = ergebnis
        monat = datum[:7]
        summe = self._monate.setdefault(monat, {})
        for feld, wert in ergebnis.items():
            # Anteile sind keine Betraege: ein_anteil ueber 30 Tage
            # aufzusummieren ergaebe 30 statt eines Anteils.
            if feld in NICHT_SUMMIERBAR:
                continue
            if isinstance(wert, (int, float)):
                summe[feld] = round(float(summe.get(feld, 0.0)) + float(wert), 4)
        self._dirty = True
        self._verdichte_alte_tage()

    # ------------------------------------------------------------------
    # Bewertung
    # ------------------------------------------------------------------
    def bewerte_tag(
        self, tag: dict[str, Any], inputs: Any, abgeschlossen: bool = True
    ) -> dict[str, Any]:
        """Geldwerte eines Tages aus seiner aufgezeichneten Reihe.

        ``abgeschlossen`` False = der laufende Tag: die Begründung eines
        negativen Vorteils nennt dann zuerst, dass es ein Zwischenstand ist.

        Liefert beide Größen samt Zwischenwerten. ``pv_ersparnis`` enthält den
        Optimierungs-Vorteil bereits — er ist kein zusätzlicher Betrag,
        sondern der Anteil daran, der auf die Steuerung zurückgeht. Wer beides
        addiert, zählt doppelt.
        """
        slots = self._sortierte_slots(tag)
        leer = {
            "pv_ersparnis": 0.0,
            "opt_vorteil": None,
            "vermieden": 0.0,
            "erloes": 0.0,
            "eigen_kwh": 0.0,
            "export_kwh": 0.0,
            "bezug_kwh": 0.0,
            "pv_kwh": 0.0,
            "eeg_kwh": 0.0,
            "ein_anteil": 0.0,
            "ist_summe": None,
            "ref_summe": None,
            "vorteil_begruendung": None,
            "vorteil_details": None,
        }
        if not slots:
            return leer

        eigen_kwh = 0.0
        vermieden = 0.0
        pv_kwh = export_kwh = bezug_kwh = 0.0
        netzgeladen_kwh = 0.0
        ein_s = gesamt_s = 0.0
        for slot in slots:
            pv_kwh += slot.get("pv", 0.0)
            export_kwh += slot.get("export", 0.0)
            bezug_kwh += slot.get("bezug", 0.0)
            ein_s += slot.get("ein_s", 0.0)
            gesamt_s += slot.get("s", 0.0)
            # Eigenverbrauch: was das Haus verbraucht hat, ohne den Anteil aus
            # dem Netz. Was aus der Batterie kam, zaehlt mit — es war PV.
            eigen = max(slot.get("haus", 0.0) - slot.get("bezug", 0.0), 0.0)
            eigen_kwh += eigen
            preis = slot.get("kwp")
            if preis is not None:
                vermieden += eigen * float(preis)
            # Was im selben Slot aus dem Netz kam UND in die Batterie ging,
            # war nie PV-Strom. Es spaeter beim Entladen als Eigenverbrauch
            # zu zaehlen waere doppelt gerechnet: einmal als Netzbezug
            # bezahlt, einmal als Ersparnis gutgeschrieben. Der kleinere der
            # beiden Werte ist die obere Schranke fuer den netzgeladenen
            # Anteil. In der Praxis ist das null — bei 26 ct Bezug gegen
            # gut 10 ct Verguetung laedt kein Fahrplan aus dem Netz.
            netzgeladen_kwh += min(
                slot.get("laden", 0.0), slot.get("bezug", 0.0)
            )

        # Netzgeladene Energie herausrechnen. Bewusst ohne Wirkungsgrad, also
        # etwas zu viel abgezogen: Die ausgewiesene Ersparnis soll im Zweifel
        # zu klein sein, nicht zu gross.
        if netzgeladen_kwh > 0 and eigen_kwh > 0:
            anteil = min(netzgeladen_kwh, eigen_kwh)
            if eigen_kwh > 0:
                vermieden *= max(0.0, (eigen_kwh - anteil)) / eigen_kwh
            eigen_kwh -= anteil

        ergebnis = dict(leer)
        ergebnis.update({
            "eigen_kwh": round(eigen_kwh, 3),
            "pv_kwh": round(pv_kwh, 3),
            "export_kwh": round(export_kwh, 3),
            "bezug_kwh": round(bezug_kwh, 3),
            "vermieden": round(vermieden, 4),
            "ein_anteil": round(ein_s / gesamt_s, 3) if gesamt_s > 0 else 0.0,
        })

        if inputs is None:
            # Ohne Fahrplan-Inputs fehlen Tarife und Kapazitaet — der
            # gemessene Teil steht trotzdem.
            ergebnis["pv_ersparnis"] = ergebnis["vermieden"]
            return ergebnis

        datum = tag.get("datum") or date.today().isoformat()
        ist_slots = self._als_slots(slots, datum)
        bewertung = self._bewerte(ist_slots, slots, inputs)
        if bewertung is None:
            ergebnis["pv_ersparnis"] = ergebnis["vermieden"]
            return ergebnis

        ergebnis["erloes"] = bewertung.get("erloes", 0.0)
        ergebnis["eeg_kwh"] = bewertung.get("eeg_kwh", 0.0)
        ergebnis["pv_ersparnis"] = round(
            ergebnis["vermieden"] + ergebnis["erloes"], 4
        )

        vorteil, referenz = self._optimierungs_vorteil(
            ist_slots, slots, inputs, bewertung
        )
        ergebnis["opt_vorteil"] = vorteil
        # Beide Seiten der Differenz mit ausweisen — sonst steht im Panel eine
        # Zahl, die niemand nachrechnen kann.
        ergebnis["ist_summe"] = round(float(bewertung.get("summe", 0.0)), 4)
        ergebnis["ref_summe"] = (
            None if referenz is None else round(float(referenz.get("summe", 0.0)), 4)
        )
        if vorteil is not None and referenz is not None:
            ergebnis["vorteil_details"] = vorteil_details(bewertung, referenz)
            if vorteil < 0:
                ergebnis["vorteil_begruendung"] = begruende_vorteil(
                    ergebnis["vorteil_details"],
                    ein_anteil=float(ergebnis.get("ein_anteil") or 0.0),
                    abgeschlossen=abgeschlossen,
                )
        return ergebnis

    def _sortierte_slots(self, tag: dict[str, Any]) -> list[dict[str, Any]]:
        roh = tag.get("slots") or {}
        try:
            schluessel = sorted(roh, key=int)
        except (TypeError, ValueError):
            schluessel = sorted(roh)
        return [roh[k] for k in schluessel]

    def _als_slots(
        self, slots: list[dict[str, Any]], datum: str
    ) -> list[dict[str, Any]]:
        """Die gemessene Reihe in der Form, die ``bewerte_geldfluesse`` erwartet.

        Vorzeichen wie im Fahrplan: ``grid_p`` positiv = Einspeisung,
        ``battery_p`` positiv = Entladen. Leistung ist Energie je Slot geteilt
        durch die Slotdauer — die Bewertung multipliziert sie wieder mit
        derselben Dauer, der Umweg hebt sich exakt auf und hält die Funktion
        unveraendert nutzbar.
        """
        stunden = SLOT_SEKUNDEN / 3600.0
        gebaut: list[dict[str, Any]] = []
        for i, slot in enumerate(slots):
            minute = (i * SLOT_SEKUNDEN) // 60
            stempel = f"{datum}T{minute // 60:02d}:{minute % 60:02d}:00"
            netz = (slot.get("export", 0.0) - slot.get("bezug", 0.0)) / stunden
            batterie = (slot.get("entladen", 0.0) - slot.get("laden", 0.0)) / stunden
            gebaut.append({
                "t": stempel,
                "grid_p": round(netz, 4),
                "battery_p": round(batterie, 4),
                "PV": round(slot.get("pv", 0.0) / stunden, 4),
                "consumption": round(slot.get("haus", 0.0) / stunden, 4),
                "soc": slot.get("soc_e"),
            })
        return gebaut

    def _inputs_fuer(
        self, ist_slots: list[dict[str, Any]], slots: list[dict[str, Any]], inputs: Any
    ) -> Any:
        """Fahrplan-Inputs auf die gemessene Reihe umgestellt.

        ``replace`` statt Neubau: Tarife, Kapazitaet, Mindest-Ladestand und
        Alterungskosten bleiben, wie der Fahrplan sie sieht. Ausgetauscht wird
        nur, was zeitgebunden ist — die eingefrorenen Basistarife und Salden.
        """
        zeiten = [datetime.fromisoformat(s["t"]) for s in ist_slots]
        basis = [
            s.get("basis") if s.get("basis") is not None else inputs.feedin_price
            for s in slots
        ]
        bedarf: dict[str, dict[int, float]] = {}
        for slot, stempel in zip(slots, zeiten):
            viertel = int(stempel.timestamp() // SLOT_SEKUNDEN)
            for name, wert in (slot.get("eeg") or {}).items():
                bedarf.setdefault(name, {})[viertel] = wert

        return replace(
            inputs,
            timestamps=zeiten,
            feedin_price_series=basis,
            eeg_bedarf=bedarf or None,
            time_res_s=SLOT_SEKUNDEN,
        )

    def _bewerte(
        self, ist_slots: list[dict[str, Any]], slots: list[dict[str, Any]], inputs: Any
    ) -> dict[str, float] | None:
        try:
            from .schedule import bewerte_geldfluesse

            return bewerte_geldfluesse(
                ist_slots, self._inputs_fuer(ist_slots, slots, inputs)
            )
        except Exception:  # noqa: BLE001 - Geldwerte duerfen nie den Takt kippen
            _LOGGER.debug("Bilanz: Ist-Bewertung fehlgeschlagen", exc_info=True)
            return None

    def _optimierungs_vorteil(
        self,
        ist_slots: list[dict[str, Any]],
        slots: list[dict[str, Any]],
        inputs: Any,
        ist_bewertung: dict[str, float],
    ) -> tuple[float | None, dict[str, float] | None]:
        """Ist gegen simulierten Standardbetrieb — beide am selben Tag gemessen.

        Der Referenzlauf bekommt die GEMESSENEN PV- und Verbrauchsreihen, nicht
        die Prognose. Damit faellt der Wetterfehler heraus, den die Gewinnkarte
        zwangslaeufig mittraegt: simuliert wird nur noch das Verhalten der
        Batterie, und das ist ein simples Greedy-Verfahren ohne Vorausschau.

        Beide starten beim Ladestand des Tagesbeginns; den Endbestand bewertet
        ``bewerte_geldfluesse`` selbst — ohne das verglichen wir ungleiche
        Endzustaende.
        """
        start_soc = None
        for slot in slots:
            if slot.get("soc_a") is not None:
                start_soc = float(slot["soc_a"])
                break
        if start_soc is None:
            return None, None

        try:
            from .schedule import bewerte_geldfluesse, simuliere_standardbetrieb

            angepasst = self._inputs_fuer(ist_slots, slots, inputs)
            referenz_slots = simuliere_standardbetrieb(
                ist_slots, replace(angepasst, soc_pct=start_soc)
            )
            referenz = bewerte_geldfluesse(referenz_slots, angepasst)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Bilanz: Referenzlauf fehlgeschlagen", exc_info=True)
            return None, None

        ref_summe = round(float(referenz.get("summe", 0.0)), 4)
        vorteil = round(float(ist_bewertung.get("summe", 0.0)) - ref_summe, 4)
        return vorteil, dict(referenz)

    # ------------------------------------------------------------------
    # Abfrage (fuer die Sensoren)
    # ------------------------------------------------------------------
    def heute(self, inputs: Any = None) -> dict[str, Any]:
        """Bewertung des laufenden Tages."""
        return self.bewerte_tag(self._heute, inputs, abgeschlossen=False)

    def summe(self, feld: str, monat: str | None = None, jahr: str | None = None) -> float:
        """Summe eines Feldes ueber Monat (``YYYY-MM``) oder Jahr (``YYYY``).

        Der laufende Tag steckt noch nicht im Archiv — die Sensoren addieren
        ihn selbst dazu, damit „diesen Monat" auch heute schon stimmt.
        """
        gesamt = 0.0
        if monat:
            eintrag = self._monate.get(monat) or {}
            gesamt = float(eintrag.get(feld, 0.0) or 0.0)
        elif jahr:
            for schluessel, eintrag in self._monate.items():
                if schluessel.startswith(jahr):
                    gesamt += float(eintrag.get(feld, 0.0) or 0.0)
        return round(gesamt, 4)

    def hat_archiv(self, monat: str | None = None, jahr: str | None = None) -> bool:
        """Gibt es abgeschlossene Tage in diesem Zeitraum?

        Ohne Archiv sind „diesen Monat" und „dieses Jahr" zwangsläufig
        derselbe Wert wie „heute" — die Karte soll das benennen können,
        statt drei gleiche Zahlen zu zeigen, die wie ein Fehler aussehen.
        """
        if monat:
            return monat in self._monate
        if jahr:
            return any(k.startswith(jahr) for k in self._monate)
        return bool(self._monate)

    @property
    def datum_heute(self) -> str:
        return self._heute.get("datum") or ""


# ---------------------------------------------------------------------------
# Begründung eines negativen Optimierungs-Vorteils
# ---------------------------------------------------------------------------
# Eine rote Zahl ohne Erklärung ist schlimmer als keine Zahl: Der Nutzer
# weiß nicht, ob die Optimierung versagt hat oder nur der Tag ungewöhnlich
# war. Die Begründung wird aus den Bestandteilen der Differenz abgeleitet —
# was hat mehr gekostet, was weniger gebracht — und für jeden Bestandteil
# steht, wodurch das typischerweise passiert. Unter 2 Cent zählt ein Posten
# nicht: das ist Rauschen zwischen Messwert und Simulation.

_BEGRUENDUNG_SCHWELLE_EUR = 0.02


def vorteil_details(ist: dict[str, float], ref: dict[str, float]) -> dict[str, float]:
    """Differenz Ist − Referenz je Bestandteil (positiv = Ist besser).

    Die Summe ist erloes − bezug − alterung + endbestand; damit jeder Posten
    dasselbe Vorzeichen hat wie sein Beitrag zum Vorteil, werden Kosten
    negiert: ``bezug`` hier = ref.bezug − ist.bezug.
    """
    def _d(feld: str, kosten: bool = False) -> float:
        a = float(ist.get(feld, 0.0) or 0.0)
        b = float(ref.get(feld, 0.0) or 0.0)
        return round((b - a) if kosten else (a - b), 4)

    return {
        "erloes": _d("erloes"),
        "bezug": _d("bezug", kosten=True),
        "alterung": _d("alterung", kosten=True),
        "endbestand": _d("endbestand"),
    }


def begruende_vorteil(
    details: dict[str, float], ein_anteil: float, abgeschlossen: bool
) -> list[str]:
    """Sätze, die einen negativen Vorteil erklären — der größte Posten zuerst."""
    def eur(v: float) -> str:
        return f"{abs(v):.2f}".replace(".", ",") + " €"

    saetze: list[str] = []

    if not abgeschlossen:
        saetze.append(
            "Zwischenstand — der Tag läuft noch; zurückgehaltene Energie zählt "
            "erst beim Einspeisen."
        )

    posten = sorted(
        ((k, v) for k, v in details.items() if v < -_BEGRUENDUNG_SCHWELLE_EUR),
        key=lambda kv: kv[1],
    )
    texte = {
        "bezug": (
            "Mehr Netzbezug als im Standardbetrieb ({v}) — meist ungeplanter "
            "Verbrauch (z. B. Elektroauto), für den der Fahrplan Energie "
            "zurückhielt."
        ),
        "erloes": (
            "Weniger Einspeiseerlös ({v}) — Energie blieb in der Batterie, "
            "oder die Gemeinschaft brauchte weniger als vorhergesagt."
        ),
        "endbestand": (
            "Am Tagesende weniger Energie in der Batterie ({v}) — der "
            "Standardbetrieb lädt bis 100 %, der Fahrplan nur bis zum Deckel."
        ),
        "alterung": "Mehr Batterienutzung ({v} Alterungskosten).",
    }
    for feld, wert in posten[:2]:
        saetze.append(texte[feld].format(v=eur(wert)))

    if 0.0 < ein_anteil < 0.9:
        saetze.append(
            f"Die Steuerung war nur {round(ein_anteil * 100)} % des Tages aktiv."
        )

    if not posten:
        saetze.append("Die Abweichung liegt im Bereich der Messungenauigkeit.")

    saetze.append(
        "Vergleich mit einem simulierten Betrieb ohne Vorausschau über dieselben "
        "Messwerte — die Referenz kennt den echten Verbrauch, der Fahrplan nur "
        "die Prognose."
    )
    return saetze
