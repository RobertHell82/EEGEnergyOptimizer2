/**
 * EEG Energy Optimizer Panel - Custom element for HA sidebar panel.
 *
 * Provides dashboard/wizard view toggle and loads config via WebSocket.
 * Wizard: 8-step setup for inverter, prerequisites, sensors, forecasts,
 * consumption, optimizer params, and summary with config save.
 */

// Guard against duplicate script loading (HA may reload after reconnect)
if (customElements.get("eeg-optimizer-panel")) {
  // Already registered — skip entire script to avoid const redeclaration errors
} else {

// Panel-Key -> unique_id-Suffix aus sensor.py. Die echten entity_ids kommen
// aus der Entity-Registry (eeg_optimizer/get_entity_ids) — sie lassen sich NICHT
// aus dem Suffix ableiten, weil HA die entity_id aus dem Anzeigenamen bildet
// (Statussensor: unique_id ..._entscheidung, entity_id ..._fahrplan_status auf
// frischen Installationen). Die Namen unten dienen nur noch als Notfall-Fallback.
const SENSOR_SUFFIXES = {
  entscheidung: "entscheidung",
  pv_heute: "pv_prognose_heute",
  pv_morgen: "pv_prognose_morgen",
  verbrauchsprofil: "verbrauchsprofil",
  prognose_heute: "tagesverbrauchsprognose_heute",
  prognose_morgen: "tagesverbrauchsprognose_morgen",
  prognose_tag2: "tagesverbrauchsprognose_tag_2",
  prognose_tag3: "tagesverbrauchsprognose_tag_3",
  prognose_tag4: "tagesverbrauchsprognose_tag_4",
  prognose_tag5: "tagesverbrauchsprognose_tag_5",
  prognose_tag6: "tagesverbrauchsprognose_tag_6",
  // Momentanwerte und Fahrplan-Sollwerte — Quelle des Ist-Verlaufs im
  // Optimierungsplan. Die entity_id bildet HA aus dem Anzeigenamen, sie muss
  // deshalb ueber die Registry aufgeloest werden (siehe _resolveEntityIds).
  hausverbrauch: "hausverbrauch",
  pv_leistung: "pv_leistung",
  netzleistung: "netzleistung",
  batterieleistung: "batterieleistung",
  fahrplan_batterieleistung: "fahrplan_batterieleistung",
  fahrplan_netzleistung: "fahrplan_netzleistung",
};
const SELECT_SUFFIX = "optimizer";

// Nur der Startwert, bis _resolveEntityIds die Registry gelesen hat. Der
// Statussensor steht in beiden Schreibweisen drin — Bestandsinstallationen
// haben ..._entscheidung, frische ..._fahrplan_status.
const DEFAULT_WATCHED = [
  "select.eeg_energy_optimizer_optimizer",
  "sensor.eeg_energy_optimizer_entscheidung",
  "sensor.eeg_energy_optimizer_fahrplan_status",
  "sensor.eeg_energy_optimizer_pv_prognose_heute",
  "sensor.eeg_energy_optimizer_pv_prognose_morgen",
  "sensor.eeg_energy_optimizer_verbrauchsprofil",
  "sensor.eeg_energy_optimizer_tagesverbrauchsprognose_heute",
  "sensor.eeg_energy_optimizer_tagesverbrauchsprognose_morgen",
  "sensor.eeg_energy_optimizer_tagesverbrauchsprognose_tag_2",
  "sensor.eeg_energy_optimizer_tagesverbrauchsprognose_tag_3",
  "sensor.eeg_energy_optimizer_tagesverbrauchsprognose_tag_4",
  "sensor.eeg_energy_optimizer_tagesverbrauchsprognose_tag_5",
  "sensor.eeg_energy_optimizer_tagesverbrauchsprognose_tag_6",
];

// _v2: die Schrittfolge hat sich geändert (Sensoren vorn, Parameter hinten) —
// ein gemerkter Zwischenstand aus der alten Folge würde im falschen Schritt
// landen. Der alte Schlüssel verfällt einfach (24-h-Ablauf ohnehin).
const WIZARD_KEY = "eeg_optimizer_wizard_state_v2";

// Schritte 1–3 sind die Sensoren (einmalig), 4–5 die Parameter. Die beiden
// Parameter-Schritte sind inhaltlich identisch mit den Einstellungs-Tabs
// „Anlage & Batterie" und „Tarife & Gemeinschaft" (gleiche Feld-Renderer).
const WIZARD_STEPS = [
  "Willkommen",
  "Wechselrichter",
  "Batterie",
  "PV-Prognose",
  "Anlage & Batterie",
  "Tarife & Gemeinschaft",
  "Zusammenfassung",
];

// Anzeigenamen der Wechselrichter-Typen (Zusammenfassung, Sensor-Übersicht).
const INVERTER_LABELS = {
  huawei_sun2000: "Huawei SUN2000",
  solax_gen4: "SolaX Gen4+",
  solaredge_storedge: "SolarEdge StorEdge",
  fronius_gen24: "Fronius Gen24",
  kostal_plenticore: "Kostal Plenticore",
  sma_smart_energy: "SMA Smart Energy",
};

// Vom Fahrplan gesteuerte Wechselrichter — alle anderen rechnen und zeigen
// an ("nur Anzeige").
const SCHEDULE_CONTROL_INVERTERS = ["fronius_gen24", "huawei_sun2000"];

// Summe der Aufteilungsschluessel — es zaehlt nur, was auch eine gewaehlte
// Gemeinschaft hat. Der Prozentsatz bleibt im Formular stehen, wenn der
// Nutzer die Gemeinschaft wieder abwaehlt (Vorgabe 50 %); blind mitgezaehlt
// riss er eine 100-%-Grenze, die es gar nicht gibt. Das Backend sieht es
// genauso: gemeinschaften_aus_config verwirft jeden Eintrag ohne Namen,
// unabhaengig vom Anteil.
const anteilssummePct = (d) =>
  (d?.peakshare_community ? Number(d.peakshare_share_pct ?? 0) : 0)
  + (d?.peakshare_community_2 ? Number(d.peakshare_share_pct_2 ?? 0) : 0);


// Kostal ist im Wizard wieder in allen Builds auswählbar (seit 1.3.13-dev).
// Der Flag bleibt als Schalter erhalten, falls ein Treiber künftig erneut
// vorübergehend aus Release-Builds ausgeblendet werden soll (DEV-Erkennung
// über den Cache-Buster der Script-URL, siehe Git-Historie zu 1.3.11).
const KOSTAL_UI_ENABLED = true;
// Auswaehlbar ist genau, was der Fahrplan auch steuern kann (Entscheid
// 28.08.2026): alles andere waere reine Anzeige. Die uebrigen Karten sind
// nur AUSGEBLENDET, ihr Code bleibt vollstaendig — ein Eintrag in
// SCHEDULE_CONTROL_INVERTERS holt sie unveraendert zurueck. Ein bereits
// konfigurierter Fremdtreiber bleibt sichtbar, damit eine bestehende
// Anlage im Wizard nicht ploetzlich ohne Auswahl dasteht.
const istWaehlbarerWr = (key) => SCHEDULE_CONTROL_INVERTERS.includes(key);


const WIZARD_DEFAULTS = {
  inverter_type: "huawei_sun2000",
  battery_soc_sensor: "",
  battery_capacity_sensor: "",
  battery_capacity_kwh: 10,
  pv_peak_kwp: "",
  pv_power_sensor: "",
  battery_power_sensor: "",
  grid_power_sensor: "",
  // Fronius / SolarNet split-sensor pairs. When both pair fields are filled
  // the backend redirects battery_power_sensor / grid_power_sensor at the
  // synthetic combined sensor on save.
  battery_power_charge_sensor: "",
  battery_power_discharge_sensor: "",
  grid_power_export_sensor: "",
  grid_power_import_sensor: "",
  huawei_device_id: "",
  // Huawei Master/Slave: alle Batteriegeräte. Bei ≥2 Geräten steuert der
  // Treiber alle Batterien und liefert einen kapazitätsgewichteten Combined-SOC.
  huawei_device_ids: [],
  // Einzelkapazität pro Batteriegerät {device_id: kwh} — für gewichteten
  // Combined-SOC + korrekte Gesamtkapazität bei Master/Slave.
  huawei_battery_capacities: {},
  pv_power_sensor_2: "",
  // Zweite Batterieleistung (Multi-Inverter, z. B. Huawei Master/Slave).
  battery_power_sensor_2: "",
  solax_remotecontrol_power_control: "",
  solax_remotecontrol_active_power: "",
  solax_remotecontrol_autorepeat_duration: "",
  solax_remotecontrol_duration: "",
  solax_remotecontrol_trigger: "",
  solax_selfuse_discharge_min_soc: "",
  solaredge_storage_control_mode: "",
  solaredge_storage_command_mode: "",
  solaredge_storage_charge_limit: "",
  solaredge_storage_discharge_limit: "",
  solaredge_storage_backup_reserve: "",
  fronius_modbus_host: "",
  fronius_modbus_port: 502,
  kostal_modbus_host: "",
  kostal_modbus_port: 1502,
  sma_modbus_host: "",
  sma_modbus_port: 502,
  forecast_source: "solcast_solar",
  forecast_remaining_entity: "",
  forecast_tomorrow_entity: "",
  forecast_today_entity: "",
  forecast_day3_entity: "",
  forecast_day4_entity: "",
  forecast_day5_entity: "",
  forecast_day6_entity: "",
  forecast_day7_entity: "",
  lookback_weeks: 4,
  enable_peakshare: true,
  peakshare_community: "BEG",
  // Gemeinschaften der Preisfunktion. Vorbelegt für den typischen Fall: die
  // Einspeisung teilt sich hälftig auf eine BEG und eine EEG auf. Die Namen
  // setzt _loadPeakShareCommunities aus der PeakShare-Liste ein — die API
  // kennt keinen Typ, es entscheidet also der Name. Gewichtung (Ersparnis
  // ohne Geldfluss) gibt es nur bei der EEG: sie spart Netzgebühren, die
  // BEG nicht. Diese Werte gelten NUR für Neueinrichtungen — bestehende
  // Anlagen behalten ihre gespeicherte Konfiguration.
  peakshare_share_pct: 50,
  peakshare_price: 0.082,
  peakshare_price_night: 0.102,
  peakshare_weight: 0,
  peakshare_community_2: "",
  peakshare_share_pct_2: 50,
  peakshare_price_2: 0.082,
  peakshare_price_night_2: 0.102,
  peakshare_weight_2: 0.01,
  // Fahrplan-Tarife (Vorgaben: österreichische Richtwerte, Stand 2026)
  // OeMAG als Vorgabe: der monatliche Marktpreis passt für die meisten
  // Anlagen und muss nicht abgetippt werden. Der feste Wert bleibt als
  // Rückfallwert stehen, falls die OeMAG-Seite nichts liefert.
  schedule_feedin_source: "oemag",
  schedule_feedin_price: 0.082,
  // Spotpreis als Basistarif (aWATTar-API): Marktgebiet und der Abschlag,
  // den der Vermarkter vom Börsenpreis abzieht (0 = voller Spot).
  spot_market_area: "at",
  spot_feedin_fee: 0.02,
  // Nachtsatz der Standardvergütung: 0 heißt „wie am Tag" (kein Nachttarif).
  schedule_feedin_price_night: 0,
  schedule_night_start: "20:00",
  schedule_night_end: "06:00",
  schedule_consumption_price: 0.26,
  // Alterungskosten der Batterie: derselbe Wert wie DEFAULT_BATTERY_COST im
  // Backend (schedule.py) — im Feld sichtbar statt nur als Platzhalter.
  schedule_battery_cost: 0.01,

  // Batterie-Leistungsgrenze des Fahrplans (Schlüssel bleibt discharge_power_kw)
  discharge_power_kw: 5.0,
  // Maximum-Ladestand: 100 heisst bis voll laden (kein eigener Schalter,
  // der Zustand steckt allein im Wert — Migration v27).
  schedule_max_soc_pct: 100,
  // Einspeisegrenze (Guard 1 + LP-Modell). Opt-in.
  grid_export_limit_enabled: false,
  grid_export_limit_kw: 4,
  inverter_ac_limit_kw: "",
  expert_mode: false,
};

// Solcast sensor names changed across versions — support both conventions
const SOLCAST_DEFAULTS_CANDIDATES = {
  forecast_remaining_entity: [
    "sensor.solcast_pv_forecast_prognose_verbleibende_leistung_heute",
    "sensor.solcast_pv_forecast_prognose_fuer_heute",
  ],
  forecast_tomorrow_entity: [
    "sensor.solcast_pv_forecast_prognose_morgen",
    "sensor.solcast_pv_forecast_prognose_fuer_morgen",
  ],
};

const FORECAST_SOLAR_DEFAULTS = {
  forecast_remaining_entity: "sensor.energy_production_today_remaining",
  forecast_tomorrow_entity: "sensor.energy_production_tomorrow",
};

// Guide-Dialoge — Inhalte werden aus docs/guides/*.md generiert (scripts/build_guides.py)
// und zur Laufzeit von /eeg_optimizer_panel/guide/<datei> geladen.
const DIALOG_CONTENT = {
  huawei: { file: "huawei.html" },
  solcast: { file: "solcast.html" },
  forecast_solar: { file: "forecast_solar.html" },
  capacity_sensor: { file: "capacity_sensor.html" },
  solax: { file: "solax.html" },
  solaredge: { file: "solaredge.html" },
  fronius: { file: "fronius.html" },
  kostal: { file: "kostal.html" },
  sma: { file: "sma.html" },
  einspeisegrenze: { file: "einspeisegrenze.html" },
};

// Suppress HA-internal unhandled promise rejections that crash the panel
window.addEventListener("unhandledrejection", (e) => {
  const msg = e.reason?.message || String(e.reason || "");
  if (msg.includes("Subscription not found") ||
      msg.includes("Transition was") ||
      msg.includes("message channel closed") ||
      msg.includes("asynchronous response")) {
    e.preventDefault();
    if (msg.includes("Transition")) {
      // Force panel recovery after View Transition failure
      const panel = document.querySelector("eeg-optimizer-panel");
      if (panel && panel._initialized && panel._shadow) {
        if (!panel._shadow.querySelector(".content")) {
          panel._render();
        }
      }
    }
  }
});

// Also suppress synchronous errors from HA internals / extensions
window.addEventListener("error", (e) => {
  const msg = e.message || "";
  if (msg.includes("message channel closed") ||
      msg.includes("asynchronous response")) {
    e.preventDefault();
  }
});

// Format a number with German decimal comma instead of dot.
const fmtDe = (value, decimals = 1) => Number(value).toFixed(decimals).replace(".", ",");

// Preise werden in Cent eingegeben, die Konfiguration haelt Euro je kWh.
// Die Umrechnung sitzt an genau zwei Stellen: hier beim Anzeigen und im
// Eingabepfad an `data-unit="ct"`. Gerundet wird, weil 0.082 * 100 in
// Gleitkomma 8.200000000000001 ergibt — das stand sonst im Feld.
const ctAus = (euro) => Math.round(Number(euro || 0) * 100000) / 1000;
const euroAus = (ct) => Math.round(Number(ct || 0) * 1000) / 100000;

// Ein Zahlenfeld lesen. Cent-Felder tragen data-unit="ct" und werden hier
// zurueckgerechnet — so kann kein einzelnes Feld die Umrechnung vergessen.
const leseZahl = (el) => {
  const roh = parseFloat(el.value) || 0;
  return el.dataset?.unit === "ct" ? euroAus(roh) : roh;
};

class EegOptimizerPanel extends HTMLElement {
  constructor() {
    super();
    this._shadow = this.attachShadow({ mode: "open" });
    this._hass = null;
    this._view = "dashboard";
    this._config = null;
    this._setupComplete = false;
    this._wizardStep = 0;
    this._wizardData = { ...WIZARD_DEFAULTS };
    // Block der zweiten Gemeinschaft: zugeklappt, bis der Nutzer ihn öffnet
    // oder eine zweite Gemeinschaft konfiguriert ist.
    this._gem2Open = false;
    // Home Assistant setzt `narrow` selbst, aber erst nach dem ersten Paint.
    // Bis dahin stand hier `false` — das Handy bekam fuer einen Frame das
    // Desktop-Layout und sprang danach um. Gleiche Schwelle wie HA (870 px).
    this._narrow = (typeof window !== "undefined" && window.matchMedia)
      ? window.matchMedia("(max-width: 870px)").matches : false;
    this._initialized = false;
    // Gemessene Anzeigebreite je Diagramm in CSS-Pixeln. Die Diagramme
    // zeichnen mit viewBox == Anzeigebreite; Schriftgroessen sind dadurch
    // echte Pixel und schrumpfen nicht mit der Kartenbreite. Beim ersten
    // Render ist die Breite unbekannt — dann greift die Schaetzung in _cw(),
    // und _measureCharts() korrigiert sie einen Frame spaeter.
    this._chartW = {};
    this._chartRemeasure = false;
    this._resizeObserver = null;
    // Wochentag, dessen Linie im Verbrauchsprofil hervorgehoben ist (null =
    // heute). Wird per Tipp auf die Legende gesetzt.
    this._profilHighlight = null;
    this._schedScrub = false;
    this._prerequisites = null;
    this._detectedSensors = null;
    this._wizardLoading = false;
    this._showDialog = null;
    this._guideCache = {};
    this._capacityMode = null;
    this._capacityModeUserSet = false;
    this._activityLog = [];
    this._activityUnsub = null;
    this._activityTotal = 0;
    this._activityHasMore = false;
    this._activityLoadingMore = false;
    this._activityShowAll = false;
    this._activityFilter = ""; // "" = alle, "laden", "entladung", "normal"
    this._activityLogOpen = this._loadPref("activity_log_open", "0", ["0", "1"]) === "1";
    this._loadConfigPending = false;
    this._profilOpen = false;
    // Persisted in localStorage so the user's last choice survives page reloads.
    this._profilChartVariant = this._loadPref("profil_chart_variant", "hourly", ["hourly", "daynight"]);
    // Statusbild und Fahrplan-Zustand werden je Ansichtsbreite getrennt
    // gemerkt — siehe _ansichtsPrefsLaden(), das sie hier und bei jedem
    // Breitenwechsel setzt.
    this._statusViewVariant = "values";
    this._forecastOpen = this._loadPref("forecast_open", "1", ["0", "1"]) === "1";
    this._settingsTab = this._loadPref("settings_tab", "tarife",
      ["tarife", "anlage", "system",
       // Aliasse aus Vorversionen — werden in _renderSettings abgebildet
       "fahrplan", "wechselrichter", "batterie", "gemeinschaft", "advanced",
       "einspeisegrenze", "telemetry"]);
    this._profileRefreshing = false;
    this._profileRefreshResult = null;
    // Welche Bedarfskurven im Fahrplan-Diagramm liegen: "all", ein
    // Gemeinschaftsname oder "off". Kein allowed-Filter — der Name ist frei.
    // Fahrplan-Archiv (Diagnose): Zustand wird beim Öffnen der Einstellungen
    // geholt, die Download-URL ist signiert und nur Minuten gültig.
    this._archivStatus = null;
    this._archivBusy = false;
    // Tagesbilanz (Diagnose, Expertenmodus): wird nur auf Knopfdruck geholt,
    // nicht beim Öffnen der Einstellungen — sie kostet eine Recorder-Abfrage
    // über einen ganzen Tag und ein Archiv-Lesen.
    this._bilanzStatus = null;
    this._bilanzBusy = false;
    // Einspeise-Statistik: wird beim Aufklappen der Karte geholt, nicht beim
    // Aufbau des Dashboards — die Antwort traegt alle Tage seit der
    // Einrichtung.
    this._feedinStats = null;
    this._feedinStatsLoaded = false;
    this._feedinStatsOpen = false;
    this._feedinStatsPeriod = "month";
    this._peakshareDataOpen = false;
    this._peakshareData = null;
    this._peakshareDataLoaded = false;

    // Fahrplan: der einzige Aktor. Wird beim Start geladen und zyklisch
    // aktualisiert — die Statuskarte zeigt daraus die Job-Laufzeiten.
    // Die Karte ist das Ergebnis der Integration — sie steht offen, bis der
    // Nutzer sie zuklappt; dann bleibt sie zu (wie das Aktivitätsprotokoll).
    this._scheduleOpen = true;
    this._scheduleData = null;
    this._scheduleLoaded = false;
    // Transparenz-Ansicht: welche Stellgröße gerade auf welchem Wert steht.
    this._controlState = null;
    this._scheduleBusy = false;
    // Ist-Verlauf im Optimierungsplan: "off" | "12h" | "yesterday".
    // Standard ist der Plan allein — der Recorder wird erst auf Wunsch
    // gelesen. 12 h als Standard-Rueckblick, weil ein laengerer Rueckblick
    // die 48 h Plan auf unter die halbe Diagrammbreite stauchen wuerde.
    // Wie viel vom Plan gezeigt wird (Stunden). 48 ist der ganze Horizont;
    // kürzer gewählt wird die Achse gestaucht und der Abend deutlicher.
    this._schedPlanRange = this._loadPref("sched_plan_h", "48", ["24", "36", "48"]);
    this._schedHistRange = this._loadPref("sched_hist_range", "off",
                                          ["off", "12h", "yesterday"]);
    this._schedHist = null;
    this._schedHistBusy = false;
    this._schedHistError = null;
    // Karte „Optimierungsgewinn": Kennzahl + Vergleichschart „ohne
    // Optimierung" (Auf/Zu bleibt gespeichert), die Geld-Details flüchtig.
    this._gewinnOpen = this._loadPref("gewinn_open", "1", ["0", "1"]) === "1";
    this._gewinnDetailsOpen = false;
    this._lastScheduleReload = 0;
    this._lastPeakshareReload = 0;

    // Karte „Was deine PV bringt": Geldwerte aus der Energiebilanz. Details
    // flüchtig, die Karte selbst ist immer offen — sie ist die Antwort auf
    // die häufigste Frage überhaupt.
    this._bilanz = null;
    this._bilanzBusy = false;
    this._bilanzGeholt = 0;
    this._bilanzDetailsOpen = false;

    this._settingsData = {};
    this._settingsFehler = null;
    // Monatlicher OeMAG-Einspeisetarif — Wert, Monat, Alter und letzter
    // Fehler. Geholt wird er über _ensureOemagTarif(), sobald ihn eine
    // Ansicht zeigt oder jemand auf OeMAG umschaltet.
    this._oemagStatus = null;
    this._oemagBusy = false;
    this._oemagRequested = false;
    // Spotpreis-Status (Quelle „Strombörse"), gleiche Mechanik wie OeMAG.
    this._spotStatus = null;
    this._spotBusy = false;
    this._spotRequested = false;
    this._peakshareCommunitiesCache = [];
    this._peakshareCommunitiesLoading = false;
    // Community-Statistik (Phase 8: Telemetrie-Opt-In)
    this._telemetryStatus = null;
    this._telemetryError = null;
    this._telemetryBusy = false;
    this._lastHassUpdate = Date.now();

    // Recover from network switches / long sleep when tab becomes visible
    this._onVisibilityChange = () => {
      if (document.visibilityState === "visible" && this._hass) {
        const elapsed = Date.now() - this._lastHassUpdate;
        // If no hass update for >30s, the connection likely dropped
        if (elapsed > 30000) {
          console.info("EEG Energy Optimizer: tab visible after " + Math.round(elapsed / 1000) + "s, refreshing");
          this._loadConfigPending = false;
          this._loadConfigWithRetry();
        }
      }
    };
    document.addEventListener("visibilitychange", this._onVisibilityChange);

    // Start watchdog for active-tab connection drops
    this._watchdogInterval = null;
    this._startWatchdog();

    this._ansichtsPrefsLaden();

    // Event delegation on shadow root
    // Legend hover: highlight matching weekday line
    // Fahrplan-Diagramm: der Tooltip folgt dem Zeiger, damit er auch beim
    // Wandern innerhalb eines Slots an der richtigen Stelle sitzt.
    this._shadow.addEventListener("mousemove", (e) => {
      const hit = e.target.closest?.(".sched-hit");
      if (hit) this._showSchedTooltip(hit, e);
    });

    this._shadow.addEventListener("mouseover", (e) => {
      // Fahrplan-Diagramm: Werte des Slots unter dem Zeiger
      const hit = e.target.closest?.(".sched-hit");
      if (hit) this._showSchedTooltip(hit, e);
      const dot = e.target.closest(".ps-dot, .ps-dot-hit");
      if (dot) this._zeigePsTooltip(dot);
      const legendItem = e.target.closest(".wl-legend");
      if (!legendItem) return;
      const idx = legendItem.dataset.idx;
      const svg = legendItem.closest("svg");
      if (!svg) return;
      svg.querySelectorAll(".wl").forEach(g => g.classList.remove("wl-legend-hover"));
      const target = svg.querySelector(`.wl[data-idx="${idx}"]`);
      if (target) target.classList.add("wl-legend-hover");
    });
    this._shadow.addEventListener("mouseout", (e) => {
      const hit = e.target.closest?.(".sched-hit");
      if (hit) {
        const tt = hit.closest(".sched-chart-card")?.querySelector(".sched-tooltip");
        if (tt) tt.style.display = "none";
        const svg = hit.ownerSVGElement;
        const cursor = svg?.querySelector(".sched-cursor");
        if (cursor) cursor.style.visibility = "hidden";
        const dot = svg?.querySelector(".sched-cursor-soc");
        if (dot) dot.style.visibility = "hidden";
      }
      const dot = e.target.closest(".ps-dot, .ps-dot-hit");
      if (dot) this._versteckePsTooltip();
      const legendItem = e.target.closest(".wl-legend");
      if (!legendItem) return;
      const svg = legendItem.closest("svg");
      if (!svg) return;
      svg.querySelectorAll(".wl").forEach(g => g.classList.remove("wl-legend-hover"));
    });

    // Touch: die Werte in Fahrplan und Bedarfskurve holt man sich mit dem
    // Finger. Nur Maus-Ereignisse genuegten dafuer nicht — iOS erzeugt beim
    // Tippen ein einmaliges mouseover (der Tooltip blieb danach stehen) und
    // beim Wischen gar kein mousemove. Zeiger-Ereignisse koennen beides:
    // ein Tipp zeigt den Wert, Ziehen fuehrt ihn mit (das Ziel bleibt
    // waehrend einer Beruehrung fest, deshalb wird das Element unter dem
    // Finger jedes Mal neu bestimmt).
    this._shadow.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse") return;
      const hit = e.target.closest?.(".sched-hit");
      if (hit) { this._schedScrub = true; this._showSchedTooltip(hit, e); return; }
      const dot = e.target.closest?.(".ps-dot, .ps-dot-hit");
      if (dot) { this._zeigePsTooltip(dot); return; }
      // Daneben tippen raeumt die Werteanzeige wieder ab.
      this._versteckeSchedTooltip();
      this._versteckePsTooltip();
    }, { passive: true });

    this._shadow.addEventListener("pointermove", (e) => {
      if (!this._schedScrub || e.pointerType === "mouse") return;
      const el = this._shadow.elementFromPoint
        ? this._shadow.elementFromPoint(e.clientX, e.clientY)
        : document.elementFromPoint(e.clientX, e.clientY);
      const hit = el?.closest?.(".sched-hit");
      if (hit) this._showSchedTooltip(hit, e);
    }, { passive: true });

    const scrubEnde = () => { this._schedScrub = false; };
    this._shadow.addEventListener("pointerup", scrubEnde, { passive: true });
    this._shadow.addEventListener("pointercancel", scrubEnde, { passive: true });

    // Beim Blaettern die Werteanzeige abraeumen: sonst bleibt sie an einer
    // Stelle stehen, die nicht mehr zum Diagramm darunter passt.
    this._onScrollHide = () => {
      if (this._schedScrub) return;
      this._versteckeSchedTooltip();
      this._versteckePsTooltip();
    };
    window.addEventListener("scroll", this._onScrollHide, { passive: true, capture: true });

    // Info popup toggle for touch devices
    this._shadow.addEventListener("click", (e) => {
      const trigger = e.target.closest(".info-popup-trigger");
      if (trigger) {
        e.stopPropagation();
        // Close any other open popups
        this._shadow.querySelectorAll(".info-popup-trigger.active").forEach(t => {
          if (t !== trigger) t.classList.remove("active");
        });
        trigger.classList.toggle("active");
        return;
      }
      // Close popups when clicking elsewhere
      this._shadow.querySelectorAll(".info-popup-trigger.active").forEach(t => t.classList.remove("active"));

      // Close dialog/info-modal when clicking overlay background (not the card itself)
      if (e.target.classList.contains("dialog-overlay")) {
        this._showDialog = null;
        this._render();
        return;
      }
      // Wochentag in der Legende des Verbrauchsprofils antippen: hebt seine
      // Linie hervor. Ohne Maus war das Hervorheben vorher nicht bedienbar.
      const legendeItem = e.target.closest?.(".wl-legend");
      if (legendeItem) {
        const idx = Number(legendeItem.dataset.idx);
        if (!isNaN(idx)) {
          this._profilHighlight = (this._profilHighlight === idx) ? null : idx;
          this._render();
        }
        return;
      }
      const btn = e.target.closest("[data-action]") || e.target;
      const action = btn?.dataset?.action;
      if (action === "toggle-telemetry") {
        // Checkbox: nutze den (nach Klick aktualisierten) checked-Zustand
        this._handleTelemetryToggle(!!btn.checked);
        return;
      }
      if (action === "forget-telemetry") {
        this._handleTelemetryForget();
        return;
      }
      if (action) {
        this._handleAction(action, btn.dataset);
      }
    });

    // Listen for value-changed events from ha-entity-picker
    this._shadow.addEventListener("value-changed", (e) => {
      const target = e.target.closest("[data-field]");
      if (target) {
        const field = target.dataset.field;
        this._wizardData[field] = e.detail?.value || "";
      }
    });

    // Listen for input/change events for native inputs
    this._shadow.addEventListener("input", (e) => {
      const target = e.target.closest("[data-field]");
      if (target) {
        const field = target.dataset.field;
        if (field.startsWith("settings_")) {
          const realField = field.replace("settings_", "");
          const type = target.type;
          if (type === "checkbox") {
            this._settingsData[realField] = target.checked;
            this._render();
          } else if (type === "number") {
            this._settingsData[realField] = leseZahl(target);
          } else {
            this._settingsData[realField] = target.value;
          }
          return;
        }
        // Huawei Master/Slave: Einzelkapazität pro Gerät → dict
        // huawei_battery_capacities[device_id]. Feld: huawei_cap_<device_id>.
        if (field.startsWith("huawei_cap_")) {
          const devId = field.slice("huawei_cap_".length);
          if (!this._wizardData.huawei_battery_capacities) {
            this._wizardData.huawei_battery_capacities = {};
          }
          const v = parseFloat(target.value);
          if (v > 0) this._wizardData.huawei_battery_capacities[devId] = v;
          else delete this._wizardData.huawei_battery_capacities[devId];
          return;
        }
        const type = target.type;
        if (type === "number") {
          this._wizardData[field] = leseZahl(target);
        } else {
          this._wizardData[field] = target.value;
        }
        // Pflichtfeld gerade ausgefüllt? Dann muss der Weiter-Knopf sofort
        // klickbar werden, ohne dass erst irgendwo anders ein Render ausgelöst
        // wird.
        this._syncWeiterKnopf();
      }
    });

    this._shadow.addEventListener("change", (e) => {
      // Auswahlfelder über dem Diagramm — kein data-field, sie gehören zu
      // keiner Konfiguration, sondern nur zur Ansicht.
      const chartWahl = e.target.closest("select[data-chart]");
      if (chartWahl) {
        this._chartBereichSetzen(chartWahl.dataset.chart, chartWahl.value);
        return;
      }
      const target = e.target.closest("[data-field]");
      if (target) {
        const field = target.dataset.field;
        if (field === "activity_filter") {
          this._activityFilter = target.value;
          this._render();
          return;
        }
        if (field.startsWith("settings_")) {
          const realField = field.replace("settings_", "");
          const type = target.type;
          if (type === "checkbox") {
            this._settingsData[realField] = target.checked;
            if (realField === "enable_peakshare" && target.checked && this._peakshareCommunitiesCache.length === 0) {
              this._loadPeakShareCommunities();
            }
            // Re-render only for toggles that change UI visibility (e.g. enable_peakshare reveals the community dropdown).
            // For simple number/text inputs we skip _render() — otherwise a click on "Speichern" right after editing
            // a value triggers blur→change→render, which replaces the save button DOM node and swallows the click.
            this._render();
          } else if (type === "number") {
            this._settingsData[realField] = leseZahl(target);
            // KEIN Render bei Zahlenfeldern. Früher wurde hier fürs
            // Nachtfenster nachgezogen, weil es nur mit eingetragenem
            // Nachtsatz sichtbar war; beide Nachtfenster stehen jetzt immer
            // da, der Nachzug ist damit weg. Das ist auch der sichere
            // Zustand: ein Render zwischen mousedown und mouseup ersetzt den
            // Speichern-Knopf, und der Klick verpufft (siehe oben).
          } else {
            this._settingsData[realField] = target.value;
            // Ein <select> ist weder checkbox noch number und landete hier
            // ohne Render: die Quelle der Standardvergütung blendet aber
            // ganze Blöcke um (OeMAG-Wert statt Eingabefeld, kein eigener
            // Nachtsatz). Das war der Grund, warum das Umschalten auf OeMAG
            // sichtbar nichts tat.
            if (realField === "schedule_feedin_source") {
              if (target.value === "oemag") this._ensureOemagTarif();
              if (target.value === "spot") this._ensureSpotStatus();
              this._render();
            }
          }
          return;
        }
        if (field === "expert_mode") {
          this._wizardData[field] = target.checked;
          this._saveWizardProgress();
          this._render();
          return;
        }
        if (field === "enable_peakshare") {
          this._wizardData[field] = target.checked;
          if (target.checked && this._peakshareCommunitiesCache.length === 0) {
            this._loadPeakShareCommunities();
          }
          this._saveWizardProgress();
          this._render();
          return;
        }
        // Wizard-Zahlenfelder speichert der input-Handler. Hier stand früher
        // ein Render-Nachzug fürs konditionale Nachtfenster — es steht jetzt
        // immer da, und ein Render beim Verlassen des Feldes würde nur den
        // gerade angeklickten Knopf unter dem Mauszeiger austauschen.
        if (target.tagName === "SELECT") {
          this._wizardData[field] = target.value;
          if (field === "forecast_source") {
            this._applyForecastDefaults(target.value);
            this._render();
          } else if (field === "schedule_feedin_source") {
            if (target.value === "oemag") this._ensureOemagTarif();
            if (target.value === "spot") this._ensureSpotStatus();
            this._saveWizardProgress();
            this._render();
          }
        } else if (target.type === "radio") {
          this._wizardData[field] = target.value;
        }
      }
    });
  }

  // localStorage-backed UI preferences (e.g. last-selected chart variants).
  // Wrapped so SSR/test environments without window.localStorage don't crash.
  _loadPref(key, fallback, allowed) {
    try {
      const raw = window.localStorage?.getItem(`eeg_optimizer_panel_${key}`);
      if (raw && (!allowed || allowed.includes(raw))) return raw;
    } catch (e) { /* ignore */ }
    return fallback;
  }

  // Ausschnitt des Plans oder Umfang des Rueckblicks umstellen. Zwei
  // Bedienwege fuehren hierher: das Auswahlfeld am Handy (change) und der
  // Segmentumschalter am Desktop (Klick) — die Logik darf nur einmal
  // existieren, sonst laufen localStorage und Nachladen auseinander.
  _chartBereichSetzen(art, wert) {
    if (art === "plan") {
      this._schedPlanRange = wert;
      this._savePref("sched_plan_h", wert);
    } else {
      this._schedHistRange = wert;
      this._savePref("sched_hist_range", wert);
      this._schedHistError = null;
      if (wert !== "off") this._loadScheduleHistory();
    }
    this._render();
  }

  _savePref(key, value) {
    try {
      window.localStorage?.setItem(`eeg_optimizer_panel_${key}`, String(value));
    } catch (e) { /* ignore */ }
  }

  // Holt den Tarif einmal je Sitzung, sobald ihn jemand sehen will: beim
  // Umschalten auf OeMAG und beim Rendern einer Ansicht, die ihn zeigt.
  // Ein fehlgeschlagener Abruf wird nicht wiederholt (sonst Retry-Sturm bei
  // jedem Render) — dafür gibt es „Jetzt holen".
  _ensureOemagTarif() {
    if (this._oemagStatus !== null || this._oemagBusy || this._oemagRequested) return;
    this._oemagRequested = true;
    this._loadOemagTarif();
  }

  // Geldwerte der Energiebilanz. Kein eigener Timer: Ein Render stößt das
  // Nachladen an, wenn die Werte älter als eine Minute sind — und weil das
  // Laden den Zeitstempel setzt, kann daraus keine Schleife werden.
  _ensureBilanz() {
    if (this._bilanzBusy || !this._hass) return;
    const alter = Date.now() - this._bilanzGeholt;
    if (this._bilanz !== null && alter < 60000) return;
    this._loadBilanz();
  }

  async _loadBilanz() {
    if (this._bilanzBusy || !this._hass) return;
    this._bilanzBusy = true;
    try {
      this._bilanz = await this._hass.callWS({ type: "eeg_optimizer/get_bilanz" });
    } catch (e) {
      console.warn("Energiebilanz nicht abrufbar:", e);
      this._bilanz = { verfuegbar: false };
    } finally {
      this._bilanzBusy = false;
      this._bilanzGeholt = Date.now();
      this._render();
    }
  }

  _ensureSpotStatus() {
    if (this._spotStatus !== null || this._spotBusy || this._spotRequested) return;
    this._spotRequested = true;
    this._loadSpotStatus();
  }

  async _loadSpotStatus(refresh = false) {
    if (this._spotBusy || !this._hass) {
      if (!this._hass) this._spotRequested = false;
      return;
    }
    this._spotBusy = true;
    if (refresh) this._render();
    try {
      this._spotStatus = await this._hass.callWS({
        type: "eeg_optimizer/get_spot_preis",
        ...(refresh ? { refresh: true } : {}),
      });
    } catch (e) {
      console.warn("Spotpreis nicht abrufbar:", e);
      this._spotStatus = { preis: null, fehler: e?.message || String(e) };
    } finally {
      this._spotBusy = false;
      this._render();
    }
  }

  async _loadArchivStatus() {
    if (this._archivBusy || !this._hass) return;
    this._archivBusy = true;
    try {
      this._archivStatus = await this._hass.callWS({
        type: "eeg_optimizer/get_schedule_archive",
      });
    } catch (e) {
      console.warn("Fahrplan-Archiv nicht abrufbar:", e);
      this._archivStatus = { aktiv: false, fehler: e?.message || String(e) };
    } finally {
      this._archivBusy = false;
      this._render();
    }
  }

  async _tagesbilanzJetzt() {
    if (this._bilanzBusy || !this._hass) return;
    this._bilanzBusy = true;
    this._bilanzStatus = null;
    this._render();
    try {
      this._bilanzStatus = await this._hass.callWS({
        type: "eeg_optimizer/tagesbilanz_jetzt",
      });
    } catch (e) {
      console.warn("Tagesbilanz nicht rechenbar:", e);
      this._bilanzStatus = { fehler: e?.message || String(e), bilanzen: [] };
    } finally {
      this._bilanzBusy = false;
      this._render();
    }
  }

  _bilanzKarte() {
    const b = this._bilanzStatus;
    const abw = (plan, ist) => {
      if (plan == null || ist == null) return "";
      const d = ist - plan;
      const vz = d >= 0 ? "+" : "−";
      return `<span style="color:var(--secondary-text-color)"> (${vz}${fmtDe(Math.abs(d), 1)})</span>`;
    };
    const kwh = (v) => (v == null ? "—" : `${fmtDe(v, 1)} kWh`);

    let ergebnis = "";
    if (this._bilanzBusy) {
      ergebnis = `<div class="help-text" style="margin-top:10px">Wird gerechnet…</div>`;
    } else if (b?.fehler) {
      ergebnis = `<div class="help-text" style="margin-top:10px;color:#e53935">Nicht rechenbar: ${this._escapeHtml(b.fehler)}</div>`;
    } else if (b && !b.bilanzen?.length) {
      ergebnis = `<div class="help-text" style="margin-top:10px">Für ${this._escapeHtml(b.tag || "gestern")} kam keine Bilanz zustande — meist fehlen Messwerte, weil die Anlage noch nicht lange genug läuft. Der Ladestand und die Leistungen müssen für 95 % des Tages in der Datenbank stehen.</div>`;
    } else if (b) {
      const zeilen = b.bilanzen.map(z => {
        const vorlauf = z.event_type === "fahrplan_tag_48h" ? "48 h" : "24 h";
        const ohnePlan = z.predicted_pv_kwh == null;
        return `
          <tr>
            <td style="padding:4px 10px 4px 0;white-space:nowrap">${vorlauf}</td>
            <td style="padding:4px 10px 4px 0;white-space:nowrap">${ohnePlan ? "—" : kwh(z.predicted_pv_kwh)}</td>
            <td style="padding:4px 10px 4px 0;white-space:nowrap">${kwh(z.actual_pv_kwh)}${abw(z.predicted_pv_kwh, z.actual_pv_kwh)}</td>
            <td style="padding:4px 10px 4px 0;white-space:nowrap">${ohnePlan ? "—" : kwh(z.predicted_consumption_kwh)}</td>
            <td style="padding:4px 0;white-space:nowrap">${kwh(z.actual_consumption_kwh)}${abw(z.predicted_consumption_kwh, z.actual_consumption_kwh)}</td>
          </tr>`;
      }).join("");
      const erste = b.bilanzen[0];
      const gesendet = b.telemetrie_aktiv
        ? `${b.gesendet} von ${b.bilanzen.length} an das Backend gesendet`
        : "nur gerechnet — die Telemetrie ist nicht aktiv";
      ergebnis = `
        <div style="margin-top:12px;font-size:13px;color:var(--primary-text-color)">
          <strong>${this._escapeHtml(b.tag || "")}</strong> · ${this._escapeHtml(gesendet)}
        </div>
        <div style="overflow-x:auto;margin-top:8px">
          <table style="border-collapse:collapse;font-size:13px">
            <thead>
              <tr style="color:var(--secondary-text-color);font-size:12px;text-align:left">
                <th style="padding:0 10px 4px 0;font-weight:500">Vorlauf</th>
                <th style="padding:0 10px 4px 0;font-weight:500">PV geplant</th>
                <th style="padding:0 10px 4px 0;font-weight:500">PV gemessen</th>
                <th style="padding:0 10px 4px 0;font-weight:500">Verbrauch geplant</th>
                <th style="padding:0 0 4px;font-weight:500">Verbrauch gemessen</th>
              </tr>
            </thead>
            <tbody>${zeilen}</tbody>
          </table>
        </div>
        <div class="help-text" style="margin-top:8px">
          Eingespeist ${kwh(erste?.grid_export_kwh)}, höchste Einspeiseleistung
          ${erste?.peak_power_kw == null ? "—" : `${fmtDe(erste.peak_power_kw, 2)} kW`},
          Ladestand ${erste?.soc_start_pct == null ? "—" : `${erste.soc_start_pct} %`}
          → ${erste?.soc_end_pct == null ? "—" : `${erste.soc_end_pct} %`}.
          ${b.bilanzen.length === 1
            ? "Nur eine Zeile: für den 48-Stunden-Vorlauf lag kein Plan vor, der den ganzen Tag abdeckt — bei Forecast.Solar ist das der Normalfall, die Prognose reicht dort nur bis zum Ende des Folgetags."
            : ""}
        </div>`;
    }

    return `
      <div class="card" style="margin-bottom:16px">
        <h3 class="settings-karte-titel" style="margin:0 0 8px">Tagesbilanz</h3>
        <div class="help-text">Stellt für den letzten abgeschlossenen Tag gegenüber, was der Fahrplan an PV-Ertrag und Verbrauch erwartet hatte und was gemessen wurde — mit zwei Prognose-Vorläufen, dem Plan vom Vorabend und dem von zwei Tagen vorher. Läuft sonst automatisch nachts um 00:15. Gerechnet wird aus der Datenbank und dem Fahrplan-Archiv, es entstehen keine neuen Aufzeichnungen.</div>
        <button class="btn-secondary" data-action="tagesbilanz-jetzt" style="margin-top:10px" ${this._bilanzBusy ? "disabled" : ""}>${this._bilanzBusy ? "Rechnet…" : "Jetzt rechnen"}</button>
        ${ergebnis}
      </div>`;
  }

  async _loadFeedinStats() {
    if (!this._hass) return;
    try {
      this._feedinStats = await this._hass.callWS({
        type: "eeg_optimizer/get_feedin_statistics",
        days: 0,
      });
      this._feedinStatsLoaded = true;
    } catch (e) {
      console.warn("Einspeise-Statistik nicht abrufbar:", e);
      this._feedinStats = null;
      this._feedinStatsLoaded = true;
    }
    this._render();
  }

  _renderFeedinStatistics() {
    if (!this._feedinStatsLoaded) {
      return `<p style="color:var(--secondary-text-color);font-size:14px">Statistik wird geladen\u2026</p>`;
    }
    const s = this._feedinStats;
    if (!s) {
      return `<p style="color:var(--secondary-text-color);font-size:14px">Statistik nicht abrufbar.</p>`;
    }

    const leer = { kwh: 0, count: 0, duration_min: 0 };
    const zeitraum = this._feedinStatsPeriod;
    const daten = s[zeitraum] || {};
    const entladung = daten.evening || leer;
    const morgen = daten.morning || leer;

    const dauer = (min) => {
      if (!min) return "0 Min";
      if (min < 60) return `${min} Min`;
      const h = Math.floor(min / 60), r = min % 60;
      return r > 0 ? `${h} h ${r} min` : `${h} h`;
    };

    const knoepfe = [
      ["week", "Woche"], ["month", "Monat"],
      ["year", "Jahr"], ["total", "Gesamt"],
    ].map(([key, text]) => {
      const an = zeitraum === key;
      return `<button data-action="feedin-period-${key}"
        style="padding:5px 13px;border:1px solid var(--divider-color);border-radius:16px;font:inherit;font-size:12px;cursor:pointer;
               background:${an ? "var(--primary-color)" : "var(--card-background-color,#fff)"};
               color:${an ? "var(--text-primary-color,#fff)" : "var(--primary-text-color)"}">${text}</button>`;
    }).join("");

    const kachel = (icon, farbe, titel, werte, zusatz) => `
      <div style="background:var(--card-background-color,#fff);border:1px solid var(--divider-color);border-radius:12px;padding:14px">
        <div style="font-size:13px;color:var(--secondary-text-color);margin-bottom:6px">
          <ha-icon icon="${icon}" style="--mdc-icon-size:16px;color:${farbe};vertical-align:middle"></ha-icon>
          ${titel}
        </div>
        <div style="font-size:24px;font-weight:600;color:var(--primary-text-color)">${fmtDe(werte.kwh, 1)} <span style="font-size:14px;font-weight:400">kWh</span></div>
        <div style="font-size:12px;color:var(--secondary-text-color);margin-top:4px">${werte.count}\u00d7 \u00b7 ${dauer(werte.duration_min)}</div>
        ${zusatz || ""}
      </div>`;

    // Die Morgen-Kachel erscheint nur, wenn im Zeitraum noch Werte von damals
    // liegen. Eine dauerhaft leere Kachel fuer ein abgeschafftes Feature
    // waere ein Raetsel, das niemand loesen kann.
    const morgenKachel = morgen.kwh > 0
      ? kachel("mdi:weather-sunny", "#FF9800", "Morgen-Einspeisung", morgen,
          `<div style="font-size:11px;color:var(--secondary-text-color);margin-top:6px">wird nicht mehr gez\u00e4hlt</div>`)
      : "";

    // Hinweis auf den Bedeutungswechsel — aber nur, wenn der gewaehlte
    // Zeitraum tatsaechlich ueber die Umstellung reicht.
    const umgestellt = s.umgestellt_am;
    const reichtZurueck = umgestellt && Object.keys(s.daily || {}).some(tag => tag < umgestellt);
    const hinweis = reichtZurueck
      ? `<div style="margin-top:12px;padding:10px 12px;border-left:3px solid var(--warning-color,#ff9800);background:var(--warning-color,#ff9800)18;border-radius:4px;font-size:12px;color:var(--primary-text-color)">
           Die Z\u00e4hlweise hat sich am ${this._escapeHtml(umgestellt)} ge\u00e4ndert. Davor wurde die Einspeisung
           w\u00e4hrend der Nacht-Entladung gez\u00e4hlt, seither die w\u00e4hrend jeder gesteuerten Entladung
           \u2014 der Fahrplan entl\u00e4dt auch tags\u00fcber. Werte vor und nach diesem Tag sind
           deshalb nicht unmittelbar vergleichbar.
         </div>`
      : "";

    return `
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 12px">${knoepfe}</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:16px">
        ${kachel("mdi:battery-arrow-down", "#2196F3", "Entladung ins Netz", entladung)}
        ${morgenKachel}
      </div>
      ${this._renderFeedinBarChart()}
      ${hinweis}`;
  }

  // Balkendiagramm, uebernommen aus der Fassung vor 1.5.1 (85bad64^).
  // Ein Morgen-Balken erscheint nur dort, wo Werte > 0 liegen — also
  // allein bei Tagen aus der Zeit der Zustands-Heuristik. Neue Tage
  // zeigen nur die Entladung, ohne Sonderbehandlung.
  _renderFeedinBarChart() {
    if (!this._feedinStats?.daily) return "";
    const daily = this._feedinStats.daily;
    const period = this._feedinStatsPeriod;

    // Determine how many days to show and whether to aggregate by month
    let byMonth = false;
    let daysBack = 30;
    if (period === "week") daysBack = 7;
    else if (period === "month") daysBack = 30;
    else if (period === "year") { daysBack = 365; byMonth = true; }
    else if (period === "total") { daysBack = 99999; byMonth = true; }

    if (byMonth) {
      return this._renderFeedinMonthlyChart(daily);
    }

    // Daily bars
    const today = new Date();
    const entries = [];
    for (let i = daysBack - 1; i >= 0; i--) {
      const d = new Date(today);
      d.setDate(d.getDate() - i);
      // LOKALES Datum als Schlüssel: das Backend legt die Tage unter
      // dt_util.now() ab (statistics.py). toISOString() liefert UTC und
      // zeigte zwischen Mitternacht und 02:00 den Vortag — also genau in
      // den Stunden, in denen die Nacht-Entladung läuft und der Ertrag
      // nachgesehen wird, während die Kacheln daneben (backend-seitig lokal
      // summiert) etwas anderes sagten.
      const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      const dayData = daily[key] || {};
      const mKwh = dayData.morning?.total_kwh || 0;
      const eKwh = dayData.evening?.total_kwh || 0;
      const mDur = dayData.morning?.total_duration_min || 0;
      const eDur = dayData.evening?.total_duration_min || 0;
      const label = d.toLocaleDateString("de-DE", {day: "2-digit", month: "2-digit"});
      entries.push({label, morning: mKwh, evening: eKwh, morningDur: mDur, eveningDur: eDur});
    }

    if (entries.length === 0) return `<p style="color:var(--secondary-text-color);font-size:13px">Noch keine Daten vorhanden</p>`;
    return this._renderGroupedFeedinBars(entries);
  }

  _renderFeedinMonthlyChart(daily) {
    // Aggregate daily data into months
    const months = {};
    for (const [dateStr, dayData] of Object.entries(daily)) {
      const monthKey = dateStr.slice(0, 7); // YYYY-MM
      if (!months[monthKey]) months[monthKey] = {morning: 0, evening: 0, morningDur: 0, eveningDur: 0};
      months[monthKey].morning += dayData.morning?.total_kwh || 0;
      months[monthKey].evening += dayData.evening?.total_kwh || 0;
      months[monthKey].morningDur += dayData.morning?.total_duration_min || 0;
      months[monthKey].eveningDur += dayData.evening?.total_duration_min || 0;
    }

    const sortedKeys = Object.keys(months).sort();
    if (sortedKeys.length === 0) return `<p style="color:var(--secondary-text-color);font-size:13px">Noch keine Daten vorhanden</p>`;

    const monthNames = ["Jan", "Feb", "M\u00e4r", "Apr", "Mai", "Jun", "Jul", "Aug", "Sep", "Okt", "Nov", "Dez"];
    const entries = sortedKeys.map(k => {
      const m = parseInt(k.slice(5, 7)) - 1;
      return {label: monthNames[m] + " " + k.slice(2, 4), morning: months[k].morning, evening: months[k].evening, morningDur: months[k].morningDur, eveningDur: months[k].eveningDur};
    });

    return this._renderGroupedFeedinBars(entries);
  }

  _renderGroupedFeedinBars(entries) {
    const width = 700, height = 300, padding = {top: 30, right: 20, bottom: 40, left: 50};
    const chartW = width - padding.left - padding.right;
    const chartH = height - padding.top - padding.bottom;
    const maxVal = Math.max(...entries.map(e => Math.max(e.morning, e.evening)), 0.1) * 1.15;
    const slotW = chartW / Math.max(entries.length, 1);
    const barW = Math.min(slotW * 0.35, 30);
    const gap = 2;

    const fmtDur = (min) => {
      if (!min) return "0 Min";
      if (min < 60) return min + " Min";
      const h = Math.floor(min / 60);
      const r = min % 60;
      return r > 0 ? h + "h " + r + "m" : h + "h";
    };

    let bars = "";
    entries.forEach((d, i) => {
      const slotX = padding.left + i * slotW;
      const x1 = slotX + (slotW - barW * 2 - gap) / 2;

      // Morning bar (left, orange)
      if (d.morning > 0) {
        const barH1 = (d.morning / maxVal) * chartH;
        const y1 = padding.top + chartH - barH1;
        const mTip = `${d.label} Morgen-Einspeisung\nEnergie: ${fmtDe(d.morning, 2)} kWh\nDauer: ${fmtDur(d.morningDur || 0)}`;
        bars += `<rect x="${x1}" y="${y1}" width="${barW}" height="${barH1}" fill="#FF9800" rx="3" style="cursor:pointer"><title>${mTip}</title></rect>`;
        if (entries.length <= 14) bars += `<text x="${x1 + barW/2}" y="${y1 - 4}" text-anchor="middle" font-size="10" fill="var(--primary-text-color)" style="pointer-events:none">${fmtDe(d.morning, 1)}</text>`;
      }

      // Evening bar (right, blue)
      const x2 = x1 + barW + gap;
      if (d.evening > 0) {
        const barH2 = (d.evening / maxVal) * chartH;
        const y2 = padding.top + chartH - barH2;
        const eTip = `${d.label} Nacht-Entladung\nEnergie: ${fmtDe(d.evening, 2)} kWh\nDauer: ${fmtDur(d.eveningDur || 0)}`;
        bars += `<rect x="${x2}" y="${y2}" width="${barW}" height="${barH2}" fill="#2196F3" rx="3" style="cursor:pointer"><title>${eTip}</title></rect>`;
        if (entries.length <= 14) bars += `<text x="${x2 + barW/2}" y="${y2 - 4}" text-anchor="middle" font-size="10" fill="var(--primary-text-color)" style="pointer-events:none">${fmtDe(d.evening, 1)}</text>`;
      }

      // Skip some labels if too many entries
      const labelEvery = entries.length > 20 ? Math.ceil(entries.length / 12) : 1;
      if (i % labelEvery === 0) {
        bars += `<text x="${slotX + slotW/2}" y="${height - 10}" text-anchor="middle" font-size="${entries.length > 14 ? 9 : 11}" fill="var(--secondary-text-color)">${d.label}</text>`;
      }
    });

    // Y-axis grid
    let yLines = "";
    for (let i = 0; i <= 4; i++) {
      const y = padding.top + (chartH / 4) * i;
      const val = fmtDe(maxVal * (4 - i) / 4, 1);
      yLines += `<line x1="${padding.left}" y1="${y}" x2="${width - padding.right}" y2="${y}" stroke="var(--divider-color)" stroke-dasharray="4"/>`;
      yLines += `<text x="${padding.left - 5}" y="${y + 4}" text-anchor="end" font-size="10" fill="var(--secondary-text-color)">${val}</text>`;
    }

    // Legend
    const lx = width - padding.right - 240;
    const ly = 14;
    const legend = `
      <rect x="${lx}" y="${ly - 8}" width="10" height="10" fill="#FF9800" rx="2"/>
      <text x="${lx + 14}" y="${ly}" font-size="11" fill="var(--primary-text-color)">Morgen (bis 08/2026)</text>
      <rect x="${lx + 135}" y="${ly - 8}" width="10" height="10" fill="#2196F3" rx="2"/>
      <text x="${lx + 149}" y="${ly}" font-size="11" fill="var(--primary-text-color)">Entladung ins Netz</text>`;

    const mobileStyle = `<style>@media (max-width: 600px) { text { font-size: 13px !important; } }</style>`;
    return `<div class="chart-card" style="margin-top:4px"><svg viewBox="0 0 ${width} ${height}" style="width:100%;height:auto;">${mobileStyle}${yLines}${bars}${legend}</svg></div>`;
  }

  _archivKarte() {
    const a = this._archivStatus;
    const mb = (bytes) => bytes >= 1048576
      ? `${fmtDe(bytes / 1048576, 1)} MB`
      : `${fmtDe(Math.max(bytes, 0) / 1024, 0)} kB`;
    let zeile;
    if (this._archivBusy || a === null) {
      zeile = "Zustand wird geholt…";
    } else if (a.fehler) {
      zeile = `Nicht abrufbar: ${this._escapeHtml(a.fehler)}`;
    } else if (!a.aktiv) {
      zeile = "Das Archiv läuft erst, wenn die Einrichtung abgeschlossen ist.";
    } else if (!a.eintraege) {
      zeile = "Noch keine Fahrpläne abgelegt — der erste kommt mit dem nächsten Rechenlauf.";
    } else {
      zeile = `<strong>${a.eintraege} Fahrpläne</strong> von ${this._escapeHtml(a.von || "?")}`
        + ` bis ${this._escapeHtml(a.bis || "?")} (${mb(a.bytes || 0)})`;
    }
    const knopf = a?.aktiv && a.eintraege && a.download_url
      ? `<a href="${this._escapeHtml(a.download_url)}" download
            style="display:inline-block;margin-top:10px;padding:8px 16px;border-radius:8px;background:var(--primary-color);color:#fff;text-decoration:none;font-size:14px">Archiv herunterladen</a>`
      : "";
    return `
      <div class="card" style="margin-bottom:16px">
        <h3 class="settings-karte-titel" style="margin:0 0 8px">Fahrplan-Archiv</h3>
        <div class="help-text" style="font-size:13px;color:var(--primary-text-color)">${zeile}</div>
        <div class="help-text" style="margin-top:6px">Alle ${a?.takt_minuten ?? 15} Minuten wird der gerechnete Fahrplan abgelegt, zusätzlich bei jeder deutlichen Planänderung. Aufbewahrt werden ${a?.aufbewahrung_tage ?? 7} Tage. Das Archiv enthält alle Eingangsgrößen des Modells, die Einstellungen und den gemessenen Verlauf — genug, um einen Lauf nachzurechnen und weiterzugeben.</div>
        ${knopf}
        <button class="btn-link" data-action="refresh-archiv" style="font-size:12px;padding:0;margin-top:8px;display:block" ${this._archivBusy ? "disabled" : ""}>Zustand aktualisieren</button>
      </div>`;
  }

  async _loadOemagTarif(refresh = false) {
    if (this._oemagBusy || !this._hass) {
      // Ohne hass gab es keinen Versuch — die Sperre darf nicht stehen bleiben.
      if (!this._hass) this._oemagRequested = false;
      return;
    }
    this._oemagBusy = true;
    if (refresh) this._render();
    try {
      this._oemagStatus = await this._hass.callWS({
        type: "eeg_optimizer/get_oemag_tarif",
        ...(refresh ? { refresh: true } : {}),
      });
    } catch (e) {
      console.warn("OeMAG-Tarif nicht abrufbar:", e);
      this._oemagStatus = { preis: null, fehler: e?.message || String(e) };
    } finally {
      this._oemagBusy = false;
      this._render();
    }
  }

  _handleAction(action, dataset) {
    switch (action) {
      case "start-wizard":
        this._startWizard();
        break;
      case "open-settings":
        this._settingsData = {...this._config};
        this._gem2Open = false;
        this._view = "settings";
        // Der Tarif steht in den Einstellungen; ohne Wert wäre der
        // Umschalter eine Behauptung.
        if (this._oemagStatus === null) this._loadOemagTarif();
        this._loadArchivStatus();
        if (this._settingsData.enable_peakshare !== false && this._peakshareCommunitiesCache.length === 0) {
          this._loadPeakShareCommunities();
        }
        this._render();
        break;
      case "restart-wizard":
        this._clearWizardProgress();
        // Direkt zu den Sensoren: wer die Einrichtung erneut durchläuft, hat
        // die Willkommensseite schon gesehen, und die Parameter stehen in den
        // Einstellungen. Alle Felder sind mit der Konfiguration vorbefüllt.
        this._wizardStep = 1;
        this._wizardData = {...WIZARD_DEFAULTS, ...this._config};
        this._gem2Open = false;
        this._view = "wizard";
        this._render();
        this._refreshStepData();
        break;
      case "save-settings":
        this._saveSettings();
        break;
      case "chart-range":
        this._chartBereichSetzen(dataset?.chart, dataset?.wert);
        break;
      case "toggle-settings-feature": {
        const feat = dataset?.feature;
        if (feat) { this._settingsData[feat] = dataset.on !== "1"; this._render(); }
        break;
      }
      case "toggle-feature": {
        // Gegenstueck fuer den Wizard. Fehlte bisher ganz: die Karte gab die
        // Aktion aus, _handleAction kannte sie nicht — die PeakShare-Karte
        // und die Einspeisegrenze liessen sich waehrend der Ersteinrichtung
        // nicht schalten, ohne Fehlermeldung.
        const feat = dataset?.feature;
        if (feat) { this._wizardData[feat] = dataset.on !== "1"; this._render(); }
        break;
      }
      case "back-to-dashboard":
        this._view = "dashboard";
        this._render();
        break;
      case "dismiss-toast":
        if (this._toastTimer) {
          clearTimeout(this._toastTimer);
          this._toastTimer = null;
        }
        this._toast = null;
        this._render();
        break;
      case "next-step":
        this._nextStep();
        break;
      case "prev-step":
        // Zum vorherigen sichtbaren Schritt (überspringt bedingt versteckte).
        this._wizardStep = this._seekVisibleStep(this._wizardStep, -1);
        this._saveWizardProgress();
        this._refreshStepData();
        break;
      case "finish-wizard":
        this._finishWizard();
        break;
      case "show-dialog":
        this._openGuideDialog(dataset?.dialog);
        break;
      case "close-dialog":
        this._showDialog = null;
        this._render();
        break;
      case "recheck-prerequisites":
        this._checkPrerequisites();
        // Auf dem Wechselrichter-Schritt auch die Sensor-Erkennung neu
        // anstoßen — "Erneut prüfen" soll den kompletten Zustand auffrischen,
        // nicht nur die Integrations-Badges.
        if (WIZARD_STEPS[this._wizardStep] === "Wechselrichter") {
          this._detectSensors();
        }
        break;
      case "open-gemeinschaft-2":
        // Der Block der zweiten Gemeinschaft ist zugeklappt, solange keine
        // gewählt ist — der Knopf öffnet ihn, gespeichert wird erst beim
        // Wählen eines Namens.
        this._gem2Open = true;
        this._render();
        break;
      case "redetect-sensors":
        this._detectSensors();
        break;
      case "toggle-sidebar":
        this._toggleHaSidebar();
        break;
      case "refresh-activity-log":
        this._activityLog = [];
        this._activityTotal = 0;
        this._activityHasMore = false;
        this._loadActivityLog();
        break;
      case "show-more-activity":
        this._loadMoreActivity();
        break;
      case "toggle-activity-show-all":
        this._activityShowAll = !this._activityShowAll;
        this._render();
        break;
      case "show-entity": {
        const entityId = dataset.entity;
        if (entityId) {
          const event = new Event("hass-more-info", { composed: true, bubbles: true });
          event.detail = { entityId };
          this._shadow.host.dispatchEvent(event);
        }
        break;
      }
      case "toggle-mode": {
        const modeState = this._readState(this._entityIds?.select || "select.eeg_energy_optimizer_optimizer");
        const currentMode = modeState ? modeState.state : "Aus";
        const newMode = currentMode === "Ein" ? "Aus" : "Ein";
        this._hass.callService("select", "select_option", {
          entity_id: this._entityIds?.select || "select.eeg_energy_optimizer_optimizer",
          option: newMode
        });
        break;
      }
      case "select-forecast": {
        const value = dataset?.value;
        if (value) {
          this._wizardData.forecast_source = value;
          this._applyForecastDefaults(value);
          this._render();
        }
        break;
      }
      case "select-inverter": {
        const invValue = dataset?.value;
        if (invValue && invValue !== this._wizardData.inverter_type) {
          this._wizardData.inverter_type = invValue;
          // Clear sensor fields so auto-detection can re-fill them
          const sensorKeys = [
            "pv_power_sensor", "battery_power_sensor", "grid_power_sensor",
            "battery_power_charge_sensor", "battery_power_discharge_sensor",
            "grid_power_export_sensor", "grid_power_import_sensor",
            "battery_soc_sensor", "battery_capacity_sensor", "huawei_device_id",
            "pv_power_sensor_2", "battery_power_sensor_2",
            "solax_remotecontrol_power_control", "solax_remotecontrol_active_power",
            "solax_remotecontrol_autorepeat_duration", "solax_remotecontrol_duration",
            "solax_remotecontrol_trigger", "solax_selfuse_discharge_min_soc",
          ];
          for (const k of sensorKeys) this._wizardData[k] = "";
          this._wizardData.huawei_device_ids = [];
          this._wizardData.huawei_battery_capacities = {};
          this._detectedSensors = null;
          this._detectSensors();
        }
        break;
      }
      case "set-cap-mode-card": {
        const mode = dataset?.value;
        if (mode) {
          this._capacityMode = mode;
          this._capacityModeUserSet = true;
          if (mode === "manual") {
            this._wizardData.battery_capacity_sensor = "";
          } else {
            this._wizardData.battery_capacity_kwh = "";
          }
          this._render();
        }
        break;
      }
      case "toggle-activity-log":
        this._activityLogOpen = !this._activityLogOpen;
        this._savePref("activity_log_open", this._activityLogOpen ? "1" : "0");
        this._render();
        break;
      case "toggle-profil":
        this._profilOpen = !this._profilOpen;
        this._render();
        break;
      case "set-profil-variant": {
        const variant = dataset?.variant;
        if (variant && variant !== this._profilChartVariant) {
          this._profilChartVariant = variant;
          this._savePref("profil_chart_variant", variant);
          this._render();
        }
        break;
      }
      case "set-status-view": {
        const variant = dataset?.variant;
        if (variant && variant !== this._statusViewVariant) {
          this._statusViewVariant = variant;
          this._savePref(this._narrow ? "status_view_variant_narrow" : "status_view_variant", variant);
          this._render();
        }
        break;
      }
      case "toggle-forecast":
        this._forecastOpen = !this._forecastOpen;
        this._savePref("forecast_open", this._forecastOpen ? "1" : "0");
        this._render();
        break;
      case "set-settings-tab": {
        const tab = dataset?.tab;
        if (tab && tab !== this._settingsTab) {
          this._settingsTab = tab;
          this._savePref("settings_tab", tab);
          this._render();
        }
        break;
      }
      case "refresh-consumption-profile": {
        if (this._profileRefreshing) break;
        this._profileRefreshing = true;
        this._profileRefreshResult = null;
        this._render();
        this._hass.callWS({ type: "eeg_optimizer/refresh_consumption_profile" })
          .then(res => {
            this._profileRefreshResult = res;
          })
          .catch(err => {
            console.error("refresh_consumption_profile failed:", err);
            this._profileRefreshResult = { success: false, error: String(err?.message || err) };
          })
          .finally(() => {
            this._profileRefreshing = false;
            this._render();
            // Auto-clear das Erfolg-/Fehlerbanner nach 6 Sekunden
            if (this._profileRefreshResult) {
              const tag = this._profileRefreshResult;
              setTimeout(() => {
                if (this._profileRefreshResult === tag) {
                  this._profileRefreshResult = null;
                  this._render();
                }
              }, 6000);
            }
          });
        break;
      }
      case "toggle-peakshare-data":
        this._peakshareDataOpen = !this._peakshareDataOpen;
        if (this._peakshareDataOpen && !this._peakshareDataLoaded) {
          this._loadPeakShareData();
        }
        this._render();
        break;
      case "refresh-control-state":
        this._loadControlState();
        break;
      case "toggle-schedule":
        this._scheduleOpen = !this._scheduleOpen;
        this._savePref(this._narrow ? "schedule_open_narrow" : "schedule_open",
                       this._scheduleOpen ? "1" : "0");
        if (this._scheduleOpen && !this._scheduleLoaded) {
          this._loadSchedule();
        }
        if (this._scheduleOpen) this._loadScheduleHistory();
        this._render();
        break;
      case "refresh-schedule":
        this._refreshSchedule();
        break;
      case "toggle-gewinn-details":
        this._gewinnDetailsOpen = !this._gewinnDetailsOpen;
        this._render();
        break;
      case "toggle-bilanz-details":
        this._bilanzDetailsOpen = !this._bilanzDetailsOpen;
        this._render();
        break;
      case "toggle-gewinn-karte":
        this._gewinnOpen = !this._gewinnOpen;
        this._savePref("gewinn_open", this._gewinnOpen ? "1" : "0");
        this._render();
        break;
      case "refresh-archiv":
        this._archivStatus = null;
        this._loadArchivStatus();
        this._render();
        break;
      case "tagesbilanz-jetzt":
        this._tagesbilanzJetzt();
        break;
      case "toggle-feedin-stats":
        this._feedinStatsOpen = !this._feedinStatsOpen;
        if (this._feedinStatsOpen && !this._feedinStatsLoaded) {
          this._loadFeedinStats();
        }
        this._render();
        break;
      case "feedin-period-week":
      case "feedin-period-month":
      case "feedin-period-year":
      case "feedin-period-total":
        this._feedinStatsPeriod = action.slice("feedin-period-".length);
        this._render();
        break;
      case "refresh-oemag":
        this._loadOemagTarif(true);
        break;
      case "refresh-spot":
        this._loadSpotStatus(true);
        break;
    }
  }

  /* ── Wizard lifecycle ─────────────────────────── */

  _startWizard() {
    this._view = "wizard";

    // Try restore from localStorage
    const saved = this._loadWizardProgress();
    if (saved) {
      this._wizardStep = saved.step;
      this._wizardData = { ...WIZARD_DEFAULTS, ...saved.data };
    } else if (this._config && this._config.setup_complete) {
      // Erneuter Durchlauf bei fertiger Einrichtung: direkt zu den Sensoren
      // (Schritt 1) — die Parameter stehen in den Einstellungen.
      this._wizardStep = 1;
      this._wizardData = { ...WIZARD_DEFAULTS };
      for (const key of Object.keys(WIZARD_DEFAULTS)) {
        if (this._config[key] !== undefined && this._config[key] !== null) {
          this._wizardData[key] = this._config[key];
        }
      }
    } else {
      this._wizardStep = 0;
      this._wizardData = { ...WIZARD_DEFAULTS };
    }

    this._prerequisites = null;
    this._detectedSensors = null;
    this._capacityMode = null;
    this._capacityModeUserSet = false;
    this._render();

    // Preload logos and prerequisites in background
    this._checkPrerequisites();
    // Beim Einstieg mitten im Wizard (localStorage-Restore oder erneuter
    // Durchlauf) die Daten des Einstiegs-Schritts nachladen — Entity-Picker
    // und Auto-Detection laufen sonst nur bei Step-Navigation. Ohne diesen
    // Aufruf bliebe ein restaurierter Wizard mit leeren Sensorfeldern
    // dauerhaft leer: Klick auf die bereits gewählte Wechselrichter-Karte
    // ist ein No-op und stößt keine neue Erkennung an.
    if (this._wizardStep >= 1) {
      this._refreshStepData();
    }
    const logos = [
      "https://brands.home-assistant.io/huawei_solar/logo.png",
      "https://brands.home-assistant.io/forecast_solar/logo.png",
      "https://brands.home-assistant.io/solcast_solar/logo.png",
    ];
    logos.forEach(src => { const img = new Image(); img.src = src; });
  }

  async _refreshStepData() {
    const name = WIZARD_STEPS[this._wizardStep];
    // Always refresh prerequisites on steps that show install status
    if (name === "Wechselrichter") {
      await this._checkPrerequisites();
      await this._ensureEntityPicker();
      await this._detectSensors();
      return; // _detectSensors calls _render
    }
    if (name === "PV-Prognose") {
      await this._checkPrerequisites();
      await this._ensureEntityPicker();
      this._render();
      return;
    }
    // Load entity picker for sensor steps
    if (name === "Batterie") {
      await this._ensureEntityPicker();
    }
    // Gemeinschaftsliste beim Betreten des Tarif-Schritts laden
    if (name === "Tarife & Gemeinschaft" && this._wizardData.enable_peakshare !== false && this._peakshareCommunitiesCache.length === 0) {
      this._loadPeakShareCommunities();
    }
    this._render();
  }

  // Ist ein Wizard-Schritt für die aktuelle Konfiguration sichtbar? Derzeit
  // sind alle Schritte sichtbar; der Mechanismus bleibt für künftige bedingte
  // Schritte (Navigation und Fortschrittsanzeige nutzen ihn bereits).
  _stepVisible(idx) {
    void idx;
    return true;
  }

  // Nächsten sichtbaren Schritt in Richtung dir (+1/-1) finden. Randschritte
  // (0 und letzter) sind immer sichtbar und begrenzen die Suche.
  _seekVisibleStep(from, dir) {
    let s = from + dir;
    while (s > 0 && s < WIZARD_STEPS.length - 1 && !this._stepVisible(s)) {
      s += dir;
    }
    return Math.max(0, Math.min(WIZARD_STEPS.length - 1, s));
  }

  async _nextStep() {
    if (this._navigating) return;
    this._navigating = true;
    try {
      this._clearValidationError();
      const valid = this._validateCurrentStep();
      if (!valid) return;

      // Async post-validation: read-only Modbus probe to confirm the
      // entered IP belongs to a Fronius inverter before letting the
      // user move past the inverter step.
      const aufWrSchritt = WIZARD_STEPS[this._wizardStep] === "Wechselrichter";
      if (
        aufWrSchritt &&
        this._wizardData.inverter_type === "fronius_gen24" &&
        this._wizardData.fronius_modbus_host
      ) {
        const ok = await this._probeFroniusConnection();
        if (!ok) return;
      }
      if (
        aufWrSchritt &&
        this._wizardData.inverter_type === "kostal_plenticore" &&
        this._wizardData.kostal_modbus_host
      ) {
        const ok = await this._probeKostalConnection();
        if (!ok) return;
      }
      if (
        aufWrSchritt &&
        this._wizardData.inverter_type === "sma_smart_energy" &&
        this._wizardData.sma_modbus_host
      ) {
        const ok = await this._probeSmaConnection();
        if (!ok) return;
      }

      // Zum nächsten sichtbaren Schritt (überspringt bedingt versteckte).
      this._wizardStep = this._seekVisibleStep(this._wizardStep, 1);
      this._saveWizardProgress();
      await this._refreshStepData();
    } finally {
      this._navigating = false;
    }
  }

  async _probeFroniusConnection() {
    const host = (this._wizardData.fronius_modbus_host || "").trim();
    const port = parseInt(this._wizardData.fronius_modbus_port, 10) || 502;
    this._froniusProbing = true;
    this._render();
    try {
      const res = await this._hass.callWS({
        type: "eeg_optimizer/probe_fronius",
        host,
        port,
      });
      if (!res || !res.success) {
        this._showValidationError(
          `Fronius unter ${host}:${port} nicht erreichbar — ${res?.error || "unbekannter Fehler"}`
        );
        return false;
      }
      if (!res.is_fronius) {
        this._showValidationError(
          `Gerät unter ${host}:${port} antwortet, ist aber kein Fronius (Hersteller: ${res.manufacturer || "unbekannt"}). Bitte IP prüfen.`
        );
        return false;
      }
      return true;
    } catch (err) {
      this._showValidationError(
        `Verbindungstest fehlgeschlagen: ${err?.message || err}`
      );
      return false;
    } finally {
      this._froniusProbing = false;
      this._render();
    }
  }

  async _probeKostalConnection() {
    const host = (this._wizardData.kostal_modbus_host || "").trim();
    const port = parseInt(this._wizardData.kostal_modbus_port, 10) || 1502;
    this._kostalProbing = true;
    this._render();
    try {
      const res = await this._hass.callWS({
        type: "eeg_optimizer/probe_kostal",
        host,
        port,
      });
      if (!res || !res.success) {
        this._showValidationError(
          `Kostal unter ${host}:${port} nicht erreichbar — ${res?.error || "unbekannter Fehler"}. Ist Modbus TCP im Kostal-Webserver aktiviert (Port 1502)?`
        );
        this._kostalProbeResult = null;
        return false;
      }
      if (!res.is_kostal) {
        this._showValidationError(
          `Gerät unter ${host}:${port} antwortet, ist aber kein Kostal Plenticore (Produkt: ${res.product || "unbekannt"}). Bitte IP prüfen.`
        );
        this._kostalProbeResult = null;
        return false;
      }
      // Batteriesteuerung noch nicht auf "Extern über Protokoll (Modbus TCP)"
      // umgestellt (Installateur-Schritt): NICHT blockieren — der Nutzer darf
      // die Einrichtung abschließen und den Installateur-Termin nachholen.
      // Der Status wird als Hinweis im Modbus-Karten-Bereich angezeigt.
      this._kostalProbeResult = res;
      // Die Kostal-REST-Integration liefert keinen Kapazitätssensor — die
      // Kapazität aus Modbus-Register 1068 vorbefüllen, sofern der Nutzer
      // noch keinen eigenen Wert/Sensor gesetzt hat (Default = 10).
      const cap = parseFloat(res.battery_capacity_kwh);
      if (
        cap > 0 &&
        !this._wizardData.battery_capacity_sensor &&
        (!this._wizardData.battery_capacity_kwh ||
          parseFloat(this._wizardData.battery_capacity_kwh) === 10)
      ) {
        this._wizardData.battery_capacity_kwh = cap;
      }
      return true;
    } catch (e) {
      this._showValidationError(
        `Kostal-Verbindungstest fehlgeschlagen: ${e?.message || e}`
      );
      this._kostalProbeResult = null;
      return false;
    } finally {
      this._kostalProbing = false;
      this._render();
    }
  }

  async _probeSmaConnection() {
    const host = (this._wizardData.sma_modbus_host || "").trim();
    const port = parseInt(this._wizardData.sma_modbus_port, 10) || 502;
    this._smaProbing = true;
    this._render();
    try {
      const res = await this._hass.callWS({
        type: "eeg_optimizer/probe_sma",
        host,
        port,
      });
      if (!res || !res.success) {
        this._showValidationError(
          `SMA unter ${host}:${port} nicht erreichbar — ${res?.error || "unbekannter Fehler"}. Ist der Modbus-TCP-Server im SMA-Webinterface aktiviert (Port 502)?`
        );
        this._smaProbeResult = null;
        return false;
      }
      if (!res.is_sma) {
        this._showValidationError(
          `Gerät unter ${host}:${port} antwortet, liefert aber keine gültige SMA-Seriennummer. Bitte IP prüfen.`
        );
        this._smaProbeResult = null;
        return false;
      }
      if (!res.has_battery) {
        this._showValidationError(
          `SMA-Gerät unter ${host}:${port} erkannt (Seriennr. ${res.serial}), aber es meldet keinen Batterie-Ladezustand. Ist das der Hybrid-/Batterie-Wechselrichter?`
        );
        this._smaProbeResult = null;
        return false;
      }
      // OpMod-Register (40236) nicht lesbar: NICHT blockieren — manche
      // Firmwares nutzen 41259 (Beta-Checkliste Punkt 2). Der Status wird
      // als Hinweis im Modbus-Karten-Bereich angezeigt.
      this._smaProbeResult = res;
      return true;
    } catch (e) {
      this._showValidationError(
        `SMA-Verbindungstest fehlgeschlagen: ${e?.message || e}`
      );
      this._smaProbeResult = null;
      return false;
    } finally {
      this._smaProbing = false;
      this._render();
    }
  }

  _validateCurrentStep() {
    switch (WIZARD_STEPS[this._wizardStep]) {
      case "Wechselrichter": {
        if (!this._wizardData.inverter_type) {
          this._showValidationError("Bitte wähle einen Wechselrichter-Typ aus.");
          return false;
        }
        const invType = this._wizardData.inverter_type;
        const invP = this._prerequisites;
        if (invType === "huawei_sun2000" && invP && !invP.huawei_solar) {
          this._showValidationError("Huawei Solar Integration muss zuerst installiert werden.");
          return false;
        }
        if (invType === "solax_gen4" && invP && !invP.solax_modbus) {
          this._showValidationError("SolaX Modbus Integration muss zuerst installiert werden.");
          return false;
        }
        if (invType === "solaredge_storedge" && invP && !invP.solaredge_modbus_multi) {
          this._showValidationError("SolarEdge Modbus Multi Integration muss zuerst installiert werden.");
          return false;
        }
        if (invType === "fronius_gen24" && invP && !invP.fronius) {
          this._showValidationError("Fronius Integration nicht gefunden. Diese wird f\u00fcr die Sensoren ben\u00f6tigt. Klicke auf 'Anleitung' f\u00fcr Hilfe.");
          return false;
        }
        if (invType === "fronius_gen24" && !this._wizardData.fronius_modbus_host) {
          this._showValidationError("Bitte gib die Modbus IP-Adresse des Fronius Wechselrichters ein.");
          return false;
        }
        if (invType === "kostal_plenticore" && invP && !invP.kostal_plenticore) {
          this._showValidationError("Kostal Plenticore Integration nicht gefunden. Diese wird für die Sensoren benötigt. Klicke auf 'Anleitung' für Hilfe.");
          return false;
        }
        if (invType === "kostal_plenticore" && !this._wizardData.kostal_modbus_host) {
          this._showValidationError("Bitte gib die Modbus IP-Adresse des Kostal Wechselrichters ein.");
          return false;
        }
        if (invType === "sma_smart_energy" && invP && !invP.sma) {
          this._showValidationError("SMA Solar Integration nicht gefunden. Diese wird für die Sensoren benötigt. Klicke auf 'Anleitung' für Hilfe.");
          return false;
        }
        if (invType === "sma_smart_energy" && !this._wizardData.sma_modbus_host) {
          this._showValidationError("Bitte gib die Modbus IP-Adresse des SMA Wechselrichters ein.");
          return false;
        }
        return true;
      }
      case "PV-Prognose": {
        if (!this._wizardData.forecast_source) {
          this._showValidationError("Bitte wähle eine Prognose-Quelle aus.");
          return false;
        }
        const fcSrc = this._wizardData.forecast_source;
        const fcP = this._prerequisites;
        if (fcSrc === "solcast_solar" && fcP && !fcP.solcast_solar) {
          this._showValidationError("Solcast Solar muss zuerst installiert werden. Klicke auf 'Anleitung' für Hilfe.");
          return false;
        }
        if (fcSrc === "forecast_solar" && fcP && !fcP.forecast_solar) {
          this._showValidationError("Forecast.Solar muss zuerst installiert werden. Klicke auf 'Anleitung' für Hilfe.");
          return false;
        }
        if (!this._wizardData.forecast_remaining_entity) {
          this._showValidationError("PV-Prognose verbleibend heute ist erforderlich.");
          return false;
        }
        if (!this._wizardData.forecast_tomorrow_entity) {
          this._showValidationError("PV-Prognose morgen ist erforderlich.");
          return false;
        }
        return true;
      }
      case "Batterie":
        // SolarEdge: SOC + Kapazität werden vom Driver kombiniert über alle
        // i1/i2/...-Inverter berechnet — Pflichtfelder entfallen, der Save-
        // Handler setzt die Combined-Sensor-IDs automatisch ein.
        if (this._wizardData.inverter_type === "solaredge_storedge") {
          return true;
        }
        // Huawei Master/Slave (≥2 Batterien): SOC läuft wie bei SolarEdge über
        // den Combined-Sensor (beim Abschluss gesetzt) — daher kein SOC-Feld.
        // Stattdessen die Einzelkapazitäten je Batterie prüfen.
        {
          const huaweiIds = this._wizardData.huawei_device_ids || [];
          if (this._wizardData.inverter_type === "huawei_sun2000" && huaweiIds.length >= 2) {
            const caps = this._wizardData.huawei_battery_capacities || {};
            const missing = huaweiIds.filter((id) => !(parseFloat(caps[id]) > 0));
            if (missing.length) {
              this._showValidationError("Bitte die Kapazität beider Batterien eintragen.");
              return false;
            }
            return true;
          }
        }
        if (!this._wizardData.battery_soc_sensor) {
          this._showValidationError("SOC-Sensor ist erforderlich.");
          return false;
        }
        if (
          !this._wizardData.battery_capacity_sensor &&
          !this._wizardData.battery_capacity_kwh
        ) {
          this._showValidationError(
            "Entweder Kapazitäts-Sensor oder manuelle Kapazität ist erforderlich."
          );
          return false;
        }
        return true;
      case "Anlage & Batterie": {
        // Leistungsdaten der Anlage: beide begrenzen im Modell die Summe aus
        // Einspeisung und Hauslast. Vorher wurde die AC-Grenze aus der
        // PV-Spitze geraten — an einer 10-kWp-Anlage mit 8-kW-Gerät plant das
        // Modell dann 2 kW, die nie ankommen.
        if (!(parseFloat(this._wizardData.inverter_ac_limit_kw) > 0)) {
          this._showValidationError("Bitte die AC-Grenzleistung des Wechselrichters eintragen.");
          return false;
        }
        if (!(parseFloat(this._wizardData.pv_peak_kwp) > 0)) {
          this._showValidationError("Bitte die PV-Spitzenleistung der Anlage eintragen.");
          return false;
        }
        if (this._wizardData.grid_export_limit_enabled
            && !(parseFloat(this._wizardData.grid_export_limit_kw) > 0)) {
          this._showValidationError("Bitte eine Einspeisegrenze größer als 0 kW angeben.");
          return false;
        }
        if (!(parseFloat(this._wizardData.discharge_power_kw) > 0)) {
          this._showValidationError("Bitte die Batterie-Leistungsgrenze eintragen.");
          return false;
        }
        return true;
      }
      case "Tarife & Gemeinschaft": {
        // Bei OeMAG oder Spot als Quelle gibt es kein Eingabefeld — dann
        // darf die Prüfung es auch nicht verlangen.
        if ((this._wizardData.schedule_feedin_source || "manual") === "manual"
            && !(parseFloat(this._wizardData.schedule_feedin_price) > 0)) {
          this._showValidationError("Bitte die Standardvergütung eintragen.");
          return false;
        }
        if (!(parseFloat(this._wizardData.schedule_consumption_price) > 0)) {
          this._showValidationError("Bitte den Bezugspreis eintragen.");
          return false;
        }
        // Dieselbe Regel wie beim Speichern der Einstellungen: der
        // Aufteilungsschlüssel ist vertraglich, über 100 % geht nicht.
        const anteile = anteilssummePct(this._wizardData);
        if (anteile > 100) {
          this._showValidationError(
            `Die Anteile der Gemeinschaften ergeben zusammen ${fmtDe(anteile, 0)} % — mehr als 100 % ist nicht möglich.`);
          return false;
        }
        return true;
      }
      default:
        return true;
    }
  }

  _showValidationError(msg) {
    this._showToast(msg, "error");
  }

  _clearValidationError() {
    if (this._toastTimer) {
      clearTimeout(this._toastTimer);
      this._toastTimer = null;
    }
    this._toast = null;
    this._render();
  }

  _showToast(msg, type = "error") {
    this._toast = { msg, type };
    if (this._toastTimer) clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => {
      this._toast = null;
      this._toastTimer = null;
      this._render();
    }, 7000);
    this._render();
  }

  async _finishWizard() {
    this._wizardLoading = true;
    this._render();

    try {
      this._wizardData.setup_complete = true;
      // SolarEdge: SOC + Kapazität laufen über die Driver-Combined-Sensoren.
      // Wir tragen die pinned IDs explizit ein, damit das Frontend (Energy-
      // Flow-Diagramm liest battery_soc_sensor direkt) und der Optimizer-
      // Snapshot konsistent denselben Wert sehen.
      if (this._wizardData.inverter_type === "solaredge_storedge") {
        this._wizardData.battery_soc_sensor = "sensor.eeg_energy_optimizer_combined_soc";
        this._wizardData.battery_capacity_sensor = "sensor.eeg_energy_optimizer_combined_capacity";
        // Manueller Capacity-Fallback wird nicht mehr genutzt — Driver liefert
        // die Summe direkt. Bewusst nicht gelöscht, damit der User seine
        // ursprüngliche Eingabe in der Config nachvollziehen kann.
      }
      // Huawei Master/Slave (≥2 Batterien): SOC + Kapazität ebenfalls über die
      // Driver-Combined-Sensoren — Dashboard und Optimizer sehen denselben
      // kapazitätsgewichteten Wert. Single-Huawei bleibt beim eigenen Sensor.
      if (
        this._wizardData.inverter_type === "huawei_sun2000" &&
        (this._wizardData.huawei_device_ids || []).length >= 2
      ) {
        this._wizardData.battery_soc_sensor = "sensor.eeg_energy_optimizer_combined_soc";
        this._wizardData.battery_capacity_sensor = "sensor.eeg_energy_optimizer_combined_capacity";
      }
      const saveData = { ...this._wizardData };
      delete saveData.consumption_sensor;
      await this._hass.callWS({
        type: "eeg_optimizer/save_config",
        config: saveData,
      });
      this._clearWizardProgress();
      this._setupComplete = true;
      this._config = { ...this._wizardData };
      this._view = "dashboard";
      this._wizardLoading = false;
      this._render();

      // Integration reloads after config save — poll until optimizer is ready
      this._waitForOptimizer();
    } catch (err) {
      console.error("Failed to save config:", err);
      this._wizardData.setup_complete = false;
      this._wizardLoading = false;
      this._render();
    }
  }

  _settingsPflichtLuecken() {
    // Dieselbe Prüfung wie im Wizard: Ohne AC-Grenzleistung und
    // PV-Spitzenleistung müsste das Modell raten, und der Ratewert begrenzt
    // die geplante Einspeisung. Was im Wizard Pflicht ist, darf man in den
    // Einstellungen nicht wieder leeren können.
    const d = this._settingsData || {};
    const fehlt = [];
    if (!(Number(d.inverter_ac_limit_kw) > 0)) fehlt.push("AC-Grenzleistung des Wechselrichters");
    if (!(Number(d.pv_peak_kwp) > 0)) fehlt.push("PV-Spitzenleistung");
    if (d.grid_export_limit_enabled && !(Number(d.grid_export_limit_kw) > 0)) {
      fehlt.push("Höhe der Einspeisegrenze");
    }
    if ((d.schedule_feedin_source || "manual") === "manual"
        && !(Number(d.schedule_feedin_price) > 0)) fehlt.push("Standardvergütung");
    if (!(Number(d.schedule_consumption_price) > 0)) fehlt.push("Bezugspreis");
    if (!(Number(d.discharge_power_kw) > 0)) fehlt.push("Batterie-Leistungsgrenze");
    // Zwei getrennte Nachtfenster: das der Standardvergütung braucht nur
    // deren Nachtsatz (und nur bei festem Wert — bei OeMAG und Spot wirkt
    // er nicht); das der Gemeinschaften nur deren Nachtsätze bei aktivem
    // EEG (das Backend ignoriert sie sonst komplett). Für die Gemeinschaften
    // genügt auch das Standard-Fenster — leer fällt das Backend darauf
    // zurück.
    if ((d.schedule_feedin_source || "manual") === "manual"
        && Number(d.schedule_feedin_price_night ?? 0) > 0
        && (!d.schedule_night_start || !d.schedule_night_end)) {
      fehlt.push("Nachtfenster der Standardvergütung");
    }
    const eegAktiv = d.enable_peakshare !== false;
    const gemeinschaftsNacht = eegAktiv && (
      Number(d.peakshare_price_night ?? 0) > 0
      || Number(d.peakshare_price_night_2 ?? 0) > 0);
    if (gemeinschaftsNacht
        && (!d.peakshare_night_start || !d.peakshare_night_end)
        && (!d.schedule_night_start || !d.schedule_night_end)) {
      fehlt.push("Nachtfenster der Gemeinschaften");
    }
    // Der Aufteilungsschlüssel ist eine vertragliche Größe: über 100 % kann er
    // nicht gehen. Darunter ist erlaubt — der Rest geht an den Energieversorger.
    const anteile = anteilssummePct(d);
    if (anteile > 100) {
      fehlt.push(`Anteile der Gemeinschaften (Summe ${fmtDe(anteile, 0)} % statt maximal 100 %)`);
    }
    return fehlt;
  }

  async _saveSettings() {
    try {
      // Re-read all settings inputs to catch values not yet captured by events
      for (const el of this._shadow.querySelectorAll("[data-field^='settings_']")) {
        const realField = el.dataset.field.replace("settings_", "");
        if (el.type === "checkbox") {
          this._settingsData[realField] = el.checked;
        } else if (el.type === "number") {
          // leseZahl, nicht parseFloat: Preisfelder stehen in Cent, die
          // Konfiguration haelt Euro. Ohne die Umrechnung landeten hier 8,2
          // als 8,20 EUR/kWh — dieser Pfad liest das DOM noch einmal
          // vollstaendig nach und traegt deshalb dasselbe Risiko.
          this._settingsData[realField] = leseZahl(el);
        } else {
          this._settingsData[realField] = el.value;
        }
      }
      // Erst nach dem Nachlesen prüfen — sonst zählt ein gerade getippter
      // Wert noch nicht mit.
      const luecken = this._settingsPflichtLuecken();
      if (luecken.length) {
        this._settingsFehler = luecken;
        this._render();
        return;
      }
      this._settingsFehler = null;

      const changed = {};
      const cfg = this._config || {};
      for (const [k, v] of Object.entries(this._settingsData)) {
        if (JSON.stringify(v) !== JSON.stringify(cfg[k])) changed[k] = v;
      }
      if (Object.keys(changed).length === 0) {
        this._view = "dashboard"; this._render(); return;
      }
      await this._hass.callWS({ type: "eeg_optimizer/save_config", config: changed });
      // Update local config immediately (no full reload anymore)
      this._config = {...this._config, ...changed};
      // Reload PeakShare data if community or enable_peakshare changed
      if ("peakshare_community" in changed || "peakshare_community_2" in changed
          || "enable_peakshare" in changed) {
        this._peakshareDataLoaded = false;
        this._peakshareData = null;
        if (this._peakshareDataOpen) this._loadPeakShareData();
      }
      this._view = "dashboard";
      this._render();
    } catch (err) {
      console.error("Settings save error:", err);
      alert("Fehler beim Speichern: " + err.message);
    }
  }

  /* ── localStorage persistence ─────────────────── */

  _saveWizardProgress() {
    localStorage.setItem(
      WIZARD_KEY,
      JSON.stringify({
        step: this._wizardStep,
        data: this._wizardData,
        ts: Date.now(),
      })
    );
  }

  _loadWizardProgress() {
    const raw = localStorage.getItem(WIZARD_KEY);
    if (!raw) return null;
    try {
      const state = JSON.parse(raw);
      if (Date.now() - state.ts > 86400000) {
        // 24h expiry
        localStorage.removeItem(WIZARD_KEY);
        return null;
      }
      return state;
    } catch {
      localStorage.removeItem(WIZARD_KEY);
      return null;
    }
  }

  _clearWizardProgress() {
    localStorage.removeItem(WIZARD_KEY);
  }

  _toggleHaSidebar() {
    // Fire the hass-toggle-menu event that HA's shell listens for.
    // This works on both desktop (toggle sidebar) and mobile (open drawer).
    const ev = new Event("hass-toggle-menu", { bubbles: true, composed: true });
    this.dispatchEvent(ev);
  }

  async _waitForOptimizer(attempt = 0) {
    // Poll config every 2s until setup_complete is reflected (max 15 attempts = 30s)
    if (attempt >= 15) {
      this._loadConfig();
      return;
    }
    try {
      const res = await this._hass.callWS({ type: "eeg_optimizer/get_config" });
      if (res?.config?.setup_complete) {
        await this._loadConfig();
        this._loadActivityLog();
        this._subscribeActivityEvents();
        return;
      }
    } catch (_) { /* integration still reloading */ }
    setTimeout(() => this._waitForOptimizer(attempt + 1), 2000);
  }

  async _loadActivityLog() {
    if (!this._hass) return;
    try {
      const result = await this._hass.callWS({
        type: "eeg_optimizer/get_activity_log",
        offset: 0,
        limit: 100,
      });
      this._activityLog = result?.entries || [];
      this._activityTotal = result?.total || 0;
      this._activityHasMore = result?.has_more || false;
      this._render();
    } catch (e) {
      // Silently ignore — log may not be available yet
    }
  }

  async _loadPeakShareCommunities() {
    if (!this._hass || this._peakshareCommunitiesLoading) return;
    this._peakshareCommunitiesLoading = true;
    try {
      const result = await this._hass.callWS({
        type: "eeg_optimizer/get_peakshare_communities",
      });
      this._peakshareCommunitiesCache = result?.communities || [];
      this._gemeinschaftenVorbelegen();
      this._render();
    } catch (e) {
      console.error("Failed to load PeakShare communities:", e);
    } finally {
      this._peakshareCommunitiesLoading = false;
    }
  }

  // Vorauswahl im WIZARD: Gemeinschaft 1 die BEG, Gemeinschaft 2 die erste
  // EEG der Liste. Die PeakShare-API liefert nur Namen und keinen Typ —
  // entschieden wird deshalb am Namen, und nur solange der Nutzer noch
  // nichts gewählt hat. In den Einstellungen (_settingsData) wird nie
  // vorbelegt: dort gilt, was gespeichert ist.
  _gemeinschaftenVorbelegen() {
    const liste = this._peakshareCommunitiesCache || [];
    if (!liste.length || !this._wizardData) return;
    const passt = (name, wort) => String(name).toUpperCase().includes(wort);
    const d = this._wizardData;

    // Gemeinschaft 1: der vorbelegte Platzhalter "BEG" steht selten so in
    // der Liste — durch den echten Namen ersetzen, sonst zeigt das
    // Auswahlfeld einen Eintrag, den es gar nicht gibt.
    if (!d.peakshare_community || !liste.includes(d.peakshare_community)) {
      const beg = liste.find(n => passt(n, "BEG"));
      if (beg) d.peakshare_community = beg;
    }
    if (!d.peakshare_community_2) {
      const eeg = liste.find(
        n => passt(n, "EEG") && n !== d.peakshare_community
      );
      if (eeg) d.peakshare_community_2 = eeg;
    }
  }

  async _loadPeakShareData() {
    if (!this._hass) return;
    try {
      const result = await this._hass.callWS({
        type: "eeg_optimizer/get_peakshare_data",
      });
      this._peakshareData = result;
      this._peakshareDataLoaded = true;
      this._render();
    } catch (e) {
      console.error("Failed to load PeakShare data:", e);
    }
  }


  async _loadSchedule() {
    if (!this._hass) return;
    try {
      this._scheduleData = await this._hass.callWS({ type: "eeg_optimizer/get_schedule" });
      this._scheduleLoaded = true;
      this._render();
    } catch (e) {
      console.error("Fahrplan konnte nicht geladen werden:", e);
    }
  }

  async _loadControlState() {
    if (!this._hass) return;
    try {
      this._controlState = await this._hass.callWS({
        type: "eeg_optimizer/get_control_state",
      });
    } catch (e) {
      console.error("Steuerwerte konnten nicht geladen werden:", e);
      this._controlState = { error: e.message || String(e), rows: [] };
    }
    this._render();
  }

  async _refreshSchedule() {
    if (!this._hass || this._scheduleBusy) return;
    this._scheduleBusy = true;
    this._render();
    try {
      this._scheduleData = await this._hass.callWS({ type: "eeg_optimizer/refresh_schedule" });
      this._scheduleLoaded = true;
    } catch (e) {
      console.error("Fahrplan-Neuberechnung fehlgeschlagen:", e);
      this._scheduleData = { available: false, error: e.message || String(e) };
    } finally {
      this._scheduleBusy = false;
      this._render();
    }
  }

  /* ── Ist-Verlauf aus dem Recorder ───────────────── */

  // Welche Entitäten der Verlauf braucht. Die IDs kommen aus der Registry
  // (this._entityIds) bzw. der Konfiguration — Home Assistant bildet die
  // entity_id aus dem ANZEIGENAMEN, sie ist also nicht ratbar.
  _schedHistEntities() {
    const ids = {};
    const put = (feld, key, muster) => {
      const eid = this._entityIds?.[key] || muster;
      if (eid) ids[feld] = eid;
    };
    put("pv", "pv_leistung", "sensor.eeg_energy_optimizer_pv_leistung");
    put("grid", "netzleistung", "sensor.eeg_energy_optimizer_netzleistung");
    put("bat", "batterieleistung", "sensor.eeg_energy_optimizer_batterieleistung");
    put("planBat", "fahrplan_batterieleistung", "sensor.eeg_energy_optimizer_fahrplan_batterieleistung");
    put("planGrid", "fahrplan_netzleistung", "sensor.eeg_energy_optimizer_fahrplan_netzleistung");
    const soc = this._config?.battery_soc_sensor;
    if (soc) ids.soc = soc;
    return ids;
  }

  _schedHistStart(range, endMs) {
    if (range === "yesterday") {
      const dt = new Date(endMs);
      dt.setHours(0, 0, 0, 0);
      dt.setDate(dt.getDate() - 1);
      return dt.getTime();
    }
    return endMs - 12 * 3600000;   // Standard-Rückblick
  }

  async _loadScheduleHistory(force = false) {
    const range = this._schedHistRange || "off";
    if (range === "off" || !this._hass) return;
    if (this._schedHistBusy) return;
    if (!force && this._schedHist?.range === range
        && Date.now() - this._schedHist.loadedAt < 60000) return;

    this._schedHistBusy = true;
    this._schedHistError = null;
    this._render();

    try {
      const endMs = Date.now();
      const startMs = this._schedHistStart(range, endMs);
      const ids = this._schedHistEntities();
      const resMs = Math.max(1, Number(this._scheduleData?.time_res_min) || 15) * 60000;
      // minimal_response + no_attributes: 48 h Leistungsdaten sind sonst ein
      // Vielfaches an Nutzlast, gebraucht wird nur Wert + Zeitstempel.
      const raw = await this._hass.callWS({
        type: "history/history_during_period",
        start_time: new Date(startMs).toISOString(),
        end_time: new Date(endMs).toISOString(),
        entity_ids: Object.values(ids),
        minimal_response: true,
        no_attributes: true,
      });
      this._schedHist = {
        range, startMs, endMs,
        loadedAt: Date.now(),
        rows: this._bucketHistory(raw || {}, ids, startMs, endMs, resMs),
      };
    } catch (e) {
      console.error("Verlauf konnte nicht geladen werden:", e);
      this._schedHist = null;
      this._schedHistError = e?.message || String(e);
    } finally {
      this._schedHistBusy = false;
      this._render();
      // Wurde während der Abfrage weitergeschaltet, gleich das Richtige
      // holen — sonst bliebe das Diagramm bis zum nächsten Zyklus leer.
      if ((this._schedHistRange || "off") !== range) this._loadScheduleHistory();
    }
  }

  // Recorder-Zustände auf das Slot-Raster des Fahrplans mitteln.
  // Zeitgewichtet, nicht als einfacher Mittelwert: die Sensoren schreiben
  // unregelmäßig (nur bei Änderung), ein Wert gilt bis zum nächsten. Ohne
  // Gewichtung zöge eine Minute mit vielen kleinen Änderungen den Mittelwert
  // eines Viertelstundenblocks nach sich.
  _bucketHistory(raw, ids, startMs, endMs, resMs) {
    const feldVon = {};
    for (const [feld, eid] of Object.entries(ids)) feldVon[eid] = feld;
    const b0 = Math.floor(startMs / resMs) * resMs;
    const eimer = new Map();   // Index -> {feld: {summe, dauer}}

    const eintragen = (idx, feld, wert, dauer) => {
      let e = eimer.get(idx);
      if (!e) { e = {}; eimer.set(idx, e); }
      const f = e[feld] || (e[feld] = { summe: 0, dauer: 0 });
      f.summe += wert * dauer;
      f.dauer += dauer;
    };

    for (const [eid, zustaende] of Object.entries(raw)) {
      const feld = feldVon[eid];
      if (!feld || !Array.isArray(zustaende)) continue;
      const pkte = [];
      for (const z of zustaende) {
        const rohWert = z?.s != null ? z.s : z?.state;
        const wert = parseFloat(rohWert);
        const ts = z?.lu != null
          ? Number(z.lu) * 1000
          : Date.parse(z?.last_updated || z?.last_changed || "");
        if (!isFinite(ts)) continue;
        pkte.push({ ts, wert: isFinite(wert) ? wert : null });
      }
      pkte.sort((a, b) => a.ts - b.ts);
      for (let i = 0; i < pkte.length; i++) {
        if (pkte[i].wert == null) continue;      // unavailable/unknown
        const von = Math.max(pkte[i].ts, startMs);
        const bis = Math.min(i + 1 < pkte.length ? pkte[i + 1].ts : endMs, endMs);
        if (bis <= von) continue;
        let t = von;
        while (t < bis) {
          const idx = Math.floor((t - b0) / resMs);
          const ende = Math.min(bis, b0 + (idx + 1) * resMs);
          eintragen(idx, feld, pkte[i].wert, ende - t);
          t = ende;
        }
      }
    }

    const mittel = (e, feld) => {
      const f = e[feld];
      return f && f.dauer > 0 ? f.summe / f.dauer : null;
    };
    const zeilen = [];
    for (const [idx, e] of [...eimer.entries()].sort((a, b) => a[0] - b[0])) {
      const pv = mittel(e, "pv");
      const grid = mittel(e, "grid");
      // Kein Drehen: Sensoren und Diagramm zählen beide positiv = LADEN.
      const bat = mittel(e, "bat");
      const planBat = mittel(e, "planBat");
      // Hausverbrauch braucht keine eigene Abfrage: er folgt aus der
      // Leistungsbilanz — PV minus Batterie minus Netz, genau die Identität
      // aus der Sensordefinition (Netz positiv = Einspeisung).
      const cons = (pv == null || bat == null || grid == null) ? null : pv - bat - grid;
      zeilen.push({
        t: b0 + idx * resMs,
        pv, grid, bat, cons,
        soc: mittel(e, "soc"),
        planBat,
        planGrid: mittel(e, "planGrid"),
      });
    }
    return zeilen;
  }

  _showSchedTooltip(hit, ev) {
    // Der Tooltip hängt bewusst NICHT im scrollenden Container: dort schnitt
    // ihn `overflow-x:auto` oben ab (setzt overflow-y implizit ebenfalls auf
    // auto), er wurde gesetzt und war trotzdem nie zu sehen.
    const wrapper = hit.closest(".sched-chart-card");
    const tt = wrapper?.querySelector(".sched-tooltip");
    if (!tt) return;
    const d = hit.dataset;

    if (tt.dataset.slot !== d.key) {
      const chip = (colour) => colour
        ? `<span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${colour};margin-right:5px"></span>`
        : "";
      if (d.past === "1") {
        // Links von „jetzt": gemessener Ist-Wert neben dem Wert, der damals
        // geplant war. Genau dafür stehen die Fahrplan-Sensoren im Recorder.
        const kopf = (t) => `<span style="color:var(--secondary-text-color);font-size:11px;text-align:right">${t}</span>`;
        const zelle = (v, stark) => `<span style="text-align:right${stark ? ";font-weight:600" : ";color:var(--secondary-text-color)"}">${v}</span>`;
        const zeile = (colour, label, ist, plan) =>
          `<span style="color:var(--secondary-text-color)">${chip(colour)}${label}</span>`
          + zelle(ist, true) + zelle(plan, false);
        tt.innerHTML =
          `<div style="font-weight:600;margin-bottom:4px">${d.time}</div>`
          + `<div style="display:grid;grid-template-columns:auto auto auto;gap:2px 12px;align-items:baseline">`
          + `<span></span>${kopf("Ist")}${kopf("geplant")}`
          + zeile("#fbc02d", "PV", d.ipv, d.ppv)
          + zeile("#616161", "Verbrauch", d.icons, d.pcons)
          + zeile("#43a047", "Netz", d.igrid, d.pgrid)
          + zeile("#1e88e5", "Batterie", d.ibat, d.pbat)
          + zeile("#7cb342", "Ladestand", d.isoc, d.psoc)
          + `</div>`
          + `<div style="margin-top:5px;color:var(--secondary-text-color);font-size:10.5px">kW bzw. %  &middot;  Netz + = Einspeisung, Batterie + = laden</div>`;
      } else {
        const line = (label, value, unit, colour) =>
          `<div style="display:flex;gap:8px;justify-content:space-between">
             <span style="color:var(--secondary-text-color)">${chip(colour)}${label}</span>
             <strong>${value} ${unit}</strong>
           </div>`;
        const bat = parseFloat(String(d.pbat).replace(",", "."));
        const batText = !isFinite(bat)
          ? "---"
          : Math.abs(bat) < 0.005
            ? "halten"
            : `${bat > 0 ? "laden" : "entladen"} ${fmtDe(Math.abs(bat), 2)} kW`;
        tt.innerHTML =
          `<div style="font-weight:600;margin-bottom:4px">${d.time}</div>`
          + line("PV", d.ppv, "kW", "#fbc02d")
          + line("Verbrauch", d.pcons, "kW", "#616161")
          + line("Netz", d.pgrid, "kW", "#43a047")
          + line("Batterie", batText, "", bat > 0 ? "#1e88e5" : "#ef6c00")
          + line("Ladestand", d.psoc, "%", "#7cb342");
      }
      if (d.preis != null) {
        tt.innerHTML +=
          `<div style="display:flex;gap:8px;justify-content:space-between;margin-top:2px">
             <span style="color:var(--secondary-text-color)">${chip("#43a047")}Einspeisepreis</span>
             <strong>${d.preis} ct/kWh</strong>
           </div>`;
      }
      if (d.eegj) {
        let serien = [];
        try { serien = JSON.parse(d.eegj); } catch (e) { serien = []; }
        for (const e of serien) {
          tt.innerHTML +=
            `<div style="display:flex;gap:8px;justify-content:space-between;margin-top:2px">
               <span style="color:var(--secondary-text-color)">${chip(e.f)}${e.u ? "Überschuss" : "Bedarf"} ${this._escapeHtml(e.n)}</span>
               <strong>${e.v} kWh</strong>
             </div>`;
        }
      }
      tt.dataset.slot = d.key;
    }

    // Fadenkreuz durch beide Felder: zeigt, welcher Zeitpunkt gelesen wird.
    const svg = hit.ownerSVGElement;
    const cursor = svg?.querySelector(".sched-cursor");
    if (cursor && d.cx) {
      cursor.setAttribute("x1", d.cx);
      cursor.setAttribute("x2", d.cx);
      cursor.style.visibility = "visible";
    }
    const dot = svg?.querySelector(".sched-cursor-soc");
    if (dot) {
      if (d.cx && d.socy) {
        dot.setAttribute("cx", d.cx);
        dot.setAttribute("cy", d.socy);
        dot.style.visibility = "visible";
      } else {
        dot.style.visibility = "hidden";
      }
    }

    tt.style.display = "block";
    const r = hit.getBoundingClientRect();
    const wr = wrapper.getBoundingClientRect();
    const tw = tt.offsetWidth || 0;
    const th = tt.offsetHeight || 0;
    // Waagrecht am Slot, aber innerhalb der Karte gehalten.
    const cx = r.left - wr.left + r.width / 2;
    const minX = tw / 2 + 4;
    const maxX = Math.max(minX, wr.width - tw / 2 - 4);
    tt.style.left = `${Math.min(Math.max(cx, minX), maxX)}px`;
    // Senkrecht am Zeiger; oben kein Platz -> unter den Zeiger klappen.
    const cy = (ev && ev.clientY != null ? ev.clientY : r.top) - wr.top;
    tt.style.top = `${cy - th - 12 < 0 ? cy + th + 16 : cy - 12}px`;
  }

  _renderSchedule() {
    const d = this._scheduleData;
    if (!d) {
      return `<p style="color:var(--secondary-text-color);font-size:14px">Lade Optimierungsplan…</p>`;
    }
    if (!d.available) {
      return `<div style="font-size:14px">
        <p style="color:var(--secondary-text-color);margin:8px 0">
          Noch kein Optimierungsplan: ${d.error ? this._escapeHtml(d.error) : "unbekannter Grund"}
        </p>
        <button class="btn" data-action="refresh-schedule" ${this._scheduleBusy ? "disabled" : ""}>
          ${this._scheduleBusy ? "Rechne..." : "Jetzt rechnen"}
        </button>
      </div>`;
    }

    const alleSlots = d.slots || [];
    if (!alleSlots.length) return `<p style="font-size:14px">Der Optimierungsplan ist leer.</p>`;
    // Gezeigt wird nur der gewählte Ausschnitt des Plans. Gerechnet wird
    // weiterhin über den vollen Horizont — was hinten wegfällt, fehlt der
    // Optimierung nicht, es steht nur nicht im Bild.
    // Der Ausschnitt gilt auf jedem Gerät — 24/36/48 h sind auch am Handy
    // wählbar. Bis 1.5.21 war er dort fest auf 24 h gedeckelt, weil längere
    // Spannen querscrollen mussten; seit die Zeichenbreite der Kartenbreite
    // folgt (unten) passt jede Spanne ins Bild. Der Preis ist die zeitliche
    // Auflösung: bei 48 h auf 300 px fasst `bucket` mehrere Slots zu einem
    // Balken zusammen, sonst wären es Haarlinien. `narrow` setzt HA.
    const schmal = !!this._narrow;
    const planStunden = Number(this._schedPlanRange) || 48;
    const planEnde = new Date(alleSlots[0].t).getTime() + planStunden * 3600000;
    const slots = alleSlots.filter(s => new Date(s.t).getTime() <= planEnde);
    // Referenz „ohne Optimierung" (Standardbetrieb, siehe schedule.py):
    // derselbe Ausschnitt, dieselben Achsen — nur so sind die Bilder
    // vergleichbar.
    const refSlots = (d.referenz_slots || []).filter(
      s => new Date(s.t).getTime() <= planEnde);

    // --- Zeitachse ------------------------------------------------------
    // Bis 1.5.11 rechnete x() mit dem Slot-INDEX. Mit dem gemessenen Verlauf
    // geht das nicht mehr: Vergangenheit und Plan haben unterschiedlich viele
    // Punkte und unterschiedliche Raster. x() ist seither eine echte
    // Zeitachse in Millisekunden — vom Beginn des Rückblicks bis zum Ende
    // des letzten Slots.
    const resMs = Math.max(1, Number(d.time_res_min) || 15) * 60000;
    const slotT = slots.map(s => new Date(s.t).getTime());
    // Batterie im Diagramm: positiv = LADEN, negativ = entladen. Haralds
    // Modell zählt umgekehrt (positiv = entladen); gedreht wird genau hier,
    // an der Anzeigegrenze. So zeigen Diagramm, Tooltip und die Sensoren
    // „Batterieleistung" / „Fahrplan Batterieleistung" dasselbe Vorzeichen —
    // vorher widersprach die Karte den eigenen Sensoren.
    const slotBat = slots.map(s => (s.battery_p == null ? null : -s.battery_p));
    const histRange = this._schedHistRange || "off";
    const hist = (histRange !== "off" && this._schedHist?.range === histRange)
      ? this._schedHist : null;
    const rows = hist?.rows || [];
    const nowMs = Date.now();
    const t0 = hist ? Math.min(hist.startMs, slotT[0]) : slotT[0];
    const t1 = Math.max(slotT[slotT.length - 1] + resMs, hist ? hist.endMs : 0);
    const span = Math.max(resMs, t1 - t0);

    // --- Skalierung -----------------------------------------------------
    // Ein SVG, zwei Felder: oben die Leistungen (kW, Null in der Mitte) und
    // der Gemeinschaftsbedarf (kWh/h, rechte Achse), unten der Ladestand (%).
    // Getrennte Wertachsen, EINE Zeitachse — drei Einheiten in einem
    // Achsensystem waeren unlesbar, zwei getrennte SVGs koennen beim
    // Querscrollen auseinanderlaufen und teilen keinen Zeiger.
    // Zeichenbreite = gemessene Anzeigebreite der Karte (siehe _cw()). Damit
    // ist eine SVG-Einheit genau ein Pixel: `font-size="10"` sind 10 px, auf
    // jedem Gerät. Vorher folgte die Breite der Zeitspanne (17,5 px/h) und
    // die viewBox stauchte alles auf die Kartenbreite — am Handy landeten die
    // Achsen bei 6–8 px, am Desktop wurde ein 24-h-Ausschnitt auf das
    // Doppelte gestreckt. Querscrollen entfällt dadurch ganz.
    const W = this._cw("sched");
    // EEG-Bedarfskurve (PeakShare) — Kontext hinter dem Fahrplan, eigene
    // rechte Achse: die Gemeinschaft rechnet in kWh je Stunde, das hat mit
    // der kW-Skala der eigenen Anlage nichts zu tun.
    // Beide Gemeinschaften: der Fahrplan rechnet mit beiden Saldokurven,
    // also stehen auch beide zur Wahl (Pillen über dem Diagramm).
    const rohSerien = this._peakshareData?.communities?.length
      ? this._peakshareData.communities
      : (this._peakshareData?.intervals
        ? [{ name: this._peakshareData.community || "Gemeinschaft",
             intervals: this._peakshareData.intervals }]
        : []);
    // Eine Farbe je Gemeinschaft, die Richtung trägt die Aussage: Bedarf
    // über der Nulllinie, Überschuss darunter. So bleibt auf einen Blick
    // erkennbar, welche Gemeinschaft gemeint ist, ohne dass sich die Farben
    // verdoppeln.
    const eegFarben = ["#8e24aa", "#00897b"];
    let eegSerien = [];
    if (this._config?.enable_peakshare !== false) {
      eegSerien = rohSerien.map((serie, i) => {
        // Viertelstundenraster wie die API (V2): 192 Intervalle über 48 h.
        const jeViertel = new Map();
        for (const h of (serie?.intervals || [])) {
          if (!h?.timestamp || h.saldoKwh == null) continue;
          const t = new Date(h.timestamp).getTime();
          if (isNaN(t)) continue;
          const saldo = Number(h.saldoKwh);
          // Betrag zeichnen, Vorzeichen merken: positiv ist Bedarf,
          // negativ Überschuss (siehe peakshare.py).
          jeViertel.set(Math.floor(t / 900000), {
            v: Math.abs(saldo),
            ueber: saldo < 0,
          });
        }
        // Punkte im dargestellten Zeitraum — je Viertelstunde, nicht je
        // Slot: die Kurve reicht damit auch in den Rückblick hinein.
        const punkte = [];
        for (const [viertel, eintrag] of jeViertel) {
          const t = viertel * 900000;
          if (t >= t0 - 900000 && t <= t1) {
            punkte.push({ t, v: eintrag.v, ueber: eintrag.ueber });
          }
        }
        punkte.sort((a, b) => a.t - b.t);
        return { name: serie?.name || `Gemeinschaft ${i + 1}`,
                 farbe: eegFarben[i % eegFarben.length],
                 punkte, jeViertel };
      }).filter(s => s.punkte.some(p => p.v > 0));
    }
    // Gemeinsame Skala über beide Kurven — zwei Gemeinschaften sind nur
    // vergleichbar, wenn sie dieselbe Achse benutzen. Nach oben und unten
    // wird aber GETRENNT skaliert: die Nulllinie liegt durch das
    // Leistungsfeld fest, und der Überschuss einer PV-starken Gemeinschaft
    // ist ein Vielfaches ihres Bedarfs (gemessen 321 gegen 45 kWh). Auf
    // einer gemeinsamen Skala bliebe vom Bedarf ein Strich von wenigen
    // Pixeln — und der Bedarf ist die Größe, um die es geht. Die
    // Achsenbeschriftung nennt beide Enden, damit der Maßstabswechsel
    // sichtbar ist.
    const sichtbareSerien = eegSerien;
    const eegMaxBedarf = Math.max(
      0, ...sichtbareSerien.flatMap(s => s.punkte.filter(p => !p.ueber).map(p => p.v)));
    const eegMaxUeber = Math.max(
      0, ...sichtbareSerien.flatMap(s => s.punkte.filter(p => p.ueber).map(p => p.v)));
    const hasEeg = eegMaxBedarf > 0 || eegMaxUeber > 0;

    // Schriftgrößen sind jetzt echte Pixel. Am Desktop etwas größer, weil
    // die alte viewBox dort nach oben skalierte und der Eindruck bleibt.
    const fsAchse = schmal ? 10 : 12;
    const fsKlein = schmal ? 10 : 11;
    const padL = schmal ? 30 : 44, padT = 14;
    // Einspeisepreis bei Quelle „Spotpreis": als Linie IM Leistungsfeld mit
    // eigener rechter ct-Achse — spart das separate Feld (Nutzerwunsch).
    // Sind EEG-Kurven sichtbar, gehört die rechte Achse dem Preis; deren
    // Salden stehen weiter exakt im Tooltip. Sonst bleibt das schmale Band.
    const preise = slots.map(sl => sl.feedin_price).filter(v => v != null);
    const pMin = preise.length ? Math.min(...preise) : 0;
    const pMax = preise.length ? Math.max(...preise) : 0;
    const preisImFeld = (this._config?.schedule_feedin_source || "manual") === "spot"
      && preise.length > 0 && pMax - pMin > 0.0005;
    const padR = (hasEeg || preisImFeld) ? (schmal ? 34 : 46) : (schmal ? 8 : 16);
    const plotW = W - padL - padR;
    const plotH = schmal ? 150 : 200;        // Leistungsfeld
    const socTop = padT + plotH + 34;        // Luecke fuer Preisband und Ladestand-Beschriftung
    const socH = schmal ? 52 : 70;           // Ladestandsfeld
    const socBottom = socTop + socH;
    const H = socBottom + (schmal ? 32 : 35); // Zeitachse + Datumszeile

    // Die Leistungsachse ist absichtlich unsymmetrisch: unter der Nulllinie
    // steht nur, was wirklich negativ wird (Netzbezug, Laden), und das ist
    // meist deutlich weniger als die Einspeisespitzen darüber. Die gewonnene
    // Höhe geht an den positiven Bereich — und an den Gemeinschaftsbedarf,
    // der auf der Nulllinie aufsetzt statt am unteren Rand.
    let maxPos = 0.5, maxNeg = 0.5;
    const messen = (v) => {
      if (v == null || !isFinite(v)) return;
      if (v > maxPos) maxPos = v;
      if (-v > maxNeg) maxNeg = -v;
    };
    slots.forEach((s, i) => {
      for (const key of ["PV", "consumption", "grid_p"]) messen(s[key] ?? 0);
      messen(slotBat[i] ?? 0);
    });
    // Der Ist-Verlauf gehört auf dieselbe Achse — sonst wird er abgeschnitten.
    for (const r of rows) {
      for (const key of ["pv", "cons", "bat", "grid", "planBat", "planGrid"]) messen(r[key]);
    }
    // Die Referenz „ohne Optimierung" auch: beide Diagramme teilen die
    // Skala, sonst zeigten sie gleiche Kurven in verschiedenem Maßstab.
    for (const s of refSlots) {
      messen(s.grid_p ?? 0);
      messen(s.battery_p == null ? null : -s.battery_p);
    }
    const halbeKw = (v) => Math.ceil(v * 1.05 * 2) / 2;   // auf halbe kW aufrunden
    maxPos = halbeKw(maxPos);
    maxNeg = halbeKw(maxNeg);
    // Der negative Teil bekommt höchstens die Hälfte der Fläche, damit ein
    // einzelner Bezugsausschlag das Diagramm nicht kippt.
    const negAnteil = Math.min(0.5, maxNeg / (maxPos + maxNeg));
    const zeroY = padT + plotH * (1 - negAnteil);

    const x = (t) => padL + (plotW * (t - t0)) / span;
    const xw = (ms) => (plotW * ms) / span;
    const y = (kw) => kw >= 0
      ? zeroY - (kw / maxPos) * (zeroY - padT)
      : zeroY + (-kw / maxNeg) * (padT + plotH - zeroY);

    const path = (key) => {
      let out = "";
      slots.forEach((s, i) => {
        const v = s[key];
        if (v == null) return;
        out += `${out ? "L" : "M"}${x(slotT[i]).toFixed(1)},${y(v).toFixed(1)}`;
      });
      return out;
    };

    // Ist-Verlauf: dieselbe Farbe wie der Plan, aber dünn und halbtransparent.
    // Lücken im Recorder (Neustart, Sensorausfall) brechen die Linie, statt
    // quer durch das Diagramm zu ziehen.
    const histPath = (key, yFn) => {
      let out = "", vorher = null;
      for (const r of rows) {
        const v = r[key];
        if (v == null) { vorher = null; continue; }
        const cmd = (out && vorher != null && r.t - vorher <= resMs * 2.5) ? "L" : "M";
        out += `${cmd}${x(r.t).toFixed(1)},${yFn(v).toFixed(1)}`;
        vorher = r.t;
      }
      return out;
    };

    // Batterie als Balken um die Nulllinie: positiv = laden.
    // Ein Slot ist bei 48 h auf ~300 px nur 1,3 px breit — als Balken eine
    // Haarlinie. Deshalb werden so viele Slots zu einem Balken gebündelt,
    // dass er gut 4 px misst; die Kurven bleiben in voller Auflösung.
    const slotPx = xw(resMs);
    const bucket = Math.max(1, Math.ceil(4.5 / Math.max(0.1, slotPx)));
    const bucketMs = bucket * resMs;
    const mittel = (werte) => {
      const gute = werte.filter(v => v != null && isFinite(v));
      return gute.length ? gute.reduce((a, b) => a + b, 0) / gute.length : null;
    };
    const barW = Math.max(1, xw(bucketMs) - 0.6);
    const balken = (t, v, deckkraft) => {
      if (v == null || Math.abs(v) < 0.001) return "";
      const yTop = v > 0 ? y(v) : y(0);
      const h = Math.abs(y(v) - y(0));
      const colour = v > 0 ? "#1e88e5" : "#ef6c00";   // laden / entladen
      return `<rect x="${(x(t) - barW / 2).toFixed(1)}" y="${yTop.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(0.6, h).toFixed(1)}" fill="${colour}" fill-opacity="${deckkraft}"/>`;
    };
    let bars = "";
    for (let i = 0; i < slots.length; i += bucket) {
      const v = mittel(slotBat.slice(i, i + bucket));
      bars += balken(slotT[i] + (bucketMs - resMs) / 2, v ?? 0, "0.55");
    }
    // Was im Rückblick geplant WAR — aus dem Recorder, blasser als der
    // gültige Plan. Nur links vom ersten Slot, sonst liegen zwei Balken
    // desselben Zeitraums übereinander.
    let histBars = "";
    if (bucket === 1) {
      for (const r of rows) {
        if (r.t >= slotT[0]) continue;
        histBars += balken(r.t, r.planBat, "0.26");
      }
    } else {
      // Gleiche Bündelung wie beim gültigen Plan, sonst überlagern sich im
      // Rückblick viele Haarlinien zu einem dunklen Streifen.
      const gruppen = new Map();
      for (const r of rows) {
        if (r.t >= slotT[0] || r.planBat == null) continue;
        const k = Math.floor(r.t / bucketMs);
        const g = gruppen.get(k) || [];
        g.push(r.planBat);
        gruppen.set(k, g);
      }
      for (const [k, werte] of gruppen) {
        histBars += balken(k * bucketMs + bucketMs / 2, mittel(werte) ?? 0, "0.26");
      }
    }

    // PV als Flaeche
    let pvArea = `M${x(slotT[0]).toFixed(1)},${y(0).toFixed(1)}`;
    slots.forEach((s, i) => { pvArea += `L${x(slotT[i]).toFixed(1)},${y(s.PV ?? 0).toFixed(1)}`; });
    pvArea += `L${x(slotT[slots.length - 1]).toFixed(1)},${y(0).toFixed(1)}Z`;

    // EEG-Bedarf: Fläche + Linie auf eigener Skala (0 unten, Maximum oben).
    // Zusammenhängende Läufe getrennt zeichnen, damit fehlende Stunden nicht
    // überbrückt werden.
    // Je Serie ein Satz Pfade — Fläche nur für die erste, sonst überdecken
    // sich zwei Flächen zu Matsch; die zweite bleibt eine Linie.
    const eegPfade = [];
    let eegAxis = "";
    // Bedarf nach oben, Überschuss nach unten — jeweils auf den Platz, den
    // das Leistungsfeld auf dieser Seite der Nulllinie hergibt.
    const eegUnten = padT + plotH;
    const yEeg = (p) => (p.ueber
      ? zeroY + (p.v / (eegMaxUeber * 1.05 || 1)) * (eegUnten - zeroY)
      : zeroY - (p.v / (eegMaxBedarf * 1.05 || 1)) * (zeroY - padT));
    if (hasEeg) {
      sichtbareSerien.forEach((serie, idx) => {
        // Getrennt wird nur an Lücken, sonst überbrückt die Linie fehlende
        // Intervalle. Ein Vorzeichenwechsel braucht keine Trennung mehr: die
        // Kurve läuft durch die Nulllinie, und die Fläche hat dort ohnehin
        // ihre Basis — sie kippt von selbst auf die andere Seite.
        const laeufe = [];
        let lauf = [];
        for (const p of serie.punkte) {
          const vorher = lauf[lauf.length - 1];
          if (vorher && p.t - vorher.t > 900000 * 1.5) {
            laeufe.push(lauf);
            lauf = [];
          }
          lauf.push(p);
        }
        if (lauf.length) laeufe.push(lauf);
        let area = "", line = "";
        for (const l of laeufe) {
          let linie = "";
          l.forEach(p => {
            linie += `${linie ? "L" : "M"}${x(p.t).toFixed(1)},${yEeg(p).toFixed(1)}`;
          });
          line += linie;
          area += `M${x(l[0].t).toFixed(1)},${zeroY.toFixed(1)}`
            + l.map(p => `L${x(p.t).toFixed(1)},${yEeg(p).toFixed(1)}`).join("")
            + `L${x(l[l.length - 1].t).toFixed(1)},${zeroY.toFixed(1)}Z`;
        }
        eegPfade.push({ farbe: serie.farbe, mitFlaeche: idx === 0, area, line });
      });
      // Die Achse gehört zur Skala, nicht zu einer Kurve — bei zwei Serien
      // in neutralem Grau, sonst behauptet sie eine Zugehörigkeit.
      const achsFarbe = sichtbareSerien.length > 1
        ? "var(--secondary-text-color,#727272)" : sichtbareSerien[0].farbe;
      // Oben der Bedarf, unten der Überschuss — jede Seite mit ihrem
      // eigenen Endwert, weil getrennt skaliert wird.
      const marken = [];
      if (eegMaxBedarf > 0) {
        marken.push({ v: eegMaxBedarf, ueber: false, text: fmtDe(eegMaxBedarf, eegMaxBedarf >= 100 ? 0 : 1) });
        marken.push({ v: eegMaxBedarf / 2, ueber: false, text: fmtDe(eegMaxBedarf / 2, eegMaxBedarf >= 100 ? 0 : 1) });
      }
      if (eegMaxUeber > 0) {
        marken.push({ v: eegMaxUeber / 2, ueber: true, text: "-" + fmtDe(eegMaxUeber / 2, eegMaxUeber >= 100 ? 0 : 1) });
        marken.push({ v: eegMaxUeber, ueber: true, text: "-" + fmtDe(eegMaxUeber, eegMaxUeber >= 100 ? 0 : 1) });
      }
      marken.forEach(m => {
        eegAxis += `<text x="${(W - padR + 4).toFixed(1)}" y="${(yEeg(m) + 3.5).toFixed(1)}" text-anchor="start" font-size="${fsAchse}" fill="${achsFarbe}">${m.text}</text>`;
      });
      // Einheit direkt an die Nulllinie, wo auf beiden Seiten Platz ist.
      eegAxis += `<text x="${(W - padR + 4).toFixed(1)}" y="${(zeroY + 3.5).toFixed(1)}" text-anchor="start" font-size="${fsKlein}" fill="${achsFarbe}">kWh</text>`;
    }

    // Raster und Achsen
    let grid = "";
    for (const kw of [maxPos, maxPos / 2, 0, -maxNeg / 2, -maxNeg]) {
      const yy = y(kw);
      const strong = kw === 0;
      grid += `<line x1="${padL}" y1="${yy.toFixed(1)}" x2="${W - padR}" y2="${yy.toFixed(1)}" stroke="var(--divider-color,#e0e0e0)" stroke-width="${strong ? 1.4 : 0.7}"/>`;
      grid += `<text x="${padL - 5}" y="${(yy + 3.5).toFixed(1)}" text-anchor="end" font-size="${fsAchse}" fill="var(--secondary-text-color,#727272)">${fmtDe(kw, 1)}</text>`;
    }

    // Stundenmarken — die Linien laufen durch BEIDE Felder, die Uhrzeit steht
    // einmal unten. Das bindet die Felder optisch zusammen. Der Abstand
    // richtet sich nach der Breite: mit Rückblick wird die Achse länger, alle
    // 6 h wäre dann Beschriftungssalat.
    const stepH = xw(6 * 3600000) >= 55 ? 6 : 12;
    let xLabels = "";
    const marke = new Date(t0);
    marke.setMinutes(0, 0, 0);
    while (marke.getHours() % stepH !== 0 || marke.getTime() < t0) {
      marke.setHours(marke.getHours() + 1);
    }
    for (let dt = marke; dt.getTime() <= t1; dt.setHours(dt.getHours() + stepH)) {
      const px = x(dt.getTime()).toFixed(1);
      const mitternacht = dt.getHours() === 0;
      xLabels += `<line x1="${px}" y1="${padT}" x2="${px}" y2="${socBottom.toFixed(1)}" stroke="var(--divider-color,#e0e0e0)" stroke-width="${mitternacht ? 1 : 0.7}" stroke-dasharray="2 3"/>`;
      xLabels += `<text x="${px}" y="${(socBottom + 14).toFixed(1)}" text-anchor="middle" font-size="${fsAchse}" fill="var(--secondary-text-color,#727272)">${String(dt.getHours()).padStart(2, "0")}:00</text>`;
      if (mitternacht) {
        // Am Handy nur Tag und Monat — der Wochentag davor macht die Marke
        // ~70 px breit und sie stoesst an die Nachbarmarke.
        const datum = schmal
          ? dt.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" })
          : dt.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit" });
        xLabels += `<text x="${px}" y="${(socBottom + 27).toFixed(1)}" text-anchor="middle" font-size="${fsKlein}" fill="var(--secondary-text-color,#727272)">${datum}</text>`;
      }
    }

    // Trennlinie „jetzt" — links davon Messwerte, rechts der Plan.
    let jetztLinie = "";
    if (hist && nowMs > t0 && nowMs < t1) {
      const px = x(nowMs).toFixed(1);
      jetztLinie = `<line x1="${px}" y1="${padT}" x2="${px}" y2="${socBottom.toFixed(1)}" stroke="var(--primary-text-color,#212121)" stroke-opacity="0.5" stroke-width="1.2"/>`
        + `<text x="${(Number(px) - 4).toFixed(1)}" y="${(padT + 9).toFixed(1)}" text-anchor="end" font-size="${fsKlein}" fill="var(--primary-text-color,#212121)" fill-opacity="0.65">jetzt</text>`;
    }

    // Einspeisepreis. Bei Quelle „Spotpreis" als Linie IM Leistungsfeld mit
    // eigener rechter ct-Achse (Nutzerwunsch: kein zweites Feld) — die
    // Achsentexte tragen die Linienfarbe, damit klar ist, wozu sie gehören.
    // Sonst als schmales Band in der Lücke (dunkel = teuer, hell = billig).
    let preisBand = "";
    let preisFeld = "";
    if (preisImFeld) {
      // Eigene Spanne über die volle Feldhöhe, unabhängig von der
      // kW-Nulllinie; bei negativen Börsenpreisen eine gestrichelte
      // Preis-Nulllinie, denn genau dort kippt Einspeisen in Abregeln.
      const yPreis = (v) =>
        padT + 4 + (plotH - 8) * (1 - (v - pMin) / (pMax - pMin));
      const preisMarken = [pMax, (pMax + pMin) / 2, pMin];
      preisMarken.forEach((wert, i) => {
        preisFeld += `<text x="${(W - padR + 4).toFixed(1)}" y="${(yPreis(wert) + 3.5).toFixed(1)}" text-anchor="start" font-size="${fsAchse}" fill="#d81b60">${fmtDe(wert * 100, 1)}${i === 0 ? " ct" : ""}</text>`;
      });
      if (pMin < 0 && pMax > 0) {
        preisFeld += `<line x1="${padL}" y1="${yPreis(0).toFixed(1)}" x2="${W - padR}" y2="${yPreis(0).toFixed(1)}" stroke="#d81b60" stroke-width="1" stroke-dasharray="4 3" stroke-opacity="0.4"/>`;
        preisFeld += `<text x="${(W - padR + 4).toFixed(1)}" y="${(yPreis(0) + 3.5).toFixed(1)}" text-anchor="start" font-size="${fsAchse}" fill="#d81b60">0</text>`;
      }
      let preisPfad = "";
      slots.forEach((s, i) => {
        if (s.feedin_price == null) return;
        preisPfad += `${preisPfad ? "L" : "M"}${x(slotT[i]).toFixed(1)},${yPreis(s.feedin_price).toFixed(1)}`;
      });
      preisFeld += `<path d="${preisPfad}" fill="none" stroke="#d81b60" stroke-width="1.8"/>`;
    } else if (preise.length && pMax - pMin > 0.0005) {   // konstanter Preis sagt nichts
      const bandY = padT + plotH + 4;
      const bandH = schmal ? 6 : 7;
      for (let i = 0; i < slots.length; i += bucket) {
        const v = mittel(slots.slice(i, i + bucket).map(sl => sl.feedin_price));
        if (v == null) continue;
        const anteil = (v - pMin) / (pMax - pMin);
        preisBand += `<rect x="${(x(slotT[i] + (bucketMs - resMs) / 2) - barW / 2).toFixed(1)}" y="${bandY}"`
          + ` width="${barW.toFixed(1)}" height="${bandH}" fill="#43a047"`
          + ` fill-opacity="${(0.12 + 0.73 * anteil).toFixed(2)}"/>`;
      }
    }

    // Ladestandsfeld darunter — eigene Skala 0..100 %
    const ys = (pct) => socTop + socH * (1 - Math.max(0, Math.min(100, pct)) / 100);
    let socPath = "";
    slots.forEach((s, i) => {
      if (s.soc == null) return;
      socPath += `${socPath ? "L" : "M"}${x(slotT[i]).toFixed(1)},${ys(s.soc).toFixed(1)}`;
    });
    const socIstPath = hist ? histPath("soc", ys) : "";
    const minSoc = Number(d.min_soc_pct ?? 0);
    // Kopfzeile des Ladestandsfeldes. Am Handy steht der Mindestwert hier mit
    // drin — als Beschriftung im Bild lag er quer ueber der Kurve.
    const socTitel = (hist ? "Ladestand (%) — Plan und Ist" : "Geplanter Ladestand (%)")
      + (schmal && minSoc > 0 ? ` \u00b7 min. ${fmtDe(minSoc, 0)} %` : "");
    // Titel getrennt vom Raster: das Vergleichsdiagramm „ohne Optimierung"
    // nutzt dasselbe Raster, braucht aber eine eigene Überschrift.
    const socTitelText = (text) =>
      `<text x="${padL}" y="${(socTop - 10).toFixed(1)}" font-size="${fsKlein}" fill="var(--secondary-text-color,#727272)">${text}</text>`;
    let socGrid = "";   // Raster ohne Titel — beide Diagramme nutzen es
    // Mindest-Ladestand als Untergrenze sichtbar machen
    if (minSoc > 0) {
      socGrid += `<line x1="${padL}" y1="${ys(minSoc).toFixed(1)}" x2="${W - padR}" y2="${ys(minSoc).toFixed(1)}" stroke="#e53935" stroke-width="1" stroke-dasharray="4 3" stroke-opacity="0.8"/>`;
      if (!schmal) {
        socGrid += `<text x="${(W - padR - 2).toFixed(1)}" y="${(ys(minSoc) - 4).toFixed(1)}" text-anchor="end" font-size="${fsKlein}" fill="#e53935">Mindest-Ladestand ${fmtDe(minSoc, 0)} %</text>`;
      }
    }
    [0, 50, 100].forEach(pct => {
      socGrid += `<line x1="${padL}" y1="${ys(pct).toFixed(1)}" x2="${W - padR}" y2="${ys(pct).toFixed(1)}" stroke="var(--divider-color,#e0e0e0)" stroke-width="0.7"/>`;
      socGrid += `<text x="${padL - 5}" y="${(ys(pct) + 3.5).toFixed(1)}" text-anchor="end" font-size="${fsAchse}" fill="var(--secondary-text-color,#727272)">${pct}</text>`;
    });

    // Unsichtbare Hover-Streifen, einer je Zeitpunkt des Rasters — sie
    // liefern die Werte für den Tooltip und liegen über den Pfaden, damit sie
    // überall im Diagramm greifen. Vergangenheit und Plan teilen dasselbe
    // Raster, deshalb eine gemeinsame Spaltenliste.
    const spalten = new Map();
    for (const r of rows) spalten.set(r.t, { t: r.t, ist: r, plan: null });
    slots.forEach((s, i) => {
      const t = slotT[i];
      const sp = spalten.get(t) || { t, ist: null, plan: null };
      sp.plan = s;
      sp.planBatKw = slotBat[i];
      spalten.set(t, sp);
    });
    // Trefferspalten: eine Spalte je Slot waere bei 48 h auf 300 px 1,3 px
    // breit — mit dem Finger nicht zu treffen. Mehrere Slots teilen sich
    // deshalb eine Spalte von mindestens 7 px; gezeigt wird der Slot aus
    // deren Mitte, und das Fadenkreuz sitzt auf dessen Zeitpunkt.
    const spaltenListe = [...spalten.values()].sort((a, b) => a.t - b.t);
    const hitBucket = Math.max(1, Math.ceil(7 / Math.max(0.1, xw(resMs))));
    const hitW = Math.max(2, xw(resMs) * hitBucket);
    let hits = "";
    for (let hi = 0; hi < spaltenListe.length; hi += hitBucket) {
      const gruppe = spaltenListe.slice(hi, hi + hitBucket);
      const sp = gruppe[Math.floor((gruppe.length - 1) / 2)];
      const spaltenLinks = x(gruppe[0].t) - xw(resMs) / 2;
      const dt = new Date(sp.t);
      const hhmm = `${String(dt.getHours()).padStart(2, "0")}:${String(dt.getMinutes()).padStart(2, "0")}`;
      const tag = dt.toLocaleDateString("de-DE", { weekday: "short" });
      const p = sp.plan, ist = sp.ist;
      const kw = (v) => (v == null ? "---" : fmtDe(v, 2));
      const pct = (v) => (v == null ? "---" : fmtDe(v, 0));
      // Geplant: der gültige Slot, wenn es einen gibt — sonst der Wert, den
      // die Fahrplan-Sensoren damals gemeldet haben.
      const pBat = p ? sp.planBatKw : (ist ? ist.planBat : null);
      const pGrid = p ? p.grid_p : (ist ? ist.planGrid : null);
      const socY = ist?.soc != null ? ys(ist.soc) : (p?.soc != null ? ys(p.soc) : null);
      const preisSlot = p && p.feedin_price != null ? p.feedin_price : null;
      // Saldo aller sichtbaren Gemeinschaften zu dieser Viertelstunde — als
      // JSON, weil die Zahl der Serien nicht feststeht.
      const eegHier = sichtbareSerien
        .map(serie => {
          const e = serie.jeViertel.get(Math.floor(sp.t / 900000));
          return e == null ? null
            : { n: serie.name, f: serie.farbe,
                v: fmtDe(e.v, e.v >= 100 ? 0 : 1), u: e.ueber ? 1 : 0 };
        })
        .filter(Boolean);
      hits += `<rect class="sched-hit" x="${spaltenLinks.toFixed(1)}" y="${padT}" width="${hitW.toFixed(1)}" height="${(socBottom - padT).toFixed(1)}" fill="transparent"`
        + ` data-key="${sp.t}" data-cx="${x(sp.t).toFixed(1)}" data-socy="${socY == null ? "" : socY.toFixed(1)}"`
        + ` data-time="${tag} ${hhmm}"`
        + ` data-ppv="${kw(p ? p.PV : null)}" data-pcons="${kw(p ? p.consumption : null)}"`
        + ` data-pgrid="${kw(pGrid)}" data-pbat="${kw(pBat)}" data-psoc="${pct(p ? p.soc : null)}"`
        + (ist
          ? ` data-past="1" data-ipv="${kw(ist.pv)}" data-icons="${kw(ist.cons)}"`
            + ` data-igrid="${kw(ist.grid)}" data-ibat="${kw(ist.bat)}" data-isoc="${pct(ist.soc)}"`
          : "")
        + (eegHier.length === 0 ? "" : ` data-eegj="${this._escapeHtml(JSON.stringify(eegHier))}"`)
        + (preisSlot == null ? "" : ` data-preis="${fmtDe(preisSlot * 100, 2)}"`)
        + `/>`;
    }

    // Kennzahl und Vergleichsdiagramm „ohne Optimierung" wohnen seit der
    // eigenen Karte „Optimierungsgewinn" in _renderGewinnKarte(). Die
    // Referenzwerte fließen hier oben trotzdem in die Achsenmessung ein,
    // damit beide Karten dieselbe kW-Skala zeigen.

    // --- Kopfzeile ------------------------------------------------------
    // Nur noch die Rechenzeit und der Knopf. Start, Slot-Anzahl, Start-SOC
    // und "berechnet" standen hier, ohne etwas zu erklaeren: der Start ist
    // per Definition jetzt, die Slot-Anzahl folgt aus Fenster und Auflösung,
    // der Ladestand steht im Diagramm darunter — und wann gerechnet wurde,
    // zeigt die Statuskarte oben bereits als "Plan HH:MM:SS".
    const legend = [
      ["#fbc02d", "PV (Prognose)"],
      ["#616161", "Verbrauch (Prognose)"],
      ["#43a047", "Netz geplant"],
      ...(preisImFeld ? [["#d81b60", "Einspeisepreis (Börse)"]] : []),
      ["#1e88e5", "Batterie laden"],
      ["#ef6c00", "Batterie entladen"],
      // Ein Eintrag je Gemeinschaft: die Farbe sagt, WER gemeint ist. Was
      // die Richtung bedeutet, sagt die beschriftete Achse rechts.
      ...sichtbareSerien.map(serie => [serie.farbe, serie.name]),
    ].map(([c, label]) =>
      `<span style="display:inline-flex;align-items:center;gap:5px;margin-right:14px;font-size:12px;color:var(--secondary-text-color);white-space:nowrap">
         <span style="width:11px;height:11px;border-radius:2px;background:${c};display:inline-block;flex-shrink:0"></span>${label}
       </span>`
    ).join("")
    + (hist
      ? `<span style="display:inline-flex;align-items:center;gap:5px;margin-right:14px;font-size:12px;color:var(--secondary-text-color);white-space:nowrap">
           <span style="width:11px;height:0;border-top:2px solid var(--primary-text-color);opacity:0.45;display:inline-block;flex-shrink:0"></span>dünn und blass = gemessener Verlauf
         </span>`
      : "");

    // --- Ansicht: Ausschnitt des Plans und Rückblick ---------------------
    // Zwei Darstellungen, je nach Platz. Am Handy Auswahlfelder: sie öffnen
    // den systemeigenen Picker, statt kleine Knöpfe zum Zielen anzubieten,
    // und ein 44-px-Tippziel muss dort sein. Am Desktop ist genau das zu
    // schwer — zwei hohe Selects mit Pfeilkästchen für je drei kurze Werte.
    // Dort deshalb ein Segmentumschalter: alle Werte sichtbar, ein Klick
    // statt zwei, und die aktive Wahl ist zu sehen, ohne sie zu lesen.
    let histHinweis = "";
    if (this._schedHistBusy) {
      histHinweis = `<span style="font-size:12px;color:var(--secondary-text-color)">Lade Verlauf…</span>`;
    } else if (this._schedHistError) {
      histHinweis = `<span style="font-size:12px;color:#e53935">Verlauf nicht ladbar: ${this._escapeHtml(this._schedHistError)}</span>`;
    } else if (hist) {
      const von = new Date(hist.startMs);
      histHinweis = `<span style="font-size:12px;color:var(--secondary-text-color)">ab ${von.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit" })} ${String(von.getHours()).padStart(2, "0")}:${String(von.getMinutes()).padStart(2, "0")}</span>`;
    }
    const wahlStil = "padding:0 28px 0 10px;min-height:44px;border:1px solid var(--divider-color);"
      + "border-radius:8px;background:var(--card-background-color,#fff);"
      + "color:var(--primary-text-color);font-size:16px;font-family:inherit;cursor:pointer";
    const wahlFeld = (art, aktiv, optionen) => `
      <select data-chart="${art}" style="${wahlStil}">
        ${optionen.map(([wert, text]) =>
          `<option value="${wert}" ${String(aktiv) === wert ? "selected" : ""}>${text}</option>`
        ).join("")}
      </select>`;
    // Ein Rahmen um die Gruppe, Trennlinien zwischen den Feldern, keine
    // eigenen Ränder je Knopf — sonst summieren sich die Linien zu Kästchen.
    const segmente = (art, aktiv, optionen) => `
      <span style="display:inline-flex;border:1px solid var(--divider-color);border-radius:7px;overflow:hidden">
        ${optionen.map(([wert, text], i) => {
          const an = String(aktiv) === wert;
          return `<button type="button" data-action="chart-range" data-chart="${art}" data-wert="${wert}"
            style="appearance:none;border:0;${i ? "border-left:1px solid var(--divider-color);" : ""}
                   padding:5px 11px;font:inherit;font-size:12px;line-height:1.5;cursor:pointer;
                   background:${an ? "var(--primary-color)" : "transparent"};
                   color:${an ? "var(--text-primary-color,#fff)" : "var(--primary-text-color)"};
                   font-weight:${an ? "500" : "400"}"
            ${an ? 'aria-pressed="true"' : 'aria-pressed="false"'}>${text}</button>`;
        }).join("")}
      </span>`;
    const wahl = schmal ? wahlFeld : segmente;
    const planWerte = [["24", "24 h"], ["36", "36 h"], ["48", "48 h"]];
    const histWerte = [["off", "aus"], ["12h", "12 h"], ["yesterday", "ab gestern"]];
    const wahlGruppen = `
      <label style="display:inline-flex;align-items:center;gap:${schmal ? "6px" : "8px"};font-size:12px;color:var(--secondary-text-color)">
        Plan${wahl("plan", planStunden, planWerte)}
      </label>
      <label style="display:inline-flex;align-items:center;gap:${schmal ? "6px" : "8px"};font-size:12px;color:var(--secondary-text-color)">
        Verlauf${wahl("hist", histRange, histWerte)}
      </label>`;
    // Am PC stehen die Segmente in der Kopfzeile: dort ist neben Rechenzeit
    // und Knopf eine ganze Zeile frei, und eine eigene Zeile nur für zwei
    // flache Umschalter schiebt das Diagramm ohne Gewinn nach unten. Am Handy
    // geht das nicht — die Auswahlfelder sind 44 px hoch und brauchen die
    // Breite, dort bleibt die eigene Zeile.
    const umschalter = schmal
      ? `<div style="display:flex;gap:8px;margin:12px 0 8px;flex-wrap:wrap;align-items:center">
           ${wahlGruppen}
           <span style="margin-left:auto">${histHinweis}</span>
         </div>`
      : "";

    return `
      <div style="margin-top:10px">
        <div style="display:flex;flex-wrap:wrap;gap:${schmal ? "8px" : "14px"};align-items:center;margin-bottom:8px;font-size:12px;color:var(--secondary-text-color)">
          ${d.duration_ms == null ? "" : `<span>Rechenzeit ${d.duration_ms} ms</span>`}
          ${schmal ? "" : `${wahlGruppen}${histHinweis}`}
          <button class="btn-link btn-tap" data-action="refresh-schedule" style="margin-left:auto" ${this._scheduleBusy ? "disabled" : ""}>
            <ha-icon icon="mdi:refresh" style="--mdc-icon-size:16px;vertical-align:middle"></ha-icon>
            ${this._scheduleBusy ? "Rechne…" : "Neu rechnen"}
          </button>
        </div>

        ${umschalter}

        <div style="margin-bottom:6px">${legend}</div>

        <div class="sched-chart-card" style="position:relative">
          <div class="sched-scroll">
            <svg data-cw="sched" viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">
              ${grid}${socTitelText(socTitel)}${socGrid}${xLabels}
              ${eegPfade.filter(p => p.mitFlaeche && p.area).map(p =>
                `<path d="${p.area}" fill="${p.farbe}" fill-opacity="0.10"/>`).join("")}
              <path d="${pvArea}" fill="#fbc02d" fill-opacity="0.18"/>
              ${histBars}${bars}
              ${eegPfade.filter(p => p.line).map(p =>
                `<path d="${p.line}" fill="none" stroke="${p.farbe}" stroke-width="1.4" stroke-dasharray="5 3" stroke-opacity="0.85"/>`).join("")}
              <path d="${path("PV")}" fill="none" stroke="#fbc02d" stroke-width="1.8"/>
              <path d="${path("consumption")}" fill="none" stroke="#616161" stroke-width="1.5" stroke-dasharray="4 3"/>
              <path d="${path("grid_p")}" fill="none" stroke="#43a047" stroke-width="2"/>
              ${hist ? `
              <path d="${histPath("planGrid", y)}" fill="none" stroke="#43a047" stroke-width="1.6" stroke-opacity="0.75"/>
              <path d="${histPath("pv", y)}" fill="none" stroke="#fbc02d" stroke-width="1.1" stroke-opacity="0.55"/>
              <path d="${histPath("cons", y)}" fill="none" stroke="#616161" stroke-width="1.1" stroke-opacity="0.5" stroke-dasharray="3 2"/>
              <path d="${histPath("grid", y)}" fill="none" stroke="#43a047" stroke-width="1.2" stroke-opacity="0.55"/>
              <path d="${histPath("bat", y)}" fill="none" stroke="#1e88e5" stroke-width="1.2" stroke-opacity="0.55"/>` : ""}
              <path d="${socPath}" fill="none" stroke="#7cb342" stroke-width="2"/>
              ${socIstPath ? `<path d="${socIstPath}" fill="none" stroke="#7cb342" stroke-width="1.2" stroke-opacity="0.55"/>` : ""}
              ${preisBand}${preisFeld}${preisImFeld ? "" : eegAxis}${jetztLinie}
              <line class="sched-cursor" x1="0" y1="${padT}" x2="0" y2="${socBottom.toFixed(1)}"
                    stroke="var(--primary-text-color,#212121)" stroke-opacity="0.45" stroke-width="1"
                    stroke-dasharray="3 3" style="visibility:hidden"/>
              <circle class="sched-cursor-soc" r="3.5" cx="0" cy="0" fill="#7cb342" style="visibility:hidden"/>
              ${hits}
            </svg>
          </div>
          <div class="sched-tooltip" style="position:absolute;display:none;pointer-events:none;background:var(--card-background-color,#fff);color:var(--primary-text-color);border:1px solid var(--divider-color);border-radius:8px;padding:8px 10px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,0.18);transform:translate(-50%,-100%);white-space:nowrap;z-index:10"></div>
        </div>

      </div>`;
  }

  // Karte „Optimierungsgewinn": was die Optimierung gegenüber dem
  // Standardbetrieb desselben Geräts voraussichtlich bringt — Kennzahl im
  // Kartenkopf (auch zugeklappt sichtbar), aufklappbare Geld-Details und
  // das simulierte Einspeisemuster ohne Optimierung als eigenes Diagramm.
  // Eigenständig gerechnet (eigene gemessene Breite, kein Rückblick, keine
  // EEG-Serien), aber mit derselben Messregel für die kW-Achse wie der
  // Optimierungsplan (Plan- UND Referenzwerte), damit beide Karten dieselbe
  // Skala zeigen, solange dort kein Verlauf eingeblendet ist.
  // Was die PV gebracht hat — heute, diesen Monat, dieses Jahr.
  //
  // Der Optimierungs-Vorteil steht bewusst als „davon"-Zeile und NICHT als
  // eigene Summe daneben: Er ist Teil der PV-Ersparnis, nicht zusaetzlich zu
  // ihr. Zwei gleichrangige Betraege wuerden zum Zusammenzaehlen einladen,
  // und das waere doppelt gezaehlt.
  //
  // Unterschied zur Karte „Optimierungsgewinn": Die schaut mit Prognosen 48
  // Stunden VORAUS, diese hier schaut auf Gemessenes ZURUECK.
  _renderBilanzKarte() {
    const b = this._bilanz;
    if (!b || !b.verfuegbar) return "";
    const pv = b.pv_ersparnis || {};
    const opt = b.opt_vorteil || {};
    const heute = b.heute || {};
    const waehrung = b.waehrung === "EUR" ? "€" : (b.waehrung || "€");
    const eur = (v) =>
      v == null ? "—" : `${v < 0 ? "−" : ""}${fmtDe(Math.abs(Number(v)), 2)}&nbsp;${waehrung}`;

    // Jede Spalte haengt an einem eigenen Sensor — ein Klick oeffnet dessen
    // Verlauf. Entity-IDs kommen aufgeloest aus dem Backend (die Entitaeten
    // koennen umbenannt worden sein), verlinkt wird nur, was auch da ist.
    const ent = b.entities || {};
    const spalte = (titel, wert, gross, entity) => {
      const klickbar = entity && this._readState(entity);
      const attrs = klickbar
        ? ` class="bilanz-zeile-klickbar" data-action="show-entity" data-entity="${entity}" title="Verlauf anzeigen"`
        : "";
      return `
      <div${attrs} style="flex:1;min-width:0;text-align:center;border-radius:8px;padding:4px 2px">
        <div style="font-size:${gross ? "26px" : "17px"};font-weight:600;color:var(--success-color,#0f9d58);white-space:nowrap">${eur(wert)}</div>
        <div style="font-size:12px;color:var(--secondary-text-color);margin-top:2px">${titel}</div>
      </div>`;
    };

    // Die „davon"-Zeile nur, wenn der Vorteil wirklich gerechnet werden
    // konnte — eine Null saehe aus wie ein Messergebnis.
    const vorteilHeute = opt.heute;
    const optEntity = (ent.opt_vorteil || {}).heute;
    // Ohne abgeschlossenen Tag sind Monat und Jahr rechnerisch dasselbe wie
    // heute. Die drei gleichen Betraege nebeneinander lesen sich wie ein
    // Fehler — also erst zeigen, wenn sie sich unterscheiden koennen.
    const archivVorhanden = !!(b.archiv && (b.archiv.monat || b.archiv.jahr));
    const davon = vorteilHeute == null ? `
      <div style="font-size:12px;color:var(--secondary-text-color);text-align:center;margin-top:10px">
        Der Anteil der Optimierung lässt sich heute noch nicht beziffern — dafür fehlt der Ladestand vom Tagesbeginn.
      </div>` : `
      <div style="font-size:13px;color:var(--secondary-text-color);text-align:center;margin-top:10px">
        <span${optEntity ? ` class="bilanz-zeile-klickbar" data-action="show-entity" data-entity="${optEntity}" title="Verlauf anzeigen" style="border-radius:6px;padding:2px 6px"` : ""}>
          davon durch die Optimierung
          <strong style="color:${Number(vorteilHeute) >= 0 ? "var(--success-color,#0f9d58)" : "#e53935"}">${eur(vorteilHeute)}</strong>
        </span>
        ${archivVorhanden ? `<span style="white-space:nowrap">(Monat ${eur(opt.monat)}, Jahr ${eur(opt.jahr)})</span>` : ""}
      </div>
      ${archivVorhanden ? "" : `
      <div style="font-size:12px;color:var(--secondary-text-color);text-align:center;margin-top:6px;line-height:1.5">
        Monats- und Jahreswerte wachsen erst mit jedem abgeschlossenen Tag — heute ist der erste.
      </div>`}`;

    let details = "";
    if (this._bilanzDetailsOpen) {
      // Die kWh-Zeilen stammen aus je einem unserer Sensoren — ein Klick
      // oeffnet dessen Verlauf. Nur verlinken, wenn die Entitaet auch da
      // ist: ein Klick ins Leere waere schlimmer als kein Klick. Die
      // Geldzeilen bleiben stumm, hinter ihnen steht kein Sensor.
      const zeile = (name, wert, einheit, entity) => {
        const klickbar = entity && this._readState(entity);
        const attrs = klickbar
          ? ` class="bilanz-zeile-klickbar" data-action="show-entity" data-entity="${entity}" title="Verlauf anzeigen"`
          : "";
        const icon = klickbar
          ? ` <ha-icon icon="mdi:chart-line" style="--mdc-icon-size:14px;vertical-align:-2px;opacity:.5"></ha-icon>`
          : "";
        return `
        <tr${attrs}>
          <td style="padding:3px 0;color:var(--secondary-text-color)">${name}${icon}</td>
          <td style="padding:3px 0;text-align:right;white-space:nowrap">${wert == null ? "—" : fmtDe(Number(wert), einheit === "kWh" ? 1 : 2)}&nbsp;${einheit === "kWh" ? "kWh" : waehrung}</td>
        </tr>`;
      };
      const sPv = "sensor.eeg_energy_optimizer_pv_leistung";
      const sNetz = "sensor.eeg_energy_optimizer_netzleistung";
      const sHaus = "sensor.eeg_energy_optimizer_hausverbrauch";
      const eegKwh = Number(heute.eeg_kwh || 0);
      const exportKwh = Number(heute.export_kwh || 0);
      const eegZeile = exportKwh > 0 ? `
        <div style="font-size:12px;color:var(--secondary-text-color);margin-top:8px;line-height:1.5">
          Zum Satz der Energiegemeinschaft vergütet: <strong>${fmtDe(eegKwh, 1)} kWh</strong>
          von ${fmtDe(exportKwh, 1)} kWh Einspeisung.
          Das beruht auf der Bedarfsprognose der Gemeinschaft — endgültig steht es erst mit der EEG-Abrechnung fest.
        </div>` : "";
      details = `
        <table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:12px">
          ${zeile("Nicht gekaufter Strom", heute.vermieden, "eur")}
          ${zeile("Einspeiseerlös", heute.erloes, "eur")}
          ${zeile("Selbst verbraucht", heute.eigen_kwh, "kWh", sHaus)}
          ${zeile("Eingespeist", heute.export_kwh, "kWh", sNetz)}
          ${zeile("Aus dem Netz bezogen", heute.bezug_kwh, "kWh", sNetz)}
          ${zeile("Erzeugt", heute.pv_kwh, "kWh", sPv)}
        </table>
        ${eegZeile}
        <div style="font-size:12px;color:var(--secondary-text-color);margin-top:8px;line-height:1.5">
          Der Anteil der Optimierung ist ein Vergleich mit einem simulierten Betrieb ohne Vorausschau,
          gerechnet über die gemessenen Werte des Tages — keine Messung, sondern eine Rechnung.
        </div>`;
    }

    return `
      <div class="card">
        <h3 style="margin:0 0 4px">
          <ha-icon icon="mdi:piggy-bank-outline" style="--mdc-icon-size:20px;color:var(--primary-color,#03a9f4);vertical-align:middle"></ha-icon>
          Was deine PV bringt
        </h3>
        <div style="display:flex;gap:8px;align-items:flex-end;margin-top:14px">
          ${spalte("heute", pv.heute, true, ent.pv_ersparnis?.heute)}
          ${spalte("diesen Monat", pv.monat, false, ent.pv_ersparnis?.monat)}
          ${spalte("dieses Jahr", pv.jahr, false, ent.pv_ersparnis?.jahr)}
        </div>
        ${davon}
        <div data-action="toggle-bilanz-details" style="margin-top:12px;font-size:13px;color:var(--primary-color,#03a9f4);cursor:pointer;user-select:none;text-align:center">
          ${this._bilanzDetailsOpen ? "Weniger anzeigen" : "Woraus setzt sich das zusammen?"}
        </div>
        ${details}
      </div>`;
  }

  _renderGewinnKarte() {
    const d = this._scheduleData;
    const gewinn = d && d.gewinn;
    if (!d || !d.available || !gewinn || !gewinn.mit || !gewinn.ohne) return "";
    const m = gewinn.mit, o = gewinn.ohne;
    const vorteil = Number(gewinn.vorteil) || 0;
    const stunden = Math.round(Number(gewinn.horizont_h) || 48);
    const farbe = vorteil >= 0 ? "var(--success-color,#0f9d58)" : "#e53935";
    const eur = (v) => `${v < 0 ? "−" : ""}${fmtDe(Math.abs(v), 2)}`;
    const eurSigniert = (v) => `${v < 0 ? "−" : "+"}${fmtDe(Math.abs(v), 2)}`;
    const offen = this._gewinnOpen;

    const kopf = `
      <div data-action="toggle-gewinn-karte" style="display:flex;align-items:center;gap:10px;cursor:pointer;user-select:none">
        <h3 style="margin:0;flex:1;min-width:0">
          <ha-icon icon="mdi:scale-balance" style="--mdc-icon-size:20px;color:var(--primary-color,#03a9f4);vertical-align:middle"></ha-icon>
          Optimierungsgewinn
          <span style="font-weight:400;font-size:13px;color:var(--secondary-text-color);white-space:nowrap">(${stunden}&nbsp;h)</span>
        </h3>
        <span style="font-size:15px;font-weight:600;color:${farbe};white-space:nowrap">${eurSigniert(vorteil)}&nbsp;€</span>
        <ha-icon icon="mdi:chevron-${offen ? "up" : "down"}" style="--mdc-icon-size:24px;color:var(--secondary-text-color);flex-shrink:0"></ha-icon>
      </div>`;
    if (!offen) return `<div class="card">${kopf}</div>`;

    // --- Kennzahl + Geld-Details -----------------------------------------
    let details = "";
    if (this._gewinnDetailsOpen) {
      // Die Zuteilungszeile zeigt, wo der Zeitvorteil herkommt: wie viel
      // der Einspeisung wirklich zum Gemeinschaftssatz vergütet wurde.
      // Nur mit aktiver Gemeinschaft — bei Spot/OeMAG ohne EEG sagt sie nichts.
      const eegAktiv = this._config?.enable_peakshare !== false
        && !!this._config?.peakshare_community;
      const eegZuteilung = (eegAktiv && m.eeg_kwh != null && o.eeg_kwh != null)
        ? ` Zur Gemeinschaft vergütet: mit Optimierung ${fmtDe(m.eeg_kwh, 1)}&nbsp;von&nbsp;${fmtDe(m.export_kwh ?? 0, 1)}&nbsp;kWh, ohne ${fmtDe(o.eeg_kwh, 1)}&nbsp;von&nbsp;${fmtDe(o.export_kwh ?? 0, 1)}&nbsp;kWh.`
        : "";
      // Kosten stehen als negative Beträge in der Tabelle, damit die
      // Summenzeile schlicht die Spaltensumme ist.
      const zeilen = [
        ["Einspeise-Erlös", m.erloes, o.erloes],
        ["Netzbezug", -m.bezug, -o.bezug],
        ["Batteriealterung", -m.alterung, -o.alterung],
        ["Endbestand (Gutschrift)", m.endbestand, o.endbestand],
        ["Summe", m.summe, o.summe],
      ];
      const zellenStil = "padding:3px 10px;text-align:right;white-space:nowrap";
      const summeStil = ";font-weight:600;border-top:1px solid var(--divider-color)";
      const tabellenzeilen = zeilen.map(([label, mitWert, ohneWert], i) => {
        const zusatz = i === zeilen.length - 1 ? summeStil : "";
        return `<tr>
          <td style="padding:3px 0;white-space:nowrap${zusatz}">${label}</td>
          <td style="${zellenStil}${zusatz}">${eur(mitWert)}</td>
          <td style="${zellenStil}${zusatz}">${eur(ohneWert)}</td>
          <td style="${zellenStil}${zusatz}">${eurSigniert(mitWert - ohneWert)}</td>
        </tr>`;
      }).join("");
      details = `
        <div style="overflow-x:auto;margin-top:8px">
          <table style="border-collapse:collapse;font-size:12px;color:var(--primary-text-color)">
            <thead><tr style="color:var(--secondary-text-color)">
              <th style="text-align:left;font-weight:400;padding:3px 0">in €</th>
              <th style="text-align:right;font-weight:400;padding:3px 10px">mit</th>
              <th style="text-align:right;font-weight:400;padding:3px 10px">ohne</th>
              <th style="text-align:right;font-weight:400;padding:3px 10px">Differenz</th>
            </tr></thead>
            <tbody>${tabellenzeilen}</tbody>
          </table>
        </div>
        <p style="margin:8px 0 0;font-size:12px;color:var(--secondary-text-color)">
          Bewertet mit den echten Tarifen, je Viertelstunde: den
          Gemeinschaftssatz gibt es nur für Energie, die die Gemeinschaft
          laut Bedarfsprognose auch aufnimmt — der Rest geht zum Basistarif
          bzw. Börsenpreis an den Restabnehmer.${eegZuteilung}
          Endbestand: Restenergie über dem Mindest-Ladestand am
          Horizontende (mit Optimierung ${fmtDe(m.rest_kwh ?? 0, 1)}&nbsp;kWh,
          ohne ${fmtDe(o.rest_kwh ?? 0, 1)}&nbsp;kWh), konservativ mit dem
          Basistarif gutgeschrieben — sonst verglichen die Pläne ungleiche
          Endzustände.
        </p>`;
    }
    const kennzahl = `
      <div style="margin-top:10px">
        <div data-action="toggle-gewinn-details" style="display:flex;align-items:flex-start;gap:8px;cursor:pointer;user-select:none">
          <div style="flex:1;min-width:0">
            <div style="font-size:14px;font-weight:500">Optimierung bringt voraussichtlich
              <span style="color:${farbe};white-space:nowrap">${eurSigniert(vorteil)}&nbsp;€</span>
              in ${stunden}&nbsp;h gegenüber Standardbetrieb</div>
            <div style="font-size:12px;color:var(--secondary-text-color);margin-top:2px">Erwarteter Mehrerlös auf Prognosebasis — kein Messwert.</div>
          </div>
          <ha-icon icon="mdi:chevron-${this._gewinnDetailsOpen ? "up" : "down"}" style="--mdc-icon-size:22px;color:var(--secondary-text-color);flex-shrink:0"></ha-icon>
        </div>
        ${details}
      </div>`;

    // --- Diagramm: das unoptimierte Einspeisemuster -----------------------
    const alleSlots = d.slots || [];
    const alleRef = d.referenz_slots || [];
    let chart = "";
    if (alleSlots.length && alleRef.length) {
      const schmal = !!this._narrow;
      const planStunden = Number(this._schedPlanRange) || 48;
      const planEnde = new Date(alleSlots[0].t).getTime() + planStunden * 3600000;
      const slots = alleSlots.filter(s => new Date(s.t).getTime() <= planEnde);
      const refSlots = alleRef.filter(s => new Date(s.t).getTime() <= planEnde);
      const resMs = Math.max(1, Number(d.time_res_min) || 15) * 60000;
      const slotT = slots.map(s => new Date(s.t).getTime());
      const refT = refSlots.map(s => new Date(s.t).getTime());
      const t0 = slotT[0];
      const t1 = slotT[slotT.length - 1] + resMs;
      const span = Math.max(resMs, t1 - t0);
      const W = this._cw("gewinn");
      const fsAchse = schmal ? 10 : 12;
      const fsKlein = schmal ? 10 : 11;
      const padL = schmal ? 30 : 44, padT = 14;
      // Einspeisepreis bei Quelle „Spotpreis": Linie im Leistungsfeld mit
      // rechter ct-Achse — wie im Optimierungsplan.
      const preise = slots.map(sl => sl.feedin_price).filter(v => v != null);
      const pMin = preise.length ? Math.min(...preise) : 0;
      const pMax = preise.length ? Math.max(...preise) : 0;
      const preisImFeld = (this._config?.schedule_feedin_source || "manual") === "spot"
        && preise.length > 0 && pMax - pMin > 0.0005;
      const padR = preisImFeld ? (schmal ? 34 : 46) : (schmal ? 8 : 16);
      const plotW = W - padL - padR;
      const plotH = schmal ? 150 : 200;
      const socTop = padT + plotH + 34;
      const socH = schmal ? 52 : 70;
      const socBottom = socTop + socH;
      const H = socBottom + (schmal ? 32 : 35);

      let maxPos = 0.5, maxNeg = 0.5;
      const messen = (v) => {
        if (v == null || !isFinite(v)) return;
        if (v > maxPos) maxPos = v;
        if (-v > maxNeg) maxNeg = -v;
      };
      slots.forEach(s => {
        for (const key of ["PV", "consumption", "grid_p"]) messen(s[key] ?? 0);
        messen(s.battery_p == null ? null : -s.battery_p);
      });
      refSlots.forEach(s => {
        messen(s.grid_p ?? 0);
        messen(s.battery_p == null ? null : -s.battery_p);
      });
      const halbeKw = (v) => Math.ceil(v * 1.05 * 2) / 2;
      maxPos = halbeKw(maxPos);
      maxNeg = halbeKw(maxNeg);
      const negAnteil = Math.min(0.5, maxNeg / (maxPos + maxNeg));
      const zeroY = padT + plotH * (1 - negAnteil);
      const x = (t) => padL + (plotW * (t - t0)) / span;
      const xw = (ms) => (plotW * ms) / span;
      const y = (kw) => kw >= 0
        ? zeroY - (kw / maxPos) * (zeroY - padT)
        : zeroY + (-kw / maxNeg) * (padT + plotH - zeroY);
      const ys = (pct) => socTop + socH * (1 - Math.max(0, Math.min(100, pct)) / 100);

      let grid = "";
      for (const kw of [maxPos, maxPos / 2, 0, -maxNeg / 2, -maxNeg]) {
        const yy = y(kw);
        grid += `<line x1="${padL}" y1="${yy.toFixed(1)}" x2="${W - padR}" y2="${yy.toFixed(1)}" stroke="var(--divider-color,#e0e0e0)" stroke-width="${kw === 0 ? 1.4 : 0.7}"/>`;
        grid += `<text x="${padL - 5}" y="${(yy + 3.5).toFixed(1)}" text-anchor="end" font-size="${fsAchse}" fill="var(--secondary-text-color,#727272)">${fmtDe(kw, 1)}</text>`;
      }
      const stepH = xw(6 * 3600000) >= 55 ? 6 : 12;
      let xLabels = "";
      const marke = new Date(t0);
      marke.setMinutes(0, 0, 0);
      while (marke.getHours() % stepH !== 0 || marke.getTime() < t0) {
        marke.setHours(marke.getHours() + 1);
      }
      for (let dt = marke; dt.getTime() <= t1; dt.setHours(dt.getHours() + stepH)) {
        const px = x(dt.getTime()).toFixed(1);
        const mitternacht = dt.getHours() === 0;
        xLabels += `<line x1="${px}" y1="${padT}" x2="${px}" y2="${socBottom.toFixed(1)}" stroke="var(--divider-color,#e0e0e0)" stroke-width="${mitternacht ? 1 : 0.7}" stroke-dasharray="2 3"/>`;
        xLabels += `<text x="${px}" y="${(socBottom + 14).toFixed(1)}" text-anchor="middle" font-size="${fsAchse}" fill="var(--secondary-text-color,#727272)">${String(dt.getHours()).padStart(2, "0")}:00</text>`;
        if (mitternacht) {
          const datum = schmal
            ? dt.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit" })
            : dt.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit" });
          xLabels += `<text x="${px}" y="${(socBottom + 27).toFixed(1)}" text-anchor="middle" font-size="${fsKlein}" fill="var(--secondary-text-color,#727272)">${datum}</text>`;
        }
      }

      const minSoc = Number(d.min_soc_pct ?? 0);
      let socGrid = `<text x="${padL}" y="${(socTop - 10).toFixed(1)}" font-size="${fsKlein}" fill="var(--secondary-text-color,#727272)">Ladestand (%) — Standardbetrieb</text>`;
      if (minSoc > 0) {
        socGrid += `<line x1="${padL}" y1="${ys(minSoc).toFixed(1)}" x2="${W - padR}" y2="${ys(minSoc).toFixed(1)}" stroke="#e53935" stroke-width="1" stroke-dasharray="4 3" stroke-opacity="0.8"/>`;
      }
      [0, 50, 100].forEach(pct => {
        socGrid += `<line x1="${padL}" y1="${ys(pct).toFixed(1)}" x2="${W - padR}" y2="${ys(pct).toFixed(1)}" stroke="var(--divider-color,#e0e0e0)" stroke-width="0.7"/>`;
        socGrid += `<text x="${padL - 5}" y="${(ys(pct) + 3.5).toFixed(1)}" text-anchor="end" font-size="${fsAchse}" fill="var(--secondary-text-color,#727272)">${pct}</text>`;
      });

      let preisFeld = "";
      if (preisImFeld) {
        const yPreis = (v) =>
          padT + 4 + (plotH - 8) * (1 - (v - pMin) / (pMax - pMin));
        const preisMarken = [pMax, (pMax + pMin) / 2, pMin];
        preisMarken.forEach((wert, i) => {
          preisFeld += `<text x="${(W - padR + 4).toFixed(1)}" y="${(yPreis(wert) + 3.5).toFixed(1)}" text-anchor="start" font-size="${fsAchse}" fill="#d81b60">${fmtDe(wert * 100, 1)}${i === 0 ? " ct" : ""}</text>`;
        });
        if (pMin < 0 && pMax > 0) {
          preisFeld += `<line x1="${padL}" y1="${yPreis(0).toFixed(1)}" x2="${W - padR}" y2="${yPreis(0).toFixed(1)}" stroke="#d81b60" stroke-width="1" stroke-dasharray="4 3" stroke-opacity="0.4"/>`;
          preisFeld += `<text x="${(W - padR + 4).toFixed(1)}" y="${(yPreis(0) + 3.5).toFixed(1)}" text-anchor="start" font-size="${fsAchse}" fill="#d81b60">0</text>`;
        }
        let preisPfad = "";
        slots.forEach((s, i) => {
          if (s.feedin_price == null) return;
          preisPfad += `${preisPfad ? "L" : "M"}${x(slotT[i]).toFixed(1)},${yPreis(s.feedin_price).toFixed(1)}`;
        });
        preisFeld += `<path d="${preisPfad}" fill="none" stroke="#d81b60" stroke-width="1.8"/>`;
      }

      // PV-Fläche und Prognose-Linien aus den Plan-Slots — dieselben
      // Prognosen gelten für beide Pläne.
      let pvArea = `M${x(slotT[0]).toFixed(1)},${y(0).toFixed(1)}`;
      slots.forEach((s, i) => { pvArea += `L${x(slotT[i]).toFixed(1)},${y(s.PV ?? 0).toFixed(1)}`; });
      pvArea += `L${x(slotT[slots.length - 1]).toFixed(1)},${y(0).toFixed(1)}Z`;
      const planPfad = (key) => {
        let out = "";
        slots.forEach((s, i) => {
          const v = s[key];
          if (v == null) return;
          out += `${out ? "L" : "M"}${x(slotT[i]).toFixed(1)},${y(v).toFixed(1)}`;
        });
        return out;
      };
      const refLinie = (werte, yFn) => {
        let out = "";
        werte.forEach((v, i) => {
          if (v == null) return;
          out += `${out ? "L" : "M"}${x(refT[i]).toFixed(1)},${yFn(v).toFixed(1)}`;
        });
        return out;
      };
      // Batterie-Balken wie im Plan-Diagramm gebündelt (positiv = laden).
      const slotPx = xw(resMs);
      const bucket = Math.max(1, Math.ceil(4.5 / Math.max(0.1, slotPx)));
      const bucketMs = bucket * resMs;
      const barW = Math.max(1, xw(bucketMs) - 0.6);
      const mittel = (werte) => {
        const gute = werte.filter(v => v != null && isFinite(v));
        return gute.length ? gute.reduce((a, b) => a + b, 0) / gute.length : null;
      };
      const refBat = refSlots.map(s => (s.battery_p == null ? null : -s.battery_p));
      let refBars = "";
      for (let i = 0; i < refSlots.length; i += bucket) {
        const v = mittel(refBat.slice(i, i + bucket));
        if (v == null || Math.abs(v) < 0.001) continue;
        const yTop = v > 0 ? y(v) : y(0);
        const h = Math.abs(y(v) - y(0));
        refBars += `<rect x="${(x(refT[i] + (bucketMs - resMs) / 2) - barW / 2).toFixed(1)}" y="${yTop.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(0.6, h).toFixed(1)}" fill="${v > 0 ? "#1e88e5" : "#ef6c00"}" fill-opacity="0.55"/>`;
      }

      const legende = [
        ["#fbc02d", "PV (Prognose)"],
        ["#616161", "Verbrauch (Prognose)"],
        ["#43a047", "Netz ohne Optimierung"],
        ...(preisImFeld ? [["#d81b60", "Einspeisepreis (Börse)"]] : []),
        ["#1e88e5", "Batterie laden"],
        ["#ef6c00", "Batterie entladen"],
      ].map(([c, label]) =>
        `<span style="display:inline-flex;align-items:center;gap:5px;margin-right:14px;font-size:12px;color:var(--secondary-text-color);white-space:nowrap">
           <span style="width:11px;height:11px;border-radius:2px;background:${c};display:inline-block;flex-shrink:0"></span>${label}
         </span>`
      ).join("");

      chart = `
        <div style="margin-top:14px">
          <p style="margin:0 0 6px;font-size:12px;color:var(--secondary-text-color)">
            So sähe die Einspeisung ohne Optimierung aus — simuliertes
            Standardverhalten über dieselben Prognosen: PV-Überschuss lädt
            zuerst die Batterie, ein Defizit entlädt sie bis zum
            Mindest-Ladestand. Gleiche Achsen wie im Optimierungsplan.
          </p>
          <div style="margin-bottom:6px">${legende}</div>
          <div class="sched-chart-card">
            <div class="sched-scroll">
              <svg data-cw="gewinn" viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">
                ${grid}${socGrid}${xLabels}${preisFeld}
                <path d="${pvArea}" fill="#fbc02d" fill-opacity="0.18"/>
                ${refBars}
                <path d="${planPfad("PV")}" fill="none" stroke="#fbc02d" stroke-width="1.8"/>
                <path d="${planPfad("consumption")}" fill="none" stroke="#616161" stroke-width="1.5" stroke-dasharray="4 3"/>
                <path d="${refLinie(refSlots.map(s => s.grid_p), y)}" fill="none" stroke="#43a047" stroke-width="2"/>
                <path d="${refLinie(refSlots.map(s => s.soc), ys)}" fill="none" stroke="#7cb342" stroke-width="2"/>
              </svg>
            </div>
          </div>
        </div>`;
    }

    return `<div class="card">${kopf}${kennzahl}${chart}</div>`;
  }


  _escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, ch => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
    ));
  }

  _renderPeakShareDashboard() {
    const d = this._peakshareData;
    // Beide konfigurierten Gemeinschaften gehoeren ins selbe Bild: der
    // Fahrplan rechnet mit beiden, also soll man sie auch nebeneinander
    // lesen koennen. `communities` traegt sie, `intervals` ist die erste
    // davon und dient nur noch als Rueckfall.
    const rohSerien = (d?.communities?.length
      ? d.communities
      : (d?.intervals ? [{ name: d.community || "Gemeinschaft", intervals: d.intervals }] : []))
      .filter(s => Array.isArray(s?.intervals) && s.intervals.length);
    if (!d || !rohSerien.length) {
      return `<p style="color:var(--secondary-text-color);font-size:14px">Keine PeakShare-Daten verfügbar. Die Daten werden beim nächsten API-Abruf geladen.</p>`;
    }

    const cacheAge = d.cache_age_minutes != null ? d.cache_age_minutes : null;
    const cacheText = cacheAge != null ? (cacheAge < 60 ? `vor ${cacheAge} Min` : `vor ${Math.round(cacheAge / 60)}h`) : "---";
    // Kein eigenes Entladefenster mehr — die Steuerung uebernimmt der
    // Fahrplan. Die Prognose bleibt als Anzeige und ist Grundlage der
    // EEG-Preisfunktion.
    const planHtml = `<div style="background:var(--secondary-text-color)22;padding:10px 14px;border-radius:10px;margin-bottom:12px;font-size:14px;color:var(--secondary-text-color)">
      <ha-icon icon="mdi:information-outline" style="--mdc-icon-size:18px;vertical-align:middle"></ha-icon>
      Anzeige — gesteuert wird nach dem Optimierungsplan.
    </div>`;

    // V2 liefert 192 Viertelstunden je Gemeinschaft. Fuer diese Uebersicht
    // werden sie auf Stunden summiert: 192 Punkte samt Tippzielen waeren
    // weder lesbar noch bedienbar, und die Einheit bleibt kWh je Stunde.
    // Dieselben Farben wie im Optimierungsplan — eine Gemeinschaft soll in
    // beiden Diagrammen dieselbe Farbe haben.
    const farben = ["#8e24aa", "#00897b"];
    const serien = rohSerien.map((s, i) => {
      const jeStunde = new Map();
      for (const iv of s.intervals) {
        if (!iv?.timestamp || iv.saldoKwh == null) continue;
        const t = new Date(iv.timestamp).getTime();
        if (isNaN(t)) continue;
        const k = Math.floor(t / 3600000);
        jeStunde.set(k, (jeStunde.get(k) || 0) + Number(iv.saldoKwh));
      }
      return { name: s.name || `Gemeinschaft ${i + 1}`,
               farbe: farben[i % farben.length], jeStunde };
    }).filter(s => s.jeStunde.size);
    if (!serien.length) return planHtml + `<p style="color:var(--secondary-text-color);font-size:13px">Keine Stundendaten vorhanden</p>`;

    // Gemeinsame Zeitachse ueber alle Serien — fehlt einer Gemeinschaft eine
    // Stunde, bleibt ihre Kurve dort auf der Nulllinie statt die Achse zu
    // verschieben.
    const stunden = [...new Set(serien.flatMap(s => [...s.jeStunde.keys()]))].sort((a, b) => a - b);

    // viewBox == Anzeigebreite (siehe _cw()): Schrift in echten Pixeln.
    const schmal = !!this._narrow;
    const width = this._cw("ps");
    const height = schmal ? 235 : 310;
    const padding = { top: schmal ? 22 : 25, right: schmal ? 10 : 20,
                      bottom: schmal ? 50 : 58, left: schmal ? 32 : 55 };
    const fsAxis = schmal ? 10 : 12;
    const fsDay = schmal ? 11 : 12;
    const chartW = width - padding.left - padding.right;
    const chartH = height - padding.top - padding.bottom;

    // Bedarf ueber der Nulllinie, Ueberschuss darunter. Die Nulllinie liegt
    // nach dem Verhaeltnis der beiden Spitzen: solange keine Seite unter ein
    // Viertel der Hoehe faellt, ist der Massstab auf beiden Seiten derselbe
    // (kWh je Pixel) und beide nutzen ihren Platz.
    //
    // Bei sehr schiefem Verhaeltnis greift die Viertel-Grenze, und dann ist
    // der Massstab NICHT mehr gleich — an der Anlage gemessen 1242 kWh
    // Ueberschuss gegen 127 kWh Bedarf, ohne Grenze blieben dem Bedarf rund
    // 20 Pixel. Die Lesbarkeit der kleineren Seite wiegt schwerer als die
    // exakte Vergleichbarkeit, und die Achsenbeschriftung nennt beide
    // Endwerte, sodass der Massstabswechsel ablesbar bleibt.
    const alleSalden = serien.flatMap(s => [...s.jeStunde.values()]);
    const maxBedarf = Math.max(0, ...alleSalden.map(v => Math.max(0, v)));
    const maxUeber = Math.max(0, ...alleSalden.map(v => Math.max(0, -v)));
    const spanne = (maxBedarf + maxUeber) || 1;
    const anteilOben = Math.min(0.75, Math.max(0.25, maxBedarf / spanne));
    const nullY = padding.top + chartH * anteilOben;
    const obenPx = nullY - padding.top;
    const untenPx = padding.top + chartH - nullY;
    const yWert = (saldo) => (saldo >= 0
      ? nullY - (saldo / (maxBedarf * 1.05 || 1)) * obenPx
      : nullY + (-saldo / (maxUeber * 1.05 || 1)) * untenPx);
    const xStunde = (i) => padding.left + (i / Math.max(stunden.length - 1, 1)) * chartW;

    const _fmtDay = (dt) => dt.toLocaleDateString("de-DE", {weekday: "short", day: "2-digit", month: "2-digit"});
    const _fmtDayShort = (dt) => dt.toLocaleDateString("de-DE", {day: "2-digit", month: "2-digit"});
    const _dayKey = (dt) => `${dt.getFullYear()}-${dt.getMonth()}-${dt.getDate()}`;
    const achse = stunden.map((k, i) => {
      const dt = new Date(k * 3600000);
      return { x: xStunde(i), hour: dt.getHours(),
               dayLabel: _fmtDay(dt), dayShort: _fmtDayShort(dt), dayKey: _dayKey(dt) };
    });

    // Je Serie eine durchgehende Kurve, die durch die Nulllinie laeuft. Die
    // Flaeche hat dort ihre Basis und kippt von selbst auf die andere Seite.
    // Gefuellt wird nur die erste — zwei Flaechen uebereinander werden Matsch.
    let areaFill = "", lineEl = "", dots = "";
    serien.forEach((serie, idx) => {
      const punkte = stunden.map((k, i) => ({
        x: xStunde(i), saldo: serie.jeStunde.get(k) ?? 0,
      }));
      let linie = `M ${punkte[0].x},${yWert(punkte[0].saldo)}`;
      for (let i = 1; i < punkte.length; i++) linie += ` L ${punkte[i].x},${yWert(punkte[i].saldo)}`;
      if (idx === 0) {
        const flaeche = `M ${punkte[0].x},${nullY}`
          + punkte.map(pt => ` L ${pt.x},${yWert(pt.saldo)}`).join("")
          + ` L ${punkte[punkte.length - 1].x},${nullY} Z`;
        areaFill += `<path d="${flaeche}" fill="${serie.farbe}" fill-opacity="0.1"/>`;
      }
      lineEl += `<path d="${linie}" fill="none" stroke="${serie.farbe}" stroke-width="2.5" stroke-linejoin="round"/>`;
    });
    lineEl += `<line x1="${padding.left}" y1="${nullY}" x2="${width - padding.right}" y2="${nullY}" stroke="var(--divider-color)" stroke-width="1.4"/>`;

    // Punkte samt Tippziel. Jedes Ziel traegt die Werte ALLER Serien dieser
    // Stunde — bei zwei Gemeinschaften liegen die Punkte oft uebereinander,
    // und dann ist es gleichgueltig, welchen man trifft.
    achse.forEach((a, i) => {
      const werte = serien.map(s => {
        const saldo = s.jeStunde.get(stunden[i]);
        return saldo == null ? null : {
          n: s.name, f: s.farbe, u: saldo < 0 ? 1 : 0,
          v: fmtDe(Math.abs(saldo), Math.abs(saldo) >= 100 ? 0 : 1),
        };
      }).filter(Boolean);
      if (!werte.length) return;
      const daten = `data-hour="${String(a.hour).padStart(2, "0")}:00" data-day="${a.dayLabel}"`
        + ` data-eegj="${this._escapeHtml(JSON.stringify(werte))}"`;
      serien.forEach(s => {
        const saldo = s.jeStunde.get(stunden[i]);
        if (saldo == null) return;
        const y = yWert(saldo);
        dots += `<circle class="ps-dot" cx="${a.x}" cy="${y}" r="3.5" fill="${s.farbe}" stroke="var(--card-background-color,#fff)" stroke-width="1.5" ${daten} style="cursor:pointer"></circle>`;
        dots += `<circle class="ps-dot-hit" cx="${a.x}" cy="${y}" r="11" fill="transparent" ${daten} style="cursor:pointer"></circle>`;
      });
    });

    // Tagesgrenze um Mitternacht
    let dayMarkers = "";
    for (let i = 1; i < achse.length; i++) {
      if (achse[i].dayKey !== achse[i - 1].dayKey) {
        const mx = (achse[i - 1].x + achse[i].x) / 2;
        dayMarkers += `<line x1="${mx}" y1="${padding.top + 4}" x2="${mx}" y2="${padding.top + chartH}" stroke="var(--secondary-text-color)" stroke-opacity="0.35" stroke-width="1" stroke-dasharray="2,3"/>`;
      }
    }

    // X-Achse: Uhrzeit, darunter der Tag
    let xLabels = "";
    const labelEvery = Math.max(2, Math.ceil(achse.length / Math.max(2, Math.floor(chartW / 42))));
    achse.forEach((a, i) => {
      if (i % labelEvery === 0) {
        xLabels += `<text x="${a.x}" y="${padding.top + chartH + 14}" text-anchor="middle" font-size="${fsAxis}" fill="var(--secondary-text-color)">${String(a.hour).padStart(2, "0")}:00</text>`;
      }
    });
    const dayRanges = [];
    let curStart = 0;
    for (let i = 1; i <= achse.length; i++) {
      if (i === achse.length || achse[i].dayKey !== achse[curStart].dayKey) {
        dayRanges.push({start: curStart, end: i - 1, label: achse[curStart].dayLabel});
        curStart = i;
      }
    }
    dayRanges.forEach(r => {
      const mx = (achse[r.start].x + achse[r.end].x) / 2;
      // Am Handy nur Tag und Monat — der Wochentag davor sprengt die Spalte.
      const text = schmal ? (achse[r.start].dayShort || r.label) : r.label;
      xLabels += `<text x="${mx}" y="${padding.top + chartH + 30}" text-anchor="middle" font-size="${fsDay}" fill="var(--primary-text-color)" font-weight="500">${text}</text>`;
    });

    // Y-Raster: von der Bedarfsspitze bis zur Ueberschussspitze
    let yLines = "";
    for (const val of [maxBedarf, maxBedarf / 2, 0, -maxUeber / 2, -maxUeber]) {
      if (val === 0 && maxBedarf <= 0 && maxUeber <= 0) continue;
      const y = yWert(val);
      yLines += `<line x1="${padding.left}" y1="${y}" x2="${width - padding.right}" y2="${y}" stroke="var(--divider-color)" stroke-dasharray="4"/>`;
      yLines += `<text x="${padding.left - 5}" y="${y + 4}" text-anchor="end" font-size="${fsAxis}" fill="var(--secondary-text-color)">${fmtDe(val, 0)}</text>`;
    }

    const yAchseX = schmal ? 9 : 14;
    const yLabel = `<text x="${yAchseX}" y="${padding.top + chartH / 2}" text-anchor="middle" font-size="${fsAxis}" fill="var(--secondary-text-color)" transform="rotate(-90,${yAchseX},${padding.top + chartH / 2})">kWh</text>`;

    // Legende: je Gemeinschaft ein Eintrag, untereinander. Nebeneinander
    // sprengen zwei Namen am Handy die Breite.
    const legX = schmal ? padding.left : width - 280;
    let legendHtml = "";
    serien.forEach((s, i) => {
      const y = (schmal ? 3 : 6) + i * (schmal ? 13 : 14);
      legendHtml += `
      <rect x="${legX}" y="${y}" width="9" height="9" fill="${s.farbe}" rx="2"/>
      <text x="${legX + 13}" y="${y + 8}" font-size="${fsDay}" fill="var(--primary-text-color)">${this._escapeHtml(s.name)}</text>`;
    });

    const titelNamen = serien.map(s => s.name).join(" und ");
    const chartTitle = schmal
      ? `<div style="font-size:12px;color:var(--secondary-text-color);margin-bottom:4px">Quelle: PeakShare, ${cacheText}</div>`
      : `<div style="font-size:14px;font-weight:500;color:var(--primary-text-color);margin-bottom:4px">Bedarf und Überschuss — ${this._escapeHtml(titelNamen)} <span style="font-weight:400;font-size:12px;color:var(--secondary-text-color)">(Quelle: PeakShare, ${cacheText})</span></div>`;
    const chartHtml = `<svg data-cw="ps" viewBox="0 0 ${width} ${height}" style="width:100%;height:auto;overflow:visible">${yLines}${yLabel}${areaFill}${dayMarkers}${lineEl}${dots}${xLabels}${legendHtml}</svg>`;
    const tooltipHtml = `<div class="ps-tooltip" style="position:absolute;display:none;pointer-events:none;background:var(--card-background-color,#fff);color:var(--primary-text-color);border:1px solid var(--divider-color);border-radius:8px;padding:6px 10px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,0.18);transform:translate(-50%,-100%);white-space:nowrap;z-index:10"></div>`;

    return planHtml + `<div class="chart-card ps-chart-card" style="margin-top:4px;position:relative;padding:0">${chartTitle}${chartHtml}${tooltipHtml}</div>`;
  }

  async _loadMoreActivity() {
    if (this._activityLoadingMore || !this._activityHasMore) return;
    this._activityLoadingMore = true;
    this._render();
    try {
      const result = await this._hass.callWS({
        type: "eeg_optimizer/get_activity_log",
        offset: this._activityLog.length,
        limit: 100,
      });
      this._activityLog = this._activityLog.concat(result?.entries || []);
      this._activityTotal = result?.total || this._activityTotal;
      this._activityHasMore = result?.has_more || false;
    } catch (e) {
      console.error("Failed to load more activity:", e);
    }
    this._activityLoadingMore = false;
    this._render();
  }

  _subscribeActivityEvents() {
    // Clean up stale subscription first
    if (this._activityUnsub) {
      try { this._activityUnsub(); } catch (_) { /* already gone */ }
      this._activityUnsub = null;
    }
    if (!this._hass?.connection) return;
    try {
      this._hass.connection.subscribeEvents((ev) => {
        try {
          if (ev.data) {
            // Prepend new event (newest first)
            this._activityLog.unshift(ev.data);
            this._activityTotal += 1;
            this._render();
          }
        } catch (err) {
          console.warn("EEG: error in activity event handler:", err);
        }
      }, "eeg_optimizer_activity").then(unsub => {
        this._activityUnsub = unsub;
      }).catch((err) => {
        console.warn("EEG: activity subscription failed, will retry on next hass update:", err);
        this._activityUnsub = null;
      });
    } catch (err) {
      console.warn("EEG: could not subscribe to activity events:", err);
      this._activityUnsub = null;
    }
  }

  /* ── Async data loading ───────────────────────── */

  async _checkPrerequisites() {
    this._wizardLoading = true;
    this._render();
    try {
      this._prerequisites = await this._hass.callWS({
        type: "eeg_optimizer/check_prerequisites",
      });
    } catch (err) {
      console.error("Failed to check prerequisites:", err);
      this._prerequisites = {
        huawei_solar: false,
        solcast_solar: false,
        forecast_solar: false,
      };
    }
    // Auto-select inverter type: first detected (alphabetically by label)
    const p = this._prerequisites;
    if (p) {
      const detected = [
        p.fronius && { key: "fronius_gen24", label: "Fronius" },
        p.huawei_solar && { key: "huawei_sun2000", label: "Huawei" },
        p.kostal_plenticore && KOSTAL_UI_ENABLED && { key: "kostal_plenticore", label: "Kostal" },
        p.sma && { key: "sma_smart_energy", label: "SMA" },
        p.solaredge_modbus_multi && { key: "solaredge_storedge", label: "SolarEdge" },
        p.solax_modbus && { key: "solax_gen4", label: "SolaX" },
      ].filter(Boolean)
        .filter((inv) => istWaehlbarerWr(inv.key))
        .sort((a, b) => a.label.localeCompare(b.label));
      if (detected.length > 0) {
        this._wizardData.inverter_type = detected[0].key;
      }
      // Auto-select forecast source — always prefer Solcast when installed
      if (p.solcast_solar) {
        this._wizardData.forecast_source = "solcast_solar";
        this._applyForecastDefaults("solcast_solar");
      } else if (p.forecast_solar) {
        this._wizardData.forecast_source = "forecast_solar";
        this._applyForecastDefaults("forecast_solar");
      }
    }

    this._wizardLoading = false;
    this._render();
  }

  async _detectSensors() {
    this._wizardLoading = true;
    this._render();
    try {
      this._detectedSensors = await this._hass.callWS({
        type: "eeg_optimizer/detect_sensors",
      });
      if (this._detectedSensors.detected && this._detectedSensors.sensors) {
        // Pre-fill detected sensors only if user hasn't already chosen values
        const sensors = this._detectedSensors.sensors;
        // SolarEdge: SOC + Kapazität laufen über die Driver-Combined-Sensoren —
        // Auto-Detection darf hier nicht den einzelnen i1-Sensor eintragen
        // (das würde den Wizard-Save-Pfad untergraben).
        const isSolarEdge = this._wizardData.inverter_type === "solaredge_storedge";
        // Huawei Master/Slave (≥2 Batteriegeräte): SOC + Kapazität laufen über
        // die Driver-Combined-Sensoren — wie bei SolarEdge den i1-Einzelsensor
        // nicht eintragen, sonst untergräbt das den Combined-Save-Pfad.
        const huaweiDevices = this._detectedSensors.huawei_device_ids || [];
        const isHuaweiMulti = huaweiDevices.length >= 2;
        const skipKeys = (isSolarEdge || isHuaweiMulti)
          ? new Set(["battery_soc_sensor", "battery_capacity_sensor"])
          : new Set();
        for (const [key, val] of Object.entries(sensors)) {
          if (skipKeys.has(key)) continue;
          if (!this._wizardData[key]) {
            this._wizardData[key] = val;
          }
        }
        if (
          this._detectedSensors.huawei_device_id &&
          !this._wizardData.huawei_device_id
        ) {
          this._wizardData.huawei_device_id =
            this._detectedSensors.huawei_device_id;
        }
        // Multi-Device-Liste immer übernehmen (steuert alle Batterien).
        if (huaweiDevices.length) {
          this._wizardData.huawei_device_ids = huaweiDevices;
        }
        // Huawei: auto-erkannte Multi-Inverter-Sensoren IMMER übernehmen — für
        // diese gibt es kein manuelles Wizard-Feld, daher dürfen veraltete/
        // falsche Werte (z. B. ein zuvor erkannter LUNA-Sensor) überschrieben
        // werden, sobald die korrekte Erkennung sie liefert.
        if (isHuaweiMulti) {
          for (const key of ["pv_power_sensor_2", "battery_power_sensor_2"]) {
            if (sensors[key]) this._wizardData[key] = sensors[key];
          }
        }
        // Modbus-Host aus der Quell-Integration (fronius / kostal_plenticore /
        // sma) vorbefüllen — deren Config-Entry kennt die Geräteadresse
        // bereits, der Nutzer muss sie nicht erneut abtippen. Nur wenn das
        // Feld noch leer ist, damit eine bewusste Eingabe erhalten bleibt.
        for (const key of ["fronius_modbus_host", "kostal_modbus_host", "sma_modbus_host"]) {
          if (this._detectedSensors[key] && !this._wizardData[key]) {
            this._wizardData[key] = this._detectedSensors[key];
          }
        }
        // SolaX-Steuer-Entities: Server löst sie per Suffix-Scan auf (auch bei
        // neueren solax_modbus-Versionen mit Mode-Suffixen wie _mode_1_7) —
        // daher direkt übernehmen, ohne solax_prefix vorauszusetzen.
        const solaxKeys = [
          "solax_remotecontrol_power_control",
          "solax_remotecontrol_active_power",
          "solax_remotecontrol_autorepeat_duration",
          "solax_remotecontrol_duration",
          "solax_remotecontrol_trigger",
          "solax_selfuse_discharge_min_soc",
        ];
        for (const key of solaxKeys) {
          if (this._detectedSensors[key] && !this._wizardData[key]) {
            this._wizardData[key] = this._detectedSensors[key];
          }
        }
        // SolarEdge control entity detection
        if (this._detectedSensors.solaredge_prefix) {
          const solaredgeKeys = [
            "solaredge_storage_control_mode",
            "solaredge_storage_command_mode",
            "solaredge_storage_charge_limit",
            "solaredge_storage_discharge_limit",
            "solaredge_storage_backup_reserve",
          ];
          for (const key of solaredgeKeys) {
            if (this._detectedSensors[key] && !this._wizardData[key]) {
              this._wizardData[key] = this._detectedSensors[key];
            }
          }
        }
      }
    } catch (err) {
      console.error("Failed to detect sensors:", err);
      this._detectedSensors = { detected: false, sensors: {} };
    }
    this._wizardLoading = false;
    this._render();
  }

  async _ensureEntityPicker() {
    // We use our own autocomplete, no HA component loading needed
  }

  _applyForecastDefaults(source) {
    if (source === "solcast_solar") {
      // Auto-detect which Solcast naming convention exists
      const states = this._hass?.states || {};
      const pick = (candidates) => candidates.find(id => states[id]) || candidates[0];
      this._wizardData.forecast_remaining_entity =
        pick(SOLCAST_DEFAULTS_CANDIDATES.forecast_remaining_entity);
      this._wizardData.forecast_tomorrow_entity =
        pick(SOLCAST_DEFAULTS_CANDIDATES.forecast_tomorrow_entity);
      // Auto-detect additional Solcast sensors (today + day 3-7)
      const prefix = this._wizardData.forecast_tomorrow_entity.replace(/morgen$/, "");
      // Handle old "fuer_" prefix — tag sensors don't have "fuer_"
      const tagPrefix = prefix.endsWith("fuer_") && !states[prefix + "tag_3"]
        ? prefix.replace(/fuer_$/, "") : prefix;
      const tryFind = (id) => states[id] ? id : "";
      this._wizardData.forecast_today_entity = tryFind(tagPrefix + "heute");
      this._wizardData.forecast_day3_entity = tryFind(tagPrefix + "tag_3");
      this._wizardData.forecast_day4_entity = tryFind(tagPrefix + "tag_4");
      this._wizardData.forecast_day5_entity = tryFind(tagPrefix + "tag_5");
      this._wizardData.forecast_day6_entity = tryFind(tagPrefix + "tag_6");
      this._wizardData.forecast_day7_entity = tryFind(tagPrefix + "tag_7");
    } else {
      this._wizardData.forecast_remaining_entity =
        FORECAST_SOLAR_DEFAULTS.forecast_remaining_entity;
      this._wizardData.forecast_tomorrow_entity =
        FORECAST_SOLAR_DEFAULTS.forecast_tomorrow_entity;
      this._wizardData.forecast_today_entity = "";
      this._wizardData.forecast_day3_entity = "";
      this._wizardData.forecast_day4_entity = "";
      this._wizardData.forecast_day5_entity = "";
      this._wizardData.forecast_day6_entity = "";
      this._wizardData.forecast_day7_entity = "";
    }
  }

  /* ── Hass / panel setters ─────────────────────── */

  set hass(hass) {
    try {
      this._setHassInner(hass);
    } catch (err) {
      console.error("EEG Energy Optimizer: error in set hass():", err);
    }
  }

  _setHassInner(hass) {
    const firstLoad = this._hass === null;
    const oldHass = this._hass;
    this._hass = hass;
    this._lastHassUpdate = Date.now();

    if (firstLoad) {
      this._loadConfigWithRetry();
      return;
    }

    // Detect reconnect: if we lost connection and hass is back, reload
    if (!this._initialized && !this._loadConfigPending) {
      this._loadConfigWithRetry();
      return;
    }

    // Detect connection object change (HA reconnect after network switch)
    if (oldHass && hass && oldHass.connection !== hass.connection) {
      console.info("EEG Energy Optimizer: connection changed (network switch?), reloading");
      this._activityUnsub = null; // old subscription is dead
      this._loadConfigPending = false;
      this._loadConfigWithRetry();
      return;
    }

    // Recover silently-dead activity subscription
    if (this._setupComplete && !this._activityUnsub && this._initialized) {
      console.info("EEG Energy Optimizer: activity subscription missing, re-subscribing");
      this._subscribeActivityEvents();
    }

    // Update entity pickers in shadow DOM with new hass
    if (this._view === "wizard") {
      const pickers = this._shadow.querySelectorAll("ha-entity-picker");
      pickers.forEach((p) => (p.hass = hass));
    }

    // Recover from blank panel (View Transition may wipe or corrupt shadow DOM)
    // Check for the .content div — every successful render produces one.
    if (this._initialized && this._shadow) {
      if (!this._shadow.querySelector(".content")) {
        this._render();
        return;
      }
    }

    // Selective re-render for dashboard: only if watched entities changed
    if (oldHass && this._view === "dashboard") {
      let changed = false;
      const watchList = [...(this._watchedEntities || DEFAULT_WATCHED)];
      if (this._config?.battery_soc_sensor) watchList.push(this._config.battery_soc_sensor);
      // Watch Solcast/Forecast.Solar original sensors for PV chart updates
      const fTomorrow = this._config?.forecast_tomorrow_entity;
      if (fTomorrow && fTomorrow.includes("solcast")) {
        const pfx = fTomorrow.replace(/morgen$/, "");
        ["heute", "morgen", "tag_3", "tag_4", "tag_5", "tag_6", "tag_7"].forEach(s => watchList.push(pfx + s));
      } else if (fTomorrow && fTomorrow.includes("energy_production")) {
        const pfx = fTomorrow.replace(/tomorrow$/, "");
        ["today", "tomorrow"].forEach(s => watchList.push(pfx + s));
      }
      for (const eid of watchList) {
        if (oldHass.states[eid] !== hass.states[eid]) {
          changed = true;
          break;
        }
      }
      if (changed) {
        const now = Date.now();
        // Fahrplan periodisch nachladen (max. alle 60 s) — Quelle für die
        // Fahrplan-Karte und die Job-Laufzeiten in der Statuskarte.
        if (now - this._lastScheduleReload > 60000) {
          this._lastScheduleReload = now;
          this._loadSchedule();
          // Der Verlauf waechst mit der Zeit; die eigene Frist in
          // _loadScheduleHistory verhindert Abfragen im Leerlauf.
          if (this._scheduleOpen) this._loadScheduleHistory();
        }
        // PeakShare-Cache-Alter gelegentlich auffrischen (max. alle 5 min)
        if (this._config?.enable_peakshare !== false &&
            now - this._lastPeakshareReload > 300000) {
          this._lastPeakshareReload = now;
          this._loadPeakShareData();
        }
        this._render();
      }
    }
  }

  set panel(panel) {
    this._panel = panel;
  }

  set narrow(narrow) {
    const vorher = this._narrow;
    this._narrow = narrow;
    if (!!vorher !== !!narrow) this._ansichtsPrefsLaden();
    this._render();
  }

  // Zwei Voreinstellungen unterscheiden sich zwischen Handy und Desktop:
  // Am Handy zeigt die Statuskarte das Flussbild — es ist dort die
  // uebersichtlichste Darstellung — und der Fahrplan startet zugeklappt,
  // weil er sonst zwei Drittel des Bildschirms belegt, bevor irgendetwas
  // anderes kommt. Beides wird je Ansichtsbreite getrennt gemerkt, damit
  // eine Wahl am Desktop die am Handy nicht ueberschreibt.
  _ansichtsPrefsLaden() {
    if (this._narrow) {
      this._statusViewVariant = this._loadPref("status_view_variant_narrow", "flow", ["values", "flow"]);
      this._scheduleOpen = this._loadPref("schedule_open_narrow", "0", ["0", "1"]) === "1";
    } else {
      this._statusViewVariant = this._loadPref("status_view_variant", "values", ["values", "flow"]);
      this._scheduleOpen = this._loadPref("schedule_open", "1", ["0", "1"]) === "1";
    }
  }

  async _loadConfigWithRetry(attempt = 0) {
    if (this._loadConfigPending) return;
    this._loadConfigPending = true;
    try {
      await this._loadConfig();
    } catch (_) {
      // Retry up to 5 times with increasing delay (2s, 4s, 6s, 8s, 10s)
      if (attempt < 5) {
        this._loadConfigPending = false;
        const delay = (attempt + 1) * 2000;
        console.warn(`EEG Energy Optimizer: config load failed, retry ${attempt + 1}/5 in ${delay}ms`);
        setTimeout(() => this._loadConfigWithRetry(attempt + 1), delay);
        return;
      }
      console.error("EEG Energy Optimizer: config load failed after 5 retries");
    }
    this._loadConfigPending = false;
  }

  async _loadConfig() {
    try {
      const result = await this._hass.callWS({
        type: "eeg_optimizer/get_config",
      });
      this._config = result;
      this._setupComplete = !!result.setup_complete;
      await this._resolveEntityIds();
    } catch (err) {
      if (err.code === "not_configured") {
        this._setupComplete = false;
      } else {
        // Connection error — rethrow so retry logic kicks in
        throw err;
      }
      this._config = null;
    }
    if (this._setupComplete) {
      // Telemetrie-Status laden (Community-Statistik). Fire-and-forget — Fehler dürfen
      // den Settings-Load nicht blockieren.
      this._hass.callWS({ type: "eeg_optimizer/telemetry_get_status" })
        .then(s => { this._telemetryStatus = s; this._render(); })
        .catch(err => {
          console.warn("EEG Optimizer: telemetry status load failed", err);
          this._telemetryStatus = { configured: false, enabled: false, registered: false };
          this._render();
        });
    }

    this._initialized = true;
    this._render();

    // Load activity log, schedule, and subscribe to live events
    if (this._setupComplete) {
      this._loadActivityLog();
      // Fahrplan sofort laden — die Statuskarte zeigt daraus die Laufzeit
      // des Rechen-Jobs, ohne dass die Fahrplan-Karte offen sein muss.
      this._loadSchedule();
      // Steht die Karte schon offen, den gewählten Verlauf gleich mitholen —
      // sonst stünde dort bis zum ersten Zyklus „Kein Verlauf geladen".
      if (this._scheduleOpen) this._loadScheduleHistory();
      if (this._config?.enable_peakshare !== false) {
        this._loadPeakShareData();
      }
      // Re-subscribe if previous subscription was lost (e.g. after reconnect)
      if (!this._activityUnsub) {
        this._subscribeActivityEvents();
      }
    }
  }

  async _resolveEntityIds() {
    if (!this._config?.entry_id) return;

    // Die Registry ist die einzige verlässliche Quelle: HA bildet die entity_id
    // aus dem Anzeigenamen, nicht aus der unique_id. Schlägt der Befehl fehl
    // (alte Backend-Version, Verbindungsfehler), greift die Namenssuche unten.
    let registry = null;
    try {
      const result = await this._hass.callWS({ type: "eeg_optimizer/get_entity_ids" });
      registry = result?.entity_ids || null;
    } catch (err) {
      console.warn("EEG Energy Optimizer: Entity-Registry nicht lesbar, Fallback auf Namensmuster", err);
    }

    this._entityIds = {};
    for (const [key, suffix] of Object.entries(SENSOR_SUFFIXES)) {
      this._entityIds[key] = registry?.[suffix] || this._guessEntityId("sensor", suffix);
    }
    this._entityIds.select =
      registry?.[SELECT_SUFFIX] || this._guessEntityId("select", SELECT_SUFFIX);

    // Build watched list for state subscriptions
    this._watchedEntities = [
      this._entityIds.select,
      ...Object.values(this._entityIds).filter(id => id.startsWith("sensor."))
    ];
  }

  _guessEntityId(domain, suffix) {
    // Fallback ohne Registry: nach dem Namensmuster suchen. Trifft nur zu,
    // solange Anzeigename und unique_id-Suffix übereinstimmen.
    const pattern = `${domain}.eeg_energy_optimizer_${suffix}`;
    if (this._hass?.states?.[pattern]) return pattern;
    const found = Object.keys(this._hass?.states || {}).find(
      eid => eid === pattern || eid.startsWith(pattern + "_")
    );
    return found || pattern;
  }

  /* ── Entity picker helper ─────────────────────── */

  _entityPickerHtml(field, value, label, helpText, domain) {
    // Show current sensor value if entity exists in HA
    let valuePreview = "";
    if (value && this._hass?.states?.[value]) {
      const stateObj = this._hass.states[value];
      const stateVal = stateObj.state;
      const unit = stateObj.attributes?.unit_of_measurement || "";
      const friendly = stateObj.attributes?.friendly_name || "";
      if (stateVal !== "unavailable" && stateVal !== "unknown") {
        valuePreview = `<div class="ep-value-preview" data-preview-for="${field}">Aktuell: <strong>${stateVal}${unit ? " " + unit : ""}</strong>${friendly ? ` — ${friendly}` : ""}</div>`;
      } else {
        valuePreview = `<div class="ep-value-preview unavailable" data-preview-for="${field}">Sensor nicht verfügbar</div>`;
      }
    }
    return `
      <div class="field-group entity-picker-wrap">
        <label>${label}</label>
        <div class="ep-container">
          <input type="text" class="entity-input" data-field="${field}" data-domain="${domain || ""}"
                 value="${value || ""}" placeholder="Tippen zum Suchen..." autocomplete="off">
          <svg class="ep-chevron" viewBox="0 0 24 24" width="20" height="20">
            <path fill="currentColor" d="M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6z"/>
          </svg>
          <div class="ep-dropdown" data-for="${field}"></div>
        </div>
        ${valuePreview}
        ${helpText ? `<div class="help-text">${helpText}</div>` : ""}
      </div>`;
  }

  /** Bind focus/input events to entity picker inputs for custom dropdown. */
  _bindEntityPickers() {
    if (!this._hass) return;
    const inputs = this._shadow.querySelectorAll("input.entity-input");
    inputs.forEach((input) => {
      const domain = input.dataset.domain;
      const field = input.dataset.field;
      const dropdown = this._shadow.querySelector(`.ep-dropdown[data-for="${field}"]`);
      if (!dropdown) return;

      const states = this._hass.states || {};
      const allEntities = Object.keys(states)
        .filter((eid) => !domain || eid.startsWith(domain + "."))
        .sort()
        .map((eid) => ({
          id: eid,
          name: states[eid]?.attributes?.friendly_name || "",
        }));

      const showDropdown = (filter) => {
        const q = (filter || "").toLowerCase();
        const matches = allEntities
          .filter((e) => !q || e.id.includes(q) || e.name.toLowerCase().includes(q))
          .slice(0, 50);
        if (matches.length === 0) {
          dropdown.style.display = "none";
          return;
        }
        dropdown.innerHTML = matches
          .map((e) => `<div class="ep-option" data-value="${e.id}">
            <span class="ep-name">${e.name || e.id}</span>
            <span class="ep-id">${e.id}</span>
          </div>`)
          .join("");
        dropdown.style.display = "block";
      };

      input.addEventListener("focus", () => showDropdown(input.value));
      input.addEventListener("input", () => {
        this._wizardData[field] = input.value;
        showDropdown(input.value);
        // Sensorfelder sind im Wechselrichter-Schritt Pflicht — Knopf nachziehen.
        this._syncWeiterKnopf();
      });

      const updatePreview = (entityId) => {
        const preview = this._shadow.querySelector(`.ep-value-preview[data-preview-for="${field}"]`);
        const stateObj = entityId && states[entityId];
        if (stateObj) {
          const sv = stateObj.state;
          const unit = stateObj.attributes?.unit_of_measurement || "";
          const friendly = stateObj.attributes?.friendly_name || "";
          const unavail = sv === "unavailable" || sv === "unknown";
          if (!preview) {
            // Insert preview after ep-container
            const container = input.closest(".ep-container");
            const div = document.createElement("div");
            div.className = "ep-value-preview" + (unavail ? " unavailable" : "");
            div.setAttribute("data-preview-for", field);
            div.innerHTML = unavail ? "Sensor nicht verfügbar" : `Aktuell: <strong>${sv}${unit ? " " + unit : ""}</strong>${friendly ? ` — ${friendly}` : ""}`;
            container.parentNode.insertBefore(div, container.nextSibling);
          } else {
            preview.className = "ep-value-preview" + (unavail ? " unavailable" : "");
            preview.innerHTML = unavail ? "Sensor nicht verfügbar" : `Aktuell: <strong>${sv}${unit ? " " + unit : ""}</strong>${friendly ? ` — ${friendly}` : ""}`;
          }
        } else if (preview) {
          preview.remove();
        }
      };

      dropdown.addEventListener("mousedown", (ev) => {
        ev.preventDefault(); // Prevent blur before click registers
        const opt = ev.target.closest(".ep-option");
        if (opt) {
          input.value = opt.dataset.value;
          this._wizardData[field] = opt.dataset.value;
          dropdown.style.display = "none";
          updatePreview(opt.dataset.value);
          this._syncWeiterKnopf();
        }
      });

      input.addEventListener("blur", () => {
        setTimeout(() => {
          dropdown.style.display = "none";
          updatePreview(input.value);
        }, 150);
      });
    });
  }

  /* ── Wizard step rendering ────────────────────── */

  _renderWizard() {
    const step = this._wizardStep;
    // Fortschritt nur über die aktuell sichtbaren Schritte (bedingte Steps wie
    // Expert/Einspeisebegrenzung werden nicht mitgezählt).
    const visibleSteps = WIZARD_STEPS
      .map((_, i) => i)
      .filter((i) => this._stepVisible(i));
    const total = visibleSteps.length;
    const displayStep = Math.max(0, visibleSteps.indexOf(step));
    const progress = ((displayStep + 1) / total) * 100;

    // Namensbasiertes Renderer-Mapping — robust gegen Einfügen neuer Schritte.
    const RENDERERS = {
      "Willkommen": "_renderStepWillkommen",
      "Wechselrichter": "_renderStepWechselrichter",
      "Batterie": "_renderStepBatterie",
      "PV-Prognose": "_renderStepPrognose",
      "Anlage & Batterie": "_renderStepAnlage",
      "Tarife & Gemeinschaft": "_renderStepTarife",
      "Zusammenfassung": "_renderStepZusammenfassung",
    };
    const rendererName = RENDERERS[WIZARD_STEPS[step]];
    const stepContent = rendererName ? this[rendererName]() : "";

    const backBtn =
      step > 0
        ? `<button class="btn-secondary" data-action="prev-step">Zurück</button>`
        : `<div></div>`;

    let forwardBtn = "";
    if (step === WIZARD_STEPS.length - 1) {
      forwardBtn = `<button class="btn-primary" data-action="finish-wizard"${
        this._wizardLoading ? " disabled" : ""
      }>Fertig</button>`;
    } else {
      const probing = !!this._froniusProbing || !!this._kostalProbing || !!this._smaProbing;
      const disabled = (this._isNextDisabled() || probing) ? " btn-disabled" : "";
      const label = this._froniusProbing
        ? "Prüfe Fronius-Verbindung…"
        : this._kostalProbing
        ? "Prüfe Kostal-Verbindung…"
        : this._smaProbing
        ? "Prüfe SMA-Verbindung…"
        : "Weiter";
      forwardBtn = `<button class="btn-primary${disabled}" data-action="next-step">${label}</button>`;
    }

    return `
      <div class="step-indicator">
        <span>Schritt ${displayStep + 1} von ${total} — ${WIZARD_STEPS[step]}</span>
        <label class="expert-toggle">
          <input type="checkbox" data-field="expert_mode"
                 ${this._wizardData.expert_mode ? "checked" : ""}>
          <span>Expertenmodus</span>
        </label>
      </div>
      <div class="progress-bar">
        <div class="progress-bar-fill" style="width:${progress}%"></div>
      </div>
      <div class="card">
        <h2>${WIZARD_STEPS[step]}</h2>
        ${this._wizardLoading ? '<div class="loading">Laden...</div>' : stepContent}
        <div class="wizard-nav">
          ${backBtn}
          ${forwardBtn}
        </div>
      </div>`;
  }

  _isNextDisabled() {
    const name = WIZARD_STEPS[this._wizardStep];
    // Wechselrichter: sperren, solange keine unterstützte Integration
    // installiert ist oder Hausverbrauch-Sensoren fehlen.
    if (name === "Wechselrichter") {
      const p = this._prerequisites;
      if (p && !p.huawei_solar && !p.solax_modbus && !p.solaredge_modbus_multi && !p.fronius && !p.kostal_plenticore && !p.sma) return true;
      const d = this._wizardData;
      if (!d.inverter_type) return true;
      if (!d.pv_power_sensor) return true;
      // Fronius and SMA require the directional pair (charge/discharge,
      // export/import). Other inverters use a single signed sensor each.
      if (d.inverter_type === "fronius_gen24" || d.inverter_type === "sma_smart_energy") {
        if (!d.battery_power_charge_sensor || !d.battery_power_discharge_sensor) return true;
        if (!d.grid_power_export_sensor || !d.grid_power_import_sensor) return true;
      } else {
        if (!d.battery_power_sensor || !d.grid_power_sensor) return true;
      }
    }
    // Anlage & Batterie: Leistungsdaten sind Pflicht — beide begrenzen das
    // Modell, ein Rateversuch aus der PV-Spitze war zu oft daneben.
    if (name === "Anlage & Batterie") {
      const d = this._wizardData;
      if (!(Number(d.inverter_ac_limit_kw) > 0)) return true;
      if (!(Number(d.pv_peak_kwp) > 0)) return true;
      if (d.grid_export_limit_enabled && !(Number(d.grid_export_limit_kw) > 0)) return true;
    }
    // PV-Prognose: sperren, wenn keine Prognose-Integration installiert ist.
    if (
      name === "PV-Prognose" &&
      this._prerequisites &&
      !this._prerequisites.solcast_solar &&
      !this._prerequisites.forecast_solar
    ) {
      return true;
    }
    return false;
  }

  // Der Weiter-Knopf haengt an den Pflichtfeldern des Schritts, sein Zustand
  // entsteht aber nur beim Rendern — und Wizard-Zahlenfelder rendern bewusst
  // nicht (ein Render beim Tippen kostet den Fokus, und ein Render zwischen
  // mousedown und mouseup verschluckt den Klick). Ohne diesen Nachzug blieb
  // der Knopf nach der letzten Pflichteingabe noch gesperrt: btn-disabled
  // traegt pointer-events:none, der Knopf ist dann wirklich tot und nicht nur
  // ausgegraut. Hier wird deshalb NUR die Klasse getauscht, kein Neuaufbau.
  _syncWeiterKnopf() {
    const btn = this._shadow?.querySelector('button[data-action="next-step"]');
    if (!btn) return;
    const probing = !!this._froniusProbing || !!this._kostalProbing || !!this._smaProbing;
    btn.classList.toggle("btn-disabled", this._isNextDisabled() || probing);
  }

  /* ── Schritt: Willkommen ──────────────────────── */

  _renderStepWillkommen() {
    return `
      <div style="text-align:center;margin-bottom:20px">
        <img src="/eeg_optimizer_panel/logo.png" alt="EEG Energy Optimizer" style="max-width:180px;height:auto">
      </div>
      <p style="line-height:1.6;margin-bottom:20px">
        Diese Home Assistant Integration optimiert deine Hausbatterie für die Energiegemeinschaft (EEG).
        Jede Minute wird der erlösbeste Lade- und Entladeplan über 48 Stunden gerechnet — allein nach dem
        Preis je Viertelstunde. Braucht deine Energiegemeinschaft gerade Strom, ist deine Kilowattstunde
        dort mehr wert, und der Fahrplan speist ein. Zeigt der Preisverlauf keinen Mehrwert, passiert nichts.
      </p>
      <h3 style="margin-bottom:8px">Was du brauchst</h3>
      <ul style="line-height:1.8;margin-bottom:20px;padding-left:20px">
        <li>Einen Fronius Gen24 oder Huawei SUN2000 mit Batteriespeicher</li>
        <li>Eine PV-Prognose-Integration (Solcast Solar oder Forecast.Solar)</li>
      </ul>
      <h3 style="margin-bottom:8px">Getestete Setups</h3>
      <ul style="line-height:1.8;padding-left:20px">
        <li>Fronius Gen24 mit BYD Batteriespeicher</li>
        <li>Huawei SUN2000 mit LUNA2000 Batteriespeicher</li>
      </ul>`;
  }

  /* ── Schritt: Wechselrichter (Typ + Sensoren) ──── */

  _renderStepWechselrichter() {
    const p = this._prerequisites;
    const huaweiOk = p && p.huawei_solar;
    const solaxOk = p && p.solax_modbus;
    const solaredgeOk = p && p.solaredge_modbus_multi;
    const froniusOk = p && p.fronius;
    const kostalOk = p && p.kostal_plenticore;
    const smaOk = p && p.sma;
    const selected = this._wizardData.inverter_type || "";
    const huaweiSelected = selected === "huawei_sun2000";
    const solaxSelected = selected === "solax_gen4";
    const solaredgeSelected = selected === "solaredge_storedge";
    const froniusSelected = selected === "fronius_gen24";
    const kostalSelected = selected === "kostal_plenticore";
    const smaSelected = selected === "sma_smart_energy";

    const huaweiBadge = huaweiOk
      ? '<span class="status-badge installed">Installiert</span>'
      : '<span class="status-badge missing">Nicht installiert</span>';

    const solaxBadge = solaxOk
      ? '<span class="status-badge installed">Installiert</span>'
      : '<span class="status-badge missing">Nicht installiert</span>';

    const solaredgeBadge = solaredgeOk
      ? '<span class="status-badge installed">Installiert</span>'
      : '<span class="status-badge missing">Nicht installiert</span>';

    const froniusBadge = froniusOk
      ? '<span class="status-badge installed">Installiert</span>'
      : '<span class="status-badge missing">Nicht installiert</span>';

    const kostalBadge = kostalOk
      ? '<span class="status-badge installed">Installiert</span>'
      : '<span class="status-badge missing">Nicht installiert</span>';

    const smaBadge = smaOk
      ? '<span class="status-badge installed">Installiert</span>'
      : '<span class="status-badge missing">Nicht installiert</span>';


    const pvHelp = huaweiSelected
      ? "Aktuelle PV-Produktion in W oder kW (Huawei: sensor.inverter_eingangsleistung)."
      : solaredgeSelected
      ? "Aktuelle PV-Produktion in W (SolarEdge: sensor.solaredge_[i1_]ac_power)."
      : froniusSelected
      ? "Aktuelle PV-Produktion in W (Fronius: sensor.*_power_photovoltaics oder *_pv_leistung)."
      : kostalSelected
      ? "Aktuelle PV-Produktion in W (Kostal: sensor.*_sum_power_of_all_pv_dc_inputs — Summe aller PV-Eingänge. Achtung: *_solar_power enthält auch die Batterieentladung und ist ungeeignet)."
      : smaSelected
      ? "Aktuelle PV-Produktion in W (SMA: sensor.*_pv_power)."
      : "Aktuelle PV-Produktion in W (SolaX: sensor.solax_energy_dashboard_solax_solar_power).";
    const batteryHelp = huaweiSelected
      ? "Lade- und Entladeleistung der Batterie in W oder kW (Huawei: sensor.batteries_lade_entladeleistung)."
      : solaredgeSelected
      ? "Lade- und Entladeleistung der Batterie in W (SolarEdge: sensor.solaredge_[i1_]b1_dc_power)."
      : froniusSelected
      ? "Lade- und Entladeleistung der Batterie in W (Fronius: sensor.*_power_battery oder *_leistung_batterie). Bei Fronius-Installationen mit getrennten Lade-/Entladesensoren bitte den signed Sensor wählen."
      : kostalSelected
      ? "Lade- und Entladeleistung der Batterie in W (Kostal: sensor.*_battery_power — positiv = Entladen, wird automatisch umgerechnet)."
      : "Lade- und Entladeleistung der Batterie in W (SolaX: sensor.solax_energy_dashboard_solax_battery_power).";
    const gridHelp = huaweiSelected
      ? "Wirkleistung am Netzanschluss in W oder kW (Huawei: sensor.power_meter_wirkleistung)."
      : solaredgeSelected
      ? "Wirkleistung am Netzanschluss in W (SolarEdge: sensor.solaredge_[i1_]m1_ac_power)."
      : froniusSelected
      ? "Wirkleistung am Netzanschluss in W (Fronius: sensor.*_power_grid oder *_leistung_netz). Bei Fronius-Installationen mit getrennten Bezugs-/Einspeisesensoren bitte den signed Sensor wählen."
      : kostalSelected
      ? "Wirkleistung am Netzanschluss in W (Kostal: sensor.*_grid_power — positiv = Bezug, wird automatisch umgerechnet)."
      : "Wirkleistung am Netzanschluss in W (SolaX: sensor.solax_energy_dashboard_solax_grid_power).";

    // Build inverter cards, sort: detected first (alphabetically), then undetected (alphabetically)
    const inverterDefs = [
      { key: "huawei_sun2000", label: "Huawei SUN2000", subtitle: "", detected: huaweiOk, badge: huaweiBadge, dialog: "huawei",
        logo: `<img src="https://brands.home-assistant.io/huawei_solar/logo.png" alt="Huawei" style="max-width:120px;max-height:60px;height:auto" onerror="this.style.display='none'">` },
      { key: "solax_gen4", label: "SolaX Gen4+", subtitle: "Gen4, Gen5, Gen6 · nur Anzeige — Steuerung derzeit nur Fronius und Huawei", detected: solaxOk, badge: solaxBadge, dialog: "solax",
        logo: `<span style="font-size:32px">SolaX</span>` },
      { key: "solaredge_storedge", label: "SolarEdge", subtitle: "StorEdge Batteriespeicher · nur Anzeige — Steuerung derzeit nur Fronius und Huawei", detected: solaredgeOk, badge: solaredgeBadge, dialog: "solaredge",
        logo: `<img src="https://brands.home-assistant.io/_/solaredge/logo.png" alt="SolarEdge" style="max-width:120px;max-height:60px;height:auto" onerror="this.outerHTML='<span style=font-size:32px>SolarEdge</span>'">` },
      { key: "fronius_gen24", label: "Fronius Gen24", subtitle: "mit BYD Batteriespeicher", detected: froniusOk, badge: froniusBadge, dialog: "fronius",
        logo: `<img src="https://brands.home-assistant.io/fronius/logo.png" alt="Fronius" style="max-width:120px;max-height:60px;height:auto" onerror="this.outerHTML='<span style=font-size:32px>Fronius</span>'">` },
      { key: "kostal_plenticore", label: "Kostal Plenticore", subtitle: "mit BYD Batteriespeicher · nur Anzeige — Steuerung derzeit nur Fronius und Huawei", detected: kostalOk, badge: kostalBadge, dialog: "kostal",
        logo: `<img src="https://brands.home-assistant.io/kostal_plenticore/logo.png" alt="Kostal" style="max-width:120px;max-height:60px;height:auto" onerror="this.outerHTML='<span style=font-size:32px>Kostal</span>'">` },
      { key: "sma_smart_energy", label: "SMA Smart Energy", subtitle: "Tripower/Sunny Boy mit Batteriespeicher · nur Anzeige — Steuerung derzeit nur Fronius und Huawei", detected: smaOk, badge: smaBadge, dialog: "sma",
        logo: `<img src="https://brands.home-assistant.io/sma/logo.png" alt="SMA" style="max-width:120px;max-height:60px;height:auto" onerror="this.outerHTML='<span style=font-size:32px>SMA</span>'">` },
    ].filter(inv =>
      inv.key !== "kostal_plenticore" || KOSTAL_UI_ENABLED || kostalSelected
    ).filter(inv =>
      istWaehlbarerWr(inv.key) || inv.key === selected
    );
    inverterDefs.sort((a, b) => {
      if (a.detected !== b.detected) return a.detected ? -1 : 1;
      return a.label.localeCompare(b.label);
    });

    const inverterCards = inverterDefs.map(inv => {
      const isSel = selected === inv.key;
      const sub = inv.subtitle ? `<p style="font-size:11px;color:var(--secondary-text-color);margin:0 0 8px">${inv.subtitle}</p>` : "";
      const guide = inv.dialog ? `<button class="btn-secondary" style="margin-top:8px" data-action="show-dialog" data-dialog="${inv.dialog}">Anleitung</button>` : "";
      return `<div class="card forecast-option ${isSel ? "selected" : ""}" style="padding:16px;cursor:pointer;text-align:center;display:flex;flex-direction:column;align-items:center" data-action="select-inverter" data-value="${inv.key}">
          <div style="height:60px;display:flex;align-items:center;justify-content:center;margin-bottom:8px">${inv.logo}</div>
          <h3 style="margin:0 0 ${inv.subtitle ? "4px" : "8px"}">${inv.label}</h3>
          ${sub}${inv.badge}${guide}
        </div>`;
    }).join("\n        ");

    return `
      <p style="margin-bottom:12px;color:var(--secondary-text-color)">Wähle deinen Wechselrichter-Typ:</p>
      <div class="prereq-cards" style="display:grid;grid-template-columns:repeat(auto-fit, minmax(150px, 1fr));gap:16px;margin-bottom:16px">
        ${inverterCards}
      </div>
      ${huaweiSelected || solaxSelected || solaredgeSelected || froniusSelected || kostalSelected || smaSelected ? `
      <div class="card" style="padding:16px;margin-bottom:16px">
        <h3 style="margin:0 0 4px">Hausverbrauch-Sensoren</h3>
        <p style="font-size:13px;color:var(--secondary-text-color);margin:0 0 12px">
          Diese Sensoren werden f&uuml;r die Berechnung des Hausverbrauchs verwendet (PV &minus; Batterie &minus; Netz).
        </p>
        ${this._renderHuaweiMultiInfo()}
        ${this._entityPickerHtml(
          "pv_power_sensor",
          this._wizardData.pv_power_sensor,
          "PV-Eingangsleistung *",
          pvHelp,
          "sensor"
        )}
        ${froniusSelected ? `
          <p style="font-size:12px;color:var(--secondary-text-color);margin:8px 0 4px;line-height:1.5">
            Fronius liefert Batterie- und Netzleistung als <strong>zwei getrennte, immer positive Sensoren</strong>
            (Lade-/Entladeleistung bzw. Bezug/Einspeisung). Trage je beide ein &mdash; die Integration kombiniert sie automatisch
            zu signed Werten und legt die kombinierten Sensoren mit Verlaufsdaten an.
          </p>
          ${this._entityPickerHtml(
            "battery_power_charge_sensor",
            this._wizardData.battery_power_charge_sensor,
            "Batterie-Ladeleistung *",
            "Positiver W-Wert beim Laden, 0 sonst (Fronius: sensor.*_battery_power_charging oder *_ladeleistung).",
            "sensor"
          )}
          ${this._entityPickerHtml(
            "battery_power_discharge_sensor",
            this._wizardData.battery_power_discharge_sensor,
            "Batterie-Entladeleistung *",
            "Positiver W-Wert beim Entladen, 0 sonst (Fronius: sensor.*_battery_power_discharging oder *_entladeleistung).",
            "sensor"
          )}
          ${this._entityPickerHtml(
            "grid_power_export_sensor",
            this._wizardData.grid_power_export_sensor,
            "Netzeinspeisung *",
            "Positiver W-Wert bei Einspeisung, 0 sonst (Fronius: sensor.*_leistung_netzeinspeisung).",
            "sensor"
          )}
          ${this._entityPickerHtml(
            "grid_power_import_sensor",
            this._wizardData.grid_power_import_sensor,
            "Netzbezug *",
            "Positiver W-Wert bei Bezug, 0 sonst (Fronius: sensor.*_leistung_netzbezug).",
            "sensor"
          )}
        ` : smaSelected ? `
          <p style="font-size:12px;color:var(--secondary-text-color);margin:8px 0 4px;line-height:1.5">
            SMA liefert Batterie- und Netzleistung als <strong>zwei getrennte, immer positive Sensoren</strong>
            (Lade-/Entladeleistung bzw. Einspeisung/Bezug). Trage je beide ein &mdash; die Integration kombiniert sie automatisch
            zu signed Werten und legt die kombinierten Sensoren mit Verlaufsdaten an.
            Hinweis: <code>sensor.*_grid_power</code> ist bei SMA die AC-Ausgangsleistung des Wechselrichters, NICHT der Netzanschluss &mdash; nicht verwenden.
          </p>
          ${this._entityPickerHtml(
            "battery_power_charge_sensor",
            this._wizardData.battery_power_charge_sensor,
            "Batterie-Ladeleistung *",
            "Positiver W-Wert beim Laden, 0 sonst (SMA: sensor.*_battery_power_charge_total).",
            "sensor"
          )}
          ${this._entityPickerHtml(
            "battery_power_discharge_sensor",
            this._wizardData.battery_power_discharge_sensor,
            "Batterie-Entladeleistung *",
            "Positiver W-Wert beim Entladen, 0 sonst (SMA: sensor.*_battery_power_discharge_total).",
            "sensor"
          )}
          ${this._entityPickerHtml(
            "grid_power_export_sensor",
            this._wizardData.grid_power_export_sensor,
            "Netzeinspeisung *",
            "Positiver W-Wert bei Einspeisung, 0 sonst (SMA: sensor.*_metering_power_supplied).",
            "sensor"
          )}
          ${this._entityPickerHtml(
            "grid_power_import_sensor",
            this._wizardData.grid_power_import_sensor,
            "Netzbezug *",
            "Positiver W-Wert bei Bezug, 0 sonst (SMA: sensor.*_metering_power_absorbed).",
            "sensor"
          )}
        ` : `
          ${this._entityPickerHtml(
            "battery_power_sensor",
            this._wizardData.battery_power_sensor,
            "Batterie Lade-/Entladeleistung *",
            batteryHelp,
            "sensor"
          )}
          ${this._entityPickerHtml(
            "grid_power_sensor",
            this._wizardData.grid_power_sensor,
            "Netzbezug/-einspeisung *",
            gridHelp,
            "sensor"
          )}
        `}
        ${solaxSelected && this._wizardData.expert_mode ? this._entityPickerHtml(
          "pv_power_sensor_2",
          this._wizardData.pv_power_sensor_2,
          "Zweiter PV-Sensor (optional)",
          "Für Anlagen mit Generator-Wechselrichter über Meter 2 (sensor.solax_inverter_meter_2_measured_power).",
          "sensor"
        ) : ""}
        ${kostalSelected && this._wizardData.expert_mode ? this._entityPickerHtml(
          "pv_power_sensor_2",
          this._wizardData.pv_power_sensor_2,
          "Zweiter PV-Sensor (optional)",
          "Für Anlagen mit zweitem Kostal-Wechselrichter ohne Batterie (sensor.<name2>_sum_power_of_all_pv_dc_inputs) — wird automatisch erkannt und zur PV-Leistung addiert.",
          "sensor"
        ) : ""}
        ${this._wizardData.inverter_type === "huawei_sun2000"
          && (this._wizardData.huawei_device_ids || []).length >= 2
          && this._wizardData.expert_mode ? `
          ${this._entityPickerHtml(
            "pv_power_sensor_2",
            this._wizardData.pv_power_sensor_2,
            "PV-Eingangsleistung zweiter Wechselrichter",
            "Nur ändern, wenn die Auto-Erkennung den zweiten Sensor nicht findet.",
            "sensor"
          )}
          ${this._entityPickerHtml(
            "battery_power_sensor_2",
            this._wizardData.battery_power_sensor_2,
            "Batterieleistung zweiter Wechselrichter",
            "Nur ändern, wenn die Auto-Erkennung den zweiten Sensor nicht findet.",
            "sensor"
          )}` : ""}
      </div>
      ${froniusSelected ? `
      <div class="card" style="padding:16px;margin-bottom:16px">
        <h3 style="margin:0 0 4px">Modbus TCP Verbindung</h3>
        <p style="font-size:13px;color:var(--secondary-text-color);margin:0 0 12px">
          IP-Adresse und Port f\u00fcr die direkte Modbus-Verbindung zum Wechselrichter (f\u00fcr Batterie-Steuerung).
        </p>
        <div style="margin-bottom:12px">
          <label style="display:block;font-weight:500;margin-bottom:4px">Modbus IP-Adresse *</label>
          <input type="text" value="${this._wizardData.fronius_modbus_host || ""}"
                 data-field="fronius_modbus_host" placeholder="z.B. 192.168.1.100"
                 style="width:100%;padding:8px;border:1px solid var(--divider-color);border-radius:4px;background:var(--card-background-color);color:var(--primary-text-color)">
          <div class="help-text">Die IP-Adresse des Fronius Wechselrichters (gleiche wie im Fronius Web-Interface).</div>
        </div>
        <div style="margin-bottom:12px">
          <label style="display:block;font-weight:500;margin-bottom:4px">Modbus Port</label>
          <input type="number" value="${this._wizardData.fronius_modbus_port || 502}"
                 data-field="fronius_modbus_port" min="1" max="65535"
                 style="width:100%;padding:8px;border:1px solid var(--divider-color);border-radius:4px;background:var(--card-background-color);color:var(--primary-text-color)">
          <div class="help-text">Standard: 502. Manche Installationen nutzen 1502.</div>
        </div>
      </div>` : ""}
      ${kostalSelected ? `
      <div class="card" style="padding:16px;margin-bottom:16px">
        <h3 style="margin:0 0 4px">Modbus TCP Verbindung</h3>
        <p style="font-size:13px;color:var(--secondary-text-color);margin:0 0 12px">
          IP-Adresse und Port für die direkte Modbus-Verbindung zum Wechselrichter (für Batterie-Steuerung).
          Modbus TCP muss im Kostal-Webserver aktiviert sein (Einstellungen &rarr; Modbus/SunSpec) &mdash; siehe Anleitung.
        </p>
        <div style="margin-bottom:12px">
          <label style="display:block;font-weight:500;margin-bottom:4px">Modbus IP-Adresse *</label>
          <input type="text" value="${this._wizardData.kostal_modbus_host || ""}"
                 data-field="kostal_modbus_host" placeholder="z.B. 192.168.1.100"
                 style="width:100%;padding:8px;border:1px solid var(--divider-color);border-radius:4px;background:var(--card-background-color);color:var(--primary-text-color)">
          <div class="help-text">Die IP-Adresse des Kostal Wechselrichters (gleiche wie im Kostal-Webserver).</div>
        </div>
        <div style="margin-bottom:12px">
          <label style="display:block;font-weight:500;margin-bottom:4px">Modbus Port</label>
          <input type="number" value="${this._wizardData.kostal_modbus_port || 1502}"
                 data-field="kostal_modbus_port" min="1" max="65535"
                 style="width:100%;padding:8px;border:1px solid var(--divider-color);border-radius:4px;background:var(--card-background-color);color:var(--primary-text-color)">
          <div class="help-text">Standard bei Kostal: 1502 (nicht 502).</div>
        </div>
        ${this._kostalProbeResult ? (this._kostalProbeResult.battery_control_external ? `
        <div style="padding:8px 12px;border-radius:4px;background:rgba(76,175,80,.12);color:var(--primary-text-color);font-size:13px">
          &#10003; ${this._kostalProbeResult.product || "Kostal"} erkannt &mdash; externe Batteriesteuerung (Modbus) ist aktiv.
        </div>` : `
        <div style="padding:8px 12px;border-radius:4px;background:rgba(255,152,0,.15);color:var(--primary-text-color);font-size:13px">
          &#9888; ${this._kostalProbeResult.product || "Kostal"} erkannt, aber die Batteriesteuerung steht noch auf <strong>Intern</strong>.
          Die Umstellung auf &bdquo;Extern &uuml;ber Protokoll (Modbus TCP)&ldquo; erfordert den Installateur (siehe Anleitung, Schritt 3).
          Du kannst die Einrichtung trotzdem abschlie&szlig;en &mdash; die Batterie-Steuerung funktioniert erst nach der Umstellung.
        </div>`) : ""}
      </div>` : ""}
      ${smaSelected ? `
      <div class="card" style="padding:16px;margin-bottom:16px">
        <h3 style="margin:0 0 4px">Modbus TCP Verbindung</h3>
        <p style="font-size:13px;color:var(--secondary-text-color);margin:0 0 12px">
          IP-Adresse und Port für die direkte Modbus-Verbindung zum Wechselrichter (für Batterie-Steuerung).
          Der Modbus-TCP-Server muss im SMA-Webinterface aktiviert sein (Ger&auml;teparameter &rarr; Externe Kommunikation &rarr; Modbus &rarr; TCP-Server) &mdash; siehe Anleitung.
          Falls ein <strong>Sunny Home Manager 2.0</strong> verbaut ist, muss dessen &bdquo;prognosebasiertes Laden&ldquo; deaktiviert werden.
        </p>
        <div style="margin-bottom:12px">
          <label style="display:block;font-weight:500;margin-bottom:4px">Modbus IP-Adresse *</label>
          <input type="text" value="${this._wizardData.sma_modbus_host || ""}"
                 data-field="sma_modbus_host" placeholder="z.B. 192.168.1.100"
                 style="width:100%;padding:8px;border:1px solid var(--divider-color);border-radius:4px;background:var(--card-background-color);color:var(--primary-text-color)">
          <div class="help-text">Die IP-Adresse des SMA Wechselrichters (gleiche wie im SMA-Webinterface).</div>
        </div>
        <div style="margin-bottom:12px">
          <label style="display:block;font-weight:500;margin-bottom:4px">Modbus Port</label>
          <input type="number" value="${this._wizardData.sma_modbus_port || 502}"
                 data-field="sma_modbus_port" min="1" max="65535"
                 style="width:100%;padding:8px;border:1px solid var(--divider-color);border-radius:4px;background:var(--card-background-color);color:var(--primary-text-color)">
          <div class="help-text">Standard: 502 (Unit-ID 3 ist fest hinterlegt).</div>
        </div>
        ${this._smaProbeResult ? (this._smaProbeResult.opmod_register_ok ? `
        <div style="padding:8px 12px;border-radius:4px;background:rgba(76,175,80,.12);color:var(--primary-text-color);font-size:13px">
          &#10003; SMA-Ger&auml;t erkannt (Seriennr. ${this._smaProbeResult.serial}${this._smaProbeResult.soc != null ? `, Batterie-SOC ${this._smaProbeResult.soc}%` : ""}) &mdash; Steuerregister (CmpBMS) verf&uuml;gbar.
        </div>` : `
        <div style="padding:8px 12px;border-radius:4px;background:rgba(255,152,0,.15);color:var(--primary-text-color);font-size:13px">
          &#9888; SMA-Ger&auml;t erkannt (Seriennr. ${this._smaProbeResult.serial}), aber das Steuerregister 40236 (CmpBMS.OpMod) ist nicht lesbar.
          Manche Firmwares nutzen eine abweichende Adresse &mdash; bitte melde dich beim Support, bevor du die Steuerung aktivierst.
        </div>`) : ""}
      </div>` : ""}
      ` : ""}
      <button class="btn-secondary" data-action="recheck-prerequisites">Erneut prüfen</button>`;
  }

  /* ── Schritt: PV-Prognose ─────────────────────── */

  _renderStepPrognose() {
    const p = this._prerequisites;
    const solcastOk = p && p.solcast_solar;
    const forecastOk = p && p.forecast_solar;

    const solcastBadge = solcastOk
      ? '<span class="status-badge installed">Installiert</span>'
      : '<span class="status-badge missing">Nicht installiert</span>';
    const forecastBadge = forecastOk
      ? '<span class="status-badge installed">Installiert</span>'
      : '<span class="status-badge missing">Nicht installiert</span>';

    const selected = this._wizardData.forecast_source || "";
    const solcastSelected = selected === "solcast_solar";
    const forecastSelected = selected === "forecast_solar";

    // Auto-suggest sensor defaults when source is selected
    const allSolcastCandidates = [
      ...SOLCAST_DEFAULTS_CANDIDATES.forecast_remaining_entity,
      ...SOLCAST_DEFAULTS_CANDIDATES.forecast_tomorrow_entity,
    ];
    const isDefaultOrEmpty = !this._wizardData.forecast_remaining_entity
      || allSolcastCandidates.includes(this._wizardData.forecast_remaining_entity)
      || this._wizardData.forecast_remaining_entity === FORECAST_SOLAR_DEFAULTS.forecast_remaining_entity;
    if (selected && isDefaultOrEmpty) {
      this._applyForecastDefaults(selected);
    }

    // Sensor fields shown below cards when a source is selected
    const solcastRemainingHint = "Verbleibende PV-Produktion f\u00fcr den heutigen Tag in kWh. "
      + "Solcast-Sensornamen variieren je nach Version, z.B.: "
      + "sensor.solcast_pv_forecast_prognose_verbleibende_leistung_heute oder "
      + "sensor.solcast_pv_forecast_prognose_fuer_heute.";
    const solcastTomorrowHint = "Prognostizierte PV-Produktion f\u00fcr morgen in kWh. "
      + "Solcast-Sensornamen variieren je nach Version, z.B.: "
      + "sensor.solcast_pv_forecast_prognose_morgen oder "
      + "sensor.solcast_pv_forecast_prognose_fuer_morgen.";
    const sensorFields = selected ? `
      <div style="margin-top:16px">
        ${this._entityPickerHtml(
          "forecast_remaining_entity",
          this._wizardData.forecast_remaining_entity,
          "Sensor f\u00fcr PV-Prognose verbleibend heute *",
          solcastSelected
            ? solcastRemainingHint
            : "Verbleibende PV-Produktion f\u00fcr den heutigen Tag in kWh (Forecast.Solar: sensor.energy_production_today_remaining).",
          "sensor"
        )}
        ${this._entityPickerHtml(
          "forecast_tomorrow_entity",
          this._wizardData.forecast_tomorrow_entity,
          "Sensor f\u00fcr PV-Prognose morgen *",
          solcastSelected
            ? solcastTomorrowHint
            : "Prognostizierte PV-Produktion f\u00fcr morgen in kWh (Forecast.Solar: sensor.energy_production_tomorrow).",
          "sensor"
        )}
      </div>` : "";

    return `
      <p style="margin-bottom:12px;color:var(--secondary-text-color)">Wähle deine PV-Prognose-Quelle:</p>
      <div class="prereq-cards" style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px">
        <div class="card forecast-option ${solcastSelected ? "selected" : ""}" style="padding:16px;cursor:pointer;text-align:center;display:flex;flex-direction:column;align-items:center" data-action="select-forecast" data-value="solcast_solar">
          <div style="height:60px;display:flex;align-items:center;justify-content:center;margin-bottom:8px">
            <img src="https://brands.home-assistant.io/solcast_solar/logo.png" alt="Solcast" style="max-width:100px;max-height:60px;height:auto" onerror="this.style.display='none'">
          </div>
          <h3 style="margin:0 0 8px">Solcast Solar (empfohlen)</h3>
          ${solcastBadge}
          <p style="font-size:13px;color:var(--secondary-text-color);margin:8px 0">Genauere Prognosen, kostenloser API-Key erforderlich.</p>
          <button class="btn-secondary" data-action="show-dialog" data-dialog="solcast">Anleitung</button>
        </div>
        <div class="card forecast-option ${forecastSelected ? "selected" : ""}" style="padding:16px;cursor:pointer;text-align:center;display:flex;flex-direction:column;align-items:center" data-action="select-forecast" data-value="forecast_solar">
          <div style="height:60px;display:flex;align-items:center;justify-content:center;margin-bottom:8px">
            <img src="https://brands.home-assistant.io/forecast_solar/logo.png" alt="Forecast.Solar" style="max-width:100px;max-height:60px;height:auto" onerror="this.style.display='none'">
          </div>
          <h3 style="margin:0 0 8px">Forecast.Solar</h3>
          ${forecastBadge}
          <p style="font-size:13px;color:var(--secondary-text-color);margin:8px 0">Einfachere Einrichtung, keine Registrierung n\u00f6tig.</p>
          <button class="btn-secondary" data-action="show-dialog" data-dialog="forecast_solar">Anleitung</button>
        </div>
      </div>
      <button class="btn-secondary" data-action="recheck-prerequisites">Erneut pr\u00fcfen</button>
      ${sensorFields}
      ${selected && this._wizardData.expert_mode && solcastSelected ? `
      <div style="margin-top:16px">
        <h3 style="margin:0 0 12px;font-size:15px">Weitere Prognose-Sensoren (optional)</h3>
        <p style="font-size:13px;color:var(--secondary-text-color);margin-bottom:12px">
          Diese Sensoren werden automatisch erkannt. Nur \u00e4ndern wenn die Auto-Erkennung nicht funktioniert.
        </p>
        ${this._entityPickerHtml(
          "forecast_today_entity",
          this._wizardData.forecast_today_entity,
          "PV-Prognose heute (gesamt)",
          "Gesamte PV-Produktion f\u00fcr heute in kWh, z.B. sensor.solcast_pv_forecast_prognose_heute.",
          "sensor"
        )}
        ${this._entityPickerHtml(
          "forecast_day3_entity",
          this._wizardData.forecast_day3_entity,
          "PV-Prognose Tag 3",
          "z.B. sensor.solcast_pv_forecast_prognose_tag_3",
          "sensor"
        )}
        ${this._entityPickerHtml(
          "forecast_day4_entity",
          this._wizardData.forecast_day4_entity,
          "PV-Prognose Tag 4",
          "z.B. sensor.solcast_pv_forecast_prognose_tag_4",
          "sensor"
        )}
        ${this._entityPickerHtml(
          "forecast_day5_entity",
          this._wizardData.forecast_day5_entity,
          "PV-Prognose Tag 5",
          "z.B. sensor.solcast_pv_forecast_prognose_tag_5",
          "sensor"
        )}
        ${this._entityPickerHtml(
          "forecast_day6_entity",
          this._wizardData.forecast_day6_entity,
          "PV-Prognose Tag 6",
          "z.B. sensor.solcast_pv_forecast_prognose_tag_6",
          "sensor"
        )}
        ${this._entityPickerHtml(
          "forecast_day7_entity",
          this._wizardData.forecast_day7_entity,
          "PV-Prognose Tag 7",
          "z.B. sensor.solcast_pv_forecast_prognose_tag_7",
          "sensor"
        )}
      </div>` : ""}`;
  }

  // Huawei Master/Slave: zeigt direkt bei der Sensor-Auswahl, welche Sensoren
  // je Wechselrichter erkannt wurden — damit der User sieht "der hat beide
  // gecheckt" (oder eben nicht). Leerer String bei Single-Inverter.
  _renderHuaweiMultiInfo() {
    if (this._wizardData.inverter_type !== "huawei_sun2000") return "";
    const ids = this._wizardData.huawei_device_ids || [];
    if (ids.length < 2) return "";
    const d = this._wizardData;
    const line = (label, primary, second) => {
      const sec = second
        ? `<span style="color:var(--success-color,#43a047)">+ ${second}</span>`
        : `<span style="color:var(--warning-color,#ffa600)">(zweiter nicht erkannt)</span>`;
      return `<li style="margin:2px 0"><strong>${label}:</strong> ${primary || "—"} ${primary ? sec : ""}</li>`;
    };
    const allOk = d.pv_power_sensor_2 && d.battery_power_sensor_2;
    const col = allOk ? "var(--primary-color)" : "var(--warning-color,#ffa600)";
    return `
      <div style="display:flex;gap:12px;padding:12px 14px;border-left:3px solid ${col};background:var(--secondary-background-color);border-radius:6px;margin-bottom:14px">
        <ha-icon icon="mdi:${allOk ? "check-circle" : "alert"}" style="--mdc-icon-size:24px;color:${col};flex-shrink:0"></ha-icon>
        <div>
          <strong>${ids.length} Wechselrichter erkannt (Master/Slave)</strong>
          <div class="help-text" style="margin-top:4px">
            Beide Batterien werden angesteuert. Automatisch erkannte Sensoren:
            <ul style="margin:6px 0 2px 16px;padding:0">
              ${line("PV-Eingangsleistung", d.pv_power_sensor, d.pv_power_sensor_2)}
              ${line("Batterieleistung", d.battery_power_sensor, d.battery_power_sensor_2)}
            </ul>
            ${!allOk ? `<div style="margin-top:6px;color:var(--warning-color,#ffa600)">Ein zweiter Sensor wurde nicht gefunden — auf „Erneut prüfen" klicken oder im Experten-Modus manuell ergänzen.</div>` : ""}
          </div>
          <button class="btn-secondary" data-action="redetect-sensors" style="margin-top:8px">Erneut prüfen</button>
        </div>
      </div>`;
  }

  /* ── Schritt: Batterie (SOC + Kapazität) ──────── */

  _renderStepBatterie() {
    const detected = this._detectedSensors && this._detectedSensors.detected;

    let detectionInfo = "";

    // Huawei: ohne "Enable battery control" registriert huawei_solar keine
    // Steuer-Dienste — dann kann der Optimizer die Batterie nicht ansteuern.
    // Sofort sichtbar warnen (huawei_battery_control === false aus detect).
    if (
      this._wizardData.inverter_type === "huawei_sun2000" &&
      this._detectedSensors &&
      this._detectedSensors.huawei_battery_control === false
    ) {
      detectionInfo += `
        <div style="display:flex;gap:12px;padding:14px;border-left:3px solid var(--error-color,#db4437);background:var(--secondary-background-color);border-radius:6px;margin-bottom:16px">
          <ha-icon icon="mdi:alert-circle" style="--mdc-icon-size:28px;color:var(--error-color,#db4437);flex-shrink:0"></ha-icon>
          <div>
            <strong>Batteriesteuerung in „Huawei Solar" nicht aktiviert</strong>
            <div class="help-text" style="margin-top:6px">
              Die Steuer-Dienste der <code>huawei_solar</code>-Integration fehlen.
              Ohne sie kann der Optimizer die Batterie <strong>nicht ansteuern</strong>
              (weder Laden blockieren noch entladen). Richte die
              <code>huawei_solar</code>-Integration mit der Option
              <strong>„Enable battery control"</strong> ein (erfordert den
              Installer-Login) und prüfe dann erneut.
            </div>
            <button class="btn-secondary" data-action="redetect-sensors" style="margin-top:10px">Erneut prüfen</button>
          </div>
        </div>`;
    }

    // SolarEdge: Driver berechnet SOC + Kapazität kapazitätsgewichtet über
    // alle Inverter (i1, i2, ...). Wizard zeigt nur einen Info-Block; die
    // Combined-Sensor-IDs werden beim Save automatisch eingetragen.
    if (this._wizardData.inverter_type === "solaredge_storedge") {
      const detectedPrefixes = [];
      if (this._wizardData.pv_power_sensor) detectedPrefixes.push("i1");
      if (this._wizardData.pv_power_sensor_2) detectedPrefixes.push("i2");
      const prefixInfo = detectedPrefixes.length > 0
        ? `Erkannte Inverter: <strong>${detectedPrefixes.join(", ")}</strong>`
        : "Inverter-Erkennung läuft …";
      return `
        <div style="display:flex;gap:12px;padding:14px;border-left:3px solid var(--primary-color);background:var(--secondary-background-color);border-radius:6px">
          <ha-icon icon="mdi:battery-sync" style="--mdc-icon-size:28px;color:var(--primary-color);flex-shrink:0"></ha-icon>
          <div>
            <strong>SOC und Kapazität werden automatisch ermittelt.</strong>
            <div class="help-text" style="margin-top:6px">
              Bei SolarEdge liest die Integration die Werte pro Inverter
              direkt aus den b1-Sensoren der <code>solaredge_modbus_multi</code>-
              Integration und kombiniert sie:
              <ul style="margin:6px 0 4px 18px">
                <li>SOC = Σ(SOC<sub>i</sub> × Kapazität<sub>i</sub>) / Σ(Kapazität<sub>i</sub>)</li>
                <li>Kapazität = Σ(Kapazität<sub>i</sub>)</li>
              </ul>
              ${prefixInfo}.
              <br>Es werden zwei neue Sensoren angelegt:
              <code>sensor.eeg_energy_optimizer_combined_soc</code> und
              <code>sensor.eeg_energy_optimizer_combined_capacity</code>.
            </div>
          </div>
        </div>`;
    }

    // Huawei Master/Slave: SOC wird treiberseitig kombiniert (wie SolarEdge).
    // Da die Anlage keinen Kapazitäts-Sensor liefert, wird die Kapazität je
    // Batterie manuell eingetragen → gewichteter SOC + korrekte Gesamtkapazität.
    const huaweiDevs = (this._detectedSensors && this._detectedSensors.huawei_battery_devices) || [];
    if (this._wizardData.inverter_type === "huawei_sun2000" && huaweiDevs.length >= 2) {
      const caps = this._wizardData.huawei_battery_capacities || {};
      const fields = huaweiDevs.map(dev => `
        <div class="field-group" style="margin-bottom:10px">
          <label>Kapazität „${dev.name}" (kWh) *</label>
          <input type="number" data-field="huawei_cap_${dev.id}"
                 value="${caps[dev.id] || ""}" min="1" max="100" step="0.5" placeholder="z.B. 10">
        </div>`).join("");
      const sum = huaweiDevs.reduce((a, d) => a + (parseFloat(caps[d.id]) || 0), 0);
      return `
        ${detectionInfo}
        <div style="display:flex;gap:12px;padding:14px;border-left:3px solid var(--primary-color);background:var(--secondary-background-color);border-radius:6px;margin-bottom:16px">
          <ha-icon icon="mdi:battery-sync" style="--mdc-icon-size:28px;color:var(--primary-color);flex-shrink:0"></ha-icon>
          <div>
            <strong>${huaweiDevs.length} Batterien — SOC wird kombiniert.</strong>
            <div class="help-text" style="margin-top:6px">
              Der Ladestand wird kapazitätsgewichtet aus beiden Batterien berechnet
              (<code>sensor.eeg_energy_optimizer_combined_soc</code>). Diese Anlage
              liefert keinen Kapazitäts-Sensor — trage daher die nutzbare Kapazität
              je Batterie ein (steht meist im Gerätenamen):
            </div>
          </div>
        </div>
        ${fields}
        ${sum > 0
          ? `<div class="help-text">Gesamtkapazität: <strong>${sum} kWh</strong></div>`
          : `<div class="help-text" style="color:var(--warning-color,#ffa600)">Bitte die Kapazität beider Batterien eintragen.</div>`}`;
    }

    // Auto-select capacity mode: if sensor was detected, pick "sensor"; else "manual"
    // Re-evaluate after detection (don't cache stale pre-detection default)
    // SolaX has no capacity sensor — always default to manual
    if (this._wizardData.inverter_type === "solax_gen4" && !this._capacityModeUserSet) {
      this._capacityMode = "manual";
    } else if (!this._capacityMode || (detected && !this._capacityModeUserSet)) {
      this._capacityMode = this._wizardData.battery_capacity_sensor ? "sensor" : "manual";
    }
    const capSensor = this._capacityMode === "sensor";

    const capSensorHtml = capSensor ? this._entityPickerHtml(
      "battery_capacity_sensor",
      this._wizardData.battery_capacity_sensor,
      "Sensor für Batteriekapazität",
      this._wizardData.inverter_type === "huawei_sun2000"
        ? "Gesamtkapazität der Batterie in kWh oder Wh (Huawei: sensor.batterien_akkukapazitat)."
        : "Gesamtkapazität der Batterie in kWh oder Wh.",
      "sensor"
    ) : "";

    const capManualHtml = !capSensor ? `
      <div class="field-group">
        <label>Batteriekapazität (in kWh)</label>
        <input type="number" data-field="battery_capacity_kwh"
               value="${this._wizardData.battery_capacity_kwh || ""}"
               min="1" max="100" step="0.5"
               placeholder="z.B. 10">
        <div class="help-text">${this._wizardData.inverter_type === "huawei_sun2000"
          ? "z.B. 10 für LUNA2000-10, 15 für LUNA2000-15"
          : this._wizardData.inverter_type === "solax_gen4"
          ? "z.B. 5.8 für Triple Power T58, 11.6 für zwei Module"
          : this._wizardData.inverter_type === "solaredge_storedge"
          ? "z.B. 9.8 für LG RESU10H, 4.8 für BYD LVS 4.0"
          : this._wizardData.inverter_type === "sma_smart_energy"
          ? "z.B. 10.2 für BYD Battery-Box Premium HVS 10.2 (SMA liefert keinen Kapazitätssensor)"
          : "Nutzbare Gesamtkapazität deines Batteriespeichers in kWh"}</div>
      </div>` : "";

    return `
      ${detectionInfo}
      ${this._entityPickerHtml(
        "battery_soc_sensor",
        this._wizardData.battery_soc_sensor,
        "Sensor für Batterieladezustand (SOC) *",
        this._wizardData.inverter_type === "huawei_sun2000"
          ? "Der SOC-Sensor zeigt den aktuellen Ladestand deiner Batterie in Prozent (Huawei: sensor.batteries_batterieladung)."
          : "Der SOC-Sensor zeigt den aktuellen Ladestand deiner Batterie in Prozent.",
        "sensor"
      )}
      <div class="field-group">
        <label>Batteriekapazität *</label>
        <div class="cap-mode-cards">
          <div class="cap-mode-card ${!capSensor ? "selected" : ""}" data-action="set-cap-mode-card" data-value="manual">
            <ha-icon icon="mdi:pencil-box-outline"></ha-icon>
            <span>Manuell eingeben</span>
          </div>
          <div class="cap-mode-card ${capSensor ? "selected" : ""}" data-action="set-cap-mode-card" data-value="sensor">
            <ha-icon icon="mdi:auto-fix"></ha-icon>
            <span>Über Sensor</span>
          </div>
        </div>
        ${capSensor && this._wizardData.inverter_type === "huawei_sun2000" ? `<div class="help-text" style="margin-top:8px;margin-bottom:8px">
          Bei Huawei ist der Kapazitätssensor standardmäßig deaktiviert.
          <button class="btn-link" data-action="show-dialog" data-dialog="capacity_sensor">Anleitung zur Aktivierung</button>
        </div>` : ""}
      </div>
      ${capSensorHtml}
      ${capManualHtml}`;
  }

  /* ── Schritte: Anlage & Batterie, Tarife & Gemeinschaft ── */

  _featureCard(opts) {
    // Einheitliche Optik für alle Ein/Aus-Optionen: anklickbare Karte mit
    // Titel, Erklärung und Zustandsabzeichen — wie bisher schon bei der
    // Einspeisegrenze. Die Felder der Option erscheinen nur, wenn sie an ist.
    const { on, action, target, feature, icon, titel, beschreibung, params } = opts;
    // data-on tragt den Zustand mit, den die Karte ANZEIGT. Die Schalter
    // invertieren ihn, statt den Datensatz selbst zu befragen — dort steht
    // der Schluessel bei einer frischen Einrichtung noch gar nicht, und die
    // Vorgaben sind je Option verschieden (PeakShare an, Einspeisegrenze aus).
    const zustand = `data-on="${on ? "1" : "0"}"`;
    const attrs = feature
      ? `data-action="${action}" data-feature="${feature}" ${zustand}`
      : `data-action="${action}" data-target="${target}" ${zustand}`;
    return `
      <div class="feature-toggle">
        <div class="feature-card ${on ? "selected" : ""}" ${attrs} style="cursor:pointer">
          <div class="feature-card-header">
            <ha-icon icon="${icon}"></ha-icon>
            <div class="feature-card-text">
              <span class="feature-title">${titel}</span>
              <span class="feature-desc">${beschreibung}</span>
            </div>
            <div class="feature-badge ${on ? "on" : "off"}">${on ? "Aktiv" : "Aus"}</div>
          </div>
          <div style="text-align:center;font-size:12px;color:var(--secondary-text-color);margin-top:4px">Zum ${on ? "Deaktivieren" : "Aktivieren"} hier klicken</div>
        </div>
        ${on && params ? `<div class="feature-params">${params}</div>` : ""}
      </div>`;
  }

  _controlHint(inverterType) {
    // Nur der Sonderfall wird gemeldet. Die Bestaetigung „wird gesteuert" war
    // Rauschen: dass gesteuert wird, ist der Normalfall, und ob gerade
    // gestellt wird, sagt der Modus-Schalter im Dashboard.
    if (SCHEDULE_CONTROL_INVERTERS.includes(inverterType)) return "";
    return `<div class="help-text" style="margin-bottom:16px;padding:10px 12px;background:var(--info-color,#2196f3)18;border-left:3px solid var(--info-color,#2196f3);border-radius:4px">
           <ha-icon icon="mdi:information-outline" style="--mdc-icon-size:16px;vertical-align:middle"></ha-icon>
           Für diesen Wechselrichter wird der Optimierungsplan nur berechnet und angezeigt — die Steuerung ist derzeit nur für Fronius und Huawei verfügbar.
         </div>`;
  }

  _verguetungFields(d, prefix) {
    // Nur die Vergütungsseite: was eine eingespeiste Kilowattstunde bringt.
    // Was sie kostet, steht in _kostenFields. Wizard (prefix="") und
    // Einstellungen (prefix="settings_") zeigen dasselbe.
    // Bei festem Wert gibt es auch einen Nachtsatz — mancher Einspeisevertrag
    // vergütet nachts anders, auch ganz ohne Gemeinschaft. Die OeMAG kennt
    // keinen Nachtsatz, dort entfällt das Feld (das Backend ignoriert einen
    // gespeicherten Wert bei Quelle OeMAG ohnehin).
    const quelle = d.schedule_feedin_source || "manual";
    const quelleOemag = quelle === "oemag";
    const quelleSpot = quelle === "spot";

    // Ansicht mit OeMAG-/Spot-Wert geöffnet (Wizard-Rücksprung, gespeicherte
    // Auswahl): den Tarif holen, statt „Noch kein Tarif geholt" zu zeigen.
    if (quelleOemag) this._ensureOemagTarif();
    if (quelleSpot) this._ensureSpotStatus();

    // Der OeMAG-Wert kommt aus einer HTML-Tabelle. Deshalb steht hier immer
    // dabei, aus welchem Monat er ist und wie alt der Abruf — bricht das
    // Lesen, bleibt der letzte Wert stehen, und nur das Alter verrät es.
    const o = this._oemagStatus;
    const monate = ["", "Jänner", "Februar", "März", "April", "Mai", "Juni",
                    "Juli", "August", "September", "Oktober", "November", "Dezember"];
    let oemagZeile;
    if (this._oemagBusy) {
      oemagZeile = "Tarif wird geholt…";
    } else if (o && o.preis) {
      const alter = o.alter_minuten == null
        ? ""
        : o.alter_minuten < 60
          ? `, geholt vor ${o.alter_minuten} min`
          : `, geholt vor ${Math.round(o.alter_minuten / 60)} h`;
      oemagZeile = `<strong>${fmtDe(o.preis * 100, 3)} ct/kWh</strong>`
        + (o.monat ? ` (Stand ${monate[o.monat] || o.monat})` : "") + alter
        + (o.fehler ? ` — letzter Abruf fehlgeschlagen: ${this._escapeHtml(o.fehler)}` : "");
    } else if (o && o.fehler) {
      oemagZeile = `Kein Tarif gelesen (${this._escapeHtml(o.fehler)}) — es gilt der fest eingetragene Wert.`;
    } else {
      oemagZeile = "Noch kein Tarif geholt.";
    }
    const oemagBlock = `
      <div class="field-group">
        <label>OeMAG-Einspeisetarif</label>
        <div class="help-text" style="font-size:13px;color:var(--primary-text-color)">${oemagZeile}</div>
        <div class="help-text">Wird zweimal täglich von oem-ag.at gelesen und wechselt monatlich; der laufende Monat erscheint dort erst im Laufe des Monats, bis dahin gilt der letzte veröffentlichte. Antwortet die Seite nicht, bleibt der zuletzt gelesene Wert stehen — und wenn es nie einen gab, der fest eingetragene.</div>
        <button class="btn-link" data-action="refresh-oemag" style="font-size:12px;padding:0" ${this._oemagBusy ? "disabled" : ""}>Jetzt holen</button>
      </div>`;
    // Spot-Status (aWATTar): aktueller Börsenpreis, Datenreichweite, Alter.
    const sp = this._spotStatus;
    let spotZeile;
    if (this._spotBusy) {
      spotZeile = "Preise werden geholt…";
    } else if (sp && sp.preis != null) {
      const alter = sp.alter_minuten == null
        ? ""
        : sp.alter_minuten < 60
          ? `, geholt vor ${sp.alter_minuten} min`
          : `, geholt vor ${Math.round(sp.alter_minuten / 60)} h`;
      const bis = sp.daten_bis
        ? `, Preise bis ${new Date(sp.daten_bis).toLocaleString("de-AT", { weekday: "short", hour: "2-digit", minute: "2-digit" })}`
        : "";
      spotZeile = `Jetzt <strong>${fmtDe(sp.preis * 100, 2)} ct/kWh</strong> an der Börse${bis}${alter}`
        + (sp.fehler ? ` — letzter Abruf fehlgeschlagen: ${this._escapeHtml(sp.fehler)}` : "");
    } else if (sp && sp.fehler) {
      spotZeile = `Keine Preise gelesen (${this._escapeHtml(sp.fehler)}) — es gilt der fest eingetragene Wert.`;
    } else {
      spotZeile = "Noch keine Preise geholt.";
    }
    const spotBlock = `
      <div class="field-group">
        <label>Marktgebiet</label>
        <select data-field="${prefix}spot_market_area">
          <option value="at" ${(d.spot_market_area || "at") === "de" ? "" : "selected"}>Österreich (EPEX Spot AT)</option>
          <option value="de" ${(d.spot_market_area || "at") === "de" ? "selected" : ""}>Deutschland (EPEX Spot DE)</option>
        </select>
      </div>
      <div class="field-group">
        <label>Spotpreis der Strombörse</label>
        <div class="help-text" style="font-size:13px;color:var(--primary-text-color)">${spotZeile}</div>
        <div class="help-text">Stündliche Day-Ahead-Preise über die freie aWATTar-API, aktualisiert stündlich; die Preise für morgen erscheinen am frühen Nachmittag. Bis dahin schreibt die Optimierung den Vortagsverlauf fort. Negative Börsenpreise gelten wirklich — der Fahrplan speist dann nicht ein, sondern speichert oder regelt ab. Antwortet die API nicht, gelten die zuletzt geholten Preise, ohne solche der fest eingetragene Wert.</div>
        <button class="btn-link" data-action="refresh-spot" style="font-size:12px;padding:0" ${this._spotBusy ? "disabled" : ""}>Jetzt holen</button>
      </div>
      <div class="field-group">
        <label>Abschlag des Vermarkters (ct/kWh)</label>
        <input type="number" data-field="${prefix}spot_feedin_fee" data-unit="ct"
               value="${Number(d.spot_feedin_fee ?? 0) !== 0 ? ctAus(d.spot_feedin_fee) : ""}"
               min="-20" max="20" step="0.01" placeholder="0 — voller Spotpreis">
        <div class="help-text">Was dein Abnahmevertrag je Kilowattstunde vom Börsenpreis abzieht (steht im Vertrag, oft 1–2 ct). Leer oder 0 heißt: du bekommst den vollen Spotpreis.</div>
      </div>`;
    return `
      <div class="field-group">
        <label>Standardvergütung — Quelle *</label>
        <select data-field="${prefix}schedule_feedin_source">
          <option value="manual" ${quelle === "manual" ? "selected" : ""}>Fester Wert</option>
          <option value="oemag" ${quelleOemag ? "selected" : ""}>OeMAG-Einspeisetarif (monatlich)</option>
          <option value="spot" ${quelleSpot ? "selected" : ""}>Spotpreis der Strombörse (stündlich)</option>
        </select>
        <div class="help-text">Was du bekommst, wenn die Energie nicht in einer Gemeinschaft landet. Der Fahrplan hält diesen Wert gegen den Bezugspreis und gegen die Vergütung der Gemeinschaften.</div>
      </div>
      ${quelleOemag ? oemagBlock : quelleSpot ? spotBlock : `
      <div style="display:flex;gap:12px;flex-wrap:wrap">
        <div class="field-group" style="flex:1;min-width:140px">
          <label>Einspeisevergütung Tag (ct/kWh) *</label>
          <input type="number" data-field="${prefix}schedule_feedin_price" data-unit="ct"
                 value="${ctAus(d.schedule_feedin_price ?? 0.082)}" min="0" max="200" step="0.1">
        </div>
        <div class="field-group" style="flex:1;min-width:140px">
          <label>Einspeisevergütung Nacht (ct/kWh)</label>
          <input type="number" data-field="${prefix}schedule_feedin_price_night" data-unit="ct"
                 value="${Number(d.schedule_feedin_price_night ?? 0) > 0 ? ctAus(d.schedule_feedin_price_night) : ""}"
                 min="0" max="200" step="0.1" placeholder="wie am Tag">
        </div>
      </div>
      <div class="help-text" style="margin-bottom:16px">Was du für eingespeiste Energie bekommst. Die Optimierung hält sie gegen den Bezugspreis und entscheidet danach, ob eine Kilowattstunde besser ins Netz geht oder in die Batterie. Vergütet dein Vertrag nachts anders, trage den Nachtsatz ein — mit eingetragenem Nachtsatz erscheint darunter das Nachtfenster.</div>`}
      ${this._nachtfensterFelder(d, prefix)}`;
  }

  // Das Nachtfenster gilt für ALLE Nachtsätze gleichermaßen — den der
  // Standardvergütung wie die der Gemeinschaften (verglichen wird immer, was
  // zum selben Zeitpunkt gilt). Es steht deshalb genau EINMAL hier bei der
  // Vergütung und erscheint, sobald irgendwo ein Nachtsatz eingetragen ist.
  // Zwei Eingabefelder für denselben Schlüssel darf es nicht geben: beim
  // Speichern liest das Panel das komplette DOM nach, und das unveränderte
  // Zweitfeld würde die Eingabe im ersten überschreiben.
  _nachtfensterFelder(d, prefix) {
    // Der Nachtsatz der Standardvergütung zählt nur bei Quelle „Fester
    // Wert": bei OeMAG und Spot wirkt er nicht (das Backend setzt ihn dort
    // auf „kein Nachttarif"), ein gespeicherter Altwert darf das Fenster
    // dann auch nicht einblenden. Die Nachtsätze der Gemeinschaften gelten
    // unabhängig von der Quelle.
    // Seit die Gemeinschaften ihr EIGENES Nachtfenster haben (in der
    // Gemeinschafts-Sektion, peakshare_night_*), gehört dieses Fenster
    // allein dem Nachtsatz der Standardvergütung — und der wirkt nur bei
    // Quelle „Fester Wert". Bei Spot/OeMAG erscheint hier nichts mehr.
    const quelleManual = (d.schedule_feedin_source || "manual") === "manual";
    if (!quelleManual) return "";
    // Das Fenster steht IMMER da, sobald die Quelle „Fester Wert" ist — auch
    // ohne eingetragenen Nachtsatz. Früher hing es zusätzlich am Nachtsatz
    // (> 0) und musste beim Tippen per Render nachgezogen werden; blieb der
    // Nachzug aus, fehlte das Fenster scheinbar grundlos und tauchte erst
    // beim nächsten Render auf (z. B. beim Umschalten des Expertenmodus).
    // Ein dauerhaft sichtbares Feld kann nicht klemmen. Pflicht ist es nur
    // mit Nachtsatz — nur dann trägt es den Stern.
    const nachtsatz = Number(d.schedule_feedin_price_night ?? 0) > 0;
    const stern = nachtsatz ? " *" : "";
    return `
      <div style="display:flex;gap:12px">
        <div class="field-group" style="flex:1">
          <label>Nachtfenster von${stern}</label>
          <input type="time" data-field="${prefix}schedule_night_start" value="${d.schedule_night_start || "20:00"}">
        </div>
        <div class="field-group" style="flex:1">
          <label>Nachtfenster bis${stern}</label>
          <input type="time" data-field="${prefix}schedule_night_end" value="${d.schedule_night_end || "06:00"}">
        </div>
      </div>
      <div class="help-text" style="margin-bottom:16px">Wann der Nachtsatz der Standardvergütung gilt. Darf über Mitternacht gehen. ${nachtsatz ? "" : "Ohne eingetragenen Nachtsatz wirkt das Fenster nicht — es gilt dann rund um die Uhr die Tagvergütung. "}Das Nachtfenster der Gemeinschaften stellst du bei deren Nachtsätzen ein.</div>`;
  }

  _kostenFields(d, prefix) {
    // Die Kostenseite: was Energie kostet, wenn sie nicht vom Dach kommt,
    // und was das Zwischenspeichern die Batterie kostet.
    //
    // Die Alterungskosten sind derzeit AUSGEBLENDET (Nutzerentscheid
    // 28.08.2026) — sie wirken weiter mit ihrer Vorgabe von 1 ct/kWh
    // (DEFAULT_BATTERY_COST in schedule.py), man kann sie nur nicht mehr
    // verstellen. Das Feld ist bewusst nur abgeschaltet und nicht entfernt:
    // auf `true` gesetzt kommt es unverändert zurück.
    const alterungskostenSichtbar = false;
    return `
      <div class="field-group">
        <label>Bezugspreis (ct/kWh) *</label>
        <input type="number" data-field="${prefix}schedule_consumption_price" data-unit="ct"
               value="${ctAus(d.schedule_consumption_price ?? 0.247)}" min="0" max="200" step="0.1">
        <div class="help-text">Dein Arbeitspreis inklusive Netz und Abgaben. Solange er klar über der Einspeisevergütung liegt, ist die genaue Höhe unwichtig — erst wenn sich beide annähern, ändert sich das Verhalten grundlegend.</div>
      </div>
      ${alterungskostenSichtbar && d.expert_mode ? `
      <div class="field-group">
        <label>Alterungskosten der Batterie (ct/kWh)</label>
        <input type="number" data-field="${prefix}schedule_battery_cost" data-unit="ct"
               value="${ctAus(Number(d.schedule_battery_cost) > 0 ? d.schedule_battery_cost : 0.01)}" min="0.1" max="100" step="0.1">
        <div class="help-text">Was eine durchgesetzte Kilowattstunde die Batterie an Lebensdauer kostet. Höhere Werte machen die Optimierung zurückhaltender: sie speichert nur, wenn sich der Umweg lohnt. Ein leeres Feld gilt als 1 ct — ohne Alterungskosten lädt und entlädt die Optimierung ohne jede Zurückhaltung.</div>
      </div>` : ""}`;
  }

  _maxSocFeld(d, prefix) {
    // Maximum-Ladestand — Gegenstück zum Mindest-Ladestand, immer sichtbar
    // und ohne eigenen Schalter: der Zustand steckt allein im Wert, 100 ist
    // die Vorgabe und heißt „bis voll laden" (Migration v27 hat den
    // früheren Ein/Aus-Schlüssel entfernt).
    const roh = Number(d.schedule_max_soc_pct ?? 100);
    // Genau die Regel aus _max_soc_pct im Backend: eine 0 ist ein geleertes
    // Feld und keine Angabe, gekappt wird auf [70, 100]. Nachgerechnet statt
    // nur angezeigt — sagte das Feld etwas anderes als die Optimierung
    // rechnet, wäre es schlimmer als kein Feld.
    const deckel = roh > 0 ? Math.max(70, Math.min(100, roh)) : 100;
    const boden = Number(d.schedule_min_soc_pct ?? 10);
    const kap = Number(d.battery_capacity_kwh) || 0;
    // Was nutzbar bleibt — die Zahl, die der Nutzer wirklich wissen will.
    const nutzbarPct = Math.max(0, deckel - boden);
    const nutzbar = kap > 0
      ? `<strong>${fmtDe(kap * nutzbarPct / 100, 1)} kWh</strong> von ${fmtDe(kap, 1)} kWh`
      : `<strong>${fmtDe(nutzbarPct, 0)} %</strong> der Kapazität`;
    // Das Feld zeigt den EFFEKTIVEN Wert, nicht den gespeicherten Rohwert:
    // ein leer gelassenes oder zu tief eingetragenes Feld füllt sich beim
    // nächsten Rendern mit dem Wert, der wirklich gilt.
    const wirkung = deckel >= 100
      ? `Die Optimierung darf bis voll laden. Nutzbar sind ${nutzbar}.`
      : `Die Optimierung plant nie über <strong>${fmtDe(deckel, 0)} %</strong>. Zusammen mit dem`
        + ` Mindest-Ladestand von ${fmtDe(boden, 0)} % bleiben ${nutzbar} nutzbar.`;

    return `
      <div class="field-group">
        <label>Maximum-Ladestand (%)</label>
        <input type="number" data-field="${prefix}schedule_max_soc_pct"
               value="${deckel}" min="70" max="100" step="1">
        <div class="help-text">100 heißt: bis voll laden — die Vorgabe. Ein Wert darunter lässt
          oben einen Rest frei; manche Zellchemien altern nahe der Vollladung schneller, wie viel
          eine solche Grenze im Einzelfall tatsächlich bringt, ist offen. An der Testanlage
          kostete ein Maximum von 90 % kaum Erlös. Mindestens 70 % — darunter bliebe zu wenig
          nutzbarer Bereich.</div>
        <div class="help-text">${wirkung}</div>
        <div class="help-text" style="margin-top:8px">
          Das begrenzt den <em>Plan</em>, nicht das Gerät: steht die Optimierung auf Test oder
          Aus, startet Home Assistant neu, oder drückt bei aktiver Einspeisegrenze mehr
          PV-Leistung nach als ins Netz darf, lädt der Wechselrichter weiterhin bis voll. Als
          echter Zellschutz gehört die Grenze zusätzlich ins Gerät.
        </div>
      </div>`;
  }

  _batterieOptFields(d, prefix) {
    const maxSocFeld = this._maxSocFeld(d, prefix);
    // Grenzen der Batterie für die Optimierung. Die frühere Notstromreserve
    // mit eigener kWh-Angabe und Überbrückungsdauer ist entfallen: sie folgt
    // jetzt aus dem Mindest-Ladestand (Kapazität × Prozent). Eine
    // vorausschauende Reserve gibt es nicht mehr — sie ist im Modell
    // abgeschaltet, nicht auf 18 Stunden gestellt.
    // Die Leistungsgrenze ist ein Gerätedatum (Datenblatt) — im Wizard immer
    // sichtbar, in den Einstellungen (prefix) nur im Expertenmodus.
    const leistungsgrenze = (!prefix || d.expert_mode) ? `
      <div class="field-group">
        <label>Batterie-Leistungsgrenze (kW) *</label>
        <input type="number" data-field="${prefix}discharge_power_kw"
               value="${d.discharge_power_kw ?? 5.0}" min="0.5" max="20" step="0.5">
        <div class="help-text">Wie viel Leistung die Batterie höchstens aufnehmen oder abgeben kann. Die Optimierung plant nie darüber — ein zu kleiner Wert verschenkt Möglichkeiten, ein zu großer erzeugt Pläne, die der Wechselrichter nicht erfüllt.</div>
      </div>` : "";
    return `
      ${leistungsgrenze}
      <div class="field-group">
        <label>Mindest-Ladestand (%) *</label>
        <input type="number" data-field="${prefix}schedule_min_soc_pct"
               value="${d.schedule_min_soc_pct ?? 10}" min="0" max="30" step="1">
        <div class="help-text">Wie viel im Speicher bleiben soll: ${fmtDe(Number(d.schedule_min_soc_pct ?? 10), 0)} % von ${d.battery_capacity_kwh ? fmtDe(Number(d.battery_capacity_kwh), 1) + " kWh" : "der Kapazität"} sind die Sicherheitsreserve, die die Optimierung vorhält. Schont die Zellen und lässt einen Puffer für Lastspitzen — 0 erlaubt die Entladung bis leer. Höchstens 30 %, darüber bliebe zu wenig für eine Nacht.</div>
      </div>
      ${maxSocFeld}
`;
  }

  _gemeinschaftFields(d, prefix) {
    const on = d.enable_peakshare !== false;
    const communities = this._peakshareCommunitiesCache || [];
    // Basistarif wie im Backend bestimmen (schedule.py): bei Quelle OeMAG
    // zählt der geholte Tarif, nicht die Handeingabe — sonst zeigt die
    // Vorschau einen Aufschlag, den der Fahrplan so nie rechnet. Ohne
    // geholten Wert gilt auch dort die Handeingabe.
    const gemQuelle = d.schedule_feedin_source || "manual";
    if (gemQuelle === "oemag") this._ensureOemagTarif();
    if (gemQuelle === "spot") this._ensureSpotStatus();
    // Basistarif für die Vorschau wie im Backend: OeMAG-Wert, bei Spot der
    // aktuelle Börsenpreis abzüglich Vermarkter-Abschlag (zeitvariabel — die
    // Vorschau nimmt den Augenblickswert als Näherung), sonst Handeingabe.
    let basis = Number(d.schedule_feedin_price ?? 0.082);
    if (gemQuelle === "oemag" && Number(this._oemagStatus?.preis ?? 0) > 0) {
      basis = Number(this._oemagStatus.preis);
    } else if (gemQuelle === "spot" && this._spotStatus?.preis != null) {
      basis = Number(this._spotStatus.preis) - Number(d.spot_feedin_fee ?? 0);
    }
    // Nachts steht die Gemeinschaft gegen den Nachtsatz der Standardvergütung,
    // wenn einer gesetzt ist (nur bei festem Wert — OeMAG und Spot kennen
    // keinen); sonst gegen denselben Wert wie am Tag. Genau die Regel des
    // Backends (feedin_nacht in async_collect_inputs) — rechnete die Vorschau
    // anders, zeigte sie einen Aufschlag, den der Fahrplan so nie rechnet.
    const basisNacht = (gemQuelle === "manual" && Number(d.schedule_feedin_price_night ?? 0) > 0)
      ? Number(d.schedule_feedin_price_night)
      : basis;

    // Bis zu zwei Gemeinschaften. Der Aufteilungsschlüssel darf sich auf beide
    // verteilen (z. B. 40 % EEG, 60 % BEG); was keiner zugeordnet ist, geht
    // zum Basistarif an den Energieversorger. Die Summe ist deshalb nach oben
    // begrenzt, aber nicht vorgeschrieben.
    const anteil = (nr) => Number(d[`peakshare_share_pct${nr === 2 ? "_2" : ""}`] ?? 0);
    const summe = anteil(1) + anteil(2);

    // Die Tarifzahlen einer Gemeinschaft an einer Stelle: der Block unten
    // zeigt sie, und die Karte für den Überschussabschlag rechnet mit
    // denselben Werten weiter. Getrennt gerechnet wären es zwei Wahrheiten.
    const tarifWerte = (nr) => {
      const s = nr === 2 ? "_2" : "";
      const pct = anteil(nr);
      const preis = Number(d[`peakshare_price${s}`] ?? 0.102);
      // Leeres Nachtfeld (0) heißt: derselbe Satz wie am Tag.
      const preisNacht = Number(d[`peakshare_price_night${s}`] ?? 0) || preis;
      const gewicht = Number(d[`peakshare_weight${s}`] ?? (nr === 1 ? 0.01 : 0));
      // Höchster Aufschlag dieser Gemeinschaft: zur Bedarfsspitze erreicht er
      // genau ihren Anteil an der Differenz zum Basistarif. Tag und Nacht
      // getrennt, weil beide Sätze verschieden sein können.
      return {
        s,
        name: d[`peakshare_community${s}`] || "",
        pct,
        preis,
        preisNacht,
        gewicht,
        aufTag: Math.max(0, (pct / 100) * (preis + gewicht - basis)),
        aufNacht: Math.max(0, (pct / 100) * (preisNacht + gewicht - basisNacht)),
      };
    };

    const block = (nr) => {
      const { s, name, pct, preis, gewicht, aufTag, aufNacht } = tarifWerte(nr);
      const aufschlag = Math.max(aufTag, aufNacht);

      // Einen konfigurierten Namen immer anbieten, auch wenn die Liste ihn
      // (noch) nicht kennt — sonst leert das Speichern das Feld.
      const namen = !name || communities.includes(name) ? communities : [name, ...communities];
      const auswahl = communities.length === 0 && !name
        ? `<div class="help-text">Gemeinschaften werden geladen…</div>`
        : `<select data-field="${prefix}peakshare_community${s}">
             <option value="" ${name ? "" : "selected"}>${nr === 2 ? "— keine —" : "— bitte wählen —"}</option>
             ${namen.map(c => `<option value="${c}" ${c === name ? "selected" : ""}>${c}</option>`).join("")}
           </select>`;

      let wirkung;
      if (!name) {
        wirkung = nr === 2
          ? "Keine zweite Gemeinschaft — der Rest des Aufteilungsschlüssels geht an den Energieversorger."
          : "Noch keine Gemeinschaft gewählt.";
      } else if (pct <= 0) {
        wirkung = "Anteil 0 % — diese Gemeinschaft wirkt nicht auf den Fahrplan.";
      } else if (aufschlag <= 0) {
        wirkung = "Vergütung liegt nicht über dem Basistarif — kein Anreiz, Energie hierher zu verschieben.";
      } else if (Math.abs(aufTag - aufNacht) < 1e-9) {
        wirkung = `Höchster Aufschlag zur Bedarfsspitze: <strong>${fmtDe(aufTag * 100, 2)} ct/kWh</strong>`
          + ` (${fmtDe(pct, 0)} % von ${fmtDe((preis + gewicht - basis) * 100, 2)} ct).`;
      } else {
        wirkung = `Höchster Aufschlag zur Bedarfsspitze: <strong>${fmtDe(aufTag * 100, 2)} ct/kWh</strong> am Tag,`
          + ` <strong>${fmtDe(aufNacht * 100, 2)} ct/kWh</strong> nachts (je ${fmtDe(pct, 0)} % der Differenz zur Standardvergütung`
          + ` von ${fmtDe(basis * 100, 2)} ct).`;
      }

      return `
        <div style="border:1px solid var(--divider-color);border-radius:8px;padding:12px 12px 4px;margin-bottom:12px">
          <div style="font-weight:500;margin-bottom:10px">Gemeinschaft ${nr}${nr === 2 ? " (optional)" : ""}</div>
          <div class="field-group">
            <label>Gemeinschaft</label>
            ${auswahl}
          </div>
          <div style="display:flex;gap:12px;flex-wrap:wrap">
            <div class="field-group" style="flex:1;min-width:100px">
              <label>Anteil (%)</label>
              <input type="number" data-field="${prefix}peakshare_share_pct${s}"
                     value="${pct}" min="0" max="100" step="1">
            </div>
            <div class="field-group" style="flex:1;min-width:120px">
              <label>Vergütung Tag (ct/kWh)</label>
              <input type="number" data-field="${prefix}peakshare_price${s}" data-unit="ct"
                     value="${ctAus(preis)}" min="0" max="200" step="0.1">
            </div>
            <div class="field-group" style="flex:1;min-width:120px">
              <label>Vergütung Nacht (ct/kWh)</label>
              <input type="number" data-field="${prefix}peakshare_price_night${s}" data-unit="ct"
                     value="${d[`peakshare_price_night${s}`] ? ctAus(d[`peakshare_price_night${s}`]) : ""}"
                     min="0" max="200" step="0.1" placeholder="wie am Tag">
            </div>
            <div class="field-group" style="flex:1;min-width:120px">
              <label>Gewichtung (ct/kWh)</label>
              <input type="number" data-field="${prefix}peakshare_weight${s}" data-unit="ct"
                     value="${ctAus(gewicht)}" min="0" max="100" step="0.1">
            </div>
          </div>
          <div class="help-text" style="margin-bottom:8px">${wirkung}</div>
        </div>`;
    };

    const summeFarbe = summe > 100 ? "#e53935" : "var(--secondary-text-color)";
    const summeText = summe > 100
      ? `Summe der Anteile: ${fmtDe(summe, 0)} % — mehr als 100 % ist nicht möglich, so lässt sich das nicht speichern.`
      : summe === 100
        ? `Summe der Anteile: 100 % — die ganze Einspeisung ist zugeordnet.`
        : `Summe der Anteile: ${fmtDe(summe, 0)} % — die restlichen ${fmtDe(100 - summe, 0)} % gehen zum Basistarif an den Energieversorger.`;

    // Der Block der zweiten Gemeinschaft erscheint erst auf Knopfdruck oder
    // wenn schon eine konfiguriert ist — ein leerer Block wäre für die
    // Mehrheit mit nur einer Gemeinschaft totes Formular.
    const zweiteOffen = !!d.peakshare_community_2 || this._gem2Open;
    const zweiterBlock = zweiteOffen
      ? block(2)
      : `<button class="btn-secondary" data-action="open-gemeinschaft-2" style="margin-bottom:12px">Zweite Gemeinschaft hinzufügen</button>`;

    // Eigenes Nachtfenster der Gemeinschaften: EEG/BEG-Verträge können ein
    // anderes Fenster haben als der Einspeisevertrag der Standardvergütung
    // (Nutzerwunsch 27.08.). Vorbelegt mit dem bisher wirksamen Fenster
    // (Fallback des Backends ist das Standard-Fenster), damit sich
    // Bestandsanlagen beim bloßen Speichern nicht ändern.
    // Wie beim Nachtfenster der Standardvergütung steht es IMMER da, sobald
    // die Gemeinschafts-Sektion sichtbar ist — nicht erst mit eingetragenem
    // Nachtsatz. Ein konditionales Feld muss beim Tippen nachgezogen werden,
    // und genau dieser Nachzug klemmte.
    const nachtGesetzt = Number(d.peakshare_price_night ?? 0) > 0
      || Number(d.peakshare_price_night_2 ?? 0) > 0;
    const gemStern = nachtGesetzt ? " *" : "";
    const nachtfenster = `
        <div style="display:flex;gap:12px;margin-top:8px">
          <div class="field-group" style="flex:1">
            <label>Nachtfenster der Gemeinschaften von${gemStern}</label>
            <input type="time" data-field="${prefix}peakshare_night_start"
                   value="${d.peakshare_night_start || d.schedule_night_start || "20:00"}">
          </div>
          <div class="field-group" style="flex:1">
            <label>Nachtfenster bis${gemStern}</label>
            <input type="time" data-field="${prefix}peakshare_night_end"
                   value="${d.peakshare_night_end || d.schedule_night_end || "06:00"}">
          </div>
        </div>
        <div class="help-text" style="margin-bottom:8px">Wann die Nachtvergütung der Gemeinschaften gilt. Darf über Mitternacht gehen — und darf sich vom Nachtfenster der Standardvergütung unterscheiden.${nachtGesetzt ? "" : " Ohne eingetragenen Nachtsatz wirkt es nicht."}</div>`;

    return this._featureCard({
      on,
      action: prefix ? "toggle-settings-feature" : "toggle-feature",
      feature: "enable_peakshare",
      icon: "mdi:account-group-outline",
      titel: "Gemeinschaftsdaten abrufen (PeakShare)",
      beschreibung: "Holt die Bedarfsprognose der Gemeinschaften. Der Fahrplan rechnet daraus einen Preisaufschlag: Stunden mit hohem Bedarf werden wertvoller, dorthin verschiebt er die Einspeisung. Ohne Anteil (0 %) ist es reine Anzeige. Das funktioniert nur für Gemeinschaften, die über PeakShare der EW Ansfelden abgewickelt werden — andere Gemeinschaften liefern hier keine Daten.",
      params: `
        ${block(1)}
        ${zweiterBlock}
        <div class="help-text" style="color:${summeFarbe}">${summeText}</div>
        ${nachtfenster}
        <div class="help-text" style="margin-top:8px">
          <strong>Anteil</strong>: dein Aufteilungsschlüssel, also welcher Teil der Einspeisung
          dieser Gemeinschaft zugeordnet ist.
          <strong>Vergütung</strong>: was sie je Kilowattstunde zahlt — für eine
          Nachtvergütung gilt das Nachtfenster der Gemeinschaften oben.
          <strong>Gewichtung</strong>: ein Zuschlag ohne Geldfluss — der EEG-Bezieher spart
          Netzgebühren, das kommt nicht bei dir an, soll im Fahrplan aber zählen. Für eine
          BEG gibt es diesen Vorteil nicht, dort 0 eintragen.
        </div>`,
    });
  }

  _anlageFields(d, prefix) {
    // Anlagendaten des Wechselrichters: was er netzseitig kann und ob eine
    // Einspeisegrenze gilt. Beides begrenzt im Modell die Summe aus
    // Einspeisung und Hauslast, gehört also zusammen — und zum Gerät, nicht
    // zur Optimierung. Im Wizard sind AC-Grenze und PV-Spitze Pflicht und
    // immer sichtbar; in den Einstellungen (prefix) sind es Gerätedaten, die
    // sich nie ändern — dort nur im Expertenmodus.
    const geraetedaten = (!prefix || d.expert_mode) ? `
      <div class="field-group">
        <label>AC-Grenzleistung des Wechselrichters (kW) *</label>
        <input type="number" data-field="${prefix}inverter_ac_limit_kw"
               value="${d.inverter_ac_limit_kw || ""}" min="0.1" max="200" step="0.1" placeholder="z.B. 8">
        <div class="help-text">Nennleistung auf der Netzseite, aus dem Datenblatt. Begrenzt in der Optimierung die Summe aus Einspeisung und Hausverbrauch — ein zu großer Wert erzeugt Pläne, die das Gerät nicht liefern kann.</div>
      </div>
      <div class="field-group">
        <label>PV-Spitzenleistung (kWp) *</label>
        <input type="number" data-field="${prefix}pv_peak_kwp"
               value="${d.pv_peak_kwp || ""}" min="0.1" max="200" step="0.1" placeholder="z.B. 9.9">
        <div class="help-text">Summe der Modulleistung. Dient der Plausibilitätsprüfung der Prognosewerte.</div>
      </div>` : "";
    return `
      ${geraetedaten}
      ${this._featureCard({
        on: !!d.grid_export_limit_enabled,
        action: prefix ? "toggle-settings-feature" : "toggle-feature",
        feature: "grid_export_limit_enabled",
        icon: "mdi:transmission-tower-export",
        titel: "Einspeisegrenze beachten",
        beschreibung: "Einschalten, wenn der Netzbetreiber die Einspeiseleistung begrenzt. Die Optimierung plant dann so, dass möglichst nichts abgeregelt wird, und die Steuerung hebt das Ladelimit an, wenn die Einspeisung trotzdem an der Grenze klebt.",
        params: `
          <div class="field-group">
            <label>Höhe der Grenze (kW) *</label>
            <input type="number" data-field="${prefix}grid_export_limit_kw"
                   value="${d.grid_export_limit_kw ?? 4}" min="0.1" max="100" step="0.1" placeholder="z.B. 4">
            <div class="help-text">Maximale Einspeiseleistung am Netzanschlusspunkt. Muss dem Wert im Wechselrichter entsprechen — eine Grenze, die es dort nicht gibt, verschenkt Einspeisung; eine, die wir nicht kennen, kostet Ertrag durch stille Abregelung.
              <button class="btn-link btn-tap" data-action="show-dialog" data-dialog="einspeisegrenze">Anleitung: Einspeisegrenze</button>
            </div>
          </div>`,
      })}
      ${this._exportLimitPlausibility(d)}`;
  }

  _exportLimitPlausibility(d) {
    // Huawei verrät über sensor.inverter_active_power_control, ob im Gerät
    // eine Exportbegrenzung aktiv ist. Weicht das von der Konfiguration ab,
    // sucht man sonst lange, warum abgeregelt wird oder Guard 1 grundlos anhebt.
    const state = this._readState("sensor.inverter_active_power_control")
      || this._readState("sensor.wechselrichter_active_power_control");
    if (!state) return "";
    const raw = String(state.state || "").toLowerCase();
    const deviceLimited = !(raw.startsWith("unlimited") || raw.startsWith("unbegrenzt") || raw === "");
    const configured = !!d.grid_export_limit_enabled;
    if (deviceLimited === configured) return "";
    const text = deviceLimited
      ? `Der Wechselrichter meldet eine aktive Exportbegrenzung („${this._escapeHtml(state.state)}“), hier ist aber keine Einspeisegrenze konfiguriert — die Optimierung plant dann Einspeisung, die still abgeregelt wird.`
      : `Hier ist eine Einspeisegrenze konfiguriert, der Wechselrichter meldet aber „${this._escapeHtml(state.state)}“ — die Ladelimit-Nachführung würde das Ladelimit grundlos anheben.`;
    return `<div class="help-text" style="margin-top:12px;padding:10px 12px;background:var(--warning-color,#ff9800)22;border-left:3px solid var(--warning-color,#ff9800);border-radius:4px">
      <ha-icon icon="mdi:alert-outline" style="--mdc-icon-size:16px;vertical-align:middle"></ha-icon> ${text}
    </div>`;
  }

  _renderStepAnlage() {
    // Alle physikalischen Grenzen an einem Ort: was die Anlage netzseitig
    // kann (AC-Grenze, PV-Spitze, Einspeisegrenze) und was die Batterie darf
    // (Leistungsgrenze, Mindest-Ladestand, Expertenmodus: Ladedeckel).
    // Inhaltlich identisch mit dem Einstellungs-Tab „Anlage & Batterie".
    const d = this._wizardData;
    return `
      <p style="margin-bottom:16px;color:var(--secondary-text-color)">
        Diese Grenzen kommen aus den Datenblättern von Wechselrichter und
        Batterie — die Optimierung plant nie darüber hinaus.
      </p>
      <h3 style="margin:4px 0 12px;font-size:16px">Anlage</h3>
      ${this._anlageFields(d, "")}
      <h3 style="margin:24px 0 12px;font-size:16px">Batterie</h3>
      ${this._batterieOptFields(d, "")}`;
  }

  _renderStepTarife() {
    const d = this._wizardData;
    return `
      <p style="margin-bottom:16px;color:var(--secondary-text-color)">
        Die Optimierung rechnet laufend den wirtschaftlich besten Lade- und
        Entladeplan. Diese Preise sind ihre Grundlage.
      </p>
      ${this._controlHint(d.inverter_type)}
      <h3 style="margin:4px 0 12px;font-size:16px">Vergütung</h3>
      ${this._verguetungFields(d, "")}
      <h3 style="margin:24px 0 12px;font-size:16px">Kosten</h3>
      ${this._kostenFields(d, "")}
      <h3 style="margin:24px 0 12px;font-size:16px">Energiegemeinschaft (EW Ansfelden – PeakShare)</h3>
      ${this._gemeinschaftFields(d, "")}`;
  }

  /* ── Schritt: Zusammenfassung ─────────────────── */

  _renderStepZusammenfassung() {
    const d = this._wizardData;
    const forecastName =
      d.forecast_source === "solcast_solar" ? "Solcast Solar" : "Forecast.Solar";
    const gesteuert = SCHEDULE_CONTROL_INVERTERS.includes(d.inverter_type);

    const row = (label, value) =>
      `<div class="summary-row"><span class="label">${label}</span><span class="value">${value}</span></div>`;
    const preis = (v, fallback) => `${fmtDe(ctAus(v ?? fallback), 2)} ct/kWh`;

    return `
      <p style="margin-bottom:16px;color:var(--secondary-text-color)">
        Überprüfe deine Einstellungen und klicke auf &ldquo;Fertig&rdquo; zum Speichern.
      </p>

      <div class="summary-section">
        <h3>Wechselrichter</h3>
        ${row("Typ", INVERTER_LABELS[d.inverter_type] || d.inverter_type)}
        ${row("Steuerung", gesteuert ? "Aktiv (Ladelimit + Entladung)" : "Nur Anzeige — Steuerung derzeit nur Huawei")}
      </div>

      <div class="summary-section">
        <h3>Batterie &amp; PV</h3>
        ${row("Batterieladezustand (SOC)", d.battery_soc_sensor || "—")}
        ${row(
          "Kapazität",
          d.battery_capacity_sensor
            ? d.battery_capacity_sensor
            : d.battery_capacity_kwh + " kWh (manuell)"
        )}
        ${((d.huawei_device_ids || []).length >= 2)
          ? row("Wechselrichter", `${(d.huawei_device_ids || []).length} Geräte (Master/Slave) — alle Batterien werden gesteuert`)
          : ""}
        ${row("PV-Sensor", d.pv_power_sensor || "—")}
        ${d.pv_power_sensor_2 ? row("PV-Sensor 2", d.pv_power_sensor_2) : ""}
        ${(d.battery_power_charge_sensor && d.battery_power_discharge_sensor)
          ? row("Batterie-Leistung", `${d.battery_power_charge_sensor} − ${d.battery_power_discharge_sensor}`)
          : row("Batterie-Leistung", d.battery_power_sensor || "—")}
        ${d.battery_power_sensor_2 ? row("Batterie-Leistung 2", d.battery_power_sensor_2) : ""}
        ${(d.grid_power_export_sensor && d.grid_power_import_sensor)
          ? row("Netz-Leistung", `${d.grid_power_export_sensor} − ${d.grid_power_import_sensor}`)
          : row("Netz-Leistung", d.grid_power_sensor || "—")}
      </div>

      <div class="summary-section">
        <h3>Prognose</h3>
        ${row("Quelle", forecastName)}
        ${row("Verbleibend heute", d.forecast_remaining_entity || "—")}
        ${row("Morgen", d.forecast_tomorrow_entity || "—")}
      </div>

      <div class="summary-section">
        <h3>Anlage &amp; Batterie</h3>
        ${d.inverter_ac_limit_kw ? row("AC-Grenzleistung", fmtDe(Number(d.inverter_ac_limit_kw), 1) + " kW") : ""}
        ${d.pv_peak_kwp ? row("PV-Spitzenleistung", fmtDe(Number(d.pv_peak_kwp), 1) + " kWp") : ""}
        ${row("Einspeisegrenze", d.grid_export_limit_enabled ? `Aktiv — ${fmtDe(Number(d.grid_export_limit_kw ?? 4), 1)} kW` : "Deaktiviert")}
        ${row("Batterie-Leistungsgrenze", fmtDe(Number(d.discharge_power_kw ?? 5), 1) + " kW")}
        ${row("Mindest-Ladestand", fmtDe(Number(d.schedule_min_soc_pct ?? 10), 0) + " %"
          + (d.battery_capacity_kwh
            ? ` (${fmtDe(Number(d.battery_capacity_kwh) * Number(d.schedule_min_soc_pct ?? 10) / 100, 2)} kWh Reserve)`
            : ""))}
      </div>

      <div class="summary-section">
        <h3>Tarife &amp; Gemeinschaft</h3>
        ${row("Standardvergütung", (d.schedule_feedin_source || "manual") === "oemag"
          ? (this._oemagStatus?.preis
            ? `OeMAG — ${fmtDe(this._oemagStatus.preis * 100, 3)} ct/kWh`
            : "OeMAG (noch nicht geholt)")
          : (d.schedule_feedin_source || "manual") === "spot"
          ? `Spotpreis ${(d.spot_market_area || "at") === "de" ? "EPEX DE" : "EPEX AT"}`
            + (Number(d.spot_feedin_fee ?? 0) !== 0 ? ` − ${fmtDe(ctAus(d.spot_feedin_fee), 2)} ct Abschlag` : "")
            + (this._spotStatus?.preis != null ? ` (jetzt ${fmtDe(this._spotStatus.preis * 100, 2)} ct/kWh)` : "")
          : preis(d.schedule_feedin_price, 0.082))}
        ${(d.schedule_feedin_source || "manual") === "manual" && Number(d.schedule_feedin_price_night ?? 0) > 0
          ? row("Standardvergütung Nacht", `${preis(d.schedule_feedin_price_night, 0)} (${d.schedule_night_start || "20:00"}–${d.schedule_night_end || "06:00"})`)
          : ""}
        ${row("Bezugspreis", preis(d.schedule_consumption_price, 0.247))}
        ${row("Gemeinschaftsdaten (PeakShare)", d.enable_peakshare !== false ? (d.peakshare_community || "BEG") : "Aus")}
        ${[["", d.peakshare_community], ["_2", d.peakshare_community_2]]
          .filter(([sfx, name]) => name && Number(d[`peakshare_share_pct${sfx}`] ?? 0) > 0)
          .map(([sfx, name]) => row(
            `Anteil ${name}`,
            `${fmtDe(Number(d[`peakshare_share_pct${sfx}`]), 0)} % zu `
            + `${preis(d[`peakshare_price${sfx}`], 0.102)}`
            + (Number(d[`peakshare_weight${sfx}`] ?? 0) > 0
              ? ` + ${fmtDe(Number(d[`peakshare_weight${sfx}`]) * 100, 1)} ct Gewichtung`
              : "")))
          .join("")}
      </div>

      <div class="summary-section">
        <h3>Allgemein</h3>
        ${row("Expertenmodus", d.expert_mode ? "Aktiviert" : "Deaktiviert")}
      </div>`;
  }

  /* Einstellungs-Screen */

  _renderSettings() {
    const d = this._settingsData;
    const isExpert = d.expert_mode;

    // Drei Tabs: die zwei Parameter-Tabs entsprechen 1:1 den beiden
    // Parameter-Schritten des Wizards (gleiche Feld-Renderer), „System" trägt
    // alles Übrige. Aus Vorversionen gemerkte Tab-Namen werden auf die neuen
    // abgebildet, statt stumm auf den ersten Tab zu springen.
    const TAB_ALIAS = {
      fahrplan: "tarife", gemeinschaft: "tarife",
      // Telemetrie-Karte und Sensor-Übersicht wohnen im System-Tab —
      // wer zuletzt dort war, soll dort wieder landen.
      telemetry: "system", wechselrichter: "system",
      batterie: "anlage", einspeisegrenze: "anlage",
      advanced: "system",
    };
    const activeTab = TAB_ALIAS[this._settingsTab] || this._settingsTab || "tarife";

    const tabBar = `
      <div class="settings-tabs" role="tablist">
        <button class="settings-tab ${activeTab === "tarife" ? "active" : ""}" data-action="set-settings-tab" data-tab="tarife" role="tab">
          <ha-icon icon="mdi:cash-multiple" style="--mdc-icon-size:18px"></ha-icon>
          <span>Tarife</span>
        </button>
        <button class="settings-tab ${activeTab === "anlage" ? "active" : ""}" data-action="set-settings-tab" data-tab="anlage" role="tab">
          <ha-icon icon="mdi:battery-charging-medium" style="--mdc-icon-size:18px"></ha-icon>
          <span>Anlage</span>
        </button>
        <button class="settings-tab ${activeTab === "system" ? "active" : ""}" data-action="set-settings-tab" data-tab="system" role="tab">
          <ha-icon icon="mdi:tune" style="--mdc-icon-size:18px"></ha-icon>
          <span>System</span>
        </button>
      </div>`;

    // --- Tab: Tarife & Gemeinschaft (== Wizard-Schritt) ---
    const tarifeTab = `
      <div class="card" style="margin-bottom:16px">
        <h3 class="settings-karte-titel" style="margin:0 0 4px">Vergütung und Kosten</h3>
        <div class="help-text" style="margin-bottom:16px">
          Die Optimierung rechnet laufend den wirtschaftlich besten Lade- und
          Entladeplan. Diese Preise sind ihre Grundlage.
        </div>
        ${this._controlHint(d.inverter_type)}
        ${this._verguetungFields(d, "settings_")}
        ${this._kostenFields(d, "settings_")}
      </div>
      <div class="card" style="margin-bottom:16px">
        <h3 class="settings-karte-titel" style="margin:0 0 16px">Energiegemeinschaft (EW Ansfelden – PeakShare)</h3>
        ${this._gemeinschaftFields(d, "settings_")}
      </div>`;

    // --- Tab: Anlage & Batterie (== Wizard-Schritt) ---
    const anlageTab = `
      <div class="card" style="margin-bottom:16px">
        <h3 class="settings-karte-titel" style="margin:0 0 16px">Anlage</h3>
        ${this._anlageFields(d, "settings_")}
      </div>
      <div class="card" style="margin-bottom:16px">
        <h3 class="settings-karte-titel" style="margin:0 0 16px">Batterie</h3>
        ${this._batterieOptFields(d, "settings_")}
      </div>`;

    // --- Tab: System ---
    // Reihenfolge auf Nutzerwunsch: Verbrauchsprofil (Experte) ganz oben,
    // Tagesbilanz vor dem Archiv, der Expertenmodus-Schalter ganz unten.
    const systemTab = `
      ${isExpert ? `
      <div class="card" style="margin-bottom:16px">
        <h3 class="settings-karte-titel" style="margin:0 0 16px">Verbrauchsprofil</h3>
        <div class="field-group">
          <label>Rückblick (Wochen)</label>
          <input type="number" data-field="settings_lookback_weeks"
                 value="${d.lookback_weeks || 4}" min="1" max="52">
          <div class="help-text">Wie weit zurück der Verbrauch gemittelt wird. Gemittelt wird über zwei Gruppen — Werktage (Mo–Fr) und Wochenende samt Feiertagen —, nicht über einzelne Wochentage: Vier Wochen ergeben so rund 20 Vergleichswerte je Werktagsstunde, und ein einzelner Ausreißer wie eine E-Auto-Ladung fällt kaum mehr auf. Der höchste Wert je Stunde wird zusätzlich verworfen.</div>
        </div>
      </div>` : ""}
      ${this._sensorUebersichtKarte(d)}
      ${this._renderTelemetrySection()}
      ${isExpert ? this._bilanzKarte() : ""}
      ${this._archivKarte()}
      <div class="card" style="margin-bottom:16px">
        <label style="display:flex;align-items:center;gap:12px;cursor:pointer">
          <input type="checkbox" data-field="settings_expert_mode" ${isExpert ? "checked" : ""}>
          <div>
            <div style="font-weight:500">Expertenmodus</div>
            <div class="help-text" style="margin-top:4px">Zeigt zusätzliche Optionen und Diagnose-Details — hier und in den anderen Tabs</div>
          </div>
        </label>
      </div>
      `;

    let tabContent;
    switch (activeTab) {
      case "anlage":  tabContent = anlageTab; break;
      case "system":  tabContent = systemTab; break;
      case "tarife":
      default:        tabContent = tarifeTab; break;
    }

    const luecken = this._settingsFehler || [];
    const fehlerHinweis = luecken.length
      ? `<div class="help-text" style="margin-bottom:12px;padding:10px 12px;background:var(--warning-color,#ff9800)22;border-left:3px solid var(--warning-color,#ff9800);border-radius:4px">
           <ha-icon icon="mdi:alert-outline" style="--mdc-icon-size:16px;vertical-align:middle"></ha-icon>
           Nicht gespeichert \u2014 bitte pr\u00fcfen: ${luecken.map(f => this._escapeHtml(f)).join(", ")}.
         </div>`
      : "";

    return `
      <div style="max-width:600px;margin:0 auto">
        ${tabBar}
        ${tabContent}
        ${fehlerHinweis}
        <button class="btn-primary" data-action="save-settings" style="width:100%;padding:12px">Speichern</button>
      </div>`;
  }

  // Sensor-Übersicht im System-Tab: nur Anzeige. Sensor-Zuordnungen werden
  // bewusst nicht in den Einstellungen geändert — sie ändern sich praktisch
  // nie, und der Wizard bringt Auto-Erkennung und Verbindungstests mit. Der
  // Knopf startet ihn mit allen bestehenden Werten vorbefüllt.
  _sensorUebersichtKarte(d) {
    const row = (label, value) => value
      ? `<div class="summary-row"><span class="label">${label}</span><span class="value">${this._escapeHtml(String(value))}</span></div>`
      : "";
    const paar = (a, b) => (a && b ? `${a} − ${b}` : "");
    const modbusHost =
      d.inverter_type === "fronius_gen24" ? (d.fronius_modbus_host ? `${d.fronius_modbus_host}:${d.fronius_modbus_port || 502}` : "")
      : d.inverter_type === "kostal_plenticore" ? (d.kostal_modbus_host ? `${d.kostal_modbus_host}:${d.kostal_modbus_port || 1502}` : "")
      : d.inverter_type === "sma_smart_energy" ? (d.sma_modbus_host ? `${d.sma_modbus_host}:${d.sma_modbus_port || 502}` : "")
      : "";
    return `
      <div class="card" style="margin-bottom:16px">
        <h3 class="settings-karte-titel" style="margin:0 0 8px">Anlage und Sensoren</h3>
        <div class="help-text" style="margin-bottom:12px">
          Einmalig im Einrichtungsassistenten zugeordnet. Zum Ändern den
          Assistenten erneut durchlaufen — alle Felder sind vorbefüllt.
        </div>
        <div class="summary-section" style="margin-bottom:12px">
          ${row("Wechselrichter", INVERTER_LABELS[d.inverter_type] || d.inverter_type)}
          ${row("Modbus", modbusHost)}
          ${row("PV-Leistung", d.pv_power_sensor)}
          ${row("PV-Leistung 2", d.pv_power_sensor_2)}
          ${row("Batterie-Leistung", paar(d.battery_power_charge_sensor, d.battery_power_discharge_sensor) || d.battery_power_sensor)}
          ${row("Batterie-Leistung 2", d.battery_power_sensor_2)}
          ${row("Netz-Leistung", paar(d.grid_power_export_sensor, d.grid_power_import_sensor) || d.grid_power_sensor)}
          ${row("Batterie-SOC", d.battery_soc_sensor)}
          ${row("Kapazität", d.battery_capacity_sensor
            || (d.battery_capacity_kwh ? `${fmtDe(Number(d.battery_capacity_kwh), 1)} kWh (manuell)` : ""))}
          ${row("Prognose", d.forecast_source === "forecast_solar" ? "Forecast.Solar" : "Solcast Solar")}
          ${row("Prognose heute (Rest)", d.forecast_remaining_entity)}
          ${row("Prognose morgen", d.forecast_tomorrow_entity)}
        </div>
        <button class="btn-secondary" data-action="restart-wizard" style="width:100%;display:flex;align-items:center;justify-content:center;gap:8px;padding:12px">
          <ha-icon icon="mdi:refresh" style="--mdc-icon-size:20px"></ha-icon> Einrichtung erneut durchlaufen
        </button>
      </div>`;
  }

  /* ── EEG-Statistik (Telemetrie-Opt-In) ───── */

  _renderTelemetrySection() {
    const s = this._telemetryStatus || { configured: false, enabled: false, registered: false };
    const enabled = !!s.enabled;
    const registered = !!s.registered;
    const notConfigured = !s.configured;
    const hasIdentity = !!(s.installation_id || s.installation_id_prefix);
    const fullId = s.installation_id || s.installation_id_prefix || "";
    // GUID auf die ersten drei Abschnitte kürzen (z.B. "8fcb4c46-ab80-4b2b…").
    const idParts = fullId.split("-");
    const shortId = idParts.length >= 3 ? `${idParts.slice(0, 3).join("-")}…` : fullId;

    let statusText;
    if (notConfigured) {
      statusText = "Backend-URL noch nicht eingerichtet (DEV-Build)";
    } else if (registered && s.registered_at) {
      const d = new Date(s.registered_at);
      const dStr = isNaN(d.getTime()) ? s.registered_at : d.toLocaleDateString("de-DE");
      statusText = `Registriert als anonyme Anlage <code title="${fullId}">${shortId}</code> seit ${dStr}`;
    } else if (hasIdentity && !enabled) {
      statusText = "Pausiert — Identität bleibt gespeichert";
    } else if (enabled && !registered) {
      statusText = "Registrierung läuft …";
    } else {
      statusText = "Nicht registriert";
    }

    const errorRow = this._telemetryError
      ? `<div class="help-text" style="color:var(--error-color,#d33);margin-bottom:12px">${this._telemetryError}</div>`
      : "";

    const showDeleteBtn = registered || hasIdentity;
    const deleteBtn = showDeleteBtn
      ? `<button class="btn-secondary"
                 data-action="forget-telemetry"
                 ${this._telemetryBusy ? "disabled" : ""}
                 style="background:var(--error-color,#d33);color:#fff;border:0;width:100%;padding:12px;border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px">
           <ha-icon icon="mdi:delete-forever"></ha-icon>Daten löschen
         </button>`
      : "";

    return `
      <div class="card" style="margin-bottom:16px">
        <h3 class="settings-karte-titel" style="margin:0 0 8px">EEG-Statistik</h3>
        <div class="help-text" style="margin-bottom:12px">
          Hilf Deiner EEG: deine Anlage sendet anonymisierte Diagnose- und
          Wirksamkeits-Daten an die EEG. Keine personenbezogenen Daten, keine
          IP-Adressen. Du kannst jederzeit aussteigen und die übermittelten
          Daten auch löschen.
        </div>
        <label style="display:flex;align-items:center;gap:12px;cursor:pointer;margin-bottom:12px">
          <input type="checkbox"
                 data-action="toggle-telemetry"
                 ${enabled ? "checked" : ""}
                 ${notConfigured || this._telemetryBusy ? "disabled" : ""}>
          <div>
            <div style="font-weight:500">EEG-Statistik aktivieren</div>
          </div>
        </label>
        <div class="help-text" style="margin-bottom:12px;word-break:break-all">${statusText}</div>
        <details style="margin-bottom:12px">
          <summary style="cursor:pointer;font-size:13px;color:var(--secondary-text-color);user-select:none">
            Datenschutz-Details (was wird gesendet?)
          </summary>
          <div class="help-text" style="margin-top:10px;line-height:1.5">
            <strong>Übermittelt:</strong>
            <ul style="margin:6px 0 10px 18px;padding:0">
              <li><strong>Profil</strong> (bei Setup, Restart, Settings-Change): App-/HA-Version, Wechselrichter-Typ, Batterie-Kapazität, PV-Peak, Prognose-Quelle, Land, ausgewählte EEG-Community (sofern PeakShare aktiv), Whitelist-Settings (numerische/kategorische Werte, keine Entity-IDs)</li>
              <li><strong>Failure</strong> (bei Auftreten): Kategorie, Schweregrad, gehashte Fehlermeldung</li>
            </ul>
            <strong>Nicht übermittelt:</strong>
            <ul style="margin:6px 0 10px 18px;padding:0">
              <li>Keine Entity-IDs / Sensor-Namen</li>
              <li>Keine IP-Adressen (serverseitig nicht persistiert)</li>
              <li>Kein Anlagenname, keine Adresse, keine Geokoordinaten</li>
              <li>Keine EEG-Mitgliedsdaten, keine personenbezogenen Daten</li>
            </ul>
            <strong>Identifikation:</strong> einmalig erzeugte UUIDv4 + API-Key, lokal gespeichert. Beim Löschen werden alle Daten serverseitig kaskadiert entfernt und die UUID lokal verworfen.
          </div>
        </details>
        ${errorRow}
        ${deleteBtn}
      </div>
    `;
  }

  async _handleTelemetryToggle(checked) {
    if (this._telemetryBusy) return;
    this._telemetryBusy = true;
    this._telemetryError = null;
    this._render();
    try {
      const cmd = checked ? "eeg_optimizer/telemetry_enable" : "eeg_optimizer/telemetry_disable";
      const res = await this._hass.callWS({ type: cmd });
      if (!res || res.success === false) {
        const errKey = res && res.error ? `: ${res.error}` : "";
        this._telemetryError = `Aktivieren fehlgeschlagen${errKey}`;
      }
    } catch (err) {
      this._telemetryError = `Aktivieren fehlgeschlagen: ${err && err.message ? err.message : err}`;
    } finally {
      // Status nach jedem Toggle frisch holen — Quelle der Wahrheit ist das Backend.
      try {
        this._telemetryStatus = await this._hass.callWS({ type: "eeg_optimizer/telemetry_get_status" });
      } catch (_) { /* ignore */ }
      this._telemetryBusy = false;
      this._render();
    }
  }

  async _handleTelemetryForget() {
    if (this._telemetryBusy) return;
    const ok = window.confirm(
      "Wirklich alle Daten löschen?\n\n" +
      "Alle gesendeten Telemetriedaten werden vom Server entfernt und die lokale " +
      "Anmeldung wird gelöscht. Diese Aktion kann nicht rückgängig gemacht werden."
    );
    if (!ok) return;
    this._telemetryBusy = true;
    this._telemetryError = null;
    this._render();
    try {
      const res = await this._hass.callWS({ type: "eeg_optimizer/telemetry_forget" });
      if (res && res.backend_deleted === false) {
        this._telemetryError = "Backend-Aufruf fehlgeschlagen — lokale Daten wurden trotzdem gelöscht.";
      }
    } catch (err) {
      this._telemetryError = "Backend-Aufruf fehlgeschlagen — lokale Daten wurden trotzdem gelöscht.";
    } finally {
      try {
        this._telemetryStatus = await this._hass.callWS({ type: "eeg_optimizer/telemetry_get_status" });
      } catch (_) { /* ignore */ }
      this._telemetryBusy = false;
      this._render();
    }
  }

  /* ── Info modal overlay ─────────────────────────── */

  _openGuideDialog(key) {
    const meta = DIALOG_CONTENT[key];
    if (!meta) {
      this._showDialog = null;
      this._render();
      return;
    }
    this._showDialog = { key };
    this._render();
    if (this._guideCache[key]) return;
    fetch(`/eeg_optimizer_panel/guide/${meta.file}`)
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((html) => {
        this._guideCache[key] = html;
        if (this._showDialog?.key === key) this._render();
      })
      .catch(() => {
        // Nicht cachen — erneutes Öffnen versucht den Download noch einmal
        if (this._showDialog?.key === key) {
          this._showDialog.error = true;
          this._render();
        }
      });
  }

  _renderDialog() {
    if (!this._showDialog) return "";
    const html = this._guideCache[this._showDialog.key];
    const body = html != null
      ? `<div class="guide-content">${html}</div>`
      : this._showDialog.error
        ? `<p style="color:var(--error-color,#db4437)">Anleitung konnte nicht geladen werden. Bitte Dialog schließen und erneut öffnen.</p>`
        : `<p style="color:var(--secondary-text-color)">Anleitung wird geladen…</p>`;
    return `
      <div class="dialog-overlay">
        <div class="dialog-card">
          ${body}
          <div style="text-align:right;margin-top:16px">
            <button class="btn-primary" data-action="close-dialog">Schließen</button>
          </div>
        </div>
      </div>`;
  }

  _getWeekdayKey(date) {
    return ["so", "mo", "di", "mi", "do", "fr", "sa"][date.getDay()];
  }

  _getWeekdayShort(date) {
    return ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"][date.getDay()];
  }

  _readState(entityId) {
    if (!this._hass || !entityId) return null;
    const s = this._hass.states[entityId];
    if (!s) return null;
    if (s.state === "unavailable" || s.state === "unknown") return null;
    return s;
  }


  _readFloat(entityId) {
    const s = this._readState(entityId);
    if (!s) return null;
    const v = parseFloat(s.state);
    return isNaN(v) ? null : v;
  }

  _renderSteuerungZeilen(decisionState) {
    // Früher eine eigene Karte „Steuerung". Sie stand direkt unter dem Status
    // und sagte in anderen Worten dasselbe — jetzt sind es die Detailzeilen
    // der Statuskarte: der Zustand steht oben, hier steht, was daraus folgt.
    const a = decisionState?.attributes || {};
    if (!a.letzte_aktualisierung) {
      return `<div style="font-size:13px;color:var(--secondary-text-color);margin-top:6px">Steuerung startet\u2026</div>`;
    }

    const zustand = decisionState?.state || "---";
    const gesteuert = a.gesteuert === true;

    // Was am Wechselrichter steht. Der Batterie-Sollwert liegt über der
    // Einspeisung im Zustand oben: er enthält die Hauslast (Plan + Haus − PV).
    const gesetzt = [];
    if (a.aktiv === "discharge" && a.entladeleistung_kw != null) {
      gesetzt.push(`Batterie-Sollwert <strong>${fmtDe(a.entladeleistung_kw, 2)} kW</strong>`);
      if (a.ziel_soc != null) gesetzt.push(`Ziel-Ladestand <strong>${Math.round(a.ziel_soc)} %</strong>`);
    } else if (a.aktiv === "charge_limit" && a.ladelimit_kw != null) {
      gesetzt.push(Number(a.ladelimit_kw) <= 0.05
        ? "Ladelimit <strong>0 kW</strong> (Laden blockiert)"
        : `Ladelimit <strong>${fmtDe(a.ladelimit_kw, 2)} kW</strong>`);
    } else if (a.plan_aktion === "release") {
      gesetzt.push("kein Eingriff, Eigenverbrauch des Wechselrichters");
    }
    if (a.plan_slot) gesetzt.push(`Slot ${String(a.plan_slot).slice(11, 16)}`);

    // Klartext des letzten Laufs, aber ohne den Zustand zu wiederholen.
    let statusText = a.status ? String(a.status) : "";
    if (statusText === zustand) {
      statusText = "";
    } else if (statusText.startsWith(zustand)) {
      statusText = statusText.slice(zustand.length);
      const trimChars = " -" + String.fromCharCode(0x2014);
      while (statusText && trimChars.indexOf(statusText.charAt(0)) >= 0) {
        statusText = statusText.slice(1);
      }
      if (statusText) statusText = statusText.charAt(0).toUpperCase() + statusText.slice(1);
    }

    // Startphase (erste 90 s nach dem Start): es gibt noch nichts zu
    // berichten — nur der Hinweis, sonst nichts. Sollwerte, Gründe und
    // Warnungen wären in diesem Moment Rauschen oder schlicht veraltet.
    if (this._istStartphase(decisionState)) {
      return `<div style="font-size:13px;color:var(--secondary-text-color);margin-top:4px">${this._escapeHtml(statusText || String(a.status))}</div>`;
    }

    const warnRow = (icon, color, text) => `
      <div style="display:flex;align-items:center;gap:8px;background:${color}22;border-left:3px solid ${color};border-radius:6px;padding:8px 12px;margin-top:10px;font-size:13px">
        <ha-icon icon="${icon}" style="--mdc-icon-size:18px;color:${color};flex-shrink:0"></ha-icon>
        <span>${text}</span>
      </div>`;

    let warnings = "";
    if (a.failsafe) {
      warnings += warnRow("mdi:shield-alert-outline", "#f44336",
        "Failsafe aktiv \u2014 kein brauchbarer Optimierungsplan, der Wechselrichter l\u00e4uft im Automatikmodus.");
    }
    if (a.notaus_gesperrt) {
      warnings += warnRow("mdi:alert-octagon-outline", "#f44336",
        "Not-Aus: anhaltender Netzbezug w\u00e4hrend der Entladung \u2014 gesperrt bis zum n\u00e4chsten Slot.");
    }
    if ((a.schreibfehler || 0) > 0 && a.letzter_schreibversuch_ok === false) {
      warnings += warnRow("mdi:alert-outline", "#ff9800",
        `Letzter Steuerbefehl fehlgeschlagen (${a.schreibfehler} Schreibfehler seit Start).`);
    }
    if (!gesteuert) {
      warnings += warnRow("mdi:information-outline", "var(--info-color, #2196f3)",
        "Dieser Wechselrichter wird nicht gesteuert \u2014 der Optimierungsplan ist nur Anzeige (Steuerung derzeit nur Huawei).");
    }

    const trenner = " " + String.fromCharCode(0x00B7) + " ";
    return `
      ${gesetzt.length ? `<div style="font-size:13px;color:var(--secondary-text-color);margin-top:4px">${gesetzt.join(trenner)}</div>` : ""}
      ${statusText ? `<div style="font-size:12px;color:var(--secondary-text-color);margin-top:4px;opacity:0.85">${this._escapeHtml(statusText)}</div>` : ""}
      ${a.plan_grund ? `<div style="font-size:12px;color:var(--secondary-text-color);margin-top:2px;opacity:0.85">${this._escapeHtml(a.plan_grund)}</div>` : ""}
      ${warnings}`;
  }

  // Startphase des Executors (erste 90 s nach dem Start, nur Modus "Ein"):
  // der Status ist die einzige verlässliche Quelle, ein eigenes Attribut
  // gibt es nicht.
  _istStartphase(decisionState) {
    return String(decisionState?.attributes?.status || "").startsWith("Startphase");
  }

  // Erstes Laden der Steuerwerte anstoßen — genau einmal, weitere Stände
  // holt der Aktualisieren-Knopf (refresh-control-state).
  _ensureControlState() {
    if (this._controlStateRequested || !this._hass) return;
    this._controlStateRequested = true;
    this._loadControlState();
  }

  _renderControlStateKarte(decisionState) {
    // Transparenz-Ansicht: was steht im Wechselrichter, und was haben wir
    // zuletzt geschrieben. Weichen beide ab, hat entweder jemand anderes
    // gestellt oder ein Schreibbefehl kam nicht an. Eigene Karte unter dem
    // Optimierungsplan, immer aufgeklappt — nur im Expertenmodus, für die
    // Fehlersuche gedacht, nicht für den Alltag. In der Startphase leer:
    // es wurde noch nichts geschrieben, und die Entitäten laden evtl. noch.
    if (this._istStartphase(decisionState)) return "";
    this._ensureControlState();
    const head = `
      <div class="card">
        <h3 style="margin:0">
          <ha-icon icon="mdi:tune-variant" style="--mdc-icon-size:20px;color:var(--primary-color,#03a9f4);vertical-align:middle"></ha-icon>
          Gesetzte Steuerwerte
        </h3>`;
    // Der Aktualisieren-Knopf gehört in jeden Pfad außer dem Lade-Hinweis —
    // ohne ihn gäbe es nach einem Fehler keinen Weg mehr, neu zu laden.
    const refreshBtn = `
      <button class="btn-link" data-action="refresh-control-state" style="font-size:12px;margin-top:6px">
        <ha-icon icon="mdi:refresh" style="--mdc-icon-size:14px;vertical-align:middle"></ha-icon> Aktualisieren
      </button>`;
    const foot = `</div>`;

    const cs = this._controlState;
    if (!cs) {
      return head + `<p style="font-size:13px;color:var(--secondary-text-color);margin:8px 0 0">Lade Steuerwerte…</p>` + foot;
    }
    if (cs.error) {
      return head + `<p style="font-size:13px;color:var(--error-color,#f44336);margin:8px 0 0">${this._escapeHtml(cs.error)}</p>` + refreshBtn + foot;
    }
    if (!cs.rows || !cs.rows.length) {
      return head + `<p style="font-size:13px;color:var(--secondary-text-color);margin:8px 0 0">
        Keine Stellgrößen gefunden. Bei Treibern, die die Optimierung nicht steuert, ist das erwartet.
      </p>` + refreshBtn + foot;
    }

    const rows = cs.rows.map(r => {
      // Ist-Wert der Entität ist in W, unser Schreibwert in kW — für den
      // Vergleich auf kW normieren, sonst liest sich die Zeile widersprüchlich.
      const raw = parseFloat(r.value);
      const isW = (r.unit || "").toUpperCase() === "W";
      const istKw = isNaN(raw) ? null : (isW ? raw / 1000 : raw);
      const istText = isNaN(raw)
        ? this._escapeHtml(String(r.value ?? "?"))
        : `${fmtDe(isW ? istKw : raw, isW ? 2 : 1)} ${isW ? "kW" : (r.unit || "")}`;
      let soll = "—";
      let abweichung = false;
      if (r.written != null) {
        soll = `${fmtDe(r.written, 2)} ${r.written_unit || ""}`;
        abweichung = istKw != null && Math.abs(istKw - r.written) > 0.25;
      } else if (r.role === "charge_limit" && r.max != null && istKw != null) {
        // Nichts geschrieben = Standardwert erwartet (Maximum der Entität).
        const maxKw = isW ? r.max / 1000 : r.max;
        soll = `Standard (${fmtDe(maxKw, 2)} kW)`;
        abweichung = Math.abs(istKw - maxKw) > 0.25;
      }
      const colour = abweichung ? "var(--warning-color,#ff9800)" : "inherit";
      return `<tr>
        <td style="padding:4px 10px 4px 0">${this._escapeHtml(r.label || "")}
          <div style="font-size:11px;color:var(--secondary-text-color);font-family:monospace;word-break:break-all">${this._escapeHtml(r.entity_id)}</div>
        </td>
        <td style="padding:4px 10px 4px 0;font-variant-numeric:tabular-nums;color:${colour}">${istText}</td>
        <td style="padding:4px 0;font-variant-numeric:tabular-nums;color:var(--secondary-text-color)">${soll}</td>
      </tr>`;
    }).join("");

    const hint = cs.mode !== "Ein"
      ? `<p style="font-size:12px;color:var(--secondary-text-color);margin:8px 0 0">
           Im Anzeige-Modus schreiben wir nichts — die Werte unten sind die Standardwerte des Geräts.
         </p>`
      : "";

    return head + `
      <div style="overflow-x:auto;margin-top:8px">
        <table style="font-size:13px;border-collapse:collapse;width:100%">
          <thead>
            <tr style="color:var(--secondary-text-color);font-size:11px;text-transform:uppercase;letter-spacing:.06em">
              <th style="text-align:left;padding:0 10px 4px 0">Stellgröße</th>
              <th style="text-align:left;padding:0 10px 4px 0">Im Gerät</th>
              <th style="text-align:left;padding:0 0 4px">Von uns gesetzt</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      ${cs.target_soc != null ? `<div style="font-size:13px;margin-top:6px">Ziel-Ladestand der Entladung: <strong>${Math.round(cs.target_soc)} %</strong></div>` : ""}
      ${hint}
      ${refreshBtn}` + foot;
  }

  _renderActivityTimeline() {
    if (!this._activityLog || this._activityLog.length === 0) {
      return `<p style="color:var(--secondary-text-color);font-size:14px;text-align:center;margin:16px 0">
        Noch keine Eintr\u00e4ge. Das Protokoll f\u00fcllt sich automatisch, w\u00e4hrend die Steuerung l\u00e4uft.
      </p>`;
    }

    // Kategorie eines Eintrags — deckt neue Kurzformen ("Laden begrenzt auf
    // 2,0 kW", "Entladung 2,8 kW bis 43 %", "Normalbetrieb", "Anzeige-Modus")
    // und Legacy-Zustände der alten Heuristik (Storage bleibt erhalten) ab.
    const kategorie = (z) => {
      const s = String(z || "");
      if (s.startsWith("Einspeisung") || s.startsWith("Entladung")
        || s === "Nacht-Entladung" || s === "Abend-Entladung") return "entladung";
      if (s.startsWith("Laden") || s === "Morgen-Einspeisung" || s === "Einspeisebegrenzung") return "laden";
      if (s.startsWith("Normal")) return "normal";
      return "sonstig";
    };
    const zustandIcon = (z) => {
      const k = kategorie(z);
      if (k === "laden") return "\u2600\uFE0F";
      if (k === "entladung") return "\uD83C\uDF19";
      if (k === "normal") return "\u26A1";
      return "\u2139\uFE0F";
    };
    const zustandColor = (z) => {
      const k = kategorie(z);
      if (k === "laden") return "var(--info-color, #2196F3)";
      if (k === "entladung") return "#FF9800";
      if (k === "normal") return "var(--success-color, #4CAF50)";
      return "var(--secondary-text-color, #888)";
    };

    // Already sorted newest-first from server
    const baseEntries = this._activityShowAll
      ? this._activityLog
      : this._activityLog.filter(e => e.reason !== "Heartbeat");

    const entries = this._activityFilter
      ? baseEntries.filter(e => kategorie(e.zustand) === this._activityFilter)
      : baseEntries;

    if (entries.length === 0) {
      const emptyMsg = this._activityFilter
        ? `Keine Eintr\u00e4ge f\u00fcr diesen Filter.`
        : `Keine Status\u00e4nderungen vorhanden. Aktiviere "Alle Eintr\u00e4ge", um auch Heartbeats zu sehen.`;
      return `<p style="color:var(--secondary-text-color);font-size:14px;text-align:center;margin:16px 0">${emptyMsg}</p>`;
    }

    const rows = entries.map(e => {
      const ts = e.timestamp ? new Date(e.timestamp) : null;
      const timeStr = ts ? `${String(ts.getHours()).padStart(2,"0")}:${String(ts.getMinutes()).padStart(2,"0")}` : "---";
      const dateStr = ts ? `${String(ts.getDate()).padStart(2,"0")}.${String(ts.getMonth()+1).padStart(2,"0")}` : "";
      const icon = zustandIcon(e.zustand);
      const color = zustandColor(e.zustand);
      const zustandLabel = this._escapeHtml(e.zustand || "---");
      const reason = e.reason === "Heartbeat" ? `<span style="opacity:0.5">${zustandLabel}</span>` : `<strong>${zustandLabel}</strong>`;
      const changeBadge = e.reason === "Heartbeat" ? "" : `<span class="activity-badge" style="background:${color}">\u00C4nderung</span>`;
      const testBadge = e.ausführung === false ? `<span class="activity-badge" style="background:var(--warning-color,#ff9800)">Anzeige-Modus</span>` : "";
      // Details: neue Einträge tragen den Executor-Status; alte Einträge der
      // Zustands-Heuristik ihre historischen Felder (best effort).
      const details = [];
      if (e.soc != null) details.push(`SOC ${e.soc}%`);
      if (e.status && e.status !== e.zustand) details.push(this._escapeHtml(e.status));
      if (e.plan && e.plan.kind === "discharge") details.push(`Plan: Einspeisung ${fmtDe(e.plan.power_kw ?? 0, 1)} kW`);
      else if (e.plan && e.plan.kind === "charge_limit") details.push(`Plan: Ladelimit ${fmtDe(e.plan.power_kw ?? 0, 1)} kW`);
      if ((e.schreibfehler || 0) > 0) details.push(`${e.schreibfehler} Schreibfehler`);
      // Legacy-Felder (Einträge vor dem Umbau)
      if (e.bedarf != null && e.status == null) details.push(`Gesamtbedarf ${fmtDe(e.bedarf, 1)} kWh`);
      return `<div class="activity-entry">
        <div class="activity-time">${dateStr}<br>${timeStr}</div>
        <div class="activity-dot" style="background:${color}">${icon}</div>
        <div class="activity-content">
          <div class="activity-header">${reason} ${changeBadge} ${testBadge}</div>
          <div class="activity-details">${details.join(" \u00b7 ") || "\u2014"}</div>
        </div>
      </div>`;
    }).join("");

    const remaining = this._activityTotal - this._activityLog.length;
    let moreBtn = "";
    if (this._activityLoadingMore) {
      moreBtn = `<div style="text-align:center;padding:12px;color:var(--secondary-text-color)">Laden\u2026</div>`;
    } else if (this._activityHasMore && remaining > 0) {
      moreBtn = `<div style="text-align:center;padding:8px">
        <button class="btn-secondary" data-action="show-more-activity" style="font-size:13px">
          Mehr laden (${remaining} weitere)
        </button>
      </div>`;
    }

    return `<div class="activity-timeline">${rows}</div>${moreBtn}`;
  }

  _renderBarChart(data, pvData = null) {
    if (!data || data.length === 0) return "<p>Keine Daten verfügbar</p>";
    const schmal = !!this._narrow;
    // Gezeichnet wird in echten Pixeln: viewBox-Breite == Anzeigebreite
    // (siehe _cw()). Vorher stand hier fest 700 — die viewBox stauchte das
    // Diagramm am Handy auf 45 % und mit ihm jede Schrift auf ~5 px.
    const width = this._cw("bar");
    const height = schmal ? 210 : 320;
    const padding = { top: schmal ? 24 : 30, right: schmal ? 10 : 20,
                      bottom: schmal ? 26 : 40, left: schmal ? 32 : 50 };
    const chartW = width - padding.left - padding.right;
    const chartH = height - padding.top - padding.bottom;
    // Schriftgroessen sind jetzt Pixel. Am Desktop etwas groesser, weil die
    // alte viewBox dort nach oben skalierte und der Eindruck bleiben soll.
    const fsVal = schmal ? 10 : 13;
    const fsAxis = schmal ? 10 : 12;
    const fsDay = schmal ? 11 : 13;
    const fsLegend = schmal ? 10 : 12;
    const maxVal = Math.max(...data.map(d => d.value), ...(pvData || []).map(d => d.value || 0), 1) * 1.1;
    const slotW = chartW / data.length;
    const grouped = pvData != null;
    const barW = grouped ? slotW * 0.35 : slotW * 0.7;
    const gap = grouped ? 2 : slotW * 0.3;

    let bars = "";
    data.forEach((d, i) => {
      const slotX = padding.left + i * slotW;
      if (grouped) {
        // Consumption bar (left)
        const x1 = slotX + (slotW - barW * 2 - gap) / 2;
        const barH1 = (d.value / maxVal) * chartH;
        const y1 = padding.top + chartH - barH1;
        const pvVal = pvData[i]?.value || 0;
        const barH2 = (pvVal / maxVal) * chartH;
        const y2 = padding.top + chartH - barH2;
        // Am Handy stehen die beiden Zahlen eines Paares nur ~15 px
        // auseinander. Liegen die Balken gleich hoch, ueberschreiben sie
        // sich — dann wandert die PV-Zahl eine Zeile hoeher.
        const dyPv = (schmal && pvVal > 0 && Math.abs(y1 - y2) < fsVal + 4) ? -(fsVal + 4) : 0;
        bars += `<rect x="${x1}" y="${y1}" width="${barW}" height="${barH1}" fill="var(--primary-color)" rx="3"/>`;
        bars += `<text x="${x1 + barW/2}" y="${y1 - 4}" text-anchor="middle" font-size="${fsVal}" fill="var(--primary-text-color)">${fmtDe(d.value, 1)}</text>`;

        // PV bar (right)
        if (pvVal > 0) {
          const x2 = x1 + barW + gap;
          bars += `<rect x="${x2}" y="${y2}" width="${barW}" height="${barH2}" fill="#FF9800" rx="3"/>`;
          bars += `<text x="${x2 + barW/2}" y="${y2 - 4 + dyPv}" text-anchor="middle" font-size="${fsVal}" fill="var(--primary-text-color)">${fmtDe(pvVal, 1)}</text>`;
        }

        // Day label centered under group
        bars += `<text x="${slotX + slotW/2}" y="${height - 8}" text-anchor="middle" font-size="${fsDay}" fill="var(--secondary-text-color)">${d.label}</text>`;
      } else {
        // Original single-bar rendering
        const x = slotX + (slotW - barW) / 2;
        const barH = (d.value / maxVal) * chartH;
        const y = padding.top + chartH - barH;
        bars += `<rect x="${x}" y="${y}" width="${barW}" height="${barH}" fill="var(--primary-color)" rx="3"/>`;
        bars += `<text x="${x + barW/2}" y="${y - 4}" text-anchor="middle" font-size="${fsVal}" fill="var(--primary-text-color)">${fmtDe(d.value, 1)}</text>`;
        bars += `<text x="${x + barW/2}" y="${height - 8}" text-anchor="middle" font-size="${fsDay}" fill="var(--secondary-text-color)">${d.label}</text>`;
      }
    });

    let yLines = "";
    const stufen = schmal ? 2 : 4;
    for (let i = 0; i <= stufen; i++) {
      const y = padding.top + (chartH / stufen) * i;
      const val = (maxVal * (stufen - i) / stufen).toFixed(0);
      yLines += `<line x1="${padding.left}" y1="${y}" x2="${width - padding.right}" y2="${y}" stroke="var(--divider-color)" stroke-dasharray="4"/>`;
      yLines += `<text x="${padding.left - 5}" y="${y + 4}" text-anchor="end" font-size="${fsAxis}" fill="var(--secondary-text-color)">${val}</text>`;
    }

    // Legend for grouped bars
    let legend = "";
    if (grouped) {
      const breite = schmal ? 150 : 200;
      const spalte = schmal ? 72 : 100;
      const lx = Math.max(padding.left, width - padding.right - breite);
      const ly = schmal ? 11 : 14;
      legend += `<rect x="${lx}" y="${ly - 8}" width="9" height="9" fill="var(--primary-color)" rx="2"/>`;
      legend += `<text x="${lx + 13}" y="${ly}" font-size="${fsLegend}" fill="var(--primary-text-color)">Verbrauch</text>`;
      legend += `<rect x="${lx + spalte}" y="${ly - 8}" width="9" height="9" fill="#FF9800" rx="2"/>`;
      legend += `<text x="${lx + spalte + 13}" y="${ly}" font-size="${fsLegend}" fill="var(--primary-text-color)">PV-Prognose</text>`;
    }

    return `<svg data-cw="bar" viewBox="0 0 ${width} ${height}" style="width:100%;height:auto;">${yLines}${bars}${legend}</svg>`;
  }

  _renderEnergyFlow(pvKw, batKw, gridKw, hausKw, socVal, ids = {}) {
    // --- Decompose flows from the four signed values ---
    const pv = Math.max(pvKw, 0);
    const batCharge = Math.max(batKw, 0);          // battery charging (sink)
    const batDischarge = Math.max(-batKw, 0);      // battery discharging (source)
    const gridExport = Math.max(gridKw, 0);        // feed-in to grid
    const gridImport = Math.max(-gridKw, 0);       // import from grid
    const haus = Math.max(hausKw, 0);

    // Priority: PV → Haus → Batterie → Netz
    const pvToHaus = Math.min(pv, haus);
    let pvLeft = pv - pvToHaus;
    const pvToBat = Math.min(pvLeft, batCharge);
    pvLeft -= pvToBat;
    const pvToGrid = Math.min(pvLeft, gridExport);

    // Remaining demand on the house side
    const hausFromBat = Math.min(haus - pvToHaus, batDischarge);
    const hausFromGrid = Math.max(haus - pvToHaus - hausFromBat, 0);
    // Battery filled by something other than PV (rare: from grid)
    const batFromGrid = Math.max(batCharge - pvToBat, 0);
    // Battery discharge beyond house demand feeds the grid (Nacht-Entladung)
    const batToGrid = Math.min(Math.max(batDischarge - hausFromBat, 0), Math.max(gridExport - pvToGrid, 0));

    // --- Layout ---
    // Narrow (Smartphone): kompaktere viewBox, damit die SVG-Skalierung nahe 1:1
    // bleibt und Schriften lesbar sind (600er-viewBox auf ~340px = ~55% Schriftgröße).
    const narrow = !!this._narrow;
    const W = narrow ? 360 : 600;
    const H = narrow ? 390 : 320;
    const NW = narrow ? 140 : 150;
    const NH = 64;
    const positions = narrow ? {
      pv:    { cx: 180, cy: 50 },
      bat:   { cx: 78,  cy: 195 },
      house: { cx: 180, cy: 340 },
      grid:  { cx: 282, cy: 195 },
    } : {
      pv:    { cx: 300, cy: 50 },
      bat:   { cx: 95,  cy: 160 },
      house: { cx: 300, cy: 270 },
      grid:  { cx: 505, cy: 160 },
    };

    // MDI-Pfade (24×24) nativ eingebettet — <foreignObject> mit ha-icon wird von
    // iOS Safari in skalierten SVGs falsch positioniert (WebKit-Bug).
    const ICON_PATHS = {
      solar: "M11.45,2V5.55L15,3.77L11.45,2M10.45,8L8,10.46L11.75,11.71L10.45,8M2,11.45L3.77,15L5.55,11.45H2M10,2H2V10C2.57,10.17 3.17,10.25 3.77,10.25C7.35,10.26 10.26,7.35 10.27,3.75C10.26,3.16 10.17,2.57 10,2M17,22V16H14L19,7V13H22L17,22Z",
      battery: "M16.67,4H15V2H9V4H7.33A1.33,1.33 0 0,0 6,5.33V20.67C6,21.4 6.6,22 7.33,22H16.67A1.33,1.33 0 0,0 18,20.67V5.33C18,4.6 17.4,4 16.67,4Z",
      home: "M10,20V14H14V20H19V12H22L12,3L2,12H5V20H10Z",
      grid: "M8.28,5.45L6.5,4.55L7.76,2H16.23L17.5,4.55L15.72,5.44L15,4H9L8.28,5.45M18.62,8H14.09L13.3,5H10.7L9.91,8H5.38L4.1,10.55L5.89,11.44L6.62,10H17.38L18.1,11.45L19.89,10.56L18.62,8M17.77,22H15.7L15.46,21.1L12,15.9L8.53,21.1L8.3,22H6.23L9.12,11H11.19L10.83,12.35L12,14.1L13.16,12.35L12.81,11H14.88L17.77,22M11.4,15L10.5,13.65L9.32,18.13L11.4,15M14.68,18.12L13.5,13.64L12.6,15L14.68,18.12Z",
    };

    // Trim line to box edge so arrow doesn't hide under the rect
    const trim = (from, to) => {
      const dx = to.cx - from.cx;
      const dy = to.cy - from.cy;
      const len = Math.hypot(dx, dy) || 1;
      const ux = dx / len, uy = dy / len;
      const tFrom = Math.min((NW / 2 + 4) / Math.max(Math.abs(ux), 0.001), (NH / 2 + 4) / Math.max(Math.abs(uy), 0.001));
      const tTo   = Math.min((NW / 2 + 4) / Math.max(Math.abs(ux), 0.001), (NH / 2 + 4) / Math.max(Math.abs(uy), 0.001));
      return {
        x1: from.cx + ux * tFrom,
        y1: from.cy + uy * tFrom,
        x2: to.cx - ux * tTo,
        y2: to.cy - uy * tTo,
      };
    };

    // --- Flow lines ---
    // labelT: Label-Position entlang der Linie (0..1). PV→Haus sitzt bei 0.35,
    // damit das Label nicht mit dem von Batterie↔Netz im Kreuzungspunkt kollidiert.
    // skipInactive: Batterie→Netz teilt sich das Segment mit Netz→Batterie —
    // die inaktive gestrichelte Linie nur einmal zeichnen.
    const flows = [
      { from: positions.pv,    to: positions.house, value: pvToHaus,      color: "#FFC107", labelT: 0.35 },
      { from: positions.pv,    to: positions.bat,   value: pvToBat,       color: "#FFC107" },
      { from: positions.pv,    to: positions.grid,  value: pvToGrid,      color: "#4CAF50" },
      { from: positions.bat,   to: positions.house, value: hausFromBat,   color: "#FF9800" },
      { from: positions.bat,   to: positions.grid,  value: batToGrid,     color: "#4CAF50", skipInactive: true },
      { from: positions.grid,  to: positions.house, value: hausFromGrid,  color: "#F44336" },
      { from: positions.grid,  to: positions.bat,   value: batFromGrid,   color: "#F44336" },
    ];

    let activeLines = "";
    let inactiveLines = "";
    let labels = "";
    flows.forEach(f => {
      const e = trim(f.from, f.to);
      if (f.value > 0.02) {
        const sw = Math.min(Math.max(2, f.value * 0.7), 5);
        activeLines += `<line class="flow-line" x1="${e.x1}" y1="${e.y1}" x2="${e.x2}" y2="${e.y2}" stroke="${f.color}" stroke-width="${sw}" fill="none"/>`;
        // Label entlang der Linie — Breite dynamisch, damit Werte >= 10 kW nicht überlaufen
        const t = f.labelT ?? 0.5;
        const mx = e.x1 + (e.x2 - e.x1) * t;
        const my = e.y1 + (e.y2 - e.y1) * t;
        const txt = fmtDe(f.value, 2);
        const lw = Math.max(44, txt.length * 7 + 16);
        labels += `<g transform="translate(${mx} ${my})">
          <rect class="ef-flow-label" x="${-lw / 2}" y="-10" width="${lw}" height="20" rx="10"/>
          <text class="ef-flow-text" x="0" y="4" text-anchor="middle">${txt}</text>
        </g>`;
      } else if (!f.skipInactive) {
        inactiveLines += `<line x1="${e.x1}" y1="${e.y1}" x2="${e.x2}" y2="${e.y2}" stroke="var(--divider-color, #e0e0e0)" stroke-width="1.5" stroke-dasharray="2 5" opacity="0.5"/>`;
      }
    });

    // --- Node renderer ---
    const node = (pos, iconPath, title, mainText, subText, accent, active, entityId) => {
      const x = pos.cx - NW / 2;
      const y = pos.cy - NH / 2;
      const opacity = active ? 1 : 0.55;
      const clickable = entityId ? `data-action="show-entity" data-entity="${entityId}" style="cursor:pointer"` : "";
      const iconSize = narrow ? 26 : 30;
      const iconX = x + (narrow ? 8 : 12);
      const iconY = y + (NH - iconSize) / 2;
      const textX = x + (narrow ? 42 : 54);
      return `<g class="ef-node" opacity="${opacity}" ${clickable}>
        <rect x="${x}" y="${y}" width="${NW}" height="${NH}" rx="14"
          fill="var(--card-background-color, #fff)"
          stroke="${accent}" stroke-width="${active ? 2.5 : 1.5}"/>
        <path d="${iconPath}" fill="${accent}"
          transform="translate(${iconX} ${iconY}) scale(${iconSize / 24})"/>
        <text class="ef-node-title" x="${textX}" y="${y + 22}" fill="var(--secondary-text-color)">${title.toUpperCase()}</text>
        <text class="ef-node-main" x="${textX}" y="${y + 40}" fill="var(--primary-text-color)">${mainText}</text>
        ${subText ? `<text class="ef-node-sub" x="${textX}" y="${y + 55}" fill="var(--secondary-text-color)">${subText}</text>` : ""}
      </g>`;
    };

    // PV
    const pvNode = node(positions.pv, ICON_PATHS.solar, "Photovoltaik", `${fmtDe(pv, 2)} kW`, "", "#FFC107", pv > 0.02, ids.pvEntity);

    // Battery — im Narrow-Layout nur Vorzeichen statt "· Ladung/Entladung" (Platz)
    let batMain = socVal != null ? `${socVal} %` : "—";
    let batSub = "Idle";
    let batAccent = "#9E9E9E";
    if (batCharge > 0.02) {
      batSub = narrow ? `+${fmtDe(batCharge, 2)} kW` : `+${fmtDe(batCharge, 2)} kW · Ladung`;
      batAccent = "#4CAF50";
    } else if (batDischarge > 0.02) {
      batSub = narrow ? `−${fmtDe(batDischarge, 2)} kW` : `${fmtDe(batDischarge, 2)} kW · Entladung`;
      batAccent = "#FF9800";
    }
    const batNode = node(positions.bat, ICON_PATHS.battery, "Batterie", batMain, batSub, batAccent, batCharge > 0.02 || batDischarge > 0.02, ids.batEntity);

    // House
    const houseNode = node(positions.house, ICON_PATHS.home, "Haus", `${fmtDe(haus, 2)} kW`, "", "#2196F3", haus > 0.02, ids.hausEntity);

    // Grid
    let gridMain = "0 kW";
    let gridSub = "";
    let gridAccent = "#9E9E9E";
    if (gridExport > 0.02) {
      gridMain = `${fmtDe(gridExport, 2)} kW`;
      gridSub = "Einspeisung";
      gridAccent = "#4CAF50";
    } else if (gridImport > 0.02) {
      gridMain = `${fmtDe(gridImport, 2)} kW`;
      gridSub = "Bezug";
      gridAccent = "#F44336";
    }
    const gridNode = node(positions.grid, ICON_PATHS.grid, "Netz", gridMain, gridSub, gridAccent, gridExport > 0.02 || gridImport > 0.02, ids.gridEntity);

    return `<svg class="energy-flow-svg${narrow ? " narrow" : ""}" viewBox="0 0 ${W} ${H}">
      ${inactiveLines}
      ${activeLines}
      ${labels}
      ${pvNode}
      ${batNode}
      ${houseNode}
      ${gridNode}
    </svg>`;
  }

  // Sonnenauf-/-untergang waren früher Parameter, werden im Diagramm aber
  // nicht mehr gezeichnet — die Nachtgrenzen kommen aus dischargeStartHour
  // und nightEndDecimal. Der Aufrufer braucht sunriseHour weiterhin als
  // Fallback für nightEndDecimal.
  _renderDayNightChart(data, dischargeStartHour, nightEndDecimal) {
    if (!data || data.length === 0) return "<p>Keine Daten verfügbar</p>";

    // Format end-of-night time from decimal hours (e.g. 6.77 → "06:46")
    const fmtDecimal = (dec) => {
      const h = Math.floor(dec);
      const m = Math.round((dec - h) * 60);
      const hh = m === 60 ? h + 1 : h;
      const mm = m === 60 ? 0 : m;
      return `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}`;
    };
    const nightStart = `${String(dischargeStartHour).padStart(2, "0")}:00`;
    const nightEnd = fmtDecimal(nightEndDecimal);

    // Die frühere Entlade-Schwelle (min_soc/safety_buffer der alten
    // Zustands-Heuristik) ist mit dem Fahrplan entfallen — deshalb gibt es
    // hier keine Limit-Linie und keinen Hinweis darauf mehr.
    const schmal = !!this._narrow;
    // viewBox == Anzeigebreite (siehe _cw()), Schrift also in echten Pixeln.
    const width = this._cw("daynight");
    const height = schmal ? 250 : 330;
    // Am Handy steht die Legende zweizeilig — die Nacht-Beschriftung mit
    // Zeitfenster ist allein schon ~190 px breit.
    const padding = { top: schmal ? 40 : 30, right: schmal ? 10 : 20,
                      bottom: schmal ? 28 : 50, left: schmal ? 32 : 50 };
    const chartW = width - padding.left - padding.right;
    const chartH = height - padding.top - padding.bottom;
    const fsVal = schmal ? 10 : 13;
    const fsAxis = schmal ? 10 : 12;
    const fsDay = schmal ? 11 : 13;
    const fsLegend = schmal ? 11 : 12;
    const allValues = data.flatMap(d => [d.tag, d.nacht]);
    const maxVal = Math.max(...allValues, 1) * 1.1;
    const slotW = chartW / data.length;
    const barW = slotW * 0.35;
    const gap = 2;

    let bars = "";
    data.forEach((d, i) => {
      const slotX = padding.left + i * slotW;

      // Day bar (left, orange)
      const x1 = slotX + (slotW - barW * 2 - gap) / 2;
      const barH1 = (d.tag / maxVal) * chartH;
      const y1 = padding.top + chartH - barH1;
      const x2 = x1 + barW + gap;
      const barH2 = (d.nacht / maxVal) * chartH;
      const y2 = padding.top + chartH - barH2;
      // Gleich hohe Balken: die zweite Zahl eine Zeile hoeher setzen, sonst
      // ueberschreiben sich die beiden bei schmaler Karte.
      const dyNacht = (schmal && d.nacht > 0 && Math.abs(y1 - y2) < fsVal + 4) ? -(fsVal + 4) : 0;
      bars += `<rect x="${x1}" y="${y1}" width="${barW}" height="${barH1}" fill="#FF9800" rx="3">
        <title>${d.label} Tag-Verbrauch (Rest des Tages außerhalb der Nachtperiode): ${fmtDe(d.tag, 2)} kWh</title>
      </rect>`;
      if (d.tag > 0) {
        bars += `<text x="${x1 + barW/2}" y="${y1 - 4}" text-anchor="middle" font-size="${fsVal}" fill="var(--primary-text-color)">${fmtDe(d.tag, 1)}</text>`;
      }

      // Night bar (right)
      bars += `<rect x="${x2}" y="${y2}" width="${barW}" height="${barH2}" fill="#2196F3" rx="3">
        <title>${d.label} Nacht-Verbrauch (${nightStart} → ${nightEnd} Folgetag): ${fmtDe(d.nacht, 2)} kWh</title>
      </rect>`;
      if (d.nacht > 0) {
        bars += `<text x="${x2 + barW/2}" y="${y2 - 4 + dyNacht}" text-anchor="middle" font-size="${fsVal}" fill="var(--primary-text-color)">${fmtDe(d.nacht, 1)}</text>`;
      }

      // Day label centered under the group
      bars += `<text x="${slotX + slotW/2}" y="${height - 8}" text-anchor="middle" font-size="${fsDay}" fill="var(--secondary-text-color)">${d.label}</text>`;
    });

    // Y-axis grid + label
    let yLines = `<text x="${padding.left - (schmal ? 26 : 36)}" y="${padding.top - 8}" font-size="${fsAxis}" fill="var(--secondary-text-color)">kWh</text>`;
    const stufen = schmal ? 2 : 4;
    for (let i = 0; i <= stufen; i++) {
      const y = padding.top + (chartH / stufen) * i;
      const val = fmtDe(maxVal * (stufen - i) / stufen, 1);
      yLines += `<line x1="${padding.left}" y1="${y}" x2="${width - padding.right}" y2="${y}" stroke="var(--divider-color)" stroke-dasharray="4"/>`;
      yLines += `<text x="${padding.left - 5}" y="${y + 4}" text-anchor="end" font-size="${fsAxis}" fill="var(--secondary-text-color)">${val}</text>`;
    }

    // Legende: am Handy zwei Zeilen, sonst eine.
    const lx = padding.left;
    const ly1 = schmal ? 12 : 14;
    const ly2 = schmal ? 28 : 14;
    const nachtX = schmal ? lx : lx + 60;
    let legend = `
      <rect x="${lx}" y="${ly1 - 8}" width="9" height="9" fill="#FF9800" rx="2"/>
      <text x="${lx + 13}" y="${ly1}" font-size="${fsLegend}" fill="var(--primary-text-color)">Tag</text>
      <rect x="${nachtX}" y="${ly2 - 8}" width="9" height="9" fill="#2196F3" rx="2"/>
      <text x="${nachtX + 13}" y="${ly2}" font-size="${fsLegend}" fill="var(--primary-text-color)">Nacht (${nightStart} → ${nightEnd} Folgetag)</text>`;

    return `<svg data-cw="daynight" viewBox="0 0 ${width} ${height}" style="width:100%;height:auto;">${yLines}${bars}${legend}</svg>`;
  }

  _renderConsumptionProfileStatus(profilState) {
    const attrs = profilState?.attributes || {};
    const statsCount = attrs.stats_count ?? 0;
    const lookback = attrs.lookback_weeks ?? this._config?.lookback_weeks ?? "?";
    const lastRefresh = attrs.last_refresh;
    const durationMs = attrs.last_duration_ms;

    const fmtRefresh = (iso) => {
      if (!iso) return "noch nie";
      try {
        return new Date(iso).toLocaleString("de-DE", {
          day: "2-digit", month: "2-digit", year: "numeric",
          hour: "2-digit", minute: "2-digit", second: "2-digit",
        });
      } catch { return "---"; }
    };

    let resultBanner = "";
    const r = this._profileRefreshResult;
    if (r) {
      if (r.success) {
        const dur = r.duration_ms != null ? `${fmtDe(r.duration_ms / 1000, 1)} s` : "";
        const cnt = r.stats_count != null ? `${r.stats_count} Datenpunkte` : "";
        const parts = [cnt, dur].filter(Boolean).join(", ");
        resultBanner = `<div class="inverter-test-result success" style="margin-top:8px">
          <ha-icon icon="mdi:check-circle"></ha-icon> Verbrauchsprofil aktualisiert${parts ? ` (${parts})` : ""}.
        </div>`;
      } else if (r.busy) {
        resultBanner = `<div class="inverter-test-result error" style="margin-top:8px">
          <ha-icon icon="mdi:timer-sand"></ha-icon> Eine Neuberechnung läuft bereits — bitte kurz warten.
        </div>`;
      } else {
        resultBanner = `<div class="inverter-test-result error" style="margin-top:8px">
          <ha-icon icon="mdi:alert-circle"></ha-icon> ${r.error || "Neuberechnung fehlgeschlagen."}
        </div>`;
      }
    }

    const running = this._profileRefreshing;
    const btnLabel = running ? "Wird neu berechnet…" : "Verbrauchsprofil neu berechnen";
    const durationHint = durationMs != null ? ` · letzte Dauer: ${fmtDe(durationMs / 1000, 1)} s` : "";

    return `
      <div style="margin-top:12px;padding:12px;background:var(--secondary-background-color);border-radius:8px">
        <div style="display:flex;flex-wrap:wrap;gap:12px 24px;font-size:13px;color:var(--secondary-text-color)">
          <div><strong style="color:var(--primary-text-color)">Datenpunkte:</strong> ${statsCount}</div>
          <div><strong style="color:var(--primary-text-color)">Fenster:</strong> ${lookback} Wochen (laut gespeicherter Konfig)</div>
          <div><strong style="color:var(--primary-text-color)">Letzte Berechnung:</strong> ${fmtRefresh(lastRefresh)}${durationHint}</div>
        </div>
        <div style="margin-top:10px;display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <button data-action="refresh-consumption-profile"
            ${running ? "disabled" : ""}
            style="padding:8px 14px;border-radius:8px;border:none;cursor:${running ? "default" : "pointer"};
              background:var(--primary-color,#03a9f4);color:#fff;font-size:14px;font-weight:500;
              opacity:${running ? "0.6" : "1"};display:inline-flex;align-items:center;gap:8px">
            ${running
              ? `<div class="manual-spinner" style="width:14px;height:14px;border-width:2px"></div>`
              : `<ha-icon icon="mdi:refresh" style="--mdc-icon-size:18px"></ha-icon>`}
            <span>${btnLabel}</span>
          </button>
          <span style="font-size:12px;color:var(--secondary-text-color)">
            Liest die Verbrauchsstatistik der letzten ${lookback} Wochen aus dem Recorder neu ein.
          </span>
        </div>
        ${resultBanner}
      </div>`;
  }

  _renderLineChart(datasets, highlightIndex = 0) {
    if (!datasets || datasets.length === 0) return "<p>Keine Daten verfügbar</p>";
    const schmal = !!this._narrow;
    // viewBox == Anzeigebreite, damit die Schrift in Pixeln stimmt (_cw()).
    const width = this._cw("line");
    const height = schmal ? 290 : 340;
    const padding = { top: schmal ? 14 : 20, right: schmal ? 10 : 20,
                      bottom: schmal ? 78 : 80, left: schmal ? 34 : 55 };
    const chartW = width - padding.left - padding.right;
    const chartH = height - padding.top - padding.bottom;
    const fsAxis = schmal ? 10 : 12;
    const fsLegend = schmal ? 11 : 12;
    const allVals = datasets.flatMap(ds => ds.data);
    const maxVal = Math.max(...allVals, 0.1) * 1.1;

    // Y-axis grid
    let yLines = "";
    const stufen = schmal ? 2 : 4;
    for (let i = 0; i <= stufen; i++) {
      const y = padding.top + (chartH / stufen) * i;
      const val = fmtDe(maxVal * (stufen - i) / stufen, 1);
      yLines += `<line x1="${padding.left}" y1="${y}" x2="${width - padding.right}" y2="${y}" stroke="var(--divider-color)" stroke-dasharray="4"/>`;
      yLines += `<text x="${padding.left - 5}" y="${y + 4}" text-anchor="end" font-size="${fsAxis}" fill="var(--secondary-text-color)">${val}</text>`;
    }

    // X-Achse — am Handy alle 6 h, sonst alle 3 h: mehr Marken haetten bei
    // ~300 px Breite keinen Platz mehr zwischen den Beschriftungen.
    let xLabels = "";
    const stepH = schmal ? 6 : 3;
    for (let h = 0; h < 24; h += stepH) {
      const x = padding.left + (h / 23) * chartW;
      xLabels += `<text x="${x}" y="${padding.top + chartH + 15}" text-anchor="middle" font-size="${fsAxis}" fill="var(--secondary-text-color)">${h}:00</text>`;
    }

    // Weekday colors (7 distinct colors)
    const weekdayColors = ["#4CAF50", "#2196F3", "#FF9800", "#9C27B0", "#F44336", "#00BCD4", "#FF5722"];

    // All lines as hoverable groups
    let allLines = "";
    datasets.forEach((ds, idx) => {
      const color = weekdayColors[idx % weekdayColors.length];
      const isHighlight = idx === highlightIndex;
      let pts = "";
      let areaPts = `${padding.left},${padding.top + chartH} `;
      ds.data.forEach((val, i) => {
        const x = padding.left + (i / 23) * chartW;
        const y = padding.top + chartH - (val / maxVal) * chartH;
        pts += `${x},${y} `;
        areaPts += `${x},${y} `;
      });
      areaPts += `${padding.left + chartW},${padding.top + chartH}`;

      // Die nicht hervorgehobenen Linien lagen bei stroke-width 1 und 0.3
      // Deckkraft — nach der alten viewBox-Stauchung real 0,4 px und damit
      // unsichtbar. Jetzt echte Pixel, am Handy einen Hauch kraeftiger.
      const baseOpacity = isHighlight ? "1" : (schmal ? "0.4" : "0.3");
      const baseSw = isHighlight ? "2.5" : (schmal ? "1.4" : "1");
      allLines += `<g class="wl${isHighlight ? " wl-today" : ""}" data-idx="${idx}">`;
      // Invisible wide hit area for easier hover/touch
      allLines += `<polyline points="${pts}" fill="none" stroke="transparent" stroke-width="16" style="pointer-events:stroke"/>`;
      // Area fill (visible on highlight or hover)
      allLines += `<polygon class="wl-area" points="${areaPts}" fill="${color}" opacity="${isHighlight ? '0.12' : '0'}"/>`;
      // Visible line
      allLines += `<polyline class="wl-line" points="${pts}" fill="none" stroke="${color}" stroke-width="${baseSw}" opacity="${baseOpacity}"/>`;
      allLines += `</g>`;
    });

    // Legende — zwei Reihen. Die Flaeche je Eintrag ist zugleich das
    // Tipp-Ziel: ein Tipp hebt den Wochentag hervor (auf dem Handy gibt es
    // kein Hover, das war vorher die einzige Bedienung).
    let legend = "";
    const legendRow1Y = padding.top + chartH + (schmal ? 38 : 40);
    const legendRow2Y = legendRow1Y + (schmal ? 26 : 22);
    const itemsPerRow = 4;
    const legendItemW = chartW / itemsPerRow;
    datasets.forEach((ds, idx) => {
      const row = Math.floor(idx / itemsPerRow);
      const col = idx % itemsPerRow;
      const lx = padding.left + col * legendItemW;
      const ly = row === 0 ? legendRow1Y : legendRow2Y;
      const isHighlight = idx === highlightIndex;
      const color = weekdayColors[idx % weekdayColors.length];
      const fw = isHighlight ? "bold" : "normal";
      const opacity = isHighlight ? "1" : "0.6";
      const sw = isHighlight ? "2.5" : "1.5";
      const trefferH = schmal ? 30 : 22;
      legend += `<g class="wl-legend" data-idx="${idx}" style="cursor:pointer">`;
      // Invisible wider hit area for easier hover/touch
      legend += `<rect x="${lx - 4}" y="${ly - trefferH / 2 - 4}" width="${legendItemW}" height="${trefferH}" fill="transparent"/>`;
      legend += `<line x1="${lx}" y1="${ly - 4}" x2="${lx + 16}" y2="${ly - 4}" stroke="${color}" stroke-width="${sw}" opacity="${opacity}"/>`;
      legend += `<text x="${lx + 20}" y="${ly}" font-size="${fsLegend}" font-weight="${fw}" fill="var(--primary-text-color)" opacity="${opacity}">${ds.label}</text>`;
      legend += `</g>`;
    });

    return `<svg data-cw="line" viewBox="0 0 ${width} ${height}" style="width:100%;height:auto;">
      <style>
        .wl { cursor: pointer; }
        .wl.wl-legend-hover .wl-line { stroke-width: 2.5 !important; opacity: 1 !important; }
        .wl.wl-legend-hover .wl-area { opacity: 0.12 !important; }
        svg:has(.wl-legend-hover) .wl:not(.wl-legend-hover):not(.wl-today) .wl-line { opacity: 0.12 !important; }
        svg:has(.wl-legend-hover) .wl-today:not(.wl-legend-hover) .wl-line { opacity: 0.4 !important; }
        @media (hover: hover) {
          .wl:hover .wl-line { stroke-width: 2.5 !important; opacity: 1 !important; }
          .wl:hover .wl-area { opacity: 0.12 !important; }
          svg:has(.wl:hover) .wl:not(:hover):not(.wl-today) .wl-line { opacity: 0.12 !important; }
          svg:has(.wl:hover) .wl-today:not(:hover) .wl-line { opacity: 0.4 !important; }
        }
      </style>
      ${yLines}
      ${allLines}
      ${xLabels}
      ${legend}
    </svg>`;
  }


  _renderJobStatusLine(decisionState, profilState) {
    // Letzte Laufzeiten der vier Jobs; rot, wenn ein Job deutlich länger als
    // sein Takt nicht gelaufen ist (Fahrplan 1 min, Steuerung 30 s,
    // Verbrauchsprofil 15 min, PeakShare-Cache bis 6 h frisch).
    const now = Date.now();
    const ageSec = (iso) => {
      if (!iso) return null;
      const t = new Date(iso).getTime();
      return isNaN(t) ? null : Math.max(0, (now - t) / 1000);
    };
    const fmtClock = (iso) => {
      try { return new Date(iso).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
      catch { return "---"; }
    };

    const jobs = [];
    const scheduleIso = this._scheduleData?.last_run;
    const scheduleTakt = 60;   // Rechentakt ist fest: jede Minute
    const scheduleAge = ageSec(scheduleIso);
    jobs.push({
      label: "Plan",
      text: scheduleIso ? fmtClock(scheduleIso) : "\u2014",
      stale: scheduleAge != null && scheduleAge > Math.max(scheduleTakt * 2, 180),
      error: !!(this._scheduleData && this._scheduleData.available === false),
      hint: this._scheduleData?.error || "",
    });

    const execIso = decisionState?.attributes?.letzte_aktualisierung;
    const execAge = ageSec(execIso);
    jobs.push({
      label: "Steuerung",
      text: execIso ? fmtClock(execIso) : "\u2014",
      stale: execAge != null && execAge > 90,
    });

    const profilIso = profilState?.attributes?.last_refresh;
    // Der Profil-Takt ist festverdrahtet (15 min) — kein Konfigschlüssel mehr.
    const slowTakt = 15 * 60;
    const profilAge = ageSec(profilIso);
    jobs.push({
      label: "Verbrauchsprofil",
      text: profilIso ? fmtClock(profilIso) : "\u2014",
      stale: profilAge != null && profilAge > slowTakt * 2 + 300,
    });

    if (this._config?.enable_peakshare !== false) {
      const ageMin = this._peakshareData?.cache_age_minutes;
      jobs.push({
        label: "PeakShare",
        text: ageMin != null ? (ageMin < 60 ? `vor ${ageMin} min` : `vor ${Math.round(ageMin / 60)} h`) : "\u2014",
        stale: ageMin != null && ageMin > 420,
      });
    }

    const span = (j) => {
      const warn = j.error || j.stale;
      if (!warn) return `<span>${j.label}: ${j.text}</span>`;
      // Der Grund stand nur im title-Attribut — am Handy gibt es kein Hover,
      // dort war die Fehlerursache damit unerreichbar. Jetzt antippbar.
      const grund = j.error
        ? `Planfehler: ${this._escapeHtml(j.hint || "unbekannt")}`
        : "Dieser Job l\u00e4uft nicht im erwarteten Takt. Bleibt es dabei, hilft ein Blick ins Protokoll von Home Assistant.";
      return `<span class="info-popup-trigger" style="color:var(--error-color,#db4437);font-weight:600;cursor:pointer">
        <span>${j.label}: ${j.text} \u26A0</span>
        <div class="info-popup"><strong>${j.label}</strong><p>${grund}</p></div>
      </span>`;
    };
    return `<div class="header-timestamps">${jobs.map(span).join("")}</div>`;
  }

  _renderDashboard() {
    const h = this._hass;
    if (!h) return "<p>Lade...</p>";

    // Geldwerte nachziehen, wenn sie älter als eine Minute sind.
    this._ensureBilanz();

    // --- Status card ---
    const modeState = this._readState(this._entityIds?.select || "select.eeg_energy_optimizer_optimizer");
    const modeValue = modeState ? modeState.state : "---";
    const modeToggleClass = modeValue === "Ein" ? "ein" : "test";

    const decisionState = this._readState(this._entityIds?.entscheidung || "sensor.eeg_energy_optimizer_entscheidung");

    // Connection lost banner
    if (!decisionState && !modeState) {
      return `<div class="connection-lost">
        <div class="connection-lost-icon">&#9888;</div>
        <h2>Verbindung verloren</h2>
        <p>Warte auf Verbindung zum Home Assistant Server...</p>
        <div class="connection-lost-spinner"></div>
      </div>`;
    }

    // Kurzform der Steuerung ("Laden begrenzt auf 2,0 kW", "Entladung 2,8 kW
    // bis 43 %", "Normalbetrieb", "Anzeige-Modus"). Einzige Stelle im
    // Dashboard, die den Zustand nennt - die Steuerungskarte darunter zeigt
    // nur noch, was daraus folgt.
    const zustand = decisionState?.state || "---";
    const zaGesteuert = decisionState?.attributes?.gesteuert === true;
    const zaAnzeige = !zaGesteuert || decisionState?.attributes?.modus !== "Ein";
    const zustandBadgeClass = zaAnzeige ? "gray" :
      (zustand.startsWith("Einspeisung") || zustand.startsWith("Entladung")) ? "orange" :
      zustand.startsWith("Laden") ? "blue" : "green";
    // Gefüllter Punkt = wird gesetzt, offener Punkt = nur gerechnet,
    // Strich = dieser Wechselrichter wird gar nicht gesteuert.
    const zustandSymbol = !zaAnzeige
      ? String.fromCharCode(0x25CF) + " "
      : zaGesteuert ? String.fromCharCode(0x25CB) + " " : String.fromCharCode(0x2014) + " ";

    // --- Metrics ---
    const socSensor = this._config?.battery_soc_sensor;
    const socVal = socSensor ? this._readFloat(socSensor) : null;
    const socText = socVal != null ? `${Math.round(socVal)}` : (socSensor ? "---" : "Nicht konfiguriert");

    // --- PV forecast: read from original Solcast/Forecast.Solar sensors ---
    const forecastTomorrowId = this._config?.forecast_tomorrow_entity || "";

    // Derive prefix from configured sensors
    // Solcast new: "sensor.solcast_pv_forecast_prognose_morgen" → prefix "sensor.solcast_pv_forecast_prognose_"
    // Solcast old: "sensor.solcast_pv_forecast_prognose_fuer_morgen" → prefix "sensor.solcast_pv_forecast_prognose_fuer_"
    // Forecast.Solar: "sensor.energy_production_tomorrow" → prefix "sensor.energy_production_"
    let solcastPrefix = "";
    let forecastSolarPrefix = "";
    if (forecastTomorrowId.includes("solcast")) {
      solcastPrefix = forecastTomorrowId.replace(/morgen$/, "");
      // If old "fuer_" prefix doesn't find tag sensors, try without "fuer_"
      const states = this._hass?.states || {};
      if (solcastPrefix.endsWith("fuer_") && !states[solcastPrefix + "tag_3"]) {
        solcastPrefix = solcastPrefix.replace(/fuer_$/, "");
      }
    } else if (forecastTomorrowId.includes("energy_production")) {
      forecastSolarPrefix = forecastTomorrowId.replace(/tomorrow$/, "");
    }

    // PV total today — prefer configured sensor, then auto-detect
    let pvHeute = null;
    if (this._config?.forecast_today_entity) {
      pvHeute = this._readFloat(this._config.forecast_today_entity);
    }
    if (pvHeute == null && solcastPrefix) {
      pvHeute = this._readFloat(solcastPrefix + "heute");
    } else if (pvHeute == null && forecastSolarPrefix) {
      pvHeute = this._readFloat(forecastSolarPrefix + "today");
    }
    if (pvHeute == null) {
      pvHeute = this._readFloat(this._entityIds?.pv_heute || "sensor.eeg_energy_optimizer_pv_prognose_heute");
    }

    // PV tomorrow
    let pvMorgen = null;
    if (solcastPrefix) {
      pvMorgen = this._readFloat(solcastPrefix + "morgen");
    } else if (forecastSolarPrefix) {
      pvMorgen = this._readFloat(forecastSolarPrefix + "tomorrow");
    }
    if (pvMorgen == null) {
      pvMorgen = this._readFloat(this._entityIds?.pv_morgen || "sensor.eeg_energy_optimizer_pv_prognose_morgen");
    }

    // 7-day PV forecast array — prefer configured sensors, then auto-detect from prefix
    const _pvDay = (dayNum) => {
      const cfgKey = `forecast_day${dayNum}_entity`;
      if (this._config?.[cfgKey]) return this._readFloat(this._config[cfgKey]) || 0;
      if (solcastPrefix) return this._readFloat(solcastPrefix + `tag_${dayNum}`) || 0;
      return 0;
    };
    const pvWeek = [
      pvHeute || 0,
      pvMorgen || 0,
      _pvDay(3), _pvDay(4), _pvDay(5), _pvDay(6), _pvDay(7),
    ];

    // --- 7-day forecast chart ---
    const forecastSensors = [
      this._entityIds?.prognose_heute || "sensor.eeg_energy_optimizer_tagesverbrauchsprognose_heute",
      this._entityIds?.prognose_morgen || "sensor.eeg_energy_optimizer_tagesverbrauchsprognose_morgen",
      this._entityIds?.prognose_tag2 || "sensor.eeg_energy_optimizer_tagesverbrauchsprognose_tag_2",
      this._entityIds?.prognose_tag3 || "sensor.eeg_energy_optimizer_tagesverbrauchsprognose_tag_3",
      this._entityIds?.prognose_tag4 || "sensor.eeg_energy_optimizer_tagesverbrauchsprognose_tag_4",
      this._entityIds?.prognose_tag5 || "sensor.eeg_energy_optimizer_tagesverbrauchsprognose_tag_5",
      this._entityIds?.prognose_tag6 || "sensor.eeg_energy_optimizer_tagesverbrauchsprognose_tag_6",
    ];
    const today = new Date();
    const forecastData = forecastSensors.map((eid, i) => {
      let val;
      if (i === 0) {
        // Today: use full-day total from attribute instead of remaining
        const s = this._readState(eid);
        val = s?.attributes?.tagesverbrauch_gesamt_kwh != null
          ? Number(s.attributes.tagesverbrauch_gesamt_kwh) : this._readFloat(eid);
      } else {
        val = this._readFloat(eid);
      }
      let label;
      if (i === 0) label = "Heute";
      else if (i === 1) label = "Morgen";
      else {
        const d = new Date(today);
        d.setDate(d.getDate() + i);
        label = this._getWeekdayShort(d);
      }
      return { label, value: val || 0 };
    });

    // --- PV forecast data for grouped bar chart (all 7 days if Solcast) ---
    const pvForecastData = forecastData.map((d, i) => {
      return { label: d.label, value: pvWeek[i] || 0 };
    });
    const _solcastDay37Missing = solcastPrefix && pvWeek.slice(2).every(v => v === 0);

    // --- Hourly profile chart (all weekdays) ---
    const profilState = this._readState(this._entityIds?.verbrauchsprofil || "sensor.eeg_energy_optimizer_verbrauchsprofil");
    const dayKey = this._getWeekdayKey(today);
    const weekdayKeys = ["mo", "di", "mi", "do", "fr", "sa", "so"];
    const weekdayLabels = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
    const weekdayDatasets = [];
    weekdayKeys.forEach((key, idx) => {
      const watts = profilState?.attributes?.[`${key}_watts`];
      if (watts && Array.isArray(watts) && watts.length === 24) {
        weekdayDatasets.push({
          data: watts.map(w => w / 1000),
          label: weekdayLabels[idx],
          key: key
        });
      }
    });
    const highlightIdx = weekdayDatasets.findIndex(ds => ds.key === dayKey);

    // --- Day/Night dataset for the alternative chart variant ---
    // sunrise_hour trägt nur noch den Fallback für nightEndDecimal.
    const sunriseHour = Number(profilState?.attributes?.sunrise_hour ?? 6);
    const dischargeStartHour = Number(profilState?.attributes?.discharge_start_hour
      ?? (this._config?.discharge_a_start_time
        ? parseInt(String(this._config.discharge_a_start_time).split(":")[0], 10)
        : 20));
    const nightEndDecimal = Number(profilState?.attributes?.night_end_decimal
      ?? (sunriseHour + 1));
    const daynightData = weekdayKeys.map((key, idx) => ({
      key,
      label: weekdayLabels[idx],
      tag: Number(profilState?.attributes?.[`${key}_tag_kwh`] ?? 0),
      nacht: Number(profilState?.attributes?.[`${key}_nacht_kwh`] ?? 0),
    }));

    const narrowClass = this._narrow ? " narrow" : "";

    // --- Live values for header card ---
    // Read power sensors and normalize to kW
    // Read all values from our own calculated sensors (normalized, multi-inverter aware)
    const pvKw = this._readFloat("sensor.eeg_energy_optimizer_pv_leistung") || 0;
    const batKw = this._readFloat("sensor.eeg_energy_optimizer_batterieleistung") || 0;
    let gridKw = this._readFloat("sensor.eeg_energy_optimizer_netzleistung") || 0;
    const hausKw = this._readFloat("sensor.eeg_energy_optimizer_hausverbrauch") || 0;
    const batLabel = batKw >= 0 ? "Ladung" : "Entladung";
    const batColor = "val-orange";
    const gridLabel = gridKw >= 0 ? "Einspeisung" : "Bezug";
    const gridColor = gridKw >= 0 ? "val-green" : "val-red";
    const socColor = socVal == null ? "" : socVal > 50 ? "val-green" : socVal >= 25 ? "val-orange" : "val-red";

    // Entity IDs for clickable live values — all our own calculated sensors
    const pvEntity = "sensor.eeg_energy_optimizer_pv_leistung";
    const batEntity = "sensor.eeg_energy_optimizer_batterieleistung";
    const gridEntity = "sensor.eeg_energy_optimizer_netzleistung";
    const socEntity = this._config?.battery_soc_sensor || "";
    const hausEntity = "sensor.eeg_energy_optimizer_hausverbrauch";

    return `
      <div class="dashboard-grid${narrowClass}">
        <!-- Header Card: Live Values Grid OR Energy Flow + Mode Toggle + Timestamps -->
        <div class="card header-card">
          <div class="header-card-top">
            <h3 class="status-card-title" style="margin:0;display:flex;align-items:center;gap:8px;flex:1;min-width:0">
              <ha-icon icon="mdi:pulse" style="--mdc-icon-size:20px;color:var(--primary-color,#03a9f4);flex-shrink:0"></ha-icon>
              <span style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">Status</span>
              ${this._config?.inverter_type === "solaredge_storedge" ? `<span class="info-popup-trigger" style="cursor:pointer;flex-shrink:0">
                <ha-icon icon="mdi:information-outline" style="--mdc-icon-size:18px;color:var(--secondary-text-color)"></ha-icon>
                <div class="info-popup">
                  <strong>Schreibvorg\u00e4nge im Wechselrichter</strong>
                  <p>${fmtDe(this._readFloat("sensor.eeg_energy_optimizer_register_schreibvorgange") ?? 0, 0)} Schreibvorg\u00e4nge in den NVRAM-Speicher seit der Installation. SolarEdge-Ger\u00e4te vertragen davon nur eine begrenzte Zahl \u2014 deshalb schreibt die Steuerung nur, wenn sich ein Wert wirklich \u00e4ndert.</p>
                </div>
              </span>` : ""}
            </h3>
            <div class="status-view-pills">
              <button class="view-pill ${this._statusViewVariant === "values" ? "active" : ""}" data-action="set-status-view" data-variant="values" title="Werte-Anzeige">
                <ha-icon icon="mdi:view-grid-outline" style="--mdc-icon-size:16px"></ha-icon>
              </button>
              <button class="view-pill ${this._statusViewVariant === "flow" ? "active" : ""}" data-action="set-status-view" data-variant="flow" title="Energieflu\u00dfdiagramm">
                <ha-icon icon="mdi:transit-connection-variant" style="--mdc-icon-size:16px"></ha-icon>
              </button>
            </div>
            <div class="header-mode-toggle">
              <div class="mode-toggle ${modeToggleClass}" data-action="toggle-mode">
                <div class="toggle-knob"></div>
              </div>
              <span class="mode-toggle-label">${modeValue === "Ein" ? "Ein" : "Aus"}</span>
            </div>
          </div>
          ${this._statusViewVariant === "flow"
            ? this._renderEnergyFlow(pvKw, batKw, gridKw, hausKw, socVal, {pvEntity, batEntity, gridEntity, hausEntity, socEntity})
            : `<div class="header-grid">
                <div class="hlv${pvEntity ? " hlv-clickable" : ""}" ${pvEntity ? `data-action="show-entity" data-entity="${pvEntity}"` : ""}><span class="hlv-label">PV</span><span class="hlv-val val-green">${fmtDe(pvKw, 2)} kW</span></div>
                <div class="hlv${batEntity ? " hlv-clickable" : ""}" ${batEntity ? `data-action="show-entity" data-entity="${batEntity}"` : ""}><span class="hlv-label">Batterie</span><span class="hlv-val ${batColor}">${fmtDe(Math.abs(batKw), 2)} kW <small>(${batLabel})</small></span></div>
                <div class="hlv${socEntity ? " hlv-clickable" : ""}" ${socEntity ? `data-action="show-entity" data-entity="${socEntity}"` : ""}><span class="hlv-label">SOC</span><span class="hlv-val ${socColor}">${socText}%</span></div>
                <div class="hlv${gridEntity ? " hlv-clickable" : ""}" ${gridEntity ? `data-action="show-entity" data-entity="${gridEntity}"` : ""}><span class="hlv-label">Netz</span><span class="hlv-val ${gridColor}">${fmtDe(Math.abs(gridKw), 2)} kW <small>(${gridLabel})</small></span></div>
                <div class="hlv hlv-clickable" data-action="show-entity" data-entity="${hausEntity}"><span class="hlv-label">Haus</span><span class="hlv-val val-blue">${fmtDe(hausKw, 2)} kW</span></div>
              </div>`}
          <div style="margin-top:12px">
            <span class="status-indicator ${zustandBadgeClass}" style="display:inline-block">${zustandSymbol}${zustand}</span>
            ${this._renderSteuerungZeilen(decisionState)}
          </div>
          ${this._istStartphase(decisionState) ? "" : this._renderJobStatusLine(decisionState, profilState)}
        </div>


        <!-- Optimierungsplan (der einzige Aktor) -->
        <div class="card">
          <div data-action="toggle-schedule" style="display:flex;justify-content:space-between;align-items:center;cursor:pointer;user-select:none">
            <h3 style="margin:0">
              <ha-icon icon="mdi:chart-timeline-variant" style="--mdc-icon-size:20px;color:var(--primary-color,#03a9f4);vertical-align:middle"></ha-icon>
              Optimierungsplan
            </h3>
            <ha-icon icon="mdi:chevron-${this._scheduleOpen ? "up" : "down"}" style="--mdc-icon-size:24px;color:var(--secondary-text-color)"></ha-icon>
          </div>
          ${this._scheduleOpen ? this._renderSchedule() : ""}
        </div>

        <!-- Was die PV gebracht hat: Rückblick auf Gemessenes -->
        ${this._renderBilanzKarte()}

        <!-- Optimierungsgewinn: was die Optimierung gegenüber Standardbetrieb bringt -->
        ${this._renderGewinnKarte()}

        ${this._config?.expert_mode ? this._renderControlStateKarte(decisionState) : ""}


        <!-- Charts (or loading hint if no consumption data yet) -->
        ${(profilState?.attributes?.stats_count || 0) === 0 ? `
        <div class="card" style="text-align:center;padding:32px">
          <ha-icon icon="mdi:chart-line" style="--mdc-icon-size:48px;color:var(--secondary-text-color);opacity:0.5"></ha-icon>
          <h3 style="margin:16px 0 8px;color:var(--secondary-text-color)">Verbrauchsdaten werden berechnet...</h3>
          <p style="color:var(--secondary-text-color);font-size:14px;margin:0">
            Die historischen Verbrauchsdaten werden aus deinen Sensoren berechnet. Das dauert beim ersten Start üblicherweise unter zwei Minuten. Ohne vorhandene Sensor-Historie füllt sich das Profil erst nach und nach — die Anzeige aktualisiert sich automatisch.
          </p>
        </div>
        ` : `
        <!-- 7-Day Forecast Chart -->
        <div class="card chart-card">
          <div data-action="toggle-forecast" style="display:flex;justify-content:space-between;align-items:center;gap:8px;cursor:pointer;user-select:none">
          <h3 class="status-card-title" style="margin:0;flex:1;min-width:0">
            <ha-icon icon="mdi:chart-bar" style="--mdc-icon-size:20px;color:var(--primary-color,#03a9f4);flex-shrink:0"></ha-icon>
            <span>Energieprognose (7 Tage)</span>
            <span class="info-popup-trigger">
              <ha-icon icon="mdi:information-outline" style="--mdc-icon-size:18px;color:var(--secondary-text-color);cursor:pointer"></ha-icon>
              <div class="info-popup">
                <strong>Energieprognose</strong>
                <p>Das Diagramm zeigt f\u00fcr die n\u00e4chsten 7 Tage den durchschnittlichen Energieverbrauch sowie den von der Prognosesoftware gesch\u00e4tzten PV-Ertrag des jeweiligen Tages. Gemittelt wird über zwei Gruppen — Werktage (Mo–Fr) und Wochenende samt Feiertagen — über die letzten Wochen (konfigurierbar, Standard: 4 Wochen). Der höchste Wert je Stunde wird verworfen, damit einmalige Verbräuche wie eine E-Auto-Ladung die Prognose nicht prägen.</p>
              </div>
            </span>
          </h3>
            <ha-icon icon="mdi:chevron-${this._forecastOpen ? "up" : "down"}" style="--mdc-icon-size:24px;color:var(--secondary-text-color);flex-shrink:0"></ha-icon>
          </div>
          ${!this._forecastOpen ? "" : `
          ${this._renderBarChart(forecastData, pvForecastData)}
          ${_solcastDay37Missing ? `<p style="margin:8px 0 0;padding:10px 12px;background:var(--warning-color,#ff9800)22;border-left:3px solid var(--warning-color,#ff9800);border-radius:4px;font-size:0.85em;color:var(--primary-text-color)">
            <ha-icon icon="mdi:alert-outline" style="--mdc-icon-size:16px;vertical-align:middle;margin-right:4px;color:var(--warning-color,#ff9800)"></ha-icon>
            Bitte die Sensoren f\u00fcr die Tage 3\u20137 in der Solcast Integration aktivieren, um die fehlenden Prognosedaten anzeigen zu lassen.</p>` : ""}`}
        </div>

        <!-- Einspeise-Statistik (aufklappbar) -->
        <div class="card">
          <div data-action="toggle-feedin-stats" style="display:flex;justify-content:space-between;align-items:center;cursor:pointer;user-select:none">
            <h3 style="margin:0">
              <ha-icon icon="mdi:chart-timeline-variant-shimmer" style="--mdc-icon-size:20px;color:var(--primary-color,#03a9f4);vertical-align:middle"></ha-icon>
              Einspeise-Statistik
            </h3>
            <ha-icon icon="mdi:chevron-${this._feedinStatsOpen ? "up" : "down"}" style="--mdc-icon-size:24px;color:var(--secondary-text-color)"></ha-icon>
          </div>
          ${this._feedinStatsOpen ? this._renderFeedinStatistics() : ""}
        </div>

        <!-- Hourly Profile Chart (collapsible) -->
        <div class="card">
          <div data-action="toggle-profil" style="display:flex;justify-content:space-between;align-items:center;cursor:pointer;user-select:none">
            <h3 style="margin:0">
              <ha-icon icon="mdi:chart-line" style="--mdc-icon-size:20px;color:var(--primary-color,#03a9f4);vertical-align:middle"></ha-icon>
              Verbrauchsprofil (Werktag / Wochenende)
            </h3>
            <ha-icon icon="mdi:chevron-${this._profilOpen ? "up" : "down"}" style="--mdc-icon-size:24px;color:var(--secondary-text-color)"></ha-icon>
          </div>
          ${this._profilOpen && this._config?.expert_mode ? this._renderConsumptionProfileStatus(profilState) : ""}
          ${this._profilOpen ? (() => {
            const variant = this._profilChartVariant || "hourly";
            // Angetippter Wochentag gewinnt, sonst der heutige.
            const hervorgehoben = (this._profilHighlight != null && this._profilHighlight < weekdayDatasets.length)
              ? this._profilHighlight : (highlightIdx >= 0 ? highlightIdx : 0);
            const pillStyle = (active) => `padding:10px 16px;min-height:44px;border:1px solid var(--divider-color);background:${active ? "var(--primary-color)" : "var(--card-background-color,#fff)"};color:${active ? "#fff" : "var(--primary-text-color)"};border-radius:22px;font-size:13px;cursor:pointer`;
            const toggleBar = `
              <div style="display:flex;gap:8px;margin:12px 0;flex-wrap:wrap">
                <button data-action="set-profil-variant" data-variant="hourly" style="${pillStyle(variant === "hourly")}">Stundenverlauf</button>
                <button data-action="set-profil-variant" data-variant="daynight" style="${pillStyle(variant === "daynight")}">Tag / Nacht</button>
              </div>`;
            const chart = variant === "daynight"
              ? this._renderDayNightChart(daynightData, dischargeStartHour, nightEndDecimal)
              : this._renderLineChart(weekdayDatasets, hervorgehoben);
            return toggleBar + chart;
          })() : ""}
        </div>

        ${this._config?.enable_peakshare !== false ? (() => {
          // Beide konfigurierten Gemeinschaften in der Ueberschrift, in der
          // Reihenfolge der Konfiguration. Das Praefix folgt dem Namen: eine
          // Gemeinschaft namens "BEG" bleibt BEG, jede andere ist eine EEG.
          const psNamen = [this._config?.peakshare_community,
                           this._config?.peakshare_community_2]
            .filter(Boolean)
            .map(n => (String(n).toUpperCase().startsWith("BEG") ? String(n) : `EEG ${n}`));
          const psDisplay = psNamen.length ? psNamen.join(" / ") : "BEG";
          return `
        <!-- PeakShare Energiebedarf (collapsible) -->
        <div class="card">
          <div data-action="toggle-peakshare-data" style="display:flex;justify-content:space-between;align-items:center;cursor:pointer;user-select:none">
            <h3 style="margin:0">
              <ha-icon icon="mdi:transmission-tower" style="--mdc-icon-size:20px;color:var(--primary-color,#03a9f4);vertical-align:middle"></ha-icon>
              Energiebedarf ${psDisplay}
            </h3>
            <ha-icon icon="mdi:chevron-${this._peakshareDataOpen ? "up" : "down"}" style="--mdc-icon-size:24px;color:var(--secondary-text-color)"></ha-icon>
          </div>
          ${this._peakshareDataOpen ? this._renderPeakShareDashboard() : ""}
        </div>`;
        })() : ""}
        `}

        <!-- Activity Timeline (collapsible) -->
        <div class="card">
          <div data-action="toggle-activity-log" style="display:flex;justify-content:space-between;align-items:center;cursor:pointer;user-select:none">
            <h3 style="margin:0">
              <ha-icon icon="mdi:history" style="--mdc-icon-size:20px;color:var(--primary-color,#03a9f4);vertical-align:middle"></ha-icon>
              Aktivit\u00e4tsprotokoll
            </h3>
            <ha-icon icon="mdi:chevron-${this._activityLogOpen ? "up" : "down"}" style="--mdc-icon-size:24px;color:var(--secondary-text-color)"></ha-icon>
          </div>
          ${this._activityLogOpen ? `
          <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;justify-content:flex-end;margin-top:12px">
            <select class="log-filter" data-field="activity_filter">
              <option value="" ${this._activityFilter === "" ? "selected" : ""}>Alle Zust\u00e4nde</option>
              <option value="laden" ${this._activityFilter === "laden" ? "selected" : ""}>Laden begrenzt / blockiert</option>
              <option value="entladung" ${this._activityFilter === "entladung" ? "selected" : ""}>Entladung</option>
              <option value="normal" ${this._activityFilter === "normal" ? "selected" : ""}>Normalbetrieb</option>
            </select>
            <label class="log-checkbox" data-action="toggle-activity-show-all">
              <input type="checkbox" ${this._activityShowAll ? "checked" : ""} style="pointer-events:none;margin:0;width:18px;height:18px"> Alle Eintr\u00e4ge
            </label>
            <button class="btn-link btn-tap" data-action="refresh-activity-log">
              <ha-icon icon="mdi:refresh" style="--mdc-icon-size:16px;vertical-align:middle"></ha-icon> Aktualisieren
            </button>
          </div>
          ${this._renderActivityTimeline()}
          ` : ""}
        </div>

        <div style="text-align:center;margin-top:32px;padding:16px 0 8px;font-size:11px;color:var(--secondary-text-color,#999);line-height:1.6">
          <img src="/eeg_optimizer_panel/logo.png" alt="EEG Energy Optimizer" style="max-height:36px;width:auto;display:block;margin:0 auto 8px;filter:brightness(1) saturate(1.2) hue-rotate(-10deg)">
          <div style="opacity:0.35">EEG Energy Optimizer${this._config?.version ? " v" + this._config.version : ""}</div>
          <div style="max-width:480px;margin:4px auto 0;font-size:10px;opacity:0.35">Diese Software steuert Batteriespeicher automatisch. Nutzung auf eigene Verantwortung \u2014 keine Haftung f\u00fcr Sch\u00e4den an Ger\u00e4ten, Ertragsausf\u00e4lle oder fehlerhafte Steuerung.</div>
        </div>

      </div>`;
  }

  // Werteanzeige des Fahrplans abraeumen (Tooltip, Fadenkreuz, SOC-Punkt).
  _versteckeSchedTooltip() {
    const tt = this._shadow.querySelector(".sched-tooltip");
    if (tt) tt.style.display = "none";
    this._shadow.querySelectorAll(".sched-cursor").forEach(el => { el.style.visibility = "hidden"; });
    this._shadow.querySelectorAll(".sched-cursor-soc").forEach(el => { el.style.visibility = "hidden"; });
  }

  _versteckePsTooltip() {
    this._shadow.querySelectorAll(".ps-tooltip").forEach(tt => { tt.style.display = "none"; });
  }

  // Werte eines Punktes der Bedarfskurve anzeigen (Maus wie Finger).
  _zeigePsTooltip(dot) {
    const wrapper = dot.closest(".ps-chart-card");
    const tt = wrapper?.querySelector(".ps-tooltip");
    if (!tt) return;
    const dotRect = dot.getBoundingClientRect();
    const wrapRect = wrapper.getBoundingClientRect();
    const dayLine = dot.dataset.day ? `<div style="color:var(--secondary-text-color);font-size:11px;margin-bottom:2px">${dot.dataset.day}</div>` : "";
    // Alle Gemeinschaften dieser Stunde, nicht nur die getroffene: bei zwei
    // Serien liegen die Punkte oft uebereinander.
    let serien = [];
    try { serien = JSON.parse(dot.dataset.eegj || "[]"); } catch (e) { serien = []; }
    const chip = (farbe) => `<span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${farbe};margin-right:5px"></span>`;
    const zeilen = serien.map(e =>
      `<div style="display:flex;gap:8px;justify-content:space-between">
         <span style="color:var(--secondary-text-color)">${chip(e.f)}${e.u ? "Ueberschuss" : "Bedarf"} ${this._escapeHtml(e.n)}</span>
         <strong>${e.v} kWh</strong>
       </div>`).join("");
    tt.innerHTML = `${dayLine}<div style="font-weight:600;margin-bottom:2px">${dot.dataset.hour}</div>${zeilen}`;
    tt.style.display = "block";
    tt.style.left = `${dotRect.left - wrapRect.left + dotRect.width / 2}px`;
    tt.style.top = `${dotRect.top - wrapRect.top - 8}px`;
  }

  /* ── Diagrammbreiten ──────────────────────────── */

  // Verfuegbare Zeichenbreite eines Diagramms in CSS-Pixeln. Die Diagramme
  // setzen sie als viewBox-Breite ein, damit eine SVG-Einheit genau einem
  // Pixel entspricht — nur so bleibt `font-size="11"` auch am Handy 11 px.
  // Vorher stand die Breite fest (700 bzw. 840 Einheiten) und wurde von der
  // viewBox auf die Kartenbreite gestaucht: auf dem iPhone landeten alle
  // Achsen- und Werteschriften bei 4–8 px.
  _cw(key) {
    const gemessen = this._chartW[key];
    if (gemessen > 120) return gemessen;
    // Erster Render: Panelbreite minus content- und Kartenpolster schaetzen
    // (siehe .content / .card im Style-Block). _measureCharts() korrigiert.
    const vw = Math.min(900, (typeof window !== "undefined" && window.innerWidth) || 900);
    const schmal = vw <= 600;
    return Math.max(260, vw - 2 * (schmal ? 8 : 16) - 2 * (schmal ? 16 : 24));
  }

  // Nach dem Rendern die tatsaechliche Breite jedes Diagramms messen und bei
  // Abweichung EINEN weiteren Render anstossen. Danach stimmen viewBox und
  // Anzeigebreite ueberein, die Messung ergibt denselben Wert und es bleibt
  // stabil. Die 3-px-Toleranz verhindert Zappeln durch Rundung.
  _measureCharts() {
    let geaendert = false;
    this._shadow.querySelectorAll("svg[data-cw]").forEach(svg => {
      const key = svg.getAttribute("data-cw");
      const w = Math.round(svg.getBoundingClientRect().width);
      if (w > 120 && Math.abs((this._chartW[key] || 0) - w) > 3) {
        this._chartW[key] = w;
        geaendert = true;
      }
    });
    if (geaendert && !this._chartRemeasure) {
      this._chartRemeasure = true;
      requestAnimationFrame(() => { this._chartRemeasure = false; this._render(); });
    }
  }

  // Geraet drehen, Sidebar ein-/ausklappen, Fenster ziehen: die gemessenen
  // Breiten sind dann veraltet. `narrow` bleibt unberuehrt — das setzt HA.
  _startResizeObserver() {
    if (this._resizeObserver || typeof ResizeObserver === "undefined") return;
    let letzte = 0;
    this._resizeObserver = new ResizeObserver(entries => {
      const w = Math.round(entries[0]?.contentRect?.width || 0);
      if (!w || Math.abs(w - letzte) < 4) return;
      letzte = w;
      if (this._resizeTimer) clearTimeout(this._resizeTimer);
      this._resizeTimer = setTimeout(() => {
        this._chartW = {};
        this._render();
      }, 150);
    });
    this._resizeObserver.observe(this);
  }

  /* ── Main render ──────────────────────────────── */

  _render() {
    try {
      this._renderInner();
      this._measureCharts();
      // Verify render succeeded
    } catch (outerErr) {
      console.error("EEG Energy Optimizer fatal render error:", outerErr);
      try {
        this._shadow.innerHTML = `
          <div style="padding:24px;font-family:sans-serif">
            <h3 style="color:#db4437;margin-top:0">Dashboard-Fehler</h3>
            <p>Ein unerwarteter Fehler ist aufgetreten.</p>
            <pre style="font-size:12px;overflow:auto;background:#f5f5f5;padding:12px;border-radius:4px">${outerErr.message}\n${outerErr.stack}</pre>
            <button onclick="location.reload()" style="margin-top:12px;padding:8px 16px;cursor:pointer">Seite neu laden</button>
          </div>`;
      } catch (_) { /* truly fatal */ }
    }
  }

  _renderInner() {
    if (!this._initialized) {
      // Show loading indicator instead of blank white screen
      this._shadow.innerHTML = `
        <style>
          :host { display:block; height:100%; background:var(--primary-background-color,#fafafa); }
          .loading-screen { display:flex; flex-direction:column; align-items:center; justify-content:center; height:100%; gap:16px; color:var(--secondary-text-color,#666); }
          .loading-spinner { width:40px; height:40px; border:3px solid var(--divider-color,#e0e0e0); border-top-color:var(--primary-color,#03a9f4); border-radius:50%; animation:spin 1s linear infinite; }
          @keyframes spin { to { transform:rotate(360deg); } }
        </style>
        <div class="loading-screen">
          <div class="loading-spinner"></div>
          <div>Verbindung wird hergestellt\u2026</div>
        </div>`;
      return;
    }

    let headerRight = "";
    if (this._setupComplete && this._view === "dashboard") {
      headerRight = `
        <button data-action="open-settings" title="Einstellungen">
          <ha-icon icon="mdi:cog"></ha-icon>
        </button>`;
    } else if (this._view === "settings") {
      headerRight = `
        <button data-action="back-to-dashboard" title="Zur\u00fcck">
          <ha-icon icon="mdi:arrow-left"></ha-icon>
        </button>`;
    } else if (this._view === "wizard") {
      headerRight = `
        <button data-action="back-to-dashboard" title="Zur\u00fcck">
          <ha-icon icon="mdi:arrow-left"></ha-icon>
        </button>`;
    }

    let content = "";
    try {
      if (this._view === "wizard") {
        content = `
          <div class="content">
            ${this._renderWizard()}
          </div>`;
      } else if (this._view === "settings") {
        content = `
          <div class="content">
            ${this._renderSettings()}
          </div>`;
      } else if (!this._setupComplete) {
        content = `
          <div class="content">
            <div class="card setup-card">
              <img src="/eeg_optimizer_panel/logo.png" alt="EEG Energy Optimizer" class="setup-logo">
              <h2>Die Einrichtung wurde noch nicht abgeschlossen</h2>
              <p>Richte den EEG Energy Optimizer ein, um die Batteriesteuerung für deine Energiegemeinschaft zu optimieren.</p>
              <button class="btn-primary" data-action="start-wizard">Einrichtung starten</button>
            </div>
          </div>`;
      } else {
        content = `
          <div class="content">
            <div id="dashboard-root">
              ${this._renderDashboard()}
            </div>
          </div>
`;
      }
    } catch (err) {
      console.error("EEG Energy Optimizer render error:", err);
      content = `
        <div class="content">
          <div class="card" style="border-left:4px solid var(--error-color, #db4437); margin:16px">
            <h3 style="color:var(--error-color, #db4437); margin-top:0">Render-Fehler</h3>
            <p style="color:var(--secondary-text-color)">Das Dashboard konnte nicht gerendert werden. Details:</p>
            <pre style="font-size:12px; overflow:auto; background:var(--secondary-background-color, #f5f5f5); padding:12px; border-radius:4px">${err.message}\n${err.stack}</pre>
          </div>
        </div>`;
    }

    // Der Guide-Dialog gehoert in jede Ansicht: er haengt nur an _showDialog,
    // und ein „Anleitung"-Knopf kann ueberall stehen.
    content += this._renderDialog();

    this._shadow.innerHTML = `
      <style>
        :host {
          display: block;
          height: 100%;
          background: var(--primary-background-color, #fafafa);
          color: var(--primary-text-color, #212121);
          font-family: var(--paper-font-body1_-_font-family, "Roboto", sans-serif);
        }
        .toolbar {
          display: flex;
          align-items: center;
          justify-content: space-between;
          padding: 0 16px;
          height: 56px;
          background: var(--app-header-background-color, var(--primary-color));
          color: var(--app-header-text-color, var(--text-primary-color));
        }
        .toolbar h1 { font-size: 20px; font-weight: 400; margin: 0; flex: 1; }
        .toolbar .menu-btn { margin-right: 8px; }
        .toolbar button {
          background: none; border: none; color: inherit;
          cursor: pointer; padding: 8px; border-radius: 50%;
        }
        .toolbar button:hover { background: rgba(255, 255, 255, 0.1); }
        .toolbar ha-icon { --mdc-icon-size: 24px; }
        .content { padding: 16px; max-width: 900px; margin: 0 auto; }
        .card {
          background: var(--card-background-color, #fff);
          border-radius: var(--ha-card-border-radius, 12px);
          box-shadow: var(--ha-card-box-shadow, 0 2px 6px rgba(0,0,0,0.1));
          /* Vertikal bewusst schlanker als seitlich: zugeklappte Karten
             bestehen nur aus der Titelzeile, und 24 px ober- und unterhalb
             ließen sie aufgebläht wirken (Nutzer-Feedback 27.08.). */
          padding: 14px 24px;
        }
        .setup-card { text-align: center; padding: 48px 24px; }
        .setup-card .setup-logo {
          max-width: 200px; height: auto; margin-bottom: 24px;
        }
        .setup-card h2 {
          color: var(--primary-text-color); margin-bottom: 16px;
          font-size: 24px; font-weight: 400;
        }
        .setup-card p {
          color: var(--secondary-text-color); margin-bottom: 24px; line-height: 1.5;
        }
        .btn-primary {
          background: var(--primary-color); color: var(--text-primary-color);
          border: none; border-radius: 4px; padding: 12px 32px;
          cursor: pointer; font-size: 16px; font-weight: 500; transition: opacity 0.2s;
        }
        .btn-primary:hover { opacity: 0.9; }
        /* Wizard styles */
        .wizard-nav { display: flex; justify-content: space-between; margin-top: 24px; }
        .step-indicator { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; color: var(--secondary-text-color); font-size: 14px; }
        .expert-toggle { display: flex; align-items: center; gap: 4px; font-size: 12px; cursor: pointer; opacity: 0.7; transition: opacity 0.2s; white-space: nowrap; }
        .expert-toggle:hover { opacity: 1; }
        .expert-toggle input { margin: 0; cursor: pointer; }
        .progress-bar { height: 4px; background: var(--divider-color); border-radius: 2px; margin-bottom: 24px; }
        .progress-bar-fill { height: 100%; background: var(--primary-color); border-radius: 2px; transition: width 0.3s; }
        .field-group { margin-bottom: 16px; }
        .field-group label { display: block; font-weight: 500; margin-bottom: 4px; color: var(--primary-text-color); }
        .field-group .help-text { font-size: 12px; color: var(--secondary-text-color); margin-top: 4px; }
        .field-group input:not([type="checkbox"]), .field-group select {
          width: 100%; padding: 8px 12px; border: 1px solid var(--divider-color);
          border-radius: 4px; background: var(--card-background-color); color: var(--primary-text-color);
          font-size: 14px; box-sizing: border-box;
        }
        .field-group ha-entity-picker { display: block; width: 100%; }
        .dialog-overlay {
          position: fixed; top: 0; left: 0; right: 0; bottom: 0;
          background: rgba(0,0,0,0.5); z-index: 999;
          display: flex; align-items: center; justify-content: center;
        }
        .dialog-card {
          background: var(--card-background-color); border-radius: 12px;
          padding: 24px; max-width: 700px; width: 92%; max-height: 85vh; overflow-y: auto;
        }
        /* Guide-Inhalte (generiert aus docs/guides/*.md) */
        .guide-content h2.guide-title { margin-top: 0; }
        .guide-content h3 { margin: 16px 0 8px; }
        .guide-content h4 { margin: 12px 0 4px; }
        .guide-content ol, .guide-content ul { padding-left: 20px; line-height: 1.8; margin: 8px 0; }
        .guide-content li > p { margin: 4px 0; }
        .guide-content table { width: 100%; border-collapse: collapse; font-size: 13px; margin: 8px 0 16px; }
        .guide-content th, .guide-content td { padding: 4px 8px; border-bottom: 1px solid var(--divider-color); text-align: left; }
        .guide-content img { max-width: 100%; border-radius: 8px; margin: 8px 0 12px; border: 1px solid var(--divider-color); }
        .guide-content em { color: var(--secondary-text-color); font-style: normal; }
        .guide-content code { font-size: 13px; }
        .guide-alert { padding: 8px 12px; border-radius: 8px; margin: 12px 0; font-size: 13px; color: #fff; }
        .guide-alert p { margin: 0; }
        .guide-alert.warning { background: var(--warning-color, #ff9800); }
        .guide-alert.note { background: var(--info-color, #2196f3); }
        .guide-alert.caution { background: var(--error-color, #db4437); }
        .guide-alert em, .guide-alert code { color: inherit; }
        .summary-section { margin-bottom: 16px; }
        .summary-section h3 { font-size: 16px; color: var(--primary-color); margin-bottom: 8px; }
        .summary-row {
          display: flex; justify-content: space-between; padding: 4px 0; font-size: 14px;
        }
        .summary-row .label { color: var(--secondary-text-color); }
        .summary-row .value { color: var(--primary-text-color); font-weight: 500; max-width: 60%; text-align: right; word-break: break-all; }
        .btn-secondary {
          background: transparent; border: 1px solid var(--primary-color);
          color: var(--primary-color); border-radius: 4px; padding: 12px 32px;
          cursor: pointer; font-size: 16px; font-weight: 500;
        }
        .btn-secondary:hover { background: var(--primary-color); color: var(--text-primary-color); }
        .btn-disabled { opacity: 0.5; cursor: not-allowed; pointer-events: none; }
        .status-badge {
          display: inline-block; padding: 4px 12px; border-radius: 12px;
          font-size: 12px; font-weight: 500; margin-right: 8px;
        }
        .status-badge.installed { background: var(--success-color, #4caf50); color: white; }
        .status-badge.missing { background: var(--error-color, #f44336); color: white; }
        .loading { text-align: center; padding: 24px; color: var(--secondary-text-color); }
        .feature-toggle { margin-bottom: 4px; }
        .feature-card {
          border: 2px solid var(--divider-color); border-radius: 8px;
          padding: 16px; cursor: pointer; transition: border-color 0.2s, background 0.2s;
        }
        .feature-card:hover { border-color: var(--primary-color); }
        .feature-card.selected {
          border-color: var(--primary-color);
          background: var(--primary-color-light, rgba(3,169,244,0.08));
        }
        .feature-card-header {
          display: flex; align-items: flex-start; gap: 12px;
        }
        .feature-card-header ha-icon { --mdc-icon-size: 28px; color: var(--secondary-text-color); flex-shrink: 0; margin-top: 2px; }
        .feature-card.selected ha-icon { color: var(--primary-color); }
        .feature-card-text { flex: 1; }
        .feature-title { display: block; font-weight: 500; font-size: 14px; margin-bottom: 4px; }
        .feature-desc { display: block; font-size: 12px; color: var(--secondary-text-color); line-height: 1.4; }
        .feature-badge {
          flex-shrink: 0; padding: 4px 10px; border-radius: 12px; font-size: 12px; font-weight: 500;
        }
        .feature-badge.on { background: var(--success-color, #4caf50); color: white; }
        .feature-badge.off { background: var(--disabled-color, #bdbdbd); color: white; }
        .feature-params { padding: 12px 0 0 40px; }
        .cap-mode-cards { display: flex; gap: 12px; margin: 8px 0; }
        .cap-mode-card {
          flex: 1; display: flex; flex-direction: column; align-items: center; gap: 8px;
          padding: 16px 12px; border: 2px solid var(--divider-color); border-radius: 8px;
          cursor: pointer; transition: border-color 0.2s, background 0.2s;
          background: var(--card-background-color);
        }
        .cap-mode-card:hover { border-color: var(--primary-color); }
        .cap-mode-card.selected {
          border-color: var(--primary-color);
          background: var(--primary-color-light, rgba(3,169,244,0.08));
        }
        .cap-mode-card ha-icon { --mdc-icon-size: 28px; color: var(--secondary-text-color); }
        .cap-mode-card.selected ha-icon { color: var(--primary-color); }
        .cap-mode-card span { font-size: 13px; font-weight: 500; text-align: center; }
        .btn-link {
          background: none; border: none; color: var(--primary-color); cursor: pointer;
          font-size: 12px; text-decoration: underline; padding: 0;
        }
        .btn-link:hover { opacity: 0.8; }
        .inverter-test-result {
          display: flex; align-items: center; gap: 8px; padding: 12px;
          border-radius: 8px; font-size: 14px; font-weight: 500;
        }
        .inverter-test-result.success {
          background: rgba(76, 175, 80, 0.1); color: var(--success-color, #4caf50);
        }
        .inverter-test-result.error {
          background: rgba(244, 67, 54, 0.1); color: var(--error-color, #f44336);
        }
        .inverter-test-result ha-icon { --mdc-icon-size: 20px; }
        .ep-value-preview {
          font-size: 12px; color: var(--success-color, #4caf50); margin-top: 4px;
          display: flex; align-items: center; gap: 4px;
        }
        .ep-value-preview.unavailable { color: var(--error-color, #f44336); }
        .ep-container { position: relative; }
        .ep-chevron {
          position: absolute; right: 10px; top: 50%; transform: translateY(-50%);
          color: var(--secondary-text-color); pointer-events: none;
        }
        .ep-container input.entity-input { padding-right: 32px; }
        .ep-dropdown {
          display: none; position: absolute; top: 100%; left: 0; right: 0; z-index: 10;
          max-height: 200px; overflow-y: auto;
          background: var(--card-background-color); border: 1px solid var(--divider-color);
          border-top: none; border-radius: 0 0 4px 4px;
          box-shadow: 0 4px 8px rgba(0,0,0,0.15);
        }
        .ep-option {
          padding: 8px 12px; cursor: pointer; display: flex; flex-direction: column;
        }
        .ep-option:hover { background: var(--primary-color-light, rgba(3,169,244,0.08)); }
        .ep-name { font-size: 14px; color: var(--primary-text-color); }
        .ep-id { font-size: 11px; color: var(--secondary-text-color); }
        .prereq-cards .card { box-shadow: none; border: 2px solid var(--divider-color); transition: border-color 0.2s; }
        .forecast-option.selected { border-color: var(--primary-color); background: var(--primary-color-light, rgba(3,169,244,0.08)); }
        /* Dashboard styles */
        .dashboard-grid { display: grid; gap: 16px; }
        /* Grid-Items dürfen unter ihre min-content-Breite schrumpfen. Ohne
           das weitet EIN breites Kind (z. B. die Steuerwerte-Tabelle mit
           langen Entity-IDs) die Grid-Spalte über den Viewport hinaus, und
           am Handy scrollt der ganze Panel-Inhalt quer — breite Tabellen
           sollen stattdessen in ihrem eigenen overflow-x-Wrapper scrollen. */
        .dashboard-grid > * { min-width: 0; }
        .mode-toggle-label { font-size: 14px; font-weight: 500; color: var(--primary-text-color); }
        .mode-toggle { position: relative; width: 56px; height: 28px; border-radius: 14px; cursor: pointer; transition: background 0.2s; }
        .mode-toggle.ein { background: var(--success-color, #4caf50); }
        .mode-toggle.test { background: var(--warning-color, #ff9800); }
        .mode-toggle .toggle-knob { position: absolute; top: 3px; width: 22px; height: 22px; border-radius: 50%; background: white; transition: left 0.2s; box-shadow: 0 1px 3px rgba(0,0,0,0.3); }
        .mode-toggle.ein .toggle-knob { left: 31px; }
        .mode-toggle.test .toggle-knob { left: 3px; }
        .badge { display: inline-block; padding: 4px 12px; border-radius: 12px; font-size: 12px; font-weight: 500; color: white; }
        .badge.green { background: var(--success-color, #4caf50); }
        .badge.blue { background: var(--info-color, #2196f3); }
        .badge.orange { background: #ff5722; }
        .badge.gray { background: var(--disabled-color, #9e9e9e); }
        .chart-card { padding: 16px; }
        .chart-card h3 { font-size: 16px; margin: 0 0 12px; color: var(--primary-text-color); }
        .activity-timeline { max-height: 400px; overflow-y: auto; }
        .activity-entry { display: flex; align-items: flex-start; gap: 10px; padding: 8px 0; border-bottom: 1px solid var(--divider-color); }
        .activity-entry:last-child { border-bottom: none; }
        .activity-time { font-size: 13px; font-weight: 500; color: var(--secondary-text-color); min-width: 40px; padding-top: 2px; }
        .activity-dot { width: 24px; height: 24px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 12px; flex-shrink: 0; }
        .activity-content { flex: 1; min-width: 0; }
        .activity-header { font-size: 14px; color: var(--primary-text-color); }
        .activity-details { font-size: 12px; color: var(--secondary-text-color); margin-top: 2px; }
        .activity-badge { font-size: 10px; color: white; padding: 1px 6px; border-radius: 8px; margin-left: 6px; vertical-align: middle; }
        .status-indicator { font-weight: 600; margin-bottom: 8px; font-size: 15px; }
        .status-indicator.green { color: var(--success-color, #4caf50); }
        .status-indicator.blue { color: var(--info-color, #2196f3); }
        .status-indicator.orange { color: #ef6c00; }
        .status-indicator.gray { color: var(--secondary-text-color, #999); }
        .header-card { padding: 16px; }
        .header-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
        .hlv { display: flex; flex-direction: column; gap: 2px; }
        .bilanz-zeile-klickbar { cursor: pointer; transition: background 0.15s; }
        .bilanz-zeile-klickbar:hover { background: var(--secondary-background-color, rgba(0,0,0,0.05)); }
        .bilanz-zeile-klickbar:active { background: var(--secondary-background-color, rgba(0,0,0,0.08)); }
        .hlv-clickable { cursor: pointer; border-radius: 8px; padding: 4px 6px; margin: -4px -6px; transition: background 0.15s; }
        .hlv-clickable:hover { background: var(--secondary-background-color, rgba(0,0,0,0.05)); }
        .hlv-label { font-size: 11px; color: var(--secondary-text-color, #999); text-transform: uppercase; letter-spacing: 0.5px; }
        .hlv-val { font-size: 15px; font-weight: 600; }
        .hlv-val small { font-weight: 400; font-size: 12px; opacity: 0.7; }
        .val-green { color: #4caf50; }
        .val-orange { color: #ff9800; }
        .val-red { color: #f44336; }
        .val-blue { color: #2196f3; }
        .header-card-top { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
        .header-mode-toggle { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
        .status-view-pills { display: inline-flex; background: var(--secondary-background-color, rgba(0,0,0,0.05)); border-radius: 999px; padding: 3px; gap: 0; flex-shrink: 0; }
        .view-pill { background: transparent; border: none; cursor: pointer; padding: 6px 12px; border-radius: 999px; color: var(--secondary-text-color, #666); display: inline-flex; align-items: center; justify-content: center; transition: background 0.15s, color 0.15s; }
        .view-pill:hover { color: var(--primary-text-color); }
        .view-pill.active { background: var(--card-background-color, #fff); color: var(--primary-color, #03a9f4); box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .settings-tabs {
          display: flex; gap: 4px; margin-bottom: 16px; padding: 4px;
          background: var(--secondary-background-color, rgba(0,0,0,0.05));
          border-radius: 12px; overflow-x: auto; scrollbar-width: thin;
        }
        .settings-tab {
          flex: 1 1 0; min-width: max-content; background: transparent; border: none; cursor: pointer;
          padding: 10px 12px; border-radius: 8px; color: var(--secondary-text-color, #666);
          display: inline-flex; align-items: center; justify-content: center; gap: 6px;
          font-size: 13px; font-weight: 500; white-space: nowrap;
          transition: background 0.15s, color 0.15s;
        }
        .settings-tab:hover { color: var(--primary-text-color); }
        .settings-tab.active {
          background: var(--card-background-color, #fff);
          color: var(--primary-color, #03a9f4);
          box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }
        @media (max-width: 540px) {
          .settings-tab span { display: none; }
          .settings-tab { flex: 1 1 0; padding: 10px 8px; }
        }
        /* Karten-Überschriften der Einstellungen: im EEG-Grün und etwas
           größer, damit die Abschnitte beim Scrollen Halt geben. */
        .settings-karte-titel {
          color: var(--success-color, #43a047);
          font-size: 17px;
          font-weight: 600;
        }
        /* Auch die Kartentitel des Dashboards im EEG-Grün (Nutzerwunsch
           27.08.) — Ausnahmen setzen ihre Farbe inline und bleiben. */
        .card h3 { color: var(--success-color, #43a047); }
        .energy-flow-svg { width: 100%; height: auto; max-height: 360px; display: block; }
        .energy-flow-svg.narrow { max-height: 460px; margin: 0 auto; }
        .energy-flow-svg .flow-line { stroke-linecap: round; stroke-dasharray: 6 6; animation: flow-anim 1.2s linear infinite; }
        @keyframes flow-anim { to { stroke-dashoffset: -24; } }
        .energy-flow-svg .ef-node { cursor: pointer; }
        @media (hover: hover) {
          .energy-flow-svg .ef-node { transition: transform 0.15s; }
          .energy-flow-svg .ef-node:hover { transform: scale(1.04); transform-origin: center; transform-box: fill-box; }
        }
        .energy-flow-svg .ef-flow-label { fill: var(--card-background-color, #fff); stroke: var(--divider-color, #e0e0e0); stroke-width: 1; }
        .energy-flow-svg .ef-flow-text { fill: var(--primary-text-color); font-size: 11px; font-weight: 500; pointer-events: none; }
        .energy-flow-svg .ef-node-title { font-size: 11px; letter-spacing: 0.5px; }
        .energy-flow-svg .ef-node-main { font-size: 15px; font-weight: 600; }
        .energy-flow-svg .ef-node-sub { font-size: 10px; }
        .energy-flow-svg.narrow .ef-node-title { font-size: 10px; letter-spacing: 0.2px; }
        .energy-flow-svg.narrow .ef-flow-text { font-size: 12px; }
        .header-timestamps { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 4px 12px; margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--divider-color, #e0e0e0); font-size: 11px; color: var(--secondary-text-color, #999); }
        .status-card-title { display: flex; align-items: center; gap: 8px; margin: 0 0 8px; font-size: 16px; }
        .info-popup-trigger {
          position: relative; display: inline-flex; align-items: center;
        }
        .info-popup {
          display: none; position: absolute; top: calc(100% + 8px); left: 50%;
          transform: translateX(-50%); z-index: 100; width: 380px; max-width: 90vw;
          max-height: 70vh; overflow-y: auto;
          background: var(--card-background-color, #fff); color: var(--primary-text-color, #212121);
          border: 1px solid var(--divider-color, #e0e0e0); border-radius: 12px;
          padding: 16px; box-shadow: 0 4px 24px rgba(0,0,0,0.15);
          font-size: 13px; font-weight: normal; line-height: 1.5; text-align: left;
        }
        .info-popup strong { display: block; font-size: 14px; margin-bottom: 6px; }
        .info-popup p { margin: 6px 0; }
        .info-popup ul { margin: 6px 0; padding-left: 20px; }
        .info-popup li { margin: 3px 0; }
        .info-popup-trigger:hover .info-popup,
        .info-popup-trigger:focus-within .info-popup,
        .info-popup-trigger.active .info-popup { display: block; }
        .info-modal {
          background: var(--card-background-color, #fff); color: var(--primary-text-color, #212121);
          border-radius: 16px; width: 850px; max-width: 100%;
          max-height: calc(100vh - 32px); overflow-y: auto;
          padding: 28px; position: relative;
          box-shadow: 0 8px 40px rgba(0,0,0,0.3);
          font-size: 14px; line-height: 1.6;
        }
        @media (max-width: 600px) {
          .info-modal {
            width: 100%; height: 100%; max-height: 100%;
            border-radius: 0; padding: 16px;
          }
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .manual-spinner {
          width: 40px; height: 40px; border-radius: 50%;
          border: 4px solid var(--divider-color, #e0e0e0);
          border-top-color: var(--primary-color, #03a9f4);
          animation: spin 0.8s linear infinite;
        }
        .connection-lost { display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 60vh; text-align: center; }
        .connection-lost-icon { font-size: 48px; color: var(--warning-color, #ffa726); margin-bottom: 8px; }
        .connection-lost h2 { color: var(--primary-text-color); font-weight: 500; margin: 8px 0; }
        .connection-lost p { color: var(--secondary-text-color, #666); font-size: 14px; margin: 4px 0 24px; }
        .connection-lost-spinner { width: 32px; height: 32px; border: 3px solid var(--divider-color, #e0e0e0); border-top-color: var(--warning-color, #ffa726); border-radius: 50%; animation: conn-spin 1s linear infinite; }
        @keyframes conn-spin { to { transform: rotate(360deg); } }

        /* Energy Flow Diagram */

        /* Live Values Card */
        .val-green { color: #4CAF50; }
        .val-red { color: #f44336; }
        .val-orange { color: #FF9800; }
        .val-blue { color: #2196F3; }
        .toast {
          position: fixed;
          left: 50%;
          bottom: 32px;
          transform: translateX(-50%);
          max-width: min(90vw, 560px);
          padding: 14px 20px;
          border-radius: 10px;
          color: #fff;
          font-size: 14px;
          line-height: 1.45;
          box-shadow: 0 8px 24px rgba(0,0,0,0.25);
          z-index: 9999;
          display: flex;
          align-items: flex-start;
          gap: 12px;
          animation: toast-in 0.22s ease-out;
        }
        .toast-error { background: #c62828; }
        .toast-info { background: #1976d2; }
        .toast-success { background: #2e7d32; }
        .toast-close {
          background: transparent;
          border: none;
          color: rgba(255,255,255,0.9);
          font-size: 18px;
          line-height: 1;
          cursor: pointer;
          padding: 0 0 0 4px;
          margin-left: auto;
        }
        .toast-close:hover { color: #fff; }
        @keyframes toast-in {
          from { opacity: 0; transform: translate(-50%, 12px); }
          to   { opacity: 1; transform: translate(-50%, 0); }
        }
        /* ── Handy / Touch ─────────────────────────────────────────────
           Gebündelt am Ende, damit diese Regeln die darüberstehenden
           Grundwerte überschreiben. Grundlage: Apple gibt 44 px als
           kleinstes Tipp-Ziel vor, iOS hält Hover-Zustände nach einem Tipp
           fest, und der Bereich hinter dem Home-Indikator gehört nicht dem
           Inhalt. Home Assistant liefert die sichere Fläche als
           --safe-area-inset-*; env() steht als Rückfall daneben, damit es
           auch in älteren HA-Versionen stimmt. */
        :host { -webkit-tap-highlight-color: transparent; }
        /* Ein zu breites Element (Info-Blase, Diagramm) darf nie die ganze
           Seite querscrollbar machen. */
        .content { overflow-x: clip; }
        .activity-timeline { overscroll-behavior-y: contain; }
        /* Der Fahrplan zeichnet in Kartenbreite und braucht keinen
           Querscroll mehr; bleibt als Netz für Extremfälle. pan-y hält das
           vertikale Blättern der Seite frei, während waagrechtes Wischen im
           Diagramm den Werte-Zeiger führt. contain verhindert, dass ein
           Wischen am Rand die Zurück-Geste der App auslöst. */
        .sched-scroll { overflow-x: auto; overscroll-behavior-x: contain; touch-action: pan-x pan-y; }
        .sched-chart-card svg { touch-action: pan-y; }
        .toast { bottom: calc(32px + var(--safe-area-inset-bottom, env(safe-area-inset-bottom, 0px))); }
        .dialog-overlay {
          box-sizing: border-box;
          padding: var(--safe-area-inset-top, env(safe-area-inset-top, 0px)) 0
                   var(--safe-area-inset-bottom, env(safe-area-inset-bottom, 0px));
        }
        .dialog-card { max-height: 85dvh; }

        /* Tipp-Ziele: sichtbar bleibt alles gleich groß, die Trefferfläche
           wächst auf 44 px. */
        .mode-toggle { position: relative; }
        .mode-toggle::before { content: ""; position: absolute; inset: -8px -6px; border-radius: 22px; }
        .toolbar button { min-width: 44px; min-height: 44px; display: inline-flex; align-items: center; justify-content: center; }
        .status-view-pills { align-items: center; padding: 2px; }
        .view-pill { min-height: 44px; min-width: 44px; }
        .btn-secondary { min-height: 44px; }
        /* Karten-Köpfe: die 44-px-Trefferfläche nur dort, wo wirklich
           getippt wird — mit der Maus machte sie zugeklappte Karten zu
           hoch (Nutzer-Feedback 27.08.). */
        @media (pointer: coarse) {
          .card > [data-action^="toggle-"] { min-height: 44px; }
        }
        .btn-tap { min-height: 44px; display: inline-flex; align-items: center; gap: 4px; font-size: 13px; }
        .log-filter {
          font-size: 14px; padding: 8px 10px; min-height: 44px;
          background: var(--card-background-color); color: var(--primary-text-color);
          border: 1px solid var(--divider-color); border-radius: 8px; font-family: inherit;
        }
        .log-checkbox {
          display: flex; align-items: center; gap: 6px; min-height: 44px; padding: 0 4px;
          font-size: 13px; color: var(--secondary-text-color); cursor: pointer; user-select: none;
        }
        .info-popup-trigger { min-width: 34px; min-height: 34px; justify-content: center; }

        /* Tipp-Rückmeldung statt Hover-Rückmeldung */
        .card > [data-action^="toggle-"]:active { background: var(--secondary-background-color, rgba(0,0,0,0.05)); border-radius: 8px; }
        .hlv-clickable:active { background: var(--secondary-background-color, rgba(0,0,0,0.08)); }
        .view-pill:active, .btn-link:active, .btn-primary:active, .btn-secondary:active { opacity: 0.75; }

        @media (hover: none) {
          /* Ohne Maus bleibt ein Hover-Zustand nach dem Tippen hängen, bis
             woanders getippt wird. Deshalb hier zurücknehmen. */
          .toolbar button:hover { background: none; }
          .hlv-clickable:hover { background: transparent; }
          .view-pill:hover { color: var(--secondary-text-color, #666); }
          .view-pill.active:hover { color: var(--primary-color, #03a9f4); }
          .btn-link:hover { opacity: 1; }
          .toast-close:hover { color: rgba(255,255,255,0.9); }
          .info-popup-trigger:hover .info-popup { display: none; }
          .info-popup-trigger.active .info-popup { display: block; }
        }

        @media (max-width: 600px) {
          /* Polster: 16 px Seitenrand plus 24 px Kartenrand haben von 390 px
             Bildschirmbreite 80 px gekostet — ein Fünftel, das den
             Diagrammen fehlte. */
          .content { padding: 8px; }
          .card { padding: 10px 16px; }
          .chart-card { padding: 12px; }
          .header-card { padding: 14px; }
          /* Drei Spalten ergaben ~100 px je Zelle; „2,50 kW (Entladung)"
             braucht 130 px und lief über den Kartenrand hinaus. */
          .header-grid { grid-template-columns: repeat(2, 1fr); gap: 10px; }
          /* Der negative Rand zog die Zellen 12 px breiter als ihre Spalte
             und schob die rechte Spalte ueber das Raster hinaus. */
          .hlv-clickable { margin: 0; padding: 4px 0; }
          .header-timestamps { font-size: 12px; }
          .hlv-label { font-size: 11px; }
          .activity-badge { font-size: 11px; }
          .log-filter { font-size: 16px; }
          /* Unter 16 px zoomt iOS beim Hineintippen in ein Feld und bleibt
             gezoomt. */
          select, input:not([type="checkbox"]), textarea { font-size: 16px; }
          /* Zweiter Scrollbereich in der Seite: am Handy fängt er das
             Blättern ab. Die Liste lädt ohnehin über „Mehr laden" nach. */
          .activity-timeline { max-height: none; }
          /* Die 380 px breite Blase ragte neben dem Auslöser aus dem Bild.
             Am Handy steht sie deshalb als Blatt am unteren Rand. */
          .info-popup {
            position: fixed; left: 8px; right: 8px; top: auto;
            bottom: calc(12px + var(--safe-area-inset-bottom, env(safe-area-inset-bottom, 0px)));
            width: auto; max-width: none; transform: none; max-height: 60dvh;
          }
        }
      </style>
      <div class="toolbar">
        <button class="menu-btn" data-action="toggle-sidebar" title="Men\u00fc">
          <ha-icon icon="mdi:menu"></ha-icon>
        </button>
        <h1>EEG Energy Optimizer</h1>
        <div class="toolbar-actions">${headerRight}</div>
      </div>
      ${content}
      ${this._toast ? `
        <div class="toast toast-${this._toast.type}" role="alert">
          <ha-icon icon="mdi:${this._toast.type === "error" ? "alert-circle" : this._toast.type === "success" ? "check-circle" : "information"}"></ha-icon>
          <span>${this._toast.msg}</span>
          <button class="toast-close" data-action="dismiss-toast" title="Schlie\u00dfen">\u00d7</button>
        </div>
      ` : ""}
    `;

    // After innerHTML, populate entity datalists
    if (this._view === "wizard" && this._hass) {
      requestAnimationFrame(() => this._bindEntityPickers());
    }
  }

  disconnectedCallback() {
    window.__eegPanelConnected = false;
    this._disconnectedAt = Date.now();
    if (this._activityUnsub) {
      try { this._activityUnsub(); } catch (_) { /* connection already gone */ }
      this._activityUnsub = null;
    }
    if (this._onVisibilityChange) {
      document.removeEventListener("visibilitychange", this._onVisibilityChange);
    }
    if (this._resizeObserver) {
      try { this._resizeObserver.disconnect(); } catch (_) { /* schon weg */ }
      this._resizeObserver = null;
    }
    if (this._onScrollHide) {
      window.removeEventListener("scroll", this._onScrollHide, { capture: true });
    }
    if (this._resizeTimer) { clearTimeout(this._resizeTimer); this._resizeTimer = null; }
    this._stopTelemetryRefresh();
  }

  connectedCallback() {
    this._disconnectedAt = null;
    window.__eegPanelConnected = true;
    // Re-register visibilitychange listener (disconnectedCallback removes it)
    if (this._onVisibilityChange) {
      document.addEventListener("visibilitychange", this._onVisibilityChange);
    }
    // If already initialized before detach, re-init data + subscription
    if (this._hass && this._initialized) {
      console.info("EEG Energy Optimizer: panel reattached, refreshing");
      this._loadConfigPending = false;
      this._loadConfigWithRetry();
    }
    // Start watchdog
    this._startWatchdog();
    this._startTelemetryRefresh();
    this._startResizeObserver();
  }

  _startTelemetryRefresh() {
    this._stopTelemetryRefresh();
    // Backend flusht alle 60 min — wir refreshen den Status alle 60 s,
    // damit Dashboard ("EEG-Statistik: HH:MM:SS") nicht hängenbleibt.
    this._telemetryRefreshInterval = setInterval(() => {
      if (!this._hass || !this._initialized) return;
      this._hass.callWS({ type: "eeg_optimizer/telemetry_get_status" })
        .then(s => {
          const old = this._telemetryStatus || {};
          if (
            old.last_send_at !== s.last_send_at ||
            old.queue_size !== s.queue_size ||
            old.registered !== s.registered
          ) {
            this._telemetryStatus = s;
            this._render();
          } else {
            this._telemetryStatus = s;
          }
        })
        .catch(() => { /* ignore — UI bleibt mit altem Status */ });
    }, 60000);
  }

  _stopTelemetryRefresh() {
    if (this._telemetryRefreshInterval) {
      clearInterval(this._telemetryRefreshInterval);
      this._telemetryRefreshInterval = null;
    }
  }

  _startWatchdog() {
    this._stopWatchdog();
    this._watchdogInterval = setInterval(() => {
      // Disconnection recovery: element was removed from DOM by HA.
      // Check URL NOW (not at disconnect time) — HA updates the URL
      // after removing the element, so we must wait before checking.
      if (!this.isConnected && this._disconnectedAt) {
        const disconnectedFor = Date.now() - this._disconnectedAt;
        if (disconnectedFor > 3000) {
          // User navigated away → URL changed → stop watchdog, no recovery
          if (window.location.pathname !== "/eeg-optimizer") {
            this._disconnectedAt = null;
            this._stopWatchdog();
            return;
          }
          // Check if a new panel instance has connected in the meantime
          if (window.__eegPanelConnected) {
            this._disconnectedAt = null;
            this._stopWatchdog();
            return;
          }
          // No active panel on /eeg-optimizer → reload to recover
          console.warn("EEG: panel removed while on /eeg-optimizer — reloading");
          this._stopWatchdog();
          window.location.reload();
          return;
        }
      }

      // Remaining checks only when tab is visible
      if (document.visibilityState !== "visible" || !this._initialized) return;

      // Check for missing content
      if (this._shadow && !this._shadow.querySelector(".content")) {
        console.warn("EEG: content missing, re-rendering");
        this._render();
      }

      const elapsed = Date.now() - this._lastHassUpdate;
      if (elapsed > 120000) {
        console.warn("EEG: no hass update for " + Math.round(elapsed / 1000) + "s, reloading config");
        this._loadConfigPending = false;
        this._loadConfigWithRetry();
      }
    }, 5000);
  }

  _stopWatchdog() {
    if (this._watchdogInterval) {
      clearInterval(this._watchdogInterval);
      this._watchdogInterval = null;
    }
  }
}

customElements.define("eeg-optimizer-panel", EegOptimizerPanel);

} // end duplicate-load guard
