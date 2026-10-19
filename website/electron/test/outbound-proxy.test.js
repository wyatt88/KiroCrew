"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  DEFAULT_CLASHX_URL,
  applyProxyEnvironment,
  normalizeProxySettings,
  parseMacSystemProxy,
  resolveProxyPolicy,
} = require("../outbound-proxy");

function scutil(body) {
  return `<dictionary> {\n${body}\n}`;
}

test("settings default to system and retain a validated URL in inactive modes", () => {
  assert.deepEqual(normalizeProxySettings(), { mode: "system", url: DEFAULT_CLASHX_URL });
  assert.deepEqual(normalizeProxySettings(null), { mode: "system", url: DEFAULT_CLASHX_URL });
  assert.deepEqual(normalizeProxySettings({ mode: "clashx", url: "" }), {
    mode: "clashx", url: DEFAULT_CLASHX_URL,
  });
  assert.deepEqual(normalizeProxySettings({ mode: "direct", url: "HTTPS://proxy.example:8443/" }), {
    mode: "direct", url: "https://proxy.example:8443",
  });
});

test("settings accept HTTP, HTTPS, IPv6 and scheme-default ports", () => {
  for (const [url, expected] of [
    ["http://127.0.0.1:7890", DEFAULT_CLASHX_URL],
    ["https://proxy.example:443", "https://proxy.example"],
    ["http://[::1]:7890/", "http://[::1]:7890"],
    ["http://localhost", "http://localhost"],
    [" http://proxy.example:80 ", "http://proxy.example"],
  ]) {
    assert.equal(normalizeProxySettings({ mode: "clashx", url }).url, expected);
  }
});

test("settings reject invalid modes and malformed objects without falling back to direct", () => {
  for (const raw of ["clashx", [], { mode: "unknown" }, { mode: null }, { url: null }]) {
    assert.throws(() => normalizeProxySettings(raw));
    assert.throws(() => resolveProxyPolicy({ settings: raw, env: {}, platform: "linux" }));
  }
});

test("settings reject credentials, normalization tricks, invalid ports and controls", () => {
  for (const url of [
    "socks5://127.0.0.1:7891",
    "127.0.0.1:7890",
    "http://user:secret@127.0.0.1:7890",
    "http://@127.0.0.1:7890",
    "http://127.0.0.1:7890/path",
    "http://127.0.0.1:7890/path/..",
    "http://127.0.0.1:7890?token=secret",
    "http://127.0.0.1:7890#fragment",
    "http://127.0.0.1:7890?",
    "http://127.0.0.1:7890#",
    "http:\\\\127.0.0.1:7890",
    "http://127.0.0.1:0",
    "http://127.0.0.1:65536",
    "http://127.0.0.1:-1",
    "http://127.0.0.1:",
    "http://",
    "http://bad host:7890",
    "http://proxy;https=other:7890",
    "http://proxy,other:7890",
    "http://proxy%3Bother:7890",
    "http://*.example:7890",
    "http://127.0.0.1:\n7890",
    "\thttp://127.0.0.1:7890",
    "http://127.0.0.1:7890\u007f",
  ]) {
    assert.throws(() => normalizeProxySettings({ mode: "clashx", url }), undefined, url);
  }
});

test("macOS static proxies use only enabled top-level fields and HTTP CONNECT for HTTPS", () => {
  const parsed = parseMacSystemProxy(scutil(`
  HTTPEnable : 1
  HTTPProxy : 127.0.0.1
  HTTPPort : 7890
  HTTPSEnable : 1
  HTTPSProxy : ::1
  HTTPSPort : 7891
  ExceptionsList : <array> {
    0 : *.example.test
    1 : 192.168.0.0/16
  }
  __SCOPED__ : <dictionary> {
    en0 : <dictionary> {
      HTTPEnable : 1
      HTTPProxy : inactive.example
      HTTPPort : 9999
    }
  }`));
  assert.equal(parsed.kind, "static");
  assert.deepEqual(parsed.environment, {
    http_proxy: DEFAULT_CLASHX_URL,
    HTTP_PROXY: DEFAULT_CLASHX_URL,
    https_proxy: "http://[::1]:7891",
    HTTPS_PROXY: "http://[::1]:7891",
    no_proxy: ".example.test,192.168.0.0/16",
    NO_PROXY: ".example.test,192.168.0.0/16",
  });
  assert.equal(parsed.warning, undefined);
});

