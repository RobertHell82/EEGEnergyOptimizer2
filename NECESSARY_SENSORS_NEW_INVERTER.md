# Neuer Wechselrichter-Treiber: was er braucht

Ein Treiber ist eine Unterklasse von `InverterBase`
(`custom_components/eeg_energy_optimizer/inverter/base.py`). Er **schreibt**
nur; gelesen wird über Sensoren, die der Nutzer im Assistenten zuordnet. Der
einzige Aufrufer der Schreibmethoden ist der `ScheduleExecutor`
(`schedule_executor.py`, alle 30 s). Vorlagen: `fronius.py`, `kostal.py`,
`sma.py` (direkt per Modbus TCP), `huawei.py`, `solax.py`, `sigenergy.py`
(über die HA-Integration des Herstellers).

Freigabe und Feldtest-Checkliste: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md),
Abschnitt „Einen Wechselrichter freigeben“.

## 1. Sensoren

Der Optimizer liest nur über Config-Keys, nie über fest verdrahtete
Entity-IDs.

| Config-Key | Inhalt | Pflicht |
|---|---|---|
| `pv_power_sensor` | PV-Leistung, immer ≥ 0 | ja |
| `pv_power_sensor_2` | zweiter PV-Eingang (z. B. Generator-WR), wird addiert | nein |
| `battery_power_sensor` | Batterieleistung, vorzeichenbehaftet | ja (oder Paar) |
| `battery_power_sensor_2` | zweite Batterie (Multi-Inverter), wird addiert | nein |
| `grid_power_sensor` | Netzleistung, vorzeichenbehaftet | ja (oder Paar) |
| `battery_soc_sensor` | Ladestand in % | ja |
| `battery_capacity_sensor` | Kapazität; ersatzweise `battery_capacity_kwh` von Hand | eins von beiden |

**Einheiten:** `power_readings.read_power_kw` rechnet nach
`unit_of_measurement` um (W, kW, MW …); ohne Einheit gilt kW. Die Kapazität
gilt als Wh bei Einheit `Wh` oder ohne Einheit über 1000
(`resolve_battery_capacity_kwh`).

**Vorzeichen:** intern Batterie + = Laden, Netz + = Einspeisung. Weicht der
Sensor ab, trägt der Typ es in `INVERTER_SIGN_CONVENTIONS` (`const.py`) ein
(`battery_sign` / `grid_sign` = ±1). Angewandt wird das nur in
`power_readings.resolve_sign`. Am Gerät prüfen: Kostal und Sigenergy drehen
das Netz-Vorzeichen, SolaX beide.

**Richtungspaare:** Liefert die Quelle nur positive Teilwerte (Fronius, SMA),
werden `battery_power_charge_sensor` / `battery_power_discharge_sensor` und
`grid_power_export_sensor` / `grid_power_import_sensor` zugeordnet. Daraus
entstehen die synthetischen Sensoren `sensor.eeg_energy_optimizer_battery_power`
und `sensor.eeg_energy_optimizer_grid_power` (`sensor.py`,
`BatteryPowerCombinedSensor` / `GridPowerCombinedSensor`); `save_config`
setzt `battery_power_sensor` / `grid_power_sensor` dann auf diese. Ihr
Vorzeichen ist schon kanonisch, die Konvention für den Typ ist `+1/+1`. Im
Panel verlangt die Prüfung des Wechselrichter-Schritts für Fronius und SMA
die Paare statt der Einzelsensoren — ein neuer Typ mit Paaren muss dort
ergänzt werden.

**Mehrere Batterien:** `get_combined_battery_state()` liefert einen
kapazitätsgewichteten SOC und die Summenkapazität,
`has_combined_battery_state` (strukturell, aus der Konfiguration, nicht aus
Werten) legt die Sensoren `…_combined_soc` / `…_combined_capacity` an. Bisher
nur Huawei Master/Slave.

**Aktualität:** Der Netzzähler zählt nur, solange sein Zustand höchstens
`MESSWERT_MAX_ALTER_S` (300 s) alt ist (`power_readings._live_kw`). Eine
Quelle, die nur bei Änderung schreibt oder lange pollt, wirkt sonst wie ein
fehlender Messwert.

