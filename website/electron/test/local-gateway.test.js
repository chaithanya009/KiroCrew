const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const {
  LOCAL_GATEWAY_KEY,
  isLocalGatewayEnabled,
  setLocalGatewayEnabled,
  classifyStartFailure,
} = require("../local-gateway");

/** Minimal electron-store stand-in: the two methods these helpers use. */
function fakeStore(initial = {}) {
  const data = { ...initial };
  return {
    data,
    get: (key) => data[key],
    set: (key, value) => { data[key] = value; },
  };
}

test("isLocalGatewayEnabled: a store that has never held the key reads as enabled", () => {
  assert.equal(isLocalGatewayEnabled(fakeStore()), true);
});

test("isLocalGatewayEnabled: only an explicit false disables it", () => {
  assert.equal(isLocalGatewayEnabled(fakeStore({ [LOCAL_GATEWAY_KEY]: false })), false);
  assert.equal(isLocalGatewayEnabled(fakeStore({ [LOCAL_GATEWAY_KEY]: true })), true);
});

test("isLocalGatewayEnabled: a non-boolean stored value is not a request to stop", () => {
  // A hand-edited config carrying "false" or 0 is malformed, not an opt-out —
  // reading it as one would silently stop starting the gateway.
  for (const value of ["false", 0, null, "", "no"]) {
    assert.equal(
      isLocalGatewayEnabled(fakeStore({ [LOCAL_GATEWAY_KEY]: value })),
      true,
      `stored ${JSON.stringify(value)} should leave the gateway enabled`,
    );
  }
});

test("setLocalGatewayEnabled: writes a real boolean and returns what it wrote", () => {
  const store = fakeStore();
  assert.equal(setLocalGatewayEnabled(store, false), false);
  assert.equal(store.data[LOCAL_GATEWAY_KEY], false);
  assert.equal(isLocalGatewayEnabled(store), false);

  assert.equal(setLocalGatewayEnabled(store, true), true);
  assert.equal(store.data[LOCAL_GATEWAY_KEY], true);
  assert.equal(isLocalGatewayEnabled(store), true);
});

test("setLocalGatewayEnabled: coerces a truthy non-boolean rather than storing it raw", () => {
  const store = fakeStore();
  assert.equal(setLocalGatewayEnabled(store, "yes"), true);
  assert.strictEqual(store.data[LOCAL_GATEWAY_KEY], true);
});

// ── classifyStartFailure ──

test("classifyStartFailure: a disabled record is client-only", () => {
  assert.equal(
    classifyStartFailure({ failedToStart: true, failure: { disabled: true, port: 5476 } }),
    "client-only",
  );
});

test("classifyStartFailure: client-only OUTRANKS a stale port-in-use log line", () => {
  // The launch log survives across launches, so a bound-port line from an
  // earlier run must not offer to force-stop a holder of a silent port.
  assert.equal(
    classifyStartFailure({
      failedToStart: true,
      failure: { disabled: true, port: 5476 },
      isOwnPort: true,
      portInUseInLog: true,
    }),
    "client-only",
  );
});

test("classifyStartFailure: a refused incomplete bundle is 'installing', not a crash", () => {
  assert.equal(
    classifyStartFailure({ failedToStart: true, failure: { incompleteBundle: true } }),
    "installing",
  );
});

test("classifyStartFailure: installing OUTRANKS a stale port-in-use log line", () => {
  // Nothing was spawned, so a bound-port line left by an earlier run must not
  // offer to force-stop a holder that this refusal says nothing about.
  assert.equal(
    classifyStartFailure({
      failedToStart: true,
      failure: { incompleteBundle: true },
      isOwnPort: true,
      portInUseInLog: true,
    }),
    "installing",
  );
});

test("classifyStartFailure: client-only outranks an incomplete bundle", () => {
  // Both can hold at once on a client-only install that also has a partial
  // bundle; the user turned the local gateway off, so that is the real story.
  assert.equal(
    classifyStartFailure({
      failedToStart: true,
      failure: { disabled: true, incompleteBundle: true },
    }),
    "client-only",
  );
});

