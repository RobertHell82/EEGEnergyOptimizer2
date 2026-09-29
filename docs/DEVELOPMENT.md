# Entwickler-Hinweise: Dokumentation

> Diese Datei richtet sich an Entwickler. Die Enduser-Dokumentation startet in [README.md](README.md).

## Synchronisation mit dem Panel

Die Dateien in `docs/guides/` und `docs/images/` sind die **Single Source of Truth** für die In-App-Anleitungen des Onboarding-Panels.

- **Bearbeiten:** Immer nur die Markdown-Dateien in `docs/guides/` ändern — niemals die generierten Dateien in `custom_components/eeg_energy_optimizer/frontend/guide/`.
- **Generieren:** Nach Änderungen `python scripts/build_guides.py` ausführen (benötigt `pip install markdown`). Das Script konvertiert die Markdown-Dateien zu HTML-Fragmenten und kopiert die Bilder in den Panel-Ordner.
- **Prüfen:** `python scripts/build_guides.py --check` schlägt fehl, wenn Quelle und generierte Dateien nicht übereinstimmen (läuft auch als GitHub Action bei jedem Push/PR).

Die Installations-Anleitungen (`docs/installation/`) existieren nur in `docs/` — sie haben kein Panel-Gegenstück.

## Markdown-Konventionen in den Guides

| Markdown | Darstellung im Panel |
|---|---|
| `# Titel` (genau eine H1) | Dialog-Überschrift |
| `## / ###` | Abschnitts-/Unterüberschriften |
| `> [!WARNING]` Blockquote | Orange Warnbox |
| `> [!NOTE]` Blockquote | Blaue Infobox |
| `> [!CAUTION]` Blockquote | Rote Pflicht-/Fehlerbox |
| `> [!IMPORTANT]` Blockquote | wie `CAUTION` (rote Box) |
| `> [!TIP]` Blockquote | wie `NOTE` (blaue Box) |
| `_kursiv_` | Grauer Sekundärtext (Hinweise) |
| `![alt](../images/...)` | Bild (Pfad wird automatisch umgeschrieben) |
| Tabellen, Listen, Links, `code`, `<br>` | wie üblich |

## Einen Wechselrichter freigeben

Welche Geräte unterstützt werden und wie weit sie erprobt sind, steht für
Anwender in [wechselrichter-status.md](wechselrichter-status.md). Ein neuer
Treiber wird an drei Stellen freigeschaltet, in dieser Reihenfolge:

1. **Backend** — `custom_components/eeg_energy_optimizer/inverter/<treiber>.py`:
   Property `supports_schedule_control` auf `True` setzen (Default in
   `inverter/base.py` ist `False`). Das ist der einzige Schalter, den der
   `ScheduleExecutor` abfragt.
2. **Einrichtungsassistent** — `frontend/eeg-optimizer-panel.js`: Die Liste
   `SCHEDULE_CONTROL_INVERTERS` steuert beides — welche Karte im Assistenten
   erscheint und ob der Hinweis „nur Anzeige" gezeigt wird. Ein bereits
   konfigurierter Treiber außerhalb der Liste bleibt unabhängig davon sichtbar.
3. **Doku** — die Zeile in der Tabelle von `wechselrichter-status.md`
   eintragen (nur dort steht der Stand der Erprobung) und das Gerät in allen
   Listen ergänzen, die es nennen: die Tabellen in `README.md`,
   `docs/README.md` und `docs/deployment/inbetriebnahme.md` (Schritt 5), im
   Panel die Willkommensseite („Was du brauchst“, „Getestete Setups“) und
   `INVERTER_LABELS`. Danach `python scripts/build_guides.py` laufen lassen.

Vor dem Schritt von „Feldtest“ zu „freigegeben“ an einer echten Anlage
nachweisen:

- Ladelimit setzen, nachführen und wieder zurücknehmen
- Erzwungene Entladung mit Ziel-Ladestand starten und stoppen
- Not-Aus greift (Netzbezug während der Entladung)
- Failsafe gibt frei (Fahrplan fehlt länger als 15 Minuten)
- Modus Ein → Aus nimmt alle Steuerwerte zurück, auch beim wiederholten
  Umschalten

Welche Sensoren ein Treiber lesen und welche Schnittstelle er anbieten muss,
steht in [NECESSARY_SENSORS_NEW_INVERTER.md](../NECESSARY_SENSORS_NEW_INVERTER.md).

## Weitere interne Dokumente

- [Telemetrie-/Reporting-Konzept](reporting-concept.md) — historisches Konzeptpapier; was heute gesendet wird, steht im Abschnitt „EEG-Statistik“ der [README](../README.md)
