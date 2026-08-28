"""Fiktiver Einspeisetarif aus dem Saldo der Energiegemeinschaften.

Der Fahrplan steuert ausschließlich über Preise: was zu einer Stunde mehr wert
ist, wird dann eingespeist. Diese Datei rechnet den Gemeinschaftssaldo in
einen Preisauf- oder -abschlag um.

    Wert_i(t)       = Vergütung_i(t) + Gewichtung_i     (Tag- oder Nachtsatz)
    Signal_i(t)     = Bedarf_i(t)/Bedarfsspitze_i − Überschuss_i(t)/Überschussspitze_i
    Aufschlag_i(t)  = Anteil_i · (Wert_i(t) − Basistarif(t)) · Signal_i(t)
    feedin_price(t) = Basistarif(t) + Σ Aufschlag_i(t)

``Signal_i(t)`` liegt zwischen −1 und +1: Bedarf und Überschuss können im
selben Intervall nie beide positiv sein (siehe ``peakshare.py``), es ist
also immer genau einer der beiden Summanden wirksam.

Tag und Nacht gibt es auf beiden Seiten: eine Gemeinschaft kann zwei Sätze
haben, und der Basistarif ist entweder ein fester Wert oder der monatliche
Einspeisetarif der OeMAG (siehe ``oemag.py``). Verglichen wird immer, was zum
selben Zeitpunkt gilt — Tag gegen Tag, Nacht gegen Nacht. Sonst entstünde ein
Aufschlag allein daraus, dass zwei Tarife verschiedene Zeitstruktur haben.

Drei Entscheidungen stecken darin, alle drei am Solver gemessen:

* **Es zählt die Differenz zum Basistarif, nicht der ganze EEG-Tarif.** Eine
  eingespeiste Kilowattstunde ist entweder den EVU-Tarif wert oder den
  EEG-Tarif, nie die Summe aus beiden. Der Verzicht kostet praktisch nichts:
  ein Aufschlag bis 2 ct bringt 22 % der Einspeisung in die Bedarfsstunden der
  Gemeinschaft, ein Aufschlag bis 10 ct bringt 24 % (ohne Aufschlag: 4 %). Es
  zählt der zeitliche Verlauf, nicht die Höhe.

* **Normiert wird auf die Spitze — je Seite auf ihre eigene.** Damit erreicht
  der Aufschlag zur Spitzenstunde genau die Differenz zwischen EEG- und
  EVU-Tarif, der Abschlag zur tiefsten Überschussstunde genau dieselbe
  Differenz nach unten. Eine Obergrenze, die man erklären kann. Die
  Alternative (Normierung auf den Tagesverbrauch) ist derselbe Verlauf mit
  anderer Amplitude und ändert das Ergebnis um zwei Prozentpunkte.

  **Getrennte Spitzen sind die gleiche Behandlung, nicht die ungleiche.** Eine
  PV-starke Gemeinschaft hat mittags ein Vielfaches an Überschuss gegenüber
  ihrem nächtlichen Bedarf. Bei einer gemeinsamen Normierung bliebe vom
  Bedarf ein Rest nahe null — und der Bedarf ist die Größe, um die es der
  Integration geht.

* **Der Überschuss senkt den Preis unter den Basistarif.** Das ist bewusst
  eine Fiktion: real bekommt man in einer Überschussstunde den vollen
  EVU-Tarif, nicht weniger. Als Steuersignal ist es trotzdem richtig — die
  Kilowattstunde ist dort weniger wert *als in einer Bedarfsstunde*, und
  genau dieses Gefälle soll die Batterie füllen statt einspeisen lassen.
  Weil es eine Fiktion ist, gilt dasselbe wie beim entfallenen
  Mittagsabschlag: **Kosten am echten Preis bewerten**, sonst misst man die
  eigene Rechengröße.

* **Nach unten ist bei null Schluss.** Ein negativer Einspeisepreis macht
  Wegwerfen (``discard_p``) attraktiver als Verschenken — der Fahrplan würde
  Energie abregeln, statt sie einzuspeisen. Bei einem Basistarif von 6 ct und
  einer Tarifdifferenz bis 10 ct ist das erreichbar, nicht theoretisch.

* **Der Preis wird unter dem Bezugspreis gedeckelt.** Darüber kauft das LP
  Strom, um ihn im selben Slot teurer zu verkaufen — gemessen 5,49 kW Kauf
  gegen 9,40 kW Verkauf bei 0,60 kW Hauslast, in 63 von 193 Slots. Im
  Ergebnis unsichtbar, weil dort nur die Differenz steht, und schädlich: die
  Batterie wird dabei WENIGER entladen, weil die Einspeisegrenze schon vom
  Scheinhandel belegt ist. Solange nur echte Tarife eingehen, kann das nicht
  passieren (ein EEG-Tarif liegt unter dem Bezugspreis, und die Anteile
  summieren sich auf höchstens 1). Die Gewichtung ist aber kein echter Tarif,
  deshalb bleibt der Deckel als Sicherung gegen Fehleingaben.

Die Anteile sind der Aufteilungsschlüssel: Summe höchstens 1. Was nicht auf
eine Gemeinschaft entfällt, geht zum Basistarif an den Energieversorger — die
Formel gibt das von selbst richtig wieder, weil jede Gemeinschaft nur ihren
Anteil an der Differenz beiträgt.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# Sicherheitsabstand des Deckels zum Bezugspreis. Gleich groß wie der
# Rundungsschritt der Preisfelder im Panel (0,001 €/kWh), damit ein bewusst
# hoch gesetzter Wert nicht zufällig genau auf dem Bezugspreis landet.
DECKEL_ABSTAND = 0.001

@dataclass(frozen=True)
class Gemeinschaft:
    """Eine Energiegemeinschaft, wie die Preisfunktion sie braucht."""

    name: str
    anteil: float       # Aufteilungsschlüssel, 0..1
    wert_tag: float     # €/kWh: Vergütung + zusätzliche Gewichtung
    wert_nacht: float   # dasselbe für das Nachtfenster

    def wert(self, ist_nacht: bool) -> float:
        return self.wert_nacht if ist_nacht else self.wert_tag


def _roh_gemeinschaften(config: dict[str, Any]) -> list[tuple]:
    """Die Konfigurationsschlüssel der (bis zu zwei) Gemeinschaften an einem Ort."""
    return [
        (
            config.get("peakshare_community"),
            config.get("peakshare_share_pct"),
            config.get("peakshare_price"),
            config.get("peakshare_price_night"),
            config.get("peakshare_weight"),
        ),
        (
            config.get("peakshare_community_2"),
            config.get("peakshare_share_pct_2"),
            config.get("peakshare_price_2"),
            config.get("peakshare_price_night_2"),
            config.get("peakshare_weight_2"),
        ),
    ]


def gemeinschaften_aus_config(config: dict[str, Any]) -> list[Gemeinschaft]:
    """Liest die konfigurierten Gemeinschaften (derzeit bis zu zwei).

    Ohne Name, ohne Anteil oder ohne Mehrwert gegenüber dem Basistarif fällt
    ein Eintrag heraus — er würde nichts bewirken und nur Rechenzeit kosten.
    Anteil 0 ist bewusst die Vorgabe: ein Update soll nichts verändern, das
    der Nutzer nicht selbst eingestellt hat.
    """
    if config.get("enable_peakshare") is False:
        return []

    roh = _roh_gemeinschaften(config)

    ergebnis: list[Gemeinschaft] = []
    for name, anteil_pct, preis, preis_nacht, gewichtung in roh:
        if not name:
            continue
        anteil = _zahl(anteil_pct) / 100.0
        gew = _zahl(gewichtung)
        tag = _zahl(preis)
        # Leeres Nachtfeld heißt: derselbe Satz wie am Tag. Eine 0 wäre eine
        # echte Angabe und würde den Anreiz nachts löschen — fast nie gemeint.
        nacht = _zahl(preis_nacht) or tag
        # Wie in echte_tarife_aus_config: auch ein reiner Nachttarif hält die
        # Gemeinschaft am Leben, sonst steuert der Fahrplan nachts gar nicht.
        if anteil <= 0 or max(tag + gew, nacht + gew) <= 0:
            continue
        ergebnis.append(
            Gemeinschaft(
                name=str(name),
                anteil=anteil,
                wert_tag=tag + gew,
                wert_nacht=nacht + gew,
            )
        )
    return ergebnis


def echte_tarife_aus_config(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Die realen Vergütungssätze der Gemeinschaften — OHNE Gewichtung.

    Für die Gewinnberechnung zählen nur echte Geldflüsse: Anteil mal
    Vergütung, Tag oder Nacht. Die Gewichtung ist ein Steuersignal ohne
    Geldfluss, und die Bedarfs-Normierung des Aufschlags ist eine Fiktion —
    beides bleibt hier draußen (Lehre „Kosten am echten Preis bewerten").

    Anders als in ``gemeinschaften_aus_config`` hält ein Eintrag mit
    Vergütung 0 nichts am Leben: ohne echten Satz fließt kein Geld, der
    Anteil fällt an den Basistarif zurück.
    """
    if config.get("enable_peakshare") is False:
        return []

    ergebnis: list[dict[str, Any]] = []
    for name, anteil_pct, preis, preis_nacht, _gewichtung in _roh_gemeinschaften(
        config
    ):
        if not name:
            continue
        anteil = _zahl(anteil_pct) / 100.0
        tag = _zahl(preis)
        # Leeres Nachtfeld heißt wie oben: derselbe Satz wie am Tag.
        nacht = _zahl(preis_nacht) or tag
        # Beide Sätze prüfen: eine Gemeinschaft mit reinem NACHTtarif (Tagfeld
        # leer, das Panel schickt dafür 0) fiel sonst heraus, und ihr echter
        # Geldfluss fehlte in der Gewinnberechnung vollständig.
        if anteil <= 0 or max(tag, nacht) <= 0:
            continue
        ergebnis.append(
            {"name": str(name), "anteil": anteil, "tag": tag, "nacht": nacht}
        )
    return ergebnis


