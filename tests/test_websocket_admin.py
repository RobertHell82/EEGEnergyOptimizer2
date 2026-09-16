"""Admin-Absicherung der WebSocket-Befehle (websocket_api.py).

Home Assistant reicht einen `websocket_command` an **jeden angemeldeten
Benutzer** weiter — `ActiveConnection.async_handle` prüft keine Rechte. Wer
eine Berechtigung will, muss `@websocket_api.require_admin` setzen; HA-Core
macht das für alles, was einen Config-Entry ändert (`config_entries/update`
und Geschwister). Das Panel dieser Integration ist bewusst mit
`require_admin=False` registriert, damit Nicht-Admins das Dashboard sehen —
die schreibenden Befehle dürfen damit aber nicht offenstehen.

Diese Tests schreiben die Einstufung jedes einzelnen Befehls fest. Kommt ein
neuer dazu, schlägt `test_jeder_befehl_ist_eingestuft` fehl, bis jemand
entschieden hat, in welche Gruppe er gehört — das ist der eigentliche Zweck
der Datei.

Gelesen wird der **Quelltext** per `ast`, nicht das importierte Modul. Der
Umweg ist nötig: In der Testumgebung ist `voluptuous` ein MagicMock, und
`vol.Required("type")` liefert dasselbe Objekt wie `vol.Required("config")`
(der return_value eines MagicMock hängt nicht an den Argumenten). Ein Schema
mit mehreren Schlüsseln kollabiert damit zur Laufzeit auf einen einzigen
dict-Eintrag, und die Kommando-Kennung wäre weg. Am Quelltext steht sie
unverfälscht — und dass ein Decorator dort steht, ist ohnehin genau das, was
hier geprüft werden soll.
"""
from __future__ import annotations

import ast
import pathlib

from custom_components.eeg_energy_optimizer import websocket_api

QUELLE = pathlib.Path(websocket_api.__file__)

# Schreibend, nach außen verbindend, Hardware ohne HA-Entsprechung steuernd
# oder die Einwilligung ändernd — diese Befehle gehören Admins:
#
# - save_config          schreibt den Config-Entry (HA-Core: admin-only)
# - detect_sensors       Einrichtungshelfer, liest die Entity-Registry aus
# - probe_*              öffnet Modbus-TCP zu einem frei wählbaren Host
# - ambibox_manual       einziger Schreibpfad zur Wallbox, kein Entity-Pendant
# - refresh_consumption_profile  schreibt das Profil neu, teure Recorder-Abfrage
# - telemetry_*          Einwilligung in die Datenübermittlung
GESICHERT = {
    "eeg_optimizer/save_config",
    "eeg_optimizer/detect_sensors",
    "eeg_optimizer/probe_fronius",
    "eeg_optimizer/probe_kostal",
    "eeg_optimizer/probe_sma",
    "eeg_optimizer/probe_ambibox",
    "eeg_optimizer/ambibox_manual",
    "eeg_optimizer/refresh_consumption_profile",
    "eeg_optimizer/telemetry_enable",
    "eeg_optimizer/telemetry_disable",
    "eeg_optimizer/telemetry_forget",
}

# Bewusst offen. Zwei Gründe, je nach Befehl:
#
# 1. Lesend — das Dashboard muss für Nicht-Admins funktionieren, sonst wäre
#    `require_admin=False` am Panel sinnlos.
# 2. set_override / clear_override / refresh_schedule / tagesbilanz_jetzt
#    steuern zwar, bringen einem Nicht-Admin aber nichts Neues: die Pause
#    hängt an den Services `eeg_energy_optimizer.pause` / `.aufheben`, die mit
#    `hass.services.async_register` registriert sind und damit ohnehin jedem
#    offenstehen — und die Batterie lässt sich über `number.set_value` am
#    Ladelimit direkt steuern (HA-Standardrichtlinie: volle Entity-Rechte für
#    normale Benutzer). Ein Gate hier würde die Bedienung einschränken, ohne
#    etwas zu schützen.
OFFEN = {
    "eeg_optimizer/get_config",
    "eeg_optimizer/check_prerequisites",
    "eeg_optimizer/get_activity_log",
    "eeg_optimizer/get_netzentgelte",
    "eeg_optimizer/get_peakshare_communities",
    "eeg_optimizer/get_peakshare_data",
    "eeg_optimizer/get_bilanz",
    "eeg_optimizer/get_override",
    "eeg_optimizer/set_override",
    "eeg_optimizer/clear_override",
    "eeg_optimizer/get_oemag_tarif",
    "eeg_optimizer/get_spot_preis",
    "eeg_optimizer/get_awattar_sunny",
    "eeg_optimizer/get_energie_ag",
    "eeg_optimizer/telemetry_get_status",
    "eeg_optimizer/get_schedule",
    "eeg_optimizer/refresh_schedule",
    "eeg_optimizer/get_feedin_statistics",
    "eeg_optimizer/tagesbilanz_jetzt",
    "eeg_optimizer/get_schedule_archive",
    "eeg_optimizer/get_entity_ids",
    "eeg_optimizer/get_control_state",
}


