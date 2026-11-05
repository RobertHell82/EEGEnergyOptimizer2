// Diagnose der Ansteuerung (Einstellungen → System): Karte, Live-Ansicht,
// und der Hinweis beim Einschalten, solange nie vollständig geprüft wurde.
import { test } from "node:test";
import assert from "node:assert/strict";
import { neuesPanel, warten } from "./panel_harness.mjs";

const BEREICHE = ["vorpruefung", "ladelimit", "entladung", "freigabe", "deckel", "netzladung"];
const TITEL = {
  vorpruefung: "Vorprüfung", ladelimit: "Ladelimit", entladung: "Zwangsentladung",
  freigabe: "Freigabe (Automatik)", deckel: "Entladedeckel", netzladung: "Netzladung",
};

function status(extra = {}) {
  const bereiche = {};
  for (const b of BEREICHE) bereiche[b] = { titel: TITEL[b] };
  return {
    verfuegbar: true, laeuft: false, reihenfolge: BEREICHE,
    nie_vollstaendig: true, bereiche, ...extra,
  };
}

function hassMit(antworten) {
  const aufrufe = [];
  const dienste = [];
  return {
    aufrufe, dienste,
    user: { is_admin: true },
    states: { "select.eeg_energy_optimizer_optimizer": { state: "Aus", attributes: {} } },
    async callWS(msg) {
      aufrufe.push(msg);
      const a = antworten[msg.type];
      return typeof a === "function" ? a(msg) : (a ?? {});
    },
    async callService(domain, service, data) { dienste.push({ domain, service, data }); },
  };
}

test("ohne Lauf: jeder Bereich als „noch nie geprüft“, Start-Knopf für Admins", () => {
  const p = neuesPanel();
  p._hass = hassMit({});
  p._diagnose = status();
  const html = p._diagnoseKarte();
  assert.match(html, /Ansteuerung prüfen/);
  for (const t of Object.values(TITEL)) assert.match(html, new RegExp(t.replace(/[()]/g, "\\$&")));
  assert.equal((html.match(/noch nie geprüft/g) || []).length, BEREICHE.length);
  assert.match(html, /data-action="diagnose-start-frage"/);
  assert.doesNotMatch(html, /diagnose-download/);
});

test("Nicht-Admin sieht keinen Start-Knopf", () => {
  const p = neuesPanel();
  p._hass = { ...hassMit({}), user: { is_admin: false } };
  p._diagnose = status();
  const html = p._diagnoseKarte();
  assert.doesNotMatch(html, /diagnose-start-frage/);
  assert.match(html, /nur ein Administrator/);
});

test("Ergebnisse mit Zeit und Text, escaped; Download nach einem Lauf", () => {
  const p = neuesPanel();
  p._hass = hassMit({});
  const s = status({
    letztes: { gestartet: "2026-11-05T14:20:00+01:00", ergebnis: "fehler",
               modus_vorher: "Ein", modus_wiederhergestellt: true },
  });
  s.bereiche.entladung = { titel: "Zwangsentladung", ergebnis: "fehler",
                           zeit: "2026-11-05T14:25:00+01:00", text: "Befehl <b>abgelehnt</b>" };
  s.bereiche.ladelimit = { titel: "Ladelimit", ergebnis: "ok", zeit: "2026-11-05T14:22:00+01:00", text: "wirksam" };
  p._diagnose = s;
  const html = p._diagnoseKarte();
  assert.match(html, /bestanden/);
  assert.match(html, /fehlgeschlagen/);
  assert.match(html, /Befehl &lt;b&gt;abgelehnt&lt;\/b&gt;/);
  assert.match(html, /data-action="diagnose-download"/);
  assert.match(html, /wieder eingeschaltet/);
});

test("abgebrochen mit Modus Aus: Hinweis zum Wiedereinschalten", () => {
  const p = neuesPanel();
  p._hass = hassMit({});
  p._diagnose = status({
    letztes: { gestartet: "2026-11-05T14:20:00+01:00", ergebnis: "abgebrochen",
               abbruch_grund: "Neustart von Home Assistant während des Tests",
               modus_vorher: "Ein", modus_wiederhergestellt: false },
  });
  const html = p._diagnoseKarte();
  assert.match(html, /Die Optimierung ist aus/);
  assert.match(html, /Neustart von Home Assistant/);
});

