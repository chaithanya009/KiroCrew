"use strict";

const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const assert = require("node:assert");

const {
  MISSING_HOST_ERROR,
  offersRemoteCrewSetup,
  parseRemoteCrewFields,
  remoteCrewDraft,
  saveRemoteCrewConfig,
} = require("../remote-crew-setup");
const { DEFAULT_REMOTE_BIN } = require("../remote-token");

const SUPERVISOR_PATH = path.join(__dirname, "..", "gateway-supervisor.js");
const { createGatewaySupervisor } = require(SUPERVISOR_PATH);
const { createWindowLifecycle } = require("../window-lifecycle");

function fakeStore(initial = {}) {
  const data = { ...initial };
  return {
    data,
    get(key, fallback) {
      return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : fallback;
    },
    set(key, value) { data[key] = value; },
  };
}

// Every state the failure dialog can be in, with the verdict each one owes.
// Only a client-only launch with no crew stored can be answered by naming one:
// the rest already have a target. Whether the dialog can retry at all is the
// dialog's own condition, asserted where it renders the action.
const OFFER_CASES = [
  {
    name: "client-only launch with nothing stored is the state that dead-ends",
    state: { localGatewayOff: true, remoteHost: "" },
    expected: true,
  },
  {
    name: "a stored crew makes it a connection failure, not a missing crew",
    state: { localGatewayOff: true, remoteHost: "myhost" },
    expected: false,
  },
  {
    name: "a whitespace host is not an address",
    state: { localGatewayOff: true, remoteHost: "   " },
    expected: true,
  },
  {
    name: "an absent host field reads the same as an empty one",
    state: { localGatewayOff: true },
    expected: true,
  },
  {
    name: "a crashed local gateway is not answered by a crew address",
    state: { localGatewayOff: false, remoteHost: "" },
    expected: false,
  },
  {
    name: "a port conflict with a stored crew is still not this offer",
    state: { localGatewayOff: false, remoteHost: "myhost" },
    expected: false,
  },
  { name: "an empty state offers nothing", state: {}, expected: false },
];

test("offersRemoteCrewSetup answers every dialog state", () => {
  for (const { name, state, expected } of OFFER_CASES) {
    assert.strictEqual(offersRemoteCrewSetup(state), expected, name);
  }
  assert.strictEqual(offersRemoteCrewSetup(), false, "no argument offers nothing");
});

test("parseRemoteCrewFields reads a save and rejects a dismissal", () => {
  assert.strictEqual(parseRemoteCrewFields(null), null);
  assert.strictEqual(parseRemoteCrewFields(""), null);
  assert.strictEqual(parseRemoteCrewFields("Remote crew for port 5476"), null);
  assert.strictEqual(parseRemoteCrewFields("{not json"), null);
  assert.strictEqual(parseRemoteCrewFields("[\"host\"]"), null);
  assert.strictEqual(parseRemoteCrewFields("{\"host\": null}").host, "");

  assert.deepStrictEqual(
    parseRemoteCrewFields(JSON.stringify({
      host: "  myhost  ",
      binPath: " ~/.local/bin/kirocrew ",
      remotePort: " 5477 ",
      remotePath: " ~/.toolbox/bin ",
    })),
    {
      host: "myhost",
      binPath: "~/.local/bin/kirocrew",
      remotePort: "5477",
      remotePath: "~/.toolbox/bin",
    },
  );
});

test("saveRemoteCrewConfig stores a validated crew under the launch port", () => {
  const store = fakeStore();
  const result = saveRemoteCrewConfig(store, 5476, {
    host: "myhost.example.com",
    binPath: "~/.local/bin/kirocrew",
    remotePort: "5477",
    remotePath: "~/.toolbox/bin:/usr/bin",
  });

  assert.deepStrictEqual(result, { saved: true, error: null });
  assert.deepStrictEqual(store.data.remoteHosts, {
    5476: {
      host: "myhost.example.com",
      binPath: "~/.local/bin/kirocrew",
      remotePort: "5477",
      remotePath: "~/.toolbox/bin:/usr/bin",
    },
  });
});

test("saveRemoteCrewConfig defaults the binary path and keeps a stored name", () => {
  const store = fakeStore({ remoteHosts: { 5476: { defaultName: "My Crew" } } });
  const result = saveRemoteCrewConfig(store, "5476", { host: "clouddesk" });

  assert.deepStrictEqual(result, { saved: true, error: null });
  assert.deepStrictEqual(store.data.remoteHosts["5476"], {
    defaultName: "My Crew",
    host: "clouddesk",
    binPath: DEFAULT_REMOTE_BIN,
    remotePort: "",
    remotePath: "",
  });
});

