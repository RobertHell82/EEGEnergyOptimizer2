// Live-Abo des Aktivitätsprotokolls im Panel. Ausgangsfehler (Daniel,
// 26.10.2026): jeder neue Eintrag erschien zwei- bis dreimal, weil mehrere
// Abos parallel liefen. Jeder Test hier baut eine Lage nach, in der ein
// zweites Abo entstehen oder ein Eintrag verloren gehen konnte.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  FakeHass, FakeVerbindung, neuesPanel, einhaengen, abhaengen,
  warten, eintrag, gestartet,
} from "./panel_harness.mjs";

// Array.from: die Liste stammt aus dem vm-Kontext, deepStrictEqual verlangt
// ein Array dieses Realms.
const zustaende = (p) => Array.from(p._activityLog, e => e.timestamp);

// ── Eine Anmeldung, ein Eintrag ─────────────────────────────

test("Start: genau eine Anmeldung, nach Bestätigung genau ein Abo", async () => {
  const { p, conn } = await gestartet();
  assert.equal(conn.abos.length, 1);
  assert.equal(p._activitySubPending, true);
  conn.bestaetigen();
  await warten();
  assert.equal(p._activitySubPending, false);
  assert.equal(typeof p._activityUnsub, "function");
  assert.equal(conn.lebende, 1);
});

test("Ausgangsfehler: viele hass-Updates vor der Bestätigung → ein Abo, kein Duplikat", async () => {
  const { p, conn, hass } = await gestartet();
  for (let i = 0; i < 5; i++) p.hass = hass.kopie();
  await warten();
  assert.equal(conn.abos.length, 1, "set hass darf keine zweite Anmeldung starten");
  conn.bestaetigen();
  await warten();
  const e = eintrag();
  conn.feuern(e);
  assert.deepEqual(zustaende(p), [e.timestamp]);
  assert.equal(p._activityTotal, 1);
});

test("hass-Updates nach der Bestätigung starten keine weitere Anmeldung", async () => {
  const { p, conn, hass } = await gestartet();
  conn.bestaetigen();
  await warten();
  for (let i = 0; i < 5; i++) p.hass = hass.kopie();
  await warten();
  assert.equal(conn.abos.length, 1);
});

test("mehrere Events nacheinander: jedes genau einmal, neuestes oben", async () => {
  const { p, conn } = await gestartet();
  conn.bestaetigen();
  await warten();
  const a = eintrag(), b = eintrag(), c = eintrag();
  conn.feuern(a); conn.feuern(b); conn.feuern(c);
  assert.deepEqual(zustaende(p), [c.timestamp, b.timestamp, a.timestamp]);
  assert.equal(p._activityTotal, 3);
  assert.equal(p.renders > 0, true);
});

test("zwei echte Einträge mit gleichem Zustand bleiben beide stehen", async () => {
  const { p, conn } = await gestartet();
  conn.bestaetigen();
  await warten();
  conn.feuern(eintrag({ zustand: "Normalbetrieb" }));
  conn.feuern(eintrag({ zustand: "Normalbetrieb" }));
  assert.equal(p._activityLog.length, 2);
});

test("Event ohne data wird ignoriert", async () => {
  const { p, conn } = await gestartet();
  conn.bestaetigen();
  await warten();
  conn.abos[0].cb({});
  assert.equal(p._activityLog.length, 0);
  assert.equal(p._activityTotal, 0);
});

test("ein Fehler beim Rendern beendet das Abo nicht", async () => {
  const { p, conn } = await gestartet();
  conn.bestaetigen();
  await warten();
  p._render = () => { throw new Error("render kaputt"); };
  conn.feuern(eintrag());
  p._render = () => {};
  conn.feuern(eintrag());
  assert.equal(p._activityLog.length, 2);
  assert.equal(conn.lebende, 1);
});

// ── Verbindungswechsel ──────────────────────────────────────

test("Verbindungswechsel meldet das alte Abo ab und legt genau ein neues an", async () => {
  const { p, conn, hass } = await gestartet();
  conn.bestaetigen();
  await warten();
  const neu = new FakeVerbindung("neu");
  p.hass = hass.kopie(neu);
  await warten();
  assert.equal(conn.abos[0].unsubAufrufe, 1, "altes Abo wurde nicht abgemeldet");
  assert.equal(neu.abos.length, 1);
  neu.bestaetigen();
  await warten();
  assert.equal(p._activityUnsub, neu.abos[0].unsub);
});

