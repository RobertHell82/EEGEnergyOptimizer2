# Tarife & Gemeinschaft

Der Fahrplan kennt keine festen Zeitfenster wie „abends entladen“ — er kennt nur einen **Preis je Viertelstunde**. Hier legst du fest, woraus dieser Preis entsteht: was eine eingespeiste Kilowattstunde bringt, was eine gekaufte kostet und was deine Energiegemeinschaft beiträgt.

## Das Prinzip

Der Einspeisepreis jeder Viertelstunde hat zwei Teile:

1. **Standardvergütung** (Basistarif) — was du bekommst, wenn der Strom nicht in einer Gemeinschaft landet.
2. **Auf- oder Abschlag der Gemeinschaft** — braucht die Gemeinschaft gerade Strom, steigt der Preis; hat sie selbst Überschuss, sinkt er.

Es zählt nur die **Differenz** zwischen Gemeinschafts- und Standardvergütung: Eine Kilowattstunde ist das eine oder das andere wert, nie beides. Diesen Preis vergleicht der Fahrplan mit dem Bezugspreis und entscheidet: ins Netz, in die Batterie oder im Haus verbrauchen.

> [!NOTE]
> **Ohne Preisunterschied passiert nichts.** Ist eine Kilowattstunde nachts nicht mehr wert als tagsüber, wird nachts nicht eingespeist. Wichtig ist der zeitliche Verlauf der Preise, kaum ihre Höhe.

## Vergütung

Unter **Standardvergütung — Quelle** wählst du, woher der Basistarif kommt. Außer beim festen Wert holt die Integration ihn selbst. Darunter steht, wie alt der Wert ist und ob der letzte Abruf geklappt hat; **Jetzt holen** ruft sofort ab. Antwortet eine Quelle nicht, bleibt der letzte Wert; gab es nie einen, gilt der fest eingetragene.

| Quelle | Wann sinnvoll | Aktualisierung |
|---|---|---|
| **Fester Wert** | Dein Vertrag nennt einen festen Satz | — |
| **OeMAG-Einspeisetarif (zuletzt veröffentlichter Monat)** | Du speist über die OeMAG ein | zweimal täglich von oem-ag.at |
| **OeMAG-Einspeisetarif (laufender Monat, hochgerechnet)** | wie oben, aber mit dem laufenden Monat statt dem Vormonat | alle drei Stunden |
| **Spotpreis der Strombörse** | Dein Vertrag zahlt den stündlichen Börsenpreis, z. B. aWATTar SUNNY Spot 60min | stündlich |
| **aWATTar SUNNY (fester Monatstarif)** | Du hast den SUNNY-Monatstarif von aWATTar | zweimal täglich |
| **Energie AG Team Sonne Float** (veröffentlicht oder hochgerechnet) | Du hast den Float-Tarif der Energie AG | veröffentlicht: zweimal täglich; hochgerechnet: alle drei Stunden (aus der OeMAG-Hochrechnung) |

### Fester Wert

**Einspeisevergütung Tag (ct/kWh)** ist Pflicht. Zahlt dein Vertrag nachts anders, trägst du auch die **Einspeisevergütung Nacht (ct/kWh)** ein; leer = wie am Tag. Einen Nachtsatz gibt es nur bei dieser Quelle.

### OeMAG

Der OeMAG-Tarif ändert sich monatlich; der laufende Monat wird erst Anfang des Folgemonats veröffentlicht. Bis dahin gilt der letzte veröffentlichte Monat. Die **Hochrechnung** schätzt den laufenden Monat so, wie die OeMAG ihn festlegt: Börsenpreise, gewichtet mit der österreichischen PV-Erzeugung, begrenzt auf 60–100 % des Quartalspreises der E-Control, minus Ausgleichsenergie. Sie trifft den veröffentlichten Wert auf rund 0,2 ct; in der ersten Monatswoche schwankt sie noch um bis zu 1,5 ct. Geht sie nicht, gilt der letzte veröffentlichte Monat.

### Spotpreis der Strombörse

