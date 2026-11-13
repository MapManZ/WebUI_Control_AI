"use strict";

let currentJob = null;

const MODEL_META = {
  "gemini-3.8-flash": { label: "Default Worker", cls: "flash" },
  "gemini-4-hydrogen": { label: "Worker", cls: "hydrogen" },
  "claude-sonnet-5.5": { label: "Hard Tasks Only", cls: "sonnet" },
  "gemini-4-argon": { label: "Review & Merge Only", cls: "argon" },
};

const TAB_TITLES = {
  relay: "Relay & Verification Board",
  mcp: "MCP Integrations (Lnwjud & External)",
  settings: "Settings & Workspace Policy",
};

function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function updateModelBadge(modelId) {
  const badge = document.getElementById("model-tier-badge");
  if (!badge) return;
  const meta = MODEL_META[modelId] || MODEL_META["gemini-3.8-flash"];
  badge.textContent = meta.label;
  badge.className = `badge model-badge ${meta.cls}`;
}

function showToast(message) {
  const toast = document.getElementById("toast");
  if (!toast) return;
  toast.textContent = message;
  toast.classList.remove("hidden");
  setTimeout(() => {
    toast.classList.add("hidden");
  }, 3200);
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const res = await fetch(path, { ...options, headers });
  if (!res.ok) {
    let errText;
    try {
      const errJson = await res.json();
      errText = errJson.error || JSON.stringify(errJson);
    } catch (_) {
      errText = await res.text();
    }
    throw new Error(errText || `HTTP ${res.status}`);
  }
  return res.json();
}

function switchTab(tabName) {
  document.querySelectorAll(".nav-pill[data-tab]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.tab === tabName);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("hidden", panel.id !== `tab-${tabName}`);
  });
  const crumb = document.getElementById("active-tab-breadcrumb");
  if (crumb) {
    crumb.textContent = TAB_TITLES[tabName] || "Relay & Verification Board";
  }
  if (tabName === "mcp") {
    loadExternalMcps();
  } else if (tabName === "settings") {
    loadSettings();
  }
}

function renderWorkingDirectory(info) {
  if (!info || !info.working_directory) return;
  const fullPath = info.working_directory;
  const relPath = info.relative_path || ".";
  const sidebarEl = document.getElementById("sidebar-workdir-path");
  const topbarEl = document.getElementById("topbar-workdir-path");
  const settingsInput = document.getElementById("settings-workdir-input");

  if (sidebarEl) sidebarEl.textContent = fullPath;
  if (topbarEl) topbarEl.textContent = fullPath;
  if (settingsInput && document.activeElement !== settingsInput) {
    settingsInput.value = fullPath;
  }
  document.title = `WebUI Control AI · Orchestra [${relPath === "." ? fullPath : relPath}]`;
}

async function loadWorkingDirectory() {
  try {
    const info = await api("/api/workdir");
    renderWorkingDirectory(info);
  } catch (err) {
    const sidebarEl = document.getElementById("sidebar-workdir-path");
    if (sidebarEl) sidebarEl.textContent = `Error: ${err.message}`;
  }
}

async function loadRecentJobs() {
  const container = document.getElementById("recent-jobs");
  try {
    const data = await api("/api/jobs?limit=20");
    const jobs = data.jobs || [];
    if (jobs.length === 0) {
      container.innerHTML = '<span class="empty-hint">No jobs created yet</span>';
      return;
    }
    container.innerHTML = "";
    jobs.forEach((j) => {
      const item = document.createElement("div");
      item.className = "recent-item";
      item.innerHTML = `
        <div class="recent-item-title">${escapeHtml(j.case_id)}</div>
        <div class="recent-item-meta">
          <span>${escapeHtml((j.job_id || "").slice(0, 8))}...</span>
          <span>${escapeHtml(j.status)}</span>
        </div>
      `;
      item.addEventListener("click", () => {
        switchTab("relay");
        loadJob(j.job_id);
      });
      container.appendChild(item);
    });
  } catch (err) {
    container.innerHTML = `<span class="empty-hint">Failed to load jobs: ${escapeHtml(err.message)}</span>`;
  }
}

