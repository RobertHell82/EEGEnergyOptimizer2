# Tarife & Gemeinschaft

Der Fahrplan kennt keine Zeitfenster und keine Automatik „abends entladen" — er kennt nur einen **Preis je Viertelstunde**. In diesem Schritt legst du fest, woraus dieser Preis entsteht: was eine eingespeiste Kilowattstunde bringt, was eine gekaufte kostet und was deine Energiegemeinschaft dazu beiträgt.

## Das Prinzip

Der Einspeisepreis jeder Viertelstunde besteht aus zwei Teilen:

1. **Standardvergütung** (Basistarif) — was du bekommst, wenn die Energie nicht in einer Gemeinschaft landet.
2. **Auf- oder Abschlag der Gemeinschaft** — braucht die Gemeinschaft in einer Viertelstunde Strom, steigt der Preis; hat sie selbst Überschuss, sinkt er.

Dabei zählt nur die **Differenz** zwischen Gemeinschaftsvergütung und Standardvergütung: Eine Kilowattstunde ist entweder das eine oder das andere wert, nie beides zusammen. Diesen Preis hält der Fahrplan gegen den Bezugspreis und entscheidet, ob eine Kilowattstunde ins Netz geht, in die Batterie oder im Haus bleibt.

> [!NOTE]
> **Ohne Preisunterschied passiert nichts.** Ist eine Kilowattstunde nachts nicht mehr wert als tagsüber, wird nachts nicht eingespeist. Auf die absolute Höhe der Preise kommt es kaum an, auf ihren zeitlichen Verlauf sehr.

## Vergütung

Unter **Standardvergütung — Quelle** wählst du, woher der Basistarif kommt. Außer beim festen Wert holt die Integration ihn selbst. Wie alt der Wert ist und ob der letzte Abruf geklappt hat, steht jeweils direkt darunter; mit **Jetzt holen** stößt du einen Abruf sofort an. Antwortet eine Quelle nicht, bleibt der zuletzt gelesene Wert stehen — gab es nie einen, gilt der fest eingetragene.

| Quelle | Wann sinnvoll | Aktualisierung |
|---|---|---|
| **Fester Wert** | Dein Vertrag nennt einen festen Satz | — |
| **OeMAG-Einspeisetarif (zuletzt veröffentlichter Monat)** | Du speist über die OeMAG ein | zweimal täglich von oem-ag.at |
| **OeMAG-Einspeisetarif (laufender Monat, hochgerechnet)** | wie oben, aber mit dem Wert des laufenden Monats statt des Vormonats | alle drei Stunden |
| **Spotpreis der Strombörse** | Dein Vertrag zahlt den stündlichen Börsenpreis, z. B. aWATTar SUNNY Spot 60min | stündlich |
| **aWATTar SUNNY (fester Monatstarif)** | Du hast den SUNNY-Monatstarif von aWATTar | zweimal täglich |
| **Energie AG Team Sonne Float** (veröffentlicht oder hochgerechnet) | Du hast den Float-Tarif der Energie AG | zweimal täglich |

### Fester Wert

**Einspeisevergütung Tag (ct/kWh)** ist Pflicht. Vergütet dein Vertrag nachts anders, trägst du zusätzlich die **Einspeisevergütung Nacht (ct/kWh)** ein; leer heißt „wie am Tag". Nur bei dieser Quelle gibt es einen Nachtsatz — alle anderen kennen keinen.

### OeMAG

Der OeMAG-Tarif wechselt monatlich, und der laufende Monat erscheint erst zu Beginn des Folgemonats. Bis dahin gilt der zuletzt veröffentlichte Monat. Die **Hochrechnung** rechnet den laufenden Monat so, wie die OeMAG ihn am Monatsende festlegt: Börsenpreise, gewichtet mit der österreichischen PV-Erzeugung, begrenzt auf 60–100 % des Quartalspreises der E-Control, abzüglich Ausgleichsenergie. Im Rückblick trifft sie den veröffentlichten Wert auf rund 0,2 ct; in der ersten Monatswoche schwankt sie noch um bis zu 1,5 ct. Ist sie nicht möglich, gilt der zuletzt veröffentlichte Monat.