| Feld | Bedeutung |
|---|---|
| **Marktgebiet** | Österreich (EPEX Spot AT) oder Deutschland (EPEX Spot DE) |
| **Abschlag des Vermarkters (ct/kWh)** | Was dein Vertrag je kWh vom Börsenpreis abzieht, oft 1–2 ct. Leer oder 0 = voller Spotpreis |
| **Abschlag des Vermarkters (% vom Börsenpreis)** | Prozentualer Abzug vom Betrag des Stundenpreises — für aWATTar SUNNY Spot 60min **19** |

Beide Abschläge wirken zusammen. Die Preise für morgen kommen am frühen Nachmittag; bis dahin nimmt der Fahrplan den Verlauf des Vortags. **Negative Börsenpreise gelten wirklich** — dann speist der Fahrplan nicht ein, sondern speichert oder regelt ab.

### aWATTar SUNNY

Ein fester Netto-Preis je Monat, den aWATTar spätestens am 1. des Monats veröffentlicht. Unter **Vertragsabschluss** wählst du deine Preisspalte: Verträge bis 25.02.2026 (Altvertrag) oder danach (aktueller Tarif). Die Spalten können mehrere Cent auseinanderliegen; das Datum steht in deiner Vertragsbestätigung. Fehlt der laufende Monat, gilt der jüngste veröffentlichte.

### Energie AG Team Sonne Float

Preis = **Referenzmarktwert Photovoltaik** nach § 13 EAG (veröffentlicht von der E-Control) minus Abschlag. Ein Monat erscheint erst Anfang des Folgemonats; die Variante „laufender Monat, hochgerechnet“ schätzt ihn vorher aus Börsenpreis und PV-Erzeugung.

| Feld | Bedeutung |
|---|---|
| **Preisvariante** | **Team Sonne Float** sinkt nie unter 0 ct. **Team Sonne Loyal Float** garantiert mindestens 2 ct/kWh — nur mit aufrechtem Stromliefervertrag bei der Energie AG Vertrieb |
| **Abschlag (ct/kWh)** | Laut Preisblatt 1,5 ct, aber wertgesichert (an die Inflation gekoppelt). Steigt er, trägst du den neuen Wert ein. Leer = 1,5 ct |

## Kosten

| Feld | Bedeutung |
|---|---|
| **Arbeitspreis (ct/kWh)** | Pflicht. Was dein Lieferant je kWh verlangt, inkl. Mehrwertsteuer — nur der Arbeitspreis. Netzverlustentgelt, Elektrizitätsabgabe und Erneuerbaren-Förderbeitrag **nicht** dazurechnen, die stecken in der Netzgebühr |
| **Netzgebühr — Netzbereich** | Dein Netzbereich (einer von 14, benannt nach dem Netzbetreiber). Die Netzgebühr kommt dann aus der Verordnung: Netznutzung, Netzverlust, Elektrizitätsabgabe und Erneuerbaren-Förderbeitrag, Netzebene 7, Haushalt, brutto. Täglich aus dem Rechtsinformationssystem gelesen; bis dahin oder bei einem Ausfall gilt eine eingebaute Kopie |

Arbeitspreis plus Netzgebühr ergeben den **Bezugspreis** unter den Feldern. **Wie kommt dieser Wert zustande?** zeigt die Netzgebühr Posten für Posten mit Quelle. Beträge je Zählpunkt (Grundpreis, Messentgelt, Pauschalen) zählen nicht mit — sie fallen ohnehin an.

Zwei Sonderfälle im Feld Netzbereich:

- **Keine eigene Netzgebühr (im Arbeitspreis enthalten)** — nur der Arbeitspreis zählt. Zeitvariable Sätze wirken dann nicht.
- **Von Hand eintragen** — für einen Anschluss außerhalb von Netzebene 7 oder wenn dein Preisblatt von der Verordnung abweicht. Ins Feld **Netzgebühr (ct/kWh)** gehört nur die Zeile „Netznutzung Arbeitspreis“ deines Netzbetreibers, brutto. Netzverlustentgelt bleibt außen vor, einen WiNAP gibt es hier nicht.