async function loadJob(jobId) {
  try {
    const job = await api(`/api/jobs/${jobId}`);
    currentJob = job;
    renderJob(job);
  } catch (err) {
    showToast(`Error loading job: ${err.message}`);
  }
}

function renderJob(job) {
  const panel = document.getElementById("active-job-panel");
  panel.classList.remove("hidden");

  document.getElementById("active-job-title").textContent = `Case: ${job.case_id}`;
  document.getElementById("active-job-id").textContent = job.job_id;
  const activeModelEl = document.getElementById("active-job-model");
  if (activeModelEl) {
    const modelId = job.model || "gemini-3.8-flash";
    const meta = MODEL_META[modelId] || MODEL_META["gemini-3.8-flash"];
    activeModelEl.textContent = `${modelId} [${meta.label}]`;
    activeModelEl.className = `badge model-badge ${meta.cls}`;
  }
  const statusEl = document.getElementById("active-job-status");
  statusEl.textContent = job.status;
  statusEl.className = "status-tag " + (job.status.toLowerCase().includes("ready") ? "completed" : "pending");

  const slots = job.slots || {};
  const packets = job.packets || {};

  ["B", "C", "D"].forEach((role) => {
    const slot = slots[role] || {};
    const pkt = packets[role] || {};

    const roleTitle = document.getElementById(`role-title-${role}`);
    const provEl = document.getElementById(`prov-${role}`);
    const slotStatus = document.getElementById(`slot-status-${role}`);
    const noteEl = document.getElementById(`reply-note-${role}`);

    if (roleTitle) roleTitle.textContent = slot.role || `Context ${role}`;
    if (provEl) provEl.textContent = `Provider: ${(slot.provider || pkt.provider || "WebUI").toUpperCase()}`;

    if (slotStatus) {
      const st = slot.status || "pending";
      slotStatus.textContent = st;
      slotStatus.className = `status-tag ${st.toLowerCase()}`;
    }

    if (noteEl) {
      if (slot.result) {
        noteEl.innerHTML = `<span style="color:#3fb950">✓ Response verified (${escapeHtml(slot.result.finding?.slice(0, 60))}...)</span>`;
      } else if (slot.error) {
        noteEl.innerHTML = `<span style="color:#f85149">✗ ${escapeHtml(slot.error)}</span>`;
      } else {
        noteEl.textContent = "";
      }
    }
  });

  const copyBundleBtn = document.getElementById("copy-bundle-btn");
  const downloadBundleBtn = document.getElementById("download-bundle-btn");
  const bundlePreview = document.getElementById("bundle-preview");

  const allCompleted = ["B", "C", "D"].every((r) => slots[r]?.status === "completed");

  if (allCompleted) {
    const bundleData = {
      case_id: job.case_id,
      job_id: job.job_id,
      created_utc: job.created_utc,
      status: "READY_FOR_A_REVIEW",
      verified_results: {
        B: slots.B?.result,
        C: slots.C?.result,
        D: slots.D?.result,
      },
    };
    bundlePreview.textContent = JSON.stringify(bundleData, null, 2);
    copyBundleBtn.disabled = false;
    downloadBundleBtn.disabled = false;
  } else {
    bundlePreview.textContent = `Missing replies: ${(job.missing || []).join(", ") || "None"}. Status: ${job.status}`;
    copyBundleBtn.disabled = true;
    downloadBundleBtn.disabled = true;
  }
}

