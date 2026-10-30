# Batterie aus dem Netz laden

Schafft die Sonne das **Ladeziel** nicht, darf der Fahrplan Strom aus dem Netz zukaufen, aber nur, wenn das günstiger ist als der Strom, den die Batterie danach in der Nacht ersetzt. Mit einer Energiegemeinschaft heißt das auch: laden, wenn die Gemeinschaft Strom übrig hat.

## Wo du es einschaltest

Die Einstellungen stehen im eigenen Reiter **Netzladen** (Beta). Er erscheint nur mit eingeschaltetem Expertenmodus (Reiter **System**) und nur mit einem Huawei-Wechselrichter oder einem Victron-System (Victron im Feldtest). Ohne den Haken **Batterie aus dem Netz laden** rechnet der Fahrplan genau wie vorher, und keines der Felder darunter ist Pflicht.

Mit Haken darf der Fahrplan das **Ladeziel** auch mit Netzstrom erreichen, wenn die Sonne an einem trüben Tag nicht reicht. Er kauft aber nur in Viertelstunden, in denen der Netzstrom billiger ist als der Strom, den die Batterie danach ersetzt: der Bezugspreis der Nacht nach dem Ende der PV-Zeit, abzüglich Lade- und Entladeverlust, Alterungskosten und Mindestvorteil. Gekauft wird nur vor dem Ende der PV-Zeit. Ob die Nacht die Energie wirklich braucht, fragt er nicht: Was übrig bleibt, speist er später ein, wenn sich das lohnt. Der Preis dafür ist eine volle Batterie am Abend. Höher als der **maximale Ladestand** (Anlage & Batterie) lädt er nie, und nie schneller als die Batterieleistung.

Billiger als die Nacht ist Netzstrom nur in drei Fällen, und mindestens einer muss eingerichtet sein:

- **Spot-Bezug.** Unter **Bezugstarif deines Lieferanten** wählst du **Börsenpreis (Spot)**. Der Preis ist dann Börsenpreis plus **Aufschlag auf den Börsenpreis (ct/kWh, netto)** plus **Aufschlag in Prozent** vom Betrag, darauf die Mehrwertsteuer, plus die Netzgebühr. Gekauft wird nur zu veröffentlichten Börsenpreisen, nie zu geschätzten.
- **Bezug aus der Gemeinschaft.** In der Karte **Bezug aus der Gemeinschaft** trägst du je Gemeinschaft den **Bezugspreis aus der Gemeinschaft (ct/kWh)** inklusive Mehrwertsteuer ein und welche Art sie ist. Bei einer EEG **lokal** oder **regional**: Der Fahrplan rechnet das Netznutzungsentgelt um 57 % bzw. 28 % verringert dazu, dazu das volle Netzverlustentgelt; Elektrizitätsabgabe und Erneuerbaren-Förderbeitrag entfallen (§ 75 Abs. 5 EAG, § 2 Abs. 1 Z 4 ElAbgG). Bei einer **Bürgerenergiegemeinschaft** gilt die volle Netzgebühr, billiger kann dort nur der Energiepreis sein. SNAP und WiNAP gelten für Gemeinschaftsstrom nicht. Zugeteilt wird wie bei der Einspeisung: Aus einer Gemeinschaft kommt höchstens dein **Anteil** am Bezug, und nur so viel, wie sie laut PeakShare in der Viertelstunde übrig hat; der Rest kostet den Preis deines Lieferanten. Lässt du den Bezugspreis leer, zählt die Gemeinschaft für das Netzladen nicht. Im Quotenmodus gibt es keinen Gemeinschaftsbezug, weil kein Überschuss bekannt ist.
- **Zeitvariable Netzentgelte.** Mit dem Haken **Zeitvariable Netzentgelte (SNAP und WiNAP)** im Reiter **Tarife** unter Kosten ist die Netzgebühr im SNAP- bzw. WiNAP-Fenster niedriger. Ein eigenes Preisfeld gibt es dafür nicht: Der Fahrplan senkt die Netzgebühr selbst, wie in der Anleitung „Tarife & Gemeinschaft“ unter „Zeitvariable Netzentgelte“ beschrieben.

Spot- und Gemeinschaftsbezug brauchen einen **Netzbereich** (Reiter **Tarife**), sonst fehlt die Netzgebühr und der Netzstrom sähe zu billig aus.

| Feld | Bedeutung |
|---|---|
| **Höchster Netzbezug beim Laden aus dem Netz (kW)** | Gilt am Zähler, Haus und Batterie zusammen. Plan und Steuerung rechnen mit Hausverbrauch und PV (die Steuerung alle 30 s mit den Messwerten) und lassen die Batterie nur so schnell laden, dass der Bezug darunter bleibt. Das Haus wird nie begrenzt. Leer = keine Grenze, die Batterie lädt mit ihrer vollen Leistung. Für die Leistungsgebühr ab 2027 lohnt eine Grenze: Der Fahrplan bewertet die Bezugsspitze sonst nicht |
| **Mindestvorteil (ct/kWh)** | Um so viel muss der Netzstrom billiger sein als die Nacht. Vorgabe 2 ct |

Während der Netzladung lädt der Wechselrichter mit der geplanten Leistung bis zum geplanten Ladestand, der Statussensor zeigt „Netzladung x kW bis y %“, im Diagramm ist sie violett gestrichelt. Der Heizstab bleibt dabei aus.

> [!WARNING]
> **Beta.** Bei Huawei ist die Zwangsladung an einem SUN2000 geprüft, sie braucht die Einstellung „Laden aus dem Netz“ nicht. Bei Victron lädt der DVCC-Ladestrom mit der geplanten Leistung, und ein positiver Netzsollwert erlaubt den Bezug; den führt der Optimizer alle 30 Sekunden nach (Anleitung „Victron einrichten“), am Gerät ist das noch nicht erprobt. Der Betrieb über den Fahrplan ist noch im Feldtest.

## Gemeinschaft statt fester Zeitfenster

Mit **Bezug aus der Gemeinschaft** lädt der Fahrplan genau in den Viertelstunden, in denen die Gemeinschaft laut PeakShare Strom übrig hat, und nur so viel, wie dein Anteil davon hergibt. Das ersetzt feste Ladezeiten am Wechselrichter, etwa das „Geplante Laden“ eines Victron-GX-Geräts, das für die Steuerung ohnehin aus sein muss. Wie viel geladen wird, bestimmt das **Ladeziel** (Reiter **Anlage**, Expertenmodus).
