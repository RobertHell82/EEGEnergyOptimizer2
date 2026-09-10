# Changelog

Alle nennenswerten Änderungen am EEG Energy Optimizer.

Format orientiert sich an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/).
Versionierung folgt [SemVer](https://semver.org/lang/de/).

> Dieses Repository beginnt mit der 2.0.0. Die Vorgeschichte — die 1.x-Reihe mit
> der zustandsbasierten Steuerung und die Prototyp-Iterationen der
> Fahrplan-Optimierung — liegt im vorherigen, nicht öffentlichen Repository
> `EEGEnergyOptimizer-chamo`.

## [2.1.0-heizstab4] - 2026-09-10

### Hinzugefügt

- **Der Heizstab steht jetzt auch in der Statuskarte.** Bisher tauchte er nur im Optimierungsplan und in den Detailzeilen auf — die Werteliste und das Energieflussdiagramm oben kannten ihn nicht. Das war nicht nur eine Lücke, sondern eine falsche Bilanz: Der Sensor „Hausverbrauch" rechnet den Heizstab heraus, seine Leistung fehlte im Bild also komplett, und die PV-Aufteilung ging um genau diesen Betrag nicht auf. Die Werteliste hat nun eine Kachel „Heizstab" mit Leistung und Wassertemperatur, das Flussdiagramm einen eigenen Kasten neben dem Haus samt der Linie, über die er seine Energie bekommt — gelb aus der PV, rot aus dem Netz beim Komfortheizen unter der Mindesttemperatur. Antwortet der Ohmpilot nicht, steht ein Gedankenstrich statt einer 0: nichts zu wissen ist nicht dasselbe wie zu wissen, dass er aus ist. Ohne konfigurierten Heizstab sieht die Karte unverändert aus.

## [2.1.0-heizstab3] - 2026-09-10

### Behoben

- **Eine URL im Feld „Adresse des Ohmpilot“ verhinderte jede Verbindung.** Der Ohmpilot hat ein Webinterface, also liegt es nahe, dessen Adresse einzutragen — an einer Anlage stand `http://192.168.100.58/`, und der Treiber scheiterte bei jedem Versuch mit einer Meldung, die wie ein Netzwerkproblem aussah. Der Heizstab heizte nie, ohne dass es jemand als Konfigurationsfehler erkennen konnte. Jetzt werden Schema, Pfad, Zugangsdaten und Port-Anhang beim Speichern und beim Verbinden abgeschält — bestehende Konfigurationen laufen ohne erneutes Speichern.
- **Die Karte „Gesetzte Steuerwerte“ zieht mit jedem Steuerungslauf nach** (aus 2.1.0-dev5). Bisher war sie eine Momentaufnahme vom Öffnen des Dashboards und widersprach nach einem Moduswechsel minutenlang der Statuskarte darüber.

### Geändert

- **Die Heizstab-Kennzahl im Optimierungsplan ist kurz, die Erklärung steckt im Info-Symbol.** Unter der Legende steht „Heizstab im Plan: x kWh“ bzw. „kein Überschuss“; warum, sagt der Tooltip.

## [2.1.0-heizstab2] - 2026-09-10

### Geändert

- **Der Heizstab ist im Optimierungsplan immer sichtbar, sobald er konfiguriert ist.** Bisher erschien die Serie „Heizstab geplant“ nur, wenn der Plan in den nächsten 48 Stunden tatsächlich Überschuss über der Einspeisegrenze sah — ohne solchen Überschuss zeigte die Karte nichts, und man konnte nicht erkennen, ob der Heizstab überhaupt mitgedacht wird. Jetzt steht unter der Legende eine Kennzahl: die geplante Wärme in kWh, oder „kein Überschuss“ mit dem Hinweis, dass die Steuerung trotzdem nach der Messung heizt, sobald die Einspeisung real an der Grenze klebt.
- **Warnung im Heizstab-Tab ohne Einspeisegrenze.** Der Heizstab nimmt nur, was über die Einspeisegrenze hinausgeht; ist unter „Anlage“ keine konfiguriert, gilt die AC-Grenzleistung minus 0,5 kW, und er startet praktisch nie. Der Tab sagt das jetzt deutlich.

## [2.1.0-heizstab1] - 2026-09-10

> Vorabversion auf dem Branch `feature/heizstab` — der Heizstab ist gebaut und getestet, aber noch nicht an einer Anlage im Feld nachgewiesen. Ohne konfigurierten Heizstab verhält sich diese Version wie 2.1.0-dev4.

### Hinzugefügt

- **Heizstab (Fronius Ohmpilot) als Senke für Überschuss.** Eine bewusst groß gebaute Anlage erzeugt an guten Tagen mehr, als Batterie, Haus und Einspeisegrenze aufnehmen — bisher hat der Wechselrichter den Rest abgeregelt. Jetzt steuert die Optimierung einen Ohmpilot direkt per Modbus TCP und gibt ihm genau diesen Überschuss: Klebt die gemessene Einspeisung an der Grenze, bekommt der Heizstab alle 30 Sekunden 0,5 kW mehr; fällt sie darunter, gibt er die Lücke in einem Schritt zurück, bei Netzbezug sofort alles. Während einer Entladung ins Netz, im Modus Aus und in der Startphase steht er auf 0 — Einspeisung an die Gemeinschaft wird nie weggeheizt, und aus der Batterie wird nie geheizt. Einstellungen unter „Anlage → Heizstab": Adresse und Port des Ohmpilot, Leistung des Heizstabs, Maximaltemperatur (bis hierher wird geheizt, 3 K Hysterese), Mindesttemperatur (darunter Vorrang vor der Einspeisung: aller PV-Überschuss, aber kein Netz- und kein Batteriestrom), der eigene Schalter „auch aus dem Netz heizen" (Vorgabe aus — eingeschaltet volle Leistung unter der Mindesttemperatur und keine Entladung ins Netz, solange das dauert), die Reihenfolge „Überschuss zuerst in den Heizstab" (sonst zuerst die Batterie) und ein Wärmewert in ct/kWh. Voraussetzung: der Ohmpilot ist vom Gen24 entkoppelt — dessen eigenes Energiemanagement regelt die Einspeisung auf null. Anleitung „Heizstab" im Panel und in `docs/guides/`.
- **Vier neue Sensoren mit konfiguriertem Heizstab:** „Heizstab Leistung", „Heizstab Temperatur", „Heizstab Sollwert" (samt Begründung als Attribut) und „Heizstab Energie heute" (seit 04:00, wie die Bilanz). Der Optimierungsplan zeigt die geplante Heizstab-Leistung als rote gestrichelte Linie — das, was das Modell sonst abregeln müsste. Die Statuskarte nennt Sollwert, Ist-Leistung und Wassertemperatur; die Steuerwerte-Ansicht führt den Heizstab mit auf.
- **Wärme in Bilanz und Gewinnkarte.** „Ersparnis durch PV" zählt die aus PV geheizte Energie zum Wärmewert, die Gewinnkarte bewertet Plan und Standardbetrieb mit derselben Regel (auch der Standardbetrieb gibt dem Heizstab, was er abregeln würde). Ohne Wärmewert wird die Energie gezählt, aber nicht bewertet.
- **Ersatzwärme und Zusatzwärme.** Wer eine zweite Heizquelle hat, die den Puffer nur bis zu einer Temperatur heizt (Fernwärme bis 55 °C), trägt diese unter „Temperatur der anderen Heizquelle" ein. Wärme bis dorthin ersetzt die andere Quelle und zählt zum Wärmewert; Wärme darüber ist Zusatzwärme — der Sensor „Heizstab Energie heute" weist beides getrennt aus, die Bilanzkarte zeigt „davon Zusatzwärme", bewertet wird nur die Ersatzwärme. Leer = keine Unterscheidung.
- **Eigener Einstellungs-Tab „Heizstab".** Der Heizstab steht nicht unter „Anlage" und nicht im Einrichtungsassistenten, sondern in einem eigenen Tab — und sagt im Titel, dass derzeit nur der Fronius Ohmpilot unterstützt wird.

### Geändert

- **Der Hausverbrauch ist ohne den Heizstab.** Sensor, Verbrauchsprofil, erster Stützpunkt des Fahrplans und Entlade-Nachführung rechnen den Heizstab heraus — er ist eine gesteuerte Senke, kein Verbrauch, den das Profil an jedem Sonnentag als Mittagslast lernen soll. Ohne konfigurierten Heizstab ändert sich nichts.

## [2.1.0-dev5] - 2026-09-10

### Behoben

- **Die Karte „Gesetzte Steuerwerte“ zieht jetzt mit jedem Steuerungslauf nach.** Bisher wurde sie genau einmal beim Öffnen des Dashboards geladen und danach nur über den Knopf „Aktualisieren“ — eine Momentaufnahme. Nach dem Umschalten von „Aus“ auf „Ein“ stand in der Statuskarte binnen 30 Sekunden „Laden begrenzt“, in der Steuerwerte-Karte darunter aber minutenlang noch der Standardwert aus dem Anzeige-Modus. Jetzt lädt die Karte bei jedem Guard-Lauf neu, solange sie sichtbar ist; der Knopf bleibt für sofort.

## [2.1.0-dev4] - 2026-09-09

### Behoben

- **Einstellungen: Überschrift „Energiegemeinschaft (EW Ansfelden – PeakShare)" hieß noch nach PeakShare.** Die Karte gilt seit 2.1.0-dev3 für jede Gemeinschaft, mit oder ohne PeakShare — sie heißt jetzt wie im Assistenten schlicht „Energiegemeinschaft".

## [2.1.0-dev3] - 2026-09-09

### Hinzugefügt

- **Energiegemeinschaft ohne PeakShare: feste Abnahmequote.** Unter „Energiegemeinschaft" gibt es die Wahl der Bedarfsdaten: PeakShare-Prognose wie bisher oder eine feste Abnahmequote je Gemeinschaft (Tag und Nacht, in Prozent). Die Quote ist der Anteil der angebotenen Einspeisung, den die Gemeinschaft erfahrungsgemäß aufnimmt — er steht in jeder EEG-Monatsabrechnung. Der Fahrplan rechnet daraus einen Mischpreis: Anteil × Quote zum Gemeinschaftssatz, der Rest zur Standardvergütung. Bisher fiel eine Gemeinschaft ohne PeakShare komplett heraus, jede Kilowattstunde zählte zum Basistarif — für einen aWATTar-SUNNY-Kunden in einer EEG im Sommer also 1,2 statt rund 9 Cent. Der Name der Gemeinschaft ist im Quotenmodus ein Freitext; PeakShare wird dann nicht mehr abgefragt, die Bedarfskarte im Dashboard entfällt.
- **Gewinn- und Bilanzkarten rechnen mit der Quote.** Die Zuteilung zur Gemeinschaft folgt im Quotenmodus der erklärten Annahme statt des Saldos und ist als solche beschriftet. Die Regel „keine Prognose, kein erfundener Erlös" bleibt für PeakShare unverändert.

## [2.1.0-dev2] - 2026-09-09

### Hinzugefügt

- **aWATTar SUNNY als Standardvergütung.** Neue Quelle „aWATTar SUNNY (fester Monatstarif)" im Einrichtungsassistenten und in den Einstellungen. Der Optimizer liest den Monatspreis zweimal täglich aus der Preistabelle „Berechnungsmethodik & Preise", die aWATTar auf der Tarifseite veröffentlicht, zur Not von der Tarifseite selbst, und zeigt Wert, Monat, Herkunft und Alter an — am Monatsanfang, solange der neue Wert fehlt, stündlich. Seit dem 25.02.2026 führt aWATTar zwei Preisspalten (Vertragsabschluss bis dahin bzw. danach), die je Monat um mehrere Cent auseinanderliegen können; welche gilt, wählt man unter „Vertragsabschluss", ein Wechsel wirkt sofort. Fehlt der laufende Monat noch, gilt der jüngste veröffentlichte; antwortet keine Quelle, bleibt der zuletzt gelesene Wert stehen, ohne einen solchen der fest eingetragene. Wie bei der OeMAG gibt es keinen Nachtsatz.
- **Prozent-Abschlag beim Spotpreis — für aWATTar SUNNY Spot 60min.** Neben dem Cent-Abschlag lässt sich jetzt ein Prozentsatz vom Betrag des Stundenpreises abziehen (SUNNY Spot 60min: 19 %). Bei negativen Börsenpreisen wird die Einspeisung dadurch noch teurer — genau wie im Tarif; der Fahrplan regelt dann ab. Cent- und Prozent-Abschlag wirken zusammen, die Vorschau der Gemeinschaftsaufschläge rechnet beide mit.

## [2.1.0-dev1] - 2026-09-08

### Hinzugefügt

- **Sigenergy SigenStor wird gesteuert (Feldtest).** Neuer Wechselrichtertyp im Einrichtungsassistenten, Steuerung über die HACS-Integration [Sigenergy Local Modbus](https://github.com/TypQxQ/Sigenergy-Local-Modbus) (`sigen`) — Remote EMS per Schalter, Modus-Auswahl und Leistungslimits, ohne eigenes Modbus. Eine Verbindung steuert die ganze Anlage, auch mit mehreren Wechselrichtern; alle Sensoren kommen in kW vom Gerät „Sigen Plant", die Speicherkapazität als Sensor (keine manuelle Eingabe nötig). Ladelimit → „Command Charging (PV First)", Entladung → „Command Discharging (ESS First)", Freigabe → „Maximum Self Consumption" und Remote EMS aus.
- **Der Assistent zeigt, welche Sigenergy-Steuerentitäten noch zu aktivieren sind.** Die Integration legt alle Schreib-Entitäten deaktiviert an; der Optimizer liest ihren Zustand aus der Entity-Registry, listet die vier Pflicht-Entitäten mit Status auf und lässt erst weiter, wenn sie aktiv sind. Die Modus-Auswahl ist erst mit eingeschaltetem Remote EMS verfügbar — der Treiber schaltet ein, wartet darauf und setzt dann den Modus, weil Home Assistant Service-Aufrufe an nicht verfügbare Entitäten sonst stillschweigend überspringt.
- **Anleitung „Sigenergy einrichten"** im Panel und in `docs/guides/`: Modbus am Gerät freischalten, Integration installieren, Steuerentitäten aktivieren, was der Optimizer am Gerät tut, offene Punkte des Feldtests.

> Sigenergy kennt kein geräteseitiges Sicherheitsnetz (weder Watchdog noch Rückfallzeit). Der Optimizer gibt bei „Aus", fehlendem Plan und Neustart aktiv frei; nach einem harten Absturz von Home Assistant bleibt der letzte Befehl am Gerät stehen — dann den Schalter „Remote EMS" manuell ausschalten. Am Gerät noch zu bestätigen: ob die Ladesperre die Hausentladung mit einschränkt, ob die Entladung ins Netz liefert und ob der Entlade-Cut-Off eine befohlene Entladung stoppt.

## [2.0.4-dev2] - 2026-09-08

### Behoben

- **Der Fahrplan kaufte nachts Strom, obwohl die Energie im Akku lag.** An einer Testanlage 5,08 kWh für 1,17 € in einer einzigen Nacht — der Ladestand fror bei 51,7 % ein, statt das Haus zu versorgen. Dahinter steckt die Notstrom-Reserve: sie schaut 18 Stunden voraus und schreibt einen Mindest-Ladestand fest, gerechnet mit dem Schlechtwetter-Pfad der Prognose. Solcasts p10 wird für spätere Tage aber immer pessimistischer (an der Anlage: morgen 44 %, übermorgen 28 %, dann 14 % der Erwartung) — das ist keine Wetteraussage mehr, sondern die Unsicherheit der Prognose selbst. Lag die Vorgabe über dem, was aus PV nachzuladen war, blieb nur der Netzbezug. Der Schlechtwetter-Pfad wird jetzt nach unten begrenzt: schlechtestenfalls 60 % der Erwartung, derselbe Wert, mit dem Forecast.Solar ohne p10 längst rechnet. Die Reserve selbst bleibt unverändert und schützt den Speicher weiter gegen die Einspeisung ins Netz — nur den eigenen Verbrauch kann sie nicht mehr ins Netz verlagern.
- **Gewinnkarte: der Vergleich hielt den Endstand nur nach unten fest.** Endet der Vorschau-Horizont mittags, lud der simulierte Standardbetrieb mit voller Leistung weiter und stand am Ende bei 98 % gegen 57 % des Fahrplans — die Schieflage aus `2.0.4-dev1` kippte damit auf die andere Seite. Beide Seiten enden jetzt auf demselben Ladestand, die Endbestands-Gutschrift kürzt sich vollständig heraus.

## [2.0.4-dev1] - 2026-09-08

### Behoben

- **Gewinnkarte rechnete der Optimierung einen Verlust an, der keiner war.** Der Fahrplan muss am Ende seines Vorschau-Horizonts einen bestimmten Ladestand vorweisen — eine Modellvorgabe, damit er die Batterie nicht in den letzten Stunden verkauft. Fällt dieses Ende in die Nacht, deckt er den Hausverbrauch aus dem Netz, weil er die Reserve halten muss. Der simulierte Standardbetrieb kannte diese Vorgabe nicht, fuhr die Batterie leer und stand damit scheinbar besser da. Verglichen wurden ungleiche Endzustände: an einer Testanlage 1,33 von 1,73 € ausgewiesenem „Verlust". Der Standardbetrieb hält jetzt denselben Endstand wie der Fahrplan — und zwar so spät wie möglich, damit ihm die Vorgabe nicht den ganzen Horizont über Energie abzwingt, die die Sonne noch nachliefert. Der Tagesrückblick („Ersparnis durch Optimierung") ist unverändert: dort ist der Endstand gemessen, nicht vorgegeben.

## [2.0.3] - 2026-09-07

> Fasst die Entwicklungsstände `2.0.3-devfronius.1` bis `.9` zusammen (Einzelheiten in den Abschnitten darunter). An dieser Version ist gegenüber `.9` inhaltlich nichts neu.

### Hinzugefügt

- **Fronius Gen24 wird gesteuert, nicht nur angezeigt.** Der Treiber bietet die vollständige Fahrplan-Steuerschnittstelle und ist im Einrichtungsassistenten auswählbar. Mit Sicherheitsnetz: Ladesperre und Entladung laufen mit der Fronius-Rückfallzeit (`InOutWRte_RvrtTms`, 5 Minuten) und werden im Minutentakt aufgefrischt — fällt Home Assistant mitten in einem Slot aus, kehrt der Wechselrichter selbst zu seiner Batteriesteuerung zurück. Skalierungsfaktoren werden vom Gerät gelesen statt angenommen, und der geräteeigene Mindest-Ladestand (`MinRsvPct`) fließt als Untergrenze in die Planung ein.
- **SolaX Gen4+ wird gesteuert.** Vollständige Steuerschnittstelle, im Assistenten auswählbar.
- **Pause als befristeter Eingriff neben dem Ein/Aus-Schalter.** Setzt die Steuerung für eine wählbare Dauer aus oder „bis Ladestand xx %" — der Fall „Auto kommt um 14 Uhr, Batterie soll bis dahin voll sein". Läuft von selbst ab, überlebt einen Neustart, auch als Services `eeg_energy_optimizer.pause` und `.aufheben` für Automationen.
- **OeMAG-Einspeisetarif für den laufenden Monat hochgerechnet.** Neue Quelle der Standardvergütung neben dem zuletzt veröffentlichten Monat. Rechnet den Monat so nach, wie die OeMAG ihn am Monatsende festlegt: Day-Ahead-Stundenpreise, gewichtet mit der österreichischen PV-Erzeugung, begrenzt auf 60–100 % des Quartalspreises der E-Control, abzüglich Ausgleichsenergie. Trifft den veröffentlichten Wert im Rückblick über 20 Monate im Mittel auf 0,21 ct.
- **„Was deine PV bringt" führt zum Sensorverlauf.** Beträge und kWh-Zeilen öffnen per Klick den jeweiligen Sensor. Ein negativer Optimierungs-Vorteil wird begründet, aufgeschlüsselt nach Netzbezug, Einspeiseerlös, Restenergie und Batterienutzung.

### Geändert

- **Optimierungs-Vorteil fairer gerechnet.** Fünf Schieflagen zugunsten der Vergleichsrechnung sind raus: der Bilanztag läuft von 04:00 bis 04:00, damit ein Abend samt Nacht-Entladung in einem Tag bleibt; Tage ohne Eingriff zeigen 0,00 € statt Modellrauschen; die Restenergie in der Batterie wird mit Wandlungsverlust und Alterung bewertet statt zum vollen Basistarif; und der simulierte Standardbetrieb zahlt jetzt dieselben Innenwiderstandsverluste und hält denselben Maximum-Ladestand ein wie der Fahrplan.

### Behoben

- **Ladelimit kam nach der Einspeisegrenzen-Regelung viel zu langsam auf den Fahrplanwert zurück** (rund 7 statt 27 Läufe).
- **SolaX: Entladung blieb am geräteeigenen Entladeboden stehen**, ohne das zu melden. Der Treiber senkt den Wert für die Dauer der Entladung ab und schreibt ihn danach zurück.
- **Panel:** kein „null" mehr in der Transparenz-Ansicht bei Modbus-Treibern; der Anteil einer abgewählten zweiten Gemeinschaft wird nicht mehr mitgezählt.

## [2.0.3-devfronius.9] - 2026-09-07

### Behoben

- **Neuauflage von .6 bis .8 unter einer Versionsnummer.** Der Release `v2.0.3-devfronius.8` war fehlerhaft veröffentlicht: das Tag entstand über die GitHub-Oberfläche und landete dadurch auf dem Standard-Branch `main` (Stand 2.0.2) statt auf dem Entwicklungsstand. HACS installierte damit die alte 2.0.2 — die zweite OeMAG-Quelle und die faire Vorteilsrechnung fehlten. Inhaltlich neu ist an dieser Version nichts, sie trägt nur den Stand aus .6 bis .8 mit einem korrekt gesetzten Tag aus.

## [2.0.3-devfronius.8] - 2026-09-07

### Geändert

- **Referenz „Standardbetrieb“ mit derselben Physik wie der Fahrplan.** Der simulierte Betrieb ohne Vorausschau, gegen den Gewinnkarte und Tagesbilanz den Optimierungs-Vorteil messen, war in zwei Punkten bevorteilt: Er lud ohne Innenwiderstandsverluste (der Fahrplan zahlt über 0,1 C 4 %, über 0,2 C 8 % der Leistung darüber und lädt deshalb gern langsam) und ohne den eingestellten Maximum-Ladestand (der eine Vorgabe des Nutzers ist, keine Entscheidung des Fahrplans). Beides gilt jetzt auch für die Referenz. Der ausgewiesene Vorteil steigt dadurch je nach Anlage um einige Cent pro Tag — nicht, weil der Fahrplan besser wurde, sondern weil der Vergleich fair ist.

## [2.0.3-devfronius.7] - 2026-09-07

### Geändert

- **Optimierungs-Vorteil („davon durch die Optimierung“) fairer gerechnet.** Drei Schieflagen zugunsten der Referenz sind raus:
  - *Bilanztag 04:00 bis 04:00.* Bisher endete der Tag um Mitternacht und zerschnitt die Nacht-Entladung: zurückgehaltene Energie stand um 23:59 nur als Endbestand zum Basistarif da, der Gemeinschaftserlös fiel auf den Folgetag, wo die Referenz mit demselben Ladestand startete und ihn einfach behielt. Jetzt gehört ein Abend samt Nacht in einen Tag; „heute“ beginnt um 04:00. Vorhandene Aufzeichnungen werden beim Update umsortiert.
  - *Tage ohne Eingriff zeigen 0,00 €.* Hat sich die Batterie wie im Standardbetrieb verhalten (kein Entladen ins Netz, kein gebremstes Laden), gibt es nichts, was der Fahrplan bewirkt hätte — die Karte sagt „kein Eingriff“ statt eines zufälligen Plus oder Minus aus Modellrauschen. Die rohe Differenz bleibt in den Attributen des Sensors sichtbar.
  - *Endbestand mit Wirkungsgrad und Alterung bewertet.* Restenergie in der Batterie zählte zum vollen Basistarif; die Referenz lädt bis 100 % und endet meist voller — sie bekam die Differenz verlustfrei angerechnet. Jetzt gilt Basistarif × 0,95 minus Alterungskosten, in der Gewinnkarte wie in der Tagesbilanz.

## [2.0.3-devfronius.6] - 2026-09-07

### Hinzugefügt

- **OeMAG-Einspeisetarif für den laufenden Monat hochgerechnet.** Die OeMAG veröffentlicht ihren Monatstarif erst am ersten Werktag des Folgemonats — wer ihn als Standardvergütung nutzt, rechnete bisher den ganzen Monat mit dem Vormonat (im September 2026: 8,997 ct statt der absehbaren 10,515 ct). Die neue Quelle „OeMAG-Einspeisetarif (laufender Monat, hochgerechnet)" rechnet den Monat so nach, wie die OeMAG ihn am Monatsende festlegt: Day-Ahead-Stundenpreise (aWATTar), gewichtet mit der österreichischen PV-Erzeugung (Energy-Charts), begrenzt auf 60–100 % des Quartalspreises der E-Control, abzüglich Ausgleichsenergie. Im Rückblick über 20 Monate trifft das den veröffentlichten Wert im Mittel auf 0,21 ct (größte Abweichung 0,57 ct); in der ersten Monatswoche schwankt die Hochrechnung noch um bis zu 1,5 ct. Aktualisiert alle drei Stunden. Ohne Hochrechnung gilt weiter der zuletzt veröffentlichte Monat, ohne den die Handeingabe. Das Panel zeigt Monat, Datenstand, Korridor und Herkunft des Quartalspreises dazu. Die bisherige Quelle heißt jetzt „OeMAG-Einspeisetarif (zuletzt veröffentlichter Monat)" und verhält sich unverändert.

## [2.0.3-devfronius.5] - 2026-08-29

### Geändert

- **Pause bis Ladestand.** Die Pause kann jetzt statt für eine Dauer auch „bis Ladestand xx %" gesetzt werden (50 bis 100 %): Die Steuerung setzt aus, die Batterie lädt in der Wechselrichter-Automatik aus dem PV-Überschuss, und sobald der gemessene Ladestand das Ziel erreicht, übernimmt der Fahrplan wieder — der Fall „Auto kommt um 14 Uhr, Batterie soll bis dahin voll sein". Als Sicherheitsnetz endet auch eine Ladestand-Pause spätestens nach 48 h (trüber Tag, Sensor ausgefallen). Läuft nach einem Neustart weiter und wird dann korrekt beendet. Im Service `eeg_energy_optimizer.pause` heißt das Feld `bis_soc_pct`; `stunden` ist jetzt optional (beides zusammen = was zuerst eintritt).
- **Reserve entfernt.** Der Eingriff „Reserve" (befristet höherer Mindest-Ladestand) ist wieder weg — er hielt nur zurück, lud aber nicht aktiv nach, und genau das wollte man in der Praxis. Die Pause bis Ladestand deckt den Fall ab. Der Service `eeg_energy_optimizer.reserve` entfällt; eine noch gespeicherte Reserve wird beim Update still verworfen.
- **Kürzere Begründung bei negativem Optimierungs-Vorteil.** Höchstens zwei Posten, je ein Satz; das Gewitter-Beispiel aus den Texten ist raus.

## [2.0.3-devfronius.4] - 2026-08-29

### Hinzugefügt

- **Pause und Reserve — befristete Eingriffe neben dem Ein/Aus-Schalter.** *Pause* setzt die Steuerung für eine wählbare Zeit aus (der Wechselrichter läuft in seiner eigenen Automatik); *Reserve* hält für eine wählbare Zeit einen höheren Mindest-Ladestand — der Fahrplan optimiert weiter, entlädt aber nicht darunter. Beides läuft von selbst ab und überlebt einen Neustart. Die Reserve wird in Prozent Ladestand eingegeben, die ungefähren kWh stehen daneben. Auch als Services `eeg_energy_optimizer.pause`, `.reserve` und `.aufheben`, damit Automationen sie auslösen können („Wallbox steckt an → Reserve 50 % für 4 h").
- **Negativer Optimierungs-Vorteil wird begründet.** Unter „davon durch die Optimierung" steht bei einem Minus, woher es kommt — abgeleitet aus den Bestandteilen der Differenz (mehr Netzbezug, weniger Einspeiseerlös, weniger Restenergie, mehr Batterienutzung), mit der typischen Ursache je Posten und dem Hinweis, dass ein laufender Tag nur ein Zwischenstand ist.
- **SolaX Gen4+ wird wieder gesteuert.** Der Treiber bietet die vollständige Fahrplan-Steuerschnittstelle an und ist im Einrichtungsassistenten wieder auswählbar.

### Behoben

- **SolaX: Entladung blieb am Entladeboden des Geräts stehen.** Der Wechselrichter stoppt die Batterieentladung bei `selfuse_discharge_min_soc` — auch mitten in einer befohlenen Zwangsentladung, ohne das zu melden: Der Befehl läuft weiter, die Batterie liefert 0,00 kW. An einer Anlage gemessen: 40 Sekunden Einspeisung, dann 1 h 47 Stillstand, während das Haus 2,6 kW aus dem Netz zog. Der Treiber senkt den Wert jetzt für die Dauer der Entladung ab und schreibt danach den Vorwert zurück; der Fahrplan kennt ihn außerdem als Untergrenze und plant nicht tiefer.

## [2.0.3-devfronius.3] - 2026-08-29

### Behoben

- **Ladelimit kam viel zu langsam auf den Fahrplanwert zurück.** Hatte die Einspeisegrenzen-Regelung das Ladelimit hochgezogen (an der Testanlage bis 14,86 kW bei einem Planwert von 1,49 kW), baute sie den Abstand in festen 0,5-kW-Schritten je 30 Sekunden ab — über 13 Minuten, während ein Fahrplan-Slot nur 15 dauert. Der Planwert wurde so kaum je wirksam. Die Rücknahme halbiert jetzt den Abstand je Lauf (rund 7 Läufe statt 27); das Anheben tastet sich weiter vorsichtig heran, weil dort die richtige Höhe unbekannt ist.
- **„null" in der Transparenz-Ansicht.** Unter jedem Label stand „null", wenn der Wechselrichter direkt über Modbus gestellt wird (Fronius) — dort gibt es keine Entität, deren ID die Zeile anzeigen könnte. Außerdem werden kW-Werte in beiden Spalten mit zwei Nachkommastellen dargestellt.
- **Anteil einer abgewählten Gemeinschaft wurde mitgezählt.** Wer als zweite Gemeinschaft „keine" wählte, bekam beim Speichern trotzdem „Anteile zusammen über 100 %" — der Prozentsatz der zweiten Gemeinschaft blieb im Formular stehen und wurde mitgerechnet, obwohl er ohne Gemeinschaft nirgends wirkt.

### Hinzugefügt

- **Beträge in „Was deine PV bringt" führen zum Sensorverlauf.** Die drei Zeitraum-Beträge, die „davon durch die Optimierung"-Zeile und die kWh-Zeilen der Aufschlüsselung öffnen per Klick den jeweiligen Sensor. Monats- und Jahreswert erscheinen erst, sobald ein Tag abgeschlossen ist — vorher sind sie zwangsläufig identisch mit „heute" und sahen wie ein Fehler aus.

## [2.0.3-devfronius.2] - 2026-08-29

### Hinzugefügt

- **Aufschlüsselung unter „Was deine PV bringt" führt zum Sensorverlauf.** Die kWh-Zeilen (Erzeugt, Eingespeist, Netzbezug, Selbst verbraucht) öffnen per Klick den jeweiligen Sensor mit seinem Verlauf — bisher standen dort Zahlen ohne Weg dahin. Die beiden Geldzeilen bleiben bewusst stumm: hinter ihnen steht kein Sensor, sondern eine Rechnung.

## [2.0.3-devfronius.1] - 2026-08-29

### Hinzugefügt

- **Fronius: Sicherheitsnetz gegen eingefrorene Batterie.** Ladesperre und Entladung werden jetzt mit der Fronius-Rückfallzeit (`InOutWRte_RvrtTms`, 5 Minuten) scharfgeschaltet und im Minutentakt aufgefrischt. Fällt Home Assistant mitten in einem Fahrplan-Slot aus, beendet der Wechselrichter den erzwungenen Betrieb selbst und kehrt zu seiner eigenen Batteriesteuerung zurück — bisher blieb die Batterie blockiert, bis jemand eingriff. Damit haben Fronius, Kostal und SMA dasselbe Failsafe-Verhalten. Hinweis: Ein zweites Programm, das denselben Wechselrichter über Modbus abfragt, hält die Rückfallzeit mit am Leben.

- **Fronius: Mindest-Ladestand des Geräts fließt in den Fahrplan ein.** `MinRsvPct` wird jetzt als Untergrenze zurückgemeldet (wie der Notstrom-Ladestand bei Huawei) — sonst plant der Fahrplan Entladungen, die der Wechselrichter verweigert, und Plan und Ist laufen dauerhaft auseinander.
- **Fronius Gen24 wird jetzt gesteuert, nicht nur angezeigt.** Der Treiber bietet die vollständige Fahrplan-Steuerschnittstelle an (Ladelimit lesen für Guard 1, Hardware-Obergrenzen für beide Guards, Stellgrößen für die Transparenz-Ansicht) und ist im Einrichtungsassistenten wieder auswählbar — gemeinsam mit Huawei. Die übrigen Wechselrichter bleiben ausgeblendet.

### Geändert

- **Fronius: Skalierungsfaktoren werden vom Gerät gelesen** statt fest angenommen (`WChaMax_SF`, `MinRsvPct_SF`, `InOutWRte_SF`). Auf Geräten, die Leistungen nicht in ganzen Watt melden, waren Lade- und Entladeleistung bisher um den Faktor 10 oder 100 daneben. Unplausible oder fehlende Werte fallen auf die bisherigen SunSpec-Vorgaben zurück.

## [2.0.2] - 2026-08-28

### Hinzugefügt

- **Sechs neue Sensoren: was die PV in Geld bringt.** *Ersparnis durch PV*
  (vermiedener Netzbezug plus Einspeiseerlös) und *Ersparnis durch
  Optimierung* (Vergleich mit einem simulierten Standardbetrieb), jeweils für
  heute, diesen Monat und dieses Jahr. Dazu die Dashboard-Karte
  **„Was deine PV bringt"**.
  > Der Anteil der Optimierung ist in der PV-Ersparnis **enthalten** und darf
  > nicht dazugezählt werden — die Karte weist ihn deshalb als „davon"-Zeile
  > aus.

  Grundlage ist das neue Modul `bilanz.py`: Es zeichnet 96 Viertelstunden je
  Tag auf und friert dabei die Preise ein, die zu dieser Viertelstunde galten.
  Bewertet wird mit derselben Funktion, die auch den Fahrplan bewertet.
  Der Anteil, der zum Satz der Energiegemeinschaft vergütet wird, beruht auf
  deren Bedarfsprognose — Sensor und Karte weisen das aus, endgültig steht er
  erst mit der EEG-Abrechnung fest.

  **Die Aufzeichnung beginnt bei null:** Monat und Jahr füllen sich erst mit
  der Zeit, es gibt keine Rückrechnung aus der Datenbank.

### Behoben

- **Im Einrichtungsassistenten war der OeMAG-Tarif nicht abrufbar** („Kein
  Tarif gelesen (Anbieter nicht geladen)", auch nach Klick auf *Jetzt holen*).
  Die Preis-Anbieter für OeMAG und Spotpreis wurden erst nach abgeschlossener
  Einrichtung geladen — der Assistent lässt die Standardvergütung aber schon
  vorher auswählen, und OeMAG ist die Vorgabe. Beide werden jetzt geladen,
  bevor die Einrichtung abgeschlossen ist; sie hängen an keiner Anlage und an
  keinem Sensor.

## [2.0.1] - 2026-08-28

### Behoben

- **Das Nachtfenster fehlte, obwohl ein Nachtsatz eingetragen war.** Es wurde
  erst eingeblendet, wenn die Oberfläche aus anderem Anlass neu gezeichnet
  wurde — etwa beim Umschalten des Expertenmodus. Beide Nachtfenster (Standard­
  vergütung und Gemeinschaften) stehen jetzt dauerhaft da; ein Pflicht-Stern
  erscheint nur, wenn ein Nachtsatz eingetragen ist. Bei den Quellen OeMAG und
  Spotpreis bleibt das Fenster der Standardvergütung wie bisher ausgeblendet,
  weil dort kein eigener Nachtsatz wirkt.
- **Der „Weiter"-Knopf im Einrichtungsassistenten blieb nach der letzten
  Pflichteingabe gesperrt** (z. B. nach der PV-Spitzenleistung). Sein Zustand
  entstand nur beim Neuzeichnen, und Zahlenfelder zeichnen bewusst nicht neu.
  Er wird jetzt bei jeder Eingabe nachgezogen — auch bei den Sensorfeldern.

## [2.0.0] - 2026-08-28

Erste 2.0: Die Steuerung ist gegenüber der 1.x-Reihe **vollständig ersetzt**.
Statt fester Zustände („Morgen-Einspeisung", „Nacht-Entladung") und Zeitfenster
plant ein linearer Optimierer jede Minute einen 48-Stunden-Fahrplan aus dem
Preis je Viertelstunde. Der Fahrplan ist der einzige Aktor.

### Hinzugefügt

- **Fahrplan-Optimierung über 48 Stunden** im 15-Minuten-Raster — auf Basis des
  gelernten Verbrauchsprofils, des Batteriezustands, der PV-Prognose (bei
  Solcast inklusive p10-Worst-Case-Pfad) und der konfigurierten Preise.
- **Steuerung über Preise statt über Zeitfenster.** Die Einspeisevergütung ist
  eine Zeitreihe aus Basistarif plus Auf- bzw. Abschlag der Energiegemeinschaft:
  Hat die Gemeinschaft in einer Viertelstunde Bedarf, steigt der Preis; hat sie
  Überschuss, sinkt er. Erkennt die Optimierung keinen Mehrwert, passiert
  nichts — eine feste Nachtentladung gibt es nicht mehr.
- **Drei Quellen für den Basistarif**: fester Wert, OeMAG-Monatstarif oder
  Börsen-Spotpreis (aWATTar AT/DE), jeweils mit eigenem Nachtsatz und
  Nachtfenster; für die Energiegemeinschaften getrennt davon ein eigenes
  Nachtfenster.
- **Optimierungsgewinn (48 h)** — eigene Karte im Dashboard: Was bringt der
  Fahrplan gegenüber dem Standardbetrieb des Wechselrichters, bewertet an den
  echten Geldflüssen inklusive Batteriealterung und Endbestand.
- **Maximum-Ladestand** — Obergrenze der Planung (Vorgabe 100 = bis voll laden).
- **Fahrplan-Steuerung mit Nachführung**: Ladelimit-Nachführung bei stiller
  Abregelung an der Einspeisegrenze, Entlade-Nachführung auf die gemessene
  Hauslast, Not-Aus bei Netzbezug während der Entladung, Failsafe nach
  15 Minuten ohne brauchbaren Fahrplan.

### Geändert

- **Unterstützt wird derzeit ausschließlich Huawei SUN2000.** Die Treiber für
  Fronius, Kostal, SMA, SolarEdge und SolaX sind vollständig erhalten, aber
  stillgelegt: Sie stehen nicht mehr zur Auswahl und werden nicht gesteuert.
  Grund: Die Steuerverifikation der 1.x-Reihe galt der alten Zustandslogik und
  überträgt sich nicht auf die minütliche Nachführung. Sie werden Schritt für
  Schritt wieder freigeschaltet — Stand, offene Punkte je Treiber und der
  Freischaltweg stehen in `docs/wechselrichter-status.md`. Ein bereits
  konfigurierter Fremdtreiber bleibt im Assistenten sichtbar.
- **Modus „Test" heißt jetzt „Aus".** Beim Umschalten auf Aus werden gesetzte
  Steuerwerte sofort zurückgenommen — der Wechselrichter läuft wieder in seinem
  Automatikmodus, es bleibt kein Ladelimit stehen.
- **Nachtfenster-Vorgabe 20:00–06:00** statt 22:00 — es deckt damit die
  Abendspitze der Gemeinschaften mit ab.
- **Neue Vorgaben für die Ersteinrichtung** (bestehende Anlagen bleiben
  unverändert): Standardvergütung vom OeMAG-Monatstarif, Bezugspreis 26 ct,
  Vermarkter-Abschlag beim Spotpreis 2 ct, beide Energiegemeinschaften mit je
  50 % vorbelegt.
- **Dokumentation durchgängig auf die Preissteuerung umgestellt** — README,
  Doku-Startseite, Steuerungs-Beschreibung, Inbetriebnahme und
  Installationsanleitung. Neu: `docs/wechselrichter-status.md` als einzige
  Wahrheitsquelle für den Stand der Wechselrichter-Unterstützung.

### Entfernt

- Die zustandsbasierte Optimierung der 1.x-Reihe samt Morgen-Einspeisung,
  Nacht-Entladung, Einspeisebegrenzung als Zustand und den zugehörigen
  Zeitfenster-Einstellungen.
- Die Alterungskosten der Batterie sind nicht mehr einstellbar; sie wirken
  unverändert mit 1 ct/kWh.
