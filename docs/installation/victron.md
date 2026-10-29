# Victron vorbereiten

> [!IMPORTANT]
> **Victron läuft als Feldtest.** Der Treiber steuert eine Victron-ESS-Anlage über die in Home Assistant eingebaute Integration **Victron GX**. Er ist an der ersten Anlage in Erprobung. Was nachgewiesen und was noch offen ist, steht im [Stand der Unterstützung](../wechselrichter-status.md#victron-ess).

Mit dieser Anleitung bereitest du Anlage und Home Assistant vor, bevor du den EEG Energy Optimizer einrichtest. Die ausführliche Anleitung mit allen Einstellungen und Fehlerbildern ist [Victron einrichten](../guides/victron.md). Sie steht auch im Einrichtungsassistenten hinter dem Victron-Eintrag.

Geprüft mit einer Anlage aus drei **Multiplus-II** (dreiphasig) und einem **Cerbo GX MK2**. Andere GX-Geräte (Venus GX, Ekrano GX) und Quattro funktionieren gleich.

## 1. Am GX-Gerät

Öffne die Oberfläche des GX-Geräts, entweder direkt am Touch-Display oder über **VRM → Remote Console**.

| Einstellung | Wo | Soll |
|---|---|---|
| **Firmware** | *Einstellungen → Firmware* | **Venus OS 3.31 oder neuer** (mit älteren Ständen wurden Probleme bei der MQTT-Anmeldung berichtet) |
| **MQTT** | *Einstellungen → Integrationen → MQTT-Zugriff* (ältere Firmware: *Einstellungen → Dienste → MQTT*) | **Eingeschaltet** |
| **Feste IP-Adresse** | Router (z. B. FRITZ!Box: *Heimnetz → Netzwerk → Gerät → „Immer die gleiche IPv4-Adresse zuweisen“*) | Das GX-Gerät behält seine Adresse |

Sieh außerdem unter *Einstellungen → Allgemein → Sicherheitsprofil lokales Netzwerk* nach, welches Profil eingestellt ist und welches Passwort dazu gehört. Davon hängt ab, was du in Abschnitt 3 einträgst.

Dazu die ESS-Einstellungen, die der Einrichtungsassistent später prüft: **ESS-Modus „Optimiert“**, **Dynamic ESS aus**, **Geplantes Laden aus**, **DVCC an**, Einspeisegrenze **nicht begrenzt oder mindestens 100 W**, empfohlen **„DC-gekoppelte PV – Überschuss einspeisen“ an**. Wo du sie findest und warum sie nötig sind, steht in [Victron einrichten, Abschnitt 4](../guides/victron.md#4-einstellungen-am-gx-gerät).

> [!NOTE]
> MQTT einzuschalten ändert nichts am Betrieb der Anlage. Es ist nur ein Zugang im Heimnetz.

## 2. Home Assistant prüfen

Die Integration **Victron GX** ist in Home Assistant eingebaut. Sie bekommt die Messwerte direkt im Heimnetz über MQTT, ohne die VRM-Cloud, und nimmt auch die Steuerbefehle an. Es ist nichts zu installieren, sie braucht aber **Home Assistant 2026.5 oder neuer** (*Einstellungen → Info*). Bei einer älteren Version zuerst Home Assistant aktualisieren.

> [!TIP]
> Unter „Victron“ zeigt Home Assistant eventuell mehrere Einträge. Gemeint ist **„Victron GX“**, nicht „Victron Remote Monitoring“ (die VRM-Cloud) und nicht „Victron BLE“.

## 3. Integration einrichten

1. Gehe zu **Einstellungen → Geräte & Dienste**. Oft schlägt Home Assistant das GX-Gerät oben unter **„Gefunden“** schon vor, dann klicke dort auf **Konfigurieren**
2. Sonst: **„+ Integration hinzufügen“** → nach **„Victron GX“** suchen und auswählen
3. Trage ein, passend zum Sicherheitsprofil aus Abschnitt 1:

| Feld | Profil „Unsicher“ | Profil „Schwach“ | Profil „Gesichert“ |
|---|---|---|---|
| **Host** | IP-Adresse des GX-Geräts | IP-Adresse des GX-Geräts | IP-Adresse des GX-Geräts |
| **Port** | **1883** | **1883** (oder 8883 mit SSL) | **8883** |
| **Benutzername** | leer | leer | leer |
| **Passwort** | leer | Passwort des Profils | Passwort des Profils |
| **SSL** | aus | aus (an bei 8883) | **an** |

4. Fragt Home Assistant nach einer **Kopplung**: am GX-Gerät *Einstellungen → Integrationen → MQTT-Geräte → Kopplungsmodus* öffnen und innerhalb von **120 Sekunden** in Home Assistant auf **Absenden** klicken

> [!WARNING]
> **„Ungültige Authentifizierung“ heißt: Das GX-Gerät lehnt das Passwort ab.** Der Benutzername wird nicht geprüft, ihn zu ändern hilft nicht. Setze das Passwort am GX-Gerät neu (*Einstellungen → Allgemein → Sicherheitsprofil lokales Netzwerk → Passwort ändern*), zum Testen ohne Sonderzeichen, und trage genau dieses ein. Das VRM-Passwort gilt hier nicht. Alternativ über die Kopplung (Schritt 4) einrichten, die kein Passwort braucht.

> [!WARNING]
> **Bitte keine Einstellungen über Home Assistant verändern.** Die Integration legt neben den Messwerten auch Entitäten an, mit denen man die Anlage verstellen kann (etwa Netzsollwert, Ladestrom oder ESS-Modus). Netzsollwert und Ladestrom schreibt später der EEG Energy Optimizer selbst.

### Prüfen

Unter **Einstellungen → Geräte & Dienste → Victron GX** sollten mehrere Geräte erscheinen, typischerweise **GX Device**, **ESS**, der Multiplus (VE.Bus), die **Batterie**, der **Netzzähler** und je nach Anlage ein **Solarcharger** je MPPT-Laderegler oder ein **PV-Inverter**.

Fehlt der Netzzähler, erkennt der Einrichtungsassistent die Netzleistung nicht. Dann musst du sie selbst zuordnen.

## 4. Was wir im Feldtest von dir brauchen

Solange Victron im Feldtest läuft, schauen wir uns die ersten Tage gemeinsam an:

1. **Lesezugriff auf Home Assistant**, damit wir Messwerte und Steuerung mitverfolgen können:
   - die Adresse, unter der Home Assistant erreichbar ist
   - einen **langlebigen Zugriffstoken**: Klicke links unten auf deinen **Benutzernamen** → Reiter **Sicherheit** → ganz unten **„Langlebige Zugriffstoken“ → Token erstellen**, Name z. B. „EEG Feldtest“. Den Token zeigt Home Assistant nur einmal an. Kopiere ihn und schick ihn uns auf einem sicheren Weg, nicht in einen öffentlichen Chat
2. **Wo die PV angeschlossen ist:** an Victron-Ladereglern (MPPT, Gleichstrom) oder an einem eigenen PV-Wechselrichter am Wechselstrom
3. **Die nutzbare Kapazität der Batterie** in kWh. Victron meldet sie nur in Ah, im Assistenten wird sie von Hand eingetragen (z. B. 4 × Pytes V5 = 20,48 kWh)

> [!TIP]
> Den Token kannst du nach dem Feldtest an derselben Stelle wieder löschen. Dann ist der Zugriff sofort beendet.

## Wie es danach weitergeht

1. [EEG Energy Optimizer installieren](eeg-integration.md) und den Einrichtungsassistenten starten. Im Schritt „Wechselrichter“ wird Victron erkannt, die Prüfung der GX-Einstellungen erscheint darunter
2. Gemeinsam mit dir an der Anlage: ein kurzer **Handtest**. Wir begrenzen einmal das Laden und speisen einmal 1 kW aus der Batterie ein, beides nur für wenige Minuten, und prüfen, dass die Freigabe alles zurücknimmt
3. Danach läuft die Steuerung im Modus **Ein**, und wir beobachten die ersten Tage mit
