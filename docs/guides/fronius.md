# Fronius Gen24 einrichten

## 1. Fronius Integration in Home Assistant

Die native Fronius Integration wird für das Lesen der Sensoren (PV, Batterie, SOC, Netz) benötigt:

1. Wird normalerweise **automatisch via Auto-Discovery** erkannt<br>
   _Falls nicht: Einstellungen → Geräte & Dienste → Integration hinzufügen → „Fronius"_
2. IP-Adresse des Wechselrichters angeben
3. Die **Solar API** muss im Fronius Web-Interface aktiviert sein (Standard ab FW 1.14.1)

## 2. Modbus TCP am Wechselrichter aktivieren

Der EEG Energy Optimizer steuert die Batterie über Modbus TCP (SunSpec Model 124). Dafür muss Modbus am Wechselrichter aktiviert werden:

1. Fronius Web-Interface öffnen: `http://<IP des Wechselrichters>`
2. **Communication → Modbus → Aktivieren**
3. Mode: **TCP Server**
4. SunSpec Model Type: **int + SF**<br>
   _Wichtig: Nicht „float" wählen — die Register-Adressen unterscheiden sich!_
5. Port: **502** (Standard)
6. **Allow Control via Modbus: EIN**<br>
   _Ohne diese Einstellung werden alle Schreibzugriffe abgelehnt!_

> [!WARNING]
> **Wichtig:** Alle Scheduled (Dis)Charging Zeitpläne im Web-Interface deaktivieren! Modbus und Web-Interface konkurrieren — der höhere Wert gewinnt.

> [!NOTE]
> **Sicherheitsnetz:** Solange eine Ladesperre oder eine Entladung aktiv ist, meldet sich der Optimizer jede Minute beim Wechselrichter. Bleiben diese Meldungen aus — etwa weil Home Assistant abgestürzt ist —, beendet der Wechselrichter den erzwungenen Betrieb nach 5 Minuten selbst und schaltet auf seine eigene Batteriesteuerung zurück. Die Batterie bleibt also nicht dauerhaft blockiert.
>
> Ein anderes Programm, das denselben Wechselrichter über Modbus abfragt (z. B. evcc), hält diesen Timer mit am Leben. In so einer Kombination kann das Sicherheitsnetz später oder gar nicht greifen.

## 3. Firmware

- **Minimum:** >= 1.34.6-1
- **Empfohlen:** >= 1.40.0

## 4. Prüfen

1. Unter **Einstellungen → Integrationen**: Fronius zeigt **„geladen"**
2. **Entwicklerwerkzeuge → Zustände**: Suche nach `power_photovoltaics` / `pv_leistung` und `state_of_charge` / `ladezustand`
3. Kehre in den Assistenten zurück — die Sensoren werden automatisch erkannt

## Im Assistenten: Schritt „Wechselrichter“

Hier bleibt man am häufigsten hängen: **„Weiter“** geht erst, wenn alles Folgende stimmt.

1. **Fronius Gen24** wählen. Ohne die Fronius-Integration aus Schritt 1 geht es nicht weiter — sie liefert alle Messwerte, Modbus allein genügt nicht.
2. **Fünf Leistungssensoren**, alle Pflicht. Fronius meldet Batterie und Netz nicht als einen Wert mit Vorzeichen, sondern als je **zwei getrennte Sensoren**:
   - PV-Leistung
   - Batterie **Laden** (`*_battery_power_charging` / `*_ladeleistung`) und Batterie **Entladen** (`*_battery_power_discharging` / `*_entladeleistung`)
   - Netz **Einspeisung** (`*_leistung_netzeinspeisung`) und Netz **Bezug** (`*_leistung_netzbezug`)

   Die Erkennung trägt sie selbst ein; fehlt einer, bleibt „Weiter“ gesperrt.
3. **Modbus IP-Adresse** (Pflicht): die IP des Wechselrichters. Sie wird aus der Fronius-Integration vorbelegt — prüfen, ob sie stimmt. Port **502**.
4. Bei **„Weiter“** prüft der Assistent die Modbus-Verbindung (nur lesend). Schlägt sie fehl, geht es nicht weiter: Modbus aktiviert (Schritt 2)? IP richtig? Wechselrichter im selben Netz?

## Häufige Probleme

| Problem | Lösung |
|---|---|
| **Modbus Connection refused** | Modbus TCP nicht aktiviert, falscher Port oder falsche IP → Schritt 2 wiederholen |
| **Verbindung klappt, Steuerbefehle werden abgelehnt** | „Allow Control via Modbus" nicht EIN → Schritt 2, Punkt 6 |
| **Alle Werte 0 oder unsinnig** | Falscher SunSpec-Modus → „int + SF" statt „float" einstellen |
| **Keine Fronius-Sensoren in HA** | Fronius Integration prüfen: Solar API im Web-Interface aktiviert? |
| **Steuerung funktioniert manchmal nicht** | Scheduled Charging/Discharging im Web-Interface deaktivieren (konkurriert mit Modbus) |
