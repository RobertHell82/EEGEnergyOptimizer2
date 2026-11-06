// Statuskarte: Warnung, wenn der Wechselrichter mehr Reserve hält als der
// eingestellte Mindest-Ladestand. Graz, Oktober 2026: MinRsvPct stand auf
// 49 % (Vorgängerversion), eingestellt waren 20 % — sichtbar war das nirgends.
import { test } from "node:test";
import assert from "node:assert/strict";
import { neuesPanel } from "./panel_harness.mjs";

function zustand(extra = {}) {
  return {
    state: "Normalbetrieb",
    attributes: {
      status: "Normalbetrieb",
      gesteuert: true,
      letzte_aktualisierung: "2026-10-30T19:00:00+01:00",
      plan_aktion: "release",
      ...extra,
    },
  };
}

test("Reserve über dem Mindest-Ladestand erscheint als Warnung, beim Fronius mit MinRsvPct", () => {
  const p = neuesPanel();
  p._config = { inverter_type: "fronius_gen24" };
  const html = p._renderSteuerungZeilen(zustand({
    geraete_reserve_pct: 49, geraete_reserve_einstellung_pct: 20,
  }));
  assert.match(html, /hält <strong>49 %<\/strong> Reserve/);
  assert.match(html, /eingestellt sind 20 %/);
  assert.match(html, /MinRsvPct/);
});

test("andere Treiber: Warnung ohne den Fronius-Satz", () => {
  const p = neuesPanel();
  p._config = { inverter_type: "huawei_sun2000" };
  const html = p._renderSteuerungZeilen(zustand({
    geraete_reserve_pct: 30, geraete_reserve_einstellung_pct: 10,
  }));
  assert.match(html, /hält <strong>30 %<\/strong> Reserve/);
  assert.doesNotMatch(html, /MinRsvPct/);
});

test("ohne Attribut keine Warnung", () => {
  const p = neuesPanel();
  p._config = { inverter_type: "fronius_gen24" };
  assert.doesNotMatch(p._renderSteuerungZeilen(zustand()), /Reserve/);
});

// Spitzenkappung: „Reserve notfalls aus dem Netz nachladen" nur, wo der
// Treiber die Entladung deckeln UND aus dem Netz laden kann — seit 2.1.64
// alle sieben; ein unbekannter Typ bekommt den Schalter nicht.
test("Reserve aus dem Netz: alle bekannten Treiber", () => {
  const p = neuesPanel();
  const sichtbar = (typ) => p._kappungReserveNetz({ inverter_type: typ });
  for (const typ of ["huawei_sun2000", "fronius_gen24", "victron_ess", "sma_smart_energy",
                     "solax_gen4", "kostal_plenticore", "sigenergy_sigenstor"]) {
    assert.equal(sichtbar(typ), true, typ);
  }
  assert.equal(sichtbar("unbekannt"), false);
});

test("Schalter steht im Reiter Spitzenkappung", () => {
  const p = neuesPanel();
  const html = p._spitzenkappungFields(
    { inverter_type: "fronius_gen24", spitzenkappung_enabled: true, spitzenkappung_reserve_netz: true },
    "settings_",
  );
  assert.match(html, /data-field="settings_spitzenkappung_reserve_netz" checked/);
  const ohne = p._spitzenkappungFields({ inverter_type: "unbekannt", spitzenkappung_enabled: true }, "settings_");
  assert.doesNotMatch(ohne, /spitzenkappung_reserve_netz/);
});

// Die Typliste nennt alle sieben; ob der Entladedeckel an der Anlage geht,
// meldet erst die laufende Spitzenkappung (kappung.batterie = null).
function mitKappung(p, kappung) {
  p._config = { inverter_type: "victron_ess" };
  p._entityIds = { ...(p._entityIds || {}), bezugsspitze_monat: "sensor.bezugsspitze_monat" };
  p._hass = { ...(p._hass || {}), states: {
    "sensor.bezugsspitze_monat": { state: "3.2", attributes: kappung === undefined ? {} : { kappung } },
  } };
}

const victronKappung = () => ({
  inverter_type: "victron_ess", spitzenkappung_enabled: true, spitzenkappung_grenze_kw: 5,
  spitzenkappung_verbraucher: [], inverter_ac_limit_kw: 5, pv_peak_kwp: 8,
});

test("Spitzenkappung: Laufzeit ohne Deckel macht Verbraucher zur Pflicht", () => {
  const p = neuesPanel();
  mitKappung(p, { schwelle_kw: 5, abgeschaltet: [], batterie: null });
  const d = victronKappung();
  assert.equal(p._kappungMitBatterie(d), false);
  assert.equal(p._kappungReserveNetz(d), false);
  p._settingsData = d;
  assert.ok(p._settingsPflichtLuecken().some(x => /mindestens ein Verbraucher/.test(x)));
  const html = p._spitzenkappungFields(d, "settings_");
  assert.doesNotMatch(html, /spitzenkappung_reserve_min_pct/);
  assert.match(html, /mindestens einen Verbraucher/);
});

test("Spitzenkappung: Deckel gemeldet oder noch kein Laufzeitwert → Typliste gilt", () => {
  const p = neuesPanel();
  const d = victronKappung();
  mitKappung(p, { schwelle_kw: 5, abgeschaltet: [], batterie: { unterstuetzt: true } });
  assert.equal(p._kappungMitBatterie(d), true);
  mitKappung(p, undefined);
  assert.equal(p._kappungMitBatterie(d), true);
  p._settingsData = d;
  assert.ok(!p._settingsPflichtLuecken().some(x => /mindestens ein Verbraucher/.test(x)));
  // Typwechsel im Assistenten: der Laufzeitwert gehört zum alten Treiber.
  mitKappung(p, { schwelle_kw: 5, abgeschaltet: [], batterie: null });
  assert.equal(p._kappungMitBatterie({ inverter_type: "huawei_sun2000" }), true);
});

test("Warnung bei zu großer Mindestreserve der Spitzenkappung (Häusler, Nov. 2026)", () => {
  const p = neuesPanel();
  const d = {
    inverter_type: "huawei_sun2000", spitzenkappung_enabled: true,
    schedule_min_soc_pct: 35, spitzenkappung_reserve_min_pct: 40,
    spitzenkappung_reserve_netz: true,
  };
  const html = p._kappungReserveWarnung(d);
  assert.match(html, /75 %/);
  assert.match(html, /vor Sonnenuntergang zugekauft/);
  // Ohne Nachladen aus dem Netz: Warnung ohne den Satz zum Zukauf
  assert.doesNotMatch(p._kappungReserveWarnung({ ...d, spitzenkappung_reserve_netz: false }), /zugekauft/);
  // Bis 50 % zusammen oder ohne Mindestreserve: keine Warnung
  assert.equal(p._kappungReserveWarnung({ ...d, spitzenkappung_reserve_min_pct: 15 }), "");
  assert.equal(p._kappungReserveWarnung({ ...d, schedule_min_soc_pct: 60, spitzenkappung_reserve_min_pct: 0 }), "");
});
