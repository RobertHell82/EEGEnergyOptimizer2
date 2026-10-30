# SolaX Modbus einrichten

## 1. Unterstützte Wechselrichter

Nur **Gen4, Gen5 und Gen6**. Ältere Geräte (Gen2/Gen3) haben keine Remote Control (Fernsteuerung).

## 2. Wechselrichter-Einstellungen

Stelle am Wechselrichter oder in der SolaX-App ein:

| Einstellung | Wert |
|---|---|
| **Work Mode** | **Self Use** (charger_use_mode = 0) |
| **Night Charge** | **Aus** — sonst lädt die Batterie nachts aus dem Netz |
| **Smart Schedule / Zeitplan** | **Aus** — stört die Optimizer-Steuerung |

> [!WARNING]
> **Wichtig:** Der Work Mode darf **NICHT** auf „Feedin Priority" oder „Force Time Use" stehen! Der Optimizer steuert über Remote Control (Mode 1) und braucht dafür den Self Use Modus.

## 3. Verbindung zum Wechselrichter — was du brauchst

Home Assistant redet mit dem Wechselrichter über **Modbus TCP** im Heimnetz, nicht über die SolaX-Cloud. _Modbus TCP ist eine Sprache für Geräte im Netzwerk._ **Nicht jeder Stick kann das** — das ist der häufigste Grund, wenn die Einrichtung scheitert:

| Verbindung | Funktioniert? |
|---|---|
| **Pocket WiFi 2.0** (ältere WLAN-Sticks) | **Nein** — kein Modbus TCP |
| **Pocket WiFi 3.0** | Nur mit aktueller Firmware; nicht immer zuverlässig |
| **Pocket LAN** (Stick mit Netzwerkkabel) | Ja |
| **RS485-zu-LAN-Adapter** am COM-/RS485-Anschluss des Wechselrichters (z. B. Waveshare) | Ja, am zuverlässigsten — am besten vom Elektriker anschließen lassen |

Du brauchst außerdem die **IP-Adresse** (Adresse im Heimnetz) des Sticks bzw. Adapters. Sie steht in der Geräteliste deines Routers (z. B. FRITZ!Box: *Heimnetz → Netzwerk*). Stelle dort „immer dieselbe IP-Adresse zuweisen" ein, damit sie sich nicht ändert.

> [!TIP]
> **Welchen Stick hast du?** Er steckt unten am Wechselrichter, das Modell steht auf dem Aufkleber. Im Zweifel schick uns ein Foto davon.

## 4. Integration installieren

**Erst prüfen, ob sie schon da ist:** **Einstellungen → Geräte & Dienste** → rechts unten **„+ Integration hinzufügen"** → **„SolaX"** eintippen. Erscheint **„SolaX Inverter Modbus"**, ist sie installiert — weiter mit Abschnitt 5.

Sonst installiere sie über HACS (Zusatz-Store für Home Assistant):