async function handleCreateJob(ev) {
  ev.preventDefault();
  const caseId = document.getElementById("case-id").value.trim();
  const ttl = Number(document.getElementById("ttl").value);
  const scenario = document.getElementById("scenario").value.trim();
  const modelSelect = document.getElementById("model-tier");
  const model = modelSelect ? modelSelect.value : "gemini-3.8-flash";

  const providerMap = {
    B: document.getElementById("provider-b").value,
    C: document.getElementById("provider-c").value,
    D: document.getElementById("provider-d").value,
  };

  try {
    const newJob = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify({
        case_id: caseId,
        scenario,
        ttl,
        model,
        provider_map: providerMap,
      }),
    });
    showToast(`Job ${newJob.job_id.slice(0, 8)} created!`);
    currentJob = newJob;
    renderJob(newJob);
    loadRecentJobs();
  } catch (err) {
    showToast(`Failed to create job: ${err.message}`);
  }
}

async function handleCopyPrompt(role) {
  if (!currentJob || !currentJob.prompts || !currentJob.prompts[role]) {
    showToast("No prompt available to copy");
    return;
  }
  const promptText = currentJob.prompts[role];
  try {
    await navigator.clipboard.writeText(promptText);
    showToast(`Copied Context ${role} prompt to clipboard!`);
  } catch (_) {
    showToast("Clipboard write permission blocked");
  }
}

async function handleSubmitReply(role) {
  if (!currentJob) {
    showToast("No active job");
    return;
  }
  const txtArea = document.getElementById(`reply-text-${role}`);
  const rawText = txtArea.value.trim();
  if (!rawText) {
    showToast(`Please paste JSON response for ${role}`);
    return;
  }

  let payload;
  try {
    const cleaned = rawText.replace(/^```json\s*/i, "").replace(/\s*```$/, "").trim();
    payload = JSON.parse(cleaned);
  } catch (err) {
    showToast(`Malformed JSON: ${err.message}`);
    return;
  }

  try {
    await api(`/api/jobs/${currentJob.job_id}/submit`, {
      method: "POST",
      body: JSON.stringify({ response: payload }),
    });
    showToast(`Context ${role} response verified and recorded!`);
    txtArea.value = "";
    loadJob(currentJob.job_id);
    loadRecentJobs();
  } catch (err) {
    showToast(`Validation rejected: ${err.message}`);
  }
}

async function loadRegistry() {
  const container = document.getElementById("registry-items");
  try {
    const data = await api("/api/registry");
    const items = data.items || [];
    if (items.length === 0) {
      container.innerHTML = '<span class="empty-hint">No registry references saved yet</span>';
      return;
    }
    container.innerHTML = "";
    items.forEach((item) => {
      const card = document.createElement("div");
      card.className = "registry-card";
      card.innerHTML = `
        <div class="reg-card-top">
          <span class="reg-title">[${escapeHtml(item.kind.toUpperCase())}] ${escapeHtml(item.title)} (${escapeHtml(item.id)})</span>
          <span class="reg-hash">sha256:${escapeHtml((item.sha256 || "").slice(0, 12))}...</span>
        </div>
        <div class="reg-content">${escapeHtml(item.content)}</div>
      `;
      container.appendChild(card);
    });
  } catch (err) {
    container.innerHTML = `<span class="empty-hint">Failed to load registry: ${escapeHtml(err.message)}</span>`;
  }
}

async function handleAddAsset(ev) {
  ev.preventDefault();
  const kind = document.getElementById("asset-kind").value;
  const id = document.getElementById("asset-id").value.trim();
  const title = document.getElementById("asset-title").value.trim();
  const project = document.getElementById("asset-scope").value.trim();
  const content = document.getElementById("asset-content").value.trim();

  try {
    await api("/api/registry", {
      method: "POST",
      body: JSON.stringify({
        kind,
        id,
        title,
        project,
        content,
        roles: ["B", "C", "D"],
        version: "1.0",
      }),
    });
    showToast(`Asset '${id}' saved to registry!`);
    document.getElementById("add-asset-form").reset();
    loadRegistry();
  } catch (err) {
    showToast(`Failed to save asset: ${err.message}`);
  }
}

