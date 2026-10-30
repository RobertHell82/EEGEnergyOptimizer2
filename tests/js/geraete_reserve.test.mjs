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
