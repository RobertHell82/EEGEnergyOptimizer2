# EEG Energy Optimizer — Dokumentation

Willkommen! Hier findest du alle Anleitungen zum EEG Energy Optimizer: von der Installation über HACS bis zur Anbindung deines Wechselrichters.

## 📦 Vorbereitetes EEG-Gerät erhalten?

Hast du von deiner Energiegemeinschaft ein fertig vorbereitetes **Home Assistant Green** bekommen? Dann musst du nichts installieren. Folge einfach der Inbetriebnahme:

→ **[Inbetriebnahme deines EEG-Geräts](deployment/inbetriebnahme.md)** — anschließen, anmelden, fertig einrichten (ca. 20 Min.)

Die Installations-Anleitungen weiter unten brauchst du nur, wenn du Home Assistant **selbst von Grund auf** einrichtest.

## 🚀 Installation

Am besten in dieser Reihenfolge:

1. **[HACS auf Home Assistant installieren](installation/hacs.md)** — HACS ist ein „App-Store“ für Home Assistant. Darüber bekommst du die Integration.
2. **[EEG Energy Optimizer über HACS installieren](installation/eeg-integration.md)** — Integration hinzufügen und Einrichtungsassistent starten

## 🔌 Wechselrichter anbinden

Unterstützt werden **Fronius Gen24, Huawei SUN2000, Kostal Plenticore, Sigenergy SigenStor, SMA Smart Energy und SolaX Gen4+**. Alle sechs werden vom Fahrplan gesteuert. Wie weit jedes Gerät erprobt ist, steht im **[Stand der Unterstützung](wechselrichter-status.md)**.

| Wechselrichter | Anleitung |
|---|---|
| **Fronius Gen24** | [Fronius einrichten](guides/fronius.md) |
| **Huawei SUN2000** | [Huawei Solar Integration einrichten](guides/huawei.md) |
| | [Huawei Akkukapazität-Sensor aktivieren](guides/capacity_sensor.md) |
| **Kostal Plenticore** | [Kostal einrichten](guides/kostal.md) |
| **Sigenergy SigenStor** | [Sigenergy einrichten](guides/sigenergy.md) |
| **SMA Smart Energy** | [SMA einrichten](guides/sma.md) |
| **SolaX Gen4+** | [SolaX Modbus einrichten](guides/solax.md) |

## ☀️ PV-Prognose einrichten

Die PV-Prognose sagt voraus, wie viel Strom deine Anlage in den nächsten Tagen erzeugt. Du brauchst **eine** dieser drei Quellen:

| Quelle | Kurz gesagt |
|---|---|
| **[Solcast Solar einrichten](guides/solcast.md)** | 7-Tage-Prognose, nutzt auch aktuelle Satellitenbilder; kostenloses Konto nötig |
| **[Forecast.Solar einrichten](guides/forecast_solar.md)** | ohne Registrierung nutzbar |
| **[Eigene Berechnung einrichten](guides/prognose_eigen.md)** | keine Zusatz-Integration; Wetter von Open-Meteo; die Flächen deiner Anlage trägst du direkt im Panel ein; wird mit der Zeit an deiner eigenen Messung genauer (kalibriert) |

Mit dem **Prognosevergleich** läuft eine zweite Quelle nebenher mit, ohne zu steuern. Jeden Morgen werden beide Prognosen festgehalten. Das Dashboard stellt sie dann der Messung gegenüber. Mehr dazu steht in der Anleitung zur eigenen Berechnung.

## ⚙️ Einstellungen

Was die Felder im Einrichtungsassistenten und in den Einstellungen bedeuten:

- **[Anlage & Batterie](guides/anlage_batterie.md)** — Gerätedaten, Batterie-Leistungsgrenze, Mindest- und Maximum-Ladestand, Sicherheitspuffer auf die Prognose, Ladeziel am Abend, vormittags bevorzugt netzdienlich
- **[Einspeisegrenze](guides/einspeisegrenze.md)** — wenn der Netzbetreiber die Einspeiseleistung begrenzt
- **[Tarife & Gemeinschaft](guides/tarife.md)** — Standardvergütung, Arbeitspreis und Netzgebühr, Energiegemeinschaften mit PeakShare oder fester Abnahmequote, Batterie aus dem Netz laden (Beta, nur Huawei)
- **[Einstellungen & Expertenmodus](guides/einstellungen.md)** — die Tabs, was nur im Expertenmodus erscheint, EEG-Statistik, Verlauf vergangener Tage und Plan-Archiv

