# Einspeisegrenze

Viele Netzbetreiber begrenzen, wie viel Leistung deine PV-Anlage ins Netz einspeisen darf (z. B. 4 kW). Normalerweise lädt die Batterie zuerst mit voller Leistung — ist sie voll, **regelt der Wechselrichter alles über der Grenze ab, und diese Energie geht verloren**.

Kennt der Fahrplan diese Grenze, rechnet er sie in jeden Lade- und Entladeplan ein: Der PV-Überschuss geht bis zur erlaubten Grenze ins Netz, und die Batterie lädt bevorzugt in den Stunden, in denen die Erzeugung über der Grenze liegt. So wird maximal in die Energiegemeinschaft eingespeist, möglichst nichts abgeregelt, und die Batterie füllt sich trotzdem — verteilt über den Tag.

## Voraussetzungen

- Ein korrekt konfigurierter **Netzleistungs-Sensor** (misst Einspeisung/Bezug)
- Eine **PV-Prognose** (Solcast, Forecast.Solar oder die eigene Berechnung)
- Die hier eingetragene Grenze muss dem Wert **im Wechselrichter** entsprechen

Die Grenze fließt bei **allen Wechselrichtern** in die Fahrplan-Berechnung ein, und bei allen sechs unterstützten Geräten führt die Steuerung das Ladelimit nach.

## So funktioniert es

Die Einspeisegrenze wirkt an zwei Stellen:

**1. In der Planung.** Der Fahrplan weiß, dass am Netzanschluss nie mehr als die Grenze eingespeist werden kann. Liegt die PV-Erzeugung laut Prognose darüber, verschiebt er das Laden der Batterie gezielt in diese Stunden — statt abzuregeln, nimmt die Batterie genau den Anteil oberhalb der Grenze auf.

**2. In der Steuerung (Ladelimit-Nachführung).** Prognosen sind nie exakt. Klebt die gemessene Einspeisung an der Grenze (±100 W) — das Anzeichen dafür, dass der Wechselrichter abregelt —, hebt die Steuerung das Ladelimit der Batterie alle 30 Sekunden um 0,5 kW an, bis der Überschuss in die Batterie fließt und die Einspeisung an der Grenze bleibt. Fällt die Einspeisung deutlich unter die Grenze (mehr als 0,3 kW darunter), wird das Ladelimit zurück Richtung Planwert genommen — je Lauf um den halben Abstand, mindestens 0,5 kW, nie unter den Planwert. Das asymmetrische tote Band dazwischen verhindert ein Pendeln.

## Konfiguration

| Feld | Bedeutung |
|---|---|
| **Einspeisegrenze beachten** | Schaltet die Funktion ein/aus (Standard: aus) |
| **Höhe der Grenze (kW)** | Die maximale Einspeiseleistung laut Vorgabe deines Netzbetreibers (z. B. 4) |
| **AC-Grenzleistung des Wechselrichters (kW)** | Pflicht. Nennleistung auf der Netzseite laut Datenblatt — begrenzt im Fahrplan die Summe aus Einspeisung und Hausverbrauch. Ein zu großer Wert erzeugt Pläne, die das Gerät nicht liefern kann |
| **PV-Spitzenleistung (kWp)** | Pflicht. Summe der Modulleistung — Ersatzwert, falls die AC-Grenzleistung fehlt, und Obergrenze für die Plausibilitätsprüfung der Tageswerte in der EEG-Statistik |

Alle vier Felder stehen im Assistenten im Schritt **Anlage & Batterie**, später unter **Einstellungen → Anlage**. Die beiden Gerätedaten (AC-Grenzleistung, PV-Spitzenleistung) zeigen die Einstellungen nur im Expertenmodus, weil sie sich nicht ändern.

Ist die Einspeisegrenze ausgeschaltet, plant der Fahrplan mit der AC-Grenzleistung minus 0,5 kW als Obergrenze am Netzanschluss.

> [!WARNING]
> **Die Grenze muss stimmen:** Eine Grenze, die es im Wechselrichter nicht gibt, verschenkt Einspeisung — der Fahrplan plant dann vorsichtiger als nötig. Eine Grenze, die wir nicht kennen, kostet Ertrag durch stille Abregelung. Bei Huawei prüft das Panel den Sensor `active_power_control` und warnt, wenn Konfiguration und Gerät nicht zusammenpassen.

> [!NOTE]
> **Hinweis:** Diese Einstellung ersetzt nicht die harte, netzseitig verpflichtende Einspeisebegrenzung deines Wechselrichters — die bleibt beim Gerät. Sie teilt dem Fahrplan nur mit, was das Gerät ohnehin tut, damit er darum herum planen kann.

> [!NOTE]
> **Volle Batterie:** Ist die Batterie voll, kann kein Überschuss mehr aufgenommen werden — dann regelt der Wechselrichter wie gewohnt ab. Das ist unvermeidbar und kein Fehler.
