# Dashboard und Bedienung

Nach der Einrichtung zeigt der Seitenleisten-Eintrag **EEG Energy Optimizer** das Dashboard. Oben steht die Statuskarte mit dem Schalter für den Modus, darunter der Optimierungsplan und die Karten, die zeigen, was die Anlage bringt. Die meisten Karten lassen sich auf- und zuklappen; ein Tipp auf einen Messwert öffnet in der Regel den Verlauf des zugehörigen Sensors.

## Modus Ein und Aus

Der Schalter oben rechts in der Statuskarte wählt, ob der Optimizer steuert.

| Modus | Was passiert |
|---|---|
| **Ein** | Der Fahrplan wird jede Minute neu gerechnet und alle 30 Sekunden am Wechselrichter durchgesetzt: Ladeleistung begrenzen, gezielt ins Netz entladen oder den Wechselrichter in seiner eigenen Automatik laufen lassen |
| **Aus** | Der Fahrplan wird weiter gerechnet und angezeigt, aber es wird nichts an den Wechselrichter geschrieben |

Beim Umschalten auf **Aus** nimmt der Optimizer sofort alle gesetzten Steuerwerte zurück — ein Ladelimit oder eine laufende Entladung bleibt nicht im Gerät stehen. Der Wechselrichter läuft danach in seiner eigenen Eigenverbrauchs-Automatik. Ein konfigurierter Heizstab geht mit aus.

Der Modus übersteht einen Neustart; eine neue Installation beginnt mit **Aus**. Für Automationen ist der Schalter die Entität `select.eeg_energy_optimizer_optimizer` mit den Optionen `Ein` und `Aus`.

_Nach einem Neustart im Modus Ein schreibt der Optimizer 90 Sekunden lang nichts, bis die Entitäten des Wechselrichters geladen sind. Die Statuskarte zeigt dann nur „Startphase — noch keine Steuerbefehle"._

## Pause

Die Taste **Pause** neben dem Schalter setzt die Steuerung befristet aus — wie Modus Aus, aber mit einem Ende. Der Wechselrichter läuft in seiner Automatik, die Batterie lädt also wie gewohnt aus dem PV-Überschuss. Der typische Fall: Das Auto lädt später, und die Batterie soll bis dahin voll werden, statt in die Gemeinschaft zu entladen.

Im Dialog wählst du eine der beiden Arten:

- **Für eine Dauer** — 0,25 bis 48 Stunden, mit Schnellwahl 2, 4, 8, 12 und 24 h.
- **Bis Ladestand** — 50 bis 100 %. Die Pause endet, sobald die Batterie diesen Ladestand erreicht hat, spätestens aber nach 48 Stunden.

Solange die Pause läuft, steht oben in der Statuskarte ein oranger Hinweis mit dem Ende und der Taste **aufheben**, die sie sofort beendet. Danach übernimmt der Fahrplan von selbst wieder. Die Pause wird gespeichert und übersteht einen Neustart von Home Assistant — ein Neustart mitten in der Pause wirft die Steuerung nicht wieder an.

### Pause aus einer Automation

Dafür gibt es zwei Dienste:

| Dienst | Felder |
|---|---|
| `eeg_energy_optimizer.pause` | `stunden` (0,25–48) und/oder `bis_soc_pct` (50–100). Mindestens eines angeben; mit beiden endet die Pause mit dem, was zuerst eintritt. Ohne `stunden` gilt die Obergrenze von 48 h. Eine laufende Pause wird ersetzt |
| `eeg_energy_optimizer.aufheben` | keine — beendet die laufende Pause |

Beide kennen zusätzlich `entry_id`, das nur nötig ist, wenn mehrere EEG Energy Optimizer eingerichtet sind.

```yaml
action: eeg_energy_optimizer.pause
data:
  bis_soc_pct: 90
  stunden: 6
```

## Die Statuskarte

**Werte.** PV, Batterie (mit „Ladung" oder „Entladung"), Ladestand (SOC), Netz (mit „Einspeisung" oder „Bezug") und Haus — mit Heizstab auch dessen Leistung und Wassertemperatur. Alle Leistungen stammen aus **einem** Zeitpunkt: Sie werden gemeinsam gelesen, damit sich die Bilanz in der Karte aufrechnen lässt. Ein Stern hinter „Haus" heißt, dass die Bilanz gerade nicht aufging und der Hausverbrauch auf 0 kW begrenzt wurde — diese Null ist eine Grenze, kein Messwert. Über die zwei Symbole neben dem Titel wechselst du zwischen Werte-Anzeige und Energieflussdiagramm.

