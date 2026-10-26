// Lädt das echte Panel-Skript in einen vm-Kontext mit so viel DOM, wie der
// Konstruktor und die Abo-Logik brauchen, und stellt eine nachgebaute
// HA-Verbindung bereit, deren Antworten der Test einzeln auslöst.
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const HIER = path.dirname(fileURLToPath(import.meta.url));
// EEG_PANEL_DATEI: eine andere Fassung prüfen (z.B. einen älteren Stand,
// um zu sehen, welche Tests den Fehler dort finden).
const PANEL = process.env.EEG_PANEL_DATEI || path.join(
  HIER, "..", "..", "custom_components", "eeg_energy_optimizer",
  "frontend", "eeg-optimizer-panel.js",
);

const stumm = { log() {}, info() {}, warn() {}, error() {}, debug() {} };

function knoten() {
  return {
    innerHTML: "",
    style: {},
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
    removeEventListener() {},
    appendChild() {},
  };
}

let _klasse = null;

export function panelKlasse() {
  if (_klasse) return _klasse;
  const registry = new Map();
  class HTMLElement {
    constructor() { this._angehaengt = false; }
    attachShadow() { return knoten(); }
    get isConnected() { return this._angehaengt; }
    addEventListener() {}
    removeEventListener() {}
    dispatchEvent() { return true; }
  }
  const speicher = new Map();
  const ctx = {
    HTMLElement,
    customElements: {
      get: (n) => registry.get(n),
      define: (n, c) => registry.set(n, c),
    },
    window: {
      matchMedia: () => ({ matches: false, addEventListener() {} }),
      addEventListener() {},
      removeEventListener() {},
      location: { pathname: "/eeg-optimizer" },
    },
    document: {
      addEventListener() {},
      removeEventListener() {},
      visibilityState: "visible",
      createElement: () => knoten(),
    },
    localStorage: {
      getItem: (k) => (speicher.has(k) ? speicher.get(k) : null),
      setItem: (k, v) => speicher.set(k, String(v)),
      removeItem: (k) => speicher.delete(k),
    },
    console: stumm,
    // Timer des Panels (Wiederholungen, Intervalle) sollen den Testprozess
    // nicht am Beenden hindern.
    setTimeout: (...a) => setTimeout(...a).unref(),
    setInterval: (...a) => setInterval(...a).unref(),
    clearTimeout, clearInterval,
    requestAnimationFrame: () => 0,
    cancelAnimationFrame() {},
  };
  ctx.window.localStorage = ctx.localStorage;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(PANEL, "utf8"), ctx, { filename: PANEL });
  _klasse = registry.get("eeg-optimizer-panel");
  if (!_klasse) throw new Error("Panel hat sich nicht registriert");
  return _klasse;
}

// Ein Promise, das der Test von außen erfüllt oder ablehnt.
export function offen() {
  let erfuellen, ablehnen;
  const promise = new Promise((res, rej) => { erfuellen = res; ablehnen = rej; });
  return { promise, erfuellen, ablehnen };
}

// Nachbau von home-assistant-js-websocket: subscribeEvents antwortet erst,
// wenn der Test bestaetigen()/ablehnen() ruft; Events erreichen jedes Abo,
// das der Server angenommen und noch nicht abgemeldet hat.
export class FakeVerbindung {
  constructor(name = "conn") {
    this.name = name;
    this.abos = [];
    this.unsubWirft = false;      // unsub() wirft synchron
    this.unsubLehntAb = false;    // unsub() gibt ein abgelehntes Promise
    this.subscribeWirft = false;  // subscribeEvents() wirft synchron
  }

  subscribeEvents(cb, typ) {
    if (this.subscribeWirft) throw new Error("socket closed");
    const antwort = offen();
    const abo = {
      cb, typ, antwort,
      angenommen: false,
      abgemeldet: false,
      unsubAufrufe: 0,
    };
    abo.unsub = () => {
      abo.unsubAufrufe += 1;
      abo.abgemeldet = true;
      if (this.unsubWirft) throw new Error("connection lost");
      return this.unsubLehntAb
        ? Promise.reject(new Error("connection lost"))
        : Promise.resolve();
    };
    this.abos.push(abo);
    return antwort.promise;
  }

