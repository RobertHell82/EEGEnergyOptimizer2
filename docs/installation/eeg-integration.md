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
3. In der Seitenleiste erscheint der Eintrag **EEG Optimizer** — das Panel führt dich durch die restliche Einrichtung:
   1. Voraussetzungsprüfung
   2. Wechselrichtertyp wählen + automatische Sensorerkennung
   3. Batterie- & PV-Sensoren zuordnen
   4. Prognosequelle wählen (Solcast / Forecast.Solar)
   5. Fahrplan-Einstellungen (Einspeisevergütung, Bezugspreis, Mindest- und Maximum-Ladestand, Alterungskosten, Batterie-Leistungsgrenze; PeakShare-Community optional)
   6. Einspeisegrenze (optional)
   7. Wechselrichter-Verbindungstest

## Voraussetzungen für den Betrieb

- Home Assistant **2025.1.0** oder neuer
- Ein **Huawei SUN2000** mit Batteriespeicher und die [Huawei Solar Integration](../guides/huawei.md), eingerichtet und funktionsfähig — andere Wechselrichter werden derzeit nicht unterstützt ([Stand der Unterstützung](../wechselrichter-status.md))
- Eine **PV-Prognose-Integration**:
  - [Solcast Solar](../guides/solcast.md) (empfohlen)
  - [Forecast.Solar](../guides/forecast_solar.md)

Die Wechselrichter- und Prognose-Anleitungen sind auch direkt im Einrichtungsassistenten über die „Anleitung"-Buttons erreichbar.

## Updates

Updates erscheinen automatisch in HACS bzw. unter **Einstellungen → Geräte & Dienste → Updates**, sobald eine neue Version veröffentlicht wird. Nach einem Update Home Assistant neu starten.
