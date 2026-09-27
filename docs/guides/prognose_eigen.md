# Eigene PV-Prognose einrichten

Die Integration rechnet die PV-Prognose selbst: Wetterdaten kommen von **Open-Meteo** (kostenlos, ohne Konto, ohne Schlüssel), die Anlage trägst du hier ein. Es ist keine weitere Home-Assistant-Integration nötig.

## 1. Standort prüfen

Open-Meteo braucht Breiten- und Längengrad. Die Integration nimmt sie aus der Home-Assistant-Konfiguration (**Einstellungen → System → Allgemein → Standort**). Steht dort noch der Platzhalter, ist die Prognose für einen falschen Ort — bitte einmal prüfen.

## 2. Flächen eintragen

Eine Zeile je Modulfläche mit gleicher Ausrichtung. Ein Süddach ist eine Fläche, ein Ost-West-Dach sind zwei.

| Feld | Wert |
|---|---|
| **Name** | Frei wählbar, z.B. „Dach Süd" |
| **kWp** | Modulleistung dieser Fläche (Summe der Module) |
| **Neigung** | 0° = flach liegend, 90° = senkrecht. Typische Dächer: **30–35°**, Flachdach-Aufständerung: 10–15° |
| **Azimut** | Himmelsrichtung der Module als Kompasswert:<br>**0° = Nord**, 90° = Ost, **180° = Süd**, 270° = West |

_**Achtung Azimut:** Es gilt die Kompass-Konvention (Süd = 180°), dieselbe wie bei `sun.sun` und bei Forecast.Solar in Home Assistant. Open-Meteo selbst zählt anders (0° = Süd) — die Umrechnung macht die Integration, du trägst den Kompasswert ein. Die Himmelsrichtung neben dem Feld zeigt, ob der Wert stimmt._

### Grenze je Fläche (optional)

Hängt eine Fläche an einem eigenen Wechselrichter oder MPP-Tracker, trägst du dessen AC-Grenze in **Grenze (kW)** ein. Beispiel: Süddach 10 kWp an einem 8-kW-Gerät, Ostdach an einem zweiten — dann wird das Süddach für sich bei 8 kW abgeschnitten, nicht erst die Summe. Leer gelassen deckelt nur die AC-Grenzleistung der ganzen Anlage.

## 3. Systemverluste

Pauschale Verluste in Prozent für Verschmutzung, Leitungen, Mismatch, Wechselrichter und Alterung. **14 %** ist der übliche Richtwert (PVWatts). Wer seine Anlage kennt, kann anpassen: neue, saubere Anlage eher 10 %, ältere oder verschmutzte eher 18 %.

Die **AC-Grenzleistung** des Wechselrichters aus dem Schritt „Anlage & Batterie" deckelt die Prognose — mehr als das Gerät liefert, wird nie prognostiziert.

## 4. Prognose berechnen

„Prognose berechnen" holt die Wetterdaten einmal und zeigt die Tagessummen der nächsten sieben Tage. So siehst du sofort, ob die Eingaben plausibel sind — ein Süddach mit 10 kWp liefert an einem klaren Septembertag um die 50 kWh, ein Ostdach deutlich weniger.

> [!NOTE]
> Die Prognose wird alle 30 Minuten aufgefrischt und überlebt Neustarts. Ist Open-Meteo einmal nicht erreichbar, rechnet der Fahrplan mit den zuletzt geholten Werten weiter; erst nach 48 Stunden ohne Abruf gibt es keinen Fahrplan mehr.

## Was das Modell kann und was nicht

Die Rechnung folgt dem PVWatts-Ansatz: Einstrahlung auf die Modulebene (von Open-Meteo aus Direkt- und Diffusstrahlung für deine Neigung und Ausrichtung berechnet), Temperaturkorrektur der Zellen (−0,4 % je Kelvin), pauschale Verluste, Deckel auf die AC-Grenze.

