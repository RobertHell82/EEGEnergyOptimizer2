# Wallbox Ambibox (Beta)

Mit einer **Ambibox** (ambiCHARGE) zeigt der Optimizer das angesteckte Auto direkt in der Statuskarte an: Ladestand, Ladeleistung und was die Ladesitzung gerade tut. Die Anbindung läuft über Modbus TCP und ist noch im Beta-Stadium.

## Was es heute kann — und was nicht

- **Anzeigen:** Zustand der Ladesitzung, Ladestand, Ladeleistung und die Energie der laufenden Ladesitzung, alle 15 Sekunden neu gelesen.
- **Von Hand testen:** Laden oder Entladen mit einer gewählten Leistung für eine gewählte Zeit starten und wieder stoppen.
- **Nicht steuern:** Der Optimierungsplan rührt die Wallbox nicht an. Das Auto ist kein Teil des Fahrplans, und außer im manuellen Test schreibt der Optimizer nichts an die Ambibox.

## Voraussetzungen

- Eine **Ambibox** im selben Netzwerk wie Home Assistant, mit **freigeschaltetem Modbus TCP**
- Für den manuellen Test: ein **angestecktes Fahrzeug**, das die Wallbox nicht als „Nicht steuerbar" meldet
- Für den Entladetest zusätzlich: ein Fahrzeug, das über **ISO 15118-20** lädt. Die älteren Protokolle (CHAdeMO, DIN 70121, ISO 15118-2) können nur laden

## Einrichtung

Die Wallbox steht in den **Einstellungen** im Tab **Anlage**, in der Karte **Wallbox**. Die Karte erscheint erst, wenn im Tab **System** der **Expertenmodus** eingeschaltet ist. Im Einrichtungsassistenten kommt sie nicht vor — das Auto ist Zubehör, keine Voraussetzung für den Fahrplan.

| Feld | Bedeutung |
|---|---|
| **Typ der Wallbox** | „Keine" oder „Ambibox (ambiCHARGE)". Erst mit der Ambibox erscheinen die übrigen Felder |
| **Adresse der Ambibox (IP oder Hostname)** | Pflichtfeld. Nur die Adresse, keine URL — also `192.168.1.70` statt `http://192.168.1.70/`. Eine eingetragene URL wird beim Speichern automatisch gekürzt |
| **Modbus-Port** | Vorgabe 502 |
| **Modbus-Unit-ID** | Vorgabe 1. Nur ändern, wenn die Ambibox hinter einem Gateway hängt, das die Geräte durchnummeriert |
| **Ladepunkt** | 1 bis 10, Vorgabe 1. Die Ambibox führt bis zu zehn Ladepunkte; bei einer einzelnen Wallbox ist es der erste |
| **Vorzeichen des Leistungssollwerts** | „Negativ = laden (Vorgabe)" oder „Positiv = laden". Gilt nur für den manuellen Test (siehe unten) |

**Verbindung testen** liest die Wallbox einmal mit den eingetragenen, noch nicht gespeicherten Werten und schreibt dabei nichts. Steht die Verbindung, zeigt der Test, ob ein Fahrzeug angesteckt ist, dessen Ladestand und Kapazität, das Ladeprotokoll und die „Steuerbarkeit laut Wallbox". Halte die Werte gegen die Anzeige der Ambibox. Schlägt der Test fehl, prüfe zuerst, ob Modbus TCP in der Ambibox freigeschaltet ist.

_Typ, Adresse, Port, Unit-ID und Ladepunkt lösen beim Speichern einen Neustart der Integration aus. Das Vorzeichen gilt ohne Neustart._

## Anzeige in der Statuskarte

Das Auto steht als eigene Zeile in der Statuskarte, weil es zum aktuellen Zustand der Anlage gehört:

- **Angesteckt:** „Auto:" mit Ladestand (samt Energie in kWh), darunter ein Balken. Lädt das Auto, steht dort „lädt mit … kW", speist es zurück, „speist zurück mit … kW"; sonst der Zustand der Ladesitzung. Meldet das Auto einen Zielladestand, erscheint er als Strich im Balken und als „Ziel … %", wenn bekannt mit der Zeit bis zur Abfahrt.
- **Nicht angesteckt:** „Kein Fahrzeug angesteckt". Verlangt die Wallbox es, kommt der Hinweis „Stecker ziehen und neu anstecken" dazu.
- **Wallbox antwortet nicht:** „Wallbox nicht erreichbar" mit Adresse und letztem Fehler.
- **Störungen** der Wallbox oder der Fahrzeugbatterie stehen rot darunter.