// A rejected form must leave the launch untouched: a half-written entry would
// make the next attempt dial a crew the user never finished naming.
const REFUSED_CASES = [
  { name: "no host", fields: {}, port: 5476, error: MISSING_HOST_ERROR },
  { name: "whitespace host", fields: { host: "  " }, port: 5476, error: MISSING_HOST_ERROR },
  { name: "invalid host", fields: { host: "my host!" }, port: 5476 },
  { name: "binary path traversal", fields: { host: "myhost", binPath: "../kirocrew" }, port: 5476 },
  { name: "non-numeric remote port", fields: { host: "myhost", remotePort: "http" }, port: 5476 },
  { name: "out-of-range remote port", fields: { host: "myhost", remotePort: "99999" }, port: 5476 },
  { name: "remote PATH traversal", fields: { host: "myhost", remotePath: "../bin" }, port: 5476 },
  // Port 80 is the one port the per-port lookup cannot key on, so a crew stored
  // there would be read as a gateway on this machine.
  { name: "unselectable port 80", fields: { host: "myhost" }, port: 80 },
  { name: "port zero", fields: { host: "myhost" }, port: 0 },
  { name: "non-numeric port", fields: { host: "myhost" }, port: "5476-old" },
];

test("saveRemoteCrewConfig writes nothing when the form is refused", () => {
  for (const { name, fields, port, error } of REFUSED_CASES) {
    const store = fakeStore();
    const result = saveRemoteCrewConfig(store, port, fields);
    assert.strictEqual(result.saved, false, name);
    assert.ok(result.error, `${name} states a reason`);
    if (error) assert.strictEqual(result.error, error, name);
    assert.strictEqual(store.data.remoteHosts, undefined, `${name} leaves the store alone`);
  }
});

test("the failure dialog offers the crew form and retries once it is saved", () => {
  const source = fs.readFileSync(SUPERVISOR_PATH, "utf8");

  assert.match(
    source,
    /act\('configure-remote'\)[^]*?Add Remote Crew/,
    "the dialog renders a button that reports the configure-remote action",
  );
  assert.match(
    source,
    /<div class="row">[^]*?\$\{remoteSetupButton\}/,
    "the dialog's action row renders the button it built",
  );
  assert.match(
    source,
    /offerRemoteSetup: offersRemoteCrewSetup\(/,
    "the call site decides the offer with the enumerated predicate",
  );
  assert.match(
    source,
    /const showRemoteSetup = offerRemoteSetup && !noRetry;/,
    "the dialog withholds the action when it cannot retry",
  );
  assert.match(
    source,
    /action === "configure-remote"[^]*?saveRemoteCrewConfig\(store, PORT, fields\)/,
    "the handler stores the collected fields for the launch port",
  );
  assert.match(
    source,
    /draft = fields;/,
    "a refused save reopens the form on what the user typed",
  );
  assert.match(
    source,
    /action === "enable-retry"\s*\|\|\s*action === "configure-remote"\s*\)/,
    "a saved crew falls into the existing retry path",
  );
});

test("remoteCrewDraft normalizes whatever the form reopens on", () => {
  assert.deepStrictEqual(
    remoteCrewDraft(),
    { host: "", binPath: "", remotePort: "", remotePath: "" },
  );
  assert.deepStrictEqual(
    remoteCrewDraft({ host: "myhost", remotePort: 5477 }),
    { host: "myhost", binPath: "", remotePort: "5477", remotePath: "" },
  );
  assert.deepStrictEqual(
    remoteCrewDraft({ defaultName: "My Crew" }),
    { host: "", binPath: "", remotePort: "", remotePath: "" },
  );
});

