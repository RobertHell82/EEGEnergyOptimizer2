"""Prognosetage an die EEG-Statistik — der Prognosevergleich im Backend.

Was die Karte „Prognosevergleich“ auf der Anlage zeigt (gemessene PV, was die
Fremdquelle und die eigene Berechnung am Morgen für den Tag vorhergesagt
haben), liegt fertig im Speicher des Prognosevergleichs
(``prognosevergleich.py``). Dieses Modul schickt jeden abgeschlossenen Tag an
``/v1/forecast``, damit das Backend die Quellen über alle Anlagen vergleichen
kann — Tagessummen und die 30-Minuten-Reihen für die Detailansicht.

Versand wie bei den Bilanztagen (``bilanz_telemetrie.TageVersand``): gemerkt
wird, welche Tage das Backend hat, der Rest geht stündlich hinaus — nach dem
Update das ganze Archiv (bis zu 400 Tage), danach jeden Tag der neue.

Gesendet werden nur Tage, die sich nicht mehr ändern (vollständig gemessen
oder zwei Tage alt): ein halber Tag wäre als gemeldet vermerkt und käme nie
vollständig nach.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .bilanz_telemetrie import TageVersand
from .prognosevergleich import QUELLE_EIGEN, QUELLE_FREMD, SLOT_MIN, statistik_tag

ENDPUNKT = "/v1/forecast"
# Ein Tag trägt vier Reihen à 48 Werte — kleinere Pakete als die Bilanztage,
# damit ein gepufferter Aufruf den Ringpuffer nicht aufbläht. Das Backend
# nimmt höchstens 100 (FORECAST_MAX_DAYS).
PAKET_TAGE = 50


def _zahl(wert: Any, stellen: int = 3) -> float | None:
    if isinstance(wert, bool) or not isinstance(wert, (int, float)):
        return None
    return round(float(wert), stellen)


def _reihe(slots: list[str], werte: dict[str, float] | None) -> list[float | None] | None:
    if not werte:
        return None
    return [_zahl(werte.get(s)) for s in slots]


def tag_payload(datum: str, tag: dict[str, Any]) -> dict[str, Any]:
    """Ein Prognosetag in der Form, die ``/v1/forecast`` erwartet.

    Kennzahlen aus ``statistik_tag`` — dieselbe Rechnung wie die Karte im
    Panel. Die eigene Prognose ohne Kalibrierung (``eigen_roh``) läuft mit
    derselben Rechnung, damit sich zeigt, was die Kalibrierung bringt.
    """
    stat = statistik_tag(tag)
    roh = statistik_tag({**tag, QUELLE_FREMD: None, QUELLE_EIGEN: tag.get("eigen_roh")})
    fremd = stat["quellen"].get(QUELLE_FREMD) or {}
    eigen = stat["quellen"].get(QUELLE_EIGEN) or {}
    eigen_roh = roh["quellen"].get(QUELLE_EIGEN) or {}
    slots: list[str] = tag.get("slots") or []
    return {
        "date": datum,
        "complete": bool(tag.get("vollstaendig")),
        "late": bool(tag.get("spaet")),
        "slot_start": slots[0] if slots else None,
        "slot_minutes": SLOT_MIN,
        "actual_kwh": _zahl(stat.get("gemessen_kwh")) if tag.get("gemessen") else None,
        "foreign_source": tag.get("fremd_name") if fremd else None,
        "foreign_kwh": _zahl(fremd.get("prognose_kwh")),
        "foreign_mae_kw": _zahl(fremd.get("mae_kw")),
        "foreign_at": tag.get("festgehalten_fremd") if fremd else None,
        "own_kwh": _zahl(eigen.get("prognose_kwh")),
        "own_mae_kw": _zahl(eigen.get("mae_kw")),
        "own_at": tag.get("festgehalten_eigen") if eigen else None,
        "own_raw_kwh": _zahl(eigen_roh.get("prognose_kwh")),
        "own_raw_mae_kw": _zahl(eigen_roh.get("mae_kw")),
        "series": {
            "actual": _reihe(slots, tag.get("gemessen")),
            "foreign": _reihe(slots, tag.get(QUELLE_FREMD)),
            "own": _reihe(slots, tag.get(QUELLE_EIGEN)),
            "own_raw": _reihe(slots, tag.get("eigen_roh")),
        },
    }


class PrognoseVersand(TageVersand):
    """Die abgeschlossenen Tage des Prognosevergleichs an ``/v1/forecast``."""

    SPEICHER = "prognose_telemetrie"
    PAKET = PAKET_TAGE
    NAME = "Prognose-Telemetrie"

    def payload(self, datum: str, tag: dict[str, Any]) -> dict[str, Any]:
        return tag_payload(datum, tag)

    async def _senden(self, reporter: Any, payloads: list[dict[str, Any]]) -> None:
        await reporter.send_forecast_days(payloads)

    async def async_melden(
        self, reporter: Any, kennung: str | None, vergleich: Any,
        jetzt: datetime | None = None,
    ) -> int:
        if vergleich is None:
            return 0
        return await self.async_senden(reporter, kennung, vergleich.abgeschlossene_tage(jetzt))
