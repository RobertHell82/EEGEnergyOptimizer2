# SMA Smart Energy einrichten

> [!NOTE]
> Unterstützt werden **Sunny Tripower Smart Energy** (STP 5.0–10.0 SE), **Sunny Boy Storage** (SBS 3.7/5.0/6.0) und **Sunny Boy Smart Energy** — jeweils mit Batteriespeicher (z.B. BYD Battery-Box). Sunny Island wird nicht unterstützt.

## 1. SMA Solar Integration in Home Assistant

Die native SMA-Integration wird für das Lesen der Sensoren (PV, Batterie, SOC, Netz) benötigt:

1. **Einstellungen → Geräte & Dienste → Integration hinzufügen → „SMA Solar"**
2. IP-Adresse des Wechselrichters angeben
3. Gruppe: **Benutzer** (user) genügt — der EEG Energy Optimizer benötigt die Integration nur lesend
4. Passwort: das Benutzer-Passwort des SMA-Webinterface (`https://<IP des Wechselrichters>`)

## 2. Modbus-TCP-Server am Wechselrichter aktivieren

Der EEG Energy Optimizer steuert die Batterie über Modbus TCP. Den Modbus-Server kann der Anlagenbetreiber selbst aktivieren — es ist **kein Grid-Guard-Code nötig**:

1. SMA-Webinterface öffnen: `https://<IP des Wechselrichters>`, als **Installateur** (oder Benutzer mit Parameterrechten) anmelden
2. **Gerätekonfiguration → Externe Kommunikation → Modbus → TCP-Server** → **aktivieren**
3. Port: **502** (Standard, nicht ändern)

Die Unit-ID 3 für die Steuerregister ist im Optimizer fest hinterlegt.

## 3. Sunny Home Manager 2.0 (falls vorhanden)

> [!CAUTION]
> **Prognosebasiertes Laden deaktivieren:** Wenn ein Sunny Home Manager 2.0 verbaut ist, muss dessen **„prognosebasiertes Batterieladen"** in Sunny Portal deaktiviert werden. Sonst steuern zwei Systeme gleichzeitig die Batterie und arbeiten gegeneinander. Ein SMA Energy Meter (ohne Home Manager) ist unproblematisch — er misst nur.

> [!NOTE]
> **Eingebauter Failsafe:** SMA erwartet zyklische Steuerbefehle (Refresh spätestens alle 300 Sekunden). Fällt der Optimizer oder Home Assistant aus, kehrt der Wechselrichter automatisch zur internen Batterie-Automatik zurück — die Anlage läuft also nie unkontrolliert weiter.

> [!WARNING]
> **Nur ein steuerndes System:** Es darf immer nur ein System die Batterie über Modbus steuern. Parallelbetrieb mit evcc-Batteriesteuerung oder anderen Modbus-Steuerungen ist nicht möglich. Lesende Zugriffe (z.B. die SMA-Integration aus Schritt 1) sind unproblematisch.

## 4. Prüfen

1. Unter **Einstellungen → Geräte & Dienste**: SMA Solar zeigt **„geladen"** und listet Sensoren
2. **Entwicklerwerkzeuge → Zustände**: Suche nach `battery_soc_total`, `metering_power_absorbed` und `pv_power` — die Werte müssen plausibel sein
3. Kehre hierher zurück — die Sensoren werden automatisch erkannt

_Hinweis: Der Sensor `sensor.*_grid_power` ist bei SMA die AC-Ausgangsleistung des Wechselrichters, **nicht** der Netzanschlusspunkt. Für die Netzleistung verwendet der Optimizer das Sensorpaar `metering_power_supplied` (Einspeisung) und `metering_power_absorbed` (Bezug)._

## Was der Optimizer am Gerät tut

Der Fahrplan stellt den SMA über das **externe Batteriemanagement** (CmpBMS, „6-Parameter-Methode"). Jeder Befehl schreibt den kompletten Block aus Betriebsart und vier Leistungsgrenzen plus Netz-Sollwert — so verlangt es die SMA-Spezifikation, einzelne Register wären wirkungslos.

| Register | Wofür |
|---|---|
| **40795** Max. Ladeleistung | Der Fahrplan begrenzt das Laden auf die geplante Leistung. 0 W blockiert das Laden, das Entladen für den Hausverbrauch bleibt möglich |
| **40801** Netz-Sollwert | Erzwungene Einspeisung: positiver Wert in Watt **am Netzanschlusspunkt** |

**Der Netz-Sollwert ist die Besonderheit gegenüber anderen Wechselrichtern.** Fronius, Huawei und Kostal bekommen gesagt, mit wie viel die *Batterie* entladen soll — davon deckt das Gerät zuerst das Haus, der Rest geht ins Netz. Der SMA bekommt stattdessen gesagt, wie viel *ins Netz* gehen soll, und legt die Hauslast selbst obendrauf. Die Steuerung gibt ihm deshalb direkt die geplante Einspeisung vor. Reicht die Entladeleistung der Batterie für Haus plus Einspeisung nicht, senkt sie den Sollwert um das Fehlende, statt dass das Gerät still weniger liefert.

**Der Watchdog ist das Sicherheitsnetz:** Der Block muss spätestens alle 300 Sekunden erneuert werden, sonst fällt der Wechselrichter in sein internes Batteriemanagement zurück. Der Treiber schreibt alle 60 Sekunden nach. Stürzt Home Assistant mitten in einer Einspeisung ab, endet sie also von selbst.

Einen Ziel-Ladestand kennt die SMA-Schnittstelle nicht; der Optimizer prüft den Ladestand alle 30 Sekunden selbst und beendet die Einspeisung.

> **Beta:** Ladeblockierung, Netz-Sollwert und Stopp sind am Gerät verifiziert (Sunny Tripower 10.0 Smart Energy). Offen ist der Dauerbetrieb über mehrere Tage — insbesondere neben einem Sunny Home Manager 2.0.

## Häufige Probleme

| Problem | Lösung |
|---|---|
| **Modbus Connection refused** | Modbus-TCP-Server nicht aktiviert → Schritt 2 wiederholen (Port 502) |
| **Verbindungstest meldet „Steuerregister 40236 nicht lesbar"** | Manche Firmwares nutzen eine abweichende Registeradresse — bitte beim Support melden, bevor die Steuerung aktiviert wird |
| **Keine SMA-Sensoren in HA** | Falsche Gruppe/Passwort bei der SMA-Integration — Benutzer-Zugang des Webinterface verwenden |
| **Batterie lädt trotz Blockierung** | Sunny Home Manager 2.0 steuert noch mit → Schritt 3: prognosebasiertes Laden deaktivieren |
| **Keine Batteriekapazität erkannt** | SMA liefert keinen Kapazitätssensor — die nutzbare Kapazität (z.B. vom BYD-Typenschild) im Wizard manuell eintragen |
