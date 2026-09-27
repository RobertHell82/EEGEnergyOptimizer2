"""PV-Modell: aus Einstrahlung und Lufttemperatur wird Wechselrichterleistung.

Bewusst das einfache Modell nach Art von PVWatts, denn die Unsicherheit der
Wetterprognose ist um ein Vielfaches größer als die eines Modulmodells:

    P_DC = kWp · GTI / 1000 · (1 + γ · (T_Zelle − 25 °C))
    T_Zelle = T_Luft + k · GTI
    P_AC = Σ P_DC · (1 − Verluste) , gedeckelt auf die AC-Grenze

mit γ = −0,4 %/K (kristallines Silizium) und k = 0,03 K·m²/W (aus einer
NOCT von 45 °C: (45 − 20) / 800). Die Verluste (Vorgabe 14 %, der PVWatts-
Wert) fassen Verschmutzung, Leitung, Mismatch, Wechselrichter und Alterung
zusammen. Mehrere Flächen werden als Gleichstrom addiert und einmal
gedeckelt — bei zwei Wechselrichtern gilt die Summe ihrer AC-Grenzen, und
die trägt ``inverter_ac_limit_kw`` ohnehin.

Was das Modell NICHT kennt und wo die Prognose deshalb systematisch
danebenliegen kann: Verschattung durch Horizont, Bäume oder Gauben, Schnee
auf den Modulen, eine Ost-West-Anlage auf einem Wechselrichter mit engem
MPPT-Fenster. Für genau diese Fälle ist eine Kalibrierung an der eigenen
Erzeugungshistorie vorgesehen (siehe Modul-Doku in ``provider.py``); bis
dahin gilt: die Tagessummen stimmen erfahrungsgemäß, der Verlauf im
Verschattungsfall nicht.

Alle Funktionen hier sind rein — keine HA-Objekte, keine Uhr, kein Netz.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from .openmeteo import SCHRITT_MIN, Wetterreihe

TEMP_KOEFF_PRO_K = -0.004
TEMP_ANSTIEG_K_PRO_W_M2 = 0.03
STC_W_M2 = 1000.0
# Fehlt die Temperatur in der Antwort (kommt vor, wenn ein Modell den
# Zeitraum nicht abdeckt), gilt ein gemäßigter Tageswert.
TEMP_VORGABE_C = 15.0

FLAECHEN_MAX = 8
VERLUSTE_VORGABE_PCT = 14.0
NEIGUNG_VORGABE = 30.0
AZIMUT_VORGABE = 180.0

_SCHRITT = timedelta(minutes=SCHRITT_MIN)


@dataclass(frozen=True)
class Flaeche:
    """Eine Modulfläche: Leistung, Neigung (0 = flach), Azimut (Kompass)."""

    name: str
    kwp: float
    neigung: float
    azimut: float
    # Optionale AC-Grenze dieser Fläche: ihr eigener Wechselrichter oder
    # MPP-Tracker. Ohne sie deckelt nur die Summe (inverter_ac_limit_kw) —
    # bei zwei Wechselrichtern schneidet real aber jeder für sich ab.
    max_kw: float | None = None

    def als_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kwp": self.kwp,
            "neigung": self.neigung,
            "azimut": self.azimut,
            "max_kw": self.max_kw,
        }


def pruefe_flaechen(roh: Any) -> tuple[list[dict[str, Any]], str | None]:
    """Konfigurationswert prüfen und normieren.

    Rückgabe: (normierte Liste, Fehlertext) — der Fehlertext ist ``None``,
    wenn alles stimmt. Gedacht für ``save_config``: Was hier durchgeht,
    liest ``flaechen_aus_config`` später ohne weitere Prüfung.
    """
    if not isinstance(roh, list) or not roh:
        return [], "Mindestens eine PV-Fläche (kWp, Neigung, Azimut) ist nötig"
    if len(roh) > FLAECHEN_MAX:
        return [], f"Höchstens {FLAECHEN_MAX} PV-Flächen"
    sauber: list[dict[str, Any]] = []
    for i, eintrag in enumerate(roh, start=1):
        if not isinstance(eintrag, dict):
            return [], f"PV-Fläche {i}: ungültiger Eintrag"
        try:
            kwp = float(eintrag.get("kwp"))
        except (TypeError, ValueError):
            return [], f"PV-Fläche {i}: Leistung (kWp) fehlt"
        if not 0.05 <= kwp <= 500:
            return [], f"PV-Fläche {i}: Leistung muss zwischen 0,05 und 500 kWp liegen"
        try:
            neigung = float(
                NEIGUNG_VORGABE if eintrag.get("neigung") in (None, "") else eintrag.get("neigung")
            )
            azimut = float(
                AZIMUT_VORGABE if eintrag.get("azimut") in (None, "") else eintrag.get("azimut")
            )
        except (TypeError, ValueError):
            return [], f"PV-Fläche {i}: Neigung oder Azimut nicht lesbar"
        if not 0 <= neigung <= 90:
            return [], f"PV-Fläche {i}: Neigung muss zwischen 0 und 90 Grad liegen"
        if not -360 < azimut < 720:
            return [], f"PV-Fläche {i}: Azimut außerhalb des gültigen Bereichs"
        roh_max = eintrag.get("max_kw")
        max_kw: float | None = None
        if roh_max not in (None, ""):
            try:
                max_kw = float(roh_max)
            except (TypeError, ValueError):
                return [], f"PV-Fläche {i}: Grenze (kW) nicht lesbar"
            if not 0.1 <= max_kw <= 500:
                return [], f"PV-Fläche {i}: Grenze muss zwischen 0,1 und 500 kW liegen"
        name = str(eintrag.get("name") or "").strip()[:40] or f"Fläche {i}"
        sauber.append(
            {
                "name": name,
                "kwp": round(kwp, 3),
                "neigung": round(neigung, 1),
                "azimut": round(azimut % 360, 1),
                "max_kw": None if max_kw is None else round(max_kw, 3),
            }
        )
    return sauber, None


def flaechen_aus_config(config: dict[str, Any]) -> list[Flaeche]:
    """Die konfigurierten Flächen — unlesbare Einträge werden übergangen."""
    from ..const import CONF_PV_FLAECHEN

    roh = config.get(CONF_PV_FLAECHEN) or []
    if not isinstance(roh, list):
        return []
    ergebnis: list[Flaeche] = []
    for i, eintrag in enumerate(roh, start=1):
        if not isinstance(eintrag, dict):
            continue
        try:
            kwp = float(eintrag.get("kwp") or 0.0)
            neigung = float(
                NEIGUNG_VORGABE if eintrag.get("neigung") in (None, "") else eintrag.get("neigung")
            )
            azimut = float(
                AZIMUT_VORGABE if eintrag.get("azimut") in (None, "") else eintrag.get("azimut")
            )
        except (TypeError, ValueError):
            continue
        if kwp <= 0:
            continue
        roh_max = eintrag.get("max_kw")
        max_kw: float | None
        try:
            max_kw = None if roh_max in (None, "") else float(roh_max)
        except (TypeError, ValueError):
            max_kw = None
        if max_kw is not None and max_kw <= 0:
            max_kw = None
        ergebnis.append(
            Flaeche(
                name=str(eintrag.get("name") or f"Fläche {i}"),
                kwp=kwp,
                neigung=min(90.0, max(0.0, neigung)),
                azimut=azimut % 360.0,
                max_kw=max_kw,
            )
        )
    return ergebnis


def verluste_aus_config(config: dict[str, Any]) -> float:
    from ..const import CONF_PV_VERLUSTE_PCT

    roh = config.get(CONF_PV_VERLUSTE_PCT)
    if roh in (None, ""):
        return VERLUSTE_VORGABE_PCT
    try:
        return min(60.0, max(0.0, float(roh)))
    except (TypeError, ValueError):
        return VERLUSTE_VORGABE_PCT


def dc_kw(gti_w_m2: float, temp_c: float | None, kwp: float) -> float:
    """Gleichstromleistung einer Fläche bei Einstrahlung und Lufttemperatur."""
    if gti_w_m2 <= 0 or kwp <= 0:
        return 0.0
    t_luft = TEMP_VORGABE_C if temp_c is None else temp_c
    t_zelle = t_luft + TEMP_ANSTIEG_K_PRO_W_M2 * gti_w_m2
    faktor = 1.0 + TEMP_KOEFF_PRO_K * (t_zelle - 25.0)
    return max(0.0, kwp * gti_w_m2 / STC_W_M2 * faktor)


@dataclass
class Leistungsreihe:
    """AC-Leistung der ganzen Anlage — Zeitstempel = Intervall-ENDE (UTC)."""

    ende: list[datetime]
    kw: list[float]

    def __len__(self) -> int:
        return len(self.ende)

    def leer(self) -> bool:
        return not self.ende


def leistungsreihe(
    paare: list[tuple[Flaeche, Wetterreihe]],
    verluste_pct: float,
    ac_limit_kw: float | None,
) -> Leistungsreihe:
    """Flächen addieren, Verluste abziehen, deckeln — je Fläche und gesamt.

    Die Verluste gehen je Fläche ab (linear, also dasselbe wie ein Faktor
    auf die Summe), damit die optionale Grenze je Fläche (``max_kw``, ihr
    eigener Wechselrichter) auf AC-Leistung wirkt; danach deckelt die
    AC-Grenze der ganzen Anlage die Summe. Gerechnet wird nur an
    Zeitpunkten, die JEDE Fläche liefert — eine Fläche mit kürzerer Reihe
    würde sonst als „0 kW" in die Summe eingehen.
    """
    if not paare:
        return Leistungsreihe([], [])
    faktor = max(0.0, 1.0 - float(verluste_pct) / 100.0)
    summen: dict[datetime, float] = {}
    zaehler: dict[datetime, int] = {}
    for flaeche, wetter in paare:
        grenze = flaeche.max_kw if flaeche.max_kw and flaeche.max_kw > 0 else None
        for ende, gti, temp in zip(wetter.ende, wetter.gti_w_m2, wetter.temp_c):
            p = dc_kw(gti, temp, flaeche.kwp) * faktor
            if grenze is not None:
                p = min(p, grenze)
            summen[ende] = summen.get(ende, 0.0) + p
            zaehler[ende] = zaehler.get(ende, 0) + 1
    n = len(paare)
    deckel = float(ac_limit_kw) if ac_limit_kw and float(ac_limit_kw) > 0 else None
    zeiten = sorted(t for t, c in zaehler.items() if c == n)
    kw: list[float] = []
    for t in zeiten:
        p = summen[t]
        if deckel is not None:
            p = min(p, deckel)
        kw.append(round(p, 4))
    return Leistungsreihe(ende=zeiten, kw=kw)


def slot_anfaenge(reihe: Leistungsreihe) -> dict[datetime, float]:
    """Vom Intervall-Ende auf den Slot-Anfang: 08:15 → gilt ab 08:00."""
    return {ende - _SCHRITT: kw for ende, kw in zip(reihe.ende, reihe.kw)}


def halbstunden(reihe: Leistungsreihe) -> dict[datetime, float]:
    """Halbstundenmittel, Schlüssel = Anfang der halben Stunde (UTC).

    Das ist das Raster, das der Fahrplan von Solcast gewohnt ist
    (``period_start`` + Mittelwert der Periode); ``opt()`` interpoliert
    daraus selbst auf 15 Minuten.
    """
    gruppen: dict[datetime, list[float]] = {}
    for anfang, kw in slot_anfaenge(reihe).items():
        halb = anfang - timedelta(
            minutes=anfang.minute % 30, seconds=anfang.second, microseconds=anfang.microsecond
        )
        gruppen.setdefault(halb, []).append(kw)
    return {t: round(sum(v) / len(v), 4) for t, v in sorted(gruppen.items())}


def energie_kwh(reihe: Leistungsreihe, von: datetime, bis: datetime) -> float:
    """Energie im Fenster [von, bis) — angeschnittene Slots anteilig."""
    if bis <= von:
        return 0.0
    gesamt = 0.0
    for ende, kw in zip(reihe.ende, reihe.kw):
        if kw <= 0:
            continue
        anfang = ende - _SCHRITT
        ueberlappung = (min(ende, bis) - max(anfang, von)).total_seconds()
        if ueberlappung > 0:
            gesamt += kw * ueberlappung / 3600.0
    return round(gesamt, 3)


def tagessummen(
    reihe: Leistungsreihe,
    jetzt: datetime,
    lokal: Callable[[datetime], datetime],
    tage: int = 7,
) -> list[float]:
    """kWh je lokalem Kalendertag, Index 0 = heute (ganzer Tag)."""
    heute = lokal(jetzt).replace(hour=0, minute=0, second=0, microsecond=0)
    return [
        energie_kwh(reihe, heute + timedelta(days=i), heute + timedelta(days=i + 1))
        for i in range(tage)
    ]


def rest_heute_kwh(
    reihe: Leistungsreihe, jetzt: datetime, lokal: Callable[[datetime], datetime]
) -> float:
    """Was heute ab jetzt noch kommt — das Gegenstück zu Solcasts „verbleibend"."""
    mitternacht = lokal(jetzt).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return energie_kwh(reihe, jetzt, mitternacht)
