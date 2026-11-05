# Wechselrichter — Stand der Unterstützung

Diese Seite ist die **einzige Stelle**, an der steht, wie weit jeder
Wechselrichter erprobt ist und was noch offen ist. Die übrige Doku nennt die
Geräte nur mit dem Link auf ihre Anleitung und verweist für den Stand hierher.

> [!IMPORTANT]
> **Unterstützt werden Fronius Gen24, Huawei SUN2000, Kostal Plenticore,
> Sigenergy SigenStor, SMA Smart Energy und SolaX Gen4+, dazu Victron ESS im
> Feldtest.** Alle sieben stehen im Einrichtungsassistenten zur Auswahl, und
> bei allen sieben setzt die Steuerung den Fahrplan am Gerät durch
> (Ladelimit und erzwungene Entladung) — keiner wird nur ausgelesen.

## Übersicht

| Wechselrichter | Anbindung | Stand |
|---|---|---|
| **Fronius Gen24** | direkt per Modbus TCP | **freigegeben** |
| **Huawei SUN2000** | Huawei-Solar-Integration | **freigegeben** |
| **Kostal Plenticore** | direkt per Modbus TCP | **freigegeben** |
| **Sigenergy SigenStor** | Sigenergy-Local-Modbus-Integration | **freigegeben** |
| **SMA Smart Energy** | direkt per Modbus TCP | **freigegeben** |
| **SolaX Gen4+** | SolaX-Modbus-Integration | **freigegeben** |
| **Victron ESS** | eingebaute Integration Victron GX (MQTT) | **Feldtest** |

**Freigegeben** heißt: im Dauerbetrieb an echten Anlagen erprobt —
Ladelimit, Entladung und Freigabe wirken am Gerät, die Sicherheitsnetze der
Steuerung (Not-Aus, Failsafe, Freigabe bei Aus und beim Entladen der
Integration) greifen. Das gilt für alle außer Victron. Die Rückfallzeit *im Gerät*
(Watchdog nach einem Ausfall von Home Assistant) ist damit nicht bei jedem
nachgewiesen: Was je Gerät noch offen ist oder fehlt, steht unten.

**Feldtest** heißt: Der Treiber ist fertig und steht im Assistenten zur
Auswahl, läuft aber erst an einer Anlage und wird dort begleitet. Ein neu
hinzukommender Wechselrichter läuft zunächst so, bis er die Prüfungen oben
bestanden hat (siehe `docs/DEVELOPMENT.md`).

### Funktionen je Wechselrichter

Über Ladelimit und Entladung hinaus gibt es zwei Wege, die jeder Treiber
eigens können muss: **Batterie aus dem Netz laden** (Beta, Expertenmodus,
dazu die Mindestladung am Abend) und den **Entladedeckel** für die
Batterie-Stufe der Spitzenkappung. Beide zusammen ergeben „Reserve notfalls
aus dem Netz nachladen“. Seit November 2026 können das alle sieben, mit zwei
Ausnahmen: SolaX lädt nur als Gen4/Gen5 aus dem Netz (Gen6 hat keinen
Mode 1), und Victron deckelt nur, wenn die Integration für „Max.
Wechselrichterleistung“ den Wert −1 schreiben kann. Fehlt der Deckel, schaltet
die Spitzenkappung nur Verbraucher.

| Wechselrichter | Aus dem Netz laden (Beta) | Entladedeckel der Spitzenkappung |
|---|---|---|
| **Fronius Gen24** | Ladepunkt im Model 124 mit `ChaGriSet` = 1 | `OutWRte` mit `StorCtl_Mod` |
| **Huawei SUN2000** | `forcible_charge_soc` — **am Gerät geprüft** | Number „Maximale Entladeleistung“ |
| **Kostal Plenticore** | negativer Sollwert in Register 1034 | Register 1040 (Beta) |
| **Sigenergy SigenStor** | „Command Charging (PV First)“ | ESS Max Discharging Limit (Beta) |
| **SMA Smart Energy** | CmpBMS-Betriebsart 2289 | `BatDschMaxW` im CmpBMS-Block |
| **SolaX Gen4+** | Mode 1 mit positiver Leistung, nur Gen4/Gen5 | `battery_discharge_max_current` |
| **Victron ESS** | DVCC-Ladestrom + positiver Netzsollwert | „Max. Wechselrichterleistung“ (Beta, nur mit −1) |

