"""Constants for EEG Energy Optimizer integration."""

DOMAIN = "eeg_energy_optimizer"

CONF_INVERTER_TYPE = "inverter_type"
CONF_BATTERY_SOC_SENSOR = "battery_soc_sensor"
CONF_BATTERY_CAPACITY_SENSOR = "battery_capacity_sensor"
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
# Optional: Anlagen-Spitzenleistung in kWp. Wird ins Profile-Payload an
# das Telemetrie-Backend mitgesendet (Sanity-Caps gegen unrealistische
# predicted_pv_kwh-Werte). Wenn nicht gesetzt → null im Payload.
CONF_PV_PEAK_KWP = "pv_peak_kwp"
# AC-Grenzleistung des Wechselrichters in kW. Leer/0 = aus CONF_PV_PEAK_KWP
# abgeleitet. Begrenzt im Fahrplan die Summe aus Einspeisung und Hauslast.
CONF_INVERTER_AC_LIMIT_KW = "inverter_ac_limit_kw"
CONF_PV_POWER_SENSOR = "pv_power_sensor"
CONF_GRID_POWER_SENSOR = "grid_power_sensor"
CONF_BATTERY_POWER_SENSOR = "battery_power_sensor"
# Optional split-sensor pairs — used when an inverter exposes only directional
# (positive-only) sensors instead of one signed sensor (e.g. Fronius).
# When both *_export/*_charge AND *_import/*_discharge are set, the signed
# value is computed as (export − import) / (charge − discharge).
# The single-sensor convention (with INVERTER_SIGN_CONVENTIONS) still
# applies whenever the pair is incomplete.
CONF_GRID_POWER_EXPORT_SENSOR = "grid_power_export_sensor"
CONF_GRID_POWER_IMPORT_SENSOR = "grid_power_import_sensor"
CONF_BATTERY_POWER_CHARGE_SENSOR = "battery_power_charge_sensor"
CONF_BATTERY_POWER_DISCHARGE_SENSOR = "battery_power_discharge_sensor"
CONF_HUAWEI_DEVICE_ID = "huawei_device_id"
# Multi-Inverter (Master/Slave): Liste aller huawei_solar-Batteriegeräte.
# Jedes Gerät hat eine eigene Batterie mit eigenem SOC/Kapazität/Ladelimit
# und eigenen forcible_charge/discharge-Services. CONF_HUAWEI_DEVICE_ID
# bleibt als Legacy-Single-Key bestehen (Fallback für Bestandsanlagen).
CONF_HUAWEI_DEVICE_IDS = "huawei_device_ids"
# Manuelle Einzelkapazität pro Batteriegerät: {device_id: kwh}. Nötig, weil
# huawei_solar bei manchen Anlagen keinen Akkukapazitäts-Sensor mit Wert
# liefert — ohne Einzelkapazitäten kann der kapazitätsgewichtete Combined-SOC
# nicht berechnet werden. Sensorwert (falls vorhanden) hat Vorrang.

INVERTER_TYPE_HUAWEI = "huawei_sun2000"
INVERTER_TYPE_SOLAX = "solax_gen4"
INVERTER_TYPE_SOLAREDGE = "solaredge_storedge"
INVERTER_TYPE_FRONIUS = "fronius_gen24"
INVERTER_TYPE_KOSTAL = "kostal_plenticore"
INVERTER_TYPE_SMA = "sma_smart_energy"

