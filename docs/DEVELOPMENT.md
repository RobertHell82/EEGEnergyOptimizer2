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
   `inverter/base.py` ist `False`). Das ist der Hauptschalter: Ohne ihn
   schreibt der `ScheduleExecutor` nichts. Zwei weitere Eigenschaften fragt
   er ab, wenn er steuert — `discharge_is_grid_setpoint` (nimmt die
   Entladung einen Netz-Sollwert wie bei SMA statt Batterieleistung? Davon
   hängt ab, was Guard 2 und Guard 3 vorgeben bzw. messen) und
   `supports_grid_charge` (nur dann wird eine Netzladung geschrieben, sonst
   ein Ladelimit). Die Schnittstelle im Einzelnen steht in
   [NECESSARY_SENSORS_NEW_INVERTER.md](../NECESSARY_SENSORS_NEW_INVERTER.md).
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
- Wirkungskontrolle (Guard 3, `GUARD_WIRKUNG_*`): Kommt eine befohlene
  Entladung nicht an, stoppt die Steuerung den Modus und setzt ihn neu auf.
  Eine Entladung, die ankommt, darf Guard 3 nicht auslösen — verglichen
  wird mit dem Batterieleistungs-Sensor, bei `discharge_is_grid_setpoint`
  mit dem Netzzähler; Vorzeichen und Einheit müssen also stimmen
- Freigabe beim Start: Nach einem Neustart im Modus Ein nimmt der erste
  Lauf die Steuerwerte der Vorsession zurück, bevor er den Plan schreibt
  (bis zu `START_RELEASE_MAX_VERSUCHE` Versuche)
- Failsafe gibt frei (Fahrplan fehlt länger als 15 Minuten)
- Modus Ein → Aus nimmt alle Steuerwerte zurück, auch beim wiederholten
  Umschalten

Welche Sensoren ein Treiber lesen und welche Schnittstelle er anbieten muss,
steht in [NECESSARY_SENSORS_NEW_INVERTER.md](../NECESSARY_SENSORS_NEW_INVERTER.md).

## Tests

- **Python:** `pytest` im Repo-Wurzelverzeichnis (`pyproject.toml`,
  `asyncio_mode = "auto"`). Home-Assistant-Importe sind abgefangen, die
  Tests laufen ohne HA-Installation. `optlang` aus `requirements_test.txt`
  braucht nur der GLPK-Vergleich in `tests/test_chamo_highs_adapter.py`.
- **Panel:** `tests/js/*.test.mjs` mit dem eingebauten Test-Runner von Node,
  einzeln `node --test tests/js/<datei>`. `tests/js/panel_harness.mjs` lädt
  die echte Panel-Datei in einen `vm`-Kontext mit DOM-Attrappen und einer
  falschen HA-Verbindung, deren Antworten der Test einzeln freigibt.
  `tests/test_panel_js.py` führt alle unter pytest aus und wird ohne Node
  übersprungen. `EEG_PANEL_DATEI=<pfad>` prüft eine andere Fassung des
  Panels (etwa einen älteren Stand).

## Neue Version

„Neue Version“ heißt die ganze Kette, am Stück:

1. **Version** in `custom_components/eeg_energy_optimizer/manifest.json`
   erhöhen und oben in `CHANGELOG.md` einen Abschnitt
   `## [X.Y.Z] - JJJJ-MM-TT` anlegen (der Inhalt von `[Unreleased]` wandert
   dorthin).
2. **Zwei Commits** auf `main`: erst der Inhalts-Commit (`feat(…)` /
   `fix(…)`), dann der Release-Commit `chore(release): X.Y.Z — <Titel>` mit
   nur `CHANGELOG.md` und `manifest.json`.
3. **Push** nach `origin/main`.
4. **Annotierter Tag** `vX.Y.Z` mit der Nachricht `X.Y.Z — <Titel>`,
   mitpushen.
5. **GitHub-Release** (`gh release create`), Name = Tag-Nachricht, Text =
   Kurzfassung des CHANGELOG-Abschnitts. Betas (`X.Y.Z-beta.N`) als
   Prerelease.

### Netzladen: in Betas an, in Releases aus

„Batterie aus dem Netz laden“ ist Beta. Zwei Schalter werden immer
gemeinsam umgelegt: `NETZLADEN_FREIGEGEBEN` in `schedule.py` (Sperre in
`netzladen_max_kw`) und `NETZLADEN_SICHTBAR` im Panel.

- **Stabiles Release:** vor dem Release-Commit beide auf `false`, den
  Abschnitt „Batterie aus dem Netz laden“ aus `docs/guides/tarife.md`
  entfernen und `python scripts/build_guides.py` laufen lassen — als eigener
  Commit (Beispiel: `ffe9747`).
- **Nächste Beta:** beide wieder auf `true`, den Abschnitt aus Git
  zurückholen, Guides neu bauen (Beispiel: `5601904`).

Die Tests in `tests/test_netzladen.py` schalten die Funktion per Fixture
ein und laufen deshalb in beiden Zuständen; `test_zurueckgezogen_wirkt_nicht`
hält fest, dass die Sperre im ausgeschalteten Zustand greift.

## Weitere interne Dokumente

- [Telemetrie-/Reporting-Konzept](reporting-concept.md) — historisches Konzeptpapier; was heute gesendet wird, steht im Abschnitt „EEG-Statistik“ der [README](../README.md)
