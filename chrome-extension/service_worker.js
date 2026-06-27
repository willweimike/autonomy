let nativePort = null;
let panelPort = null;

function connectNative() {
  if (nativePort) return nativePort;
  nativePort = chrome.runtime.connectNative("com.autonomy.app");
  nativePort.onMessage.addListener((message) => {
    if (panelPort) panelPort.postMessage(message);
  });
  nativePort.onDisconnect.addListener(() => {
    nativePort = null;
    if (panelPort) panelPort.postMessage({ ok: false, error: "Autonomy native host disconnected" });
  });
  return nativePort;
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "autonomy-panel") return;
  if (panelPort && panelPort !== port) {
    port.postMessage({ ok: false, error: "Another Autonomy panel is already connected" });
    port.disconnect();
    return;
  }
  panelPort = port;
  port.onDisconnect.addListener(() => {
    if (panelPort === port) panelPort = null;
  });
  port.onMessage.addListener((message) => {
    const type = message && message.type;
    if (
      type !== "status" &&
      type !== "session.start" &&
      type !== "chat.send" &&
      type !== "run.inspect" &&
      type !== "approval.respond"
    ) {
      port.postMessage({ ok: false, error: "unknown panel message type" });
      return;
    }
    connectNative().postMessage(message);
  });
});
