# Inbetriebnahme deines EEG-Geräts (Home Assistant Green)

Dein Home Assistant Green wurde bereits vorbereitet: Alle benötigten Programme,
der EEG Energy Optimizer und der Fernzugang sind installiert. Diese Anleitung
führt dich durch die wenigen Schritte, bis dein System läuft.

---

## Schritt 1: Gerät anschließen (Strom & Netzwerk)

1. **Netzwerk:** Verbinde das **Netzwerkkabel** mit dem Gerät und einem freien
   LAN-Port deines Routers (oder einer Netzwerkdose in deinem Heimnetz).
2. **Strom:** Verbinde das **Netzteil** mit dem Gerät und der Steckdose. Das
   Gerät startet automatisch.
3. **Warten:** Der erste Start dauert einige Minuten — warte, bis die Status-LED
   ruhig leuchtet (nicht mehr blinkt).

> [!TIP]
> Das Gerät bezieht seine Netzwerkadresse **automatisch** vom Router (DHCP). Am
> Router musst du nichts einstellen.

---

## Schritt 2: Erste Anmeldung

1. Öffne auf einem PC, Tablet oder Handy **im selben Netzwerk** einen Browser.
2. Rufe **`http://homeassistant.local:8123`** auf — das ist die
   **Home-Assistant-Oberfläche** deines Geräts. Alle weiteren Schritte dieser
   Anleitung finden dort statt.
3. Melde dich auf der **Anmeldeseite** mit dem **Benutzernamen und Passwort** an,
   die du von uns erhalten hast.

> [!NOTE]
> Funktioniert `homeassistant.local` nicht, findest du die IP-Adresse des Geräts
> in der Geräteliste deines Routers und rufst sie direkt auf, z.B.
> `http://192.168.1.50:8123`.

---

## Schritt 3: Basiseinrichtung

| Einstellung | Wo | Was |
|---|---|---|
| **Standort** (wichtigster Schritt!) | Einstellungen → System → Allgemein | Ab Werk auf **„Linz Hauptplatz"** voreingestellt — **unbedingt auf deine eigene Adresse ändern** (auf der Karte oder per Koordinaten), Höhe & Zeitzone prüfen. Ohne korrekten Standort berechnet der Optimizer Sonnenauf-/-untergang und PV-Prognose für den falschen Ort. |
| **Passwort ändern** (Benutzer `ewa-mitglied`) | Profil (Name unten links) → *Sicherheit* → *Passwort ändern* | Voreingestelltes Passwort durch ein eigenes ersetzen |

---

## Schritt 4: PV-Prognose

Der Optimizer braucht eine PV-Prognose für deine Anlage. Du hast drei Möglichkeiten:

| Quelle | Aufwand | Anleitung |
|---|---|---|
| **Solcast** (empfohlen) | eigenes, kostenloses Konto; dort die PV-Anlage erfassen und den API-Key in Home Assistant eintragen | [Solcast Solar einrichten](../guides/solcast.md) |
| **Eigene Berechnung** | kein Konto — du trägst im Assistenten nur die Flächen deiner Anlage ein (kWp, Neigung, Ausrichtung) | [Eigene PV-Prognose](../guides/prognose_eigen.md) |
| **Forecast.Solar** | ohne Registrierung, muss als Integration hinzugefügt werden | [Forecast.Solar einrichten](../guides/forecast_solar.md) |

> [!NOTE]
> Solcast ist am genauesten, weil es morgens das aktuelle Satellitenbild
> einrechnet — das zeigt sich vor allem an Nebeltagen. Die eigene Berechnung
> ist der schnellste Weg ohne Konto. Du kannst später beide nebeneinander
> laufen lassen und im Dashboard vergleichen (Prognosevergleich).

---

## Schritt 5: Wechselrichter anbinden

