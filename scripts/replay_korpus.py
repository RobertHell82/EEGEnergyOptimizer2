"""Replay-Korpus: archivierte Fahrpläne nachrechnen und gegen Regeln prüfen.

Zwei Befehle, beide mit einem Fahrplan-Archiv als Quelle — dem ZIP aus
Einstellungen → System → Fahrplan-Archiv (``plaene.zip``) oder einem
entpackten Ordner davon:

    python scripts/replay_korpus.py pruefen <archiv> [--jeder N] [--varianten a,b]
        Rechnet jeden N-ten Plan in jeder Variante nach und listet
        Regelverstöße und den Geldvorteil gegen den Standardbetrieb.
        Schreibt nichts — der Weg, ein frisches Archiv durchzusehen.

    python scripts/replay_korpus.py aufnehmen <archiv> --kuerzel fronius-a [--anzahl 12]
                                              [--eintraege 2026-10-28/1113,…]
        Wählt über die Tage verteilt Pläne aus (morgens, mittags, abends,
        nachts), dazu die ausdrücklich genannten (etwa die eines Fundes),
        anonymisiert sie und legt sie nach tests/fixtures/replay/.
        Von dort rechnet tests/test_replay_korpus.py sie bei jedem Lauf.

Läuft aus dem Repo-Wurzelverzeichnis (wegen der HA-Stubs in conftest.py).
"""

from __future__ import annotations

import argparse
import gzip
import json
import pathlib
import sys
import tempfile
import zipfile

WURZEL = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL))

import conftest  # noqa: E402,F401 — HA-Stubs, bevor die Integration geladen wird
from custom_components.eeg_energy_optimizer import schedule as sched  # noqa: E402
from tests import replay_korpus as rk  # noqa: E402

# Uhrzeiten, um die herum je Tag ein Plan genommen wird.
ZIELSTUNDEN = (5, 11, 16, 21)


def _eintraege(quelle: pathlib.Path) -> list[pathlib.Path]:
    if quelle.is_file():
        ziel = pathlib.Path(tempfile.mkdtemp(prefix="replay_"))
        with zipfile.ZipFile(quelle) as z:
            # Nur die Pläne — und keine Pfade, die aus dem Ordner führen.
            for name in z.namelist():
                if name.startswith("plaene/") and name.endswith(".json.gz") and ".." not in name:
                    z.extract(name, ziel)
        quelle = ziel
    return sorted(quelle.glob("plaene/*/*.json.gz"))


def _nachrechnen(eintrag: dict, variante: str):
    inputs = rk.VARIANTEN[variante](rk.inputs_aus_eintrag(eintrag))
    plan = sched.solve(inputs)
    return inputs, plan


def pruefen(args: argparse.Namespace) -> int:
    varianten = args.varianten.split(",") if args.varianten else list(rk.VARIANTEN)
    dateien = _eintraege(pathlib.Path(args.archiv))[:: args.jeder]
    schlecht = 0
    for pfad in dateien:
        eintrag = rk.eintrag_laden(pfad)
        name = f"{pfad.parent.name} {pfad.name[:6]}"
        if "eingaben" not in eintrag or eintrag["plan"].get("error"):
            print(f"{name}  übersprungen (ohne Eingaben oder Fehlerlauf)")
            continue
        for variante in varianten:
            try:
                inputs, plan = _nachrechnen(eintrag, variante)
            except Exception as fehler:  # noqa: BLE001 — genau das wollen wir sehen
                schlecht += 1
                print(f"{name}  {variante:20s} KEIN PLAN: {type(fehler).__name__}: {fehler}")
                continue
            verstoesse = rk.verstoesse(inputs, plan)
            vorteil = (plan.get("gewinn") or {}).get("vorteil")
            text = f"{vorteil:+.3f} €" if isinstance(vorteil, (int, float)) else "   –   "
            if verstoesse:
                schlecht += 1
            print(f"{name}  {variante:20s} {text}  {len(verstoesse)} Verstöße")
            for v in verstoesse[: args.zeilen]:
                print(f"      {v}")
    print(f"\n{schlecht} Läufe mit Verstößen oder ohne Plan")
    return 1 if schlecht else 0