### Spotpreis der Strombörse

| Feld | Bedeutung |
|---|---|
| **Marktgebiet** | Österreich (EPEX Spot AT) oder Deutschland (EPEX Spot DE) |
| **Abschlag des Vermarkters (ct/kWh)** | Was dein Abnahmevertrag je Kilowattstunde vom Börsenpreis abzieht, oft 1–2 ct. Leer oder 0 = voller Spotpreis |
| **Abschlag des Vermarkters (% vom Börsenpreis)** | Ein prozentualer Abzug, gerechnet vom Betrag des Stundenpreises — für aWATTar SUNNY Spot 60min **19** |

Cent- und Prozentabschlag wirken zusammen. Die Preise für morgen erscheinen am frühen Nachmittag; bis dahin schreibt der Fahrplan den Verlauf des Vortags fort. **Negative Börsenpreise gelten wirklich** — der Fahrplan speist dann nicht ein, sondern speichert oder regelt ab.

### aWATTar SUNNY

Ein fester Netto-Preis je Monat, den aWATTar spätestens am 1. des Monats veröffentlicht. Unter **Vertragsabschluss** wählst du, welche der beiden Preisspalten für dich gilt: Verträge bis zum 25.02.2026 (Altvertrag) oder danach (aktueller Tarif). Die beiden Spalten können je Monat mehrere Cent auseinanderliegen; das Datum steht in deiner Vertragsbestätigung. Fehlt der laufende Monat noch, gilt der jüngste veröffentlichte.

### Energie AG Team Sonne Float

Der Preis ist der **Referenzmarktwert Photovoltaik** nach § 13 EAG, den die E-Control veröffentlicht, abzüglich eines Abschlags. Auch hier erscheint ein Monat erst Anfang des Folgemonats; die Variante „laufender Monat, hochgerechnet" rechnet ihn vorher aus Börsenpreis und PV-Erzeugung hoch.

| Feld | Bedeutung |
|---|---|
| **Preisvariante** | **Team Sonne Float** sinkt nie unter 0 ct. **Team Sonne Loyal Float** garantiert mindestens 2 ct/kWh — aber nur mit aufrechtem Stromliefervertrag bei der Energie AG Vertrieb |
| **Abschlag (ct/kWh)** | Laut Preisblatt 1,5 ct, aber wertgesichert. Steigt er, trägst du den neuen Wert hier ein. Leer = 1,5 ct |

## Kosten

| Feld | Bedeutung |
|---|---|
| **Arbeitspreis (ct/kWh)** | Pflicht. Was dein Lieferant je Kilowattstunde verlangt, inklusive Mehrwertsteuer — nur der Arbeitspreis. Netzverlustentgelt, Elektrizitätsabgabe und Erneuerbaren-Förderbeitrag **nicht** dazurechnen, die stecken in der Netzgebühr |
| **Netzgebühr — Netzbereich** | Dein Netzbereich (einer von 14, benannt nach dem Netzbetreiber). Die Netzgebühr kommt dann aus der Verordnung: Netznutzung, Netzverlust, Elektrizitätsabgabe und Erneuerbaren-Förderbeitrag zusammen, Netzebene 7, Haushalt, brutto. Die Sätze werden täglich aus dem Rechtsinformationssystem gelesen; bis dahin oder bei einem Ausfall gilt eine eingebaute Kopie der Verordnung |

Arbeitspreis und Netzgebühr ergeben zusammen den **Bezugspreis**, der unter den Feldern steht. Unter **Wie kommt dieser Wert zustande?** siehst du die Netzgebühr Posten für Posten samt Quelle. Beträge je Zählpunkt (Grundpreis, Messentgelt, Pauschalen) zählen nicht mit — sie fallen an, egal wie viel gespeichert wird.

Zwei Sonderfälle im Feld Netzbereich:

- **Keine eigene Netzgebühr (im Arbeitspreis enthalten)** — der Fahrplan rechnet nur mit dem Arbeitspreis. Die zeitvariablen Sätze wirken dann nicht.
- **Von Hand eintragen** — für einen Anschluss, der nicht auf Netzebene 7 hängt, oder wenn dein Preisblatt von der Verordnung abweicht. Ins Feld **Netzgebühr (ct/kWh)** gehört nur die Zeile „Netznutzung Arbeitspreis" deines Netzbetreibers, brutto. Das Netzverlustentgelt bleibt dabei außen vor, einen WiNAP gibt es hier nicht.

### Zeitvariable Netzentgelte (SNAP und WiNAP)

Mit diesem Haken rechnet der Fahrplan in bestimmten Fenstern mit einer günstigeren Netzgebühr:

- **SNAP** (Sommer-Nieder-Arbeitspreis): 1. April bis 30. September, täglich 10 bis 16 Uhr, 20 % günstiger.
- **WiNAP** (Winter-Nieder-Arbeitspreis): ab 2027, 1. Oktober bis 31. März, 22 bis 4 Uhr.

Zeiträume, Uhrzeiten und Rabatt stehen in der Verordnung und sind deshalb fest. Gesenkt wird nur das Netznutzungsentgelt, die übrigen Posten gelten rund um die Uhr.

> [!WARNING]
> **Setze den Haken nur, wenn dein Zähler viertelstündlich misst.** Die viertelstündliche Messung beim Netzbetreiber ist Voraussetzung für die zeitvariablen Sätze. Für Mengen, die einer Energiegemeinschaft zugeordnet sind, gilt der Rabatt nicht.

### Alterungskosten der Batterie (Expertenmodus)

Nur sichtbar, wenn im Tab **System** der Expertenmodus eingeschaltet ist. **Alterungskosten der Batterie (ct/kWh)** ist, was eine durchgesetzte Kilowattstunde die Batterie an Lebensdauer kostet. Höhere Werte machen die Optimierung zurückhaltender: Sie speichert nur, wenn sich der Umweg lohnt. Leer gilt 1 ct. Bei der Entladung in die Gemeinschaft ist der Wert die Schwelle — sie kommt erst zustande, wenn der Nachtsatz über der Einspeisevergütung plus Alterungskosten liegt.

## Nachtsatz und Nachtfenster

Es gibt zwei Nachtfenster, weil ein Gemeinschaftsvertrag andere Zeiten haben kann als dein Einspeisevertrag:

- **Nachtfenster von / bis** gilt für den Nachtsatz der Standardvergütung. Es erscheint nur bei der Quelle **Fester Wert**, ist mit eingetragenem Nachtsatz Pflicht und wirkt ohne ihn nicht.
- **Nachtfenster der Gemeinschaften von / bis** gilt für die Nachtvergütung der Gemeinschaften und steht im Abschnitt Energiegemeinschaft.

Beide dürfen über Mitternacht gehen; vorbelegt ist 20:00 bis 06:00. _Gerechnet wird mit ganzen Stunden — Minuten im Feld bleiben unberücksichtigt._

## Energiegemeinschaft

Bist du Mitglied einer Energiegemeinschaft, schaltest du die Karte **Energiegemeinschaft** ein. Unter **Bedarfsdaten der Gemeinschaft** wählst du, woher der Fahrplan weiß, wie viel die Gemeinschaft aufnimmt:

- **PeakShare-Prognose (EW Ansfelden)** — die Integration holt alle 30 Minuten die Bedarfsprognose der Gemeinschaft. Stunden mit hohem Bedarf werden wertvoller, dorthin verschiebt der Fahrplan die Einspeisung; Stunden mit Überschuss werden billiger, dort lädt er eher die Batterie. Zur Bedarfsspitze erreicht der Aufschlag genau deinen Anteil an der Differenz zur Standardvergütung. Fehlen die Daten, gibt es keinen Aufschlag. Das funktioniert nur für Gemeinschaften, die über PeakShare der EW Ansfelden abgewickelt werden.
- **Feste Abnahmequote (Gemeinschaft ohne PeakShare)** — für alle anderen. Statt einer Prognose rechnet der Fahrplan mit einem Mischpreis: Anteil × Abnahmequote zum Gemeinschaftssatz, der Rest zur Standardvergütung, getrennt für Tag und Nacht. Der Name der Gemeinschaft ist dann ein freier Text.