Das Netzladen ist außer bei Huawei an keinem Gerät geprüft. Wo das Gerät
keinen Ziel-Ladestand kennt (alle außer Huawei), beobachtet der Treiber den
Ladestand selbst und gibt am Ziel frei. Was je Gerät offen ist, steht in den
Abschnitten unten.

## Am Gerät geprüft (Diagnose der Ansteuerung)

Was die **Diagnose der Ansteuerung** (Einstellungen → System, seit 2.1.70)
an echten Anlagen gemessen hat: Jeder Befehl wird geschrieben,
zurückgelesen und auf seine Wirkung an Batterie- und Netzleistung geprüft.
Manches lässt sich nur zu bestimmten Zeiten prüfen. Das Ladelimit braucht
eine Batterie, die gerade aus PV lädt. Der Entladedeckel und die Netzladung
brauchen einen Abend, an dem das Haus mehr braucht, als die PV liefert.

✓ = wirkt am Gerät · ◐ = angenommen und zurückgelesen, Wirkung noch nicht
prüfbar · ✗ = Problem gefunden · – = noch nicht geprüft

| Wechselrichter | Ladelimit | Zwangsentladung | Freigabe | Entladedeckel | Netzladung |
|---|---|---|---|---|---|
| **Fronius Gen24** | ✓ | ✓ | ✓ | ◐ | ◐ |
| **Huawei SUN2000** | ◐ ¹ | ✓ | ✓ | ◐ | – ² |
| **Kostal Plenticore** | – | – | – | – | – |
| **Sigenergy SigenStor** | ✓ | ✓ ³ | ✓ | ◐ | ◐ |
| **SMA Smart Energy** | – | – | – | – | – |
| **SolaX Gen4** | ✓ | ✓ | ✓ | ◐ ⁴ | ◐ |
| **Victron ESS** | – | – | – | – | – |

Stand 05.11.2026, je eine Anlage, alle Läufe mittags mit PV-Überschuss.

1. Batterie war voll. 0 kW und 1 kW wurden geschrieben und korrekt
   zurückgelesen, die Wirkung ist noch offen.
2. In der Diagnose übersprungen (Batterie voll). Im Betrieb ist das
   Netzladen am Gerät geprüft (Oktober 2026).
3. Der frühere Modus „Command Discharging (ESS First)“ regelte die ganze
   PV ab (17,9 → 0 kW). Seit 2.1.72 entlädt der Treiber mit „PV First“, am
   Gerät bestätigt: Die Batterie gibt 1,00 kW ab, die PV läuft mit 12,4 kW
   weiter, die Einspeisung steigt von 3,7 auf 13,0 kW.
4. Die Stellwerte zeigen den Entladestrom nicht. Belegen kann den Deckel
   nur die gemessene Wirkung, nicht das Zurücklesen.

## Was je Wechselrichter noch zu klären ist

Alphabetisch. Die Einrichtung selbst beschreibt jeweils der verlinkte Guide.

### Fronius Gen24