test("Verbindungswechsel: ein Event über den noch offenen alten Socket erscheint nicht doppelt", async () => {
  const { p, conn, hass } = await gestartet();
  conn.bestaetigen();
  await warten();
  const alterCb = conn.abos[0].cb;
  const neu = new FakeVerbindung("neu");
  p.hass = hass.kopie(neu);
  await warten();
  neu.bestaetigen();
  await warten();
  const e = eintrag();
  neu.feuern(e);
  alterCb({ data: e }); // der alte Socket liefert nach, bevor unsub ankommt
  assert.deepEqual(zustaende(p), [e.timestamp]);
  assert.equal(p._activityTotal, 1);
});

test("Verbindungswechsel während der Anmeldung: späte Bestätigung meldet sich selbst ab", async () => {
  const { p, conn, hass } = await gestartet();
  const neu = new FakeVerbindung("neu");
  p.hass = hass.kopie(neu);
  await warten();
  neu.bestaetigen();
  await warten();
  conn.bestaetigen(0); // altes Abo antwortet erst jetzt
  await warten();
  assert.equal(conn.abos[0].unsubAufrufe, 1);
  assert.equal(p._activityUnsub, neu.abos[0].unsub);
  const e = eintrag();
  conn.feuern(e);
  neu.feuern(e);
  assert.equal(p._activityLog.length, 1);
});

test("Review 1: späte Ablehnung eines alten Abos löscht das Handle des neuen nicht", async () => {
  const { p, conn, hass } = await gestartet();
  const neu = new FakeVerbindung("neu");
  p.hass = hass.kopie(neu);
  await warten();
  neu.bestaetigen();
  await warten();
  conn.ablehnen(0); // alte Verbindung verloren
  await warten();
  assert.equal(p._activityUnsub, neu.abos[0].unsub);
  for (let i = 0; i < 3; i++) p.hass = hass.kopie(neu);
  await warten();
  assert.equal(neu.abos.length, 1, "es darf kein drittes Abo entstehen");
});

test("späte Ablehnung eines alten Abos hebt die Sperre der laufenden Anmeldung nicht auf", async () => {
  const { p, conn, hass } = await gestartet();
  const neu = new FakeVerbindung("neu");
  p.hass = hass.kopie(neu);
  await warten();
  conn.ablehnen(0);
  await warten();
  assert.equal(p._activitySubPending, true);
  p.hass = hass.kopie(neu);
  await warten();
  assert.equal(neu.abos.length, 1);
});

// ── Fehlgeschlagene und hängende Anmeldungen ────────────────

test("abgelehnte Anmeldung: der nächste hass-Update versucht es erneut", async () => {
  const { p, conn, hass } = await gestartet();
  conn.ablehnen();
  await warten();
  assert.equal(p._activitySubPending, false);
  assert.equal(p._activityUnsub, null);
  p.hass = hass.kopie();
  await warten();
  assert.equal(conn.abos.length, 2);
  conn.bestaetigen();
  await warten();
  assert.equal(conn.lebende, 1);
});

test("subscribeEvents wirft synchron: keine Sperre, später neuer Versuch", async () => {
  const conn = new FakeVerbindung();
  conn.subscribeWirft = true;
  const hass = new FakeHass(conn);
  const p = neuesPanel();
  p._angehaengt = true;
  p.hass = hass;
  await warten();
  assert.equal(p._activitySubPending, false);
  conn.subscribeWirft = false;
  p.hass = hass.kopie();
  await warten();
  assert.equal(conn.abos.length, 1);
});

test("Review 2: hängende Anmeldung blockiert nur bis zum Timeout", async () => {
  const { p, conn, hass } = await gestartet();
  p.hass = hass.kopie();
  await warten();
  assert.equal(conn.abos.length, 1, "vor dem Timeout keine zweite Anmeldung");
  p._activitySubSeit -= 16000; // Antwort bleibt aus
  p.hass = hass.kopie();
  await warten();
  assert.equal(conn.abos.length, 2, "nach dem Timeout eine neue Anmeldung");
  conn.bestaetigen(1);
  await warten();
  conn.bestaetigen(0); // die hängende antwortet doch noch
  await warten();
  assert.equal(conn.abos[0].unsubAufrufe, 1);
  assert.equal(conn.lebende, 1);
  const e = eintrag();
  conn.feuern(e);
  assert.equal(p._activityLog.length, 1);
});

