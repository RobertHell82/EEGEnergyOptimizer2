# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**EEG Energy Optimizer** — a Home Assistant custom integration for grid-friendly battery management, optimized for energy communities (Energiegemeinschaften / EEG) in the DACH region. It computes a 48-hour charge/discharge schedule with a linear program (Harald Geyer's `opt()`, vendored under `chamo/`) and steers the battery so that feed-in lands in the hours the community actually needs it.

This repository is the **chamo prototype** — a clone of the main integration with the same domain, so only one of the two can be installed per HA instance. Adjustments go through the parameters `schedule.py` hands to `opt()`; the two exceptions that touch Harald's code are marked `LOCAL CHANGE` and listed in `chamo/README.md`.

**Language**: Python (async, Home Assistant framework) + plain JS (panel)
**Distribution**: HACS-compatible repository structure

## Architecture

All code lives in `custom_components/eeg_energy_optimizer/`. The integration runs as a Home Assistant config-flow hub with a sidebar onboarding panel.

### Two Loops: Planning (1 min) and Execution (30 s)

There is no state heuristic any more. The LP schedule is the **only** actor.
Computing and enforcing run on separate clocks because they have different
costs: solving needs a worker thread, enforcing needs live measurements.

```
__init__.py: async_setup_entry()
  → Inverter created via factory (inverter/__init__.py)
  → Platforms forwarded: sensor, select
  → WebSocket API registered for panel
  → Frontend panel registered
  → Activity log: persistent ring buffer (5000 entries, paginated API)
  → 30s timer: _guard_cycle()          — ScheduleExecutor
  → 1min timer: ScheduleRunner.async_step()
  → 30min timer: PeakShare + OeMAG + grid-tariff refresh (+ spot / OeMAG estimate / aWATTar SUNNY, when chosen as base tariff; + own PV forecast from Open-Meteo when `forecast_source = "eigen"`)

schedule.py: ScheduleRunner (planning, 1 min)
  → async_collect_inputs()  [event loop] — profile, battery, PV forecast,
       PeakShare demand, prices → ScheduleInputs (pandas-free dataclass)
  → HAConfig — the bridge to Harald's opt(); every intervention of ours
       lives in these parameters, never in the model:
        - min-SOC as *missing capacity* (opt() counts free room to full,
          a smaller capacity cuts the bottom off) → hard floor
        - export limit only when enabled, else ac_limit − 0.5
        - feedin_price from eeg_price.py, capped below the purchase price
  → _solve()  [worker thread] — imports pandas, calls opt(), 15-min slots
       over 48 h; result = slots with pv / cons / battery / grid / prices
  → push() is deliberately empty — the runner never writes to the inverter

schedule_executor.py: ScheduleExecutor (execution, 30 s)
  → plan_action(result, now) — pure function: current slot → intent
       (charge limit / discharge / release), driver-neutral
  → async_guard_cycle(schedule_state, mode)
        Guard 1 — raise the charge limit when measured export sticks to the
                  limit (silent curtailment), step GUARD_CHARGE_STEP_KW
        Guard 2 — track discharge: planned export + measured house load,
                  divided by GUARD_DISCHARGE_EFFICIENCY
        Not-Aus  — grid import > 1 kW in 3 consecutive runs blocks discharge
                  until the slot changes (buying power to sell it cheaper)
        Guard 3 — does the commanded discharge actually happen? Measured
                  battery output (grid meter for `discharge_is_grid_setpoint`
                  drivers) below half the setpoint AND more than 0.3 kW short,
                  over 6 runs → stop the forced mode and set it up again;
                  at most twice per slot, never at the target SOC
        Failsafe — no fresh plan for 15 min → release the inverter
        Deadbands — 0.2 kW / 1 % SOC, so we don't write on LP noise
        Write brake — a failed write is not retried every run; the gap
                  doubles (1, 2, 4 … 10 runs) for the intent that failed
  → writes only via InverterBase, only in mode "Ein", only for drivers with
    supports_schedule_control=True (Fronius, Huawei, Kostal, Sigenergy, SMA, SolaX).
    Guard 2 asks `discharge_is_grid_setpoint`: SMA takes a GRID setpoint
    (inverter adds house load itself) → executor hands over the planned
    export; all others take battery power → plan export + house load − PV
  → _heizstab_schritt() after every run — the heater (heizstab/) is a second
    sink with two modes: (1) the running slot plans heat → regulate on
    "export ≈ 0", capped at the planned kW (the forecast says how much is
    allowed, the measurement how much is there; up only by the MEASURED
    surplus, ≤ 0.5 kW/run, settles at 0.3 kW export); (2) no planned heat → the
    surplus rule at the export limit: export sticks to the limit → +0.5 kW
    per run, below the limit → down by half the measured gap per run (one run of settling after a raise; the full gap at once on grid import). Discharge / mode
    Aus / startup → 0. Unplanned surplus is shared with the battery by SOC
    (`_heizstab_deckel_kw`: battery gets all below 20 % SOC, half from 50 %)
```

### Key Files

