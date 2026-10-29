# Dashboard und Bedienung

Der Seitenleisten-Eintrag **EEG Energy Optimizer** öffnet das Dashboard. Von oben nach unten: Statuskarte mit dem Modus-Schalter, Optimierungsplan, „Was deine PV bringt“, „Einspeisung“, „Bezug“, Optimierungsgewinn, (mit eingeschaltetem Vergleich) Prognosevergleich, Energieprognose, Verbrauchsprofil und Aktivitätsprotokoll. Die meisten Karten lassen sich auf- und zuklappen. Ein Tipp auf einen Messwert öffnet meist den Verlauf des Sensors.

Karten, die du nicht brauchst, blendest du unter Einstellungen → System → **Dashboard** aus. Das gilt nur für dieses Gerät und diesen Browser, wirkt sofort und braucht kein Speichern; gerechnet und gesteuert wird weiter. Die Statuskarte lässt sich nicht ausblenden.

## Modus Ein und Aus

Der Schalter oben rechts in der Statuskarte legt fest, ob der Optimizer steuert.

| Modus | Was passiert |
|---|---|
| **Ein** | Der Fahrplan wird jede Minute neu gerechnet und alle 30 Sekunden am Wechselrichter umgesetzt: Ladeleistung begrenzen, gezielt ins Netz entladen oder den Wechselrichter selbst regeln lassen |
| **Aus** | Der Fahrplan wird gerechnet und angezeigt, aber nichts an den Wechselrichter geschrieben |

Beim Umschalten auf **Aus** nimmt der Optimizer sofort alle Steuerwerte zurück. Der Wechselrichter läuft dann in seiner eigenen Eigenverbrauchs-Automatik. Ein Heizstab geht mit aus.

Der Modus bleibt nach einem Neustart erhalten. Eine neue Installation beginnt mit **Aus**; der (vorausgewählte) Haken „Steuerung nach dem Fertigstellen einschalten“ in der Zusammenfassung des Assistenten stellt auf **Ein**. Für Automationen: Entität `select.eeg_energy_optimizer_optimizer` mit den Optionen `Ein` und `Aus`.

_Nach einem Neustart im Modus Ein wartet der Optimizer 90 Sekunden auf die Entitäten des Wechselrichters; die Statuskarte zeigt dann nur „Startphase — noch keine Steuerbefehle". Danach gibt er den Wechselrichter zuerst frei (alte Steuerwerte werden zurückgenommen) und setzt dann den Plan um. Scheitert die Freigabe, steht dort „Startphase: Freigabe fehlgeschlagen — wird wiederholt"._

## Pause

Die Taste **Pause** neben dem Schalter setzt die Steuerung befristet aus — wie Modus Aus, aber mit Ende. Die Batterie lädt dann wie gewohnt aus dem PV-Überschuss. Typischer Fall: Das Auto lädt später, und die Batterie soll bis dahin voll werden.

Im Dialog wählst du:

- **Für eine Dauer** — 0,25 bis 48 Stunden, Schnellwahl 2, 4, 8, 12 und 24 h.
- **Bis Ladestand** — 50 bis 100 %. Endet, sobald die Batterie diesen Ladestand erreicht, spätestens nach 48 Stunden.

Während der Pause zeigt die Statuskarte oben einen orangen Hinweis mit dem Ende und der Taste **aufheben**. Danach übernimmt der Fahrplan von selbst. Die Pause bleibt auch nach einem Neustart von Home Assistant bestehen.

### Pause aus einer Automation

| Dienst | Felder |
|---|---|
| `eeg_energy_optimizer.pause` | `stunden` (0,25–48) und/oder `bis_soc_pct` (50–100). Mindestens eines angeben; mit beiden endet die Pause mit dem, was zuerst eintritt. Ohne `stunden` gilt die Obergrenze von 48 h. Eine laufende Pause wird ersetzt |
| `eeg_energy_optimizer.aufheben` | keine — beendet die laufende Pause |

Beide kennen zusätzlich `entry_id`; das brauchst du nur bei mehreren eingerichteten EEG Energy Optimizern.

