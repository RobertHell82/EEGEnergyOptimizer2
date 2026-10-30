# Victron einrichten

> [!IMPORTANT]
> **Feldtest.** Der Victron-Treiber ist neu und läuft an der ersten Anlage im Feldtest. Was dort schon nachgewiesen ist und was noch offen ist, steht in der Doku unter „Stand der Unterstützung“.

## 1. Unterstützte Geräte

**Victron ESS** mit einem **GX-Gerät** (Cerbo GX, Cerbo GX MK2, Venus GX, Ekrano GX) und einem oder mehreren **Multiplus-II** oder **Quattro**, einphasig oder dreiphasig. Die PV darf an Victron-Ladereglern hängen (MPPT, Gleichstrom) oder an einem eigenen PV-Wechselrichter am Wechselstrom.

Angebunden wird über die in Home Assistant **eingebaute** Integration **Victron GX**. Sie spricht im Heimnetz direkt mit dem GX-Gerät (MQTT), ohne VRM-Cloud. Dieselbe Integration liefert die Messwerte und nimmt die Steuerbefehle an; eine HACS-Integration oder ein eigenes Modbus braucht es nicht.

Voraussetzungen:

- **Home Assistant 2026.5 oder neuer** (*Einstellungen → Info*)
- **Venus OS 3.31 oder neuer** am GX-Gerät empfohlen (*Einstellungen → Firmware*). Mit älteren Ständen wurden Probleme bei der MQTT-Anmeldung berichtet.
- Ein **Netzzähler** am GX-Gerät (z. B. Carlo Gavazzi ET340 / EM24), wie ihn ESS ohnehin braucht

_Geprüft mit drei Multiplus-II 48/5000 (dreiphasig), Cerbo GX MK2, drei SmartSolar-MPPT und Pytes-Batterie._

## 2. MQTT am GX-Gerät einschalten

Öffne die Oberfläche des GX-Geräts, am Display oder über **VRM → Remote Console**:

1. **Einstellungen → Integrationen → MQTT-Zugriff** einschalten _(ältere Firmware: Einstellungen → Dienste → MQTT)_
2. Unter **Einstellungen → Allgemein → Sicherheitsprofil lokales Netzwerk** nachsehen, welches Profil eingestellt ist. Davon hängt ab, was du in Abschnitt 3 einträgst.
3. Dem GX-Gerät im Router eine **feste IP-Adresse** geben (z. B. FRITZ!Box: *Heimnetz → Netzwerk → Gerät → „Immer die gleiche IPv4-Adresse zuweisen“*). Sonst verliert Home Assistant die Verbindung, sobald der Router eine neue Adresse vergibt.

> [!NOTE]
> MQTT einzuschalten ändert nichts am Betrieb der Anlage. Es öffnet nur einen Zugang im Heimnetz.

## 3. Integration „Victron GX“ einrichten

Oft hat Home Assistant das GX-Gerät schon selbst gefunden: Unter **Einstellungen → Geräte & Dienste** steht es dann oben bei **„Gefunden“** (z. B. „Cerbo GX (…)“). Klicke dort auf **Konfigurieren**. Sonst: **„+ Integration hinzufügen“** → nach **„Victron GX“** suchen.

> [!TIP]
> Unter „Victron“ zeigt Home Assistant mehrere Einträge. Gemeint ist **„Victron GX“**, nicht „Victron Remote Monitoring“ (die VRM-Cloud) und nicht „Victron BLE“.

Trage ein, passend zum Sicherheitsprofil:

| Feld | Profil „Unsicher“ | Profil „Schwach“ | Profil „Gesichert“ |
|---|---|---|---|
| **Host** | IP-Adresse des GX-Geräts | IP-Adresse des GX-Geräts | IP-Adresse des GX-Geräts |
| **Port** | **1883** | **1883** (oder 8883 mit SSL) | **8883** |
| **Benutzername** | leer | leer | leer |
| **Passwort** | leer | Passwort des Profils | Passwort des Profils |
| **SSL** | aus | aus (an bei 8883) | **an** |

