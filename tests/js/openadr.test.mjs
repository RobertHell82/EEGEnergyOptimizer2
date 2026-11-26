// OpenADR als Bedarfsquelle: Name als Freitext, je Gemeinschaft eine
// VTN-Adresse (Pflicht), „Verbindung prüfen“ ruft probe_openadr mit der
// ungespeicherten Adresse und zeigt Quote, Vorlauf und Preis.
import { test } from "node:test";
import assert from "node:assert/strict";
import { neuesPanel } from "./panel_harness.mjs";

function panel(daten = {}) {
  const p = neuesPanel();
  p._render = () => {};
  p._wizardData = {
    eeg_demand_source: "openadr",
    peakshare_community: "EEG Wien",
    peakshare_share_pct: 100,
    ...daten,
  };
  return p;
}

test("OpenADR: Name als Freitext, Adressfeld und Prüfknopf", () => {
  const p = panel({ openadr_url: "https://vtn.at/OpenADR/3.1.1" });
  const html = p._gemeinschaftFields(p._wizardData, "");
  assert.match(html, /<option value="openadr" selected>/);
  assert.match(html, /type="text" data-field="peakshare_community"/);
  assert.match(html, /data-field="openadr_url"\s+value="https:\/\/vtn.at\/OpenADR\/3.1.1"/);
  assert.match(html, /data-action="probe-openadr" data-scope="" data-sfx=""/);
  // PeakShare-Gemeinschaften werden für OpenADR nicht geladen.
  assert.ok(!p._peakshareCommunitiesLoading);
});

test("ohne Adresse fehlt ein Pflichtfeld", () => {
  const p = panel();
  const html = p._gemeinschaftFields(p._wizardData, "");
  assert.match(html, /OpenADR-Adresse \*/);
});

test("Verbindung prüfen schickt die ungespeicherte Adresse und zeigt das Ergebnis", async () => {
  const p = panel({ openadr_url: " https://vtn.at/oa " });
  const anfragen = [];
  p._hass = {
    callWS: async (m) => {
      anfragen.push(m);
      return {
        ok: true, url: "https://vtn.at/oa", warnungen: ["ec_feedin: Preise als ct/kWh gelesen"],
        einspeisung: { ols_viertel: 192, ols_min_pct: 20, ols_max_pct: 100, vorlauf_h: 48, preis: 0.115, preis_variabel: false },
        bezug: { ols_viertel: 192, ols_min_pct: 40, ols_max_pct: 40, vorlauf_h: 48, preis: null, preis_variabel: true },
      };
    },
  };
  await p._probeOpenAdr("", "");
  assert.equal(anfragen.length, 1);
  assert.equal(anfragen[0].type, "eeg_optimizer/probe_openadr");
  assert.equal(anfragen[0].url, "https://vtn.at/oa");
  const html = p._openadrFeld(p._wizardData, "", "");
  assert.match(html, /✓ Einspeisung: Quote 20–100 %, 48 h voraus, 11,50 ct\/kWh/);
  assert.match(html, /Bezug: Quote 40 %, 48 h voraus, zeitvariabler Preis/);
  assert.match(html, /Preise als ct\/kWh gelesen/);
});

test("ein Fehler der Prüfung steht rot am Feld", async () => {
  const p = panel({ openadr_url: "https://vtn.at/oa" });
  p._hass = { callWS: async () => ({ ok: false, fehler: "Adresse nicht gefunden (HTTP 404)" }) };
  await p._probeOpenAdr("", "");
  assert.match(p._openadrFeld(p._wizardData, "", ""), /✗ Adresse nicht gefunden \(HTTP 404\)/);
});

test("Bedarfskurve aus der OLS: Achse in %, Tooltip mit beiden Quoten", () => {
  const p = panel();
  p._config = { eeg_demand_source: "openadr", enable_peakshare: true };
  p._cw = () => 800;
  const start = Date.UTC(2026, 10, 26, 12, 0);
  const iso = (ms) => new Date(ms).toISOString();
  p._scheduleData = {
    available: true, time_res_min: 15,
    slots: Array.from({ length: 8 }, (_, i) => ({
      t: iso(start + i * 900000), PV: 1, consumption: 0.5, grid_p: 0.5, battery_p: 0, soc: 50,
    })),
  };
  // Erst Überschuss (nimmt 30 %, liefert 100 %), dann Bedarf (nimmt 100 %, liefert 40 %).
  p._peakshareData = {
    communities: [{
      name: "EEG Wien", warnings: [],
      intervals: Array.from({ length: 8 }, (_, i) => ({
        timestamp: iso(start + i * 900000),
        olsEinspeisung: i < 4 ? 0.3 : 1.0,
        olsBezug: i < 4 ? 1.0 : 0.4,
      })),
    }],
  };
  assert.equal(p._peakshareAktiv(), true);
  const html = p._renderSchedule();
  assert.match(html, />%<\/text>/);
  assert.doesNotMatch(html, />kWh<\/text>/);
  const eegj = [...html.matchAll(/data-eegj="([^"]*)"/g)].map(m => m[1].replace(/&quot;/g, '"'));
  assert.ok(eegj.some(j => j.includes("nimmt 30 %, liefert 100 %")), eegj[0]);
  assert.ok(eegj.some(j => j.includes("nimmt 100 %, liefert 40 %")));
});

test("zwei Prüfungen nebeneinander verwerfen sich nicht", async () => {
  const p = panel({ openadr_url: "https://a.at/oa", openadr_url_2: "https://b.at/oa" });
  const offen = [];
  p._hass = { callWS: (m) => new Promise(res => offen.push(() => res({ ok: false, fehler: m.url }))) };
  const eins = p._probeOpenAdr("", "");
  const zwei = p._probeOpenAdr("", "_2");
  offen[0](); offen[1]();
  await eins; await zwei;
  assert.equal(p._openadrStatus[""].busy, false);
  assert.equal(p._openadrStatus["_2"].busy, false);
});

test("eine Adresse ohne http(s) zählt als fehlend", () => {
  const p = panel({ openadr_url: "ftp://vtn.at" });
  p._settingsData = { ...p._wizardData };
  assert.match(p._settingsPflichtLuecken().join(" "), /gültige OpenADR-Adresse/);
  p._settingsData.openadr_url = "https://vtn.at/oa";
  assert.doesNotMatch(p._settingsPflichtLuecken().join(" "), /OpenADR/);
});
