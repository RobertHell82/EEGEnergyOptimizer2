# Eigene PV-Prognose einrichten

Die Integration rechnet die PV-Prognose selbst. Die Wetterdaten kommen von **Open-Meteo** (kostenlos, ohne Konto, ohne Schlüssel), deine Anlage trägst du hier ein. Eine weitere Home-Assistant-Integration brauchst du nicht.

## 1. Standort prüfen

Der Standort kommt aus **Einstellungen → System → Allgemein → Standort**. Steht dort noch der Platzhalter, gilt die Prognose für einen falschen Ort — bitte prüfen.

## 2. Flächen eintragen

Eine Zeile je Modulfläche mit gleicher Ausrichtung. Ein Süddach ist eine Fläche, ein Ost-West-Dach sind zwei.

| Feld | Wert |
|---|---|
| **Name** | Frei wählbar, z.B. „Dach Süd" |
| **Leistung (kWp)** | Summe der Module dieser Fläche |
| **Neigung** | 0° = flach, 90° = senkrecht. Typische Dächer: **30–35°**, aufgeständert am Flachdach: 10–15° |
| **Azimut** | Himmelsrichtung als Kompasswert:<br>**0° = Nord**, 90° = Ost, **180° = Süd**, 270° = West |

_**Achtung Azimut:** Süd = 180°, wie bei Forecast.Solar in Home Assistant. Die Himmelsrichtung neben dem Feld zeigt dir, ob der Wert stimmt._

### Grenze je Fläche (optional)

Hängt eine Fläche an einem eigenen Wechselrichter oder MPP-Tracker (Solareingang des Wechselrichters), trag dessen AC-Grenze in **Grenze (kW)** ein. Beispiel: Süddach 10 kWp an einem 8-kW-Gerät, Ostdach an einem zweiten — dann wird das Süddach allein bei 8 kW gedeckelt. Leer gelassen deckelt nur die AC-Grenzleistung der ganzen Anlage.

## 3. Systemverluste

Pauschale Verluste in Prozent (Schmutz, Leitungen, Wechselrichter, Alterung). Richtwert: **14 %**. Neue, saubere Anlage eher 10 %, ältere oder schmutzige eher 18 %.

Mehr als die **AC-Grenzleistung** aus dem Schritt „Anlage & Batterie" wird nie prognostiziert.

## 4. Prognose berechnen

„Prognose berechnen" zeigt die Tagessummen der nächsten sieben Tage. So siehst du gleich, ob die Eingaben passen: Ein Süddach mit 10 kWp liefert an einem klaren Septembertag um die 50 kWh, ein Ostdach deutlich weniger.

> [!NOTE]
> Die Prognose wird alle 30 Minuten aufgefrischt und bleibt bei einem Neustart erhalten. Ist Open-Meteo nicht erreichbar, rechnet der Fahrplan mit den letzten Werten weiter. Erst nach 48 Stunden ohne neue Daten gibt es keinen Fahrplan mehr.

## Was das Modell kann und was nicht

Gerechnet wird mit der Sonneneinstrahlung auf deine Module (Mittel aus drei Wettermodellen: ICON vom Deutschen Wetterdienst, ECMWF und Météo-France), einer Temperaturkorrektur (−0,4 % je Kelvin), den Verlusten und dem Deckel auf die AC-Grenze.

Verschattung (Bäume, Nachbarhaus, Gauben), Horizont und örtliche Eigenheiten kennt das Modell nicht. Das lernt die **Kalibrierung**: Sie vergleicht jeden Tag Prognose und Messung und merkt sich je Sonnenstand einen Korrekturfaktor.

- Sie beginnt mit dem ersten gemessenen Tag und wird mit jedem Tag sicherer. Einzelne Ausreißer ziehen sie nur ein Stück.
- Einen Sonnenstand, den sie noch nie gesehen hat (z.B. die tiefe Wintersonne im Herbst), lässt sie unverändert. Ganz fertig ist sie nach einem Jahr.
- Tage mit ganz anderem Wetter als vorhergesagt und Zeiten mit Abregelung (Einspeisegrenze erreicht, Wechselrichter am Anschlag) zählen nicht.
- Änderst du Flächen, Verluste oder AC-Grenze, beginnt sie von vorn — ebenso bei anderen Wettermodellen.