// --- MCP Integrations Tab ---
function parseArgsString(raw) {
  const trimmed = (raw || "").trim();
  if (!trimmed) return [];
  if (trimmed.startsWith("[")) {
    const arr = JSON.parse(trimmed);
    if (Array.isArray(arr)) return arr.map(String);
  }
  return trimmed.split(/\s+/).filter(Boolean);
}

async function loadExternalMcps() {
  const container = document.getElementById("external-mcp-list");
  if (!container) return;
  try {
    const data = await api("/api/mcp/external");
    const servers = data.servers || [];
    if (servers.length === 0) {
      container.innerHTML = '<span class="empty-hint">No external MCP servers registered yet. Click "Load Lnwjud Preset" above to add Lnwjud.</span>';
      return;
    }
    container.innerHTML = "";
    servers.forEach((srv) => {
      const card = document.createElement("div");
      card.className = "registry-card";
      const cmdLine = [srv.command, ...(srv.args || [])].join(" ");
      card.innerHTML = `
        <div class="reg-card-top">
          <div>
            <span class="reg-title">${escapeHtml(srv.name)}</span>
            <span class="status-tag ${srv.enabled ? "completed" : "expired"}">${srv.enabled ? "ENABLED" : "DISABLED"}</span>
          </div>
          <div class="mcp-card-actions">
            <button type="button" class="btn btn-ghost btn-sm probe-mcp-btn" data-name="${escapeHtml(srv.name)}">🔍 Probe</button>
            <button type="button" class="btn btn-sm btn-danger-ghost remove-mcp-btn" data-name="${escapeHtml(srv.name)}">✕ Remove</button>
          </div>
        </div>
        <div class="reg-content">${escapeHtml(cmdLine)}${srv.description ? "\n" + escapeHtml(srv.description) : ""}</div>
      `;
      container.appendChild(card);
    });

    container.querySelectorAll(".probe-mcp-btn").forEach((btn) => {
      btn.addEventListener("click", () => probeExternalMcpByName(btn.dataset.name));
    });
    container.querySelectorAll(".remove-mcp-btn").forEach((btn) => {
      btn.addEventListener("click", () => removeExternalMcp(btn.dataset.name));
    });
  } catch (err) {
    container.innerHTML = `<span class="empty-hint">Failed to load external MCPs: ${escapeHtml(err.message)}</span>`;
  }
}

async function handleRegisterMcp(ev) {
  ev.preventDefault();
  const name = document.getElementById("mcp-server-name").value.trim();
  const command = document.getElementById("mcp-server-command").value.trim();
  const argsRaw = document.getElementById("mcp-server-args").value;
  const description = document.getElementById("mcp-server-desc").value.trim();
  const enabled = document.getElementById("mcp-server-enabled").value === "true";

  let args;
  try {
    args = parseArgsString(argsRaw);
  } catch (err) {
    showToast(`Invalid args format: ${err.message}`);
    return;
  }

  try {
    await api("/api/mcp/external", {
      method: "POST",
      body: JSON.stringify({ name, command, args, description, enabled }),
    });
    showToast(`External MCP '${name}' saved to external.json!`);
    loadExternalMcps();
  } catch (err) {
    showToast(`Failed to register MCP: ${err.message}`);
  }
}

async function probeExternalMcpByName(name) {
  const out = document.getElementById("mcp-probe-output");
  if (out) out.textContent = `Probing '${name}'...`;
  try {
    const res = await api("/api/mcp/external/probe", {
      method: "POST",
      body: JSON.stringify({ name, live_handshake: false }),
    });
    if (out) out.textContent = JSON.stringify(res.probe, null, 2);
    showToast(`Probe complete for '${name}' (available=${res.probe?.available})`);
  } catch (err) {
    if (out) out.textContent = `Probe failed: ${err.message}`;
    showToast(`Probe error: ${err.message}`);
  }
}

