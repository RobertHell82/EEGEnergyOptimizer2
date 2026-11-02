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
  const p = assistent({ ...IN_ORDNUNG, probleme: ["DVCC ist aus"] });
  assert.equal(p._validateCurrentStep(), false);
  assert.match(p.fehler[0], /^Victron: DVCC ist aus/);
  assert.doesNotMatch(p._renderVictronPruefung(), /victron-trotzdem/);
});

test("eine Warnung sperrt bis „Trotzdem weitermachen“, bleibt aber rot", () => {
  const pr = { ...IN_ORDNUNG, warnungen: ["ESS „Geplantes Laden“ ist eingeschaltet"] };
  const p = assistent(pr);
  assert.equal(p._validateCurrentStep(), false);
  assert.match(p.fehler[0], /^Victron: ESS „Geplantes Laden“.*Trotzdem weitermachen/);
  const vorher = p._renderVictronPruefung();
  assert.match(vorher, /error-color/);
  assert.match(vorher, /data-action="victron-trotzdem"/);

  p._victronTrotzdem = pr.warnungen.join("|"); // wie der Knopf
  p.fehler = [];
  p._validateCurrentStep();
  assert.ok(p.fehler.every((m) => !m.startsWith("Victron")), p.fehler.join(" | "));
  const nachher = p._renderVictronPruefung();
  assert.match(nachher, /error-color/);
  assert.doesNotMatch(nachher, /data-action="victron-trotzdem"/);

  // eine andere Warnung nach erneuter Prüfung fragt neu
  p._detectedSensors.victron_pruefung = { ...IN_ORDNUNG, warnungen: ["etwas anderes"] };
  assert.equal(p._validateCurrentStep(), false);
});

test("Trotzdem weitermachen hilft nicht gegen ein Problem", () => {
  const p = assistent({ ...IN_ORDNUNG, probleme: ["DVCC ist aus"], warnungen: ["x"] });
  p._victronTrotzdem = "x";
  assert.equal(p._validateCurrentStep(), false);
  assert.doesNotMatch(p._renderVictronPruefung(), /victron-trotzdem/);
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

test("Bezugstarife stehen im Reiter Tarife, getrennt von der Einspeisung", () => {
  const daten = {
    inverter_type: "victron_ess", schedule_netzladen: true,
    peakshare_community: "EEG Musterdorf", peakshare_share_pct: 100,
  };
  const netzladen = einstellungen(daten, "netzladen");
  // Ein/Aus als Schalterkarte wie „Bezugsspitzen kappen“, nicht als Haken
  assert.match(netzladen, /data-action="toggle-settings-feature" data-feature="schedule_netzladen" data-on="1"/);
  assert.match(netzladen, /feature-badge on">Aktiv/);
  assert.doesNotMatch(netzladen, /data-field="settings_schedule_netzladen"/);
  for (const feld of ["schedule_netzladen_max_kw", "schedule_netzladen_vorteil"]) {
    assert.match(netzladen, new RegExp(`data-field="settings_${feld}"`), feld);
  }
  assert.doesNotMatch(netzladen, /schedule_bezug_quelle|peakshare_bezug/);

  const tarife = einstellungen(daten, "tarife");
  assert.match(tarife, /settings-karte-titel"[^>]*>Einspeisung</);
  assert.match(tarife, /settings-karte-titel"[^>]*>Bezug</);
  for (const feld of ["schedule_bezug_quelle", "peakshare_bezug_price", "peakshare_bezug_ebene"]) {
    assert.match(tarife, new RegExp(`data-field="settings_${feld}"`), feld);
  }
  // Der Bezugstarif steht in der Karte „Bezug“, nach der Netzgebühr.
  assert.ok(tarife.indexOf("settings_schedule_bezug_quelle") > tarife.indexOf("settings_schedule_netzbereich"));
  // In der Gemeinschaft: erst Einspeisung, dann Bezug.
  assert.ok(tarife.indexOf("settings_peakshare_bezug_price") > tarife.indexOf("settings_peakshare_price_night"));
});

test("Bezugstarife auch ohne Expertenmodus, aber nur für Treiber mit Netzladen", () => {
  assert.match(einstellungen({ inverter_type: "huawei_sun2000", expert_mode: false }, "tarife"),
    /settings_schedule_bezug_quelle/);
  // Seit 2.1.64 können alle sieben Treiber aus dem Netz laden — ohne Bezugstarife bleibt nur ein unbekannter Typ.
  assert.doesNotMatch(einstellungen({ inverter_type: "unbekannt" }, "tarife"),
    /schedule_bezug_quelle|peakshare_bezug/);
});

test("Netzladen lässt sich ohne günstigeren Bezugspreis nicht einschalten", () => {
  const p = neuesPanel();
  p._settingsData = { expert_mode: true, inverter_type: "huawei_sun2000", schedule_netzladen: false };
  p._settingsTab = "netzladen";
  const fehler = [];
  p._showValidationError = (m) => fehler.push(m);
  p._render = () => {};
  p._handleAction("toggle-settings-feature", { feature: "schedule_netzladen", on: "0" });
  assert.equal(p._settingsData.schedule_netzladen, false);
  assert.match(fehler[0], /günstigerer Bezugspreis im Reiter Tarife/);
  assert.match(p._renderSettings(), /Zum Einschalten fehlt noch/);

  // Mit zeitvariablen Netzentgelten geht es.
  p._settingsData.schedule_snap_enabled = true;
  p._handleAction("toggle-settings-feature", { feature: "schedule_netzladen", on: "0" });
  assert.equal(p._settingsData.schedule_netzladen, true);
});

test("Quotenmodus: kein Gemeinschaftsbezug, nur der Hinweis", () => {
  const html = einstellungen({
    inverter_type: "victron_ess", eeg_demand_source: "quote",
    peakshare_community: "EEG Musterdorf",
  }, "tarife");
  assert.doesNotMatch(html, /peakshare_bezug_price/);
  assert.match(html, /feste[rn]? Abnahmequote statt PeakShare/);
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
