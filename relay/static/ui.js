import { state, elements, controlToken } from "./state.js";

function showView(name) {
  const view = elements.views[name] ? name : "receive";
  for (const [key, panel] of Object.entries(elements.views)) panel.hidden = key !== view;
  elements.viewLabel.textContent = { receive: "Nhận", send: "Gửi", activity: "Lịch sử" }[view];
  elements.nav.style.setProperty("--nav-index", { receive: 0, send: 1, activity: 2 }[view]);
  elements.viewButtons.forEach((button) => {
    const active = button.dataset.view === view;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
}

async function api(path, options = {}) {
  const { timeoutMs = 8000, ...fetchOptions } = options;
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  let response;
  try {
    response = await fetch(path, {
      ...fetchOptions,
      signal: controller.signal,
      headers: {
        ...(fetchOptions.body instanceof Blob ? { "Content-Type": "application/octet-stream" } : {}),
        ...(fetchOptions.method && fetchOptions.method !== "GET"
          ? { "X-Relay-Control-Token": controlToken }
          : {}),
        ...(fetchOptions.headers || {}),
      },
    });
  } catch (error) {
    if (error.name === "AbortError") {
      throw new Error("Relay chưa phản hồi. Đang thử lại…");
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || "Không thực hiện được thao tác này.");
  }
  if (response.status === 204) {
    return null;
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatBytes(value) {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function formatSpeed(value) {
  if (!value) return "đang tính";
  const units = ["B/s", "KB/s", "MB/s", "GB/s"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** index).toFixed(index > 1 ? 1 : 0)} ${units[index]}`;
}

function percent(transferred, total) {
  if (!total) return 0;
  return Math.max(0, Math.min(100, Math.round((transferred / total) * 100)));
}

function fileName(path) {
  const parts = path.replaceAll("\\", "/").split("/");
  return parts.at(-1) || path;
}

function inferBatchName() {
  if (state.staged.length === 1) return fileName(state.staged[0].relative_path);
  const roots = new Set(state.staged.map((item) => item.relative_path.replaceAll("\\", "/").split("/")[0]));
  if (roots.size === 1) return [...roots][0];
  return `${fileName(state.staged[0].relative_path)} +${state.staged.length - 1} tệp`;
}

function showToast(message, error = false) {
  elements.toast.textContent = message;
  elements.toast.classList.toggle("error", error);
  elements.toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    elements.toast.hidden = true;
  }, 4200);
}

function selectedPeers() {
  return state.devices.filter((peer) => state.selectedPeerIds.has(peer.id));
}

function peerAlreadySending(peer) {
  return state.outgoing.some((transfer) => isSending(transfer)
    && transfer.peer_id === peer.id
    && state.staged.some((item) => transfer.item_ids.includes(item.id)));
}

function isSending(transfer) {
  return ["preparing", "sending", "waiting"].includes(transfer.status);
}

function formatDuration(seconds) {
  const value = Math.max(0, Math.ceil(seconds));
  if (value < 60) return `${value} giây`;
  if (value < 3600) return `${Math.floor(value / 60)} phút ${value % 60} giây`;
  return `${Math.floor(value / 3600)} giờ ${Math.floor(value % 3600 / 60)} phút`;
}

function askNotificationsOnce() {
  if (!("Notification" in window) || !window.isSecureContext || Notification.permission !== "default") return;
  try {
    if (localStorage.getItem("relay-notifications-asked")) return;
    localStorage.setItem("relay-notifications-asked", "1");
  } catch (_) { /* Storage may be disabled; the prompt still works for this visit. */ }
  elements.notificationDialog.showModal();
}

function notifyReceived(message) {
  showToast(message);
  if ("Notification" in window && Notification.permission === "granted") {
    try { new Notification("Relay · Tệp mới", { body: message }); } catch (_) { /* Keep the toast. */ }
  }
}

export {
  showView,
  api,
  escapeHtml,
  formatBytes,
  formatSpeed,
  percent,
  fileName,
  inferBatchName,
  showToast,
  selectedPeers,
  peerAlreadySending,
  isSending,
  formatDuration,
  askNotificationsOnce,
  notifyReceived
};
