"use strict";

const { EventEmitter } = require("node:events");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { test } = require("node:test");
const { createProxyPrompt } = require("../runtime/window/proxy-prompt");

function harness(initial, options = {}) {
  const windows = [];
  const writes = [];
  const dialogs = [];
  let restartCount = 0;
  const store = {
    get: () => initial,
    set(key, value) { writes.push([key, value]); },
  };
  class BrowserWindow extends EventEmitter {
    constructor(config) {
      super();
      this.config = config;
      this.destroyed = false;
      this.focusCount = 0;
      this.scripts = [];
      this.webContents = new EventEmitter();
      this.webContents.getURL = () => this.url;
      this.webContents.setWindowOpenHandler = (handler) => { this.openHandler = handler; };
      this.webContents.executeJavaScript = async (script) => {
        this.scripts.push(script);
        if (this.context) vm.runInNewContext(script, this.context);
      };
      windows.push(this);
    }
    isDestroyed() { return this.destroyed; }
    setMenu() {}
    focus() { this.focusCount += 1; }
    loadURL(url) {
      this.url = url;
      this.html = decodeURIComponent(url.slice(url.indexOf(",") + 1));
      return Promise.resolve();
    }
    close() { this.destroyed = true; this.emit("closed"); }
  }
  const prompt = createProxyPrompt({
    BrowserWindow,
    nativeTheme: { shouldUseDarkColors: false },
    store,
    getParent: () => null,
    getProxyStatus: () => options.status || "Active for this launch: System proxy.",
    dialog: {
      async showMessageBox(_parent, detail) {
        dialogs.push(detail);
        return { response: options.response ?? 1 };
      },
    },
    restartApp: async () => { restartCount += 1; },
  });
  async function submit(win, fields) {
    const event = { preventDefault() {} };
    await win.listeners("page-title-updated")[0](
      event, `kirocrew-proxy:${encodeURIComponent(JSON.stringify(fields))}`,
    );
  }
  return { ...prompt, windows, writes, dialogs, submit, restarts: () => restartCount };
}

function renderScript(win, values = {}) {
  const elements = new Map();
  for (const id of ["proxy-mode", "proxy-url", "proxy-form", "cancel", "error"]) {
    elements.set(id, {
      value: values[id] || "",
      textContent: "",
      listeners: {},
      addEventListener(event, fn) { this.listeners[event] = fn; },
    });
  }
  const document = {
    title: "",
    listeners: {},
    getElementById: (id) => elements.get(id),
    addEventListener(event, fn) { this.listeners[event] = fn; },
  };
  win.context = { document, window: { close: () => win.close() } };
  const script = win.html.match(/<script>([\s\S]*?)<\/script>/)[1];
  vm.runInNewContext(script, win.context);
  return { document, elements };
}

test("proxy prompt is sandboxed, labels inputs, escapes stored values and blocks navigation", async () => {
  const built = harness({ mode: "clashx", url: '"><script>bad()</script>' }, {
    status: "<img src=network>",
  });
  const result = built.promptNetworkProxy();
  const win = built.windows[0];
  assert.deepEqual(win.config.webPreferences, {
    nodeIntegration: false, contextIsolation: true, sandbox: true,
  });
  assert.match(win.html, /for="proxy-mode"/);
  assert.match(win.html, /for="proxy-url"/);
  assert.match(win.html, /&quot;&gt;&lt;script&gt;bad\(\)&lt;\/script&gt;/);
  assert.match(win.html, /&lt;img src=network&gt;/);
  assert.match(win.html, /default-src 'none'/);
  assert.deepEqual(win.openHandler(), { action: "deny" });
  for (const event of ["will-navigate", "will-redirect"]) {
    let prevented = false;
    win.webContents.emit(event, { preventDefault() { prevented = true; } }, "https://example.test");
    assert.equal(prevented, true);
  }
  win.url = "https://example.test";
  await built.submit(win, { mode: "direct" });
  assert.equal(built.writes.length, 0, "a different document cannot submit a title");
  win.close();
  assert.equal(await result, false);
});

