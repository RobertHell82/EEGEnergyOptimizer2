"""Hochrechnung des OeMAG-Einspeisetarifs für den laufenden Monat.

Die OeMAG veröffentlicht den Monatstarif erst am ersten Werktag des
Folgemonats (geprüft an den Zeitstempeln ihrer Excel-Dateien). Wer ihn als
Basistarif nutzt, rechnet den ganzen Monat mit dem Vormonat — im September
2026 mit 8,997 ct, während der September selbst auf 10,515 ct zulief. Die
Differenz landet direkt im Aufschlag der Gemeinschaft (``eeg_price.py``),
weil dort die Differenz zum Basistarif zählt.

Der Tarif ist vorrechenbar, weil die OeMAG ihre Rechnung offenlegt
(``Berechnungsgrundlagen_MP_MM_JJJJ.xlsx`` auf oem-ag.at/marktpreis):

    roh   = Σ Menge_h · Preis_h / Σ Menge_h        über die Stunden des Monats
    Tarif = clamp(roh, 0,6 · Q, Q) − Ausgleichsenergie

* ``Preis_h`` ist der Day-Ahead-Stundenpreis der Gebotszone AT. Die OeMAG
  nimmt die EXAA, die aWATTar-API liefert den EPEX-Preis — es ist seit der
  Marktkopplung dieselbe Zahl (verglichen über 20 Monate: 0,000 ct Abstand).
* ``Menge_h`` ist die Stundenmenge der OeMAG-Marktpreis-Bilanzgruppe — fast
  reine PV-Einspeisung. Sie ist nicht öffentlich, aber die österreichische
  Solarerzeugung (Energy-Charts, Fraunhofer ISE) läuft mit ihr im
  Gleichschritt (Korrelation 0,94–0,997 je Monat). Damit gewichtet trifft die
  Rechnung den veröffentlichten Rohwert über 2025-01 bis 2026-08 mit einem
  mittleren Fehler von 0,21 ct (größter 0,57 ct). Das ungewichtete
  Stundenmittel läge 1,5 bis 5,4 ct daneben — PV speist ein, wenn der Preis am
  tiefsten ist.
* ``Q`` ist der Quartalsmarktpreis der E-Control nach § 41 Abs. 1 ÖSG: das
  Mittel der nächsten vier EEX-Quartalsfutures über die letzten fünf
  Handelstage des Vorquartals. Der Korridor greift oft — April bis Juli 2026
  lagen auf der Untergrenze, Jänner und September auf der Obergrenze — und in
  solchen Monaten ist die Hochrechnung schon am ersten Tag exakt.

Im Monat selbst zählen nur Viertelstunden, für die Preis UND Solarerzeugung
vorliegen (Energy-Charts hängt rund zwei Stunden nach). Die erste Woche
schwankt dadurch um bis zu 1,5 ct (August 2026: Tag 3 → 7,2 ct, Tag 7 →
9,9 ct, Ende 9,0 ct); ab der zweiten Woche liegt der Wert näher am Ergebnis
als der Vormonat. Einen Vormonats-Prior für die restlichen Tage haben wir
gemessen und verworfen — er macht es eher schlechter.

Drei Quellen, drei Rückfallebenen:

* Fehlt der Quartalsanker (E-Control-Seite nicht lesbar), wird er aus der
  OeMAG-Tabelle zurückgerechnet — jeder Monat desselben Quartals, der auf
  Ober- oder Untergrenze lag, verrät ihn (``oemag.anker_aus_tabelle``). Fehlt
  auch das, gilt der Rohwert ohne Korridor, und der Status sagt es.
* Fehlt die Ausgleichsenergie (Text der OeMAG-Seite), gilt der Wert von 2026.
* Fehlen Preise oder Solardaten, bleibt die letzte Hochrechnung stehen; ist
  sie aus einem anderen Monat, gilt sie nicht mehr, und ``schedule.py`` fällt
  auf den veröffentlichten Vormonat zurück — wie ohne Hochrechnung.

Beide APIs drosseln mit HTTP 429 bei schnellen Folgeabfragen; dreistündlich
ist unkritisch. Alles Interne rechnet in €/kWh wie der Rest der Integration.
"""

from __future__ import annotations