# Sign conventions per inverter type for battery and grid power sensors.
# battery_sign: +1 = positive means charging (Huawei), -1 = positive means discharging (SolaX)
# grid_sign:    +1 = positive means export (Huawei),   -1 = positive means import (SolaX)
# pv_includes_battery: True = PV sensor includes battery discharge power (SolarEdge ac_power)
#   → real PV = pv_raw + battery_raw (before sign normalization)
INVERTER_SIGN_CONVENTIONS = {
    "huawei_sun2000": {"battery_sign": 1, "grid_sign": 1},
    "solax_gen4":     {"battery_sign": -1, "grid_sign": -1},
    "solaredge_storedge": {"battery_sign": 1, "grid_sign": 1, "pv_includes_battery": True},
    # Fronius exposes only directional sensors (charging/discharging,
    # netzeinspeisung/netzbezug) — never a single signed value. The setup
    # therefore creates synthetic combined sensors that are *already canonical*
    # (positive = charging / positive = export). Sign convention = identity.
    "fronius_gen24": {"battery_sign": 1, "grid_sign": 1},
    # Kostal REST sensors (kostal_plenticore): "Battery Power" positive =
    # discharging (matches Modbus register 582), "Grid Power" positive =
    # Bezug/import (matches Modbus register 252) → both inverted to our
    # canonical convention. AM GERÄT VERIFIZIEREN (Beta-Checkliste Punkt 4).
    "kostal_plenticore": {"battery_sign": -1, "grid_sign": -1},
    # SMA (`sma` WebConnect integration) exposes only directional pairs
    # (battery_power_charge_total/…_discharge_total, metering_power_supplied/
    # …_absorbed) — same situation as Fronius: the setup creates synthetic
    # combined sensors that are already canonical. Sign convention = identity.
    "sma_smart_energy": {"battery_sign": 1, "grid_sign": 1},
}

# Huawei EMMA-Energiemanagement: Die Einspeiseleistung des EMMA-Geräts
# (entity_id-Präfix "sensor.emma…", z. B. sensor.emma_einspeiseleistung)
# liefert das Netz-Vorzeichen umgekehrt gegenüber der normalen SUN2000-
# Konvention. Wird ein solcher Sensor bei einem Huawei-Setup als Netz-Sensor
# konfiguriert, dreht resolve_sign das grid_sign um (siehe
# power_readings.resolve_sign). Die EMMA-Batterieleistung folgt der normalen
# Konvention und wird NICHT invertiert.
EMMA_SENSOR_PREFIX = "sensor.emma"

# Entity IDs of the synthetic combined sensors created at setup time when
# the user (or auto-detect) configures pair sensors. Held as constants so
# wizard, backfill, and sensor platform agree on the names.
COMBINED_BATTERY_POWER_SENSOR_ID = "sensor.eeg_energy_optimizer_battery_power"
COMBINED_GRID_POWER_SENSOR_ID = "sensor.eeg_energy_optimizer_grid_power"
# Multi-battery driver-side combined sensors (currently SolarEdge i1+i2+…).
# Pinned so Wizard, Optimizer-Snapshot, and frontend dashboard agree on the
# entity names without depending on HA's slugify rules.
COMBINED_BATTERY_SOC_SENSOR_ID = "sensor.eeg_energy_optimizer_combined_soc"
COMBINED_BATTERY_CAPACITY_SENSOR_ID = "sensor.eeg_energy_optimizer_combined_capacity"

CONF_FRONIUS_MODBUS_HOST = "fronius_modbus_host"
CONF_FRONIUS_MODBUS_PORT = "fronius_modbus_port"
DEFAULT_FRONIUS_MODBUS_PORT = 502

CONF_KOSTAL_MODBUS_HOST = "kostal_modbus_host"
CONF_KOSTAL_MODBUS_PORT = "kostal_modbus_port"
DEFAULT_KOSTAL_MODBUS_PORT = 1502

CONF_SMA_MODBUS_HOST = "sma_modbus_host"
CONF_SMA_MODBUS_PORT = "sma_modbus_port"
DEFAULT_SMA_MODBUS_PORT = 502

CONF_PV_POWER_SENSOR_2 = "pv_power_sensor_2"
# Optionaler zweiter Batterieleistungs-Sensor (Multi-Inverter, z. B. Huawei
# Master/Slave mit je einer Batterie). Wird in Hausverbrauch- und
# Batterieleistung-Sensor zur ersten Batterie addiert, damit der berechnete
# Hausverbrauch (→ Verbrauchsprofil) das Gesamtsystem abbildet.
CONF_BATTERY_POWER_SENSOR_2 = "battery_power_sensor_2"