## 📊 Im Betrieb

- **[Dashboard & Bedienung](guides/dashboard.md)** — Statuskarte, Modus und Pause, „Was deine PV bringt“, Einspeisung, Bezug, Bezugsspitze, Karten ausblenden, was bei Störungen zu tun ist

## ⚡ Spitzenkappung (optional)

- **[Spitzenkappung](guides/spitzenkappung.md)** — hält die Bezugsspitze des Monats niedrig. Die Batterie hält eine Reserve für Spitzen zurück, berechnet aus deinem Verbrauch (Huawei, Fronius, SMA, SolaX). Reicht sie nicht, werden Verbraucher abgeschaltet.

## 🔥 Heizstab (optional)

Hast du einen Warmwasserpuffer? Dann kannst du PV-Strom als Wärme speichern. Der Fahrplan rechnet aus, ob eine Kilowattstunde als Wärme oder als Einspeisung mehr bringt:

- **[Heizstab (Fronius Ohmpilot) einrichten](guides/heizstab.md)** — direkt per Modbus TCP gesteuert, nach Plan und aus ungeplantem Überschuss

## 🚗 Wallbox (optional)

- **[Wallbox Ambibox](guides/wallbox.md)** — zeigt das Fahrzeug in der Statuskarte, dazu ein manueller Lade- und Entladetest. Der Fahrplan steuert die Wallbox noch nicht.

> [!TIP]
> Die Einrichtungs-Anleitungen findest du auch direkt im Panel. Klicke einfach auf die „Anleitung"-Buttons: im Einrichtungsassistenten bei Wechselrichter, PV-Prognose, Anlage & Batterie und Tarife & Gemeinschaft, in den Einstellungen in jedem Tab. Die Anleitung zum Dashboard steht ganz unten im Dashboard.

## 🌐 Fernzugang (von außen erreichbar)

So erreichst du Home Assistant über eine eigene Internet-Adresse — ohne Portfreigabe am Router:

- **[Fernzugang einrichten (Cloudflare Tunnel)](deployment/fernzugang-cloudflared.md)**

> [!NOTE]
> **Nur für selbst eingerichtete Geräte.** Auf einem vorbereiteten EEG-Gerät ist der
> Fernzugang schon fertig. Die Adresse steht auf deinem Begleitschreiben. Wie du
> ihn abschaltest, steht in der [Inbetriebnahme](deployment/inbetriebnahme.md#fernzugang).

## ℹ️ So funktioniert es

Die Anlage folgt einem **Fahrplan**. Der Fahrplan richtet sich nur nach **Preisen**:

- Jede Minute wird neu gerechnet, für die nächsten 48 Stunden.
- Gesucht ist der Lade- und Entladeplan, der am meisten Erlös bringt.
- Die Einspeisevergütung ist dabei kein fester Wert, sondern ändert sich über den Tag: ein Basistarif plus Auf- oder Abschlag, je nach Bedarf deiner Energiegemeinschaften.
- Ist eine Kilowattstunde gerade mehr wert, wird eingespeist. Ist sie weniger wert, wird geladen oder gehalten.

> [!IMPORTANT]
> Mit aktiver Energiegemeinschaft fließt deren Bedarfsprognose in den Preis ein.
> Braucht die Gemeinschaft gerade Strom, ist deine Kilowattstunde dort mehr wert,
> und der Fahrplan speist ein. Hat sie Überschuss, lohnt eher das Laden.
> Vergütet wird nur, was die Gemeinschaft wirklich abnimmt. Der Rest geht zum
> Basistarif an den Reststromlieferanten.

> **Erkennt die Optimierung keinen Mehrwert, passiert nichts.** Ist der Einspeisepreis nachts nicht besser als tagsüber, wird nachts nicht eingespeist. Es gibt keine feste Nachtentladung und kein festes Zeitfenster. Ohne Preisunterschied bleibt die Batterie, wo sie ist.

Wie das im Detail funktioniert, steht in der **[Projekt-Übersicht](../README.md)**. Den Ablauf der Steuerung mit Diagrammen zeigt **[steuerung.md](steuerung.md)**.

Die **Einspeisegrenze** sagt dem Fahrplan, wie viel am Netzanschluss höchstens ins Netz gehen darf. Er plant dann so, dass möglichst nichts abgeregelt (also weggeworfen) wird. Details stehen in der Anleitung **[Einspeisegrenze](guides/einspeisegrenze.md)**. Im Panel findest du sie im Schritt „Anlage & Batterie“ bzw. unter Einstellungen → Anlage.
