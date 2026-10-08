# Anlage & Batterie

In diesem Schritt trägst du die Grenzen deiner Anlage ein: was der Wechselrichter netzseitig leisten kann und in welchem Bereich die Batterie arbeiten darf. Die Optimierung plant nie über diese Grenzen hinaus. Die meisten Werte stehen im Datenblatt von Wechselrichter und Batterie.

## Anlage

| Feld | Bedeutung |
|---|---|
| **AC-Grenzleistung des Wechselrichters (kW)** | Nennleistung auf der Netzseite, aus dem Datenblatt. Sie begrenzt im Plan die Summe aus Einspeisung und Hausverbrauch |
| **PV-Spitzenleistung (kWp)** | Summe der Modulleistung. Ersatzwert, falls die AC-Grenzleistung fehlt, und Obergrenze für die Plausibilitätsprüfung der Tageswerte in der EEG-Statistik |

Beide Felder sind **Pflicht**: Ohne sie lässt sich der Schritt nicht abschließen, und auch in den Einstellungen kannst du sie nicht wieder leeren.

Die AC-Grenzleistung sollte stimmen. Ist sie zu groß, entstehen Pläne, die das Gerät nicht liefern kann. Sie wirkt an mehreren Stellen:

- **Einspeisung plus Hausverbrauch** bleiben im Plan darunter. Eine Verbrauchsspitze über der AC-Grenze (etwa eine 11-kW-Wallbox an einem 10-kW-Gerät) rechnet der Plan als Netzbezug. Der Wechselrichter könnte sie ohnehin nicht decken.
- **Ohne Einspeisegrenze** gilt die AC-Grenzleistung minus 0,5 kW als höchste Einspeisung.
- Die **eigene PV-Prognose** wird auf diesen Wert gedeckelt. Mehr, als das Gerät liefert, wird nie prognostiziert.

Darunter steht die Karte **Einspeisegrenze beachten**. Schalte sie ein, wenn dein Netzbetreiber die Einspeiseleistung begrenzt. Was die Grenze bewirkt und wie du sie richtig einträgst, beschreibt die Anleitung „Einspeisegrenze“.

## Batterie

### Batterie-Leistungsgrenze (kW)

So viel Leistung kann die Batterie höchstens aufnehmen oder abgeben. Der Wert gilt in beide Richtungen, für Laden und Entladen; vorgegeben sind 5 kW. Ist er zu klein, verschenkst du Möglichkeiten. Ist er zu groß, entstehen Pläne, die der Wechselrichter nicht erfüllt. Im Assistenten ist das Feld Pflicht.

### Mindest-Ladestand (%)

Unter diesen Ladestand plant die Optimierung **nie**. Er ist eine harte Untergrenze, keine Empfehlung: Für die Optimierung ist die Batterie bei diesem Wert „leer“. Vorgegeben sind 10 %, mit 0 darf bis leer entladen werden.

Der Mindest-Ladestand **ist** die Sicherheitsreserve. Eine eigene Notstromreserve mit kWh-Angabe gibt es nicht mehr. Was im Speicher bleiben soll, stellst du allein hier ein. Er schont die Zellen und lässt einen Puffer für Lastspitzen. Wie viel sinnvoll ist, hängt von der Größe deines Speichers ab.

Der Mindest-Ladestand darf **höchstens 20 Prozentpunkte unter dem Maximum-Ladestand** liegen, damit der Optimierung ein nutzbarer Bereich bleibt. Bei einem Maximum von 100 % sind das 80 %, beim tiefsten erlaubten Maximum von 70 % sind es 50 %. Trägst du mehr ein, wird der Wert auf diese Grenze gesetzt. Das Feld zeigt immer den Wert, mit dem die Optimierung tatsächlich rechnet.

