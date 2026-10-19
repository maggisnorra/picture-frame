(() => {
  "use strict";
  const byId = (id) => document.getElementById(id);
  const card = byId("brightnessCard");
  if (!card) return;
  const controls = byId("brightnessControls");
  const slider = byId("brightnessSlider");
  const value = byId("brightnessValue");
  const day = byId("brightnessDay");
  const night = byId("brightnessNight");
  const error = byId("brightnessError");
  const url = `/api/${encodeURIComponent(document.body.dataset.self)}/brightness`;
  let sending = false;
  const queue = [];
  let editingSlider = false;
  let settingsDirty = false;
  let revision = 0;

  function dateLabel(raw, timezone) {
    return new Date(raw).toLocaleString("is-IS", timezone ? { timeZone: timezone } : undefined);
  }

  function render(state) {
    controls.disabled = false;
    const labels = { disabled: "Birtustýring óvirk", override: "Handvirk birta", automatic: "Sjálfvirk birta" };
    byId("brightnessStatus").textContent = labels[state.mode] || state.mode;
    if (!editingSlider && queue.length === 0) {
      const shown = state.level_percent;
      slider.value = String(Math.round(shown));
      value.textContent = `${Math.round(shown)}%`;
    }
    if (!settingsDirty) {
      day.value = state.day_percent;
      night.value = state.night_percent;
    }
    const details = [];
    details.push(`Birta myndefnis: ${state.level_percent}%`);
    if (state.override) details.push(`Sjálfvirk birta hefst aftur: ${dateLabel(state.override.expires_at, state.location?.timezone)}`);
    if (state.next_transition) details.push(`Næsta sólarbreyting: ${dateLabel(state.next_transition, state.location?.timezone)}`);
    if (state.location) {
      const sources = { config_override: "stilling", ip_cache: "IP-áætlun", config_fallback: "varastilling" };
      details.push(`Staðsetning (${sources[state.location_source] || state.location_source}): ${state.location.latitude.toFixed(2)}, ${state.location.longitude.toFixed(2)} · ${state.location.timezone}`);
    }
    byId("brightnessDetails").textContent = details.join("\n");
    error.textContent = Object.values(state.errors || {}).filter(Boolean).join(" · ");
    card.setAttribute("aria-busy", "false");
  }

  async function request(method, body) {
    const response = await fetch(url, {
      method, cache: "no-store",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}: ${await response.text()}`);
    return response.json();
  }

  async function refresh() {
    const started = revision;
    if (sending) return;
    try {
      const state = await request("GET");
      if (started === revision && !sending) render(state);
    } catch (err) {
      if (started !== revision || sending) return;
      error.textContent = `Ekki tókst að sækja birtustillingar: ${err.message}`;
      controls.disabled = true;
      card.setAttribute("aria-busy", "false");
    }
  }

  // Serialize all actions; coalesce only adjacent queued slider values.
  async function send(body) {
    if (body.action === "manual" && queue.at(-1)?.action === "manual") queue[queue.length - 1] = body;
    else queue.push(body);
    revision += 1;
    if (sending) return;
    sending = true;
    card.setAttribute("aria-busy", "true");
    try {
      while (queue.length) {
        const next = queue.shift();
        try {
          const state = await request("PUT", next);
          if (queue.length === 0) render(state);
        } catch (err) {
          error.textContent = `Ekki tókst að breyta birtu: ${err.message}`;
          // Preserve newer queued input; a failed older request must not discard it.
        }
      }
    } finally {
      sending = false;
      card.setAttribute("aria-busy", "false");
    }
  }

  slider.addEventListener("input", () => {
    editingSlider = true;
    value.textContent = `${slider.value}%`;
  });
  slider.addEventListener("change", () => {
    editingSlider = false;
    send({ action: "manual", manual_percent: Number(slider.value) });
  });
  slider.addEventListener("blur", () => { editingSlider = false; });
  for (const input of [day, night]) input.addEventListener("input", () => { settingsDirty = true; });
  byId("brightnessSettings").addEventListener("submit", async (event) => {
    event.preventDefault();
    const values = [Number(day.value), Number(night.value)];
    if (values.some((n) => !Number.isInteger(n) || n < 5 || n > 100)) {
      error.textContent = "Veldu heilar tölur á bilinu 5 til 100%.";
      return;
    }
    settingsDirty = false;
    await send({ action: "settings", day_percent: values[0], night_percent: values[1] });
  });
  byId("brightnessResume").addEventListener("click", () => send({ action: "resume" }));
  byId("brightnessDisable").addEventListener("click", () => send({ action: "settings", enabled: false }));
  refresh();
  // Existing remote status polling uses its own endpoint; keep brightness polling independent.
  window.setInterval(() => { if (!document.hidden) refresh(); }, 5000);
})();