async function removeExternalMcp(name) {
  try {
    await api("/api/mcp/external/delete", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    showToast(`Removed external MCP '${name}'`);
    loadExternalMcps();
  } catch (err) {
    showToast(`Failed to remove MCP: ${err.message}`);
  }
}

// --- Settings Tab ---
function syncPolicyRoleLock() {
  const roleSelect = document.getElementById("settings-user-role");
  const badge = document.getElementById("policy-role-lock-badge");
  const isReviewer = roleSelect && roleSelect.value === "reviewer";

  ["settings-default-worker-model", "settings-allow-argon-override", "settings-policy-notes"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.disabled = !isReviewer;
  });

  if (badge) {
    badge.textContent = isReviewer ? "UNLOCKED (REVIEWER)" : "LOCKED (REQUIRES REVIEWER ROLE)";
    badge.className = `status-tag ${isReviewer ? "completed" : "blocked"}`;
  }
}

async function loadSettings() {
  try {
    const data = await api("/api/settings");
    renderWorkingDirectory(data);
    const workdirInput = document.getElementById("settings-workdir-input");
    const extJsonEl = document.getElementById("settings-external-mcp-json");
    const defModelEl = document.getElementById("settings-default-worker-model");
    const argonEl = document.getElementById("settings-allow-argon-override");
    const notesEl = document.getElementById("settings-policy-notes");

    if (workdirInput) workdirInput.value = data.working_directory || "";
    if (extJsonEl) extJsonEl.value = JSON.stringify(data.external_mcp_servers || {}, null, 2);
    const policy = data.model_policy_overrides || {};
    if (defModelEl && policy.default_worker_model) defModelEl.value = policy.default_worker_model;
    if (argonEl) argonEl.value = String(Boolean(policy.allow_argon_worker_override));
    if (notesEl) notesEl.value = policy.notes || "";
    syncPolicyRoleLock();
  } catch (err) {
    showToast(`Failed to load settings: ${err.message}`);
  }
}