```yaml
action: eeg_energy_optimizer.pause
data:
  bis_soc_pct: 90
  stunden: 6
```

## Die Statuskarte

**Werte.** PV, Batterie („Ladung“ oder „Entladung“), Ladestand (SOC), Netz („Einspeisung“ oder „Bezug“) und Haus, mit Heizstab auch dessen Leistung und Wassertemperatur. Alle Leistungen stammen aus **einem** Zeitpunkt, damit die Bilanz aufgeht. Ein Stern hinter „Haus“ heißt: Die Bilanz ging gerade nicht auf, der Hausverbrauch wurde auf 0 kW begrenzt — diese Null ist kein Messwert. Mit den zwei Symbolen neben dem Titel wechselst du zwischen Werten und Energieflussdiagramm.

**Energieflussdiagramm.** PV oben, Netz links, Haus rechts, Batterie unten. Wandernde Punkte zeigen, wohin Strom fließt (je mehr Leistung, desto schneller); die Werte je Leitung stehen unter dem Diagramm. Oben links: Produktion, Netzbezug und Einspeisung des Tages in kWh, gezählt ab 04:00. Der Ring ums Haus zeigt, woher der Verbrauch kommt (gelb PV, orange Batterie, rot Netz), der Ring um die Batterie ihren Ladestand. Mit „Bewegung reduzieren“ im System siehst du Linien statt Punkte.

**Zustand.** Darunter steht, was die Steuerung gerade tut. Das Zeichen davor: ● = wird am Wechselrichter gesetzt, ○ = wird nur gerechnet, — = dieser Wechselrichter wird nicht gesteuert.

| Zustand | Bedeutung |
|---|---|
| **Laden begrenzt auf x kW** | Der Plan will den Überschuss im Netz statt in der Batterie und begrenzt die Ladeleistung. Klebt die gemessene Einspeisung an der Einspeisegrenze, wird das Limit schrittweise angehoben, damit nichts abgeregelt wird |
| **Laden blockiert** | Das Ladelimit steht auf 0 kW |
| **Einspeisung x kW bis y %** | Gezielte Entladung ins Netz. Die Zahl ist die geplante Einspeisung, der Ziel-Ladestand kommt aus dem Plan. Der Batterie-Sollwert in der Zeile darunter ist höher, weil er den Hausverbrauch mit abdeckt |
| **Netzladung x kW bis y %** | Nur mit „Batterie aus dem Netz laden“ (Beta, Huawei): Der Wechselrichter lädt mit der geplanten Leistung bis zum geplanten Ladestand, PV zuerst, den Rest aus dem Netz |
| **Normalbetrieb** | Kein Eingriff, der Wechselrichter regelt selbst. Der Grund steht in der Zeile darunter, etwa „Normalbetrieb (Plan lädt den ganzen Überschuss)“ — der Plan will alles in die Batterie, das macht die Automatik von selbst —, „Normalbetrieb (kein PV-Überschuss)“ — ohne Überschuss hielte ein Ladelimit nichts frei — oder „Normalbetrieb (Batterie voll)“ |
| **Anzeige-Modus** | Modus Aus oder eine laufende Pause — es wird nur gerechnet |
| **Nur Anzeige** | Der Wechselrichter wird nicht gesteuert, der Plan ist nur Anzeige |

Darunter steht, was am Wechselrichter eingestellt ist (Batterie-Sollwert, Ziel-Ladestand oder Ladelimit, laufender Slot = Viertelstunde), und der Klartext des letzten Laufs, z. B. „Aus — es wird nicht gesteuert", „Pause bis 14:30 — der Wechselrichter läuft im Automatikmodus" oder „Treiber wird nicht gesteuert — Plan nur Anzeige".

Kommt eine befohlene Entladung nicht an — die Batterie (bei SMA: die Einspeisung am Netzzähler) liefert drei Minuten lang weniger als die Hälfte des Sollwerts und mehr als 0,3 kW zu wenig —, setzt der Optimizer den Befehl neu und meldet „Guard 3: Entladung wirkungslos (x statt y kW) — neu aufgesetzt". Höchstens zweimal je Viertelstunde und nie, wenn der Ziel-Ladestand erreicht ist.

