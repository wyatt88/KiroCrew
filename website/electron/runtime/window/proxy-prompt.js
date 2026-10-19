"use strict";

const { normalizeProxySettings } = require("../../outbound-proxy");

const TITLE_PREFIX = "kirocrew-proxy:";

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * A local-only form with no preload or IPC. Only the exact data document loaded
 * here may submit a title. Validation stays in the main process and errors are
 * written as text into the existing form so an invalid URL never loses a draft.
 */
function createProxyPrompt({
  BrowserWindow,
  nativeTheme,
  store,
  getParent,
  dialog,
  restartApp = async () => {},
  getProxyStatus = () => "",
}) {
  let activeWindow = null;
  let activeResult = null;

  function promptNetworkProxy({ notice = "", restartRequired = false } = {}) {
    if (activeWindow && !activeWindow.isDestroyed()) {
      activeWindow.focus();
      return activeResult;
    }

    const raw = store.get("networkProxy");
    let initial;
    let initialError = "";
    try {
      initial = normalizeProxySettings(raw);
    } catch (error) {
      initialError = error.message;
      initial = {
        mode: raw && typeof raw.mode === "string" ? raw.mode : "",
        url: raw && typeof raw.url === "string" ? raw.url : "",
      };
    }
    const dark = nativeTheme.shouldUseDarkColors;
    const parent = getParent();
    const win = new BrowserWindow({
      title: "Network Proxy",
      width: 540,
      height: 560,
      minWidth: 460,
      minHeight: 450,
      useContentSize: true,
      resizable: true,
      parent: parent && !parent.isDestroyed() ? parent : undefined,
      modal: !!(parent && !parent.isDestroyed()),
      backgroundColor: dark ? "#1e293b" : "#f8fafc",
      webPreferences: {
        nodeIntegration: false,
        contextIsolation: true,
        sandbox: true,
      },
    });
    activeWindow = win;
    win.setMenu(null);
    const selected = (mode) => initial.mode === mode ? " selected" : "";
    const knownMode = ["system", "clashx", "direct"].includes(initial.mode);
    const html = `<!DOCTYPE html><html><head><meta charset="utf-8">
      <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; form-action 'none'; base-uri 'none'">
      <title>Network Proxy</title><style>
      * { box-sizing:border-box; }
      body { margin:0; padding:24px; font:14px -apple-system,BlinkMacSystemFont,sans-serif;
        color:${dark ? "#e2e8f0" : "#1e293b"}; background:${dark ? "#1e293b" : "#f8fafc"}; }
      h1 { font-size:19px; margin:0 0 16px; }
      label { display:block; margin:14px 0 7px; font-weight:600; }
      select,input { width:100%; border:1px solid ${dark ? "#64748b" : "#94a3b8"};
        border-radius:6px; padding:10px; font:inherit;
        background:${dark ? "#0f172a" : "#ffffff"}; color:inherit; }
      :focus-visible { outline:2px solid #8e48ff; outline-offset:2px; }
      p { margin:10px 0; line-height:1.5; font-size:13px; }
      #error { color:${dark ? "#fca5a5" : "#b91c1c"}; min-height:20px; }
      .actions { display:flex; justify-content:flex-end; gap:10px; margin-top:16px; }
      button { font:inherit; padding:9px 15px; border:1px solid ${dark ? "#64748b" : "#94a3b8"};
        border-radius:6px; cursor:pointer; color:inherit; background:transparent; }
      button[type=submit] { color:white; background:#7c3aed; border-color:#7c3aed; }
      </style></head><body>
      <h1>Network Proxy</h1>
      <form id="proxy-form">
        <label for="proxy-mode">Proxy mode</label>
        <select id="proxy-mode" autofocus>
          ${knownMode ? "" : '<option value="" selected>Choose a proxy mode</option>'}
          <option value="system"${selected("system")}>Use system / shell proxy</option>
          <option value="clashx"${selected("clashx")}>ClashX / custom HTTP proxy</option>
          <option value="direct"${selected("direct")}>Direct connection</option>
        </select>
        <label for="proxy-url">HTTP proxy URL</label>
        <input id="proxy-url" type="text" value="${escapeHtml(initial.url)}"
          placeholder="http://127.0.0.1:7890" spellcheck="false" autocomplete="off"
          aria-describedby="url-hint">
        <p id="url-hint">Used in ClashX mode. Enter the HTTP or mixed port shown in ClashX. Start ClashX separately.</p>
        <p>${escapeHtml(notice || getProxyStatus())}</p>
        <p>Changes apply after restarting this app, to its browser traffic and the proxy environment inherited by newly started local Gateway and Agent processes. Clients must honor HTTP(S)_PROXY. Existing Gateways and remote hosts keep their own settings.</p>
        <p id="error" role="alert" aria-live="polite">${escapeHtml(initialError)}</p>
        <div class="actions"><button id="cancel" type="button">Cancel</button>
          <button type="submit">${restartRequired ? "Save and Restart" : "Save"}</button></div>
      </form>
      <script>
        let serial = 0;
        const mode = document.getElementById('proxy-mode');
        const url = document.getElementById('proxy-url');
        document.getElementById('proxy-form').addEventListener('submit', event => {
          event.preventDefault();
          document.title = '${TITLE_PREFIX}' + encodeURIComponent(JSON.stringify({
            mode: mode.value, url: url.value, serial: ++serial
          }));
        });
        document.getElementById('cancel').addEventListener('click', () => window.close());
        document.addEventListener('keydown', event => {
          if (event.key === 'Escape') window.close();
        });
      </script></body></html>`;
    const documentUrl = `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
    win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    // Navigation initiated by the page is never needed; loadURL below is the
    // sole document load and does not emit will-navigate.
    win.webContents.on("will-navigate", (event) => event.preventDefault());
    win.webContents.on("will-redirect", (event) => event.preventDefault());

    let saving = false;
    let saved = false;
    activeResult = new Promise((resolve) => {
      win.on("closed", () => {
        activeWindow = null;
        activeResult = null;
        resolve(saved);
      });
      win.on("page-title-updated", async (event, title) => {
        event.preventDefault();
        if (saving || win.isDestroyed()
          || win.webContents.getURL() !== documentUrl
          || !title.startsWith(TITLE_PREFIX)) return;
        let settings;
        try {
          settings = normalizeProxySettings(
            JSON.parse(decodeURIComponent(title.slice(TITLE_PREFIX.length))),
          );
          store.set("networkProxy", settings);
        } catch (error) {
          // executeJavaScript receives a JS string literal, not HTML. The error
          // never goes through innerHTML, and the form's current inputs stay put.
          await win.webContents.executeJavaScript(
            `document.getElementById('error').textContent = ${JSON.stringify(error.message)};`,
          ).catch(() => {});
          return;
        }
        saving = true;
        saved = true;
        if (restartRequired) {
          try { await restartApp(); }
          finally { if (!win.isDestroyed()) win.close(); }
          return;
        }
        try {
          const { response } = await dialog.showMessageBox(win, {
            type: "info",
            title: "Network Proxy Saved",
            message: "Restart Kiro Crew to apply the proxy settings.",
            detail: "The app will stop only the local Gateway it started. Existing external "
              + "Gateways and remote hosts keep their own proxy settings.",
            buttons: ["Restart App", "Later"],
            defaultId: 0,
            cancelId: 1,
          });
          if (response === 0) await restartApp();
        } finally {
          if (!win.isDestroyed()) win.close();
        }
      });
    });
    // A failed local document load cannot leave an invisible pending form.
    win.loadURL(documentUrl).catch(() => { if (!win.isDestroyed()) win.close(); });
    return activeResult;
  }

  return { promptNetworkProxy };
}

module.exports = { createProxyPrompt };
