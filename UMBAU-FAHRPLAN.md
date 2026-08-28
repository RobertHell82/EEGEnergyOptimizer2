# Umbauplan: Fahrplan als einziger Aktor

> **Status: umgesetzt.** Alle 7 Schritte sind committet (b1909a6…953be6a),
> veröffentlicht als **`1.5.1-chamo`** (statt der hier geplanten `1.6.0-chamo`,
> Entscheidung vom 24.08.). Dieses Dokument bleibt als Arbeitsgrundlage und
> Begründungsarchiv stehen.
>
> Ausgangsstand war: `1.5.0-chamo`, Tag `v1.5.0-chamo` gepusht, GitHub-Release
> noch nicht angelegt.

## Zielbild

Die Zustands-Heuristik (Morgen-Einspeisung, Nacht-Entladung,
Einspeisebegrenzung) verschwindet vollständig. Der LP-Fahrplan aus `chamo/` wird
der einzige Aktor:

* **Jede Minute** rechnet der `ScheduleRunner` einen Fahrplan über 48 Stunden.
* **Alle 30 Sekunden** hält ein neuer Guard-Lauf den zuletzt gerechneten
  Fahrplan gegen die Messwerte und steuert nach.
* Gesteuert wird **nur Huawei**, und zwar Ladelimit **und** erzwungene
  Entladung. Die anderen fünf Treiber bleiben vollständig im Code und im UI,
  rechnen und zeigen an, steuern aber nicht (`supports_schedule_control = False`).

Nicht verhandelbar: `chamo/` bleibt unangetastet.

> **Überholt (26.08.):** Die zweite Randbedingung — Config-Entry-Version bleibt
> 20, gespeicherte Schlüssel werden nie gelöscht — galt für einen Rückwechsel
> auf die produktive Integration. Der ist nicht mehr vorgesehen. Migration
> **v21** entfernt die dreizehn Altschlüssel der Heuristik aus `entry.data`;
> `discharge_a_start_time` bleibt, weil `sensor.py` ihn für den Tag/Nacht-Split
> der Verbrauchsprofil-Anzeige liest.

---

## 1. Die zwei Guards

### Guard 1 — Ladelimit anheben, wenn die Einspeisung am Limit klebt

Läuft **nur**, wenn alle drei Bedingungen gelten:

1. Der laufende Slot plant Laden (`battery_p < 0`).
2. `grid_export_limit_enabled` ist an **und** `grid_export_limit_kw > 0`.
3. Die gemessene Einspeisung liegt innerhalb ±100 W an dieser Grenze.

Dann:

```
aktuell = await inverter.async_get_charge_limit_kw()
neu     = max(fahrplanwert, (aktuell or fahrplanwert) + 500 W)
neu     = min(neu, inverter.get_charge_limit_max_kw() or neu)
```

Das deckt die drei Fälle ab: aktuell > Plan → aktuell + Schritt; aktuell ==
Plan → + Schritt; Plan > aktuell → Planwert.

**Warum schrittweise und nicht einmal rechnen:** Bei aktiver Abregelung ist die
gemessene PV-Leistung bereits beschnitten. „PV − Hauslast − Grenze" fällt dann
systematisch zu klein aus und würde im nächsten Takt wieder kleben.

**Rücknahme mit Hysterese:** Klebt der Export im ±100-W-Band → pro Lauf ein
Schritt hoch. Liegt der Export unter Grenze − 300 W → pro Lauf ein Schritt
Richtung Fahrplanwert zurück, nie darunter. Im Band dazwischen (Grenze − 300 bis
Grenze − 100 W) nichts tun; das asymmetrische tote Band verhindert Pendeln.

Ohne aktivierte Einspeisegrenze wird schlicht der Fahrplanwert gesetzt.

### Guard 2 — erzwungene Entladung nachführen

Bedingung: Der laufende Slot plant Einspeisung aus der Batterie (`battery_p > 0`
und `grid_p > 0`).

```
entladeleistung = slot["grid_p"] + gemessene Hauslast
entladeleistung = min(entladeleistung, max. Entladeleistung der Batterie)
ziel_soc        = slot["soc"]        # SOC am ENDE des laufenden Slots
await inverter.async_set_discharge(entladeleistung, target_soc=ziel_soc)
```

**Warum `grid_p` und nicht `battery_p`:** `forcible_discharge` gibt die Leistung
an, die die Batterie abgibt. Davon deckt der Wechselrichter zuerst den
Hausverbrauch, nur der Rest wird eingespeist. In `battery_p` steckt bereits die
*prognostizierte* Hauslast — ein Aufschlag der Messung würde sie doppelt zählen.
Mit `grid_p` + gemessener Hauslast kommt die geplante Einspeisung tatsächlich am
Netzanschluss an, auch wenn die reale Last von der Prognose abweicht.

**Ziel-SOC ist der Wert des laufenden Slots, nicht des Folge-Slots.** Belegt über
die Batteriebilanz in `chamo/opt_highs.py`: `battery_constr` erzwingt
`battery_free[t] = battery_free[t-1] + battery_p[t] · dt`, der Wert eines Slots
beschreibt also den Zustand an seinem **Ende**. (Der Test
`test_fahrplan_ist_energetisch_konsistent` prüft diese Kette, nicht die
Zuordnung — er hätte einen Versatz nicht gefunden.)

