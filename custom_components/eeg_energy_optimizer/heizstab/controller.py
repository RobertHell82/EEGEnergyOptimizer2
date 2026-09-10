"""Heizstab-Steuerung: wann heizt der Heizstab, mit wie viel, und warum.

Der Heizstab ist eine Senke für Überschuss, den weder Batterie noch Netz
aufnehmen. Der Fahrplan kennt diesen Überschuss als Spalte ``discard`` je
Viertelstunde (Prognose, für Anzeige und Bilanz) — geregelt wird aber nach
der MESSUNG, nicht nach der Prognose: Ist die PV real niedriger als geplant,
zöge ein fest geschriebener Sollwert Netzstrom.

Die Regel je Guard-Lauf (30 s), siehe ``naechster_sollwert``:

* Nicht erlaubt (Optimierung aus, Startphase, Entladung, kein Messwert) → 0.
* Unter der Mindesttemperatur → Vorrang vor der Einspeisung: der Heizstab
  nimmt allen PV-Überschuss, auch den unterhalb der Einspeisegrenze (Regel
  auf „Einspeisung ≈ 0"), aber weder Netz- noch Batteriestrom. Nur wenn
  „auch aus dem Netz heizen" erlaubt ist, läuft er mit voller Leistung —
  dann unterdrückt der Executor derweil jede erzwungene Entladung, sonst
  landete die Batterie im Boiler.
* Maximaltemperatur erreicht → 0 (bis hierher darf geheizt werden), frei erst wieder 3 K darunter.
* Einspeisung klebt an der Grenze → ein Schritt (0,5 kW) hinauf. Die wahre
  Höhe des Überschusses ist dann unsichtbar, der Wechselrichter regelt schon
  ab — deshalb tasten statt springen.
* Einspeisung deutlich unter der Grenze → um genau die Lücke hinunter, in
  einem Lauf. Bei Netzbezug wird die Lücke größer als die Grenze und der
  Sollwert fällt auf 0. Nach unten ist die Lücke messbar, es gibt nichts zu
  ertasten.
* Dazwischen (totes Band) → halten.

Vorrang gegenüber der Batterie steckt in ``vorrang_frei``: der Executor
gibt den Heizstab frei, wenn er nach der Konfiguration an der Reihe ist —
bei „Überschuss zuerst in den Heizstab" immer, bei Batterie-Vorrang erst,
wenn das Ladelimit am Maximum steht oder die Batterie voll ist. Umgekehrt
wartet Guard 1 (Ladelimit anheben) auf ``gesaettigt``.

Der Heizstab ist absichtlich KEINE Variable im LP: Haralds Modell bleibt
unverändert (siehe CLAUDE.md). ``discard`` fällt beim Optimieren ohnehin
an, der Heizstab ist die Verwendung dafür.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from ..const import (
    CONF_HEIZSTAB_ALT_TEMP_C,
    CONF_HEIZSTAB_ENABLED,
    CONF_HEIZSTAB_HOST,
    CONF_HEIZSTAB_MAX_KW,
    CONF_HEIZSTAB_MINTEMP_C,
    CONF_HEIZSTAB_NETZBEZUG,
    CONF_HEIZSTAB_PORT,
    CONF_HEIZSTAB_VORRANG,
    CONF_HEIZSTAB_WAERMEWERT,
    CONF_HEIZSTAB_MAXTEMP_C,
    DEFAULT_HEIZSTAB_ALT_TEMP_C,
    DEFAULT_HEIZSTAB_ENABLED,
    DEFAULT_HEIZSTAB_MAX_KW,
    DEFAULT_HEIZSTAB_MINTEMP_C,
    DEFAULT_HEIZSTAB_NETZBEZUG,
    DEFAULT_HEIZSTAB_PORT,
    DEFAULT_HEIZSTAB_VORRANG,
    DEFAULT_HEIZSTAB_WAERMEWERT,
    DEFAULT_HEIZSTAB_MAXTEMP_C,
    GUARD_EXPORT_RELEASE_KW,
    GUARD_EXPORT_STICKY_BAND_KW,
    HEIZSTAB_KOMFORT_EXPORT_ZIEL_KW,
    HEIZSTAB_MINTEMP_HYSTERESE_K,
    HEIZSTAB_SATT_TOLERANZ_KW,
    HEIZSTAB_STEP_KW,
    HEIZSTAB_TEMP_HYSTERESE_K,
)

_LOGGER = logging.getLogger(__name__)

# Unter dieser Änderung wird nicht sofort geschrieben — der 30-s-Schreiber
# bringt den Wert ohnehin mit dem nächsten Watchdog-Takt.
_SOFORT_SCHREIBEN_AB_KW = 0.05


def normalisiere_host(roh: Any) -> str:
    """Aus einer Eingabe die reine Modbus-Adresse machen.

    Der Ohmpilot hat ein Webinterface, also trägt man naheliegend dessen URL
    ein („http://192.168.100.58/"). pymodbus braucht aber Host oder IP allein
    — mit der URL scheitert jeder Verbindungsversuch, und die Meldung
    („Verbindung fehlgeschlagen http://192.168.100.58/:502") liest sich wie
    ein Netzwerkproblem. Deshalb wird hier geschält statt abgelehnt:
    Schema, Pfad, Port-Anhang und Leerzeichen fallen weg.
    """
    text = str(roh or "").strip()
    if not text:
        return ""
    if "//" in text:
        text = text.split("//", 1)[1]
    text = text.split("/", 1)[0].strip()
    if "@" in text:  # user:pass@host
        text = text.rsplit("@", 1)[1]
    # Port abtrennen — aber nicht in einer IPv6-Adresse (die trägt Doppelpunkte
    # und steht dann in eckigen Klammern).
    if text.startswith("["):
        text = text.split("]", 1)[0].lstrip("[")
    elif text.count(":") == 1:
        text = text.split(":", 1)[0]
    return text


def heizstab_host(config: dict) -> str:
    """Konfigurierte Adresse des Ohmpilot, normalisiert."""
    return normalisiere_host(config.get(CONF_HEIZSTAB_HOST))


def heizstab_enabled(config: dict) -> bool:
    return bool(config.get(CONF_HEIZSTAB_ENABLED, DEFAULT_HEIZSTAB_ENABLED))


def heizstab_max_kw(config: dict) -> float:
    """Maximale Heizstab-Leistung in kW; 0, wenn der Heizstab aus ist.

    Ein leeres Panel-Zahlenfeld kommt als 0 an und heißt „nicht gesetzt":
    dann gilt die Vorgabe, denn ein Heizstab mit 0 kW wäre keiner.
    """
    if not heizstab_enabled(config):
        return 0.0
    try:
        wert = float(config.get(CONF_HEIZSTAB_MAX_KW) or 0.0)
    except (TypeError, ValueError):
        wert = 0.0
    return wert if wert > 0 else DEFAULT_HEIZSTAB_MAX_KW


def heizstab_waermewert(config: dict) -> float:
    """Wert einer Kilowattstunde Wärme in EUR/kWh; 0 = unbewertet."""
    if not heizstab_enabled(config):
        return 0.0
    try:
        return max(0.0, float(config.get(CONF_HEIZSTAB_WAERMEWERT) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def naechster_sollwert(
    alt_kw: float,
    export_kw: float | None,
    grenze_kw: float,
    max_kw: float,
    vorrang_frei: bool,
) -> tuple[float, str]:
    """Die Überschuss-Regel als reine Funktion (siehe Modul-Docstring).

    ``export_kw`` positiv = Einspeisung, negativ = Bezug, None = kein
    Messwert. Liefert den neuen Sollwert in kW und die Begründung.
    """
    if export_kw is None:
        return 0.0, "kein Netz-Messwert"
    if max_kw <= 0:
        return 0.0, "keine Leistung konfiguriert"
    if export_kw >= grenze_kw - GUARD_EXPORT_STICKY_BAND_KW:
        if not vorrang_frei:
            return alt_kw, "Einspeisung an der Grenze — Batterie hat Vorrang"
        if alt_kw >= max_kw - HEIZSTAB_SATT_TOLERANZ_KW:
            return max_kw, "Einspeisung an der Grenze — Heizstab am Maximum"
        neu = min(max_kw, alt_kw + HEIZSTAB_STEP_KW)
        return round(neu, 3), "Einspeisung an der Grenze — Heizstab angehoben"
    if export_kw < grenze_kw - GUARD_EXPORT_RELEASE_KW:
        luecke = grenze_kw - export_kw
        neu = max(0.0, alt_kw - luecke)
        if neu <= 0.0:
            grund = "Netzbezug — Heizstab aus" if export_kw < 0 else "Einspeisung unter der Grenze — Heizstab aus"
            return 0.0, grund
        return round(neu, 3), "Einspeisung unter der Grenze — Heizstab zurückgenommen"
    return alt_kw, "totes Band — Heizstab bleibt"


class HeizstabController:
    """Hält den Sollwert, die Temperatur-Hysteresen und schreibt an den Treiber.

    ``treiber`` ist ein ``OhmpilotModbus`` (oder None, dann gibt es nichts
    zu schreiben — der Controller rechnet trotzdem, für Tests und Anzeige).
    Die Messwerte (Ist-Leistung, Temperatur) kommen aus dem Cache des
    Treibers, den ``async_lesen`` alle 10 s füllt.
    """

    def __init__(self, hass: Any, config: dict, treiber: Any = None) -> None:
        self._hass = hass
        self._config = config
        self._treiber = treiber

        self.sollwert_kw = 0.0
        self.grund = "noch kein Lauf"
        # Hysteresen
        self._max_gesperrt = False
        self._komfort_aktiv = False
        # Schreibpfad
        self.last_write_ok: bool | None = None
        self.write_failures = 0
        self._letzter_schreibversuch: float | None = None
        # Wer bei neuen Messwerten Bescheid haben will (Sensoren, Push-Modell).
        self._listener: list[Callable[[], None]] = []

    # ------------------------------------------------------------------
    # Konfiguration
    # ------------------------------------------------------------------
    def update_config(self, config: dict) -> None:
        """Hot-Reload: Maximaltemperatur, Mindesttemperatur, Vorrang, Wärmewert.

        Host, Port und Maximalleistung brauchen einen vollen Reload (der
        Treiber wird dann neu gebaut) — das regelt ``_requires_full_reload``.
        """
        self._config = config

    @property
    def enabled(self) -> bool:
        return heizstab_enabled(self._config)

    @property
    def max_kw(self) -> float:
        return heizstab_max_kw(self._config)

    @property
    def maxtemp_c(self) -> float:
        try:
            wert = float(self._config.get(CONF_HEIZSTAB_MAXTEMP_C) or 0.0)
        except (TypeError, ValueError):
            wert = 0.0
        return wert if wert > 0 else DEFAULT_HEIZSTAB_MAXTEMP_C

    @property
    def mintemp_c(self) -> float:
        """0 = keine Mindesttemperatur."""
        try:
            return max(0.0, float(self._config.get(CONF_HEIZSTAB_MINTEMP_C) or DEFAULT_HEIZSTAB_MINTEMP_C))
        except (TypeError, ValueError):
            return 0.0

    @property
    def alt_temp_c(self) -> float:
        """Bis wohin die andere Heizquelle den Puffer heizt; 0 = keine Unterscheidung."""
        try:
            return max(0.0, float(self._config.get(CONF_HEIZSTAB_ALT_TEMP_C) or DEFAULT_HEIZSTAB_ALT_TEMP_C))
        except (TypeError, ValueError):
            return 0.0

    @property
    def zusatzwaerme(self) -> bool:
        """Liegt der Puffer über der Temperatur der anderen Heizquelle?

        Dann ist jede weitere Kilowattstunde Zusatzwärme: die andere Quelle
        hätte sie nie geliefert, sie ersetzt nichts und zählt nicht zum
        Wärmewert. Ohne Schwelle oder ohne Fühlerwert False.
        """
        schwelle = self.alt_temp_c
        temp = self.temperatur_c
        return schwelle > 0 and temp is not None and temp > schwelle

    @property
    def netzbezug_erlaubt(self) -> bool:
        """Unter der Mindesttemperatur auch aus dem Netz heizen? Opt-in."""
        return bool(self._config.get(CONF_HEIZSTAB_NETZBEZUG, DEFAULT_HEIZSTAB_NETZBEZUG))

    @property
    def vorrang_heizstab(self) -> bool:
        """True = Überschuss zuerst in den Heizstab, dann in die Batterie."""
        wert = self._config.get(CONF_HEIZSTAB_VORRANG, DEFAULT_HEIZSTAB_VORRANG)
        return DEFAULT_HEIZSTAB_VORRANG if wert is None else bool(wert)

    @property
    def waermewert(self) -> float:
        return heizstab_waermewert(self._config)

    # ------------------------------------------------------------------
    # Messwerte (aus dem Treiber-Cache)
    # ------------------------------------------------------------------
    @property
    def leistung_kw(self) -> float | None:
        """Gemessene Leistungsaufnahme in kW; None ohne Messwert."""
        if self._treiber is None:
            return None
        watt = getattr(self._treiber, "last_power_w", None)
        if watt is None:
            return None
        try:
            return max(0.0, float(watt) / 1000.0)
        except (TypeError, ValueError):
            return None

    @property
    def temperatur_c(self) -> float | None:
        if self._treiber is None:
            return None
        wert = getattr(self._treiber, "last_temperature", None)
        if wert is None:
            return None
        try:
            wert = float(wert)
        except (TypeError, ValueError):
            return None
        # Der Ohmpilot meldet ohne Fühler 0,0 °C — das ist kein Messwert.
        return wert if wert > 0.0 else None

    @property
    def verfuegbar(self) -> bool:
        """Treiber vorhanden und zuletzt erreichbar."""
        if self._treiber is None:
            return False
        return bool(getattr(self._treiber, "connected", False)) or self.leistung_kw is not None

    # ------------------------------------------------------------------
    # Temperatur-Hysteresen
    # ------------------------------------------------------------------
    def pruefe_temperaturen(self) -> None:
        """Hysteresen fortschreiben — einmal je Guard-Lauf, VOR der Absicht.

        Der Executor braucht ``komfort_aktiv`` schon beim Übersetzen des
        Slots (eine Entladung wird dann unterdrückt), also läuft das hier
        getrennt vom Regeln.
        """
        temp = self.temperatur_c
        if temp is None:
            # Ohne Fühlerwert keine Aussage — der Ohmpilot hat seinen eigenen
            # Übertemperaturschutz, die Regel darf weiterlaufen. Komfort ohne
            # Messwert wäre Heizen ins Blaue.
            self._komfort_aktiv = False
            return
        maximum = self.maxtemp_c
        if temp >= maximum:
            self._max_gesperrt = True
        elif temp < maximum - HEIZSTAB_TEMP_HYSTERESE_K:
            self._max_gesperrt = False

        minimum = self.mintemp_c
        if minimum <= 0:
            self._komfort_aktiv = False
        elif temp < minimum:
            self._komfort_aktiv = True
        elif temp >= minimum + HEIZSTAB_MINTEMP_HYSTERESE_K:
            self._komfort_aktiv = False

    @property
    def komfort_aktiv(self) -> bool:
        """Unter der Mindesttemperatur (mit Hysterese)."""
        return self.enabled and self._komfort_aktiv

    @property
    def komfort_aus_netz(self) -> bool:
        """Unter der Mindesttemperatur UND Netzbezug erlaubt: volle Leistung,
        egal woher der Strom kommt — der Executor unterdrückt dann Entladungen."""
        return self.komfort_aktiv and self.netzbezug_erlaubt

    @property
    def max_erreicht(self) -> bool:
        return self._max_gesperrt

    @property
    def gesaettigt(self) -> bool:
        """Kann der Heizstab gerade nichts (mehr) aufnehmen?

        Dann darf Guard 1 das Ladelimit der Batterie anheben, obwohl der
        Heizstab Vorrang hat. Gesättigt heißt: nicht steuerbar, Ziel
        erreicht, oder Sollwert am Maximum.
        """
        if not self.enabled or not self.verfuegbar:
            return True
        if self._max_gesperrt:
            return True
        return self.sollwert_kw >= self.max_kw - HEIZSTAB_SATT_TOLERANZ_KW

    # ------------------------------------------------------------------
    # Regeln
    # ------------------------------------------------------------------
    def regeln(
        self,
        *,
        entladung: bool,
        export_kw: float | None,
        grenze_kw: float,
        vorrang_frei: bool,
    ) -> tuple[float, str]:
        """Nächsten Sollwert bestimmen (ohne zu schreiben).

        Reihenfolge: Komfort mit erlaubtem Netzbezug schlägt alles (der
        Executor unterdrückt dann die Entladung); dann die Entladung ins Netz
        (kein Überschuss — alles, was der Heizstab zöge, käme aus der
        Batterie); dann Komfort ohne Netzbezug (Vorrang vor der Einspeisung,
        Regel auf Einspeisung ≈ 0); dann die Maximaltemperatur; dann die
        Überschuss-Regel an der Einspeisegrenze. Modus Aus und Startphase
        entscheidet der Executor selbst — dort ist der Sollwert 0, ohne diese
        Funktion.
        """
        if not self.enabled:
            return 0.0, "Heizstab deaktiviert"
        temp_text = (
            f"({self.temperatur_c:.0f} °C < {self.mintemp_c:.0f} °C)"
            if self.temperatur_c is not None else ""
        )
        if self.komfort_aus_netz:
            return self.max_kw, (
                f"Mindesttemperatur unterschritten {temp_text} — Heizstab auf "
                "volle Leistung, auch aus dem Netz"
            )
        if entladung:
            return 0.0, "Entladung ins Netz — kein Überschuss für den Heizstab"
        if self.komfort_aktiv:
            # Komfort ohne Netzbezug: die ganze Einspeisung nehmen, aber
            # keinen Netzstrom — geregelt wird auf Einspeisung ≈ 0 statt auf
            # die Einspeisegrenze. Die Maximaltemperatur ist hier ohne Belang,
            # sie liegt über der Mindesttemperatur.
            soll, grund = naechster_sollwert(
                self.sollwert_kw, export_kw, HEIZSTAB_KOMFORT_EXPORT_ZIEL_KW,
                self.max_kw, True,
            )
            return soll, f"Mindesttemperatur unterschritten {temp_text} — Vorrang vor der Einspeisung: {grund}"
        if self._max_gesperrt:
            return 0.0, f"Maximaltemperatur erreicht ({self.maxtemp_c:.0f} °C)"
        return naechster_sollwert(
            self.sollwert_kw, export_kw, grenze_kw, self.max_kw, vorrang_frei
        )

    # ------------------------------------------------------------------
    # Schreiben / Lesen (Treiber)
    # ------------------------------------------------------------------
    async def async_set_sollwert(self, kw: float, grund: str) -> None:
        """Sollwert übernehmen; bei relevanter Änderung sofort schreiben.

        Unabhängig davon schreibt der 30-s-Takt (``async_schreiben``) den
        aktuellen Wert immer wieder — das ist der Watchdog des Ohmpilot.
        """
        kw = max(0.0, float(kw))
        geaendert = abs(kw - self.sollwert_kw) >= _SOFORT_SCHREIBEN_AB_KW
        self.sollwert_kw = kw
        self.grund = grund
        if geaendert:
            _LOGGER.debug("Heizstab: Sollwert %.2f kW (%s)", kw, grund)
            await self.async_schreiben()

    async def async_schreiben(self) -> bool:
        """Aktuellen Sollwert an den Treiber schreiben (Watchdog-Takt)."""
        if self._treiber is None:
            return False
        self._letzter_schreibversuch = time.time()
        try:
            ok = bool(await self._treiber.async_set_power(int(round(self.sollwert_kw * 1000))))
        except Exception:  # noqa: BLE001 — der Takt darf nie sterben
            _LOGGER.exception("Heizstab: Schreiben fehlgeschlagen")
            ok = False
        self.last_write_ok = ok
        if not ok:
            self.write_failures += 1
        return ok

    async def async_lesen(self) -> None:
        """Ist-Leistung und Temperatur aus dem Gerät holen, Sensoren anstoßen."""
        if self._treiber is None:
            return
        try:
            await self._treiber.async_read_sensors()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Heizstab: Lesen fehlgeschlagen")
        for melden in list(self._listener):
            try:
                melden()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Heizstab: Listener fehlgeschlagen")

    async def async_zeit_sync(self) -> None:
        if self._treiber is None:
            return
        try:
            await self._treiber.async_sync_time()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Heizstab: Zeitsynchronisation fehlgeschlagen")

    async def async_shutdown(self) -> None:
        """Beim Entladen der Integration: 0 W schreiben, Verbindung schließen.

        Ohne das liefe der Heizstab noch bis zum Watchdog (50 s) weiter.
        """
        self.sollwert_kw = 0.0
        self.grund = "Integration gestoppt"
        if self._treiber is None:
            return
        try:
            await self._treiber.async_set_power(0)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Heizstab: 0 W beim Stopp nicht geschrieben", exc_info=True)
        try:
            await self._treiber.async_close()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Heizstab: Verbindung nicht sauber geschlossen", exc_info=True)

    def add_listener(self, melden: Callable[[], None]) -> Callable[[], None]:
        self._listener.append(melden)

        def entfernen() -> None:
            if melden in self._listener:
                self._listener.remove(melden)

        return entfernen

    # ------------------------------------------------------------------
    # Zustand für Panel, Statussensor und Steuerwerte-Ansicht
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        temp = self.temperatur_c
        return {
            "enabled": self.enabled,
            "verfuegbar": self.verfuegbar,
            "host": heizstab_host(self._config),
            "port": self._config.get(CONF_HEIZSTAB_PORT, DEFAULT_HEIZSTAB_PORT),
            "sollwert_kw": round(self.sollwert_kw, 3),
            "leistung_kw": None if self.leistung_kw is None else round(self.leistung_kw, 3),
            "temperatur_c": None if temp is None else round(temp, 1),
            "max_kw": self.max_kw,
            "maxtemp_c": self.maxtemp_c,
            "mintemp_c": self.mintemp_c,
            "vorrang_heizstab": self.vorrang_heizstab,
            "netzbezug_erlaubt": self.netzbezug_erlaubt,
            "alt_temp_c": self.alt_temp_c or None,
            "zusatzwaerme": self.zusatzwaerme,
            "waermewert": self.waermewert,
            "komfort_aktiv": self.komfort_aktiv,
            "komfort_aus_netz": self.komfort_aus_netz,
            "max_erreicht": self.max_erreicht,
            "gesaettigt": self.gesaettigt,
            "grund": self.grund,
            "last_write_ok": self.last_write_ok,
            "write_failures": self.write_failures,
            "last_error": None if self._treiber is None else getattr(self._treiber, "last_error", None),
        }


def create_heizstab(hass: Any, config: dict) -> HeizstabController | None:
    """Controller samt Treiber bauen — None, wenn der Heizstab aus ist.

    Der Treiber braucht pymodbus; fehlt es, gibt es den Controller ohne
    Treiber (rechnet und zeigt an, schreibt nichts) und eine Warnung.
    """
    if not heizstab_enabled(config):
        return None
    host = heizstab_host(config)
    if not host:
        _LOGGER.warning("Heizstab aktiviert, aber kein Host konfiguriert — es wird nicht gesteuert")
        return HeizstabController(hass, config, None)
    try:
        port = int(config.get(CONF_HEIZSTAB_PORT) or DEFAULT_HEIZSTAB_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_HEIZSTAB_PORT
    try:
        from .ohmpilot_modbus import OhmpilotModbus
    except ImportError:  # pragma: no cover — pymodbus fehlt
        _LOGGER.warning("pymodbus nicht verfügbar — Heizstab wird nicht gesteuert")
        return HeizstabController(hass, config, None)
    treiber = OhmpilotModbus(host, port, int(round(heizstab_max_kw(config) * 1000)))
    return HeizstabController(hass, config, treiber)