test("classifyStartFailure: a real port conflict still wins when nothing is disabled", () => {
  assert.equal(
    classifyStartFailure({ failedToStart: true, isOwnPort: true, portInUseInLog: true }),
    "port-conflict",
  );
});

test("classifyStartFailure: a bound port on ANOTHER window's port is not our conflict", () => {
  assert.equal(
    classifyStartFailure({ failedToStart: true, isOwnPort: false, portInUseInLog: true }),
    "failed",
  );
});

test("classifyStartFailure: a plain spawn failure and a timeout stay distinct", () => {
  assert.equal(classifyStartFailure({ failedToStart: true }), "failed");
  assert.equal(classifyStartFailure({ failedToStart: false }), "unreachable");
  assert.equal(classifyStartFailure(), "unreachable");
});

// #6138: the client-only dialog must not dress an expected state as a crash.
test("client-only: the failure dialog derives its log pane from that one bit", () => {
  // Source-level pin. The log pane is exactly the client-only condition, so a
  // second flag for it would be a duplicate spelling that can drift.
  const source = fs.readFileSync(
    path.join(__dirname, "..", "gateway-supervisor.js"),
    "utf8",
  );
  assert.match(source, /const showLog = !localGatewayOff;/);
  assert.doesNotMatch(source, /showLog:/);
});