import html as html_entities
import logging
import re
from datetime import datetime, timezone
from typing import Any

from .const import DOMAIN
from .oemag import AUSGLEICHSENERGIE_PV_DEFAULT, KORRIDOR_UNTEN
from .spot import parse_marketdata

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from homeassistant.helpers.storage import Store
    from homeassistant.util import dt as dt_util

    _utcnow = dt_util.utcnow
    _now_local = dt_util.now
except ImportError:  # Testumgebung
    _utcnow = lambda: datetime.now(tz=timezone.utc)  # noqa: E731
    _now_local = lambda: datetime.now().astimezone()  # noqa: E731
    async_get_clientsession = None  # type: ignore[assignment]
    Store = None  # type: ignore[assignment,misc]


AWATTAR_URL = "https://api.awattar.at/v1/marketdata"
ENERGY_CHARTS_URL = "https://api.energy-charts.info/public_power"
ECONTROL_URL = "https://www.e-control.at/marktteilnehmer/oeko-energie/marktpreis"
USER_AGENT = "HomeAssistant/EEGEnergyOptimizer"

# Neue Preise kommen einmal täglich (~14:00), Solardaten laufen stündlich
# nach — dreistündlich ist frisch genug und schont beide Dienste.
CACHE_FRESH_SECONDS = 3 * 3600
# Der Quartalsanker ändert sich alle drei Monate; zweimal täglich nachsehen
# reicht, um den Wechsel am Quartalsende mitzubekommen.
ANKER_FRESH_SECONDS = 12 * 3600
# Preise so weit nach vorn holen, dass der Folgetag dabei ist. Gezählt wird
# ohnehin nur, wo auch Solardaten vorliegen.
FETCH_FUTURE_SECONDS = 48 * 3600
SLOT_SEKUNDEN = 900


def quartal(monat: int) -> int:
    return (monat - 1) // 3 + 1


def parse_solar(payload: Any) -> dict[int, float]:
    """Energy-Charts-JSON → ``{Epochenviertelstunde: MW}`` der Solarerzeugung.

    Das Format: ``unix_seconds`` (15-Minuten-Raster) und ``production_types``
    mit Name und Datenreihe; ``None`` steht für noch nicht gemeldete Werte
    (die letzten rund zwei Stunden). Gelesen wird nur ``Solar``; Nullen fallen
    weg, weil sie als Gewicht nichts beitragen.
    """
    if not isinstance(payload, dict):
        return {}
    zeiten = payload.get("unix_seconds") or []
    reihe = None
    for typ in payload.get("production_types") or []:
        if isinstance(typ, dict) and str(typ.get("name", "")).strip().lower() == "solar":
            reihe = typ.get("data") or []
            break
    if reihe is None:
        return {}
    gewichte: dict[int, float] = {}
    for t, v in zip(zeiten, reihe):
        if v is None:
            continue
        try:
            wert = float(v)
            slot = int(t) // SLOT_SEKUNDEN
        except (TypeError, ValueError):
            continue
        if wert > 0:
            gewichte[slot] = wert
    return gewichte


def gewichtetes_mittel(
    preise: dict[int, float], gewichte: dict[int, float]
) -> tuple[float | None, int]:
    """Mengengewichtetes Preismittel über die Slots, die beides haben.

    Rückgabe: (€/kWh, Anzahl gewerteter Slots); ohne Überlappung (None, 0).
    """
    zaehler = nenner = 0.0
    anzahl = 0
    for slot, gewicht in gewichte.items():
        preis = preise.get(slot)
        if preis is None or gewicht <= 0:
            continue
        zaehler += preis * gewicht
        nenner += gewicht
        anzahl += 1
    if nenner <= 0:
        return None, 0
    return zaehler / nenner, anzahl


def tarif_aus_roh(roh: float, anker: float | None, ausgleichsenergie: float) -> float:
    """Korridor anwenden und Ausgleichsenergie abziehen — alles in €/kWh.

    ``anker`` ist der Quartalsmarktpreis Q; ohne ihn (None) entfällt der
    Korridor, der Rohwert geht dann ungebremst durch.
    """
    wert = roh
    if anker is not None and anker > 0:
        wert = min(max(roh, KORRIDOR_UNTEN * anker), anker)
    return round(wert - ausgleichsenergie, 6)


