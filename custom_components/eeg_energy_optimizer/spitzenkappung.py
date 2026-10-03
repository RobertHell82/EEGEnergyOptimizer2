"""Spitzenkappung: Verbraucher abschalten, bevor die Bezugsspitze steigt.

Ab 2027 richtet sich der Leistungspreis nach dem höchsten Viertelstunden-
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
HYSTERESE_KW = 0.3
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
        liste.append(Verbraucher(name, leistung, schalter))
    return liste


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
) -> list[Aktion]:
    """Die Schaltbefehle dieses Takts.

    ``laufend`` ist ``Leistungsspitze.laufend`` (mit ``hochrechnung_kw``,
    ``bezug_kw`` und ``rest_s``). Ohne frische Messung wird nichts geschaltet
    — weder ab noch wieder ein: Blind einzuschalten könnte genau die Spitze
    erzeugen, die die Wache verhindern soll.
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
        if isinstance(seit, datetime) and (jetzt - seit).total_seconds() < MIN_AUS_S:
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
    ) -> None:
        self._hass = hass
        self._config_quelle = config_quelle
        self._spitze = leistungsspitze
        self._steuern_erlaubt = steuern_erlaubt
        self._protokoll = protokoll
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
                }
                for entity, info in self._abgeschaltet.items()
            ],
            "fehler": self._letzter_fehler,
        }

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
            }
        }

    def aus_dict(self, daten: dict[str, Any]) -> None:
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
            self._abgeschaltet[entity] = {
                "seit": seit,
                "leistung_kw": float(info.get("leistung_kw") or 0.0),
                "name": info.get("name"),
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
        )
        namen = {v.schalter_entity: v.name for v in verbraucher}
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
                self._log(f"Spitzenkappung: {name} ein", a.grund)
            else:
                z = zustaende[a.schalter_entity]
                self._abgeschaltet[a.schalter_entity] = {
                    "seit": jetzt,
                    "leistung_kw": round(z.leistung_kw or 0.0, 3),
                    "name": name,
                }
                self._letztes_abschalten = jetzt
                self._log(f"Spitzenkappung: {name} aus", a.grund)
            self._geaendert()

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
