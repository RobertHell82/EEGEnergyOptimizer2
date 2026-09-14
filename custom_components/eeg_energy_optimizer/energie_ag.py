"""Monatlicher Einspeisetarif der Energie AG („Team Sonne Float").

Die Energie AG rechnet den Einspeisepreis für jeden Monat im Nachhinein aus
einer veröffentlichten Größe:

    Einspeisevergütung = Referenzmarktwert Photovoltaik § 13 EAG
                         − 1,5 Cent Abschlag pro kWh (VPI-wertgesichert)

Zwei Preisvarianten (Preisblatt, Stand September 2026):

* **Team Sonne Float** — für alle Anlagen bis 50 kWp. „Der Preis sinkt nie
  unter 0 Cent/kWh."
* **Team Sonne Loyal Float** — für Kundinnen und Kunden mit aufrechtem
  Stromliefervertrag bei der Energie AG Vertrieb: „immer mindestens 2 Cent
  je kWh". Im April 2026 war das der ganze Unterschied (0,20 gegen 2,00).

Der Referenzmarktwert ist kein Firmenwert, sondern eine Veröffentlichung der
E-Control: der mit der stündlichen österreichischen PV-Erzeugung gewichtete
Mittelwert der Day-Ahead-Preise des Monats. Gelesen wird er deshalb dort —
``e-control.at/referenzmarktwert`` nennt ihn im Fließtext („für
Photovoltaikanlagen bei 9,42 Cent/kWh"), dieselbe Adresse, die auch das
Preisblatt der Energie AG als Beleg angibt. Die Rechnung wurde gegen alle
zwölf Monate der Preisgrafik des Preisblatts geprüft (Sep 2025 bis Aug 2026,
Abweichung 0,00 ct in jedem Monat, inklusive der Mindestvergütung im April).

Wie bei der OeMAG gilt: jeder Fehler lässt den zuletzt gelesenen Wert
stehen, und ohne jeden Wert greift die Handeingabe — der Ausfall einer
Website darf den Fahrplan nicht anhalten. Der Wert kommt immer mit Monat und
Alter, damit ein stehender Tarif im Panel auffällt.

Die E-Control veröffentlicht einen Monat erst Anfang des Folgemonats (August
2026 am 4. September). Diese Quelle läuft dem laufenden Monat deshalb immer
hinterher — genauso wie ``oemag.py``. Wer den Preis des laufenden Monats
braucht, nimmt ``energie_ag_estimate``: Der Rohwert der OeMAG-Hochrechnung
(``oemag_schaetzung.py``) IST dieser Referenzmarktwert, gerechnet nach
derselben Vorschrift, nur vor Korridor und Ausgleichsenergie. Es gibt dafür
keinen zweiten Abruf — beide Quellen teilen sich denselben Schätzer.
"""

from __future__ import annotations

import html as html_entities
import logging
import re
from datetime import datetime, timezone
from typing import Any

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from homeassistant.helpers.storage import Store
    from homeassistant.util import dt as dt_util

    _utcnow = dt_util.utcnow
except ImportError:  # Testumgebung
    _utcnow = lambda: datetime.now(tz=timezone.utc)  # noqa: E731
    async_get_clientsession = None  # type: ignore[assignment]
    Store = None  # type: ignore[assignment,misc]


# Die E-Control ist zur monatlichen Veröffentlichung verpflichtet (§ 13 EAG);
# dieselbe Adresse steht im Preisblatt der Energie AG als Beleg.
ECONTROL_URL = "https://www.e-control.at/referenzmarktwert"
# Das Preisblatt, auf das sich Formel und Varianten stützen — nur zur Anzeige.
PREISBLATT_URL = "https://www.energieag.at/privat/photovoltaik/pv-einspeisung"
USER_AGENT = "HomeAssistant/EEGEnergyOptimizer"

# Der Wert wechselt einmal im Monat; zweimal täglich nachsehen genügt. Fehlt
# der Vormonat noch (er erscheint zwischen dem 2. und 9.), wird stündlich
# nachgesehen, und bis dahin gilt der Monat davor.
CACHE_FRESH_SECONDS = 12 * 3600
CACHE_RETRY_SECONDS = 3600
# So lange gilt ein gespeicherter Wert weiter, wenn die Quelle schweigt.
# Großzügig, weil ein Wert einen ganzen Monat gilt.
CACHE_MAX_SECONDS = 40 * 24 * 3600
# Ältere Monate fliegen aus dem Speicher — sie helfen keinem Rückfall mehr.
AUFBEWAHRUNG_JAHRE = 1