test("invalid submission keeps both draft fields and supports a corrected save", async () => {
  const built = harness({ mode: "clashx", url: "http://127.0.0.1:7890" });
  const result = built.promptNetworkProxy();
  const win = built.windows[0];
  const { elements } = renderScript(win, {
    "proxy-mode": "clashx", "proxy-url": "socks5://127.0.0.1:7891",
  });
  await built.submit(win, { mode: "clashx", url: elements.get("proxy-url").value });
  assert.equal(built.writes.length, 0);
  assert.equal(win.isDestroyed(), false);
  assert.equal(elements.get("proxy-mode").value, "clashx");
  assert.equal(elements.get("proxy-url").value, "socks5://127.0.0.1:7891");
  assert.match(elements.get("error").textContent, /HTTP or HTTPS/);
  await built.submit(win, { mode: "clashx", url: "http://127.0.0.1:7897/" });
  assert.deepEqual(built.writes, [["networkProxy", { mode: "clashx", url: "http://127.0.0.1:7897" }]]);
  assert.equal(await result, true);
  assert.equal(built.restarts(), 0, "Later saves for the next launch without applying now");
  assert.match(built.dialogs[0].detail, /Existing external Gateways and remote hosts/);
});

test("save and restart runs the injected lifecycle callback exactly once", async () => {
  const built = harness(undefined, { response: 0 });
  const result = built.promptNetworkProxy();
  const win = built.windows[0];
  await built.submit(win, { mode: "direct" });
  await built.submit(win, { mode: "direct" });
  assert.equal(await result, true);
  assert.equal(built.restarts(), 1);
  assert.equal(built.writes.length, 1);
});

test("startup repair requires an explicit valid mode and restarts after saving", async () => {
  const built = harness({ mode: "unknown", url: "http://127.0.0.1:7890" });
  const result = built.promptNetworkProxy({ restartRequired: true });
  const win = built.windows[0];
  assert.match(win.html, /Choose a proxy mode/);
  assert.match(win.html, /Save and Restart/);
  await built.submit(win, { mode: "unknown", url: "http://127.0.0.1:7890" });
  assert.equal(built.writes.length, 0);
  await built.submit(win, { mode: "system", url: "http://127.0.0.1:7890" });
  assert.equal(await result, true);
  assert.equal(built.restarts(), 1);
  assert.equal(built.dialogs.length, 0);
});

for (const cancel of ["button", "Escape", "window close"]) {
  test(`${cancel} discards an unsaved proxy draft`, async () => {
    const built = harness();
    const result = built.promptNetworkProxy();
    const win = built.windows[0];
    const { document, elements } = renderScript(win);
    if (cancel === "button") elements.get("cancel").listeners.click();
    else if (cancel === "Escape") document.listeners.keydown({ key: "Escape" });
    else win.close();
    assert.equal(await result, false);
    assert.equal(built.writes.length, 0);
    assert.equal(built.restarts(), 0);
  });
}

test("opening settings twice focuses the existing draft", async () => {
  const built = harness();
  const first = built.promptNetworkProxy();
  const second = built.promptNetworkProxy();
  assert.equal(first, second);
  assert.equal(built.windows.length, 1);
  assert.equal(built.windows[0].focusCount, 1);
  built.windows[0].close();
  await first;
});

test("form submits current mode and URL and each retry emits a fresh title", async () => {
  const built = harness();
  const result = built.promptNetworkProxy();
  const win = built.windows[0];
  const { document, elements } = renderScript(win, {
    "proxy-mode": "system", "proxy-url": "http://127.0.0.1:7899",
  });
  elements.get("proxy-mode").value = "clashx";
  let prevented = false;
  const event = { preventDefault() { prevented = true; } };
  elements.get("proxy-form").listeners.submit(event);
  const firstTitle = document.title;
  elements.get("proxy-form").listeners.submit(event);
  assert.equal(prevented, true);
  assert.notEqual(document.title, firstTitle);
  const payload = JSON.parse(decodeURIComponent(document.title.slice("kirocrew-proxy:".length)));
  assert.equal(payload.mode, "clashx");
  assert.equal(payload.url, "http://127.0.0.1:7899");
  win.close();
  await result;
});
