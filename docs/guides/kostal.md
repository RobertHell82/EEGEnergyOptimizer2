# Kostal Plenticore einrichten

Du brauchst zwei Verbindungen: die **Kostal-Integration** zum Lesen der Messwerte und **Modbus TCP** zum Steuern der Batterie.

_**Modbus TCP** ist die Sprache, mit der Home Assistant im Heimnetz direkt mit dem Wechselrichter redet. Die **IP-Adresse** ist die Adresse des Geräts in deinem Heimnetz._

## 1. Kostal Plenticore Integration in Home Assistant

Die eingebaute Kostal-Integration liest die Sensoren (PV, Batterie, SOC = Ladestand, Netz, Hausverbrauch).

1. **Einstellungen → Geräte & Dienste → Integration hinzufügen → „Kostal Plenticore Solar Inverter"**
2. Gib die IP-Adresse des Wechselrichters an.
3. Passwort: das **Anlagenbetreiber-Passwort** des Kostal-Webservers (dasselbe wie beim Login auf `http://<IP des Wechselrichters>`).
4. Service Code: **leer lassen**.<br>
   _Er ist nur für den Installer-Modus. Der Optimizer liest nur — das Anlagenbetreiber-Passwort genügt._

_**Mehrere Wechselrichter** (z. B. ein zweiter Plenticore ohne Batterie): Füge die Integration für jedes Gerät einzeln hinzu. Die Erkennung nimmt automatisch den Wechselrichter **mit Batterie** als Hauptgerät (SOC, Batterie, Netz). Die PV-Leistung des zweiten Geräts trägt sie als zweiten PV-Sensor ein, damit der Hausverbrauch die ganze Erzeugung enthält._

## 2. Modbus TCP am Wechselrichter aktivieren

Das kannst du als Anlagenbetreiber selbst:

1. Öffne den Kostal-Webserver (`http://<IP des Wechselrichters>`) und melde dich als **Anlagenbetreiber** an.
2. **Einstellungen → Modbus / SunSpec (TCP)** → **Modbus aktivieren**
3. Port: **1502** (Standard)
4. Byte-Reihenfolge: **Little-endian (CDAB)** — Werkseinstellung, nicht ändern!

## 3. Batteriesteuerung auf „Extern über Protokoll" umstellen

Damit der Optimizer die Batterie steuern darf:

1. **Servicemenü → Batterieeinstellungen**
2. Batteriesteuerung: **„Extern über Protokoll (Modbus TCP)"**
3. Watchdog-Timeout: **60 Sekunden** (falls einstellbar)

> [!CAUTION]
> **Nur mit Installateur-Zugang:** Ins Servicemenü kommt nur der Installateur (Master Key vom Typenschild + PARAKO-Servicecode, nur für Fachbetriebe). Frag deinen Installateur / Solarteur — oft geht das per Fernwartung in wenigen Minuten.

> [!NOTE]
> **Eingebauter Failsafe:** Kostal erwartet regelmäßig neue Steuerbefehle (Watchdog). Fallen Optimizer oder Home Assistant aus, kehrt der Wechselrichter nach Ablauf der Zeit von selbst zu seiner eigenen Batterie-Automatik zurück.

> [!WARNING]
> **Nur ein steuerndes System:** Immer nur ein System darf die Batterie über Modbus steuern — also nicht zusätzlich evcc-Batteriesteuerung oder andere Modbus-Steuerungen. Nur lesen (z. B. die Kostal-Integration aus Schritt 1) ist kein Problem.

## 4. Firmware

| Gerät | Firmware |
|---|---|
| **Plenticore G1** | mindestens UI 01.16.05025 / MC 01.44 |
| **Plenticore G2 / G3** | aktuelle Firmware empfohlen |

_Die Version steht im Kostal-Webserver unter „Info". Updates macht der Wechselrichter über das Webinterface._

## 5. Prüfen