Damit der Optimizer deinen Speicher steuern kann, wird er mit deinem
Wechselrichter verbunden. Unterstützt werden **Fronius Gen24, Huawei SUN2000,
Kostal Plenticore, Sigenergy SigenStor, SMA Smart Energy und SolaX Gen4+**:

| Wechselrichter | Anleitung |
|---|---|
| **Fronius Gen24** | [Fronius einrichten](../guides/fronius.md) |
| **Huawei SUN2000** | [Huawei Solar einrichten](../guides/huawei.md) + [Akkukapazität-Sensor](../guides/capacity_sensor.md) |
| **Kostal Plenticore** | [Kostal einrichten](../guides/kostal.md) |
| **Sigenergy SigenStor** | [Sigenergy einrichten](../guides/sigenergy.md) |
| **SMA Smart Energy** | [SMA einrichten](../guides/sma.md) |
| **SolaX Gen4+** | [SolaX Modbus einrichten](../guides/solax.md) |

Wie weit jedes Gerät erprobt ist, steht im
[Stand der Unterstützung](../wechselrichter-status.md).

---

## Schritt 6: EEG Energy Optimizer fertig einrichten

Zum Schluss verbindest du den Optimizer mit deiner Anlage:

1. Öffne **Home Assistant im Browser** (gleiche Adresse wie in Schritt 2:
   `http://homeassistant.local:8123`).
2. Klicke in der **Seitenleiste links** auf den Eintrag **„EEG Energy Optimizer"** —
   das öffnet den Einrichtungsassistenten.

Der Assistent führt dich in sieben Schritten durch: Willkommen · Wechselrichter ·
Batterie · PV-Prognose · Anlage & Batterie · Tarife & Gemeinschaft ·
Zusammenfassung. Was in jedem Schritt abgefragt wird, steht in der
[Installationsanleitung](../installation/eeg-integration.md#4-einrichtungsassistent).

> [!TIP]
> Bei den Schritten Wechselrichter, PV-Prognose und Anlage & Batterie (bei
> Huawei auch Batterie) gibt es einen **„Anleitung"-Button**, der die passende Hilfe direkt im Panel
> anzeigt.

---

## Fertig 🎉

Wenn alle Schritte erledigt sind, läuft der EEG Energy Optimizer und steuert
deinen Speicher nach den **Einspeisepreisen**: Er speist ein, wenn eine
Kilowattstunde gerade mehr wert ist — etwa weil deine Energiegemeinschaft dann
Bedarf hat — und lädt oder hält, wenn sie weniger wert ist. Den Fahrplan und
den Status siehst du jederzeit im Panel **EEG Energy Optimizer**.

> [!NOTE]
> **Ohne Preisunterschied passiert nichts.** Feste Zeitfenster gibt es nicht,
> und es gibt auch keine automatische Entladung am Abend oder in der Nacht.
> Lohnt sich das Verschieben laut Preisen nicht, bleibt die Batterie, wo sie
> ist — das ist kein Fehler, sondern das erwartete Verhalten.

> [!NOTE]
> **Einlaufzeit — mindestens eine Woche laufen lassen:** Der Optimizer lernt das
> Verbrauchsprofil deines Haushalts aus den aufgezeichneten Daten — stundenweise,
> getrennt nach Werktag und Wochenende/Feiertag, aus den letzten vier Wochen.
> Direkt nach der Inbetriebnahme sind noch keine Verbrauchsdaten vorhanden; nach
> **etwa einer Woche** Dauerbetrieb liegen für beide Gruppen genug Werte vor,
> und mit jeder weiteren Woche werden die Prognosen (und damit die Lade- und
> Entladeentscheidungen) genauer. Lass das Gerät daher durchgehend laufen und
> beurteile das Verhalten des Optimizers frühestens nach einer Woche.

> [!TIP]
> Auf deinem Gerät ist ein **Fernzugang für die EEG** eingerichtet, damit wir dich
> beim Setup unterstützen können. Sobald alles läuft, kannst du ihn bei Bedarf
> deaktivieren.
