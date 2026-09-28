"""Kalibrierung der eigenen PV-Prognose an der eigenen Messung.

Das Modell (``modell.py``) kennt weder Horizont noch Verschattung, weder die
Eigenheiten eines Standorts (Dunst im Becken am Morgen) noch eine Ausrichtung,
die in Wahrheit ein paar Grad anders ist. All das steht in der Messung — und
es hängt am **Sonnenstand**, nicht an der Uhrzeit: um 9 Uhr steht die Sonne
im Dezember ganz woanders als im Juni, ein Baum im Südosten verschattet aber
immer dieselbe Himmelsgegend.

Deshalb EIN Korrekturfaktor je Sonnenstand-Feld (``AZIMUT_SCHRITT`` ×
``HOEHE_SCHRITT`` Grad): Summe gemessen durch Summe prognostiziert, also nach
Energie gewichtet. Felder mit wenig Energie werden zu 1 hin gezogen —
``(gemessen + K) / (prognose + K)`` mit ``SCHRUMPF_KWH`` als K: ein Feld muss
sich seinen Faktor erst verdienen. Zwischen den Feldern wird bilinear
interpoliert, nur über Felder mit Daten; ein Sonnenstand, den die Messung nie
gesehen hat (die tiefe Wintersonne aus Herbstdaten), bekommt 1.

Gelernt wird aus den Tagen des Prognosevergleichs (``prognosevergleich.py``):
die **unkalibrierte** Reihe (``eigen_roh`` — sonst lernte die Kalibrierung
ihre eigene Korrektur nach) gegen das Halbstundenmittel des PV-Sensors. Nur
Tage mit derselben ``eigen_kennung`` zählen: Flächen, Verluste und
Wettermodelle müssen dieselben sein, sonst korrigiert der Faktor einen
Fehler, den es nicht mehr gibt.

Was NICHT zählt, weil es nichts über die Module sagt:

* **Abregelung** — Einspeisung an der Exportgrenze (die Messung zeigt, was
  abgenommen wurde, nicht, was die Module konnten), und die Nähe der
  AC-Grenze (dort deckelt der Wechselrichter die Prognose wie die Messung).
  Ohne diesen Filter senkte die Kalibrierung die Prognose genau dort, wo die
  Sonne am stärksten ist.
* **Tage mit ganz anderem Wetter** (Tagesverhältnis außerhalb
  ``TAG_VERHAELTNIS``) — Wetterzufall, keine Eigenschaft der Anlage.
* **Kleine Werte** unter ``MIN_PROGNOSE_KW`` und einzelne Halbstunden mit
  Verhältnis außerhalb ``SLOT_VERHAELTNIS`` (eine Wolke).

Alles hier ist rein — keine HA-Objekte, keine Uhr, kein Netz.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

AZIMUT_SCHRITT = 10.0
HOEHE_SCHRITT = 5.0
SCHRUMPF_KWH = 3.0
FAKTOR_MIN = 0.6
FAKTOR_MAX = 1.5
# Ein Feld zählt für die Interpolation erst mit so viel prognostizierter Energie.
FELD_MIN_KWH = 0.5
MIN_PROGNOSE_KW = 0.3
SLOT_VERHAELTNIS = (1.0 / 3.0, 3.0)
TAG_VERHAELTNIS = (0.6, 1.6)
TAG_MIN_KWH = 1.0
# Abstand zu Export- und AC-Grenze, ab dem eine Halbstunde als abgeregelt gilt.
GRENZE_ABSTAND_KW = 0.5
# Unter dieser Exportgrenze (Nulleinspeisung) heißt „an der Grenze" nur dann
# abgeregelt, wenn die Batterie voll ist — sonst läge jede Halbstunde daran.
NULLEINSPEISUNG_KW = 1.0
SOC_VOLL_PCT = 95.0
SLOT_MIN = 30


def sonnenstand(t: datetime, breite: float, laenge: float) -> tuple[float, float]:
    """(Azimut Kompass 0 = Nord, Höhe) in Grad — NOAA-Näherung, auf ±0,5° genau."""
    t = t.astimezone(timezone.utc)
    tag = t.timetuple().tm_yday
    stunde = t.hour + t.minute / 60.0 + t.second / 3600.0
    g = 2.0 * math.pi / 365.0 * (tag - 1 + (stunde - 12.0) / 24.0)
    zeitgl = 229.18 * (
        0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
        - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g)
    )
    dekl = (
        0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
        - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
        - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g)
    )
    sonnenzeit = stunde * 60.0 + zeitgl + 4.0 * laenge
    h = math.radians(sonnenzeit / 4.0 - 180.0)
    phi = math.radians(breite)
    cos_zenit = math.sin(phi) * math.sin(dekl) + math.cos(phi) * math.cos(dekl) * math.cos(h)
    hoehe = 90.0 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zenit))))
    azimut = math.degrees(
        math.atan2(math.sin(h), math.cos(h) * math.sin(phi) - math.tan(dekl) * math.cos(phi))
    )
    return (azimut + 180.0) % 360.0, hoehe


def _feld(azimut: float, hoehe: float) -> tuple[int, int]:
    return int(azimut // AZIMUT_SCHRITT), int(hoehe // HOEHE_SCHRITT)


@dataclass
class Kalibrierung:
    """Faktoren je Sonnenstand-Feld — leer heißt überall 1."""

    breite: float
    laenge: float
    # Feld → [gemessen kWh, prognostiziert kWh]
    felder: dict[tuple[int, int], list[float]] = field(default_factory=dict)
    tage: int = 0
    slots: int = 0
    ausgelassen_abregelung: int = 0
    ausgelassen_tage: int = 0

    @property
    def aktiv(self) -> bool:
        return any(p >= FELD_MIN_KWH for _, p in self.felder.values())

    def feldfaktor(self, feld: tuple[int, int]) -> float | None:
        werte = self.felder.get(feld)
        if not werte or werte[1] < FELD_MIN_KWH:
            return None
        gemessen, prognose = werte
        faktor = (gemessen + SCHRUMPF_KWH) / (prognose + SCHRUMPF_KWH)
        return min(FAKTOR_MAX, max(FAKTOR_MIN, faktor))

    def faktor(self, t: datetime) -> float:
        """Faktor für den Zeitpunkt ``t`` (Mitte des Intervalls)."""
        if not self.felder:
            return 1.0
        azimut, hoehe = sonnenstand(t, self.breite, self.laenge)
        if hoehe <= 0:
            return 1.0
        # Bilinear zwischen den Feldmitten — nur über Felder mit Daten.
        x = azimut / AZIMUT_SCHRITT - 0.5
        y = hoehe / HOEHE_SCHRITT - 0.5
        x0, y0 = math.floor(x), math.floor(y)
        fx, fy = x - x0, y - y0
        summe = gewicht = 0.0
        for dx, wx in ((0, 1.0 - fx), (1, fx)):
            for dy, wy in ((0, 1.0 - fy), (1, fy)):
                w = wx * wy
                if w <= 0:
                    continue
                f = self.feldfaktor((int((x0 + dx) % (360 / AZIMUT_SCHRITT)), y0 + dy))
                if f is None:
                    continue
                summe += w * f
                gewicht += w
        return round(summe / gewicht, 4) if gewicht > 0 else 1.0

    def status(self) -> dict[str, Any]:
        faktoren = [f for f in (self.feldfaktor(k) for k in self.felder) if f is not None]
        return {
            "aktiv": self.aktiv,
            "tage": self.tage,
            "slots": self.slots,
            "felder": len(faktoren),
            "faktor_min": round(min(faktoren), 3) if faktoren else None,
            "faktor_max": round(max(faktoren), 3) if faktoren else None,
            "ausgelassen_abregelung": self.ausgelassen_abregelung,
            "ausgelassen_tage": self.ausgelassen_tage,
        }


def _abgeregelt(
    pv: float,
    prognose: float,
    netz: float | None,
    soc: float | None,
    export_grenze_kw: float | None,
    ac_limit_kw: float | None,
) -> bool:
    if ac_limit_kw and max(pv, prognose) >= ac_limit_kw - GRENZE_ABSTAND_KW:
        return True
    if export_grenze_kw is None or netz is None:
        return False
    if netz < export_grenze_kw - GRENZE_ABSTAND_KW:
        return False
    if export_grenze_kw >= NULLEINSPEISUNG_KW:
        return True
    return soc is None or soc >= SOC_VOLL_PCT


def lerne(
    tage: Iterable[dict[str, Any]],
    breite: float,
    laenge: float,
    kennung: str,
    export_grenze_kw: float | None,
    ac_limit_kw: float | None,
) -> Kalibrierung:
    """Kalibrierung aus den Tagesaufzeichnungen des Prognosevergleichs."""
    kal = Kalibrierung(breite=breite, laenge=laenge)
    halbe = timedelta(minutes=SLOT_MIN / 2)
    for tag in tage:
        if tag.get("eigen_kennung") != kennung:
            continue
        roh: dict[str, float] = tag.get("eigen_roh") or {}
        gemessen: dict[str, float] = tag.get("gemessen") or {}
        netz: dict[str, float] = tag.get("netz") or {}
        soc: dict[str, float] = tag.get("soc") or {}
        paare: list[tuple[str, float, float]] = []
        for slot, prognose in roh.items():
            pv = gemessen.get(slot)
            if pv is None or prognose < MIN_PROGNOSE_KW:
                continue
            if _abgeregelt(pv, prognose, netz.get(slot), soc.get(slot), export_grenze_kw, ac_limit_kw):
                kal.ausgelassen_abregelung += 1
                continue
            paare.append((slot, prognose, pv))
        summe_prognose = sum(p for _, p, _ in paare) * SLOT_MIN / 60.0
        if summe_prognose < TAG_MIN_KWH:
            continue
        verhaeltnis = sum(m for _, _, m in paare) * SLOT_MIN / 60.0 / summe_prognose
        if not TAG_VERHAELTNIS[0] <= verhaeltnis <= TAG_VERHAELTNIS[1]:
            kal.ausgelassen_tage += 1
            continue
        genutzt = 0
        for slot, prognose, pv in paare:
            if not SLOT_VERHAELTNIS[0] <= pv / prognose <= SLOT_VERHAELTNIS[1]:
                continue
            try:
                mitte = datetime.fromisoformat(slot) + halbe
            except ValueError:
                continue
            azimut, hoehe = sonnenstand(mitte, breite, laenge)
            if hoehe <= 0:
                continue
            werte = kal.felder.setdefault(_feld(azimut, hoehe), [0.0, 0.0])
            werte[0] += pv * SLOT_MIN / 60.0
            werte[1] += prognose * SLOT_MIN / 60.0
            genutzt += 1
        if genutzt:
            kal.tage += 1
            kal.slots += genutzt
    return kal