1. Unter **Einstellungen → Geräte & Dienste** zeigt Kostal Plenticore **„geladen"** und listet Sensoren.
2. Unter **Entwicklerwerkzeuge → Zustände** suchst du `battery_soc` / `ladezustand` und `home_power`. Die Werte müssen plausibel sein.
3. Kehre in den Assistenten zurück. Die Sensoren werden automatisch erkannt.

## Im Assistenten: Schritt „Wechselrichter“

**„Weiter“** geht erst, wenn alle Punkte stimmen:

1. Wähle **Kostal Plenticore**. Ohne die Kostal-Integration aus Schritt 1 geht es nicht weiter — Modbus allein genügt nicht.
2. **Leistungssensoren** für PV, Batterie und Netz: werden automatisch erkannt, alle drei sind Pflicht.
3. **Modbus IP-Adresse** (Pflicht): die IP des Wechselrichters **mit Batterie**. Sie ist **nicht** vorausgefüllt — bitte eintragen. Port **1502** (nicht 502).
4. Bei **„Weiter“** prüft der Assistent die Modbus-Verbindung (nur lesend). Klappt das nicht: Ist Modbus aktiviert (Schritt 2)? Port 1502? Stimmt die IP?

## Was der Optimizer am Gerät tut

Der Optimizer stellt zwei Register. Beide liegen im flüchtigen Speicher (RAM) — häufiges Schreiben nutzt also nichts ab.

| Register | Wofür |
|---|---|
| **1038** Max. Ladeleistung | Begrenzt das Laden auf die geplante Leistung. 0 W sperrt das Laden ganz; Entladen für das Haus bleibt möglich |
| **1034** Batterie-Sollwert | Erzwungene Entladung ins Netz — positiver Wert, in Watt |

**Sicherheitsnetz (Watchdog):** Der Sollwert muss regelmäßig erneuert werden (empfohlen: 60 Sekunden, Einstellung im Servicemenü). Der Optimizer schreibt alle 25 Sekunden nach. Stürzt Home Assistant während einer Entladung ab, fällt der Wechselrichter nach Ablauf der Zeit in sein eigenes Batteriemanagement zurück.

Einen Ziel-Ladestand kennt Kostal nicht. Der Optimizer prüft den Ladestand alle 30 Sekunden selbst und beendet die Entladung.

**Nach einer Entladung kann die Batterie bis zu 15 Minuten stillstehen.** Der Optimizer schreibt dann nichts, bis der Wechselrichter von selbst in sein eigenes Batteriemanagement zurückgefallen ist (höchstens 15 Minuten). Zeigt die Statuskarte in dieser Zeit „Normalbetrieb“ und die Batterie steht still, ist das kein Fehler.

> **Teil-Ladelimits** (der Normalfall) prüft der Optimizer beim ersten Mal nach. Weicht der zurückgelesene Wert stark ab, steht eine Warnung im Protokoll — dann melde dich bitte mit der Firmware-Version.

## Häufige Probleme

| Problem | Lösung |
|---|---|
| **Modbus Connection refused** | Modbus TCP nicht aktiviert oder falscher Port → Schritt 2 wiederholen (Port 1502, nicht 502) |
| **Steuerbefehle ohne Wirkung** | Batteriesteuerung steht noch auf „Intern" → Schritt 3 (Installateur) |
| **Keine Kostal-Sensoren in HA** | Falsches Passwort — gebraucht wird das Anlagenbetreiber-Passwort des Webservers, nicht der Master Key |
| **Alle Werte 0 oder unsinnig** | Byte-Reihenfolge verstellt → auf Little-endian (Werkseinstellung) zurücksetzen |
| **Steuerung reagiert kurz nach einer Netzstörung nicht** | Nach dem Wiederzuschalten fährt der Wechselrichter ca. 10 Minuten langsam hoch (länderabhängig, z. B. Österreich). In dieser Zeit sind Steuerbefehle gesperrt; der Optimizer wiederholt sie automatisch |