Je Gemeinschaft:

| Feld | Bedeutung |
|---|---|
| **Gemeinschaft** | Aus der PeakShare-Liste gewählt, im Quotenmodus frei eingetragen |
| **Anteil (%)** | Dein Aufteilungsschlüssel: welcher Teil der Einspeisung dieser Gemeinschaft zugeordnet ist. 0 % = reine Anzeige, die Gemeinschaft wirkt nicht auf den Fahrplan |
| **Vergütung Tag (ct/kWh)** | Was die Gemeinschaft je Kilowattstunde zahlt |
| **Vergütung Nacht (ct/kWh)** | Der Satz im Nachtfenster der Gemeinschaften. Leer = wie am Tag |
| **Gewichtung (ct/kWh)** | Ein Zuschlag ohne Geldfluss, siehe unten |
| **Abnahmequote Tag (%)** / **Nacht (%)** | Nur im Quotenmodus, Tag ist Pflicht. Welcher Teil deiner Einspeisung erfahrungsgemäß in der Gemeinschaft landet — steht in der EEG-Monatsabrechnung. Nacht leer = wie am Tag |

Unter jeder Gemeinschaft steht, was sie bewirkt: der höchste Aufschlag zur Bedarfsspitze oder, im Quotenmodus, der Mischpreis für Tag und Nacht. Liegt die Vergütung nicht über der Standardvergütung, gibt es mit PeakShare keinen Anreiz, Energie dorthin zu verschieben.

**Gewichtung.** Der Betrag wird im Fahrplan zur Vergütung dazugezählt, bevor die Differenz zur Standardvergütung gebildet wird — die Gemeinschaft zählt dadurch mehr, als sie tatsächlich zahlt. Gedacht ist das für eine EEG: Wer dort deinen Strom bezieht, spart Netzgebühren; bei dir kommt das nicht an, im Fahrplan soll es trotzdem zählen. Für eine BEG gibt es diesen Vorteil nicht, dort trägst du 0 ein. In die Geldrechnung (Gewinn- und Bilanzkarten) geht die Gewichtung nicht ein.

**Zweite Gemeinschaft.** Mit **Zweite Gemeinschaft hinzufügen** kommt ein zweiter Block dazu, etwa für die Aufteilung auf eine EEG und eine BEG. Die Anteile beider zusammen dürfen höchstens 100 % ergeben, sonst lässt sich nicht speichern. Was keiner Gemeinschaft zugeordnet ist, geht zur Standardvergütung an den Energieversorger.

> [!NOTE]
> **Warum die Vergütung unter dem Bezugspreis gedeckelt wird:** Läge der Einspeisepreis in einer Viertelstunde über dem Bezugspreis, würde der Fahrplan Strom kaufen, nur um ihn im selben Moment teurer wieder einzuspeisen — ein Scheinhandel, der die Einspeisegrenze belegt und die Batterie weniger entladen lässt. Deshalb bleibt der Preis knapp darunter. Das betrifft praktisch nur eine zu hohe Gewichtung; ein echter Börsenpreis wird nicht gekappt.

> [!TIP]
> **Die feste Abnahmequote ist eine Annahme, keine Messung.** Mittags im Sommer ist die Quote niedrig, weil alle einspeisen; nachts liegt sie nahe 100 %, solange die Gemeinschaft nachts mehr verbraucht als eingespeist wird. Prüfe den Wert gelegentlich an deiner Monatsabrechnung — auch die Gewinn- und Bilanzkarten rechnen damit.

## Wo du die Felder später findest

Alle Felder dieses Schritts stehen später in den **Einstellungen** im Tab **Tarife**: in der Karte **Vergütung und Kosten** und in der Karte **Energiegemeinschaft**. Die Alterungskosten erscheinen dort nur mit eingeschaltetem Expertenmodus (Tab **System**).
