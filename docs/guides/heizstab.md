# Heizstab (nur Fronius Ohmpilot)

An guten Tagen erzeugt eine große PV-Anlage mehr, als Batterie, Haus und Einspeisegrenze aufnehmen. Den Rest regelt der Wechselrichter ab — er ist verloren. Mit einem Heizstab im Warmwasserspeicher wird daraus Wärme: Der Optimizer steuert einen **Fronius Ohmpilot** direkt per Modbus TCP und gibt ihm den Überschuss.

## Warum nicht das Energiemanagement des Wechselrichters?

Der Gen24 regelt den Ohmpilot so, dass die **Einspeisung auf null** geht. In einer Energiegemeinschaft soll aber bis zur Grenze eingespeist werden — dort ist der Strom Geld wert. Nur was **darüber** hinausgeht, gehört in den Heizstab. Deshalb steuert der Optimizer den Ohmpilot selbst, und deshalb muss der Ohmpilot vom Gen24 entkoppelt sein.

## Voraussetzungen

- Ein **Fronius Ohmpilot** mit Netzwerkanschluss (LAN oder WLAN) und angeschlossenem Heizstab
- Der Ohmpilot ist **vom Wechselrichter entkoppelt**: Kopplung im Gen24-Webinterface unter *Geräte- und Systemeinstellungen → Komponenten* lösen, oder den Ohmpilot in ein anderes Subnetz stellen
- Eine eingestellte **Einspeisegrenze** (siehe Anleitung „Einspeisegrenze"). Ohne sie gilt die AC-Grenzleistung des Wechselrichters minus 0,5 kW — dann startet der Heizstab praktisch nie
- Ein **Netzleistungs-Sensor**, der Einspeisung und Bezug misst

> [!CAUTION]
> **Zwei Steuerungen auf einem Gerät gehen nicht.** Beim Ohmpilot gewinnt, wer zuletzt schreibt. Bleibt er mit dem Gen24 gekoppelt, heizt der Gen24 jede Einspeisung weg und der Optimizer setzt sie zurück. Dasselbe gilt für **jede andere Steuerung**: alte Automatisierung, Node-RED-Flow, zweite Integration. Schalte erst alles andere ab, dann diese Funktion ein.
>
> Prüfe, dass wirklich nichts mehr **schreibt** — auch eine auf „Aus" gestellte Integration kann im Hintergrund weiter Sollwerte senden. Die Statuskarte meldet diesen Fall (siehe unten).

## So funktioniert es

Der Heizstab ist neben der Batterie ein **zweiter Abnehmer** für PV-Strom. Alle 30 Sekunden entscheidet sich, welche von zwei Betriebsarten gilt.

**1. Nach Plan.** Sieht der Optimierungsplan für die laufende Viertelstunde Wärme vor, ist diese Leistung die Vorgabe. Das passiert, wenn der Wärmewert höher ist als die Einspeisevergütung — dann wird geheizt, auch weit unter der Einspeisegrenze. Geregelt wird auf **Einspeisung ≈ 0**, nie über die geplante Leistung hinaus. Kommt weniger PV als vorhergesagt, sinkt der Sollwert von selbst, statt Netzstrom zu verheizen. Angehoben wird nur um die **gemessene** Einspeisung, höchstens 0,5 kW je Lauf; bei etwa 0,3 kW Einspeisung bleibt er stehen.

**2. Nach der Einspeisegrenze.** Plant die Viertelstunde keine Wärme (oder ist der Plan veraltet), gilt diese Regel für Überschuss, den keine Prognose kannte:

| Gemessene Einspeisung | Heizstab |
|---|---|
| **Klebt an der Grenze** (± 0,1 kW) | Der Wechselrichter regelt ab. Der Heizstab bekommt 0,5 kW mehr — in Schritten, weil man nicht sieht, wie groß der Überschuss wirklich ist |
| **Deutlich unter der Grenze** (mehr als 0,3 kW) | Der Heizstab gibt je Lauf die **Hälfte** der Lücke ab; direkt nach einem Anheben wartet er einen Lauf. Bei **Netzbezug** nimmt er die ganze Lücke sofort zurück — an der Einspeisegrenze heißt das: aus |
| **Dazwischen** | Nichts ändern |

_Fehlt der Netz-Messwert einen Lauf lang, hält der Heizstab seinen Sollwert. Erst beim zweiten fehlenden Wert in Folge geht er auf 0._

Was der Heizstab **nie** tut:

- **Aus der Batterie heizen.** Entlädt die Optimierung gerade ins Netz, steht er auf 0. Einspeisung aus der Batterie ist kein Überschuss.
- **Aus dem Netz heizen** — außer du erlaubst es unter der Mindesttemperatur (siehe Konfiguration).
- **Weiterlaufen, wenn niemand steuert.** Der Ohmpilot schaltet nach 50 Sekunden ohne neuen Sollwert selbst ab. Ist die Verbindung weg oder die Optimierung aus, ist auch der Heizstab aus. Ist die letzte Vorgabe älter als 90 Sekunden oder schlägt ein Regelschritt fehl, geht der Sollwert auf 0.

Die geplante Heizstab-Leistung steht im **Optimierungsplan** (rote gestrichelte Linie). Sie ist die Obergrenze für die laufende Viertelstunde — was wirklich fließt, entscheidet die Messung.

Was der Heizstab **gerade** zieht, zeigt die Statuskarte: als Kachel „Heizstab" mit Leistung und Wassertemperatur und als eigener Kreis im Energieflussdiagramm (gelbe Linie aus der PV, rote aus dem Netz beim Komfortheizen). Ein Gedankenstrich statt einer Zahl heißt: Der Ohmpilot antwortet nicht.

## Heizstab oder Batterie zuerst?

Geplante Wärme teilt das Modell schon im Plan mit der Batterie auf.

**Ungeplanten** Überschuss (mehr Sonne als vorhergesagt) teilen sich beide nach dem Ladestand der Batterie:

- **Unter 20 %:** alles in die Batterie
- **Ab 50 %:** halb und halb
- **Dazwischen:** gleitend

Unter der Mindesttemperatur hat der Heizstab Vorrang. Am Plan für die Batterie ändert das nichts. Nur eine **volle** Batterie hebt die Aufteilung auf.

## Was die Wärme wert ist

Jede Kilowattstunde aus PV in den Speicher zählt mit dem **Wärmewert**, egal wie warm er gerade ist. Liegt der Wärmewert über der Einspeisevergütung, heizt der Optimizer, statt einzuspeisen.

_Die Grenze ist die **Maximaltemperatur**: Ist sie erreicht, geht die Energie wieder ins Netz._

## Konfiguration

Du findest alles unter **Einstellungen** → Tab **Verbraucher** → Karte **Heizstab**. Den Tab gibt es nur, wenn im Tab **System** der **Expertenmodus** eingeschaltet ist. Im Einrichtungsassistenten kommt der Heizstab nicht vor.

| Feld | Bedeutung |
|---|---|
| **Heizstab steuern — nur Fronius Ohmpilot** | Schaltet die Steuerung ein. Aus = der Optimizer fasst den Ohmpilot nicht an |
| **Adresse des Ohmpilot** | IP-Adresse oder Hostname |
| **Modbus-Port** | Standard 502 |
| **Leistung des Heizstabs (kW)** | Nennleistung — 3 kW einphasig, 6 oder 9 kW dreiphasig. Vorgabe 6, erlaubt 0,5 bis 30 |
| **Maximaltemperatur (°C)** | Bis hierher darf er heizen; darüber bleibt er aus, weiter geht es 3 K darunter. Vorgabe 80, erlaubt 30 bis 95 |
| **Mindesttemperatur (°C)** | Darunter hat der Heizstab Vorrang vor der Einspeisung: Er nimmt allen PV-Überschuss, auch unter der Einspeisegrenze, bis 5 K darüber — aber weder Netz- noch Batteriestrom. 0 = aus, erlaubt bis 90 |
| **Unter der Mindesttemperatur auch aus dem Netz heizen** | Nur sichtbar mit Mindesttemperatur. Ein: Darunter heizt er mit voller Leistung, egal woher der Strom kommt — auch die Batterie kann dann Haus und Heizstab versorgen —, und die Optimierung entlädt nicht ins Netz. Aus (Vorgabe): nie Netzstrom; ohne Sonne bleibt das Wasser kalt |
| **Wärmewert (ct/kWh)** | Was eine Kilowattstunde Wärme ersetzt — der Preis der Energie, mit der du sonst heizt. Damit wägt der Fahrplan gegen die Einspeisung ab; der Wert fließt in „Ersparnis durch PV" und den Optimierungsgewinn ein. 0 = Wärme wird gezählt, aber nicht bewertet |
| **Puffervolumen (Liter)** | Größe des Speichers. Daraus rechnet der Fahrplan, wie viel Wärme noch hineinpasst (1,163 Wh je Liter und Kelvin plus 10 % Verlustaufschlag). Bei Schichtspeichern nur den Teil angeben, der warm wird. Leer = der Heizstab bekommt nur, was sonst abgeregelt würde |
| **Heizstab sperren, solange diese Entität eingeschaltet ist** | Für eine zweite Wärmequelle (Holzvergaser, Kessel, Wärmepumpe): Ist die Entität „ein", bleibt der Heizstab aus und der Plan rechnet nicht mit ihm. Nicht erreichbar = nicht gesperrt |

> [!NOTE]
> **Optimierung aus heißt Heizstab aus.** Einen Notbetrieb gibt es nicht. Willst du den Ohmpilot ohne Optimizer betreiben, koppel ihn wieder an den Gen24.

> [!NOTE]
> **Die Batterie speist den Heizstab nicht.** Er bekommt nur, was die PV über den Hausverbrauch hinaus liefert. Ausnahme: Unter der Mindesttemperatur mit „auch aus dem Netz heizen" kann die Batterie Haus und Heizstab versorgen. Sonst bleibt der Heizstab ohne PV (nachts) aus, auch bei voller Batterie — fehlt die Energie abends, müsste sie zum vollen Bezugspreis gekauft werden.

> [!NOTE]
> **Folgt das Gerät der Vorgabe nicht, meldet es die Statuskarte** — in beide Richtungen.
>
> Zieht der Heizstab länger als drei Minuten deutlich **mehr** als vorgegeben, schreibt jemand anderes auf den Ohmpilot. Dann greifen weder die Maximaltemperatur noch der Schutz vor dem Verheizen von Batteriestrom — der Optimizer schreibt zwar weiter alle 20 Sekunden, wird aber überschrieben.
>
> Liefert er länger als zwei Minuten deutlich **weniger**, führt das Gerät die Vorgabe nicht aus. Fällt die Leistung auf null, obwohl ein Sollwert ansteht, schreibt der Optimizer ihn sofort nach (häufigster Grund: der Watchdog des Ohmpilot, eine Zeitsperre im Gerät). Bleibt es dabei, liegt es am Gerät: Übertemperaturabschaltung, Fühler am Puffer oder Verkabelung.

> [!NOTE]
> **Das Fahrplan-Diagramm zeigt die voraussichtliche Puffertemperatur** als violette Linie im Ladestandsfeld: gemessene Temperatur plus geplante Wärme, gedeckelt an der Maximaltemperatur. Sie rechnet vorsichtig — mit 10 % Verlustaufschlag und rund 0,12 kW Wärmeverlust an die Umgebung (bei 600 Litern gut 4 K am Tag). Entnommenes Warmwasser und Heizkreise kennt sie nicht; die echte Temperatur fällt dadurch schneller. Mit eingeblendetem Verlauf liegt der Messwert dünn daneben. Die Linie dient nur der Anzeige.

> [!NOTE]
> **Der Hausverbrauch ist ohne den Heizstab.** Sensor „Hausverbrauch", Verbrauchsprofil und Entlade-Nachführung rechnen ihn heraus. Seine Werte stehen in den Sensoren „Heizstab Leistung", „Heizstab Sollwert", „Heizstab Temperatur" und „Heizstab Energie heute".

## Was der Optimizer am Gerät tut

| Register | Zweck |
|---|---|
| 40599 | Leistungs-Sollwert in Watt — alle 20 s geschrieben, auch ohne Änderung (Watchdog des Ohmpilot: 50 s). Misst der Ohmpilot 0 W, obwohl ein Sollwert ansteht, geht er sofort erneut hinaus |
| 40800 | Ist-Leistung, alle 10 s gelesen. Werte über dem Doppelten der Nennleistung gelten als Lesefehler |
| 40808 | Wassertemperatur in 0,1 °C, alle 10 s gelesen |
| 40400 | Unix-Zeit, alle 6 Stunden gesetzt — sonst meldet der Ohmpilot Fehler 925 |

Beim Beenden der Integration wird 0 W geschrieben und die Verbindung geschlossen.