**Freigegeben.** Steuerung über direktes Modbus TCP (SunSpec
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

**Beta: Batterie aus dem Netz laden** (Expertenmodus): ein erzwungener
Ladepunkt im Model 124 (`InWRte` = +x, `OutWRte` = −x, `StorCtl_Mod` = 3) mit
`ChaGriSet` = 1. Den Vorwert von `ChaGriSet` sichert der Treiber und schreibt
ihn bei jeder Freigabe zurück; den Ziel-Ladestand kennt der Gen24 fürs Laden
nicht, der Treiber liest `ChaState` und gibt am Ziel frei. **Noch nicht am
Gerät geprüft:** ob `ChaGriSet` allein reicht oder im Webinterface zusätzlich
das Laden aus dem Netz erlaubt sein muss, ob die Ladeleistung bei
schwankender PV hält, und ob die Freigabe sauber in den Automatikbetrieb
zurückführt.
Guide: [fronius.md](guides/fronius.md)

### Huawei SUN2000

**Freigegeben.** Single oder Master/Slave (mehrere Wechselrichter + Batterien),
Steuerung über die [Huawei Solar](https://github.com/wlcrs/huawei_solar)
Integration. Direkte Anbindung an Wechselrichter/Dongle oder über das
EMMA-Energiemanagement (`sensor.emma_*`-Sensoren, Netz-Vorzeichen wird
automatisch korrigiert).
**Beta: Batterie aus dem Netz laden** (Expertenmodus):
Zwangsladung über `forcible_charge_soc` mit Leistung und Ziel-Ladestand aus
dem Fahrplan. Am Gerät geprüft (Traun, 12.10.2026): lädt auch mit
ausgeschaltetem „Laden aus dem Netz“, 3 s nach dem Befehl. Beschreibung: [tarife.md](guides/tarife.md).
Guide: [huawei.md](guides/huawei.md) · [Akkukapazität-Sensor](guides/capacity_sensor.md)

### Kostal Plenticore

**Freigegeben.** plus/G2/G3, Steuerung über direktes Modbus TCP (Port 1502, proprietäre
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
zur internen Automatik zeigt (nach Ablauf seines Watchdog-Timeouts, beobachtet:
bis 15 min) — der Watchdog-Timeout ist
per Modbus nicht lesbar und je Anlage verschieden.

**Beta: Entladedeckel der Spitzenkappung** über Register 1040 (max.
Entladeleistung, W). Der Keepalive schreibt ihn neben dem Ladelimit (1038)
zyklisch neu; beim Aufheben schreibt der Treiber den Wert von vorher zurück
(ohne gesicherten Vorwert die Entladegrenze der Batterie aus Register 1078).
Nach einer Entladung oder Netzladung wartet auch der Deckel auf den
Rückfall — jeder Schreibzugriff auf 1040 hielte sonst den alten 1034-Sollwert
fest.

**Beta: Batterie aus dem Netz laden** (Expertenmodus): negativer Sollwert in
1034 (DC-Batterieleistung — PV zuerst, der Rest aus dem Netz). Vorher gehen
1038 und 1040 auf ihre Vorwerte zurück. Einen Ziel-Ladestand kennt der
Plenticore nicht; der Treiber liest Register 210 bei jedem Schreiben und im
Keepalive und hält am Ziel mit 1034 = 0 an, danach gilt dieselbe
Rückfall-Wartezeit wie nach einer Entladung. Neu gestartet wird erst
2 Prozentpunkte unter dem erreichten Ziel — sonst pendelte die Ladung im
Rückfall zwischen Ziel und Stillstand.

**Österreich: Gradientensperre.** Nach jeder Netzzuschaltung ist 1034 rund
10 Minuten gesperrt (Kostal-Spezifikation, Fußnote 7) — Entladung und
Netzladung kommen in dieser Zeit mit dem Grund „1034 gesperrt (Gradient …)“
zurück, der Executor versucht es gebremst erneut. 1038 und 1040 sind nicht
betroffen.

**Noch nicht am Gerät geprüft** (auch nicht in Ansfelden):
- Netzladung: lädt 1034 = −1000 W nachts (ohne PV) 5 Minuten lang wirklich
  1 kW aus dem Netz?
- Deckel: hält 1040 = 1000 W nachts bei mehr als 1 kW Hauslast die Batterie
  bei 1 kW? Belegt ist bisher nur 1040 = 0 (aus evcc). Und: setzt der
  Watchdog-Rückfall 1040 zurück oder bleibt der Wert stehen?
- Deckel 1040 allein (ohne 1034) nachts mit Hauslast: Bleibt der
  Eigenverbrauch normal, oder steht die Batterie?
- Watchdog-Timeout der Anlage im Servicemenü ablesen — er bestimmt, wie lange
  die Batterie nach einer Entladung oder Netzladung stillsteht.

Guide: [kostal.md](guides/kostal.md)

### Sigenergy SigenStor

**Freigegeben.** SigenStor-Anlagen (EC-/CMU-Serie) ab Firmware
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

Ladelimit: Der Modus bleibt „Maximum Self Consumption“, geschrieben wird nur
das Limit — „Command Charging“ wird nicht verwendet, weil es aus dem Netz lädt.
Das Limit wirkt im Eigenverbrauchsmodus laut Anwenderberichten erst ab
Firmware **SPC113**; auf älterer Firmware bleibt es wirkungslos (die Anlage
lädt dann mit Überschuss weiter, aus dem Netz lädt sie nicht). Den
Entlade-Cut-Off (40048) liest der Optimizer als Untergrenze, schreibt ihn aber
nicht.

**Lade- und Entladelimit wirken global.** Laut Spezifikation V2.9 gelten
„ESS Max Charging Limit“ (40032) und „ESS Max Discharging Limit“ (40034) in
jedem Betriebsmodus — auch nach dem Ausschalten des Remote EMS im eigenen EMS
des Geräts. Bis 2.1.62 blieben sie bei der Freigabe stehen (ein Ladelimit 0
hätte das Laden weiter gesperrt). Seitdem setzt jede Freigabe beide auf die
Nennleistung zurück, bevor Remote EMS ausgeht, und jeder Befehl stellt das
Limit, das er nicht braucht, ebenfalls auf die Nennleistung. Findet die
Freigabe Remote EMS schon ausgeschaltet vor, setzt sie nur zurück, was
nachweislich vom Optimizer stammt — ohne Remote EMS einzuschalten: einen Wert,
den der Treiber selbst geschrieben und gemerkt hat (Store
`eeg_energy_optimizer.sigen_limits`), oder ohne Merker ein Limit von höchstens
0,01 kW (unsere Signatur: ein Ladelimit 0 aus der Zeit bis 2.1.62, ein
Entladedeckel „halten“, nach dem jemand Remote EMS von Hand ausgeschaltet hat).
Ein Limit, das der Betreiber selbst gesetzt hat, bleibt stehen. Ohne Sensor
„ESS Rated Charging/Discharging Power“ ist „zu niedrig“ nicht entscheidbar —
dann setzt der Treiber bei ausgeschaltetem Remote EMS nur die Signatur zurück
(auf das Maximum der Zahl-Entität) und meldet das Fehlen einmal im Log; lehnt
das Gerät das Maximum ab, scheitert die Freigabe mit dem Grund
„Nennleistungs-Sensor fehlt“ — dann die beiden Sensoren aktivieren. Scheitert
nach einem Moduswechsel nur das Zurücksetzen des anderen Limits, gilt der
Befehl trotzdem (der Executor muss eine laufende Entladung kennen) und das
Limit wird nachgeholt. Offen am Gerät: ob die Limits nach dem Ausschalten
wirklich auf der Nennleistung stehen.

**Entladedeckel der Spitzenkappung und Netzladen (Beta, am Gerät nicht
geprüft).** Der Deckel ist das Entladelimit (40034) im Modus „Maximum Self
Consumption“, neben einem laufenden Ladelimit; „halten“ schreibt der Treiber
als 0,01 kW, weil das Gerät eine 0 laut Maintainer der Integration oft als
„nicht gesetzt“ liest. Netzladen nutzt „Command Charging (PV First)“ mit dem
Ladelimit als Leistung — genau der Modus, der am 11.09.2026 an der Testanlage
aus dem Netz geladen hat. Das Gerät hört am Ziel-Ladestand nicht von selbst
auf; der Treiber beobachtet den Ladestand und gibt am Ziel frei.

> [!WARNING]
> **Netzladen ohne Watchdog.** Stürzt Home Assistant während einer Netzladung
> ab, lädt das Gerät weiter, bis die Batterie voll ist — aus dem Netz, soweit
> die Sonne nicht reicht. Abhilfe: Remote EMS von Hand ausschalten **und**
> „ESS Max Charging Limit“ / „ESS Max Discharging Limit“ auf die
> Nennleistung stellen — die Limits wirken auch bei ausgeschaltetem Remote
> EMS. Dasselbe gilt für einen stehenden Entladedeckel.

Am Gerät zu prüfen, bevor die Beta-Funktionen als erprobt gelten:

1. Deckel 1 kW und 0 (= 0,01 kW) im Eigenverbrauch nachts: hält die Batterie,
   kommt der Rest der Hauslast aus dem Netz?
2. Lädt die PV bei stehendem Deckel tagsüber weiter in die Batterie?
3. „Command Charging (PV First)“ mit 2 kW bei 5 kW PV: Wohin geht der
   Überschuss — ins Haus, ins Netz oder wird er abgeregelt?
4. Stehen Lade- und Entladelimit nach dem Ausschalten des Remote EMS auf der
   Nennleistung?
Guide: [sigenergy.md](guides/sigenergy.md)

### SMA Smart Energy

**Freigegeben.** Sunny Tripower Smart Energy, Sunny Boy Storage, Sunny Boy Smart Energy.
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
(`discharge_is_grid_setpoint`).

**Beta: Batterie aus dem Netz laden** (Expertenmodus): derselbe
CmpBMS-Block mit Betriebsart 2289 („Laden“), minimaler und maximaler
Ladeleistung = geplante Batterieleistung, Entladen 0 und Netz-Sollwert 0 —
keine zusätzlichen Register. Die PV deckt davon, was sie kann, der Rest kommt
aus dem Netz. Einen Ziel-Ladestand kennt das CmpBMS nicht: Der Treiber liest
den Ladestand (Register 30845) bei jedem Schreiben und alle 60 Sekunden und
reagiert dazwischen auf jede Änderung des SOC-Sensors; am Ziel geht es
zurück in den Normalbetrieb. Scheitert diese Freigabe, wiederholt der
Keepalive nur noch sie — auch wenn 30845 knapp unter dem Ziel steht, das der
Sensor schon gemeldet hat; den Ladeblock (und damit den Watchdog) erneuert er
nicht mehr. **Noch nicht am Gerät geprüft:** ob 2289 mit gleicher minimaler
und maximaler Ladeleistung tatsächlich aus dem Netz lädt (eine Forennotiz
spricht von „nur bei Einspeisung am Netzanschlusspunkt“, vermutlich nur mit
minimaler Ladeleistung 0), ob die minimale Ladeleistung als erzwungene
Leistung wirkt, ob sich Sunny Boy Storage (AC-gekoppelt) und Sunny
Tripower / Sunny Boy Smart Energy dabei gleich verhalten, und ob Register
30845 und der SOC-Sensor in Home Assistant denselben Ladestand zeigen —
laufen sie auseinander, endet die Ladung zu früh oder zu spät, und die
Telemetrie meldet eine Netzladung ohne Wirkung (`guard_wirkung`), die keine
ist. Mit Sunny Home
Manager 2.0: Firmware ab 2.15.5, „Prognosebasiertes Laden“ aus.
Guide: [sma.md](guides/sma.md)

### SolaX Gen4+

**Freigegeben.** Steuerung über die
[SolaX Modbus](https://github.com/wills106/homeassistant-solax-modbus)
Integration (RemoteControl Mode 1).

**Rückfall:** Die Zwangsentladung ist ein Mode-1-Befehl mit 300 s Laufzeit
(`remotecontrol_duration`), den die SolaX-Modbus-Integration 600 s lang selbst
wiederholt (`remotecontrol_autorepeat_duration`). Seit etwa solax_modbus
2026.10 schreibt die Integration bei Ablauf dieser Wiederholung ausdrücklich
„Disabled" — der Treiber löst den Befehl deshalb alle 240 s selbst erneut
aus, solange er läuft (bis 2.1.62 nicht: Mit den damaligen 60 s Wiederholung
endete eine geplante Entladung nach etwa einer Minute). Fällt Home Assistant
aus, fehlen beide Wiederholungen, und der Befehl endet am Gerät spätestens
300 s nach dem letzten Schreiben. Das Ladelimit dagegen ist ein
Ladestrom (`battery_charge_max_current`) ohne Rückfallzeit: Nach einem harten
Absturz von Home Assistant bleibt es stehen, bis die Freigabe beim nächsten
Start den gesicherten Vorwert zurückschreibt.

**Entladeboden:** Der Wechselrichter stoppt die Entladung bei
`selfuse_discharge_min_soc` — auch mitten in einer befohlenen Zwangsentladung,
ohne das zu melden. Der Treiber senkt den Wert deshalb für die Dauer der
Entladung ab und schreibt danach den Vorwert zurück; der Fahrplan kennt ihn
außerdem als Untergrenze und plant nicht tiefer. Es ist also **keine** manuelle
Einstellung nötig. Wer den Wert im SolaX-Portal ändert, verschiebt damit die
Untergrenze der Planung — nach unten bringt das Ertrag, nach oben kostet es
welchen.

**Gen6 ohne Mode 1:** Gen6 kennt RemoteControl Mode 1 nicht. Dort wirkt nur
das Ladelimit (`battery_charge_max_current`); Zwangsentladung und Netzladen
nicht. Fehlen die Mode-1-Entitäten, meldet der Treiber
`supports_forced_discharge = False`: Der Plan rechnet zwar weiter mit
Einspeisung, die Steuerung gibt in diesen Slots aber frei, statt eine
Zwangsentladung zu befehlen (bis 2.1.63 schrieb der Treiber an Entitäten, die
es nicht gibt, und meldete Erfolg; danach scheiterte jede geplante Entladung
mit „Mode 1 nicht verfügbar“ — Schreibbremse und tägliche
`inverter_write`-Telemetrie). Netzladen bietet der Treiber nicht an —
der Reiter **Netzladen** erscheint im Panel trotzdem, wirkt aber nicht. Ob
Gen6 eine Zwangsentladung auf anderem Weg kann, ist am Gerät nicht geklärt.

**Ladelimit in Ampere:** SolaX begrenzt die Ladung über einen Strom, der
Fahrplan rechnet in Leistung. Umgerechnet wird über die Batteriespannung
(Rückfallwert 400 V, wenn der Spannungssensor fehlt) — bei einer fehlenden
Spannung ist das Limit entsprechend ungenau.

**Beta: Batterie aus dem Netz laden** (Expertenmodus, nur Gen4/Gen5 — Gen6
hat keinen Mode 1, dort bietet der Treiber es nicht an): derselbe
Mode-1-Befehl wie die Entladung, mit positiver Leistung
(`remotecontrol_active_power` > 0). Die Batterie lädt diese Leistung, die
Integration nimmt zuerst die PV und den Rest aus dem Netz. Ein stehendes
Ladelimit (`battery_charge_max_current`) stellt der Treiber vorher auf den
gesicherten Vorwert zurück. Einen Ziel-Ladestand kennt Mode 1 nicht: Der
Treiber beobachtet den SOC-Sensor und gibt am Ziel frei; scheitert diese
Freigabe, wiederholt er sie im 240-s-Takt, auch wenn der Ladestand inzwischen
wieder unter dem Ziel liegt. Ist der SOC-Sensor nicht lesbar, gibt es keinen
Stopp am Ziel — die Ladung endet erst mit dem nächsten Befehl der Steuerung,
und der Treiber warnt einmal je Befehl im Log.
`remotecontrol_import_limit` bleibt unberührt — die Netzgrenze führt die
Steuerung über die Ladeleistung nach. **Noch nicht am Gerät geprüft:** ob
die Batterie mit der befohlenen Leistung lädt und PV dabei Vorrang hat, ob
ein Ladestrom von 0 A die Mode-1-Ladung tatsächlich deckelt (und das
Zurückstellen damit nötig ist), ob die Ladung bei schwankender PV und
Hauslast hält, ob der Keepalive sie ohne Lücke über den Ablauf der
Wiederholung trägt, und ob die Freigabe am Ziel sauber in Self Use
zurückführt.

Guide: [solax.md](guides/solax.md)

### Victron ESS

**Feldtest** (seit Oktober 2026, eine Anlage: drei Multiplus-II 48/5000
dreiphasig, Cerbo GX MK2, drei MPPT-Laderegler, Pytes-Batterie,
Netzzähler). Steuerung und Messwerte über die in Home Assistant eingebaute
Integration [Victron GX](https://www.home-assistant.io/integrations/victron_gx/)
(Domain `victron_gx`, MQTT im Heimnetz, ab Home Assistant 2026.5) — keine
HACS-Integration, kein eigenes Modbus. Am GX-Gerät muss MQTT eingeschaltet
sein; Venus OS ab 3.31 empfohlen.

Ladelimit = DVCC „Max. Ladestrom“ (Leistung ÷ Batteriespannung, gilt für
Multiplus und Laderegler zusammen), Entladung = ESS-Netzsollwert negativ.
Wie bei SMA ist die Entladung ein **Netz-Sollwert** — der Multiplus legt die
Hauslast selbst obendrauf, die Steuerung gibt die geplante Einspeisung vor
(`discharge_is_grid_setpoint`). Beide Werte (und der Entladedeckel, unten) sind gespeicherte Einstellungen
des GX-Geräts: Der Wert vor dem ersten Eingriff wird gesichert und bei der
Freigabe zurückgeschrieben, auch über einen Neustart hinweg.

Der Einrichtungsassistent prüft die GX-Einstellungen über die Integration
und lässt erst weiter, wenn ESS-Modus „Optimiert“, Dynamic ESS aus,
DVCC an und die Einspeisegrenze nicht begrenzt oder mindestens 100 W ist
(eingeschaltetes **Geplantes Laden** ist eine rote Warnung, die sich mit
„Trotzdem weitermachen“ übergehen lässt),
Netzsollwert, DVCC-Ladestrom und Batteriespannung in Home Assistant aktiv sind und der
Ladestrom nicht auf −1 steht, wo die Integration −1 nicht zurückschreiben kann
(ältere Stände, Bereich 0–100 A).

> [!WARNING]
> **Kein geräteseitiges Failsafe.** Netzsollwert und Ladestrom haben am
> GX-Gerät keine Rückfallzeit. Nach einem harten Absturz von Home Assistant
> regelt das GX-Gerät auf den letzten Sollwert weiter, bis zum
> ESS-Mindest-Ladestand. Den Ziel-Ladestand einer Entladung überwacht der
> Treiber deshalb selbst, und die Steuerung gibt bei „Aus“, fehlendem Plan
> und nach jedem Start aktiv frei.

Am Gerät nachgewiesen (29.10.2026, nur gelesen): Vorzeichen — Batterie
positiv = Laden, Netzzähler positiv = Bezug (wird gedreht); alle Stell- und
Lesewerte werden über ihre Kennung gefunden. Die Netzleistung kommt nur vom
Netzzähler: Eine einzelne Phase zeigte mit Phasenausgleich 1,4 kW Bezug,
wo der Zähler fast null maß. Aus demselben Grund wird PV an einem AC-Wechselrichter
nicht erkannt (das GX meldet sie nur je Phase), sie wird im Assistenten von
Hand gewählt.

Offen:

- Begrenzt DVCC-Ladestrom 0 A das Laden vollständig, und speisen die
  Laderegler den Überschuss dann ein („DC-gekoppelte PV – Überschuss
  einspeisen“)?
- Folgt der Multiplus einem negativen Netzsollwert mit voller Leistung,
  auch dreiphasig mit Phasenausgleich?
- Wie schnell kommen Messwerte und Befehle über MQTT an?
- Die Feldtest-Anlage hat „Geplantes Laden“ eingeschaltet (drei
  Zeitfenster täglich). Der Assistent warnt, lässt aber weiter; in den
  Zeitfenstern wirkt der Fahrplan nicht. Zu klären, ob der Betreiber es
  aufgibt.
- Freigabe beim Start, Failsafe, Not-Aus und Wirkungskontrolle am Gerät
  (Checkliste in `docs/DEVELOPMENT.md`).
- Netzladung (Beta): Lädt die Batterie mit DVCC-Deckel und positivem
  Netzsollwert PV zuerst, und hält die Nachführung (alle 30 s, Batterie
  0,2 kW unter dem Deckel) den Bezug so, dass die PV nicht abgeregelt wird?

- Spitzenkappung (Beta): Welchen Wertebereich meldet die Integration für
  „Max. Wechselrichterleistung“ auf der Feldtest-Anlage, und ist −1
  schreibbar? Hält der Multiplus die Batterie bei einem Deckel knapp über
  der DC-PV wirklich (folgt hub4control), und reicht die Nachführung der
  DC-PV alle 30 s, ohne die Laderegler abzuregeln?
- Laut ESS-Handbuch (§ 4.3.5) ist der DVCC-Ladestromdeckel **inaktiv**,
  solange „DC-gekoppelte PV – Überschuss einspeisen“ an ist — der Assistent
  empfiehlt das aber gerade für das Ladelimit. Am Gerät zu klären, ob das
  Ladelimit mit DC-gekoppelter PV dann überhaupt wirkt.

**Spitzenkappung mit der Batterie (Beta):** Victron hat keine Grenze für die
Entladeleistung der Batterie, nur die ESS-Einstellung „Max.
Wechselrichterleistung“ (`Settings/CGwacs/MaxDischargePower`, −1 =
unbegrenzt). Sie deckelt den ganzen Wandler des Multiplus, Summe aller
Phasen, und die Leistung der MPPT-Laderegler läuft mit durch. Ein Deckel
von 0 W ließe deshalb auch die Sonne vom Dach nicht mehr ins Haus. Der
Treiber schreibt Deckel + gemessene DC-PV + Verluste (5 % und 50 W) und
führt das alle 30 s an der DC-PV nach — asymmetrisch, weil jede Änderung im
Einstellungsspeicher des GX landet: steigende DC-PV ab 200 W sofort (sonst
Netzbezug, auch mit leerem Vorrat), fallende erst nach vier Takten mit
Überhang und nur aus einem Schreibvorrat (5, einer je Minute nach). Befehle
des Executors gehen immer durch. Ehrlich gerechnet: Senken im Mittel
höchstens einmal je Minute, jedes Anheben folgt einem Senken oder echtem
PV-Zuwachs — bei Wolken also bis zu etwa zwei Schreibvorgänge je Minute,
sonst weit weniger. Fehlt die DC-PV kurz (MQTT-Aussetzer), gilt bis zu
10 min die letzte Messung, ein stehender Deckel wird gehalten; erst danach
(Laderegler nachts ohne Wert) zählt sie als 0 W — der Deckel ist knapper,
greift aber.
AC-gekoppelte PV ist nicht betroffen. Der Wert vor dem ersten Eingriff
(meist −1) wird gesichert und bei der Freigabe zurückgeschrieben; eine
eigene Grenze des Betreibers bleibt die Obergrenze. Ein Wert, den der
Treiber selbst geschrieben hat, wird nie Vorwert (das MQTT-Echo kommt
verzögert — Freigabe und neuer Deckel im selben Lauf hätten sonst den alten
Deckel als „Betreiberwert“ gesichert); dasselbe gilt für Netzsollwert und
Ladestrom. Als eigen zählt nach einer Freigabe nur der letzte
Eingriffswert, und auch der nur, bis das Echo des zurückgeschriebenen
Werts ankommt — sonst gälte nach Tagen fast jeder Wert als eigen, und eine
Änderung des Betreibers am GX würde still überschrieben. Scheitert das
Aufheben des Deckels, gelten Freigabe und Entladung trotzdem, der Vorwert
bleibt für den nächsten Versuch, und der Deckel folgt weiter der DC-PV. Auch dieser Deckel hat
keine Rückfallzeit: Bleibt Home Assistant stehen, bleibt er stehen — die
Freigabe beim Start, „Aus“ und das Entladen der Integration heben ihn auf.
Fehlt die Entität, kappt die Spitzenkappung nur über Verbraucher. Kann die
Integration −1 nicht schreiben (ältere Stände, Bereich 0–100), gibt der
Assistent einen Hinweis, und es bleibt ebenfalls bei den Verbrauchern.

Batterie aus dem Netz laden (Beta) geht wie bei Huawei: DVCC-Ladestrom als
Ladeleistung, dazu ein positiver Netzsollwert, den der Treiber an der
Messung nachführt. Die Kapazität liefert Victron nur
in Ah, sie wird im Assistenten in kWh von Hand eingetragen.
Guide: [victron.md](guides/victron.md) · Vorbereitung:
[installation/victron.md](installation/victron.md)