test("hängende Anmeldung, die nach dem Timeout abgelehnt wird, stört die neue nicht", async () => {
  const { p, conn, hass } = await gestartet();
  p._activitySubSeit -= 16000;
  p.hass = hass.kopie();
  await warten();
  conn.ablehnen(0);
  await warten();
  assert.equal(p._activitySubPending, true);
  conn.bestaetigen(1);
  await warten();
  assert.equal(p._activityUnsub, conn.abos[1].unsub);
});

// ── Abmelden ────────────────────────────────────────────────

test("unsub, das ein abgelehntes Promise liefert, erzeugt keine unbehandelte Ablehnung", async () => {
  const unbehandelt = [];
  const merken = (e) => unbehandelt.push(e);
  process.on("unhandledRejection", merken);
  try {
    const { p, conn, hass } = await gestartet();
    conn.bestaetigen();
    await warten();
    conn.unsubLehntAb = true;
    p.hass = hass.kopie(new FakeVerbindung("neu"));
    await warten();
    await new Promise(r => setTimeout(r, 20));
    assert.equal(conn.abos[0].unsubAufrufe, 1);
    assert.deepEqual(unbehandelt, []);
  } finally {
    process.off("unhandledRejection", merken);
  }
});

test("unsub, das synchron wirft, bricht den Verbindungswechsel nicht ab", async () => {
  const { p, conn, hass } = await gestartet();
  conn.bestaetigen();
  await warten();
  conn.unsubWirft = true;
  const neu = new FakeVerbindung("neu");
  p.hass = hass.kopie(neu);
  await warten();
  assert.equal(p._activityUnsub, null);
  assert.equal(neu.abos.length, 1);
});

// ── Abhängen und wieder einhängen ───────────────────────────

test("Abhängen meldet das Abo ab", async () => {
  const { p, conn } = await gestartet();
  conn.bestaetigen();
  await warten();
  abhaengen(p);
  assert.equal(conn.abos[0].unsubAufrufe, 1);
  assert.equal(p._activityUnsub, null);
  assert.equal(p._activitySubPending, false);
});

test("Review 5: hass-Updates bei abgehängtem Panel melden nicht wieder an", async () => {
  const { p, conn, hass } = await gestartet();
  conn.bestaetigen();
  await warten();
  abhaengen(p);
  for (let i = 0; i < 3; i++) p.hass = hass.kopie();
  await warten();
  assert.equal(conn.abos.length, 1);
});

test("Abhängen während der Anmeldung: späte Bestätigung meldet sich selbst ab", async () => {
  const { p, conn } = await gestartet();
  abhaengen(p);
  conn.bestaetigen();
  await warten();
  assert.equal(conn.abos[0].unsubAufrufe, 1);
  assert.equal(p._activityUnsub, null);
  conn.feuern(eintrag());
  assert.equal(p._activityLog.length, 0);
});

test("Wieder einhängen: genau ein neues Abo, Einträge wieder live", async () => {
  const { p, conn } = await gestartet();
  conn.bestaetigen();
  await warten();
  abhaengen(p);
  einhaengen(p);
  await warten();
  assert.equal(conn.abos.length, 2);
  conn.bestaetigen(1);
  await warten();
  assert.equal(conn.lebende, 1);
  conn.feuern(eintrag());
  assert.equal(p._activityLog.length, 1);
});

test("mehrfaches Ab- und Einhängen hinterlässt genau ein lebendes Abo", async () => {
  const { p, conn } = await gestartet();
  for (let i = 0; i < 4; i++) {
    abhaengen(p);
    einhaengen(p);
    await warten();
    // Bestätigungen kommen in beliebiger Reihenfolge
    for (let j = conn.abos.length - 1; j >= 0; j--) {
      if (!conn.abos[j].angenommen) conn.bestaetigen(j);
    }
    await warten();
  }
  assert.equal(conn.lebende, 1);
  conn.feuern(eintrag());
  assert.equal(p._activityLog.length, 1);
});

