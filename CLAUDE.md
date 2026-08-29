# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**EEG Energy Optimizer** — a Home Assistant custom integration for grid-friendly battery management, optimized for energy communities (Energiegemeinschaften / EEG) in the DACH region. It computes a 36-hour charge/discharge schedule with a linear program (Harald Geyer's `opt()`, vendored under `chamo/`) and steers the battery so that feed-in lands in the hours the community actually needs it.

This repository is the **chamo prototype** — a clone of the main integration with the same domain, so only one of the two can be installed per HA instance. Harald's code in `chamo/` stays unmodified; every adjustment goes through the parameters `schedule.py` hands to `opt()`.

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
  → 30min timer: PeakShare + OeMAG refresh

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
        Failsafe — no fresh plan for 15 min → release the inverter
        Deadbands — 0.2 kW / 1 % SOC, so we don't write on LP noise
  → writes only via InverterBase, only in mode "Ein", only for drivers with
    supports_schedule_control=True (currently Huawei only)
```

### Key Files

| File | Role |
|------|------|
| `__init__.py` | Entry setup, 30s guard timer + 1min schedule timer, activity log, panel registration, telemetry watchdogs, config migration |
| `schedule.py` | Planning — `ScheduleInputs`, `HAConfig` (the bridge to `opt()`), `ScheduleRunner` (collect in loop, solve in worker thread); profit comparison (`simuliere_standardbetrieb` greedy self-consumption reference + `bewerte_geldfluesse` with real tariffs — community rates only for energy the community's quarter-hour saldo actually absorbs, rest at base tariff/spot series; incl. end-of-horizon battery credit) |
| `schedule_executor.py` | Execution — `plan_action()` + `ScheduleExecutor.async_guard_cycle()`; the **only** place that writes inverter commands |
| `eeg_price.py` | Synthetic feed-in tariff from community demand — turns PeakShare demand into a price surcharge |
| `oemag.py` | Optional base tariff: OeMAG monthly market price, scraped from the HTML table (no API), cached across restarts |
| `power_readings.py` | Shared sensor reads — house load, PV now, grid export, battery capacity resolution |
| `schedule_archive.py` | Rolling archive of computed plans (7 days, gzip, ~8 KB each) for after-the-fact debugging |
| `schedule_archive_view.py` | HTTP view that packs archive + settings + measured history into a downloadable ZIP |
| `chamo/` | **Upstream, unmodified** — Harald Geyer's LP optimizer (`opt_highs.py`, `timetableopt`) plus a HiGHS adapter |
| `sensor.py` | 25 sensors (+4 conditional): consumption profile, forecasts, power flows, plan values, grid discharge energy, register writes, Fahrplan-Status, money balance |
| `bilanz.py` | Energy balance in money — records 96 quarter-hours per day (energy, SOC, **frozen** prices and community balances), evaluates them with `bewerte_geldfluesse`, and derives the optimiser advantage against a simulated standard operation over the measured series |
| `override.py` | Time-boxed user overrides — **Pause** (behave like mode Aus until expiry) and **Reserve** (raise the schedule's min-SOC floor until expiry). Persisted via `Store` so a restart mid-pause does not resume control. Evaluated in `async_collect_inputs` (reserve) and the guard cycle in `__init__.py` (pause); exposed as HA services `pause` / `reserve` / `aufheben` (`services.yaml`) |
| `coordinator.py` | Loads hourly consumption averages from recorder (rolling, weekday split) |
| `forecast_provider.py` | Abstract PV forecast provider — Solcast and Forecast.Solar implementations |
| `config_flow.py` | Single-click config flow (full setup happens in panel) |
| `peakshare.py` | PeakShareProvider — fetches + caches community demand forecasts (half-hourly refresh; hourly values, `opt()` resamples to 15 min itself) |
| `telemetry.py`, `telemetry_buffer.py` | Opt-in reporting — profile + failures only, ring buffer with backoff |
| `websocket_api.py` | 24 WebSocket commands for panel (config, schedule, control state, PeakShare, OeMAG, spot price, feed-in statistics, daily balance, probes, telemetry, activity log) |
| `inverter/base.py` | Abstract inverter interface (InverterBase ABC) |
| `inverter/huawei.py` | Huawei SUN2000 implementation via HA services — Single + Master/Slave (multi-device) |
| `inverter/_distribution.py` | Shared proportional discharge distribution (SolarEdge + Huawei multi-battery) |
| `inverter/fronius.py` | Fronius Gen24 implementation via direct Modbus TCP (SunSpec Model 124, device-read scale factors, RvrtTms watchdog + keepalive task) |
| `inverter/kostal.py` | Kostal Plenticore implementation via direct Modbus TCP (proprietary registers 1034/1038, watchdog keepalive task) |
| `inverter/sma.py` | SMA Smart Energy / Sunny Boy Storage implementation via direct Modbus TCP (CmpBMS 6-parameter method, complete-block writes, watchdog keepalive task) |
| `inverter/solax.py` | SolaX Gen4+ implementation via solax_modbus Mode 1 |
| `inverter/solaredge.py` | SolarEdge StorEdge implementation via solaredge-modbus-multi |
| `inverter/__init__.py` | Factory function `create_inverter()` |
| `select.py` | Mode select entity (Ein/Test), restores state across restarts |
| `const.py` | All constants, defaults, mode enums, state names |
| `frontend/eeg-optimizer-panel.js` | Dashboard + onboarding panel (plain HTMLElement, Shadow DOM) |

### Sensors (25 always + up to 4 conditional)

| # | Sensor | Update | Description |
|---|--------|--------|-------------|
| 1 | Verbrauchsprofil | slow | Hourly averages per weekday for dashboard charts |
| 2–8 | Tagesverbrauchsprognose heute..Tag 6 | fast | Daily consumption forecasts (7 sensors) |
| 9 | PV-Prognose heute | fast | Remaining PV today from forecast provider |
| 10 | PV-Prognose morgen | fast | PV forecast tomorrow |
| 11 | Hausverbrauch | fast | Calculated: PV - Battery - Grid (kW, MEASUREMENT) |
| 12 | PV-Leistung | fast | Current PV production (kW, MEASUREMENT) |
| 13 | Netzleistung | fast | Current grid power — positive = export (Einspeisung), negative = import (kW, MEASUREMENT) |
| 14 | Batterieleistung | fast | Current battery power — positive = charge, negative = discharge (kW, MEASUREMENT) |
| 15 | Register-Schreibvorgänge | fast | Cumulative inverter Modbus write counter (used for SolarEdge NVRAM monitoring) |
| 16 | Fahrplan Batterieleistung | fast | **Planned** battery power for the current slot — same cadence as the measured one, so recorder history makes plan and reality comparable |
| 17 | Fahrplan Netzleistung | fast | **Planned** grid power for the current slot |
| 18 | Entladung ins Netz | fast | Battery energy that actually reached the grid (kWh, TOTAL with `last_reset`) — the basis of the feed-in statistics card |
| 19 | Fahrplan-Status | 30s | Executor state ("Laden begrenzt auf 2,0 kW", "Entladung 2,8 kW bis 43 %", "Normalbetrieb", "Anzeige-Modus") + plan/written-value attributes |
| 20–22 | Ersparnis durch PV — heute / Monat / Jahr | fast | Avoided grid purchase + feed-in revenue (MONETARY, TOTAL). A **measurement**: every kWh is metered, prices come frozen per quarter-hour from `bilanz.py` |
| 23–25 | Ersparnis durch Optimierung — heute / Monat / Jahr | fast | Actual vs. simulated standard operation over the **measured** PV/load series (MONETARY, TOTAL). A **model**, not a measurement — `None` when the day's starting SOC is unknown |

> **Never add sensors 20–22 and 23–25 together.** The optimiser advantage is
> already contained in the PV saving — it is the share of it that stems from
> the steering, exposed as attribute `davon_optimierung`. Adding both
> double-counts. The self-check: in mode "Aus" the optimiser advantage must
> approach zero, since the plant then runs standard operation itself
> (attribute `modus_ein_anteil` makes this verifiable).

Conditional, created only when the setup calls for them: *Batterieleistung* /
*Netzleistung* combined-pair sensors (split-sensor inverters like Fronius) and
*Batterie-Ladestand/-Kapazität kombiniert* (multi-battery drivers).

`Fahrplan-Status` keeps the unique_id of the former `Entscheidung` sensor so
the entity and its history survive — but its attributes changed completely
(`markdown`, `morning_*`, `discharge_*` are gone). Gone with the heuristic:
*Morgen-Einspeisung / Nacht-Entladung Energie heute* (1.5.1),
*Prognose bis Sonnenaufgang* and *Batterie fehlende Energie* (1.5.23 — they
were inputs of the heuristic and had no reader left). The statistics tracker
(`statistics.py`) went with them in 1.5.1 but is back: *Entladung ins Netz*
feeds it, and `get_feedin_statistics` serves the panel card from it.

### Select Entity

| Entity | Options | Description |
|--------|---------|-------------|
| `select.eeg_energy_optimizer_optimizer` | Ein / Test | Ein executes inverter commands, Test computes and displays only (Aus is internal state only) |

### Executor States

There are no named optimizer states any more — the schedule decides per
15-minute slot, and `plan_action()` translates the running slot into one of
three intents. `Fahrplan-Status` shows what actually happened:

- **Laden begrenzt auf x kW** — the plan wants surplus in the grid rather than
  in the battery, so the charge limit is capped (0 kW = charging blocked).
  Guard 1 raises the cap again when the measured export sticks to the limit,
  which is the signature of silent curtailment.
- **Entladung x kW bis y %** — forced discharge; the target SOC comes **from
  the plan**, there is no independent floor. Guard 2 tracks the setpoint from
  planned export + measured house load.
- **Normalbetrieb** — inverter released to its own automatic mode.
- **Anzeige-Modus** / **Treiber wird nicht gesteuert** — mode is Test, or the
  driver has `supports_schedule_control=False`. Plan is computed and shown,
  nothing is written.
- **Failsafe / Not-Aus** — no fresh plan for 15 min releases the inverter;
  grid import above 1 kW in three consecutive runs blocks discharge until the
  slot changes.

### Activity Log

- **Ring buffer**: 5000 entries (`collections.deque`), persisted via `homeassistant.helpers.storage.Store`
- **Logging**: At full hours (:00) as heartbeat + on every state change
- **API**: Paginated WebSocket endpoint (`get_activity_log` with `offset`/`limit`)
- **Frontend**: Loads 100 entries initially, "Mehr laden" fetches 100 more per click, live events via subscription

### WebSocket API (28 commands)

| Command | Description |
|---------|-------------|
| `eeg_optimizer/get_config` | Read config entry data |
| `eeg_optimizer/save_config` | Update config entry |
| `eeg_optimizer/check_prerequisites` | Check required integrations |
| `eeg_optimizer/detect_sensors` | Auto-detect Huawei sensors |
| `eeg_optimizer/get_entity_ids` | Resolve the integration's own entity_ids for the panel |
| `eeg_optimizer/probe_fronius` | Probe Fronius Modbus TCP during setup |
| `eeg_optimizer/probe_kostal` | Probe Kostal Modbus TCP during setup |
| `eeg_optimizer/probe_sma` | Probe SMA Modbus TCP during setup |
| `eeg_optimizer/get_schedule` | Current plan — slots, header values, prices; plus `referenz_slots` (simulated standard operation) and `gewinn` (profit breakdown vs. standard operation, real money flows) |
| `eeg_optimizer/refresh_schedule` | Recompute the plan now |
| `eeg_optimizer/get_control_state` | What the executor last wrote vs. what the driver reports |
| `eeg_optimizer/get_schedule_archive` | List archived plans (ZIP download goes through the HTTP view) |
| `eeg_optimizer/get_activity_log` | Paginated activity log (offset, limit) |
| `eeg_optimizer/get_peakshare_communities` | List of PeakShare community names for dropdown |
| `eeg_optimizer/get_peakshare_data` | PeakShare community demand forecast |
| `eeg_optimizer/get_oemag_tarif` | Current OeMAG market price (base tariff option) |
| `eeg_optimizer/get_bilanz` | Money balance for the "Was deine PV bringt" card — PV saving and optimiser share for today / month / year plus the day's breakdown (incl. `vorteil_begruendung` when the share is negative) |
| `eeg_optimizer/get_override` | Active time-boxed override (pause / reserve) or `{aktiv: false}` |
| `eeg_optimizer/set_override` | Start a pause (`stunden`) or reserve (`min_soc_pct`, `stunden`); replaces a running one, takes effect immediately |
| `eeg_optimizer/clear_override` | End the running override |
| `eeg_optimizer/get_spot_preis` | Current exchange spot price, data range, age (base tariff option; `refresh` forces a fetch) |
| `eeg_optimizer/get_feedin_statistics` | Feed-in statistics for the panel card (daily + period summaries) |
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
in 1.5.23. `get_feedin_statistics` was dropped in 1.5.1 as well but came
back with the feed-in statistics card — it and `statistics.py` are live
again (see the handler's docstring).

### Inverter Abstraction

```
InverterBase (ABC)
  Write path (abstract — every driver implements these):
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
  ├── SMAInverter — via direct Modbus TCP (CmpBMS external battery management, port 502, unit 3; every command writes the complete 6-register block, 60s keepalive, 300s watchdog fallback; discharge = grid-exchange setpoint GridWSpt → house load auto-compensated, house-load entry guard skipped)
  ├── SolarEdgeInverter — via HA solaredge_modbus_multi StorEdge
  └── SolaXInverter — via HA solax_modbus Mode 1
