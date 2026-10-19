"use strict";

const { isIP } = require("node:net");

const DEFAULT_CLASHX_URL = "http://127.0.0.1:7890";
const PROXY_MODES = new Set(["system", "clashx", "direct"]);
const PROXY_NAMES = ["http_proxy", "https_proxy", "all_proxy"];
const LOCAL_NO_PROXY = ["localhost", "127.0.0.1", "::1", "[::1]"];
const LOCAL_CHROMIUM_BYPASS = ["localhost", "127.0.0.1/8", "[::1]"];
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f]/;

function normalizeProxyUrl(raw) {
  if (typeof raw !== "string" || CONTROL_CHARACTERS.test(raw)) {
    throw new Error("Proxy URL must be an HTTP or HTTPS address without control characters.");
  }
  const value = raw.trim();
  // Inspect the spelling before URL normalizes dot paths or backslashes away.
  if (!/^https?:\/\/[^/?#\\\s]+\/?$/i.test(value)) {
    throw new Error("Proxy URL must use HTTP or HTTPS and must not contain a path, query or fragment.");
  }
  const authority = value.replace(/^https?:\/\//i, "").replace(/\/$/, "");
  if (authority.includes("@") || authority.endsWith(":")) {
    throw new Error("Proxy URL must not contain credentials or an empty port.");
  }
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error("Proxy URL contains an invalid host or port.");
  }
  if (!parsed.hostname || parsed.username || parsed.password || parsed.port === "0") {
    throw new Error("Proxy URL contains an invalid host, port or credentials.");
  }
  // Chromium interprets these as proxy-list syntax even when URL accepts them in a host.
  if (/[;,=*]/.test(parsed.hostname)) {
    throw new Error("Proxy URL host must not contain proxy-rule separators or wildcards.");
  }
  return parsed.origin;
}

/**
 * Validate durable settings without silently replacing an invalid choice.
 * The inactive URL is retained so changing modes does not discard its value.
 */
function normalizeProxySettings(raw) {
  if (raw != null && (typeof raw !== "object" || Array.isArray(raw))) {
    throw new Error("Proxy settings must be an object.");
  }
  const settings = raw || {};
  const mode = settings.mode === undefined ? "system" : settings.mode;
  if (!PROXY_MODES.has(mode)) throw new Error("Proxy mode must be system, clashx or direct.");
  const url = settings.url === undefined || settings.url === ""
    ? DEFAULT_CLASHX_URL
    : normalizeProxyUrl(settings.url);
  return { mode, url };
}

function mergeNoProxy(...values) {
  const seen = new Set();
  const items = [];
  for (const value of values) {
    if (typeof value !== "string") continue;
    for (const item of value.split(",").map((entry) => entry.trim()).filter(Boolean)) {
      const key = item.toLowerCase();
      if (!seen.has(key)) {
        seen.add(key);
        items.push(item);
      }
    }
  }
  return items.join(",");
}

function addLocalBypass(environment, extra = "") {
  const value = mergeNoProxy(
    environment.no_proxy, environment.NO_PROXY, extra, LOCAL_NO_PROXY.join(","),
  );
  environment.no_proxy = value;
  environment.NO_PROXY = value;
}

function chromiumBypass(environment) {
  const items = mergeNoProxy(environment.no_proxy, environment.NO_PROXY).split(",").filter(Boolean);
  const rules = [];
  for (const item of items) {
    // This Chromium-only subtraction would defeat the required loopback exemption.
    if (item === "<-loopback>") continue;
    if (item === "::1") {
      rules.push("[::1]");
    } else if (item.startsWith(".")) {
      rules.push(item.slice(1), `*${item}`);
    } else {
      rules.push(item);
      const domain = item.match(/^([a-z\d_-]+(?:\.[a-z\d_-]+)*\.?)(:\d+)?$/i);
      // NO_PROXY domain entries cover subdomains; Chromium's bare host rule does not.
      // Keep IP literals, abbreviated numeric addresses and CIDR ranges literal.
      if (domain && /[a-z_-]/i.test(domain[1]) && !isIP(domain[1])) {
        rules.push(`*.${domain[1]}${domain[2] || ""}`);
      }
    }
  }
  return [...new Set([...rules, ...LOCAL_CHROMIUM_BYPASS])].join(";");
}

function readScutilDictionary(text) {
  if (typeof text !== "string") return null;
  const fields = {};
  const exceptions = [];
  let depth = 0;
  let rootSeen = false;
  let rootClosed = false;
  let inExceptions = false;
  for (const line of text.split(/\r?\n/)) {
    const value = line.trim();
    if (!value) continue;
    if (!rootSeen) {
      if (value !== "<dictionary> {") return null;
      rootSeen = true;
      depth = 1;
      continue;
    }
    if (rootClosed) return null;
    if (value === "}") {
      if (depth === 2) inExceptions = false;
      depth -= 1;
      if (depth === 0) rootClosed = true;
      if (depth < 0) return null;
      continue;
    }
    const entry = value.match(/^([^:]+?)\s*:\s*(.*)$/);
    if (entry && depth === 1) {
      const [, key, item] = entry;
      fields[key.trim()] = item;
      inExceptions = key.trim() === "ExceptionsList" && item === "<array> {";
    } else if (entry && depth === 2 && inExceptions) {
      exceptions.push(entry[2]);
    }
    if (/<(?:dictionary|array)>\s*\{$/.test(value)) depth += 1;
  }
  return rootSeen && rootClosed ? { fields, exceptions } : null;
}

function systemProxyUrl(host, port) {
  if (!host || /[/@?#\\\s]/.test(host) || !/^\d+$/.test(port || "")) {
    throw new Error("Invalid system proxy endpoint.");
  }
  const number = Number(port);
  if (number < 1 || number > 65535) throw new Error("Invalid system proxy port.");
  const hostname = host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
  // HTTPSProxy names destination traffic; macOS still supplies an HTTP CONNECT proxy.
  return normalizeProxyUrl(`http://${hostname}:${number}`);
}

/**
 * Read only scutil's active top-level dictionary, never an inactive scoped interface.
 * PAC/WPAD and SOCKS-only settings cannot be exported as HTTP proxy environment variables.
 */
function parseMacSystemProxy(scutilText) {
  const dictionary = readScutilDictionary(scutilText);
  if (!dictionary) {
    return { kind: "invalid", environment: {}, warning: "The macOS proxy settings could not be read." };
  }
  const { fields, exceptions } = dictionary;
  if (fields.ProxyAutoConfigEnable === "1" || fields.ProxyAutoDiscoveryEnable === "1") {
    return {
      kind: "unsupported",
      environment: {},
      warning: "macOS PAC/WPAD remains available to Chromium but cannot be applied to the gateway. Select ClashX to use a fixed HTTP proxy.",
    };
  }
  const environment = {};
  try {
    for (const [prefix, name] of [["HTTP", "http_proxy"], ["HTTPS", "https_proxy"]]) {
      if (fields[`${prefix}Enable`] !== "1") continue;
      const url = systemProxyUrl(fields[`${prefix}Proxy`], fields[`${prefix}Port`]);
      environment[name] = url;
      environment[name.toUpperCase()] = url;
    }
  } catch {
    return {
      kind: "invalid",
      environment: {},
      warning: "An enabled macOS HTTP proxy has an invalid host or port. The gateway proxy was not changed.",
    };
  }
  const hasStaticProxy = Object.keys(environment).length > 0;
  if (!hasStaticProxy) {
    return fields.SOCKSEnable === "1"
      ? {
        kind: "unsupported",
        environment: {},
        warning: "macOS SOCKS-only settings cannot be applied to the gateway. Select ClashX to use its HTTP port.",
      }
      : { kind: "none", environment: {} };
  }
  if (exceptions.length) {
    environment.no_proxy = mergeNoProxy(...exceptions.map((entry) => entry.replace(/^\*\./, ".")));
    environment.NO_PROXY = environment.no_proxy;
  }
  return {
    kind: "static",
    environment,
    ...(fields.SOCKSEnable === "1" ? {
      warning: "Only macOS HTTP/HTTPS proxy settings were applied to the gateway; SOCKS settings were not exported.",
    } : {}),
  };
}

function normalizeShellEnvironment(environment) {
  let explicit = false;
  for (const name of PROXY_NAMES) {
    const upper = name.toUpperCase();
    const lowerPresent = typeof environment[name] === "string";
    const upperPresent = typeof environment[upper] === "string";
    if (!lowerPresent && !upperPresent) continue;
    explicit = true;
    const value = lowerPresent ? environment[name] : environment[upper];
    environment[name] = value;
    environment[upper] = value;
  }
  return explicit;
}

/** Apply a launch policy without replacing the caller's newly prepared gateway variables. */
function applyProxyEnvironment(baseEnv, proxyEnvironment) {
  const environment = { ...baseEnv };
  for (const name of [...PROXY_NAMES, "no_proxy"]) {
    for (const key of [name, name.toUpperCase()]) {
      delete environment[key];
      if (typeof proxyEnvironment[key] === "string") {
        environment[key] = proxyEnvironment[key];
      }
    }
  }
  return environment;
}

function chromiumFromEnvironment(environment) {
  const rules = [];
  for (const [name, protocol] of [
    ["http_proxy", "http"], ["https_proxy", "https"], ["all_proxy", "socks"],
  ]) {
    if (environment[name]) rules.push(`${protocol}=${normalizeProxyUrl(environment[name])}`);
  }
  return rules.length
    ? { mode: "fixed_servers", proxyRules: rules.join(";"), proxyBypassRules: chromiumBypass(environment) }
    : { mode: "direct" };
}

/**
 * Resolve one launch policy with no Electron import, process mutation or system writes.
 * readSystemProxy is a synchronous injected reader returning `scutil --proxy` text.
 */
function resolveProxyPolicy({
  settings,
  env = process.env,
  platform = process.platform,
  readSystemProxy,
} = {}) {
  const normalized = normalizeProxySettings(settings);
  const environment = { ...env };
  if (normalized.mode === "direct") {
    for (const name of PROXY_NAMES) {
      delete environment[name];
      delete environment[name.toUpperCase()];
    }
    // A completely empty proxy environment makes macOS urllib rediscover system proxies.
    environment.no_proxy = "*";
    environment.NO_PROXY = "*";
    return { environment, chromium: { mode: "direct" }, description: "Direct connection" };
  }
  if (normalized.mode === "clashx") {
    for (const name of PROXY_NAMES) {
      environment[name] = normalized.url;
      environment[name.toUpperCase()] = normalized.url;
    }
    addLocalBypass(environment);
    return {
      environment,
      chromium: {
        mode: "fixed_servers",
        proxyRules: normalized.url,
        proxyBypassRules: chromiumBypass(environment),
      },
      description: "ClashX proxy",
    };
  }
  if (normalizeShellEnvironment(environment)) {
    const enabled = PROXY_NAMES.some((name) => Boolean(environment[name]));
    if (enabled) {
      addLocalBypass(environment);
    } else {
      // Explicitly empty shell proxies must not be revived by macOS urllib's system fallback.
      environment.no_proxy = "*";
      environment.NO_PROXY = "*";
    }
    try {
      return {
        environment,
        chromium: chromiumFromEnvironment(environment),
        description: "Shell proxy environment",
      };
    } catch {
      return {
        environment,
        chromium: { mode: "system" },
        description: "Shell proxy environment",
        warning: "The gateway keeps the shell proxy settings, but Chromium could not use that address. SOCKS and authenticated shell proxies require separate browser configuration.",
      };
    }
  }
  const chromium = { mode: "system" };
  if (platform !== "darwin") {
    return { environment, chromium, description: "System proxy" };
  }
  let system;
  try {
    if (typeof readSystemProxy !== "function") throw new Error("Missing system proxy reader.");
    system = parseMacSystemProxy(readSystemProxy());
  } catch {
    return {
      environment,
      chromium,
      description: "System proxy",
      warning: "The macOS proxy settings could not be read. The gateway environment was not changed.",
    };
  }
  if (system.kind === "static") {
    const excludes = mergeNoProxy(system.environment.no_proxy, system.environment.NO_PROXY);
    for (const name of PROXY_NAMES) {
      if (system.environment[name]) {
        environment[name] = system.environment[name];
        environment[name.toUpperCase()] = system.environment[name];
      }
    }
    addLocalBypass(environment, excludes);
  }
  return {
    environment,
    chromium,
    description: system.kind === "static" ? "macOS system proxy" : "System proxy",
    ...(system.warning ? { warning: system.warning } : {}),
  };
}

module.exports = {
  DEFAULT_CLASHX_URL,
  applyProxyEnvironment,
  normalizeProxySettings,
  parseMacSystemProxy,
  resolveProxyPolicy,
};
