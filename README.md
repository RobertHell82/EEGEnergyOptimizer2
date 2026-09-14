# EEG Energy Optimizer

> Gesteuert wird ausschließlich über einen **LP-Fahrplan-Optimierer** (aus [EngagePV/chamo](https://gitlab.com/EngagePV/chamo) von Harald Geyer): Er rechnet jede Minute einen Plan über 48 Stunden und setzt ihn am Wechselrichter durch. Feste Zeitfenster und Zustandsregeln gibt es nicht — das Verhalten entsteht aus den Preisen. Technische Details: [CHAMO.md](CHAMO.md).

HACS-kompatible Home Assistant Integration für vorausschauendes Batteriemanagement, optimiert für Energiegemeinschaften (EEG) im DACH-Raum.

## Funktionen

- **Fahrplan-Optimierung** — rechnet jede Minute aus den Einspeisepreisen den erlösbesten Lade- und Entladeplan über 48 Stunden. Feste Zeitfenster gibt es nicht: Zeigt der Preisverlauf keinen Mehrwert, plant der Fahrplan auch keine Einspeisung
- **Einspeisetarife, die sich selbst aktualisieren** — OeMAG, Energie AG „Team Sonne Float“, aWATTar SUNNY oder der Börsen-Spotpreis; die Monatstarife auch als Hochrechnung für den laufenden Monat, statt einen Monat hinterherzulaufen
- **Fahrplan-Steuerung** — setzt den Plan alle 30 Sekunden am Wechselrichter durch: Ladelimit und erzwungene Entladung, nachgeführt an den Messwerten, mit Not-Aus und Failsafe
- **Einspeisegrenze** — plant um die Exportgrenze des Netzbetreibers herum und hebt das Ladelimit an, wenn die Einspeisung trotzdem an der Grenze klebt
- **PeakShare-Integration** — die Bedarfsprognose deiner EEG-Community wird zum Auf- bzw. Abschlag auf den Basistarif und geht so direkt in den Fahrplan ein; im Dashboard ist die Bedarfskurve sichtbar
- **Heizstab** (Beta) — ein Fronius Ohmpilot als zweite Senke: Der Optimierer plant die Wärme mit, wenn sie mehr bringt als die Einspeisung, und die Steuerung führt den Heizstab dem Plan nach. Statt abgeregelt zu werden, geht der Überschuss in den Puffer
- **Wallbox** (Beta) — eine bidirektionale Ambibox als dritte Senke, mit Ladezustand und Ladeleistung des Fahrzeugs im Dashboard
- **PV-Prognose** — Solcast Solar und Forecast.Solar Unterstützung mit 7-Tage-Ausblick
- **Verbrauchsprofil** — lernt stündliche Verbrauchsmuster pro Wochentag aus den HA-Recorder-Daten
- **Live-Dashboard** — Sidebar-Panel mit Energiefluss, Fahrplan und Ist-Verlauf, PeakShare-Bedarfskurve, Geldbilanz und Aktivitätsprotokoll. Die Steuerung lässt sich pausieren — für eine Dauer oder bis die Batterie einen Ladestand erreicht hat
- **Einrichtungsassistent** — schrittweises Onboarding mit automatischer Sensorerkennung

### EEG-Statistik

Der EEG Energy Optimizer sendet anonymisierte Diagnose- und Wirksamkeitsdaten an einen vom Maintainer betriebenen Cloudflare-Backend. Damit lassen sich Schwachstellen schneller finden und die Wirksamkeit der EEG-Steuerung über mehrere Anlagen hinweg auswerten — ohne Personenbezug. Die Funktion ist bei **neuen Installationen standardmäßig aktiv**; deaktivieren und vollständig löschen lässt sie sich jederzeit im Panel unter *Einstellungen → EEG-Statistik*. Bestehende Installationen behalten ihre vorherige Einstellung (Default war zuvor *aus*).

#### Was übermittelt wird

| Kategorie | Frequenz | Inhalt |
|-----------|----------|--------|
| **Profil** | bei Setup, Restart, Settings-Change | App-Version, HA-Version, Wechselrichter-Typ, Batterie-Kapazität, PV-Peak, Prognose-Quelle, Länder-ISO-Code, ausgewählte EEG-Community (sofern PeakShare aktiv), gefilterte Settings (Whitelist) |
| **Failure** | bei Auftreten (mit Dedup) | Zeitstempel, Kategorie, Schweregrad, gehashte Fehlermeldung, Kontext-JSON |

> Snapshot-, State-Change- und Outcome-Meldungen gibt es nicht mehr — ihre Semantik war an die frühere Zustandslogik gebunden, die es nicht mehr gibt. Profil- und Failure-Meldungen laufen weiter.

Die Settings-Whitelist enthält ausschließlich numerische/kategorische Konfigurationswerte (Tarife, Batterie-Leistungsgrenze, Vorausschau etc.) — **keine Entity-IDs**, keine Sensor-Namen.

#### Was nicht übermittelt wird

- Keine Entity-IDs / Sensor-Namen
- Keine IP-Adressen (werden serverseitig nicht persistiert)
- Kein Anlagenname, keine Adresse, keine Geokoordinaten
- Keine Mitgliedsdaten der Energiegemeinschaft
- Keine sonstigen personenbezogenen Daten

#### Identifikation

Pro Anlage wird einmalig eine zufällige **UUIDv4** + ein **API-Key** erzeugt und lokal gespeichert. Es gibt keinen Bezug zu HA-Account, IP, Hardware-ID oder sonstigen Identifikatoren. Beim Klick auf „Daten löschen" werden alle Daten dieser Anlage serverseitig kaskadiert gelöscht und die UUID lokal entfernt.

## Unterstützte Wechselrichter

Alle sechs werden vom Fahrplan **gesteuert** (Ladelimit und erzwungene Entladung), nicht nur ausgelesen:

| Wechselrichter | Anbindung | Guide |
|---|---|---|
| **Fronius Gen24** | direkt per Modbus TCP (SunSpec Model 124) | [Guide](docs/guides/fronius.md) |
| **Huawei SUN2000** | via [Huawei Solar](https://github.com/wlcrs/huawei_solar); Single oder Master/Slave (mehrere Wechselrichter + Batterien), direkt am Dongle oder über EMMA | [Guide](docs/guides/huawei.md) |
| **Kostal Plenticore** (Beta) | direkt per Modbus TCP | [Guide](docs/guides/kostal.md) |
| **Sigenergy SigenStor** | via [Sigenergy-Integration](https://github.com/TypQxQ/Sigenergy-Local-Modbus) | [Guide](docs/guides/sigenergy.md) |
| **SMA Smart Energy** (Beta) | direkt per Modbus TCP (externes Batteriemanagement) | [Guide](docs/guides/sma.md) |
| **SolaX Gen4+** | via [solax_modbus](https://github.com/wills106/homeassistant-solax-modbus) | [Guide](docs/guides/solax.md) |

**Beta** heißt: Die Steuerung ist an einer echten Anlage nachgewiesen, aber noch nicht so breit erprobt wie die übrigen. Den genauen Stand je Gerät führt [docs/wechselrichter-status.md](docs/wechselrichter-status.md).

> **SolarEdge wird derzeit nicht unterstützt.** Der Treiber ist vollständig enthalten, aber stillgelegt: Er steht nicht zur Auswahl und wird nicht gesteuert. Er wird wieder freigeschaltet, sobald die Fahrplan-Steuerung an einer echten Anlage des jeweiligen Typs nachgewiesen ist — Stand, offene Punkte und Freischaltweg: **[docs/wechselrichter-status.md](docs/wechselrichter-status.md)**.

## Installation

1. HACS in Home Assistant öffnen
2. Oben rechts auf die drei Punkte klicken
3. "Benutzerdefinierte Repositories" auswählen
4. Repository-URL eingeben und als Kategorie "Integration" wählen
5. "Hinzufügen" klicken und "EEG Energy Optimizer" installieren
6. Home Assistant neu starten

Ausführliche Schritt-für-Schritt-Anleitungen (inkl. HACS-Installation, Wechselrichter-Anbindung und PV-Prognose-Einrichtung) gibt es in der **[Dokumentation](docs/README.md)**.

## Konfiguration

Nach der Installation die Integration hinzufügen:

**Einstellungen > Geräte & Dienste > Integration hinzufügen > EEG Energy Optimizer**

Das Sidebar-Panel (`/eeg-optimizer`) führt durch die Einrichtung:
1. Voraussetzungsprüfung
2. Wechselrichtertyp wählen + automatische Sensorerkennung
3. Batterie- & PV-Sensoren zuordnen
4. Prognosequelle wählen (Solcast / Forecast.Solar)
5. Fahrplan-Einstellungen (Einspeisevergütung, Arbeitspreis + Netzbereich, Mindest- und Maximum-Ladestand, Batterie-Leistungsgrenze; PeakShare-Community optional)
6. Einspeisegrenze (optional)
7. Wechselrichter-Verbindungstest

## Funktionsweise

### Fahrplan

Jede Minute rechnet die Integration einen **linearen Optimierungs-Fahrplan** über 48 Stunden (15-Minuten-Raster): Welche Viertelstunde lädt, hält oder entlädt die Batterie, und was geht dabei ins Netz. Grundlage sind das gelernte Verbrauchsprofil, der Batteriezustand, die PV-Prognose (bei Solcast inklusive echtem p10-Worst-Case-Pfad) und die konfigurierten Preise.

### Gesteuert wird über Preise

Der Fahrplan kennt keine Zeitfenster und keine Zustände — er kennt nur einen **Preis je Viertelstunde**. Die Einspeisevergütung ist eine Zeitreihe aus zwei Teilen:

1. **Basistarif** — was du bekommst, wenn die Energie nicht in einer Gemeinschaft landet. Zur Wahl stehen ein fester Wert und vier Quellen, die sich selbst aktualisieren:
   - **OeMAG-Monatstarif** — zuletzt veröffentlicht oder für den laufenden Monat hochgerechnet (aus Börsenpreis und österreichischer PV-Erzeugung, Abweichung im Mittel 0,2 ct)
   - **Energie AG „Team Sonne Float"** — Referenzmarktwert Photovoltaik § 13 EAG minus Abschlag, ebenfalls veröffentlicht oder hochgerechnet; die Variante *Loyal Float* mit ihrer Mindestvergütung von 2 ct ist wählbar
   - **aWATTar SUNNY** — fester Monatstarif, beide Vertragsvarianten
   - **Börsen-Spotpreis** (EPEX AT/DE über die aWATTar-API) — als Zeitreihe je Stunde, mit Cent- und Prozentabschlag des Vermarkters
2. **Auf- und Abschlag der Energiegemeinschaften** — hat eine Gemeinschaft in einer Viertelstunde Bedarf, steigt der Preis; hat sie Überschuss, sinkt er.

Der Optimierer maximiert daraus den Erlös über 48 Stunden: Wo eine Kilowattstunde mehr wert ist, wird eingespeist; wo sie weniger wert ist, wird geladen oder gehalten.

**Mit aktiver Energiegemeinschaft heißt das konkret:** Die Integration holt
die Bedarfsprognose der Gemeinschaft (PeakShare) und rechnet sie in den Preis
ein. Braucht die Gemeinschaft in einer Viertelstunde Strom, ist deine
Kilowattstunde dort mehr wert — der Fahrplan hält sie bis dahin zurück und
speist dann ein. Hat die Gemeinschaft dagegen selbst Überschuss, sinkt der
Wert, und es lohnt sich eher, die Batterie zu laden. Genau daraus entsteht
das Verhalten, das man von außen als „abends einspeisen" kennt: nicht als
Uhrzeit, sondern weil dann der Bedarf da ist.

Vergütet wird ohnehin nur, was die Gemeinschaft tatsächlich abnimmt: Der
Anteil, den sie in dieser Viertelstunde nicht braucht, geht zum Basistarif an
den Reststromlieferanten. Deshalb bringt es nichts, in eine Überschussstunde
der Gemeinschaft einzuspeisen — und der Fahrplan tut es auch nicht.

> **Erkennt die Optimierung keinen Mehrwert, passiert nichts.** Ist der Einspeisepreis nachts nicht besser als tagsüber, wird nachts **nicht** eingespeist. Es gibt keine feste Nachtentladung, kein Zeitfenster und keine Automatik „abends entladen" — ohne Preisunterschied bleibt die Batterie, wo sie ist.

Auf die absolute Höhe der Preise kommt es dabei kaum an, auf ihren **zeitlichen Verlauf** sehr: Gemessen genügen rund zwei Cent Unterschied, um Energie in die richtige Viertelstunde zu verschieben.

Weitere Steuergrößen im Fahrplan:

| Größe | Wirkung |
|---|---|
| **Mindest-Ladestand** | Harte Untergrenze — darunter wird nicht entladen. Höchstens 20 Prozentpunkte unter dem Maximum-Ladestand, damit ein nutzbarer Bereich bleibt |
| **Maximum-Ladestand** | Obergrenze der Planung (Vorgabe 100 = bis voll laden) |
| **Einspeisegrenze** | Maximale Leistung am Netzanschluss, um die herum geplant wird |
| **Alterungskosten der Batterie** | Preis pro umgesetzter kWh — ein zu kleiner Preisunterschied lohnt den Zyklus nicht. Vorgabe 1 ct, im Expertenmodus einstellbar; bei der Entladung in die Gemeinschaft ist der Wert die Schwelle (Vergütung plus Alterungskosten) |
| **Arbeitspreis** | Was der Lieferant je kWh verlangt (inkl. MwSt) — bewertet Strom, der sonst aus dem Netz gekauft werden müsste |
| **Netzbereich** | Bestimmt die Netzgebühr: Das Netznutzungsentgelt der Netzebene 7 kommt aus der Systemnutzungsentgelte-Verordnung (Rechtsinformationssystem des Bundes), täglich aktualisiert. Für Sonderfälle lässt sich die Gebühr auch von Hand eintragen |
| **Zeitvariable Netzentgelte** | Der Sommer-Nieder-Arbeitspreis (SNAP, April–September 10–16 Uhr) und ab 2027 der Winter-Nieder-Arbeitspreis (WiNAP, Oktober–März 22–4 Uhr) senken die Netzgebühr. Voraussetzung ist die viertelstündliche Messung beim Netzbetreiber |

Für die Standardvergütung (der Basistarif — Nachtsatz nur bei der Quelle „Fester Wert") und getrennt davon für die Gemeinschaften lässt sich je ein **Nachtsatz** mit eigenem Nachtfenster hinterlegen.

### Steuerung

Alle 30 Sekunden hält die **Steuerung** den zuletzt gerechneten Fahrplan gegen die Messwerte und setzt ihn am Wechselrichter durch. Rechnen und Steuern sind strikt getrennt: Der Optimierer schreibt nie selbst, nur die Steuerung.

- **Plant der Slot Laden**, wird das Ladelimit auf die Planleistung gesetzt. Die **Ladelimit-Nachführung** hebt es schrittweise an, wenn die gemessene Einspeisung an der Einspeisegrenze klebt (stille Abregelung), und nimmt es mit Hysterese wieder auf den Planwert zurück.
- **Plant der Slot Einspeisung aus der Batterie**, wird eine erzwungene Entladung gestartet. Die **Entlade-Nachführung** rechnet die gemessene Hauslast auf die geplante Netzleistung auf, damit die geplante Einspeisung tatsächlich am Netzanschluss ankommt.
- **Plant der Slot nichts**, bleibt das Laden blockiert — sonst würde der Automatikmodus Überschuss in die Batterie laden, den der Plan einspeisen will. Ist die Batterie ohnehin voll, wird gar nicht eingegriffen: Ein Ladelimit bewirkt dort nichts und stünde nur im Weg.
- **Ist ein Heizstab eingerichtet**, führt die Steuerung ihn im selben Takt nach: geregelt auf „Einspeisung ≈ 0“, gedeckelt auf die geplante Wärme. Der Sollwert geht alle 20 Sekunden an den Ohmpilot, weil dessen Watchdog nach 50 Sekunden abschaltet.

Sicherheitsnetze:

- **Not-Aus:** Netzbezug über 1 kW in drei aufeinanderfolgenden Läufen während einer Entladung → die Entladung wird gestoppt und bis zum nächsten Slotwechsel gesperrt.
- **Failsafe:** Fehlt länger als 15 Minuten ein brauchbarer Fahrplan, wird der Wechselrichter einmalig in den Automatikmodus freigegeben.
- **Totbänder:** Geschrieben wird nur bei relevanter Änderung (> 200 W bzw. ≥ 1 Prozentpunkt Ziel-SOC) — das minimiert Schreibzugriffe.

### Modus

Der Schalter im Dashboard entscheidet, ob gesteuert wird:

| Modus | Rechnen | Schreiben |
|---|---|---|
| **Ein** | jede Minute | Ladelimit und Entladung werden gesetzt |
| **Aus** | jede Minute | nichts — der Fahrplan wird nur angezeigt |

Beim Wechsel von Ein auf Aus werden gesetzte Steuerwerte zurückgenommen: Der Wechselrichter wird freigegeben und läuft wieder in seinem Automatikmodus, es bleibt kein Ladelimit stehen.

**Der vollständige Ablauf eines Steuerungslaufs — mit Diagrammen zum Herzeigen — steht in [docs/steuerung.md](docs/steuerung.md).**

### Einspeisegrenze

Viele Netzbetreiber begrenzen die maximale Einspeiseleistung (z. B. 4 kW). Kennt der Fahrplan diese Grenze, plant er so, dass möglichst nichts abgeregelt wird: Der Überschuss geht bis zur Grenze ins Netz, die Batterie lädt bevorzugt in den Stunden, in denen die Erzeugung über der Grenze liegt. Klebt die gemessene Einspeisung trotzdem an der Grenze, hebt die Ladelimit-Nachführung das Ladelimit an. Details im Einspeisegrenze-Guide im Panel.

## Voraussetzungen

- Home Assistant 2025.1.0 oder neuer
- Einer der oben genannten **Wechselrichter mit Batteriespeicher**. Fronius, Kostal und SMA werden direkt per Modbus TCP angesprochen und brauchen keine weitere Integration; Huawei, Sigenergy und SolaX setzen die jeweilige Integration voraus
- Eine PV-Prognose-Integration (Solcast Solar oder Forecast.Solar)

## Lizenz

MIT
