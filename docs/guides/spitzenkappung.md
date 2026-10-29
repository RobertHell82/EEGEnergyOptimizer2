# Spitzenkappung

Mit der neuen Tarifstruktur richtet sich ein Teil der Netzgebühr nach der **Bezugsspitze**: dem höchsten Netzbezug, gemittelt über eine Viertelstunde, im ganzen Monat (Elektrizitätswirtschaftsgesetz, ElWG). Wann das für dich gilt, legt dein Netzbetreiber fest — frühestens 1.1.2027, ohne eigene Meldung 1.12.2027, spätestens 1.7.2028 (siehe Anleitung „Dashboard"). Eine einzige Viertelstunde, in der Auto, Herd und Wärmepumpe gleichzeitig laufen, legt den Preis für den ganzen Monat fest. Die Spitzenkappung hält diese Viertelstunden niedrig, in zwei Stufen:

1. **Die Batterie.** Solange sie genug hat, deckt sie Spitzen ohnehin. Wird sie knapp, hält sie eine **Reserve** zurück und gibt darin nur noch ab, was über deiner Grenze liegt. Den Grundverbrauch bezieht das Haus dann aus dem Netz.
2. **Verbraucher abschalten.** Reicht die Batterie nicht, etwa weil sie leer ist oder das Auto mehr zieht, als sie liefern kann, schaltet die Integration die Verbraucher, die du einträgst, der Reihe nach ab. Sobald wieder Platz ist, schaltet sie sie wieder ein.

> [!NOTE]
> Die erste Stufe gibt es mit **Huawei**, **Fronius**, **SMA** und **SolaX**. Mit den anderen Wechselrichtern kappt die Integration nur über das Abschalten von Verbrauchern.

## Wie es funktioniert

### Die Grenze

Du stellst eine **Grenze** in kW ein. Darunter wird nie gekappt. Wer ohnehin regelmäßig viel braucht, etwa ein Auto, das jeden Abend mit 5 kW lädt, trägt diesen Wert ein. Dann versucht die Integration gar nicht erst, darunter zu bleiben.

Liegt die Bezugsspitze des Monats schon höher als die Grenze, gilt die Monatsspitze: Bis dorthin kostet eine Viertelstunde nichts mehr. Unter 2 kW geht es nicht, so viel wird ohnehin mindestens verrechnet.

Gezählt wird wie beim Netzbetreiber das **Mittel der Viertelstunde**, nicht der Augenblick. Ein Wasserkocher, der zwei Minuten läuft, kostet keine Batterie, solange der Rest der Viertelstunde ruhig ist.

### Die Reserve

Ein fester Prozentwert wäre entweder zu klein (die Spitze kommt trotzdem) oder zu groß (die Batterie hält jede Nacht Energie zurück, die das Haus dann teuer aus dem Netz kauft). Deshalb **rechnet** die Integration die Reserve:

- Aus den letzten **vier Wochen** liest sie, wie viel Strom das Haus zu jeder Uhrzeit über der Grenze gebraucht hat (Hausverbrauch minus PV, je Viertelstunde). Mehr, als die Batterie abgeben kann, zählt dabei nicht mit: Das kann sie ohnehin nicht kappen.
- Die Reserve ist die Energie, die von **jetzt** bis zur nächsten Sonne noch über der Grenze gebraucht wird. Gezählt wird so viel, wie **9 von 10** vergleichbaren Tagen nicht überschritten haben. Lädt das Auto eine Nacht pro Woche, ist diese Nacht also abgedeckt. Der Durchschnitt würde genau sie verfehlen.
- Die Reserve **fällt über die Nacht**: Was schon vorbei ist, braucht keine Reserve mehr. Kurz vor Sonnenaufgang ist fast nichts mehr zurückgehalten, und die Batterie gibt den Rest wieder fürs Haus frei.

Die Werte kommen aus der Statistik von Home Assistant. Beim ersten Einschalten liegen dort meist schon die letzten zehn Tage, die Integration führt sie dann selbst auf vier Wochen fort. Solange weniger als **sieben** vergleichbare Tage da sind, gilt nur die Mindestreserve.

Die Reserve liegt immer **über dem Mindest-Ladestand** (Tab **Anlage**). Bei 10 % Mindest-Ladestand und 20 % Reserve hält die Batterie unter 30 % zurück. Den Mindest-Ladestand selbst unterschreitet sie auch für Spitzen nicht.

### Was die Batterie in der Reserve tut

Über der Reserve läuft alles wie bisher. Darunter begrenzt die Integration alle 30 Sekunden, wie viel die Batterie abgeben darf:

- **Bleibt der Verbrauch unter der Grenze**, gibt sie nichts ab. Das Haus bezieht seinen Grundverbrauch aus dem Netz.
- **Steigt er darüber**, gibt sie genau den Teil ab, der über der Grenze liegt. Kleinigkeiten unter 0,2 kW deckt sie nicht (aus dem Halten heraus erst ab 0,4 kW, damit die Einstellung nicht ständig hin- und herspringt): Dafür ist unter der Grenze ein Abstand von 0,3 kW eingerechnet.

Der Fahrplan rechnet die Reserve mit ein. Er verkauft am Abend nicht mehr, was in der Nacht für Spitzen gebraucht wird. Tagsüber soll die Batterie am Ende der Sonnenstunden mindestens Mindest-Ladestand plus Abendreserve erreichen; ist das **Ladeziel** (Tab **Anlage**) höher, gilt das Ladeziel. Steht die Batterie unter der Reserve, etwa morgens nach einer Nacht mit Spitzen, rechnet der Plan vom tatsächlichen Ladestand aus und lädt sie wieder auf. Im Modus **Aus** und während einer **Pause** rechnet der Plan ohne Reserve.

> [!NOTE]
> Hast du das Ladeziel ausgeschaltet, erscheint tagsüber trotzdem eine Ladeziel-Marke im Ladestandsdiagramm: Das ist die Abendreserve.

### Verbraucher abschalten

Die Verbraucher werden in der Reihenfolge der Liste abgeschaltet, aber nur, wenn die Batterie die Spitze nicht mehr decken kann und der Verbraucher gerade läuft (ab 0,2 kW). Ob die Batterie noch mehr könnte, weiß die Integration nur bei Huawei, Fronius, SMA und SolaX; bei den anderen Wechselrichtern rechnet sie ohne die Batterie und schaltet ab, sobald die Viertelstunde über die Grenze zu gehen droht. Nach jedem Abschalten wartet die Integration 60 Sekunden, denn ein Auto regelt langsam herunter und meldet seine Leistung oft verzögert.

Wieder eingeschaltet wird ein Verbraucher frühestens nach 5 Minuten, und nur, wenn er neben dem übrigen Haus eine ganze Viertelstunde lang unter die Grenze passt. Die Batterie zählt dabei nicht mit: Sonst würde der Verbraucher die Reserve leeren. Ein 11-kW-Auto kommt bei einer Grenze von 5 kW also erst zurück, wenn die Spitze vorbei ist und der Monat eine höhere Spitze erlaubt.

Damit das nicht hin- und herpendelt:

- Als Leistung gilt die **höchste der letzten fünf Minuten** vor dem Abschalten. Ein Auto, das beim Abschalten gerade erst auf 2 kW hochregelt, wird so nicht für einen 2-kW-Verbraucher gehalten. Ist die **Nennleistung** eingetragen und größer, gilt sie.
- Muss ein Verbraucher innerhalb von 15 Minuten nach dem Wiedereinschalten erneut aus, **verdoppelt sich seine Wartezeit**: 5, 10, 20, 40, höchstens 60 Minuten. Nach einer Stunde ohne erneutes Abschalten gelten wieder 5 Minuten. Ein Neustart von Home Assistant setzt das nicht zurück.

Was du von Hand wieder einschaltest, bleibt an. Die Integration greift erst bei der nächsten Spitze wieder ein. Ist die Optimierung aus, pausiert oder die Spitzenkappung abgeschaltet, schaltet sie alles wieder ein, was sie abgeschaltet hat.

> [!TIP]
> Gut geeignet sind Verbraucher, die nach dem Wiedereinschalten selbst langsam hochfahren, etwa eine Wallbox mit Ladeprofil. Schlecht geeignet sind Geräte, die sofort mit voller Leistung starten oder nach dem Abschalten eine lange Mindestpause brauchen, etwa manche Wärmepumpen.

## Einrichtung

Die Spitzenkappung steht in den **Einstellungen** im Tab **Spitzenkappung**.

| Feld | Bedeutung |
|---|---|
| **Bezugsspitzen kappen** | Schaltet die Funktion ein oder aus |
| **Grenzwert (kW)** | 2 bis 20 kW, Vorgabe 5. Darunter wird nichts gekappt |
| **Mindestreserve (%)** | Nur mit Huawei, Fronius, SMA oder SolaX. Hält die Batterie immer für Spitzen zurück, auch wenn die Statistik weniger verlangt. Vorgabe 0 |
| **Maximalreserve (%)** | Nur mit Huawei, Fronius, SMA oder SolaX. Mehr hält die Batterie nie zurück. Vorgabe 30, ein leeres Feld heißt ebenfalls 30, 0 schaltet die Reserve ab |
| **Verbraucher** | Bis zu zehn, in der Reihenfolge, in der abgeschaltet wird. Je Verbraucher ein **Leistungssensor** (W oder kW), ein **Schalter** (`switch.` oder `input_boolean.`) und optional die **Nennleistung** in kW |

Ohne Batterie-Stufe (andere Wechselrichter) braucht es mindestens einen Verbraucher, sonst täte die Funktion nichts.

**Wann sich eine Mindestreserve lohnt:** Die Statistik kennt nur die letzten vier Wochen. Ist ein großer Verbraucher neu dazugekommen, etwa ein neues E-Auto, kann eine Mindestreserve von 5 bis 10 % die Zeit überbrücken, bis die Statistik ihn kennt. Sonst bleibt sie besser bei 0: Sie hält die Batterie auch in Nächten ohne Spitze zurück.

_Alle Felder wirken ohne Neustart der Integration._

## Anzeige

- **Karte „Bezugsspitze & Spitzenkappung"** im Dashboard: Monatsspitze, laufende Viertelstunde gegen die Schwelle, Tagesspitzen des Monats und darunter, was die Spitzenkappung gerade tut — Stufe 1 (Batterie) mit Ladestand, Schwelle und Reserve, Stufe 2 (Verbraucher) mit dem Zustand jedes Geräts (siehe Anleitung „Dashboard").
- **Statuskarte, Zeile „Bezugsspitze":** Greift die Spitzenkappung gerade, steht dort ein Hinweis („Batterie hält Reserve", „Batterie deckt nur Spitzen", „Wallbox aus"). Ein Tipp springt zur Karte.
- **Fahrplan, Ladestandsdiagramm:** Die orange gestrichelte Linie über dem Mindest-Ladestand ist die **Spitzenreserve** je Viertelstunde. Unter ihr deckt die Batterie nur noch Spitzen. Ist die Reserve 0, fehlt die Linie.
- **Einstellungen, Tab Spitzenkappung:** oben der aktuelle Stand in einem Satz.
- **Fahrplan-Status:** Der Statussensor zeigt „Spitzenkappung: Batterie hält Reserve" oder „… deckt Spitzen", die Begründung steht im Attribut `status`.
- **Aktivitätsprotokoll:** Jedes Ab- und Einschalten eines Verbrauchers steht dort mit Grund.