test("während des Laufs: aktueller Schritt, Messwerte, Abbrechen, kein Start", () => {
  const p = neuesPanel();
  p._hass = hassMit({});
  p._diagnose = status({
    laeuft: true,
    schritt: { titel: "Ladelimit 0 kW (Laden sperren)", erwartung: "Batterie lädt ≤ 0.25 kW" },
    probe: { soc: 54.4, pv: 5.12, batt: 0.05, netz: 4.0, haus: 1.07 },
    lauf: { schritte: [
      { titel: "Wechselrichter erreichbar", ergebnis: "ok", text: "Treiber meldet verfügbar" },
      { titel: "Ladelimit 0 kW (Laden sperren)", ergebnis: null },
    ] },
  });
  const html = p._diagnoseKarte();
  assert.match(html, /Diagnose läuft/);
  assert.match(html, /Ladelimit 0 kW/);
  assert.match(html, /Ladestand <strong>54 %/);
  assert.match(html, /5,12 kW/);
  assert.match(html, /data-action="diagnose-stop"/);
  assert.match(html, /läuft<\/span>/);
  assert.doesNotMatch(html, /diagnose-start-frage/);
});

test("nicht gesteuerter Treiber: Erklärung statt Tabelle", () => {
  const p = neuesPanel();
  p._hass = hassMit({});
  p._diagnose = { verfuegbar: false };
  const html = p._diagnoseKarte();
  assert.match(html, /nicht gesteuert/);
  assert.doesNotMatch(html, /<table/);
});

test("Einschalten ohne vollständige Prüfung zeigt den Hinweis statt zu schalten", async () => {
  const p = neuesPanel();
  const hass = hassMit({ "eeg_optimizer/get_diagnose": status() });
  p._hass = hass;
  p._handleAction("toggle-mode", {});
  await warten();
  assert.equal(p._diagnoseDialog, "modus");
  assert.equal(hass.dienste.length, 0);
  const dialog = p._renderDiagnoseDialog();
  assert.match(dialog, /noch nicht geprüft/);
  assert.match(dialog, /diagnose-oeffnen/);
  p._handleAction("diagnose-trotzdem-ein", {});
  assert.equal(p._diagnoseDialog, null);
  assert.deepEqual(hass.dienste[0].data.option, "Ein");
});

test("Einschalten nach vollständiger Prüfung schaltet sofort", async () => {
  const p = neuesPanel();
  const hass = hassMit({ "eeg_optimizer/get_diagnose": status({ nie_vollstaendig: false }) });
  p._hass = hass;
  p._handleAction("toggle-mode", {});
  await warten();
  assert.equal(p._diagnoseDialog, null);
  assert.equal(hass.dienste[0].data.option, "Ein");
});

test("Ausschalten fragt nie", async () => {
  const p = neuesPanel();
  const hass = hassMit({ "eeg_optimizer/get_diagnose": status() });
  hass.states["select.eeg_energy_optimizer_optimizer"].state = "Ein";
  p._hass = hass;
  p._handleAction("toggle-mode", {});
  await warten();
  assert.equal(hass.dienste[0].data.option, "Aus");
  assert.equal(hass.aufrufe.length, 0);
});

test("laufende Diagnose: Einschalten warnt vor dem Abbruch", async () => {
  const p = neuesPanel();
  p._hass = hassMit({ "eeg_optimizer/get_diagnose": status({ laeuft: true, nie_vollstaendig: false }) });
  await p._modusEinMitHinweis();
  assert.equal(p._diagnoseDialog, "modus");
  const dialog = p._renderDiagnoseDialog();
  assert.match(dialog, /bricht den laufenden Test ab/);
  assert.doesNotMatch(dialog, /diagnose-oeffnen/);
  if (p._diagnoseTimer) clearTimeout(p._diagnoseTimer);
});

