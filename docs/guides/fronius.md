# Fronius Gen24 einrichten

Bei Fronius braucht der EEG Energy Optimizer zwei Wege zum Wechselrichter:

| Weg | Wofür |
|---|---|
| **Fronius-Integration** in Home Assistant | Lesen der Messwerte (PV, Batterie, SOC, Netz) |
| **Modbus TCP** am Wechselrichter | Steuern der Batterie |

_**Modbus TCP** ist die Sprache, mit der Home Assistant im Heimnetz direkt mit dem Wechselrichter redet. Die **IP-Adresse** ist die Adresse des Geräts in deinem Heimnetz. **SOC** ist der Ladestand der Batterie._

## 1. Fronius Integration in Home Assistant

Die eingebaute Fronius-Integration liest die Sensoren (PV, Batterie, SOC, Netz).

1. Home Assistant findet den Wechselrichter normalerweise **von selbst (Auto-Discovery)**.<br>
   _Falls nicht: Einstellungen → Geräte & Dienste → Integration hinzufügen → „Fronius"_
2. Gib die IP-Adresse des Wechselrichters an.
3. Im Fronius Web-Interface muss die **Solar API** aktiviert sein. Ab FW 1.14.1 ist sie das standardmäßig.

## 2. Modbus TCP am Wechselrichter aktivieren

Der EEG Energy Optimizer steuert die Batterie über Modbus TCP (SunSpec Model 124). So schaltest du Modbus ein:

1. Öffne das Fronius Web-Interface: `http://<IP des Wechselrichters>`
2. **Communication → Modbus → Aktivieren**
3. Mode: **TCP Server**
4. SunSpec Model Type: **int + SF**<br>
   _Wichtig: Nicht „float" wählen — die Register-Adressen sind dann andere!_
5. Port: **502** (Standard)
6. **Allow Control via Modbus: EIN**<br>
   _Ohne diese Einstellung lehnt der Wechselrichter alle Steuerbefehle ab!_

> [!WARNING]
> **Wichtig:** Schalte alle Zeitpläne für Scheduled (Dis)Charging im Web-Interface aus! Modbus und Web-Interface arbeiten sonst gegeneinander — der höhere Wert gewinnt.

> [!NOTE]
> **Sicherheitsnetz:** Solange eine Ladesperre oder eine Entladung läuft, meldet sich der Optimizer jede Minute beim Wechselrichter. Bleiben diese Meldungen aus — etwa weil Home Assistant abgestürzt ist —, beendet der Wechselrichter den erzwungenen Betrieb nach 5 Minuten selbst. Er schaltet dann auf seine eigene Batteriesteuerung zurück. Die Batterie bleibt also nicht dauerhaft blockiert.
>
> Achtung: Fragt ein anderes Programm denselben Wechselrichter über Modbus ab (z. B. evcc), hält es diesen Zeitgeber mit am Leben. Dann greift das Sicherheitsnetz eventuell später oder gar nicht.

## 3. Firmware

| | Version |
|---|---|
| **Minimum** | >= 1.34.6-1 |
| **Empfohlen** | >= 1.40.0 |

## 4. Prüfen

1. Unter **Einstellungen → Integrationen** zeigt Fronius **„geladen"**.
2. Unter **Entwicklerwerkzeuge → Zustände** suchst du nach `power_photovoltaics` / `pv_leistung` und `state_of_charge` / `ladezustand`.
3. Kehre in den Assistenten zurück. Die Sensoren werden automatisch erkannt.

## Im Assistenten: Schritt „Wechselrichter“

Hier bleiben die meisten hängen. **„Weiter“** geht erst, wenn alle folgenden Punkte stimmen:

1. Wähle **Fronius Gen24**. Ohne die Fronius-Integration aus Schritt 1 geht es nicht weiter. Sie liefert alle Messwerte — Modbus allein genügt nicht.
2. **Fünf Leistungssensoren**, alle Pflicht. Fronius meldet Batterie und Netz nicht als einen Wert mit Plus/Minus, sondern als je **zwei getrennte Sensoren**:

    - PV-Leistung
    - Batterie **Laden** (`*_battery_power_charging` / `*_ladeleistung`) und Batterie **Entladen** (`*_battery_power_discharging` / `*_entladeleistung`)
    - Netz **Einspeisung** (`*_leistung_netzeinspeisung`) und Netz **Bezug** (`*_leistung_netzbezug`)

    Die Erkennung trägt sie selbst ein. Fehlt einer, bleibt „Weiter“ gesperrt.

3. **Modbus IP-Adresse** (Pflicht): die IP des Wechselrichters. Das Feld ist mit der IP aus der Fronius-Integration schon ausgefüllt — prüfe, ob sie stimmt. Port **502**.
4. Bei **„Weiter“** prüft der Assistent die Modbus-Verbindung (nur lesend). Klappt das nicht, geht es nicht weiter. Prüfe dann: Ist Modbus aktiviert (Schritt 2)? Stimmt die IP? Ist der Wechselrichter im selben Netz?

## Häufige Probleme

| Problem | Lösung |
|---|---|
| **Modbus Connection refused** | Modbus TCP nicht aktiviert, falscher Port oder falsche IP → Schritt 2 wiederholen |
| **Verbindung klappt, Steuerbefehle werden abgelehnt** | „Allow Control via Modbus" ist nicht EIN → Schritt 2, Punkt 6 |
| **Alle Werte 0 oder unsinnig** | Falscher SunSpec-Modus → „int + SF" statt „float" einstellen |
| **Keine Fronius-Sensoren in HA** | Fronius-Integration prüfen: Ist die Solar API im Web-Interface aktiviert? |
| **Steuerung funktioniert manchmal nicht** | Scheduled Charging/Discharging im Web-Interface ausschalten (arbeitet gegen Modbus) |
