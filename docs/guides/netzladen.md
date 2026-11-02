# Batterie aus dem Netz laden

Schafft die Sonne das **Ladeziel** nicht, darf der Fahrplan Strom aus dem Netz zukaufen, aber nur, wenn das günstiger ist als der Strom, den die Batterie danach in der Nacht ersetzt. Mit einer Energiegemeinschaft heißt das auch: laden, wenn die Gemeinschaft Strom übrig hat.

## Wo du es einschaltest

Der Schalter steht im eigenen Reiter **Netzladen** (Beta). Er erscheint nur mit eingeschaltetem Expertenmodus (Reiter **System**) und bei allen Wechselrichtern. Bei SolaX wirkt er nur mit Gen4 und Gen5; Gen6 hat die nötige Fernsteuerung nicht, dort bleibt der Schalter ohne Wirkung. Ist der Schalter **Batterie aus dem Netz laden** aus, rechnet der Fahrplan genau wie vorher.

Die **Bezugspreise** stehen nicht hier, sondern im Reiter **Tarife**: der Bezugstarif deines Lieferanten in der Karte **Bezug** (Abschnitt **Beim Laden aus dem Netz**) und der Bezug aus der Gemeinschaft im Block jeder Gemeinschaft. Einschalten lässt sich der Schalter erst, wenn dort ein günstigerer Bezugspreis eingerichtet ist (siehe unten); bis dahin steht unter der Karte, was noch fehlt.

Steht er auf **Aktiv**, darf der Fahrplan das **Ladeziel** auch mit Netzstrom erreichen, wenn die Sonne an einem trüben Tag nicht reicht. Er kauft aber nur in Viertelstunden, in denen der Netzstrom billiger ist als der Strom, den die Batterie danach ersetzt: der Bezugspreis der Nacht nach dem Ende der PV-Zeit, abzüglich Lade- und Entladeverlust, Alterungskosten und Mindestvorteil. Gekauft wird nur vor dem Ende der PV-Zeit. Ob die Nacht die Energie wirklich braucht, fragt er nicht: Was übrig bleibt, speist er später ein, wenn sich das lohnt. Der Preis dafür ist eine volle Batterie am Abend. Höher als der **maximale Ladestand** (Anlage & Batterie) lädt er nie, und nie schneller als die Batterieleistung.

Billiger als die Nacht ist Netzstrom nur in drei Fällen, und mindestens einer muss im Reiter **Tarife** eingerichtet sein (Einzelheiten in der Anleitung „Tarife & Gemeinschaft“):

- **Spot-Bezug.** Karte **Bezug**, Abschnitt **Beim Laden aus dem Netz**: **Bezugstarif deines Lieferanten** auf **Börsenpreis (Spot)**, dazu die Aufschläge. Gekauft wird nur zu veröffentlichten Börsenpreisen, nie zu geschätzten.
- **Bezug aus der Gemeinschaft.** Im Block der Gemeinschaft, Abschnitt **Bezug aus der Gemeinschaft**: Bezugspreis und Art (lokal, regional, Bürgerenergiegemeinschaft). Dann lädt der Fahrplan bevorzugt dort, wo die Gemeinschaft laut PeakShare Strom übrig hat, höchstens mit deinem Anteil. Im Quotenmodus gibt es keinen Gemeinschaftsbezug.
- **Zeitvariable Netzentgelte.** Karte **Bezug**, Haken **Zeitvariable Netzentgelte (SNAP und WiNAP)**: Im SNAP- bzw. WiNAP-Fenster ist die Netzgebühr niedriger.

Spot- und Gemeinschaftsbezug brauchen einen **Netzbereich** (Karte **Bezug**), sonst fehlt die Netzgebühr und der Netzstrom sähe zu billig aus.

| Feld | Bedeutung |
|---|---|
| **Höchster Netzbezug beim Laden aus dem Netz (kW)** | Gilt am Zähler, Haus und Batterie zusammen. Plan und Steuerung rechnen mit Hausverbrauch und PV (die Steuerung alle 30 s mit den Messwerten) und lassen die Batterie nur so schnell laden, dass der Bezug darunter bleibt. Das Haus wird nie begrenzt. Leer = keine Grenze, die Batterie lädt mit ihrer vollen Leistung. Ist die **Spitzenkappung** mit Batterie an, bleibt jede Netzladung ohnehin 0,3 kW unter deren Grenzwert |
| **Mindestvorteil (ct/kWh)** | Um so viel muss der Netzstrom billiger sein als die Nacht. Vorgabe 2 ct |
| **Mindestladung am Abend (%)** | So voll soll die Batterie am Ende der PV-Zeit auf jeden Fall sein, unabhängig vom Preis (siehe unten). Leer = aus |

Während der Netzladung lädt der Wechselrichter mit der geplanten Leistung bis zum geplanten Ladestand, der Statussensor zeigt „Netzladung x kW bis y %“, im Diagramm ist sie violett gestrichelt. Der Heizstab bleibt dabei aus.