## 2. Schreibpfad (abstrakt, jeder Treiber)

| Methode | Bedeutung |
|---|---|
| `async_set_charge_limit(power_kw) → bool` | Ladeleistung der Batterie begrenzen; `0` = Laden gesperrt. Der Rest des Überschusses geht ins Netz. |
| `async_set_discharge(power_kw, target_soc) → bool` | Erzwungene Entladung. `power_kw` ist Batterieleistung, außer bei `discharge_is_grid_setpoint` (dann Netz-Export). `target_soc` = Slot-End-SOC aus dem Plan; ohne Register dafür (Kostal, SMA) nur angenommen — der Plan rechnet jede Minute mit dem gemessenen SOC neu. |
| `async_stop_forcible() → bool` | Alles zurück in den Automatikmodus des Geräts — auch das Ladelimit. Wird bei Modus Aus, Failsafe, Not-Aus, Guard 3 und beim Start aufgerufen. |
| `is_available` (Property) | Steuerweg grundsätzlich da? HA-Treiber: Config-Entry der Quell-Integration `loaded`; Modbus-Treiber: Host konfiguriert (verbunden wird erst beim Schreiben). |

**Fehler:** Ausnahmen im Treiber fangen und `return self._fehler("kurzer
Grund")` liefern, bei Erfolg `return self._erfolg()`. Der Grund landet in
`last_write_error` und von dort in der Telemetrie (als `message_hash`) —
deshalb ohne veränderliche Zahlen. Vor jedem Schreiben
`if self._nicht_endlich(power_kw, target_soc): return False`.

`True` heißt nur „quittiert“, nicht „wirkt“: Guard 3 vergleicht den Befehl
mit der gemessenen Batterieleistung (bzw. dem Netzzähler) und setzt den
Modus neu auf, wenn er nicht ankommt.

## 3. Fahrplan-Steuerung (optional, aber nötig zum Steuern)

Ohne `supports_schedule_control = True` rechnet und zeigt der Fahrplan nur
(„Nur Anzeige“). Wer `True` liefert, sollte die Lesemethoden mitliefern.

| Methode / Property | Default | Wofür |
|---|---|---|
| `supports_schedule_control` | `False` | Hauptschalter des Executors |
| `async_get_charge_limit_kw()` | `None` | Guard 1 rechnet vom tatsächlich gesetzten Limit weiter (gemessene PV ist bei Abregelung schon beschnitten); `None` → zuletzt geschriebener Wert, dann Planwert |
| `get_charge_limit_max_kw()` | `None` | Obergrenze für Guard 1; `None` → ungeklemmt |
| `get_max_discharge_power_kw()` | `None` | Obergrenze für Guard 2; `None` → `discharge_power_kw` aus der Konfiguration |
| `get_backup_reserve_soc_pct()` | `None` | Notstrom-SOC des Geräts; hebt den Mindest-Ladestand im Plan, sonst plant er Entladungen, die das Gerät verweigert |
| `discharge_is_grid_setpoint` | `False` | `True`, wenn das Gerät „speise X kW ein“ statt „entlade mit X kW“ nimmt (SMA, `GridWSpt`): Guard 2 gibt dann den Export vor statt Export + Hauslast − PV, Guard 3 misst am Netzzähler |
| `supports_grid_charge` / `async_set_grid_charge(kw, target_soc)` | `False` / Fehler | Beta „Batterie aus dem Netz laden“: PV zuerst, Rest aus dem Netz, bis `target_soc`; beendet mit `async_stop_forcible()`. Derzeit nur Huawei (`forcible_charge_soc`) |
| `get_control_entities()` | `[]` | Stellgrößen als HA-Entitäten (`label`, `entity_id`, `role`) für `get_control_state` |
| `async_get_control_values()` | `[]` | dasselbe für Treiber ohne Entitäten (Modbus): `label`, `value`, `unit`, `role` |
| `get_combined_battery_state()` / `has_combined_battery_state` | `(None, None)` / `False` | siehe Mehrere Batterien |
| `async_disconnect()` | — (nicht in der Basis) | wird beim Entladen der Integration aufgerufen, falls vorhanden: Keepalive stoppen, Verbindung schließen |

