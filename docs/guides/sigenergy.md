# Sigenergy einrichten

_**Modbus TCP** ist die Sprache, mit der Home Assistant im Heimnetz direkt mit dem Wechselrichter redet. Die **IP-Adresse** ist die Adresse des Geräts in deinem Heimnetz. Eine **Entität** ist ein einzelner Wert oder Schalter in Home Assistant._

## 1. Unterstützte Geräte

**SigenStor**-Anlagen mit Batteriespeicher (EC- und CMU-Serie, Hybrid):

| Firmware | |
|---|---|
| ab **SPC109** | unterstützt |
| ab **SPC113** | nötig, damit das Ladelimit auch im Eigenverbrauchsmodus wirkt (siehe unten) |

Gesteuert wird die **ganze Anlage** über eine Verbindung — auch bei mehreren Wechselrichtern.

_Getestet mit SigenStor EC 25.0 TP (Firmware V100R001C21SPC116, 40 kWh Speicher)._

## 2. Modbus TCP am Gerät freischalten

Das macht der Installateur (oder du mit Installateur-Zugang) in der **Sigen-App**:

**System → Devices → SigenStor → Operation → Parameter Settings → ModBus Settings → Local ModBus TCP** aktivieren.

Port ist **502**. Home Assistant muss das Gerät im Netzwerk erreichen (gleiches LAN oder Route).

> [!NOTE]
> Die IP des Wechselrichters steht in der Sigen-App bei den Geräteinformationen. Oder du siehst im Router nach, oder suchst aus Home Assistant nach einem offenen Port 502.

## 3. HACS-Integration installieren

_**Voraussetzung:** [HACS](https://hacs.xyz/) (Zusatz-Store für Home Assistant) muss installiert sein._

1. Gehe zu **HACS** und suche nach **„Sigenergy"**.<br>
   _Repository: `TypQxQ/Sigenergy-Local-Modbus`_
2. Installiere die Integration und **starte Home Assistant neu**.

## 4. Integration konfigurieren

1. **Einstellungen → Geräte & Dienste → Integration hinzufügen**
2. Suche nach **„Sigenergy ESS"**.
3. Gib die **IP-Adresse** des Wechselrichters ein, Port **502**.
4. Die Anlagen-Adresse (Plant-ID) bleibt auf **247** — das ist die Adresse für die ganze Anlage.

Danach gibt es mehrere Geräte: **„Sigen Plant"** (die Anlage), „Sigen Inverter" (je Wechselrichter), „Sigen Inverter PV1…" (Strings) und ggf. ein DC-Ladegerät. **Der Optimizer arbeitet mit „Sigen Plant".**

## 5. Steuerentitäten aktivieren — Pflicht

Die Integration legt **alle Steuerentitäten ausgeschaltet (deaktiviert)** an. Ohne die vier unten kann der Optimizer nichts steuern, und der Assistent lässt dich nicht weiter.

1. **Einstellungen → Geräte & Dienste → Sigenergy ESS**
2. Öffne das Gerät **„Sigen Plant"**.
3. Klicke in der Entitäten-Liste auf **„+ x Entitäten sind deaktiviert"**.
4. Aktiviere die Entitäten aus der Tabelle unten, eine nach der anderen: Entität anklicken → Zahnrad → **Aktiviert** → **Aktualisieren**.
5. Warte etwa 30 Sekunden, bis die Entitäten Werte zeigen.

**Pflicht:**

| Entität | Typ | Wofür |
|---|---|---|
| **Remote EMS (Controlled by Home Assistant)** | Schalter | Übergibt die Steuerung an Home Assistant |
| **Remote EMS Control Mode** | Auswahl | Betriebsmodus (Laden / Entladen / Eigenverbrauch) |
| **ESS Max Charging Limit** | Zahl (kW) | Ladelimit des Speichers |
| **ESS Max Discharging Limit** | Zahl (kW) | Entladeleistung des Speichers |

**Optional** — Untergrenze für den Fahrplan (der Optimizer liest sie nur):

| Entität | Wofür |
|---|---|
| ESS Discharge Cut-Off State of Charge | Entladegrenze des Geräts |
| ESS Backup State of Charge | Notstrom-Reserve des Geräts |

> [!NOTE]
> **„Remote EMS Control Mode" zeigt „nicht verfügbar", solange der Schalter „Remote EMS" aus ist.** Das ist normal. Der Optimizer schaltet ihn selbst ein, wenn er steuert, und wieder aus, wenn er freigibt.

> [!CAUTION]
> Der Assistent zeigt im Schritt „Wechselrichter" den Zustand dieser Entitäten. Steht dort „deaktiviert" oder „nicht vorhanden": zuerst hier aktivieren, dann den Wechselrichter im Assistenten noch einmal anklicken.

## 6. Sensoren

Der Optimizer erkennt die **Anlagen-Sensoren** automatisch (alle in **kW**):

| Größe | Sensor | Hinweis |
|---|---|---|
| PV-Leistung | `sensor.sigen_plant_pv_power` | Summe aller Wechselrichter |
| Netzleistung | `sensor.sigen_plant_grid_active_power` | positiv = Bezug — wird automatisch umgerechnet |
| Batterieleistung | `sensor.sigen_plant_battery_power` | positiv = Laden |
| Ladestand | `sensor.sigen_plant_battery_state_of_charge` | |
| Kapazität | `sensor.sigen_plant_rated_energy_capacity` | Kommt als Sensor — keine Eingabe von Hand nötig |

_Hast du das Gerät „Sigen Plant" umbenannt, heißen die Entitäten anders. Der Optimizer findet sie trotzdem über das Ende des Namens._

## 7. Betriebsmodus der Anlage

Stelle die Anlage in der Sigen-App auf **„Maximum Self Consumption"** (Eigenverbrauch). Zeitpläne wie **TOU** oder **Peak Shaving** stören die Steuerung — ausschalten.

Während der Optimizer steuert, ist der Schalter „Remote EMS" **an**. Gibt er frei (Modus „Aus", Neustart, kein Plan), schaltet er ihn **aus**, und die Anlage läuft in ihrem eigenen Modus weiter.

