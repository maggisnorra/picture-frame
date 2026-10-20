const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { randomUUID } = require("node:crypto");
const source = fs.readFileSync(path.join(__dirname, "../apps/remote/static/radio.js"), "utf8");
const template = fs.readFileSync(path.join(__dirname, "../apps/remote/templates/remote.html"), "utf8");
const templateIds = new Set([...template.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]));
const flush = () => new Promise((resolve) => setImmediate(resolve));
const stationUrl = "https://example.com/live.mp3";
const entry = { id: "saved-id", start_date: "2026-10-19", weekday: 0, start_time: "07:00", duration_minutes: 30, enabled: true };
const state = () => ({ settings: { station_url: stationUrl, schedules: [{ ...entry }] }, timezone: "Atlantic/Reykjavik", active: false, playing: false });

function setup(respond, extraIds = []) {
  const elements = new Map(), calls = [], intervals = [], html = [];
  const availableIds = new Set([...templateIds, ...extraIds]);
  function element(id) {
    if (elements.has(id)) return elements.get(id);
    const el = {
      value: "", textContent: "", disabled: true, hidden: false, dataset: {}, children: [], fields: {}, listeners: {},
      setAttribute() {}, addEventListener(name, handler) { this.listeners[name] = handler; },
      fire(name) { return this.listeners[name]?.({ preventDefault() {} }); },
      set innerHTML(value) { html.push(value); },
      querySelector(selector) { return this.fields[selector] ??= element(`${id}-${selector}`); },
      replaceChildren() { this.children = []; },
      appendChild(row) { row.parent = this; this.children.push(row); },
      remove() { this.parent.children = this.parent.children.filter((row) => row !== this); },
      add() {},
    };
    elements.set(id, el);
    return el;
  }
  vm.runInNewContext(source, {
    document: { body: { dataset: { self: "adam" } }, getElementById: (id) => availableIds.has(id) ? element(id) : null, createElement: () => element(randomUUID()) },
    window: { setInterval(callback) { intervals.push(callback); } },
    crypto: { randomUUID }, Option: function () {}, Intl, Date,
    fetch: async (url, init) => {
      const call = { url, ...init, body: init.body ? JSON.parse(init.body) : null };
      calls.push(call);
      return respond ? respond(call, calls.length) : { ok: true, json: async () => state() };
    },
  });
  return { element, calls, html, poll: () => intervals[0]() };
}

test("loads own frame, edits weekly times and sends the complete settings", async () => {
  const ui = setup();
  await flush();
  assert.equal(ui.calls[0].url, "/api/adam/radio");
  const rows = ui.element("radioSchedules");
  rows.children[0].querySelector('[data-field="duration_minutes"]').value = "25";
  ui.element("radioAdd").fire("click");
  assert.equal(rows.children.length, 2);
  await ui.element("radioForm").fire("submit");
  const put = ui.calls.at(-1);
  assert.equal(put.method, "PUT");
  assert.equal(put.body.station_url, stationUrl);
  assert.equal("timezone" in put.body, false);
  assert.equal(put.body.schedules[0].duration_minutes, 25);
  assert.equal(put.body.schedules[1].start_time, "07:00");
});

test("polling preserves unsaved edits and deleting a time is saved", async () => {
  const ui = setup();
  await flush();
  ui.element("radioStation").value = "https://example.org/custom.m3u8";
  await ui.poll();
  assert.equal(ui.element("radioStation").value, "https://example.org/custom.m3u8");
  ui.element("radioSchedules").children[0].querySelector("[data-remove]").fire("click");
  await ui.element("radioForm").fire("submit");
  assert.deepEqual(ui.calls.at(-1).body.schedules, []);
});

test("failed loads leave controls disabled and can be retried", async () => {
  let offline = true;
  const ui = setup(async () => {
    if (offline) throw new Error("offline");
    return { ok: true, json: async () => state() };
  });
  await flush();
  assert.equal(ui.element("radioControls").disabled, true);
  assert.match(ui.element("radioError").textContent, /offline/);
  offline = false;
  await ui.element("radioReload").fire("click");
  assert.equal(ui.element("radioControls").disabled, false);
});

test("stored schedule IDs are never interpolated into HTML", async () => {
  const payload = '<img src=x onerror="alert(1)">';
  const ui = setup(async () => {
    const data = state();
    data.settings.schedules[0].id = payload;
    return { ok: true, json: async () => data };
  });
  await flush();
  assert.equal(ui.element("radioSchedules").children[0].dataset.id, payload);
  assert.equal(ui.html.some((value) => value.includes(payload)), false);
});

test("saving during a status poll succeeds and stale status cannot replace save feedback", async () => {
  let finish;
  const ui = setup(async (call, number) => {
    if (number === 2) await new Promise((resolve) => { finish = resolve; });
    return { ok: true, json: async () => state() };
  });
  await flush();
  const poll = ui.poll();
  await ui.element("radioForm").fire("submit");
  assert.equal(ui.calls.at(-1).method, "PUT");
  const saved = ui.element("radioStatus").textContent;
  finish();
  await poll;
  assert.equal(ui.element("radioStatus").textContent, saved);
});

