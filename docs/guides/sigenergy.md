# Sigenergy einrichten

## 1. Unterstützte Geräte

**SigenStor**-Anlagen mit Batteriespeicher (EC- und CMU-Serie, Hybrid) ab Firmware **SPC109**. Gesteuert wird die **ganze Anlage** über eine Verbindung — auch wenn mehrere Wechselrichter parallel laufen.

_Getestet mit SigenStor EC 25.0 TP (Firmware V100R001C21SPC116, 40 kWh Speicher)._

## 2. Modbus TCP am Gerät freischalten

Der Wechselrichter muss seine Modbus-TCP-Schnittstelle anbieten. Das schaltet der Installateur (oder du mit Installateur-Zugang) in der **Sigen-App** frei:

**System → Devices → SigenStor → Operation → Parameter Settings → ModBus Settings → Local ModBus TCP** aktivieren.

Port ist **502**. Home Assistant muss das Gerät im Netzwerk erreichen (gleiches LAN oder Route).

> [!NOTE]
> Die IP des Wechselrichters zeigt die Sigen-App unter den Geräteinformationen. Alternativ im Router nachsehen oder aus Home Assistant heraus nach einem offenen Port 502 suchen.

## 3. HACS-Integration installieren

_**Voraussetzung:** [HACS](https://hacs.xyz/) muss installiert sein._

1. Gehe zu **HACS → Integrationen → Suche „Sigenergy"**<br>
   _Repository: `TypQxQ/Sigenergy-Local-Modbus`_
2. Installiere die Integration und **starte Home Assistant neu**

## 4. Integration konfigurieren

1. Gehe zu **Einstellungen → Geräte & Dienste → Integration hinzufügen**
2. Suche nach **„Sigenergy ESS"**
3. Gib die **IP-Adresse** des Wechselrichters ein, Port **502**
4. Die Anlagen-Adresse (Plant-ID) bleibt auf **247** — das ist die Sammeladresse für die ganze Anlage

Danach erscheinen mehrere Geräte: **„Sigen Plant"** (die Anlage), „Sigen Inverter" (je Wechselrichter), „Sigen Inverter PV1…" (Strings) und ggf. ein DC-Ladegerät. **Der Optimizer arbeitet mit dem Gerät „Sigen Plant".**

## 5. Steuerentitäten aktivieren — Pflicht

Die Integration legt **alle Steuerentitäten deaktiviert** an. Ohne die folgenden vier kann der Optimizer nichts stellen, und der Einrichtungsassistent lässt dich nicht weiter:

1. Gehe zu **Einstellungen → Geräte & Dienste → Sigenergy ESS**
2. Öffne das Gerät **„Sigen Plant"**
3. Klicke in der Entitäten-Liste auf **„+ x Entitäten sind deaktiviert"**
4. Aktiviere nacheinander (Entität anklicken → Zahnrad → **Aktiviert** → **Aktualisieren**):

| Entität | Typ | Wofür |
|---|---|---|
| **Remote EMS (Controlled by Home Assistant)** | Schalter | Übergibt die Steuerung an Home Assistant |
| **Remote EMS Control Mode** | Auswahl | Betriebsmodus (Laden / Entladen / Eigenverbrauch) |
| **ESS Max Charging Limit** | Zahl (kW) | Ladelimit des Speichers |
| **ESS Max Discharging Limit** | Zahl (kW) | Entladeleistung des Speichers |

Optional, als Untergrenze für den Fahrplan (der Optimizer liest sie nur):

| Entität | Wofür |
|---|---|
| ESS Discharge Cut-Off State of Charge | Entladeboden des Geräts |
| ESS Backup State of Charge | Notstrom-Reserve des Geräts |

5. Warte etwa 30 Sekunden, bis die Entitäten Werte zeigen

> [!NOTE]
> **„Remote EMS Control Mode" zeigt „nicht verfügbar", solange der Schalter „Remote EMS" aus ist.** Das ist normal — die Auswahl wird erst mit dem Schalter freigegeben. Der Optimizer schaltet ihn selbst ein, wenn er steuert, und wieder aus, wenn er freigibt.

> [!CAUTION]
> Der Einrichtungsassistent zeigt im Schritt „Wechselrichter" den Zustand dieser Entitäten an. Steht dort „deaktiviert" oder „nicht vorhanden", zuerst hier aktivieren, dann den Wechselrichter im Assistenten erneut anklicken.

## 6. Sensoren

Der Optimizer erkennt die **Anlagen-Sensoren** automatisch (alle in **kW**):

| Größe | Sensor | Hinweis |
|---|---|---|
| PV-Leistung | `sensor.sigen_plant_pv_power` | Summe aller Wechselrichter |
| Netzleistung | `sensor.sigen_plant_grid_active_power` | positiv = Bezug — wird automatisch umgerechnet |
| Batterieleistung | `sensor.sigen_plant_battery_power` | positiv = Laden |
| Ladestand | `sensor.sigen_plant_battery_state_of_charge` | |
| Kapazität | `sensor.sigen_plant_rated_energy_capacity` | Sigenergy liefert die Kapazität als Sensor — keine manuelle Eingabe nötig |

_Wer das Gerät „Sigen Plant" umbenannt hat, bekommt andere Entity-IDs. Der Optimizer findet die Sensoren auch dann über ihre Endung._

## 7. Betriebsmodus der Anlage

Die Anlage soll in der Sigen-App im Modus **„Maximum Self Consumption"** (Eigenverbrauch) laufen. Zeitpläne wie **TOU** oder **Peak Shaving** kollidieren mit der Steuerung — ausschalten.

Während der Optimizer steuert, steht der Schalter „Remote EMS" auf **an**; gibt er frei (Modus „Aus", Neustart, kein Plan), schaltet er ihn wieder **aus** und die Anlage läuft in ihrem eigenen Modus weiter.

