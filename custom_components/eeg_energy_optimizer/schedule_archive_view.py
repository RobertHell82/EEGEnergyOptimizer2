"""HTTP-Endpunkt, der das Fahrplan-Archiv als ZIP ausliefert.

Getrennt von ``schedule_archive.py``, weil dort nichts aus Home Assistant
importiert wird — das Archiv selbst ist ohne HA testbar, dieser Endpunkt
nicht.

Aufgerufen wird er über einen **signierten Pfad**: das Panel holt sich per
WebSocket eine URL mit begrenzter Gültigkeit und öffnet sie. Ein gewöhnlicher
Link würde am fehlenden Authorization-Header scheitern, den ein Download
nicht mitschicken kann.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.http import HomeAssistantView
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .schedule_archive import AUFBEWAHRUNG_TAGE, async_ist_verlauf

_LOGGER = logging.getLogger(__name__)

ARCHIV_URL = "/api/eeg_optimizer/plaene.zip"


class ScheduleArchiveView(HomeAssistantView):
    """Liefert alle archivierten Fahrpläne samt Ist-Verlauf als ZIP."""

    url = ARCHIV_URL
    name = "api:eeg_optimizer:plaene"
    # Signierte Pfade authentifizieren sich selbst; ohne Signatur greift die
    # normale Anmeldung.
    requires_auth = True

    async def get(self, request):
        """ZIP bauen und ausliefern."""
        from aiohttp import web

        hass = request.app["hass"]
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return web.Response(status=404, text="Keine Konfiguration gefunden")

        entry = entries[0]
        archiv = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get(
            "schedule_archive"
        )
        if archiv is None:
            return web.Response(status=404, text="Archiv nicht aktiv")

        jetzt = dt_util.now()
        ist = None
        try:
            ist = await async_ist_verlauf(
                hass, entry.entry_id, jetzt - timedelta(days=AUFBEWAHRUNG_TAGE), jetzt
            )
        except Exception:  # noqa: BLE001 - ohne Ist-Verlauf ist das ZIP trotzdem brauchbar
            _LOGGER.warning("Ist-Verlauf nicht lesbar", exc_info=True)
            ist = {"fehler": "Verlauf nicht lesbar", "reihen": {}}

        try:
            daten = await archiv.async_build_zip(ist)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Fahrplan-Archiv konnte nicht gepackt werden")
            return web.Response(status=500, text="Archiv konnte nicht gepackt werden")

        name = f"fahrplaene-{jetzt.strftime('%Y%m%d-%H%M')}.zip"
        return web.Response(
            body=daten,
            content_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )


def async_register_archive_view(hass) -> None:
    """Endpunkt einmal je Start registrieren."""
    flag = f"{DOMAIN}_archive_view_registered"
    if hass.data.get(flag):
        return
    hass.http.register_view(ScheduleArchiveView())
    hass.data[flag] = True


def async_signed_url(hass, refresh_token_id: str | None = None, minuten: int = 10) -> str | None:
    """Signierte, zeitlich begrenzte URL für den Download-Knopf im Panel.

    ``async_sign_path`` braucht die Anmeldung, für die signiert wird. Im
    WebSocket-Handler steht sie in der Verbindung — sie mitzugeben ist
    verlässlicher, als sich auf den Kontext zu verlassen.
    """
    try:
        from homeassistant.components.http.auth import async_sign_path
    except ImportError:
        return None
    try:
        if refresh_token_id:
            return async_sign_path(
                hass, ARCHIV_URL, timedelta(minutes=minuten),
                refresh_token_id=refresh_token_id,
            )
        return async_sign_path(hass, ARCHIV_URL, timedelta(minutes=minuten))
    except Exception:  # noqa: BLE001 - ohne Signatur bleibt der Knopf aus
        _LOGGER.debug("Signierter Pfad nicht verfügbar", exc_info=True)
        return None