```

**Multi-Inverter / Multi-Battery (Master/Slave):** Both SolarEdge and Huawei
support setups with multiple inverters + batteries. Each battery is a separate
device in the source integration (no cross-device summing), so the driver must
read **and** control every battery:

- **SolarEdge** addresses extra units via entity prefix (`solaredge_i2_`),
  derived from `pv_power_sensor_2`.
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
  Huawei (same as SolarEdge).

### Dependencies

- **recorder** — long-term hourly statistics for consumption history
- **sun** — sunrise/sunset calculations
- **http**, **frontend**, **websocket_api** — onboarding panel
- **huawei_solar** (after_dependency) — Huawei inverter control
- **fronius** (after_dependency) — Fronius sensor data via Solar API
- **kostal_plenticore** (after_dependency) — Kostal sensor data via REST
- **sma** (after_dependency) — SMA sensor data via WebConnect (directional pairs → synthetic combined sensors)
- **solax_modbus** (after_dependency) — SolaX inverter control
- **solaredge_modbus_multi** (after_dependency) — SolarEdge inverter control
- **solcast_solar**, **forecast_solar** (after_dependency) — PV forecasts

Python requirements (`manifest.json`): `pymodbus>=3.6.0` for the direct-Modbus
drivers, plus `pandas>=2.3,<3`, `highspy>=1.15.1` and `holidays>=0.60` for the
LP solver. pandas is imported inside the worker thread only — importing it in
the event loop is long enough for HA to flag a blocking call.

## Key Domain Concepts

- **Fahrplan (Schedule)**: 15-minute slots over a 48-hour horizon, recomputed
  every minute. 15 min is the settlement grid — finer costs time without
  changing decisions, coarser blurs short price and load windows. Neither the
  slot length nor the horizon is configurable.
- **`HAConfig` is the only lever**: Harald Geyer's `opt()` is used unmodified,
  so every intervention of ours is expressed as a parameter it already
  understands. Verified by diff against his commit `08819a0`.
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
- **Minimum state of charge** is a **hard floor**, modelled as *missing
  capacity* (`opt()` counts free room up to full, so a smaller capacity cuts
  the bottom off). Capped at 30 %, above which too little usable range is left
  to carry a night. There is no separate blackout reserve — the minimum SOC
  *is* the safety reserve. The inverter's own backup SOC raises it when
  higher, otherwise the device refuses discharges the plan expects. The
  `max_blackout_reserve` route was built and discarded (it is forward-looking
  and releases the floor whenever no deficit lies ahead — deepest planned SOC
  stayed at 30.8 % for every setting); do not retry it. Note that the target
  SOC handed to the inverter comes from the plan; there is no independent
  interlock, the protection lives in the plan alone.
- **Huawei is the only supported inverter for now**: `supports_schedule_control`
  gates writing, and `NUR_HUAWEI_WAEHLBAR` in the panel hides the other five
  from the wizard (already-configured foreign drivers stay visible). The other
  drivers stay in the tree, fully intact, for mergeability with the main
  integration. Status, open points per driver and the three-step release path:
  `docs/wechselrichter-status.md` — that file is the single source of truth,
  keep it in sync when a driver is enabled.
- **Not-Aus** (`GUARD_EMERGENCY_IMPORT_KW` = 1 kW, `GUARD_EMERGENCY_IMPORT_RUNS`
  = 3): without the old grid-import watchdog, discharge would be unsecured if
  the grid sensor misreads or the house load sits permanently above the
  discharge power (buying power to sell it cheaper). Blocks discharge until
  the slot changes.
- **PeakShare is an input, not an actor**: the demand forecast feeds the price
  function; it no longer computes a discharge window. Hourly values suffice —
  `opt()` resamples to 15 min itself (hourly means deviate ≤ 5 %, no time
  shift). Refreshed every 30 min, because when fetched only at startup the
  timestamps age into the past within a day and the surcharge goes silent.
- **Consumption Profile**: Hourly averages from recorder, split by 7 individual weekdays (mo–so), rolling window (default 4 weeks), with weekday fallback chain for missing data.
- **Dual Update Timers**: Slow sensors (profile) every 15min, fast sensors (forecasts, battery, Hausverbrauch) every 1min. Hard-wired since v26 — the former config keys `update_interval_fast_min`/`update_interval_slow_min` are removed by migration.

## Config Flow & Onboarding

The config flow is a single-click setup that creates a config entry with `setup_complete=False`. Full configuration happens through the sidebar panel (`/eeg-optimizer`), which provides:

Wizard steps (`RENDERERS` map in the panel — the step methods are called
dynamically by name, so they look unreferenced to a grep). Steps 1–3 are the
sensors (assigned once), steps 4–5 are the parameters:

1. Willkommen — prerequisite checks (inverter integration installed?)
2. Wechselrichter — type selection, auto-detection / Modbus probe, power
   sensors (PV / battery / grid, directional pairs for Fronius & SMA)
3. Batterie — SOC sensor, capacity (sensor or manual, per-device for Huawei
   Master/Slave)
4. PV-Prognose — forecast source (Solcast / Forecast.Solar) + two mandatory
   forecast sensors; expert mode: day 3–7 sensors
5. Anlage & Batterie — AC power limit, PV peak power (both mandatory, checked
   in the wizard *and* on save), export limit, battery power limit, minimum
   state of charge, maximum state of charge (always visible, no toggle —
   100 = charge to full, the value alone carries the state since v27)
6. Tarife & Gemeinschaft — base tariff (manual or OeMAG; manual also takes a
   night rate `schedule_feedin_price_night`), consumption price, community
   shares/prices/weights. The night window lives in the Vergütung section
   (rendered once via `_nachtfensterFelder`) and appears when ANY night rate
   is set — base tariff or community; second community collapsed behind a
   button; expert mode: battery aging cost
7. Zusammenfassung

Settings live in three tabs: **Tarife** and **Anlage** are exactly the two
parameter wizard steps (same field renderers, `settings_` prefix); **System**
holds the expert-mode switch, a read-only sensor overview (with the
restart-wizard button — sensor mappings are wizard-only by design), telemetry
opt-in, schedule archive, and (expert) balance card + profile lookback. In the
settings, device-datasheet values (AC limit, PV peak, battery power limit) are
expert-only; in the wizard they are always visible.

Dashboard notes: during the executor's startup grace period (status
"Startphase — …") the status card shows only that hint — no setpoints,
reasons, warnings, or job line. The "Gesetzte Steuerwerte" transparency view
is its own always-expanded card below the Optimierungsplan card (expert mode
only, hidden during the startup phase).

Config entry version: 27 (migrations in `__init__.py`)

## Development Notes

- Tests in `tests/` directory, run with `pytest` (asyncio_mode=auto)
- `pyproject.toml` configures pytest
- All UI strings in German (`strings.json`, `translations/de.json`), English fallback (`translations/en.json`)
- HA imports are guarded with try/except for test environment compatibility (stubs provided)
- The plan is computed and displayed in every mode; inverter commands are
  written only in mode "Ein" and only for a driver with
  `supports_schedule_control=True`
- Do **not** modify anything under `chamo/` — it is Harald Geyer's upstream
  code, kept byte-identical so his changes stay mergeable (`tests/
  test_chamo_highs_adapter.py` compares HiGHS against GLPK column by column).
  `chamo/opt_test.py` has a syntax error upstream and is never imported
- Before deleting seemingly unused panel code, check for **dynamic** dispatch:
  `_renderStep0`–`_renderStep6` run through the `RENDERERS` map,
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
