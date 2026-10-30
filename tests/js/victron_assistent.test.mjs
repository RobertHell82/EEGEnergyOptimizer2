// Victron im Einrichtungsassistenten: Die Erkennung (detect_sensors) liefert
// `victron_pruefung` — fehlende Stellgrößen und falsche ESS-Einstellungen am
// GX-Gerät sperren den Schritt „Wechselrichter", Hinweise nicht.
import { test } from "node:test";
import assert from "node:assert/strict";
import { neuesPanel } from "./panel_harness.mjs";

function assistent(pruefung, prerequisites = { victron_gx: true }) {
  const p = neuesPanel();
  p._wizardStep = 1; // „Wechselrichter"
  p._wizardData = { inverter_type: "victron_ess" };
  p._prerequisites = prerequisites;
  p._detectedSensors = { inverter_type: "victron_ess", victron_pruefung: pruefung };
  p.fehler = [];
  p._showValidationError = (m) => p.fehler.push(m);
  return p;
}

const IN_ORDNUNG = { entities: {}, fehlt: [], probleme: [], hinweise: [] };

test("ohne Integration Victron GX geht es nicht weiter", () => {
  const p = assistent(IN_ORDNUNG, { victron_gx: false });
  assert.equal(p._validateCurrentStep(), false);
  assert.match(p.fehler[0], /Victron GX ist in Home Assistant noch nicht eingerichtet/);
});

test("ohne Prüfergebnis (Erkennung lieferte einen anderen Typ) sperrt der Schritt", () => {
  const p = assistent(null);
  assert.equal(p._validateCurrentStep(), false);
  assert.match(p.fehler[0], /noch nicht geprüft/);
});

test("die Erkennung bekommt den gewählten Typ mit", async () => {
  const p = assistent(IN_ORDNUNG);
  const anfragen = [];
  p._hass = { callWS: async (m) => { anfragen.push(m); return { detected: false }; } };
  await p._detectSensors();
  assert.equal(anfragen[0].type, "eeg_optimizer/detect_sensors");
  assert.equal(anfragen[0].inverter_type, "victron_ess");
});

test("fehlende Stellgrößen sperren den Schritt", () => {
  const p = assistent({ ...IN_ORDNUNG, fehlt: ["ESS-Netzsollwert"] });
  assert.equal(p._validateCurrentStep(), false);
  assert.match(p.fehler[0], /Stellgrößen fehlen.*ESS-Netzsollwert/);
});

test("ein Problem am GX-Gerät sperrt den Schritt", () => {
  const p = assistent({ ...IN_ORDNUNG, probleme: ["ESS „Geplantes Laden“ ist eingeschaltet"] });
  assert.equal(p._validateCurrentStep(), false);
  assert.match(p.fehler[0], /^Victron: ESS „Geplantes Laden“/);
});

test("Hinweise sperren nicht", () => {
  const p = assistent({ ...IN_ORDNUNG, hinweise: ["„DC-gekoppelte PV — Überschuss einspeisen“ ist aus"] });
  p._validateCurrentStep();
  assert.ok(p.fehler.every((m) => !m.startsWith("Victron")), p.fehler.join(" | "));
});

function einstellungen(daten, reiter) {
  const p = neuesPanel();
  p._settingsData = { expert_mode: true, ...daten };
  p._settingsTab = reiter;
  p._peakshareCommunitiesCache = ["EEG Musterdorf"];
  return p._renderSettings();
}

test("Reiter „Netzladen“ mit Beta nur im Expertenmodus und für Huawei/Victron", () => {
  const reiter = /data-tab="netzladen"[\s\S]*?Netzladen<\/span><sup class="tab-beta">Beta<\/sup>/;
  assert.match(einstellungen({ inverter_type: "victron_ess" }, "tarife"), reiter);
  assert.match(einstellungen({ inverter_type: "huawei_sun2000" }, "tarife"), reiter);
  assert.doesNotMatch(einstellungen({ inverter_type: "victron_ess", expert_mode: false }, "tarife"), /data-tab="netzladen"/);
  assert.doesNotMatch(einstellungen({ inverter_type: "sma" }, "tarife"), /data-tab="netzladen"/);
  // Spitzenkappung trägt die Kennzeichnung ebenso
  assert.match(einstellungen({ inverter_type: "sma" }, "tarife"), /kappung<\/span><sup class="tab-beta">Beta<\/sup>/);
});

test("ohne den Reiter landet ein gemerkter Netzladen-Reiter bei Tarife", () => {
  const html = einstellungen({ inverter_type: "sma" }, "netzladen");
  assert.match(html, /data-tab="tarife" role="tab"/);
  assert.match(html, /class="settings-tab active" data-action="set-settings-tab" data-tab="tarife"/);
});

test("alle Netzladen-Felder stehen im Reiter Netzladen, keines mehr unter Tarife", () => {
  const daten = {
    inverter_type: "victron_ess", schedule_netzladen: true,
    peakshare_community: "EEG Musterdorf", peakshare_share_pct: 100,
  };
  const netzladen = einstellungen(daten, "netzladen");
  for (const feld of ["schedule_netzladen", "schedule_bezug_quelle", "schedule_netzladen_max_kw",
                      "schedule_netzladen_vorteil", "peakshare_bezug_price", "peakshare_bezug_ebene"]) {
    assert.match(netzladen, new RegExp(`data-field="settings_${feld}"`), feld);
  }
  assert.match(netzladen, /EEG Musterdorf/);
  const tarife = einstellungen(daten, "tarife");
  assert.doesNotMatch(tarife, /schedule_netzladen|peakshare_bezug/);
});

test("Quotenmodus: kein Gemeinschaftsbezug, nur der Hinweis", () => {
  const html = einstellungen({
    inverter_type: "victron_ess", schedule_netzladen: true, eeg_demand_source: "quote",
    peakshare_community: "EEG Musterdorf",
  }, "netzladen");
  assert.doesNotMatch(html, /peakshare_bezug_price/);
  assert.match(html, /feste[rn]? Abnahmequote/);
});

test("Prüfkarte: grün ohne Befund, rot mit Problem, Text wird maskiert", () => {
  const ok = assistent(IN_ORDNUNG)._renderVictronPruefung();
  assert.match(ok, /success-color/);
  assert.match(ok, /der Optimizer kann steuern/);

  const rot = assistent({ ...IN_ORDNUNG, probleme: ["DVCC ist aus <b>"] })._renderVictronPruefung();
  assert.match(rot, /error-color/);
  assert.match(rot, /DVCC ist aus &lt;b&gt;/);

  assert.equal(assistent(null)._renderVictronPruefung(), "");
});