### Zeitvariable Netzentgelte (SNAP und WiNAP)

Mit diesem Haken rechnet der Fahrplan in bestimmten Fenstern mit einer günstigeren Netzgebühr:

- **SNAP** (Sommer-Nieder-Arbeitspreis): 1. April bis 30. September, täglich 10 bis 16 Uhr, 20 % günstiger.
- **WiNAP** (Winter-Nieder-Arbeitspreis): ab 2027, 1. Oktober bis 31. März, 22 bis 4 Uhr.

Zeiten und Rabatt sind von der Verordnung fest vorgegeben. Gesenkt wird nur das Netznutzungsentgelt, die anderen Posten bleiben gleich.

> [!WARNING]
> **Setze den Haken nur, wenn dein Zähler viertelstündlich misst.** Das ist Voraussetzung für die zeitvariablen Sätze. Für Strom, der einer Energiegemeinschaft zugeordnet ist, gilt der Rabatt nicht.

### Alterungskosten der Batterie (Expertenmodus)

Nur sichtbar, wenn im Tab **System** der Expertenmodus eingeschaltet ist. **Alterungskosten der Batterie (ct/kWh)** ist, was eine durchgesetzte Kilowattstunde die Batterie an Lebensdauer kostet. Höhere Werte machen die Optimierung zurückhaltender: Sie speichert nur, wenn sich der Umweg lohnt. Leer gilt 1 ct. Bei der Entladung in die Gemeinschaft ist der Wert die Schwelle — sie kommt erst zustande, wenn der Nachtsatz über der Einspeisevergütung plus Alterungskosten liegt.

### Batterie aus dem Netz laden (Beta, Expertenmodus, nur Huawei)

Nur in den **Einstellungen**, nur mit Expertenmodus und nur mit einem Huawei-Wechselrichter. Ohne Haken rechnet der Fahrplan genau wie vorher, und keines der Felder darunter ist Pflicht.

Mit Haken darf der Fahrplan das **Ladeziel** auch mit Netzstrom erreichen, wenn die Sonne an einem trüben Tag nicht reicht. Er kauft aber nur in Viertelstunden, in denen der Netzstrom billiger ist als der Strom, den die Batterie danach ersetzt: der Bezugspreis der Nacht nach dem Ende der PV-Zeit, abzüglich Lade- und Entladeverlust, Alterungskosten und Mindestvorteil. Gekauft wird nur vor dem Ende der PV-Zeit. Ob die Nacht die Energie wirklich braucht, fragt er nicht: Was übrig bleibt, speist er später ein, wenn sich das lohnt. Der Preis dafür ist eine volle Batterie am Abend. Höher als der **maximale Ladestand** (Anlage & Batterie) lädt er nie, und nie schneller als die Batterieleistung.

Billiger als die Nacht ist Netzstrom nur in drei Fällen, und mindestens einer muss eingerichtet sein:

- **Spot-Bezug.** Unter **Bezugstarif deines Lieferanten** wählst du **Börsenpreis (Spot)**. Der Preis ist dann Börsenpreis plus **Aufschlag auf den Börsenpreis (ct/kWh, netto)** plus **Aufschlag in Prozent** vom Betrag, darauf die Mehrwertsteuer, plus die Netzgebühr. Gekauft wird nur zu veröffentlichten Börsenpreisen, nie zu geschätzten.
- **Bezug aus der Gemeinschaft.** Je Gemeinschaft trägst du den **Bezugspreis aus der Gemeinschaft (ct/kWh)** inklusive Mehrwertsteuer ein und welche Art sie ist. Bei einer EEG **lokal** oder **regional**: Der Fahrplan rechnet das Netznutzungsentgelt um 57 % bzw. 28 % verringert dazu, dazu das volle Netzverlustentgelt; Elektrizitätsabgabe und Erneuerbaren-Förderbeitrag entfallen (§ 75 Abs. 5 EAG, § 2 Abs. 1 Z 4 ElAbgG). Bei einer **Bürgerenergiegemeinschaft** gilt die volle Netzgebühr, billiger kann dort nur der Energiepreis sein. SNAP und WiNAP gelten für Gemeinschaftsstrom nicht. Zugeteilt wird wie bei der Einspeisung: Aus einer Gemeinschaft kommt höchstens dein **Anteil** am Bezug, und nur so viel, wie sie laut PeakShare in der Viertelstunde übrig hat; der Rest kostet den Preis deines Lieferanten. Lässt du den Bezugspreis leer, zählt die Gemeinschaft für das Netzladen nicht. Im Quotenmodus gibt es keinen Gemeinschaftsbezug, weil kein Überschuss bekannt ist.
- **Zeitvariable Netzentgelte.** Mit dem Haken **Zeitvariable Netzentgelte (SNAP und WiNAP)** unter Kosten ist die Netzgebühr im SNAP- bzw. WiNAP-Fenster niedriger. Ein eigenes Preisfeld gibt es dafür nicht: Der Fahrplan senkt die Netzgebühr selbst, wie oben unter „Zeitvariable Netzentgelte“ beschrieben.

