"""Bilanztage an die EEG-Statistik — die Zahlen der Einspeise-Karte im Backend.

Was die Karte „Einspeisung“ auf der Anlage zeigt (eingespeist, davon aus der
Batterie, davon an die Gemeinschaft, dazu dieselben Mengen im simulierten
Standardbetrieb), liegt fertig bewertet im Tagesarchiv der Energiebilanz
(``bilanz.py``). Dieses Modul schickt es an ``/v1/balance``, damit das Backend
dieselbe Ansicht über die ganze Flotte summieren kann.

**Keine Geldbeträge.** Erlös, Ersparnis und Optimierungs-Vorteil bleiben auf
der Anlage — gesendet werden nur Energiemengen, der Zeitanteil im Modus Ein und
zwei Ja/Nein-Werte. ``tag_payload`` ist die einzige Stelle, die entscheidet, was
hinausgeht; sie nimmt Felder ausdrücklich auf, statt das Tagesergebnis
durchzureichen, damit ein neues Geldfeld dort nie aus Versehen mitreist.

**Ein Mechanismus für Nachlieferung und Alltag.** Gemerkt wird, welche Tage
das Backend schon hat. Einmal pro Stunde (und kurz nach dem Start) gehen alle
übrigen hinaus, in Paketen zu ``PAKET_TAGE``: nach dem Update das ganze Archiv
(bis zu 400 Tage), danach jeden Morgen der eine neue Tag, und ein Tag, dessen
Versand ausfiel, kommt beim nächsten Durchlauf von selbst. Das Backend
überschreibt je (Anlage, Datum), doppelt Gesendetes schadet nicht.

**Gebunden an die Kennung der Anlage.** Nach „Daten löschen“ bekommt die Anlage
eine neue Kennung, und das Backend kennt keinen ihrer Tage mehr — der Merker
gilt deshalb nur für die Kennung, unter der er entstand.
"""

from __future__ import annotations

import logging
from typing import Any

from .const import DOMAIN

try:  # pragma: no cover - im Test nicht vorhanden
    from homeassistant.helpers.storage import Store
except ImportError:  # pragma: no cover
    Store = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

ENDPUNKT = "/v1/balance"
# Höchstzahl je Aufruf — dieselbe Grenze prüft das Backend (BALANCE_MAX_DAYS).
PAKET_TAGE = 100


def _zahl(wert: Any) -> float | None:
    if isinstance(wert, bool) or not isinstance(wert, (int, float)):
        return None
    return round(float(wert), 3)


def tag_payload(datum: str, ergebnis: dict[str, Any]) -> dict[str, Any]:
    """Ein Bilanztag in der Form, die ``/v1/balance`` erwartet.

    Fehlt ein Feld im Tagesergebnis (Tage vor 2.1.26 kennen die
    Batterie-Einspeisung nicht), geht ``None`` hinaus statt einer Null — eine
    Null wäre eine Messung, die es nicht gab. ``no_intervention`` ist nur dort
    eine Aussage, wo es eine Referenz gab.
    """
    hat_referenz = ergebnis.get("ref_export_kwh") is not None
    kein_eingriff = ergebnis.get("kein_eingriff")
    quote = ergebnis.get("quotenmodus")
    return {
        "date": datum,
        "export_kwh": _zahl(ergebnis.get("export_kwh")),
        "battery_export_kwh": _zahl(ergebnis.get("batterie_export_kwh")),
        "community_kwh": _zahl(ergebnis.get("eeg_kwh")),
        "pv_kwh": _zahl(ergebnis.get("pv_kwh")),
        "import_kwh": _zahl(ergebnis.get("bezug_kwh")),
        "house_kwh": _zahl(ergebnis.get("haus_kwh")),
        "heater_kwh": _zahl(ergebnis.get("heizstab_kwh")),
        "self_consumed_kwh": _zahl(ergebnis.get("eigen_kwh")),
        "mode_on_share": _zahl(ergebnis.get("ein_anteil")),
        "no_intervention": bool(kein_eingriff) if hat_referenz else None,
        "quota_mode": quote if isinstance(quote, bool) else None,
        "ref_export_kwh": _zahl(ergebnis.get("ref_export_kwh")),
        "ref_battery_export_kwh": _zahl(ergebnis.get("ref_batterie_export_kwh")),
        "ref_community_kwh": _zahl(ergebnis.get("ref_eeg_kwh")),
    }


class BilanzVersand:
    """Merkt sich die gemeldeten Tage und schickt die übrigen."""

    def __init__(self, hass: Any, entry_id: str) -> None:
        self._store: Any = (
            Store(hass, 1, f"{DOMAIN}_{entry_id}_bilanz_telemetrie")
            if Store is not None else None
        )
        self._kennung: str | None = None
        self._gesendet: set[str] = set()
        self._geladen = False

    async def _laden(self) -> None:
        if self._geladen:
            return
        self._geladen = True
        if self._store is None:
            return
        try:
            daten = await self._store.async_load() or {}
        except Exception:  # noqa: BLE001
            daten = {}
        self._kennung = daten.get("installation_id")
        self._gesendet = set(daten.get("gesendet") or [])

    async def _speichern(self) -> None:
        if self._store is None:
            return
        try:
            await self._store.async_save({
                "installation_id": self._kennung,
                "gesendet": sorted(self._gesendet),
            })
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Bilanz-Telemetrie: Merker nicht speicherbar: %s", err)

    async def async_senden(
        self, reporter: Any, kennung: str | None, tage: dict[str, dict[str, Any]]
    ) -> int:
        """Alle noch nicht gemeldeten Tage schicken. Liefert die Zahl der Tage.

        Gesendet wird nur, wenn die EEG-Statistik eingerichtet, eingeschaltet
        und registriert ist — sonst bliebe der Aufruf im Reporter wirkungslos,
        und hier würde fälschlich „gemeldet“ vermerkt. Scheitert der Versand
        selbst, legt ihn der Reporter in seinen Puffer und liefert ihn nach;
        als gemeldet gilt der Tag dann trotzdem.
        """
        if not kennung or not getattr(reporter, "is_configured", False):
            return 0
        if not getattr(reporter, "is_enabled", False):
            return 0
        await self._laden()
        if kennung != self._kennung:
            self._kennung = kennung
            self._gesendet = set()
        # Nur Tage merken, die es im Archiv noch gibt — sonst wüchse der Merker
        # über die 400 Tage hinaus unbegrenzt.
        vorher = len(self._gesendet)
        self._gesendet &= set(tage)
        offen = sorted(d for d in tage if d not in self._gesendet)
        if not offen:
            if len(self._gesendet) != vorher:
                await self._speichern()
            return 0
        gesendet = 0
        for i in range(0, len(offen), PAKET_TAGE):
            paket = offen[i:i + PAKET_TAGE]
            try:
                await reporter.send_balance([tag_payload(d, tage[d]) for d in paket])
            except Exception:  # noqa: BLE001 - Telemetrie darf nie den Takt kippen
                _LOGGER.exception("Bilanz-Telemetrie: Senden fehlgeschlagen")
                break
            self._gesendet.update(paket)
            gesendet += len(paket)
        await self._speichern()
        if gesendet:
            _LOGGER.info("Bilanz-Telemetrie: %d Bilanztag(e) gemeldet", gesendet)
        return gesendet
