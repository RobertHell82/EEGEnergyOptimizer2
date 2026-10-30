# Wallbox Ambibox (Beta)

Mit einer **Ambibox** (ambiCHARGE) zeigt der Optimizer das angesteckte Auto in der Statuskarte: Ladestand, Ladeleistung und was die Ladesitzung gerade tut. Die Verbindung läuft über Modbus TCP (ein Netzwerkprotokoll für Geräte) und ist noch Beta.

## Was es kann — und was nicht

- **Anzeigen:** Zustand der Ladesitzung, Ladestand, Ladeleistung und Energie der laufenden Sitzung, alle 15 Sekunden neu gelesen.
- **Von Hand testen:** Laden oder Entladen mit gewählter Leistung für eine gewählte Zeit starten und stoppen.
- **Nicht steuern:** Der Fahrplan rührt die Wallbox nicht an. Außer im manuellen Test schreibt der Optimizer nichts an die Ambibox.

## Voraussetzungen

- Eine **Ambibox** im selben Netzwerk wie Home Assistant, mit **freigeschaltetem Modbus TCP**
- Für den manuellen Test: ein **angestecktes Fahrzeug**, das die Wallbox nicht als „Nicht steuerbar" meldet
- Für den Entladetest zusätzlich: ein Fahrzeug, das über **ISO 15118-20** lädt. Ältere Protokolle (CHAdeMO, DIN 70121, ISO 15118-2) können nur laden

## Einrichtung

Du findest die Wallbox unter **Einstellungen** → Tab **Verbraucher** → Karte **Wallbox**. Den Tab gibt es nur, wenn im Tab **System** der **Expertenmodus** eingeschaltet ist. Im Einrichtungsassistenten kommt sie nicht vor.

| Feld | Bedeutung |
|---|---|
| **Typ der Wallbox** | „Keine" oder „Ambibox (ambiCHARGE)". Erst mit der Ambibox erscheinen die übrigen Felder |
| **Adresse der Ambibox (IP oder Hostname)** | Pflichtfeld. Nur die Adresse, z.B. `192.168.1.70` statt `http://192.168.1.70/`. Eine URL wird beim Speichern automatisch gekürzt |
| **Modbus-Port** | Vorgabe 502 |
| **Modbus-Unit-ID** | Vorgabe 1. Nur ändern, wenn die Ambibox hinter einem Gateway hängt, das die Geräte durchnummeriert |
| **Ladepunkt** | 1 bis 10, Vorgabe 1. Bei einer einzelnen Wallbox ist es der erste |
| **Vorzeichen des Leistungssollwerts** | „Negativ = laden (Vorgabe)" oder „Positiv = laden". Gilt nur für den manuellen Test |

**Verbindung testen** liest die Wallbox einmal mit den eingetragenen, noch nicht gespeicherten Werten und schreibt nichts. Klappt es, siehst du: ob ein Fahrzeug angesteckt ist, Ladestand und Kapazität, Ladeprotokoll und die „Steuerbarkeit laut Wallbox". Vergleiche das mit der Anzeige der Ambibox. Schlägt der Test fehl, prüfe zuerst, ob Modbus TCP in der Ambibox freigeschaltet ist.

_Typ, Adresse, Port, Unit-ID und Ladepunkt starten die Integration beim Speichern neu. Das Vorzeichen gilt sofort._

## Anzeige in der Statuskarte

Das Auto hat eine eigene Zeile in der Statuskarte:

- **Angesteckt:** „Auto:" mit Ladestand (samt kWh) und einem Balken. Dazu „lädt mit … kW", „speist zurück mit … kW" oder der Zustand der Ladesitzung. Meldet das Auto einen Zielladestand, steht er als Strich im Balken und als „Ziel … %", wenn bekannt mit der Zeit bis zur Abfahrt.
- **Nicht angesteckt:** „Kein Fahrzeug angesteckt", wenn nötig mit „Stecker ziehen und neu anstecken".
- **Wallbox antwortet nicht:** „Wallbox nicht erreichbar" mit Adresse und letztem Fehler.
- **Störungen** der Wallbox oder der Fahrzeugbatterie stehen rot darunter.

Ob das Auto lädt oder zurückspeist, liest der Optimizer aus dem Batteriezustand, den die Ambibox meldet. Die Anzeige stimmt also, egal welches Vorzeichen das Gerät verwendet.

Dazu gibt es vier Sensoren:

| Sensor | Inhalt |
|---|---|
| **Auto Status** | Zustand der Ladesitzung als Text („Lädt", „Pausiert", „Kein Fahrzeug angesteckt" …) oder „Ambibox nicht erreichbar". Details (Ladeprotokoll, Steuermodus, Kapazität, Batteriegesundheit, Temperatur, Ladezyklen, Höchstleistungen, Abfahrt) stehen in den Attributen |
| **Auto Ladestand** | Ladestand in %. Ohne angestecktes Auto „nicht verfügbar" — sonst stünde dort der Wert des letzten Fahrzeugs |
| **Auto Ladeleistung** | Leistung in kW — positiv beim Laden, negativ beim Rückspeisen, wie bei der Hausbatterie |
| **Auto Energie Ladesitzung** | Geladene Energie der laufenden Sitzung in kWh, beginnt bei jeder Sitzung bei null. Die zurückgespeiste Energie steht im Attribut |

_Antwortet die Ambibox nicht, stehen die drei Messwert-Sensoren auf „nicht verfügbar". So bleibt kein alter Ladestand stehen, wenn das Auto weg ist._

## Manueller Lade- und Entladetest

Der Test erscheint in der Auto-Zeile der Statuskarte, wenn ein Fahrzeug angesteckt und der **Expertenmodus** an ist:

- **Leistung** in kW: ab 0,5, höchstens so viel, wie die Wallbox als Stellbereich meldet (ohne Angabe 11 kW)
- **Dauer** in Minuten: 1 bis 60, Vorgabe 15
- Knöpfe **Laden** und **Entladen**

Während des Tests zeigt die Zeile „Handbetrieb" mit Richtung, Leistung, Restzeit und dem geschriebenen Sollwert in Watt, dazu den Knopf **Stoppen**.

Starten und stoppen dürfen nur **Administratoren** von Home Assistant.

Vier Sicherungen, weil das Verhalten des Geräts nicht ganz bekannt ist:

- **Nachschreiben:** Der Sollwert wird alle 30 Sekunden neu geschrieben.
- **Höchstdauer:** Nach der eingestellten Zeit, spätestens nach 60 Minuten, stoppt der Test von selbst. Er endet auch, wenn das Auto abgesteckt wird.
- **Ende mit der Integration:** Wird die Integration beendet oder neu geladen, stoppt der Test.
- **Falsche Richtung:** Arbeitet die Wallbox nach einer Minute in die Gegenrichtung (sie entlädt, obwohl du **Laden** gedrückt hast, oder umgekehrt), bricht der Test ab: „Die Wallbox arbeitet in Gegenrichtung — Vorzeichen-Einstellung prüfen.“

Beim Stoppen schreibt der Optimizer erst 0 W, dann den Befehl zum Beenden des Ladevorgangs.

Der Start wird verweigert, wenn kein Fahrzeug angesteckt ist, wenn die Wallbox es als nicht steuerbar meldet und — nur beim Entladen — wenn es nicht über ISO 15118-20 lädt. Der Grund steht unter den Knöpfen.

> [!WARNING]
> **Der Test ist für jemanden, der am Auto steht.** Er ist keine Ladesteuerung für den Alltag. Starte ihn nur, wenn du Wallbox und Fahrzeug dabei beobachtest.

## Offene Punkte

Drei Fragen beantwortet der Hersteller nicht. Dafür gibt es den manuellen Test:

1. **Wie lange gilt ein Sollwert?** Ob die Ambibox einen Wert ohne Nachschreiben verwirft, ist nicht dokumentiert. Die 30 Sekunden sind bewusst knapp.
2. **Welches Vorzeichen heißt Laden?** Steht nirgends. Die Vorgabe „Negativ = laden" folgt der einzigen bekannten anderen Umsetzung. Bei falscher Richtung bricht der Test nach einer Minute selbst ab. Passiert gar nichts oder kommt diese Meldung, stell das Vorzeichen in den Einstellungen um und teste noch einmal.
3. **Was macht die Ambibox selbst?** Sie regelt mit ihrem eigenen Energiemanagement (sidOS). Was passiert, wenn der Optimizer gleichzeitig einen Sollwert schreibt, ist nicht beschrieben.

Achte beim Test darauf:

- Lädt oder entlädt das Auto in die gewählte Richtung, mit ungefähr der gewählten Leistung?
- Fällt die Leistung zwischen zwei Nachschreib-Zeitpunkten weg oder springt sie?
- Ändert die Ambibox die Leistung von sich aus?
- Hört das Auto nach **Stoppen** oder nach Ablauf der Zeit wirklich auf?
