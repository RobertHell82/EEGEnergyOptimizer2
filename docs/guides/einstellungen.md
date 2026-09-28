# Einstellungen und Expertenmodus

Die Einstellungen öffnest du über das Zahnrad oben rechts im Dashboard. Sie sind in Tabs gegliedert. Die Tabs **Tarife**, **Anlage** und **Prognose** enthalten dieselben Felder wie die entsprechenden Schritte des Einrichtungsassistenten. Du musst den Assistenten also nicht noch einmal durchlaufen, nur um einen Preis oder den Mindest-Ladestand zu ändern.

## Die Tabs

| Tab | Was dort steht |
|---|---|
| **Tarife** | Karten **Vergütung und Kosten** (Standardvergütung, Nachtsatz, Bezugspreis, Netzbereich) und **Energiegemeinschaft**. Entspricht dem Assistentenschritt „Tarife & Gemeinschaft“ |
| **Anlage** | Karten **Anlage** (Einspeisegrenze) und **Batterie** (Mindest- und Maximum-Ladestand). Entspricht dem Assistentenschritt „Anlage & Batterie“ |
| **Prognose** | Die steuernde Quelle der PV-Prognose mit ihren Sensoren, der Schalter **Prognosevergleich** und, wenn die eigene Berechnung steuert oder mitläuft, die Flächentabelle der Anlage. Entspricht dem Assistentenschritt „PV-Prognose“ |
| **Verbraucher** | Karten **Heizstab** (Fronius Ohmpilot, siehe Anleitung „Heizstab“) und **Wallbox** (siehe Anleitung „Wallbox“). Der Tab erscheint **nur im Expertenmodus** und kommt im Assistenten nicht vor |
| **System** | Übersicht über Anlage und Sensoren, EEG-Statistik, Fahrplan-Archiv und ganz unten der Schalter **Expertenmodus** |

## Expertenmodus

Der Schalter steht im Tab **System** ganz unten. Er blendet zusätzliche Optionen und Diagnose-Details ein. Die Einstellungen zeigen das sofort; im Dashboard erscheint es, sobald du gespeichert hast.

Zusätzlich sichtbar werden:

- **Tab Tarife:** *Alterungskosten der Batterie (ct/kWh)*
- **Tab Anlage:** die Gerätedaten *AC-Grenzleistung des Wechselrichters*, *PV-Spitzenleistung* und *Batterie-Leistungsgrenze*, dazu *Sicherheitspuffer auf die Prognose (%)*
- **Tab Prognose:** bei Solcast die *Weiteren Prognose-Sensoren* (PV-Prognose heute gesamt, Tag 3 bis Tag 7)
- **Tab Verbraucher** als Ganzes, mit Heizstab und Wallbox
- **Tab System:** die Karten **Verbrauchsprofil** (Rückblick in Wochen) und **Tagesbilanz**
- **Dashboard:** in der aufgeklappten Karte **Verbrauchsprofil** Datenpunkte, Fenster, letzte Berechnung und der Knopf **Verbrauchsprofil neu berechnen**, bei einer Wallbox der Handbetrieb (Laden/Entladen) in der Statuskarte
- **Assistent:** Der Assistent hat oben einen eigenen Haken *Expertenmodus*. Damit erscheinen bei Solcast die *Weiteren Prognose-Sensoren* (PV-Prognose heute gesamt, Tag 3 bis Tag 7) und je nach Wechselrichter ein zweiter PV- bzw. Batterie-Sensor

_Die Gerätedaten stehen im Assistenten immer da. Du trägst sie einmal aus dem Datenblatt ein; danach ändern sie sich nicht mehr. Deshalb sind sie in den Einstellungen im Expertenmodus versteckt._

> [!NOTE]
> Der Sicherheitspuffer steht bewusst auf 0 %. Ein Aufschlag macht die Prognose nicht besser, sondern verschiebt sie nur. Außerdem verlagert er Einspeisung aus den Bedarfsstunden der Gemeinschaft in die Batterie und kostet so Ertrag.

## Sensoren ändern

Die Karte **Anlage und Sensoren** im Tab **System** zeigt nur an, welcher Wechselrichter und welche Sensoren zugeordnet sind. Ändern kannst du sie hier nicht. Dafür gibt es den Knopf **Einrichtung erneut durchlaufen**: Er startet den Assistenten direkt beim Schritt „Wechselrichter“, und alle Felder sind mit deiner aktuellen Konfiguration vorbefüllt. Der Grund für den Umweg: Nur der Assistent erkennt Sensoren automatisch und testet die Verbindung zum Wechselrichter.

Es gibt eine Ausnahme: die Sensoren der PV-Prognose im Tab **Prognose**. Die steuernde Quelle wählst du dort wie im Assistenten über die drei Karten. Bei Solcast und Forecast.Solar stehen die Sensoren darunter, zugeklappt unter **Prognose-Sensoren**; die Zeile zeigt, welche gerade zugeordnet sind. Ein Wechsel der Quelle belegt sie aus der Erkennung vor, aufgeklappt kannst du sie ändern. Fehlt ein Pflichtsensor, ist der Bereich von selbst offen. Ohne diese Sensoren wäre der Wechsel wertlos.

## Speichern