def _kommando_aus_dekorator(dek) -> str | None:
    """Die Kommando-Kennung aus `@websocket_api.websocket_command({...})`."""
    if not isinstance(dek, ast.Call):
        return None
    if getattr(dek.func, "attr", None) != "websocket_command":
        return None
    for arg in dek.args:
        if not isinstance(arg, ast.Dict):
            continue
        for wert in arg.values:
            if (
                isinstance(wert, ast.Constant)
                and isinstance(wert.value, str)
                and wert.value.startswith("eeg_optimizer/")
            ):
                return wert.value
    return None


def _befehle() -> dict[str, bool]:
    """Alle Befehle der Datei als {Kommando: admin-gesichert?}."""
    baum = ast.parse(QUELLE.read_text(encoding="utf-8"))
    gefunden: dict[str, bool] = {}
    for knoten in ast.walk(baum):
        if not isinstance(knoten, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        kommando = None
        admin = False
        for dek in knoten.decorator_list:
            kommando = kommando or _kommando_aus_dekorator(dek)
            if isinstance(dek, ast.Attribute) and dek.attr == "require_admin":
                admin = True
        if kommando is not None:
            gefunden[kommando] = admin
    return gefunden


def test_handler_werden_ueberhaupt_gefunden():
    """Schutz vor einem stillen Fehlschlag: findet die Auswertung nichts,
    wären alle folgenden Tests trivial grün."""
    gefunden = _befehle()
    assert len(gefunden) >= 30, sorted(gefunden)


def test_jeder_befehl_ist_eingestuft():
    """Ein neuer Befehl muss bewusst einsortiert werden — gesichert oder offen.

    Ohne diesen Test rutschte ein neuer schreibender Befehl ungeschützt durch,
    weil das Fehlen eines Decorators nichts meldet.
    """
    gefunden = set(_befehle())
    eingestuft = GESICHERT | OFFEN
    assert gefunden - eingestuft == set(), (
        "Nicht eingestufte Befehle — bitte in GESICHERT oder OFFEN aufnehmen: "
        f"{sorted(gefunden - eingestuft)}"
    )
    assert eingestuft - gefunden == set(), (
        "Eingestufte Befehle, die es nicht mehr gibt: "
        f"{sorted(eingestuft - gefunden)}"
    )


def test_schreibende_befehle_sind_admin_gesichert():
    gefunden = _befehle()
    ungesichert = sorted(k for k in GESICHERT if not gefunden.get(k))
    assert ungesichert == [], (
        "@websocket_api.require_admin fehlt bei: " + ", ".join(ungesichert)
    )


def test_lesende_befehle_bleiben_offen():
    """Sonst sieht ein Nicht-Admin das Dashboard nicht mehr."""
    gefunden = _befehle()
    zu_streng = sorted(k for k in OFFEN if gefunden.get(k))
    assert zu_streng == [], (
        "Unerwartet admin-gesichert — das Panel bricht für Nicht-Admins: "
        + ", ".join(zu_streng)
    )


def test_save_config_ist_gesichert():
    """Der wichtigste Einzelfall, ausdrücklich benannt.

    `ws_save_config` mischt `msg["config"]` ungefiltert in `entry.data` und
    schreibt das mit `async_update_entry` — darüber ließen sich
    Wechselrichtertyp, Modbus-Host, Mindest-Ladestand und Einspeisegrenze
    dauerhaft verstellen.
    """
    assert _befehle()["eeg_optimizer/save_config"] is True