test("Start: Bestätigung, dann diagnose_start, dann Live-Abfrage", async () => {
  const p = neuesPanel();
  let laeuft = false;
  const hass = hassMit({
    "eeg_optimizer/diagnose_start": () => { laeuft = true; return { success: true }; },
    "eeg_optimizer/get_diagnose": () => status({ laeuft }),
  });
  p._hass = hass;
  p._angehaengt = true;
  p._view = "settings";
  p._handleAction("diagnose-start-frage", {});
  assert.equal(p._diagnoseDialog, "start");
  assert.match(p._renderDiagnoseDialog(), /ausgeschaltet/);
  p._handleAction("diagnose-start", {});
  await warten();
  assert.equal(p._diagnoseDialog, null);
  assert.deepEqual(hass.aufrufe.map(a => a.type),
    ["eeg_optimizer/diagnose_start", "eeg_optimizer/get_diagnose"]);
  // Läuft → nächste Abfrage ist geplant; abhängen räumt sie ab.
  assert.ok(p._diagnoseTimer);
  p.disconnectedCallback();
  assert.equal(p._diagnoseTimer, null);
});

test("Start abgelehnt: Meldung als Toast", async () => {
  const p = neuesPanel();
  p._hass = hassMit({
    "eeg_optimizer/diagnose_start": { success: false, error: "Die Diagnose läuft bereits." },
    "eeg_optimizer/get_diagnose": status(),
  });
  await p._diagnoseStarten();
  assert.equal(p._toast?.msg, "Die Diagnose läuft bereits.");
  if (p._toastTimer) clearTimeout(p._toastTimer);
});

test("Karte steht im System-Reiter", () => {
  const p = neuesPanel();
  p._hass = hassMit({});
  p._diagnose = status();
  p._config = { inverter_type: "huawei_sun2000", setup_complete: true };
  p._settingsData = { ...p._config };
  p._settingsTab = "system";
  // _renderSettings liefert den ganzen Einstellungsbereich als HTML.
  const html = p._renderSettings();
  assert.match(html, /Ansteuerung prüfen/);
});

test("Hinweis fragt immer den Server — alter Stand im Panel zählt nicht", async () => {
  const p = neuesPanel();
  const hass = hassMit({ "eeg_optimizer/get_diagnose": status({ nie_vollstaendig: false }) });
  p._hass = hass;
  // Im Panel steht noch „nie geprüft" — auf einem anderen Gerät ist der
  // Test inzwischen durchgelaufen.
  p._diagnose = status();
  await p._modusEinMitHinweis();
  assert.equal(p._diagnoseDialog, null);
  assert.equal(hass.dienste[0].data.option, "Ein");
});

test("Live-Abfrage nur in den Einstellungen und nur eingehängt", async () => {
  const p = neuesPanel();
  p._hass = hassMit({ "eeg_optimizer/get_diagnose": status({ laeuft: true }) });
  p._angehaengt = true;
  p._view = "dashboard";
  await p._loadDiagnose();
  assert.equal(p._diagnoseTimer, null);
  p._view = "settings";
  p._angehaengt = false;
  await p._loadDiagnose();
  assert.equal(p._diagnoseTimer, null);
  p._angehaengt = true;
  await p._loadDiagnose();
  assert.ok(p._diagnoseTimer);
  clearTimeout(p._diagnoseTimer);
});

test("Wiedereinhängen nimmt eine laufende Diagnose wieder auf", async () => {
  const p = neuesPanel();
  const hass = hassMit({
    "eeg_optimizer/get_diagnose": status({ laeuft: true }),
    "eeg_optimizer/get_config": { config: { setup_complete: true } },
  });
  p._hass = hass;
  p._initialized = true;
  p._view = "settings";
  p._diagnose = status({ laeuft: true });
  p._loadConfigWithRetry = () => {};
  p._angehaengt = true;
  p.connectedCallback();
  await warten();
  assert.ok(hass.aufrufe.some(a => a.type === "eeg_optimizer/get_diagnose"));
  assert.ok(p._diagnoseTimer);
  p.disconnectedCallback();
  assert.equal(p._diagnoseTimer, null);
});

test("schmal: Liste statt Tabelle", () => {
  const p = neuesPanel();
  p._hass = hassMit({});
  p._diagnose = status();
  const html = p._diagnoseKarte();
  assert.doesNotMatch(html, /<table/);
  assert.match(html, /diagnose-bereiche/);
});