**Reserve 0 %** heißt: In den letzten Wochen kam keine Spitze über die Schwelle, die Batterie arbeitet normal. Sie hält dann erst am Mindest-Ladestand an — mit eingeschalteter Spitzenkappung ist er eine echte Untergrenze, auch im Automatikbetrieb.

## Gut zu wissen

- **Optimierung aus oder Pause:** Die Spitzenkappung tut nichts. Die Batterie läuft im Automatikbetrieb, abgeschaltete Verbraucher kommen zurück.
- **Absturz von Home Assistant:** Fronius und SMA geben die Batterie nach spätestens fünf Minuten von selbst frei. Bei **Huawei** bleibt die gesetzte Entladeleistung im Wechselrichter stehen, bis Home Assistant wieder läuft. Ist die Spitzenkappung beim Start eingeschaltet, setzt die Integration sie auf das Maximum zurück. Wer sie in der Zwischenzeit ausgeschaltet hat, prüft die Entität **Maximale Entladeleistung** und stellt sie von Hand zurück.
- **SolaX:** Die Integration stellt den maximalen Entladestrom (`battery_discharge_max_current`). Wie bei Huawei bleibt er bei einem Absturz stehen; den Wert, der vorher eingestellt war, sichert die Integration und stellt ihn beim Aufheben wieder her. Am Gerät ist das noch nicht geprüft — wer es testet, schaut auf diese Entität.
- **Huawei: Entladeleistung:** Die Integration stellt die Number-Entität **Maximale Entladeleistung**. Wird die Batterie freigegeben, setzt sie sie auf das Maximum des Geräts zurück. Ein eigener, niedrigerer Wert, der dort vorher stand, geht dabei verloren.
- **Kosten:** Die Spitzenkappung rechnet nicht nach, ob sie sich lohnt. Eingeschaltet heißt, sie kappt. Was die Monatsspitze kostet, steht im ⓘ der Zeile „Bezugsspitze" (siehe Anleitung „Dashboard").
