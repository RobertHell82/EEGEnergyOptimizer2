"""Wetterdaten von Open-Meteo für die eigene PV-Prognose.

Open-Meteo (open-meteo.com) liefert die Vorhersagen der nationalen
Wetterdienste ohne Schlüssel und ohne Registrierung; für Mitteleuropa im
15-Minuten-Raster aus dem ICON-D2 des DWD, darüber hinaus aus den gröberen
Modellen interpoliert. Gelesen wird je Fläche genau eine Strahlungsgröße: die
**Einstrahlung auf die geneigte Modulebene** (``global_tilted_irradiance``).
Open-Meteo rechnet sie aus Direkt- und Diffusstrahlung für Neigung und
Azimut selbst um — samt der Integration über das Intervall bei tiefem
Sonnenstand, die eine eigene Transposition erst nachbauen müsste. Dazu die
Lufttemperatur für die Zelltemperatur (``modell.py``).

Zwei Konventionen, die man leicht verwechselt:

* **Azimut**: Open-Meteo zählt 0° = Süd, −90° = Ost, +90° = West. Die
  Konfiguration hält den Kompasswert (0° = Nord, 90° = Ost, 180° = Süd,
  270° = West) — dieselbe Konvention wie ``sun.sun`` in Home Assistant und
  wie Forecast.Solar in HA. ``azimut_openmeteo`` rechnet um; am 27.09.2026
  live geprüft: −90° zeigt das Vormittagsmaximum, +90° das Nachmittagsmaximum.
* **Zeitstempel**: Jeder Wert ist das Mittel des **vorangehenden**
  Intervalls — der Wert zu 08:15 gilt für 08:00–08:15 (Open-Meteo-Doku:
  „preceding 15 minutes mean"). ``Wetterreihe`` trägt deshalb das
  Intervall-ENDE; ``modell.py`` legt die Werte beim Umrechnen auf die
  Slot-Anfänge, wie der Fahrplan sie erwartet.

Nutzungsbedingungen von Open-Meteo: kostenlos für nichtkommerzielle Nutzung,
Richtwert 10 000 Abrufe je Tag. Bei halbstündlichem Abruf sind das 48 je
Fläche und Tag.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

API_URL = "https://api.open-meteo.com/v1/forecast"
# Sieben Tage: der Fahrplan braucht 48 h, die Tagessensoren und das
# Wochendiagramm des Panels sieben Tage. Am 27.09.2026 live geprüft: 672
# Viertelstundenwerte je Abruf.
TAGE = 7
SCHRITT_MIN = 15
USER_AGENT = "EEG-Energy-Optimizer/Home-Assistant (github.com/RobertHell82/EEGEnergyOptimizer2)"


def azimut_openmeteo(kompass: float) -> float:
    """Kompass (0 = Nord, im Uhrzeigersinn) → Open-Meteo (0 = Süd, Ost negativ)."""
    wert = (float(kompass) - 180.0) % 360.0
    if wert > 180.0:
        wert -= 360.0
    return round(wert, 2)


def baue_url(
    breite: float, laenge: float, neigung: float, azimut_kompass: float, tage: int = TAGE
) -> str:
    """Abruf-URL für eine Fläche — Viertelstundenwerte in UTC."""
    params = {
        "latitude": f"{float(breite):.4f}",
        "longitude": f"{float(laenge):.4f}",
        "minutely_15": "global_tilted_irradiance,temperature_2m",
        "tilt": f"{float(neigung):.1f}",
        "azimuth": f"{azimut_openmeteo(azimut_kompass):.1f}",
        "forecast_days": str(int(tage)),
        "timezone": "UTC",
    }
    return API_URL + "?" + urlencode(params, safe=",")


@dataclass
class Wetterreihe:
    """Viertelstundenwerte einer Fläche — Zeitstempel = Intervall-ENDE (UTC)."""

    ende: list[datetime]
    gti_w_m2: list[float]
    temp_c: list[float | None]

    def __len__(self) -> int:
        return len(self.ende)


def parse_antwort(daten: Any) -> Wetterreihe:
    """Open-Meteo-Antwort zerlegen; ``ValueError`` bei jeder Abweichung."""
    if not isinstance(daten, dict):
        raise ValueError("Antwort ist kein JSON-Objekt")
    if daten.get("error"):
        raise ValueError(str(daten.get("reason") or "Open-Meteo meldet einen Fehler"))
    block = daten.get("minutely_15")
    if not isinstance(block, dict):
        raise ValueError("Antwort ohne minutely_15-Block")
    zeiten = block.get("time") or []
    gti = block.get("global_tilted_irradiance") or []
    temp = block.get("temperature_2m") or []
    if not zeiten:
        raise ValueError("Antwort ohne Zeitachse")
    if len(gti) != len(zeiten):
        raise ValueError("Strahlungsreihe passt nicht zur Zeitachse")

    ende: list[datetime] = []
    gti_werte: list[float] = []
    temp_werte: list[float | None] = []
    for i, roh in enumerate(zeiten):
        try:
            stamp = datetime.fromisoformat(str(roh))
        except ValueError as err:
            raise ValueError(f"Zeitstempel nicht lesbar: {roh!r}") from err
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        wert = gti[i]
        try:
            g = 0.0 if wert is None else max(0.0, float(wert))
        except (TypeError, ValueError):
            g = 0.0
        t_roh = temp[i] if i < len(temp) else None
        try:
            t = None if t_roh is None else float(t_roh)
        except (TypeError, ValueError):
            t = None
        ende.append(stamp)
        gti_werte.append(g)
        temp_werte.append(t)
    return Wetterreihe(ende=ende, gti_w_m2=gti_werte, temp_c=temp_werte)


async def hole_wetter(session: Any, url: str) -> Wetterreihe:
    """Eine Fläche abrufen. Wirft ``RuntimeError`` (Netz/HTTP) oder ``ValueError`` (Inhalt)."""
    import aiohttp

    async with session.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status != 200:
            grund = ""
            try:
                fehler = await resp.json(content_type=None)
                grund = str(fehler.get("reason") or "") if isinstance(fehler, dict) else ""
            except Exception:  # noqa: BLE001 — der Statuscode reicht als Grund
                grund = ""
            raise RuntimeError(f"HTTP {resp.status}" + (f": {grund}" if grund else ""))
        daten = await resp.json(content_type=None)
    return parse_antwort(daten)
