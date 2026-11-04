"""Messkorrektur der PV-Prognose für die nächsten Stunden („Smart Persistence“).

Die Wettermodelle sehen Hochnebel, rechnen darunter aber zu viel Strahlung:
Traun 03.11.2026 — 100 % tiefe Wolken in allen drei Modellen, trotzdem
100–180 W/m², die eigene Prognose 13,0 kWh, gemessen 1,8. Grünbach
04.11.2026 den ganzen Tag gleichmäßig 35–40 % der Prognose. Der Plan verließ
sich auf diese Sonne, auch bei der Mindestladung am Abend.

Die Messung weiß es besser, und zwar sofort: Jeder Planlauf legt eine Probe
ab (gemessene PV gegen die Prognose für jetzt), über die letzte Stunde wird
das Verhältnis gebildet, und die Prognose der nächsten ``ABKLINGEN_H``
Stunden wird damit skaliert — voll gleich nach jetzt, linear abnehmend bis
zur unveränderten Prognose. Weiter hinaus weiß die Messung von jetzt nichts
mehr: Ein Nebel, der um 9 Uhr liegt, sagt wenig über 14 Uhr und nichts über
morgen.

Proben, in denen die Anlage abgeregelt haben könnte, zählen nur, wenn die
Messung trotzdem über der Prognose lag — sonst hielte eine volle Batterie
mit Einspeisegrenze die Prognose für zu hoch. Dieselbe Regel wie die
Kalibrierung (``pvprognose.kalibrierung._abgeregelt``).

Rein, ohne hass: Der Puffer liegt wie ``_messmittel`` in ``hass.data``, nach
einem Neuladen beginnt er leer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

# Über dieses Fenster wird das Verhältnis gebildet.
FENSTER_S = 3600
# So alt muss die älteste brauchbare Probe mindestens sein — eine Wolke von
# fünf Minuten ist noch kein Wetter.
MIN_ABDECKUNG_S = 1200
MIN_PROBEN = 10
# Unter diesem Prognose-Mittel (kW) gibt es nichts zu korrigieren: Morgens
# und abends ist das Verhältnis Rauschen.
MIN_PROGNOSE_KW = 0.3
FAKTOR_MIN = 0.2
FAKTOR_MAX = 2.0
# Nach so vielen Stunden gilt wieder die Prognose allein.
ABKLINGEN_H = 3.0


def probe_ablegen(
    puffer: list,
    jetzt: datetime,
    gemessen_kw: float | None,
    prognose_kw: float | None,
    abgeregelt: bool,
) -> None:
    """Probe dieses Planlaufs ablegen, Proben außerhalb des Fensters verwerfen.

    Ein Zeitsprung rückwärts verwirft sie ebenfalls.
    """
    puffer[:] = [
        p for p in puffer if 0 <= (jetzt - p[0]).total_seconds() < FENSTER_S
    ]
    if gemessen_kw is None or prognose_kw is None:
        return
    puffer.append((jetzt, max(0.0, float(gemessen_kw)), max(0.0, float(prognose_kw)), bool(abgeregelt)))


def faktor(puffer: Sequence, jetzt: datetime) -> float | None:
    """Gemessen ÷ Prognose über die brauchbaren Proben — None, wenn es dafür
    nicht reicht (zu wenig Proben, zu kurz, zu wenig Sonne)."""
    brauchbar = [
        p for p in puffer
        if 0 <= (jetzt - p[0]).total_seconds() < FENSTER_S
        and (not p[3] or p[1] >= p[2])
    ]
    if len(brauchbar) < MIN_PROBEN:
        return None
    if max((jetzt - p[0]).total_seconds() for p in brauchbar) < MIN_ABDECKUNG_S:
        return None
    prognose = sum(p[2] for p in brauchbar)
    if prognose / len(brauchbar) < MIN_PROGNOSE_KW:
        return None
    verhaeltnis = sum(p[1] for p in brauchbar) / prognose
    return max(FAKTOR_MIN, min(FAKTOR_MAX, verhaeltnis))


def anwenden(
    reihe: list[float],
    stempel: Sequence[datetime],
    jetzt: datetime,
    wert: float,
    obergrenze_kw: float | None = None,
) -> list[float]:
    """Die Reihe ab dem zweiten Stützpunkt skalieren, abklingend bis
    ``ABKLINGEN_H`` nach jetzt. Der erste Stützpunkt ist schon die Messung."""
    ergebnis = list(reihe)
    for k in range(1, len(ergebnis)):
        stunden = (stempel[k] - jetzt).total_seconds() / 3600.0
        gewicht = max(0.0, 1.0 - stunden / ABKLINGEN_H)
        if gewicht <= 0.0:
            break
        neu = ergebnis[k] * (1.0 + (wert - 1.0) * gewicht)
        if obergrenze_kw:
            neu = min(neu, float(obergrenze_kw))
        ergebnis[k] = round(neu, 4)
    return ergebnis
