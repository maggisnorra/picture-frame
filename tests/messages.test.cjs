// Execute the real hook with controlled React state/effects and a virtual clock.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const frontend = path.join(__dirname, "../apps/kiosk/frontend/picture-frame-kiosk-frontend");
const ts = require(path.join(frontend, "node_modules/typescript"));
const source = fs.readFileSync(path.join(frontend, "src/hooks/useMessages.ts"), "utf8");

function setup() {
  const slots = [];
  const timers = new Map();
  let cursor = 0, now = 0, timerId = 0, hook, result;
  function render() { cursor = 0; result = hook(); }
  const react = {
    useState(initial) {
      const i = cursor++;
      if (!slots[i]) slots[i] = { value: initial };
      return [slots[i].value, (update) => {
        slots[i].value = typeof update === "function" ? update(slots[i].value) : update;
        render();
      }];
    },
    useRef(initial) {
      const i = cursor++;
      return slots[i] ??= { current: initial };
    },
    useCallback(callback) { cursor++; return callback; },
    useEffect(effect, dependencies) {
      const i = cursor++;
      if (!slots[i] || dependencies.some((value, index) => !Object.is(value, slots[i].dependencies[index]))) {
        slots[i]?.cleanup?.();
        slots[i] = { dependencies, cleanup: effect() };
      }
    },
  };
  const module = { exports: {} };
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText, {
    exports: module.exports, require: (name) => { assert.equal(name, "react"); return react; },
    window: {
      setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { at: now + delay, callback }); return id; },
      clearTimeout(id) { timers.delete(id); },
    },
  });
  hook = module.exports.useMessages;
  render();
  return {
    show: (text) => result.showMessage(text),
    current: () => result.currentMessage,
    advance(ms) {
      const target = now + ms;
      while (true) {
        const next = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
        if (!next || next[1].at > target) break;
        now = next[1].at;
        timers.delete(next[0]);
        next[1].callback();
      }
      now = target;
    },
  };
}

test("hello at t=0 is replaced at t=5 and whoopsie remains until t=35", () => {
  const messages = setup();
  messages.show("hello");
  messages.advance(4_999);
  assert.equal(messages.current().text, "hello");
  messages.advance(1);
  messages.show("whoopsie");
  assert.equal(messages.current().text, "whoopsie");
  messages.advance(25_000); // The original message's timeout must not hide its replacement.
  assert.equal(messages.current().text, "whoopsie");
  messages.advance(4_999);
  assert.equal(messages.current().text, "whoopsie");
  messages.advance(1);
  assert.equal(messages.current(), null);
});

test("repeated identical messages also reset the timer", () => {
  const messages = setup();
  messages.show("hello");
  const id = messages.current().id;
  messages.advance(20_000);
  messages.show("hello");
  assert.notEqual(messages.current().id, id);
  messages.advance(29_999);
  assert.equal(messages.current().text, "hello");
  messages.advance(1);
  assert.equal(messages.current(), null);
});

test("a new message after idle gets its own full lifetime", () => {
  const messages = setup();
  messages.show("❤️");
  messages.advance(40_000);
  assert.equal(messages.current(), null);
  messages.show(" Hello ");
  assert.equal(messages.current().text, "Hello");
  messages.advance(29_999);
  assert.equal(messages.current().text, "Hello");
  messages.advance(1);
  assert.equal(messages.current(), null);
});