> [!WARNING]
> **Nicht parallel eingreifen.** Die MySigen-App und andere Steuerungen (z. B. evcc) schreiben dieselben Register. Wer während der Steuerung in der App Werte ändert, überschreibt den Optimizer — oder umgekehrt.

## 8. Prüfen

1. Unter **Einstellungen → Integrationen**: Sigenergy ESS zeigt **„geladen"**
2. **Entwicklerwerkzeuge → Zustände**: `sensor.sigen_plant_battery_state_of_charge` zeigt den Ladestand (0–100 %)
3. `switch.sigen_plant_remote_ems_controlled_by_home_assistant` existiert und steht auf **aus**
4. Kehre hierher zurück — der Wechselrichter wird automatisch erkannt

## Was der Optimizer am Gerät tut

| Absicht des Fahrplans | Am Gerät |
|---|---|
| **Laden begrenzen** (auch 0 kW = Laden sperren) | Remote EMS an → „Command Charging (PV First)" mit Ladelimit |
| **Entladen ins Netz** | Remote EMS an → „Command Discharging (ESS First)" mit Entladelimit |
| **Freigabe** (Normalbetrieb) | „Maximum Self Consumption" → Remote EMS **aus** |

> [!WARNING]
> **Kein geräteseitiges Sicherheitsnetz.** Sigenergy kennt — anders als Fronius, Kostal oder SMA — keine Rückfallzeit: Ein Befehl läuft am Gerät weiter, bis er zurückgenommen wird. Der Optimizer gibt beim Umschalten auf „Aus", bei fehlendem Plan und beim Neustart aktiv frei. Stürzt Home Assistant **hart** ab (Stromausfall, eingefrorenes System), bleibt der letzte Befehl stehen — dann den Schalter „Remote EMS" in Home Assistant oder der Sigen-App manuell ausschalten.

## Feldtest — was noch zu bestätigen ist

Die Steuerung folgt der offiziellen Modbus-Spezifikation (V2.7), ist aber am Gerät noch nicht in allen Punkten nachgewiesen:

- **Ladesperre bei Nacht:** Sperrt „Command Charging (PV First)" mit 0 kW nur das Laden, oder entlädt der Speicher in diesem Modus auch nicht mehr für das Haus? Im zweiten Fall käme die Hauslast in den Randstunden kurz aus dem Netz.
- **Einspeisung:** Liefert „Command Discharging (ESS First)" tatsächlich ins Netz oder nur bis zur Hauslast?
- **Entladeboden:** Stoppt „ESS Discharge Cut-Off State of Charge" eine befohlene Entladung vorzeitig? Der Optimizer liest den Wert als Untergrenze, schreibt ihn aber nicht.

## Häufige Probleme

| Problem | Lösung |
|---|---|
| **Integration lädt nicht / Timeout** | Local Modbus TCP am Gerät nicht freigeschaltet (→ Abschnitt 2) oder IP falsch |
| **Assistent meldet „Steuerentitäten deaktiviert"** | Abschnitt 5 — die vier Pflicht-Entitäten aktivieren, dann Wechselrichter erneut anklicken |
| **„Remote EMS Control Mode" ist nicht verfügbar** | Normal, solange „Remote EMS" aus ist — kein Fehler |
| **Befehle ohne Wirkung** | Sigen-App: EMS-Modus auf „Maximum Self Consumption", TOU/Peak Shaving aus; kein zweites Programm (evcc, App) parallel |
| **Netzleistung mit falschem Vorzeichen** | Der Optimizer dreht das Sigenergy-Vorzeichen selbst (positiv = Bezug). Bei Einspeisung muss `binary_sensor.sigen_plant_exporting_to_grid` „an" zeigen |
| **Nach HA-Absturz läuft ein Befehl weiter** | Schalter „Remote EMS (Controlled by Home Assistant)" manuell ausschalten |
