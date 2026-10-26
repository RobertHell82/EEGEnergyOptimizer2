// Einspeise- und Bezugskarte, Ausblenden von Dashboard-Karten (aus PR #8,
// mit den Korrekturen aus dessen Review). Geprüft wird das echte Panel.
//
// Das Panel legt den Bilanztag in Ortszeit an (04:00); die Zeitstempel unten
// tragen +01:00. Deshalb gilt hier Wiener Zeit, egal wo der Test läuft.
process.env.TZ = "Europe/Vienna";
import { test, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { neuesPanel, offen, speicher, warten } from "./panel_harness.mjs";

const P = "eeg_optimizer_panel_";

// hass, das jede Anfrage aufzeichnet und vom Test beantworten lässt.
function aufzeichnendesHass() {
  const anfragen = [];
  return {
    anfragen,
    connection: null,
    callWS(msg) {
      const o = offen();
      anfragen.push({ msg, antworten: o.erfuellen, fehler: o.ablehnen });
      return o.promise;
    },
  };
}

function panelMit(hass, vorlieben = {}) {
  for (const [k, v] of Object.entries(vorlieben)) speicher.set(P + k, v);
  const p = neuesPanel();
  p._hass = hass;
  return p;
}

const karten = (hass, art) => hass.anfragen.filter(a => a.msg.type === `eeg_optimizer/get_${art}`);

beforeEach(() => speicher.clear());

// ── Lader ───────────────────────────────────────────────────

for (const art of ["einspeisung", "bezug"]) {
  test(`${art}: offene, sichtbare Karte lädt einmal je Minute`, async () => {
    const hass = aufzeichnendesHass();
    const p = panelMit(hass, { [`${art}_open`]: "1" });
    p._ensureKarte(art);
    p._ensureKarte(art);
    assert.equal(karten(hass, art).length, 1, "während der Anfrage keine zweite");
    karten(hass, art)[0].antworten({ verfuegbar: true, zeitraum: "monat", reihe: [] });
    await warten();
    for (let i = 0; i < 10; i++) p._ensureKarte(art);
    assert.equal(karten(hass, art).length, 1);
    p[`_${art}Geholt`] -= 61000;
    p._ensureKarte(art);
    assert.equal(karten(hass, art).length, 2, "nach einer Minute neu");
  });

  test(`${art}: Review 1 — {verfuegbar:false} löst keine Anfrageschleife aus`, async () => {
    const hass = aufzeichnendesHass();
    const p = panelMit(hass, { [`${art}_open`]: "1" });
    p._ensureKarte(art);
    karten(hass, art)[0].antworten({ verfuegbar: false }); // ohne zeitraum, wie das Backend
    await warten();
    for (let i = 0; i < 20; i++) p._ensureKarte(art); // jeder Render ruft das
    assert.equal(karten(hass, art).length, 1);
    assert.equal(p[`_${art}`].verfuegbar, false);
  });

  test(`${art}: ein Fehler löst ebenfalls keine Schleife aus`, async () => {
    const hass = aufzeichnendesHass();
    const p = panelMit(hass, { [`${art}_open`]: "1" });
    p._ensureKarte(art);
    karten(hass, art)[0].fehler(new Error("weg"));
    await warten();
    for (let i = 0; i < 20; i++) p._ensureKarte(art);
    assert.equal(karten(hass, art).length, 1);
  });

  test(`${art}: Review 7 — zugeklappte Karte lädt nicht`, () => {
    const hass = aufzeichnendesHass();
    const p = panelMit(hass, { [`${art}_open`]: "0" });
    p._ensureKarte(art);
    assert.equal(karten(hass, art).length, 0);
  });

  test(`${art}: Review 7 — ausgeblendete Karte lädt nicht`, () => {
    const hass = aufzeichnendesHass();
    const p = panelMit(hass, { [`${art}_open`]: "1", [`ansicht_${art}`]: "0" });
    p._ensureKarte(art);
    assert.equal(karten(hass, art).length, 0);
  });

  test(`${art}: überholte Antwort nach Zeitraumwechsel wird verworfen`, async () => {
    const hass = aufzeichnendesHass();
    const p = panelMit(hass, { [`${art}_open`]: "1", [`${art}_zeitraum`]: "monat" });
    p._ensureKarte(art);
    p._chartBereichSetzen(art, "jahr");
    const [alt, neu] = karten(hass, art);
    assert.equal(alt.msg.zeitraum, "monat");
    assert.equal(neu.msg.zeitraum, "jahr");
    neu.antworten({ verfuegbar: true, zeitraum: "jahr", reihe: [] });
    await warten();
    alt.antworten({ verfuegbar: true, zeitraum: "monat", reihe: [] });
    await warten();
    assert.equal(p[`_${art}`].zeitraum, "jahr");
    assert.equal(speicher.get(`${P}${art}_zeitraum`), "jahr");
  });

  test(`${art}: Blättern schickt das Datum mit`, async () => {
    const hass = aufzeichnendesHass();
    const p = panelMit(hass, { [`${art}_open`]: "1", [`${art}_zeitraum`]: "monat" });
    p._ensureKarte(art);
    karten(hass, art)[0].antworten({
      verfuegbar: true, zeitraum: "monat", bilanztag: "2026-10-26", auswahl: "2026-10",
      aeltester: "2026-01-03", neuester: "2026-10-26", reihe: [],
    });
    await warten();
    p._chartDatumSetzen(art, -1);
    const letzte = karten(hass, art).at(-1);
    assert.equal(letzte.msg.datum, "2026-09");
    p._chartDatumSetzen(art, 0);
    assert.equal(karten(hass, art).at(-1).msg.datum, undefined, "zurück zum laufenden");
  });
}

// ── Karten ausblenden (Review 5) ────────────────────────────

test("Review 5: ohne Vorliebe sind alle Karten sichtbar", () => {
  const p = panelMit(aufzeichnendesHass());
  for (const [id] of p.constructor.ANSICHT_KARTEN) assert.equal(p._zeigt(id), true, id);
});

test("Review 5: Ausblenden merkt sich das Gerät, ohne save_config", () => {
  const hass = aufzeichnendesHass();
  const p = panelMit(hass);
  p._ansichtUmschalten("bezug");
  assert.equal(p._zeigt("bezug"), false);
  assert.equal(speicher.get(`${P}ansicht_bezug`), "0");
  assert.equal(hass.anfragen.length, 0, "keine WebSocket-Anfrage, kein Neuladen");
  assert.ok(p.renders > 0);
  p._ansichtUmschalten("bezug");
  assert.equal(p._zeigt("bezug"), true);
});

test("Review 5: unbekannte Karte wird ignoriert", () => {
  const p = panelMit(aufzeichnendesHass());
  p._ansichtUmschalten("gibtsnicht");
  assert.equal(speicher.has(`${P}ansicht_gibtsnicht`), false);
});

test("Review 5: Einstellungskarte schaltet über toggle-ansicht, nicht über die Config", () => {
  const p = panelMit(aufzeichnendesHass(), { ansicht_profil: "0" });
  const html = p._ansichtKarte();
  assert.equal((html.match(/data-action="toggle-ansicht"/g) || []).length, p.constructor.ANSICHT_KARTEN.length);
  assert.doesNotMatch(html, /toggle-settings-feature/);
  const profil = html.split("<label").find(s => s.includes('data-karte="profil"'));
  assert.doesNotMatch(profil, /checked/);
  const bezug = html.split("<label").find(s => s.includes('data-karte="bezug"'));
  assert.match(bezug, /checked/);
});

test("Review 5: der Klick in den Einstellungen landet beim Umschalten", () => {
  const p = panelMit(aufzeichnendesHass());
  let umgeschaltet = null;
  p._ansichtUmschalten = (id) => { umgeschaltet = id; };
  const quelle = p.constructor.toString();
  assert.match(quelle, /case "toggle-ansicht":\s*this\._ansichtUmschalten\(dataset\?\.karte\)/);
  p._ansichtUmschalten("plan");
  assert.equal(umgeschaltet, "plan");
});

// ── Bezugskarte ─────────────────────────────────────────────

const bezugDaten = (felder = {}) => ({
  verfuegbar: true, zeitraum: "heute", bilanztag: "2026-10-26", auswahl: "2026-10-26",
  aeltester: "2026-10-01", neuester: "2026-10-26", angefragt: null,
  kennzahlen: { bezug_kwh: 0.75, kosten: 0.2, kosten_je_kwh: 0.2667, ohne_preis_kwh: 0,
    spitze_kw: 2.0, spitze_zeit: "2026-10-26T06:00" },
  kosten_seit: "2026-10-01", erster_tag: "2026-10-01",
  reihe: [
    { t: "2026-10-26T06:00:00+01:00", bezug: 0.5, kosten: 0.15 },
    { t: "2026-10-26T06:15:00+01:00", bezug: 0.25, kosten: 0.05 },
  ],
  ...felder,
});

test("Bezugskarte zugeklappt ohne Daten zeigt den Kopf", () => {
  const p = panelMit(aufzeichnendesHass(), { bezug_open: "0" });
  const html = p._renderBezugKarte();
  assert.match(html, /Bezug/);
  assert.match(html, /toggle-bezug-karte/);
});

test("Bezugskarte ohne Bilanz verschwindet", () => {
  const p = panelMit(aufzeichnendesHass(), { bezug_open: "1" });
  p._bezug = { verfuegbar: false, zeitraum: "heute" };
  assert.equal(p._renderBezugKarte(), "");
});

test("Bezugskarte offen, noch ohne Daten: nichts statt leerer Kacheln", () => {
  const p = panelMit(aufzeichnendesHass(), { bezug_open: "1" });
  assert.equal(p._renderBezugKarte(), "");
});

test("Bezugskarte nennt die Menge ohne Preis", () => {
  const p = panelMit(aufzeichnendesHass(), { bezug_open: "1", bezug_zeitraum: "heute" });
  p._bezug = bezugDaten({ kennzahlen: { ...bezugDaten().kennzahlen, ohne_preis_kwh: 5 } });
  assert.match(p._renderBezugKarte(), /5,0 kWh ohne erfassten Bezugspreis/);
});

test("Bezugskarte ohne Menge ohne Preis: kein Hinweis darauf", () => {
  const p = panelMit(aufzeichnendesHass(), { bezug_open: "1", bezug_zeitraum: "heute" });
  p._bezug = bezugDaten();
  assert.doesNotMatch(p._renderBezugKarte(), /ohne erfassten Bezugspreis/);
});

// ── Gemeinsames Balkengerüst (Review 8) ─────────────────────

test("Bezug-Diagramm: Viertelstundenraster, ein Balken je Bezug", () => {
  const p = panelMit(aufzeichnendesHass());
  const html = p._bezugChart(bezugDaten());
  assert.match(html, /data-einsp-n="96"/);
  assert.match(html, /data-auswahl="bezug"/);
  assert.equal((html.match(/fill="#e53935"/g) || []).length, 2);
});

test("Bezug-Diagramm: angetippter Balken zeigt Kosten und Preis", () => {
  const p = panelMit(aufzeichnendesHass());
  p._bezugAuswahl = 8; // 06:00 im Bilanztag ab 04:00
  const html = p._bezugChart(bezugDaten());
  assert.match(html, /0,50 kWh bezogen/);
  assert.match(html, /0,15 € Kosten/);
  assert.match(html, /30,0 ct je kWh/);
});

test("Bezug-Diagramm: Tag ohne Viertelstunden nennt den Grund", () => {
  const p = panelMit(aufzeichnendesHass());
  const html = p._bezugChart(bezugDaten({ reihe: [] }));
  assert.match(html, /nur die Tagessumme/);
});

test("Monatsraster: Februar 2026 hat 28 Tage", () => {
  const p = panelMit(aufzeichnendesHass());
  const html = p._bezugChart(bezugDaten({
    zeitraum: "monat", auswahl: "2026-02",
    reihe: [{ datum: "2026-02-03", bezug: 4, kosten: 1.2 }],
  }));
  assert.match(html, /data-einsp-n="28"/);
});

test("Jahresraster: 12 Monate, Balken an der richtigen Stelle", () => {
  const p = panelMit(aufzeichnendesHass());
  const g = p._balkenRahmen({ zeitraum: "jahr", reihe: [] }, "bezug", 10);
  assert.equal(g.n, 12);
  assert.equal(g.index({ monat: "2026-10" }), 9);
});

test("Gesamt: ein Jahr je Balken, Index ist die Position", () => {
  const p = panelMit(aufzeichnendesHass());
  const g = p._balkenRahmen({ zeitraum: "gesamt", reihe: [{ jahr: "2025" }, { jahr: "2026" }] }, "bezug", 1);
  assert.equal(g.n, 2);
  assert.equal(g.index({ jahr: "2026" }, 1), 1);
  assert.ok(g.bw <= 64, "breitester Balken 64 px");
});

const einspDaten = (felder = {}) => ({
  verfuegbar: true, zeitraum: "heute", bilanztag: "2026-10-26", auswahl: "2026-10-26",
  gemeinschaft: true, quotenmodus: false, kennzahlen: { export_kwh: 1.0 },
  reihe: [
    { t: "2026-10-26T12:00:00+01:00", export: 0.6, batterie: 0, eeg: 0.4, bedarf: true },
    { t: "2026-10-26T20:00:00+01:00", export: 0.4, batterie: 0.4, eeg: 0, bedarf: false },
  ],
  ...felder,
});

test("Einspeise-Diagramm nach dem Umbau: EEG-Anteil, Rest, Batterie-Schraffur, Bedarfsband", () => {
  const p = panelMit(aufzeichnendesHass());
  const html = p._einspeisungChart(einspDaten());
  assert.match(html, /data-auswahl="einspeisung"/);
  assert.match(html, /data-einsp-n="96"/);
  assert.equal((html.match(/fill="#43a047"\/>/g) || []).length, 1, "EEG-Anteil");
  assert.equal((html.match(/fill="#90a4ae"/g) || []).length, 2, "Rest");
  assert.equal((html.match(/fill="url\(#einsp-schraffur\)"/g) || []).length, 1, "aus der Batterie");
  assert.match(html, /fill-opacity="0.08"/, "Bedarf der Gemeinschaft hinterlegt");
  assert.match(html, /<pattern id="einsp-schraffur"/);
  assert.match(html, /bei Gemeinschaftsbedarf/);
});

test("Einspeise-Diagramm ohne Gemeinschaft: ein Balken in Blau", () => {
  const p = panelMit(aufzeichnendesHass());
  const html = p._einspeisungChart(einspDaten({ gemeinschaft: false }));
  assert.equal((html.match(/fill="#1e88e5"/g) || []).length, 2);
  assert.doesNotMatch(html, /fill="#43a047"\/>/);
});

test("Einspeise-Diagramm: Details des angetippten Balkens", () => {
  const p = panelMit(aufzeichnendesHass());
  p._einspeisungAuswahl = 64; // 20:00
  const html = p._einspeisungChart(einspDaten());
  assert.match(html, /0,40 kWh eingespeist/);
  assert.match(html, /davon 0,40 kWh aus der Batterie/);
});

test("Achsen: Uhrzeiten ab 04:00 am Tag, Monatsnamen im Jahr", () => {
  const p = panelMit(aufzeichnendesHass());
  const tag = p._einspeisungChart(einspDaten());
  assert.match(tag, />04:00</);
  assert.match(tag, />08:00</);
  const jahr = p._bezugChart(bezugDaten({ zeitraum: "jahr", reihe: [{ monat: "2026-03", bezug: 50, kosten: 15 }] }));
  assert.match(jahr, />Mär</);
  assert.match(jahr, />Dez</);
});
