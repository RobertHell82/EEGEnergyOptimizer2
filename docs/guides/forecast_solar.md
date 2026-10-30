# Forecast.Solar einrichten

## 1. Integration hinzufügen

1. Gehe zu **Einstellungen → Geräte & Dienste → Integration hinzufügen**
2. Suche nach **„Forecast.Solar"**
3. Klicke auf **Forecast.Solar**

## 2. Anlagendaten eingeben

| Feld | Wert |
|---|---|
| **Name** | Frei wählbar, z.B. „PV Süd" |
| **Latitude / Longitude** | Kommt aus der HA-Konfiguration — prüfen, nicht ändern |
| **Dachneigung (Declination)** | Neigung in Grad. Typisch: **30–35°**, Flachdach: 0–10° |
| **Azimuth** | Himmelsrichtung der Module:<br>**0° = Nord**, 90° = Ost, **180° = Süd**, 270° = West<br>_Achtung: HA zählt wie ein Kompass (0 = Nord), nicht wie die Forecast.Solar-Website (0 = Süd)!_ |
| **Modulleistung** | Leistung der Module **in Watt**, nicht kWp!<br>Beispiel: 10 kWp → **10000** |

_**Tipp:** Die häufigsten Fehler sind ein falscher Azimuth (Süd = 180°, nicht 0°) und kWp statt Watt._

_Einen **API-Key** (Zugangsschlüssel) brauchst du nicht. Hast du einen bezahlten Zugang, trägst du ihn danach unter **Konfigurieren** ein._

## 3. Mehrere Ausrichtungen (Ost/West)

Füge die Integration **einmal pro Ausrichtung** hinzu, z.B. „PV Ost" mit 90° und „PV West" mit 270°.

_Die zweite bekommt Sensoren mit `_2` am Ende (z.B. `sensor.energy_production_tomorrow_2`). Der Fahrplan zählt alle zusammen. Die Anzeigen „PV-Prognose heute / morgen“ zeigen aber nur den Sensor, den du im Assistenten gewählt hast — also nur diese Fläche._

## 4. Prüfen

1. Warte 1–2 Minuten
2. Suche unter **Entwicklerwerkzeuge → Zustände** nach `energy_production`
3. `sensor.energy_production_today_remaining` und `sensor.energy_production_tomorrow` sollten kWh-Werte zeigen
4. Kehre hierher zurück — die Sensoren werden automatisch zugeordnet

_**Kostenlose Version:** 12 Abrufe pro Stunde, Prognose für heute und morgen, stündlich — das reicht. Weil die Prognose mit morgen endet, plant der Fahrplan nur bis Ende morgen statt 48 Stunden; am Abend sind das rund 26 Stunden._
