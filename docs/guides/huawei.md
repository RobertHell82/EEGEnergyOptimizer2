# Huawei Solar Integration einrichten

_**Modbus TCP** ist die Sprache, mit der Home Assistant im Heimnetz direkt mit dem Wechselrichter redet. Die **IP-Adresse** ist die Adresse des Geräts in deinem Heimnetz. Eine **Entität** ist ein einzelner Wert oder Schalter in Home Assistant._

> [!NOTE]
> **Hast du ein Huawei-EMMA (Energiemanagement)?** Dann heißen die Sensoren `sensor.emma_…`. Das EMMA liefert die Netzleistung mit umgekehrtem Vorzeichen als die direkte Anbindung an den SUN2000. Der EEG Energy Optimizer erkennt solche Sensoren automatisch und dreht das Netz-Vorzeichen selbst um. Die Batterieleistung folgt der normalen Regel und bleibt unverändert. Du trägst die `sensor.emma_*`-Entitäten einfach in der Sensor-Zuordnung ein.

## 1. Wechselrichter vorbereiten

Am Wechselrichter muss Modbus TCP eingeschaltet sein. Sonst kommt Home Assistant nicht hinein.

1. Verbinde das WLAN deines Handys mit dem Hotspot des Wechselrichters (`SUN2000-<Seriennummer>`).<br>
   _Das Passwort steht auf dem Aufkleber am Dongle. Schalte die mobilen Daten am Handy aus!_
2. Öffne die **FusionSolar App** oder die **SUN2000 App** → **Geräte-Inbetriebnahme**.
3. Melde dich als **Installer** an, mit dem Passwort `00000a`.<br>
   _Das ist das Standard-Passwort (6 Zeichen). Wurde es geändert, nimm das aktuelle Installer-Passwort._
4. Gehe zu **Einstellungen → Kommunikationskonfiguration → Dongle-Parameter**.
5. Stelle Modbus-TCP auf **„Aktivieren (uneingeschränkt)"**.

## 2. HACS Integration installieren

_**Voraussetzung:** [HACS](https://hacs.xyz/) muss installiert sein. HACS ist ein Zusatz-Store für Home Assistant, über den du Integrationen aus der Community installierst._

1. Gehe zu **HACS** und suche dort nach **„Huawei Solar"**.<br>
   _Repository: `wlcrs/huawei_solar`_
2. Installiere die Integration und **starte Home Assistant neu**.

## 3. Integration konfigurieren

1. Gehe zu **Einstellungen → Geräte & Dienste → Integration hinzufügen**.
2. Suche nach **„Huawei Solar"**.
3. Wähle **Netzwerk** als Verbindungstyp.
4. Gib die **IP-Adresse** des Wechselrichters bzw. Dongles ein.
5. Port: **6607** (neuere Firmware) oder **502** (ältere Firmware).
6. Slave ID: **1** (Standard). Bei Problemen versuche 0.
7. **„Enable battery control“ MUSS eingeschaltet werden!** In älteren Versionen der Integration hieß diese Option „Elevated Permissions“.<br>
   _Ohne diese Option fehlen die Steuer-Dienste der Integration. Der EEG Energy Optimizer kann dann nicht steuern, und der Assistent meldet „Die Steuer-Dienste der huawei_solar-Integration fehlen“._
8. Gib das Installer-Passwort `00000a` ein.

_**Option vergessen?** Du kannst sie nachträglich einschalten: Einstellungen → Integrationen → Huawei Solar → Drei-Punkte-Menü → „Neu konfigurieren". Danach im Assistenten auf „Erneut prüfen“ klicken._

## 4. Prüfen

1. Unter **Einstellungen → Integrationen** zeigt Huawei Solar **„geladen"**.
2. Unter **Entwicklerwerkzeuge → Zustände** zeigt `sensor.batterien_batterieladung` (englisch benannt: `sensor.batteries_batterieladung`) den Ladestand (0–100 %).
3. Es gibt die Entität `number.batteries_maximale_ladeleistung`. Das heißt: „Enable battery control“ ist aktiv.
4. Kehre hierher zurück. Der Wechselrichter wird automatisch erkannt.

## Häufige Probleme

| Problem | Lösung |
|---|---|
| **Connection refused** | Modbus TCP ist nicht eingeschaltet → Schritt 1 wiederholen |
| **Connection timeout** | Port 6607 statt 502 versuchen (oder umgekehrt) |
| **Keine Batterie-Entities** | „Enable battery control“ fehlt → neu konfigurieren |
| **Permission denied** | Passwort `00000a` oder `0000000a` (8 Zeichen) versuchen |
| **Verbindung bricht ab** | FusionSolar App ganz schließen, nicht nur minimieren |
