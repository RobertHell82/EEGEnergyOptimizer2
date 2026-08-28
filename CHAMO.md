# Fahrplan-Prototyp (chamo)

Dieses Repo ist ein **Prototyp-Zweig** des EEG Energy Optimizer. Es enthält
zusätzlich zur produktiven Integration den LP-Fahrplan-Optimierer aus
[EngagePV/chamo](https://gitlab.com/EngagePV/chamo) von Harald Geyer.

Der Fahrplan ist der **einzige Aktor**: Jede Minute rechnet der
`ScheduleRunner` einen Fahrplan über 48 Stunden, alle 30 Sekunden hält die
**Steuerung** (`ScheduleExecutor`) den zuletzt gerechneten Plan gegen die
Messwerte und setzt ihn am Wechselrichter durch — derzeit nur bei **Huawei
SUN2000**, die anderen fünf Treiber rechnen und zeigen an
(`supports_schedule_control = False`). Die Zustands-Heuristik der produktiven
Integration (Morgen-Einspeisung, Nacht-Entladung, Einspeisebegrenzung) ist
vollständig entfernt — ihre Verhalten entstehen im Fahrplan von selbst aus den
Tarifen. Rechnen und Steuern sind strikt getrennt: Der Optimierer schreibt nie
selbst, nur die Steuerung.

> **Wichtig:** Dieses Repo hat dieselbe Domain wie die produktive Integration.
> In einem Home Assistant kann nur **eine** von beiden installiert sein. Die
> Config-Entry-Version bleibt bei 20, ein Rückwechsel ist also jederzeit
> möglich, ohne die Konfiguration zu verlieren.

## Was ist neu

| Pfad | Inhalt |
|------|--------|
| `custom_components/eeg_energy_optimizer/chamo/` | Haralds Optimierer. `config_dummy.py` unverändert, `opt_highs.py` bis auf die Import-Zeile unverändert. |
| `custom_components/eeg_energy_optimizer/chamo/highs_adapter.py` | optlang-kompatible Minimalschicht auf HiGHS (unser Code). |
| `custom_components/eeg_energy_optimizer/schedule.py` | Brücke zu Home Assistant: Daten sammeln, Fahrplan rechnen. Der erste Stützpunkt der Zeitreihen wird mit gemessener PV und Hauslast überschrieben. |
| `custom_components/eeg_energy_optimizer/schedule_executor.py` | Die Steuerung: übersetzt den laufenden Slot treiberneutral in eine Absicht (Ladelimit / Entladung / Freigabe) und setzt sie alle 30 Sekunden am Wechselrichter durch — mit Nachführungen, Not-Aus, Totbändern und Failsafe. |
| `custom_components/eeg_energy_optimizer/power_readings.py` | Messwerte im 30-Sekunden-Takt: PV-Leistung, Netzleistung (vorzeichen-aufgelöst) und daraus die Hauslast. |
| Panel-Abschnitt „Fahrplan" | Diagramm plus die nächsten Slots als Tabelle, dazu eine Fahrplan-Statuskarte mit der aktuellen Aktion und den Job-Laufzeiten. |
| Sensoren *Fahrplan Batterieleistung* / *Fahrplan Netzleistung* | Planwerte des laufenden Slots, Vorzeichen wie bei den Ist-Sensoren. |
| Sensor *Fahrplan-Status* | Zustand der Steuerung („Laden begrenzt auf x kW", „Entladung y kW bis z %", „Normalbetrieb", „Anzeige-Modus") — gleiche `unique_id` wie der frühere Entscheidungs-Sensor, Entität und Verlaufshistorie bleiben. |
| `tests/test_chamo_highs_adapter.py` | Beweis, dass HiGHS dieselben Ergebnisse liefert wie GLPK. |
| `tests/test_schedule.py` | Anbindung: Verbrauchsprofil, PV-Prognose, Batteriezustand, Rechenlauf. |
| `tests/test_schedule_executor.py` | Die Steuerung: Slot-Zuordnung, Totbänder, Nachführungen, Not-Aus, Failsafe, Modus-Wechsel. |

## Woher die PV-Prognose kommt

Erste Wahl sind die Solcast-Tagessensoren: sie tragen ein Attribut
`detailedForecast` mit 48 Halbstundenwerten je Tag, jeweils `pv_estimate`,
`pv_estimate10` und `pv_estimate90` in kW. Über sieben Tagessensoren ergibt das
eine Woche Vorausschau — und mit `pv_estimate10` einen echten Worst-Case-Pfad
für `min_production()` statt eines geschätzten Faktors.

Gesucht wird über das Attribut, nicht über Entity-Namen: die sind lokalisiert
(`prognose_heute` gegen `forecast_today`) und wären eine dauerhafte Fehlerquelle.

Fehlt Solcast, greift die Energy-Dashboard-Schnittstelle
`async_get_solar_forecast()` (auch Forecast.Solar bietet sie an). Die liefert nur
Stunden-Erwartungswerte, dort bleibt der Worst-Case-Faktor in Kraft.

## Steuerung

Alle 30 Sekunden läuft die Steuerung (`ScheduleExecutor`) und setzt den
laufenden Fahrplan-Slot am Wechselrichter durch:

* **Slot plant Laden** → Ladelimit auf die Planleistung. Die
  **Ladelimit-Nachführung** hebt es um 0,5 kW pro Lauf an, wenn die gemessene
  Einspeisung an der Einspeisegrenze klebt (±100 W — Anzeichen für stille
  Abregelung), und nimmt es erst unter Grenze − 0,3 kW schrittweise auf den
  Planwert zurück. Das asymmetrische tote Band verhindert Pendeln.
* **Slot plant Einspeisung aus der Batterie** → erzwungene Entladung. Die
  **Entlade-Nachführung** rechnet die gemessene Hauslast auf die geplante
  Netzleistung auf (Entladeleistung = `grid_p` + Hauslast), damit die geplante
  Einspeisung tatsächlich am Netzanschluss ankommt; Ziel-SOC ist der Wert am
  Ende des laufenden Slots.
* **Slot plant nichts** → Ladelimit 0. Freigeben wäre falsch: Der
  Automatikmodus würde Überschuss in die Batterie laden, den der Plan
  einspeisen will — die Morgen-Einspeisung entsteht genau hier.
* **Slot plant Entladung nur für den Hausverbrauch** → Freigabe; das erledigt
  der Wechselrichter im Automatikmodus selbst.

Sicherheitsnetze:

* **Not-Aus:** Netzbezug über 1 kW in drei aufeinanderfolgenden Läufen während
  einer Entladung → Entladung wird gestoppt und bis zum nächsten Slotwechsel
  gesperrt.
* **Failsafe:** Fehlt länger als 15 Minuten ein brauchbarer Fahrplan, wird der
  Wechselrichter einmalig in den Automatikmodus freigegeben.
* **Totbänder:** Geschrieben wird nur bei relevanter Änderung (> 200 W bzw.
  ≥ 1 Prozentpunkt Ziel-SOC).
* **Grace Period:** Nach einem Neustart schreibt die Steuerung erst nach der
  Startphase — ein nach hartem Absturz stehengebliebenes Limit bleibt bis zum
  ersten Lauf danach.

Ob gesteuert wird, entscheidet der bestehende Select
`select.eeg_energy_optimizer_optimizer`:

| Modus | Rechnen | Schreiben |
|---|---|---|
| **Ein** | jede Minute | Ladelimit und Entladung werden gesetzt |
| **Test** | jede Minute | nichts — der Fahrplan wird nur angezeigt |

Beim Wechsel Ein → Test wird der Wechselrichter einmalig freigegeben, sonst
bliebe das letzte Ladelimit stehen. Dieselbe Freigabe läuft beim Entladen der
Integration (Neustart, Konfig-Änderung).

Gesteuert wird derzeit nur **Huawei SUN2000** (Ladelimit über die
Number-Entität, Entladung über `forcible_discharge_soc`). Die anderen fünf
Treiber melden `supports_schedule_control = False` — bei ihnen zeigt die
Statuskarte „Anzeige — Steuerung derzeit nur Huawei".

Freigeben heißt bei Huawei: Ladelimit zurück auf das Maximum der
Number-Entität, also auf den anlagenspezifischen Standardwert. Deshalb wird nur
eingegriffen, wenn der Fahrplan einen konkreten Wert vorgibt — plant er kein
Laden und ist die Batterie voll (Plan-SOC ≥ 99 %), bleibt der Standard stehen.
Hat die Batterie Platz, wird weiter auf 0 begrenzt: Sonst lädt der
Automatikmodus den Überschuss weg, den der Plan einspeisen will.

Der vollständige Ablauf als Diagramm: [docs/steuerung.md](docs/steuerung.md).

## Verbrauchsprofil

Grundlage ist der eigene Hausverbrauchs-Sensor (PV − Batterie − Netz), gelesen
aus den Stunden-Statistiken des Recorders über `lookback_weeks` (Vorgabe 4).

Gemittelt wird über **zwei Gruppen** statt über sieben einzelne Wochentage:
`wt` (Mo–Fr, sofern kein Feiertag) und `we` (Sa, So, Feiertag). Vier Wochen
ergeben damit rund 20 Vergleichswerte je Werktagsstunde statt vier — eine
einmalige E-Auto-Ladung schlägt entsprechend nur noch mit einem Zwanzigstel
durch. Zusätzlich verwirft `_aggregate()` ab fünf Werten den **größten**
(getrimmtes Mittel), womit ein Einzelereignis praktisch verschwindet. Ein
Median wäre robuster, würde die rechtsschiefe Haushaltslast aber systematisch
unterschätzen und den Fahrplan zu knapp planen lassen.

`hourly_avg` behält das Schema `{7 Wochentage × 24 h}` für Panel und
Sensor-Attribute (die Gruppenwerte werden ausgefächert); gerechnet wird über
`bucket_avg` bzw. `hourly_for(dt)`. Der Weg über `hourly_for()` ist zwingend:
Ein Feiertag am Dienstag muss die `we`-Werte bekommen, über den
Wochentagsschlüssel käme der Werktagswert. Feiertage kommen aus dem Paket
`holidays` (Land aus der HA-Konfiguration); fehlt das Land, zählen nur Sa/So.

## Warum HiGHS statt optlang

`optlang` braucht `swiglpk`, und dafür gibt es keine musllinux-Wheels. Home
Assistant OS läuft auf Alpine (musl); pip müsste dort aus dem sdist bauen, und
SWIG, GLPK-Sourcen und ein C-Compiler sind im HA-Container nicht vorhanden —
die Requirements-Installation scheitert, die Integration lädt nicht.

`optlang.scipy_interface` ist kein Ausweg: es liefert keine Dual-Werte
(`Constraint.dual` wirft `NotImplementedError`), und `opt()` braucht sie für
`ac_price`, `bat_price` und `dc_price`.

Der Adapter stellt genau die vier Namen bereit, die `opt()` aus optlang holt.
Gemessen auf der synthetischen Testanlage (36 h, 15-Minuten-Raster, 144 Slots):
**alle 13 Spalten identisch bis 1e-6**, Dual-Werte eingeschlossen, bei 0,03 s
statt 1,31 s Rechenzeit.

## Installation zum Testen

HACS kann keinen beliebigen Branch ziehen. Drei Wege:

1. **Ordner kopieren** (am schnellsten): `custom_components/eeg_energy_optimizer/`
   aus diesem Repo nach `/config/custom_components/` im Home Assistant kopieren
   (Samba- oder Studio-Code-Server-Add-on), dann HA neu starten.
2. **HACS als Custom Repository**: dieses Repo als Integration hinzufügen. Vorher
   die produktive Installation entfernen — gleiche Domain.
3. **Pre-Release-Tag** `-dev` setzen und in HACS „Beta-Versionen anzeigen"
   aktivieren.

Beim ersten Start installiert HA `pandas` und `highspy` nach (rund 20 MB
Download); das kann den ersten Ladevorgang um ein bis zwei Minuten verlängern.

## Bedienung

Im Dashboard zeigt die **Fahrplan-Statuskarte** die aktuelle Aktion der
Steuerung („Laden begrenzt auf x kW", „Entladung y kW bis z %",
„Normalbetrieb", „Anzeige-Modus") samt den letzten Laufzeiten der Jobs
(Fahrplan, Steuerung, Verbrauchsprofil, PeakShare). Die Karte **„Fahrplan"**
darunter zeigt das Diagramm und die nächsten Slots; der Fahrplan wird jede
Minute neu gerechnet, „Neu rechnen" löst einen Lauf sofort aus.

Wenn kein Fahrplan zustande kommt, nennt die Karte den Grund — meist fehlt das
Verbrauchsprofil (Recorder-Historie noch zu kurz), die PV-Prognose-Zeitreihe
oder der Batterie-Ladestand.

Abschalten ohne Codeänderung: `schedule_enabled` auf `false` in der
Konfiguration.

## Einstellungen

Einstellbar im Panel unter *Einstellungen → Tarife* bzw. *Anlage* (die beiden
Parameter-Tabs entsprechen den Wizard-Schritten „Tarife & Gemeinschaft" und
„Anlage & Batterie"); die Einspeisegrenze liegt im Anlage-Tab.
Die Preis-Defaults sind österreichische Richtwerte (Stand 2026);
alle Werte sind additiv, die Config-Entry-Version bleibt unberührt.

| Schlüssel | Default | Bedeutung |
|-----------|---------|-----------|
| `schedule_enabled` | `true` | Fahrplan überhaupt rechnen |
| `schedule_time_res_min` | 15 | Auflösung des Fahrplans in Minuten |
| `schedule_interval_min` | 1 | Wie oft neu gerechnet wird. Ein Lauf kostet rund 40 ms im Executor. |
| `schedule_horizon_hours` | 36 | Vorausschau in Stunden |
| `schedule_worst_case_factor` | 0.6 | Worst-Case-PV als Anteil des Erwartungswerts — greift nur ohne Solcast-p10 |
| `inverter_ac_limit_kw` | *(pv_peak_kwp)* | **AC-Grenzleistung des Wechselrichters** — im Panel unter den Anlagendaten einstellbar. Ohne Angabe wird die PV-Spitzenleistung genommen, sonst 10 kW. |
| `schedule_feedin_source` | `manual` | Woher die Standardvergütung kommt: `manual` oder `oemag` (monatlicher Einspeisetarif, aus der HTML-Tabelle von oem-ag.at gelesen, siehe `oemag.py`) |
| `schedule_feedin_price` | 0.082 | Standardvergütung je kWh — gilt bei `manual` und als Rückfall, wenn der OeMAG-Wert fehlt |
| `schedule_feedin_price_night` | 0.102 | Zweiter Einspeisetarif für das Nachtfenster. 0 = nur ein Tarif. |
| `schedule_night_start` / `_end` | 22:00 / 06:00 | Nachtfenster, darf über Mitternacht gehen |
| `schedule_consumption_price` | *(0.2467)* | Bezugspreis je kWh. Ohne Angabe Einspeisung + `schedule_grid_fee`. |
| `schedule_grid_fee` | 0.1647 | Aufschlag vom Einspeise- auf den Bezugspreis — **nicht** das Netzentgelt allein |
| `schedule_battery_cost` | 0.01 | Alterungskosten je kWh Durchsatz |
| `lookback_weeks` | 4 | Rückblick des Verbrauchsprofils. Gemittelt wird über zwei Gruppen (Werktag Mo–Fr, Wochenende samt Feiertagen), der höchste Wert je Stunde wird verworfen. |
| `schedule_min_soc_pct` | 10 | **Mindest-Ladestand** (0–30 %) — die Sicherheitsreserve der Batterie, umgesetzt als fehlende Kapazität und damit harte Untergrenze in jedem Slot. Der Backup-Ladestand des Wechselrichters (`number.batteries_backup_power_ladestand`) wird eingelesen; der höhere Wert gewinnt. |
| `schedule_max_soc_pct` | 100 | **Maximum-Ladestand** (70–100 %) — darüber plant der Fahrplan nicht; 100 heißt bis voll laden. Kein eigener Ein/Aus-Schlüssel (seit v27): der Zustand steckt allein im Wert. Gegenstück zum Mindest-Ladestand, aber mit einem Schritt mehr: `battery_free` endet in `opt()` bei 0 und hat dort keinen Parameter, deshalb rechnet das Modell im verschobenen Fenster `[Boden, Deckel]` (`HAConfig`) und der Ladestand wird beim Auslesen zurückgerechnet (`solve()`). Eine 0 gilt als leeres Feld, nicht als Deckel von 0 %. |
| `peakshare_community` / `_2` | `BEG` / — | Bis zu zwei Energiegemeinschaften für die Preisfunktion |
| `peakshare_share_pct` / `_pct_2` | 0 / 0 | Anteil am Aufteilungsschlüssel, Summe höchstens 100 %. Was nicht zugeordnet ist, geht zur Standardvergütung an den Energieversorger. **0 = wirkt nicht auf den Fahrplan.** |
| `peakshare_price` / `_2` | 0.102 | Vergütung der Gemeinschaft am Tag |
| `peakshare_price_night` / `_night_2` | — | Vergütung im Nachtfenster; leer = wie am Tag |
| `peakshare_weight` / `_weight_2` | 0.01 / 0 | Zusätzliche Gewichtung je kWh (kein Geldfluss — der EEG-Bezieher spart Netzgebühren) |

**Entfallen mit v25:** `peakshare_surplus_override` / `peakshare_surplus_delta`.
Damit ließ sich die Amplitude des Überschussabschlags nach unten verschieben —
ein Auf- oder Abschlag auf die Tarifdifferenz. Der Abschlag selbst bleibt und
hängt wieder allein an dieser Differenz, genau spiegelbildlich zum Aufschlag
bei Bedarf: gibt es eine Gemeinschaft, ergibt er sich von selbst, gibt es
keine, gibt es auch keinen Abschlag. Ein Regler dafür war einer mehr, als die
Sache braucht.

**Entfallen mit v24:** `schedule_midday_discount_pct` / `_start` / `_end`.
Der Mittagsabschlag senkte den Einspeisepreis zwischen 10:00 und 14:00
rechnerisch um 20 %, damit die Batterie früher voll wird. Seit PeakShare V2
auch den **Überschuss** der Gemeinschaft liefert, gibt es dieselbe Aussage
gemessen statt geraten — hat die Gemeinschaft Überschuss, findet eingespeister
Strom dort keinen Abnehmer und ist weniger wert als in einer Bedarfsstunde.
Das Fenster ergibt sich damit aus dem Profil der jeweiligen Gemeinschaft,
nicht aus einer festen Uhrzeit.

**Entfallen:** `schedule_blackout_reserve_kwh` und `schedule_blackout_hours`
als *Optionen* — seit Migration v21 stehen sie auch nicht mehr in der
Konfiguration. Was daraus geworden ist, sind zwei getrennte Dinge:

* Der **Reserve-Deckel** (`max_blackout_reserve`) steht fest auf 0. Die
  getrennte Notstromreserve ist im Mindest-Ladestand aufgegangen; nachts gibt
  es dadurch keine Untergrenze und die Einspeisung bleibt frei.
* Das **Vorschaufenster** (`blackout_time`) steht seit 1.5.28 fest auf
  **18 Stunden** (`BLACKOUT_LOOKAHEAD`). Bis dahin stand dort ein einziger
  Slot, mit der Begründung, `bor` falle damit überall auf null.

**Diese Begründung war falsch, und der Fehler ist es wert, festgehalten zu
werden:** Sie wurde an einem sonnigen Tag geprüft, und dort stimmt sie —
`bor` ist der größte *kumulierte* Fehlbetrag ab einem Zeitpunkt, und liegt
ein voller PV-Tag zwischen ihm und der Nacht, wird die Kumulation nie
positiv. An wechselhaften Tagen liegt eben kein voller PV-Tag dazwischen.
Gemessen an echten Anlagendaten, ein Slot gegen 18 Stunden:

| Tag | Ladestand 10:00 | tiefster Ladestand | Erlös / Bezug |
|---|---|---|---|
| sonnig | unverändert | unverändert | unverändert |
| 80 % PV | unverändert | unverändert | unverändert |
| 40 % PV | 49 % statt 19 % | 21,6 % statt 5,0 % | unverändert |
| 25 % PV | 69 % statt 21 % | 36,1 % statt 20,9 % | unverändert |

An guten Tagen ändert sich nichts, an schlechten hält der Fahrplan die
Batterie erheblich voller — und zwar **zum selben Preis**: Export, Erlös und
Netzbezug sind in jeder Wetterlage bis auf die dritte Nachkommastelle
gleich. Es verschiebt sich nur, wann die Energie im Speicher liegt.

Damit greifen die beiden Puffer an verschiedenen Tagen: der
**Überschussabschlag** an sonnigen (dort hat die Gemeinschaft mittags Überschuss
und der Fahrplan lädt statt einzuspeisen), das **18-Stunden-Fenster** an trüben
(dort gibt es keinen Überschuss zu verteilen, der Abschlag greift ins Leere).
| `discharge_power_kw` | 5.0 | **Batterie-Leistungsgrenze** des Fahrplans (Label im Panel; Schlüssel aus der produktiven Integration umgedeutet) |
| `grid_export_limit_enabled` | `false` | Einspeisegrenze beachten — fließt ins LP-Modell ein und aktiviert die Ladelimit-Nachführung |
| `grid_export_limit_kw` | 4.0 | Höhe der Einspeisegrenze |

## Tests

```
pip install -r requirements_test.txt
pytest tests/
```

`optlang` in `requirements_test.txt` dient nur dem Referenzvergleich gegen GLPK
und lässt sich auf musl-Systemen nicht installieren — der Vergleichstest
überspringt sich dort selbst.

## Erster Lauf an einer echten Anlage

Anlage Traun (15 kWh Speicher, Solcast, 77 % Ladestand, 36 Stunden Vorausschau):
144 Slots in 40 ms. PV-Erwartung 45,8 kWh, p10-Pfad 25,0 kWh, Verbrauchsprognose
29,4 kWh.

Der Fahrplan findet die Morgen-Einspeisung von selbst — vormittags lädt er nicht,
der Überschuss geht ins Netz — und lädt erst ab dem späten Vormittag, wenn die
Batterie noch bis Sonnenuntergang voll wird. Nachts entlädt er sanft nur den
Hausverbrauch statt mit voller Leistung.

Was er **nicht** tut: abends ins Netz einspeisen. In der Abendspitze entlädt er
1,5 kW und kauft den Rest zu. Zwei Gründe: ohne EEG-Preissignal ist Eigenverbrauch
(0,26 €/kWh vermiedener Bezug) mehr wert als Einspeisung (0,097 €/kWh), und die
Endbedingung am Horizont hält Ladestand zurück. Der Fahrplan war damit
batterieschonender, aber EEG-seitig schwächer als die alte Zustandslogik — die
Preisfunktion ist kein Feinschliff, sondern der Kern. (Messung aus der
Vorschau-Phase; seit den zwei Einspeisetarifen speist der Fahrplan nachts ein,
siehe unten.)

## Was die Preise bewirken — und was nicht

Die Preisvorgaben stammen aus Haralds `config_dummy.py`: `feedin_price = 0.0973`,
`grid_fee = 0.1647`, Bezugspreis ist die Summe (0,262 €/kWh). Das sind seine
Tarifwerte, keine allgemeingültigen — bei uns konfigurierbar über
`schedule_feedin_price` und `schedule_grid_fee`.

Gerechnet an der Anlage Traun, 36 Stunden, Werte in kWh:

| Einspeisung / Bezug | Einspeisung | Netzbezug | davon 17–23 Uhr | max. Entladung |
|---|---|---|---|---|
| 9,7 / 26,2 ct (Haralds Defaults) | 24,3 | 6,5 | 0,3 | 2,41 kW |
| 12 / 22 ct | 24,3 | 6,5 | 0,3 | 2,41 kW |
| 15 / 20 ct | 24,4 | 6,7 | 0,3 | 1,78 kW |
| 20 / 20 ct | 33,3 | 15,6 | 0,5 | 0,24 kW |
| 30 / 20 ct | 33,3 | 15,6 | 0,5 | 0,24 kW |

Ein **höheres Preisniveau bewirkt nichts** — solange der Preis über den Tag
konstant ist, gibt es keinen Grund, Energie in den Abend zu verschieben. Fällt der
Spread ganz weg (20/20), wird die Batterie sogar unbenutzt: ohne Preisdifferenz
sind Wandlungs- und Alterungsverluste der einzige Effekt, und der Fahrplan lässt
alles direkt durchs Netz laufen.

Ein **zeitabhängiges** Signal wirkt dagegen sofort:

| Signal | Einspeisung | Netzbezug | davon im Fenster | max. Entladung |
|---|---|---|---|---|
| 18–22 h 30 ct, sonst 8 ct | 24,4 | 7,0 | **7,8** | 5,00 kW |
| 18–22 h 50 ct, sonst 8 ct | 24,6 | 7,2 | **8,0** | 4,91 kW |
| 06–10 h 50 ct, sonst 8 ct | 25,6 | 8,0 | **14,3** | 2,52 kW |

Auch ein kleiner Unterschied genügt. Mit zwei echten Tarifen (8,2 ct tags,
10,2 ct für Nachteinspeisung, Fenster 22–06 Uhr) speist der Fahrplan **7,7 kWh
nachts** ein statt 0 — die Menge wird dann von der Batterie und der Endbedingung
begrenzt, nicht vom Preis: ein hypothetischer Nachttarif von 15 ct ändert nichts
mehr. Auch die Alterungskosten (0 bis 1 ct/kWh) verschieben nichts.

Der Bezugspreis ist dagegen fast bedeutungslos: von 18 bis 28 ct durchgerechnet
kommt jedes Mal derselbe Fahrplan heraus. Solange er klar über der Einspeisung
liegt, bleibt die Rangfolge dieselbe (Eigenverbrauch vor Einspeisung). Erst wenn
er in deren Nähe kommt, kippt das Verhalten.

Mit einem Abendfenster entlädt der Fahrplan mit voller Leistung ins Netz, mit
einem Morgenfenster hält er die Batterie leer und speist den PV-Überschuss ein —
Nacht-Entladung und Morgen-Einspeisung entstehen also von selbst, ohne eine Regel
dafür. Damit ist die Preisfunktion für die EEG kein Feinschliff: sie ist der
Hebel, über den unsere beiden Kernfunktionen im Fahrplan überhaupt entstehen.

## Gemessen: Entlade-Nachführung

**Frage:** `forcible_discharge` nimmt seinen Leistungswert vermutlich als
DC-Leistung der Batterie, eingespeist wird AC — fehlt deshalb systematisch
Einspeisung, und muss `GUARD_DISCHARGE_EFFICIENCY` das ausgleichen?

**Antwort: nein.** Es fehlen konstant **59 W**, unabhängig von der befohlenen
Leistung. Das ist ein fester Abzug, kein Wirkungsgrad.

**Datenbasis:** Nacht vom 24. auf den 25.08.2026, 20:55–05:46 — die einzige
Nacht mit geplanter Entladung in der Historie der Fahrplan-Sensoren. 63
Abschnitte, in denen der geschriebene Sollwert mindestens 150 s konstant
stand; die ersten 90 s nach jedem Befehlswechsel verworfen, sonst steht der
alte Messwert neben dem neuen Befehl (daher Einzelverhältnisse bis 4,5).

| Befehl | Ist | Verhältnis | Differenz |
|---|---|---|---|
| 0,86 kW | 0,80 kW | 0,931 | −0,059 kW |
| 0,94 kW | 0,88 kW | 0,937 | −0,059 kW |
| 1,17 kW | 1,11 kW | 0,946 | −0,063 kW |
| 1,71 kW | 1,65 kW | 0,967 | −0,057 kW |

Der Beleg steckt in der **Differenz**, nicht im Verhältnis: das Verhältnis
wandert von 0,93 auf 0,97, die Differenz bleibt bei −0,059 kW. Ein
Wirkungsgrad von 0,93 müsste bei 1,71 kW ein Defizit von 0,12 kW erzeugen.
Theil-Sen über alle Abschnitte: Steigung **0,987** bei b = −0,048 kW. Im
Modellvergleich lässt „fester Abzug" 3,2 W Restfehler übrig, „Faktor" 6,1 W.

**Warum die Konstante trotzdem auf 1,0 bleibt:** Ein fester Abzug lässt sich
durch eine Division nicht abbilden — durch 0,93 geteilt schlüge sie bei 5 kW
317 W auf, wo 59 W fehlen. Zudem liegen 59 W unter dem Totband von 200 W, die
Korrektur würde meist gar nicht geschrieben.

**Was damit NICHT beantwortet ist:** Der Vergleich zeigt, dass der
Wechselrichter dem Befehl folgt. Er zeigt nicht, dass die Einspeisung am Netz
zum Plan passt — `Hausverbrauch` wird als `PV − Batterie − Netz` gerechnet,
ein Vergleich über das Netz wäre zirkulär. Die DC/AC-Frage im engeren Sinn
bräuchte einen unabhängigen AC-seitigen Messwert.

**Nebenbefund:** Drei Abschnitte, in denen die Batterie dem Befehl grob nicht
folgte, lagen alle am Ladestands-Boden gegen Ende der Nacht — 25.08. 05:33
befahl der Fahrplan 1,81 kW, abrufbar waren 0,28 kW (SOC 15,0 % gegen Ziel
14,3 %). Erwartetes Verhalten am Anschlag, aber Plan und Gerät laufen in der
letzten Stunde auseinander.

## Offene Punkte

> **Erledigt seit 1.5.17:** die Preisfunktion für die EEG. `feedin_price()`
> liefert jetzt eine Zeitreihe, in der der Bedarf der Gemeinschaften als
> Aufschlag steckt — gebaut in `eeg_price.py`, Datenquelle ist PeakShare.

* ~~**Endbedingung am Horizont**~~ — **gemessen, unkritisch.** `opt()` fixiert
  die Batterie am letzten Slot auf halben Ladestand (`battery_free =
  capacity / 2`); sie bindet exakt (letzter Slot 52,50 % bei 15 kWh und 5 %
  Mindest-Ladestand). Die frühere Vermutung, das dämpfe die Abendentladung
  merklich, ist **widerlegt**: Bei gekürztem Horizont bleibt der heutige Abend
  Slot für Slot identisch — 48 h, 36 h und 24 h ergeben denselben Verlauf
  (20:00 60,3 % / 00:00 23,9 % / 04:00 15,5 %). Erst bei 12 h greift die
  Endbedingung in die Nacht (dann 00:00 bei 52,5 %, keine Einspeisung). Ihre
  Reichweite sind also die letzten rund 12 Stunden des Horizonts. Da der
  Fahrplan minütlich neu gerechnet wird, wandert dieser Bereich ständig mit
  und erreicht nie die Stunden, die tatsächlich ausgeführt werden. **Wichtig
  bleibt nur:** der Horizont darf nicht unter etwa 24 Stunden fallen.
* ~~**DC oder AC**~~ — **bewusst nicht verfolgt** (Entscheidung 26.08.2026).
  Möglicherweise zählen wir die Wandlungsverluste doppelt: `opt()` erwartet
  DC am Modul und rechnet selbst mit `ac_efficiency = 0,95`, Solcast schätzt
  AC nach Wechselrichter. Beziffert wurde es (PV vor der Übergabe durch 0,95
  geteilt): geplanter Export 42,6 statt 38,6 kWh je 48 h. Der Ladeverlauf
  ändert sich dabei nicht — es geht um die geplante Menge, nicht um das
  Timing, und der Fahrplan plant dann eben etwas vorsichtiger. Da eine
  Korrektur ohne geklärte Solcast-Konvention geraten wäre und ein falsches
  Vorzeichen den Fehler verdoppelte, bleibt es wie es ist.
* ~~**Weitere Treiber steuern**~~ — **kein offener Punkt, sondern der
  bekannte Stand.** Gesteuert wird nur Huawei (`supports_schedule_control`).
  Fronius, Kostal, SMA und SolaX hätten stufenlose Steuerwege, SolarEdge nur
  eine grobe Modusumschaltung. Sie rechnen und zeigen an; das ist bewusst so
  und wartet auf Zeit und Geräte, nicht auf eine Erkenntnis.
* ~~**Entlade-Nachführung oberhalb 2 kW**~~ — **geschlossen** (Entscheidung
  26.08.2026). Gemessen ist die Nachführung nur zwischen 0,47 und 1,86 kW;
  dort fehlen konstant 59 W. Oberhalb gibt es keine Messpunkte, und ein
  quadratischer Verlustanteil, der sich in dieser Streuung verstecken könnte,
  bliebe selbst bei 5 kW unter etwa 50 W zusätzlich — also unter dem Totband
  von 200 W. Vor allem aber schließt sich die Schleife ohnehin über den
  **Ladestand**: liefert die Batterie zu wenig, bleibt der SOC höher, und der
  Planlauf eine Minute später verteilt die Energie neu. Nachgeregelt wird
  also die Energiebilanz; offen bliebe nur die Momentanleistung innerhalb
  eines Slots, und die ist bei 59 W ohne Belang.
* ~~**Bedarfsprofil aus der Historie**~~ — **erledigt, V2 ist angebunden.**
  Die Gemeinschaftsprognose reichte nur 24 Stunden, weshalb der Tagesverlauf
  wiederholt wurde (`get_hours`, Kopien mit `copied: True`). Der V2-Endpunkt
  (`/api/public/v2/community-grid-import-forecast`) liefert **192
  Viertelstunden über 48 Stunden** — die Kopien sind ersatzlos entfallen,
  ebenso die Grenze über die Wochenendkante hinweg. V1 wird nicht mehr
  unterstützt: er kennt den Überschuss nicht, auf dem die Preisfunktion jetzt
  aufbaut.
* ~~**Kurze PV-Prognose macht den Fahrplan blind für den zweiten Tag.**~~ —
  **erledigt: der Horizont endet jetzt am Prognoseende**
  (`_horizont_aus_wh_hours`). Forecast.Solar liefert je nach Zugang nur heute
  und morgen; fehlende Stunden kamen als 0 kW an (`_production_from_wh`), und
  das ist für `opt()` kein "unbekannt", sondern die Zusage "hier scheint
  garantiert keine Sonne". Abends fehlten so bis zu 20 der 48 Stunden. Der
  Horizont läuft jetzt nur so weit wie die Prognose (um 20:07 sind das 27 h,
  um 06:30 noch 41 h); Solcast bleibt bei 48 h, weil es die Reichweite über
  die Tagessensoren immer erfüllt. Liegt die Prognose ganz in der
  Vergangenheit, gibt es keinen Plan mehr — der Failsafe im Executor gibt den
  Wechselrichter frei, was ehrlicher ist als ein Plan aus Nullen.

  **Die Nacht nach dem Prognoseende mitzunehmen ist naheliegend und falsch —
  nicht nochmal versuchen.** Dort ist die 0 zwar eine Tatsache, aber der
  letzte Slot ist auf halben Ladestand festgenagelt
  (`battery_free.iloc[-1] = capacity / 2`). Liegt dieser Nagel hinter einer
  Nacht, muss der Plan sie mit Reserve durchqueren; die Nachtstunden
  verlangen Vorsorge, ohne Ertrag beizusteuern. Gemessen (Start Mo 20:00,
  Export der ersten 24 h): Horizont 27 h → 16,25 kWh, 33 h ("bis Nachtende")
  → 12,98 kWh, 48 h → 12,98 kWh. Der Export fällt monoton mit jeder Stunde
  jenseits der Prognose.

  **Die Erwartung gehört geradegerückt: das ist eine Korrektheitskorrektur,
  kein Ertragsgewinn.** Die auslösende Zahl (19,0 statt 38,6 kWh geplanter
  Export je 48 h) misst überwiegend etwas Triviales mit — in einem Plan mit
  20 erfundenen dunklen Stunden ist schlicht weniger PV zum Einspeisen da.
  Rollend gemessen (48 h ab Mo 20:00, alle 15 min neu geplant, nur der erste
  Slot gefahren, Endbestand zum Bezugspreis bewertet):

  | Wetter | EEG-Export | Gesamtwert |
  |---|---|---|
  | sonnig | ±0,00 kWh | ±0,000 € |
  | mittel | −0,86 kWh | +0,088 € |
  | trüb | +1,95 kWh | −0,199 € |

  In der Zielgröße (Einspeisung in den Bedarfsstunden) netto leicht positiv,
  in Euro etwa neutral — dasselbe Muster wie beim 18-Stunden-Fenster: es
  verschiebt vor allem, WANN die Energie im Speicher liegt. Richtig ist die
  Änderung trotzdem, weil sie eine falsche Eingabe aus dem Modell nimmt.
  **Merksatz für Messungen dieser Art:** der Unterschied im *Plan* ist groß,
  der in der *gefahrenen Realität* klein — bei minütlicher Neuplanung wird
  nur der erste Slot umgesetzt. Wer einen Planvergleich als Ertragsaussage
  liest, überschätzt sich; und ohne bewerteten Endbestand vergleicht man
  ungleiche Endzustände.

  **Noch nicht sichtbar:** die Reichweite steht als `forecast_source`
  ("forecast_solar (wh_hours, 27 h)") im Plan-Dict und im Log, das Panel
  liest das Feld aber nicht — die Stelle im Wizard zeigt die Konfiguration,
  nicht den gerechneten Plan. Wer den verkürzten Horizont anzeigen will, hat
  die Zahl bereits an der Hand.

  **Geprüft und unbedenklich:** das 18-Stunden-Fenster der Reserve
  verschlimmert das nicht — mit abgeschnittener Prognose liefern ein Slot und
  18 h bitidentische Pläne.
* **Upstream, betrifft uns nicht** — beide in `chamo/` und deshalb
  unangetastet: `timetableopt` greift auf `timetable.model.status` zu, das
  `opt()` nicht liefert (wir nutzen die Schleife nicht), und der
  `fullcharge_try`-Zweig setzt mit `battery_free[len-1] = 0` einen
  Integer-Schlüssel auf einer Serie mit Datetime-Index, was pandas 2 eher ein
  neues Element anlegen lässt als das letzte zu überschreiben (wir lassen
  `fullcharge_try` auf False). Beides gehört an Harald gemeldet, nicht von
  uns geändert.