// The dialog's markup is the product here, so these drive the REAL dialog
// through the supervisor with a fake BrowserWindow and read what it loaded.
// A structural assertion cannot tell a button that is built from one that is
// rendered.
function captureDialogHtml(storeData, actions = ["mc-action:quit"]) {
  const loaded = [];
  const queue = [...actions];
  const messageBoxes = [];
  const BrowserWindowFake = class {
    constructor(options) {
      this.options = options;
      this.handlers = {};
    }

    setMenu() {}

    isDestroyed() { return false; }

    on(event, handler) {
      (this.handlers[event] = this.handlers[event] || []).push(handler);
      return this;
    }

    loadURL(url) {
      const prefix = "data:text/html;charset=utf-8,";
      loaded.push(decodeURIComponent(url.slice(prefix.length)));
      const title = queue.shift();
      // A queued null is a window closed without reporting anything, which is
      // how the form says it was dismissed.
      if (title !== null && title !== undefined) {
        for (const handler of this.handlers["page-title-updated"] || []) {
          handler({}, title);
        }
      }
      for (const handler of this.handlers.closed || []) handler();
    }
  };

  const hostWindow = {
    destroyed: false,
    isDestroyed() { return this.destroyed; },
    isMinimized() { return false; },
    restore() {},
    show() {},
    focus() {},
    destroy() { this.destroyed = true; },
    on() {}, once() {}, removeListener() {},
    webContents: {
      loadFile() {}, loadURL() {}, send() {},
      on() {}, once() {}, removeListener() {},
    },
  };

  const store = fakeStore(storeData);
  const supervisor = createGatewaySupervisor({
    app: { isPackaged: false, getVersion: () => "0.8.0", quit() {}, focus() {} },
    store,
    BrowserWindow: BrowserWindowFake,
    nativeTheme: { shouldUseDarkColors: false },
    dialog: {
      async showMessageBox(_parent, options) {
        messageBoxes.push(options);
        return { response: 0 };
      },
    },
    shell: { showItemInFolder() {} },
    ipcMain: { on() {}, removeListener() {} },
    port: 5476,
    home: "/virtual/kirocrew-home",
    getMainWindow: () => null,
    isQuitting: () => false,
    requestQuit() {},
    cancelPendingTrayHide() {},
    exitImmersiveModes() {},
    log() {}, warn() {}, error() {},
    logPath: () => "/virtual/logs/gateway-launch.log",
    fsMod: {
      constants: { X_OK: 1 },
      mkdirSync() {}, openSync() { return 41; }, closeSync() {},
      accessSync() { const e = new Error("not found"); e.code = "ENOENT"; throw e; },
      existsSync() { return false; },
      readFileSync() { throw new Error("no launch log"); },
    },
    osMod: { homedir: () => "/virtual/home" },
    pathMod: path.posix,
    httpMod: {
      get(_url, _options, callback) {
        const request = new EventEmitter();
        request.destroy = () => {};
        if (typeof callback === "function") { /* never answers */ }
        queueMicrotask(() => request.emit("error", new Error("connection refused")));
        return request;
      },
    },
    spawnFn() { throw new Error("spawn must not run: the local gateway is off"); },
    execFileFn() { throw new Error("execFile must not run in this harness"); },
    execFileSyncFn() { throw new Error("execFileSync must not run in this harness"); },
    processRef: {
      platform: "test",
      arch: "x64",
      env: { KIROCREW_HOME: "/virtual/kirocrew-home" },
      resourcesPath: "/virtual/resources",
      kill() { throw new Error("process kill must not run in this harness"); },
    },
    dirname: "/virtual/electron",
  });

  return { supervisor, hostWindow, loaded, messageBoxes, store };
}

async function dialogHtmlFor(storeData) {
  const { supervisor, hostWindow, loaded } = captureDialogHtml(storeData);
  await supervisor.start();
  await supervisor.connect(hostWindow);
  assert.strictEqual(loaded.length, 1, "exactly one dialog was rendered");
  return loaded[0];
}

test("the client-only dialog renders the crew action only when nothing is stored", async () => {
  const empty = await dialogHtmlFor({ runLocalGateway: false });
  assert.match(empty, /act\('configure-remote'\)/, "the empty state offers the crew form");
  assert.match(empty, /Add Remote Crew/, "the button carries its label");
  assert.match(empty, /act\('enable-retry'\)/, "the local-gateway escape hatch is still there");
  assert.match(empty, /no gateway on port 5476/, "the title names the port");

  const configured = await dialogHtmlFor({
    runLocalGateway: false,
    remoteHosts: { 5476: { host: "myhost", binPath: "~/.local/bin/kirocrew" } },
  });
  assert.doesNotMatch(
    configured,
    /act\('configure-remote'\)/,
    "a stored crew is a connection failure, so the form is withheld",
  );
  assert.doesNotMatch(
    configured,
    /act\('enable-retry'\)/,
    "starting a local gateway on a crew's port stays withheld",
  );
  assert.match(configured, /nothing answering at myhost/, "the title names the crew");
});