test("macOS disabled and scoped-only proxies do not affect gateway environment", () => {
  const parsed = parseMacSystemProxy(scutil(`
  HTTPEnable : 0
  HTTPProxy : disabled.example
  HTTPPort : 9999
  __SCOPED__ : <dictionary> {
    en0 : <dictionary> {
      HTTPEnable : 1
      HTTPProxy : inactive.example
      HTTPPort : 9999
    }
  }`));
  assert.deepEqual(parsed, { kind: "none", environment: {} });
});

test("macOS PAC, WPAD and SOCKS-only settings return explicit unsupported results", () => {
  for (const body of [
    "ProxyAutoConfigEnable : 1\nProxyAutoConfigURLString : https://proxy.example/config.pac",
    "ProxyAutoDiscoveryEnable : 1",
    "SOCKSEnable : 1\nSOCKSProxy : 127.0.0.1\nSOCKSPort : 7891",
    "HTTPEnable : 1\nHTTPProxy : localhost\nHTTPPort : 7890\nProxyAutoConfigEnable : 1",
  ]) {
    const parsed = parseMacSystemProxy(scutil(body));
    assert.equal(parsed.kind, "unsupported");
    assert.deepEqual(parsed.environment, {});
    assert.ok(parsed.warning);
  }
});

test("macOS static HTTP with SOCKS warns about the unexported SOCKS setting", () => {
  const parsed = parseMacSystemProxy(scutil(
    "HTTPEnable : 1\nHTTPProxy : localhost\nHTTPPort : 7890\nSOCKSEnable : 1",
  ));
  assert.equal(parsed.kind, "static");
  assert.equal(parsed.environment.http_proxy, "http://localhost:7890");
  assert.match(parsed.warning, /SOCKS/);
});

test("macOS invalid input and enabled endpoints never yield partially populated proxy settings", () => {
  for (const text of [
    "",
    undefined,
    "<dictionary> {",
    scutil("HTTPEnable : 1\nHTTPProxy : localhost"),
    scutil("HTTPEnable : 1\nHTTPProxy : localhost\nHTTPPort : 0"),
    scutil("HTTPEnable : 1\nHTTPProxy : localhost\nHTTPPort : 65536"),
    scutil("HTTPEnable : 1\nHTTPProxy : user@localhost\nHTTPPort : 7890"),
    scutil("HTTPEnable : 1\nHTTPProxy : localhost\nHTTPPort : 7890\nHTTPSEnable : 1"),
  ]) {
    const parsed = parseMacSystemProxy(text);
    assert.equal(parsed.kind, "invalid");
    assert.deepEqual(parsed.environment, {});
    assert.ok(parsed.warning);
  }
});

test("ClashX replaces all proxy casings, merges excludes and leaves its input untouched", () => {
  const env = Object.freeze({
    PATH: "/test/bin",
    HTTP_PROXY: "http://old.example:8000",
    http_proxy: "http://other.example:8001",
    HTTPS_PROXY: "http://old.example:8000",
    ALL_PROXY: "socks5://old.example:9000",
    NO_PROXY: "example.test, localhost,EXAMPLE.test",
    no_proxy: "intranet.test,.private.test",
  });
  const settings = Object.freeze({ mode: "clashx", url: "http://127.0.0.1:7890/" });
  const result = resolveProxyPolicy({ settings, env, platform: "darwin" });
  for (const name of ["http", "https", "all"]) {
    assert.equal(result.environment[`${name}_proxy`], DEFAULT_CLASHX_URL);
    assert.equal(result.environment[`${name.toUpperCase()}_PROXY`], DEFAULT_CLASHX_URL);
  }
  assert.equal(result.environment.PATH, env.PATH);
  assert.equal(
    result.environment.no_proxy,
    "intranet.test,.private.test,example.test,localhost,127.0.0.1,::1,[::1]",
  );
  assert.equal(result.environment.NO_PROXY, result.environment.no_proxy);
  assert.equal(result.chromium.mode, "fixed_servers");
  assert.equal(result.chromium.proxyRules, DEFAULT_CLASHX_URL);
  for (const bypass of ["localhost", "127.0.0.1/8", "[::1]", "private.test", "*.private.test"]) {
    assert.ok(result.chromium.proxyBypassRules.split(";").includes(bypass), bypass);
  }
  assert.equal(env.HTTP_PROXY, "http://old.example:8000");
  assert.equal(settings.url, "http://127.0.0.1:7890/");
  assert.notEqual(result.environment, env);
});

test("empty lowercase no_proxy still preserves uppercase excludes and adds loopback", () => {
  const result = resolveProxyPolicy({
    settings: { mode: "clashx" },
    env: { no_proxy: "", NO_PROXY: "legacy.example" },
    platform: "linux",
  });
  assert.equal(result.environment.NO_PROXY, "legacy.example,localhost,127.0.0.1,::1,[::1]");
});

