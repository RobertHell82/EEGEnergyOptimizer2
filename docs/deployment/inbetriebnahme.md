# Inbetriebnahme deines EEG-Geräts (Home Assistant Green)

Dein Home Assistant Green wurde bereits vorbereitet: Alle benötigten Programme,
der EEG Energy Optimizer und der Fernzugang sind installiert. Diese Anleitung
führt dich durch die wenigen Schritte, bis dein System läuft.

Du brauchst dafür dein **Begleitschreiben** — darauf stehen die Adresse deines
Geräts, der Benutzername und das Passwort.

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

1. Öffne auf einem PC, Tablet oder Handy einen Browser.
2. Rufe die **Adresse von deinem Begleitschreiben** auf. Sie hat die Form
   **`https://<deine-geräte-id>.ew-ansfelden.cc`** — die Geräte-ID ist eine lange,
   zufällige Zeichenfolge, die nur dein Gerät hat. Am einfachsten tippst du sie
   genau so ab, wie sie auf dem Blatt steht.
   Das ist die **Home-Assistant-Oberfläche** deines Geräts; alle weiteren Schritte
   dieser Anleitung finden dort statt. Die Adresse funktioniert **von überall**,
   auch unterwegs übers Handynetz — lege dir am besten ein Lesezeichen an.
3. Melde dich auf der **Anmeldeseite** mit dem **Benutzernamen und Passwort** vom
   Begleitschreiben an.

> [!NOTE]
> **Adresse geht nicht?** Bitte ein paar Minuten nach dem Einschalten warten — der
> Fernzugang startet als Letztes. Klappt es dann noch immer nicht, erreichst du
> das Gerät auch direkt im Heimnetz: **`http://homeassistant.local:8123`**, oder
> über seine IP-Adresse aus der Geräteliste deines Routers, z.B.
> `http://192.168.1.50:8123` (PC oder Handy müssen dafür im selben WLAN bzw.
> Netzwerk sein).

---

## Schritt 3: Basiseinrichtung

| Einstellung | Wo | Was |
|---|---|---|
| **Standort** (wichtigster Schritt!) | Einstellungen → System → Allgemein | Ab Werk auf **„Linz Hauptplatz"** voreingestellt — **unbedingt auf deine eigene Adresse ändern** (auf der Karte oder per Koordinaten), Höhe & Zeitzone prüfen. Ohne korrekten Standort berechnet der Optimizer Sonnenauf-/-untergang und PV-Prognose für den falschen Ort. |
| **Passwort ändern** (Benutzer `ewa-mitglied`) | Profil (Name unten links) → Reiter *Sicherheit* → *Passwort ändern* | Das Passwort vom Begleitschreiben durch ein eigenes ersetzen — die Adresse ist aus dem Internet erreichbar, ein gutes Passwort ist deshalb wichtig |

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

1. Öffne **Home Assistant im Browser** — gleiche Adresse wie in Schritt 2, also
   die vom Begleitschreiben.
2. Klicke in der **Seitenleiste links** auf den Eintrag **„EEG Energy Optimizer"** —
   das öffnet den Einrichtungsassistenten.

Der Assistent führt dich in sieben Schritten durch: Willkommen · Wechselrichter ·
Batterie · PV-Prognose · Anlage & Batterie · Tarife & Gemeinschaft ·
Zusammenfassung. Was in jedem Schritt abgefragt wird, steht in der
[Installationsanleitung](../installation/eeg-integration.md#4-einrichtungsassistent).

> [!IMPORTANT]
> **Haken in der Zusammenfassung gesetzt lassen:** „Steuerung nach dem
> Fertigstellen einschalten“ stellt den Optimizer auf **Ein** — erst dann steuert
> er deinen Speicher. Ohne Haken bleibt er auf **Aus**: Er rechnet und zeigt den
> Fahrplan, schreibt aber nichts an den Wechselrichter. Den Modus siehst und
> änderst du jederzeit mit dem Schalter oben im Dashboard.

> [!TIP]
> Bei den Schritten Wechselrichter, PV-Prognose, Anlage & Batterie und Tarife &
> Gemeinschaft (bei Huawei auch Batterie) gibt es einen **„Anleitung"-Button**, der die passende Hilfe direkt im Panel
> anzeigt.

---

## Fertig 🎉

Wenn alle Schritte erledigt sind und der Schalter oben im Dashboard auf **Ein**
steht, läuft der EEG Energy Optimizer und steuert deinen Speicher nach den
**Einspeisepreisen**: Er speist ein, wenn eine
Kilowattstunde gerade mehr wert ist — etwa weil deine Energiegemeinschaft dann
Bedarf hat — und lädt oder hält, wenn sie weniger wert ist. Den Fahrplan und
den Status siehst du jederzeit im Panel **EEG Energy Optimizer** — was dort
steht und wie du die Steuerung pausierst, erklärt
**[Dashboard & Bedienung](../guides/dashboard.md)**.

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

---

## Gut zu wissen

### Fernzugang

Die Adresse vom Begleitschreiben ist der **Fernzugang** deines Geräts. Du
erreichst dein Home Assistant damit von überall, und wir können dir damit bei der
Einrichtung helfen. Du musst ihn nicht einrichten, er ist schon fertig.

Willst du ihn nicht mehr: **Einstellungen → Apps → cloudflared** öffnen, dort
**„Beim Systemstart starten“** ausschalten und **„Stoppen“** klicken. Danach geht
die Adresse vom Begleitschreiben nicht mehr, und du erreichst das Gerät nur noch
im Heimnetz unter `http://homeassistant.local:8123`. Wieder einschalten geht
genauso, mit **„Starten“**.

### EEG-Statistik

Nach der Einrichtung schickt dein Gerät **anonymisierte Diagnose- und
Wirksamkeitsdaten** an die EEG: etwa alle 30 Minuten Ladestand und Leistungen,
einmal täglich die Tagesbilanz mit den Mengen der Einspeise-Karte (eingespeist,
davon aus der Batterie, davon an die Gemeinschaft — keine Geldbeträge), dazu Störungsmeldungen und die Eckdaten deiner
Anlage (Wechselrichter-Typ, Batteriegröße, PV-Leistung). Keine Namen, keine
Adresse, keine Standortdaten. Damit sehen wir, ob die Steuerung wirkt, und finden
Fehler schneller.

Abschalten kannst du das jederzeit im Panel **EEG Energy Optimizer** unter
**Einstellungen → System → EEG-Statistik**. Dort steht auch genau, was gesendet
wird, und du kannst die übermittelten Daten löschen lassen.

### Fragen?

Schreib uns an **info@ew-ansfelden.at**.
