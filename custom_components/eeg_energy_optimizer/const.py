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
INVERTER_TYPE_SIGENERGY = "sigenergy_sigenstor"

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
    # Sigenergy (HACS-Integration `sigen`, TypQxQ): Anlagen-Sensoren in kW,
    # roh aus den Registern. 30037 ESS-Leistung positiv = laden (wie bei uns,
    # an der Testanlage bestätigt), 30005 Netzleistung positiv = BEZUG →
    # Netz-Vorzeichen gedreht. Am Gerät bei Einspeisung gegenprüfen
    # (binary_sensor …exporting_to_grid muss dann „on" sein).
    "sigenergy_sigenstor": {"battery_sign": 1, "grid_sign": -1},
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

# Über wie viele Stunden der ausgewiesene Optimierungsgewinn gerechnet wird.
# Der Fahrplan selbst schaut weiter voraus (er braucht die zweite Nacht, um
# heute richtig zu entscheiden) — der Gewinn wird aber nur über den Teil
# ausgewiesen, den die Prognosen tragen: je weiter hinten ein Slot liegt,
# desto mehr ist sein Geldwert Prognose und nicht Plan. Beide Seiten des
# Vergleichs werden über dasselbe Fenster bewertet.
GEWINN_HORIZONT_H = 24.0

# ---------------------------------------------------------------------------
# Heizstab (heizstab/) — ein Fronius Ohmpilot als steuerbare Senke für
# PV-Überschuss, den weder Batterie noch Netz aufnehmen. Direkt per Modbus TCP
# gesteuert; Voraussetzung ist ein vom Gen24 ENTKOPPELTER Ohmpilot, sonst
# schreiben zwei Master auf dasselbe Register.
# ---------------------------------------------------------------------------
CONF_HEIZSTAB_ENABLED = "heizstab_enabled"
CONF_HEIZSTAB_HOST = "heizstab_host"
CONF_HEIZSTAB_PORT = "heizstab_port"
# Maximale Leistung des Heizstabs in kW (Ohmpilot: 3 kW einphasig, 6 bzw.
# 9 kW dreiphasig — der Wert steht auf dem Heizstab, nicht am Ohmpilot).
CONF_HEIZSTAB_MAX_KW = "heizstab_max_kw"
# Maximaltemperatur: bis hierher darf der Heizstab heizen (Hysterese darunter).
CONF_HEIZSTAB_MAXTEMP_C = "heizstab_maxtemp_c"
# Mindesttemperatur: darunter hat der Heizstab Vorrang vor der Einspeisung —
# er nimmt allen PV-Überschuss, auch den unterhalb der Einspeisegrenze, aber
# weder Netz- noch Batteriestrom. 0 = keine Mindesttemperatur.
CONF_HEIZSTAB_MINTEMP_C = "heizstab_mintemp_c"
# Unter der Mindesttemperatur auch aus dem Netz heizen (volle Leistung, eine
# geplante Entladung ins Netz wird derweil unterdrückt). Opt-in — wer keinen
# Netzstrom verheizen will, lässt es aus; die Mindesttemperatur wirkt dann
# nur als Vorrang vor der Einspeisung.
CONF_HEIZSTAB_NETZBEZUG = "heizstab_netzbezug"
# Vorrang bei ungeplantem Überschuss (Einspeisung klebt an der Grenze):
# True = zuerst der Heizstab, das Ladelimit der Batterie wird erst angehoben,
# wenn er gesättigt ist; False = zuerst die Batterie (Guard 1 wie bisher),
# der Heizstab bekommt, was sie nicht mehr aufnimmt.
# Entfallen mit 2.1.1-dev5: Der Überschuss wird immer geteilt, gewichtet
# nach Ladestand (HEIZSTAB_TEILUNG_*). Der Schlüssel bleibt nur stehen,
# damit gespeicherte Konfigurationen nicht stolpern — gelesen wird er
# nicht mehr.
CONF_HEIZSTAB_VORRANG = "heizstab_vorrang"
# Was eine Kilowattstunde Wärme wert ist (EUR/kWh) — der Preis der Energie,
# die sie ersetzt (Gas, Wärmepumpe, Strom). 0 = Wärme bleibt unbewertet.
CONF_HEIZSTAB_WAERMEWERT = "heizstab_waermewert"
DEFAULT_HEIZSTAB_ENABLED = False
DEFAULT_HEIZSTAB_PORT = 502
DEFAULT_HEIZSTAB_MAX_KW = 6.0
DEFAULT_HEIZSTAB_MAXTEMP_C = 80.0
DEFAULT_HEIZSTAB_MINTEMP_C = 0.0
DEFAULT_HEIZSTAB_NETZBEZUG = False
DEFAULT_HEIZSTAB_WAERMEWERT = 0.0
# Entität, die den Heizstab sperrt, solange sie „ein" meldet — gedacht für
# eine zweite Wärmequelle (Holzvergaser, Kessel), die den Puffer selbst
# heizt. Leer = keine Sperre. Bewusst ein einziges Kriterium ohne
# Temperatur- oder PV-Bedingung: Der Schalter sagt alles.
CONF_HEIZSTAB_SPERR_ENTITY = "heizstab_sperr_entity"
# Volumen des Puffers in Litern — die einzige neue Angabe, aus der sich
# berechnet, wie viel Wärme er noch aufnehmen kann. Gemeint ist der Teil,
# den der Heizstab tatsächlich erwärmt (bei Schichtspeichern oft weniger
# als das Typenschild sagt). 0 = unbekannt, dann plant der Fahrplan den
# Heizstab nicht ein (er bekommt weiterhin, was abgeregelt wird).
CONF_HEIZSTAB_PUFFER_LITER = "heizstab_puffer_liter"
DEFAULT_HEIZSTAB_PUFFER_LITER = 0.0