# Phase 2: Forecast & Consumption
CONF_FORECAST_SOURCE = "forecast_source"
CONF_FORECAST_REMAINING_ENTITY = "forecast_remaining_entity"
CONF_FORECAST_TOMORROW_ENTITY = "forecast_tomorrow_entity"
CONF_LOOKBACK_WEEKS = "lookback_weeks"

CONSUMPTION_SENSOR = "sensor.eeg_energy_optimizer_hausverbrauch"

FORECAST_SOURCE_SOLCAST = "solcast_solar"

DEFAULT_LOOKBACK_WEEKS = 4
# Die Update-Takte sind festverdrahtet (v26 entfernt die alten Konfig-
# Schlüssel): 1 min war ohnehin Minimum und Vorgabe, 15 min genügt für ein
# Profil, das sich nur über Wochen ändert.
DEFAULT_UPDATE_INTERVAL_FAST = 1   # minutes
DEFAULT_UPDATE_INTERVAL_SLOW = 15  # minutes

WEEKDAY_KEYS = ["mo", "di", "mi", "do", "fr", "sa", "so"]

# Batterie-Leistungsgrenze des Fahrplans (battery_power_limit). Historisch
# die Entladeleistung der Zustands-Heuristik — der Schlüssel bleibt gleich
# (gespeicherte Werte werden nie gelöscht), die Bedeutung ist jetzt: maximale
# Lade-/Entladeleistung, mit der der LP-Fahrplan plant.
CONF_DISCHARGE_POWER_KW = "discharge_power_kw"
DEFAULT_DISCHARGE_POWER_KW = 5.0

# Optimizer modes (D-17)
MODE_EIN = "Ein"
# „Test" hieß der nicht steuernde Modus bis 1.5.52 — er rechnete den Plan,
# schrieb aber nichts. Genau das tut jetzt „Aus", und der Name sagt es auch:
# beim Umschalten werden gesetzte Steuerwerte zurückgenommen, das Gerät läuft
# im Automatikmodus. Die Konstante bleibt, damit ein gespeicherter Zustand
# aus einer Vorsession weiter erkannt (und auf „Aus" abgebildet) wird.
MODE_TEST = "Test"
MODE_AUS = "Aus"
OPTIMIZER_MODES = [MODE_EIN, MODE_AUS]

# Startup grace period: delay inverter commands after HA restart
# to let sensors (PV forecast, sun.sun) settle with valid data
STARTUP_GRACE_SECONDS = 90

# ---------------------------------------------------------------------------
# Fahrplan-Executor (schedule_executor.py)
# ---------------------------------------------------------------------------
# Einspeisegrenze für den Fahrplan. Bewusst NICHT der alte Schlüssel
# enable_feedin_limit — der meinte den eigenen Einspeisebegrenzungs-Regler.
# Diese Grenze fließt ins LP-Modell ein und aktiviert Guard 1 (Ladelimit
# anheben, wenn die gemessene Einspeisung am Limit klebt = stille Abregelung).
CONF_GRID_EXPORT_LIMIT_ENABLED = "grid_export_limit_enabled"
CONF_GRID_EXPORT_LIMIT_KW = "grid_export_limit_kw"
DEFAULT_GRID_EXPORT_LIMIT_ENABLED = False
DEFAULT_GRID_EXPORT_LIMIT_KW = 4.0