> [!NOTE]
> **Die Reserve des Wechselrichters hebt den Mindest-Ladestand an.** Viele Geräte halten selbst einen Ladestand zurück und geben darunter nichts mehr ab. Liegt dieser Gerätewert höher als dein Mindest-Ladestand, plant die Optimierung mit dem Gerätewert. Sonst würde sie Entladungen planen, die der Wechselrichter verweigert, und Plan und Wirklichkeit liefen dauerhaft auseinander.
>
> - **Fronius:** die Mindestreserve der Batterie (im Fronius-Webinterface eingestellt). Eine Änderung dort greift erst am nächsten Tag oder nach einem Neustart von Home Assistant.
> - **Huawei:** der Backup-Ladestand (Notstrom) der Batterie. Bei mehreren Batterien zählt der höchste Wert.
> - **Sigenergy:** das Höhere aus Backup-Ladestand und Entlade-Abschaltgrenze. Beide Entitäten sind in der Sigenergy-Integration ab Werk deaktiviert — solange sie aus sind, gilt allein dein Mindest-Ladestand.
> - **SolaX:** der Entladeboden im Eigenverbrauchsmodus. Er ist ein harter Riegel: Der Wechselrichter stoppt die Entladung dort, auch mitten in einer befohlenen Entladung. Für die Dauer einer geplanten Entladung senkt die Integration ihn deshalb auf 5 Prozentpunkte unter das Ziel und stellt danach deinen Wert wieder her. Die Optimierung plant immer mit deinem eingestellten Wert.
>
> Soll die Batterie tiefer entladen werden, musst du die Reserve im Gerät senken. Eine Einstellung hier reicht dafür nicht.

### Maximum-Ladestand (%)

Über diesen Ladestand plant die Optimierung nicht. **100** heißt „bis voll laden“ und ist die Vorgabe. Der kleinste erlaubte Wert ist 70 %, darunter bliebe zu wenig nutzbarer Bereich.

Mit einem niedrigeren Wert bleibt oben ein Rest frei. Manche Zellchemien altern nahe der Vollladung schneller. Wie viel eine solche Grenze im Einzelfall bringt, ist offen. An der Testanlage kostete ein Maximum von 90 % kaum Erlös. Unter dem Feld steht, wie viel Kapazität zwischen Mindest- und Maximum-Ladestand nutzbar bleibt.

> [!WARNING]
> **Der Maximum-Ladestand begrenzt den Plan, nicht das Gerät.** Der Wechselrichter lädt weiterhin bis voll, wenn die Optimierung auf Aus steht, pausiert ist oder Home Assistant neu startet. Dasselbe passiert, wenn bei aktiver Einspeisegrenze mehr PV-Leistung kommt, als ins Netz darf. Soll die Grenze die Zellen wirklich schützen, stellst du sie zusätzlich im Gerät ein.

### Sicherheitspuffer auf die Prognose (%)

Dieses Feld gibt es nur in den **Einstellungen** und nur im **Expertenmodus**, im Assistenten fehlt es. Es verändert keinen Gerätewert. Es ist eine bewusste Abweichung von der Prognose.

Mit einem Puffer von z. B. 10 % rechnet der Fahrplan mit 10 % mehr Verbrauch und 10 % weniger PV-Ertrag, als die Prognose sagt. Er lädt dadurch eher und entlädt zurückhaltender. Die Batterie ist abends eher voll und nachts seltener leer. Erlaubt sind 0 bis 50 %. Bei 50 % plant das Modell mit der halben Sonne und dem anderthalbfachen Verbrauch.

Der Puffer wirkt **nur auf die Vorausschau**. Für die laufende Viertelstunde rechnet der Fahrplan immer mit den gemessenen Werten für PV und Hausverbrauch. Über diese Viertelstunde gibt es nichts zu raten, und ein Aufschlag auf einen Messwert wäre ein Fehler, keine Vorsicht.

**Die Vorgabe ist 0 %, und dabei sollte es meist bleiben.** Ein Aufschlag macht die Vorhersage nicht besser, er verschiebt sie nur, und das in der Hälfte der Fälle in die falsche Richtung. Außerdem lenkt er Einspeisung aus den Bedarfsstunden der Gemeinschaft in den Speicher. Das kostet Ertrag und läuft gegen den Zweck der Optimierung.

Sinnvoll ist ein Puffer, wenn du bewusst vorsichtiger fahren willst: Eine volle Batterie am Abend ist dir dann wichtiger als der letzte Cent Einspeisung.

