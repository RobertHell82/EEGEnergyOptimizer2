# Wechselrichter — Stand der Unterstützung

Diese Seite ist die **einzige Stelle**, an der steht, welcher Wechselrichter
unterstützt wird und wie weit er erprobt ist. Die übrige Doku verweist
hierher, statt eigene Listen zu führen.

> [!IMPORTANT]
> **Unterstützt werden Fronius Gen24, Huawei SUN2000, Kostal Plenticore,
> Sigenergy SigenStor, SMA Smart Energy und SolaX Gen4+.** Alle sechs stehen
> im Einrichtungsassistenten zur Auswahl, und bei allen sechs setzt die
> Steuerung den Fahrplan am Gerät durch (Ladelimit und erzwungene
> Entladung) — keiner wird nur ausgelesen.

## Übersicht

| Wechselrichter | Anbindung | Stand |
|---|---|---|
| **Fronius Gen24** | direkt per Modbus TCP | Feldtest |
| **Huawei SUN2000** | Huawei-Solar-Integration | **freigegeben** |
| **Kostal Plenticore** | direkt per Modbus TCP | Feldtest |
| **Sigenergy SigenStor** | Sigenergy-Local-Modbus-Integration | Feldtest |
| **SMA Smart Energy** | direkt per Modbus TCP | Feldtest |
| **SolaX Gen4+** | SolaX-Modbus-Integration | Feldtest |

- **Freigegeben** heißt: über längere Zeit im Dauerbetrieb, alle
  Sicherheitsnetze am Gerät nachgewiesen.
- **Feldtest** heißt: Laden sperren, Entladen und Freigeben sind an einer
  echten Anlage nachgewiesen, der Dauerbetrieb über Tage und Wochen noch
  nicht vollständig. Was je Gerät noch offen ist, steht unten. Wer so ein
  Gerät betreibt, schaut in den ersten Tagen am besten öfter in die
  Statuskarte und das Aktivitätsprotokoll.

## Was je Wechselrichter noch zu klären ist

Alphabetisch. Die Einrichtung selbst beschreibt jeweils der verlinkte Guide.

### Fronius Gen24

