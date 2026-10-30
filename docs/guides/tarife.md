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

### Batterie aus dem Netz laden

Die Felder dafür stehen im eigenen Reiter **Netzladen** (Beta, Expertenmodus, Huawei und Victron), auch der Bezugspreis aus der Gemeinschaft. Siehe Anleitung „Batterie aus dem Netz laden“.

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

**Tarif automatisch vom Verein.** Hinterlegt der Verein deiner Gemeinschaft seine Sätze zentral (derzeit die Energiewende Ansfelden mit allen ihren Gemeinschaften in PeakShare), steht im Block der Gemeinschaft ein Kasten **Tarif von …** mit Gültigkeit und Link zum Tarifblatt. Die Felder **Vergütung Tag**, **Vergütung Nacht**, das **Nachtfenster der Gemeinschaften** und der Bezugspreis fürs Netzladen folgen dann dem Tarifblatt und sind gesperrt. Ein neues Quartal übernimmt die Integration am Stichtag von selbst. Gilt die Nachtvergütung „nach Sonnenuntergang“, rechnet der Fahrplan das Fenster jeden Tag aus dem Sonnenstand, auf volle Stunden nach innen gerundet. Anteil, Gewichtung und Abnahmequote bleiben deine Sache. Mit **Eigene Werte verwenden** gelten wieder die eingetippten Zahlen.

Unter jeder Gemeinschaft steht, was sie bewirkt: der höchste Aufschlag zur Bedarfsspitze oder, im Quotenmodus, der Mischpreis für Tag und Nacht. Liegt die Vergütung nicht über der Standardvergütung, gibt es mit PeakShare keinen Anreiz, Strom dorthin zu verschieben.

**Gewichtung.** Dieser Betrag wird im Fahrplan zur Vergütung addiert, bevor die Differenz zur Standardvergütung gebildet wird. Die Gemeinschaft zählt dadurch mehr, als sie zahlt. Gedacht für eine EEG: Wer dort deinen Strom bezieht, spart Netzgebühren — das soll im Fahrplan zählen, auch wenn du es nicht bekommst. Bei einer BEG (Bürgerenergiegemeinschaft) gibt es diesen Vorteil nicht, dort trägst du 0 ein. In die Geldrechnung (Gewinn- und Bilanzkarten) geht die Gewichtung nicht ein.

**Zweite Gemeinschaft.** Mit **Zweite Gemeinschaft hinzufügen** kommt ein zweiter Block dazu, z. B. für eine EEG und eine BEG. Beide Anteile zusammen dürfen höchstens 100 % ergeben, sonst lässt sich nicht speichern. Der Rest geht zur Standardvergütung an den Energieversorger.

> [!NOTE]
> **Die Vergütung bleibt immer knapp unter dem Bezugspreis.** Sonst würde der Fahrplan Strom kaufen, nur um ihn im selben Moment teurer einzuspeisen — das belegt die Einspeisegrenze und die Batterie entlädt weniger. Das betrifft praktisch nur eine zu hohe Gewichtung; ein echter Börsenpreis wird nicht gekappt.

> [!TIP]
> **Die feste Abnahmequote ist eine Annahme, keine Messung.** Mittags im Sommer ist sie niedrig, weil alle einspeisen; nachts liegt sie nahe 100 %, solange die Gemeinschaft nachts mehr verbraucht als eingespeist wird. Prüfe den Wert ab und zu an deiner Monatsabrechnung — auch die Gewinn- und Bilanzkarten rechnen damit.

## Wo du die Felder später findest

Alle Felder stehen in den **Einstellungen**, Tab **Tarife**: Karte **Vergütung und Kosten** und Karte **Energiegemeinschaft**. Die Alterungskosten erscheinen dort nur im Expertenmodus (Tab **System**).