Weitere Regeln: Wirkungsgradkorrektur (Batterie gibt DC ab, eingespeist wird AC)
als Konstante anlegen und nach dem ersten Testtag anhand der Messung kalibrieren;
Hauslast-Messwert nicht lesbar → Rückfall auf `slot["consumption"]` (fail-open,
geloggt); Wechsel von Einspeisung auf keine Einspeisung → `async_stop_forcible()`;
Huawei floort den Ziel-SOC intern bei 12 %.

### Not-Aus (Empfehlung: gleich mitbauen)

Mit dem Grid-Import-Watchdog fällt jede Absicherung der Entladung weg. Wenn der
Netz-Sensor ein falsches Vorzeichen liefert oder die Hauslast dauerhaft über der
Entladeleistung liegt, kauft die Anlage Strom für 25 Cent, um ihn für 10 Cent zu
verkaufen — und niemand bricht ab. Vorschlag: Netzbezug über 1 kW in drei
aufeinanderfolgenden Guard-Läufen → `async_release()`, Sperre bis zum nächsten
Slotwechsel. Rund 20 Zeilen im Executor.

### Erster Stützpunkt mit Messwerten

`async_collect_inputs()` überschreibt den ersten Punkt der Zeitreihen mit
gemessenen Werten: `production[0]` und `min_production[0]` aus
`power_readings.compute_pv_now_kw()`, `consumption[0]` aus dem neuen
`compute_house_load_kw()`. Nur überschreiben, wenn der Messwert nicht `None` ist.

Begründung: Für die nächsten 15 Minuten ist die aktuelle Messung der beste
Schätzer, und nur der erste Slot wird gefahren — die späteren dienen der
Vorausschau und dürfen bei der Prognose bleiben. `opt()` interpoliert bis zum
nächsten 30-Minuten-Stützpunkt zurück auf die Prognose.

---

## 2. Was wegfällt

### Backend