test("a refused save reopens the form on the typed values, not on the empty store", async () => {
  const typed = {
    host: "my host",
    binPath: "~/.local/bin/kirocrew",
    remotePort: "5477",
    remotePath: "~/.toolbox/bin",
  };
  const { supervisor, hostWindow, loaded, messageBoxes, store } = captureDialogHtml(
    { runLocalGateway: false },
    // dialog -> open the form -> save an invalid host -> dismiss -> quit
    ["mc-action:configure-remote", JSON.stringify(typed), null, "mc-action:quit"],
  );
  await supervisor.start();
  await supervisor.connect(hostWindow);

  assert.strictEqual(loaded.length, 4, "dialog, form, reopened form, dialog again");
  const reopened = loaded[2];
  assert.match(reopened, /value="my host"/, "the rejected host comes back");
  assert.match(reopened, /value="~\/\.local\/bin\/kirocrew"/, "so does the binary path");
  assert.match(reopened, /value="5477"/, "so does the remote port");
  assert.match(reopened, /value="~\/\.toolbox\/bin"/, "so does the remote PATH");

  assert.strictEqual(messageBoxes.length, 1, "the refusal is reported once");
  assert.strictEqual(messageBoxes[0].title, "Invalid Input");
  assert.strictEqual(
    store.data.remoteHosts,
    undefined,
    "a refused save writes nothing to the store",
  );
});

// The dashboard's own "Set Remote Host" prompt writes the same store, so it
// takes the same validated path. Driving it here is what proves the two
// surfaces cannot drift apart again.
function lifecycleHarness(tabPort, savedFields) {
  const store = fakeStore({});
  const messageBoxes = [];
  const tab = {
    _mcBackendUrl: `http://localhost:${tabPort}`,
    isDestroyed: () => false,
  };
  const PromptWindowFake = class {
    constructor() { this.handlers = {}; }

    setMenu() {}

    on(event, handler) {
      (this.handlers[event] = this.handlers[event] || []).push(handler);
      return this;
    }

    // Production attaches its listeners AFTER calling loadURL, because a real
    // load is asynchronous. Firing inline would reach no listener at all.
    loadURL() {
      setImmediate(() => {
        for (const handler of this.handlers["page-title-updated"] || []) {
          handler({}, JSON.stringify(savedFields));
        }
        for (const handler of this.handlers.closed || []) handler();
      });
    }
  };

  const lifecycle = createWindowLifecycle({
    electron: {
      BaseWindow: { getFocusedWindow: () => tab },
      BrowserWindow: PromptWindowFake,
      nativeTheme: { shouldUseDarkColors: false },
      dialog: {
        showMessageBox(_parent, options) {
          messageBoxes.push(options);
          return Promise.resolve({ response: 0 });
        },
      },
    },
    store,
    backendUrl: "http://localhost:5476",
    port: 5476,
    fetchLocalToken: async () => "",
    fetchRemoteToken: async () => ({ token: "" }),
    requestQuit() {},
    connectWindow: async () => {},
    platform: "test",
  });

  return { lifecycle, store, messageBoxes };
}

// The close handler runs on the tick after loadURL, so the assertions wait for
// it rather than for promptRemoteHost, which returns before the save lands.
const settle = () => new Promise((resolve) => { setImmediate(resolve); });

test("the dashboard prompt stores a crew through the shared validated writer", async () => {
  const { lifecycle, store, messageBoxes } = lifecycleHarness("5478", {
    host: "clouddesk",
    bin: "",
    remotePort: "",
    remotePath: "",
  });
  await lifecycle.promptRemoteHost();
  await settle();

  assert.deepStrictEqual(store.data.remoteHosts["5478"], {
    host: "clouddesk",
    binPath: DEFAULT_REMOTE_BIN,
    remotePort: "",
    remotePath: "",
  });
  assert.strictEqual(messageBoxes.at(-1).type, "info");
});

test("the dashboard prompt refuses a crew on a port the lookup cannot key", async () => {
  // A tab on port 80 reports an empty URL port, which is exactly the key the
  // per-port lookup cannot find again.
  const { lifecycle, store, messageBoxes } = lifecycleHarness("", {
    host: "clouddesk",
    bin: "",
    remotePort: "",
    remotePath: "",
  });
  await lifecycle.promptRemoteHost();
  await settle();

  assert.strictEqual(store.data.remoteHosts, undefined, "nothing is stored");
  assert.strictEqual(messageBoxes.at(-1).title, "Invalid Input");
});