Die Richtung kommt aus dem Batteriezustand, den die Ambibox meldet, nicht aus dem Vorzeichen des Messwerts — die Anzeige stimmt also unabhängig davon, wie das Gerät das Vorzeichen setzt.

Dazu legt die Integration vier Sensoren an:

| Sensor | Inhalt |
|---|---|
| **Auto Status** | Zustand der Ladesitzung als Text („Lädt", „Pausiert", „Kein Fahrzeug angesteckt" …) oder „Ambibox nicht erreichbar". Die Details — Ladeprotokoll, Steuermodus, Kapazität, Batteriegesundheit, Temperatur, Ladezyklen, Höchstleistungen, Abfahrt — stehen in den Attributen |
| **Auto Ladestand** | Ladestand in %. Ohne angestecktes Auto „nicht verfügbar", denn dann wäre es der Wert des zuletzt verbundenen Fahrzeugs |
| **Auto Ladeleistung** | Leistung in kW — positiv beim Laden, negativ beim Rückspeisen, wie bei der Hausbatterie |
| **Auto Energie Ladesitzung** | Geladene Energie der laufenden Sitzung in kWh. Beginnt mit jeder neuen Sitzung bei null; die zurückgespeiste Energie steht im Attribut |

_Antwortet die Ambibox nicht, stehen die drei Messwert-Sensoren auf „nicht verfügbar" — so bleibt kein alter Ladestand stehen, nachdem das Auto weggefahren ist._

## Manueller Lade- und Entladetest

Der Test steht in der Auto-Zeile der Statuskarte, sobald ein Fahrzeug angesteckt und der **Expertenmodus** eingeschaltet ist: zwei Felder für die Leistung (kW, 0,5 bis 22) und die Dauer (min, 1 bis 60, Vorgabe 15) sowie die Knöpfe **Laden** und **Entladen**. Läuft ein Test, zeigt die Zeile „Handbetrieb" mit Richtung, Leistung, Restzeit und dem geschriebenen Sollwert in Watt, dazu den Knopf **Stoppen**.

Starten und stoppen dürfen nur **Administratoren** von Home Assistant; bei allen anderen lehnt die Integration die Anfrage ab.

Drei Sicherungen hängen am Test, weil das Verhalten des Geräts nicht vollständig bekannt ist:

- **Nachschreiben:** Der Sollwert wird alle 30 Sekunden erneut geschrieben.
- **Höchstdauer:** Nach der eingestellten Zeit, spätestens nach 60 Minuten, stoppt der Test von selbst — auch wenn niemand mehr hinsieht. Er endet ebenso, wenn das Auto abgesteckt wird.
- **Ende mit der Integration:** Wird die Integration beendet oder neu geladen, wird der Test gestoppt.

Beim Stoppen schreibt der Optimizer erst 0 W und dann den Befehl zum Beenden des Ladevorgangs.

Verweigert wird der Start, wenn kein Fahrzeug angesteckt ist, wenn die Wallbox das Fahrzeug als nicht steuerbar meldet und — nur beim Entladen — wenn das Fahrzeug nicht über ISO 15118-20 lädt. Der Grund steht dann unter den Knöpfen.

> [!WARNING]
> **Der Test ist ein Werkzeug für jemanden, der am Auto steht.** Er ist kein Alltagsknopf und keine Ladesteuerung. Starte ihn nur, wenn du das Ergebnis an der Wallbox und am Fahrzeug beobachtest.

## Offene Punkte

Das Herstellerdokument lässt drei Fragen offen. Genau dafür gibt es den manuellen Test:

1. **Wie lange gilt ein Sollwert?** Ob und wann die Ambibox einen Wert ohne Nachschreiben verwirft, ist nicht dokumentiert. Die 30 Sekunden sind bewusst eng gewählt.
2. **Welches Vorzeichen heißt Laden?** Steht nirgends. Die Vorgabe „Negativ = laden" folgt der einzigen bekannten fremden Umsetzung. Entlädt das Auto, wenn du **Laden** drückst, oder passiert gar nichts, stelle das Vorzeichen in den Einstellungen um und teste erneut.
3. **Was macht die Eigenoptimierung der Ambibox?** Die Ambibox regelt mit ihrem Energiemanagement (sidOS) selbst. Was passiert, wenn der Optimizer gleichzeitig einen Sollwert schreibt, ist nicht beschrieben.

Beobachte beim Test deshalb: ob das Auto in die gewählte Richtung und mit ungefähr der gewählten Leistung lädt oder entlädt, ob die Leistung zwischen zwei Nachschreib-Zeitpunkten wegfällt oder springt, ob die Ambibox die Leistung von sich aus verändert, und ob das Auto nach **Stoppen** oder nach Ablauf der Zeit wirklich aufhört.
