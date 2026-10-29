// Karte „Bezugsspitze & Spitzenkappung": Laden, Blättern und was sie zeigt.
// Geprüft wird das echte Panel mit Antworten im Format von
// websocket_api.spitze_karte.
process.env.TZ = "Europe/Vienna";
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { neuesPanel, offen, speicher, warten } from "./panel_harness.mjs";

const P = "eeg_optimizer_panel_";

function aufzeichnendesHass() {
  const anfragen = [];
  return {
    anfragen,
    connection: null,
    callWS(msg) {
      const o = offen();
      anfragen.push({ msg, antworten: o.erfuellen });
      return o.promise;
    },
  };
}

const spitzeAnfragen = (hass) => hass.anfragen.filter(a => a.msg.type === "eeg_optimizer/get_spitze");

function antwort(extra = {}) {
  return {
    verfuegbar: true,
    monat: "2026-10",
    laufend_monat: "2026-10",
    aeltester: "2026-08",
    neuester: "2026-10",
    spitze: { kw: 7.43, start: "2026-10-14T16:45:00+00:00", vollstaendig: true },
    netzkosten: {
      verrechnet_kw: 7.43, endstufe_eur: 25.13, start_eur: 14.12,
      satz_bis_eur_kw_monat: 3.38, satz_darueber_eur_kw_monat: 6.76, stufe_kw: 10,
      start_satz_eur_kw_monat: 1.9, start_spanne_eur_kw_monat: [1.5, 2.6],
      quelle: "E-Control, Richtwerte vom 14.07.2026",
      umstellung: { fruehestens: "2027-01-01", vorgabe: "2027-12-01", spaetestens: "2028-07-01", quelle: "E-Control" },
      anteil_pct: [{ ab: "2027", pct: 40 }, { ab: "2029", pct: 50 }],
    },
    tage: [
      { datum: "2026-10-01", spitze_kw: 3.2, zeit: "2026-10-01T18:00" },
      { datum: "2026-10-14", spitze_kw: 7.43, zeit: "2026-10-14T18:45" },
    ],
    grenze_kw: 5,
    schwelle_kw: 7.43,
    laufend: { bisher_kw: 1.1, hochrechnung_kw: 2.4, rest_s: 400 },
    kappung: {
      aktiv: true, schwelle_kw: 7.43, abgeschaltet: [], fehler: null, soc_pct: 42,
      verbraucher: [
        { name: "Wallbox", entity: "switch.wb", zustand: "abgeschaltet", leistung_kw: 0,
          frei_ab: new Date(Date.now() + 10 * 60000).toISOString() },
        { name: "Wärme<pumpe>", entity: "switch.wp", zustand: "an", leistung_kw: 1.2 },
      ],
      batterie: { unterstuetzt: true, aktiv: true, reserve_pct: 10, schwelle_pct: 30, deckel_kw: 0, statistik: true, tage: 20 },
    },
    ...extra,
  };
}

function panel(hass) {
  speicher.set(P + "spitze_open", "1");
  const p = neuesPanel();
  p._hass = hass;
  return p;
}

beforeEach(() => speicher.clear());

test("offene Karte lädt höchstens alle 30 s, zugeklappt gar nicht", async () => {
  const hass = aufzeichnendesHass();
  const p = panel(hass);
  p._ensureSpitze();
  p._ensureSpitze();
  assert.equal(spitzeAnfragen(hass).length, 1);
  spitzeAnfragen(hass)[0].antworten(antwort());
  await warten();
  p._ensureSpitze();
  assert.equal(spitzeAnfragen(hass).length, 1);
  p._spitzeOpen = false;
  p._spitzeGeholt = 0;
  p._ensureSpitze();
  assert.equal(spitzeAnfragen(hass).length, 1);
});

test("zeigt Kennzahlen, Tagesspitzen, beide Stufen — Namen escaped", async () => {
  const hass = aufzeichnendesHass();
  const p = panel(hass);
  p._loadSpitze();
  spitzeAnfragen(hass)[0].antworten(antwort());
  await warten();
  const html = p._renderSpitzeKarte();
  assert.match(html, /Monatsspitze/);
  assert.match(html, /7,43&nbsp;kW/);
  assert.match(html, /Laufende Viertelstunde/);
  assert.match(html, /Stufe 1 · Batterie/);
  assert.match(html, /Hält die Reserve/);
  assert.match(html, /Stufe 2 · Verbraucher/);
  assert.match(html, /frühestens in \d+ min zurück/);
  assert.match(html, /Wärme&lt;pumpe&gt;/);
  assert.doesNotMatch(html, /Wärme<pumpe>/);
  assert.match(html, /data-cw="spitze"/);
  // Tarifdetails zugeklappt, aufgeklappt mit Umstellungstermin.
  assert.doesNotMatch(html, /1\.12\.2027/);
  p._spitzeTarifOpen = true;
  assert.match(p._renderSpitzeKarte(), /ohne eigene Meldung 1\.12\.2027/);
});