# Spezifische Wärmekapazität von Wasser in Gebrauchseinheiten: 1,163 Wh
# erwärmen einen Liter um ein Kelvin (4,182 kJ/(kg·K) ÷ 3,6 kJ/Wh).
WASSER_WH_PRO_LITER_KELVIN = 1.163

# Nachführung: klebt die Einspeisung an der Grenze, ist die wahre Höhe des
# Überschusses unsichtbar (der Wechselrichter regelt bereits ab) — deshalb
# in Schritten nach oben; nach unten ist die Lücke messbar und wird in einem
# Lauf geschlossen. Dieselben Bänder wie Guard 1 (GUARD_EXPORT_*).
HEIZSTAB_STEP_KW = 0.5
# Watchdog des Ohmpilot: 50 s ohne Sollwert → Heizstab aus. Geschrieben
# wird deshalb alle 30 s, unabhängig davon, ob sich der Wert geändert hat.
HEIZSTAB_WRITE_INTERVAL_S = 30
HEIZSTAB_READ_INTERVAL_S = 10
HEIZSTAB_TIMESYNC_INTERVAL_H = 6
# Maximaltemperatur: gesperrt ab Maximum, frei erst wieder unter Maximum − Hysterese.
HEIZSTAB_TEMP_HYSTERESE_K = 3.0
# Mindesttemperatur: Komfortheizen ab unter Minimum, Ende bei Minimum + Hysterese.
HEIZSTAB_MINTEMP_HYSTERESE_K = 5.0
# Komfort ohne Netzbezug: der Heizstab regelt auf „Einspeisung ≈ 0" — mit
# dieser Marke als Grenze bleibt zwischen 0 und 0,2 kW Einspeisung ein totes
# Band, und ein dauerhafter kleiner Netzbezug ist ausgeschlossen.
HEIZSTAB_KOMFORT_EXPORT_ZIEL_KW = 0.3
# Fahrplan-Betrieb: Sieht der laufende Slot Wärme vor, regelt der Heizstab
# nicht auf die Einspeisegrenze, sondern ebenfalls auf „Einspeisung ≈ 0" —
# gedeckelt auf die geplante Leistung. Das LP hat die Kilowattstunde der
# Wärme zugeschlagen, weil sie mehr bringt als die Einspeisung; die Messung
# entscheidet nur noch, ob sie auch wirklich da ist. Dieselbe Marke wie beim
# Komfortheizen: darunter bliebe ein dauerhafter kleiner Netzbezug möglich.
HEIZSTAB_PLAN_EXPORT_ZIEL_KW = 0.3
# Ab diesem Abstand zum Maximum gilt der Heizstab als gesättigt.
HEIZSTAB_SATT_TOLERANZ_KW = 0.05
# Fremdsteuerung: Zieht der Heizstab dauerhaft mehr, als vorgegeben ist,
# schreibt jemand anderes auf dasselbe Modbus-Register — an einer Anlage war
# es die Vorgänger-Integration, deren Keepalive den Sollwert im Sekundentakt
# überschrieb, während unsere 0 alle 30 s kurz durchschlug. Toleranz gegen
# Regelrauschen und die Trägheit beim Herunterfahren, Dauer gegen den
# normalen Rampenvorgang: Der Ohmpilot ist in weit unter einer Minute unten.
HEIZSTAB_KONFLIKT_TOLERANZ_KW = 0.3
HEIZSTAB_KONFLIKT_MINUTEN = 3.0

