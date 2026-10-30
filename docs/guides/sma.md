# SMA Smart Energy einrichten

> [!NOTE]
> Unterstützt werden **Sunny Tripower Smart Energy** (STP 5.0–10.0 SE), **Sunny Boy Storage** (SBS 3.7/5.0/6.0) und **Sunny Boy Smart Energy** — jeweils mit Batteriespeicher (z. B. BYD Battery-Box). Sunny Island wird nicht unterstützt.

Du brauchst zwei Verbindungen: die **SMA-Integration** zum Lesen der Messwerte und **Modbus TCP** zum Steuern der Batterie.

_**Modbus TCP** ist die Sprache, mit der Home Assistant im Heimnetz direkt mit dem Wechselrichter redet. Die **IP-Adresse** ist die Adresse des Geräts in deinem Heimnetz._

## 1. SMA Solar Integration in Home Assistant

Die eingebaute SMA-Integration liest die Sensoren (PV, Batterie, SOC = Ladestand, Netz).

1. **Einstellungen → Geräte & Dienste → Integration hinzufügen → „SMA Solar"**
2. Gib die IP-Adresse des Wechselrichters an.
3. Gruppe: **Benutzer** (user) genügt — der Optimizer liest nur.
4. Passwort: das Benutzer-Passwort des SMA-Webinterface (`https://<IP des Wechselrichters>`).

## 2. Modbus-TCP-Server am Wechselrichter aktivieren

Das kannst du selbst — es ist **kein Grid-Guard-Code nötig**.

1. Öffne das SMA-Webinterface (`https://<IP des Wechselrichters>`) und melde dich als **Installateur** an (oder als Benutzer mit Parameterrechten).
2. **Geräteparameter → Externe Kommunikation → Modbus → TCP-Server** → **aktivieren**
3. Port: **502** (Standard, nicht ändern)

Die Unit-ID 3 für die Steuerregister ist im Optimizer fest eingestellt.

## 3. Sunny Home Manager 2.0 (falls vorhanden)

> [!CAUTION]
> **Prognosebasiertes Laden ausschalten:** Hast du einen Sunny Home Manager 2.0, schalte in Sunny Portal sein **„prognosebasiertes Batterieladen"** aus. Sonst steuern zwei Systeme die Batterie gegeneinander. Ein SMA Energy Meter (ohne Home Manager) ist kein Problem — er misst nur.

> [!NOTE]
> **Eingebauter Failsafe:** SMA erwartet spätestens alle 300 Sekunden einen neuen Steuerbefehl. Fallen Optimizer oder Home Assistant aus, kehrt der Wechselrichter von selbst zu seiner eigenen Batterie-Automatik zurück.

> [!WARNING]
> **Nur ein steuerndes System:** Immer nur ein System darf die Batterie über Modbus steuern — also nicht zusätzlich evcc-Batteriesteuerung oder andere Modbus-Steuerungen. Nur lesen (z. B. die SMA-Integration aus Schritt 1) ist kein Problem.

## 4. Prüfen

1. Unter **Einstellungen → Geräte & Dienste** zeigt SMA Solar **„geladen"** und listet Sensoren.
2. Unter **Entwicklerwerkzeuge → Zustände** suchst du `battery_soc_total`, `metering_power_absorbed` und `pv_power`. Die Werte müssen plausibel sein.
3. Kehre in den Assistenten zurück. Die Sensoren werden automatisch erkannt.

_Achtung: `sensor.*_grid_power` ist bei SMA die Ausgangsleistung des Wechselrichters, **nicht** die Leistung am Netzanschluss. Für die Netzleistung nimmt der Optimizer `metering_power_supplied` (Einspeisung) und `metering_power_absorbed` (Bezug)._

## Im Assistenten: Schritt „Wechselrichter“

**„Weiter“** geht erst, wenn alle Punkte stimmen:

