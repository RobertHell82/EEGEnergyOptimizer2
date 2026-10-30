// Tarife der Gemeinschaft vom Server: Der Block der Gemeinschaft zeigt den
// Tarif, sperrt die Felder und rechnet die Vorschau mit den Server-Sätzen.
import { test } from "node:test";
import assert from "node:assert/strict";
import { neuesPanel, warten } from "./panel_harness.mjs";

const EWA = {
  name: "BEG",
  verband_name: "Energiewende Ansfelden",
  tarif: {
    gueltig_ab: "2026-10-01", einspeisung_tag: 0.098, einspeisung_nacht: 0.108,
    nacht: "sonne", bezug: 0.137, quelle: "https://www.ew-ansfelden.at/about-7",
    hinweis: "Speicherstrom nach Sonnenuntergang bekommt den Wasser/Wind-Tarif.",
  },
  fenster: [17, 7],
  naechster: null,
};

function einstellungen(daten, reiter = "tarife", gemeinschaften = [EWA]) {
  const p = neuesPanel();
  p.anfragen = [];
  p._hass = { states: {}, callWS: async (m) => { p.anfragen.push(m); return { gemeinschaften }; } };
  p._settingsData = {
    expert_mode: true, inverter_type: "huawei_sun2000", enable_peakshare: true,
    peakshare_community: "BEG", peakshare_share_pct: 100,
    peakshare_price: 0.12, peakshare_price_night: 0.15,
    peakshare_night_start: "20:00", peakshare_night_end: "06:00", ...daten,
  };
  p._settingsTab = reiter;
  p._peakshareCommunitiesCache = ["BEG", "Marchtrenk"];
  return p;
}

test("fragt den Server mit den gewählten Namen", async () => {
  const p = einstellungen({});
  p._renderSettings();
  await warten();
  const tarif = () => p.anfragen.filter(m => m.type === "eeg_optimizer/get_gemeinschaftstarif");
  assert.equal(tarif().length, 1);
  assert.equal(tarif()[0].peakshare_community, "BEG");
  // Gleiche Namen → kein zweiter Abruf, neuer Name → neu gefragt
  p._renderSettings();
  await warten();
  assert.equal(tarif().length, 1);
  p._settingsData.peakshare_community = "Marchtrenk";
  p._renderSettings();
  await warten();
  assert.equal(tarif().length, 2);
});

test("Server-Tarif: Kasten, gesperrte Felder mit den Server-Sätzen, Fenster nach Sonne", async () => {
  const p = einstellungen({});
  p._renderSettings();
  await warten();
  const html = p._renderSettings();
  assert.match(html, /Tarif von Energiewende Ansfelden<\/strong>, gültig ab 01\.10\.2026/);
  assert.match(html, /Einspeisung 9,80 ct netto, nach Sonnenuntergang 10,80 ct · Bezug 13,70 ct brutto/);
  assert.match(html, /data-field="settings_peakshare_price" data-unit="ct"\s+value="9\.8"[^>]*disabled/);
  assert.match(html, /data-field="settings_peakshare_price_night" data-unit="ct"\s+value="10\.8"[\s\S]*?disabled/);
  assert.match(html, /data-field="settings_peakshare_night_start"\s+value="17:00" disabled/);
  assert.match(html, /data-field="settings_peakshare_night_end"\s+value="07:00" disabled/);
  assert.match(html, /von Sonnenuntergang bis Sonnenaufgang/);
  assert.match(html, /data-feature="gemeinschaftstarif_auto" data-on="1">Eigene Werte verwenden/);
});

test("Schalter aus: eigene Werte, Felder offen, Kasten bietet die Übernahme an", async () => {
  const p = einstellungen({ gemeinschaftstarif_auto: false });
  p._renderSettings();
  await warten();
  const html = p._renderSettings();
  assert.match(html, /data-field="settings_peakshare_price" data-unit="ct"\s+value="12"[^>]*>/);
  assert.doesNotMatch(html, /data-field="settings_peakshare_price" [^>]*disabled/);
  assert.match(html, /data-field="settings_peakshare_night_start"\s+value="20:00" >/);
  assert.match(html, /data-on="0">Tarif automatisch übernehmen/);
});

test("Gemeinschaft ohne Server-Eintrag bleibt wie bisher", async () => {
  const p = einstellungen({ peakshare_community: "Marchtrenk" }, "tarife", []);
  p._renderSettings();
  await warten();
  const html = p._renderSettings();
  assert.doesNotMatch(html, /Tarif von /);
  assert.doesNotMatch(html, /data-field="settings_peakshare_price" [^>]*disabled/);
});

test("Netzladen: Bezugspreis aus dem Tarifblatt", async () => {
  const p = einstellungen({ schedule_netzladen: true, peakshare_bezug_price: 0.2 }, "netzladen");
  p._renderSettings();
  await warten();
  const html = p._renderSettings();
  assert.match(html, /data-field="settings_peakshare_bezug_price" data-unit="ct"\s+value="13\.7"[\s\S]*?disabled/);
  assert.match(html, /Aus dem Tarifblatt/);
});
