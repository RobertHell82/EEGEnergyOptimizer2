# Einstellungen und Expertenmodus

Die Einstellungen öffnest du über das Zahnrad oben rechts im Dashboard. Die Tabs **Tarife**, **Anlage** und **Prognose** haben dieselben Felder wie der Einrichtungsassistent. Für eine kleine Änderung musst du den Assistenten also nicht neu durchlaufen.

## Die Tabs

| Tab | Inhalt |
|---|---|
| **Tarife** | Karten **Vergütung und Kosten** (Standardvergütung, Nachtsatz, Bezugspreis, Netzbereich) und **Energiegemeinschaft**. Wie der Assistentenschritt „Tarife & Gemeinschaft“ |
| **Anlage** | Karten **Anlage** (Einspeisegrenze) und **Batterie** (Mindest- und Maximum-Ladestand). Wie der Assistentenschritt „Anlage & Batterie“ |
| **Prognose** | Steuernde Quelle der PV-Prognose mit Sensoren, Schalter **Prognosevergleich** und – wenn die eigene Berechnung steuert oder mitläuft – die Flächentabelle. Wie der Assistentenschritt „PV-Prognose“ |
| **Spitzenkappung** | Bezugsspitzen kappen: Grenzwert, Mindest- und Maximalreserve der Batterie (Huawei, Fronius, SMA, SolaX) und Verbraucher, die abgeschaltet werden dürfen (siehe Anleitung „Spitzenkappung“) |
| **Verbraucher** | Karten **Heizstab** (Fronius Ohmpilot, siehe Anleitung „Heizstab“) und **Wallbox** (siehe Anleitung „Wallbox“). **Nur im Expertenmodus**, nicht im Assistenten |
| **System** | Karte **Dashboard** (Karten ausblenden), Übersicht über Anlage und Sensoren, EEG-Statistik, **Verlauf vergangener Tage**, Fahrplan-Archiv und ganz unten der Schalter **Expertenmodus** |

## Expertenmodus

Der Schalter steht ganz unten im Tab **System**. Er zeigt zusätzliche Optionen und Diagnose-Details. In den Einstellungen sofort, im Dashboard nach dem Speichern.

Zusätzlich sichtbar werden:

- **Tab Tarife:** *Alterungskosten der Batterie (ct/kWh)*, bei Huawei *Batterie aus dem Netz laden* (Beta, siehe Anleitung „Tarife & Gemeinschaft“)
- **Tab Anlage:** die Gerätedaten *AC-Grenzleistung des Wechselrichters*, *PV-Spitzenleistung* und *Batterie-Leistungsgrenze*, dazu *Sicherheitspuffer auf die Prognose (%)*, *Ladeziel am Abend (%)* und *Vormittags bevorzugt netzdienlich* (siehe Anleitung „Anlage & Batterie“)
- **Tab Prognose:** bei Solcast die *Weiteren Prognose-Sensoren* (PV-Prognose heute gesamt, Tag 3 bis Tag 7)
- **Tab Verbraucher** als Ganzes, mit Heizstab und Wallbox
- **Tab System:** Karten **Verbrauchsprofil** (Rückblick in Wochen) und **Tagesbilanz**
- **Dashboard:** in der aufgeklappten Karte **Verbrauchsprofil** Datenpunkte, Fenster, letzte Berechnung und der Knopf **Verbrauchsprofil neu berechnen**; bei einer Wallbox der Handbetrieb (Laden/Entladen) in der Statuskarte
- **Assistent:** eigener Haken *Expertenmodus* oben. Damit erscheinen bei Solcast die *Weiteren Prognose-Sensoren* und je nach Wechselrichter ein zweiter PV- bzw. Batterie-Sensor

_Im Assistenten stehen die Gerätedaten immer da. Sie ändern sich nach dem ersten Eintragen kaum, darum sind sie in den Einstellungen im Expertenmodus versteckt._

