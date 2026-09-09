# EEG Energy Optimizer — Dokumentation

Willkommen! Hier findest du alle Anleitungen, um den EEG Energy Optimizer zu installieren und einzurichten — von der HACS-Installation bis zur Wechselrichter-Anbindung.

## 📦 Vorbereitetes EEG-Gerät erhalten?

Hast du von deiner Energiegemeinschaft ein bereits vorbereitetes **Home Assistant Green** bekommen? Dann musst du nichts installieren — folge einfach der Inbetriebnahme:

→ **[Inbetriebnahme deines EEG-Geräts](deployment/inbetriebnahme.md)** — anschließen, anmelden, fertig einrichten (ca. 20 Min.)

Die folgenden Installations-Anleitungen brauchst du nur, wenn du Home Assistant **selbst von Grund auf** einrichtest.

## 🚀 Installation

Am besten in dieser Reihenfolge:

1. **[HACS auf Home Assistant installieren](installation/hacs.md)** — der Community Store, über den die Integration verteilt wird
2. **[EEG Energy Optimizer über HACS installieren](installation/eeg-integration.md)** — Integration hinzufügen und Einrichtungsassistent starten

## 🔌 Wechselrichter anbinden

Unterstützt werden derzeit **Fronius Gen24, Huawei SUN2000, Sigenergy SigenStor und SolaX Gen4+**:

| Wechselrichter | Anleitung |
|---|---|
| **Fronius Gen24** | [Fronius einrichten](guides/fronius.md) |
| **Huawei SUN2000** | [Huawei Solar Integration einrichten](guides/huawei.md) |
| | [Huawei Akkukapazität-Sensor aktivieren](guides/capacity_sensor.md) |
| **Sigenergy SigenStor** | [Sigenergy einrichten](guides/sigenergy.md) |
| **SolaX Gen4+** | [SolaX Modbus einrichten](guides/solax.md) |

> Kostal, SMA und SolarEdge werden **derzeit nicht** unterstützt — ihre Treiber sind enthalten, aber stillgelegt. Welcher Wechselrichter wann dazukommt und was dafür noch fehlt: **[Stand der Unterstützung](wechselrichter-status.md)**.

## ☀️ PV-Prognose einrichten

Eine der beiden Prognose-Quellen wird benötigt:

- **[Solcast Solar einrichten](guides/solcast.md)** (empfohlen — 7-Tage-Prognose)
- **[Forecast.Solar einrichten](guides/forecast_solar.md)** (ohne Registrierung nutzbar)

## 🔥 Heizstab (optional)

Wer mehr erzeugt, als Batterie, Haus und Einspeisegrenze aufnehmen, kann den Rest in einen Heizstab schicken statt ihn abzuregeln:

- **[Heizstab (Fronius Ohmpilot) einrichten](guides/heizstab.md)** — direkt per Modbus TCP gesteuert, nur der Überschuss oberhalb der Einspeisegrenze

> [!TIP]
> Alle Einrichtungs-Anleitungen sind auch direkt im Einrichtungsassistenten der Integration verfügbar — einfach auf die „Anleitung"-Buttons im Panel klicken.

## 🌐 Fernzugang (von außen erreichbar)

Home Assistant über eine eigene Internet-Adresse erreichbar machen — ohne Portfreigabe am Router:

- **[Fernzugang einrichten (Cloudflare Tunnel)](deployment/fernzugang-cloudflared.md)**

## ℹ️ Funktionsweise

Die Anlage wird von einem **Fahrplan** gesteuert, und der richtet sich ausschließlich nach **Preisen**: Jede Minute wird der erlösbeste Lade- und Entladeplan über 48 Stunden gerechnet. Die Einspeisevergütung ist dabei eine Zeitreihe — ein Basistarif plus Auf- bzw. Abschlag aus dem Bedarf deiner Energiegemeinschaften. Wo eine Kilowattstunde mehr wert ist, wird eingespeist; wo sie weniger wert ist, wird geladen oder gehalten.

> [!IMPORTANT]
Mit aktiver Energiegemeinschaft fließt deren Bedarfsprognose in den Preis ein:
Braucht die Gemeinschaft gerade Strom, ist deine Kilowattstunde dort mehr wert
und der Fahrplan speist ein; hat sie Überschuss, lohnt eher das Laden.
Vergütet wird nur, was die Gemeinschaft wirklich abnimmt — der Rest geht zum
Basistarif an den Reststromlieferanten.

> **Erkennt die Optimierung keinen Mehrwert, passiert nichts.** Ist der Einspeisepreis nachts nicht besser als tagsüber, wird nachts nicht eingespeist. Es gibt keine feste Nachtentladung und kein Zeitfenster — ohne Preisunterschied bleibt die Batterie, wo sie ist.

Wie das im Detail funktioniert, steht in der **[Projekt-Übersicht](../README.md)**; den Ablauf der Steuerung zeigt **[steuerung.md](steuerung.md)** mit Diagrammen.

Die **Einspeisegrenze** teilt dem Fahrplan mit, wie viel am Netzanschluss höchstens eingespeist werden darf — er plant dann so, dass möglichst nichts abgeregelt wird. Details in der Anleitung auf der gleichnamigen Wizard-Seite.