## 4. Muster der Modbus-TCP-Treiber (Fronius, Kostal, SMA)

- `pymodbus` `AsyncModbusTcpClient`, Verbindung erst beim Schreiben
  (`_ensure_connected`). `_slave_kw()` deckt die Umbenennung
  `slave` → `device_id` ab pymodbus 3.9 ab.
- Ein `asyncio.Lock` serialisiert Executor, WebSocket-Befehle und Keepalive.
- **Watchdog + Keepalive:** Das Gerät fällt nach einer Frist ohne Schreiben
  selbst in den Automatikmodus zurück (Fronius `RvrtTms` 300 s, SMA 300 s,
  Kostal eigener Watchdog). Weil der Executor wegen der Totbänder oft nichts
  schreibt, schreibt ein eigener Task den aktiven Befehl alle 60 s neu.
  `async_stop_forcible()` bricht den Keepalive zuerst ab, sonst überschreibt
  er die Freigabe.
- Nur flüchtige Steuerregister schreiben. Persistente Parameter (Flash)
  gehören nicht in den 30-s-Takt (siehe Kopf von `sma.py`).
- Host und Port als Config-Keys (`<typ>_modbus_host` / `_port` mit
  `DEFAULT_<TYP>_MODBUS_PORT` in `const.py`), geprüft in `ws_save_config`.

## 5. Einbinden: alle Stellen

1. `inverter/<treiber>.py` und Eintrag in `INVERTER_TYPES`
   (`inverter/__init__.py`, Factory `create_inverter`).
2. `const.py`: `INVERTER_TYPE_<TYP>`, Eintrag in
   `INVERTER_SIGN_CONVENTIONS`, bei Modbus Host/Port-Keys und Default-Port.
3. `manifest.json`: Quell-Integration der Sensoren in `after_dependencies`.
4. `websocket_api.py`:
   - `ws_check_prerequisites`: Domain in `check_domains`
   - `ws_detect_sensors`: Auto-Erkennung der Sensoren (optional, aber
     bequem)
   - `ws_save_config`: Host/Port-Prüfung (Modbus)
   - bei Modbus ein `probe_<treiber>` (Lese-Test im Assistenten) mit
     `@websocket_api.require_admin` als oberstem Dekorator, angemeldet in
     `async_register_websocket_commands`, eingestuft in
     `tests/test_websocket_admin.py` (der Test schlägt sonst fehl)
5. Panel `frontend/eeg-optimizer-panel.js`: `INVERTER_LABELS`,
   `SCHEDULE_CONTROL_INVERTERS`, die Karte in `_renderStepWechselrichter`
   (samt Host-/Port-Dialog und Probe-Aufruf bei Modbus), bei Richtungspaaren
   die Prüfung im Wechselrichter-Schritt, Anzeige des Endpunkts in der
   Sensor-Übersicht.
6. Tests: `tests/test_<treiber>_inverter.py` nach dem Vorbild der
   bestehenden, dazu `tests/test_inverter_factory.py`.
7. Doku: wie in `docs/DEVELOPMENT.md` beschrieben
   (`docs/wechselrichter-status.md` und die Gerätelisten).

## Referenz: Huawei (HA-Dienste)

| Methode | Umsetzung |
|---|---|
| `async_set_charge_limit` | `number.set_value` auf die Ladeleistungs-Entität jedes Batteriegeräts (aus der Entity-Registry, unabhängig von DE/EN-Namen) |
| `async_set_discharge` | `huawei_solar.forcible_discharge_soc` je `device_id`, Leistung anteilig nach nutzbarer Energie (`inverter/_distribution.py`) |
| `async_stop_forcible` | je Gerät erst `huawei_solar.stop_forcible_charge`, dann die Ladeleistung auf das Maximum zurück (getrennte `try`, ein abgelehntes Ladelimit darf keine Entladung weiterlaufen lassen) |
| `async_set_grid_charge` | `huawei_solar.forcible_charge_soc` |
| `is_available` | Config-Entry von `huawei_solar` geladen |