**Zustand.** Darunter steht, was die Steuerung gerade tut. Das Zeichen davor sagt, ob es wirkt: ● = wird am Wechselrichter gesetzt, ○ = wird nur gerechnet, — = dieser Wechselrichter wird gar nicht gesteuert.

| Zustand | Bedeutung |
|---|---|
| **Laden begrenzt auf x kW** | Der Plan will den Überschuss im Netz statt in der Batterie und begrenzt die Ladeleistung. Klebt die gemessene Einspeisung an der Einspeisegrenze, wird das Limit schrittweise angehoben, damit nichts abgeregelt wird |
| **Laden blockiert** | Das Ladelimit steht auf 0 kW |
| **Einspeisung x kW bis y %** | Gezielte Entladung ins Netz. Die Zahl ist die geplante Einspeisung, der Ziel-Ladestand kommt aus dem Plan. Der Batterie-Sollwert in der Zeile darunter ist höher, weil er den Hausverbrauch mit abdeckt |
| **Normalbetrieb** | Kein Eingriff, der Wechselrichter regelt selbst |
| **Anzeige-Modus** | Modus Aus oder eine laufende Pause — es wird nur gerechnet |
| **Nur Anzeige** | Der Wechselrichter wird nicht gesteuert, der Plan ist nur Anzeige |

Die Zeilen darunter zeigen, was am Wechselrichter steht (Batterie-Sollwert, Ziel-Ladestand oder Ladelimit, dazu der laufende Slot), und den Klartext des letzten Laufs. Dort steht zum Beispiel „Aus — es wird nicht gesteuert", „Pause bis 14:30 — der Wechselrichter läuft im Automatikmodus" oder „Treiber wird nicht gesteuert — Plan nur Anzeige".

Kommt eine befohlene Entladung nicht an — die Batterie gibt über drei Minuten weniger als die Hälfte des Sollwerts ab und mehr als 0,3 kW zu wenig —, setzt der Optimizer den Befehl neu auf und meldet „Guard 3: Entladung wirkungslos (x statt y kW) — neu aufgesetzt". Das geschieht höchstens zweimal je Viertelstunde und nie, wenn die Batterie ihren Ziel-Ladestand erreicht hat.

**Bezugsspitze.** Die Zeile „Bezugsspitze *Monat*" zeigt den höchsten Viertelstunden-Netzbezug des laufenden Monats (siehe unten). Das ⓘ daneben nennt den Zeitpunkt, die laufende und die letzte abgeschlossene Viertelstunde und die Vormonate. Orange wird der Wert, wenn die Hochrechnung der laufenden Viertelstunde über der bisherigen Monatsspitze liegt — dann entsteht gerade eine neue.

**Zeitstempel.** Ganz unten stehen die letzten Läufe von Plan, Steuerung, Verbrauchsprofil und, mit PeakShare, dem Abruf der Bedarfsprognose. Rot mit ⚠ heißt: Dieser Teil läuft nicht im erwarteten Takt oder der Plan konnte nicht gerechnet werden; ein Tipp darauf nennt den Grund.

## Optimierungsplan

Die Karte zeigt den Fahrplan als Diagramm: PV- und Verbrauchsprognose, die geplante Netzleistung, Laden und Entladen der Batterie als Balken, darunter den geplanten Ladestand mit dem Mindest-Ladestand als rote Linie. Je nach Einrichtung kommen der Einspeisepreis, der Bedarf der Energiegemeinschaften, die geplante Heizstab-Leistung und die voraussichtliche Puffertemperatur dazu. Jeder Eintrag der Legende blendet seine Kurve aus und wieder ein.

- **Plan** wählt den gezeigten Ausschnitt: 24, 36 oder 48 Stunden. Gerechnet wird immer über den vollen Horizont.
- **Verlauf** legt die Messung dünn und blass daneben: aus, die letzten 12 Stunden oder ab gestern.
- **Neu rechnen** rechnet den Plan sofort, statt auf die nächste Minute zu warten.

Fährst du mit der Maus über das Diagramm oder tippst darauf, zeigt ein Fenster die Werte dieser Viertelstunde — geplant und, für die Vergangenheit, gemessen.

### Optimierungsgewinn

Die Karte darunter vergleicht den Plan mit einem simulierten **Standardbetrieb**: PV-Überschuss lädt zuerst die Batterie, ein Defizit entlädt sie bis zum Mindest-Ladestand. Die Zahl im Kopf ist der erwartete Mehrerlös über die nächsten Stunden — auf Prognosebasis, kein Messwert. Aufgeklappt stehen die Geldposten beider Betriebsarten nebeneinander und ein Diagramm, wie die Einspeisung ohne Optimierung aussähe.