**Bezugsspitze.** Die Zeile „Bezugsspitze *Monat*" zeigt den höchsten Viertelstunden-Netzbezug des Monats. Orange heißt: Die Hochrechnung der laufenden Viertelstunde liegt darüber, gerade entsteht eine neue Spitze. Greift die Spitzenkappung, steht ein Hinweis daneben („Batterie hält Reserve", „Batterie deckt nur Spitzen", „Wallbox aus"). Ein Tipp springt zur Karte **Bezugsspitze & Spitzenkappung** (siehe unten).

**Zeitstempel.** Ganz unten: die letzten Läufe von Plan, Steuerung, Verbrauchsprofil und (mit PeakShare) Bedarfsprognose. Rot mit ⚠ heißt: Dieser Teil läuft nicht im Takt oder der Plan konnte nicht gerechnet werden. Ein Tipp darauf nennt den Grund.

## Optimierungsplan

Das Diagramm zeigt den Fahrplan: PV- und Verbrauchsprognose, geplante Netzleistung, Laden und Entladen als Balken, darunter den geplanten Ladestand mit dem Mindest-Ladestand als rote Linie und (mit Spitzenkappung) der Spitzenreserve als orange gestrichelte Linie. Je nach Einrichtung kommen Einspeisepreis, Bedarf der Energiegemeinschaften, geplante Heizstab-Leistung und voraussichtliche Puffertemperatur dazu. Ein Klick auf einen Legenden-Eintrag blendet die Kurve aus und ein.

- **Plan**: gezeigter Ausschnitt, 24, 36 oder 48 Stunden. Gerechnet wird immer der volle Horizont.
- **Verlauf**: Messung blass daneben — aus, letzte 12 Stunden oder ab gestern.
- **Neu rechnen**: rechnet den Plan sofort.

Maus darüber oder Tipp zeigt die Werte der Viertelstunde — geplant und, für die Vergangenheit, gemessen.

**Zoomen.** Zwei Finger auseinanderziehen zoomt hinein, zusammenschieben heraus. Am PC: Trackpad, Strg + Mausrad oder mit gedrückter linker Maustaste einen Bereich markieren. Gezoomt verschiebt waagrechtes Wischen den Ausschnitt (am PC auch Umschalt + Mausrad). Doppelklick, **Ganzer Plan** oder das Umstellen von Plan oder Verlauf zeigt wieder alles.

## Was deine PV bringt

Die Karte zeigt die **Ersparnis durch PV** für heute, Monat und Jahr: nicht gekaufter Strom plus Einspeiseerlös, mit Heizstab auch die Wärme. Das ist eine **Messung**: Jede kWh ist gemessen, die Preise werden je Viertelstunde so festgehalten, wie sie damals galten. „Woraus setzt sich das zusammen?" zeigt die Posten des Tages. Welcher Teil der Einspeisung zum Gemeinschaftssatz zählt, beruht auf der Bedarfsprognose (PeakShare) bzw. im Quotenmodus auf der Abnahmequote; endgültig steht es erst mit der EEG-Abrechnung fest.

Die Sensoren **Ersparnis durch Optimierung** (heute, Monat, Jahr) sind dagegen eine **Modellrechnung**: der Unterschied zu einem simulierten Standardbetrieb mit den gemessenen PV- und Verbrauchswerten.

