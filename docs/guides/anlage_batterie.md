# Anlage & Batterie

Hier trägst du die Grenzen deiner Anlage ein: was der Wechselrichter ins Netz leisten kann und in welchem Bereich die Batterie arbeiten darf. Die Optimierung plant nie über diese Grenzen hinaus. Die meisten Werte stehen im Datenblatt von Wechselrichter und Batterie.

## Anlage

| Feld | Bedeutung |
|---|---|
| **AC-Grenzleistung des Wechselrichters (kW)** | Nennleistung auf der Netzseite (AC = Wechselstrom), laut Datenblatt. Begrenzt im Plan Einspeisung plus Hausverbrauch |
| **PV-Spitzenleistung (kWp)** | Summe der Modulleistung. Ersatzwert, wenn die AC-Grenzleistung fehlt, und Obergrenze für die Prüfung der Tageswerte in der EEG-Statistik |

Beide Felder sind **Pflicht**. Ohne sie kannst du den Schritt nicht abschließen, und in den Einstellungen lassen sie sich nicht leeren.

Die AC-Grenzleistung sollte stimmen. Ist sie zu groß, plant die Optimierung mehr, als das Gerät liefern kann. Sie wirkt hier:

- **Einspeisung plus Hausverbrauch** bleiben im Plan darunter. Eine größere Verbrauchsspitze (z. B. 11-kW-Wallbox an einem 10-kW-Gerät) rechnet der Plan als Netzbezug.
- **Ohne Einspeisegrenze** gilt AC-Grenzleistung minus 0,5 kW als höchste Einspeisung.
- Die **eigene PV-Prognose** sagt nie mehr voraus als diesen Wert.

Darunter steht die Karte **Einspeisegrenze beachten**. Schalte sie ein, wenn dein Netzbetreiber die Einspeiseleistung begrenzt. Details stehen in der Anleitung „Einspeisegrenze“.

## Batterie

### Batterie-Leistungsgrenze (kW)

So viel kann die Batterie höchstens laden oder entladen (gilt für beide Richtungen). Vorgabe: 5 kW. Zu klein verschenkt Möglichkeiten, zu groß ergibt Pläne, die der Wechselrichter nicht schafft. Im Assistenten ist das Feld Pflicht.

### Mindest-Ladestand (%)

Unter diesen Ladestand plant die Optimierung **nie**. Für sie ist die Batterie bei diesem Wert „leer“. Vorgabe: 10 %. Mit 0 darf sie ganz entladen werden.

Der Mindest-Ladestand **ist** deine Sicherheitsreserve. Eine eigene Notstromreserve in kWh gibt es nicht. Er schont die Zellen und hält einen Puffer für Lastspitzen. Wie viel sinnvoll ist, hängt von der Größe deines Speichers ab.

Der Mindest-Ladestand muss **mindestens 20 Prozentpunkte unter dem Maximum-Ladestand** liegen. Bei 100 % Maximum sind das höchstens 80 %, beim kleinsten Maximum von 70 % höchstens 50 %. Trägst du mehr ein, wird der Wert auf diese Grenze gesetzt. Das Feld zeigt immer den Wert, mit dem tatsächlich gerechnet wird.

> [!NOTE]
> **Die Reserve des Wechselrichters hebt den Mindest-Ladestand an.** Viele Geräte geben unter einem eigenen Ladestand nichts mehr ab. Liegt dieser Gerätewert höher als dein Mindest-Ladestand, plant die Optimierung mit dem Gerätewert.
>
> - **Fronius:** die Mindestreserve der Batterie (im Fronius-Webinterface). Eine Änderung dort wirkt erst am nächsten Tag oder nach einem Neustart von Home Assistant.
> - **Huawei:** der Backup-Ladestand (Notstrom). Bei mehreren Batterien zählt der höchste Wert.
> - **Sigenergy:** der höhere Wert aus Backup-Ladestand und Entlade-Abschaltgrenze. Beide Entitäten sind in der Sigenergy-Integration ab Werk deaktiviert — solange sie aus sind, gilt nur dein Mindest-Ladestand.
> - **SolaX:** der Entladeboden im Eigenverbrauchsmodus. Dort stoppt der Wechselrichter jede Entladung. Während einer geplanten Entladung senkt die Integration ihn deshalb auf 5 Prozentpunkte unter das Ziel und stellt danach deinen Wert wieder her. Geplant wird immer mit deinem Wert.
>
> Soll die Batterie tiefer entladen werden, musst du die Reserve im Gerät senken.

### Maximum-Ladestand (%)

Über diesen Ladestand plant die Optimierung nicht. **100** heißt „bis voll laden“ und ist die Vorgabe. Kleinster Wert: 70 %.

Ein niedrigerer Wert lässt oben Platz frei. Manche Zellchemien altern nahe der Vollladung schneller. Wie viel das bringt, ist offen; ein Maximum von 90 % kostete an der Testanlage kaum Erlös. Unter dem Feld steht, wie viel Kapazität zwischen Mindest- und Maximum-Ladestand nutzbar bleibt.

