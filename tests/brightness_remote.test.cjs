// Browser-free behavior tests for the real remote script, using a minimal DOM surface.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../apps/remote/static/brightness.js"), "utf8");

function state(extra = {}) {
  return { enabled: true, method: "software", mode: "automatic",
    day_percent: 80, night_percent: 15, level_percent: 80,
    errors: {}, ...extra };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

function setup(respond) {
  const elements = new Map();
  const calls = [];
  const intervals = [];
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value: "", textContent: "", disabled: true, hidden: true, listeners: {}, attributes: {},
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(name, handler) { this.listeners[name] = handler; },
      fire(name) { return this.listeners[name]?.({ preventDefault() {} }); },
    });
    return elements.get(id);
  }
  vm.runInNewContext(source, {
    document: { body: { dataset: { self: "adam" } }, getElementById: element, hidden: false },
    window: { setInterval(callback) { intervals.push(callback); } },
    fetch: async (url, init) => {
      calls.push({ url, ...init, body: init.body ? JSON.parse(init.body) : null });
      return respond ? respond(calls.at(-1), calls.length) : { ok: true, json: async () => state() };
    },
  });
  return { element, calls, poll: () => intervals[0]() };
}

test("renders software level, own-frame URL and location", async () => {
  const ui = setup(async () => ({ ok: true, json: async () => state({
    level_percent: 35, override: { percent: 35, expires_at: "2026-10-16T17:00:00Z" }, mode: "override",
    location: { latitude: 52.37, longitude: 4.90, timezone: "Europe/Amsterdam" }, location_source: "ip_cache",
  }) }));
  await flush();
  assert.equal(ui.calls[0].url, "/api/adam/brightness");
  assert.equal(ui.element("brightnessSlider").value, "35");
  assert.match(ui.element("brightnessDetails").textContent, /35%/);
  assert.match(ui.element("locationSource").textContent, /IP-áætlun/);
  assert.equal(ui.element("locationTimezone").textContent, "Europe/Amsterdam");
});

test("software controls are usable without hardware capability flags", async () => {
  const ui = setup();
  await flush();
  assert.equal(ui.element("brightnessControls").disabled, false);
  assert.equal(ui.element("brightnessSlider").value, "80");
});

test("slider mutations serialize and newest queued value wins", async () => {
  let finish;
  const ui = setup(async (call) => {
    if (call.method === "PUT" && !finish) await new Promise((resolve) => { finish = resolve; });
    return { ok: true, json: async () => state({ level_percent: call.body?.manual_percent ?? 80 }) };
  });
  await flush();
  const slider = ui.element("brightnessSlider");
  for (const percent of [30, 35, 45]) {
    slider.value = String(percent);
    slider.fire("input");
    slider.fire("change");
  }
  assert.equal(ui.calls.length, 2); // Initial GET and the first PUT.
  finish();
  await flush();
  const puts = ui.calls.filter((call) => call.method === "PUT");
  assert.deepEqual(puts.map((call) => call.body.manual_percent), [30, 45]);
  assert.equal(slider.value, "45");
});

test("settings and disable/resume actions are never discarded by queued slider changes", async () => {
  let finish;
  const ui = setup(async (call) => {
    if (call.method === "PUT" && !finish) await new Promise((resolve) => { finish = resolve; });
    return { ok: true, json: async () => state() };
  });
  await flush();
  ui.element("brightnessSlider").value = "30";
  ui.element("brightnessSlider").fire("change");
  ui.element("brightnessDay").value = "70";
  ui.element("brightnessNight").value = "10";
  ui.element("brightnessSettings").fire("submit");
  ui.element("brightnessResume").fire("click");
  ui.element("brightnessDisable").fire("click");
  ui.element("brightnessSlider").value = "50";
  ui.element("brightnessSlider").fire("change");
  finish();
  await flush();
  assert.deepEqual(ui.calls.filter((call) => call.method === "PUT").map((call) => call.body), [
    { action: "manual", manual_percent: 30 },
    { action: "settings", day_percent: 70, night_percent: 10 },
    { action: "resume" }, { action: "settings", enabled: false }, { action: "manual", manual_percent: 50 },
  ]);
});

