"use strict";
/**
 * Browser CDP Worker for WebUI Control AI.
 * Explicit, user-consented CDP tab transport supporting all 5 WebUI providers:
 * ChatGPT, Claude, Gemini, GLM, DeepSeek.
 * Never reads or stores cookies, credentials, or session tokens.
 */
const fs = require("node:fs");

const ALLOWED_HOSTS = [
  "chatgpt.com",
  "claude.ai",
  "gemini.google.com",
  "chat.z.ai",
  "chatglm.cn",
  "chat.deepseek.com",
];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function fail(msg) {
  throw new Error(msg);
}

function parseArgs(argsList) {
  const parsed = {};
  for (let i = 0; i < argsList.length; i++) {
    const key = argsList[i];
    if (!key.startsWith("--")) fail("Invalid option: " + key);
    if (key === "--approve-send" || key === "--fixture") {
      parsed[key] = true;
      continue;
    }
    parsed[key] = argsList[++i];
  }
  return parsed;
}

function urlCheck(raw, fixture) {
  const u = new URL(raw);
  if (u.username || u.password || u.hash) {
    fail("No credentials or URL fragments allowed in target");
  }
  if (fixture) {
    if ((u.protocol !== "http:" && u.protocol !== "https:") || (u.hostname !== "127.0.0.1" && u.hostname !== "localhost")) {
      fail("Fixture target must be localhost or 127.0.0.1");
    }
  } else {
    if (u.protocol !== "https:") fail("Target must be HTTPS");
    const matched = ALLOWED_HOSTS.some((host) => u.hostname === host || u.hostname.endsWith("." + host));
    if (!matched) fail("Unapproved provider origin: " + u.hostname);
    if (u.pathname.length < 1) fail("Explicit conversation or provider URL required");
  }
  return u.href;
}

class CDP {
  constructor(url) {
    this.url = url;
    this.ws = null;
    this.seq = 0;
    this.pending = new Map();
  }

  async open() {
    this.ws = new WebSocket(this.url);
    const w = this.ws;
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("CDP connect timeout")), 5000);
      w.addEventListener("open", () => {
        clearTimeout(timer);
        resolve();
      }, { once: true });
      w.addEventListener("error", (e) => {
        clearTimeout(timer);
        reject(new Error("CDP connection error: " + (e.message || "failed")));
      }, { once: true });
    });

    w.addEventListener("message", (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch (err) {
        return;
      }
      if (!this.pending.has(msg.id)) return;
      const p = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      clearTimeout(p.timer);
      if (msg.error) {
        p.reject(new Error(msg.error.message || "CDP command error"));
      } else {
        p.resolve(msg.result);
      }
    });

    w.addEventListener("close", () => {
      for (const p of this.pending.values()) {
        clearTimeout(p.timer);
        p.reject(new Error("CDP connection closed"));
      }
      this.pending.clear();
    });
  }

  send(method, params = {}) {
    if (!this.ws || this.ws.readyState !== 1) fail("CDP socket not ready");
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("CDP command timeout: " + method));
      }, 8000);
      this.pending.set(id, { resolve, reject, timer });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  async eval(expression) {
    const res = await this.send("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    if (res.exceptionDetails) {
      fail("Browser evaluation exception: " + (res.exceptionDetails.text || "unknown"));
    }
    return res.result?.value;
  }

  close() {
    try {
      this.ws?.close();
    } catch (_) {}
  }
}