> [!IMPORTANT]
> **Die beiden Werte nie addieren.** Der Vorteil der Optimierung ist bereits in der Ersparnis durch PV enthalten — er ist der Teil davon, der aus der Steuerung stammt (Attribut `davon_optimierung` am Sensor „Ersparnis durch PV heute"). Wer beide zusammenzählt, zählt ihn doppelt.

**Kein Eingriff.** Lief die Batterie praktisch wie im Standardbetrieb (Abweichung höchstens 1 kWh bzw. 10 % des Durchsatzes, je nachdem, was größer ist), zeigt die Ersparnis durch Optimierung 0, und das Attribut `kein_eingriff` ist wahr. Die Rest-Differenz ist Rauschen und steht in `vorteil_roh`. Im Modus Aus sollte der Wert gegen null gehen.

**Negative Tage.** Ein Tag kann schlechter sein als der Standardbetrieb. Die Gründe stehen im Attribut `begruendung`, der größte zuerst — meist ungeplanter Verbrauch (z. B. Elektroauto), eine Gemeinschaft, die weniger brauchte als vorhergesagt, oder Energie, die am Tagesende nicht mehr in der Batterie war. Der Vergleich ist streng: Die Simulation kennt den echten Verbrauch, der Fahrplan nur die Prognose. Solange der Tag läuft, ist jeder Wert ein Zwischenstand.

_Ein **Bilanztag** läuft von 04:00 bis 04:00, damit Abend und Nacht-Entladung im selben Tag liegen. „Heute" beginnt also um 04:00, und um 02:00 am Monatsersten zählt noch der Vormonat._

## Einspeisung

Diese Karte zeigt in **Energie** (nicht in Geld), was ins Netz ging, woher es kam und wer es genommen hat. Oben wählst du den Zeitraum: **Tag**, **Monat**, **Jahr** oder **Gesamt** (seit Beginn der Aufzeichnung); die Wahl wird gemerkt. Mit ‹ › blätterst du zurück, ein Tipp auf den Namen springt zum laufenden Zeitraum.

| Kachel | Bedeutung |
|---|---|
| **eingespeist** | Alles, was ins Netz ging |
| **aus der Batterie** | Einspeisung in Viertelstunden **ohne PV-Leistung** (unter 50 W) — egal, ob die Optimierung dabei entladen hat. Darunter der Anteil an der gesamten Einspeisung |
| **bei Gemeinschaftsbedarf** | Einspeisung in Viertelstunden, in denen deine Gemeinschaft laut PeakShare-Prognose zu wenig Strom hatte, höchstens ihr Restbedarf. Das ist **nicht** alles, was die Gemeinschaft aufnimmt (auch bei Überschuss bekommt jeder Erzeuger anteilig etwas, das lässt sich aber ohne ihre Daten nicht berechnen). Nur mit eingerichteter Gemeinschaft; im Quotenmodus heißt die Kachel „an die Gemeinschaft (Quote)“ und folgt der Abnahmequote — eine Annahme, keine Messung |
| **Erlös je kWh** | Einspeiseerlös geteilt durch eingespeiste Energie — der Durchschnittspreis je kWh |
| **Autarkie** | Anteil des Verbrauchs (Haus und Heizstab), der nicht aus dem Netz kam |
| **Eigenverbrauch** | Anteil der PV-Erzeugung, der nicht ins Netz ging |

Klein darunter steht teils der **Standardbetrieb** zum Vergleich („ohne Optimierung …“). Fehlt der Vergleich für manche Tage, steht dort „an N Tagen: … statt …“; beide Werte gelten dann für dieselben Tage.

Das **Diagramm** zeigt gestapelte Balken: grün die Einspeisung bei Gemeinschaftsbedarf, grau die übrige, der Batterie-Anteil orange schraffiert darüber. Beim Tag je Viertelstunde (blassgrün dahinter: Viertelstunden mit Gemeinschaftsbedarf), im Monat je Tag, im Jahr je Monat, bei Gesamt je Jahr. Vergangene Tage zeigen die Viertelstunden aus dem Archiv (Einstellungen → System → **Verlauf vergangener Tage**); ältere Tage nur die Tagessumme. Ein Tipp auf einen Balken zeigt seine Werte.

_Batterie-Anteil und Vergleich werden erst seit Version 2.1.26 aufgezeichnet; die Karte weist bei älteren Tagen darauf hin. Der Sensor **Entladung ins Netz** zählt nur Energie aus gesteuerten Entladungen — daher eine andere Zahl._

## Bezug

Das Gegenstück zur Einspeisung: was aus dem Netz kam und was es gekostet hat. Zeitraum und Blättern wie dort.

| Kachel | Bedeutung |
|---|---|
| **bezogen** | Netzbezug im Zeitraum |
| **Kosten** | Netzbezug mal Bezugspreis der jeweiligen Viertelstunde, brutto (Preis wie damals gültig). Ohne Grundgebühr und Leistungspreis |
| **Kosten je kWh** | Mittlerer Bezugspreis, gewichtet mit der bezogenen Menge |
| **höchste Viertelstunde** | Höchste mittlere Bezugsleistung einer Viertelstunde im Zeitraum, mit Zeitpunkt |

Das Diagramm zeigt den Bezug als Balken (Tag: je Viertelstunde, sonst je Tag, Monat oder Jahr); ein Tipp nennt Menge und Kosten. Viertelstunden ohne erfassten Bezugspreis (z. B. vor dieser Funktion oder nach einem Neustart ohne Fahrplan) zählen nicht als 0 €: Die Karte nennt ihre Menge, und der Preis je kWh gilt nur für den Rest.

## Optimierungsgewinn

Vergleicht den Plan mit einem simulierten **Standardbetrieb**: PV-Überschuss lädt zuerst die Batterie, ein Defizit entlädt sie bis zum Mindest-Ladestand; ein Heizstab bekommt, was sonst abgeregelt würde, höchstens so viel, wie der Puffer je Tag aufnimmt. Die Zahl im Kopf ist der erwartete Mehrerlös — eine Prognose, kein Messwert. Gezählt werden mindestens 24 Stunden und dann bis zur nächsten Mitternacht (mittags also rund 36 Stunden); die Stundenzahl steht in der Überschrift. Aufgeklappt: die Geldposten beider Betriebsarten und ein Diagramm der Einspeisung ohne Optimierung.

## Bezugsspitze & Spitzenkappung

Mit der neuen Tarifstruktur kostet die höchste Viertelstunde Netzbezug im Monat extra — der **Leistungspreis**. Die Karte zeigt einen Monat; mit ‹ › blätterst du zurück.

- **Kennzahlen:** **Monatsspitze** mit Zeitpunkt, **laufende Viertelstunde** (hochgerechnet, als Balken gegen die Schwelle: grün, orange ab 80 %, rot darüber), **Schwelle** (deine Grenze oder die schon höhere Monatsspitze) und **Leistungspreis je Monat** als Spanne von Einführung bis Endausbau. In früheren Monaten steht statt der laufenden Viertelstunde, an wie vielen Tagen die Grenze überschritten wurde.
- **Tagesspitzen:** höchste Viertelstunde jedes Tages als Balken, orange über der Grenze; die Grenze als gestrichelte, die Monatsspitze als rote Linie. Ein Tipp zeigt Wert und Uhrzeit. Gezählt je Bilanztag (04:00 bis 04:00).
- **Spitzenkappung** (nur im laufenden Monat): ob sie bereit ist oder greift, darunter die zwei Stufen — **Batterie** (Ladestand und Mindest-Ladestand als Balken, darüber schraffiert die Reserve mit „Reserve bis x %“ — bei Reserve 0 entfällt sie —, dazu ein Satz, was sie gerade tut) und **Verbraucher** (an, aus oder abgeschaltet, mit der Zeit, ab der sie frühestens wieder dürfen). Ist die Spitzenkappung aus, führt ein Knopf in die Einstellungen.
- **Was kostet die Spitze — und ab wann?** (zugeklappt): Richtwerte der E-Control für deine Spitze und der Termin der Umstellung.

Gemessen wird wie beim Netzbetreiber: Bezug je fester Viertelstunde (:00, :15, :30, :45); Einspeisung wird nicht gegengerechnet. Die Werte kommen vom Netzsensor des Wechselrichters, nicht vom Zähler, sind aber mit dem Smart-Meter-Portal vergleichbar. Sensoren: **Netzbezug Viertelstunde** (die zuletzt abgeschlossene) und **Bezugsspitze Monat** (ohne die Mindestbemessung von 2 kW, die nur für die Abrechnung gilt). Eine Viertelstunde mit Messlücken zählt trotzdem, als Untergrenze, und ist als unvollständig markiert.

**Ab wann die Spitze kostet:** erst ab der Umstellung deines Netzbereichs. Den Termin legt der Netzbetreiber fest: frühestens 1.1.2027, ohne eigene Meldung 1.12.2027, spätestens 1.7.2028. Bis dahin zahlt ein Haushalt eine Pauschale. Der Leistungspreis macht 2027 und 2028 rund 40 % des Netznutzungsentgelts aus, ab 2029 die Hälfte. Verrechnet werden mindestens 2 kW; im Endausbau kostet jedes kW über 10 kW doppelt (für die Einführung ist der Satz darüber noch nicht bekannt).

**Was sie kostet:** Die Beträge je Netzbereich kommen erst mit der Tarifverordnung (Erlass im Dezember 2026). Bis dahin rechnet die Karte mit Richtwerten der E-Control (Juli 2026, inkl. 20 % USt.): zur Einführung rund 1,90 € je kW und Monat (je Netzbereich 1,50 bis 2,60 €), im Endausbau bis zu 3,38 € je kW bis 10 kW und 6,76 € darüber.

## Die übrigen Karten

- **Prognosevergleich** — nur mit eingeschaltetem Vergleich: Fremdprognose, eigene Berechnung und Messung nebeneinander (siehe Anleitung „Eigene PV-Prognose").
- **Energieprognose (7 Tage)** — erwarteter Verbrauch und PV-Ertrag je Tag; mit Prognosevergleich ein zusätzlicher Balken für die mitlaufende Quelle.
- **Verbrauchsprofil (Werktag / Wochenende)** — der gelernte Verbrauch, als Stundenverlauf oder Tag / Nacht.
- **Aktivitätsprotokoll** — jeder Zustandswechsel der Steuerung mit Zeit und Klartext, filterbar nach Laden, Entladung und Normalbetrieb. „Alle Einträge" zeigt auch das stündliche Lebenszeichen, „Mehr laden" ältere Einträge.

## Wenn etwas nicht stimmt

**Failsafe.** Gibt es 15 Minuten lang keinen brauchbaren Plan, gibt der Optimizer den Wechselrichter frei. Die Statuskarte zeigt rot „Failsafe aktiv — kein brauchbarer Optimierungsplan, der Wechselrichter läuft im Automatikmodus." Fehlt der Plan nur kurz, bleibt der letzte Zustand („Plan fehlt kurzzeitig — letzter Zustand bleibt"). Den Grund nennen der Optimierungsplan („Noch kein Optimierungsplan: …") und der rote Zeitstempel „Plan" — meist ein Ladestand-Sensor, der nicht antwortet, oder eine Prognose ohne Daten. Ist das behoben, übernimmt der Plan von selbst; mit **Jetzt rechnen** sofort.

**Not-Aus.** Bezieht das Haus während einer Entladung in drei Läufen hintereinander (rund eineinhalb Minuten) mehr als 1 kW aus dem Netz, stoppt der Optimizer die Entladung: „Not-Aus: anhaltender Netzbezug während der Entladung — gesperrt bis zum nächsten Slot." Mit der nächsten Viertelstunde ist die Sperre aufgehoben. Passiert das oft, prüf die Statuskarte: Zeigt sie beim Einspeisen einen negativen Netzwert und beim Bezug einen positiven, ist der Netzsensor falsch zugeordnet — dann den Assistenten erneut durchlaufen.

**Treiber wird nicht gesteuert.** „Nur Anzeige“ und der blaue Hinweis „Dieser Wechselrichter wird nicht gesteuert" heißen: Für dein Gerät ist keine Steuerung freigegeben. Bei den sechs unterstützten Wechselrichtern kommt das nicht vor (siehe „Stand der Unterstützung“ in der Doku). Der Plan wird trotzdem gerechnet und angezeigt; am Wechselrichter ändert sich nichts, auch nicht im Modus Ein.

> [!NOTE]
> **Ein fehlgeschlagener Steuerbefehl wird nicht in jedem Lauf wiederholt.** Die Statuskarte meldet ihn orange als „Letzter Steuerbefehl fehlgeschlagen", und der Optimizer wartet zwischen zwei Versuchen immer länger. Hält das an, hilft das Fahrplan-Archiv (Einstellungen → System) bei der Fehlersuche.