Was das Modell **nicht** kennt: Verschattung durch Bäume, Nachbargebäude oder Gauben, Schnee auf den Modulen, und ein Ost-West-Dach an einem Wechselrichter mit einem einzigen MPP-Tracker. In diesen Fällen liegen die Tagessummen meist noch richtig, der Tagesverlauf nicht.

Gegenüber Solcast fehlt der Satelliten-Nowcast für die nächsten Stunden — bei durchbrochener Bewölkung ist Solcast dort im Vorteil. Für den Fahrplan zählt aber vor allem, wie viel Energie heute noch und morgen kommt, und da sind Wettermodell-Prognosen gleichauf.

## Prognosevergleich: beide Quellen nebeneinander

Ob die eigene Berechnung auf deiner Anlage so gut ist wie Solcast, sagen nur Zahlen von deiner Anlage. Dafür gibt es den Schalter **Prognosevergleich** — im Assistenten beim Schritt „PV-Prognose“ und in den **Einstellungen → Anlage**:

- Steuert **Solcast** (oder Forecast.Solar), läuft die eigene Berechnung mit — trage dafür in derselben Karte deine Flächen ein.
- Steuert die **eigene Berechnung**, wird Solcast bzw. Forecast.Solar mitgelesen; die Integration muss dafür installiert sein.

Jeden Morgen ab 5 Uhr werden beide Prognosen für den Tag festgehalten, im Lauf des Tages kommt die gemessene PV-Leistung dazu. Die Karte **Prognosevergleich** im Dashboard zeigt für einen wählbaren Tag drei Linien — Fremdprognose, eigene Berechnung, Messung — und darunter die letzten Tage mit Tagessumme je Quelle, Abweichung und wer näher an der Messung lag. Aufgehoben werden 30 Tage.

Lief Home Assistant am Morgen nicht, werden die Prognosen erst beim nächsten Start festgehalten. Nach 8 Uhr haben sie den halben Tag schon gesehen — solche Tage stehen mit † in der Tabelle, zählen aber nicht in die Zusammenfassung und nicht in den p10.

Den Schalter gibt es auch im Einrichtungsassistenten beim Schritt „PV-Prognose“. Die steuernde Quelle selbst lässt sich in den Einstellungen unter **Anlage → PV-Prognose** wechseln; für Solcast und Forecast.Solar werden die Sensoren dabei vorbelegt.

### Sensoren

Steuert die eigene Berechnung oder läuft sie als Vergleich mit, legt die Integration neun Sensoren an: **Eigene PV-Prognose Leistung** (kW, die laufende Viertelstunde, das Gegenstück zu „PV-Leistung“), **verbleibend heute**, **heute**, **morgen** und **Tag 3** bis **Tag 7** (kWh). Sie haben dieselbe Form wie die Tagessensoren von Solcast und lassen sich in jedem Home-Assistant-Diagramm neben Solcast und die gemessene PV legen. Mit eingeschaltetem Vergleich zeigt das Wochendiagramm im Dashboard außerdem je Tag einen dritten Balken für die mitlaufende Quelle.

### Der p10 der eigenen Prognose

Die Notstrom-Reserve rechnet mit einem Worst-Case-Pfad. Solcast liefert dafür ein echtes 10-%-Perzentil, die eigene Berechnung nicht. Mit eingeschaltetem Vergleich holt sie sich eines:

1. **Von Solcast geliehen**: das Verhältnis p10 zu Erwartung je Halbstunde, angewendet auf die eigene Erwartung — ein Unsicherheitsmaß aus dem Wetter, keine erfundene Zahl.
2. **Empirisch**: ohne Solcast, sobald 14 vollständige Vergleichstage da sind, das 10-%-Quantil des Verhältnisses gemessen zu prognostiziert aus der eigenen Historie.
3. Sonst rechnet die Reserve mit 60 % der Erwartung, genau wie bei Forecast.Solar.

Welcher Weg gerade gilt, steht im Plan-Archiv in der Quelle der PV-Prognose.

_Open-Meteo ist für nichtkommerzielle Nutzung frei (Richtwert 10 000 Abrufe je Tag). Bei halbstündlichem Abruf sind das 48 je Fläche und Tag._
