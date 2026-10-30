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

test("„Batterie aus dem Netz laden“ steht auch für Victron in den Einstellungen", () => {
  const p = neuesPanel();
  const feld = (inverter_type, expert_mode = true) =>
    p._netzladenFeld({ inverter_type, expert_mode }, "settings_");
  assert.match(feld("victron_ess"), /Batterie aus dem Netz laden/);
  assert.match(feld("huawei_sun2000"), /Batterie aus dem Netz laden/);
  assert.equal(feld("victron_ess", false), "");
  assert.equal(feld("sma"), "");
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
