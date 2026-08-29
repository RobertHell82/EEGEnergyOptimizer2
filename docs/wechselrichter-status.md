# Wechselrichter — Stand der Unterstützung

Diese Seite ist die **einzige Wahrheitsquelle** dafür, welcher Wechselrichter
unterstützt wird. README und Doku verweisen hierher, statt eigene Listen zu
führen.

> [!IMPORTANT]
> **Unterstützt werden derzeit Fronius Gen24, Huawei SUN2000 und SolaX Gen4+.**
> Nur diese drei steuert der Fahrplan, und nur sie stehen im
> Einrichtungsassistenten zur Auswahl. Fronius und SolaX sind neu
> dazugekommen und im Feldtest — siehe unten.

## Übersicht

| Wechselrichter | Treiber im Code | Fahrplan-Steuerung | Im Assistenten wählbar |
|---|---|---|---|
| **Fronius Gen24** | vorhanden | **ja** (Feldtest) | **ja** |
| **Huawei SUN2000** | vorhanden | **ja** | **ja** |
| Kostal Plenticore | vorhanden | nein | nein |
| SMA Smart Energy | vorhanden | nein | nein |
| SolarEdge StorEdge | vorhanden | nein | nein |
| **SolaX Gen4+** | vorhanden | **ja** (Feldtest) | **ja** |

Die Treiber der übrigen Wechselrichter sind **vollständig erhalten und nur
stillgelegt** — nichts davon wurde entfernt. Sie werden Schritt für Schritt
wieder freigeschaltet, sobald die Steuerung an einer echten Anlage des
jeweiligen Typs nachgewiesen ist.

## Warum stillgelegt?

Die 2.0 hat die Steuerung vollständig ersetzt: Statt Zuständen und Zeitfenstern
setzt ein Executor alle 30 Sekunden einen 48-Stunden-Fahrplan durch — mit
Ladelimit-Nachführung, Entlade-Nachführung, Not-Aus und Failsafe. Dieser Weg
ist bisher nur für Huawei nachgewiesen. Für die anderen Treiber stammt die
Steuerverifikation aus der 1.x-Reihe und galt der alten Zustandslogik; sie
überträgt sich nicht automatisch auf die minütliche Nachführung.

Ein Treiber, der nur anzeigt, aber nicht steuert, ist für den Anwender
irreführend — deshalb steht er gar nicht erst zur Auswahl.

## Einen Wechselrichter wieder freischalten

Drei Stellen, in dieser Reihenfolge:

1. **Backend** — `custom_components/eeg_energy_optimizer/inverter/<treiber>.py`:
   Property `supports_schedule_control` auf `True` setzen (Default in
   `inverter/base.py` ist `False`). Das ist der einzige Schalter, den der
   `ScheduleExecutor` abfragt.
2. **Einrichtungsassistent** — `frontend/eeg-optimizer-panel.js`: Die Liste
   `SCHEDULE_CONTROL_INVERTERS` steuert beides — welche Karte im Assistenten
   erscheint und ob der Hinweis „nur Anzeige" gezeigt wird. Den Schlüssel des
   Treibers dort eintragen; ein bereits konfigurierter Fremdtreiber bleibt
   unabhängig davon sichtbar.
3. **Doku** — die Zeile in der Tabelle oben umstellen, den Guide in
   `docs/README.md` wieder verlinken und `python scripts/build_guides.py`
   laufen lassen.

Vor der Freigabe an einer echten Anlage nachweisen:

- Ladelimit setzen, nachführen und wieder zurücknehmen
- Erzwungene Entladung mit Ziel-Ladestand starten und stoppen
- Not-Aus greift (Netzbezug während der Entladung)
- Failsafe gibt frei (Fahrplan fehlt länger als 15 Minuten)
- Modus Ein → Aus nimmt alle Steuerwerte zurück, auch beim wiederholten
  Umschalten

Welche Sensoren ein Treiber lesen und welche Schnittstelle er anbieten muss,
steht in [NECESSARY_SENSORS_NEW_INVERTER.md](../NECESSARY_SENSORS_NEW_INVERTER.md).

## Was je Wechselrichter noch zu klären ist

Alphabetisch. Die Guides in `docs/guides/` beschreiben die Einrichtung
unverändert weiter und bleiben erhalten.

### Fronius Gen24

**Freigegeben, im Feldtest.** Steuerung über direktes Modbus TCP (SunSpec
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

plus/G2/G3, Steuerung über direktes Modbus TCP (Port 1502, proprietäre
Batterie-Steuerregister), Sensordaten über die native
[Kostal Plenticore](https://www.home-assistant.io/integrations/kostal_plenticore/)
Integration (REST). Die Umstellung der Batteriesteuerung auf „Extern über
Protokoll (Modbus TCP)" liegt im Servicemenü und erfordert einen
**Installateur-Login**. Kostal erwartet zyklische Steuerbefehle (Watchdog):
Fällt Home Assistant aus, kehrt der Wechselrichter zur internen Automatik
zurück. Die Steuerregister sind flüchtig (RAM) — kein NVRAM-Verschleiß.
Offen: Feldtest der Fahrplan-Nachführung; das Encoding der Einspeisebegrenzung
war zuletzt noch ungeklärt.
Guide: [kostal.md](guides/kostal.md)

### SMA Smart Energy

Sunny Tripower Smart Energy, Sunny Boy Storage, Sunny Boy Smart Energy.
Steuerung über direktes Modbus TCP (Port 502, externes Batteriemanagement /
CmpBMS-Register), Sensordaten über die native
[SMA Solar](https://www.home-assistant.io/integrations/sma/) Integration
(WebConnect). Den Modbus-TCP-Server aktiviert der Anlagenbetreiber selbst im
SMA-Webinterface — kein Grid-Guard-Code nötig. Watchdog wie bei Kostal, die
Steuerregister sind flüchtige Sollwerte. Bei vorhandenem Sunny Home Manager
2.0 muss dessen prognosebasiertes Laden deaktiviert werden.
Offen: Feldtest der Fahrplan-Nachführung, Koexistenz mit dem SHM 2.0 im
Dauerbetrieb.
Guide: [sma.md](guides/sma.md)

### SolarEdge StorEdge

Steuerung über die
[SolarEdge Modbus Multi](https://github.com/WillCodeForCats/solaredge-modbus-multi)
Integration, maximal 2 Wechselrichter.

> [!WARNING]
> **Der kritische Punkt für die 2.0:** SolarEdge persistiert die
> Steuerregister im Flash-Speicher (NVRAM) des Wechselrichters, und Flash hat
> eine begrenzte Zahl an Schreibzyklen (typisch 100.000+). Der Fahrplan führt
> minütlich nach — bevor SolarEdge freigegeben wird, muss geklärt sein, wie
> oft dabei tatsächlich geschrieben wird und ob die Totbänder das ausreichend
> begrenzen. Solange nicht gesteuert wird, fallen keine Schreibvorgänge an.

Guide: [solaredge.md](guides/solaredge.md)

### SolaX Gen4+

**Freigegeben, im Feldtest.** Steuerung über die
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