Der Knopf **Speichern** unter den Tabs übernimmt die Änderungen aus **allen** Tabs auf einmal. Danach bist du wieder im Dashboard. Fehlt ein Pflichtwert (AC-Grenzleistung, PV-Spitzenleistung, Höhe der Einspeisegrenze, die Sensoren einer fremden Prognosequelle, gültige PV-Flächen, Adresse und Leistung des Heizstabs), wird nichts gespeichert. Über dem Knopf steht dann, was fehlt.

Die meisten Änderungen wirken **sofort**: Der Fahrplan wird gleich neu gerechnet, die Steuerung behält ihren Zustand. Einen geänderten Rückblick des Verbrauchsprofils rechnet die Integration im Hintergrund neu.

Bei einigen Änderungen **lädt die Integration neu**, weil Treiber und Sensoren mit der neuen Anbindung neu entstehen müssen. Das dauert einige Sekunden:

- die steuernde Quelle der PV-Prognose und ihre Sensoren
- PV-Flächen, Verluste und der Schalter Prognosevergleich
- beim Heizstab: Ein/Aus, Adresse, Port und Leistung (Temperaturen, Wärmewert und die anderen Heizstab-Felder wirken sofort)
- bei der Wallbox: Typ, Adresse, Port, Unit-ID und Ladepunkt
- alles, was du über **Einrichtung erneut durchlaufen** an Wechselrichter und Sensoren änderst

## Der Tab System im Einzelnen

### EEG-Statistik

Mit **EEG-Statistik aktivieren** sendet deine Anlage anonymisierte Diagnose- und Wirksamkeitsdaten, also keine personenbezogenen Daten und keine IP-Adressen. Der Schalter wirkt sofort, **Speichern** brauchst du dafür nicht. Ausschalten pausiert nur: Die anonyme Kennung bleibt gespeichert. Mit **Daten löschen** werden nach einer Rückfrage alle übermittelten Daten am Server entfernt und die lokale Anmeldung verworfen. Das lässt sich nicht rückgängig machen. Was genau übertragen wird, steht in der Karte unter *Datenschutz-Details* und ausführlich in der [README](https://github.com/RobertHell82/EEGEnergyOptimizer2#eeg-statistik).

### Fahrplan-Archiv

Alle 15 Minuten und zusätzlich bei jeder deutlichen Planänderung legt die Integration den gerechneten Fahrplan ab und bewahrt ihn **7 Tage** auf. Die Karte zeigt, wie viele Fahrpläne aus welchem Zeitraum vorliegen. **Archiv herunterladen** liefert ein ZIP mit:

- allen archivierten Fahrplänen, jeweils mit sämtlichen Eingangsgrößen des Modells (PV, Verbrauch, Batterie, Preise je Viertelstunde)
- dem gemessenen Verlauf der Leistungen im 5-Minuten-Raster
- den Einstellungen, **ohne** Netzwerk- und Zugangsdaten
- einer Lesehilfe

Hat die Anlage etwas Unerwartetes getan, schick dieses ZIP an den Support. Damit lässt sich jeder Lauf nachrechnen, ohne deine Anlage zu befragen. _Der Download-Link gilt nur wenige Minuten. Funktioniert er nicht mehr, klick auf **Zustand aktualisieren**._

### Verbrauchsprofil (Expertenmodus)

**Rückblick (Wochen)** legt fest, über wie viele Wochen der Hausverbrauch gemittelt wird (1–52, Vorgabe 4). Gemittelt wird über zwei Gruppen: Werktage und Wochenende samt Feiertagen. Der höchste Wert je Stunde wird verworfen, damit ein Ausreißer wie eine E-Auto-Ladung kaum ins Gewicht fällt. Wenn du das Profil sofort neu berechnen willst, klapp im Dashboard die Karte **Verbrauchsprofil** auf und klick auf **Verbrauchsprofil neu berechnen**.

### Tagesbilanz (Expertenmodus)

Die Karte stellt für den letzten abgeschlossenen Tag gegenüber, was der Fahrplan an PV-Ertrag und Verbrauch erwartet hatte und was gemessen wurde. Verglichen wird mit dem Plan vom Vorabend (24 h) und dem von zwei Tagen vorher (48 h). Sie läuft jede Nacht um 00:15 von selbst; **Jetzt rechnen** holt das sofort nach. Ist die EEG-Statistik aktiv, wird das Ergebnis mitgesendet. Die Bilanz braucht für 95 % des Tages Messwerte, deshalb bleibt sie bei einer frisch eingerichteten Anlage anfangs leer.

## Wer was darf

Das Panel sehen **alle angemeldeten Benutzer**. Dashboard, Optimierungsplan und alle Anzeigen funktionieren auch ohne Administratorrechte, ebenso Pause und Aufheben, das Neurechnen des Plans, **Jetzt rechnen** in der Tagesbilanz und der Archiv-Download.

Nur **Administratoren** dürfen:

- Einstellungen **speichern** und den Einrichtungsassistenten nutzen (Sensorerkennung, Verbindungstests der Wechselrichter)
- **Prognose berechnen** bei der eigenen PV-Prognose
- die EEG-Statistik ein- und ausschalten und **Daten löschen**
- **Verbrauchsprofil neu berechnen**
- bei der Wallbox **Verbindung testen** und den Handbetrieb starten

> [!TIP]
> Meldet ein Benutzer „Fehler beim Speichern“, fehlen ihm meist die Administratorrechte. Die Einstellungen kann er ansehen, ändern kann sie nur ein Administrator.