test("stale polling cannot overwrite a newer command response", async () => {
  let finish;
  const ui = setup(async (call, number) => {
    if (number === 2) await new Promise((resolve) => { finish = resolve; });
    return { ok: true, json: async () => state({ level_percent: call.method === "PUT" ? 25 : 80 }) };
  });
  await flush();
  ui.poll();
  ui.element("brightnessSlider").value = "25";
  ui.element("brightnessSlider").fire("change");
  await flush();
  finish();
  await flush();
  assert.equal(ui.element("brightnessSlider").value, "25");
});

test("polling preserves unfinished settings and shows offline errors", async () => {
  let offline = false;
  const ui = setup(async () => {
    if (offline) throw new Error("offline");
    return { ok: true, json: async () => state() };
  });
  await flush();
  ui.element("brightnessDay").value = "65";
  ui.element("brightnessDay").fire("input");
  ui.poll();
  await flush();
  assert.equal(ui.element("brightnessDay").value, "65");
  offline = true;
  ui.poll();
  await flush();
  assert.match(ui.element("brightnessError").textContent, /offline/);
  assert.equal(ui.element("brightnessControls").disabled, true);
});

test("rejects invalid settings without sending commands", async () => {
  const ui = setup();
  await flush();
  ui.element("brightnessDay").value = "101";
  await ui.element("brightnessSettings").fire("submit");
  assert.equal(ui.calls.length, 1);
  assert.match(ui.element("brightnessError").textContent, /5 til 100/);
});

test("separate location card shows geolocation and timezone without debug mode", async () => {
  const ui = setup(async () => ({ ok: true, json: async () => state({
    location: { latitude: 52.37, longitude: 4.90, timezone: "Europe/Amsterdam", city: "Amsterdam", country: "Netherlands" },
    location_source: "ip_cache", local_time: "2026-10-20T07:00:00+02:00",
    location_fetched_at: "2026-10-20T04:00:00Z", next_transition: "2026-10-20T06:13:00Z",
  }) }));
  await flush();
  assert.equal(ui.element("locationName").textContent, "Amsterdam, Netherlands");
  assert.equal(ui.element("locationTimezone").textContent, "Europe/Amsterdam");
  assert.equal(ui.element("locationCoordinates").textContent, "52.3700, 4.9000");
  assert.match(ui.element("locationLocalTime").textContent, /07:00/);
  assert.notEqual(ui.element("locationUpdated").textContent, "—");
  assert.notEqual(ui.element("locationSunChange").textContent, "—");
  assert.equal(ui.element("locationError").hidden, true);
});

test("location card exposes missing geolocation, cached lookup errors and offline status", async () => {
  let response = state({ location: null });
  let offline = false;
  const ui = setup(async () => {
    if (offline) throw new Error("offline");
    return { ok: true, json: async () => response };
  });
  await flush();
  assert.equal(ui.element("locationTimezone").textContent, "—");
  assert.match(ui.element("locationStatus").textContent, /bíða eftir tímabelti/);
  response = state({ location: { latitude: 52.37, longitude: 4.9, timezone: "Europe/Amsterdam" }, location_source: "ip_cache", errors: { location: "lookup failed" } });
  await ui.poll();
  await flush();
  assert.equal(ui.element("locationTimezone").textContent, "Europe/Amsterdam");
  assert.equal(ui.element("locationError").hidden, false);
  offline = true;
  await ui.poll();
  await flush();
  assert.match(ui.element("locationStatus").textContent, /síðast þekktu/);
  assert.equal(ui.element("locationTimezone").textContent, "Europe/Amsterdam");
});