Spot- und Gemeinschaftsbezug brauchen einen **Netzbereich**, sonst fehlt die Netzgebühr und der Netzstrom sähe zu billig aus.

| Feld | Bedeutung |
|---|---|
| **Höchster Netzbezug beim Laden aus dem Netz (kW)** | Gilt am Zähler, Haus und Batterie zusammen. Plan und Steuerung rechnen mit Hausverbrauch und PV (die Steuerung alle 30 s mit den Messwerten) und lassen die Batterie nur so schnell laden, dass der Bezug darunter bleibt. Das Haus wird nie begrenzt. Leer = keine Grenze, die Batterie lädt mit ihrer vollen Leistung. Für die Leistungsgebühr ab 2027 lohnt eine Grenze: Der Fahrplan bewertet die Bezugsspitze sonst nicht |
| **Mindestvorteil (ct/kWh)** | Um so viel muss der Netzstrom billiger sein als die Nacht. Vorgabe 2 ct |

Während der Netzladung lädt der Wechselrichter mit der geplanten Leistung bis zum geplanten Ladestand, der Statussensor zeigt „Netzladung x kW bis y %“, im Diagramm ist sie violett gestrichelt. Der Heizstab bleibt dabei aus.

> [!WARNING]
> **Beta.** Die Zwangsladung ist an einem SUN2000 geprüft, sie braucht die Einstellung „Laden aus dem Netz“ nicht. Der Betrieb über den Fahrplan ist noch im Feldtest.

## Nachtsatz und Nachtfenster

Es gibt zwei Nachtfenster, weil ein Gemeinschaftsvertrag andere Zeiten haben kann als dein Einspeisevertrag:

- **Nachtfenster von / bis** gilt für den Nachtsatz der Standardvergütung. Nur bei der Quelle **Fester Wert**; mit eingetragenem Nachtsatz Pflicht, ohne ihn wirkungslos.
- **Nachtfenster der Gemeinschaften von / bis** gilt für die Nachtvergütung der Gemeinschaften (im Abschnitt Energiegemeinschaft).

Beide dürfen über Mitternacht gehen; vorbelegt ist 20:00 bis 06:00. _Es zählen nur ganze Stunden — Minuten werden ignoriert._

## Energiegemeinschaft

Bist du Mitglied einer Energiegemeinschaft, schalte die Karte **Energiegemeinschaft** ein. Unter **Bedarfsdaten der Gemeinschaft** wählst du, woher der Fahrplan weiß, wie viel die Gemeinschaft aufnimmt:

