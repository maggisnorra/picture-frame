const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { randomUUID } = require("node:crypto");
const source = fs.readFileSync(path.join(__dirname, "../apps/remote/static/radio.js"), "utf8");
const flush = () => new Promise((resolve) => setImmediate(resolve));
const stationUrl = "https://example.com/live.mp3";
const entry = { id: "saved-id", start_date: "2026-10-19", weekday: 0, start_time: "07:00", duration_minutes: 30, enabled: true };
const state = () => ({ settings: { station_url: stationUrl, schedules: [{ ...entry }] }, timezone: "Atlantic/Reykjavik", active: false, playing: false });

function setup(respond) {
  const elements = new Map(), calls = [], intervals = [], html = [];
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
    document: { body: { dataset: { self: "adam" } }, getElementById: element, createElement: () => element(randomUUID()) },
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
  assert.match(ui.element("radioTimezoneHint").textContent, /Europe\/Amsterdam/);
  ui.element("radioAdd").fire("click");
  assert.equal(ui.element("radioSchedules").children.length, 2);
});