test("radio follows the frame timezone from status and waits when geolocation is missing", async () => {
  let timezone = null;
  const ui = setup(async () => ({ ok: true, json: async () => ({ ...state(), timezone }) }));
  await flush();
  assert.equal(ui.element("radioAdd").disabled, true);
  assert.match(ui.element("radioStatus").textContent, /Bíður eftir staðsetningu/);
  timezone = "Europe/Amsterdam";
  await ui.poll();
  assert.equal(ui.element("radioAdd").disabled, false);
  assert.equal(ui.element("radioError").hidden, true);
  ui.element("radioAdd").fire("click");
  assert.equal(ui.element("radioSchedules").children.length, 2);
});

test("radio loads and polls with the actual template when the optional timezone hint is absent", async () => {
  const ui = setup();
  await flush();
  assert.equal(ui.element("radioError").hidden, true);
  assert.equal(ui.element("radioStatus").textContent, "Útvarp bíður næsta tíma");
  assert.equal(ui.element("radioAdd").disabled, false);
  await ui.poll();
  assert.equal(ui.element("radioError").hidden, true);
});

test("timezone hint is updated when included in a page", async () => {
  const ui = setup(undefined, ["radioTimezoneHint"]);
  await flush();
  assert.match(ui.element("radioTimezoneHint").textContent, /Atlantic\/Reykjavik/);
  assert.equal(ui.element("radioError").hidden, true);
});

test("play and stop use own-frame commands without saving or replacing edits", async () => {
  const ui = setup(async (call) => {
    const data = state();
    if (call.url.endsWith("/play")) Object.assign(data, { mode: "manual", manual_until: "2026-10-20T15:00:00Z", playing: true });
    if (call.url.endsWith("/stop")) data.mode = "stopping";
    return { ok: true, json: async () => data };
  });
  await flush();
  ui.element("radioStation").value = "https://example.org/unsaved.mp3";
  assert.equal(ui.element("radioPlay").disabled, false);
  await ui.element("radioPlay").fire("click");
  assert.equal(ui.calls.at(-1).url, "/api/adam/radio/play");
  assert.equal(ui.calls.at(-1).method, "POST");
  assert.equal(ui.calls.at(-1).body, null);
  assert.match(ui.element("radioStatus").textContent, /Handvirk spilun.*stöðvast sjálfkrafa/);
  assert.equal(ui.element("radioStation").value, "https://example.org/unsaved.mp3");
  await ui.element("radioPlay").fire("click");
  assert.equal(ui.calls.at(-1).url, "/api/adam/radio/play");
  await ui.element("radioStop").fire("click");
  assert.equal(ui.calls.at(-1).url, "/api/adam/radio/stop");
  assert.match(ui.element("radioStatus").textContent, /stöðvast/);
  assert.equal(ui.element("radioStop").disabled, false);
});

test("play is available and manual status is visible without a timezone", async () => {
  const ui = setup(async (call) => ({ ok: true, json: async () => ({
    ...state(), timezone: null,
    ...(call.method === "POST" ? { mode: "manual", manual_until: "2026-10-20T15:00:00Z" } : {}),
  }) }));
  await flush();
  assert.equal(ui.element("radioAdd").disabled, true);
  assert.equal(ui.element("radioPlay").disabled, false);
  await ui.element("radioPlay").fire("click");
  assert.match(ui.element("radioStatus").textContent, /Handvirk spilun/);
  assert.equal(ui.element("radioError").hidden, true);
});

test("a stale poll cannot overwrite a playback command and pending commands are not duplicated", async () => {
  let finishPoll, finishPlay;
  const ui = setup(async (call, number) => {
    if (number === 2) await new Promise((resolve) => { finishPoll = resolve; });
    if (call.method === "POST") await new Promise((resolve) => { finishPlay = resolve; });
    return { ok: true, json: async () => ({ ...state(), ...(call.method === "POST" ? { mode: "manual", manual_until: "2026-10-20T15:00:00Z" } : {}) }) };
  });
  await flush();
  const poll = ui.poll();
  const play = ui.element("radioPlay").fire("click");
  assert.equal(ui.element("radioPlay").disabled, true);
  assert.equal(ui.element("radioStop").disabled, true);
  await ui.element("radioPlay").fire("click");
  assert.equal(ui.calls.filter((call) => call.method === "POST").length, 1);
  finishPlay();
  await play;
  const manualStatus = ui.element("radioStatus").textContent;
  finishPoll();
  await poll;
  assert.match(manualStatus, /Handvirk spilun/);
  assert.equal(ui.element("radioStatus").textContent, manualStatus);
  assert.equal(ui.element("radioStop").disabled, false);
});

test("failed commands show the error and allow retry", async () => {
  let failed = true;
  const ui = setup(async (call) => {
    if (call.method === "POST" && failed) return { ok: false, status: 503, json: async () => ({ detail: "frame offline" }) };
    return { ok: true, json: async () => state() };
  });
  await flush();
  await ui.element("radioStop").fire("click");
  assert.match(ui.element("radioError").textContent, /frame offline/);
  assert.equal(ui.element("radioPlay").disabled, false);
  assert.equal(ui.element("radioStop").disabled, false);
  failed = false;
  await ui.element("radioStop").fire("click");
  assert.equal(ui.element("radioError").hidden, true);
});
