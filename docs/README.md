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

Unterstützt werden **Fronius Gen24, Huawei SUN2000, Kostal Plenticore, Sigenergy SigenStor, SMA Smart Energy und SolaX Gen4+** — alle sechs werden vom Fahrplan gesteuert. Wie weit jedes Gerät erprobt ist, steht im **[Stand der Unterstützung](wechselrichter-status.md)**:

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

Eine der drei Prognose-Quellen wird benötigt:

- **[Solcast Solar einrichten](guides/solcast.md)** (7-Tage-Prognose mit Satelliten-Nowcast, kostenloses Konto nötig)
- **[Forecast.Solar einrichten](guides/forecast_solar.md)** (ohne Registrierung nutzbar)
- **[Eigene Berechnung einrichten](guides/prognose_eigen.md)** (keine Zusatz-Integration, Wetter von Open-Meteo, Flächen der Anlage direkt im Panel; kalibriert sich mit der Zeit an der eigenen Messung)

Mit dem **Prognosevergleich** läuft eine zweite Quelle mit, ohne zu steuern: Jeden Morgen werden beide Prognosen festgehalten und im Dashboard gegen die Messung gestellt. Beschrieben in der Anleitung zur eigenen Berechnung.

## ⚙️ Einstellungen

Was die Felder im Einrichtungsassistenten und in den Einstellungen bedeuten:

- **[Anlage & Batterie](guides/anlage_batterie.md)** — Gerätedaten, Batterie-Leistungsgrenze, Mindest- und Maximum-Ladestand, Sicherheitspuffer auf die Prognose
- **[Einspeisegrenze](guides/einspeisegrenze.md)** — wenn der Netzbetreiber die Einspeiseleistung begrenzt
- **[Tarife & Gemeinschaft](guides/tarife.md)** — Standardvergütung, Arbeitspreis und Netzgebühr, Energiegemeinschaften mit PeakShare oder fester Abnahmequote
- **[Einstellungen & Expertenmodus](guides/einstellungen.md)** — die Tabs, was nur im Expertenmodus erscheint, EEG-Statistik und Plan-Archiv

## 📊 Im Betrieb

- **[Dashboard & Bedienung](guides/dashboard.md)** — Statuskarte, Modus und Pause, „Was deine PV bringt“, Einspeisung, Bezugsspitze, was bei Störungen zu tun ist

## 🔥 Heizstab (optional)

Wer einen Warmwasserpuffer hat, kann PV-Strom als Wärme speichern — der Fahrplan wägt ab, ob eine Kilowattstunde als Wärme oder als Einspeisung mehr bringt:

- **[Heizstab (Fronius Ohmpilot) einrichten](guides/heizstab.md)** — direkt per Modbus TCP gesteuert, nach Plan und aus ungeplantem Überschuss

## 🚗 Wallbox (optional)

- **[Wallbox Ambibox](guides/wallbox.md)** — Fahrzeug in der Statuskarte, manueller Lade- und Entladetest; noch ohne Steuerung durch den Fahrplan

> [!TIP]
> Die Einrichtungs-Anleitungen sind auch direkt im Panel verfügbar — einfach auf die „Anleitung"-Buttons klicken: im Einrichtungsassistenten bei Wechselrichter, PV-Prognose, Anlage & Batterie und Tarife & Gemeinschaft, in den Einstellungen in jedem Tab, und die Anleitung zum Dashboard ganz unten im Dashboard.

## 🌐 Fernzugang (von außen erreichbar)

Home Assistant über eine eigene Internet-Adresse erreichbar machen — ohne Portfreigabe am Router:

- **[Fernzugang einrichten (Cloudflare Tunnel)](deployment/fernzugang-cloudflared.md)**

> [!NOTE]
> **Nur für selbst eingerichtete Geräte.** Auf einem vorbereiteten EEG-Gerät ist der
> Fernzugang schon fertig — die Adresse steht auf deinem Begleitschreiben, und wie du
> ihn abschaltest, steht in der [Inbetriebnahme](deployment/inbetriebnahme.md#fernzugang).

## ℹ️ Funktionsweise

Die Anlage wird von einem **Fahrplan** gesteuert, und der richtet sich ausschließlich nach **Preisen**: Jede Minute wird der erlösbeste Lade- und Entladeplan über 48 Stunden gerechnet. Die Einspeisevergütung ist dabei eine Zeitreihe — ein Basistarif plus Auf- bzw. Abschlag aus dem Bedarf deiner Energiegemeinschaften. Wo eine Kilowattstunde mehr wert ist, wird eingespeist; wo sie weniger wert ist, wird geladen oder gehalten.

> [!IMPORTANT]
> Mit aktiver Energiegemeinschaft fließt deren Bedarfsprognose in den Preis ein:
> Braucht die Gemeinschaft gerade Strom, ist deine Kilowattstunde dort mehr wert
> und der Fahrplan speist ein; hat sie Überschuss, lohnt eher das Laden.
> Vergütet wird nur, was die Gemeinschaft wirklich abnimmt — der Rest geht zum
> Basistarif an den Reststromlieferanten.

> **Erkennt die Optimierung keinen Mehrwert, passiert nichts.** Ist der Einspeisepreis nachts nicht besser als tagsüber, wird nachts nicht eingespeist. Es gibt keine feste Nachtentladung und kein Zeitfenster — ohne Preisunterschied bleibt die Batterie, wo sie ist.

Wie das im Detail funktioniert, steht in der **[Projekt-Übersicht](../README.md)**; den Ablauf der Steuerung zeigt **[steuerung.md](steuerung.md)** mit Diagrammen.

Die **Einspeisegrenze** teilt dem Fahrplan mit, wie viel am Netzanschluss höchstens eingespeist werden darf — er plant dann so, dass möglichst nichts abgeregelt wird. Details in der Anleitung **[Einspeisegrenze](guides/einspeisegrenze.md)** — im Panel im Schritt „Anlage & Batterie“ bzw. unter Einstellungen → Anlage.