test("Chromium cannot inherit a bypass subtraction that disables the loopback exemption", () => {
  const result = resolveProxyPolicy({
    settings: { mode: "clashx" },
    env: { NO_PROXY: "<-loopback>,example.test" },
  });
  assert.ok(!result.chromium.proxyBypassRules.includes("<-loopback>"));
  assert.ok(result.environment.NO_PROXY.includes("<-loopback>"));
  assert.ok(result.chromium.proxyBypassRules.includes("127.0.0.1/8"));
});

test("Chromium NO_PROXY domain rules cover subdomains without widening IP, port or CIDR rules", () => {
  const result = resolveProxyPolicy({
    settings: { mode: "clashx" },
    env: { NO_PROXY: "example.test,private.test:8443,10.0.0.0/8,10.2.3.4:8443,10.2.3.4,[2001:db8::1]" },
  });
  const bypass = result.chromium.proxyBypassRules.split(";");
  for (const rule of [
    "example.test", "*.example.test", "private.test:8443", "*.private.test:8443",
    "10.0.0.0/8", "10.2.3.4:8443", "10.2.3.4", "[2001:db8::1]",
  ]) assert.ok(bypass.includes(rule), rule);
  for (const rule of ["*.10.0.0.0/8", "*.10.2.3.4:8443", "*.10.2.3.4", "*.[2001:db8::1]"]) {
    assert.ok(!bypass.includes(rule), rule);
  }
});

test("system mode honors lowercase shell proxies before macOS and normalizes both casings", () => {
  let reads = 0;
  const env = Object.freeze({
    HTTP_PROXY: "http://upper.example:8000",
    http_proxy: "http://lower.example:8001",
    HTTPS_PROXY: "https://secure.example:8443",
    ALL_PROXY: "http://fallback.example:8888",
    no_proxy: "lower.example",
    NO_PROXY: "upper.example",
  });
  const result = resolveProxyPolicy({
    env, platform: "darwin",
    readSystemProxy: () => { reads += 1; throw new Error("must not read"); },
  });
  assert.equal(reads, 0);
  assert.equal(result.environment.HTTP_PROXY, env.http_proxy);
  assert.equal(result.environment.https_proxy, env.HTTPS_PROXY);
  assert.equal(result.environment.all_proxy, env.ALL_PROXY);
  assert.equal(result.chromium.mode, "fixed_servers");
  assert.equal(
    result.chromium.proxyRules,
    "http=http://lower.example:8001;https=https://secure.example:8443;socks=http://fallback.example:8888",
  );
  assert.equal(
    result.environment.no_proxy,
    "lower.example,upper.example,localhost,127.0.0.1,::1,[::1]",
  );
});

test("an explicit empty lowercase proxy overrides uppercase without system rediscovery", () => {
  let reads = 0;
  const result = resolveProxyPolicy({
    env: { http_proxy: "", HTTP_PROXY: "http://upper.example:8000" },
    platform: "darwin",
    readSystemProxy: () => { reads += 1; return scutil(""); },
  });
  assert.equal(reads, 0);
  assert.equal(result.environment.http_proxy, "");
  assert.equal(result.environment.HTTP_PROXY, "");
  assert.equal(result.environment.no_proxy, "*");
  assert.equal(result.environment.NO_PROXY, "*");
  assert.deepEqual(result.chromium, { mode: "direct" });
});

test("unsupported shell proxies survive unchanged and secrets never appear in diagnostics", () => {
  for (const proxy of ["socks5://127.0.0.1:7891", "http://user:secret@proxy.example:7890"]) {
    const result = resolveProxyPolicy({
      env: { HTTPS_PROXY: proxy }, platform: "darwin",
      readSystemProxy: () => { throw new Error("must not read"); },
    });
    assert.equal(result.environment.HTTPS_PROXY, proxy);
    assert.equal(result.environment.https_proxy, proxy);
    assert.deepEqual(result.chromium, { mode: "system" });
    assert.ok(result.warning);
    assert.ok(!`${result.description} ${result.warning}`.includes(proxy));
    assert.ok(!`${result.description} ${result.warning}`.includes("secret"));
    assert.ok(result.environment.NO_PROXY.includes("127.0.0.1"));
  }
});