> [!WARNING]
> **Nicht gleichzeitig eingreifen.** Die MySigen-App und andere Steuerungen (z. B. evcc) schreiben dieselben Register. Änderst du während der Steuerung Werte in der App, überschreibst du den Optimizer — oder umgekehrt.

## 8. Prüfen

1. Unter **Einstellungen → Integrationen** zeigt Sigenergy ESS **„geladen"**.
2. Unter **Entwicklerwerkzeuge → Zustände** zeigt `sensor.sigen_plant_battery_state_of_charge` den Ladestand (0–100 %).
3. `switch.sigen_plant_remote_ems_controlled_by_home_assistant` existiert und steht auf **aus**.
4. Kehre hierher zurück. Der Wechselrichter wird automatisch erkannt.

## Was der Optimizer am Gerät tut

| Fahrplan will | Am Gerät |
|---|---|
| **Laden begrenzen** (auch 0 kW = Laden sperren) | Remote EMS an, Modus bleibt „Maximum Self Consumption“ — nur das Ladelimit wird gesetzt |
| **Entladen ins Netz** | Remote EMS an → „Command Discharging (ESS First)" mit Entladelimit |
| **Freigabe** (Normalbetrieb) | „Maximum Self Consumption" → Remote EMS **aus** |

> [!WARNING]
> **Kein Sicherheitsnetz im Gerät.** Anders als Fronius, Kostal oder SMA hat Sigenergy keine Rückfallzeit: Ein Befehl läuft weiter, bis er zurückgenommen wird. Der Optimizer gibt bei „Aus", bei fehlendem Plan und beim Neustart selbst frei. Stürzt Home Assistant **hart** ab (Stromausfall, eingefroren), bleibt der letzte Befehl stehen. Dann schalte „Remote EMS" in Home Assistant oder in der Sigen-App von Hand aus.

## Zu beachten

- **Kein „Command Charging“:** Für ein Ladelimit nutzt der Optimizer bewusst nicht „Command Charging (PV First)“. Dieser Modus lädt den Rest aus dem Netz, wenn die PV nicht reicht.
- **Firmware SPC113:** Laut Berichten wirkt das Ladelimit im Eigenverbrauchsmodus erst ab dieser Firmware. Davor wirkt es nicht — die Anlage lädt dann mit Überschuss weiter, aber nie aus dem Netz. Den Stand zeigt die Sigen-App.
- **Entladegrenze:** „ESS Discharge Cut-Off State of Charge“ liest der Optimizer als Untergrenze für den Plan, ändert sie aber nicht.

## Häufige Probleme

| Problem | Lösung |
|---|---|
| **Integration lädt nicht / Timeout** | Local Modbus TCP nicht freigeschaltet (→ Abschnitt 2) oder IP falsch |
| **Assistent meldet „Steuerentitäten deaktiviert"** | Abschnitt 5 — die vier Pflicht-Entitäten aktivieren, dann Wechselrichter noch einmal anklicken |
| **„Remote EMS Control Mode" ist nicht verfügbar** | Normal, solange „Remote EMS" aus ist |
| **Befehle ohne Wirkung** | Sigen-App: EMS-Modus „Maximum Self Consumption", TOU/Peak Shaving aus; kein zweites Programm (evcc, App) gleichzeitig |
| **Netzleistung mit falschem Vorzeichen** | Der Optimizer dreht das Vorzeichen selbst (positiv = Bezug). Bei Einspeisung muss `binary_sensor.sigen_plant_exporting_to_grid` „an" zeigen |
| **Nach HA-Absturz läuft ein Befehl weiter** | Schalter „Remote EMS (Controlled by Home Assistant)" von Hand ausschalten |