# Aufteilung des Überschusses ohne Heizstab-Vorrang. Nicht „erst die Batterie,
# dann der Heizstab", sondern beide gleichzeitig — mit einem Anteil, der vom
# Ladestand abhängt: Eine fast leere Batterie hat Vorrang, denn ihre Energie
# trägt durch die Nacht, die Wärme nicht. Ab HEIZSTAB_TEILUNG_SOC_VOLL_PCT
# teilen sich beide den Überschuss hälftig.
# Unterhalb der Mindesttemperatur gilt nichts davon: Dann hat der Heizstab
# Vorrang, mit voller Leistung (siehe HeizstabController.komfort_aktiv).
HEIZSTAB_TEILUNG_SOC_LEER_PCT = 20.0
HEIZSTAB_TEILUNG_SOC_VOLL_PCT = 50.0
HEIZSTAB_TEILUNG_MAX_ANTEIL = 0.5

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
    # Prognose (PeakShare) oder feste Abnahmequote — erklaert, warum ein Plan
    # keinen Bedarfsverlauf kennt.
    "eeg_demand_source",
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
    "spot_feedin_fee_pct",
    "awattar_sunny_vertrag",
    "schedule_consumption_price",
    # Zweiter Bezugspreis samt Fenster (leer = ein Preis rund um die Uhr).
    "schedule_consumption_price_night",
    "schedule_consumption_price_snap",
    "schedule_consumption_night_start",
    "schedule_consumption_night_end",
    "schedule_grid_fee",
    "schedule_battery_cost",
    "schedule_night_start",
    "schedule_night_end",
    # Eigenes Nachtfenster der Gemeinschaften (leer = wie schedule_night_*).
    "peakshare_night_start",
    "peakshare_night_end",
    "schedule_ac_limit_kw",
    # Heizstab als Senke — erklaert, warum ein Plan abgeregelte Energie nutzt.
    # Bewusst OHNE Host und Port.
    "heizstab_enabled",
    "heizstab_max_kw",
    "heizstab_maxtemp_c",
    "heizstab_mintemp_c",
    "heizstab_netzbezug",
    "heizstab_waermewert",
    # Puffergroesse erklaert, warum ein Plan Waerme einplant. Die
    # Sperr-Entitaet bleibt draussen, sie ist anlagenspezifisch.
    "heizstab_puffer_liter",
    # Wallbox als zweiter Speicher — erklaert spaeter, warum ein Plan mit
    # dem Auto rechnet. Bewusst OHNE Adresse.
    "wallbox_type",
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

