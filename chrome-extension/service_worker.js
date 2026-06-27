let nativePort = null;
const panelPorts = new Set();

function connectNative() {
  if (nativePort) return nativePort;
  nativePort = chrome.runtime.connectNative("com.autonomy.app");
  nativePort.onMessage.addListener((message) => {
    for (const port of panelPorts) port.postMessage(message);
  });
  nativePort.onDisconnect.addListener(() => {
    nativePort = null;
    for (const port of panelPorts) {
      port.postMessage({ ok: false, error: "Autonomy native host disconnected" });
    }
  });
  return nativePort;
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "autonomy-panel") return;
  panelPorts.add(port);
  port.onDisconnect.addListener(() => panelPorts.delete(port));
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
