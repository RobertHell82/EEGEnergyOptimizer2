"""Spitzenkappung: Verbraucher abschalten, bevor die Bezugsspitze steigt.

Mit der neuen Tarifstruktur (Umstellung je Netzbereich 2027/28, siehe
leistungsspitze.UMSTELLUNG) richtet sich der Leistungspreis nach dem höchsten Viertelstunden-
Mittel des Netzbezugs im Monat (siehe leistungsspitze.py). Diese Wache
schaltet konfigurierte Verbraucher — typischer Fall: das Laden des E-Autos —
ab, wenn die laufende Viertelstunde darüber hinauszuwachsen droht, und
schaltet sie wieder ein, wenn wieder Platz ist.

Bewusst am **Viertelstunden-Mittel**, nicht am Augenblickswert: Verrechnet
wird das Mittel. Ein Wasserkocher, der 30 s lang den Bezug hochtreibt, hebt
die Viertelstunde kaum — ihn mit dem Abschalten des Autos zu beantworten
hieße, ohne Not zu schalten. Maßgeblich ist die Hochrechnung der laufenden
Viertelstunde (``Leistungsspitze.laufend``), gemessen am Netz, also NACH der
Batterie: Was die Batterie deckt, kommt hier gar nicht erst an.

Die Schwelle ist ``max(Grenzwert, Monatsspitze)`` — liegt die Spitze des
Monats schon höher, kostet jede Viertelstunde bis dorthin nichts mehr.

Regeln, die nicht aus dem Code allein hervorgehen:

- Geschaltet wird nur, was diese Wache selbst abgeschaltet hat. Wer das
  Laden in der App stoppt, bekommt es nicht von hier wieder eingeschaltet;
  wer es von Hand wieder einschaltet, nimmt es aus unserer Hand.
- Nach jedem Abschalten wird ``BERUHIGUNG_S`` gewartet, bevor der nächste
  folgt: Ein Auto regelt über Sekunden herunter, und Fahrzeug-Integrationen
  melden die Leistung oft nur alle 30–60 s. Ohne die Pause wären nach drei
  Takten alle Verbraucher aus, bevor die Messung das erste Abschalten sieht.
- Wieder eingeschaltet wird erst nach ``MIN_AUS_S`` und nur, wenn der
  Verbraucher eine VOLLE Viertelstunde lang neben dem aktuellen Bezug Platz
  hätte (mit ``HYSTERESE_KW`` Abstand). Sonst schaltete man spät in einer
  Viertelstunde ein, weil dort wenig übrig bleibt, und zu Beginn der
  nächsten wieder aus.
- Modus Aus, eine Pause oder die Funktion abgeschaltet: alles, was wir
  abgeschaltet haben, wird wieder eingeschaltet. „Optimierung aus" heißt
  hier wie beim Heizstab: kein Eingriff.
- Zuerst die Batterie: Solange sie mehr abgeben könnte, als sie gerade tut
  (``batterie_luft_kw``, vom Executor), wird nichts abgeschaltet. Der
  Entladedeckel der Spitzenkappung (spitzenreserve.py) folgt alle 30 s; ein
  Abschalten in dieser Zeit wäre unnötig.
- Gegen Pendeln: Gemerkt wird beim Abschalten die HÖCHSTE Leistung der
  letzten ``SPITZE_FENSTER_S`` (ein Auto, das gerade hochregelt, zeigt erst
  2 kW und zieht später 11 kW) — oder die eingetragene Nennleistung, wenn
  sie größer ist. Muss ein Verbraucher binnen ``PENDEL_FENSTER_S`` nach dem
  Wiedereinschalten erneut aus, verdoppelt sich seine Mindest-Aus-Zeit
  (5 → 10 → 20 → 40 → 60 min); nach ``PENDEL_RUHE_S`` ohne erneutes
  Abschalten gilt wieder die kurze.

Die Entscheidung (``entscheide``) ist eine reine Funktion und ohne Home
Assistant testbar; die Klasse verdrahtet sie mit Zeitgeber, Zuständen,
Diensten und Speicher.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

try:  # pragma: no cover - im Testlauf ohne HA
    from homeassistant.helpers.storage import Store
except ImportError:  # pragma: no cover
    Store = None  # type: ignore[assignment,misc]

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_SPITZENKAPPUNG_ENABLED = "spitzenkappung_enabled"
CONF_SPITZENKAPPUNG_GRENZE_KW = "spitzenkappung_grenze_kw"
CONF_SPITZENKAPPUNG_VERBRAUCHER = "spitzenkappung_verbraucher"

GRENZE_MIN_KW = 2.0   # darunter spart es nichts: verrechnet werden mindestens 2 kW
GRENZE_MAX_KW = 20.0
DEFAULT_GRENZE_KW = 5.0
MAX_VERBRAUCHER = 10

# Ab dieser gemessenen Leistung gilt ein Verbraucher als aktiv — darunter
# brächte das Abschalten nichts (Standby, Ladeerhaltung).
AKTIV_KW = 0.2
BERUHIGUNG_S = 60.0
MIN_AUS_S = 300.0
MAX_AUS_S = 3600.0
HYSTERESE_KW = 0.3
# Über dieses Fenster wird die höchste Leistung eines Verbrauchers gemerkt.
SPITZE_FENSTER_S = 300.0
# Erneut abgeschaltet binnen dieser Zeit nach dem Wiedereinschalten: Pendeln.
PENDEL_FENSTER_S = 900.0
# So lange ohne erneutes Abschalten, und die Mindest-Aus-Zeit ist wieder kurz.
PENDEL_RUHE_S = 3600.0
NENNLEISTUNG_MAX_KW = 50.0
# Höher steigt die Pendelstufe nicht — ab hier gilt ohnehin MAX_AUS_S.
PENDEL_STUFE_MAX = 4
# Ein gescheiterter Schaltbefehl wird nicht in jedem Takt wiederholt.
FEHLER_PAUSE_S = 60.0
DIENST_TIMEOUT_S = 15.0
TAKT_S = 10.0

_VIERTELSTUNDE_S = 900.0
_SPEICHER_VERZOEGERUNG_S = 5.0

SCHALT_DOMAENEN = ("switch", "input_boolean")


@dataclass(frozen=True)
class Verbraucher:
    """Ein konfigurierter Verbraucher, in der Reihenfolge der Liste."""

    name: str
    leistung_entity: str
    schalter_entity: str
    # Optional: Leistung, die der Verbraucher eingeschaltet höchstens zieht
    # (z. B. 11 kW Wallbox) — für das Wiedereinschalten, wenn die Messung
    # beim Abschalten weniger zeigte.
    nennleistung_kw: float | None = None


@dataclass(frozen=True)
class VerbraucherZustand:
    """Was die Wache in diesem Takt über einen Verbraucher weiß."""

    leistung_kw: float | None   # None = nicht lesbar
    eingeschaltet: bool | None  # None = Schalter nicht lesbar


@dataclass(frozen=True)
class Aktion:
    schalter_entity: str
    einschalten: bool
    grund: str


def verbraucher_aus_config(config: dict) -> list[Verbraucher]:
    """Gültige Einträge der Konfiguration, ohne Doppelte (je Schalter einer)."""
    roh = config.get(CONF_SPITZENKAPPUNG_VERBRAUCHER) or []
    if not isinstance(roh, list):
        return []
    liste: list[Verbraucher] = []
    gesehen: set[str] = set()
    for i, eintrag in enumerate(roh[:MAX_VERBRAUCHER]):
        if not isinstance(eintrag, dict):
            continue
        schalter = str(eintrag.get("schalter_entity") or "").strip()
        leistung = str(eintrag.get("leistung_entity") or "").strip()
        if not schalter or not leistung or schalter in gesehen:
            continue
        if schalter.split(".", 1)[0] not in SCHALT_DOMAENEN:
            continue
        gesehen.add(schalter)
        name = str(eintrag.get("name") or "").strip() or f"Verbraucher {i + 1}"
        liste.append(Verbraucher(name, leistung, schalter, _nennleistung(eintrag)))
    return liste


def _nennleistung(eintrag: dict) -> float | None:
    """Nennleistung in kW aus dem Eintrag; leer, 0 oder unlesbar = keine."""
    try:
        wert = float(eintrag.get("nennleistung_kw") or 0.0)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(wert) or wert <= 0:
        return None
    return min(wert, NENNLEISTUNG_MAX_KW)


def mindest_aus_s(stufe: int) -> float:
    """Mindest-Aus-Zeit nach ``stufe``-fachem Pendeln (verdoppelt, gedeckelt)."""
    return min(MIN_AUS_S * (2 ** min(max(0, stufe), PENDEL_STUFE_MAX)), MAX_AUS_S)


def pendel_stufe(
    jetzt: datetime, stufe: int, wieder_ein: datetime | None
) -> int:
    """Stufe beim erneuten Abschalten: höher bei Pendeln, 0 nach langer Ruhe."""
    if wieder_ein is None:
        return 0
    seit = (jetzt - wieder_ein).total_seconds()
    if seit < PENDEL_FENSTER_S:
        return min(stufe + 1, PENDEL_STUFE_MAX)
    if seit >= PENDEL_RUHE_S:
        return 0
    return stufe


def grenze_aus_config(config: dict) -> float:
    try:
        wert = float(config.get(CONF_SPITZENKAPPUNG_GRENZE_KW) or DEFAULT_GRENZE_KW)
    except (TypeError, ValueError):
        wert = DEFAULT_GRENZE_KW
    if not math.isfinite(wert):
        wert = DEFAULT_GRENZE_KW
    return min(max(wert, GRENZE_MIN_KW), GRENZE_MAX_KW)


def schwelle_kw(grenze_kw: float, monatsspitze_kw: float | None) -> float:
    """Ab hier kostet eine Viertelstunde: Grenzwert oder schon erreichte Spitze."""
    if monatsspitze_kw is None or not math.isfinite(monatsspitze_kw):
        return grenze_kw
    return max(grenze_kw, monatsspitze_kw)


def entscheide(
    *,
    jetzt: datetime,
    laufend: dict[str, Any] | None,
    schwelle: float,
    verbraucher: list[Verbraucher],
    zustaende: dict[str, VerbraucherZustand],
    abgeschaltet: dict[str, dict[str, Any]],
    letztes_abschalten: datetime | None,
    batterie_luft_kw: float = 0.0,
) -> list[Aktion]:
    """Die Schaltbefehle dieses Takts.

    ``laufend`` ist ``Leistungsspitze.laufend`` (mit ``hochrechnung_kw``,
    ``bezug_kw`` und ``rest_s``). Ohne frische Messung wird nichts geschaltet
    — weder ab noch wieder ein: Blind einzuschalten könnte genau die Spitze
    erzeugen, die die Wache verhindern soll.

    ``batterie_luft_kw``: was die Batterie für den Rest der Viertelstunde
    noch zusätzlich abgeben könnte. Es zählt beim Abschalten mit, beim
    Wiedereinschalten nicht — wer wieder an ist, soll auch ohne die Batterie
    unter die Schwelle passen, sonst leerte er die Reserve.
    """
    if not laufend:
        return []
    hochrechnung = laufend.get("hochrechnung_kw")
    bezug = laufend.get("bezug_kw")
    rest_s = float(laufend.get("rest_s") or 0.0)
    if hochrechnung is None or bezug is None:
        return []
    anteil = max(min(rest_s / _VIERTELSTUNDE_S, 1.0), 0.0)

    # Was die gerade abgeschalteten noch ziehen (Herunterregeln, verzögerte
    # Meldung), ist so gut wie weg — es zählt beim Bedarf nicht noch einmal.
    noch_im_abklingen = sum(
        max(zustaende[v.schalter_entity].leistung_kw or 0.0, 0.0)
        for v in verbraucher
        if v.schalter_entity in abgeschaltet and v.schalter_entity in zustaende
    )
    erwartet = hochrechnung - noch_im_abklingen * anteil
    erwartet -= max(0.0, batterie_luft_kw) * anteil

    if erwartet > schwelle:
        if (
            letztes_abschalten is not None
            and (jetzt - letztes_abschalten).total_seconds() < BERUHIGUNG_S
        ):
            return []
        # Der Reihe nach, bis die Hochrechnung unter der Schwelle läge. Spät
        # in der Viertelstunde bringt jedes Abschalten weniger — es wird
        # trotzdem geschaltet: Eine Viertelstunde, die ohnehin drüber liegt,
        # soll wenigstens so wenig wie möglich drüber liegen.
        aktionen: list[Aktion] = []
        for v in verbraucher:
            if v.schalter_entity in abgeschaltet:
                continue
            z = zustaende.get(v.schalter_entity)
            if z is None or z.eingeschaltet is not True:
                continue
            if z.leistung_kw is None or z.leistung_kw < AKTIV_KW:
                continue
            aktionen.append(Aktion(
                v.schalter_entity, False,
                f"Hochrechnung {hochrechnung:.2f} kW über {schwelle:.2f} kW",
            ))
            erwartet -= z.leistung_kw * anteil
            if erwartet <= schwelle:
                break
        return aktionen

    # Wieder einschalten: in umgekehrter Listenreihenfolge (wer zuerst ging,
    # kommt zuletzt), höchstens einer je Takt — der nächste Takt sieht dann
    # schon, was der erste dazugebracht hat.
    for v in reversed(verbraucher):
        info = abgeschaltet.get(v.schalter_entity)
        if info is None:
            continue
        seit = info.get("seit")
        try:
            min_aus = float(info.get("min_aus_s") or MIN_AUS_S)
        except (TypeError, ValueError):
            min_aus = MIN_AUS_S
        if isinstance(seit, datetime) and (jetzt - seit).total_seconds() < min_aus:
            continue
        leistung = float(info.get("leistung_kw") or 0.0)
        voll = bezug + leistung <= schwelle - HYSTERESE_KW
        diese = hochrechnung + leistung * anteil <= schwelle - HYSTERESE_KW
        if voll and diese:
            return [Aktion(
                v.schalter_entity, True,
                f"Platz unter {schwelle:.2f} kW (Bezug {bezug:.2f} kW + {leistung:.2f} kW)",
            )]
        # Ein Späterer in der Reihe darf nicht vor einem Früheren zurück,
        # sonst stünde bei knappem Platz immer derselbe draußen.
        break
    return []


def _jetzt_utc() -> datetime:
    return datetime.now(timezone.utc)


class Spitzenkappung:
    """Verdrahtung: 10-s-Takt, Zustände lesen, Dienste rufen, Stand merken."""

    def __init__(
        self,
        hass: Any,
        entry_id: str,
        config_quelle: Callable[[], dict],
        leistungsspitze: Any,
        steuern_erlaubt: Callable[[], bool],
        protokoll: Callable[[str, str], None] | None = None,
        batterie_luft: Callable[[], float] | None = None,
    ) -> None:
        self._hass = hass
        self._config_quelle = config_quelle
        self._spitze = leistungsspitze
        self._steuern_erlaubt = steuern_erlaubt
        self._protokoll = protokoll
        # Was die Batterie zusätzlich decken könnte (Executor); None = 0.
        self._batterie_luft = batterie_luft
        self._store: Any = (
            Store(hass, 1, f"{DOMAIN}_{entry_id}_spitzenkappung")
            if Store is not None and hass is not None
            else None
        )
        # Schalter-Entität → {"seit", "leistung_kw", "name"}
        self._abgeschaltet: dict[str, dict[str, Any]] = {}
        self._letztes_abschalten: datetime | None = None
        self._fehler_bis: dict[str, datetime] = {}
        self._letzter_fehler: str | None = None
        self._schwelle: float | None = None
        # Gemessene Leistung je Verbraucher über SPITZE_FENSTER_S:
        # Schalter → [(Zeit, kW), …]
        self._leistung_verlauf: dict[str, list[tuple[datetime, float]]] = {}
        # Pendelschutz je Verbraucher: {"stufe", "wieder_ein"}
        self._pendel: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._stillgelegt = False
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------------
    # Zustand nach außen
    # ------------------------------------------------------------------
    @property
    def aktiv(self) -> bool:
        return bool(self._config_quelle().get(CONF_SPITZENKAPPUNG_ENABLED))

    def status(self) -> dict[str, Any]:
        return {
            "aktiv": self.aktiv,
            "schwelle_kw": self._schwelle,
            "abgeschaltet": [
                {
                    "name": info.get("name"),
                    "entity": entity,
                    "seit": info["seit"].isoformat() if isinstance(info.get("seit"), datetime) else None,
                    "leistung_kw": info.get("leistung_kw"),
                    "min_aus_s": info.get("min_aus_s", MIN_AUS_S),
                }
                for entity, info in self._abgeschaltet.items()
            ],
            "fehler": self._letzter_fehler,
        }

    def verbraucher_status(self, jetzt: datetime | None = None) -> list[dict[str, Any]]:
        """Jeder konfigurierte Verbraucher mit seinem Zustand — für die Karte.

        ``zustand``: ``abgeschaltet`` (von uns, mit ``frei_ab``, ab wann er
        frühestens zurück darf), ``an``, ``aus`` (von Hand oder von selbst)
        oder ``unbekannt`` (Schalter nicht lesbar).
        """
        jetzt = jetzt or _jetzt_utc()
        liste = []
        for v in verbraucher_aus_config(self._config_quelle()):
            info = self._abgeschaltet.get(v.schalter_entity)
            z = self._zustand(v) if self._hass is not None else VerbraucherZustand(None, None)
            eintrag: dict[str, Any] = {
                "name": v.name,
                "entity": v.schalter_entity,
                "leistung_kw": None if z.leistung_kw is None else round(z.leistung_kw, 2),
            }
            if info is not None:
                eintrag["zustand"] = "abgeschaltet"
                seit = info.get("seit")
                if isinstance(seit, datetime):
                    frei = seit + timedelta(seconds=float(info.get("min_aus_s") or MIN_AUS_S))
                    eintrag["seit"] = seit.isoformat()
                    eintrag["frei_ab"] = frei.isoformat() if frei > jetzt else None
            elif z.eingeschaltet is True:
                eintrag["zustand"] = "an"
            elif z.eingeschaltet is False:
                eintrag["zustand"] = "aus"
            else:
                eintrag["zustand"] = "unbekannt"
            liste.append(eintrag)
        return liste

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
                _LOGGER.exception("Spitzenkappung: Beobachter fehlgeschlagen")

    # ------------------------------------------------------------------
    # Speicher
    # ------------------------------------------------------------------
    def _als_dict(self) -> dict[str, Any]:
        return {
            "abgeschaltet": {
                entity: {
                    **info,
                    "seit": info["seit"].isoformat() if isinstance(info.get("seit"), datetime) else None,
                }
                for entity, info in self._abgeschaltet.items()
            },
            # Pendelschutz: Ein Neustart soll ein pendelndes Gerät nicht
            # wieder bei 5 Minuten beginnen lassen.
            "pendel": {
                entity: {
                    "stufe": int(info.get("stufe") or 0),
                    "wieder_ein": info["wieder_ein"].isoformat()
                    if isinstance(info.get("wieder_ein"), datetime) else None,
                }
                for entity, info in self._pendel.items()
            },
        }

    def aus_dict(self, daten: dict[str, Any]) -> None:
        pendel = daten.get("pendel") or {}
        if isinstance(pendel, dict):
            for entity, info in pendel.items():
                if not isinstance(info, dict):
                    continue
                try:
                    wieder_ein = datetime.fromisoformat(info.get("wieder_ein"))
                except (TypeError, ValueError):
                    wieder_ein = None
                try:
                    stufe = max(0, min(int(info.get("stufe") or 0), PENDEL_STUFE_MAX))
                except (TypeError, ValueError):
                    stufe = 0
                self._pendel[entity] = {"stufe": stufe, "wieder_ein": wieder_ein}
        roh = daten.get("abgeschaltet") or {}
        if not isinstance(roh, dict):
            return
        for entity, info in roh.items():
            if not isinstance(info, dict):
                continue
            try:
                seit = datetime.fromisoformat(info.get("seit"))
            except (TypeError, ValueError):
                seit = _jetzt_utc()
            try:
                min_aus = float(info.get("min_aus_s") or MIN_AUS_S)
            except (TypeError, ValueError):
                min_aus = MIN_AUS_S
            self._abgeschaltet[entity] = {
                "seit": seit,
                "leistung_kw": float(info.get("leistung_kw") or 0.0),
                "name": info.get("name"),
                "min_aus_s": min_aus,
            }

    async def async_load(self) -> None:
        if self._store is None:
            return
        try:
            daten = await self._store.async_load()
        except Exception:  # noqa: BLE001
            return
        if isinstance(daten, dict):
            self.aus_dict(daten)

    # ------------------------------------------------------------------
    # Lesen und Schalten
    # ------------------------------------------------------------------
    def _zustand(self, v: Verbraucher) -> VerbraucherZustand:
        from .power_readings import read_power_kw

        leistung = read_power_kw(self._hass, v.leistung_entity)
        if leistung is not None:
            leistung = abs(leistung)  # Vorzeichen je Integration verschieden
        s = self._hass.states.get(v.schalter_entity)
        if s is None or s.state in ("unknown", "unavailable", ""):
            ein = None
        else:
            ein = s.state == "on"
        return VerbraucherZustand(leistung, ein)

    async def _schalte(self, entity: str, ein: bool) -> bool:
        domaene = entity.split(".", 1)[0]
        try:
            await asyncio.wait_for(
                self._hass.services.async_call(
                    domaene, "turn_on" if ein else "turn_off",
                    {"entity_id": entity}, blocking=True,
                ),
                timeout=DIENST_TIMEOUT_S,
            )
        except Exception as err:  # noqa: BLE001 — Dienstfehler jeder Art
            self._letzter_fehler = f"{entity}: {err or type(err).__name__}"
            _LOGGER.warning(
                "Spitzenkappung: %s %s fehlgeschlagen: %s",
                entity, "einschalten" if ein else "abschalten", err,
            )
            return False
        self._letzter_fehler = None
        return True

    def _log(self, zustand: str, grund: str) -> None:
        _LOGGER.info("Spitzenkappung: %s — %s", zustand, grund)
        if self._protokoll is not None:
            try:
                self._protokoll(zustand, grund)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Spitzenkappung: Protokolleintrag fehlgeschlagen")

    async def async_alles_zurueck(self, grund: str) -> None:
        """Alles wieder einschalten, was wir abgeschaltet haben."""
        for entity in list(self._abgeschaltet):
            info = self._abgeschaltet[entity]
            s = self._hass.states.get(entity)
            # Steht er schon wieder an (Hand, Automation), ist nichts zu tun.
            if s is None or s.state != "on":
                if not await self._schalte(entity, True):
                    continue
                self._log(f"Spitzenkappung: {info.get('name') or entity} ein", grund)
            self._abgeschaltet.pop(entity, None)
        self._geaendert()

    async def async_takt(self, jetzt: datetime | None = None) -> None:
        if self._stillgelegt or self._lock.locked():
            return
        async with self._lock:
            await self._takt(jetzt or _jetzt_utc())

    async def _takt(self, jetzt: datetime) -> None:
        config = self._config_quelle()
        verbraucher = verbraucher_aus_config(config)
        bekannt = {v.schalter_entity for v in verbraucher}

        if not config.get(CONF_SPITZENKAPPUNG_ENABLED) or not self._steuern_erlaubt():
            self._schwelle = None
            if self._abgeschaltet:
                await self.async_alles_zurueck(
                    "Spitzenkappung aus" if not config.get(CONF_SPITZENKAPPUNG_ENABLED)
                    else "Optimierung aus oder pausiert"
                )
            return

        # Aus der Liste entfernt: zurückgeben, was wir ihm genommen haben.
        for entity in [e for e in self._abgeschaltet if e not in bekannt]:
            if await self._schalte(entity, True):
                self._log(
                    f"Spitzenkappung: {self._abgeschaltet[entity].get('name') or entity} ein",
                    "nicht mehr in der Liste",
                )
                self._abgeschaltet.pop(entity, None)
                self._geaendert()

        zustaende = {v.schalter_entity: self._zustand(v) for v in verbraucher}
        self._leistung_merken(jetzt, zustaende)

        # Von Hand wieder eingeschaltet: nicht mehr unser Eingriff. Erst nach
        # der Beruhigungszeit — Fahrzeug-Integrationen melden den neuen
        # Schalterstand oft erst nach dem nächsten Abruf aus der Cloud.
        for entity in list(self._abgeschaltet):
            seit = self._abgeschaltet[entity].get("seit")
            if isinstance(seit, datetime) and (jetzt - seit).total_seconds() < BERUHIGUNG_S:
                continue
            if zustaende.get(entity) and zustaende[entity].eingeschaltet is True:
                self._abgeschaltet.pop(entity, None)
                self._log(
                    f"Spitzenkappung: {entity} von Hand eingeschaltet",
                    "wird nicht mehr gesteuert, bis es wieder nötig ist",
                )
                self._geaendert()

        spitze = (self._spitze.spitze or {}).get("kw") if self._spitze else None
        schwelle = schwelle_kw(grenze_aus_config(config), spitze)
        if schwelle != self._schwelle:
            self._schwelle = schwelle
            self._geaendert()

        aktionen = entscheide(
            jetzt=jetzt,
            laufend=self._spitze.laufend(jetzt) if self._spitze else None,
            schwelle=schwelle,
            verbraucher=verbraucher,
            zustaende=zustaende,
            abgeschaltet=self._abgeschaltet,
            letztes_abschalten=self._letztes_abschalten,
            batterie_luft_kw=self._luft_kw(),
        )
        namen = {v.schalter_entity: v.name for v in verbraucher}
        nenn = {v.schalter_entity: v.nennleistung_kw for v in verbraucher}
        for a in aktionen:
            gesperrt = self._fehler_bis.get(a.schalter_entity)
            if gesperrt is not None and jetzt < gesperrt:
                continue
            if not await self._schalte(a.schalter_entity, a.einschalten):
                self._fehler_bis[a.schalter_entity] = jetzt + timedelta(seconds=FEHLER_PAUSE_S)
                self._geaendert()
                continue
            self._fehler_bis.pop(a.schalter_entity, None)
            name = namen.get(a.schalter_entity, a.schalter_entity)
            if a.einschalten:
                self._abgeschaltet.pop(a.schalter_entity, None)
                pendel = self._pendel.setdefault(a.schalter_entity, {"stufe": 0})
                pendel["wieder_ein"] = jetzt
                self._log(f"Spitzenkappung: {name} ein", a.grund)
            else:
                z = zustaende[a.schalter_entity]
                leistung = max(
                    z.leistung_kw or 0.0,
                    self._spitze_der_letzten_minuten(a.schalter_entity, jetzt),
                    nenn.get(a.schalter_entity) or 0.0,
                )
                pendel = self._pendel.setdefault(a.schalter_entity, {"stufe": 0})
                pendel["stufe"] = pendel_stufe(
                    jetzt, int(pendel.get("stufe") or 0), pendel.get("wieder_ein")
                )
                min_aus = mindest_aus_s(pendel["stufe"])
                self._abgeschaltet[a.schalter_entity] = {
                    "seit": jetzt,
                    "leistung_kw": round(leistung, 3),
                    "name": name,
                    "min_aus_s": min_aus,
                }
                self._letztes_abschalten = jetzt
                grund = a.grund
                if pendel["stufe"] > 0:
                    grund += f" — erneut binnen kurzem, bleibt mindestens {min_aus / 60:.0f} min aus"
                self._log(f"Spitzenkappung: {name} aus", grund)
            self._geaendert()

    def _luft_kw(self) -> float:
        if self._batterie_luft is None:
            return 0.0
        try:
            wert = float(self._batterie_luft() or 0.0)
        except Exception:  # noqa: BLE001 — im Zweifel ohne Batterie rechnen
            _LOGGER.debug("Spitzenkappung: Batteriereserve nicht lesbar", exc_info=True)
            return 0.0
        return wert if math.isfinite(wert) and wert > 0 else 0.0

    def _leistung_merken(
        self, jetzt: datetime, zustaende: dict[str, VerbraucherZustand]
    ) -> None:
        grenze = jetzt - timedelta(seconds=SPITZE_FENSTER_S)
        for entity, z in zustaende.items():
            liste = [p for p in self._leistung_verlauf.get(entity, []) if p[0] >= grenze]
            if z.leistung_kw is not None and math.isfinite(z.leistung_kw):
                liste.append((jetzt, z.leistung_kw))
            self._leistung_verlauf[entity] = liste
        for entity in [e for e in self._leistung_verlauf if e not in zustaende]:
            del self._leistung_verlauf[entity]

    def _spitze_der_letzten_minuten(self, entity: str, jetzt: datetime) -> float:
        grenze = jetzt - timedelta(seconds=SPITZE_FENSTER_S)
        werte = [kw for t, kw in self._leistung_verlauf.get(entity, []) if t >= grenze]
        return max(werte) if werte else 0.0

    # ------------------------------------------------------------------
    # Anbindung an Home Assistant
    # ------------------------------------------------------------------
    def async_start(self, entry: Any) -> None:
        try:
            from homeassistant.helpers.event import async_track_time_interval
        except ImportError:  # pragma: no cover
            return

        async def _tick(_now: Any) -> None:
            try:
                await self.async_takt()
            except Exception:  # noqa: BLE001 — der Takt darf nie kippen
                _LOGGER.exception("Spitzenkappung: Takt fehlgeschlagen")

        entry.async_on_unload(
            async_track_time_interval(self._hass, _tick, timedelta(seconds=TAKT_S))
        )

    def stilllegen(self) -> None:
        self._stillgelegt = True

    async def async_shutdown(self) -> None:
        """Beim Entladen: zurückgeben, was wir abgeschaltet haben.

        Sonst bliebe das Auto ohne Aufsicht aus — wer die Integration
        entfernt, fände es nie wieder eingeschaltet.
        """
        self._stillgelegt = True
        async with self._lock:
            if self._abgeschaltet:
                await self.async_alles_zurueck("Integration entladen")
        if self._store is not None:
            await self._store.async_save(self._als_dict())
