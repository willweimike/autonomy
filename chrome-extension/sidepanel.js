const port = chrome.runtime.connect({ name: "autonomy-panel" });
let nativeConnected = false;
let sessionId = "";
let lastRunId = "";
let busy = false;
let pendingApprovalId = "";

const messages = document.getElementById("messages");
const emptyState = document.getElementById("empty-state");
const workspace = document.getElementById("workspace");
const maxSteps = document.getElementById("max-steps");
const runIdInput = document.getElementById("run-id");
const promptBox = document.getElementById("prompt");
const sendButton = document.getElementById("send");
const startSessionButton = document.getElementById("start-session");
const sessionStatus = document.getElementById("session-status");
const runMetadata = document.getElementById("run-metadata");
const busyIndicator = document.getElementById("busy-indicator");
const approvalModal = document.getElementById("approval-modal");
const approvalMessage = document.getElementById("approval-message");
const approvalAllow = document.getElementById("approval-allow");
const approvalDeny = document.getElementById("approval-deny");

function updateControls() {
  const hasPrompt = promptBox.value.trim().length > 0;
  sendButton.disabled = !sessionId || !hasPrompt || busy;
  startSessionButton.disabled = busy;
  busyIndicator.hidden = !busy;
}

function setStatus(text, tone) {
  sessionStatus.textContent = text;
  sessionStatus.className = `status-pill ${tone}`;
}

function setBusy(value) {
  busy = value;
  updateControls();
}

function persistSettings() {
  chrome.storage.local.set({
    workspace: workspace.value,
    maxSteps: maxSteps.value,
  });
}

function restoreSettings() {
  chrome.storage.local.get(["workspace", "maxSteps"], (settings) => {
    if (settings.workspace) {
      workspace.value = settings.workspace;
    }
    if (settings.maxSteps) {
      maxSteps.value = settings.maxSteps;
    }
    updateControls();
  });
}

function append(role, text, metadata = []) {
  const item = document.createElement("article");
  item.className = `message ${role}`;

  const label = document.createElement("div");
  label.className = "message-label";
  label.textContent = role;

  const body = document.createElement("div");
  body.className = "message-body";
  body.textContent = text || "";

  item.append(label, body);
  if (metadata.length) {
    const chips = document.createElement("div");
    chips.className = "message-metadata";
    for (const entry of metadata) {
      const chip = document.createElement("span");
      chip.className = "metadata-chip";
      chip.textContent = entry;
      chips.appendChild(chip);
    }
    item.appendChild(chips);
  }

  emptyState.hidden = true;
  messages.appendChild(item);
  messages.scrollTop = messages.scrollHeight;
}

function setRunMetadata(message) {
  runMetadata.textContent = message || "";
}

function send(message) {
  port.postMessage(message);
}

port.onMessage.addListener((message) => {
  if (!message.ok) {
    setBusy(false);
    setStatus(nativeConnected ? "Host error" : "Disconnected", nativeConnected ? "warning" : "disconnected");
    append("error", message.error || "Unknown error");
    return;
  }
  if (message.type === "status.result") {
    nativeConnected = true;
    setStatus(sessionId ? "Session ready" : "Host connected", sessionId ? "ready" : "connected");
    append(
      "system",
      `status: host connected; active sessions=${message.sessions}. This is host/session count only, not model/tool status.`,
    );
  } else if (message.type === "session.started") {
    sessionId = message.session_id;
    nativeConnected = true;
    setStatus("Session ready", "ready");
    persistSettings();
    append("system", `session: ${sessionId}`);
  } else if (message.type === "chat.result") {
    setBusy(false);
    lastRunId = message.run_id || lastRunId;
    if (message.run_id) {
      runIdInput.value = message.run_id;
    }
    const stepCount = message.steps_executed ?? message.steps;
    const steps = stepCount === undefined ? undefined : `steps=${stepCount}`;
    const metadata = [
      message.run_id ? `run_id=${message.run_id}` : "",
      message.termination ? `termination=${message.termination}` : "",
      steps || "",
    ].filter(Boolean);
    append("assistant", message.reply, metadata);
    setRunMetadata(metadata.join("  "));
  } else if (message.type === "run.inspect.result") {
    append("system", JSON.stringify(message.run, null, 2));
  } else if (message.type === "approval.requested") {
    pendingApprovalId = message.approval_id;
    approvalMessage.textContent = message.message;
    approvalModal.showModal();
  }
  updateControls();
});

document.getElementById("status").addEventListener("click", () => {
  send({ type: "status" });
});

document.getElementById("start-session").addEventListener("click", () => {
  persistSettings();
  send({
    type: "session.start",
    workspace: workspace.value,
    max_steps: Number(maxSteps.value || 12),
  });
});

function submitPrompt() {
  if (busy) {
    append("system", "session is busy");
    return;
  }
  const text = promptBox.value.trim();
  if (!sessionId || !text) return;
  append("user", text);
  promptBox.value = "";
  setBusy(true);
  send({ type: "chat.send", session_id: sessionId, text });
}

sendButton.addEventListener("click", submitPrompt);

document.getElementById("inspect-run").addEventListener("click", () => {
  const run_id = runIdInput.value.trim() || lastRunId;
  if (!run_id) return;
  send({ type: "run.inspect", run_id });
});

approvalAllow.addEventListener("click", () => {
  send({ type: "approval.respond", approval_id: pendingApprovalId, decision: "allow" });
  pendingApprovalId = "";
  approvalModal.close();
  updateControls();
});

approvalDeny.addEventListener("click", () => {
  send({ type: "approval.respond", approval_id: pendingApprovalId, decision: "deny" });
  pendingApprovalId = "";
  approvalModal.close();
  updateControls();
});

promptBox.addEventListener("input", updateControls);
workspace.addEventListener("change", persistSettings);
maxSteps.addEventListener("change", persistSettings);
promptBox.addEventListener("keydown", (event) => {
  const isShiftEnter = event.shiftKey;
  if (event.key === "Enter" && !isShiftEnter) {
    event.preventDefault();
    submitPrompt();
  }
});

port.onDisconnect.addListener(() => {
  nativeConnected = false;
  sessionId = "";
  setBusy(false);
  setStatus("Disconnected", "disconnected");
  append("error", chrome.runtime.lastError?.message || "Native host disconnected");
});

restoreSettings();
updateControls();