Sie lernt immer, wenn die eigene Berechnung steuert. Ihren Stand siehst du in der Karte **Prognosevergleich** im Dashboard — die erscheint nur, wenn du den Prognosevergleich einschaltest.

Schnee auf den Modulen und ein Ost-West-Dach an einem einzigen MPP-Tracker kann auch die Kalibrierung nicht abbilden.

Solcast hat zusätzlich Satellitendaten für die nächsten Stunden und ist bei wechselnder Bewölkung dort im Vorteil. Für den Fahrplan zählt aber vor allem, wie viel heute noch und morgen kommt — da sind beide gleichauf.

## Prognosevergleich: beide Quellen nebeneinander

Ob die eigene Berechnung bei dir so gut ist wie Solcast, zeigt der Schalter **Prognosevergleich** — im Assistenten beim Schritt „PV-Prognose“ und unter **Einstellungen → Prognose**:

- Steuert **Solcast** (oder Forecast.Solar), läuft die eigene Berechnung mit — trag dafür in derselben Karte deine Flächen ein.
- Steuert die **eigene Berechnung**, läuft Solcast bzw. Forecast.Solar mit; die Integration muss installiert sein.

Jeden Morgen ab 5 Uhr werden beide Prognosen für den Tag festgehalten, über den Tag kommt die gemessene PV-Leistung dazu. Die Karte **Prognosevergleich** zeigt für einen wählbaren Tag drei Linien — Fremdprognose, eigene Berechnung, Messung — und darunter die letzten Tage: Tagessumme je Quelle, Abweichung, wer näher lag. Aufgehoben wird gut ein Jahr (die Kalibrierung lernt daraus); Abweichung, Fehler und p10 rechnen über die letzten 30 Tage. Steuert die eigene Berechnung, wird auch ohne Schalter aufgezeichnet.

Lief Home Assistant am Morgen nicht, werden die Prognosen erst beim Start festgehalten. Tage, an denen das nach 8 Uhr war, stehen mit † in der Tabelle und zählen nicht in Zusammenfassung und p10.

Die steuernde Quelle wechselst du in den Einstellungen im Tab **Prognose**; die Sensoren für Solcast und Forecast.Solar werden dabei vorbelegt und lassen sich dort ändern.

### Sensoren

Steuert die eigene Berechnung oder läuft sie mit, gibt es neun Sensoren: **Eigene PV-Prognose Leistung** (kW, laufende Viertelstunde — das Gegenstück zu „PV-Leistung“), **verbleibend heute**, **heute**, **morgen** und **Tag 3** bis **Tag 7** (kWh). Sie sind gleich aufgebaut wie die Tagessensoren von Solcast, du kannst sie also in Diagrammen nebeneinander legen. Mit eingeschaltetem Vergleich zeigt das Wochendiagramm im Dashboard je Tag einen dritten Balken für die mitlaufende Quelle.

### Der p10 der eigenen Prognose

Der Fahrplan hält eine Reserve zurück, falls die PV schwächer kommt. Dafür braucht er einen vorsichtigen Wert, den **p10**: so viel PV kommt mit 90 % Sicherheit mindestens. Solcast liefert ihn, die eigene Berechnung nicht. Mit eingeschaltetem Vergleich holt sie ihn sich so:

1. **Von Solcast geliehen**: das Verhältnis p10 zu Erwartung je Halbstunde, angewendet auf die eigene Erwartung.
2. **Aus der eigenen Messung**: ohne Solcast, ab 14 vollständigen Vergleichstagen, aus dem Verhältnis Messung zu Prognose.
3. Sonst rechnet die Reserve mit 60 % der Erwartung, wie bei Forecast.Solar.

Welcher Weg gilt, steht im Plan-Archiv bei der Quelle der PV-Prognose.

_Open-Meteo ist für nichtkommerzielle Nutzung frei (Richtwert 10 000 Abrufe am Tag). Bei halbstündlichem Abruf sind das 48 je Fläche und Tag._
