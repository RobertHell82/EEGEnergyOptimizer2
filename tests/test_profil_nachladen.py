"""Tests für das Nachladen des Verbrauchsprofils nach dem Statistik-Backfill.

Hintergrund (13.09.2026, SolaX-Testanlage): Nach jedem Reload — und ein
Reload passiert bei jedem Speichern im Panel — baute die Integration das
Verbrauchsprofil aus den Statistiken, wie sie gerade dastanden, und startete
erst danach den Backfill, der sie aus den Quellsensoren neu rechnet. Die
Nachfass-Schleife brach ab, sobald ``stats_count > 0`` war; dieser Wert stand
aber schon vom ersten Laden. Ergebnis: 15 Minuten (bis zum Slow-Timer) plante
der Fahrplan mit 43,3 statt 17,5 kWh Tagesverbrauch — er sah keinen
PV-Überschuss und unterließ jede Entladung in die Gemeinschaft, während er
echt steuerte.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.eeg_energy_optimizer.sensor import (
    profil_fingerabdruck,
    profil_nachladen,
    warte_auf_recorder,
)


class FakeCoordinator:
    """Coordinator, dessen Profil sich nach einer gewissen Zahl Läufe ändert.

    Bildet den realen Ablauf nach: Der Backfill-Import wird erst sichtbar,
    wenn die Recorder-Queue ihn geschrieben hat — bis dahin liefert jeder
    Refresh unverändert die alten Werte.
    """

    def __init__(self, *, stats_count: int, watt_alt: float, watt_neu: float | None = None,
                 sichtbar_ab_lauf: int | None = None) -> None:
        self.stats_count = stats_count
        self._watt_alt = watt_alt
        self._watt_neu = watt_neu
        self._sichtbar_ab_lauf = sichtbar_ab_lauf
        self.laeufe = 0
        self._setze(watt_alt)

    def _setze(self, watt: float) -> None:
        self.bucket_avg = {"wt": {0: watt}, "we": {0: watt}}

    async def refresh(self) -> None:
        self.laeufe += 1
        if self._sichtbar_ab_lauf is not None and self.laeufe >= self._sichtbar_ab_lauf:
            if self._watt_neu is not None:
                self._setze(self._watt_neu)
            if self.stats_count == 0:
                self.stats_count = 671


@pytest.fixture
def sleep_mock():
    """asyncio.sleep ersetzen — die Tests sollen nicht real warten."""
    return AsyncMock()


class TestProfilFingerabdruck:
    def test_gleiche_anzahl_andere_werte_ergibt_anderen_fingerabdruck(self):
        """Der Kern des Bugs: stats_count bleibt gleich, die Werte nicht."""
        alt = FakeCoordinator(stats_count=671, watt_alt=4493.0)
        neu = FakeCoordinator(stats_count=671, watt_alt=928.0)
        assert alt.stats_count == neu.stats_count
        assert profil_fingerabdruck(alt) != profil_fingerabdruck(neu)

    def test_ohne_daten_ist_der_fingerabdruck_null(self):
        leer = MagicMock()
        leer.bucket_avg = {}
        assert profil_fingerabdruck(leer) == 0.0

    def test_fehlendes_attribut_bricht_nicht(self):
        assert profil_fingerabdruck(object()) == 0.0


class TestProfilNachladen:
    @pytest.mark.asyncio
    async def test_queue_durch_und_daten_da_laedt_genau_einmal(self, sleep_mock):
        """Steht der Import nachweislich in der Datenbank, genügt ein Lauf."""
        coord = FakeCoordinator(stats_count=671, watt_alt=928.0)
        laeufe = await profil_nachladen(
            coord, coord.refresh, queue_durch=True, vorher=99999.0, sleep=sleep_mock
        )
        assert laeufe == 1
        assert coord.laeufe == 1
        sleep_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_alte_werte_halten_das_nachfassen_nicht_auf(self, sleep_mock):
        """Regression: stats_count > 0 allein darf nicht als „fertig" gelten.

        Ohne abwartbaren Recorder liefern die ersten Läufe noch die alten
        Zahlen. Die frühere Fassung brach hier nach dem ersten Lauf ab.
        """
        coord = FakeCoordinator(
            stats_count=671, watt_alt=4493.0, watt_neu=928.0, sichtbar_ab_lauf=3
        )
        vorher = profil_fingerabdruck(coord)

        laeufe = await profil_nachladen(
            coord, coord.refresh, queue_durch=False, vorher=vorher, sleep=sleep_mock
        )

        assert laeufe == 3
        assert profil_fingerabdruck(coord) != vorher
        assert sleep_mock.await_count == 2

    @pytest.mark.asyncio
    async def test_ohne_historie_laufen_die_versuche_aus(self, sleep_mock):
        """Fabrikneue Instanz: nichts zu holen — aber kein Hänger."""
        coord = FakeCoordinator(stats_count=0, watt_alt=0.0)
        laeufe = await profil_nachladen(
            coord,
            coord.refresh,
            queue_durch=True,
            vorher=0.0,
            delays=(5, 10, 20),
            sleep=sleep_mock,
        )
        assert laeufe == 4  # erster Lauf + drei Versuche
        assert coord.stats_count == 0

    @pytest.mark.asyncio
    async def test_erste_daten_beenden_die_schleife(self, sleep_mock):
        """Kommt die Historie unterwegs, wird nicht weiter gepollt."""
        coord = FakeCoordinator(
            stats_count=0, watt_alt=0.0, watt_neu=928.0, sichtbar_ab_lauf=2
        )
        laeufe = await profil_nachladen(
            coord, coord.refresh, queue_durch=True, vorher=0.0, sleep=sleep_mock
        )
        assert laeufe == 2
        assert coord.stats_count == 671


class TestWarteAufRecorder:
    @pytest.mark.asyncio
    async def test_ohne_recorder_meldet_false_statt_zu_fliegen(self):
        """Kein Recorder (oder ältere HA-Version): der Start darf nicht brechen."""
        assert await warte_auf_recorder(MagicMock()) is False