# Guard 1 — Ladelimit anheben, wenn die Einspeisung am Limit klebt.
GUARD_EXPORT_STICKY_BAND_KW = 0.1   # „klebt am Limit“-Band (±100 W)
# Anhebeschritt: im 30-s-Takt 1 kW/min Aufholrate — klein genug, dass ein
# Überschwinger im nächsten Takt korrigierbar bleibt.
GUARD_CHARGE_STEP_KW = 0.5
GUARD_EXPORT_RELEASE_KW = 0.3       # Rücknahme erst unter Grenze − 0,3 kW
# Rücknahme: Anteil des Abstands zum Fahrplanwert, der je Lauf abgebaut wird.
# Anders als beim Anheben ist das Ziel hier bekannt — es gibt nichts zu
# ertasten. Mit festen 0,5-kW-Schritten brauchte ein Limit, das sich bis ans
# Hardware-Maximum hochgearbeitet hat, über 13 Minuten zurück; ein Slot dauert
# 15. Halbierend sind es rund 7 Läufe (3,5 min), und weil jeder Schritt
# kleiner wird als der vorige, nähert sich das Limit an, statt zu überschwingen
# — ein Sprung direkt auf den Planwert würde den ganzen Überschuss auf einmal
# ins Netz schicken und eine Abregelung auslösen.
GUARD_CHARGE_RELEASE_FACTOR = 0.5

# Guard 2 — Wirkungsgradkorrektur. Sollwert = benötigte Leistung / Wert.
#
# BLEIBT AUF 1.0 — gemessen, nicht geraten (Nacht 24./25.08.2026, 63
# eingeschwungene Abschnitte): der Wechselrichter liefert konstant 59 W
# weniger als befohlen, UNABHÄNGIG von der befohlenen Leistung. Bei 0,86 kW
# fehlen 0,059 kW, bei 1,71 kW fehlen 0,057 kW. Ein Wirkungsgrad würde
# proportional wachsen (bei 1,71 kW wären es 0,12 kW) — er tut es nicht.
# Theil-Sen über alle Abschnitte: Steigung 0,987, also ≈ 1.
#
# Ein fester Abzug lässt sich mit dieser Konstante nicht abbilden: eine
# Division durch 0,93 schlüge bei 5 kW 317 W auf, wo 59 W fehlen. Und
# 59 W liegen unter dem Totband (EXECUTOR_DISCHARGE_DEADBAND_KW = 0,2),
# die Korrektur würde also meist nicht einmal geschrieben.
#
# Offen bleibt: alle Messpunkte lagen zwischen 0,47 und 1,86 kW. Ein
# zusätzlicher Faktor oberhalb 2 kW ist damit nicht ausgeschlossen.
# Details in CHAMO.md, Abschnitt „Gemessen: Entlade-Nachführung".
GUARD_DISCHARGE_EFFICIENCY = 1.0

# Not-Aus für Guard 2: Ohne den alten Grid-Import-Watchdog wäre eine
# Entladung ungesichert, wenn der Netz-Sensor falsch liest oder die Hauslast
# dauerhaft über der Entladeleistung liegt (Strom kaufen um ihn billiger zu
# verkaufen). Netzbezug über 1 kW in drei aufeinanderfolgenden Guard-Läufen
# (~90 s) → Freigabe, Sperre bis zum nächsten Slotwechsel.
GUARD_EMERGENCY_IMPORT_KW = 1.0
GUARD_EMERGENCY_IMPORT_RUNS = 3
# Zweite, zeitliche Sperre nach einem Not-Aus. Der Not-Aus überwacht bewusst
# auch dann, wenn gerade kein Fahrplan vorliegt — dann gibt es aber keinen
# Slot, an den sich die Sperre hängen kann. Eine Slotlänge deckt genau den
# Zeitraum ab, für den die Slot-Sperre gedacht ist.
GUARD_EMERGENCY_BLOCK_MINUTES = 15

# Totbänder: Schreiben nur bei relevanter Änderung — der Guard-Lauf kommt
# alle 30 s, geschrieben werden soll aber nur, was den Wechselrichter
# wirklich bewegen würde (SolarEdge-NVRAM-Lektion aus der Hauptintegration).
EXECUTOR_CHARGE_DEADBAND_KW = 0.2
EXECUTOR_DISCHARGE_DEADBAND_KW = 0.2
EXECUTOR_TARGET_SOC_DEADBAND_PCT = 1.0

