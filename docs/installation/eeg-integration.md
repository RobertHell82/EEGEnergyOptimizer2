# EEG Energy Optimizer über HACS installieren

Du installierst den EEG Energy Optimizer als **benutzerdefiniertes Repository** (Custom Repository) über HACS.

> [!NOTE]
> **Voraussetzung:** [HACS muss installiert sein](hacs.md).

## 1. Repository in HACS hinzufügen

1. Öffne **HACS** in der Seitenleiste
2. Klicke oben rechts auf das **Drei-Punkte-Menü → Benutzerdefinierte Repositories**
3. Trage ein:
   - **Repository:** `https://github.com/RobertHell82/EEGEnergyOptimizer2`
   - **Typ:** `Integration`
4. Klicke auf **Hinzufügen** und schließe den Dialog

## 2. Integration herunterladen

1. Suche in HACS nach **„EEG Energy Optimizer"**
2. Öffne den Eintrag und klicke auf **Herunterladen**
3. **Starte Home Assistant neu** (Einstellungen → System → Power-Symbol → Neu starten)

## 3. Integration einrichten

1. Gehe zu **Einstellungen → Geräte & Dienste → Integration hinzufügen**
2. Suche nach **„EEG Energy Optimizer"** und füge ihn hinzu
3. In der Seitenleiste erscheint **EEG Energy Optimizer** — dort startet der Einrichtungsassistent.

## 4. Einrichtungsassistent

Der Assistent hat sieben Schritte. In Schritt 1–3 ordnest du einmalig die Sensoren zu. Schritt 4–6 sind Einstellungen, die du später im Panel unter **Einstellungen** jederzeit ändern kannst.

| Schritt | Was abgefragt wird |
|---|---|
| **1. Willkommen** | Was du brauchst. Ob die nötigen Integrationen installiert sind, prüfen die Schritte Wechselrichter und PV-Prognose (mit „Erneut prüfen“, falls du eine nachinstallierst) |
| **2. Wechselrichter** | Typ wählen; je nach Gerät automatische Sensorerkennung oder Verbindungsprüfung per Modbus. Dazu die Sensoren für PV-, Batterie- und Netzleistung |
| **3. Batterie** | Ladestand und Kapazität — als Sensor oder von Hand eingetragen |
| **4. PV-Prognose** | Quelle: Solcast, Forecast.Solar oder **eigene Berechnung** (Flächen mit kWp, Neigung und Ausrichtung, ohne Konto). Optional der Prognosevergleich: eine zweite Quelle läuft zum Vergleich mit |
| **5. Anlage & Batterie** | Pflicht: AC-Grenzleistung des Wechselrichters, PV-Spitzenleistung, Batterie-Leistungsgrenze, Mindest-Ladestand. Dazu Einspeisegrenze des Netzbetreibers und Maximum-Ladestand |
| **6. Tarife & Gemeinschaft** | Standardvergütung (fester Wert, OeMAG, Energie AG, aWATTar SUNNY oder Börsen-Spotpreis), Arbeitspreis und Netzbereich; optional bis zu zwei Energiegemeinschaften — mit PeakShare-Bedarfsprognose oder fester Abnahmequote |
| **7. Zusammenfassung** | Überblick, Haken **„Steuerung nach dem Fertigstellen einschalten“**, dann **„Fertig“** |

> [!IMPORTANT]
> **Gesteuert wird nur im Modus „Ein“.** Mit dem Haken in der Zusammenfassung (vorausgewählt) schaltet der Assistent ihn beim Fertigstellen ein. Ohne Haken bleibt er auf **„Aus“**: Der Fahrplan wird berechnet und angezeigt, aber nichts an den Wechselrichter geschrieben. Umschalten kannst du jederzeit oben im Dashboard.

Bei Wechselrichter, PV-Prognose, Anlage & Batterie und Tarife & Gemeinschaft (bei Huawei auch Batterie) öffnet der Button **„Anleitung"** die passende Hilfe. Die Felder erklären [Anlage & Batterie](../guides/anlage_batterie.md) und [Tarife & Gemeinschaft](../guides/tarife.md), die Bedienung [Dashboard & Bedienung](../guides/dashboard.md).

## Voraussetzungen für den Betrieb

- Home Assistant **2025.1.0** oder neuer
- Einer der sechs unterstützten **Wechselrichter mit Batteriespeicher** — welche und wie gut erprobt, steht im [Stand der Unterstützung](../wechselrichter-status.md). Jeder braucht seine Integration in Home Assistant, eingerichtet und funktionsfähig (siehe die Guides dort):
  - **Fronius, Kostal Plenticore, SMA Solar:** in Home Assistant eingebaut, liefern die Messwerte. Gesteuert werden sie direkt per Modbus TCP — das muss am Wechselrichter eingeschaltet sein.
  - **Huawei Solar, Sigenergy, SolaX Modbus:** kommen über HACS und übernehmen Messen und Steuern.
- Eine **PV-Prognose**, eine von drei:
  - [Solcast Solar](../guides/solcast.md) — kostenloses Konto nötig, am genauesten
  - [Forecast.Solar](../guides/forecast_solar.md) — ohne Registrierung
  - [Eigene Berechnung](../guides/prognose_eigen.md) — ohne Konto und ohne weitere Integration, braucht nur den richtigen Standort in Home Assistant

## Updates

Updates erscheinen automatisch in HACS und oben unter **Einstellungen**. Nach einem Update Home Assistant neu starten.