test("client-only: the local-start offer routes through a re-exec on a crew's port", () => {
  // The spawn binds THIS port (`"--port", String(PORT)`), so on a port that
  // names a remote crew the escape hatch cannot start a gateway in place without
  // shadowing that crew. Port selection reads the setting once per process, so
  // the offer there means "restart and choose again". Cases the gate covers:
  // gateway on -> no offer; client-only on this launch's own port -> offer, start
  // in place; client-only on a crew's port -> offer only while a re-exec is
  // possible and has not already failed; noRetry -> no offer.
  const source = fs.readFileSync(
    path.join(__dirname, "..", "gateway-supervisor.js"),
    "utf8",
  );
  assert.match(source, /const enableButton = offerLocalStart && !noRetry/);
  assert.match(source, /offerLocalStart: canOfferLocalStart\(localGatewayOff, remoteTarget\)/);
  // One named gate, so the two questions -- may we offer it, and what does it do
  // -- cannot drift into two spellings of the port condition.
  assert.match(
    source,
    /function canOfferLocalStart\([\s\S]*?return canRelaunchThisApp\(\) && !localStartRelaunchFailed;/,
  );
  // A crew's port must not reach the in-place spawn: the action returns after
  // handing off to the re-exec, and only the own-port path arms runLocalGateway.
  assert.match(
    source,
    /if \(remoteTarget\) \{[\s\S]*?relaunchViaConfirmedSuccessor\([\s\S]*?\n {12}return;\n {10}\}\n {10}runLocalGateway = true;/,
  );
  assert.match(source, /remotePort: remoteConfig\?\.remotePort \|\| ""/);
  // The successor re-runs port selection and lands on a port THIS process never
  // served, so the handshake watches the port this process chose and pinned.
  // Polling this process's own port times out against a healthy successor and
  // then kills it.
  // The options stay on ONE line: splash-close.test.js slices this function's
  // body up to the first two-space-indented `}`, so a multi-line destructure
  // would end that slice at the parameter list instead of the function.
  assert.match(source, /async function relaunchViaConfirmedSuccessor\(\n {4}onFailed,\n {4}\{ expectPort = PORT, pinPort = false, restartingStatus = RESTARTING_STATUS \} = \{\},\n {2}\) \{/);
  assert.match(source, /const readyUrl = `http:\/\/localhost:\$\{expectPort\}\$\{READY_PATH\}`;/);
  assert.match(source, /await fetchGatewayReadiness\(readyUrl\)/);
  assert.match(source, /const successorPort = predictLocalPort\(\);/);
  assert.match(source, /\}, \{ expectPort: successorPort, pinPort: true, restartingStatus: RESTARTING_FOR_LOCAL_GATEWAY_STATUS \}\);/);
  // The splash is the only surface showing status during the handoff, and this
  // caller is not updating anything, so it must not borrow the update wording.
  assert.match(source, /const RESTARTING_FOR_LOCAL_GATEWAY_STATUS = "Restarting Kiro Crew to start a local gateway/);
  assert.doesNotMatch(source, /restartingStatus = RESTARTING_FOR_LOCAL_GATEWAY_STATUS/);
  // Pinning the port is what makes the watched port and the bound port one
  // value: the successor reads KIROCREW_PORT ahead of its own selection.
  assert.match(
    source,
    /if \(pinPort\) \{\n {6}spawnOptions\.env = \{ \.\.\.processObj\.env, KIROCREW_PORT: String\(expectPort\) \};/,
  );
  // A port that already answers cannot distinguish a successor from a gateway
  // some other install or terminal started, so confirming there would exit this
  // instance on a stranger's liveness. Only "unknown" is silence: a bound legacy
  // gateway still answers, and a draining one is a gateway too.
  assert.match(source, /const occupant = await fetchGatewayReadiness\(readyUrl\);\n {4}if \(occupant !== "unknown"\) \{/);
  // The relaunch poll specifically must not be the bare call: that one reads this
  // process's BACKEND_URL, which is the abandoned crew port on the re-exec path.
  // Two unrelated call sites legitimately take no argument, so the probe reads
  // only this function's body. The positive assertion is the control: an empty or
  // mis-sliced region fails it rather than passing the absence check for free.
  const relaunchStart = source.indexOf("function relaunchViaConfirmedSuccessor(");
  const relaunchEnd = source.indexOf("function fetchHealthInfo(");
  assert.ok(relaunchStart > 0 && relaunchEnd > relaunchStart);
  const relaunchBody = source.slice(relaunchStart, relaunchEnd);
  assert.match(relaunchBody, /await fetchGatewayReadiness\(readyUrl\)/);
  assert.doesNotMatch(relaunchBody, /await fetchGatewayReadiness\(\);/);
  // Order is the whole point of the check: refusing after the spawn, the lock
  // release or the splash swap would already have torn this instance down. So
  // the occupancy read must sit ahead of the spawn and of every teardown step,
  // and the refusal must hand back to the caller instead of continuing.
  const occupantAt = relaunchBody.indexOf("const occupant = await fetchGatewayReadiness(readyUrl);");
  const refusalAt = relaunchBody.indexOf("onFailed();\n      return;");
  const spawnAt = relaunchBody.indexOf("spawn(target, args, spawnOptions)");
  const releaseAt = relaunchBody.indexOf("app.releaseSingleInstanceLock()");
  const splashAt = relaunchBody.indexOf("livenessMonitor.stop()");
  assert.ok(occupantAt > 0, "the occupancy read must be inside this function");
  assert.ok(refusalAt > occupantAt, "the refusal must follow the occupancy read");
  for (const [name, at] of [["spawn", spawnAt], ["lock release", releaseAt], ["monitor stop", splashAt]]) {
    assert.ok(at > 0, `${name} must be inside this function`);
    assert.ok(occupantAt < at, `the occupancy read must precede the ${name}`);
  }
  // A failed handoff withholds the button, so the record the reopened dialog
  // reads must not still advertise it.
  assert.match(
    source,
    /localStartRelaunchFailed = true;[\s\S]*?if \(gatewayStartFailure\) gatewayStartFailure\.canStartHere = false;/,
  );
  // One predicate answers for both the button and the sentence, so the dialog
  // cannot render a button the message says is absent.
  assert.match(source, /canStartHere: canOfferLocalStart\(true, remoteHost\)/);
  // The title must name the crew's own port, not this end of the link.
  assert.match(source, /nothing answering at \$\{remoteTarget\}:\$\{remoteTargetPort\}/);
});
