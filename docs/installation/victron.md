# Victron vorbereiten (Machbarkeitsprüfung)

> [!IMPORTANT]
> **Victron wird noch nicht unterstützt.** Mit dieser Anleitung bereitest du deine Anlage darauf vor, dass wir prüfen können, ob und wie der EEG Energy Optimizer sie steuern kann. Dabei wird nur **gelesen**, am Wechselrichter wird nichts verstellt.

Geprüft wird mit einer Anlage aus **Multiplus-II** und **Cerbo GX**. Andere GX-Geräte (Venus GX, Ekrano GX) funktionieren gleich.

## 1. Am Cerbo GX prüfen

Öffne die Oberfläche des Cerbo GX, entweder direkt am Touch-Display oder über **VRM → Remote Console**.

| Einstellung | Wo | Soll |
|---|---|---|
| **MQTT** | *Einstellungen → Integrationen → MQTT-Zugriff* (ältere Firmware: *Einstellungen → Dienste → MQTT*) | **Eingeschaltet** |
| **Feste IP-Adresse** | Router (z. B. FRITZ!Box: *Heimnetz → Netzwerk → Gerät → „Immer dieselbe IPv4-Adresse zuweisen“*) | Der Cerbo behält seine Adresse |

Sieh außerdem unter *Einstellungen → Allgemein → Sicherheitsprofil lokales Netzwerk* nach, welches Profil eingestellt ist. Davon hängt ab, was du im Abschnitt 3 einträgst. Empfohlen ist **„Schwach“** oder **„Sicher“** mit Passwort.

Notiere dir die **IP-Adresse** des Cerbo, du brauchst sie im Abschnitt 3.

> [!NOTE]
> MQTT einzuschalten ändert nichts am Betrieb der Anlage. Es ist nur ein Zugang im Heimnetz.

## 2. Home Assistant prüfen

Wir verwenden die in Home Assistant **eingebaute** Integration **Victron GX**. Sie bekommt die Messwerte direkt im Heimnetz über MQTT, ohne die VRM-Cloud. Es ist nichts zu installieren, sie braucht aber **Home Assistant 2026.5 oder neuer** (*Einstellungen → Info*). Bei einer älteren Version zuerst Home Assistant aktualisieren.

> [!TIP]
> Unter „Victron“ zeigt Home Assistant eventuell mehrere Einträge. Gemeint ist **„Victron GX“**, nicht „Victron Remote Monitoring“ (die VRM-Cloud) und nicht „Victron BLE“.

## 3. Integration einrichten

1. Gehe zu **Einstellungen → Geräte & Dienste → „+ Integration hinzufügen“**. Oft schlägt Home Assistant den Cerbo dort schon als **gefundenes Gerät** vor, dann klicke auf **Konfigurieren**
2. Sonst: nach **„Victron GX“** suchen und auswählen
3. Trage ein, passend zum Sicherheitsprofil aus Abschnitt 1:

| Feld | Profil „Unsicher“ | Profil „Schwach“ oder „Sicher“ |
|---|---|---|
| **Host** | IP-Adresse des Cerbo | IP-Adresse des Cerbo |
| **Port** | **1883** | **8883** |
| **Benutzername** | leer | leer |
| **Passwort** | leer | das Passwort, das am Cerbo für das Profil gesetzt ist |
| **SSL** | aus | an |

4. Fragt Home Assistant nach einer **Kopplung**: am Cerbo *Einstellungen → Integrationen → MQTT-Geräte → Kopplungsmodus* öffnen und innerhalb von **120 Sekunden** in Home Assistant auf **Absenden** klicken

> [!WARNING]
> **Bitte keine Einstellungen über Home Assistant verändern.** Die Integration legt neben den Messwerten auch Entitäten an, mit denen man den Multiplus verstellen kann (etwa Netzsollwert, Ladestrom oder ESS-Modus). Für die Prüfung brauchen wir nur die Messwerte. Steuern wird später der EEG Energy Optimizer selbst, über dieselbe Integration.

### Prüfen

Unter **Einstellungen → Geräte & Dienste → Victron GX** sollten mehrere Geräte erscheinen, typischerweise **System**, **VE.Bus** (der Multiplus), **Battery** und je nach Anlage **Solarcharger** (MPPT), **Grid** (Netzzähler) oder **PV-Inverter**. Wenn ein Gerät fehlt, das bei dir verbaut ist, sag uns bitte Bescheid.

## 4. Was wir von dir brauchen

Für die Prüfung schick uns bitte:

1. **Fotos oder Screenshots vom Cerbo GX:**
   - *Einstellungen → ESS*: die ganze Seite (Modus, Netzzähler, Mindest-Ladestand, Netzeinspeisung)
   - *Einstellungen → DVCC*: die ganze Seite
2. **Wo die PV angeschlossen ist:** an einem Victron-Laderegler (MPPT, Gleichstrom) oder an einem eigenen PV-Wechselrichter am Wechselstrom (z. B. Fronius am AC-Ausgang des Multiplus)?
3. **Ob ein Netzzähler** (z. B. Carlo Gavazzi ET340/EM24) verbaut ist
4. **Lesezugriff auf Home Assistant**, damit wir die Messwerte prüfen können:
   - Die Adresse, unter der Home Assistant erreichbar ist
   - Einen **langlebigen Zugriffstoken**: Klicke links unten auf deinen **Benutzernamen** → Reiter **Sicherheit** → ganz unten **„Langlebige Zugriffstoken“ → Token erstellen**, Name z. B. „EEG Prüfung“. Den Token zeigt Home Assistant nur einmal an. Kopiere ihn und schick ihn uns auf einem sicheren Weg, nicht in einen öffentlichen Chat.

> [!TIP]
> Den Token kannst du nach der Prüfung an derselben Stelle wieder löschen. Dann ist der Zugriff sofort beendet.

## Wie es danach weitergeht

1. Wir prüfen die Messwerte: Vorzeichen, Einheiten, Netzzähler, Batterie
2. Gemeinsam mit dir an der Anlage: ein kurzer **Handtest**. Wir begrenzen einmal das Laden und speisen einmal 1 kW aus der Batterie ein, beides nur für wenige Minuten, und nehmen danach alles wieder zurück
3. Wenn das klappt, bauen wir den Victron-Treiber und testen ihn an deiner Anlage im Feldtest
