"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const {
  initNativeLogging,
  nativeLogPath,
  previousNativeLogPath,
  nativeLoggingSwitches,
  rotateNativeLog,
  NATIVE_LOG_BASENAME,
  NATIVE_LOG_PREVIOUS_BASENAME,
} = require("../native-logging");

const LIVE = path.join("/logs", NATIVE_LOG_BASENAME);
const PREV = path.join("/logs", NATIVE_LOG_PREVIOUS_BASENAME);

/**
 * fs double over an in-memory file set, recording renames.
 * `present` lists paths that exist; `throwOn` makes renameSync fail.
 */
function fakeFs({ present = [], throwOn = null } = {}) {
  const files = new Set(present);
  const renames = [];
  return {
    files,
    renames,
    existsSync: (p) => files.has(p),
    renameSync(from, to) {
      if (throwOn) throw new Error(throwOn);
      renames.push({ from, to });
      files.delete(from);
      files.add(to);
    },
  };
}

describe("nativeLogPath / previousNativeLogPath", () => {
  it("sits next to the other launch logs in the logs directory", () => {
    assert.equal(nativeLogPath("/logs/Kiro Crew"), path.join("/logs/Kiro Crew", NATIVE_LOG_BASENAME));
  });

  it("keeps the previous generation beside the live file", () => {
    assert.equal(previousNativeLogPath(LIVE), PREV);
  });

  it("does not throw on a missing directory", () => {
    assert.equal(nativeLogPath(undefined), NATIVE_LOG_BASENAME);
  });
});

describe("nativeLoggingSwitches", () => {
  // These are Chromium's spellings, and an unknown switch is IGNORED rather
  // than rejected — so a typo turns logging silently off and this assertion is
  // the only thing standing between that and a shipped no-op.
  it("uses the exact Chromium switch names", () => {
    assert.deepEqual(nativeLoggingSwitches("/tmp/c.log"), [
      ["enable-logging", "file"],
      ["log-file", "/tmp/c.log"],
    ]);
  });

  // `--enable-logging` without `=file` leaves output on stderr, which the GUI
  // launch this module exists to compensate for discards again.
  it("routes to the file sink, not stderr", () => {
    const [[, value]] = nativeLoggingSwitches("/tmp/c.log");
    assert.equal(value, "file");
  });
});

describe("rotateNativeLog", () => {
  // THE point of the whole rotation step: the run under investigation is not
  // the run doing the investigating. A boot that destroyed the prior session
  // would delete the evidence at the moment someone relaunched to read it.
  it("preserves the previous session instead of discarding it", () => {
    const fs = fakeFs({ present: [LIVE] });
    const out = rotateNativeLog(LIVE, { fs });
    assert.deepEqual(out, { rotated: true, previousPath: PREV });
    assert.deepEqual(fs.renames, [{ from: LIVE, to: PREV }]);
    assert.equal(fs.files.has(PREV), true);
    // Left absent so Chromium starts clean whether it appends or truncates.
    assert.equal(fs.files.has(LIVE), false);
  });

  // Two files, never N: the bound is one generation, so an older previous is
  // replaced rather than accumulated.
  it("overwrites an older generation instead of accumulating", () => {
    const fs = fakeFs({ present: [LIVE, PREV] });
    assert.equal(rotateNativeLog(LIVE, { fs }).rotated, true);
    assert.deepEqual([...fs.files], [PREV]);
  });

  it("is a no-op on the first launch, when there is nothing to preserve", () => {
    const fs = fakeFs({ present: [] });
    assert.deepEqual(rotateNativeLog(LIVE, { fs }), { rotated: false, previousPath: null });
    assert.deepEqual(fs.renames, []);
  });

  it("reports and degrades when the rename fails, never throwing", () => {
    const fs = fakeFs({ present: [LIVE], throwOn: "EROFS" });
    const lines = [];
    const out = rotateNativeLog(LIVE, { fs, log: (m) => lines.push(m) });
    assert.deepEqual(out, { rotated: false, previousPath: null });
    assert.equal(lines.length, 1);
    assert.match(lines[0], /EROFS/);
  });
});

describe("initNativeLogging", () => {
  function harness(over = {}) {
    const applied = [];
    const started = [];
    const lines = [];
    const fs = over.fs === undefined ? fakeFs({ present: [LIVE] }) : over.fs;
    const result = initNativeLogging({
      logsDir: "/logs",
      appendSwitch: (n, v) => applied.push([n, v]),
      startCrashReporter: (o) => started.push(o),
      log: (m) => lines.push(m),
      ...over,
      fs,
    });
    return { applied, started, lines, result, fs };
  }

  it("applies both switches and starts the crash reporter", () => {
    const { applied, started, result } = harness();
    assert.deepEqual(applied, [
      ["enable-logging", "file"],
      ["log-file", LIVE],
    ]);
    assert.equal(started.length, 1);
    assert.equal(result.crashReporter, true);
    assert.equal(result.rotated, true);
    assert.equal(result.previousPath, PREV);
    assert.deepEqual(result.switches, ["enable-logging", "log-file"]);
  });

  // The one non-negotiable option: this app does not phone home, so a minidump
  // that left the machine would be a new egress path rather than a diagnostic.
  it("never uploads crash dumps off the machine", () => {
    const { started } = harness();
    assert.equal(started[0].uploadToServer, false);
  });

  // Ordering is load-bearing: Chromium opens the log path during init, so a
  // rotation that ran afterwards would preserve nothing.
  it("rotates before arming the switches", () => {
    const { fs } = harness();
    assert.deepEqual(fs.renames, [{ from: LIVE, to: PREV }]);
    assert.equal(fs.files.has(LIVE), false);
  });

  // A boot-path helper must never be the reason the app fails to start.
  it("survives an appendSwitch that throws, keeping the other switch", () => {
    const { result, lines } = harness({
      appendSwitch: (n) => {
        if (n === "enable-logging") throw new Error("refused");
      },
    });
    assert.deepEqual(result.switches, ["log-file"]);
    assert.ok(lines.some((l) => /refused/.test(l)));
  });

  it("survives a crashReporter that throws", () => {
    const { result, lines } = harness({
      startCrashReporter: () => {
        throw new Error("no dump dir");
      },
    });
    assert.equal(result.crashReporter, false);
    assert.deepEqual(result.switches, ["enable-logging", "log-file"]);
    assert.ok(lines.some((l) => /no dump dir/.test(l)));
  });

  it("still arms logging when no crash reporter is supplied", () => {
    const { result, started } = harness({ startCrashReporter: undefined });
    assert.equal(result.crashReporter, false);
    assert.equal(started.length, 0);
    assert.deepEqual(result.switches, ["enable-logging", "log-file"]);
  });

  it("skips rotation when no fs is supplied", () => {
    const { result } = harness({ fs: null });
    assert.equal(result.rotated, false);
    assert.equal(result.previousPath, null);
    assert.deepEqual(result.switches, ["enable-logging", "log-file"]);
  });

  it("logs a one-line verdict naming both generations", () => {
    const { lines } = harness();
    const verdict = lines.find((l) => /native logging armed/.test(l));
    assert.ok(verdict, "expected an armed verdict line");
    assert.match(verdict, /chromium\.log/);
    assert.match(verdict, /chromium\.previous\.log/);
    assert.match(verdict, /minidumps=true/);
  });

  it("names no previous generation on a first launch", () => {
    const { lines, result } = harness({ fs: fakeFs({ present: [] }) });
    assert.equal(result.previousPath, null);
    assert.match(
      lines.find((l) => /native logging armed/.test(l)),
      /previous=none/
    );
  });
});
