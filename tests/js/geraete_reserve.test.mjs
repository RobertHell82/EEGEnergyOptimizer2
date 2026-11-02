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
