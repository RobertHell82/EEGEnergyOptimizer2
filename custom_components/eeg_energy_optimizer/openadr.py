"""OpenADR 3.1: Signale der Gemeinschaft nach Harald Geyers EEG-Profil.

Vierte Bedarfsquelle neben PeakShare, energy agent und der festen
Abnahmequote (``eeg_demand_source = "openadr"``). Die Gemeinschaft betreibt
einen VTN (Virtual Top Node), wir sind der VEN und lesen nur — Phase 1:
öffentliche Events, keine Anmeldung, keine Rückmeldung an die Gemeinschaft.

Das Profil („OpenADR 3.1 und EEGs“, Harald Geyer, 2026):

* **ein VTN je Gemeinschaft** — EEG und BEG haben je eine eigene Adresse,
  hier ``openadr_url`` / ``openadr_url_2`` neben den beiden Gemeinschaften;
* zwei Pflichtprogramme ``ec_feedin`` (Einspeisung) und ``ec_demand``
  (Bezug), ``program.id`` = ``programName``;
* je Programm ein **OLS-Event** (Optimum Load Shape, 0..1, 15 min, ≥ 48 h):
  bei ``ec_feedin`` der Anteil meiner Einspeisung, den die Gemeinschaft
  aufnimmt, bei ``ec_demand`` der Anteil meines Bezugs, den sie liefert
  (jeweils bei Teilnahmefaktor 1);
* dazu ``EXPORT_PRICE`` bzw. ``PRICE`` als eigenes Event, auch als einzelner
  Dauerwert zulässig;
* Priorität 51–100 öffentlich/indikativ, ≤ 50 mitgliedsspezifisch und
  verbindlich — der kleinere Wert gewinnt;
* höchstens stündlich dieselbe Information abfragen. Wir fragen nach einem
  erfolgreichen Abruf genau stündlich — damit ist auch die Regel „reicht die
  Reihe weniger als 10 h voraus, früher nachfragen“ erfüllt; ein knapper
  Vorlauf wird nur als Warnung gemeldet. Nach einem Fehlschlag (es kam keine
  Information) nach 15 min erneut.

**Die OLS ist eine Abnahmequote je Viertelstunde.** Sie geht deshalb nicht
in den PeakShare-Saldo (kWh, auf die Spitze normiert), sondern dorthin, wo
der Quotenmodus seine feste Quote einsetzt (``eeg_price.ols_aufschlag_reihe``,
``schedule._eeg_zuteilung``, ``schedule.bezugsreihe_netzladen``).

Zwei Robustheitsregeln, beide an Haralds eigenem Static VTN
(github.com/haraldg/openADR-static-vtn) abgelesen:

* Ein statischer VTN ignoriert die Abfrageparameter und liefert ALLE Events.
  Die Zuordnung zum Programm prüfen wir deshalb selbst (``programID``).
* Die Preiseinheit ist nicht eindeutig (das Beispiel des Static VTN trägt
  ``currency: EUR`` und Werte um 22 — ct/kWh). Werte über
  ``PREIS_CT_SCHWELLE`` gelten als ct/kWh und werden umgerechnet, mit
  Hinweis. Ein Preis über 2 €/kWh ist für Haushaltsstrom nicht plausibel.

Zeitvariable Preise kann der Fahrplan je Gemeinschaft noch nicht abbilden
(dort gibt es Tag- und Nachtsatz). Ein über den Horizont konstanter Preis
überlagert die eingetragenen Sätze (``tarife_ueberlagern``); ein
veränderlicher bleibt mit Warnung draußen, es gelten die eingetragenen.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

try:
    from homeassistant.helpers.aiohttp_client import async_get_clientsession
    from homeassistant.helpers.storage import Store
    from homeassistant.util import dt as dt_util

    _utcnow = dt_util.utcnow
except ImportError:
    _utcnow = lambda: datetime.now(tz=timezone.utc)  # noqa: E731
    async_get_clientsession = None  # type: ignore[assignment]
    Store = None  # type: ignore[assignment,misc]


QUELLE = "openadr"
CONF_OPENADR_URL = "openadr_url"
CONF_OPENADR_URL_2 = "openadr_url_2"
USER_AGENT = "HomeAssistant/EEGEnergyOptimizer"

PROGRAMM_EINSPEISUNG = "ec_feedin"
PROGRAMM_BEZUG = "ec_demand"
TYP_OLS = "OLS"
TYP_PREIS_EINSPEISUNG = "EXPORT_PRICE"
TYP_PREIS_BEZUG = "PRICE"

# Profil: „Ein VEN soll nicht öfter als einmal stündlich dieselbe Information
# abfragen.“ Der 30-min-Takt der Integration ist nur der Anlass.
CACHE_FRESH_SECONDS = 3600
# Profil: Signale sollen mindestens 10 h vorausreichen — darunter warnen.
VORLAUF_MIN_S = 10 * 3600
# Nach einem Fehlschlag kam keine Information: dann früher erneut versuchen.
NACHFRAGE_MIN_S = 900
# Ohne erfolgreichen Abruf gilt der Cache so lange — die Reihe selbst reicht
# ohnehin nur so weit, wie der VTN sie geliefert hat.
CACHE_MAX_SECONDS = 48 * 3600

TIMEOUT_S = 30
SEITE = 50
SEITEN_MAX = 20
VIERTEL_S = 900
# Über diesem Betrag ist ein „€/kWh“-Preis in Wahrheit ct/kWh.
PREIS_CT_SCHWELLE = 2.0


class OpenAdrFehler(Exception):
    """Abruf gescheitert — ``grund`` ist kurz und für das Panel gedacht."""

    def __init__(self, grund: str) -> None:
        super().__init__(grund)
        self.grund = grund


# ---------------------------------------------------------------------------
# Reine Hilfsfunktionen (ohne Netz, einzeln getestet)
# ---------------------------------------------------------------------------


def basis_url(roh: Any) -> str | None:
    """Eingetippte Adresse auf die VTN-Basis bringen.

    Wer die Event-URL aus der Anleitung der Gemeinschaft kopiert
    (``…/events?programID=ec_feedin``), soll trotzdem die Basis bekommen.
    Nur http(s); alles andere ist keine Adresse, die wir anfragen.
    """
    text = str(roh or "").strip()
    if not text:
        return None
    teile = urlsplit(text)
    if teile.scheme not in ("http", "https") or not teile.netloc:
        return None
    pfad = teile.path.rstrip("/")
    pfad = re.sub(r"/(events|programs)(/[^/]*)?$", "", pfad)
    return urlunsplit((teile.scheme, teile.netloc, pfad, "", "")).rstrip("/")


_DAUER = re.compile(
    r"^P(?:(?P<w>\d+(?:\.\d+)?)W)?(?:(?P<d>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<h>\d+(?:\.\d+)?)H)?(?:(?P<m>\d+(?:\.\d+)?)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$"
)


def dauer_s(roh: Any) -> float | None:
    """ISO-8601-Dauer (``PT15M``, ``PT1H``, ``P1D``) in Sekunden.

    Jahre und Monate haben keine feste Länge und kommen in Zeitreihen nicht
    vor — sie gelten als unlesbar. ``P9999Y`` (OpenADR: „unbegrenzt“) damit
    ebenso; ein unbegrenztes Intervall ohne Ende hilft dem Fahrplan nicht.
    """
    if not isinstance(roh, str):
        return None
    m = _DAUER.match(roh.strip().upper())
    if not m or roh.strip().upper() in ("P", "PT"):
        return None
    w = {k: float(v) if v else 0.0 for k, v in m.groupdict().items()}
    sekunden = w["w"] * 604800 + w["d"] * 86400 + w["h"] * 3600 + w["m"] * 60 + w["s"]
    return sekunden if sekunden > 0 else None


def _zeit(roh: Any) -> datetime | None:
    if not isinstance(roh, str) or not roh:
        return None
    try:
        t = datetime.fromisoformat(roh.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _zahl(roh: Any) -> float | None:
    if isinstance(roh, bool):
        return None
    try:
        wert = float(roh)
    except (TypeError, ValueError):
        return None
    return wert if math.isfinite(wert) else None


def _prioritaet(event: dict) -> float:
    """Kleiner gewinnt; ohne Angabe ganz hinten (OpenADR: null = niedrigste)."""
    wert = _zahl(event.get("priority"))
    return wert if wert is not None else math.inf


# Ein Dauerwert (``P9999Y`` = „unbegrenzt“ in OpenADR 3, oder ganz ohne
# Dauer) gilt ab seinem Start bis auf Weiteres. Ausgerollt wird er nur über
# das Fenster, das der Fahrplan braucht — ab gestern bis eine Woche voraus.
OFFEN_ZURUECK_S = 86400
OFFEN_VORAUS_S = 7 * 86400
_JAHRE = re.compile(r"^P\d+Y$")


def _offen(roh: Any) -> bool:
    """Keine Dauer oder eine in Jahren: das Intervall läuft bis auf Weiteres."""
    return roh is None or roh == "" or (isinstance(roh, str) and bool(_JAHRE.match(roh.strip().upper())))


def reihe_aus_events(
    events: list[dict], typ: str, jetzt: datetime | None = None
) -> dict[int, float]:
    """Alle Intervalle eines Payload-Typs auf ``{Epochenviertelstunde: Wert}``.

    Ein Intervall trägt seine eigene ``intervalPeriod`` oder erbt die des
    Events; ohne eigene Startzeit folgt es lückenlos auf das vorige (so
    beschreibt OpenADR 3 eine Reihe mit einem Kopf). Intervalle, die eine
    Viertelstunde nur teilweise abdecken, gehen zeitgewichtet ein.

    Mehrere Events: je Viertelstunde gewinnt die kleinste ``priority``; bei
    Gleichstand das zuletzt geänderte. Werte eines unterlegenen Events füllen
    nur Viertelstunden, die das gewinnende nicht abdeckt.

    Ein Dauerwert (Profil: „auch ein einzelner Dauerwert ist zulässig“) hat
    ``P9999Y`` oder gar keine Dauer; er gilt von ``max(Start, jetzt − 1 Tag)``
    bis ``jetzt + 7 Tage``.
    """
    jetzt_s = (jetzt or _utcnow()).timestamp()
    def _rang(e: dict) -> tuple:
        geaendert = _zeit(e.get("modificationDateTime"))
        return (_prioritaet(e), -(geaendert.timestamp() if geaendert else 0.0))

    ergebnis: dict[int, float] = {}
    for event in sorted((e for e in events if isinstance(e, dict)), key=_rang):
        kopf = event.get("intervalPeriod") if isinstance(event.get("intervalPeriod"), dict) else {}
        kopf_start = _zeit(kopf.get("start"))
        kopf_dauer = dauer_s(kopf.get("duration"))
        summe: dict[int, float] = {}
        gewicht: dict[int, float] = {}
        naechster: datetime | None = kopf_start
        for intervall in event.get("intervals") or []:
            if not isinstance(intervall, dict):
                continue
            periode = intervall.get("intervalPeriod")
            periode = periode if isinstance(periode, dict) else {}
            start = _zeit(periode.get("start")) or naechster
            roh_dauer = periode.get("duration") if periode.get("duration") else kopf.get("duration")
            dauer = dauer_s(periode.get("duration")) or kopf_dauer
            offen = dauer is None and _offen(roh_dauer)
            if start is None or (dauer is None and not offen):
                naechster = None
                continue
            naechster = None if offen else start + timedelta(seconds=dauer)
            wert = None
            for payload in intervall.get("payloads") or []:
                if isinstance(payload, dict) and str(payload.get("type") or "").upper() == typ:
                    werte = payload.get("values")
                    if isinstance(werte, list) and werte:
                        wert = _zahl(werte[0])
                    break
            if wert is None:
                continue
            anfang = start.timestamp()
            if offen:
                anfang = max(anfang, jetzt_s - OFFEN_ZURUECK_S)
                ende = jetzt_s + OFFEN_VORAUS_S
            else:
                ende = anfang + dauer
            v = int(anfang // VIERTEL_S)
            while v * VIERTEL_S < ende:
                ueberlapp = min(ende, (v + 1) * VIERTEL_S) - max(anfang, v * VIERTEL_S)
                if ueberlapp > 0:
                    summe[v] = summe.get(v, 0.0) + wert * ueberlapp
                    gewicht[v] = gewicht.get(v, 0.0) + ueberlapp
                v += 1
        for v, s in summe.items():
            if v not in ergebnis and gewicht[v] > 0:
                ergebnis[v] = s / gewicht[v]
    return ergebnis


def ols_klemmen(reihe: dict[int, float]) -> dict[int, float]:
    """OLS ist ein Anteil: alles außerhalb 0..1 auf den Rand."""
    return {v: min(1.0, max(0.0, w)) for v, w in reihe.items()}


def einheit_aus_deskriptoren(deskriptoren: list[Any], typ: str) -> str | None:
    """``units`` des passenden ``payloadDescriptors`` (``KWH``, ``MWH`` …)."""
    for d in deskriptoren or []:
        if isinstance(d, dict) and str(d.get("payloadType") or "").upper() == typ:
            einheit = str(d.get("units") or "").strip().upper()
            return einheit or None
    return None


def preise_in_euro(
    reihe: dict[int, float], einheit: str | None = None
) -> tuple[dict[int, float], bool]:
    """In €/kWh. Rückgabe (Reihe, war_ct).

    Steht ``MWH`` im Deskriptor, ist es ein Preis je MWh (÷ 1000). Sonst —
    ``KWH`` oder keine Angabe — zählen Werte über ``PREIS_CT_SCHWELLE`` als
    ct/kWh: Haralds Static VTN schreibt ``KWH``/``EUR`` mit Werten um 22.
    """
    if einheit == "MWH":
        return {v: w / 1000.0 for v, w in reihe.items()}, False
    if reihe and max(abs(w) for w in reihe.values()) > PREIS_CT_SCHWELLE:
        return {v: w / 100.0 for v, w in reihe.items()}, True
    return reihe, False


def konstanter_wert(reihe: dict[int, float], ab_viertel: int) -> float | None:
    """Der Preis, wenn er ab jetzt überall gleich ist — sonst None."""
    werte = [w for v, w in reihe.items() if v >= ab_viertel]
    if not werte:
        # Nur Vergangenheit, oder ein Dauerwert, der jetzt schon läuft: der
        # letzte bekannte Wert ist der einzige, den wir haben.
        if not reihe:
            return None
        werte = [reihe[max(reihe)]]
    if max(werte) - min(werte) > 1e-6:
        return None
    return round(werte[0], 6)


def _zu_text(reihe: dict[int, float]) -> dict[str, float]:
    return {str(v): round(w, 6) for v, w in sorted(reihe.items())}


def _aus_text(roh: Any) -> dict[int, float]:
    if not isinstance(roh, dict):
        return {}
    out: dict[int, float] = {}
    for k, w in roh.items():
        try:
            out[int(k)] = float(w)
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


def quellen_aus_config(config: dict[str, Any]) -> list[tuple[str, str]]:
    """``[(Gemeinschaftsname, VTN-Basis)]`` — nur Gemeinschaften mit beidem."""
    paare = []
    for name_key, url_key in (
        ("peakshare_community", CONF_OPENADR_URL),
        ("peakshare_community_2", CONF_OPENADR_URL_2),
    ):
        name = str(config.get(name_key) or "").strip()
        url = basis_url(config.get(url_key))
        if name and url:
            paare.append((name, url))
    return paare


class OpenAdrProvider:
    """Holt OLS und Preise je Gemeinschaft und hält sie im Cache.

    Schnittstelle des ``PeakShareProvider`` (``async_load``, ``async_fetch``,
    ``get_communities``, ``get_intervals``, ``get_warnings``,
    ``letzter_fehler``) plus ``ols`` und ``preis`` für den Fahrplan — er
    liegt wie energy agent auf ``data["peakshare"]``.
    """

    QUELLE = QUELLE

    def __init__(
        self, hass: Any, entry_id: str, quellen: list[tuple[str, str]]
    ) -> None:
        self._hass = hass
        self._quellen = list(quellen)
        if Store is not None and entry_id != "temp":
            self._store = Store(hass, 1, f"{DOMAIN}_{entry_id}_openadr")
        else:
            self._store = None  # type: ignore[assignment]
        # {Name: {"url", "ols_einspeisung", "ols_bezug", "preis_einspeisung",
        #         "preis_bezug" (je {Viertel: Wert}), "warnings": [...]}}
        self._daten: dict[str, dict[str, Any]] = {}
        self._cache: dict | None = None      # für ws get_peakshare_data
        self._cache_time: datetime | None = None
        # Je Gemeinschaft: letzter Versuch, letzter Erfolg, aktueller Fehler —
        # damit ein ausgefallener VTN den gesunden nicht mitzieht.
        self._versuch: dict[str, datetime] = {}
        self._erfolg: dict[str, datetime] = {}
        self._fehler: dict[str, str] = {}
        self.letzter_fehler: str | None = None

    # -- Netz ---------------------------------------------------------------

    async def _get(self, url: str, params: dict | None = None) -> Any:
        if async_get_clientsession is None:
            raise OpenAdrFehler("Kein HTTP-Client")
        import aiohttp

        session = async_get_clientsession(self._hass)
        try:
            async with session.get(
                url,
                params=params,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=TIMEOUT_S),
            ) as resp:
                if resp.status in (401, 403):
                    raise OpenAdrFehler(f"Zugriff verweigert (HTTP {resp.status})")
                if resp.status == 404:
                    raise OpenAdrFehler("Adresse nicht gefunden (HTTP 404)")
                if resp.status != 200:
                    raise OpenAdrFehler(f"HTTP {resp.status}")
                return await resp.json(content_type=None)
        except OpenAdrFehler:
            raise
        except Exception as err:  # Netz, Zeitlimit, kaputtes JSON
            raise OpenAdrFehler(f"Nicht erreichbar ({type(err).__name__})") from err

    async def _liste(self, url: str, params: dict | None = None) -> list[dict]:
        """Seitenweise lesen (``skip``/``limit``), höchstens ``SEITEN_MAX``.

        Ein statischer VTN ignoriert ``skip`` und liefert jedes Mal dasselbe —
        eine Seite ohne neue ``id`` beendet das Lesen.
        """
        alle: list[dict] = []
        gesehen: set[str] = set()
        for seite in range(SEITEN_MAX):
            antwort = await self._get(url, {**(params or {}), "skip": seite * SEITE, "limit": SEITE})
            if not isinstance(antwort, list):
                raise OpenAdrFehler("Ungültige Antwort (keine Liste)")
            neu = 0
            for obj in antwort:
                if not isinstance(obj, dict):
                    continue
                schluessel = str(obj.get("id") or id(obj))
                if schluessel in gesehen:
                    continue
                gesehen.add(schluessel)
                alle.append(obj)
                neu += 1
            if neu == 0 or len(antwort) < SEITE:
                break
        return alle

    async def _programme(self, basis: str) -> dict[str, dict]:
        """``{programName: Programm}``. Fällt das aus, gilt id = Name (Profil)."""
        try:
            programme = await self._liste(f"{basis}/programs")
        except OpenAdrFehler:
            programme = []
        nach_name = {
            str(p.get("programName")): p
            for p in programme
            if p.get("programName") and p.get("id")
        }
        for name in (PROGRAMM_EINSPEISUNG, PROGRAMM_BEZUG):
            nach_name.setdefault(name, {"id": name})
        return nach_name

    async def abrufen_einzeln(self, basis: str, jetzt: datetime | None = None) -> dict[str, Any]:
        """Eine Gemeinschaft lesen. Wirft ``OpenAdrFehler``, wenn nichts kam."""
        jetzt = jetzt or _utcnow()
        programme = await self._programme(basis)
        warnungen: list[str] = []
        ergebnis: dict[str, Any] = {"url": basis}
        for programm, ols_key, preis_key, preis_typ in (
            (PROGRAMM_EINSPEISUNG, "ols_einspeisung", "preis_einspeisung", TYP_PREIS_EINSPEISUNG),
            (PROGRAMM_BEZUG, "ols_bezug", "preis_bezug", TYP_PREIS_BEZUG),
        ):
            pid = str(programme[programm]["id"])
            try:
                events = await self._liste(f"{basis}/events", {"programID": pid})
            except OpenAdrFehler as err:
                warnungen.append(f"{programm}: {err.grund}")
                events = []
            # Selbst filtern: ein statischer VTN liefert alle Events.
            events = [e for e in events if str(e.get("programID") or "") == pid]
            ols = ols_klemmen(reihe_aus_events(events, TYP_OLS, jetzt))
            # Einheit: zuerst am Event, sonst am Programm.
            einheit = None
            for e in events:
                einheit = einheit_aus_deskriptoren(e.get("payloadDescriptors"), preis_typ)
                if einheit:
                    break
            einheit = einheit or einheit_aus_deskriptoren(
                programme[programm].get("payloadDescriptors"), preis_typ
            )
            preis, war_ct = preise_in_euro(
                reihe_aus_events(events, preis_typ, jetzt), einheit
            )
            if not ols:
                warnungen.append(f"{programm}: keine OLS")
            if war_ct:
                warnungen.append(f"{programm}: Preise als ct/kWh gelesen")
            ergebnis[ols_key] = ols
            ergebnis[preis_key] = preis
        jetzt_v = int(jetzt.timestamp() // VIERTEL_S)
        ende = max(ergebnis["ols_einspeisung"], default=None)
        # Ohne Einspeise-OLS ab jetzt wirkt die Gemeinschaft auf die
        # Einspeisung nicht — das ist ein Versagen, kein Hinweis.
        ergebnis["fehler"] = (
            f"{PROGRAMM_EINSPEISUNG}: keine OLS ab jetzt"
            if ende is None or ende < jetzt_v else None
        )
        if ende is not None and ende >= jetzt_v and (ende + 1 - jetzt_v) * VIERTEL_S < VORLAUF_MIN_S:
            warnungen.append(
                f"{PROGRAMM_EINSPEISUNG}: OLS reicht nur {max(0, ende + 1 - jetzt_v) * 15} min voraus"
            )
        if not ergebnis["ols_einspeisung"] and not ergebnis["ols_bezug"]:
            raise OpenAdrFehler("; ".join(warnungen) or "Keine Daten")
        ergebnis["warnings"] = warnungen
        return ergebnis

    def _faellig(self, name: str, jetzt: datetime) -> bool:
        """Nach Erfolg stündlich, nach einem Fehlschlag nach 15 min."""
        versuch = self._versuch.get(name)
        if versuch and (jetzt - versuch).total_seconds() < NACHFRAGE_MIN_S:
            return False
        erfolg = self._erfolg.get(name)
        if erfolg is None or name in self._fehler:
            return True
        return (jetzt - erfolg).total_seconds() >= CACHE_FRESH_SECONDS

    # -- Schnittstelle wie PeakShareProvider --------------------------------

    async def async_load(self) -> None:
        if self._store is None:
            return
        try:
            stored = await self._store.async_load()
        except Exception:
            return
        if not isinstance(stored, dict) or not isinstance(stored.get("daten"), dict):
            return
        urls = dict(self._quellen)
        for name, eintrag in stored["daten"].items():
            # Ein Cache gehört zu genau einer Adresse.
            if not isinstance(eintrag, dict) or urls.get(name) != eintrag.get("url"):
                continue
            self._daten[name] = {
                "url": eintrag["url"],
                "ols_einspeisung": _aus_text(eintrag.get("ols_einspeisung")),
                "ols_bezug": _aus_text(eintrag.get("ols_bezug")),
                "preis_einspeisung": _aus_text(eintrag.get("preis_einspeisung")),
                "preis_bezug": _aus_text(eintrag.get("preis_bezug")),
                "warnings": list(eintrag.get("warnings") or []),
            }
        if self._daten:
            try:
                self._cache_time = datetime.fromisoformat(stored.get("fetched_at") or "")
            except ValueError:
                self._cache_time = None
            if self._cache_time is not None:
                for name in self._daten:
                    self._erfolg[name] = self._cache_time
            self._cache = {"communities": list(self._daten)}

    async def async_fetch(self) -> dict | None:
        """Frisch holen, wenn fällig; ein Fehler behält die alte Reihe."""
        jetzt = _utcnow()
        if not self._quellen:
            self.letzter_fehler = "Keine OpenADR-Adresse eingetragen"
            return None
        faellig = [(n, b) for n, b in self._quellen if self._faellig(n, jetzt)]
        if faellig:
            geholt = False
            for name, basis in faellig:
                self._versuch[name] = jetzt
                try:
                    daten = await self.abrufen_einzeln(basis, jetzt)
                except OpenAdrFehler as err:
                    self._fehler[name] = err.grund
                    _LOGGER.warning("OpenADR '%s': %s, verwende Cache", name, err.grund)
                    continue
                self._daten[name] = daten
                self._erfolg[name] = jetzt
                geholt = True
                if daten.get("fehler"):
                    self._fehler[name] = daten["fehler"]
                else:
                    self._fehler.pop(name, None)
                if daten["warnings"]:
                    _LOGGER.warning("OpenADR '%s': %s", name, ", ".join(daten["warnings"]))
            # Was nicht mehr konfiguriert ist, fällt heraus.
            aktiv = {n for n, _ in self._quellen}
            for name in list(self._daten):
                if name not in aktiv:
                    self._daten.pop(name)
            for name in list(self._fehler):
                if name not in aktiv:
                    self._fehler.pop(name)
            self.letzter_fehler = "; ".join(
                f"{n}: {g}" for n, g in sorted(self._fehler.items())
            ) or None
            if geholt:
                neu = self._daten
                self._cache_time = jetzt
                self._cache = {"communities": list(neu)}
                if self._store is not None:
                    await self._store.async_save({
                        "fetched_at": jetzt.isoformat(),
                        "daten": {
                            n: {
                                "url": e["url"],
                                "ols_einspeisung": _zu_text(e["ols_einspeisung"]),
                                "ols_bezug": _zu_text(e["ols_bezug"]),
                                "preis_einspeisung": _zu_text(e["preis_einspeisung"]),
                                "preis_bezug": _zu_text(e["preis_bezug"]),
                                "warnings": e["warnings"],
                            }
                            for n, e in neu.items()
                        },
                    })
        if self._cache_time and (jetzt - self._cache_time).total_seconds() < CACHE_MAX_SECONDS:
            return self._cache
        return None

    def get_communities(self) -> list[str]:
        return list(self._daten)

    def get_intervals(self, community_name: str) -> list[dict]:
        """Für das Panel: je Viertelstunde die beiden OLS (kein ``saldoKwh``)."""
        eintrag = self._daten.get(community_name) or {}
        ein = eintrag.get("ols_einspeisung") or {}
        bez = eintrag.get("ols_bezug") or {}
        return [
            {
                "timestamp": datetime.fromtimestamp(v * VIERTEL_S, tz=timezone.utc)
                .isoformat().replace("+00:00", "Z"),
                "olsEinspeisung": ein.get(v),
                "olsBezug": bez.get(v),
            }
            for v in sorted(set(ein) | set(bez))
        ]

    def get_warnings(self, community_name: str) -> list[str]:
        return list((self._daten.get(community_name) or {}).get("warnings") or [])

    # -- Für den Fahrplan ---------------------------------------------------

    def ols(self, community_name: str, richtung: str) -> dict[int, float]:
        """``{Viertel: 0..1}``, ``richtung`` = ``einspeisung`` | ``bezug``."""
        return dict((self._daten.get(community_name) or {}).get(f"ols_{richtung}") or {})

    def preis(self, community_name: str, richtung: str) -> dict[int, float]:
        return dict((self._daten.get(community_name) or {}).get(f"preis_{richtung}") or {})


def tarife_ueberlagern(
    config: dict[str, Any], provider: Any, jetzt: datetime
) -> tuple[dict[str, Any], list[str]]:
    """Konstante Preise des VTN über die eingetragenen Sätze legen.

    Rückgabe: (Konfiguration, Hinweise). Ein Einspeisepreis ersetzt den
    Tagsatz und löscht den Nachtsatz (ein konstanter Preis HAT keinen
    eigenen Nachtsatz); ein Bezugspreis ersetzt ``peakshare_bezug_price``.
    Ein zeitvariabler Preis bleibt draußen — der Fahrplan kennt je
    Gemeinschaft nur Tag und Nacht, und ein erratener Satz wäre schlechter
    als der eingetragene.
    """
    if provider is None or not hasattr(provider, "preis"):
        return config, []
    neu = dict(config)
    hinweise: list[str] = []
    jetzt_v = int(jetzt.timestamp() // VIERTEL_S)
    for s in ("", "_2"):
        name = str(config.get(f"peakshare_community{s}") or "").strip()
        if not name:
            continue
        for richtung, ziel, wort in (
            ("einspeisung", "peakshare_price", "Einspeisepreis"),
            ("bezug", "peakshare_bezug_price", "Bezugspreis"),
        ):
            reihe = provider.preis(name, richtung)
            if not reihe:
                continue
            wert = konstanter_wert(reihe, jetzt_v)
            if wert is None:
                hinweise.append(
                    f"{name}: zeitvariabler {wort} wird noch nicht übernommen"
                )
                continue
            neu[ziel + s] = wert
            if richtung == "einspeisung":
                neu[f"peakshare_price_night{s}"] = None
    return neu, hinweise
