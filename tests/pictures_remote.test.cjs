const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../apps/remote/static/remote.js"), "utf8");
const flush = () => new Promise((resolve) => setImmediate(resolve));
const pictures = [
  { picture_id: "one", filename: "Beach.jpg", uploaded_at: 1 },
  { picture_id: "two", filename: "Mountains.jpg", uploaded_at: 2 },
];

function setup(respond) {
  const elements = new Map(), requests = [], imageLoads = [], intervals = [];
  function create(tag = "div") {
    const el = {
      tag, children: [], attributes: {}, listeners: {}, value: "", files: [], textContent: "", className: "", disabled: false,
      classList: {
        add(name) { if (!el.classList.contains(name)) el.className += ` ${name}`; },
        remove(name) { el.className = el.className.split(" ").filter((item) => item !== name).join(" "); },
        toggle(name, enabled) { if (enabled) el.classList.add(name); else el.classList.remove(name); },
        contains(name) { return el.className.split(" ").includes(name); },
      },
      setAttribute(name, value) { this.attributes[name] = value; },
      getAttribute(name) { return this.attributes[name] ?? null; },
      removeAttribute(name) { delete this.attributes[name]; },
      addEventListener(name, handler) { this.listeners[name] = handler; },
      fire(name = "click") { return this.listeners[name]?.({ preventDefault() {} }); },
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      set innerHTML(value) { this.html = value; this.children = []; },
      set src(value) { this.attributes.src = value; imageLoads.push(value); },
      get src() { return this.attributes.src; },
    };
    return el;
  }
  function element(id) {
    if (!elements.has(id)) elements.set(id, create(id.includes("Img") || id === "picturePreview" ? "img" : "div"));
    return elements.get(id);
  }
  vm.runInNewContext(source, {
    document: { body: { dataset: { self: "adam", other: "steve" } }, getElementById: element, createElement: create, querySelectorAll: () => [] },
    window: { location: { search: "" }, setInterval(callback) { intervals.push(callback); } },
    URL, URLSearchParams, FormData: class { append() {} },
    fetch: async (url, init) => {
      const call = { url, ...init };
      requests.push(call);
      const result = respond ? await respond(call) : undefined;
      const body = result?.body ?? (url.endsWith("/pictures") ? { images: pictures, empty: false } : url.endsWith("/picture/meta") ? { empty: false, url: "/api/picture", updated_at: 3 } : { interval_seconds: 30 });
      return { ok: result?.ok ?? true, status: result?.status ?? 200, text: async () => JSON.stringify(body) };
    },
  });
  return {
    element, requests, imageLoads, intervals,
    async loadTitles() { element("pictureTitlesLoadBtn").fire(); await flush(); },
    rows: () => element("partnerPlaylist").children,
    image: (row) => row.children[0],
    show: (row) => row.children[1].children[1].children[0],
    remove: (row) => row.children[1].children[1].children[1],
  };
}

test("opening the remote requests no image titles, metadata, or files and starts no picture polling", async () => {
  const ui = setup();
  await flush();
  assert.deepEqual(ui.requests.map((call) => call.url), ["/api/adam/slideshow"]);
  assert.deepEqual(ui.imageLoads, []);
  assert.equal(ui.intervals.length, 0);
});

test("load titles fetches only filenames; each image needs its own explicit click", async () => {
  const ui = setup();
  await ui.loadTitles();
  assert.equal(ui.requests.at(-1).url, "/api/adam/pictures");
  assert.equal(ui.rows().length, 2);
  assert.deepEqual(ui.imageLoads, []);
  const [first, second] = ui.rows();
  assert.equal(first.children[1].children[0].textContent, "Beach.jpg");
  ui.show(first).fire();
  assert.deepEqual(ui.imageLoads, ["/api/adam/pictures/one/file?t=1"]);
  assert.equal(ui.image(second).src, undefined);
  ui.image(first).onload();
  assert.equal(ui.show(first).textContent, "Fela mynd");
  ui.show(first).fire();
  assert.equal(ui.image(first).src, undefined);
  assert.equal(ui.image(first).classList.contains("d-none"), true);
  assert.equal(ui.imageLoads.length, 1);
});

test("refreshing titles preserves existing previews and never opts in other images", async () => {
  const ui = setup();
  await ui.loadTitles();
  const [first, second] = ui.rows();
  ui.show(first).fire();
  ui.image(first).onload();
  await ui.loadTitles();
  assert.equal(ui.rows()[0], first);
  assert.equal(ui.rows()[1], second);
  assert.equal(ui.imageLoads.length, 1);
  assert.equal(ui.image(second).src, undefined);
});

test("failed title and image loads can be retried without loading unrelated pictures", async () => {
  let failed = false;
  const ui = setup(async (call) => {
    if (call.url.endsWith("/pictures") && !failed) { failed = true; return { ok: false, status: 502, body: {} }; }
  });
  await ui.loadTitles();
  assert.equal(ui.element("pictureTitlesLoadBtn").disabled, false);
  assert.deepEqual(ui.imageLoads, []);
  assert.equal(ui.requests.some((call) => call.url.endsWith("/picture/meta")), false);
  await ui.loadTitles();
  const row = ui.rows()[0];
  ui.show(row).fire();
  ui.image(row).onerror();
  assert.equal(ui.show(row).disabled, false);
  assert.equal(ui.image(row).src, undefined);
  ui.show(row).fire();
  assert.equal(ui.imageLoads.length, 2);
  assert.equal(ui.image(ui.rows()[1]).src, undefined);
});

test("current-frame preview loads only through its own button", async () => {
  const ui = setup();
  await ui.loadTitles();
  await ui.element("frameCurrentLoadBtn").fire();
  assert.equal(ui.requests.at(-1).url, "/api/adam/picture/meta");
  assert.deepEqual(ui.imageLoads, ["/api/adam/picture?t=3"]);
  ui.element("frameCurrentImg").onerror();
  assert.equal(ui.element("frameCurrentImg").src, undefined);
  await ui.element("frameCurrentLoadBtn").fire();
  assert.equal(ui.imageLoads.length, 2);
});

test("deleting a picture refreshes titles without loading previews or the current frame", async () => {
  let images = [...pictures];
  const ui = setup(async (call) => {
    if (call.method === "DELETE") { images = images.filter((image) => !call.url.endsWith(image.picture_id)); return { body: { ok: true } }; }
    if (call.url.endsWith("/pictures")) return { body: { images, empty: images.length === 0 } };
  });
  await ui.loadTitles();
  await ui.remove(ui.rows()[0]).fire();
  assert.equal(ui.rows().length, 1);
  assert.deepEqual(ui.imageLoads, []);
  assert.equal(ui.requests.some((call) => call.url.endsWith("/picture/meta")), false);
});

test("uploading before opting in to titles leaves remote images unloaded", async () => {
  const ui = setup();
  ui.element("pictureFile").files = [{ name: "new.jpg" }];
  await ui.element("pictureUploadBtn").fire();
  assert.equal(ui.requests.at(-1).method, "POST");
  assert.equal(ui.requests.some((call) => call.url.endsWith("/pictures")), false);
  assert.deepEqual(ui.imageLoads, []);
});