| File | Role |
|------|------|
| `__init__.py` | Entry setup, 30s guard timer + 1min schedule timer, activity log, panel registration, telemetry watchdogs, config migration |
| `schedule.py` | Planning — `ScheduleInputs`, `HAConfig` (the bridge to `opt()`), `ScheduleRunner` (collect in loop, solve in worker thread); battery SOC + capacity are buffered for `BATTERIE_PUFFER_MAX_S` (5 min) so a dropped Modbus connection costs no run — only a complete pair is stored, after the deadline the run aborts again as before; profit comparison (`simuliere_standardbetrieb` greedy self-consumption reference with the same physics as the LP: AC efficiency, Harald's two-step internal-resistance losses above 0.1 C / 0.2 C, the configured max-SOC cap, and the heater's buffer budget per calendar day like the LP — without it the reference heated 18.5 kWh into a buffer with room for 8.3 (Grünbach 28.09.2026); the daily balance caps it at the day's measured heat + remaining room; evaluated over at least `GEWINN_HORIZONT_H` = 24 h and then up to the next local midnight, so each day's heat budget lies wholly inside the window on both sides; `bewerte_geldfluesse` with real tariffs — community rates only for energy the community's quarter-hour saldo actually absorbs, rest at base tariff/spot series; end-of-horizon battery credit at `endbestand_satz` = base tariff × efficiency − aging) |
| `schedule_executor.py` | Execution — `plan_action()` + `ScheduleExecutor.async_guard_cycle()`; the **only** place that writes inverter commands |
| `eeg_price.py` | Synthetic feed-in tariff from community demand — turns PeakShare demand into a price surcharge. Quote mode (`eeg_demand_source = "quote"`, for communities without PeakShare): fixed day/night acceptance quota per community (`peakshare_quote_pct[_2]`, `peakshare_quote_night_pct[_2]`) → blended price `Anteil · Quote · (Wert − Basistarif)` instead of the demand-normalised signal; the same quota replaces the saldo in `bewerte_geldfluesse` (declared assumption, the one exception to "no invented revenue"); PeakShare is not fetched in quote mode |
| `netzentgelt.py` | Grid usage tariff (Netzebene 7) per grid area, read from the regulation instead of typed in: RIS OGD API (`data.bka.gv.at`) → paragraph HTML → table `LP \| AP \| SNAP \| WiNAP`. The user picks one of the 14 grid areas (ElWG Anlage I); the provider returns AP plus the time-variable rates, net cents → gross €/kWh. Two regulations, one parser: SNE-V 2018 § 5 until 31.12.2026, SNE-T-V (to the SNE-G-V) from 2027 — columns found by name, one row per area or the 2018 sub-rows (`nicht gemessene Leistung` = household). Built-in `SNAPSHOT` (SNE-V 2018 idF BGBl. II 305/2025) covers first start and every fetch failure; `schedule_netzbereich = "manual"` keeps a hand-typed fee. **TODO 2027: put the published SNE-T-V's `Gesetzesnummer`/paragraph into `QUELLEN` and refresh `SNAPSHOT`.** |
| `spot.py` | Optional base tariff: EPEX day-ahead spot price via the aWATTar API (free, no key, €/MWh); negative prices stay negative, a cent and/or percent fee on top (`spot_feedin_fee`, `spot_feedin_fee_pct`) |
| `oemag.py` | Optional base tariff: OeMAG monthly market price, scraped from the HTML table (no API), cached across restarts; also reads the per-month calculation basis + balancing-energy cost for the estimator |
| `oemag_schaetzung.py` | Estimate of the OeMAG tariff for the *current* month (source `oemag_estimate`): aWATTar day-ahead prices weighted by Austrian solar generation (Energy-Charts), clamped to 60–100 % of the E-Control quarterly price (scraped; fallback derived from clamped months of the OeMAG table), minus balancing cost. Validated 2025-01…2026-08: MAE 0.21 ct |
| `awattar_sunny.py` | Optional base tariff: aWATTar SUNNY fixed monthly feed-in price (source `awattar_sunny`). No API — reads the yearly tab of aWATTar's published price sheet (Google Sheet, gviz CSV) and falls back to the tariff page; two contract variants (`awattar_sunny_vertrag` = `neu`/`alt`, contracts after/until 25.02.2026) because the sheet carries two SUNNY columns; cached across restarts, hourly retry while the current month is missing |
| `energie_ag.py` | Optional base tariff: Energie AG „Team Sonne Float“ (sources `energie_ag` / `energie_ag_estimate`). Price = **reference market value PV § 13 EAG − 1.5 ct discount** (`energie_ag_abschlag`, VPI-indexed so it is configurable). Two price variants (`energie_ag_variante` = `float`/`loyal_float`); Loyal Float guarantees a minimum of 2 ct but requires an electricity contract with Energie AG Vertrieb. The reference market value is scraped from e-control.at/referenzmarktwert (the very page the price sheet cites), cached across restarts, hourly retry while the due month is missing. Verified against all 12 months of the price sheet's chart (0.00 ct deviation). `energie_ag_estimate` reuses `oemag_schaetzung`'s **raw** value — that raw monthly mean IS this reference market value, before corridor and balancing cost — so there is no second fetch |
| `prognosevergleich.py` | **Prognosevergleich** (`pv_prognose_vergleich`): the non-steering source runs alongside — Solcast/Forecast.Solar next to the own forecast or vice versa. Once per day after 05:00 local (first 30-min tick) it freezes both 30-min series for the local calendar day (foreign: Solcast `detailedForecast`, else Forecast.Solar wh_hours; own: `PvPrognoseProvider.halbstunden()`), then keeps pulling the measured PV as 30-min means from the recorder 5-min statistics (`schedule_archive.async_ist_verlauf`) for today and yesterday until the day is complete, plus grid export (`netz`) and SOC (`soc`) for the calibration's curtailment filter. Kept for `TAGE_AUFBEWAHRUNG` = 400 days (the calibration learns from them and should see every sun position once) in **two** `Store`s: finished days in `…_prognosevergleich_archiv`, written only when a day moves in (once a day — a year is megabytes, the tick runs every 30 min), the rest in `…_prognosevergleich`. Every statistic (overview, summary, empirical p10) uses only the last `TAGE_AUSWERTUNG` = 30 days — a year would mix summer and winter. Also records `eigen_roh` (own forecast *without* calibration) + `eigen_kennung`; recording runs with the switch on **or** while the own forecast steers (`aufzeichnung_noetig`), since otherwise it would never learn. Slot keys are UTC ISO strings (local midnight → midnight, 46/50 slots on DST days). Per day and source: forecast kWh, deviation, MAE over daylight slots; over the complete days: bias, MAE, "who was closer" count, and the **empirical p10** = 10 % quantile of measured/forecast daily ratios, only from `P10_MIN_TAGE` = 14 days on, clamped to [0.2, 1]. `schedule._eigene_prognose` uses, in this order and only with the switch on: the p10/p50 ratio borrowed from Solcast, the empirical factor, else none (60 % worst-case factor). A day whose first capture happens after `FESTHALTEN_SPAET_STUNDE` (08:00, HA was down in the morning) is flagged `spaet`: shown with † in the table, excluded from the summary and the empirical p10, since a forecast made at noon has seen half the day. Capture times are kept per source (`festgehalten_fremd` / `_eigen`). Reads and shows, never steers — but feeds the calibration after every tick with new data (`provider.kalibrieren(lerntage())`) |
| `power_readings.py` | Shared sensor reads — house load (minus heater), PV now, grid export, heater power, battery capacity resolution |
| `heizstab/controller.py` | Heater control — `HeizstabController` (`regeln()`: comfort → discharge → max temperature → plan (`plan_kw`) → surplus rule `naechster_sollwert`; temperature hysteresis, saturation, `puffer_budget_kwh` for the LP, 20-s watchdog write (plus an immediate rewrite when the device reads 0 W under a standing setpoint), 10-s read, 6-h time sync, detection of both foreign control and a device that does not follow), `create_heizstab()` factory |
| `heizstab/ohmpilot_modbus.py` | Fronius Ohmpilot driver via direct Modbus TCP (setpoint 40599 int32 W big-endian, actual power 40800, temperature 40808 in 0.1 °C, unix time 40400; 50-s device watchdog). Taken over from HA_Optimierung_Gruenbach, registers verified on the device there |
| `leistungsspitze.py` | Measures the basis of the 2027 capacity charge (SNE-G-V § 6): grid **import** energy per fixed quarter-hour (UTC-floored, :00/:15/:30/:45) ÷ 0.25 h, commercially rounded, and the month's maximum (local calendar month, 24 months of history, `Store`). Integrates `max(0, −grid)` — export never offsets import. Sample points = every state change of the grid sensor plus a 10-s sampling; a value counts only while the source's `last_reported` is younger than `HALTEN_MAX_S` (a hung Modbus link keeps its last state without going `unavailable`). A quarter with gaps is a lower bound, still counted, flagged `vollstaendig=False`. Measures only, steers nothing. `netzkosten_monat(kw)` turns a peak into € per month incl. 20 % VAT from E-Control's **guide values** (14.07.2026, not tariffs): final stage 33.82 €/kW·a up to 10 kW, 67.64 above (the highest known rates), start 2027 ≈ 19 €/kW·a (15–26 by grid area), billed at least 2 kW; exposed as attribute `netzkosten_monat` of *Bezugsspitze Monat* and shown in the status card's ⓘ. **TODO 2027: replace with the SNE-T-V rates per grid area.** Created in `async_setup_entry` **before** the platforms so the sensors can subscribe |
| `statistics.py` | Counter behind the *Entladung ins Netz* sensor — battery energy that reached the grid during a controlled discharge (store `…_feedin_stats`, format kept since 1.5.x). The panel card that showed it is gone (2.1.24) |
| `bilanz_telemetrie.py` | Balance days of the Einspeisung card to the telemetry (`/v1/balance`, backend table `daily_balance`): `tag_payload` whitelists the fields — energy, mode share, two flags, `ref_*`, the day's import peak (`bezug_spitze_kw` / `_zeit` from `bewerte_tag`: highest quarter-hour import ÷ 0.25 h, same grid as the grid operator; not recoverable for days archived before 2.1.30), **never money**. `BilanzVersand` remembers the dates the backend has (bound to the installation id, so a forget resends the archive), sends the rest hourly and after boot in packets of 100 — the post-update backfill (≤ 400 days) and the daily new day are the same mechanism. The backend upserts per (installation, date) |
| `tagesbilanz.py` | Daily outcome for the telemetry (`/v1/outcome`): forecast vs. measurement of the finished day, with the plans of the evening before and two days before (built nightly 00:15 or via `tagesbilanz_jetzt`) |
| `schedule_archive.py` | Rolling archive of computed plans (7 days, gzip, ~8 KB each) for after-the-fact debugging. Each entry: plan (`to_dict()`), filtered settings (allowlist incl. `heizstab_*`, blocklist host/port/token/…) and, since 2.1.24, `eingaben` — the `ScheduleInputs` fields the plan does not carry (p10 path `min_production_kw`, base tariff `feedin_price_series`, `eeg_tarife`/`eeg_bedarf`, heater budget/temperature), so a plan can be replayed exactly |
| `schedule_archive_view.py` | HTTP view that packs archive + settings + measured history into a downloadable ZIP |
| `chamo/` | Harald Geyer's LP optimizer (`opt_highs.py`, `timetableopt`) plus a HiGHS adapter. `opt_highs.py` carries three local additions, all marked `LOCAL CHANGE` and documented in `chamo/README.md`: the heater as a valued sink (`heater_p`, 2.1.1-dev2), the blackout reserve capped at what is reachable **without buying** and a solver-status check after `optimize()` — everything else is upstream |
| `sensor.py` | 26 sensors (+ up to 21 conditional): consumption profile, forecasts, power flows, plan values, grid discharge energy, register writes, Fahrplan-Status, money balance |
| `bilanz.py` | Energy balance in money — records 96 quarter-hours per day (energy, SOC, **frozen** prices and community balances), evaluates them with `bewerte_geldfluesse`, and derives the optimiser advantage against a simulated standard operation over the measured series. The balance day runs 04:00–04:00 (night discharge stays in one day; old midnight-based records are migrated on load). Days where the battery behaved like the reference (power deviation ≤ max(1 kWh, 10 % of throughput)) report advantage 0 with `kein_eingriff`; the raw difference stays in `vorteil_roh`. Also backs the **Einspeisung** card (`einspeisung()`): per day `haus_kwh`, `batterie_export_kwh` (export in slots with PV below `BATTERIE_EXPORT_PV_SCHWELLE_KW`, `ohne_pv()` — *not* only controlled discharge, unlike `statistics.py`) and the same amounts of the reference run as `ref_*` (None without a reference). Month/year come from the 400-day archive, not `_monate`; `gesamt` adds the `_monate` sums for months *before* the oldest archived day (never overlapping, their `ref_*` dropped) and returns a gap-free series per year (months are what `jahr` shows); ratios are formed from sums at query time, and the comparison uses only days with `ref_*` on both sides. The per-slot community share comes from `schedule.eeg_aufnahme_je_slot`, which shares `_eeg_zuteilung` with `bewerte_geldfluesse` — one settlement rule |
| `override.py` | Time-boxed user override — **Pause** (behave like mode Aus) with two end conditions: expiry time (`stunden`, 0.25–48 h) and/or target SOC (`bis_soc_pct`, 50–100 %; ends when the measured SOC reaches it, 48 h cap as safety net). Persisted via `Store` so a restart mid-pause does not resume control. Evaluated in the guard cycle in `__init__.py` (`async_tick(now, soc_pct)`); exposed as HA services `pause` / `aufheben` (`services.yaml`) |
| `coordinator.py` | Loads hourly consumption averages from recorder (rolling, weekday split) |
| `forecast_provider.py` | Abstract PV forecast provider — Solcast, Forecast.Solar (entity reads) and `EigenProvider` (wraps `pvprognose/`) |
| `pvprognose/` | **Own PV forecast** (`forecast_source = "eigen"`), fully self-contained: `openmeteo.py` fetches `global_tilted_irradiance` + `temperature_2m` per surface from Open-Meteo as the **mean of three models** (`MODELLE`: `icon_seamless`, `ecmwf_ifs025`, `meteofrance_seamless`; per timestamp over the models that have a value — ICON alone ran 40 % low in Traun on clear mornings, basin haze/fog that never came) (15-min, 7 days, no key; azimuth converted from compass to Open-Meteo's 0 = south; values are means of the *preceding* interval and get shifted to slot starts), `modell.py` is the pure PVWatts-style model (γ = −0.4 %/K, cell = air + 0.03 K·m²/W, losses `pv_verluste_pct` default 14 %, DC sum over all `pv_flaechen`, an optional clip per surface at its `max_kw`, then one plant-wide AC clip at `inverter_ac_limit_kw`), `provider.py` holds the weather per surface and two 7-day 15-min AC series derived from it (raw and calibrated, see below) in a `Store` (fetched in the 30-min cycle, `FRISCH_S` 25 min; failures keep the old series, older than 48 h counts as no forecast so the schedule fails loudly), serves `halbstunden()` for the schedule (Solcast raster, **no p10 → `min_production=None` → 60 % worst-case factor like Forecast.Solar**), `rest_heute_kwh()`/`morgen_kwh()` for the sensors and `tage_kwh()` for the week chart; `berechne_einmalig()` backs the panel's "Prognose berechnen" with unsaved surfaces (uncalibrated). **Calibration** (`kalibrierung.py`): one factor per sun-position cell (10° azimuth × 5° elevation, NOAA sun position), energy-weighted Σmeasured/Σforecast, shrunk towards 1 with `SCHRUMPF_KWH` = 3 kWh, clamped 0.6–1.5, bilinear between cells *with* data, 1 where the sun was never seen. Learns from the Prognosevergleich days whose `eigen_kennung` matches (`modellkennung()` = surfaces + losses + AC limit + weather models — any change starts over); skips curtailment (export ≥ limit − 0.5 kW; below a 1-kW limit only with SOC ≥ 95 %; PV or forecast within 0.5 kW of the AC limit), weather days (day ratio outside 0.6–1.6), slots < 0.3 kW and single slots with ratio outside ⅓–3. `leistungsreihe(…, faktor)` applies it per surface **before** both clips. The provider keeps the weather per surface (`_paare`, persisted as `wetter`) and two series: calibrated for everyone, raw (`roh=True`) only for the Prognosevergleich — learning from the calibrated one would learn its own correction. No historical-forecast backfill, by decision (28.09.2026): it learns from the recorded days only |
| `config_flow.py` | Single-click config flow (full setup happens in panel) |
| `peakshare.py` | PeakShareProvider — fetches + caches community demand forecasts (half-hourly refresh; hourly values, `opt()` resamples to 15 min itself) |
| `telemetry.py`, `telemetry_buffer.py` | Opt-in reporting — profile, failures, half-hourly snapshots (`/v1/snapshot`: SOC, PV/house/grid/battery kW, mode, executor state, plan min-SOC) and a daily outcome (`/v1/outcome`, `tagesbilanz.py`), ring buffer with backoff. README and the panel's privacy details list all five (incl. the balance day); keep them in sync when a payload changes. Snapshots are taken on the half-hour grid but **offset by `TELEMETRY_SNAPSHOT_OFFSET_MIN`**: `_collect_snapshot()` runs in the same guard cycle *after* the executor wrote, and the plain grid hit exactly the cycle that writes on a slot change (slots turn at :00/:15/:30/:45) — Huawei briefly drops the battery when `forcible_discharge_soc` is rewritten, so the power columns systematically recorded the gap we cause ourselves. Weismann, 21.09.2026: the grid meter read ~0 W at the grid start in 9 of 10 half-hours while the window averaged 271–661 W. `soc_pct` is unaffected; for power questions use the plant's own history, not the snapshots. The snapshot queue is sent hourly and lives in a `Store` (`…_telemetry_snapshots`), so a restart no longer drops the pending ones. `schedule_solver` and `inverter_write` stay silent for `TELEMETRY_START_SCHONFRIST_S` (5 min) after setup — neither reported nor counted; the source integration is not ready yet, and every update used to report "Batterie-Ladestand oder -Kapazität unbekannt" (Huawei) or a failed start release (SolaX) |
| `websocket_api.py` | 36 WebSocket commands for panel (config, schedule, control state, PeakShare, OeMAG, spot price, aWATTar SUNNY, grid tariffs, daily balance, probes, telemetry, activity log) |
| `inverter/base.py` | Abstract inverter interface (InverterBase ABC) |
| `inverter/huawei.py` | Huawei SUN2000 implementation via HA services — Single + Master/Slave (multi-device) |
| `inverter/_distribution.py` | Shared proportional discharge distribution (Huawei multi-battery) |
| `inverter/fronius.py` | Fronius Gen24 implementation via direct Modbus TCP (SunSpec Model 124, device-read scale factors, RvrtTms watchdog + keepalive task) |
| `inverter/kostal.py` | Kostal Plenticore implementation via direct Modbus TCP (proprietary registers 1034/1038, watchdog keepalive task) |
| `inverter/sma.py` | SMA Smart Energy / Sunny Boy Storage implementation via direct Modbus TCP (CmpBMS 6-parameter method, complete-block writes, watchdog keepalive task; `discharge_is_grid_setpoint=True`, charge limit read from the active block) |
| `inverter/solax.py` | SolaX Gen4+ implementation via solax_modbus Mode 1 |
| `inverter/sigenergy.py` | Sigenergy SigenStor via the HA integration `sigen` (Remote EMS: switch/select/number entities) |
| `inverter/__init__.py` | Factory function `create_inverter()` |
| `select.py` | Mode select entity (Ein/Aus), restores state across restarts (a stored „Test“ from before 1.5.52 maps to Aus) |
| `const.py` | All constants, defaults, mode enums, state names |
| `ambibox/modbus.py` | Ambibox (sidOS) Modbus TCP — bulk read of the EV charger block (104 registers at 4000 + 200 × connector); writes only the manual-test setpoint (holding 3000 + 100 × connector) |
| `ambibox/controller.py` | Interprets those registers into an `AutoZustand`, polls every 15 s, pushes to sensors/panel; owns the manual charge/discharge test (keepalive, time limit, stop) |
| `frontend/eeg-optimizer-panel.js` | Dashboard + onboarding panel (plain HTMLElement, Shadow DOM) |

### Sensors (26 always + up to 21 conditional)

| # | Sensor | Update | Description |
|---|--------|--------|-------------|
| 1 | Verbrauchsprofil | slow | Hourly averages per weekday for dashboard charts |
| 2–8 | Tagesverbrauchsprognose heute..Tag 6 | fast | Daily consumption forecasts (7 sensors) |
| 9 | PV-Prognose heute | fast | Remaining PV today from forecast provider. With the own forecast it also carries `tage_kwh` (7 daily sums), `heute_gesamt_kwh`, `quelle`, `geholt`, `alter_minuten`, `fehler` — the panel's week chart reads them there, since there are no foreign day sensors |
| 10 | PV-Prognose morgen | fast | PV forecast tomorrow |
| 11 | Hausverbrauch | fast | Calculated: PV - Battery - Grid (kW, MEASUREMENT) |
| 12 | PV-Leistung | fast | Current PV production (kW, MEASUREMENT) |
| 13 | Netzleistung | fast | Current grid power — positive = export (Einspeisung), negative = import (kW, MEASUREMENT) |
| 14 | Batterieleistung | fast | Current battery power — positive = charge, negative = discharge (kW, MEASUREMENT) |
| 15 | Fahrplan Batterieleistung | fast | **Planned** battery power for the current slot — same cadence as the measured one, so recorder history makes plan and reality comparable |
| 16 | Fahrplan Netzleistung | fast | **Planned** grid power for the current slot |
| 17 | Entladung ins Netz | fast | Battery energy that actually reached the grid (kWh, TOTAL with `last_reset`) — fed by `statistics.py`; the feed-in statistics card that read it is gone (2.1.24) |
| 18 | Fahrplan-Status | 30s | Executor state ("Laden begrenzt auf 2,0 kW", "Einspeisung 2,80 kW bis 43 %" (planned export; "Entladung …" only without a plan value), "Laden blockiert", "Normalbetrieb", "Anzeige-Modus", "Nur Anzeige") + plan/written-value attributes |
| 19–21 | Ersparnis durch PV — heute / Monat / Jahr | fast | Avoided grid purchase + feed-in revenue (MONETARY, TOTAL). A **measurement**: every kWh is metered, prices come frozen per quarter-hour from `bilanz.py` |
| 22–24 | Ersparnis durch Optimierung — heute / Monat / Jahr | fast | Actual vs. simulated standard operation over the **measured** PV/load series (MONETARY, TOTAL). A **model**, not a measurement — `None` when the day's starting SOC is unknown |
| 25 | Netzbezug Viertelstunde | quarter-hour close + fast | Mean grid import of the last **completed** quarter-hour (comparable 1:1 with the grid operator's smart-meter portal); running quarter in unrecorded attributes `laufend_bisher_kw` / `laufend_hochrechnung_kw` |
| 26 | Bezugsspitze Monat | quarter-hour close + fast | Highest quarter-hour import this month — the capacity-charge basis; without the 2 kW billing minimum (a billing rule, not a measurement); previous months in `verlauf` |

> **Never add sensors 19–21 and 22–24 together.** The optimiser advantage is
> already contained in the PV saving — it is the share of it that stems from
> the steering, exposed as attribute `davon_optimierung`. Adding both
> double-counts. The self-check: in mode "Aus" the optimiser advantage must
> approach zero, since the plant then runs standard operation itself
> (attribute `modus_ein_anteil` makes this verifiable).

Conditional, created only when the setup calls for them: with the own PV
forecast steering *or* running as comparison (`pv_prognose_vergleich`) the
nine *Eigene PV-Prognose* sensors (Leistung in kW for the running quarter
hour — the counterpart of *PV-Leistung* — plus verbleibend heute / heute /
morgen / Tag 3–7 in kWh, the Solcast day-sensor shape). They are output
only: schedule and comparison read the provider directly. The 30-min
series on the Leistung sensor is called `prognose_halbstunden` (never
`detailedForecast`, or `_solcast_detailed` would collect it as Solcast
data) and is an unrecorded attribute. With the comparison on, the panel's
week chart draws the non-steering source as a third bar per day, read from
these sensors or from the Solcast/Forecast.Solar day sensors. *Batterieleistung* /
*Netzleistung* combined-pair sensors (split-sensor inverters like Fronius),
*Batterie-Ladestand/-Kapazität kombiniert* (multi-battery drivers), with a
configured heater the four *Heizstab*-sensors (Leistung / Temperatur /
Sollwert / Energie heute), and with a configured wallbox the four
*Auto*-sensors (Status / Ladestand / Ladeleistung / Energie Ladesitzung).
Both push from their controller's read cycle. With a heater, `Hausverbrauch`
is net of it — it is a steered sink, not load the profile should learn.

`Fahrplan-Status` keeps the unique_id of the former `Entscheidung` sensor so
the entity and its history survive — but its attributes changed completely
(`markdown`, `morning_*`, `discharge_*` are gone). Gone with the heuristic:
*Morgen-Einspeisung / Nacht-Entladung Energie heute* (1.5.1),
*Prognose bis Sonnenaufgang* and *Batterie fehlende Energie* (1.5.23 — they
were inputs of the heuristic and had no reader left). The statistics tracker
(`statistics.py`) went with them in 1.5.1 but is back: it feeds *Entladung
ins Netz*. The panel card it served (and `get_feedin_statistics`) was
removed again in 2.1.24 — the counter and the sensor stay.

### Select Entity

| Entity | Options | Description |
|--------|---------|-------------|
| `select.eeg_energy_optimizer_optimizer` | Ein / Aus | Ein executes inverter commands, Aus computes and displays only and releases what was set. „Test“ was the name of the non-writing mode until 1.5.52; `MODE_TEST` stays in `const.py` only so a stored state maps to Aus |

### Executor States

There are no named optimizer states any more — the schedule decides per
15-minute slot, and `plan_action()` translates the running slot into one of
three intents. `Fahrplan-Status` shows what actually happened:

- **Laden begrenzt auf x kW** — the plan wants surplus in the grid rather than
  in the battery, so the charge limit is capped (0 kW = charging blocked).
  Guard 1 raises the cap again when the measured export sticks to the limit,
  which is the signature of silent curtailment.
- **Einspeisung x kW bis y %** — forced discharge, named after the *planned export*, not the battery setpoint (which includes the house load); „Entladung x kW …“ only when no plan value is known; the target SOC comes **from
  the plan**, there is no independent floor. Guard 2 tracks the setpoint from
  planned export + measured house load.
- **Normalbetrieb** — inverter released to its own automatic mode.
- **Anzeige-Modus** / **Nur Anzeige** — mode is Aus (or a pause runs), or the
  driver has `supports_schedule_control=False` (status text „Treiber wird nicht gesteuert — Plan nur Anzeige“). Plan is computed and shown,
  nothing is written.
- **Failsafe / Not-Aus** — no fresh plan for 15 min releases the inverter;
  grid import above 1 kW in three consecutive runs blocks discharge until the
  slot changes.

### Activity Log

- **Ring buffer**: 5000 entries (`collections.deque`), persisted via `homeassistant.helpers.storage.Store`
- **Logging**: At full hours (:00) as heartbeat + on every state change
- **API**: Paginated WebSocket endpoint (`get_activity_log` with `offset`/`limit`)
- **Frontend**: Loads 100 entries initially, "Mehr laden" fetches 100 more per click, live events via subscription

### WebSocket API (36 commands)

Home Assistant hands a `websocket_command` to **every logged-in user** —
`ActiveConnection.async_handle` checks no permissions. Anything that writes
config, dials out to a caller-chosen host, drives hardware with no entity
equivalent, or changes consent therefore carries
`@websocket_api.require_admin` (topmost decorator, above `websocket_command`
and `async_response`): `save_config`, `detect_sensors`, the five `probe_*` (`probe_pvprognose`
dials a fixed host, but with caller-chosen surfaces — same family),
`ambibox_manual`, `refresh_consumption_profile` and the three `telemetry_*`.
The read commands stay open so the dashboard works for non-admins, and so do
`set_override` / `clear_override` / `refresh_schedule` / `tagesbilanz_jetzt`
— a non-admin reaches those through the `pause` / `aufheben` services and
`number.set_value` anyway, so gating them would restrict operation without
protecting anything. `tests/test_websocket_admin.py` pins the classification
of every single command and fails on a new, unclassified one.

| Command | Description |
|---------|-------------|
| `eeg_optimizer/get_config` | Read config entry data |
| `eeg_optimizer/save_config` | Update config entry |
| `eeg_optimizer/probe_ambibox` | Read-only Modbus probe of an Ambibox (settings connection test) |
| `eeg_optimizer/ambibox_manual` | Start/stop the manual charge or discharge test (the only write path to the wallbox) |
| `eeg_optimizer/check_prerequisites` | Check required integrations |
| `eeg_optimizer/detect_sensors` | Auto-detect Huawei sensors |
| `eeg_optimizer/get_entity_ids` | Resolve the integration's own entity_ids for the panel |
| `eeg_optimizer/probe_fronius` | Probe Fronius Modbus TCP during setup |
| `eeg_optimizer/probe_kostal` | Probe Kostal Modbus TCP during setup |
| `eeg_optimizer/probe_sma` | Probe SMA Modbus TCP during setup |
| `eeg_optimizer/get_schedule` | Current plan — slots, header values, prices; plus `referenz_slots` (simulated standard operation) and `gewinn` (profit breakdown vs. standard operation, real money flows) |
| `eeg_optimizer/refresh_schedule` | Recompute the plan now |
| `eeg_optimizer/get_control_state` | What the executor last wrote vs. what the driver reports — no panel card any more, kept for debugging |
| `eeg_optimizer/get_schedule_archive` | List archived plans (ZIP download goes through the HTTP view) |
| `eeg_optimizer/get_activity_log` | Paginated activity log (offset, limit) |
| `eeg_optimizer/get_peakshare_communities` | List of PeakShare community names for dropdown |
| `eeg_optimizer/get_peakshare_data` | PeakShare community demand forecast |
| `eeg_optimizer/get_oemag_tarif` | Current OeMAG market price (base tariff option); with `schaetzung: true` also computes/returns the current-month estimate under `schaetzung` |
| `eeg_optimizer/get_netzentgelte` | Grid usage tariff per grid area (net + gross, AP/SNAP/WiNAP) with regulation, effective date, age, last error (`refresh` forces a fetch) |
| `eeg_optimizer/get_bilanz` | Money balance for the "Was deine PV bringt" card — PV saving and optimiser share for today / month / year plus the day's breakdown (incl. `vorteil_begruendung` when the share is negative) |
| `eeg_optimizer/get_einspeisung` | Feed-in card (`zeitraum` = `heute`/`monat`/`jahr`/`gesamt`): exported, from the battery (quarter-hours with PV < 50 W), absorbed by the community, revenue per kWh, self-sufficiency, self-consumption; comparison with standard operation over the days that have a reference only; chart series (quarter-hours / days / months) |
| `eeg_optimizer/get_override` | Active pause or `{aktiv: false}` |
| `eeg_optimizer/set_override` | Start a pause — `stunden` and/or `bis_soc_pct` (at least one); replaces a running one, takes effect immediately, answers with the state *after* the immediate guard run |
| `eeg_optimizer/clear_override` | End the running override |
| `eeg_optimizer/get_spot_preis` | Current exchange spot price, data range, age (base tariff option; `refresh` forces a fetch) |
| `eeg_optimizer/get_awattar_sunny` | aWATTar SUNNY monthly tariff for both contract variants (`neu`/`alt`) with month, source (`tabelle`/`tarifseite`), age, last error (base tariff option; `refresh` forces a fetch) |
| `eeg_optimizer/get_energie_ag` | Energie AG feed-in tariff for both price variants with reference market value, month, age, last error, and the current-month estimate (base tariff option; `refresh` forces a fetch of both the monthly value and the estimate, `abschlag` lets the panel preview an unsaved discount) |
| `eeg_optimizer/get_pvprognose` | Own PV forecast: surfaces, location, 7 daily sums, age of the weather data, last error (`refresh` forces an Open-Meteo fetch) |
| `eeg_optimizer/probe_pvprognose` | "Prognose berechnen" — one-off fetch + model run with the *unsaved* surfaces (`flaechen` incl. optional `max_kw`, `verluste_pct`, `ac_limit_kw`), stores nothing; `invalid_config` for input/location problems, `fetch_failed` for network |
| `eeg_optimizer/get_prognosevergleich` | Prognosevergleich for the dashboard card: list of recorded days, per-day stats, summary over complete days (bias, MAE, closer-count, empirical p10), one day in detail (`datum`, else the newest) with the three 30-min series |
| `eeg_optimizer/tagesbilanz_jetzt` | Build yesterday's daily balance now instead of waiting for 00:15 |
| `eeg_optimizer/refresh_consumption_profile` | Manually recompute the consumption profile from recorder statistics |
| `eeg_optimizer/telemetry_enable` | Opt in to reporting |
| `eeg_optimizer/telemetry_disable` | Opt out |
| `eeg_optimizer/telemetry_forget` | Delete the identity at the backend |
| `eeg_optimizer/telemetry_get_status` | Reporting status |

Gone with the heuristic: the `*_test_overrides` commands (1.5.1), the
`manual_*` commands (1.5.5, when manual control was dropped), plus
`test_inverter` (the connection-test button is gone) and
`get_consumption_profile_status` (the panel reads it from sensor attributes)
in 1.5.23. `get_feedin_statistics` was dropped in 1.5.1, came back with the
feed-in statistics card and went again with it in 2.1.24; `statistics.py`
stays for the *Entladung ins Netz* sensor.

### Inverter Abstraction

```
InverterBase (ABC)
  Write path (abstract — every driver implements these; on failure each sets
  `last_write_error` via the inherited `_fehler("…")`, a short reason without
  changing numbers since it becomes the telemetry's `message_hash`):
  ├── async_set_charge_limit(power_kw) → bool
  ├── async_set_discharge(power_kw, target_soc) → bool
  ├── async_stop_forcible() → bool
  └── is_available → bool

  Schedule control (optional — the executor only steers a driver that
  offers the whole set; defaults keep the others display-only):
  ├── supports_schedule_control → bool          (default False)
  ├── async_get_charge_limit_kw() → float|None  (Guard 1 counts up from the
  │                                              limit actually set — with
  │                                              curtailment active, measured
  │                                              PV is already clipped)
  ├── get_charge_limit_max_kw() → float|None    (upper bound for Guard 1)
  ├── get_max_discharge_power_kw() → float|None (upper bound for Guard 2)
  ├── get_backup_reserve_soc_pct() → float|None (raises the min-SOC floor)
  └── get_control_entities() → list[dict]       (panel transparency view)

Implementations:
  ├── HuaweiInverter — via HA huawei_solar services
  ├── FroniusInverter — via direct Modbus TCP (SunSpec Model 124, pymodbus; scale factors read from the device; InOutWRte_RvrtTms armed at 300s as the inverter-side failsafe, fed by a 60s keepalive)
  ├── KostalInverter — via direct Modbus TCP (proprietary registers, port 1502, unit 71; cyclic keepalive feeds the inverter watchdog, timeout = failsafe fallback to internal automatic)
  ├── SMAInverter — via direct Modbus TCP (CmpBMS external battery management, port 502, unit 3; every command writes the complete 6-register block, 60s keepalive, 300s watchdog fallback; discharge = grid-exchange setpoint GridWSpt → house load auto-compensated, so the executor hands over the planned export (`discharge_is_grid_setpoint`))
  ├── SolaXInverter — via HA solax_modbus Mode 1
  └── SigenergyInverter — via HA sigen integration (Remote EMS entities)
```

**Multi-Inverter / Multi-Battery (Master/Slave):** Huawei supports setups
with multiple inverters + batteries. Each battery is a separate
device in the source integration (no cross-device summing), so the driver must
read **and** control every battery:

- **Huawei** addresses each battery via its `device_id` (services
  `forcible_discharge_soc` / `stop_forcible_charge`) and resolves per-device
  entities (charge-limit number, SOC, capacity) through the HA **entity
  registry** — robust against DE/EN naming. Config key `huawei_device_ids`
  (list); `huawei_device_id` remains as legacy single fallback.
- `get_combined_battery_state()` (InverterBase) returns a capacity-weighted SOC
  + summed capacity; the optimizer snapshot overrides its config-sensor values
  with it. Single-battery setups return `(None, None)` → unchanged behavior.
- Discharge power is split proportional to each battery's usable energy via the
  shared `inverter/_distribution.py` helper (equal-split fallback when a sensor
  is unavailable). On save, `ws_save_config` points `battery_soc_sensor` /
  `battery_capacity_sensor` at the synthetic combined sensors for multi-battery
  Huawei.

### Dependencies

- **recorder** — long-term hourly statistics for consumption history
- **sun** — sunrise/sunset calculations
- **http**, **frontend**, **websocket_api** — onboarding panel
- **huawei_solar** (after_dependency) — Huawei inverter control
- **fronius** (after_dependency) — Fronius sensor data via Solar API
- **kostal_plenticore** (after_dependency) — Kostal sensor data via REST
- **sma** (after_dependency) — SMA sensor data via WebConnect (directional pairs → synthetic combined sensors)
- **solax_modbus** (after_dependency) — SolaX inverter control
- **sigen** (after_dependency) — Sigenergy control (Remote EMS)
- **solcast_solar**, **forecast_solar** (after_dependency) — PV forecasts

Python requirements (`manifest.json`): `pymodbus>=3.6.0` for the direct-Modbus
drivers, plus `pandas>=2.3,<3`, `highspy>=1.15.1` and `holidays>=0.60` for the
LP solver. pandas is imported inside the worker thread only — importing it in
the event loop is long enough for HA to flag a blocking call.

## Key Domain Concepts

- **Fahrplan (Schedule)**: 15-minute slots over a 48-hour horizon, recomputed
  every minute. 15 min is the settlement grid — finer costs time without
  changing decisions, coarser blurs short price and load windows. Neither the
  slot length nor the horizon is configurable. **`start` is rounded down to
  that grid** (since 2.1.1-dev36): `opt()` resamples every series to
  `time_res`, and pandas 2.3 — the version in the HA container, unlike
  pandas 3 on a dev machine — drops a support point that sits beside the
  grid. With a start on a stray minute the measured house load and PV of the
  first point (`consumption[0]`, `production[0]`) never reached the slot the
  executor actually drives; it was planned from the profile instead, in 14 of
  15 runs.
- **`HAConfig` is the almost-only lever**: every intervention of ours is
  expressed as a parameter `opt()` already understands — except where the
  model itself was wrong, and that is exactly twice (heater, blackout
  reserve; `chamo/README.md` — a third `LOCAL CHANGE`, the solver-status
  check, changes no model). Reach for a parameter first: a divergence
  costs on every upstream merge, forever.
- **The blackout reserve must never force a purchase**: `bor`
  is capped at the fill level reachable *without buying* — house first,
  limited at empty and at full. Before that it was capped at "content + all
  PV", which let it freeze energy the house needed in the same night
  (Ansfelden 21.09.2026: 5.43 vs. 2.31 kWh grid import, −0.51 € against
  standard operation, 36 of 119 archived plans negative). The reserve still
  pushes surplus into the battery instead of the grid; it can no longer hold
  on to what is already there. Pinned by three tests in
  `tests/test_schedule.py` against real forecast series.
- **The blackout reserve must never prevent a schedule** either. The same cap
  asked whether the energy *exists*, not whether it *fits in time*: every
  surplus kWh counted as instantly stored. With a lot of PV and little
  charging power the imagined fill level outruns the real one, the reserve
  demands a state of charge the battery cannot reach, and the LP has **no
  solution** — no plan at all, not a worse one (Ansfelden 22.09.2026: 14.94
  kWh demanded for 13:45, 9.58 kWh reachable at 4.1 kW; two days of hours
  without a schedule, failsafe, plant uncontrolled). `steps` is therefore
  clamped to `battery_power_limit · p2e`, upwards only — a discharge step
  that falls too fast only lowers the level and makes the cap more
  conservative. Over 336 scenarios, 39 had no solution before; afterwards
  **all 336 solve with the full 18 h window**, so the reserve loses nothing.
  Same `LOCAL CHANGE` block, `chamo/README.md` § 2a.
  A retry cascade over a shortened lookahead was built as a safety net and
  **deliberately dropped again**: it would have caught the symptom of every
  future cause too, which is exactly the problem — a plan that quietly holds
  less in reserve looks healthy, and nobody goes looking. `infeasible` is
  supposed to be loud. What stayed from that attempt is the solver status in
  the failure report (below).
- **EEG price function** (`eeg_price.py`): the schedule steers purely on
  prices. Community demand becomes a surcharge on the base tariff —
  `surcharge_i(t) = share_i · (value_i(t) − base_tariff(t)) · demand_i(t) / peak_i`,
  summed over up to two communities. Only the **difference** to the base
  tariff enters: a kWh is worth either the utility tariff or the EEG tariff,
  never their sum. Measured: the amplitude barely matters, the time course
  decides everything (no surcharge → 4 % of feed-in lands in demand hours, up
  to 2 ct → 22 %, up to 10 ct → 24 %).
- **The cap below the purchase price is mandatory**, measured: with a feed-in
  price above it the LP buys power and sells it in the same slot for more —
  invisible in `grid_p`, which only carries the difference. Harmful because
  the battery then discharges *less*, the export limit being occupied by the
  sham trade.
- **Safety margin on the forecasts** (`schedule_sicherheitspuffer_pct`,
  0–50 %, default **0**): consumption enters the model raised by that share,
  PV lowered by it, so the plan charges sooner and discharges more
  cautiously. Applied to the forecast only — the first support point is
  overwritten with the measurement right afterwards, and a safety margin on a
  measurement would be an error, not caution (the running slot is the one
  thing there is nothing to guess about). Solcast's p10 path drops with it,
  otherwise the lower bound would sit above its own expected value. The
  default is 0 on purpose: a fixed markup is not a better estimate, it shifts
  the expected value and points the wrong way half the time, and it moves
  feed-in out of the community's demand hours into the battery. Settings
  only, expert mode only — it has no place in the wizard.
- **Own PV forecast (`forecast_source = "eigen"`, package `pvprognose/`)**:
  the third source, and the only one without a foreign HA integration or an
  account. Weather from Open-Meteo, plant from the configuration (surfaces
  with kWp / tilt / compass azimuth, losses), location from the HA core
  config. Design decisions that are not obvious from the code: (1) Open-Meteo
  does the transposition itself (`global_tilted_irradiance` with `tilt` /
  `azimuth`), one request per surface — cheaper to get right than an own
  Perez model, verified live 27.09.2026 (−90° gives the morning peak);
  (2) every value is the mean of the **preceding** interval, so the value
  stamped 08:15 is the slot 08:00–08:15 and gets shifted to the slot start
  before it meets the schedule; (3) the schedule gets 30-min means keyed
  like Solcast's `period_start`; the model has **no p10 of its own** — an
  invented percentile is none. With the Prognosevergleich switch on it
  borrows Solcast's p10/p50 ratio per half hour, or falls back to the
  empirical factor from ≥ 14 complete comparison days; without either,
  `min_production=None` and the reserve uses the 60 % factor like
  Forecast.Solar. The chosen way is spelled out in the inputs'
  `forecast_source` string (archive + panel); (3a) surfaces may carry an
  optional `max_kw` — the AC limit of *their* inverter/MPPT — clipped per
  surface after losses, before the plant-wide AC clip (two inverters clip
  separately, the sum alone would under-clip); (4) a failed fetch keeps the old series (7 days
  cover the 48-h horizon even after two days of outage), a series older
  than 48 h counts as *no forecast* so the failure is loud, and the
  "old weather" warning is throttled with the same one-shot merker as the
  price hints; (5) the model knows no shading, snow or MPPT window — the
  calibration (`kalibrierung.py`) learns those per sun position from the
  plant's own measurement; it learns from the Prognosevergleich days only
  and needs a season to have seen every sun position, so until then the
  intraday shape under shading is right only where the sun has already
  been measured; (6) the weather is the mean of three models, because ICON
  alone saw fog and haze in the Linzer Becken on clear mornings (Traun
  27./28.09.2026, −40 % in the morning). Open-Meteo is free for
  non-commercial use only.
- **Minimum state of charge** is a **hard floor**, modelled as *missing
  capacity* (`opt()` counts free room up to full, so a smaller capacity cuts
  the bottom off). Capped at 20 percentage points below the maximum SOC
  (`schedule.py`), so a usable range is always left to carry a night. There is no separate blackout reserve — the minimum SOC
  *is* the safety reserve. The inverter's own backup SOC raises it when
  higher, otherwise the device refuses discharges the plan expects. The
  `max_blackout_reserve` route was built and discarded (it is forward-looking
  and releases the floor whenever no deficit lies ahead — deepest planned SOC
  stayed at 30.8 % for every setting); do not retry it. Note that the target
  SOC handed to the inverter comes from the plan; there is no independent
  interlock, the protection lives in the plan alone.
- **All six drivers are steered and selectable**: every driver returns
  `supports_schedule_control=True`, and `SCHEDULE_CONTROL_INVERTERS` in the
  panel lists all six for the wizard (`NUR_HUAWEI_WAEHLBAR` is gone). All six are
  „freigegeben“ (since 2.1.29 — the field test is done); „Feldtest“ remains
  the entry stage for a new driver only, see `docs/DEVELOPMENT.md`. Status and open points per driver: `docs/wechselrichter-status.md`,
  the single source of truth for users (no other doc keeps its own list); the
  release path for a new driver lives in `docs/DEVELOPMENT.md`.
- **Not-Aus** (`GUARD_EMERGENCY_IMPORT_KW` = 1 kW, `GUARD_EMERGENCY_IMPORT_RUNS`
  = 3): without the old grid-import watchdog, discharge would be unsecured if
  the grid sensor misreads or the house load sits permanently above the
  discharge power (buying power to sell it cheaper). Blocks discharge until
  the slot changes.
- **A driver's `True` is not proof of effect**: it only means the writes were
  acknowledged. Grünbach, 21.09.2026: from 18:15 to 20:00 the executor
  commanded 1.26 kW of discharge, partly with successful confirmation — the
  battery delivered 0.2–0.6 kW (the house load) and nothing reached the grid
  for 1¾ hours. **Guard 3** (`GUARD_WIRKUNG_*`) asks the question nobody was
  asking: measured output below half the setpoint *and* more than 0.3 kW
  short, over 6 runs → `async_release()` and a fresh command on the next run.
  Both thresholds must break together (the ratio alone trips on noise at small
  setpoints, the absolute gap alone never trips at large ones), the target SOC
  is exempt (an emptied battery is a fulfilled command, not a missing one),
  and after `GUARD_WIRKUNG_MAX_NEUVERSUCHE` per slot it stops and says so
  instead of flapping between stop and command.
- **Write failures need a brake and a reason.** After a failed write
  `_written_*` keeps the last CONFIRMED value while the setpoint drifts on, so
  the difference clears the deadband and *every* run rewrites — one rejected
  register value became 1258 failed writes in a day. `EXECUTOR_WRITE_RETRY_MAX_RUNS`
  caps a doubling backoff, keyed on the intent that failed (**not** on
  `_active_kind`: that is `None` after a failure, which would look like a state
  change and disable the brake in the only case it is for). The reason travels
  via `InverterBase.last_write_error`, set by each driver where it arises
  (`_fehler()` / `_erfolg()`), and reaches the telemetry both in the context
  and in the `message_hash` — two causes of the same action must not
  deduplicate each other away.
- **PeakShare is an input, not an actor**: the demand forecast feeds the price
  function; it no longer computes a discharge window. Hourly values suffice —
  `opt()` resamples to 15 min itself (hourly means deviate ≤ 5 %, no time
  shift). Refreshed every 30 min, because when fetched only at startup the
  timestamps age into the past within a day and the surcharge goes silent.
- **Heizstab (heater)**: A Fronius Ohmpilot as a second sink. Since
  2.1.1-dev2 it is a **valued LP variable** (`heater_p` ≤ `discard_p` in
  `opt_highs.py`, objective += heat × `heizstab_waermewert`, capped per slot
  by `heizstab_max_kw` and per calendar day by `heizstab_budget_kwh` from
  buffer volume × (max temperature − measured temperature)). **Hard bound since 2.1.1-dev25: `heater_p` ≤ PV production − house load/η per slot — the battery never feeds the heater.** Without it the LP fed the house from the battery and the heater from PV (Grünbach 14.09.2026: 3.38 kW heat next to 2.02 kW discharge), because it valued the stored kWh only at the feed-in price. The executor mirrors this: planned heat in a slot that also plans discharge is not executed (`_heizstab_plan_kw`). With heat value
  > feed-in price the model deliberately curtails feed-in in favour of the
  buffer; without volume or heat value it falls back to "heater takes what
  is curtailed anyway" (`_heizstab_plan_kw` from the `discard` column).
  **Execution follows the plan, bounded by the measurement** (2.1.1-dev7):
  the executor hands the running slot's `heizstab` kW to `regeln(plan_kw=…)`,
  which regulates on "export ≈ 0" but never above the planned value — weaker
  PV than forecast lets the setpoint fall back instead of burning grid power.
  Below the export limit the setpoint rises only by the **measured** surplus
  (≤ 0.5 kW per run, `naechster_sollwert(tasten=False)`) and settles at 0.3 kW
  export — the fixed 0.5-kW step there produced a limit cycle with grid import
  every second run (Grünbach 14.09.2026, fixed 2.1.1-dev26); at the export
  limit the fixed step stays (curtailment hides the surplus). A single missing
  grid reading holds the last setpoint for one run
  (`HEIZSTAB_NETZ_FEHLT_HALTEN_LAEUFE`), the second in a row means 0. **Measured battery
  discharge (> 0.1 kW) counts as grid import** for the heater
  (`HEIZSTAB_BATTERIE_ENTLADUNG_TOLERANZ_KW`): the battery covers a PV drop
  so fast that the grid meter stays at ≈ 0 and the "export ≈ 0" rule sees
  nothing — Grünbach 25.09.2026, 5 min of 3–3.8 kW discharge into the heater
  with the setpoint parked in the dead band. Battery *charging* does not count
  as surplus (that share belongs to plan and cap). Actual
  power > 2 × `heizstab_max_kw` counts as no reading (int32 read error).
  `compute_heizstab_kw` resolves the entry's OWN controller — by config-dict
  identity, else by Ohmpilot host (two entries on one instance). Only a release
  with reason "Normalbetrieb (Batterie voll)" counts as a saturated battery for
  the surplus share; a release for house-discharge does not.
  A stale plan (> 15 min, same check as the inverter part) counts as no plan.
  Without planned heat the surplus rule at the export limit applies (up in
  0.5-kW steps because curtailment hides the true surplus, down by half the
  measured gap per run after one settling run, the full gap at once on grid
  import — `HEIZSTAB_RUECKNAHME_ANTEIL`), shared with the battery by SOC.
  Never during a forced discharge, never in mode Aus ("Optimierung aus heißt
  Heizstab aus"). Minimum temperature = comfort guard: by default priority
  over feed-in (export ≈ 0, never grid or battery power); with
  `heizstab_netzbezug` (opt-in) full power from anywhere while the executor
  releases a planned discharge. `heizstab_sperr_entity` blocks the heater
  while a second heat source runs (unreachable entity = not blocked).
  Requires the Ohmpilot to be **decoupled from the Gen24**. House load,
  consumption profile, Guard 2 and the balance all subtract the heater
  (`compute_heizstab_kw`; bilanz column `heizstab`). **Every PV-fed kWh into
  the buffer counts at the full heat value** — the former "temperature of the
  other heat source" (Zusatzwärme) distinction was removed in 2.1.1-dev6
  because it contradicted the plan. Config keys `heizstab_*` live in their
  settings tab **Verbraucher** (shared with the wallbox); host/port/max/enabled trigger a full reload,
  the rest hot-reload. Foreign control (a second writer on the Ohmpilot) is
  detected after 3 min of "draws more than set"; the opposite case ("delivers
  less than set" for 2 min, `folgt_nicht`) is detected too. Both are shown in
  the status card. The heater's measured power reaches the status card through
  the Hausverbrauch sensor's attributes, not its own entity: that card must
  show one point in time (see **Status card** below).
- **Status card — one point in time**: PV, battery, grid and Hausverbrauch are
  computed together every 30 s; the heater pushes its own value every 10 s.
  Reading the four entities separately put three different timestamps in one
  card and the balance did not add up (Grünbach, 14.09.2026: 72 % of samples,
  up to 5 kW off). The panel therefore reads PV/battery/grid/heater from the
  **attributes of the Hausverbrauch sensor**, which reads them in one go, and
  the Hausverbrauch value from its state. `bilanz_unvollstaendig` marks the
  case where the formula went negative and was clamped to 0 — that 0 is a
  limit, not a measurement. Any new value on that card belongs in the same
  attribute set.
- **Consumption Profile**: Hourly averages from recorder in two groups — `wt` (Mon–Fri unless a holiday) and `we` (Sat, Sun, every holiday) — over a rolling window (default 4 weeks), trimmed mean. Two groups instead of seven weekdays give ~20 instead of 4 support values per weekday hour (`coordinator.py` docstring).
- **The Hausverbrauch backfill must compute exactly what the live sensor
  computes, and it fills gaps only.** It runs on every start and rebuilds
  hourly statistics from the source sensors (`power_readings.backfill_stunden`).
  Until 23.09.2026 it overwrote the whole lookback window — and it had
  forgotten the heater: the sensor measured 0.69 kW, the statistic said
  4.48 kW (Grünbach 15.09., 13–14 h), the profile learned 23.5 instead of
  10–13 kWh/day. The EMMA sign bug was the same pattern. Hours that already
  have a statistic now stay; a formula change bumps `BACKFILL_FORMEL`, which
  rewrites the window once per entry (Store `…_backfill`). **Units:** the source statistics are requested with `units={"power": "kW"}` — without it HA returns them in the sensor's *display* unit, which can differ from the stored one; formula 3 applied the metadata factor (W → 0.001) on top of values HA had already converted and wrote hourly house load 1000× too small (Traun 28.09.2026: profile 0.02 kWh/day, plan without house load). The state-history fallback uses the state's unit (`history_faktor_kw`).
- **Dual Update Timers**: Slow sensors (profile) every 15min, fast sensors (forecasts, battery, Hausverbrauch) every 1min. Hard-wired since v26 — the former config keys `update_interval_fast_min`/`update_interval_slow_min` are removed by migration.

### Wallbox / Ambibox (read-only, step 1)

`wallbox_type` selects the driver the way `inverter_type` does; currently only
`ambibox`. Configured **exclusively in the settings** (tab "Verbraucher", expert
mode only) — deliberately not in the wizard: the car is an accessory, not a
prerequisite for the schedule.

The vehicle is shown **inside the status card** (`_renderAutoZeile`), not in
a card of its own — it belongs to the plant's current state. The panel reads
the *Auto* sensors rather than calling a WebSocket command: the controller
writes them on every poll and the panel is subscribed, so the display is live
without any polling of its own.

**Manual test (`ambibox_manual`)** is the only write path. Someone standing at
the car starts charging or discharging at a chosen power for a chosen time;
the schedule never touches the wallbox. Three safeguards hang on that path,
because the device's behaviour is unknown: the setpoint is rewritten every
`AMBIBOX_KEEPALIVE_S` (unknown watchdog), it expires on its own after at most
`AMBIBOX_MANUAL_MAX_MINUTES`, and unloading the integration stops it. Entry is
refused without a plugged-in car, with `Control Mode = 0`, and for discharge
without ISO 15118-20.

Three questions remain open, none of which the vendor document answers — the
manual test exists to settle them at the device:

1. **Watchdog** — how long does a `Target Power AC` setpoint stay valid without
   being rewritten? Unknown; 30 s was picked as tight enough for any usual
   watchdog.
2. **Sign convention** — the document does not fix it, and evcc's driver (not
   MIT-licensed, do not copy) treats setpoint and measurement in opposite
   directions. Two consequences: the controller derives the *displayed*
   direction from `Battery State` (SLEEP/IDLE/CHARGE/DISCHARGE) and keeps
   power as a magnitude, and the *setpoint* sign is a setting
   (`ambibox_charge_sign`, default "negative = charge") instead of a
   hard-coded guess.
3. **Coexistence** — sidOS optimises on its own (`ESS State`). What happens
   when we write at the same time is undocumented; the Ohmpilot/Gen24 case
   showed how that ends.

### Heizstab im Optimierungsmodell (2.1.1-dev2)

> **Achtung, Upstream-Divergenz:** Mit dieser Änderung ist `chamo/opt_highs.py`
> nicht mehr Haralds unveränderter Stand. Das LP hat seitdem eine
> Entscheidungsvariable (`heater_p`), zwei Nebenbedingungen (Kopplung an
> `discard_p`, Pufferbudget) und einen Term in der Zielfunktion mehr. Bei
> jedem Abgleich mit Haralds Original ist dieser Block gesondert zu
> behandeln; alles andere im Modell ist unangetastet.

Bis dahin war `discard` in der Zielfunktion nichts wert: Das Modell regelte
ab, wenn es musste, und der Heizstab bekam den Rest nachgelagert zugeteilt
(`_heizstab_plan_kw`). Damit konnte es abends entladen, obwohl dieselbe
Kilowattstunde am nächsten Tag im Puffer mehr gebracht hätte.

Jetzt wird `discard_p` in `heater_p` + `spill_p` aufgeteilt, und `heater_p`
geht mit dem Wärmewert in die Zielfunktion. Zwei Deckel begrenzen ihn:
`heizstab_max_kw` je Slot (was physikalisch durchpasst) und
`heizstab_budget_kwh` **je Kalendertag** (was der Puffer noch aufnimmt) statt
eines zweiten Speichers mit Zeitverlauf. Das Budget kommt aus der gemessenen
Puffertemperatur (`HeizstabController.puffer_budget_kwh`, 1,163 Wh/(L·K)) und
wird bei jedem Planlauf neu gebildet: Es schrumpft, während der Puffer warm
wird, und gibt die Abendentladung von selbst wieder frei.

Die Tagesschranke ist seit 2.1.1-dev17 wichtig (vorher eine Summe über den
ganzen Horizont): Der Puffer ist kein Vorrat, der einmal gefüllt wird — über
Nacht kühlt er aus und wird leergezapft. Mit einer Schranke über 48 Stunden
sparte das Modell die Kapazität für den sonnigsten Tag auf und ließ die Wärme
heute liegen, obwohl sie bis dahin ohnehin verloren geht. Für den laufenden
Tag stimmt die Schranke exakt (gemessene Temperatur), für die Folgetage ist
sie eine Annahme, die jeder neue Planlauf korrigiert.

**Alles davon hängt an drei Bedingungen** — Leistung, Wärmewert *und* Budget
müssen größer null sein. Fehlt eine, wird im LP nichts aufgebaut und das
Modell ist strukturell identisch zu vorher (Regressionstest
`test_ohne_heizstab_identisch`). Das ist bewusst so: Ein unbegrenzt bewerteter
Heizstab würde jede Entladung dauerhaft blockieren.

`heizstab_sperr_entity` sperrt den Heizstab, solange eine zweite Wärmequelle
(Holzvergaser, Kessel) den Puffer selbst heizt — an zwei Stellen, weil eine
nicht reicht: sofort im Controller (`regeln()`, noch vor der
Mindesttemperatur) und im Plan (Budget 0). Eine unerreichbare Entität gilt
als „frei": Ein ausgefallener Sensor soll den Heizstab nicht unbemerkt
stilllegen.

## Config Flow & Onboarding

The config flow is a single-click setup that creates a config entry with `setup_complete=False`. Full configuration happens through the sidebar panel (`/eeg-optimizer`), which provides:

Wizard steps (`RENDERERS` map in the panel — the step methods are called
dynamically by name, so they look unreferenced to a grep). Steps 1–3 are the
sensors (assigned once), steps 4–6 are the parameters:

1. Willkommen — prerequisite checks (inverter integration installed?)
2. Wechselrichter — type selection, auto-detection / Modbus probe, power
   sensors (PV / battery / grid, directional pairs for Fronius & SMA)
3. Batterie — SOC sensor, capacity (sensor or manual, per-device for Huawei
   Master/Slave)
4. PV-Prognose — forecast source (Solcast / Forecast.Solar / **Eigene
   Berechnung**). Solcast and Forecast.Solar take two mandatory forecast
   sensors (expert mode: day 3–7 sensors); "eigen" takes the plant instead:
   a surface table (`pv_flaechen`: name, kWp, tilt, compass azimuth, up to
   8 rows, add/remove, direction label next to the azimuth), the loss
   percentage and a "Prognose berechnen" button (`probe_pvprognose`, works
   before anything is saved). The surface inputs carry `data-flaeche` /
   `data-key` / `data-scope` instead of `data-field`, because the settings
   save re-reads every `data-field` flat and a list does not fit; the same
   renderer (`_pvPrognoseFelder`) serves the settings card in **Prognose**.
   Leaving the step pre-fills `pv_peak_kwp` with the kWp sum when empty.
   Pre-selected when neither forecast integration is installed. Each row
   also has an optional "Grenze (kW)" (`max_kw`, the AC limit of that
   surface's own inverter). The wizard's "Prognose berechnen" runs before
   the AC limit is known (it is asked one step later) and says so under
   the result; the settings preview and the live provider always clip.
   The step also carries the **Prognosevergleich** feature card; with the
   switch on and a foreign source steering, the surface table appears
   below it. Settings tab **Prognose** mirrors this step: the "PV-Prognose"
   card with the steering source as the wizard's three cards in compact form
   (`select-settings-forecast`; only a real switch fills the
   Solcast/Forecast.Solar sensors from detection), the sensors as pickers
   collapsed under "Prognose-Sensoren" (kept in the DOM with display:none,
   because the save re-reads every data-field; open by itself while a
   mandatory one is missing), in expert mode also Solcast's today/day 3–7 sensors — the one exception
   to "sensor mappings are wizard-only", because a source switch is useless
   without them — and reloads on save), plus the same feature card; the
   surface table appears there when the own forecast steers *or* runs as
   the comparison source
5. Anlage & Batterie — AC power limit, PV peak power (both mandatory, checked
   in the wizard *and* on save), export limit, battery power limit, minimum
   state of charge, maximum state of charge (always visible, no toggle —
   100 = charge to full, the value alone carries the state since v27)
6. Tarife & Gemeinschaft — base tariff (manual, OeMAG published month, OeMAG current-month estimate, spot with cent and/or percent fee, aWATTar SUNNY monthly tariff with contract variant, or Energie AG „Team Sonne Float“ (published or estimated, with discount and price variant); manual also takes a
   night rate `schedule_feedin_price_night`), consumption price, community
   shares/prices/weights, demand source PeakShare forecast or fixed acceptance
   quota (`eeg_demand_source`; quotas per community, day mandatory in quote
   mode, name becomes free text). The base tariff's night window belongs to the
   source „Fester Wert“ only (the other sources have no night rate); each
   community has its own window. Only whole hours count — the planner reads
   the hour of the time field (`_stunde_aus_zeit`). Second community collapsed
   behind a button; expert mode: battery aging cost
7. Zusammenfassung — with the checkbox „Steuerung nach dem Fertigstellen
   einschalten“ (`steuerung_einschalten`, pre-checked, only for steered
   drivers). It is a one-off operating decision, not a setting: stripped from
   the saved config; after the reload `_modusEinschalten()` sets the select to
   Ein (retried, since the select restores its last state during the reload).
   Without it a fresh install stays on **Aus** (`select.py`) — the member guide
   promised steering that never happened

Settings live in five tabs (Verbraucher only in expert mode): **Tarife**, **Anlage** and **Prognose** are exactly the
parameter wizard steps (same field renderers, `settings_` prefix); **Verbraucher**
holds the *Heizstab* and *Wallbox* cards, expert mode only —
`_heizstabFields` / `_wallboxFields`, settings only, not in the wizard (a
remembered `heizstab` tab maps to it); **System**
holds the expert-mode switch, a read-only sensor overview (with the
restart-wizard button — sensor mappings are wizard-only by design), telemetry
opt-in, schedule archive, and (expert) balance card + profile lookback. In the
settings, device-datasheet values (AC limit, PV peak, battery power limit) are
expert-only; in the wizard they are always visible. The settings use the full content width
(`.settings-wrap`, 900 px like the dashboard; it was 600 px and broke the
surface table in two rows); the surface table wraps on its **own** width
(`@container`, 6 → 3 → 2 columns), not the viewport's, because the HA
sidebar takes 0–256 px.

Dashboard notes: the **Einspeisung** card (below "Was deine PV bringt",
`_renderEinspeisungKarte` / `_einspeisungChart`) shows energy, not money —
apart from revenue per kWh (a ratio) it carries no € amount, so nothing
reads as a summand next to the balance card. One period at a time
(`chart-range` with `data-chart="einspeisung"`, pref `einspeisung_zeitraum`).
Bar details work by tap: the click handler maps the x position to the bar
(`svg[data-einsp-n]`), because a quarter-hour is 3 px wide on a phone and
`<title>` never shows on touch; for the same reason the "aus der Batterie"
definition is a visible note, not only a tooltip.
The status card ends with the capacity-charge line
(`_renderSpitzeZeile`, below `_renderAutoZeile`): only "Bezugsspitze <Monat>"
and the value — details (time, running quarter-hour, last quarter, previous
months) live in the ⓘ `.info-popup-trigger` (hover on desktop, tap on touch,
bottom sheet on phones); a tap on the rest of the row opens the sensor. The
value turns orange when the running quarter's projection exceeds the peak. It reads the two
leistungsspitze sensors, not the Hausverbrauch attributes — it is not part of
the one-point-in-time power set. During the executor's startup grace period (status
"Startphase — …") the status card shows only that hint — no setpoints,
reasons, warnings, or job line. The "Gesetzte Steuerwerte" card is gone
(2.1.24, also from expert mode — the user did not want it in the panel);
`get_control_state` stays as a WebSocket command for debugging.

Config entry version: 29 (migrations in `__init__.py`). The own forecast
added no migration: `pv_flaechen` / `pv_verluste_pct` are optional keys,
absent for every other source.

## Development Notes

- Tests in `tests/` directory, run with `pytest` (asyncio_mode=auto)
- `pyproject.toml` configures pytest
- All UI strings in German (`strings.json`, `translations/de.json`), English fallback (`translations/en.json`)
- HA imports are guarded with try/except for test environment compatibility (stubs provided)
- The plan is computed and displayed in every mode; inverter commands are
  written only in mode "Ein" and only for a driver with
  `supports_schedule_control=True`
- Treat `chamo/` as upstream code: it is Harald Geyer's, and every line we
  change costs on every merge with him. Two divergences change the model
  (heater, blackout reserve) — both marked `LOCAL CHANGE`, both explained in
  `chamo/README.md`, both reported upstream; a third `LOCAL CHANGE` only
  checks the solver status. A third one needs a reason of
  the same weight: the model is provably wrong, and no parameter can express
  the fix. `tests/test_chamo_highs_adapter.py` compares HiGHS against GLPK
  column by column and stays valid (it loads the same file twice).
  `chamo/opt_test.py` has a syntax error upstream and is never imported
- Before deleting seemingly unused panel code, check for **dynamic** dispatch:
  `_renderStepWillkommen` … `_renderStepZusammenfassung` run through the `RENDERERS` map,
  `toggle-telemetry` / `forget-telemetry` are handled before the switch,
  `.toast-*` classes are assembled as `toast-${type}`, and `guide-alert`
  classes live in the generated guide HTML
- Config changes trigger full integration reload via `_async_update_listener`
- `__pycache__/` directories should be added to `.gitignore`

## Documentation Sync (docs/ ↔ Panel)

- `docs/guides/*.md` + `docs/images/**` are the **single source of truth** for the in-app guides ("Anleitung" dialogs in the panel)
- `scripts/build_guides.py` converts them to HTML fragments in `custom_components/eeg_energy_optimizer/frontend/guide/` (requires `pip install markdown`); the panel fetches these at runtime
- **Never edit `frontend/guide/*.html` directly** — edit the Markdown source and regenerate
- After changing any file in `docs/guides/` or `docs/images/`: run `python scripts/build_guides.py` and commit both sides
- CI (`.github/workflows/docs-sync.yml`) runs `build_guides.py --check` and fails on divergence
- Markdown conventions (alerts, secondary text, image paths) are documented in `docs/DEVELOPMENT.md`
- `docs/README.md` is the end-user entry page — keep it free of developer notes
- Installation docs (`docs/installation/`) exist only in `docs/` — they have no panel counterpart