async function handleSaveSettings(ev) {
  ev.preventDefault();
  const role = document.getElementById("settings-user-role")?.value || "operator";
  const workdir = document.getElementById("settings-workdir-input").value.trim();
  const extRaw = document.getElementById("settings-external-mcp-json").value.trim();
  const banner = document.getElementById("settings-status-banner");

  let externalServers = {};
  if (extRaw) {
    try {
      externalServers = JSON.parse(extRaw);
    } catch (err) {
      showToast(`Invalid External MCP JSON: ${err.message}`);
      return;
    }
  }

  const payload = {
    working_directory: workdir,
    external_mcp_servers: externalServers,
    user_role: role,
  };

  if (role === "reviewer") {
    payload.model_policy_overrides = {
      default_worker_model: document.getElementById("settings-default-worker-model").value,
      allow_argon_worker_override: document.getElementById("settings-allow-argon-override").value === "true",
      notes: document.getElementById("settings-policy-notes").value.trim(),
    };
  }

  try {
    const updated = await api("/api/settings", {
      method: "POST",
      headers: { "X-Orchestra-Role": role },
      body: JSON.stringify(payload),
    });
    renderWorkingDirectory(updated);
    if (banner) banner.textContent = `✓ Saved settings to SQLite DB at ${updated.updated_at_utc}`;
    showToast("Settings saved to DB!");
  } catch (err) {
    if (banner) banner.textContent = `✗ Rejected: ${err.message}`;
    showToast(`Settings error: ${err.message}`);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".nav-pill[data-tab]").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  const sidebarWorkdirBox = document.getElementById("sidebar-workdir-box");
  if (sidebarWorkdirBox) {
    sidebarWorkdirBox.addEventListener("click", () => switchTab("settings"));
  }

  const modelSelect = document.getElementById("model-tier");
  if (modelSelect) {
    updateModelBadge(modelSelect.value);
    modelSelect.addEventListener("change", () => updateModelBadge(modelSelect.value));
  }
  document.getElementById("create-job-form").addEventListener("submit", handleCreateJob);
  document.getElementById("refresh-job-btn").addEventListener("click", () => {
    if (currentJob) loadJob(currentJob.job_id);
  });

  document.querySelectorAll(".copy-prompt-btn").forEach((btn) => {
    btn.addEventListener("click", () => handleCopyPrompt(btn.dataset.role));
  });

  document.querySelectorAll(".submit-reply-btn").forEach((btn) => {
    btn.addEventListener("click", () => handleSubmitReply(btn.dataset.role));
  });

  document.getElementById("copy-bundle-btn").addEventListener("click", async () => {
    const text = document.getElementById("bundle-preview").textContent;
    try {
      await navigator.clipboard.writeText(text);
      showToast("Verified bundle copied to clipboard!");
    } catch (_) {
      showToast("Clipboard write permission blocked");
    }
  });

  document.getElementById("download-bundle-btn").addEventListener("click", () => {
    const text = document.getElementById("bundle-preview").textContent;
    const blob = new Blob([text], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `bundle_${currentJob?.job_id || "synthesis"}.json`;
    a.click();
    URL.revokeObjectURL(url);
  });

  document.getElementById("refresh-registry-btn").addEventListener("click", loadRegistry);
  document.getElementById("add-asset-form").addEventListener("submit", handleAddAsset);

  // MCP Integrations bindings
  const lnwjudBtn = document.getElementById("load-lnwjud-preset-btn");
  if (lnwjudBtn) {
    lnwjudBtn.addEventListener("click", () => {
      document.getElementById("mcp-server-name").value = "lnwjud";
      document.getElementById("mcp-server-command").value = "npx";
      document.getElementById("mcp-server-args").value = "-y @engasnm111/lnwjud";
      document.getElementById("mcp-server-desc").value = "Lnwjud External MCP Server (@engasnm111/lnwjud)";
      document.getElementById("mcp-server-enabled").value = "true";
      showToast("Loaded Lnwjud preset! Click 'Save MCP Server' to register.");
    });
  }
  const refreshMcpBtn = document.getElementById("refresh-mcp-btn");
  if (refreshMcpBtn) refreshMcpBtn.addEventListener("click", loadExternalMcps);
  const regMcpForm = document.getElementById("register-mcp-form");
  if (regMcpForm) regMcpForm.addEventListener("submit", handleRegisterMcp);
  const probeFormBtn = document.getElementById("probe-mcp-form-btn");
  if (probeFormBtn) {
    probeFormBtn.addEventListener("click", async () => {
      const name = document.getElementById("mcp-server-name").value.trim() || "custom";
      const command = document.getElementById("mcp-server-command").value.trim();
      const args = parseArgsString(document.getElementById("mcp-server-args").value);
      const out = document.getElementById("mcp-probe-output");
      if (out) out.textContent = `Probing '${name}' (${command})...`;
      try {
        const res = await api("/api/mcp/external/probe", {
          method: "POST",
          body: JSON.stringify({ name, command, args, live_handshake: false }),
        });
        if (out) out.textContent = JSON.stringify(res.probe, null, 2);
      } catch (err) {
        if (out) out.textContent = `Probe failed: ${err.message}`;
      }
    });
  }

  // Settings bindings
  const roleSelect = document.getElementById("settings-user-role");
  if (roleSelect) roleSelect.addEventListener("change", syncPolicyRoleLock);
  const reloadSettingsBtn = document.getElementById("reload-settings-btn");
  if (reloadSettingsBtn) reloadSettingsBtn.addEventListener("click", loadSettings);
  const settingsForm = document.getElementById("settings-form");
  if (settingsForm) settingsForm.addEventListener("submit", handleSaveSettings);

  loadWorkingDirectory();
  loadRecentJobs();
  loadRegistry();
});