### Gesetzte Steuerwerte (Expertenmodus)

Mit eingeschaltetem Expertenmodus zeigt diese Karte je Stellgröße, was **im Gerät** steht und was der Optimizer **gesetzt** hat. Weichen beide deutlich ab, ist der Wert orange — dann hat jemand anderes gestellt oder ein Schreibbefehl kam nicht an. Hat der Optimizer nichts gesetzt, wird beim Ladelimit der Standardwert des Geräts erwartet. Die Karte zieht mit jedem Steuerungslauf nach und fehlt in der Startphase.

## Was deine PV bringt

Die Karte zeigt die **Ersparnis durch PV** für heute, diesen Monat und dieses Jahr: nicht gekaufter Strom plus Einspeiseerlös, mit Heizstab auch die Wärme. Das ist eine **Messung** — jede Kilowattstunde ist gemessen, die Preise werden je Viertelstunde festgehalten, wie sie zu diesem Zeitpunkt galten. „Woraus setzt sich das zusammen?" zeigt die Posten des Tages. Wie viel der Einspeisung zum Satz der Energiegemeinschaft zählt, beruht auf deren Bedarfsprognose; endgültig steht es erst mit der EEG-Abrechnung fest.

Daneben gibt es die Sensoren **Ersparnis durch Optimierung** heute, diesen Monat und dieses Jahr. Sie sind eine **Modellrechnung**: der Unterschied zu einem simulierten Standardbetrieb über die gemessenen PV- und Verbrauchswerte des Tages. Einen Betrieb, den es nicht gegeben hat, kann man nicht messen.