VARIANTE_FLOAT = "float"              # Team Sonne Float
VARIANTE_LOYAL = "loyal_float"        # Team Sonne Loyal Float
VARIANTEN = (VARIANTE_FLOAT, VARIANTE_LOYAL)

# „Der Preis sinkt nie unter 0 Cent/kWh" (Team Sonne Float) bzw. „immer
# mindestens 2 Cent je kWh" (Loyal Float, nur mit Stromliefervertrag).
UNTERGRENZE_EUR = {VARIANTE_FLOAT: 0.0, VARIANTE_LOYAL: 0.02}

# Abschlag laut Preisblatt. Er ist VPI-wertgesichert, steigt also irgendwann —
# deshalb ist er einstellbar und steht hier nur als Vorgabe.
DEFAULT_ABSCHLAG_EUR = 0.015

_MONATE = {
    "jänner": 1, "januar": 1, "jaenner": 1,
    "februar": 2, "feber": 2,
    "märz": 3, "maerz": 3,
    "april": 4,
    "mai": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "dezember": 12,
}

# „… Referenzmarktwert im August 2026 : für Wasserkraftanlagen bei 15,19
# Cent/kWh für Windkraftanlagen bei 15,32 Cent/kWh für Photovoltaikanlagen
# bei 9,42 Cent/kWh". Monat und Wert stehen in einem Satz, aber mit zwei
# anderen Technologien dazwischen — deshalb zwei Ausdrücke statt einem.
_MONAT_RE = re.compile(
    r"Referenzmarktwert\s+im\s+(" + "|".join(sorted(_MONATE, key=len, reverse=True))
    + r")\s+(\d{4})",
    re.IGNORECASE,
)
_PV_RE = re.compile(
    r"Photovoltaikanlagen\s+bei\s+([\d.,]+)\s*Cent", re.IGNORECASE
)


def monatsschluessel(jahr: int, monat: int) -> int:
    """(2026, 9) → 202609 — sortierbar über Jahresgrenzen hinweg."""
    return jahr * 100 + monat


def jahr_monat(schluessel: int) -> tuple[int, int]:
    return schluessel // 100, schluessel % 100


def aktueller_schluessel() -> int:
    """Laufender Monat in Ortszeit als Schlüssel (JJJJMM) — eigene Funktion,
    damit Tests sie ersetzen können."""
    jetzt = datetime.now()
    return monatsschluessel(jetzt.year, jetzt.month)


def nur_text(roh: str | None) -> str:
    """HTML → Fließtext, wie in ``oemag.py``: Skripte raus, Tags raus,
    Entities auflösen, Leerraum eindampfen."""
    text = re.sub(
        r"<(script|style)[^>]*>.*?</\1>", " ", roh or "", flags=re.S | re.I
    )
    text = html_entities.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def parse_referenzmarktwert(roh: str | None) -> tuple[int, int, float] | None:
    """E-Control-Seite → (Jahr, Monat, Referenzmarktwert PV in €/kWh).

    None, wenn Monat oder Wert nicht zu finden sind — dann bleibt der zuletzt
    gelesene Wert stehen, statt eine halbe Angabe zu übernehmen.
    """
    text = nur_text(roh)
    if not text:
        return None
    monat_treffer = _MONAT_RE.search(text)
    if monat_treffer is None:
        return None
    monat = _MONATE.get(monat_treffer.group(1).lower())
    if monat is None:
        return None
    jahr = int(monat_treffer.group(2))
    # Nur hinter der Monatsangabe suchen: Weiter oben steht die Erklärung des
    # Verfahrens, weiter unten die Historie — beides nennt Photovoltaik.
    pv_treffer = _PV_RE.search(text, monat_treffer.end())
    if pv_treffer is None:
        return None
    wert = _ct_wert(pv_treffer.group(1))
    if wert is None:
        return None
    return jahr, monat, wert


def tarif_aus_referenzwert(
    referenzwert: float, abschlag: float, variante: str
) -> float:
    """Referenzmarktwert → Einspeisevergütung in €/kWh.

    Abschlag ab, dann die Untergrenze der Variante — geprüft gegen zwölf
    Monate des Preisblatts (siehe Modul-Docstring).
    """
    untergrenze = UNTERGRENZE_EUR.get(variante, UNTERGRENZE_EUR[VARIANTE_FLOAT])
    return round(max(referenzwert - abschlag, untergrenze), 6)