| Datei | Was | Umfang |
|---|---|---|
| `optimizer.py` | **komplette Datei** — `Snapshot`, `Decision`, `EEGOptimizer` mit Zustandsbewertung, allen Guards, Markdown-Aufbau, `_execute`, `async_run_cycle` | 2.671 Z. |
| `statistics.py` | **komplette Datei** — `FeedinStatistics`, Sessions, Outcome-Skalierung | 756 Z. |
| `__init__.py` | Optimizer-Zyklus (`_optimizer_cycle`), Block-State-Store, State-Change- und Snapshot-Telemetrie, Feedin-Statistik-Verdrahtung, Snapshot-Timer, `_build_state_change_payload`, `_build_snapshot_payload`, `_build_block_predictions`, `_normalize_state` | ~600 Z. |
| `peakshare.py` | `find_discharge_window`, `get_discharge_plan`, `get_jitter_today`, Planzustand. **Bleiben:** `async_fetch`, Cache/Store, `get_communities`, `_validate_api_response` | −330 Z. |
| `websocket_api.py` | `ws_start/stop_manual_discharge`, `ws_get_manual_override`, `ws_set/get/clear_test_overrides`, `ws_get_feedin_statistics`, Slot-XOR- und Dual-Window-Validierungen in `ws_save_config`, `plan_info` in `ws_get_peakshare_data`. **Bleiben:** `ws_manual_stop`/`_discharge`/`_block_charge` (Testbefehle der Karte „Manuelle Steuerung") | −380 Z. |
| `sensor.py` | `FeedinEnergySensor` samt Instanzen; `EntscheidungsSensor` wird zum Fahrplan-Statussensor (gleiche `unique_id`, damit Entität und Verlaufshistorie bleiben) | −120/+80 Z. |
| `const.py` | Zustandsnamen, Guard-Konstanten, Dual-Slot- und Einspeisebegrenzungs-Schlüssel (Liste in Abschnitt 4) | −110 Z. |
| `telemetry.py`, `telemetry_buffer.py`, `coordinator.py`, `forecast_provider.py`, `config_flow.py`, `select.py` | **unverändert.** Zustands- und Outcome-Meldungen bleiben als Code, werden nur nicht mehr aufgerufen | 0 |

### Panel (`frontend/eeg-optimizer-panel.js`)

| Bereich | Was |
|---|---|
| Statuskarten | `_renderStatusCards`, `_renderMorningConditions`, `_renderDischargeConditions` → eine kompakte Fahrplan-Statuskarte |
| Manuelle Entladung | `_renderManualDischargeDialog`, Banner, Actions, Loader |
| Simulation | Test-Overrides, Sim-Banner, Wizard- und Settings-Schalter |
| EEG-Statistik | Karte, Balken- und Monatsdiagramme (kommt mit der Nacht-Statistik zurück) |
| Aktivitätsprotokoll | Detailfelder und Filter auf die neuen Zustände umstellen |
| PeakShare-Karte | bleibt vollständig; Hinweistext: „Anzeige — Steuerung übernimmt der Fahrplan" |
| Manuelle Steuerung | bleibt (Wechselrichter-Testbefehle) |
| Statuskarte oben | **bleibt**, inhaltlich umgestellt: Modus-Umschalter, Ladestand, Prognosen bleiben; Zustand zeigt künftig „Laden begrenzt auf x kW" / „Entladung y kW auf Ziel-SOC z" / „Normalbetrieb" / „Anzeige-Modus". **Neu:** Zeile mit den letzten Laufzeiten der Jobs (Fahrplan 1 min, Steuerung 30 s, Verbrauchsprofil 15 min, PeakShare stündlich) — Zeile einfärben, wenn ein Job länger als sein Takt nicht gelaufen ist. Daten liegen alle vor: `ScheduleRunner.last_run`, Executor-Status, `coordinator.last_update_iso`/`last_duration_ms`, PeakShare-Cache-Alter |

---

## 3. Was neu gebaut wird

| Baustein | Inhalt | Umfang |
|---|---|---|
| `inverter/base.py` | `supports_schedule_control` (Property, Default `False`), `async_get_charge_limit_kw()` → `None`, `get_charge_limit_max_kw()` → `None`, `get_max_discharge_power_kw()` → `None` | +45 Z. |
| `inverter/huawei.py` | Umsetzung der vier: `supports_schedule_control = True`; Ladelimit über den State der via `_ensure_charge_entity` aufgelösten Number-Entität lesen (W→kW, bei mehreren Batterien das **Minimum**, weil `async_set_charge_limit` denselben Wert auf alle schreibt); Maxima aus `_get_max_charge_power` bzw. `_read_max_discharge_power_w` (Summe über Geräte) | +60 Z. |
| `schedule_executor.py` (neu) | `plan_action(result, now) -> PlanAction \| None` übersetzt den Slot treiberneutral in Absicht (`charge_limit` / `discharge` / `release`); `ScheduleExecutor` mit `async_guard_cycle(schedule_result, mode)`, `async_release()`, `status()`, beiden Guards, Totbändern, Grace Period, Failsafe. Entscheiden und Setzen strikt getrennt — Setzen/Lesen nur über `InverterBase` | +350 Z. |
| `power_readings.py` | `compute_house_load_kw(hass, config)` — dieselbe Formel wie der Hausverbrauchs-Sensor (PV − Batterie − Netz, Vorzeichen über `resolve_sign`, auf ≥ 0 begrenzt), aber im 30-Sekunden-Takt direkt lesbar | +55 Z. |
| `schedule.py` | Messwerte für den ersten Stützpunkt; gemeinsamer `slot_for()`-Helfer (heute in `sensor._aktueller_slot` dupliziert); Umstellung auf die neuen Export-Schlüssel; Notstrom-Ladestand des Wechselrichters als Untergrenze einlesen | +50 Z. |
| `__init__.py` | 30-Sekunden-Timer ruft `_guard_cycle` (Modus aus Select, Executor, Statussensor, Aktivitätslog, Telemetrie-Watchdogs, Log-Flush); Executor statt Optimizer erzeugen; `executor.async_release()` beim Entladen der Integration; Failure-Callback auf Schreibfehler des Executors | +150 Z. |

### Totbänder und Konstanten

| Konstante | Wert | Zweck |
|---|---|---|
| `GUARD_EXPORT_STICKY_BAND_KW` | 0,1 | „klebt am Limit"-Band (±100 W) |
| `GUARD_CHARGE_STEP_KW` | 0,5 | Anhebeschritt Guard 1 — im 30-s-Takt 1 kW/min Aufholrate, klein genug, dass ein Überschwinger im nächsten Takt korrigierbar bleibt |
| `GUARD_EXPORT_RELEASE_KW` | 0,3 | Rücknahme erst unter Grenze − 0,3 kW |
| `EXECUTOR_CHARGE_DEADBAND_KW` | 0,2 | Ladelimit nur bei > 200 W Änderung schreiben |
| `EXECUTOR_DISCHARGE_DEADBAND_KW` | 0,2 | Entladeleistung nur bei > 200 W Änderung schreiben |
| `EXECUTOR_TARGET_SOC_DEADBAND_PCT` | 1,0 | Ziel-SOC nur bei ≥ 1 Prozentpunkt schreiben |
| `SCHEDULE_FAILSAFE_MINUTES` | 15 | Fahrplan fehlt/fehlerhaft/älter → einmalig `async_release()` |
| `STARTUP_GRACE_SECONDS` | bestehend | **bleibt** — wandert von der Zustandsausführung in den Guard-Lauf. Wird hier wichtiger als vorher: früher wurde nur bei Zustandswechsel geschrieben, jetzt jede Minute |

---

## 4. Konfigurationsschlüssel

### Entfallen (nur die Konstanten im Code — gespeicherte Werte bleiben in `entry.data`)

`CONF_ENABLE_MORNING_DELAY`, `CONF_ENABLE_NIGHT_DISCHARGE`,
`CONF_ÜBERSCHUSS_SCHWELLE`, `CONF_MORNING_START_OFFSET`,
`CONF_MORNING_END_TIME`, `CONF_MIN_SOC`, `CONF_SAFETY_BUFFER_PCT`,
`CONF_ENABLE_FEEDIN_LIMIT`, `CONF_FEEDIN_LIMIT_KW`, alle `FEEDIN_*`-Regler-
Konstanten, `MIN_SOC_BLOCK_EXIT_HYSTERESIS_PCT`, `CONF_ENABLE_DUAL_DISCHARGE`,
`CONF_ENABLE_SLOT_A/B`, `CONF_DISCHARGE_A/B_START_TIME`,
`CONF_DISCHARGE_B_END_CAP`, `STATE_MORGEN_EINSPEISUNG`, `STATE_NORMAL`,
`STATE_ABEND_ENTLADUNG`, `STATE_EINSPEISEBEGRENZUNG`,
`GRID_IMPORT_PAUSE_MINUTES`, `MANUAL_OVERRIDE_MAX_HOURS`,
`STATS_COMPACT_AFTER_DAYS`, `STATE_TO_STATS_KEY`, `CONF_ENABLE_SIMULATION`,
`MIN_BLOCK_OUTCOME_MINUTES`, `RESERVE_ENTRY_BONUS_PCT`,
`RESERVE_EXIT_HYSTERESIS_PCT`, zugehörige Defaults.

### Neu

| Schlüssel | Default | Zweck |
|---|---|---|
| `grid_export_limit_enabled` | `False` | Einspeisegrenze beachten — speist ins LP-Modell ein und aktiviert Guard 1. Bewusst **nicht** der alte Name, weil `enable_feedin_limit` einen eigenen Regler meinte |
| `grid_export_limit_kw` | 4,0 | Höhe der Grenze |
| ~~`schedule_blackout_hours`~~ | ~~18~~ | **Keine Option geworden, der Wert gilt aber.** Das Vorschaufenster steht seit 1.5.28 fest auf 18 h (`BLACKOUT_LOOKAHEAD`), nicht mehr auf einem Slot — an trüben Tagen hält der Fahrplan damit deutlich mehr im Speicher, bei unverändertem Erlös und Netzbezug. Der Reserve-*Deckel* bleibt 0: nachts keine Untergrenze, die Einspeisung bleibt frei. Messung in CHAMO.md |

**Kein** `schedule_control_enabled`: Ob gesteuert wird, entscheidet der
bestehende Select `select.eeg_energy_optimizer_optimizer`.

| Modus | Rechnen | Schreiben |
|---|---|---|
| **Ein** | jede Minute | Ladelimit und Entladung werden gesetzt |
| **Test** | jede Minute | nichts — Fahrplan wird nur angezeigt |

Beim Wechsel **Ein → Test** einmalig `async_release()`, sonst bleibt das letzte
Ladelimit im Wechselrichter stehen. Beim Wechsel auf Ein schreibt erst der
nächste Guard-Lauf (maximal 30 Sekunden Verzögerung).

### Bleiben (teils umgedeutet)

`CONF_DISCHARGE_POWER_KW` — **umgedeutet** zur Batterie-Leistungsgrenze des
Fahrplans (`battery_power_limit`); Label im Panel entsprechend umbenennen, der
Schlüssel bleibt gleich. `CONF_ENABLE_PEAKSHARE` / `CONF_PEAKSHARE_COMMUNITY` —
nur noch Anzeige. Alle `schedule_*`, alle Inverter- und Sensor-Schlüssel,
`CONF_PV_PEAK_KWP`, `CONF_INVERTER_AC_LIMIT_KW`, `MODE_*`,
`STARTUP_GRACE_SECONDS`, Telemetrie-Block, `CONSUMPTION_SENSOR`,
`WEEKDAY_KEYS`, `INVERTER_SIGN_CONVENTIONS`, `COMBINED_*`.

### Notstrom: zwei verschiedene Dinge

> **Überholt.** Eine eigene Notstromreserve gibt es nicht mehr. Der
> **Mindest-Ladestand** (`schedule_min_soc_pct`, 0–30 %) *ist* die
> Sicherheitsreserve und wirkt in `HAConfig` als harte Untergrenze;
> `schedule_blackout_reserve_kwh` und `schedule_blackout_hours` werden nicht
> mehr gelesen und sind seit Migration v21 auch nicht mehr gespeichert.
> Geblieben ist die zweite Hälfte des Gedankens: der Backup-Ladestand des
> Wechselrichters (`get_backup_reserve_soc_pct()`) hebt die Untergrenze, wenn
> er höher liegt — sonst plant der Fahrplan Entladungen, die das Gerät
> verweigert, und Plan und Ist laufen dauerhaft auseinander.

Was **nicht** gebraucht wird: eine Reserve für den Nachtverbrauch. Die
ergibt sich aus der Optimierung, weil Netzbezug teurer ist als Einspeisung.

---

## 5. Hilfetexte für alle Fahrplan-Parameter

Diese Texte gehören so ins Panel (Label · Vorgabe, darunter der Hilfetext).

> **Nicht mehr einstellbar** (Entscheidung 1.5.7–1.5.15): Vorausschau (fest
> 48 h), Auflösung (15 min), Rechentakt (1 min), ungünstigster PV-Verlauf
> (0,6) und „Fahrplan berechnen". Ihre Zeilen unten stehen als Begründung, was
> die Werte bedeuten — nicht als Beschreibung des Panels.

### Fahrplan

| Parameter | Label · Vorgabe | Hilfetext |
|---|---|---|
| `schedule_feedin_price` | Einspeisevergütung · 0,082 €/kWh | Was du für eingespeiste Energie bekommst. Der Fahrplan hält sie gegen den Bezugspreis und entscheidet danach, ob eine Kilowattstunde besser ins Netz geht oder in die Batterie. |
| `schedule_feedin_price_night` | Einspeisevergütung nachts · 0,102 €/kWh | Höherer Tarif für Nachteinspeisung. Auf 0 setzen, wenn es nur einen Tarif gibt. Entscheidend ist nicht die Höhe, sondern der Abstand zum Tagtarif — schon zwei Cent genügen, damit Energie in die Nacht verschoben wird. |
| `schedule_night_start` / `_end` | Nachttarif von / bis · 22:00 – 06:00 | Zeitfenster, in dem der Nachttarif gilt. Darf über Mitternacht gehen. |
| `schedule_consumption_price` | Bezugspreis · 0,247 €/kWh | Dein Arbeitspreis inklusive Netz und Abgaben. Solange er klar über der Einspeisevergütung liegt, ist die genaue Höhe unwichtig — erst wenn sich beide annähern, ändert sich das Verhalten grundlegend. |
| `schedule_battery_cost` | Alterungskosten · 0,01 €/kWh | Was eine durchgesetzte Kilowattstunde die Batterie an Lebensdauer kostet. Höhere Werte machen den Fahrplan zurückhaltender: er speichert nur, wenn sich der Umweg lohnt. |
| `discharge_power_kw` | Batterie-Leistungsgrenze · 5,0 kW | Wie viel Leistung die Batterie höchstens aufnehmen oder abgeben kann. Der Fahrplan plant nie darüber — ein zu kleiner Wert verschenkt Möglichkeiten, ein zu großer erzeugt Pläne, die der Wechselrichter nicht erfüllt. |
| `schedule_min_soc_pct` | Mindest-Ladestand · 10 % | Wie viel im Speicher bleiben soll. Das ist die Sicherheitsreserve und zugleich die Notstromvorsorge — ein Regler statt der drei ursprünglich geplanten. Wirkt als harte Untergrenze (fehlende Kapazität im Modell), 0 erlaubt die Entladung bis leer, mehr als 30 % ließe zu wenig Spielraum für eine Nacht. Der Backup-Ladestand des Wechselrichters hebt den Wert, wenn er höher liegt. |
| `schedule_horizon_hours` | Vorausschau · 36 h | Wie weit der Fahrplan rechnet. Kürzer heißt kurzsichtiger, länger heißt mehr Rechenzeit und mehr Einfluss der Prognoseunsicherheit. Unter 24 Stunden verliert er den nächsten Tag aus dem Blick. |
| `schedule_time_res_min` | Auflösung · 15 min | Länge eines Fahrplan-Schritts. Feiner heißt genauer, aber auch mehr Rechenaufwand — 15 Minuten passt zum Abrechnungsraster. |
| `schedule_interval_min` | Rechentakt · 1 min | Wie oft der Fahrplan neu gerechnet wird. Ein Lauf kostet rund 40 Millisekunden. Häufiger rechnen heißt: die Steuerung folgt dem tatsächlichen Ladestand statt einem veralteten Plan. |
| `schedule_worst_case_factor` | Ungünstigster PV-Verlauf · 0,6 | Anteil der Prognose, mit dem im schlechtesten Fall gerechnet wird. Greift nur, wenn die Prognosequelle kein Perzentil liefert — bei Solcast wird der echte p10-Wert verwendet und dieser Faktor bleibt ungenutzt. |
| `schedule_enabled` | Fahrplan berechnen · ein | Notbremse für die Berechnung selbst. Ausschalten hält den Optimierer an; ob gesteuert wird, entscheidet dagegen der Modus-Schalter oben im Dashboard. |

### Einspeisegrenze

| Parameter | Label · Vorgabe | Hilfetext |
|---|---|---|
| `grid_export_limit_enabled` | Einspeisegrenze beachten · aus | Einschalten, wenn der Wechselrichter die Einspeisung begrenzt. Der Fahrplan plant dann so, dass möglichst nichts abgeregelt wird, und hebt das Ladelimit an, wenn die Einspeisung trotzdem an die Grenze stößt. |
| `grid_export_limit_kw` | Höhe der Grenze · 4,0 kW | Maximale Einspeiseleistung am Netzanschlusspunkt. Muss dem Wert im Wechselrichter entsprechen — eine Grenze, die es dort nicht gibt, verschenkt Einspeisung; eine, die wir nicht kennen, kostet Ertrag durch stille Abregelung. |
| `inverter_ac_limit_kw` | AC-Grenzleistung · aus PV-Spitze | Nennleistung des Wechselrichters auf der Netzseite. Begrenzt im Fahrplan die Summe aus Einspeisung und Hausverbrauch. Ohne Angabe wird die PV-Spitzenleistung als Näherung verwendet. |
| `pv_peak_kwp` | PV-Spitzenleistung · optional | Anlagenleistung in kWp. Dient der Plausibilitätsprüfung der Prognosewerte und als Rückfall für die AC-Grenzleistung. |

### Bestehende, mit angepasstem Text

| Parameter | Label | Hilfetext |
|---|---|---|
| `lookback_weeks` | Rückblick Verbrauchsprofil · 2 Wochen | Wie weit zurück der Verbrauch gemittelt wird. Kürzer reagiert schneller auf geänderte Gewohnheiten, länger ist ruhiger gegen einzelne Ausreißer. |
| `update_interval_fast_min` | Takt der Messwerte · 1 min | *Entfallen mit v26 (1.5.39): festverdrahtet auf 1 min.* |
| `update_interval_slow_min` | Takt des Verbrauchsprofils · 15 min | *Entfallen mit v26 (1.5.39): festverdrahtet auf 15 min.* |
| `enable_peakshare` | Gemeinschaftsdaten abrufen · ein | Holt die Bedarfsprognose der Energiegemeinschaft und zeigt sie im Dashboard. Steuert derzeit nichts — sie wird zur Grundlage der Preisfunktion, sobald diese gebaut ist. |
| `peakshare_community` | Gemeinschaft | Welche Gemeinschaft angezeigt wird. |

**Plausibilitätsprüfung:** `sensor.inverter_active_power_control` verrät, ob im
Gerät eine Einspeisegrenze aktiv ist. Weicht das von der Konfiguration ab, warnt
das Panel — sonst sucht man lange, warum abgeregelt wird oder Guard 1 grundlos
anhebt.

---

## 6. Assistent und Einstellungen

### Wizard-Schritte

| Heute | Künftig |
|---|---|
| Willkommen · Wechselrichter · Prognose · Batterie | unverändert; bei den fünf Nicht-Huawei-Kacheln Zusatz „nur Anzeige — Steuerung derzeit nur Huawei" |
| **Ladung & Einspeisung** (Morgen-Einspeisung, Nacht-Entladung, Slots A/B, Min-SOC, Sicherheitspuffer, Entladeleistung, PeakShare) | **Fahrplan** — nur was individuell ist: Einspeisevergütung Tag, Einspeisevergütung nachts + Zeitfenster, Bezugspreis, Batterie-Leistungsgrenze, Notstrom-Reserve, Hinweis ob gesteuert wird. PeakShare-Auswahl bleibt (Anzeige) |
| **Einspeisebegrenzung** | **Einspeisegrenze** — die beiden Export-Schlüssel, AC-Grenzleistung, PV-Spitzenleistung |
| Erweiterte Einstellungen | gleich, ohne Simulation |
| Zusammenfassung mit Morgen-/Nacht-Abschnitten | Zusammenfassung mit Fahrplan-Abschnitt (Tarife, Leistungsgrenze, Reserve, Einspeisegrenze, ob gesteuert wird) |

Die technischen Fahrplan-Werte (Vorausschau, Auflösung, Rechentakt,
Alterungskosten, ungünstigster PV-Verlauf, Blackout-Dauer, Berechnung an/aus)
kommen **nicht** in den Wizard — die Vorgaben sind so gewählt, dass niemand sie
anfassen muss. Sie stehen im Einstellungs-Tab.

### Einstellungs-Tabs

| Tab | Status |
|---|---|
| Morgen-Einspeisung · Nacht-Entladung | entfallen |
| Einspeisebegrenzung | wird **Einspeisegrenze** |
| Fahrplan (bisher eine Karte unter „Erweitert") | wird **eigener Tab**, mit allen Parametern aus Abschnitt 5 |
| EEG-Statistik · Erweitert | bleiben |

---

## 7. Sensoren

| Sensor | Status | Anmerkung |
|---|---|---|
| 17 bestehende (Verbrauchsprofil, 7 Tagesprognosen, Prognose bis Sonnenaufgang, Batterie fehlende Energie, PV-Prognose heute/morgen, Hausverbrauch, PV-/Netz-/Batterieleistung, Register-Writes, Combined-Sensoren) | bleiben | unverändert |
| Fahrplan Batterieleistung · Fahrplan Netzleistung | bleiben | unverändert |
| Entscheidung | wird Fahrplan-Statussensor | gleiche `unique_id` → Entität und Verlaufshistorie bleiben; Automationen, die alte Attribute lesen, brechen |
| Morgen-Einspeisung Energie heute | entfällt endgültig | Entität und Langzeitstatistik bleiben unangetastet stehen; alte Werte erscheinen weiter im Diagramm der Statistikkarte |
| ~~Nacht-Entladung Energie heute~~ | **zurück mit 1.5.37** | derselbe `unique_id`, neuer Anzeigename „Entladung ins Netz heute", Quelle sind die Entlade-Sessions des Executors — Historie läuft weiter, die Zählweise steht in den Attributen (offener Punkt 2) |


---

## 8. Reihenfolge

Nach jedem Schritt muss `pytest tests/` grün sein. Schritte 1–3 ändern das
Anlagenverhalten nicht.

| # | Schritt | Commit |
|---|---|---|
| 1 | Messwerte für den ersten Stützpunkt, gemeinsamer `slot_for()`, `compute_house_load_kw` | `feat(schedule): erster Stützpunkt nutzt gemessene PV und Hauslast` |
| 2 | Interface-Erweiterung + Huawei-Leseweg | `feat(inverter): Fahrplan-Steuerschnittstelle` |
| 3 | `ScheduleExecutor` mit beiden Guards und Not-Aus, neue Konstanten — additiv, noch nicht verdrahtet | `feat(executor): Ladelimit-Guard und Entlade-Nachführung` |
| 4 | **Umschaltung und Löschung**, atomar: Guard-Lauf im 30-Sekunden-Timer, `optimizer.py` und `statistics.py` löschen, Sensoren, WebSocket, PeakShare, `const.py` bereinigen, Tests anpassen | `feat!: Fahrplan ist der einzige Aktor` |
| 5 | Dashboard: Fahrplan-Statuskarte samt Job-Laufzeiten, Wegfall Override/Simulation/Statistikkarte, Aktivitätsprotokoll | `feat(panel): Dashboard zeigt Fahrplan-Steuerung` |
| 6 | Wizard und Einstellungen gemäß Abschnitt 6, Kennzeichnung der nicht gesteuerten Treiber | `feat(panel): Wizard und Einstellungen` |
| 7 | Doku: Guide für die Einspeisegrenze neu, `python scripts/build_guides.py`, `CHAMO.md`, `CHANGELOG.md`, Version `1.6.0-chamo` | `docs: Fahrplan-Steuerung dokumentiert` |

Schritt 4 ist bewusst ein großer Commit über zwölf Dateien — anders bleibt die
Testsuite nicht grün.

---

## 9. Tests

**Entfallen komplett:** `test_optimizer.py` (108), `test_dual_window.py` (94),
`test_dual_window_integration.py` (24), `test_feedin_limit.py` (22),
`test_execute_discharge_target.py` (5), `test_statistics.py` (15),
`test_decision_sensor.py` (11). Aus `test_optimizer.py` werden die
Sign-Convention-Fälle nach `test_power_readings.py` portiert.

**Anpassen:** `conftest.py` (`_make_optimizer` → `_make_executor`,
`_make_schedule_result`-Fabrik; `mock_inverter` bekommt die vier neuen
Interface-Mitglieder), `test_telemetry_hooks.py` (Outcome-/Trapez-/
Block-Prediction-Tests raus, Failure-Callback-Tests umschreiben),
`test_schedule.py` (neue Tests für Messwert-Übernahme und die neuen
Export-Schlüssel), `test_inverter_base.py`, `test_huawei_inverter.py`,
`test_fahrplan_sensoren.py` (Slot-Lookup auf `schedule.slot_for`).

**Neu:** `test_schedule_executor.py` (~26 Tests: Slot-Zuordnung, Totbänder, alle
Guard-1-Fälle inklusive Rücknahme-Hysterese, Guard-2-Formel und Ziel-SOC,
Modus-Wechsel, Grace Period, Failsafe, Release, nicht unterstützter Treiber,
Not-Aus), `test_fahrplan_status_sensor.py` (~4 Tests).

**Bilanz:** 733 → etwa 450 Tests.

Testumgebung: `pip install -r requirements_test.txt` in ein venv (enthält
`pandas`, `highspy`, und `optlang` nur für den GLPK-Referenzvergleich — auf
musl-Systemen nicht installierbar, der Test überspringt sich dort selbst).

---

## 10. Risiken

1. **Kein Netzbezugs-Wächter mehr.** Guard 2 rechnet die Hauslast alle 30
   Sekunden nach, aber springt die Last zwischen zwei Läufen, wird zu wenig
   entladen. Der alte Watchdog mit Abbruch nach fünf Minuten ist weg. Deshalb die
   Empfehlung, den Not-Aus in Schritt 3 mitzubauen.
2. **Stehenbleibendes Ladelimit.** Wir schreiben Limits statt Zustände. Ohne
   Rückgabe bliebe nach einem Absturz ein 0-kW-Limit stehen und die Batterie lädt
   nie wieder. Abgedeckt über Release beim Entladen, bei Ein → Test und über den
   Failsafe; bei hartem Absturz bleibt es bis zum ersten Guard-Lauf nach dem
   Neustart (Grace Period).
3. **Verlaufsdaten und Automationen.** Zwei Sensoren verschwinden, ihre
   Langzeitstatistiken verwaisen. Karten oder Automationen, die Attribute des
   Entscheidungs-Sensors lesen, brechen. Store-Dateien bleiben absichtlich liegen.
4. **Rückwechsel** auf die produktive Integration funktioniert (Version 20, keine
   gelöschten Schlüssel). Vorher prüfen: eine offene Einspeise-Session in
   `.storage/eeg_energy_optimizer_*_feedin_stats` würde Monate später mit absurder
   Dauer geschlossen.
5. ~~**Telemetrie** verstummt teilweise~~ — **erledigt mit 1.5.36.** Snapshots
   sind zurück, mit den Zuständen des Executors statt der Heuristik (`normal`,
   `charge_limit`, `discharge`, `release`, `failsafe`, `emergency`,
   `unsupported`); dazu ein täglicher Profil-Herzschlag und vier
   Fahrplan-Störungskategorien (`schedule_solver`, `schedule_stale`,
   `guard_emergency`, `inverter_unsupported`).

   Die Outcomes sind ebenfalls zurück, aber als **Tagesbilanz** statt als
   Blockbilanz (`tagesbilanz.py`, `event_type` `fahrplan_tag` und
   `fahrplan_tag_48h`): einmal täglich Prognose gegen Messung, mit zwei
   Vorläufen. Gespeist aus dem, was ohnehin auf der Anlage liegt — Ist aus den
   Kurzzeitstatistiken des Recorders (`async_ist_verlauf`), Prognose aus dem
   Plan-Archiv (`async_lies_vor`). Damit lebt `/v1/stats/forecast-quality`
   wieder, und zwar mit besseren Zahlen als vorher: die alte Rechnung skalierte
   den Tagesforecast linear (`/24`) auf das Blockfenster, hier stehen echte
   Viertelstundenreihen.

   State-Changes bleiben weg — ein Übergang zwischen zwei Zuständen existiert
   nicht mehr. Die Nacht-Statistik (offener Punkt 2) ist damit **nicht**
   erledigt: sie will die Einspeisung während einer gesteuerten Entladung, die
   Tagesbilanz nur die Tagessumme.

   Welche Variante eine Anlage fährt, sagt `settings.steuerung` im Profil:
   `"fahrplan"` hier, **kein Schlüssel** in der produktiven Integration. Damit
   werten Backend und Dashboard beide Flotten parallel aus, ohne dass die
   bestehenden Anlagen ein Update brauchen.
6. **Plan am Anschlag.** Plant der Fahrplan die volle Leistung, schreibt der
   Executor exakt die Batterie-Leistungsgrenze — bei falsch gesetztem Wert
   drosselt das Hardware, die mehr könnte. Hilfetext im Wizard ist deshalb wichtig.
7. **Guard-1-Messpfad** braucht einen korrekt vorzeichen-aufgelösten Netz-Sensor
   (EMMA-Sonderfall in `resolve_sign`) — sonst hebt der Guard bei *Bezug* an.
8. **Frisches Setup ohne Verbrauchsprofil:** `async_collect_inputs` meldet
   „Verbrauchsprofil noch nicht geladen", der Executor bleibt im Normalbetrieb.
   Kein Steuerverlust, aber auch keine Optimierung, bis der Backfill steht.

---

## 11. Offene Punkte

1. ~~**Not-Aus für Guard 2**~~ — **erledigt**, wie empfohlen in Schritt 3
   mitgebaut: Netzbezug über `GUARD_EMERGENCY_IMPORT_KW` (1 kW) in
   `GUARD_EMERGENCY_IMPORT_RUNS` (3) aufeinanderfolgenden Guard-Läufen sperrt
   die Entladung bis zum Slotwechsel.
2. ~~**Nacht-Einspeisungs-Statistik**~~ — **erledigt mit 1.5.37**, und zwar
   beides zugleich: Die Frage war ein Scheingegensatz. Die `unique_id` betrifft
   nur Entität und Recorder-Historie, der Speicher nur die Panel-Karte, die
   Quelle nur den Zählzeitpunkt — man kann die Kennung behalten **und** aus den
   Entlade-Sessions speisen.

   Umgesetzt: Kennung unverändert (`..._feedin_evening_heute`), Speicherformat
   unverändert (`{DOMAIN}_{entry_id}_feedin_stats`, Schlüssel `morning` und
   `evening`), Quelle ist `active_kind == "discharge"` bei Modus Ein. Der
   Bedeutungswechsel wird ausgewiesen statt versteckt: Sensor-Attribute
   `zaehlweise` und `umgestellt_am`, Hinweis in der Panel-Karte, neuer
   Anzeigename „Entladung ins Netz heute". Der Morgen-Zähler kommt nicht
   zurück; seine Entität und ihre Statistik bleiben unangetastet stehen.
3. ~~**Wirkungsgradkorrektur bei Guard 2**~~ — **erledigt, gemessen**
   (Nacht 24./25.08.2026, 63 eingeschwungene Abschnitte): Es fehlen konstant
   59 W, unabhängig von der befohlenen Leistung — ein fester Abzug, kein
   Wirkungsgrad. `GUARD_DISCHARGE_EFFICIENCY` bleibt deshalb auf 1,0; eine
   Division würde den Fehler bei hoher Leistung vervielfachen, und 59 W
   liegen ohnehin unter dem Totband. Herleitung in CHAMO.md, Abschnitt
   „Gemessen: Entlade-Nachführung".

   Der frühere Einzelmesspunkt (Di 03:30, 0,92 kW geplant gegen 0,72 kW Ist)
   verglich Plan-Batterieleistung mit Ist — diese Differenz enthält die
   absichtliche Korrektur des Guards (er rechnet mit der *gemessenen* statt
   der prognostizierten Hauslast) und sagt über den Wirkungsgrad nichts.
   Verglichen gehört der geschriebene Sollwert (Attribut
   `entladeleistung_kw` am Fahrplan-Status) gegen die gemessene
   Batterieleistung.

---

## Anhang: Steuerflächen des Huawei (Testanlage)

| Stellgröße | Bereich | Verwendung |
|---|---|---|
| `number.batteries_maximale_ladeleistung` | 0–5000 W | Guard 1 — stufenlos, ist eine **Obergrenze**, kein Sollwert |
| `number.batteries_maximale_entladeleistung` | 0–5000 W | Obergrenze Entladung |
| `number.batteries_ladeende_ladestand` | 90–100 % | ungenutzt |
| `number.batteries_entlade_ende_ladestand` | 0–20 % | ungenutzt |
| `number.batteries_backup_power_ladestand` | 0–100 % | **einlesen** als Untergrenze (Notstrom) |
| `number.inverter_power_derating` | 0–8800 W | starre AC-Drosselung ohne Zählerbezug — **nicht** für Einspeisegrenzen verwenden. Das Maximum verrät die Nennleistung des Geräts (hier 8,8 kW) |
| `select.batteries_betriebsmodus` | adaptive, fixed_charge_discharge, maximise_self_consumption, fully_fed_to_grid, time_of_use_luna2000 | bleibt auf `maximise_self_consumption` |
| `switch.batteries_laden_aus_dem_netz` | on/off | ungenutzt (`no_grid_charging` im Modell) |
| Service `forcible_discharge_soc` | device_id, power (W, als Text), target_soc | Guard 2 — `device_id` ist das **Batterie**-Gerät |
| Service `set_maximum_feed_grid_power` | device_id, power (W) | echte Exportbegrenzung am Netzanschlusspunkt, geregelt über den Smart Meter; `device_id` ist der **Wechselrichter**. Modi: `UNLIMITED`, `ZERO_POWER_GRID_CONNECTION`, `POWER_LIMITED_GRID_CONNECTION` (W oder %), `DI_ACTIVE_SCHEDULING`; sichtbar in `sensor.inverter_active_power_control` |

Die Exportbegrenzung ist ein geschlossener Regelkreis am Übergabepunkt: Der
Wechselrichter liest den Smart Power Sensor und fährt seine Wirkleistung nach.
Der Hausverbrauch wird dabei nicht berechnet, er ist in der Messung enthalten.
Reihenfolge der Senken bei aktiver Grenze: Eigenverbrauch → Batterie laden →
einspeisen bis zur Grenze → abregeln. **Offen und nur am Gerät messbar:** ob ein
von uns gesetztes Ladelimit in Schritt zwei gewinnt oder ob die Firmware es
überschreibt, um Abregelung zu vermeiden.