> [!WARNING]
> **Der Maximum-Ladestand begrenzt den Plan, nicht das Gerät.** Der Wechselrichter lädt trotzdem bis voll, wenn die Optimierung auf Aus steht, pausiert ist oder Home Assistant neu startet. Ebenso, wenn bei aktiver Einspeisegrenze mehr PV kommt, als ins Netz darf. Für echten Zellschutz stellst du die Grenze zusätzlich im Gerät ein.

### Sicherheitspuffer auf die Prognose (%)

Nur in den **Einstellungen** im **Expertenmodus**. Ändert keinen Gerätewert.

Bei z. B. 10 % rechnet der Fahrplan mit 10 % mehr Verbrauch und 10 % weniger PV, als die Prognose sagt. Er lädt dann früher und entlädt vorsichtiger. Erlaubt sind 0 bis 50 %.

Der Puffer gilt **nur für die Vorausschau**. Für die laufende Viertelstunde rechnet der Fahrplan immer mit den Messwerten.

**Vorgabe ist 0 %, und dabei sollte es meist bleiben.** Ein Aufschlag macht die Prognose nicht besser, nur vorsichtiger. Er verlagert Einspeisung aus den Bedarfsstunden der Gemeinschaft in den Speicher und kostet Ertrag. Sinnvoll ist er nur, wenn dir eine volle Batterie am Abend wichtiger ist als der letzte Cent.

### Ladeziel am Abend (%)

Nur in den **Einstellungen** im **Expertenmodus**. Vorgabe: **100 %**. Leeres Feld = Ladeziel aus.

Ohne Ladeziel lädt der Fahrplan nur, so weit es sich nach den Preisen lohnt. Oft lädt er dann nur, was das Haus über Nacht braucht, und die Batterie wird abends nicht voll.

Mit Ladeziel soll die Batterie jeden Tag **am Ende der PV-Zeit** (wenn die PV unter den Hausverbrauch fällt) mindestens so voll sein. Wann sie tagsüber lädt, entscheidet der Fahrplan. Erlaubt sind 50 % bis zum Maximum-Ladestand.

Damit das Ziel nicht an einer zu guten Prognose scheitert, rechnet der Fahrplan vorsichtig: Nachts und am Vormittag gibt die Batterie nur so viel ins Netz ab, wie sie auch an einem schwächeren Tag als vorhergesagt wieder hereinbekommt. Dafür nimmt er das untere Band der Prognose (bei Solcast den p10-Wert, sonst 60 % der Erwartung). An klaren Tagen ändert das wenig. Ist der Tag unsicher, bleibt mehr in der Batterie.

Das Ziel holt keinen Strom aus dem Netz, außer du hast „Batterie aus dem Netz laden“ eingeschaltet (Beta, nur Huawei, siehe Anleitung „Tarife & Gemeinschaft“). An einem trüben Tag wird daraus sonst, was die PV schafft. Im Diagramm des Fahrplans steht das Ziel als kleiner Kreis, und bleibt die Kurve darunter, sagt der Kreis beim Darüberfahren, warum. Es kostet etwas Einspeisung oder Wärme im Heizstab, die sonst aus demselben Überschuss gekommen wäre.

### Vormittags bevorzugt netzdienlich

Nur in den **Einstellungen** im **Expertenmodus**. Vorgabe: **aus**.

Ohne die Option lädt der Fahrplan oft schon am Morgen langsam und gleichmäßig, weil langsames Laden weniger Verlust kostet.

Mit der Option geht der PV-Überschuss **bis zur eingestellten Uhrzeit** (Vorgabe 11 Uhr) lieber ins Netz. Die Batterie lädt danach mit voller Leistung in der Mittagsspitze, wenn das Netz die Einspeisung am wenigsten braucht. Das gilt nur an Tagen, an denen die Sonne auch bei vorsichtiger Rechnung deutlich mehr bringt, als in die Batterie passt. An anderen Tagen (im Herbst und Winter oft) wirkt die Option nicht.

Dazu rechnet der Fahrplan mit einem **Bonus** auf die Einspeisung am Vormittag (Vorgabe 5 ct/kWh). Der Bonus wird nicht ausgezahlt, er dient nur der Steuerung; die Bilanz rechnet mit deinem echten Tarif. Er gilt nur für Sonnenstrom: In diesen Stunden entlädt die Batterie nicht ins Netz.

Die Option kostet etwas: Schnelleres Laden bringt mehr Verlust, und zieht es mittags zu, ist die Batterie abends weniger voll. Das **Ladeziel** fängt das teilweise ab — lass es eingeschaltet.

## Später ändern

Alle Felder findest du in den **Einstellungen**, Tab **Anlage**: Karte **Anlage** (mit Einspeisegrenze) und Karte **Batterie**. **AC-Grenzleistung**, **PV-Spitzenleistung** und **Batterie-Leistungsgrenze** erscheinen dort nur im **Expertenmodus** (Tab **System**). Mindest- und Maximum-Ladestand sind immer sichtbar.