> [!WARNING]
> **Beta.** Geprüft ist das Laden aus dem Netz bisher nur bei Huawei. Bei allen anderen Wechselrichtern ist es am Gerät noch nicht erprobt. Wer es ausprobiert: Bitte melden, was Batterie- und Netzleistung während einer Netzladung zeigen.
>
> - **Huawei:** Die Zwangsladung ist an einem SUN2000 geprüft, sie braucht die Einstellung „Laden aus dem Netz“ nicht.
> - **Victron:** Der DVCC-Ladestrom lädt mit der geplanten Leistung, und ein positiver Netzsollwert erlaubt den Bezug; den führt der Optimizer alle 30 Sekunden nach (Anleitung „Victron einrichten“).
> - **Fronius Gen24:** Der Optimizer legt einen festen Ladepunkt fest und erlaubt dafür das Laden aus dem Netz; danach stellt er die Erlaubnis zurück, wie sie vorher war. Ob im Webinterface zusätzlich „Laden aus dem Netz“ erlaubt sein muss, ist offen.
> - **SMA:** Der Optimizer schaltet die Batteriesteuerung auf die Betriebsart **Laden** mit der geplanten Leistung. Mit einem Sunny Home Manager 2.0 braucht es Firmware ab 2.15.5 und ausgeschaltetes prognosebasiertes Laden.
> - **SolaX (Gen4/Gen5):** Die Fernsteuerung (Mode 1) bekommt eine positive Batterieleistung; die Integration nimmt die PV heraus und holt den Rest aus dem Netz.
> - **Kostal:** Ein negativer Batterie-Sollwert lädt die Batterie. In Österreich nimmt der Kostal nach jeder Netzzuschaltung rund 10 Minuten lang keinen Sollwert an. Ob er dafür zusätzlich das Laden aus dem Netz freigegeben haben will, ist offen.
> - **Sigenergy:** Der Optimizer schaltet auf „Command Charging (PV First)“ mit der geplanten Leistung. Das Gerät hat kein eigenes Sicherheitsnetz: Stürzt Home Assistant während einer Netzladung ab, lädt die Anlage weiter, bis die Batterie voll ist (Anleitung „Sigenergy einrichten“).
>
> Bis auf Huawei kennt keines dieser Geräte einen Ziel-Ladestand. Den Ladestand beobachtet dort der Optimizer selbst und beendet die Ladung am Ziel.

## Gemeinschaft statt fester Zeitfenster

Mit **Bezug aus der Gemeinschaft** lädt der Fahrplan genau in den Viertelstunden, in denen die Gemeinschaft laut PeakShare Strom übrig hat, und nur so viel, wie dein Anteil davon hergibt. Das ersetzt feste Ladezeiten am Wechselrichter, etwa das „Geplante Laden“ eines Victron-GX-Geräts, das für die Steuerung ohnehin aus sein muss. Wie viel geladen wird, bestimmt das **Ladeziel** (Reiter **Anlage**, Expertenmodus).

## Mindestladung am Abend

Das Ladeziel kauft nur zu, wenn sich das rechnet. Willst du am Abend auf jeden Fall einen bestimmten Ladestand haben, etwa für die Nacht oder als Reserve, trag ihn bei **Mindestladung am Abend** ein. Der Termin ist derselbe wie beim Ladeziel: das Ende der PV-Zeit, also der letzte Moment des Tages, an dem die Sonne mehr liefert, als das Haus braucht.

An einem dunklen Tag liefert die Sonne nie mehr, als das Haus braucht — dann gibt es kein Ende der PV-Zeit. Für die Mindestladung gilt an so einem Tag das Ende der Erzeugung: die letzte Viertelstunde, in der die Anlage laut Prognose noch nennenswert Strom macht (mehr als 50 W). Gerade an diesen Tagen wird also aus dem Netz nachgeladen. Sagt die Prognose für einen Tag gar keine Erzeugung voraus, etwa bei Schnee auf den Modulen, gibt es keinen Termin und keine Mindestladung. Das Ladeziel bleibt davon unberührt: An einem Tag ohne Sonnenüberschuss hat es nichts zu planen.

Bei jeder Neuberechnung prüft der Fahrplan, ob die erwartete PV die Batterie bis dahin auf diesen Wert bringt. Fehlt etwas, lädt er den Rest aus dem Netz, egal was der Strom kostet, aber in den günstigsten Viertelstunden vor dem Termin. Mit Bezug aus der Gemeinschaft sind das die Viertelstunden, in denen die Gemeinschaft Strom übrig hat; gibt es an dem Tag keine, nimmt er die günstigste andere. Wird es trüber als vorhergesagt, kauft der nächste Lauf entsprechend mehr.

Gekauft wird nur bis zur Mindestladung. Was darüber bis zum **Ladeziel** fehlt, kommt weiter nur von der Sonne oder aus Viertelstunden, in denen sich der Zukauf rechnet (Mindestvorteil). Die Grenze am Zähler und die der Spitzenkappung gelten auch hier.