# Failsafe: Fahrplan fehlt, ist fehlerhaft oder älter als diese Spanne →
# einmalig async_release(), der Wechselrichter läuft im Automatikmodus weiter.
# Ab diesem Plan-SOC gilt die Batterie als voll. Plant der Fahrplan dann kein
# Laden, ist das keine Blockierabsicht, sondern Platzmangel — wir greifen nicht
# ein und lassen den Standardwert des Wechselrichters stehen.
SCHEDULE_BATTERY_FULL_SOC_PCT = 99.0

SCHEDULE_FAILSAFE_MINUTES = 15

# ------------------------------------------------------------------
# Phase 8: Telemetry (v1.1)
# ------------------------------------------------------------------
# Backend-URL und Bootstrap-Token werden nur im RELEASE-Repo gefüllt.
# Im DEV-Repo bleiben sie leer → TelemetryReporter ist ein No-Op.
# Siehe .planning/milestones/v1.1-phases/08-ha-reporter-modul/08-CONTEXT.md, D-01.
TELEMETRY_BACKEND_URL = "https://telemetry.ew-ansfelden.cc"
# Siehe 08-CONTEXT.md D-01 — Bootstrap-Token gibt Anlagen das Recht, sich am Backend
# einmalig zu registrieren. Pro Anlage wird ein eigener api_key generiert; der hardcoded
# Bootstrap-Token dient nur als IP-Rate-Limit-Schutz, nicht als echte Authentifizierung.
TELEMETRY_BOOTSTRAP_TOKEN = "4c604d119e5e4c08f0a020e3d2aab487bcd05ab62de3fcaf0dd9138185744fa6"

# Storage-Keys (D-04, D-06). Identity und Buffer nutzen GETRENNTE Dateien,
# damit ein korrupter Buffer die Identity nicht zerstören kann.
STORAGE_TELEMETRY = f"{DOMAIN}.telemetry"
STORAGE_TELEMETRY_BUFFER = f"{DOMAIN}.telemetry_buffer"

# Config-Entry-Flag, default False (08-03 ergänzt es via async_migrate_entry v12→v13).
CONF_TELEMETRY_ENABLED = "telemetry_enabled"

# Buffer- und HTTP-Defaults
TELEMETRY_BUFFER_MAX = 100        # D-06: Ringbuffer-Maximum
TELEMETRY_HTTP_TIMEOUT = 10       # D-34: Per-Request-Timeout in Sekunden
TELEMETRY_BACKOFF_MIN_S = 60      # D-36: 1 min initialer Backoff
TELEMETRY_BACKOFF_MAX_S = 1800    # D-36: 30 min Maximum
TELEMETRY_FLUSH_BATCH = 10        # D-35: maximal Events pro erfolgreichem Send-Drain

# Settings-Whitelist für /v1/profile (D-18, D-19). NICHTS außerhalb dieses
# Tupels wird gesendet — entity_ids, IPs, Gerätenamen etc. können nicht leaken.
# Kennung der Steuerungsvariante, gesendet als ``settings.steuerung``. Das
# Backend wertet beide Varianten parallel aus: die produktive Integration mit
# der Zustands-Heuristik sendet den Schluessel NICHT, ihr Fehlen bedeutet dort
# "heuristik". So braucht die bestehende Flotte kein Update, und das Dashboard
# weiss trotzdem, welche Zustands- und Ereignis-Semantik eine Anlage liefert.
TELEMETRY_STEUERUNG = "fahrplan"