# ---------------------------------------------------------------------------
# Ambibox (ambibox/) — bidirektionale DC-Wallbox mit eigenem
# Energiemanagement (sidOS), gelesen über Modbus TCP. Schritt 1 zeigt nur
# an, welches Fahrzeug angesteckt ist und wie es dasteht; Laden und
# Entladen folgen in einem zweiten Schritt.
# ---------------------------------------------------------------------------
# Typ der Wallbox — wie CONF_INVERTER_TYPE die Auswahl des Treibers, damit
# weitere Fabrikate dazukommen können, ohne dass jede für sich einen eigenen
# Ein/Aus-Schalter mitbringt. Leer heißt: keine Wallbox angebunden.
CONF_WALLBOX_TYPE = "wallbox_type"
WALLBOX_TYPE_NONE = ""
WALLBOX_TYPE_AMBIBOX = "ambibox"
WALLBOX_TYPES = (WALLBOX_TYPE_AMBIBOX,)

CONF_AMBIBOX_HOST = "ambibox_host"
CONF_AMBIBOX_PORT = "ambibox_port"
# Modbus-Unit-ID der Ambibox. Das Herstellerdokument nennt keine — 1 ist der
# Wert, mit dem fremde Umsetzungen arbeiten, und bleibt einstellbar.
CONF_AMBIBOX_UNIT_ID = "ambibox_unit_id"
# sidOS führt bis zu zehn Ladepunkte; der erste ist die Regel.
CONF_AMBIBOX_CONNECTOR = "ambibox_connector"
DEFAULT_WALLBOX_TYPE = WALLBOX_TYPE_NONE
DEFAULT_AMBIBOX_PORT = 502
DEFAULT_AMBIBOX_UNIT_ID = 1
DEFAULT_AMBIBOX_CONNECTOR = 1

# Lesetakt. Ein Zug über 104 Register je Lauf; enger getaktet gäbe es nur
# mehr Verkehr, denn Ladestand und Ladeleistung ändern sich in Minuten,
# nicht in Sekunden.
AMBIBOX_READ_INTERVAL_S = 15

# Manueller Test der Wallbox (Laden/Entladen von Hand starten). Der Fahrplan
# steuert die Wallbox nicht — dieser Weg existiert, um am Gerät die Fragen zu
# klären, die das Herstellerdokument offenlässt.
#
# Vorzeichen des Leistungssollwerts: nirgends dokumentiert. Die einzige fremde
# Umsetzung lädt mit negativen Werten; genau das ist hier die Vorgabe, aber
# umstellbar — zeigt der Test, dass die Box es andersherum meint, kostet das
# eine Einstellung statt eines neuen Releases.
CONF_AMBIBOX_CHARGE_SIGN = "ambibox_charge_sign"
AMBIBOX_SIGN_NEGATIVE = "negative"   # negativer Sollwert = laden
AMBIBOX_SIGN_POSITIVE = "positive"   # positiver Sollwert = laden
DEFAULT_AMBIBOX_CHARGE_SIGN = AMBIBOX_SIGN_NEGATIVE

# Der Sollwert wird zyklisch nachgeschrieben, solange der manuelle Test läuft.
# Wie lange ein Wert ohne Nachschreiben gilt, steht nicht im Dokument — 30 s
# ist eng genug für jeden üblichen Watchdog (der Ohmpilot etwa fällt nach 50 s
# ab) und immer noch wenig Verkehr.
AMBIBOX_KEEPALIVE_S = 30
# Harte Obergrenze für einen manuellen Lauf. Ein Testknopf darf nichts
# hinterlassen, das unbeaufsichtigt weiterläuft: Nach Ablauf wird gestoppt,
# auch wenn niemand mehr hinsieht.
AMBIBOX_MANUAL_MAX_MINUTES = 60
DEFAULT_AMBIBOX_MANUAL_MINUTES = 15