Das Passwort ist das, das am GX-Gerät für das Sicherheitsprofil gesetzt ist, nicht das VRM-Passwort.

Fragt Home Assistant nach einer **Kopplung**: am GX-Gerät **Einstellungen → Integrationen → MQTT-Geräte → Kopplungsmodus** öffnen und innerhalb von **120 Sekunden** in Home Assistant auf **Absenden** klicken.

Danach erscheinen unter **Einstellungen → Geräte & Dienste → Victron GX** mehrere Geräte, typischerweise **GX Device** (die Anlage), **ESS**, der Multiplus (VE.Bus), die **Batterie**, der **Netzzähler** und je Laderegler ein **Solarcharger**.

> [!WARNING]
> **Nicht parallel eingreifen.** Die Integration legt auch Entitäten an, mit denen man die Anlage verstellen kann (Netzsollwert, Ladestrom, ESS-Modus …). Zwei davon schreibt der Optimizer selbst (siehe unten). Wer sie gleichzeitig in Home Assistant, der Remote Console oder per Node-RED ändert, überschreibt den Optimizer, oder umgekehrt.

## 4. Einstellungen am GX-Gerät

Der Einrichtungsassistent liest diese Einstellungen über die Integration und zeigt sie im Schritt „Wechselrichter“ an. Steht dort ein **Problem**, lässt er dich nicht weiter, bis es am GX-Gerät behoben ist; danach den Wechselrichter im Assistenten erneut anklicken.

| Einstellung | Wo am GX-Gerät | Soll | Ohne das |
|---|---|---|---|
| **ESS-Modus** | Einstellungen → ESS → Modus | **Optimiert** (mit oder ohne BatteryLife) | Der Netzsollwert wirkt nicht |
| **Dynamic ESS** | Einstellungen → Dynamic ESS | **Aus** | Dynamic ESS überschreibt den Netzsollwert |
| **Geplantes Laden** | Einstellungen → ESS → Geplantes Laden | alle Zeitfenster **aus** | Lädt zu festen Zeiten aus dem Netz und hält die Batterie dort gegen jede Entladung; der Fahrplan weiß davon nichts |
| **DVCC** | Einstellungen → DVCC | **An** | Das Ladelimit wirkt nicht |
| **Einspeisung begrenzen** | Einstellungen → ESS → Netzeinspeisung | nicht begrenzt oder mindestens 100 W | Einspeisen aus der Batterie ist gesperrt |
| **DC-gekoppelte PV – Überschuss einspeisen** | Einstellungen → ESS → Netzeinspeisung | **An** (empfohlen) | Ein Ladelimit regelt die Laderegler ab, statt den Überschuss ins Netz zu geben. Nur ein Hinweis, kein Hindernis, und nur bei Ladereglern (MPPT) |

Der **Mindest-Ladestand** unter *Einstellungen → ESS* bleibt deine Einstellung. Der Optimizer liest ihn und plant nie darunter (bei BatteryLife zählt die höhere, gerade aktive Grenze).

## 5. Sensoren

Der Optimizer erkennt die Messwerte automatisch über ihre Kennung in der Integration, auch wenn Geräte umbenannt oder auf Deutsch angelegt sind:

| Größe | Quelle | Hinweis |
|---|---|---|
| Ladestand | GX Device → DC-Batterie Ladestand | |
| PV-Leistung | GX Device → PV-Leistung | Summe aller Laderegler (DC) |
| PV-Leistung 2 (optional) | **selbst wählen** | Nur mit PV-Wechselrichter am Wechselstrom: ein Sensor mit seiner **gesamten** Leistung über alle Phasen, etwa aus seiner eigenen Integration. Das GX-Gerät meldet AC-PV nur je Phase, deshalb erkennt der Assistent ihn nicht. Wird zur DC-PV addiert |
| Netzleistung | Netzzähler → Leistung | Summe aller Phasen; positiv = Bezug, wird automatisch umgerechnet |
| Batterieleistung | GX Device → DC-Batterie Leistung | positiv = Laden |
| Kapazität | **von Hand** | Victron liefert die Kapazität nur in Ah. Trage die nutzbare Kapazität in kWh ein, z. B. 4 × Pytes V5 (je 5,12 kWh) = **20,48 kWh** |