def _auswaehlen(dateien: list[pathlib.Path], anzahl: int) -> list[pathlib.Path]:
    """Je Tag die Pläne nächst den Zielstunden, dann gleichmäßig ausgedünnt."""
    kandidaten: list[pathlib.Path] = []
    nach_tag: dict[str, list[pathlib.Path]] = {}
    for pfad in dateien:
        nach_tag.setdefault(pfad.parent.name, []).append(pfad)
    for tag in sorted(nach_tag):
        for stunde in ZIELSTUNDEN:
            def abstand(p: pathlib.Path) -> int:
                minuten = int(p.name[:2]) * 60 + int(p.name[2:4])
                return abs(minuten - stunde * 60)

            bester = min(nach_tag[tag], key=abstand)
            if abstand(bester) <= 90 and bester not in kandidaten:
                kandidaten.append(bester)
    if len(kandidaten) <= anzahl:
        return kandidaten
    schritt = len(kandidaten) / anzahl
    return [kandidaten[int(i * schritt)] for i in range(anzahl)]


def aufnehmen(args: argparse.Namespace) -> int:
    ziel = rk.KORPUS
    ziel.mkdir(parents=True, exist_ok=True)
    brauchbar = []
    for pfad in _eintraege(pathlib.Path(args.archiv)):
        eintrag = rk.eintrag_laden(pfad)
        if "eingaben" in eintrag and not eintrag["plan"].get("error"):
            brauchbar.append(pfad)
    auswahl = _auswaehlen(brauchbar, args.anzahl)
    # Ausdrücklich genannte Einträge (z. B. die eines Fundes) zusätzlich.
    for name in filter(None, args.eintraege.split(",")):
        treffer = [p for p in brauchbar if f"{p.parent.name}/{p.name}".startswith(name)]
        if not treffer:
            print(f"{name}: nicht im Archiv (oder ohne Eingaben)")
        auswahl.extend(p for p in treffer[:1] if p not in auswahl)
    for pfad in auswahl:
        eintrag = rk.eintrag_laden(pfad)
        rk.inputs_aus_eintrag(eintrag)  # wirft, wenn das Raster nicht passt
        korpus = rk.anonymisieren(eintrag, args.kuerzel)
        # Benannt nach dem Archiv-Eintrag (Tag/HHMMSS), nicht nach dem auf
        # 15 min gerundeten Planstart — zwei Läufe im selben Viertel
        # bekämen sonst denselben Namen.
        datei = ziel / f"{args.kuerzel}_{pfad.parent.name}_{pfad.name[:6]}.json.gz"
        roh = json.dumps(korpus, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        # mtime=0: dieselbe Aufnahme ergibt dieselbe Datei, kein Git-Rauschen.
        with open(datei, "wb") as roh_datei, gzip.GzipFile(
            fileobj=roh_datei, mode="wb", mtime=0
        ) as f:
            f.write(roh)
        print(f"{datei.relative_to(WURZEL)}  {datei.stat().st_size // 1024} KB")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    unter = parser.add_subparsers(dest="befehl", required=True)
    p = unter.add_parser("pruefen")
    p.add_argument("archiv")
    p.add_argument("--jeder", type=int, default=1)
    p.add_argument("--varianten", default="")
    p.add_argument("--zeilen", type=int, default=5)
    p.set_defaults(fn=pruefen)
    a = unter.add_parser("aufnehmen")
    a.add_argument("archiv")
    a.add_argument("--kuerzel", required=True)
    a.add_argument("--anzahl", type=int, default=12)
    a.add_argument("--eintraege", default="",
                   help="zusätzlich diese Einträge, z. B. 2026-10-28/1113,2026-11-01/1944")
    a.set_defaults(fn=aufnehmen)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