### Ladeziel am Abend (%)

Auch dieses Feld gibt es nur in den **Einstellungen** und nur im **Expertenmodus**. Die Vorgabe ist **100 %**. Leerst du das Feld, ist das Ladeziel aus.

Ohne Ladeziel lädt der Fahrplan nur so weit, wie es sich nach den Preisen lohnt. Bringt eine Kilowattstunde am Abend nicht mehr als zu Mittag, lädt er nur, was das Haus über Nacht braucht. Die Batterie wird dann abends nicht voll.

Mit einem Ladeziel von z. B. 100 % soll die Batterie jeden Tag **zum Ende der PV-Zeit** mindestens so voll sein, also dann, wenn die PV unter den Hausverbrauch fällt. Wann sie tagsüber lädt, entscheidet der Fahrplan weiter selbst. Erlaubt sind 50 % bis zum Maximum-Ladestand.

Damit das Ziel nicht an einer zu guten Prognose scheitert, rechnet der Fahrplan vorsichtig: Nachts und am Vormittag gibt die Batterie nur so viel ins Netz ab, wie sie auch an einem schwächeren Tag als vorhergesagt wieder hereinbekommt. Dafür nimmt er das untere Band der Prognose (bei Solcast den p10-Wert, sonst 60 % der Erwartung). An klaren Tagen ändert das wenig. Ist der Tag unsicher, bleibt mehr in der Batterie.

Das Ziel holt nie Strom aus dem Netz. An einem trüben Tag wird daraus, was die PV schafft. Im Diagramm des Fahrplans steht das Ziel als kleiner Kreis, und bleibt die Kurve darunter, sagt der Kreis beim Darüberfahren, warum. Es kostet etwas Einspeisung oder Wärme im Heizstab, die sonst aus demselben Überschuss gekommen wäre.

### Vormittags bevorzugt netzdienlich

Auch diese Option gibt es nur in den **Einstellungen** und nur im **Expertenmodus**. Die Vorgabe ist **aus**.

Ohne die Option lädt der Fahrplan die Batterie oft schon am Morgen, langsam und gleichmäßig. Bringt Einspeisen um 8 Uhr gleich viel wie um 12 Uhr, ist das für ihn die günstigste Art zu laden, weil langsames Laden weniger Batterieverluste kostet.

Mit der Option geht der PV-Überschuss **bis zur eingestellten Uhrzeit** (Vorgabe 11 Uhr) lieber ins Netz. Die Batterie lädt danach mit voller Leistung, also in der Mittagsspitze, wenn das Netz die Einspeisung am wenigsten braucht. Das gilt nur, soweit es sich ausgeht: Reicht die Sonne am Nachmittag nicht mehr zum Vollladen, lädt der Fahrplan trotzdem früher.

Dafür rechnet der Fahrplan mit einem **Bonus** auf die Einspeisung am Vormittag (Vorgabe 5 ct/kWh). Ausgezahlt wird der Bonus nicht, er dient nur der Steuerung. Die Bilanz rechnet weiter mit deinem echten Tarif. Ein kleiner Bonus verschiebt kaum etwas. Den Bonus bekommt nur Sonnenstrom: In diesen Stunden entlädt der Fahrplan die Batterie nicht ins Netz.

Die Option kostet wenig, aber nicht nichts. Schnelleres Laden bringt etwas mehr Verlust, und an einem Tag, an dem es mittags zuzieht, ist die Batterie abends weniger voll. Das **Ladeziel** fängt das zum Teil ab, deshalb lass es eingeschaltet.

## Später ändern

Alle Felder dieses Schritts findest du wieder in den **Einstellungen**, Tab **Anlage**: die Karte **Anlage** mit der Einspeisegrenze und die Karte **Batterie**. Gerätedaten aus dem Datenblatt ändern sich im Betrieb praktisch nie. Deshalb erscheinen **AC-Grenzleistung**, **PV-Spitzenleistung** und **Batterie-Leistungsgrenze** in den Einstellungen nur im **Expertenmodus** (Tab **System**). Mindest- und Maximum-Ladestand sind immer sichtbar.
