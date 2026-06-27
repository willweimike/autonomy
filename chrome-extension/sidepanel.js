const port = chrome.runtime.connect({ name: "autonomy-panel" });
let sessionId = "";
let pendingApprovalId = "";

const messages = document.getElementById("messages");
const workspace = document.getElementById("workspace");
const maxSteps = document.getElementById("max-steps");
const runIdInput = document.getElementById("run-id");
const promptBox = document.getElementById("prompt");
const approvalModal = document.getElementById("approval-modal");
const approvalMessage = document.getElementById("approval-message");
let lastRunId = "";

function append(role, text) {
  const item = document.createElement("article");
  item.className = role;
  item.textContent = text;
  messages.appendChild(item);
  messages.scrollTop = messages.scrollHeight;
}

function send(message) {
  port.postMessage(message);
}

port.onMessage.addListener((message) => {
  if (!message.ok) {
    append("error", message.error || "Unknown error");
    return;
  }
  if (message.type === "status.result") {
    append("system", `status: ${JSON.stringify(message)}`);
  } else if (message.type === "session.started") {
    sessionId = message.session_id;
    append("system", `session: ${sessionId}`);
  } else if (message.type === "chat.result") {
    lastRunId = message.run_id || lastRunId;
    if (message.run_id) {
      runIdInput.value = message.run_id;
    }
    append("assistant", `${message.reply}\nrun_id=${message.run_id} termination=${message.termination}`);
  } else if (message.type === "run.inspect.result") {
    append("system", JSON.stringify(message.run, null, 2));
  } else if (message.type === "approval.requested") {
    pendingApprovalId = message.approval_id;
    approvalMessage.textContent = message.message;
    approvalModal.showModal();
  }
});

document.getElementById("status").addEventListener("click", () => {
  send({ type: "status" });
});

document.getElementById("start-session").addEventListener("click", () => {
  send({
    type: "session.start",
    workspace: workspace.value,
    max_steps: Number(maxSteps.value || 12),
  });
});

document.getElementById("send").addEventListener("click", () => {
  const text = promptBox.value.trim();
  if (!sessionId || !text) return;
  append("user", text);
  promptBox.value = "";
  send({ type: "chat.send", session_id: sessionId, text });
});

document.getElementById("inspect-run").addEventListener("click", () => {
  const run_id = runIdInput.value.trim() || lastRunId;
  if (!run_id) return;
  send({ type: "run.inspect", run_id });
});

document.getElementById("approval-allow").addEventListener("click", () => {
  send({ type: "approval.respond", approval_id: pendingApprovalId, decision: "allow" });
});

document.getElementById("approval-deny").addEventListener("click", () => {
  send({ type: "approval.respond", approval_id: pendingApprovalId, decision: "deny" });
});