> [!NOTE]
> **Netzleistung immer vom Netzzähler.** Eine einzelne Phase (etwa „Netzleistung L1“ am GX Device) reicht bei einer dreiphasigen Anlage nicht: Mit Phasenausgleich kann L1 1,4 kW Bezug zeigen, während der Zähler insgesamt fast null misst. Ohne Netzzähler erkennt der Assistent die Netzleistung nicht, dann wählst du den Sensor selbst aus.

Der Hausverbrauch wird aus PV, Batterie und Netz berechnet. Er enthält damit die Wandlerverluste des Multiplus. Bei hoher Entladeleistung sind das einige hundert Watt.

## Was der Optimizer am Gerät tut

| Absicht des Fahrplans | Am Gerät |
|---|---|
| **Laden begrenzen** (auch 0 kW = Laden sperren) | DVCC **Max. Ladestrom** = Leistung ÷ Batteriespannung. Gilt für das ganze System, Multiplus und Laderegler zusammen |
| **Einspeisen ins Netz** | ESS **Netzsollwert** = −geplante Einspeisung (negativ = Einspeisung). Den Hausverbrauch legt der Multiplus selbst obendrauf |
| **Aus dem Netz laden** (Beta) | DVCC **Max. Ladestrom** = geplante Ladeleistung ÷ Batteriespannung, dazu ein **positiver Netzsollwert** (Bezug). Den Sollwert führt der Optimizer alle 30 Sekunden an der Messung nach, so dass die Batterie knapp (0,2 kW) unter der geplanten Leistung lädt. Ziel: Die PV hat Vorrang und wird nicht zugunsten des Netzbezugs abgeregelt. Nimmt die Batterie weniger auf (gegen Ende der Ladung), wird der Bezug zurückgenommen statt erhöht. Am Gerät noch nicht erprobt |
| **Freigabe** (Normalbetrieb) | Netzsollwert und Ladestrom zurück auf deine Werte von vorher |

Beide Stellgrößen sind deine gespeicherten Einstellungen am GX-Gerät (Netzsollwert oft 0 oder +50 W, Ladestrom oft eine Schutzgrenze). Der Optimizer merkt sich den Wert vor seinem **ersten** Eingriff, auch über einen Neustart von Home Assistant hinweg, und schreibt ihn bei der Freigabe zurück. Steht der Ladestrom auf −1 („nicht begrenzen“), wird auch −1 zurückgeschrieben. Ältere Stände der Integration lassen −1 nicht zu. Dann meldet der Assistent ein Problem: Trag am GX-Gerät unter *DVCC* einen festen Höchststrom ein (etwa den Ladestrom des BMS) oder aktualisiere Home Assistant. Sonst stünde nach der ersten Freigabe still das Maximum der Entität statt −1.

Der Ziel-Ladestand einer Entladung kommt aus dem Fahrplan. Das GX-Gerät kennt für einen Netzsollwert keinen Ziel-Ladestand. Deshalb beobachtet der Optimizer den Ladestand selbst und nimmt den Sollwert zurück, sobald das Ziel erreicht ist. Fehlt im Plan ein Ziel, gilt die Reserve des Geräts (ESS-Mindest-Ladestand bzw. die aktive BatteryLife-Grenze). Ist der aktuelle Wert einer Stellgröße beim ersten Eingriff nicht lesbar (Verbindung kurz weg), greift der Optimizer gar nicht ein: Ohne gemerkten Wert könnte er ihn später nicht zurückstellen.