_**Voraussetzung:** [HACS](https://hacs.xyz/) muss installiert sein._

1. Gehe zu **HACS** und suche nach **„SolaX Inverter Modbus"**.<br>
   _Repository: `wills106/homeassistant-solax-modbus`_
2. Installiere die Integration und **starte Home Assistant neu** (Einstellungen → oben rechts die drei Punkte → **Neu starten**).

> [!WARNING]
> Ohne Neustart meldet Home Assistant beim Hinzufügen **„Konfigurationsablauf konnte nicht geladen werden"**. Dann neu starten und noch einmal versuchen.

## 5. Integration konfigurieren

1. **Einstellungen → Geräte & Dienste → „+ Integration hinzufügen"**
2. Suche nach **„SolaX Inverter Modbus"**.
3. Verbindungsart: **TCP / Ethernet**
4. Gib die **IP-Adresse** des Sticks bzw. Adapters ein (→ Abschnitt 3).
5. Port: **502** (Standard für Modbus TCP), Modbus-Adresse: **1**
6. Mit **OK** bzw. **Absenden** bestätigen. Die Integration erkennt den Wechselrichter selbst.

Bei einer Fehlermeldung: siehe **Häufige Probleme** unten.

## 6. Batterie-Kapazität

SolaX hat **keinen Sensor für die Batteriekapazität**. Du gibst sie später im Wizard von Hand ein (z. B. 5.8 kWh für eine T-BAT 5.8).

## 7. Zweiter Wechselrichter am Generator-Eingang

Hängt am SolaX-Hybrid ein **zweiter Wechselrichter als Generator** (z. B. eine bestehende PV-Anlage am Generator-/Meter-2-Eingang)? Dann zählt dessen Erzeugung **nicht** im normalen PV-Sensor (`sensor.solax_*solar_power`), sondern nur über **Meter 2**.

Den Sensor dafür musst du erst aktivieren — er ist ab Werk **deaktiviert**:

1. **Einstellungen → Geräte & Dienste → SolaX Inverter Modbus**
2. Klicke auf dein **Wechselrichter-Gerät**.
3. Klicke in der Liste der Entitäten (einzelne Werte und Schalter) auf **„+ x Entitäten sind deaktiviert"**.
4. Suche **„Meter 2 Measured Power"**.
5. Entität anklicken → Zahnrad → **Aktiviert** → **Aktualisieren**
6. Warte ca. 30 Sekunden, bis der Sensor Werte liefert.

_Der Sensor heißt meist `sensor.solax_inverter_meter_2_measured_power` (Prefix je Installation anders) und zeigt die Leistung in W._

> [!NOTE]
> **Voraussetzung:** Meter 2 muss auch am Wechselrichter bzw. in der SolaX-App als Generator-/zweiter Zähler eingerichtet sein. Sonst zeigt der Sensor immer 0.

Im Assistenten trägst du ihn im Feld **„Zweiter PV-Sensor (optional)"** ein. Das Feld gibt es nur im **Expertenmodus** (Schalter oben rechts im Assistenten). Es ist schon ausgefüllt, wenn der Sensor **vor** der Sensorerkennung aktiv war. Hast du ihn erst danach aktiviert, klicke auf **„Erneut prüfen"** oder trag ihn von Hand ein. Der Optimizer zählt diesen Wert zur PV-Leistung des Hybrid-Wechselrichters dazu.

> [!WARNING]
> Fehlt der Sensor, rechnet der Optimizer mit **zu wenig PV-Leistung**. Hausverbrauch und Fahrplan stimmen dann nicht.

## 8. Prüfen

1. Unter **Einstellungen → Integrationen** zeigt SolaX Inverter Modbus **„geladen"**.
2. Unter **Entwicklerwerkzeuge → Zustände** zeigt `sensor.solax_*battery_capacity` den Ladestand (0–100%).
3. Es gibt einen Button `button.solax_*remotecontrol_trigger*` (neuere Versionen hängen den Modus an, z. B. `…_remotecontrol_trigger_mode_1_7`). Dann ist Remote Control verfügbar.
4. Kehre hierher zurück. Der Wechselrichter wird automatisch erkannt.

_**Hinweis:** Der Anfang der Entity-Namen ist je Installation anders (z. B. `solax_inverter_` statt `solax_`). Der Optimizer erkennt das automatisch._

## Häufige Probleme

| Problem | Lösung |
|---|---|
| **„Konfigurationsablauf konnte nicht geladen werden"** | Integration frisch installiert, aber Home Assistant nicht neu gestartet → neu starten (→ Abschnitt 4) |
| **„Verbindung fehlgeschlagen" / Connection refused / Zeitüberschreitung** | IP-Adresse falsch oder geändert → im Router nachsehen. Stimmt sie, kann der Stick vermutlich kein Modbus TCP (Pocket WiFi 2.0, alte 3.0-Firmware) → Abschnitt 3 |
| **SolaX taucht bei „Integration hinzufügen" nicht auf** | Integration nicht installiert → Abschnitt 4 |
| **EEG-Assistent: „SolaX Modbus Integration muss zuerst installiert werden"** | Erst die SolaX-Integration einrichten (Abschnitte 4–5), dann im Assistenten **„Erneut prüfen"** |
| **Kein remotecontrol_trigger** | Gen2/Gen3 oder X1 Fit (AC-coupled) → nicht unterstützt |
| **Befehle ohne Wirkung** | Work Mode „Self Use"? Night Charge und Smart Schedule aus? |
| **Batterie lädt trotz Sperre** | Lock State prüfen — Passwort `2014` zum Entsperren |
| **Sensoren nachts „unavailable"** | Normal — der Wechselrichter schläft (kein PV, keine Last) |
| **PV-Leistung zu niedrig (Generator-WR fehlt)** | `sensor.solax_inverter_meter_2_measured_power` aktivieren (→ Abschnitt 7) und im Assistenten (Expertenmodus) als zweiten PV-Sensor eintragen |
| **Meter-2-Sensor zeigt immer 0** | Meter 2 am Wechselrichter / in der SolaX-App als Generator-Zähler einrichten |