_QUARTAL_RE = re.compile(r"\bQ([1-4])\s*(\d{4})\b")


def _zahl(text: str) -> float | None:
    """„109,23" → 109.23; „1.234,56" → 1234.56; „103.58" → 103.58."""
    t = (text or "").strip()
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    treffer = re.search(r"-?\d+(?:\.\d+)?", t)
    if not treffer:
        return None
    try:
        return float(treffer.group(0))
    except ValueError:
        return None


def parse_econtrol(html: str) -> tuple[int, int, float] | None:
    """E-Control-Seite → (Jahr, Quartal, Q in €/kWh).

    Die Seite zeigt die Rechnung nach § 41 Abs. 1: eine Tabelle mit den
    Settlement-Preisen der nächsten vier Quartalsfutures an den letzten fünf
    Handelstagen des Vorquartals, darunter „Mittelwert über die 5 Tage" in
    €/MWh. Das erste gelistete Future ist das Quartal, für das der Anker gilt
    — die Handelstage liegen im Vorquartal, deshalb kann man ihn nicht aus
    dem Datum ablesen.
    """
    for tabelle in re.findall(r"<table.*?</table>", html or "", re.S | re.I):
        jahr = q = None
        anker = None
        for zeile in re.findall(r"<tr.*?</tr>", tabelle, re.S | re.I):
            zellen = [
                html_entities.unescape(re.sub(r"<[^>]+>", " ", z)).replace("\xa0", " ").strip()
                for z in re.findall(r"<t[dh].*?</t[dh]>", zeile, re.S | re.I)
            ]
            if not zellen:
                continue
            kopf = zellen[0]
            treffer = _QUARTAL_RE.search(kopf)
            if treffer and q is None:
                q, jahr = int(treffer.group(1)), int(treffer.group(2))
            kopf_l = kopf.lower()
            if "mittelwert" in kopf_l and "5 tage" in kopf_l and len(zellen) >= 2:
                anker = _zahl(zellen[1])
        if q is not None and jahr is not None and anker is not None and anker > 0:
            return jahr, q, round(anker / 1000.0, 6)  # €/MWh → €/kWh
    return None


def _iso(slot: int | None) -> str | None:
    if slot is None:
        return None
    return datetime.fromtimestamp((slot + 1) * SLOT_SEKUNDEN, tz=timezone.utc).isoformat()