> [!NOTE]
> Der Sicherheitspuffer steht bewusst auf 0 %. Ein Aufschlag macht die Prognose nicht besser, er verlagert nur Einspeisung aus den Bedarfsstunden der Gemeinschaft in die Batterie und kostet Ertrag.

## Sensoren ändern

Die Karte **Anlage und Sensoren** im Tab **System** zeigt nur an, welcher Wechselrichter und welche Sensoren zugeordnet sind. Ändern kannst du sie mit **Einrichtung erneut durchlaufen**: Der Assistent startet beim Schritt „Wechselrichter“, mit deinen aktuellen Werten vorbefüllt. Nur der Assistent erkennt Sensoren automatisch und testet die Verbindung.

Ausnahme: die Sensoren der PV-Prognose im Tab **Prognose**. Die Quelle wählst du dort über die drei Karten. Bei Solcast und Forecast.Solar stehen die Sensoren darunter, zugeklappt unter **Prognose-Sensoren**. Wechselst du die Quelle, werden sie aus der Erkennung vorbelegt; aufgeklappt kannst du sie ändern. Fehlt ein Pflichtsensor, ist der Bereich von selbst offen.

## Speichern

Der Knopf **Speichern** unter den Tabs übernimmt die Änderungen aus **allen** Tabs auf einmal. Danach bist du wieder im Dashboard. Fehlt ein Pflichtwert oder passt etwas nicht zusammen, wird nichts gespeichert; über dem Knopf steht dann, was fehlt. Geprüft werden:

- **Anlage:** AC-Grenzleistung, PV-Spitzenleistung, Batterie-Leistungsgrenze, Höhe der Einspeisegrenze (wenn eingeschaltet)
- **Tarife:** Standardvergütung (bei „Fester Wert“), Arbeitspreis, Netzbereich (wenn Sommer-Nieder- oder Winter-Nieder-Arbeitspreis eingeschaltet ist), das Nachtfenster, sobald ein Nachtsatz eingetragen ist (Standardvergütung und je Gemeinschaft), die Anteile der Gemeinschaften (zusammen höchstens 100 %), im Quotenmodus die Tages-Abnahmequote und mit eingeschaltetem *Batterie aus dem Netz laden* ein Bezugspreis, der billiger sein kann als die Nacht (Spot-Tarif, Bezugspreis einer Gemeinschaft oder zeitvariable Netzentgelte), bei Spot- oder Gemeinschaftsbezug außerdem der Netzbereich
- **Prognose:** die Sensoren einer fremden Prognosequelle, gültige PV-Flächen
- **Verbraucher:** Adresse, Leistung und Maximaltemperatur des Heizstabs, Adresse der Ambibox
- **Spitzenkappung** (wenn eingeschaltet): Grenzwert zwischen 2 und 20 kW, Mindest- und Maximalreserve zwischen 0 und 80 % (die Mindestreserve nicht über der maximalen), je Verbraucher Leistungssensor und Schalter, und ohne Batterie-Stufe mindestens ein Verbraucher

Die meisten Änderungen wirken **sofort**: Der Fahrplan wird neu gerechnet, die Steuerung läuft weiter. Ein geänderter Rückblick des Verbrauchsprofils wird im Hintergrund neu berechnet.

Bei diesen Änderungen **lädt die Integration neu** (dauert einige Sekunden):

- steuernde Quelle der PV-Prognose und ihre Sensoren
- PV-Flächen, Verluste und Schalter Prognosevergleich
- Heizstab: Ein/Aus, Adresse, Port und Leistung (die übrigen Heizstab-Felder wirken sofort)
- Wallbox: Typ, Adresse, Port, Unit-ID und Ladepunkt
- alles, was du über **Einrichtung erneut durchlaufen** änderst

## Der Tab System im Einzelnen

### Dashboard

Hier wählst du, welche Karten das Dashboard zeigt. Das gilt nur für dieses Gerät und diesen Browser, wirkt sofort und braucht kein **Speichern**. Ausgeblendete Karten rechnen, steuern und zeichnen weiter auf. Die Statuskarte lässt sich nicht ausblenden.

### Verlauf vergangener Tage