def anteile_summe(gemeinschaften: list[Gemeinschaft]) -> float:
    return sum(g.anteil for g in gemeinschaften)


def saldo_je_intervall(intervalle: list[dict] | None) -> dict[int, float]:
    """PeakShare-Viertelstunden auf ``{Epochenviertelstunde: kWh}`` abbilden.

    Der Wert trägt sein Vorzeichen aus ``peakshare.py``: positiv ist Bedarf,
    negativ Überschuss. Der Schlüssel ist die Viertelstunde seit der Epoche,
    nicht die Ortszeit — die Zeitstempel der API sind UTC, das Zeitraster des
    Fahrplans ist lokal, und über die Epoche treffen sich beide ohne
    Zeitzonenrechnung.
    """
    werte: dict[int, float] = {}
    for eintrag in intervalle or []:
        if not isinstance(eintrag, dict):
            continue
        roh = eintrag.get("saldoKwh")
        if roh is None:
            continue
        stempel = _stempel(eintrag.get("timestamp"))
        if stempel is None:
            continue
        try:
            werte[int(stempel.timestamp() // 900)] = float(roh)
        except (TypeError, ValueError):
            continue
    return werte


def aufschlag_reihe(
    gemeinschaften: list[Gemeinschaft],
    bedarf: dict[str, dict[int, float]],
    stamps: list[datetime],
    basis: float | list[float],
    ist_nacht: list[bool] | None = None,
) -> tuple[list[float], list[dict[str, Any]]]:
    """Preisaufschlag je Zeitpunkt, plus eine Aufschlüsselung für die Anzeige.

    ``basis`` ist der Basistarif: ein fester Wert oder eine Reihe je Zeitpunkt
    (der OeMAG-Tarif wechselt monatlich, ein Nachttarif stündlich).
    ``ist_nacht`` entscheidet je Zeitpunkt, welcher Satz der Gemeinschaft gilt.

    Rückgabe: (Aufschläge in €/kWh, je Gemeinschaft ein Diagnose-Eintrag).
    Ohne Bedarfsdaten bleibt der Aufschlag 0 — dann steuert wieder allein der
    Basistarif, und das ist richtig: eine fehlende Prognose darf keinen
    erfundenen Anreiz erzeugen.
    """
    anzahl = len(stamps)
    aufschlaege = [0.0] * anzahl
    basis_reihe = (
        list(basis) if isinstance(basis, (list, tuple)) else [float(basis)] * anzahl
    )
    if len(basis_reihe) < anzahl:      # defensiv: mit dem letzten Wert auffüllen
        fehlt = anzahl - len(basis_reihe)
        basis_reihe += [basis_reihe[-1] if basis_reihe else 0.0] * fehlt
    nacht_reihe = ist_nacht if ist_nacht is not None else [False] * anzahl
    diagnose: list[dict[str, Any]] = []

    for g in gemeinschaften:
        werte = bedarf.get(g.name) or {}
        # Jede Seite auf ihre eigene Spitze — Begründung im Modul-Docstring.
        bedarfsspitze = max((w for w in werte.values() if w > 0), default=0.0)
        ueberschussspitze = max((-w for w in werte.values() if w < 0), default=0.0)
        eintrag: dict[str, Any] = {
            "name": g.name,
            "anteil_pct": round(g.anteil * 100, 1),
            "wert_tag_ct": round(g.wert_tag * 100, 2),
            "wert_nacht_ct": round(g.wert_nacht * 100, 2),
            "spitze_kwh": round(bedarfsspitze, 1),
            "ueberschuss_spitze_kwh": round(ueberschussspitze, 1),
            "max_aufschlag_ct": 0.0,
            "max_abschlag_ct": 0.0,
            "stunden": len(werte),
        }
        if bedarfsspitze <= 0 and ueberschussspitze <= 0:
            eintrag["hinweis"] = "keine Bedarfsdaten"
            diagnose.append(eintrag)
            continue

        hoechster = 0.0
        tiefster = 0.0
        for i, stamp in enumerate(stamps):
            wert = werte.get(int(stamp.timestamp() // 900))
            if not wert:
                continue
            # Zahlt die Gemeinschaft nicht mehr als der Energieversorger, gibt
            # es nichts zu verschieben — dann wirkt sie in keine Richtung.
            differenz = g.wert(bool(nacht_reihe[i])) - basis_reihe[i]
            if differenz <= 0:
                continue
            if wert > 0:
                if bedarfsspitze <= 0:
                    continue
                signal = wert / bedarfsspitze
            else:
                if ueberschussspitze <= 0:
                    continue
                signal = wert / ueberschussspitze     # bleibt negativ
            zuschlag = g.anteil * differenz * signal
            aufschlaege[i] += zuschlag
            hoechster = max(hoechster, zuschlag)
            tiefster = min(tiefster, zuschlag)

        eintrag["max_aufschlag_ct"] = round(hoechster * 100, 2)
        eintrag["max_abschlag_ct"] = round(tiefster * 100, 2)
        if hoechster <= 0 and tiefster >= 0:
            eintrag["hinweis"] = "kein Mehrwert gegenüber dem Basistarif"
        diagnose.append(eintrag)

    return aufschlaege, diagnose


def mit_deckel(
    preise: list[float], bezugspreis: float
) -> tuple[list[float], int, float]:
    """Preise unter den Bezugspreis klemmen.

    Rückgabe: (gedeckelte Preise, Anzahl betroffener Zeitpunkte, höchster
    ungedeckelter Wert). Der Aufrufer soll das protokollieren — greift der
    Deckel, ist die Konfiguration zu erklären und nicht der Fahrplan.
    """
    if not preise or bezugspreis <= 0:
        return preise, 0, max(preise, default=0.0)

    grenze = bezugspreis - DECKEL_ABSTAND
    hoechster = max(preise)
    betroffen = sum(1 for p in preise if p > grenze)
    if not betroffen:
        return preise, 0, hoechster
    return [min(p, grenze) for p in preise], betroffen, hoechster


def mit_boden(
    preise: list[float], untergrenzen: list[float] | None = None
) -> tuple[list[float], int, float]:
    """Preise am Boden abfangen — standardmäßig bei null.

    Der Überschussabschlag kann den Einspeisepreis rechnerisch unter null
    drücken (großer Überschuss, kleiner Basistarif, große Tarifdifferenz).
    Ein negativer Preis kehrt die Logik um: ``opt()`` würde die Energie
    lieber wegwerfen (``discard_p``) als sie zu verschenken, und Abregeln ist
    für die Gemeinschaft in jedem Fall schlechter als eine Einspeisung, die
    nichts einbringt.

    ``untergrenzen`` erlaubt einen Boden je Zeitpunkt: bei einem ECHT
    negativen Basistarif (Spotpreis) ist der Boden dieser Basiswert — die
    Fiktion des Abschlags darf nicht weiter drücken, der echte Preis selbst
    bleibt aber negativ, und Abregeln ist dann genau richtig.

    Rückgabe: (angehobene Preise, Anzahl betroffener Zeitpunkte, tiefster
    ursprünglicher Wert). Wie beim Deckel gilt: greift der Boden, ist die
    Konfiguration zu erklären und nicht der Fahrplan.
    """
    if not preise:
        return preise, 0, 0.0

    boeden = (
        list(untergrenzen) if untergrenzen is not None else [0.0] * len(preise)
    )
    if len(boeden) < len(preise):
        boeden += [0.0] * (len(preise) - len(boeden))

    tiefster = min(preise)
    betroffen = sum(1 for p, b in zip(preise, boeden) if p < b)
    if not betroffen:
        return preise, 0, tiefster
    return [max(p, b) for p, b in zip(preise, boeden)], betroffen, tiefster


# ---------------------------------------------------------------------------
# Kleinkram
# ---------------------------------------------------------------------------


def _zahl(roh: Any) -> float:
    """Panel-Zahlenfeld lesen: leer und Unsinn ergeben 0."""
    if roh is None or roh == "":
        return 0.0
    try:
        return float(roh)
    except (TypeError, ValueError):
        return 0.0


def _stempel(roh: Any) -> datetime | None:
    """Zeitstempel der API lesen. Ohne Zone gilt UTC — so schreibt sie es."""
    stempel: datetime | None = None
    if isinstance(roh, datetime):
        stempel = roh
    elif isinstance(roh, str) and roh:
        try:
            stempel = datetime.fromisoformat(roh.replace("Z", "+00:00"))
        except ValueError:
            return None
    if stempel is None:
        return None
    return stempel if stempel.tzinfo else stempel.replace(tzinfo=timezone.utc)
