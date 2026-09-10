# Heizstab (Fronius Ohmpilot)

Eine bewusst groß gebaute PV-Anlage erzeugt an guten Tagen mehr, als Batterie, Haus und Einspeisegrenze zusammen aufnehmen. Der Wechselrichter regelt den Rest ab — diese Energie ist verloren. Mit einem Heizstab am Warmwasserspeicher bekommt sie eine Verwendung: Der Optimizer steuert einen **Fronius Ohmpilot** direkt per Modbus TCP und gibt ihm genau den Überschuss, den sonst niemand nimmt.

## Warum nicht das Energiemanagement des Wechselrichters?

Der Gen24 kann den Ohmpilot selbst regeln — aber er regelt die **Einspeisung auf null**. Für eine Anlage in einer Energiegemeinschaft ist das falsch: Die Einspeisung bis zur Grenze soll ins Netz, dort ist sie Geld wert. Nur was **über** die Grenze hinausgeht, gehört in den Heizstab. Deshalb steuert der Optimizer den Ohmpilot selbst, und deshalb muss der Ohmpilot vom Gen24 entkoppelt sein.

## Voraussetzungen

- Ein **Fronius Ohmpilot** mit Netzwerkanschluss (LAN oder WLAN) und einem angeschlossenen Heizstab
- Der Ohmpilot ist **vom Wechselrichter entkoppelt**: Kopplung im Gen24-Webinterface unter *Geräte- und Systemeinstellungen → Komponenten* lösen, oder den Ohmpilot in ein anderes Subnetz stellen
- Eine konfigurierte **Einspeisegrenze** (siehe Anleitung „Einspeisegrenze"). Ohne sie gilt die AC-Grenzleistung des Wechselrichters als Grenze
- Ein **Netzleistungs-Sensor**, der Einspeisung und Bezug misst

> [!CAUTION]
> **Zwei Steuerungen auf einem Gerät gehen nicht.** Bleibt der Ohmpilot mit dem Gen24 gekoppelt, schreiben beide auf dasselbe Register: Der Gen24 heizt jede Einspeisung weg, der Optimizer setzt sie zurück, und am Ende gewinnt keiner. Erst entkoppeln, dann einschalten.

## So funktioniert es

Der Heizstab ist eine **zweite Senke** neben der Batterie. Alle 30 Sekunden schaut die Steuerung auf die gemessene Einspeisung:

| Gemessene Einspeisung | Heizstab |
|---|---|
| **Klebt an der Grenze** (± 0,1 kW) | Der Wechselrichter regelt gerade ab. Der Heizstab bekommt 0,5 kW mehr — in Schritten, weil die wahre Höhe des Überschusses unsichtbar ist |
| **Deutlich unter der Grenze** (mehr als 0,3 kW) | Der Heizstab gibt genau die Lücke wieder her, in einem Schritt. Bei Netzbezug fällt er sofort auf 0 |
| **Dazwischen** | Nichts ändern |

Was der Heizstab **nie** tut:

- **Einspeisung wegheizen.** Solange die Einspeisung unter der Grenze liegt, bleibt er aus — die Energie gehört der Gemeinschaft.
- **Aus der Batterie heizen.** Entlädt die Optimierung gerade ins Netz, steht der Heizstab auf 0. Eine Einspeisung aus der Batterie ist kein Überschuss.
- **Aus dem Netz heizen** — außer du erlaubst es ausdrücklich unter der Mindesttemperatur (siehe unten). Ohne diese Erlaubnis zieht der Heizstab nie Netzstrom.
- **Weiterlaufen, wenn niemand steuert.** Der Ohmpilot schaltet nach 50 Sekunden ohne neuen Sollwert selbst ab. Bricht die Verbindung ab oder ist die Optimierung aus, ist auch der Heizstab aus.

Die geplante Heizstab-Leistung steht auch im **Optimierungsplan** (rote gestrichelte Linie): Der Plan weiß je Viertelstunde, was er abregeln müsste — genau das würde der Heizstab nehmen. Gesteuert wird trotzdem nach der Messung, nicht nach der Prognose.

## Reihenfolge: Heizstab oder Batterie zuerst?

Meist ist die Batterie schon nach Plan am Laden, wenn die Einspeisung ans Limit kommt. Für den **ungeplanten** Überschuss — mehr Sonne als vorhergesagt — entscheidet die Einstellung **„Überschuss zuerst in den Heizstab"**:

- **Eingeschaltet** (Vorgabe): Der Heizstab nimmt den Überschuss. Erst wenn er mit voller Leistung läuft oder die Zieltemperatur erreicht hat, hebt die Steuerung das Ladelimit der Batterie über den Planwert an.
- **Ausgeschaltet**: Erst die Batterie — das Ladelimit wird angehoben, bis sie am Maximum lädt oder voll ist. Dann bekommt der Heizstab den Rest.

Der Plan für die Batterie bleibt in beiden Fällen unangetastet. Es geht nur darum, wer den Teil bekommt, den die Prognose nicht kannte.

## Konfiguration

Alle Felder stehen in den **Einstellungen → Anlage → Heizstab**.

| Feld | Bedeutung |
|---|---|
| **Heizstab (Fronius Ohmpilot)** | Schaltet die Steuerung ein. Aus = der Optimizer fasst den Ohmpilot nicht an |
| **Adresse des Ohmpilot** | IP-Adresse oder Hostname des Ohmpilot im Netzwerk |
| **Modbus-Port** | Standard 502 |
| **Leistung des Heizstabs (kW)** | Nennleistung des angeschlossenen Heizstabs — 3 kW einphasig, 6 oder 9 kW dreiphasig |
| **Zieltemperatur (°C)** | Ab dieser Temperatur wird nicht mehr geheizt; weiter geht es 3 K darunter |
| **Mindesttemperatur (°C)** | Darunter hat der Heizstab Vorrang vor der Einspeisung: Er nimmt allen PV-Überschuss, auch den unterhalb der Einspeisegrenze, bis 5 K darüber — aber weder Netz- noch Batteriestrom. 0 = aus |
| **Unter der Mindesttemperatur auch aus dem Netz heizen** | Nur sichtbar mit Mindesttemperatur. Eingeschaltet heizt der Heizstab darunter mit voller Leistung, egal woher der Strom kommt, und die Optimierung entlädt derweil nicht ins Netz. Ausgeschaltet (Vorgabe) wird nie Netzstrom verheizt — ohne Sonne bleibt das Wasser dann kalt |
| **Überschuss zuerst in den Heizstab** | Reihenfolge bei ungeplantem Überschuss, siehe oben |
| **Wärmewert (ct/kWh)** | Was eine Kilowattstunde Wärme ersetzt. Fließt in „Ersparnis durch PV" und in den Optimierungsgewinn ein. 0 = Wärme wird gezählt, aber nicht bewertet |

> [!NOTE]
> **Optimierung aus heißt Heizstab aus.** Es gibt keinen Notbetrieb, der den Ohmpilot ohne Optimierung weiterregelt. Wer den Ohmpilot ohne Optimizer betreiben will, koppelt ihn wieder an den Gen24.

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
