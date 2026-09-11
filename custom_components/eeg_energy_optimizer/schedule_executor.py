"""Fahrplan-Executor: hält den Fahrplan alle 30 Sekunden gegen die Messwerte.

Der ScheduleRunner (schedule.py) rechnet jede Minute einen Fahrplan über
36 Stunden. Dieser Executor läuft im 30-Sekunden-Timer (__init__.py) und
setzt den laufenden Slot am Wechselrichter durch — er ist der einzige Ort,
der Steuerbefehle schreibt.

Entscheiden und Setzen sind strikt getrennt:

* ``plan_action()`` übersetzt den laufenden Slot treiberneutral in eine
  Absicht (Ladelimit / Entladung / Freigabe) — reine Funktion ohne hass.
* ``ScheduleExecutor.async_guard_cycle()`` legt die Absicht gegen die
  Messwerte (Guard 1, Guard 2, Not-Aus, Totbänder) und schreibt
  ausschließlich über das ``InverterBase``-API.

Gesteuert wird nur ein Treiber mit ``supports_schedule_control=True``
(derzeit Huawei). Alle anderen Treiber rechnen und zeigen an.

Heizstab (heizstab/): eine zweite Senke neben der Batterie. Nach jedem
Guard-Lauf bestimmt ``_heizstab_schritt`` den Sollwert des Heizstabs aus der
gemessenen Einspeisung — klebt sie an der Einspeisegrenze, nimmt der
Heizstab den Überschuss in Schritten auf; bei einer Entladung ins Netz, im
Modus Aus und in der Startphase steht er auf 0. Wer bei ungeplantem
Überschuss zuerst an der Reihe ist (Heizstab oder Batterie), sagt die
Aufteilung: Guard 1 wartet auf einen gesättigten
Heizstab, oder der Heizstab wartet auf eine gesättigte Batterie.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .const import (
    CONF_DISCHARGE_POWER_KW,
    CONF_GRID_EXPORT_LIMIT_ENABLED,
    CONF_GRID_EXPORT_LIMIT_KW,
    CONF_INVERTER_AC_LIMIT_KW,
    CONF_PV_PEAK_KWP,
    DEFAULT_DISCHARGE_POWER_KW,
    DEFAULT_GRID_EXPORT_LIMIT_ENABLED,
    DEFAULT_GRID_EXPORT_LIMIT_KW,
    EXECUTOR_CHARGE_DEADBAND_KW,
    EXECUTOR_DISCHARGE_DEADBAND_KW,
    EXECUTOR_TARGET_SOC_DEADBAND_PCT,
    GUARD_CHARGE_RELEASE_FACTOR,
    GUARD_CHARGE_STEP_KW,
    GUARD_DISCHARGE_EFFICIENCY,
    GUARD_EMERGENCY_BLOCK_MINUTES,
    GUARD_EMERGENCY_IMPORT_KW,
    GUARD_EMERGENCY_IMPORT_RUNS,
    GUARD_EXPORT_RELEASE_KW,
    GUARD_EXPORT_STICKY_BAND_KW,
    MODE_AUS,
    MODE_EIN,
    SCHEDULE_BATTERY_FULL_SOC_PCT,
    SCHEDULE_FAILSAFE_MINUTES,
    STARTUP_GRACE_SECONDS,
)
from .const import (
    CONF_BATTERY_SOC_SENSOR,
    HEIZSTAB_TEILUNG_MAX_ANTEIL,
    HEIZSTAB_TEILUNG_SOC_LEER_PCT,
    HEIZSTAB_TEILUNG_SOC_VOLL_PCT,
)
from .power_readings import (
    compute_grid_export_kw,
    compute_house_load_kw,
    compute_pv_now_kw,
)
from .schedule import DEFAULT_AC_LIMIT_KW, _now_local, slot_for

_LOGGER = logging.getLogger(__name__)

# LP-Rauschen: |Werte| unterhalb dieser Schwelle gelten als 0.
_EPS_KW = 0.001
# Unter dieser Entladeleistung lohnt keine erzwungene Entladung — die
# gemessene PV deckt die geplante Einspeisung bereits, der Automatikmodus
# speist den Überschuss von selbst ein.
_MIN_DISCHARGE_KW = 0.05


@dataclass
class PlanAction:
    """Treiberneutrale Absicht des laufenden Fahrplan-Slots."""

    kind: str  # "charge_limit" | "discharge" | "release"
    # charge_limit: geplante Ladeleistung (Obergrenze, 0 = Laden blockiert);
    # discharge: geplante Netzeinspeisung (grid_p) des Slots.
    power_kw: float = 0.0
    # discharge: SOC am ENDE des laufenden Slots (chamo-Batteriebilanz:
    # battery_free[t] = battery_free[t-1] + battery_p[t]·dt — der Wert eines
    # Slots beschreibt den Zustand an seinem Ende).
    target_soc: float | None = None
    slot_t: str | None = None
    # Prognose-Hauslast des Slots — Rückfall für Guard 2, wenn der
    # Hauslast-Messwert nicht lesbar ist.
    consumption_kw: float | None = None
    # Klartext für Statussensor und Aktivitätslog, wenn „release" mehr
    # bedeutet als Normalbetrieb (Beispiel: volle Batterie).
    reason: str | None = None


def _voll_ab(result: dict | None) -> float:
    """Ab welchem geplanten Ladestand „nicht laden" eine Feststellung ist.

    Ohne Ladedeckel ist das ``SCHEDULE_BATTERY_FULL_SOC_PCT`` (99 %) wie
    bisher. Mit Deckel rückt die Schwelle um denselben Abstand unter ihn —
    bei einem Deckel von 90 % also 89 %.
    """
    try:
        deckel = float((result or {}).get("max_soc_pct") or 100.0)
    except (TypeError, ValueError):
        deckel = 100.0
    # Ein unplausibler Wert (Altplan, fremdes Archiv) darf die Schwelle nicht
    # nach oben schieben und damit die Erkennung ausschalten.
    deckel = max(0.0, min(100.0, deckel))
    return deckel - (100.0 - SCHEDULE_BATTERY_FULL_SOC_PCT)


def plan_action(result: dict | None, now: datetime) -> PlanAction | None:
    """Übersetzt den laufenden Slot in eine Absicht. None = kein Slot.

    Vorzeichen wie im Fahrplan (chamo-Konvention): ``battery_p`` positiv =
    entladen, negativ = laden; ``grid_p`` positiv = Einspeisung.
    """
    slot = slot_for((result or {}).get("slots"), now)
    if slot is None:
        return None
    try:
        battery_p = float(slot.get("battery_p") or 0.0)
        grid_p = float(slot.get("grid_p") or 0.0)
    except (TypeError, ValueError):
        return None
    slot_t = slot.get("t")
    consumption = slot.get("consumption")

    if battery_p < -_EPS_KW:
        # Slot plant Laden → Ladelimit auf die Planleistung. Guard 1 hebt es
        # bei stiller Abregelung an.
        return PlanAction(
            "charge_limit", power_kw=-battery_p, slot_t=slot_t, consumption_kw=consumption
        )
    if battery_p > _EPS_KW and grid_p > _EPS_KW:
        # Slot plant Einspeisung aus der Batterie → erzwungene Entladung.
        return PlanAction(
            "discharge",
            power_kw=grid_p,
            target_soc=slot.get("soc"),
            slot_t=slot_t,
            consumption_kw=consumption,
        )
    if battery_p > _EPS_KW:
        # Entladung nur für den Hausverbrauch — das erledigt der
        # Wechselrichter im Automatikmodus (maximise_self_consumption) selbst.
        return PlanAction("release", slot_t=slot_t, consumption_kw=consumption)
    # battery_p ≈ 0: kein Laden geplant. Zwei sehr verschiedene Fälle.
    soc = slot.get("soc")
    try:
        soc_val = None if soc is None else float(soc)
    except (TypeError, ValueError):
        soc_val = None
    # Die Schwelle wandert mit dem Ladedeckel: ist er auf 90 gesetzt, plant
    # der Fahrplan nie darüber, und eine feste 99er-Schwelle würde nie mehr
    # greifen — die Anlage stünde dauerhaft unter „Ladelimit 0" statt im
    # Automatikmodus, mit entsprechend vielen Registerschreibvorgängen. Was
    # gleich bleibt, ist der ABSTAND: das letzte Prozent vor „voll".
    if soc_val is not None and soc_val >= _voll_ab(result):
        # Die Batterie ist voll — „nicht laden" ist hier keine Absicht,
        # sondern eine Feststellung. Ein Ladelimit 0 bewirkt nichts (es ist
        # kein Platz) und stünde nur im Weg, sobald wieder Platz entsteht
        # oder die Integration neu startet. Also kein Eingriff.
        return PlanAction(
            "release",
            slot_t=slot_t,
            consumption_kw=consumption,
            reason="Normalbetrieb (Batterie voll)",
        )
    # Batterie hat Platz: Freigeben wäre falsch — der Automatikmodus würde
    # PV-Überschuss in die Batterie laden, den der Plan einspeisen will
    # (Morgen-Einspeisung entsteht genau hier). Ladelimit 0 blockiert nur das
    # Laden; Entladung für den Hausverbrauch bleibt möglich.
    return PlanAction("charge_limit", power_kw=0.0, slot_t=slot_t, consumption_kw=consumption)


class ScheduleExecutor:
    """Setzt den Fahrplan am Wechselrichter durch — Guards, Totbänder, Failsafe.

    Schreibt Limits statt Zustände: Absicherung gegen stehenbleibende Limits
    über async_release() beim Entladen der Integration, beim Wechsel
    Ein → Test und über den Failsafe (kein brauchbarer Fahrplan seit
    SCHEDULE_FAILSAFE_MINUTES). Nach einem harten Absturz bleibt ein Limit
    bis zum ersten Guard-Lauf nach dem Neustart stehen (Grace Period).
    """

    def __init__(
        self,
        hass: Any,
        entry_id: str,
        config: dict,
        inverter: Any,
        failure_callback: Any = None,
        heizstab: Any = None,
    ) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._config = config
        self._inverter = inverter
        # Heizstab-Controller (heizstab/controller.py) oder None. Der Executor
        # bestimmt seinen Sollwert je Lauf, geschrieben wird dort.
        self._heizstab = heizstab
        # Guard 1 stand zuletzt am Hardware-Maximum des Ladelimits — dann ist
        # die Batterie gesättigt und der Heizstab darf nachrücken (bei
        # Batterie-Vorrang).
        self._ladelimit_am_maximum = False
        # Wird bei Schreibfehlern mit der Aktion ("charge_limit" / "discharge"
        # / "release") aufgerufen — Telemetrie-Anbindung aus __init__.py.
        self._failure_callback = failure_callback
        self._created_at = _now_local()

        # Zuletzt geschriebene Werte (Totbänder + Wechsel-Erkennung).
        self._active_kind: str | None = None
        self._written_charge_limit_kw: float | None = None
        self._written_discharge_kw: float | None = None
        self._written_target_soc: float | None = None

        # Fahrplan-Frische (Failsafe).
        self._plan_seen_at: datetime | None = None
        self._failsafe_released = False

        # Not-Aus (Guard 2): anhaltender Netzbezug während einer Entladung.
        self._emergency_runs = 0
        self._emergency_blocked_slot: str | None = None
        # Zeitliche Not-Aus-Sperre — greift, wenn beim Zuschlagen kein
        # Fahrplan vorlag und es deshalb keinen Slot zum Anhängen gab.
        self._emergency_blocked_until: datetime | None = None
        self._current_slot_t: str | None = None

        self._last_mode: str | None = None
        # Freigabe im Anzeige-Modus nachholen (Limit aus einer Vorsession).
        self._display_release_pending = False
        # Aktive Pause (Ablaufzeit, ggf. Ziel-Ladestand) — nur für den
        # Statustext; die Wirkung ist dieselbe wie Modus Aus.
        self._pause_bis: datetime | None = None
        self._pause_soc_pct: float | None = None

        # Status für Panel, Statussensor und Aktivitätslog.
        self.last_run_iso: str | None = None
        self.last_action: PlanAction | None = None
        self.last_status: str = "noch kein Lauf"
        self.last_write_ok: bool | None = None
        self.write_failures = 0

    # ------------------------------------------------------------------
    # Öffentliches API
    # ------------------------------------------------------------------
    @property
    def _supported(self) -> bool:
        return self._inverter is not None and bool(
            getattr(self._inverter, "supports_schedule_control", False)
        )

    def update_config(self, config: dict) -> None:
        """Hot-Reload: neue Config übernehmen, Zustand behalten.

        Totbänder, Grace Period, Not-Aus-Sperre und die zuletzt geschriebenen
        Werte überleben eine reine Einstellungs-Änderung — nur die Config-
        Referenz wird getauscht (der Executor liest sie bei jedem Zugriff).
        """
        self._config = config

    def _notify_failure(self, action: str) -> None:
        """Schreibfehler an die Telemetrie melden (fail-safe, nie werfend)."""
        if self._failure_callback is None:
            return
        try:
            self._failure_callback(action)
        except Exception:  # pragma: no cover — defensiv
            _LOGGER.exception("Executor: failure_callback fehlgeschlagen")

    async def async_release(self) -> bool:
        """Wechselrichter freigeben: erzwungene Modi stoppen, Automatik läuft.

        Bei Huawei stellt ``async_stop_forcible`` das Ladelimit auf das
        Maximum der Number-Entität zurück — also auf den anlagenspezifischen
        Standardwert. Freigeben heißt damit: kein Eingriff mehr.

        Der Zustand wird nur bei Erfolg auf ``release`` gesetzt. Nach einem
        Fehlschlag steht im Gerät weiter unser letztes Limit; würde der
        Executor sich trotzdem als freigegeben merken, stiege
        ``_apply_release`` künftig früh aus und die Batterie bliebe dauerhaft
        blockiert.
        """
        if not self._supported:
            return True
        ok = await self._inverter.async_stop_forcible()
        self._written_charge_limit_kw = None
        self._written_discharge_kw = None
        self._written_target_soc = None
        if ok:
            self._active_kind = "release"
        else:
            self._active_kind = None
            self.write_failures += 1
            self._notify_failure("release")
            _LOGGER.warning("Executor: Freigabe (stop_forcible) fehlgeschlagen")
        return ok

    async def async_guard_cycle(
        self,
        schedule_state: dict | None,
        mode: str,
        now: datetime | None = None,
        pause_bis: datetime | None = None,
        pause_soc_pct: float | None = None,
    ) -> None:
        """Ein Guard-Lauf: Absicht bestimmen, gegen Messwerte halten, setzen.

        ``schedule_state`` ist das ``ScheduleRunner.to_dict()``-Payload,
        ``mode`` der Wert des Optimizer-Selects (nur MODE_EIN schreibt).
        ``now`` ist injizierbar für Tests.

        Der Heizstab kommt nach dem Wechselrichter dran — in jedem Fall, auch
        wenn der Lauf früh aussteigt (Treiber nicht steuerbar, Startphase,
        Failsafe): sein Sollwert muss dann 0 werden, sonst stünde er bis zum
        Watchdog des Ohmpilot mit dem letzten Wert da.
        """
        now = now or _now_local()
        if self._heizstab is not None:
            # Hysteresen fortschreiben, BEVOR die Absicht übersetzt wird —
            # unter der Mindesttemperatur wird eine Entladung unterdrückt.
            self._heizstab.pruefe_temperaturen()
        try:
            await self._guard_cycle_wechselrichter(
                schedule_state, mode, now, pause_bis, pause_soc_pct
            )
        finally:
            await self._heizstab_schritt(mode, now, pause_bis, schedule_state)

    async def _guard_cycle_wechselrichter(
        self,
        schedule_state: dict | None,
        mode: str,
        now: datetime,
        pause_bis: datetime | None,
        pause_soc_pct: float | None,
    ) -> None:
        """Der Wechselrichter-Teil des Guard-Laufs (siehe async_guard_cycle)."""
        self.last_run_iso = now.isoformat()

        # Eine Pause ist ein Aus mit Ablaufzeit: dieselbe Freigabe beim
        # Eintritt, derselbe Verzicht auf Schreibbefehle — nur der Status
        # sagt, wann es weitergeht. Der Modus-Wechsel unten sieht deshalb
        # einfach "Aus" und macht das Richtige.
        self._pause_bis = pause_bis
        self._pause_soc_pct = pause_soc_pct if pause_bis is not None else None
        if pause_bis is not None:
            mode = MODE_AUS

        if not self._supported:
            self.last_status = "Treiber wird nicht gesteuert — Plan nur Anzeige"
            return

        # Wechsel Ein → Test/Aus: einmalig freigeben, sonst bleibt das letzte
        # Ladelimit im Wechselrichter stehen.
        if self._last_mode == MODE_EIN and mode != MODE_EIN:
            _LOGGER.info("Executor: Modus %s → %s — Wechselrichter freigegeben", self._last_mode, mode)
            await self.async_release()
        elif self._last_mode is None and mode != MODE_EIN:
            # Erster Lauf nach einem Neustart, und wir steuern nicht. Ein in
            # der Vorsession geschriebenes Limit steht dann weiter im Gerät —
            # im Anzeige-Modus würde es NIE zurückgenommen (der Lauf steigt
            # unten aus, bevor geschrieben wird). Nachgeholt wird die Freigabe
            # erst nach Startphase und Verfügbarkeitsprüfung, siehe unten.
            self._display_release_pending = True
        self._last_mode = mode

        # Fahrplan-Frische und Absicht (auch im Test-Modus, für die Anzeige).
        fresh = self._plan_is_fresh(schedule_state, now)
        if fresh:
            self._plan_seen_at = now
            self._failsafe_released = False
            action = plan_action(schedule_state, now)
        else:
            action = None
        if (
            action is not None
            and action.kind == "discharge"
            and self._heizstab is not None
            and self._heizstab.komfort_aus_netz
        ):
            # Unter der Mindesttemperatur MIT erlaubtem Netzbezug heizt der
            # Heizstab mit voller Leistung — eine erzwungene Entladung landete
            # jetzt im Boiler statt in der Gemeinschaft. Also Freigabe: der
            # Automatikmodus deckt Haus und Heizstab aus PV und Batterie, den
            # Rest holt sich der Heizstab aus dem Netz. Das ist der Preis des
            # Komforts. Ohne erlaubten Netzbezug bleibt die Entladung — der
            # Heizstab steht dann auf 0 (siehe HeizstabController.regeln).
            action = PlanAction(
                "release",
                slot_t=action.slot_t,
                consumption_kw=action.consumption_kw,
                reason="Normalbetrieb (Heizstab unter Mindesttemperatur — keine Entladung ins Netz)",
            )
        self.last_action = action

        # Slotwechsel: Not-Aus-Sperre gilt nur bis zum nächsten Slot. Ein
        # FEHLENDER Plan ist dabei kein Slotwechsel — sonst hob ein einzelner
        # Lauf ohne Fahrplan (SOC-Sensor kurz unavailable, Prognose-Hiccup)
        # die Sperre auf und setzte zugleich _current_slot_t auf None, womit
        # die Entladung im nächsten Lauf sofort wieder in denselben Netzbezug
        # startete.
        slot_t = action.slot_t if action else None
        if slot_t is not None and slot_t != self._current_slot_t:
            self._current_slot_t = slot_t
            if (
                self._emergency_blocked_slot is not None
                and slot_t != self._emergency_blocked_slot
            ):
                _LOGGER.info("Executor: Slotwechsel — Not-Aus-Sperre aufgehoben")
                self._emergency_blocked_slot = None
                self._emergency_blocked_until = None

        if mode != MODE_EIN:
            # Umschalten auf „Aus" nimmt die gesetzten Steuerwerte SOFORT
            # zurück — sonst liefe der Wechselrichter mit dem letzten Limit
            # oder einer laufenden Zwangsentladung weiter, obwohl die
            # Optimierung aus ist. Bis 1.5.51 geschah das nur nach einem
            # Neustart (_display_release_pending), nicht beim Umschalten im
            # Betrieb.
            if self._active_kind not in (None, "release") and getattr(
                self._inverter, "is_available", False
            ):
                if await self.async_release():
                    _LOGGER.info(
                        "Executor: Modus 'Aus' — Steuerwerte zurückgenommen, "
                        "der Wechselrichter läuft im Automatikmodus"
                    )
            # Nachgeholte Freigabe nach einem Neustart: ein Limit aus der
            # Vorsession darf nicht stehenbleiben, nur weil wir gerade nicht
            # steuern — es käme sonst nie zurück. Erst nach der Startphase,
            # damit die Wechselrichter-Entitäten geladen sind. Bei Fehlschlag
            # bleibt das Flag stehen und der nächste Lauf versucht es erneut.
            if (
                self._display_release_pending
                and (now - self._created_at).total_seconds() >= STARTUP_GRACE_SECONDS
                and getattr(self._inverter, "is_available", False)
            ):
                if await self.async_release():
                    self._display_release_pending = False
                    _LOGGER.info(
                        "Executor: Modus 'Aus' — Steuerwerte aus der Vorsession "
                        "auf Standard zurückgenommen"
                    )
            if self._pause_bis is not None and self._pause_soc_pct is not None:
                self.last_status = (
                    f"Pause bis Ladestand {self._pause_soc_pct:.0f} % — "
                    "der Wechselrichter läuft im Automatikmodus"
                )
            elif self._pause_bis is not None:
                self.last_status = (
                    f"Pause bis {self._pause_bis.strftime('%H:%M')} — "
                    "der Wechselrichter läuft im Automatikmodus"
                )
            else:
                self.last_status = "Aus — es wird nicht gesteuert"
            return

        if (now - self._created_at).total_seconds() < STARTUP_GRACE_SECONDS:
            self.last_status = "Startphase — noch keine Steuerbefehle"
            return

        if not getattr(self._inverter, "is_available", False):
            self.last_status = "Wechselrichter nicht verfügbar"
            return

        # Not-Aus: läuft eine Entladung, wird der Netzbezug IMMER überwacht —
        # auch wenn der Fahrplan gerade fehlt. Sensor nicht lesbar → fail-open
        # (kein Zähler), die Freigabe übernimmt notfalls der Failsafe.
        if self._active_kind == "discharge":
            export = compute_grid_export_kw(self._hass, self._config)
            if export is not None and export < -GUARD_EMERGENCY_IMPORT_KW:
                self._emergency_runs += 1
            else:
                self._emergency_runs = 0
            if self._emergency_runs >= GUARD_EMERGENCY_IMPORT_RUNS:
                _LOGGER.warning(
                    "Executor: Not-Aus — Netzbezug > %.1f kW in %d Guard-Läufen, "
                    "Entladung gestoppt (Sperre bis zum nächsten Slot)",
                    GUARD_EMERGENCY_IMPORT_KW,
                    GUARD_EMERGENCY_IMPORT_RUNS,
                )
                await self.async_release()
                # Zweite Sperre über die Uhr: der Not-Aus überwacht bewusst
                # auch ohne Fahrplan, dann ist _current_slot_t aber None —
                # und eine Sperre auf None trifft in der Prüfung unten keinen
                # Slot, die Entladung startete sofort wieder.
                self._emergency_blocked_slot = self._current_slot_t
                self._emergency_blocked_until = now + timedelta(
                    minutes=GUARD_EMERGENCY_BLOCK_MINUTES
                )
                self._emergency_runs = 0
                self.last_status = (
                    "Not-Aus: anhaltender Netzbezug — Entladung bis zum Slotwechsel gesperrt"
                )
                return
        else:
            self._emergency_runs = 0

        # Failsafe: kein brauchbarer Fahrplan → einmalig freigeben.
        if action is None:
            overdue = (
                self._plan_seen_at is None
                or now - self._plan_seen_at
                >= timedelta(minutes=SCHEDULE_FAILSAFE_MINUTES)
                or bool((schedule_state or {}).get("available"))  # verfügbar, aber zu alt
            )
            if overdue and not self._failsafe_released:
                _LOGGER.warning(
                    "Executor: Failsafe — kein brauchbarer Fahrplan, Wechselrichter freigegeben"
                )
                await self.async_release()
                self._failsafe_released = True
                self.last_status = "Failsafe: kein Plan — Wechselrichter freigegeben"
            elif self._failsafe_released:
                self.last_status = "Failsafe aktiv — warte auf neuen Plan"
            else:
                self.last_status = "Plan fehlt kurzzeitig — letzter Zustand bleibt"
            return

        # Not-Aus-Sperre: im gesperrten Slot keine neue Entladung starten —
        # und keine, solange die Zeitsperre läuft (sie greift, wenn der
        # Not-Aus ohne gültigen Plan zuschlug und es keinen Slot gab).
        if action.kind == "discharge" and (
            self._emergency_blocked_slot == action.slot_t
            or (
                self._emergency_blocked_until is not None
                and now < self._emergency_blocked_until
            )
        ):
            self.last_status = "Not-Aus aktiv — Entladung bis zum Slotwechsel gesperrt"
            return

        if action.kind == "release":
            await self._apply_release(action.reason or "Normalbetrieb")
        elif action.kind == "charge_limit":
            await self._apply_charge_limit(action)
        else:
            await self._apply_discharge(action)

    def status(self) -> dict[str, Any]:
        """Zustand für Panel, Statussensor und WebSocket."""
        action = self.last_action
        return {
            "supported": self._supported,
            "mode": self._last_mode,
            "status": self.last_status,
            "last_run": self.last_run_iso,
            "active_kind": self._active_kind,
            "written_charge_limit_kw": self._written_charge_limit_kw,
            "written_discharge_kw": self._written_discharge_kw,
            "written_target_soc": self._written_target_soc,
            "plan_action": None
            if action is None
            else {
                "kind": action.kind,
                "power_kw": round(action.power_kw, 3),
                "target_soc": action.target_soc,
                "slot": action.slot_t,
                "reason": action.reason,
            },
            "failsafe_released": self._failsafe_released,
            "pause_bis": None if self._pause_bis is None else self._pause_bis.isoformat(),
            "pause_soc_pct": self._pause_soc_pct,
            "emergency_runs": self._emergency_runs,
            "emergency_blocked_slot": self._emergency_blocked_slot,
            "write_failures": self.write_failures,
            "last_write_ok": self.last_write_ok,
            "heizstab": None if self._heizstab is None else self._heizstab.status(),
        }

    # ------------------------------------------------------------------
    # Heizstab — zweite Senke neben der Batterie
    # ------------------------------------------------------------------
    def _heizstab_grenze_kw(self) -> float:
        """Die Einspeisegrenze, auf die der Heizstab regelt.

        Dieselbe Schranke wie im LP (schedule.py): die konfigurierte
        Einspeisegrenze, sonst die AC-Grenzleistung abzüglich 0,5 kW — ohne
        Netzbetreiber-Grenze begrenzt allein der Wechselrichter, und alles
        darüber wäre abgeregelt.
        """
        enabled = bool(
            self._config.get(CONF_GRID_EXPORT_LIMIT_ENABLED, DEFAULT_GRID_EXPORT_LIMIT_ENABLED)
        )
        try:
            limit_kw = float(
                self._config.get(CONF_GRID_EXPORT_LIMIT_KW, DEFAULT_GRID_EXPORT_LIMIT_KW) or 0.0
            )
        except (TypeError, ValueError):
            limit_kw = 0.0
        if enabled and limit_kw > 0:
            return limit_kw
        ac = (
            self._config.get(CONF_INVERTER_AC_LIMIT_KW)
            or self._config.get("schedule_ac_limit_kw")
            or self._config.get(CONF_PV_PEAK_KWP)
        )
        try:
            ac_kw = float(ac) if ac else DEFAULT_AC_LIMIT_KW
        except (TypeError, ValueError):
            ac_kw = DEFAULT_AC_LIMIT_KW
        return max(0.5, ac_kw - 0.5)

    def _batterie_gesaettigt(self) -> bool:
        """Kann die Batterie gerade nichts (mehr) aufnehmen?

        Dann ist der Heizstab auch bei Batterie-Vorrang an der Reihe. Ein
        nicht steuerbarer Treiber zählt als gesättigt — sein Ladelimit lässt
        sich nicht anheben, der Überschuss ginge sonst verloren.
        """
        if not self._supported:
            return True
        action = self.last_action
        if action is not None and action.kind == "release":
            # Batterie voll oder Entladung fürs Haus — Laden ist kein Thema.
            return True
        return self._ladelimit_am_maximum

    def _heizstab_vorrang_frei(self) -> bool:
        """Darf der Heizstab jetzt Überschuss aufnehmen?

        Immer, sobald einer da ist — begrenzt wird er nicht über die
        Reihenfolge, sondern über seinen Anteil (``_heizstab_deckel_kw``).
        Das alte „erst die Batterie, dann der Heizstab" ist darin enthalten:
        Bei fast leerer Batterie ist sein Anteil null.
        """
        return self._heizstab is not None

    def _batterie_soc_pct(self) -> float | None:
        """Ladestand für die Aufteilung; None, wenn nicht lesbar."""
        entity = self._config.get(CONF_BATTERY_SOC_SENSOR)
        if not entity:
            return None
        try:
            state = self._hass.states.get(entity)
        except Exception:  # noqa: BLE001
            return None
        if state is None:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _heizstab_deckel_kw(self) -> float | None:
        """Anteil des Heizstabs am Überschuss — None heißt „kein Deckel".

        Batterie und Heizstab teilen sich den Überschuss, gewichtet nach
        dem Ladestand:
        Unter ``HEIZSTAB_TEILUNG_SOC_LEER_PCT`` bekommt die Batterie alles —
        ihre Energie trägt durch die Nacht, die Wärme nicht. Ab
        ``HEIZSTAB_TEILUNG_SOC_VOLL_PCT`` ist es die Hälfte, dazwischen
        linear. Ist die Batterie gesättigt oder der Ladestand unbekannt,
        entfällt der Deckel — der Überschuss soll nicht verfallen.
        """
        hz = self._heizstab
        if hz is None or not hz.enabled:
            return None
        if self._batterie_gesaettigt():
            return None
        soc = self._batterie_soc_pct()
        if soc is None:
            return None
        spanne = HEIZSTAB_TEILUNG_SOC_VOLL_PCT - HEIZSTAB_TEILUNG_SOC_LEER_PCT
        if spanne <= 0:
            anteil = HEIZSTAB_TEILUNG_MAX_ANTEIL
        else:
            anteil = (soc - HEIZSTAB_TEILUNG_SOC_LEER_PCT) / spanne
            anteil = max(0.0, min(1.0, anteil)) * HEIZSTAB_TEILUNG_MAX_ANTEIL
        return round(hz.max_kw * anteil, 3)

    def _heizstab_gesaettigt(self) -> bool:
        """Muss Guard 1 auf den Heizstab warten? Nein, wenn er gesättigt ist."""
        if self._heizstab is None or not self._heizstab.enabled:
            return True
        return self._heizstab.gesaettigt

    def _heizstab_plan_kw(self, schedule_state: dict | None, now: datetime) -> float:
        """Wärme, die der laufende Fahrplan-Slot vorsieht (kW). 0 = keine.

        Nur aus einem frischen Plan — ein eingefrorener Runner soll den
        Heizstab nicht stundenlang nach veralteter Prognose heizen lassen.
        Dieselbe Frischeprüfung wie beim Wechselrichter-Teil.
        """
        if not self._plan_is_fresh(schedule_state, now):
            return 0.0
        slot = slot_for((schedule_state or {}).get("slots"), now)
        if slot is None:
            return 0.0
        try:
            return max(0.0, float(slot.get("heizstab") or 0.0))
        except (TypeError, ValueError):
            return 0.0

    async def _heizstab_schritt(
        self,
        mode: str,
        now: datetime,
        pause_bis: datetime | None,
        schedule_state: dict | None = None,
    ) -> None:
        """Sollwert des Heizstabs für diesen Lauf bestimmen und übergeben.

        Läuft nach dem Wechselrichter-Teil, mit dessen Ergebnis: ob gerade
        entladen wird (dann 0), ob die Batterie gesättigt ist (dann darf der
        Heizstab bei Batterie-Vorrang nachrücken). Mode Aus, Pause und
        Startphase heißen 0 — „wenn die Optimierung aus ist, ist der Heizstab
        aus", auch unter der Mindesttemperatur.

        Sieht der laufende Slot Wärme vor, ist sie die Vorgabe (``plan_kw``);
        sonst bleibt es bei der Regel an der Einspeisegrenze, die ungeplanten
        Überschuss auffängt.
        """
        hz = self._heizstab
        if hz is None or not hz.enabled:
            return
        try:
            if pause_bis is not None or mode != MODE_EIN:
                await hz.async_set_sollwert(0.0, "Optimierung aus")
                return
            if (now - self._created_at).total_seconds() < STARTUP_GRACE_SECONDS:
                await hz.async_set_sollwert(0.0, "Startphase")
                return
            entladung = self._active_kind == "discharge" or (
                self.last_action is not None and self.last_action.kind == "discharge"
            )
            export = compute_grid_export_kw(self._hass, self._config)
            plan_kw = self._heizstab_plan_kw(schedule_state, now)
            soll, grund = hz.regeln(
                entladung=entladung,
                export_kw=export,
                grenze_kw=self._heizstab_grenze_kw(),
                vorrang_frei=self._heizstab_vorrang_frei(),
                deckel_kw=self._heizstab_deckel_kw(),
                plan_kw=plan_kw,
            )
            await hz.async_set_sollwert(soll, grund)
        except Exception:  # noqa: BLE001 — der Heizstab darf den Takt nie kippen
            _LOGGER.exception("Executor: Heizstab-Schritt fehlgeschlagen")

    # ------------------------------------------------------------------
    # Fahrplan-Frische
    # ------------------------------------------------------------------
    def _plan_is_fresh(self, schedule_state: dict | None, now: datetime) -> bool:
        """Brauchbarer Fahrplan: verfügbar und jünger als der Failsafe-Horizont.

        Wichtig gegen einen eingefrorenen Runner: dessen letztes Ergebnis
        bleibt in hass.data „verfügbar“ stehen — die Slots reichen 36 Stunden
        weit, ohne Frische-Prüfung würde der Executor stundenlang einen
        veralteten Plan fahren.
        """
        if not schedule_state or not schedule_state.get("available"):
            return False
        raw = schedule_state.get("last_run")
        if raw:
            try:
                age = now - datetime.fromisoformat(raw)
                return age < timedelta(minutes=SCHEDULE_FAILSAFE_MINUTES)
            except (TypeError, ValueError):
                pass
        # Runner setzt last_run immer — ohne Zeitstempel nicht bewertbar,
        # verfügbar zählt dann als frisch.
        return True

    # ------------------------------------------------------------------
    # Ausführung (nur hier wird geschrieben — ausschließlich via InverterBase)
    # ------------------------------------------------------------------
    async def _apply_release(self, status_text: str) -> None:
        if self._active_kind == "release":
            self.last_status = status_text
            return
        ok = await self.async_release()
        self.last_write_ok = ok
        self.last_status = (
            f"{status_text} — Wechselrichter freigegeben"
            if ok
            else f"Schreibfehler: Freigabe fehlgeschlagen ({status_text})"
        )

    async def _apply_charge_limit(self, action: PlanAction) -> None:
        # Wechsel von Einspeisung auf keine Einspeisung → erzwungene
        # Entladung zuerst stoppen (stop_forcible stellt bei Huawei auch das
        # Ladelimit auf Maximum zurück; direkt danach wird der Planwert gesetzt).
        if self._active_kind == "discharge":
            await self._inverter.async_stop_forcible()
            self._written_discharge_kw = None
            self._written_target_soc = None

        ziel, grund = await self._resolve_charge_limit(action.power_kw)

        kind_change = self._active_kind != "charge_limit"
        if (
            not kind_change
            and self._written_charge_limit_kw is not None
            and abs(ziel - self._written_charge_limit_kw) <= EXECUTOR_CHARGE_DEADBAND_KW
        ):
            self.last_status = (
                f"Laden begrenzt auf {self._written_charge_limit_kw:.2f} kW ({grund}, unverändert)"
            )
            return

        ok = await self._inverter.async_set_charge_limit(ziel)
        self.last_write_ok = ok
        if ok:
            self._active_kind = "charge_limit"
            self._written_charge_limit_kw = ziel
            self.last_status = f"Laden begrenzt auf {ziel:.2f} kW ({grund})"
        else:
            self.write_failures += 1
            self._notify_failure("charge_limit")
            self.last_status = f"Schreibfehler: Ladelimit {ziel:.2f} kW nicht gesetzt"
            _LOGGER.warning("Executor: Ladelimit %.2f kW nicht gesetzt", ziel)

    async def _resolve_charge_limit(self, plan_kw: float) -> tuple[float, str]:
        """Zielwert für das Ladelimit: Fahrplanwert oder Guard-1-Anpassung.

        Guard 1 läuft nur mit aktivierter Einspeisegrenze. Er rechnet
        schrittweise vom aktuell gesetzten Limit aus weiter — bei aktiver
        Abregelung ist die gemessene PV bereits beschnitten, „PV − Hauslast −
        Grenze“ fiele systematisch zu klein aus und würde im nächsten Takt
        wieder kleben.
        """
        enabled = bool(
            self._config.get(
                CONF_GRID_EXPORT_LIMIT_ENABLED, DEFAULT_GRID_EXPORT_LIMIT_ENABLED
            )
        )
        limit_kw = float(
            self._config.get(CONF_GRID_EXPORT_LIMIT_KW, DEFAULT_GRID_EXPORT_LIMIT_KW)
            or 0.0
        )
        if not enabled or limit_kw <= 0:
            return plan_kw, "Planwert"

        export = compute_grid_export_kw(self._hass, self._config)
        if export is None:
            # Ohne Netz-Messwert kein Guard — fail-open auf den Planwert.
            return plan_kw, "Planwert (Netz-Messwert fehlt)"

        aktuell = await self._inverter.async_get_charge_limit_kw()
        basis = aktuell
        if basis is None:
            basis = self._written_charge_limit_kw
        if basis is None:
            basis = plan_kw

        if export >= limit_kw - GUARD_EXPORT_STICKY_BAND_KW:
            # Klebt am Limit (oder liegt darüber) → ein Schritt hoch. Deckt
            # alle drei Fälle ab: aktuell > Plan → aktuell + Schritt;
            # aktuell == Plan → + Schritt; Plan > aktuell → Planwert.
            # Guard 1 wartet auf nichts mehr: Heizstab und Batterie regeln
            # im selben Lauf hoch, der Heizstab nur bis zu seinem Anteil
            # (_heizstab_deckel_kw). Bis 2.1.1-dev4 gab es hier einen
            # Wartezweig für den Heizstab-Vorrang — der Schalter ist entfallen.
            neu = max(plan_kw, basis + GUARD_CHARGE_STEP_KW)
            max_kw = self._inverter.get_charge_limit_max_kw()
            if max_kw is not None:
                neu = min(neu, max_kw)
                self._ladelimit_am_maximum = neu >= max_kw - 0.01
            else:
                self._ladelimit_am_maximum = False
            return neu, "Guard 1: Einspeisung am Limit — Ladelimit angehoben"
        self._ladelimit_am_maximum = False
        if export < limit_kw - GUARD_EXPORT_RELEASE_KW:
            # Deutlich unter der Grenze → zurück Richtung Fahrplanwert, nie
            # darunter. Je Lauf wird der halbe Abstand abgebaut, mindestens
            # aber ein voller Schritt: das Ziel ist bekannt (siehe
            # GUARD_CHARGE_RELEASE_FACTOR), und mit festen Schritten wäre ein
            # Slot vorbei, bevor sein Planwert wirkt.
            abstand = basis - plan_kw
            schritt = max(abstand * GUARD_CHARGE_RELEASE_FACTOR, GUARD_CHARGE_STEP_KW)
            neu = max(plan_kw, basis - schritt)
            if neu < basis:
                return neu, "Guard 1: Rücknahme Richtung Planwert"
            return neu, "Planwert"
        # Asymmetrisches totes Band (Grenze − 0,3 … − 0,1 kW): nichts tun,
        # das verhindert Pendeln zwischen Anheben und Rücknahme.
        return basis, "Guard 1: totes Band — Ladelimit bleibt"

    async def _apply_discharge(self, action: PlanAction) -> None:
        """Guard 2: erzwungene Entladung an der gemessenen Hauslast nachführen.

        forcible_discharge gibt die Leistung an, die die Batterie abgibt;
        davon deckt der Wechselrichter zuerst den Hausverbrauch, nur der Rest
        wird eingespeist. In battery_p steckt bereits die PROGNOSTIZIERTE
        Hauslast — deshalb grid_p + GEMESSENE Hauslast, nicht battery_p.
        Läuft noch PV, liefert sie einen Teil der Einspeisung selbst und wird
        abgezogen (nachts 0, dann exakt die Formel aus dem Umbauplan).
        """
        haus = compute_house_load_kw(self._hass, self._config)
        haus_quelle = "gemessen"
        if haus is None:
            # Fail-open, geloggt: Rückfall auf die Prognose-Hauslast des Slots.
            haus = float(action.consumption_kw or 0.0)
            haus_quelle = "Prognose (Messwert fehlt)"
            _LOGGER.debug(
                "Executor: Hauslast nicht lesbar — Rückfall auf Prognose %.2f kW", haus
            )
        pv = compute_pv_now_kw(self._hass, self._config) or 0.0

        power = (action.power_kw + haus - pv) / GUARD_DISCHARGE_EFFICIENCY
        cap = self._inverter.get_max_discharge_power_kw()
        if cap is None:
            cap = float(
                self._config.get(CONF_DISCHARGE_POWER_KW, DEFAULT_DISCHARGE_POWER_KW)
                or 0.0
            ) or None
        if cap is not None:
            power = min(power, cap)
        power = max(power, 0.0)

        if power < _MIN_DISCHARGE_KW:
            # PV deckt die geplante Einspeisung — keine erzwungene Entladung.
            await self._apply_release("Entladung nicht nötig (PV deckt den Plan)")
            return

        ziel_soc = action.target_soc
        kind_change = self._active_kind != "discharge"
        power_same = (
            self._written_discharge_kw is not None
            and abs(power - self._written_discharge_kw) <= EXECUTOR_DISCHARGE_DEADBAND_KW
        )
        soc_same = ziel_soc is None or (
            self._written_target_soc is not None
            and abs(ziel_soc - self._written_target_soc) < EXECUTOR_TARGET_SOC_DEADBAND_PCT
        )
        if not kind_change and power_same and soc_same:
            self.last_status = (
                f"Entladung {self._written_discharge_kw:.2f} kW auf Ziel-SOC "
                f"{self._written_target_soc:.0f} % (unverändert)"
                if self._written_target_soc is not None
                else f"Entladung {self._written_discharge_kw:.2f} kW (unverändert)"
            )
            return

        ok = await self._inverter.async_set_discharge(power, target_soc=ziel_soc)
        self.last_write_ok = ok
        if ok:
            self._active_kind = "discharge"
            self._written_discharge_kw = power
            self._written_target_soc = ziel_soc
            soc_text = f" auf Ziel-SOC {ziel_soc:.0f} %" if ziel_soc is not None else ""
            self.last_status = (
                f"Entladung {power:.2f} kW{soc_text} "
                f"(Plan {action.power_kw:.2f} kW Einspeisung + Hauslast {haus:.2f} kW "
                f"[{haus_quelle}] − PV {pv:.2f} kW)"
            )
        else:
            self.write_failures += 1
            self._notify_failure("discharge")
            self.last_status = f"Schreibfehler: Entladung {power:.2f} kW nicht gesetzt"
            _LOGGER.warning("Executor: Entladung %.2f kW nicht gesetzt", power)
