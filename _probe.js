class X {
  _renderStep3() {
    const detected = this._detectedSensors && this._detectedSensors.detected;

    let detectionInfo = "";

    // Huawei: ohne "Enable battery control" registriert huawei_solar keine
    // Steuer-Dienste — dann kann der Optimizer die Batterie nicht ansteuern.
    // Sofort sichtbar warnen (huawei_battery_control === false aus detect).
    if (
      this._wizardData.inverter_type === "huawei_sun2000" &&
      this._detectedSensors &&
      this._detectedSensors.huawei_battery_control === false
    ) {
      detectionInfo += `
        <div style="display:flex;gap:12px;padding:14px;border-left:3px solid var(--error-color,#db4437);background:var(--secondary-background-color);border-radius:6px;margin-bottom:16px">
          <ha-icon icon="mdi:alert-circle" style="--mdc-icon-size:28px;color:var(--error-color,#db4437);flex-shrink:0"></ha-icon>
          <div>
            <strong>Batteriesteuerung in „Huawei Solar" nicht aktiviert</strong>
            <div class="help-text" style="margin-top:6px">
              Die Steuer-Dienste der <code>huawei_solar</code>-Integration fehlen.
              Ohne sie kann der Optimizer die Batterie <strong>nicht ansteuern</strong>
              (weder Laden blockieren noch entladen). Richte die
              <code>huawei_solar</code>-Integration mit der Option
              <strong>„Enable battery control"</strong> ein (erfordert den
              Installer-Login) und prüfe dann erneut.
            </div>
            <button class="btn-secondary" data-action="redetect-sensors" style="margin-top:10px">Erneut prüfen</button>
          </div>
        </div>`;
    }

    // SolarEdge: Driver berechnet SOC + Kapazität kapazitätsgewichtet über
    // alle Inverter (i1, i2, ...). Wizard zeigt nur einen Info-Block; die
    // Combined-Sensor-IDs werden beim Save automatisch eingetragen.
    if (this._wizardData.inverter_type === "solaredge_storedge") {
      const detectedPrefixes = [];
      if (this._wizardData.pv_power_sensor) detectedPrefixes.push("i1");
      if (this._wizardData.pv_power_sensor_2) detectedPrefixes.push("i2");
      const prefixInfo = detectedPrefixes.length > 0
        ? `Erkannte Inverter: <strong>${detectedPrefixes.join(", ")}</strong>`
        : "Inverter-Erkennung läuft …";
      return `
        <div style="display:flex;gap:12px;padding:14px;border-left:3px solid var(--primary-color);background:var(--secondary-background-color);border-radius:6px">
          <ha-icon icon="mdi:battery-sync" style="--mdc-icon-size:28px;color:var(--primary-color);flex-shrink:0"></ha-icon>
          <div>
            <strong>SOC und Kapazität werden automatisch ermittelt.</strong>
            <div class="help-text" style="margin-top:6px">
              Bei SolarEdge liest die Integration die Werte pro Inverter
              direkt aus den b1-Sensoren der <code>solaredge_modbus_multi</code>-
              Integration und kombiniert sie:
              <ul style="margin:6px 0 4px 18px">
                <li>SOC = Σ(SOC<sub>i</sub> × Kapazität<sub>i</sub>) / Σ(Kapazität<sub>i</sub>)</li>
                <li>Kapazität = Σ(Kapazität<sub>i</sub>)</li>
              </ul>
              ${prefixInfo}.
              <br>Es werden zwei neue Sensoren angelegt:
              <code>sensor.eeg_energy_optimizer_combined_soc</code> und
              <code>sensor.eeg_energy_optimizer_combined_capacity</code>.
            </div>
          </div>
        </div>`;
    }

    // Huawei Master/Slave: SOC wird treiberseitig kombiniert (wie SolarEdge).
    // Da die Anlage keinen Kapazitäts-Sensor liefert, wird die Kapazität je
    // Batterie manuell eingetragen → gewichteter SOC + korrekte Gesamtkapazität.
    const huaweiDevs = (this._detectedSensors && this._detectedSensors.huawei_battery_devices) || [];
    if (this._wizardData.inverter_type === "huawei_sun2000" && huaweiDevs.length >= 2) {
      const caps = this._wizardData.huawei_battery_capacities || {};
      const fields = huaweiDevs.map(dev => `
        <div class="field-group" style="margin-bottom:10px">
          <label>Kapazität „${dev.name}" (kWh) *</label>
          <input type="number" data-field="huawei_cap_${dev.id}"
                 value="${caps[dev.id] || ""}" min="1" max="100" step="0.5" placeholder="z.B. 10">
        </div>`).join("");
      const sum = huaweiDevs.reduce((a, d) => a + (parseFloat(caps[d.id]) || 0), 0);
      return `
        ${detectionInfo}
        <div style="display:flex;gap:12px;padding:14px;border-left:3px solid var(--primary-color);background:var(--secondary-background-color);border-radius:6px;margin-bottom:16px">
          <ha-icon icon="mdi:battery-sync" style="--mdc-icon-size:28px;color:var(--primary-color);flex-shrink:0"></ha-icon>
          <div>
            <strong>${huaweiDevs.length} Batterien — SOC wird kombiniert.</strong>
            <div class="help-text" style="margin-top:6px">
              Der Ladestand wird kapazitätsgewichtet aus beiden Batterien berechnet
              (<code>sensor.eeg_energy_optimizer_combined_soc</code>). Diese Anlage
              liefert keinen Kapazitäts-Sensor — trage daher die nutzbare Kapazität
              je Batterie ein (steht meist im Gerätenamen):
            </div>
          </div>
        </div>
        ${fields}
        ${sum > 0
          ? `<div class="help-text">Gesamtkapazität: <strong>${sum} kWh</strong></div>`
          : `<div class="help-text" style="color:var(--warning-color,#ffa600)">Bitte die Kapazität beider Batterien eintragen.</div>`}`;
    }

    const socHelp =
      "Der SOC-Sensor zeigt den aktuellen Ladestand deiner Batterie in Prozent.";

    // Auto-select capacity mode: if sensor was detected, pick "sensor"; else "manual"
    // Re-evaluate after detection (don't cache stale pre-detection default)
    // SolaX has no capacity sensor — always default to manual
    if (this._wizardData.inverter_type === "solax_gen4" && !this._capacityModeUserSet) {
      this._capacityMode = "manual";
    } else if (!this._capacityMode || (detected && !this._capacityModeUserSet)) {
      this._capacityMode = this._wizardData.battery_capacity_sensor ? "sensor" : "manual";
    }
    const capSensor = this._capacityMode === "sensor";

    const capSensorHtml = capSensor ? this._entityPickerHtml(
      "battery_capacity_sensor",
      this._wizardData.battery_capacity_sensor,
      "Sensor für Batteriekapazität",
      this._wizardData.inverter_type === "huawei_sun2000"
        ? "Gesamtkapazität der Batterie in kWh oder Wh (Huawei: sensor.batterien_akkukapazitat)."
        : "Gesamtkapazität der Batterie in kWh oder Wh.",
      "sensor"
    ) : "";

    const capManualHtml = !capSensor ? `
      <div class="field-group">
        <label>Batteriekapazität (in kWh)</label>
        <input type="number" data-field="battery_capacity_kwh"
               value="${this._wizardData.battery_capacity_kwh || ""}"
               min="1" max="100" step="0.5"
               placeholder="z.B. 10">
        <div class="help-text">${this._wizardData.inverter_type === "huawei_sun2000"
          ? "z.B. 10 für LUNA2000-10, 15 für LUNA2000-15"
          : this._wizardData.inverter_type === "solax_gen4"
          ? "z.B. 5.8 für Triple Power T58, 11.6 für zwei Module"
          : this._wizardData.inverter_type === "solaredge_storedge"
          ? "z.B. 9.8 für LG RESU10H, 4.8 für BYD LVS 4.0"
          : this._wizardData.inverter_type === "sma_smart_energy"
          ? "z.B. 10.2 für BYD Battery-Box Premium HVS 10.2 (SMA liefert keinen Kapazitätssensor)"
          : "Nutzbare Gesamtkapazität deines Batteriespeichers in kWh"}</div>
      </div>` : "";

    return `
      ${detectionInfo}
      ${this._entityPickerHtml(
        "battery_soc_sensor",
        this._wizardData.battery_soc_sensor,
        "Sensor für Batterieladezustand (SOC) *",
        this._wizardData.inverter_type === "huawei_sun2000"
          ? "Der SOC-Sensor zeigt den aktuellen Ladestand deiner Batterie in Prozent (Huawei: sensor.batteries_batterieladung)."
          : "Der SOC-Sensor zeigt den aktuellen Ladestand deiner Batterie in Prozent.",
        "sensor"
      )}
      <div class="field-group">
        <label>Batteriekapazität *</label>
        <div class="cap-mode-cards">
          <div class="cap-mode-card ${!capSensor ? "selected" : ""}" data-action="set-cap-mode-card" data-value="manual">
            <ha-icon icon="mdi:pencil-box-outline"></ha-icon>
            <span>Manuell eingeben</span>
          </div>
          <div class="cap-mode-card ${capSensor ? "selected" : ""}" data-action="set-cap-mode-card" data-value="sensor">
            <ha-icon icon="mdi:auto-fix"></ha-icon>
            <span>Über Sensor</span>
          </div>
        </div>
        ${capSensor && this._wizardData.inverter_type === "huawei_sun2000" ? `<div class="help-text" style="margin-top:8px;margin-bottom:8px">
          Bei Huawei ist der Kapazitätssensor standardmäßig deaktiviert.
          <button class="btn-link" data-action="show-dialog" data-dialog="capacity_sensor">Anleitung zur Aktivierung</button>
        </div>` : ""}
      </div>
      ${capSensorHtml}
      ${capManualHtml}`;
  }

  /* ── Schritt 4: Fahrplan ────────────── */

  _controlHint(inverterType) {
    const gesteuert = SCHEDULE_CONTROL_INVERTERS.includes(inverterType);
    return gesteuert
      ? `<div class="help-text" style="margin-bottom:16px;padding:10px 12px;background:var(--success-color,#4caf50)18;border-left:3px solid var(--success-color,#4caf50);border-radius:4px">
           <ha-icon icon="mdi:check-circle-outline" style="--mdc-icon-size:16px;vertical-align:middle"></ha-icon>
           Dieser Wechselrichter wird vom Fahrplan gesteuert (Ladelimit und Entladung), sobald der Modus im Dashboard auf „Ein“ steht.
         </div>`
      : `<div class="help-text" style="margin-bottom:16px;padding:10px 12px;background:var(--info-color,#2196f3)18;border-left:3px solid var(--info-color,#2196f3);border-radius:4px">
           <ha-icon icon="mdi:information-outline" style="--mdc-icon-size:16px;vertical-align:middle"></ha-icon>
           Für diesen Wechselrichter wird der Fahrplan nur berechnet und angezeigt — die Steuerung ist derzeit nur für Huawei verfügbar.
         </div>`;
  }

  _fahrplanTarifFields(d, prefix) {
    // Tarif- und Anlagenfelder des Fahrplans — identisch im Wizard (prefix="")
    // und in den Einstellungen (prefix="settings_").
    return `
      <div class="field-group">
        <label>Einspeisevergütung (€/kWh)</label>
        <input type="number" data-field="${prefix}schedule_feedin_price"
               value="${d.schedule_feedin_price ?? 0.082}" min="0" max="2" step="0.001">
        <div class="help-text">Was du für eingespeiste Energie bekommst. Der Fahrplan hält sie gegen den Bezugspreis und entscheidet danach, ob eine Kilowattstunde besser ins Netz geht oder in die Batterie.</div>
      </div>
      <div class="field-group">
        <label>Einspeisevergütung nachts (€/kWh)</label>
        <input type="number" data-field="${prefix}schedule_feedin_price_night"
               value="${d.schedule_feedin_price_night ?? 0.102}" min="0" max="2" step="0.001">
        <div class="help-text">Höherer Tarif für Nachteinspeisung. Auf 0 setzen, wenn es nur einen Tarif gibt. Entscheidend ist nicht die Höhe, sondern der Abstand zum Tagtarif — schon zwei Cent genügen, damit Energie in die Nacht verschoben wird.</div>
      </div>
      <div style="display:flex;gap:12px">
        <div class="field-group" style="flex:1">
          <label>Nachttarif von</label>
          <input type="time" data-field="${prefix}schedule_night_start" value="${d.schedule_night_start || "22:00"}">
        </div>
        <div class="field-group" style="flex:1">
          <label>Nachttarif bis</label>
          <input type="time" data-field="${prefix}schedule_night_end" value="${d.schedule_night_end || "06:00"}">
        </div>
      </div>
      <div class="help-text" style="margin-top:-8px;margin-bottom:12px">Zeitfenster, in dem der Nachttarif gilt. Darf über Mitternacht gehen.</div>
      <div class="field-group">
        <label>Bezugspreis (€/kWh)</label>
        <input type="number" data-field="${prefix}schedule_consumption_price"
               value="${d.schedule_consumption_price ?? 0.247}" min="0" max="2" step="0.001">
        <div class="help-text">Dein Arbeitspreis inklusive Netz und Abgaben. Solange er klar über der Einspeisevergütung liegt, ist die genaue Höhe unwichtig — erst wenn sich beide annähern, ändert sich das Verhalten grundlegend.</div>
      </div>
      <div class="field-group">
        <label>Batterie-Leistungsgrenze (kW)</label>
        <input type="number" data-field="${prefix}discharge_power_kw"
               value="${d.discharge_power_kw ?? 5.0}" min="0.5" max="20" step="0.5">
        <div class="help-text">Wie viel Leistung die Batterie höchstens aufnehmen oder abgeben kann. Der Fahrplan plant nie darüber — ein zu kleiner Wert verschenkt Möglichkeiten, ein zu großer erzeugt Pläne, die der Wechselrichter nicht erfüllt.</div>
      </div>
      <div class="field-group">
        <label>Notstrom-Reserve (kWh)</label>
        <input type="number" data-field="${prefix}schedule_blackout_reserve_kwh"
               value="${d.schedule_blackout_reserve_kwh ?? 0}" min="0" max="100" step="0.5">
        <div class="help-text">Energie, die für einen Stromausfall zurückgehalten wird. 0 heißt: keine Reserve, die Batterie steht voll für die Optimierung zur Verfügung. Nicht zu verwechseln mit einer Nachtreserve — die ergibt sich von selbst, weil Netzbezug teurer ist als Einspeisung.</div>
      </div>`;
  }

  _peakshareFields(d, prefix) {
    const on = d.enable_peakshare !== false;
    const communities = this._peakshareCommunitiesCache || [];
    const selected = d.peakshare_community || "BEG";
    const dropdown = communities.length === 0
      ? `<div class="help-text" style="margin-top:4px">Communities werden geladen...</div>`
      : `<div class="field-group">
          <label>Deine Energiegemeinschaft</label>
          <select data-field="${prefix}peakshare_community">${communities.map(c => `<option value="${c}" ${c === selected ? "selected" : ""}>${c}</option>`).join("")}</select>
          <div class="help-text">Welche Gemeinschaft angezeigt wird.</div>
        </div>`;
    return `
      <label style="display:flex;align-items:flex-start;gap:8px;cursor:pointer;margin:16px 0 12px">
        <input type="checkbox" data-field="${prefix}enable_peakshare" ${on ? "checked" : ""}>
        <div>
          <div style="font-weight:500">Gemeinschaftsdaten abrufen (PeakShare)</div>
          <div class="help-text" style="margin-top:2px">Holt die Bedarfsprognose der Energiegemeinschaft und zeigt sie im Dashboard. Steuert derzeit nichts — sie wird zur Grundlage der Preisfunktion, sobald diese gebaut ist.</div>
        </div>
      </label>
      ${on ? dropdown : ""}`;
  }

  _exportLimitFields(d, prefix) {
    const on = !!d.grid_export_limit_enabled;
    const limitParams = on ? `
      <div class="feature-params">
        <div class="field-group">
          <label>Höhe der Grenze (kW) *</label>
          <input type="number" data-field="${prefix}grid_export_limit_kw"
                 value="${d.grid_export_limit_kw ?? 4}" min="0.1" max="100" step="0.1" placeholder="z.B. 4">
          <div class="help-text">Maximale Einspeiseleistung am Netzanschlusspunkt. Muss dem Wert im Wechselrichter entsprechen — eine Grenze, die es dort nicht gibt, verschenkt Einspeisung; eine, die wir nicht kennen, kostet Ertrag durch stille Abregelung.</div>
        </div>
      </div>` : "";
    const featureAction = prefix ? "toggle-settings-feature" : "toggle-feature";
    return `
      <div class="feature-toggle">
        <div class="feature-card ${on ? "selected" : ""}" data-action="${featureAction}" data-feature="grid_export_limit_enabled" style="cursor:pointer">
          <div class="feature-card-header">
            <ha-icon icon="mdi:transmission-tower-export"></ha-icon>
            <div class="feature-card-text">
              <span class="feature-title">Einspeisegrenze beachten</span>
              <span class="feature-desc">Einschalten, wenn der Wechselrichter die Einspeisung begrenzt. Der Fahrplan plant dann so, dass möglichst nichts abgeregelt wird, und hebt das Ladelimit an, wenn die Einspeisung trotzdem an die Grenze stößt.</span>
            </div>
            <div class="feature-badge ${on ? "on" : "off"}">${on ? "Aktiv" : "Aus"}</div>
          </div>
          <div style="text-align:center;font-size:12px;color:var(--secondary-text-color);margin-top:4px">Zum ${on ? "Deaktivieren" : "Aktivieren"} hier klicken</div>
        </div>
        ${limitParams}
      </div>
      ${this._exportLimitPlausibility(d)}
      <div class="field-group" style="margin-top:16px">
        <label>AC-Grenzleistung des Wechselrichters (kW, optional)</label>
        <input type="number" data-field="${prefix}inverter_ac_limit_kw"
               value="${d.inverter_ac_limit_kw || ""}" min="0" max="200" step="0.1" placeholder="aus PV-Spitze">
        <div class="help-text">Nennleistung des Wechselrichters auf der Netzseite. Begrenzt im Fahrplan die Summe aus Einspeisung und Hausverbrauch. Ohne Angabe wird die PV-Spitzenleistung als Näherung verwendet.</div>
      </div>
      <div class="field-group">
        <label>PV-Spitzenleistung (kWp, optional)</label>
        <input type="number" data-field="${prefix}pv_peak_kwp"
               value="${d.pv_peak_kwp || ""}" min="0" max="200" step="0.1" placeholder="z.B. 9.9">
        <div class="help-text">Anlagenleistung in kWp. Dient der Plausibilitätsprüfung der Prognosewerte und als Rückfall für die AC-Grenzleistung.</div>
      </div>`;
  }

  _exportLimitPlausibility(d) {
    // Huawei verrät über sensor.inverter_active_power_control, ob im Gerät
    // eine Exportbegrenzung aktiv ist. Weicht das von der Konfiguration ab,
    // sucht man sonst lange, warum abgeregelt wird oder Guard 1 grundlos anhebt.
    const state = this._readState("sensor.inverter_active_power_control")
      || this._readState("sensor.wechselrichter_active_power_control");
    if (!state) return "";
    const raw = String(state.state || "").toLowerCase();
    const deviceLimited = !(raw.startsWith("unlimited") || raw.startsWith("unbegrenzt") || raw === "");
    const configured = !!d.grid_export_limit_enabled;
    if (deviceLimited === configured) return "";
    const text = deviceLimited
      ? `Der Wechselrichter meldet eine aktive Exportbegrenzung („${this._escapeHtml(state.state)}“), hier ist aber keine Einspeisegrenze konfiguriert — der Fahrplan plant dann Einspeisung, die still abgeregelt wird.`
      : `Hier ist eine Einspeisegrenze konfiguriert, der Wechselrichter meldet aber „${this._escapeHtml(state.state)}“ — Guard 1 würde das Ladelimit grundlos anheben.`;
    return `<div class="help-text" style="margin-top:12px;padding:10px 12px;background:var(--warning-color,#ff9800)22;border-left:3px solid var(--warning-color,#ff9800);border-radius:4px">
      <ha-icon icon="mdi:alert-outline" style="--mdc-icon-size:16px;vertical-align:middle"></ha-icon> ${text}
    </div>`;
  }


}
