# Heizstab (nur Fronius Ohmpilot)

Eine bewusst groß gebaute PV-Anlage erzeugt an guten Tagen mehr, als Batterie, Haus und Einspeisegrenze zusammen aufnehmen. Der Wechselrichter regelt den Rest ab — diese Energie ist verloren. Mit einem Heizstab am Warmwasserspeicher bekommt sie eine Verwendung: Der Optimizer steuert einen **Fronius Ohmpilot** direkt per Modbus TCP und gibt ihm genau den Überschuss, den sonst niemand nimmt.

## Warum nicht das Energiemanagement des Wechselrichters?

Der Gen24 kann den Ohmpilot selbst regeln — aber er regelt die **Einspeisung auf null**. Für eine Anlage in einer Energiegemeinschaft ist das falsch: Die Einspeisung bis zur Grenze soll ins Netz, dort ist sie Geld wert. Nur was **über** die Grenze hinausgeht, gehört in den Heizstab. Deshalb steuert der Optimizer den Ohmpilot selbst, und deshalb muss der Ohmpilot vom Gen24 entkoppelt sein.

## Voraussetzungen

- Ein **Fronius Ohmpilot** mit Netzwerkanschluss (LAN oder WLAN) und einem angeschlossenen Heizstab
- Der Ohmpilot ist **vom Wechselrichter entkoppelt**: Kopplung im Gen24-Webinterface unter *Geräte- und Systemeinstellungen → Komponenten* lösen, oder den Ohmpilot in ein anderes Subnetz stellen
- Eine konfigurierte **Einspeisegrenze** (siehe Anleitung „Einspeisegrenze"). Ohne sie gilt die AC-Grenzleistung des Wechselrichters als Grenze
- Ein **Netzleistungs-Sensor**, der Einspeisung und Bezug misst

> [!CAUTION]
> **Zwei Steuerungen auf einem Gerät gehen nicht.** Der Ohmpilot kennt keine Zugriffsrechte — wer zuletzt auf das Register schreibt, gewinnt. Bleibt er mit dem Gen24 gekoppelt, heizt der Gen24 jede Einspeisung weg und der Optimizer setzt sie zurück; am Ende gewinnt keiner. Dasselbe gilt für **jede andere Steuerung**: eine alte Automatisierung, ein Node-RED-Flow, eine zweite Integration. Erst alles andere abschalten, dann einschalten.
>
> Achte darauf, dass wirklich der **schreibende** Teil aus ist. An einer Anlage stand der Modus der Vorgänger-Integration auf „Aus", ihr Keepalive schrieb den letzten Sollwert aber weiter alle paar Sekunden ins Register — der Heizstab lief mit 2,4 kW, während der Optimizer „Heizstab aus" anzeigte und alle 30 Sekunden 0 W schrieb. Sichtbar war das nur als Sägezahn in der gemessenen Leistung. Genau diesen Fall meldet die Statuskarte inzwischen von selbst (siehe unten).

## So funktioniert es

Der Heizstab ist eine **zweite Senke** neben der Batterie. Er hat zwei Betriebsarten, und alle 30 Sekunden entscheidet sich, welche gilt.

**1. Nach Plan.** Sieht die laufende Viertelstunde des Optimierungsplans Wärme vor, ist diese Leistung die Vorgabe. Das Modell hat die Kilowattstunde dem Puffer zugeschlagen, weil der Wärmewert höher liegt als die Einspeisevergütung — dann wird geheizt, auch wenn die Einspeisung weit unter der Grenze bleibt. Ausgeführt wird der Plan aber nur so weit, wie die Messung ihn trägt: Geregelt wird auf **Einspeisung ≈ 0**, nie über die geplante Leistung hinaus. Liefert die PV weniger als vorhergesagt, fällt der Sollwert von selbst zurück, statt Netzstrom zu verheizen.

**2. Nach der Einspeisegrenze.** Plant der Slot keine Wärme — oder ist der Plan veraltet —, gilt die alte Regel für den Überschuss, den keine Prognose kannte:

| Gemessene Einspeisung | Heizstab |
|---|---|
| **Klebt an der Grenze** (± 0,1 kW) | Der Wechselrichter regelt gerade ab. Der Heizstab bekommt 0,5 kW mehr — in Schritten, weil die wahre Höhe des Überschusses unsichtbar ist |
| **Deutlich unter der Grenze** (mehr als 0,3 kW) | Der Heizstab gibt genau die Lücke wieder her, in einem Schritt. Bei Netzbezug fällt er sofort auf 0 |
| **Dazwischen** | Nichts ändern |

Was der Heizstab **nie** tut:

- **Aus der Batterie heizen.** Entlädt die Optimierung gerade ins Netz, steht der Heizstab auf 0. Eine Einspeisung aus der Batterie ist kein Überschuss.
- **Aus dem Netz heizen** — außer du erlaubst es ausdrücklich unter der Mindesttemperatur (siehe unten). Ohne diese Erlaubnis zieht der Heizstab nie Netzstrom.
- **Weiterlaufen, wenn niemand steuert.** Der Ohmpilot schaltet nach 50 Sekunden ohne neuen Sollwert selbst ab. Bricht die Verbindung ab oder ist die Optimierung aus, ist auch der Heizstab aus.

Die geplante Heizstab-Leistung steht im **Optimierungsplan** (rote gestrichelte Linie): So viel Wärme sieht das Modell je Viertelstunde vor. Sie ist die Obergrenze für den laufenden Slot — wie viel davon wirklich fließt, entscheidet die Messung.

Was der Heizstab **gerade** zieht, steht oben in der Statuskarte: in der Werteliste als eigene Kachel „Heizstab" mit Leistung und Wassertemperatur, im Energieflussdiagramm als eigener Kasten neben dem Haus — samt der Linie, über die er seine Energie bekommt (gelb aus der PV, rot aus dem Netz beim Komfortheizen). Ein Gedankenstrich statt einer Zahl heißt: Der Ohmpilot antwortet nicht.

## Reihenfolge: Heizstab oder Batterie zuerst?

Für die geplante Wärme stellt sich die Frage nicht — das Modell plant Batterie und Heizstab gemeinsam, die Aufteilung steckt schon im Plan.

Beim **ungeplanten** Überschuss — mehr Sonne als vorhergesagt — teilen sich beide, gewichtet nach dem Ladestand der Batterie:

- **Unter 20 %** bekommt die Batterie alles. Ihre Energie trägt durch die Nacht, die Wärme nicht.
- **Ab 50 %** ist es die Hälfte für jeden.
- **Dazwischen** gleitend.

Unter der Mindesttemperatur hat der Heizstab davon unabhängig Vorrang und volle Leistung. Der Plan für die Batterie bleibt in jedem Fall unangetastet.

## Was die Wärme wert ist

Jede Kilowattstunde, die aus PV in den Puffer geht, zählt mit dem **Wärmewert** — unabhängig davon, wie warm der Puffer gerade ist. Das ist die Zahl, gegen die der Optimizer die Einspeisung abwägt: Liegt der Wärmewert darüber, heizt er den Puffer, statt einzuspeisen.

_Die Obergrenze ist allein die **Maximaltemperatur**: Ist sie erreicht, nimmt der Puffer nichts mehr auf, und die Energie geht wieder ins Netz._

## Konfiguration

Alle Felder stehen in den **Einstellungen** im eigenen Tab **Heizstab**. Im Einrichtungsassistenten kommt der Heizstab nicht vor — er ist die Ausnahme, nicht die Regel.

| Feld | Bedeutung |
|---|---|
| **Heizstab (Fronius Ohmpilot)** | Schaltet die Steuerung ein. Aus = der Optimizer fasst den Ohmpilot nicht an |
| **Adresse des Ohmpilot** | IP-Adresse oder Hostname des Ohmpilot im Netzwerk |
| **Modbus-Port** | Standard 502 |
| **Leistung des Heizstabs (kW)** | Nennleistung des angeschlossenen Heizstabs — 3 kW einphasig, 6 oder 9 kW dreiphasig |
| **Maximaltemperatur (°C)** | Bis zu dieser Temperatur darf der Heizstab heizen; darüber bleibt er aus, weiter geht es 3 K darunter |
| **Mindesttemperatur (°C)** | Darunter hat der Heizstab Vorrang vor der Einspeisung: Er nimmt allen PV-Überschuss, auch den unterhalb der Einspeisegrenze, bis 5 K darüber — aber weder Netz- noch Batteriestrom. 0 = aus |
| **Unter der Mindesttemperatur auch aus dem Netz heizen** | Nur sichtbar mit Mindesttemperatur. Eingeschaltet heizt der Heizstab darunter mit voller Leistung, egal woher der Strom kommt, und die Optimierung entlädt derweil nicht ins Netz. Ausgeschaltet (Vorgabe) wird nie Netzstrom verheizt — ohne Sonne bleibt das Wasser dann kalt |
| **Wärmewert (ct/kWh)** | Was eine Kilowattstunde Wärme ersetzt — der Preis der Energie, mit der du sonst heizen würdest. Gegen diesen Wert wägt der Fahrplan die Einspeisung ab; er fließt in „Ersparnis durch PV" und in den Optimierungsgewinn ein. 0 = Wärme wird gezählt, aber nicht bewertet |
| **Puffervolumen (Liter)** | Wie groß der Speicher ist, den der Heizstab erwärmt. Daraus rechnet der Fahrplan, wie viel Wärme noch hineinpasst. Bei Schichtspeichern nur den Teil angeben, der tatsächlich warm wird. Leer = der Heizstab bekommt nur, was ohnehin abgeregelt würde |
| **Heizstab sperren, solange diese Entität eingeschaltet ist** | Für eine zweite Wärmequelle (Holzvergaser, Kessel, Wärmepumpe): Meldet die Entität „ein", bleibt der Heizstab aus und der Plan rechnet nicht mit ihm. Nicht erreichbar = nicht gesperrt |

> [!NOTE]
> **Optimierung aus heißt Heizstab aus.** Es gibt keinen Notbetrieb, der den Ohmpilot ohne Optimierung weiterregelt. Wer den Ohmpilot ohne Optimizer betreiben will, koppelt ihn wieder an den Gen24.

> [!NOTE]
> **Wenn das Gerät der Vorgabe nicht folgt, sagt es die Statuskarte.** Zieht der Heizstab länger als drei Minuten deutlich mehr, als vorgegeben ist, erscheint eine Warnung: Dann schreibt jemand anderes auf denselben Ohmpilot. Solange das so ist, greift weder die Maximaltemperatur noch der Schutz davor, Batteriestrom zu verheizen — der Optimizer setzt seinen Sollwert zwar weiter alle 30 Sekunden, wird aber überschrieben.

> [!NOTE]
> **Der Hausverbrauch ist ohne den Heizstab.** Sensor „Hausverbrauch", Verbrauchsprofil und Entlade-Nachführung rechnen den Heizstab heraus — er ist eine gesteuerte Senke, kein Verbrauch, den das Profil lernen soll. Sein Anteil steht in den eigenen Sensoren „Heizstab Leistung", „Heizstab Sollwert", „Heizstab Temperatur" und „Heizstab Energie heute".

## Was der Optimizer am Gerät tut

| Register | Zweck |
|---|---|
| 40599 | Leistungs-Sollwert in Watt — alle 30 s geschrieben, auch wenn er sich nicht ändert (Watchdog des Ohmpilot: 50 s) |
| 40800 | Ist-Leistung, alle 10 s gelesen |
| 40808 | Wassertemperatur in 0,1 °C, alle 10 s gelesen |
| 40400 | Unix-Zeit, alle 6 Stunden gesetzt — sonst meldet der Ohmpilot Fehler 925 |

Beim Beenden der Integration wird 0 W geschrieben und die Verbindung geschlossen.