> [!WARNING]
> **Kein geräteseitiges Sicherheitsnetz.** Victron kennt für diese Einstellungen keine Rückfallzeit: Ein gesetzter Netzsollwert gilt weiter, bis er zurückgenommen wird. Der Optimizer gibt beim Umschalten auf „Aus“, bei fehlendem Plan und nach jedem Neustart aktiv frei. Stürzt Home Assistant **hart** ab (Stromausfall, eingefrorenes System), regelt das GX-Gerät auf den letzten Sollwert weiter, bis zum ESS-Mindest-Ladestand. Dann am GX-Gerät unter **Einstellungen → ESS → Netzsollwert** den gewohnten Wert (meist 0 oder 50 W) eintragen und unter **Einstellungen → DVCC** den Ladestrom prüfen.

## Zu beachten

- **Kein „External control“:** Der ESS-Modus 3 hätte eine Rückfallzeit, übergibt aber die ganze ESS-Regelung nach außen. Der Optimizer müsste dann auch den Eigenverbrauch regeln. Deshalb bleibt ESS in „Optimiert“, geschrieben werden nur Netzsollwert und Ladestrom.
- **Spitzenkappung mit der Batterie** gibt es für Victron noch nicht. Verbraucher abschalten funktioniert wie bei jedem Wechselrichter.
- **Batterie aus dem Netz laden** (Beta, Expertenmodus, Einstellungen → Tarife) funktioniert auch mit Victron, siehe Anleitung „Tarife & Gemeinschaft“. Mit Bezug aus der Gemeinschaft lädt der Fahrplan genau in den Viertelstunden, in denen die Gemeinschaft laut PeakShare Strom übrig hat. Damit ersetzt er das **Geplante Laden** am GX-Gerät, das für die Steuerung aus sein muss. Den Ziel-Ladestand überwacht der Optimizer wie bei der Entladung selbst.

## Häufige Probleme

| Problem | Lösung |
|---|---|
| **„Verbindung fehlgeschlagen“** | MQTT am GX-Gerät aus (→ Abschnitt 2), IP falsch, oder Port passt nicht zum Profil: „Gesichert“ braucht 8883 mit SSL |
| **„Ungültige Authentifizierung“** | Das GX-Gerät lehnt das **Passwort** ab, der Benutzername spielt keine Rolle. Passwort am GX-Gerät neu setzen (*Einstellungen → Allgemein → Sicherheitsprofil lokales Netzwerk → Passwort ändern*), zum Testen ohne Sonderzeichen, und erneut eintragen. Oder über **Kopplung** einrichten (Abschnitt 3) |
| **Assistent meldet „In Home Assistant deaktiviert“** | Die Entität (Netzsollwert, DVCC-Ladestrom oder Batteriespannung) gibt es, sie ist aber abgeschaltet. Unter *Einstellungen → Geräte & Dienste → Victron GX → Entitäten* aktivieren, dann den Wechselrichter im Assistenten erneut anklicken |
| **Assistent meldet „Stellgrößen fehlen“** | Die Integration bietet Netzsollwert oder DVCC-Ladestrom nicht an. ESS am GX-Gerät einrichten (Einstellungen → ESS), in Home Assistant prüfen, ob die Entitäten der Victron-GX-Integration aktiviert sind, dann den Wechselrichter im Assistenten erneut anklicken |
| **Assistent zeigt ein Problem** | Abschnitt 4: Einstellung am GX-Gerät ändern, dann den Wechselrichter im Assistenten erneut anklicken |
| **Netzleistung nicht erkannt** | Kein Netzzähler am GX-Gerät. Netzsensor im Assistenten selbst wählen, nie eine einzelne Phase einer dreiphasigen Anlage |
| **Ladelimit ohne Wirkung** | DVCC an? „DC-gekoppelte PV – Überschuss einspeisen“ an? Läuft daneben Node-RED oder Dynamic ESS? |
| **Nach HA-Absturz speist die Batterie weiter ein** | Netzsollwert am GX-Gerät auf den gewohnten Wert zurücksetzen (siehe Warnung oben) |
