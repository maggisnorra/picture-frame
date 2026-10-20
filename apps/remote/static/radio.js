(() => {
  "use strict";
  const byId = (id) => document.getElementById(id);
  if (!byId("radioCard")) return;
  const url = `/api/${encodeURIComponent(document.body.dataset.self)}/radio`;
  const controls = byId("radioControls");
  const container = byId("radioSchedules");
  const error = byId("radioError");
  const station = byId("radioStation");
  const playbackButtons = [byId("radioPlay"), byId("radioStop")];
  const ras2 = "https://ruv-radio-live.akamaized.net/streymi/ras2/ras2.m3u8";
  const weekdays = ["Mánudagur", "Þriðjudagur", "Miðvikudagur", "Fimmtudagur", "Föstudagur", "Laugardagur", "Sunnudagur"];
  let busy = false;
  let loaded = false;
  let polling = false;
  let revision = 0;
  let activeTimezone = null;

  function showError(message) {
    error.textContent = message || "";
    error.hidden = !message;
  }

  function updatePlaybackButtons() {
    playbackButtons.forEach((button) => { button.disabled = busy || !loaded; });
  }

  async function request(method, body, action = "") {
    const response = await fetch(url + action, {
      method, cache: "no-store",
      ...(body ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}),
    });
    const data = await response.json();
    if (!response.ok) {
      const detail = data.detail;
      throw new Error(Array.isArray(detail) ? detail.map((item) => `${item.loc.slice(1).join(".")}: ${item.msg}`).join("; ") : detail || `HTTP ${response.status}`);
    }
    return data;
  }

  function renderStatus(state) {
    activeTimezone = state.timezone || null;
    const timezoneHint = byId("radioTimezoneHint");
    if (timezoneHint) {
      timezoneHint.textContent = activeTimezone
        ? `Útvarpstímar nota ${activeTimezone}, sama tímabelti og sólarupprás og sólsetur.`
        : "Útvarpstímar bíða eftir staðsetningu og tímabelti rammans.";
    }
    byId("radioAdd").disabled = !activeTimezone;
    let status;
    if (state.error) status = "Ekki tókst að spila útvarp";
    else if (state.mode === "manual") {
      const until = new Intl.DateTimeFormat("is-IS", { ...(activeTimezone ? { timeZone: activeTimezone } : {}), weekday: "short", hour: "2-digit", minute: "2-digit" }).format(new Date(state.manual_until));
      status = `Handvirk spilun · stöðvast sjálfkrafa ${until}`;
    } else if (state.mode === "stopping") status = "Útvarp að stöðvast…";
    else status = !activeTimezone ? "Bíður eftir staðsetningu" : state.playing ? "Útvarp í gangi" : state.active ? "Útvarp að ræsa…" : "Útvarp bíður næsta tíma";
    byId("radioStatus").textContent = status;
    showError([state.error, state.storage_error].filter(Boolean).join(" · "));
  }

  function addSchedule(entry) {
    const row = document.createElement("div");
    row.className = "radio-schedule border rounded p-3";
    row.dataset.id = entry.id;
    const uiKey = crypto.randomUUID();
    // Only the generated UI key enters HTML. Saved values are assigned as properties.
    row.innerHTML = `
      <div class="row g-2 align-items-end">
        <div class="col-12 col-sm-6 col-lg-3"><label class="form-label" for="radio-date-${uiKey}">Upphafsdagur</label><input id="radio-date-${uiKey}" data-field="start_date" class="form-control" type="date" required></div>
        <div class="col-12 col-sm-6 col-lg-3"><label class="form-label" for="radio-day-${uiKey}">Vikudagur</label><select id="radio-day-${uiKey}" data-field="weekday" class="form-select"></select></div>
        <div class="col-6 col-lg-2"><label class="form-label" for="radio-time-${uiKey}">Upphafstími</label><input id="radio-time-${uiKey}" data-field="start_time" class="form-control" type="time" required></div>
        <div class="col-6 col-lg-2"><label class="form-label" for="radio-duration-${uiKey}">Lengd (mín.)</label><input id="radio-duration-${uiKey}" data-field="duration_minutes" class="form-control" type="number" min="1" max="1440" step="1" required></div>
        <div class="col-12 col-lg-2"><button data-remove class="btn btn-outline-danger" type="button">Fjarlægja</button></div>
      </div>
      <label class="form-check-label mt-2"><input data-field="enabled" class="form-check-input me-2" type="checkbox">Virkur</label>`;
    const day = row.querySelector('[data-field="weekday"]');
    weekdays.forEach((name, value) => day.add(new Option(name, String(value))));
    ["start_date", "weekday", "start_time", "duration_minutes"].forEach((key) => {
      row.querySelector(`[data-field="${key}"]`).value = entry[key];
    });
    row.querySelector('[data-field="enabled"]').checked = entry.enabled;
    row.querySelector("[data-remove]").addEventListener("click", () => row.remove());
    container.appendChild(row);
  }

  function renderSettings(state) {
    station.value = state.settings.station_url;
    container.replaceChildren();
    state.settings.schedules.forEach((entry) => addSchedule(entry));
    controls.disabled = false;
    loaded = true;
    renderStatus(state);
  }

  async function load() {
    if (busy) return;
    revision += 1;
    busy = true;
    updatePlaybackButtons();
    controls.disabled = true;
    byId("radioCard").setAttribute("aria-busy", "true");
    try { renderSettings(await request("GET")); }
    catch (err) { showError(`Ekki tókst að sækja útvarpsstillingar: ${err.message}`); }
    finally {
      busy = false;
      updatePlaybackButtons();
      controls.disabled = !loaded;
      byId("radioCard").setAttribute("aria-busy", "false");
    }
  }

  byId("radioRas2").addEventListener("click", () => { station.value = ras2; });
  byId("radioReload").addEventListener("click", load);
  async function playback(action) {
    if (busy || !loaded) return;
    revision += 1;
    busy = true;
    updatePlaybackButtons();
    try { renderStatus(await request("POST", null, `/${action}`)); }
    catch (err) { showError(`Ekki tókst að ${action === "play" ? "spila" : "stöðva"} útvarp: ${err.message}`); }
    finally { busy = false; updatePlaybackButtons(); }
  }
  byId("radioPlay").addEventListener("click", () => playback("play"));
  byId("radioStop").addEventListener("click", () => playback("stop"));
  byId("radioAdd").addEventListener("click", () => {
    if (!activeTimezone) { showError("Bíddu eftir staðsetningu og tímabelti rammans."); return; }
    let startDate;
    try {
      const parts = new Intl.DateTimeFormat("en", { timeZone: activeTimezone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date());
      const part = (type) => parts.find((item) => item.type === type).value;
      startDate = `${part("year")}-${part("month")}-${part("day")}`;
    } catch { showError("Ekki tókst að lesa tímabelti rammans. Sæktu stöðuna aftur."); return; }
    addSchedule({ id: crypto.randomUUID(), start_date: startDate, weekday: (new Date(`${startDate}T12:00:00Z`).getUTCDay() + 6) % 7, start_time: "07:00", duration_minutes: 30, enabled: true });
  });

  byId("radioForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy || !loaded) return;
    const schedules = Array.from(container.children).map((row) => {
      const field = (key) => row.querySelector(`[data-field="${key}"]`);
      return { id: row.dataset.id, start_date: field("start_date").value, weekday: Number(field("weekday").value), start_time: field("start_time").value, duration_minutes: Number(field("duration_minutes").value), enabled: field("enabled").checked };
    });
    const settings = { station_url: station.value, schedules };
    revision += 1;
    busy = true;
    updatePlaybackButtons();
    controls.disabled = true;
    try {
      renderSettings(await request("PUT", settings));
      byId("radioStatus").textContent = "Útvarpsstillingar vistaðar";
    } catch (err) { showError(`Ekki tókst að vista útvarpsstillingar: ${err.message}`); }
    finally { busy = false; controls.disabled = false; updatePlaybackButtons(); }
  });

  load();
  window.setInterval(async () => {
    if (busy || polling || !loaded) return;
    polling = true;
    const requestRevision = revision;
    try {
      const state = await request("GET");
      if (requestRevision === revision && !busy) renderStatus(state);
    } catch (err) {
      if (requestRevision === revision && !busy) showError(`Ekki tókst að sækja stöðu útvarps: ${err.message}`);
    } finally { polling = false; }
  }, 15000);
})();