  bestaetigen(i = this.abos.length - 1) {
    const abo = this.abos[i];
    abo.angenommen = true;
    abo.antwort.erfuellen(abo.unsub);
  }

  ablehnen(i = this.abos.length - 1, fehler = new Error("subscribe failed")) {
    this.abos[i].antwort.ablehnen(fehler);
  }

  feuern(data) {
    for (const abo of this.abos) {
      if (abo.angenommen && !abo.abgemeldet) abo.cb({ data });
    }
  }

  get lebende() {
    return this.abos.filter(a => a.angenommen && !a.abgemeldet).length;
  }
}

// hass-Objekt: get_config sofort, get_activity_log über eine Warteschlange,
// damit der Test die Reihenfolge von Antwort und Live-Event bestimmen kann.
export class FakeHass {
  constructor(verbindung, { serverLog = [], ladenSofort = true } = {}) {
    this.connection = verbindung;
    this.serverLog = serverLog;
    this.ladenSofort = ladenSofort;
    this.ladungen = [];
  }

  async callWS(msg) {
    if (msg.type === "eeg_optimizer/get_config") {
      return { setup_complete: true, entry_id: "test" };
    }
    if (msg.type === "eeg_optimizer/get_activity_log") {
      const antwort = () => {
        const entries = this.serverLog.slice(msg.offset, msg.offset + msg.limit);
        return {
          entries,
          total: this.serverLog.length,
          has_more: msg.offset + entries.length < this.serverLog.length,
        };
      };
      if (this.ladenSofort) return antwort();
      const o = offen();
      this.ladungen.push({ msg, antworten: () => o.erfuellen(antwort()),
        fehler: (e) => o.ablehnen(e) });
      return o.promise;
    }
    if (msg.type === "eeg_optimizer/telemetry_get_status") {
      return { configured: false, enabled: false, registered: false };
    }
    return {};
  }

  // Neues Objekt mit derselben Verbindung, wie HA es bei jeder
  // Zustandsänderung an das Panel übergibt.
  kopie(verbindung = this.connection) {
    const h = new FakeHass(verbindung, {
      serverLog: this.serverLog, ladenSofort: this.ladenSofort,
    });
    h.ladungen = this.ladungen;
    return h;
  }
}

// Ein Panel, wie HA es einhängt — ohne Watchdogs und Rendering.
export function neuesPanel() {
  const Klasse = panelKlasse();
  const p = new Klasse();
  p.renders = 0;
  p._render = () => { p.renders += 1; };
  p._resolveEntityIds = async () => {};
  p._loadSchedule = () => {};
  p._loadScheduleHistory = () => {};
  p._loadPeakShareData = () => {};
  p._startWatchdog = () => {};
  p._startTelemetryRefresh = () => {};
  p._startResizeObserver = () => {};
  return p;
}

export function einhaengen(p) {
  p._angehaengt = true;
  p.connectedCallback();
}

export function abhaengen(p) {
  p._angehaengt = false;
  p.disconnectedCallback();
}

// Alle anstehenden Promise-Fortsetzungen abarbeiten.
export async function warten(runden = 10) {
  for (let i = 0; i < runden; i++) await new Promise(r => setImmediate(r));
}

let _nr = 0;
export function eintrag(felder = {}) {
  _nr += 1;
  return {
    timestamp: `2026-10-26T09:15:00.${String(_nr).padStart(6, "0")}+00:00`,
    zustand: "Laden begrenzt",
    reason: "Plan",
    status: "Laden begrenzt auf 3.1 kW",
    ...felder,
  };
}

// Panel bis zum ersten, bestätigten Abo hochfahren.
export async function gestartet({ serverLog = [] } = {}) {
  const conn = new FakeVerbindung();
  const hass = new FakeHass(conn, { serverLog });
  const p = neuesPanel();
  p._angehaengt = true;
  p.hass = hass;
  await warten();
  return { p, conn, hass };
}
