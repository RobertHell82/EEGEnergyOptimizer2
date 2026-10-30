# Einspeisegrenze

Viele Netzbetreiber begrenzen, wie viel Leistung deine PV-Anlage ins Netz einspeisen darf (z. B. 4 kW). Normalerweise lädt die Batterie zuerst mit voller Leistung. Ist sie voll, **regelt der Wechselrichter alles über der Grenze ab. Diese Energie geht verloren.**

Kennt der Fahrplan die Grenze, plant er so:

- Der PV-Überschuss geht bis zur Grenze ins Netz.
- Die Batterie lädt bevorzugt in den Stunden, in denen die Erzeugung über der Grenze liegt.

So geht möglichst viel in die Energiegemeinschaft, möglichst nichts wird abgeregelt, und die Batterie wird trotzdem voll, verteilt über den Tag.

## Voraussetzungen

- Ein richtig eingerichteter **Netzleistungs-Sensor** (misst Einspeisung und Bezug)
- Eine **PV-Prognose** (Solcast, Forecast.Solar oder die eigene Berechnung)
- Die hier eingetragene Grenze muss dem Wert **im Wechselrichter** entsprechen

Die Grenze fließt bei **allen Wechselrichtern** in den Fahrplan ein. Bei allen sieben unterstützten Geräten führt die Steuerung außerdem das Ladelimit nach.

## So funktioniert es

**1. In der Planung.** Der Fahrplan weiß, dass nie mehr als die Grenze ins Netz geht. Liegt die PV-Erzeugung laut Prognose darüber, legt er das Laden der Batterie in genau diese Stunden. Statt abzuregeln, nimmt die Batterie den Anteil über der Grenze auf.

**2. In der Steuerung (Ladelimit-Nachführung).** Das Ladelimit ist die Leistung, mit der die Batterie höchstens laden darf. Prognosen stimmen nie genau, deshalb prüft die Steuerung alle 30 Sekunden die gemessene Einspeisung:

- **Einspeisung klebt an der Grenze (±100 W):** Der Wechselrichter regelt vermutlich ab. Die Steuerung hebt das Ladelimit um 0,5 kW an, bis der Überschuss in die Batterie fließt.
- **Einspeisung mehr als 0,3 kW unter der Grenze:** Das Ladelimit geht zurück Richtung Planwert, je Lauf um den halben Abstand, mindestens 0,5 kW, nie unter den Planwert.
- **Dazwischen** passiert nichts. Dieser Bereich verhindert ein Hin- und Herpendeln.

## Konfiguration

| Feld | Bedeutung |
|---|---|
| **Einspeisegrenze beachten** | Schaltet die Funktion ein oder aus (Standard: aus) |
| **Höhe der Grenze (kW)** | Die höchste erlaubte Einspeiseleistung laut deinem Netzbetreiber (z. B. 4) |
| **AC-Grenzleistung des Wechselrichters (kW)** | Pflicht. Nennleistung auf der Netzseite laut Datenblatt. Begrenzt im Fahrplan die Summe aus Einspeisung und Hausverbrauch. Ein zu großer Wert erzeugt Pläne, die das Gerät nicht liefern kann |
| **PV-Spitzenleistung (kWp)** | Pflicht. Summe der Modulleistung. Ersatzwert, falls die AC-Grenzleistung fehlt, und Obergrenze für die Plausibilitätsprüfung der Tageswerte in der EEG-Statistik |

Alle vier Felder stehen im Assistenten im Schritt **Anlage & Batterie**, später unter **Einstellungen → Anlage**. AC-Grenzleistung und PV-Spitzenleistung zeigen die Einstellungen nur im Expertenmodus, weil sie sich nicht ändern.

Ist die Einspeisegrenze ausgeschaltet, plant der Fahrplan mit der AC-Grenzleistung minus 0,5 kW als Obergrenze am Netzanschluss.

> [!WARNING]
> **Die Grenze muss stimmen.** Trägst du eine Grenze ein, die es im Wechselrichter nicht gibt, plant der Fahrplan zu vorsichtig und verschenkt Einspeisung. Fehlt eine Grenze, die es gibt, kostet stilles Abregeln Ertrag. Bei Huawei prüft das Panel den Sensor `active_power_control` und warnt, wenn Einstellung und Gerät nicht zusammenpassen.

> [!NOTE]
> Diese Einstellung ersetzt nicht die vorgeschriebene Einspeisebegrenzung im Wechselrichter. Die bleibt beim Gerät. Der Fahrplan erfährt hier nur, was das Gerät ohnehin tut, und plant darum herum.

> [!NOTE]
> **Volle Batterie:** Ist die Batterie voll, kann sie keinen Überschuss mehr aufnehmen. Dann regelt der Wechselrichter wie gewohnt ab. Das ist unvermeidbar und kein Fehler.