def wert_fuer(werte: dict[int, float], schluessel: int) -> tuple[float, int] | None:
    """Referenzmarktwert des Monats, sonst der jüngste davor bekannte.

    Rückgabe: (€/kWh, Monatsschlüssel). Der laufende Monat fehlt hier immer —
    er wird erst im Folgemonat veröffentlicht —, deshalb ist der Rückfall auf
    den jüngsten bekannten Monat der Normalfall und keine Ausnahme.
    """
    if not werte:
        return None
    if schluessel in werte:
        return werte[schluessel], schluessel
    frueher = [s for s in werte if s < schluessel]
    if frueher:
        letzter = max(frueher)
        return werte[letzter], letzter
    letzter = max(werte)
    return werte[letzter], letzter


class EnergieAgProvider:
    """Holt den Referenzmarktwert und hält ihn über Neustarts hinweg.

    Variante und Abschlag sind keine Eigenschaften des Anbieters, sondern der
    Abfrage (``preis_fuer``): So wirkt eine Änderung in den Einstellungen
    sofort, und das Panel kann beide Varianten nebeneinander zeigen.

    ``schaetzer`` ist der ``OemagSchaetzer``; sein Rohwert ist derselbe
    Referenzmarktwert, nur für den laufenden Monat hochgerechnet.
    """

    def __init__(self, hass: Any, entry_id: str, schaetzer: Any = None) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._schaetzer = schaetzer
        if Store is not None:
            self._store = Store(hass, 1, f"{DOMAIN}_{entry_id}_energie_ag")
        else:
            self._store = None  # type: ignore[assignment]
        self._werte: dict[int, float] = {}
        self._geholt: datetime | None = None
        self._fehler: str | None = None

    # -- Zustand -------------------------------------------------------

    def hat_daten(self) -> bool:
        return bool(self._werte)

    def _gueltig(self) -> bool:
        if self._geholt is None:
            return False
        return (_utcnow() - self._geholt).total_seconds() <= CACHE_MAX_SECONDS

    def _treffer(self) -> tuple[float, int] | None:
        return wert_fuer(self._werte, aktueller_schluessel())

    def preis_fuer(self, variante: str, abschlag: float | None = None) -> float | None:
        """Einspeisevergütung in €/kWh aus dem zuletzt veröffentlichten Monat.

        None ohne Daten oder wenn der letzte Abruf länger als
        ``CACHE_MAX_SECONDS`` zurückliegt.
        """
        if not self._gueltig():
            return None
        treffer = self._treffer()
        if treffer is None:
            return None
        return tarif_aus_referenzwert(
            treffer[0], _abschlag(abschlag), _variante(variante)
        )

    def preis_geschaetzt(
        self, variante: str, abschlag: float | None = None
    ) -> float | None:
        """Dasselbe aus der Hochrechnung des LAUFENDEN Monats.

        Der Schätzer liefert seinen Rohwert nur für den laufenden Monat; ist
        er nicht da, gibt es hier None und der Aufrufer nimmt den
        veröffentlichten Wert.
        """
        roh = getattr(self._schaetzer, "roh", None) if self._schaetzer else None
        if roh is None:
            return None
        return tarif_aus_referenzwert(
            float(roh), _abschlag(abschlag), _variante(variante)
        )

    def status(self, abschlag: float | None = None) -> dict[str, Any]:
        """Für die Anzeige: Referenzmarktwert, Monat, beide Varianten, Alter."""
        alter_min = None
        if self._geholt is not None:
            alter_min = int((_utcnow() - self._geholt).total_seconds() / 60)
        abschlag_eur = _abschlag(abschlag)
        ergebnis: dict[str, Any] = {
            "abschlag": abschlag_eur,
            "alter_minuten": alter_min,
            "fehler": self._fehler,
            "quelle_url": ECONTROL_URL,
            "preisblatt_url": PREISBLATT_URL,
            "referenzwert": None,
            "jahr": None,
            "monat": None,
        }
        treffer = self._treffer() if self._gueltig() else None
        if treffer is not None:
            referenzwert, schluessel = treffer
            jahr, monat = jahr_monat(schluessel)
            ergebnis.update({"referenzwert": referenzwert, "jahr": jahr, "monat": monat})
            for variante in VARIANTEN:
                ergebnis[variante] = {
                    "preis": tarif_aus_referenzwert(referenzwert, abschlag_eur, variante),
                    "jahr": jahr,
                    "monat": monat,
                }
        else:
            for variante in VARIANTEN:
                ergebnis[variante] = None
        # Die Hochrechnung des laufenden Monats, wenn der Schätzer läuft.
        roh = getattr(self._schaetzer, "roh", None) if self._schaetzer else None
        ergebnis["schaetzung"] = None if roh is None else {
            "referenzwert": round(float(roh), 6),
            **{
                variante: tarif_aus_referenzwert(float(roh), abschlag_eur, variante)
                for variante in VARIANTEN
            },
        }
        return ergebnis

    def _vormonat_bekannt(self) -> bool:
        """Steht der zuletzt fällige Monat schon im Speicher?

        Fällig ist der Vormonat: Der laufende erscheint erst nach seinem
        Ende. Solange er fehlt, wird häufiger nachgesehen.
        """
        jahr, monat = jahr_monat(aktueller_schluessel())
        if monat == 1:
            jahr, monat = jahr - 1, 12
        else:
            monat -= 1
        return monatsschluessel(jahr, monat) in self._werte

    # -- Laden und Holen -----------------------------------------------

    async def async_load(self) -> None:
        if self._store is None:
            return
        try:
            stored = await self._store.async_load()
            if stored and isinstance(stored, dict):
                self._werte = {
                    int(k): float(v) for k, v in (stored.get("werte") or {}).items()
                }
                geholt = stored.get("geholt")
                if geholt:
                    self._geholt = datetime.fromisoformat(geholt)
        except Exception:
            _LOGGER.debug("Energie AG: kein gespeicherter Referenzmarktwert vorhanden")

    async def async_fetch(self, force: bool = False) -> None:
        """Referenzmarktwert holen, wenn der gespeicherte Wert alt genug ist."""
        jetzt = _utcnow()
        if not force and self._geholt is not None:
            frist = (
                CACHE_FRESH_SECONDS if self._vormonat_bekannt() else CACHE_RETRY_SECONDS
            )
            if (jetzt - self._geholt).total_seconds() < frist:
                return
        if async_get_clientsession is None:
            return

        import aiohttp

        session = async_get_clientsession(self._hass)
        try:
            async with session.get(
                ECONTROL_URL,
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                seite = await resp.text()
        except Exception as err:  # noqa: BLE001 — jeder Fehler heißt: alter Wert bleibt
            self._fehler = f"E-Control: {err}"
            _LOGGER.warning(
                "Referenzmarktwert der E-Control nicht abrufbar (%s) — es gilt weiter %s",
                self._fehler,
                "der zuletzt gelesene Wert" if self.hat_daten() else "die Handeingabe",
            )
            return

        gelesen = parse_referenzmarktwert(seite)
        if gelesen is None:
            self._fehler = "E-Control: kein Referenzmarktwert für Photovoltaik erkannt"
            _LOGGER.warning(
                "Referenzmarktwert der E-Control nicht lesbar — es gilt weiter %s",
                "der zuletzt gelesene Wert" if self.hat_daten() else "die Handeingabe",
            )
            return

        jahr, monat, referenzwert = gelesen
        self._werte[monatsschluessel(jahr, monat)] = referenzwert
        self._fehler = None
        self._geholt = jetzt
        grenze = monatsschluessel(jahr - AUFBEWAHRUNG_JAHRE, 1)
        self._werte = {s: w for s, w in self._werte.items() if s >= grenze}
        _LOGGER.debug(
            "Energie AG: Referenzmarktwert %.5f €/kWh (%02d/%d)",
            referenzwert, monat, jahr,
        )

        if self._store is not None:
            try:
                await self._store.async_save(
                    {
                        "werte": {str(k): w for k, w in self._werte.items()},
                        "geholt": jetzt.isoformat(),
                    }
                )
            except Exception:
                _LOGGER.debug("Referenzmarktwert konnte nicht gespeichert werden")


# ---------------------------------------------------------------------------
# Kleinkram
# ---------------------------------------------------------------------------


def _variante(roh: Any) -> str:
    wert = str(roh or "").lower().strip()
    return wert if wert in VARIANTEN else VARIANTE_FLOAT


def _abschlag(roh: Any) -> float:
    """Abschlag in €/kWh; ein leeres oder unlesbares Feld nimmt die Vorgabe.

    Eine 0 ist eine Aussage (kein Abschlag) und keine fehlende Angabe.
    """
    if roh is None or roh == "":
        return DEFAULT_ABSCHLAG_EUR
    try:
        wert = float(roh)
    except (TypeError, ValueError):
        return DEFAULT_ABSCHLAG_EUR
    return max(0.0, wert)


def _ct_wert(text: str | None) -> float | None:
    """„9,42" oder „9.42" (Cent/kWh) → 0,0942 €/kWh; leer → None."""
    if text is None:
        return None
    t = str(text).strip()
    if not t:
        return None
    if "," in t and "." not in t:
        t = t.replace(",", ".")
    elif "," in t and "." in t:
        # Tausenderpunkt in einer Cent-Angabe wäre absurd, aber billig zu
        # entschärfen: „1.234,5" → „1234.5".
        t = t.replace(".", "").replace(",", ".")
    try:
        return round(float(t) / 100.0, 6)
    except ValueError:
        return None
