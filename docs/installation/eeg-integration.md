# EEG Energy Optimizer über HACS installieren

Der EEG Energy Optimizer wird als **benutzerdefiniertes Repository** (Custom Repository) über HACS installiert.

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
3. In der Seitenleiste erscheint der Eintrag **EEG Energy Optimizer** — er öffnet den Einrichtungsassistenten.

## 4. Einrichtungsassistent

Der Assistent hat sieben Schritte. Die ersten drei ordnen einmalig die Sensoren zu, Schritt 4 bis 6 sind die Einstellungen, die du später unter **Einstellungen** im Panel jederzeit wieder ändern kannst.

| Schritt | Was abgefragt wird |
|---|---|
| **1. Willkommen** | Prüft, ob die nötigen Integrationen installiert sind (je nach Wechselrichter und Prognosequelle) |
| **2. Wechselrichter** | Typ wählen; je nach Gerät automatische Sensorerkennung oder Verbindungsprüfung per Modbus. Dazu die Sensoren für PV-, Batterie- und Netzleistung |
| **3. Batterie** | Sensor für den Ladestand und die Kapazität — als Sensor oder von Hand eingetragen |
| **4. PV-Prognose** | Quelle wählen: Solcast, Forecast.Solar oder die **eigene Berechnung** (Flächen der Anlage mit kWp, Neigung und Ausrichtung, ohne Konto). Optional der Prognosevergleich, bei dem die zweite Quelle zum Vergleich mitläuft |
| **5. Anlage & Batterie** | AC-Grenzleistung des Wechselrichters und PV-Spitzenleistung (beide Pflicht), Einspeisegrenze des Netzbetreibers, Batterie-Leistungsgrenze, Mindest- und Maximum-Ladestand |
| **6. Tarife & Gemeinschaft** | Standardvergütung (fester Wert, OeMAG, Energie AG, aWATTar SUNNY oder Börsen-Spotpreis), Arbeitspreis und Netzbereich; optional bis zu zwei Energiegemeinschaften — mit PeakShare-Bedarfsprognose oder fester Abnahmequote |
| **7. Zusammenfassung** | Alles noch einmal im Überblick, dann speichern |

Bei den Schritten Wechselrichter, PV-Prognose und Anlage & Batterie (bei Huawei auch Batterie) gibt es einen **„Anleitung"-Button**, der die passende Hilfe direkt im Panel öffnet.

## Voraussetzungen für den Betrieb

- Home Assistant **2025.1.0** oder neuer
- Einer der sechs unterstützten **Wechselrichter mit Batteriespeicher** — welche das sind und wie weit sie erprobt sind, steht im [Stand der Unterstützung](../wechselrichter-status.md). Fronius, Kostal und SMA werden direkt per Modbus TCP angesprochen; Huawei, Sigenergy und SolaX brauchen die jeweilige Integration, eingerichtet und funktionsfähig (siehe die Guides dort)
- Eine **PV-Prognose**, eine von drei:
  - [Solcast Solar](../guides/solcast.md) — kostenloses Konto nötig, am genauesten
  - [Forecast.Solar](../guides/forecast_solar.md) — ohne Registrierung
  - [Eigene Berechnung](../guides/prognose_eigen.md) — ohne Konto und ohne weitere Integration, braucht nur den richtigen Standort in Home Assistant

## Updates

Updates erscheinen automatisch in HACS und oben unter **Einstellungen**, sobald eine neue Version veröffentlicht wird. Nach einem Update Home Assistant neu starten.
