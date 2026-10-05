# SolaX Modbus einrichten

## 1. Unterstützte Wechselrichter

Nur **Gen4, Gen5 und Gen6** Wechselrichter werden unterstützt. Ältere Generationen (Gen2/Gen3) haben keine Remote Control Funktion.

## 2. Wechselrichter-Einstellungen

Diese Einstellungen müssen am Wechselrichter oder in der SolaX-App korrekt gesetzt sein:

| Einstellung | Wert |
|---|---|
| **Work Mode** | **Self Use** (charger_use_mode = 0) |
| **Night Charge** | **Aus** — sonst lädt die Batterie nachts aus dem Netz |
| **Smart Schedule / Zeitplan** | **Aus** — kollidiert mit der Optimizer-Steuerung |

> [!WARNING]
> **Wichtig:** Der Work Mode darf **NICHT** auf „Feedin Priority" oder „Force Time Use" stehen! Der EEG Energy Optimizer steuert die Batterie über Remote Control (Mode 1) und setzt voraus, dass der Wechselrichter im Self Use Modus läuft.

## 3. Verbindung zum Wechselrichter — was du brauchst

Home Assistant spricht mit dem Wechselrichter über **Modbus TCP** im Heimnetz, nicht über die SolaX-Cloud. Dafür braucht der Wechselrichter eine Netzwerkverbindung, die Modbus TCP kann. **Das kann nicht jeder Stick**, und das ist die häufigste Ursache, wenn die Einrichtung scheitert:

| Verbindung | Funktioniert? |
|---|---|
| **Pocket WiFi 2.0** (ältere WLAN-Sticks) | **Nein** — kein Modbus TCP |
| **Pocket WiFi 3.0** | Nur mit aktueller Firmware; nicht immer zuverlässig |
| **Pocket LAN** (Stick mit Netzwerkkabel) | Ja |
| **RS485-zu-LAN-Adapter** am COM-/RS485-Anschluss des Wechselrichters (z. B. Waveshare) | Ja, am zuverlässigsten — Anschluss am besten durch den Elektriker |

Außerdem brauchst du die **IP-Adresse** des Sticks bzw. Adapters. Du findest sie in der Geräteliste deines Routers (z. B. bei der FRITZ!Box unter *Heimnetz → Netzwerk*). Damit sie sich nicht ändert, stelle im Router „immer dieselbe IP-Adresse zuweisen" ein.

> [!TIP]
> **Unsicher, welchen Stick du hast?** Er steckt unten am Wechselrichter, das Modell steht auf dem Aufkleber. Schick uns im Zweifel ein Foto davon.

## 4. Integration installieren

**Zuerst prüfen, ob sie schon da ist:** Gehe zu **Einstellungen → Geräte & Dienste**, klicke rechts unten auf **„+ Integration hinzufügen"** und tippe **„SolaX"** ein. Erscheint **„SolaX Inverter Modbus"** in der Liste, ist sie installiert — weiter mit Abschnitt 5.

Erscheint sie nicht, installiere sie über HACS:

_**Voraussetzung:** [HACS](https://hacs.xyz/) muss installiert sein._

1. Gehe zu **HACS** und suche dort nach **„SolaX Inverter Modbus"**<br>
   _Repository: `wills106/homeassistant-solax-modbus`_
2. Installiere die Integration und **starte Home Assistant neu** (Einstellungen → oben rechts die drei Punkte → **Neu starten**)

> [!WARNING]
> Ohne Neustart meldet Home Assistant beim Hinzufügen einen Fehler wie **„Konfigurationsablauf konnte nicht geladen werden"**. Dann einfach neu starten und es noch einmal versuchen.

## 5. Integration konfigurieren

1. Gehe zu **Einstellungen → Geräte & Dienste → „+ Integration hinzufügen"**
2. Suche nach **„SolaX Inverter Modbus"**
3. Als Verbindungsart **TCP / Ethernet** wählen
4. Gib die **IP-Adresse** des Sticks bzw. Adapters ein (→ Abschnitt 3)
5. Port: **502** (Standard für Modbus TCP), Modbus-Adresse: **1**
6. Mit **OK** bzw. **Absenden** bestätigen — die Integration erkennt den Wechselrichter dann selbst

Kommt eine Fehlermeldung, schau unter **Häufige Probleme** ganz unten nach.

## 6. Batterie-Kapazität

SolaX stellt **keinen Sensor für die Batteriekapazität** bereit. Die Kapazität gibst du später im Wizard manuell ein (z.B. 5.8 kWh für eine T-BAT 5.8).

## 7. Zweiter Wechselrichter am Generator-Eingang

Ist am SolaX-Hybrid ein **zweiter Wechselrichter als Generator** angeschlossen (z.B. eine bestehende PV-Anlage am Generator-/Meter-2-Eingang), wird dessen Erzeugung **nicht** im normalen PV-Sensor (`sensor.solax_*solar_power`) mitgezählt — sie läuft ausschließlich über **Meter 2**.

Der dafür benötigte Sensor ist in der SolaX-Modbus-Integration standardmäßig **deaktiviert** und muss aktiviert werden:

1. Gehe zu **Einstellungen → Geräte & Dienste → SolaX Inverter Modbus**
2. Klicke auf dein **Wechselrichter-Gerät**
3. Öffne in der Entitäten-Liste den Filter für **deaktivierte Entitäten** („+ x Entitäten sind deaktiviert")
4. Suche nach **„Meter 2 Measured Power"**
5. Klicke auf die Entität → Zahnrad → **Aktiviert** → **Aktualisieren**
6. Warte ca. 30 Sekunden bis der Sensor Werte liefert

_Der Sensor heißt typischerweise `sensor.solax_inverter_meter_2_measured_power` (Prefix je Installation abweichend) und zeigt die Leistung in W._

> [!NOTE]
> **Voraussetzung:** Meter 2 muss auch am Wechselrichter bzw. in der SolaX-App als Generator-/zweiter Zähler konfiguriert sein. Ohne diese Konfiguration liefert der Sensor dauerhaft 0.

Im Assistenten gibt es dafür das Feld **„Zweiter PV-Sensor (optional)"** — es erscheint nur im **Expertenmodus** (Schalter oben rechts im Assistenten). Vorbelegt wird es mit dem Meter-2-Sensor, wenn dieser schon **vor** der Sensorerkennung aktiv war; hast du ihn erst danach aktiviert, klicke auf **„Erneut prüfen"** oder trage ihn im Expertenmodus von Hand ein. Der Optimizer addiert diesen Wert zur PV-Leistung des Hybrid-Wechselrichters.

> [!WARNING]
> Wird der Sensor nicht aktiviert bzw. nicht im Wizard hinterlegt, rechnet der Optimizer mit **zu geringer PV-Leistung**. Folge: Hausverbrauch und Fahrplan arbeiten mit falschen Werten.

## 8. Prüfen

1. Unter **Einstellungen → Integrationen**: SolaX Inverter Modbus zeigt **„geladen"**
2. **Entwicklerwerkzeuge → Zustände**: `sensor.solax_*battery_capacity` zeigt SOC (0–100%)
3. Ein Button `button.solax_*remotecontrol_trigger*` existiert (neuere Versionen hängen den Modus an, z. B. `…_remotecontrol_trigger_mode_1_7`) — dann ist Remote Control verfügbar
4. Kehre hierher zurück — der Wechselrichter wird automatisch erkannt

_**Hinweis:** Der Entity-Prefix variiert je Installation (z.B. `solax_inverter_` statt `solax_`). Der EEG Energy Optimizer erkennt den Prefix automatisch._

## Häufige Probleme

| Problem | Lösung |
|---|---|
| **„Konfigurationsablauf konnte nicht geladen werden"** | Integration frisch installiert, aber Home Assistant nicht neu gestartet → neu starten (→ Abschnitt 4) |
| **„Verbindung fehlgeschlagen" / Connection refused / Zeitüberschreitung** | IP-Adresse falsch oder geändert → im Router nachsehen. Stimmt sie, kann der Stick vermutlich kein Modbus TCP (Pocket WiFi 2.0, alte 3.0-Firmware) → Abschnitt 3 |
| **SolaX taucht bei „Integration hinzufügen" nicht auf** | Integration nicht installiert → Abschnitt 4 |
| **EEG-Assistent: „SolaX Modbus Integration muss zuerst installiert werden"** | Erst die SolaX-Integration einrichten (Abschnitte 4–5), dann im Assistenten auf **„Erneut prüfen"** |
| **Kein remotecontrol_trigger** | Gen2/Gen3 oder X1 Fit (AC-coupled) → nicht unterstützt |
| **Kommandos ohne Wirkung** | Work Mode auf „Self Use" prüfen, Night Charge und Smart Schedule aus |
| **Batterie lädt trotz Blockierung** | Lock State prüfen — Passwort `2014` zum Entsperren |
| **Sensoren „unavailable" nachts** | Normal — Wechselrichter im Sleep Mode (kein PV, keine Last) |
| **PV-Leistung zu niedrig (Generator-WR fehlt)** | `sensor.solax_inverter_meter_2_measured_power` aktivieren (→ Abschnitt 7) und im Assistenten (Expertenmodus) als zweiten PV-Sensor hinterlegen |
| **Meter 2 Sensor zeigt immer 0** | Meter 2 am Wechselrichter / in der SolaX-App als Generator-Zähler konfigurieren |