test("ohne Spitzenkappung: Hinweis mit Weg in die Einstellungen", async () => {
  const hass = aufzeichnendesHass();
  const p = panel(hass);
  p._loadSpitze();
  spitzeAnfragen(hass)[0].antworten(antwort({ kappung: null }));
  await warten();
  const html = p._renderSpitzeKarte();
  assert.match(html, /Spitzenkappung ist aus/);
  assert.match(html, /open-spitzenkappung-settings/);
});

test("Reserve 0: sagt, dass keine nötig ist, statt ein leeres Band zu zeigen", async () => {
  const hass = aufzeichnendesHass();
  const p = panel(hass);
  p._loadSpitze();
  const a = antwort();
  a.kappung.batterie = { unterstuetzt: true, aktiv: false, reserve_pct: 0, schwelle_pct: 20, deckel_kw: null, statistik: true, tage: 25 };
  spitzeAnfragen(hass)[0].antworten(a);
  await warten();
  assert.match(p._renderSpitzeKarte(), /Keine Reserve nötig/);
});

test("Blättern: ein Monat zurück fragt den Monat an, 0 springt zurück", async () => {
  const hass = aufzeichnendesHass();
  const p = panel(hass);
  p._loadSpitze();
  spitzeAnfragen(hass)[0].antworten(antwort());
  await warten();
  p._handleAction("spitze-monat", { schritt: "-1" });
  assert.equal(spitzeAnfragen(hass).at(-1).msg.monat, "2026-09");
  spitzeAnfragen(hass).at(-1).antworten(antwort({ monat: "2026-09", kappung: undefined, laufend: undefined }));
  await warten();
  const html = p._renderSpitzeKarte();
  assert.match(html, /Tage über der Grenze/);
  assert.doesNotMatch(html, /Stufe 1/);
  p._handleAction("spitze-monat", { schritt: "0" });
  assert.equal(p._spitzeMonat, null);
  // Vor dem ältesten Monat geht es nicht weiter.
  p._spitze = antwort({ monat: "2026-08" });
  p._spitzeMonat = "2026-08";
  const vorher = spitzeAnfragen(hass).length;
  p._handleAction("spitze-monat", { schritt: "-1" });
  assert.equal(spitzeAnfragen(hass).length, vorher);
});

test("Modus Aus: Spitzenkappung inaktiv statt 'wartet auf Planlauf'", async () => {
  const hass = aufzeichnendesHass();
  const p = panel(hass);
  p._loadSpitze();
  const a = antwort();
  a.kappung.steuert = false;
  a.kappung.batterie = { unterstuetzt: true };
  spitzeAnfragen(hass)[0].antworten(a);
  await warten();
  const html = p._renderSpitzeKarte();
  assert.match(html, /inaktiv \(Optimierung aus oder pausiert\)/);
  assert.doesNotMatch(html, /Planlauf/);
});

test("fehlgeschlagener Vormonat: zurück zum laufenden statt verschwinden", async () => {
  const hass = aufzeichnendesHass();
  const p = panel(hass);
  p._loadSpitze();
  spitzeAnfragen(hass)[0].antworten(antwort());
  await warten();
  p._handleAction("spitze-monat", { schritt: "-1" });
  spitzeAnfragen(hass).at(-1).antworten({ verfuegbar: false });
  await warten();
  assert.equal(p._spitzeMonat, null);
  assert.notEqual(p._renderSpitzeKarte(), "");
});

test("ohne Spitzenkappung keine Grenze im Diagramm", async () => {
  const hass = aufzeichnendesHass();
  const p = panel(hass);
  p._loadSpitze();
  spitzeAnfragen(hass)[0].antworten(antwort({ kappung: null, kappung_an: false }));
  await warten();
  const html = p._renderSpitzeKarte();
  assert.doesNotMatch(html, /Tag über der Grenze/);
  assert.match(html, /Letzte Viertelstunde/);
});