TELEMETRY_SETTINGS_KEYS = (
    # Bis 1.5.22 standen hier 15 Schluessel der abgeschafften
    # Zustands-Heuristik (enable_morning_delay, min_soc, enable_slot_*,
    # enable_feedin_limit ...). Sie existierten in der Konfiguration nur noch
    # als eingefrorene Migrationswerte und sagten nichts ueber die Anlage;
    # ihre Nachfolger aus dem Fahrplan fehlten dagegen. Jetzt: nur was
    # tatsaechlich wirkt.
    "steuerung",
    "enable_peakshare",
    "peakshare_community",
    "discharge_power_kw",
    "forecast_source",
    "schedule_min_soc_pct",
    # Gehoert in die Auswertung, weil ein Maximum-Ladestand erklaert, warum
    # eine Anlage nie voll wird. Seit v27 traegt der Wert allein den Zustand
    # (100 = bis voll laden), der fruehere Ein/Aus-Schluessel ist entfallen.
    "schedule_max_soc_pct",
    "grid_export_limit_enabled",
    "grid_export_limit_kw",
    # Die Zielfunktion des Fahrplans. Ohne diese Werte sieht das Backend das
    # Ergebnis einer Optimierung, deren Zielfunktion es nicht kennt — jede
    # Aussage darueber, ob ein Plan sinnvoll war, waere Raten. Tarife und
    # Alterungskosten sind keine personenbezogenen Daten.
    "schedule_feedin_source",
    "schedule_feedin_price",
    "schedule_feedin_price_night",
    "spot_market_area",
    "spot_feedin_fee",
    "schedule_consumption_price",
    "schedule_grid_fee",
    "schedule_battery_cost",
    "schedule_night_start",
    "schedule_night_end",
    # Eigenes Nachtfenster der Gemeinschaften (leer = wie schedule_night_*).
    "peakshare_night_start",
    "peakshare_night_end",
    "schedule_ac_limit_kw",
)
# ``discharge_a_start_time`` steht bewusst nicht mehr drin: der Schluessel
# bleibt in der Konfiguration (Rueckwechsel-Garantie), verschiebt aber nur
# noch eine Trennlinie im Dashboard-Diagramm und steuert nichts.

# Einspeise-Statistik: Ab wann die Abschnittslisten alter Tage gelöscht
# werden. Die Tagessummen bleiben, nur die Einzelabschnitte fallen weg —
# sonst wächst die Speicherdatei unbegrenzt.
STATS_COMPACT_AFTER_DAYS = 90

# Phase 8 — Runtime Watchdog-Schwellen (08-03, D-16)
SENSOR_UNAVAIL_THRESHOLD_S = 600        # Sensor 10 min unverfügbar → Failure
FORECAST_NONE_STREAK_THRESHOLD = 3      # 3 aufeinanderfolgende None-Forecasts → Failure
FAILURE_DEDUP_WINDOW_S = 3600           # 1 h Dedup pro (category, message_hash)
# Dauerzustände (sensor_unavailable, forecast_provider, inverter_unsupported)
# melden sich sonst jede Stunde neu — eine komplett tote Quell-Integration
# erzeugte so ~120 Events/Tag (5 Rollen × 24), und ein nicht gesteuerter
# Treiber meldete rund um die Uhr dasselbe. 6 h Reminder reicht; bei Recovery
# wird der Dedup-Key gelöscht, ein erneuter Ausfall meldet sich also sofort
# wieder. Übernommen aus der produktiven Integration (8c24343).
FAILURE_PERSISTENT_DEDUP_WINDOW_S = 21600

# Snapshot-Telemetrie. Gesammelt wird im Guard-Takt (30 s), abgelegt aber nur
# alle SNAPSHOT_INTERVAL_MIN Minuten und gesendet im Sammelpaket des
# Flush-Timers — 48 Zeilen pro Tag, wie in der produktiven Integration, damit
# die Auswertung über beide Varianten dieselbe Auflösung hat.
TELEMETRY_SNAPSHOT_INTERVAL_MIN = 30
# Herzschlag: ohne regelmäßiges authentifiziertes Ereignis bleibt
# ``installations.last_seen_at`` im Backend auf dem letzten Neustart stehen —
# und der Cron-Job dort löscht Installationen, die 90 Tage nichts gesendet
# haben. Ein tägliches Profil-Update ist idempotent (COALESCE-UPDATE) und
# hält die Anlage sichtbar, auch wenn sie monatelang fehlerfrei läuft.
TELEMETRY_PROFILE_HEARTBEAT_S = 86400
