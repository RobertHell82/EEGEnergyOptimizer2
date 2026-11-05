"""Zustand des angesteckten Fahrzeugs — gelesen über die Ambibox.

Der Controller pollt den EV-Charger-Block (``modbus.py``), deutet die
Rohregister und hält das Ergebnis als :class:`AutoZustand`. Sensoren und
Panel lesen daraus.

Geschrieben wird ausschließlich im manuellen Test (``async_manuell_start``),
den jemand im Panel auslöst. Der Fahrplan rührt die Wallbox nicht an. Drei
Sicherungen hängen an dem Weg, weil das Verhalten des Geräts unbekannt ist:
der Sollwert wird zyklisch nachgeschrieben (unbekannter Watchdog), er endet
nach einer festen Zeit von selbst, und beim Entladen der Integration wird
gestoppt.

Zur Richtung des Leistungsflusses: Das Herstellerdokument legt die
Vorzeichenkonvention von ``Power AC`` nicht fest, und die einzige fremde
Umsetzung (evcc) behandelt sie beim Sollwert genau andersherum als beim
Messwert. Deshalb kommt die Richtung hier ausschließlich aus ``Battery
State`` (Offset 70, dokumentiert als SLEEP/IDLE/CHARGE/DISCHARGE) und die
Leistung wird als Betrag geführt. So stimmt die Anzeige unabhängig davon,
wie das Gerät das Vorzeichen setzt.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..const import (
    AMBIBOX_MANUAL_MAX_MINUTES,
    AMBIBOX_SIGN_NEGATIVE,
    AMBIBOX_SIGN_POSITIVE,
    CONF_AMBIBOX_CHARGE_SIGN,
    CONF_AMBIBOX_CONNECTOR,
    CONF_AMBIBOX_HOST,
    CONF_AMBIBOX_PORT,
    CONF_AMBIBOX_UNIT_ID,
    CONF_WALLBOX_TYPE,
    DEFAULT_AMBIBOX_CHARGE_SIGN,
    DEFAULT_AMBIBOX_CONNECTOR,
    DEFAULT_AMBIBOX_PORT,
    DEFAULT_AMBIBOX_UNIT_ID,
    WALLBOX_TYPE_AMBIBOX,
)
from . import modbus as mb

_LOGGER = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Aufzählungen aus dem Herstellerdokument
# --------------------------------------------------------------------------
SESSION_STATES = {
    0: "Sitzung wird aufgebaut",
    1: "Autorisierung",
    2: "Ladeparameter werden ausgehandelt",
    3: "Kabelprüfung",
    4: "Vorladen",
    5: "Lädt",
    6: "Ladeende",
    7: "Pausiert",
    8: "Gestoppt",
    9: "Fehler",
}
SESSION_STATE_CHARGE_LOOP = 5

BATTERY_STATES = {0: "Ruhezustand", 1: "Bereit", 2: "Lädt", 3: "Entlädt"}
BATTERY_STATE_CHARGE = 2
BATTERY_STATE_DISCHARGE = 3
SESSION_STATE_LAEDT = 5
# Ab diesem Betrag zählt Power AC als Energiefluss; darunter liegt der
# Eigenbedarf der Box (~170 W).
AMBIBOX_RICHTUNG_SCHWELLE_W = 300

# Obergrenze des Handtests, wenn die Wallbox ihren Stellbereich (Max
# Charge/Discharge Power) nicht meldet: 11 kW, die Nennleistung der Box.
# Das Panel begrenzt zwar schon, aber der WebSocket-Befehl nimmt jede Zahl.
AMBIBOX_MANUAL_FALLBACK_MAX_KW = 11.0
# So lange nach dem Start darf der Battery State noch in die Gegenrichtung
# zeigen (Anlauf, ein Lesetakt Verzug). Danach heißt Gegenrichtung: die Box
# versteht das Vorzeichen anders als eingestellt — dann sofort abbrechen,
# statt den Test in die falsche Richtung zu Ende laufen zu lassen.
AMBIBOX_RICHTUNG_ANLAUF_S = 60.0

CONTROL_MODES = {
    0: "Nicht steuerbar",
    1: "Voll steuerbar",
    2: "Nur begrenzbar",
}
CONTROL_MODE_CONTROLLABLE = 1

CHARGE_PROTOCOLS = {
    0: "Sonstiges",
    1: "CHAdeMO",
    2: "DIN 70121",
    3: "ISO 15118-2",
    4: "ISO 15118-2 VAS",
    5: "ISO 15118-20",
}
# Rückspeisen ins Haus oder Netz ist in ISO 15118-20 genormt. Es ist aber
# nicht die einzige Spur: Manche Fahrzeuge speisen über ISO 15118-2 mit
# Herstellererweiterungen zurück — siehe ``_rueckspeisen_moeglich``. DIN 70121
# kann nur laden.
CHARGE_PROTOCOL_BIDIREKTIONAL = 5
CHARGE_PROTOCOL_DIN_70121 = 2
# VW-Konzern (z. B. Skoda Enyaq 2021): Rückspeisen über ISO 15118-2 mit der
# herstellereigenen Erweiterung VAS — älter als ISO 15118-20.
CHARGE_PROTOCOL_VAS = 4

EVCHARGER_ERROR_BITS = {
    0: "Kommunikation mit dem Fahrzeug",
    1: "Kommunikation mit der Ladeelektronik",
    2: "Lastabwurf",
    3: "Steckertemperatur",
    4: "Kabelprüfung",
    5: "Not-Aus",
}

BATTERY_ERROR_BITS = {
    0: "Spannung zu niedrig",
    1: "Spannung zu hoch",
    2: "Ladestrom zu hoch",
    3: "Entladestrom zu hoch",
    4: "Temperatur zu niedrig",
    5: "Temperatur zu hoch",
    6: "Zellspannung zu niedrig",
    7: "Zellspannung zu hoch",
    8: "Modulspannung zu niedrig",
    9: "Modulspannung zu hoch",
    10: "BMS-Temperatur zu hoch",
    11: "Relais",
    12: "Vorladung",
}


def _flags(wert: int | None, namen: dict[int, str]) -> list[str]:
    """Ein Flagset in Klartext auflösen (mehrere Fehler in einer Zahl)."""
    if not wert:
        return []
    treffer = [text for bit, text in namen.items() if wert & (1 << bit)]
    # Bits ohne Namen nicht verschlucken — sonst meldet die Karte „kein
    # Fehler", während das Gerät einen hat.
    bekannt = 0
    for bit in namen:
        bekannt |= 1 << bit
    if wert & ~bekannt:
        treffer.append(f"Unbekannter Fehlercode {wert}")
    return treffer


def _wh_to_kwh(wert: float | int | None) -> float | None:
    return None if wert is None else round(float(wert) / 1000.0, 3)


def normalisiere_host(roh: Any) -> str:
    """Aus einer Eingabe die reine Modbus-Adresse machen.

    Die Ambibox hat ein Webinterface, also liegt es nahe, dessen URL
    einzutragen. pymodbus braucht Host oder IP allein — mit der URL scheitert
    jeder Verbindungsversuch, und die Meldung liest sich wie ein
    Netzwerkproblem. Deshalb wird geschält statt abgelehnt.
    """
    text = str(roh or "").strip()
    if not text:
        return ""
    if "//" in text:
        text = text.split("//", 1)[1]
    text = text.split("/", 1)[0].strip()
    if "@" in text:  # user:pass@host
        text = text.rsplit("@", 1)[1]
    if text.startswith("["):  # IPv6 in eckigen Klammern
        text = text.split("]", 1)[0].lstrip("[")
    elif text.count(":") == 1:
        text = text.split(":", 1)[0]
    return text


def ambibox_enabled(config: dict) -> bool:
    """True, wenn als Wallbox eine Ambibox eingestellt ist."""
    return str(config.get(CONF_WALLBOX_TYPE) or "").strip().lower() == WALLBOX_TYPE_AMBIBOX


def ambibox_host(config: dict) -> str:
    return normalisiere_host(config.get(CONF_AMBIBOX_HOST))


def ambibox_port(config: dict) -> int:
    try:
        return int(config.get(CONF_AMBIBOX_PORT) or DEFAULT_AMBIBOX_PORT)
    except (TypeError, ValueError):
        return DEFAULT_AMBIBOX_PORT


def ambibox_unit_id(config: dict) -> int:
    try:
        return int(config.get(CONF_AMBIBOX_UNIT_ID) or DEFAULT_AMBIBOX_UNIT_ID)
    except (TypeError, ValueError):
        return DEFAULT_AMBIBOX_UNIT_ID


def ambibox_connector(config: dict) -> int:
    try:
        wert = int(config.get(CONF_AMBIBOX_CONNECTOR) or DEFAULT_AMBIBOX_CONNECTOR)
    except (TypeError, ValueError):
        return DEFAULT_AMBIBOX_CONNECTOR
    return wert if 1 <= wert <= mb.MAX_CONNECTOR else DEFAULT_AMBIBOX_CONNECTOR


def ambibox_charge_sign(config: dict) -> str:
    """Welches Vorzeichen des Sollwerts die Wallbox als Laden versteht."""
    wert = str(config.get(CONF_AMBIBOX_CHARGE_SIGN) or DEFAULT_AMBIBOX_CHARGE_SIGN).strip().lower()
    return wert if wert in (AMBIBOX_SIGN_NEGATIVE, AMBIBOX_SIGN_POSITIVE) else DEFAULT_AMBIBOX_CHARGE_SIGN


@dataclass
class AutoZustand:
    """Was die Ambibox über das angesteckte Fahrzeug weiß.

    Alle Felder dürfen None sein: Je nach Ladeprotokoll und Ladephase
    liefert das Fahrzeug nur einen Teil der Werte, und vor der ersten
    erfolgreichen Abfrage ist gar nichts bekannt.
    """

    verbunden: bool = False
    # Ladesitzung
    session_state: int | None = None
    session_text: str = "Unbekannt"
    charge_protocol: int | None = None
    protokoll_text: str | None = None
    bidirektional_faehig: bool = False
    replug_required: bool = False
    # Fahrzeugbatterie
    soc_pct: float | None = None
    kapazitaet_kwh: float | None = None
    energie_kwh: float | None = None
    soh_pct: float | None = None
    min_soc_pct: float | None = None
    max_soc_pct: float | None = None
    temperatur_c: float | None = None
    zyklen: int | None = None
    zeit_bis_voll_s: int | None = None
    # Leistung — Betrag, Richtung getrennt (siehe Modul-Docstring)
    leistung_kw: float | None = None
    richtung: str = "aus"  # "laden" | "entladen" | "aus"
    battery_state: int | None = None
    battery_text: str | None = None
    # Stellbereich (für Schritt 2)
    min_charge_kw: float | None = None
    max_charge_kw: float | None = None
    min_discharge_kw: float | None = None
    max_discharge_kw: float | None = None
    control_mode: int | None = None
    control_mode_text: str | None = None
    steuerbar: bool = False
    # Abfahrtsplanung
    abfahrt_in_s: int | None = None
    abfahrt_soc_pct: float | None = None
    abfahrt_energie_kwh: float | None = None
    # Energie dieser Ladesitzung
    session_geladen_kwh: float | None = None
    session_entladen_kwh: float | None = None
    # Störungen
    fehler: list[str] = field(default_factory=list)


class AmbiboxController:
    """Hält den zuletzt gelesenen Fahrzeugzustand und meldet Änderungen.

    Push-Modell wie beim Heizstab: Nach jedem Lesevorgang werden die
    registrierten Listener gerufen, damit die Sensoren nicht bis zum
    nächsten Minutentakt auf veralteten Werten stehen.
    """

    def __init__(self, hass: Any, config: dict, treiber: Any = None) -> None:
        self._hass = hass
        self._config = dict(config or {})
        self._treiber = treiber
        self._zustand = AutoZustand()
        self._listener: list[Callable[[], None]] = []
        self._gelesen = False
        # Rohregister des letzten Lesevorgangs (Beta-Diagnose, siehe
        # ``rohregister``).
        self._roh_eingang: list[int] | None = None
        self._roh_holding: list[int] | None = None
        # Manueller Test: Richtung ("laden"/"entladen"), Sollwert in kW und
        # der Zeitpunkt, zu dem von selbst gestoppt wird.
        self._manuell_richtung: str | None = None
        self._manuell_kw: float = 0.0
        self._manuell_bis: float | None = None
        self._manuell_seit: float | None = None
        self._manuell_fehler: str | None = None
        self._letzter_sollwert_w: int | None = None

    # -- Konfiguration ----------------------------------------------------

    def update_config(self, config: dict) -> None:
        """Konfiguration übernehmen, die keinen neuen Treiber braucht.

        Host, Port, Unit-ID und Anschluss lösen einen vollen Reload aus
        (``_RELOAD_CONFIG_KEYS``) — hier kommt nur an, was sich im Betrieb
        ändern darf.
        """
        self._config = dict(config or {})

    @property
    def enabled(self) -> bool:
        return ambibox_enabled(self._config)

    @property
    def host(self) -> str:
        return ambibox_host(self._config)

    @property
    def connector(self) -> int:
        return ambibox_connector(self._config)

    @property
    def verfuegbar(self) -> bool:
        """True, wenn zuletzt erfolgreich gelesen wurde."""
        return bool(self._treiber is not None and self._gelesen
                    and getattr(self._treiber, "read_failures", 0) == 0)

    @property
    def zustand(self) -> AutoZustand:
        return self._zustand

    @property
    def last_error(self) -> str | None:
        return None if self._treiber is None else getattr(self._treiber, "last_error", None)

    # -- Lesen ------------------------------------------------------------

    async def async_lesen(self) -> None:
        """Einen Lesevorgang ausführen und den Zustand nachführen."""
        if self._treiber is None:
            return
        regs = await self._treiber.async_read_block()
        if regs is None:
            # Verbindung weg: den letzten Zustand nicht als aktuell stehen
            # lassen — sonst zeigt die Karte stundenlang ein Auto an, das
            # längst weggefahren ist.
            if self._gelesen:
                self._gelesen = False
                self._melden()
            return
        self._zustand = self._deuten(regs)
        self._roh_eingang = regs
        lesen_holding = getattr(self._treiber, "async_read_holding", None)
        self._roh_holding = await lesen_holding() if lesen_holding else None
        self._gelesen = True
        self._melden()

    def _deuten(self, regs: list[int]) -> AutoZustand:
        """Rohregister in den Fahrzeugzustand übersetzen."""
        z = AutoZustand()

        z.verbunden = bool(mb.read_bool(regs, mb.OFF_EV_CONNECTED))
        z.replug_required = bool(mb.read_bool(regs, mb.OFF_REPLUG_REQUIRED))

        session = mb.read_u32(regs, mb.OFF_SESSION_STATE)
        z.session_state = session
        z.session_text = SESSION_STATES.get(
            session, "Unbekannt" if session is None else f"Zustand {session}"
        )
        if not z.verbunden:
            z.session_text = "Kein Fahrzeug angesteckt"

        protokoll = mb.read_u32(regs, mb.OFF_CHARGE_PROTOCOL)
        z.charge_protocol = protokoll
        z.protokoll_text = CHARGE_PROTOCOLS.get(protokoll) if protokoll is not None else None

        # Fahrzeugbatterie
        z.soc_pct = _runden(mb.read_f32(regs, mb.OFF_SOC), 1)
        z.kapazitaet_kwh = _wh_to_kwh(mb.read_f32(regs, mb.OFF_CAPACITY))
        if z.soc_pct is not None and z.kapazitaet_kwh:
            z.energie_kwh = round(z.kapazitaet_kwh * z.soc_pct / 100.0, 2)
        z.soh_pct = _runden(mb.read_f32(regs, mb.OFF_SOH), 1)
        z.min_soc_pct = _runden(mb.read_f32(regs, mb.OFF_MIN_SOC), 1)
        z.max_soc_pct = _runden(mb.read_f32(regs, mb.OFF_MAX_SOC), 1)
        z.temperatur_c = _runden(mb.read_f32(regs, mb.OFF_BATTERY_TEMP), 1)
        z.zyklen = mb.read_u32(regs, mb.OFF_CYCLES)
        z.zeit_bis_voll_s = mb.read_u32(regs, mb.OFF_TIME_TO_FULL)

        # Leistung als Betrag. Die Richtung kommt aus dem Batteriezustand,
        # wenn das Fahrzeug ihn meldet, sonst aus dem Vorzeichen von Power AC
        # (positiv = Rückspeisen). Ansfelden, 05.11.2026: Der Skoda meldete
        # durchgehend Battery State 0, Power AC stand beim Rückspeisen auf
        # +4970 W, im Leerlauf auf −170 W (Eigenbedarf der Box).
        leistung_w = mb.read_i32(regs, mb.OFF_POWER_AC)
        z.leistung_kw = None if leistung_w is None else round(abs(leistung_w) / 1000.0, 3)
        battery_state = mb.read_u32(regs, mb.OFF_BATTERY_STATE)
        z.battery_state = battery_state
        z.battery_text = BATTERY_STATES.get(battery_state) if battery_state is not None else None
        if battery_state == BATTERY_STATE_CHARGE:
            z.richtung = "laden"
        elif battery_state == BATTERY_STATE_DISCHARGE:
            z.richtung = "entladen"
        elif battery_state in (None, 0) and leistung_w is not None and (
            abs(leistung_w) >= AMBIBOX_RICHTUNG_SCHWELLE_W
        ):
            z.richtung = "entladen" if leistung_w > 0 else "laden"
        else:
            z.richtung = "aus"
            # Ohne Ladevorgang ist eine Restleistung Messrauschen der
            # Ladeelektronik, kein Energiefluss ins Auto.
            if z.leistung_kw is not None and z.leistung_kw < AMBIBOX_RICHTUNG_SCHWELLE_W / 1000:
                z.leistung_kw = 0.0

        # „Lädt" ist der Sitzungszustand der Box (die Ladeschleife läuft),
        # in beide Richtungen.
        if z.richtung == "entladen" and session == SESSION_STATE_LAEDT:
            z.session_text = "Entlädt"

        # Stellbereich — für Schritt 2, aber schon jetzt aussagekräftig:
        # daran sieht man, ob die Box das Fahrzeug überhaupt regeln darf.
        z.min_charge_kw = _w_to_kw(mb.read_u32(regs, mb.OFF_MIN_CHARGE_POWER))
        z.max_charge_kw = _w_to_kw(mb.read_u32(regs, mb.OFF_MAX_CHARGE_POWER))
        z.min_discharge_kw = _w_to_kw(mb.read_u32(regs, mb.OFF_MIN_DISCHARGE_POWER))
        z.max_discharge_kw = _w_to_kw(mb.read_u32(regs, mb.OFF_MAX_DISCHARGE_POWER))
        z.bidirektional_faehig = _rueckspeisen_moeglich(protokoll, z.max_discharge_kw)
        control_mode = mb.read_u32(regs, mb.OFF_CONTROL_MODE)
        z.control_mode = control_mode
        z.control_mode_text = (
            CONTROL_MODES.get(control_mode) if control_mode is not None else None
        )
        z.steuerbar = control_mode == CONTROL_MODE_CONTROLLABLE

        # Abfahrtsplanung
        z.abfahrt_in_s = mb.read_u32(regs, mb.OFF_SECONDS_TO_DEPARTURE)
        z.abfahrt_soc_pct = _runden(mb.read_f32(regs, mb.OFF_DEPARTURE_SOC), 1)
        z.abfahrt_energie_kwh = _wh_to_kwh(mb.read_u32(regs, mb.OFF_DEPARTURE_ENERGY))

        # Energie dieser Sitzung
        z.session_geladen_kwh = _wh_to_kwh(mb.read_f32(regs, mb.OFF_SESSION_IMPORT))
        z.session_entladen_kwh = _wh_to_kwh(mb.read_f32(regs, mb.OFF_SESSION_EXPORT))

        # Störungen aus beiden Flagsets zusammen — für die Anzeige ist es
        # eine Liste, die Herkunft interessiert erst bei der Fehlersuche.
        z.fehler = (
            _flags(mb.read_u32(regs, mb.OFF_EVCHARGER_ERROR), EVCHARGER_ERROR_BITS)
            + _flags(mb.read_u32(regs, mb.OFF_BATTERY_ERROR), BATTERY_ERROR_BITS)
        )
        return z

    # -- Manueller Lade-/Entladetest --------------------------------------
    #
    # Der Fahrplan steuert die Wallbox nicht. Dieser Weg existiert, um am
    # Gerät zu klären, was das Herstellerdokument offenlässt: mit welchem
    # Vorzeichen geladen wird, wie lange ein Sollwert ohne Nachschreiben
    # gilt, und ob sidOS dazwischenfunkt.

    @property
    def manuell_aktiv(self) -> bool:
        return self._manuell_richtung is not None

    @property
    def manuell_restsekunden(self) -> int | None:
        if self._manuell_bis is None:
            return None
        return max(0, int(self._manuell_bis - time.time()))

    def _sollwert_watt(self, richtung: str, kw: float) -> int:
        """Sollwert mit dem Vorzeichen, das die Wallbox als Richtung versteht."""
        betrag = int(round(abs(kw) * 1000))
        laden_negativ = ambibox_charge_sign(self._config) == AMBIBOX_SIGN_NEGATIVE
        if richtung == "laden":
            return -betrag if laden_negativ else betrag
        return betrag if laden_negativ else -betrag

    async def async_manuell_start(
        self, richtung: str, kw: float, minuten: int
    ) -> tuple[bool, str]:
        """Laden oder Entladen von Hand starten.

        Gibt (Erfolg, Klartext) zurück — der Text landet im Panel, damit ein
        abgelehnter Versuch seinen Grund nennt statt nur zu scheitern.
        """
        if richtung not in ("laden", "entladen"):
            return False, f"Unbekannte Richtung: {richtung}"
        if self._treiber is None:
            return False, "Keine Verbindung zur Wallbox konfiguriert."
        try:
            kw = float(kw)
        except (TypeError, ValueError):
            return False, "Die Leistung muss eine Zahl sein."
        if not math.isfinite(kw) or kw <= 0:
            return False, "Die Leistung muss größer als 0 kW sein."
        # Serverseitig begrenzen: auf den Stellbereich, den die Wallbox für
        # diese Richtung meldet, sonst auf die Nennleistung. Ablehnen statt
        # stillschweigend kappen — bei einem Test soll klar sein, was lief.
        gemeldet = (
            self._zustand.max_charge_kw if richtung == "laden"
            else self._zustand.max_discharge_kw
        )
        obergrenze = (
            gemeldet if gemeldet is not None and gemeldet > 0
            else AMBIBOX_MANUAL_FALLBACK_MAX_KW
        )
        if kw > obergrenze + 1e-9:
            return False, (
                f"Höchstens {obergrenze:.1f} kW — "
                + ("so viel meldet die Wallbox für diese Richtung."
                   if gemeldet is not None and gemeldet > 0
                   else "die Wallbox meldet keinen Stellbereich.")
            )
        # Ohne angestecktes Fahrzeug gibt es nichts zu steuern. Das ist keine
        # Förmlichkeit: Ein Sollwert ins Leere ließe sich nicht auswerten,
        # und genau die Auswertung ist der Zweck dieses Wegs.
        if not self._zustand.verbunden:
            return False, "Kein Fahrzeug angesteckt."
        if self._zustand.control_mode == 0:
            return False, "Die Wallbox meldet das Fahrzeug als nicht steuerbar."
        if richtung == "entladen" and not self._zustand.bidirektional_faehig:
            protokoll = self._zustand.protokoll_text or "unbekannt"
            return False, (
                "Rückspeisen setzt ISO 15118-20 oder eine vom Fahrzeug gemeldete "
                f"Entladeleistung voraus — das Fahrzeug lädt über {protokoll} "
                "und meldet keine."
            )

        minuten = max(1, min(int(minuten or 1), AMBIBOX_MANUAL_MAX_MINUTES))
        watt = self._sollwert_watt(richtung, kw)

        # Aufwecken vor dem ersten Sollwert: Schläft die Box, ginge der Wert
        # sonst ins Leere. Ein Fehlschlag ist kein Abbruchgrund — nicht jedes
        # Gerät braucht den Weckruf.
        try:
            await self._treiber.async_wake_up()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Ambibox: Weckruf fehlgeschlagen", exc_info=True)

        if not await self._treiber.async_write_target_power(watt):
            self._manuell_fehler = self._treiber.last_error or "Schreiben fehlgeschlagen"
            self._melden()
            return False, f"Sollwert nicht angekommen: {self._manuell_fehler}"

        self._manuell_richtung = richtung
        self._manuell_kw = abs(float(kw))
        self._manuell_bis = time.time() + minuten * 60
        self._manuell_seit = time.time()
        self._manuell_fehler = None
        self._letzter_sollwert_w = watt
        _LOGGER.info(
            "Ambibox: manueller Test %s mit %.2f kW (Sollwert %d W) für %d min",
            richtung, self._manuell_kw, watt, minuten,
        )
        self._melden()
        return True, f"{richtung.capitalize()} mit {self._manuell_kw:.1f} kW für {minuten} min gestartet."

    async def async_manuell_stopp(self, grund: str = "Von Hand beendet") -> bool:
        """Den manuellen Test beenden und die Wallbox freigeben."""
        war_aktiv = self.manuell_aktiv
        self._manuell_richtung = None
        self._manuell_kw = 0.0
        self._manuell_bis = None
        self._manuell_seit = None
        if self._treiber is None:
            return False
        # Erst 0 W, dann Stop Charge: Welcher der beiden Wege die Box
        # tatsächlich freigibt, ist nicht dokumentiert — beide zu gehen ist
        # billiger als ein Fahrzeug, das weiterlädt.
        ok = await self._treiber.async_write_target_power(0)
        try:
            await self._treiber.async_stop_charge()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Ambibox: Stop Charge fehlgeschlagen", exc_info=True)
        self._letzter_sollwert_w = 0
        if war_aktiv:
            _LOGGER.info("Ambibox: manueller Test beendet (%s)", grund)
        self._melden()
        return ok

    async def async_keepalive(self) -> None:
        """Den laufenden Test überwachen: Zeit, Fahrzeug, Richtung.

        Der Sollwert wird NICHT nachgeschrieben (Beta-Versuch): Beim
        Nachschreiben alle 30 s setzte das Auto in Ansfelden (05.11.2026)
        jede Minute 10–15 s aus, Power AC fiel von 4970 auf −170 W. Ohne
        Nachschreiben zeigt sich, ob die Pausen verschwinden und ob die Box
        einen Watchdog hat. Das Ende per Zeitlimit stoppt weiter aktiv.
        """
        if not self.manuell_aktiv or self._treiber is None:
            return
        if self._manuell_bis is not None and time.time() >= self._manuell_bis:
            await self.async_manuell_stopp("Zeit abgelaufen")
            return
        # Fährt das Auto weg oder meldet die Box einen Fehler, hat der Test
        # sich erledigt — weiterzuschreiben brächte nichts.
        if self._gelesen and not self._zustand.verbunden:
            await self.async_manuell_stopp("Fahrzeug nicht mehr angesteckt")
            return
        if self._gegenrichtung():
            self._manuell_fehler = (
                "Die Wallbox arbeitet in Gegenrichtung — Vorzeichen-Einstellung prüfen."
            )
            await self.async_manuell_stopp("Die Wallbox arbeitet in Gegenrichtung")
            return

    def _gegenrichtung(self) -> bool:
        """Meldet die Box nach der Anlaufzeit die entgegengesetzte Richtung?

        Nur die Gegenrichtung zählt, nicht Ruhe/Bereit: Dass eine Vorgabe
        nicht ausgeführt wird, ist eine Beobachtung, die der Test liefern
        soll. Gegenrichtung dagegen bewegt Energie, die niemand wollte.
        """
        if not self._gelesen or self._manuell_seit is None:
            return False
        if time.time() - self._manuell_seit < AMBIBOX_RICHTUNG_ANLAUF_S:
            return False
        richtung = self._zustand.richtung
        if self._manuell_richtung == "laden":
            return richtung == "entladen"
        if self._manuell_richtung == "entladen":
            return richtung == "laden"
        return False

    # -- Beobachter -------------------------------------------------------

    def add_listener(self, melden: Callable[[], None]) -> Callable[[], None]:
        self._listener.append(melden)

        def entfernen() -> None:
            if melden in self._listener:
                self._listener.remove(melden)

        return entfernen

    def _melden(self) -> None:
        for melden in list(self._listener):
            try:
                melden()
            except Exception:  # noqa: BLE001 — ein Zuhörer darf den Takt nicht kippen
                _LOGGER.exception("Ambibox: Listener hat einen Fehler geworfen")

    # -- Aufräumen --------------------------------------------------------

    async def async_shutdown(self) -> None:
        # Ein laufender Test darf die Integration nicht überleben: Sonst
        # lädt oder entlädt das Auto weiter, und niemand schreibt den
        # Sollwert mehr zurück.
        if self.manuell_aktiv:
            try:
                await self.async_manuell_stopp("Integration wird beendet")
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Ambibox: manueller Test ließ sich nicht beenden")
        if self._treiber is not None:
            try:
                await self._treiber.async_close()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Ambibox: Fehler beim Schließen der Verbindung", exc_info=True)

    # -- Zustand für Panel und Sensoren -----------------------------------

    def rohregister(self) -> dict[str, Any] | None:
        """Beta-Diagnose: jeder 32-Bit-Wert des letzten Lesevorgangs.

        Je Adresse (Basis + Offset) dreimal gedeutet — u32, i32, f32 —, weil
        offen ist, welche Deutung stimmt: In Ansfelden (05.11.2026) stand
        *Power AC* beim Laden mit 5 kW auf 0, der Batteriezustand auf
        Ruhezustand. Nullwerte bleiben weg, damit das Attribut kurz bleibt.
        """
        if self._roh_eingang is None:
            return None

        def tabelle(regs: list[int] | None, basis: int) -> dict[str, list] | None:
            if regs is None:
                return None
            werte: dict[str, list] = {}
            for offset in range(0, len(regs) - 1, 2):
                if not (regs[offset] or regs[offset + 1]):
                    continue
                f32 = mb.read_f32(regs, offset)
                werte[str(basis + offset)] = [
                    mb.read_u32(regs, offset),
                    mb.read_i32(regs, offset),
                    None if f32 is None else float(f"{f32:.6g}"),
                ]
            return werte

        return {
            "eingang": tabelle(self._roh_eingang, mb.input_base(self.connector)),
            "holding": tabelle(self._roh_holding, mb.holding_base(self.connector)),
        }

    def status(self) -> dict[str, Any]:
        """Vollständiger Zustand als einfaches dict (Panel, WebSocket)."""
        z = self._zustand
        return {
            "enabled": self.enabled,
            "verfuegbar": self.verfuegbar,
            "host": self.host,
            "port": ambibox_port(self._config),
            "unit_id": ambibox_unit_id(self._config),
            "connector": self.connector,
            "last_error": self.last_error,
            "verbunden": z.verbunden,
            "session_state": z.session_state,
            "session_text": z.session_text,
            "protokoll": z.protokoll_text,
            "bidirektional_faehig": z.bidirektional_faehig,
            "replug_required": z.replug_required,
            "soc_pct": z.soc_pct,
            "kapazitaet_kwh": z.kapazitaet_kwh,
            "energie_kwh": z.energie_kwh,
            "soh_pct": z.soh_pct,
            "min_soc_pct": z.min_soc_pct,
            "max_soc_pct": z.max_soc_pct,
            "temperatur_c": z.temperatur_c,
            "zyklen": z.zyklen,
            "zeit_bis_voll_s": z.zeit_bis_voll_s,
            "leistung_kw": z.leistung_kw,
            "richtung": z.richtung,
            "battery_text": z.battery_text,
            "min_charge_kw": z.min_charge_kw,
            "max_charge_kw": z.max_charge_kw,
            "min_discharge_kw": z.min_discharge_kw,
            "max_discharge_kw": z.max_discharge_kw,
            "control_mode": z.control_mode,
            "control_mode_text": z.control_mode_text,
            "steuerbar": z.steuerbar,
            "abfahrt_in_s": z.abfahrt_in_s,
            "abfahrt_soc_pct": z.abfahrt_soc_pct,
            "abfahrt_energie_kwh": z.abfahrt_energie_kwh,
            "session_geladen_kwh": z.session_geladen_kwh,
            "session_entladen_kwh": z.session_entladen_kwh,
            "fehler": list(z.fehler),
            # Manueller Test
            "manuell_aktiv": self.manuell_aktiv,
            "manuell_richtung": self._manuell_richtung,
            "manuell_kw": self._manuell_kw or None,
            "manuell_restsekunden": self.manuell_restsekunden,
            "manuell_sollwert_w": self._letzter_sollwert_w,
            "manuell_fehler": self._manuell_fehler,
            "vorzeichen": ambibox_charge_sign(self._config),
            "schreibvorgaenge": None if self._treiber is None else getattr(self._treiber, "writes", 0),
        }


def _rueckspeisen_moeglich(protokoll: int | None, max_discharge_kw: float | None) -> bool:
    """Darf der Handtest das Fahrzeug entladen?

    ISO 15118-20 genügt, ebenso VWs Erweiterung VAS auf ISO 15118-2. Sonst
    entscheidet, was das Fahrzeug selbst als
    höchste Entladeleistung meldet: Ansfelden, 05.11.2026 — ein Auto, das
    zurückspeisen kann, handelte ISO 15118-2 aus und meldete 10 kW
    Entladeleistung; die Sperre auf das Protokoll allein ließ den Test nicht
    zu. DIN 70121 kennt kein Rückspeisen, dort zählt die Meldung nicht.
    """
    if protokoll in (CHARGE_PROTOCOL_BIDIREKTIONAL, CHARGE_PROTOCOL_VAS):
        return True
    if protokoll == CHARGE_PROTOCOL_DIN_70121:
        return False
    return max_discharge_kw is not None and max_discharge_kw > 0


def _runden(wert: float | None, stellen: int) -> float | None:
    return None if wert is None else round(wert, stellen)


def _w_to_kw(wert: int | float | None) -> float | None:
    return None if wert is None else round(float(wert) / 1000.0, 3)


def create_ambibox(hass: Any, config: dict) -> AmbiboxController | None:
    """Controller samt Treiber bauen — None, wenn keine Ambibox konfiguriert ist.

    Ohne Host oder ohne pymodbus gibt es den Controller ohne Treiber: Die
    Sensoren existieren dann und stehen auf „nicht verfügbar", statt dass die
    Integration mit einem Fehler stehen bleibt.
    """
    if not ambibox_enabled(config):
        return None
    host = ambibox_host(config)
    if not host:
        _LOGGER.warning("Ambibox aktiviert, aber keine Adresse konfiguriert — es wird nicht gelesen")
        return AmbiboxController(hass, config, None)
    if mb.AsyncModbusTcpClient is None:  # pragma: no cover — pymodbus fehlt
        _LOGGER.warning("pymodbus nicht verfügbar — Ambibox wird nicht gelesen")
        return AmbiboxController(hass, config, None)
    treiber = mb.AmbiboxModbus(
        host,
        port=ambibox_port(config),
        unit_id=ambibox_unit_id(config),
        connector=ambibox_connector(config),
    )
    return AmbiboxController(hass, config, treiber)
