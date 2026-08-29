# Changelog

Alle nennenswerten Änderungen am EEG Energy Optimizer.

Format orientiert sich an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/).
Versionierung folgt [SemVer](https://semver.org/lang/de/).

> Dieses Repository beginnt mit der 2.0.0. Die Vorgeschichte — die 1.x-Reihe mit
> der zustandsbasierten Steuerung und die Prototyp-Iterationen der
> Fahrplan-Optimierung — liegt im vorherigen, nicht öffentlichen Repository
> `EEGEnergyOptimizer-chamo`.

## [2.0.3-dev] - 2026-08-29

### Hinzugefügt

- **Fronius: Sicherheitsnetz gegen eingefrorene Batterie.** Ladesperre und Entladung werden jetzt mit der Fronius-Rückfallzeit (`InOutWRte_RvrtTms`, 5 Minuten) scharfgeschaltet und im Minutentakt aufgefrischt. Fällt Home Assistant mitten in einem Fahrplan-Slot aus, beendet der Wechselrichter den erzwungenen Betrieb selbst und kehrt zu seiner eigenen Batteriesteuerung zurück — bisher blieb die Batterie blockiert, bis jemand eingriff. Damit haben Fronius, Kostal und SMA dasselbe Failsafe-Verhalten. Hinweis: Ein zweites Programm, das denselben Wechselrichter über Modbus abfragt, hält die Rückfallzeit mit am Leben.

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