test("system mode exports macOS static proxies and merges every source of excludes", () => {
  const env = Object.freeze({ PATH: "/test/bin", NO_PROXY: "upper.test", no_proxy: "lower.test" });
  let reads = 0;
  const result = resolveProxyPolicy({
    env, platform: "darwin",
    readSystemProxy: () => {
      reads += 1;
      return scutil(`
  HTTPEnable : 1
  HTTPProxy : 127.0.0.1
  HTTPPort : 7890
  HTTPSEnable : 1
  HTTPSProxy : 127.0.0.1
  HTTPSPort : 7890
  ExceptionsList : <array> {
    0 : *.system.test
  }`);
    },
  });
  assert.equal(reads, 1);
  assert.equal(result.environment.http_proxy, DEFAULT_CLASHX_URL);
  assert.equal(result.environment.HTTPS_PROXY, DEFAULT_CLASHX_URL);
  assert.equal(
    result.environment.NO_PROXY,
    "lower.test,upper.test,.system.test,localhost,127.0.0.1,::1,[::1]",
  );
  assert.deepEqual(result.chromium, { mode: "system" });
  assert.equal(result.description, "macOS system proxy");
  assert.equal(env.NO_PROXY, "upper.test");
});

test("system discovery warnings preserve the environment and Chromium system policy", () => {
  const env = Object.freeze({ PATH: "/test/bin", NO_PROXY: "keep.test" });
  for (const readSystemProxy of [
    undefined,
    () => { throw new Error("private path must not leak"); },
    () => "malformed",
    () => scutil("ProxyAutoConfigEnable : 1"),
    () => scutil("SOCKSEnable : 1"),
  ]) {
    const result = resolveProxyPolicy({ env, platform: "darwin", readSystemProxy });
    assert.deepEqual(result.environment, env);
    assert.notEqual(result.environment, env);
    assert.deepEqual(result.chromium, { mode: "system" });
    assert.ok(result.warning);
    assert.ok(!result.warning.includes("private path"));
  }
});

test("other systems do not probe macOS or change an environment without shell proxies", () => {
  for (const platform of ["linux", "win32"]) {
    const env = Object.freeze({ PATH: "/test/bin", no_proxy: "keep.test" });
    const result = resolveProxyPolicy({
      env, platform,
      readSystemProxy: () => { throw new Error("must not read"); },
    });
    assert.deepEqual(result.environment, env);
    assert.notEqual(result.environment, env);
    assert.deepEqual(result.chromium, { mode: "system" });
    assert.equal(result.warning, undefined);
  }
});

test("direct mode clears proxy endpoints and disables macOS system fallback without mutating input", () => {
  const env = Object.freeze({
    PATH: "/test/bin", APP_SETTING: "keep",
    HTTP_PROXY: "http://upper.example:8000", http_proxy: "http://lower.example:8001",
    HTTPS_PROXY: "http://upper.example:8000", https_proxy: "http://lower.example:8001",
    ALL_PROXY: "http://upper.example:8000", all_proxy: "http://lower.example:8001",
    NO_PROXY: "upper.test", no_proxy: "lower.test",
  });
  const result = resolveProxyPolicy({ settings: { mode: "direct" }, env, platform: "darwin" });
  assert.deepEqual(result.environment, {
    PATH: "/test/bin", APP_SETTING: "keep", no_proxy: "*", NO_PROXY: "*",
  });
  assert.deepEqual(result.chromium, { mode: "direct" });
  assert.equal(env.http_proxy, "http://lower.example:8001");
});

test("applying a resolved policy preserves late gateway values and removes stale proxies", () => {
  const base = Object.freeze({
    PATH: "/new/bin", KIROCREW_PROJECT_DIR: "/project", PYTHONUTF8: "1",
    HTTP_PROXY: "http://stale.example:8000", http_proxy: "http://stale.example:8000",
    ALL_PROXY: "socks5://stale.example:9000", no_proxy: "stale.test",
  });
  const policy = Object.freeze({
    PATH: "/launch/bin", http_proxy: DEFAULT_CLASHX_URL, HTTP_PROXY: DEFAULT_CLASHX_URL,
    no_proxy: "localhost", NO_PROXY: "localhost",
  });
  const actual = applyProxyEnvironment(base, policy);
  assert.deepEqual(actual, {
    PATH: "/new/bin", KIROCREW_PROJECT_DIR: "/project", PYTHONUTF8: "1",
    http_proxy: DEFAULT_CLASHX_URL, HTTP_PROXY: DEFAULT_CLASHX_URL,
    no_proxy: "localhost", NO_PROXY: "localhost",
  });
  assert.equal(base.ALL_PROXY, "socks5://stale.example:9000");
  assert.notEqual(actual, base);
  assert.deepEqual(applyProxyEnvironment(base, {}), {
    PATH: "/new/bin", KIROCREW_PROJECT_DIR: "/project", PYTHONUTF8: "1",
  });
});