1. Wähle **SMA Smart Energy**. Ohne die SMA-Integration aus Schritt 1 geht es nicht weiter — Modbus allein genügt nicht.
2. **Fünf Leistungssensoren**, alle Pflicht. SMA meldet Batterie und Netz als je **zwei getrennte Sensoren**:

    - PV-Leistung
    - Batterie **Laden** (`*_battery_power_charge_total`) und Batterie **Entladen** (`*_battery_power_discharge_total`)
    - Netz **Einspeisung** (`*_metering_power_supplied`) und Netz **Bezug** (`*_metering_power_absorbed`)

    Die Erkennung trägt sie selbst ein. Fehlt einer, bleibt „Weiter“ gesperrt.

3. **Modbus IP-Adresse** (Pflicht): die IP des Wechselrichters. Sie ist **nicht** vorausgefüllt — bitte eintragen. Port **502**.
4. Bei **„Weiter“** prüft der Assistent die Modbus-Verbindung und liest das Steuerregister (nur lesend). Klappt das nicht: Ist der Modbus-TCP-Server aktiviert (Schritt 2)? Stimmt die IP?

## Was der Optimizer am Gerät tut

Der Optimizer steuert den SMA über das **externe Batteriemanagement** (CmpBMS, „6-Parameter-Methode"). Jeder Befehl schreibt den ganzen Block: Betriebsart, vier Leistungsgrenzen und Netz-Sollwert. So verlangt es SMA — einzelne Register wirken nicht.

| Register | Wofür |
|---|---|
| **40795** Max. Ladeleistung | Begrenzt das Laden auf die geplante Leistung. 0 W sperrt das Laden; Entladen für das Haus bleibt möglich |
| **40801** Netz-Sollwert | Erzwungene Einspeisung: positiver Wert in Watt **am Netzanschluss** |

**Der Unterschied zu anderen Wechselrichtern:** Fronius, Huawei, Kostal, Sigenergy und SolaX bekommen gesagt, wie viel die *Batterie* entladen soll — zuerst fürs Haus, der Rest geht ins Netz. Der SMA bekommt gesagt, wie viel *ins Netz* gehen soll, und deckt das Haus selbst dazu. Der Optimizer gibt ihm daher direkt die geplante Einspeisung vor. Victron arbeitet genauso (ESS-Netzsollwert). Reicht die Batterie nicht für Haus plus Einspeisung, senkt er den Sollwert um das Fehlende.

**Sicherheitsnetz (Watchdog):** Der Block muss spätestens alle 300 Sekunden erneuert werden, sonst fällt der Wechselrichter in sein eigenes Batteriemanagement zurück. Der Optimizer schreibt alle 60 Sekunden nach. Stürzt Home Assistant während einer Einspeisung ab, endet sie also von selbst.

Einen Ziel-Ladestand kennt SMA nicht. Der Optimizer prüft den Ladestand alle 30 Sekunden selbst und beendet die Einspeisung.

> Ladesperre, Netz-Sollwert und Stopp sind am Gerät getestet (Sunny Tripower 10.0 Smart Energy). Mit einem Sunny Home Manager 2.0 muss dessen prognosebasiertes Laden aus sein (Abschnitt 3).

## Häufige Probleme

| Problem | Lösung |
|---|---|
| **Modbus Connection refused** | Modbus-TCP-Server nicht aktiviert → Schritt 2 wiederholen (Port 502) |
| **Die Verbindungsprüfung im Assistenten meldet „Steuerregister 40236 nicht lesbar"** | Manche Firmwares nutzen eine andere Registeradresse. Bitte beim Support melden, bevor du die Steuerung einschaltest |
| **Keine SMA-Sensoren in HA** | Falsche Gruppe oder falsches Passwort bei der SMA-Integration — den Benutzer-Zugang des Webinterface verwenden |
| **Batterie lädt trotz Sperre** | Der Sunny Home Manager 2.0 steuert noch mit → Schritt 3: prognosebasiertes Laden ausschalten |
| **Keine Batteriekapazität erkannt** | SMA liefert keinen Kapazitätssensor. Trag die nutzbare Kapazität (z. B. vom BYD-Typenschild) im Wizard von Hand ein |
