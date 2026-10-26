"""Panel-Tests in JavaScript (tests/js/*.test.mjs) über Node ausführen.

Das Panel ist reines JS ohne Build-Schritt; seine Tests laufen mit dem in
Node eingebauten Test-Runner gegen die echte Datei. Ohne Node wird
übersprungen, damit pytest auf einer Maschine ohne Node nicht rot wird.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

JS_TESTS = sorted((Path(__file__).parent / "js").glob("*.test.mjs"))
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="Node.js nicht installiert")
@pytest.mark.parametrize("datei", JS_TESTS, ids=lambda p: p.name)
def test_panel_js(datei: Path) -> None:
    ergebnis = subprocess.run(
        [NODE, "--test", str(datei)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert ergebnis.returncode == 0, ergebnis.stdout + ergebnis.stderr