> [!IMPORTANT]
> **Die beiden Werte nie addieren.** Der Vorteil der Optimierung ist bereits in der Ersparnis durch PV enthalten — er ist der Teil davon, der aus der Steuerung stammt (Attribut `davon_optimierung` am Sensor „Ersparnis durch PV heute"). Wer beide zusammenzählt, zählt ihn doppelt.

**Kein Eingriff.** Lief die Batterie an einem Tag praktisch wie im Standardbetrieb — Abweichung höchstens 1 kWh oder 10 % des Durchsatzes —, zeigt die Ersparnis durch Optimierung 0 und das Attribut `kein_eingriff` ist wahr. Was die Rechnung dann trotzdem an Differenz ergäbe, ist Rauschen zwischen Messung und Simulation; es bleibt als `vorteil_roh` nachlesbar. Im Modus Aus sollte der Wert deshalb gegen null gehen.

**Negative Tage.** Ein Tag kann schlechter ausfallen als der Standardbetrieb. Die Gründe stehen im Attribut `begruendung`, der größte Posten zuerst — meist ungeplanter Verbrauch wie ein Elektroauto, für den der Fahrplan Energie zurückhielt, eine Gemeinschaft, die weniger brauchte als vorhergesagt, oder Energie, die am Tagesende nicht mehr in der Batterie war. Der Vergleich ist streng: Die Referenz kennt den echten Verbrauch, der Fahrplan nur die Prognose. Solange der Tag läuft, ist jeder Wert ein Zwischenstand.

_Ein **Bilanztag** läuft von 04:00 bis 04:00, damit ein Abend samt Nacht-Entladung in einem Tag bleibt. „Heute" beginnt deshalb um 04:00, und um 02:00 am Monatsersten zählt noch der Vormonat._

## Bezugsspitze und Netzbezug je Viertelstunde

Ab 2027 richtet sich der Leistungspreis des Netzentgelts nach dem höchsten Viertelstunden-Mittel des Netzbezugs im Monat. Der Optimizer misst diese Größe mit — er steuert nichts danach.

- **Netzbezug Viertelstunde** — mittlerer Bezug der zuletzt **abgeschlossenen** Viertelstunde (:00, :15, :30, :45). Einspeisung zählt nicht und verrechnet sich nicht mit dem Bezug derselben Viertelstunde. Der Wert ist mit dem Smart-Meter-Portal des Netzbetreibers vergleichbar; gemessen wird allerdings am Netzsensor des Wechselrichters, nicht am Zähler.
- **Bezugsspitze Monat** — die höchste dieser Viertelstunden im laufenden Kalendermonat, mit Zeitpunkt und den Vormonaten. Die Mindestbemessung von 2 kW ist **nicht** eingerechnet: Sie ist eine Regel der Abrechnung, keine Messung.
- **Netzkosten** — das ⓘ neben der Bezugsspitze zeigt, was sie als Leistungspreis im Monat kosten würde: höchstens mit den Sätzen im Endausbau (33,82 €/kW im Jahr bis 10 kW, 67,64 €/kW darüber), zum Start 2027 mit rund 19 €/kW im Jahr, jeweils mit mindestens 2 kW und inklusive Umsatzsteuer. Das sind Richtwerte der E-Control vom Juli 2026; die Tarife je Netzbereich kommen erst mit der Tarifverordnung Ende 2026.

_Hatte eine Viertelstunde Messlücken, ist ihr Wert eine Untergrenze. Sie zählt trotzdem zur Monatsspitze und ist als unvollständig markiert._

## Die übrigen Karten

- **Energieprognose (7 Tage)** — erwarteter Verbrauch und PV-Ertrag je Tag. Mit eingeschaltetem Prognosevergleich kommt je Tag ein Balken für die mitlaufende Quelle dazu.
- **Prognosevergleich** — nur mit eingeschaltetem Vergleich: Fremdprognose, eigene Berechnung und Messung nebeneinander. Mehr dazu in der Anleitung „Eigene PV-Prognose".
- **Einspeise-Statistik** — wie viel Energie bei gesteuerten Entladungen aus der Batterie ins Netz ging („Entladung ins Netz"), mit Anzahl und Dauer, für Woche, Monat, Jahr oder gesamt.
- **Verbrauchsprofil** — der gelernte Verbrauch, umschaltbar zwischen Stundenverlauf und Tag / Nacht.
- **Energiebedarf** der Gemeinschaft — nur mit PeakShare als Bedarfsquelle: Bedarf und Überschuss je Stunde aus der PeakShare-Prognose. Die Karte ist Anzeige; gesteuert wird nach dem Optimierungsplan.
- **Aktivitätsprotokoll** — jeder Zustandswechsel der Steuerung mit Zeit und Klartext, filterbar nach Laden, Entladung und Normalbetrieb. „Alle Einträge" zeigt zusätzlich das stündliche Lebenszeichen, „Mehr laden" holt ältere Einträge.

## Wenn etwas nicht stimmt

**Failsafe.** Gibt es 15 Minuten lang keinen brauchbaren Plan, gibt der Optimizer den Wechselrichter frei. Die Statuskarte zeigt rot „Failsafe aktiv — kein brauchbarer Optimierungsplan, der Wechselrichter läuft im Automatikmodus." Fehlt der Plan nur kurz, bleibt der letzte Zustand stehen („Plan fehlt kurzzeitig — letzter Zustand bleibt"). Den Grund nennt der Optimierungsplan („Noch kein Optimierungsplan: …") und der rote Zeitstempel „Plan". Meist fehlt eine Eingabe — ein Ladestand-Sensor, der nicht antwortet, oder eine Prognose ohne Daten. Ist die Ursache behoben, übernimmt der Plan von selbst; mit **Jetzt rechnen** geht es sofort.

**Not-Aus.** Bezieht das Haus während einer Entladung in drei Läufen hintereinander (rund eineinhalb Minuten) mehr als 1 kW aus dem Netz, stoppt der Optimizer die Entladung: „Not-Aus: anhaltender Netzbezug während der Entladung — gesperrt bis zum nächsten Slot." Sonst würde Strom gekauft, um Batteriestrom billiger zu verkaufen. Mit der nächsten Viertelstunde ist die Sperre von selbst aufgehoben. Kommt das oft vor, lohnt ein Blick in die Statuskarte: Zeigt sie beim Einspeisen einen positiven Netzwert und beim Bezug einen negativen? Ist es umgekehrt, stimmt die Zuordnung des Netzsensors nicht — dann den Assistenten erneut durchlaufen.

**Treiber wird nicht gesteuert.** Der Zustand „Nur Anzeige" und der blaue Hinweis „Dieser Wechselrichter wird nicht gesteuert" bedeuten, dass für dein Gerät keine Steuerung freigegeben ist. Bei den sechs unterstützten Wechselrichtern kommt das nicht vor — siehe „Stand der Unterstützung“ in der Doku. Der Plan wird trotzdem gerechnet und angezeigt; am Wechselrichter ändert sich nichts, auch nicht im Modus Ein.

> [!NOTE]
> **Ein fehlgeschlagener Steuerbefehl wird nicht in jedem Lauf wiederholt.** Die Statuskarte meldet ihn orange als „Letzter Steuerbefehl fehlgeschlagen", und der Optimizer wartet zwischen zwei Versuchen immer länger. Hält das an, zeigt die Karte **Gesetzte Steuerwerte** im Expertenmodus, was wirklich im Gerät steht.