async function run(rawArgs) {
  const args = parseArgs(rawArgs);
  if (!args["--approve-send"]) fail("Explicit --approve-send flag required");
  const fixture = Boolean(args["--fixture"]);
  const role = args["--role"] || "B";
  const provider = (args["--provider"] || "chatgpt").toLowerCase();
  const validProviders = ["chatgpt", "claude", "gemini", "glm", "deepseek"];
  if (!validProviders.includes(provider)) {
    fail("Invalid provider: " + provider);
  }

  const manifestPath = args["--manifest"];
  if (!manifestPath || !fs.existsSync(manifestPath)) {
    fail("Manifest file does not exist: " + manifestPath);
  }
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));

  let target = manifest.targets?.[role];
  if (!target && manifest.providers?.[provider]) {
    const pcfg = manifest.providers[provider];
    target = {
      provider,
      url: pcfg.url,
      selectors: pcfg.selectors,
    };
  }
  if (!target) fail("No target configuration found for role " + role + " or provider " + provider);

  const expectedUrl = urlCheck(target.url, fixture);
  const sel = target.selectors || {};
  for (const k of ["input", "send", "assistant"]) {
    if (typeof sel[k] !== "string" || !sel[k] || sel[k].length > 200) {
      fail("Missing bounded CSS selector for '" + k + "'");
    }
  }

  const port = Number(args["--cdp-port"] || 9222);
  const deadline = Number(args["--deadline-seconds"] || 60);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) fail("Invalid CDP port: " + port);
  if (!Number.isInteger(deadline) || deadline < 5 || deadline > 300) fail("Invalid deadline seconds");

  const promptFile = args["--prompt-file"];
  if (!promptFile || !fs.existsSync(promptFile)) fail("Prompt file not found: " + promptFile);
  const prompt = fs.readFileSync(promptFile, "utf8");
  const jobId = args["--job"];
  if (!jobId || !prompt.includes(jobId)) fail("Job ID missing or not correlated with prompt text");

  // Query CDP tabs
  const ctrl = new AbortController();
  const fetchTimer = setTimeout(() => ctrl.abort(), 4000);
  let tabs;
  try {
    const resp = await fetch("http://127.0.0.1:" + port + "/json/list", { signal: ctrl.signal });
    if (!resp.ok) fail("CDP discovery HTTP " + resp.status);
    tabs = await resp.json();
  } catch (err) {
    fail("Cannot reach Chrome CDP on 127.0.0.1:" + port + " (" + err.message + ")");
  } finally {
    clearTimeout(fetchTimer);
  }

  const exactTabs = tabs.filter(
    (t) => t.type === "page" && t.webSocketDebuggerUrl && (t.url === expectedUrl || t.url.startsWith(expectedUrl.split("?")[0]))
  );
  if (exactTabs.length === 0) {
    fail("No open browser tab matching " + expectedUrl);
  }

  const cdpClient = new CDP(exactTabs[0].webSocketDebuggerUrl);
  const startTime = Date.now();

  try {
    await cdpClient.open();
    let baseline = null;

    // Wait for composer readiness
    for (let i = 0; i < 50; i++) {
      baseline = await cdpClient.eval(
        "(() => ({" +
          "url: location.href," +
          "count: document.querySelectorAll(" + JSON.stringify(sel.assistant) + ").length," +
          "ready: Boolean(document.querySelector(" + JSON.stringify(sel.input) + ") && document.querySelector(" + JSON.stringify(sel.send) + "))" +
        "}))()"
      );
      if (baseline.ready) break;
      await sleep(250);
    }

    if (!baseline || !baseline.ready) {
      fail("WebUI composer input or send button not ready within timeout");
    }

    // Focus composer
    const focused = await cdpClient.eval(
      "(() => {" +
        "const inp = document.querySelector(" + JSON.stringify(sel.input) + ");" +
        "if (!inp) return false;" +
        "inp.focus();" +
        "return document.activeElement === inp;" +
      "})()"
    );
    if (!focused) fail("Could not focus WebUI composer input");

    // Insert text
    await cdpClient.send("Input.insertText", { text: prompt });

    // Verify text insertion
    const verified = await cdpClient.eval(
      "(() => {" +
        "const inp = document.querySelector(" + JSON.stringify(sel.input) + ");" +
        "const txt = inp ? (inp.value || inp.innerText || inp.textContent || '') : '';" +
        "return txt.includes(" + JSON.stringify(jobId) + ");" +
      "})()"
    );
    if (!verified) fail("Typed prompt verification failed inside composer");

    // Click send
    const clicked = await cdpClient.eval(
      "(() => {" +
        "const btn = document.querySelector(" + JSON.stringify(sel.send) + ");" +
        "if (!btn || btn.disabled) return false;" +
        "btn.click();" +
        "return true;" +
      "})()"
    );
    if (!clicked) fail("Send button unavailable or disabled");

    // Await response
    let reply = null;
    while (Date.now() - startTime < deadline * 1000) {
      await sleep(500);
      const text = await cdpClient.eval(
        "(() => {" +
          "const msgs = Array.from(document.querySelectorAll(" + JSON.stringify(sel.assistant) + "));" +
          "if (msgs.length > " + baseline.count + ") {" +
            "return msgs[msgs.length - 1].textContent;" +
          "}" +
          "return null;" +
        "})()"
      );
      if (typeof text !== "string" || !text.trim()) continue;
      if (text.length > 65536) fail("WebUI response exceeded maximum buffer size");

      let candidate;
      try {
        const cleaned = text.trim().replace(/^```json\s*/i, "").replace(/\s*```$/, "").trim();
        candidate = JSON.parse(cleaned);
      } catch (_) {
        continue;
      }

      if (candidate.job_id !== jobId) {
        fail("Captured reply job_id mismatch");
      }
      reply = candidate;
      break;
    }

    if (!reply) fail("WebUI reply deadline exceeded");

    return {
      status: "REPLY_CAPTURED",
      job_id: jobId,
      context_id: role,
      provider,
      elapsed_ms: Date.now() - startTime,
      reply,
    };
  } finally {
    cdpClient.close();
  }
}

run(process.argv.slice(2))
  .then((output) => {
    process.stdout.write(JSON.stringify(output) + "\n");
  })
  .catch((err) => {
    process.stderr.write("BROWSER_BRIDGE_FAILED: " + (err.message || "Unknown error").slice(0, 300) + "\n");
    process.exitCode = 2;
  });