- **PeakShare-Prognose (EW Ansfelden)** — die Integration holt alle 30 Minuten die Bedarfsprognose der Gemeinschaft. Stunden mit hohem Bedarf werden wertvoller, dorthin verschiebt der Fahrplan die Einspeisung; in Stunden mit Überschuss lädt er eher die Batterie. Zur Bedarfsspitze erreicht der Aufschlag genau deinen Anteil an der Differenz zur Standardvergütung. Fehlen die Daten, gibt es keinen Aufschlag. Nur für Gemeinschaften, die über PeakShare der EW Ansfelden laufen.
- **Feste Abnahmequote (Gemeinschaft ohne PeakShare)** — für alle anderen. Der Fahrplan rechnet mit einem Mischpreis: Anteil × Abnahmequote zum Gemeinschaftssatz, der Rest zur Standardvergütung, getrennt für Tag und Nacht. Der Name der Gemeinschaft ist dann frei wählbar.

Je Gemeinschaft:

| Feld | Bedeutung |
|---|---|
| **Gemeinschaft** | Aus der PeakShare-Liste gewählt, im Quotenmodus frei eingetragen |
| **Anteil (%)** | Dein Aufteilungsschlüssel: welcher Teil der Einspeisung dieser Gemeinschaft zugeordnet ist. 0 % = nur Anzeige, ohne Wirkung auf den Fahrplan |
| **Vergütung Tag (ct/kWh)** | Was die Gemeinschaft je kWh zahlt |
| **Vergütung Nacht (ct/kWh)** | Satz im Nachtfenster der Gemeinschaften. Leer = wie am Tag |
| **Gewichtung (ct/kWh)** | Ein Zuschlag ohne Geldfluss, siehe unten |
| **Abnahmequote Tag (%)** / **Nacht (%)** | Nur im Quotenmodus, Tag ist Pflicht. Welcher Teil deiner Einspeisung erfahrungsgemäß in der Gemeinschaft landet — steht in der EEG-Monatsabrechnung. Nacht leer = wie am Tag |

Unter jeder Gemeinschaft steht, was sie bewirkt: der höchste Aufschlag zur Bedarfsspitze oder, im Quotenmodus, der Mischpreis für Tag und Nacht. Liegt die Vergütung nicht über der Standardvergütung, gibt es mit PeakShare keinen Anreiz, Strom dorthin zu verschieben.

**Gewichtung.** Dieser Betrag wird im Fahrplan zur Vergütung addiert, bevor die Differenz zur Standardvergütung gebildet wird. Die Gemeinschaft zählt dadurch mehr, als sie zahlt. Gedacht für eine EEG: Wer dort deinen Strom bezieht, spart Netzgebühren — das soll im Fahrplan zählen, auch wenn du es nicht bekommst. Bei einer BEG (Bürgerenergiegemeinschaft) gibt es diesen Vorteil nicht, dort trägst du 0 ein. In die Geldrechnung (Gewinn- und Bilanzkarten) geht die Gewichtung nicht ein.

**Zweite Gemeinschaft.** Mit **Zweite Gemeinschaft hinzufügen** kommt ein zweiter Block dazu, z. B. für eine EEG und eine BEG. Beide Anteile zusammen dürfen höchstens 100 % ergeben, sonst lässt sich nicht speichern. Der Rest geht zur Standardvergütung an den Energieversorger.

> [!NOTE]
> **Die Vergütung bleibt immer knapp unter dem Bezugspreis.** Sonst würde der Fahrplan Strom kaufen, nur um ihn im selben Moment teurer einzuspeisen — das belegt die Einspeisegrenze und die Batterie entlädt weniger. Das betrifft praktisch nur eine zu hohe Gewichtung; ein echter Börsenpreis wird nicht gekappt.

> [!TIP]
> **Die feste Abnahmequote ist eine Annahme, keine Messung.** Mittags im Sommer ist sie niedrig, weil alle einspeisen; nachts liegt sie nahe 100 %, solange die Gemeinschaft nachts mehr verbraucht als eingespeist wird. Prüfe den Wert ab und zu an deiner Monatsabrechnung — auch die Gewinn- und Bilanzkarten rechnen damit.

## Wo du die Felder später findest

Alle Felder stehen in den **Einstellungen**, Tab **Tarife**: Karte **Vergütung und Kosten** und Karte **Energiegemeinschaft**. Die Alterungskosten erscheinen dort nur im Expertenmodus (Tab **System**).