class OemagSchaetzer:
    """Rechnet den laufenden Monat hoch und hält das Ergebnis über Neustarts."""

    def __init__(self, hass: Any, entry_id: str, oemag: Any = None) -> None:
        self._hass = hass
        self._entry_id = entry_id
        # Der Tabellen-Provider (oemag.py) liefert Ausgleichsenergie und den
        # Rückfall für den Quartalsanker.
        self._oemag = oemag
        if Store is not None:
            self._store = Store(hass, 1, f"{DOMAIN}_{entry_id}_oemag_schaetzung")
        else:
            self._store = None  # type: ignore[assignment]
        self._preis: float | None = None
        self._roh: float | None = None
        self._anker: float | None = None
        self._anker_quelle: str | None = None
        self._ausgleichsenergie: float | None = None
        self._jahr: int | None = None
        self._monat: int | None = None
        self._slots = 0
        self._preise_bis: int | None = None
        self._solar_bis: int | None = None
        self._geholt: datetime | None = None
        self._fehler: str | None = None
        # Quartalsanker der E-Control, „JJJJ-Q" → €/kWh. Die Seite zeigt nur
        # das laufende Quartal; gemerkt wird jeder, den wir je gesehen haben.
        self._anker_tabelle: dict[str, float] = {}
        self._anker_geholt: datetime | None = None
        self._anker_fehler: str | None = None

    # -- Zustand -------------------------------------------------------

    @property
    def preis(self) -> float | None:
        """Hochgerechneter Tarif in €/kWh für den laufenden Monat, sonst None.

        Eine Hochrechnung aus einem anderen Monat gilt nicht — dann greift
        in ``schedule.py`` der veröffentlichte Vormonat.
        """
        if self._preis is None or self._geholt is None:
            return None
        jetzt = _now_local()
        if (self._jahr, self._monat) != (jetzt.year, jetzt.month):
            return None
        return self._preis

    def status(self) -> dict[str, Any]:
        """Für die Anzeige: Wert, Herleitung, Datenstand, Fehler."""
        alter_min = None
        if self._geholt is not None:
            alter_min = int((_utcnow() - self._geholt).total_seconds() / 60)
        ae = self._ausgleichsenergie
        korridor_von = korridor_bis = None
        if self._anker and ae is not None:
            korridor_von = round(KORRIDOR_UNTEN * self._anker - ae, 6)
            korridor_bis = round(self._anker - ae, 6)
        return {
            "preis": self.preis,
            "roh": self._roh,
            "anker": self._anker,
            "anker_quelle": self._anker_quelle,
            "korridor_von": korridor_von,
            "korridor_bis": korridor_bis,
            "ausgleichsenergie": ae,
            "jahr": self._jahr,
            "monat": self._monat,
            "slots": self._slots,
            "preise_bis": _iso(self._preise_bis),
            "solar_bis": _iso(self._solar_bis),
            "alter_minuten": alter_min,
            "fehler": self._fehler,
            "anker_fehler": self._anker_fehler,
            "quellen": [AWATTAR_URL, ENERGY_CHARTS_URL, ECONTROL_URL],
        }

    # -- Laden und Holen -----------------------------------------------

    async def async_load(self) -> None:
        if self._store is None:
            return
        try:
            stored = await self._store.async_load()
            if not stored or not isinstance(stored, dict):
                return
            self._preis = stored.get("preis")
            self._roh = stored.get("roh")
            self._anker = stored.get("anker")
            self._anker_quelle = stored.get("anker_quelle")
            self._ausgleichsenergie = stored.get("ausgleichsenergie")
            self._jahr = stored.get("jahr")
            self._monat = stored.get("monat")
            self._slots = int(stored.get("slots") or 0)
            self._preise_bis = stored.get("preise_bis")
            self._solar_bis = stored.get("solar_bis")
            if stored.get("geholt"):
                self._geholt = datetime.fromisoformat(stored["geholt"])
            self._anker_tabelle = {
                str(k): float(v) for k, v in (stored.get("anker_tabelle") or {}).items()
            }
            if stored.get("anker_geholt"):
                self._anker_geholt = datetime.fromisoformat(stored["anker_geholt"])
        except Exception:
            _LOGGER.debug("OeMAG-Hochrechnung: kein gespeicherter Stand vorhanden")

    async def _speichere(self) -> None:
        if self._store is None:
            return
        try:
            await self._store.async_save(
                {
                    "preis": self._preis,
                    "roh": self._roh,
                    "anker": self._anker,
                    "anker_quelle": self._anker_quelle,
                    "ausgleichsenergie": self._ausgleichsenergie,
                    "jahr": self._jahr,
                    "monat": self._monat,
                    "slots": self._slots,
                    "preise_bis": self._preise_bis,
                    "solar_bis": self._solar_bis,
                    "geholt": self._geholt.isoformat() if self._geholt else None,
                    "anker_tabelle": self._anker_tabelle,
                    "anker_geholt": (
                        self._anker_geholt.isoformat() if self._anker_geholt else None
                    ),
                }
            )
        except Exception:
            _LOGGER.debug("OeMAG-Hochrechnung konnte nicht gespeichert werden")

    async def async_fetch(self, force: bool = False) -> float | None:
        """Hochrechnung erneuern, wenn die letzte älter als drei Stunden ist."""
        jetzt = _utcnow()
        if (
            not force
            and self._geholt is not None
            and (jetzt - self._geholt).total_seconds() < CACHE_FRESH_SECONDS
        ):
            return self.preis
        if async_get_clientsession is None:
            return self.preis

        lokal = _now_local()
        monatsanfang = lokal.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start_s = int(monatsanfang.timestamp())
        ende_s = int(jetzt.timestamp()) + FETCH_FUTURE_SECONDS

        try:
            import aiohttp

            session = async_get_clientsession(self._hass)
            preise_roh = await self._hole(
                session,
                aiohttp,
                AWATTAR_URL,
                {"start": str(start_s * 1000), "end": str(ende_s * 1000)},
            )
            solar_roh = await self._hole(
                session,
                aiohttp,
                ENERGY_CHARTS_URL,
                {
                    "country": "at",
                    "start": monatsanfang.isoformat(timespec="minutes"),
                    "end": jetzt.isoformat(timespec="minutes"),
                },
            )
        except Exception as err:
            self._fehler = str(err)
            _LOGGER.warning(
                "OeMAG-Hochrechnung nicht möglich (%s) — es gilt weiter %s",
                err,
                f"{self._preis:.5f} €/kWh" if self.preis else "der veröffentlichte Monat",
            )
            return self.preis

        preise = parse_marketdata(preise_roh)
        gewichte = parse_solar(solar_roh)
        roh, anzahl = gewichtetes_mittel(preise, gewichte)
        if roh is None:
            self._fehler = "keine Viertelstunde mit Preis und Solarerzeugung"
            _LOGGER.warning(
                "OeMAG-Hochrechnung: %d Preise, %d Solarwerte, aber keine Überlappung"
                " — Format geändert? Es gilt weiter der veröffentlichte Monat.",
                len(preise),
                len(gewichte),
            )
            return self.preis

        # Der Anker ist ein eigener Abruf mit eigener Frist; sein Ausfall
        # kostet nur den Korridor, nicht die Hochrechnung.
        try:
            await self._aktualisiere_anker(session, aiohttp, jetzt, force)
        except Exception as err:  # pragma: no cover - Netz
            self._anker_fehler = str(err)
        ae = self._oemag.ausgleichsenergie if self._oemag is not None else None
        ae = float(ae) if ae else AUSGLEICHSENERGIE_PV_DEFAULT
        anker, anker_quelle = self._anker_fuer(lokal.year, quartal(lokal.month))

        self._preis = tarif_aus_roh(roh, anker, ae)
        self._roh = round(roh, 6)
        self._anker, self._anker_quelle = anker, anker_quelle
        self._ausgleichsenergie = ae
        self._jahr, self._monat = lokal.year, lokal.month
        self._slots = anzahl
        gemeinsam = [s for s in gewichte if s in preise]
        self._preise_bis = max(preise) if preise else None
        self._solar_bis = max(gemeinsam) if gemeinsam else None
        self._geholt, self._fehler = jetzt, None
        _LOGGER.debug(
            "OeMAG-Hochrechnung %d/%d: %.5f €/kWh (roh %.5f, Anker %s, %d Slots)",
            lokal.month,
            lokal.year,
            self._preis,
            roh,
            f"{anker:.5f} von {anker_quelle}" if anker else "keiner",
            anzahl,
        )
        await self._speichere()
        return self._preis

    # -- Hilfen --------------------------------------------------------

    async def _hole(self, session: Any, aiohttp: Any, url: str, params: dict) -> Any:
        async with session.get(
            url,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status} von {url.split('/')[2]}")
            return await resp.json(content_type=None)

    async def _aktualisiere_anker(
        self, session: Any, aiohttp: Any, jetzt: datetime, force: bool
    ) -> None:
        if (
            not force
            and self._anker_geholt is not None
            and (jetzt - self._anker_geholt).total_seconds() < ANKER_FRESH_SECONDS
        ):
            return
        try:
            async with session.get(
                ECONTROL_URL,
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status} von e-control.at")
                html = await resp.text()
        except Exception as err:
            self._anker_fehler = str(err)
            _LOGGER.debug("E-Control-Seite nicht abrufbar: %s", err)
            return
        treffer = parse_econtrol(html)
        if treffer is None:
            self._anker_fehler = "Tabelle nicht lesbar"
            _LOGGER.debug("E-Control-Seite gelesen, aber kein Quartalspreis erkannt")
            return
        jahr, q, wert = treffer
        self._anker_tabelle[f"{jahr}-{q}"] = wert
        self._anker_geholt, self._anker_fehler = jetzt, None

    def _anker_fuer(self, jahr: int, q: int) -> tuple[float | None, str | None]:
        wert = self._anker_tabelle.get(f"{jahr}-{q}")
        if wert:
            return wert, "E-Control"
        if self._oemag is not None:
            rueck = getattr(self._oemag, "anker_fuer_quartal", None)
            wert = rueck(q) if callable(rueck) else None
            if wert:
                return wert, "OeMAG-Tabelle"
        return None, None