**Feldtest.** Steuerung über direktes Modbus TCP (SunSpec
Model 124), Sensordaten über die native
[Fronius](https://www.home-assistant.io/integrations/fronius/) Integration
(Solar API). Keine zusätzliche HACS-Integration nötig — nur die Fronius Core
Integration und eine Netzwerkverbindung zum Wechselrichter (Standard-Port 502).

Der Wechselrichter beendet einen erzwungenen Betrieb nach 5 Minuten ohne
Modbus-Nachricht selbst (Rückfallzeit `InOutWRte_RvrtTms`), der Treiber hält
sie mit einem Keepalive am Leben. **Achtung:** Jede Modbus-Nachricht startet
diesen Timer neu, auch die eines anderen Programms — läuft daneben eine
zweite Steuerung (z. B. evcc) auf demselben Gerät, greift das Sicherheitsnetz
später oder gar nicht.

Offen: Nachweis der Rückfallzeit am Gerät (Ladesperre setzen, Home Assistant
hart stoppen, nach 5 Minuten prüfen, ob die Batterie wieder lädt).
Guide: [fronius.md](guides/fronius.md)

### Huawei SUN2000

**Freigegeben.** Single oder Master/Slave (mehrere Wechselrichter + Batterien),
Steuerung über die [Huawei Solar](https://github.com/wlcrs/huawei_solar)
Integration. Direkte Anbindung an Wechselrichter/Dongle oder über das
EMMA-Energiemanagement (`sensor.emma_*`-Sensoren, Netz-Vorzeichen wird
automatisch korrigiert).
Guide: [huawei.md](guides/huawei.md) · [Akkukapazität-Sensor](guides/capacity_sensor.md)

### Kostal Plenticore

**Feldtest.** plus/G2/G3, Steuerung über direktes Modbus TCP (Port 1502, proprietäre
Batterie-Steuerregister), Sensordaten über die native
[Kostal Plenticore](https://www.home-assistant.io/integrations/kostal_plenticore/)
Integration (REST). Die Umstellung der Batteriesteuerung auf „Extern über
Protokoll (Modbus TCP)" liegt im Servicemenü und erfordert einen
**Installateur-Login**. Kostal erwartet zyklische Steuerbefehle (Watchdog):
Fällt Home Assistant aus, kehrt der Wechselrichter zur internen Automatik
zurück. Die Steuerregister sind flüchtig (RAM) — kein NVRAM-Verschleiß.
Fahrplan-Steuerung seit 2.1.1-dev9: Ladeblockierung, Entladung und
Stopp sind am Gerät verifiziert (1.x-Reihe, 19.08.2026). Die Kodierung von
Register 1038 für Teil-Ladelimits prüft der Treiber beim ersten Wert selbst
nach und meldet eine Abweichung im Protokoll. **Feldbefund Ansfelden
(22./25.09.2026):** Ein 1034-Sollwert von 0 W hält, solange *irgendein*
Steuerregister geschrieben wird — auch der 1038-Keepalive eines Ladelimits.
Folgte auf „Normalbetrieb“ ein Ladelimit, stand die Batterie stundenlang.
Seitdem schreibt der Treiber 1034 nur nach einer echten Entladung und
danach kein Steuerregister, bis die gemessene Batterieleistung den Rückfall
zur internen Automatik zeigt (höchstens 15 min) — der Watchdog-Timeout ist
per Modbus nicht lesbar und je Anlage verschieden. Offen: Feldtest der
Fahrplan-Nachführung über mehrere Tage.
Guide: [kostal.md](guides/kostal.md)

### Sigenergy SigenStor

**Feldtest.** SigenStor-Anlagen (EC-/CMU-Serie) ab Firmware
SPC109, Steuerung über die HACS-Integration
[Sigenergy Local Modbus](https://github.com/TypQxQ/Sigenergy-Local-Modbus)
(Domain `sigen`) — Remote EMS per Schalter, Auswahl und Zahlen-Entitäten,
kein eigenes Modbus. Eine Verbindung steuert die ganze Anlage, auch mit
mehreren Wechselrichtern; alle Sensoren kommen in kW vom Gerät „Sigen Plant",
die Kapazität als Sensor.

**Steuerentitäten ab Werk deaktiviert:** Die Integration legt alle
Schreib-Entitäten deaktiviert an. Der Einrichtungsassistent liest ihren
Zustand aus der Entity-Registry und lässt erst weiter, wenn die vier
Pflicht-Entitäten aktiv sind. Die Modus-Auswahl ist zudem nur verfügbar,
solange der Schalter „Remote EMS" an ist — der Treiber schaltet ein, wartet
auf die Auswahl und setzt dann den Modus (Home Assistant überspringt
Service-Aufrufe an nicht verfügbare Entitäten stillschweigend).

> [!WARNING]
> **Kein geräteseitiges Failsafe.** Die Sigenergy-Modbus-Spezifikation kennt
> weder Watchdog noch Rückfallzeit. Ein Befehl läuft am Gerät weiter, bis er
> zurückgenommen wird — deshalb ist die Freigabe hier das Ausschalten des
> Remote EMS, und die Integration gibt bei „Aus", fehlendem Plan und Unload
> immer aktiv frei. Nach einem harten Absturz von Home Assistant bleibt der
> letzte Befehl stehen.

Offen (Feldtest): ob „Command Charging (PV First)" mit Limit 0 nur das Laden
sperrt oder auch die Hausentladung; ob „Command Discharging (ESS First)" ins
Netz liefert; ob der Entlade-Cut-Off (40048) eine befohlene Entladung stoppt
(dann wie bei SolaX für die Dauer absenken). Netz-Vorzeichen bei Einspeisung
gegenprüfen.
Guide: [sigenergy.md](guides/sigenergy.md)

### SMA Smart Energy

**Feldtest.** Sunny Tripower Smart Energy, Sunny Boy Storage, Sunny Boy Smart Energy.
Steuerung über direktes Modbus TCP (Port 502, externes Batteriemanagement /
CmpBMS-Register), Sensordaten über die native
[SMA Solar](https://www.home-assistant.io/integrations/sma/) Integration
(WebConnect). Den Modbus-TCP-Server aktiviert der Anlagenbetreiber selbst im
SMA-Webinterface — kein Grid-Guard-Code nötig. Watchdog wie bei Kostal, die
Steuerregister sind flüchtige Sollwerte. Bei vorhandenem Sunny Home Manager
2.0 muss dessen prognosebasiertes Laden deaktiviert werden.
Fahrplan-Steuerung seit 2.1.1-dev12: Ladeblockierung, Netz-
Sollwert und Stopp sind am Gerät verifiziert (1.x-Reihe, STP10.0-3SE-40).
Besonderheit: Die Entladung ist ein **Netz-Sollwert** (GridWSpt) — der
Wechselrichter legt die Hauslast selbst obendrauf, deshalb gibt die
Steuerung hier die geplante Einspeisung vor statt der Batterieleistung
(`discharge_is_grid_setpoint`). Offen: Feldtest der Fahrplan-Nachführung
über mehrere Tage, Koexistenz mit dem SHM 2.0 im Dauerbetrieb.
Guide: [sma.md](guides/sma.md)

### SolaX Gen4+

**Feldtest.** Steuerung über die
[SolaX Modbus](https://github.com/wills106/homeassistant-solax-modbus)
Integration (RemoteControl Mode 1).

**Entladeboden:** Der Wechselrichter stoppt die Entladung bei
`selfuse_discharge_min_soc` — auch mitten in einer befohlenen Zwangsentladung,
ohne das zu melden. Der Treiber senkt den Wert deshalb für die Dauer der
Entladung ab und schreibt danach den Vorwert zurück; der Fahrplan kennt ihn
außerdem als Untergrenze und plant nicht tiefer. Es ist also **keine** manuelle
Einstellung nötig. Wer den Wert im SolaX-Portal ändert, verschiebt damit die
Untergrenze der Planung — nach unten bringt das Ertrag, nach oben kostet es
welchen.

**Ladelimit in Ampere:** SolaX begrenzt die Ladung über einen Strom, der
Fahrplan rechnet in Leistung. Umgerechnet wird über die Batteriespannung
(Rückfallwert 400 V, wenn der Spannungssensor fehlt) — bei einer fehlenden
Spannung ist das Limit entsprechend ungenau.

Offen: Feldtest der Nachführung, insbesondere ob der abgesenkte Entladeboden
am Gerät greift und nach dem Stopp sauber zurückkommt.
Guide: [solax.md](guides/solax.md)