Der Schalter **Viertelstunden speichern** (Vorgabe ein) legt beim Tageswechsel Bezug, Einspeisung und Kosten je Viertelstunde ab. So zeigen die Karten **Einspeisung** und **Bezug** auch für vergangene Tage den Verlauf, nicht nur die Summe. Aufbewahrt werden 400 Tage (rund 4 kB je Tag, nach einem Jahr knapp 2 MB). Ausgeschaltet wird nichts Neues abgelegt; gespeicherte Tage bleiben sichtbar.

### EEG-Statistik

Mit **EEG-Statistik aktivieren** sendet deine Anlage anonymisierte Diagnose- und Wirksamkeitsdaten – keine personenbezogenen Daten, keine IP-Adressen. Wirkt sofort, ohne **Speichern**. Ausschalten pausiert nur, die anonyme Kennung bleibt. **Daten löschen** entfernt nach einer Rückfrage alle übermittelten Daten am Server und verwirft die lokale Anmeldung – endgültig. Was übertragen wird, steht unter *Datenschutz-Details* und in der [README](https://github.com/RobertHell82/EEGEnergyOptimizer2#eeg-statistik).

### Fahrplan-Archiv

Alle 15 Minuten und bei jeder deutlichen Planänderung wird der Fahrplan abgelegt und **7 Tage** aufbewahrt. Die Karte zeigt, wie viele Fahrpläne aus welchem Zeitraum vorliegen. **Archiv herunterladen** liefert ein ZIP mit:

- allen archivierten Fahrplänen samt allen Eingangsgrößen (PV samt unterer Prognose, Verbrauch, Batterie, Preise und Gemeinschaftsbedarf je Viertelstunde, Heizstab-Zustand)
- dem gemessenen Verlauf der Leistungen im 5-Minuten-Raster
- den Einstellungen, **ohne** Netzwerk- und Zugangsdaten
- einer Lesehilfe

Hat die Anlage etwas Unerwartetes getan, schick dieses ZIP an den Support. _Der Download-Link gilt nur wenige Minuten. Geht er nicht mehr, klick auf **Zustand aktualisieren**._

### Verbrauchsprofil (Expertenmodus)

**Rückblick (Wochen)**: über wie viele Wochen der Hausverbrauch gemittelt wird (1–52, Vorgabe 4). Getrennt nach Werktagen und Wochenende samt Feiertagen. Der höchste Wert je Stunde wird verworfen, damit Ausreißer wie eine E-Auto-Ladung kaum zählen. Sofort neu berechnen: im Dashboard die Karte **Verbrauchsprofil** aufklappen und **Verbrauchsprofil neu berechnen** klicken.

### Tagesbilanz (Expertenmodus)

Vergleicht für den letzten abgeschlossenen Tag erwarteten und gemessenen PV-Ertrag und Verbrauch – mit dem Plan vom Vorabend (24 h) und von zwei Tagen vorher (48 h). Läuft jede Nacht um 00:15; **Jetzt rechnen** startet sie sofort. Mit aktiver EEG-Statistik wird das Ergebnis mitgesendet. Sie braucht Messwerte für 95 % des Tages und bleibt bei einer neuen Anlage daher anfangs leer.

## Wer was darf

Das Panel sehen **alle angemeldeten Benutzer**. Ohne Administratorrechte funktionieren Dashboard, Optimierungsplan und alle Anzeigen, Pause und Aufheben, Neurechnen des Plans, **Jetzt rechnen** in der Tagesbilanz und der Archiv-Download.

Nur **Administratoren** dürfen:

- Einstellungen **speichern** und den Assistenten nutzen (Sensorerkennung, Verbindungstests)
- **Prognose berechnen** bei der eigenen PV-Prognose
- EEG-Statistik ein- und ausschalten und **Daten löschen**
- **Verbrauchsprofil neu berechnen**
- bei der Wallbox **Verbindung testen** und den Handbetrieb starten

> [!TIP]
> Meldet jemand „Fehler beim Speichern“, fehlen meist die Administratorrechte.