test("Event eines alten Abos nach dem Abhängen landet nicht im Protokoll", async () => {
  const { p, conn } = await gestartet();
  conn.bestaetigen();
  await warten();
  const cb = conn.abos[0].cb;
  abhaengen(p);
  cb({ data: eintrag() });
  assert.equal(p._activityLog.length, 0);
});

// ── Laden der Liste und Live-Events ─────────────────────────

test("Review 6: Live-Eintrag während des Ladens, nicht in der Antwort, bleibt erhalten", async () => {
  const alt = [eintrag(), eintrag()].reverse();
  const conn = new FakeVerbindung();
  const hass = new FakeHass(conn, { serverLog: alt, ladenSofort: false });
  const p = neuesPanel();
  p._angehaengt = true;
  p.hass = hass;
  await warten();
  conn.bestaetigen();
  await warten();
  const neu = eintrag();
  conn.feuern(neu);           // kommt vor der Antwort an
  hass.ladungen[0].antworten(); // Server hatte ihn beim Lesen noch nicht
  await warten();
  assert.deepEqual(zustaende(p), [neu.timestamp, ...alt.map(e => e.timestamp)]);
  assert.equal(p._activityTotal, 3);
});

test("Live-Eintrag während des Ladens, der auch in der Antwort steht, erscheint einmal", async () => {
  const serverLog = [eintrag()];
  const conn = new FakeVerbindung();
  const hass = new FakeHass(conn, { serverLog, ladenSofort: false });
  const p = neuesPanel();
  p._angehaengt = true;
  p.hass = hass;
  await warten();
  conn.bestaetigen();
  await warten();
  const neu = eintrag();
  conn.feuern(neu);
  serverLog.unshift(neu);      // Server hat ihn schon gespeichert
  hass.ladungen[0].antworten();
  await warten();
  assert.deepEqual(zustaende(p), serverLog.map(e => e.timestamp));
  assert.equal(p._activityTotal, 2);
});

test("Live-Eintrag nach dem Laden wird normal angehängt", async () => {
  const { p, conn } = await gestartet({ serverLog: [eintrag()] });
  conn.bestaetigen();
  await warten();
  assert.equal(p._activityNachzuegler, null);
  conn.feuern(eintrag());
  assert.equal(p._activityLog.length, 2);
  assert.equal(p._activityTotal, 2);
});

test("überholte Ladung: nur die zuletzt gestartete Antwort zählt", async () => {
  const conn = new FakeVerbindung();
  const serverLog = [eintrag()];
  const hass = new FakeHass(conn, { serverLog, ladenSofort: false });
  const p = neuesPanel();
  p._angehaengt = true;
  p.hass = hass;
  await warten();
  const zweite = p._loadActivityLog();
  serverLog.unshift(eintrag());
  hass.ladungen[1].antworten();
  await zweite;
  hass.ladungen[0].antworten(); // ältere Antwort kommt zuletzt
  await warten();
  assert.equal(p._activityLog.length, 2);
  assert.equal(p._activityTotal, 2);
});

test("fehlgeschlagene Ladung beendet das Sammeln der Nachzügler", async () => {
  const conn = new FakeVerbindung();
  const hass = new FakeHass(conn, { ladenSofort: false });
  const p = neuesPanel();
  p._angehaengt = true;
  p.hass = hass;
  await warten();
  hass.ladungen[0].fehler(new Error("weg"));
  await warten();
  assert.equal(p._activityNachzuegler, null);
});

test("„Mehr laden“ nach Live-Einträgen fragt ab der richtigen Stelle", async () => {
  const serverLog = Array.from({ length: 150 }, () => eintrag()).reverse();
  const { p, conn, hass } = await gestartet({ serverLog });
  conn.bestaetigen();
  await warten();
  assert.equal(p._activityLog.length, 100);
  assert.equal(p._activityHasMore, true);
  const neu = eintrag();
  serverLog.unshift(neu);
  conn.feuern(neu);
  const gefragt = [];
  const orig = hass.callWS.bind(hass);
  p._hass.callWS = (msg) => { gefragt.push(msg); return orig(msg); };
  await p._loadMoreActivity();
  assert.equal(gefragt[0].offset, 101);
  assert.equal(p._activityLog.length, 151);
  assert.equal(new Set(zustaende(p)).size, 151, "kein Eintrag doppelt");
});
