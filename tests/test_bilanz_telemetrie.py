"""Tests für die Bilanztage an die EEG-Statistik (bilanz_telemetrie.py).

Die zwei Zusagen dieses Moduls:

* Es gehen **keine Geldbeträge** hinaus — nur die Felder, die ``tag_payload``
  ausdrücklich aufnimmt.
* Jeder Tag kommt beim Backend an, auch nach Ausfällen, und nach „Daten
  löschen" (neue Kennung) kommt das ganze Archiv neu.
"""

import pytest

from custom_components.eeg_energy_optimizer.bilanz_telemetrie import (
    PAKET_TAGE,
    BilanzVersand,
    tag_payload,
)

from .test_bilanz import _bilanz, _inputs, _slot, _tag_mit  # noqa: F401

GELDFELDER = {"erloes", "vermieden", "pv_ersparnis", "opt_vorteil", "waerme",
              "ref_erloes", "ist_summe", "ref_summe", "vorteil_roh"}


class _Reporter:
    def __init__(self, enabled=True, configured=True, fehler_ab=None):
        self.is_enabled = enabled
        self.is_configured = configured
        self.pakete: list[list[dict]] = []
        self._fehler_ab = fehler_ab

    async def send_balance(self, tage):
        if self._fehler_ab is not None and len(self.pakete) >= self._fehler_ab:
            raise RuntimeError("Backend weg")
        self.pakete.append(tage)


def _versand() -> BilanzVersand:
    v = BilanzVersand(None, "test")
    v._store = None
    return v


def _tag(**werte):
    tag = {"export_kwh": 4.0, "eeg_kwh": 1.0, "pv_kwh": 20.0, "bezug_kwh": 2.0,
           "haus_kwh": 9.0, "heizstab_kwh": 0.0, "eigen_kwh": 11.0,
           "ein_anteil": 1.0, "batterie_export_kwh": 1.5, "kein_eingriff": False,
           "erloes": 0.42, "vermieden": 2.5, "pv_ersparnis": 2.9, "opt_vorteil": 0.1,
           "ref_export_kwh": 5.0, "ref_eeg_kwh": 0.3, "ref_batterie_export_kwh": 0.0,
           "ref_erloes": 0.33, "quotenmodus": False}
    tag.update(werte)
    return tag


def test_payload_traegt_keine_geldbetraege():
    payload = tag_payload("2026-09-28", _tag())

    assert not GELDFELDER & set(payload)
    assert not any(isinstance(v, float) and v in (0.42, 2.5, 2.9, 0.1, 0.33)
                   for v in payload.values())
    assert payload["export_kwh"] == 4.0
    assert payload["battery_export_kwh"] == 1.5
    assert payload["community_kwh"] == 1.0
    assert payload["ref_community_kwh"] == 0.3
    assert payload["no_intervention"] is False


def test_fehlende_felder_gehen_als_none_statt_null():
    """Ein Tag vor 2.1.26 kennt die Batterie-Einspeisung nicht — eine 0 wäre
    eine Messung, die es nicht gab."""
    alt = _tag()
    for feld in ("batterie_export_kwh", "haus_kwh", "ref_export_kwh",
                 "ref_eeg_kwh", "ref_batterie_export_kwh", "quotenmodus"):
        del alt[feld]

    payload = tag_payload("2026-08-01", alt)

    assert payload["battery_export_kwh"] is None
    assert payload["house_kwh"] is None
    assert payload["ref_export_kwh"] is None
    # Ohne Referenz ist „kein Eingriff" keine Aussage.
    assert payload["no_intervention"] is None
    assert payload["quota_mode"] is None


async def test_archiv_geht_in_paketen_und_nur_einmal():
    tage = {f"2025-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}": _tag() for i in range(250)}
    reporter = _Reporter()
    v = _versand()

    assert await v.async_senden(reporter, "id-1", tage) == 250
    assert [len(p) for p in reporter.pakete] == [PAKET_TAGE, PAKET_TAGE, 50]

    # Zweiter Durchlauf: nichts Neues — nichts gesendet.
    assert await v.async_senden(reporter, "id-1", tage) == 0
    tage["2026-01-01"] = _tag()
    assert await v.async_senden(reporter, "id-1", tage) == 1
    assert reporter.pakete[-1][0]["date"] == "2026-01-01"


async def test_ausgefallenes_paket_kommt_beim_naechsten_mal():
    tage = {f"2025-01-{i + 1:02d}": _tag() for i in range(20)}
    v = _versand()

    await v.async_senden(_Reporter(fehler_ab=0), "id-1", tage)
    reporter = _Reporter()

    assert await v.async_senden(reporter, "id-1", tage) == 20


async def test_ohne_einwilligung_wird_nichts_vermerkt():
    tage = {"2026-09-28": _tag()}
    v = _versand()

    assert await v.async_senden(_Reporter(enabled=False), "id-1", tage) == 0
    assert await v.async_senden(_Reporter(configured=False), "id-1", tage) == 0
    assert await v.async_senden(_Reporter(), None, tage) == 0
    # Später eingeschaltet: der Tag ist nicht verloren.
    assert await v.async_senden(_Reporter(), "id-1", tage) == 1


async def test_neue_kennung_schickt_das_archiv_neu():
    """Nach „Daten löschen" kennt das Backend keinen Tag der Anlage mehr."""
    tage = {"2026-09-27": _tag(), "2026-09-28": _tag()}
    v = _versand()
    await v.async_senden(_Reporter(), "id-alt", tage)

    assert await v.async_senden(_Reporter(), "id-neu", tage) == 2


async def test_merker_waechst_nicht_ueber_das_archiv():
    v = _versand()
    await v.async_senden(_Reporter(), "id-1", {"2025-01-01": _tag(), "2026-09-28": _tag()})
    await v.async_senden(_Reporter(), "id-1", {"2026-09-28": _tag()})

    assert v._gesendet == {"2026-09-28"}


def test_bilanz_merkt_sich_den_quotenmodus():
    b = _bilanz()
    tag = _tag_mit({48: _slot(export=1.0, basis=0.08, kwp=0.26, s=900.0)})
    tarif = {"name": "EEG", "anteil": 1.0, "tag": 0.12, "nacht": 0.12}

    mit_quote = b.bewerte_tag(tag, _inputs(eeg_tarife=[{**tarif, "quote_tag": 0.5, "quote_nacht": 0.1}]))
    ohne = b.bewerte_tag(tag, _inputs(eeg_tarife=[tarif]))
    unbekannt = b.bewerte_tag(tag, None)

    assert mit_quote["quotenmodus"] is True
    assert ohne["quotenmodus"] is False
    assert unbekannt["quotenmodus"] is None


def test_quotenmodus_wird_nicht_ueber_den_monat_summiert():
    from custom_components.eeg_energy_optimizer import bilanz as bilanz_modul

    assert "quotenmodus" in bilanz_modul.NICHT_SUMMIERBAR


@pytest.mark.parametrize("wert", [True, "ja", None])
def test_zahlenfelder_nehmen_nur_zahlen(wert):
    payload = tag_payload("2026-09-28", _tag(export_kwh=wert))
    assert payload["export_kwh"] is None
