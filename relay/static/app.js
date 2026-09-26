const state = {
  status: null,
  devices: [],
  peers: [],
  staged: [],
  outgoing: [],
  incoming: [],
  phoneUploads: [],
  phoneConnection: null,
  hasSnapshot: false,
  selectedPeerId: null,
  ticket: null,
  uploading: false,
  uploadProgress: null,
  phoneInvite: null,
  ticketDialogOpen: false,
  historyFilter: "all",
  completedAt: new Map(),
};

const controlToken = document.querySelector('meta[name="relay-control-token"]').content;

const elements = {
  viewButtons: document.querySelectorAll("[data-view]"),
  viewLabel: document.querySelector("#currentViewLabel"),
  nav: document.querySelector(".view-nav"),
  views: {
    receive: document.querySelector("#receiveView"),
    send: document.querySelector("#sendView"),
    activity: document.querySelector("#activityView"),
  },
  deviceName: document.querySelector("#deviceName"),
  deviceAddress: document.querySelector("#deviceAddress"),
  discoveryText: document.querySelector("#discoveryText"),
  discoveryLamp: document.querySelector("#discoveryLamp"),
  deviceList: document.querySelector("#deviceList"),
  pairingOptions: document.querySelector("#pairingOptions"),
  pairForm: document.querySelector("#pairForm"),
  pairingCode: document.querySelector("#pairingCode"),
  pairMessage: document.querySelector("#pairMessage"),
  createCodeButton: document.querySelector("#createCodeButton"),
  phoneConnectButton: document.querySelector("#phoneConnectButton"),
  phoneConnectionStatus: document.querySelector("#phoneConnectionStatus"),
  phoneHeaderStatus: document.querySelector("#phoneHeaderStatus"),
  phoneInvite: document.querySelector("#phoneInvite"),
  closePhoneInviteButton: document.querySelector("#closePhoneInviteButton"),
  phoneInviteQr: document.querySelector("#phoneInviteQr"),
  phoneInviteLink: document.querySelector("#phoneInviteLink"),
  phoneDisconnectButton: document.querySelector("#phoneDisconnectButton"),
  phoneUploadCount: document.querySelector("#phoneUploadCount"),
  enableNotificationsButton: document.querySelector("#enableNotificationsButton"),
  phoneUploadsList: document.querySelector("#phoneUploadsList"),
  pairingTicket: document.querySelector("#pairingTicket"),
  closePairingTicketButton: document.querySelector("#closePairingTicketButton"),
  pairingQr: document.querySelector("#pairingQr"),
  ticketCode: document.querySelector("#ticketCode"),
  ticketCountdown: document.querySelector("#ticketCountdown"),
  newCodeButton: document.querySelector("#newCodeButton"),
  scanCodeButton: document.querySelector("#scanCodeButton"),
  scanPanel: document.querySelector("#scanPanel"),
  scanVideo: document.querySelector("#scanVideo"),
  scanMessage: document.querySelector("#scanMessage"),
  closeScannerButton: document.querySelector("#closeScannerButton"),
  transferSubtitle: document.querySelector("#transferSubtitle"),
  targetStamp: document.querySelector("#targetStamp strong"),
  fileInput: document.querySelector("#fileInput"),
  folderInput: document.querySelector("#folderInput"),
  chooseFilesButton: document.querySelector("#chooseFilesButton"),
  chooseFolderButton: document.querySelector("#chooseFolderButton"),
  dropZone: document.querySelector("#dropZone"),
  stagedList: document.querySelector("#stagedList"),
  queueSummary: document.querySelector("#queueSummary"),
  clearStagedButton: document.querySelector("#clearStagedButton"),
  sendButton: document.querySelector("#sendButton"),
  historyList: document.querySelector("#historyList"),
  historyFilters: document.querySelectorAll("[data-history-filter]"),
  destinationInput: document.querySelector("#destinationInput"),
  settingsForm: document.querySelector("#settingsForm"),
  settingsMessage: document.querySelector("#settingsMessage"),
  fingerprint: document.querySelector("#fingerprint"),
  toast: document.querySelector("#toast"),
};

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

function setPairMessage(message, error = false) {
  elements.pairMessage.textContent = message;
  elements.pairMessage.classList.toggle("error", error);
}

function renderStatus() {
  if (!state.status) return;
  const { device, addresses, destination } = state.status;
  elements.deviceName.textContent = device.name;
  elements.deviceAddress.textContent = addresses.find((address) => !address.startsWith("127.")) || "Máy này";
  elements.fingerprint.textContent = device.fingerprint;
  if (document.activeElement !== elements.destinationInput) elements.destinationInput.value = destination;
  const hasDevices = state.devices.length > 0;
  elements.discoveryText.textContent = hasDevices ? `${state.devices.length} máy gần đây` : "Đang tìm";
  elements.discoveryLamp.style.background = hasDevices ? "var(--green)" : "var(--blue)";
}

function renderPhoneConnection() {
  const status = state.phoneConnection?.status;
  elements.phoneConnectionStatus.textContent = status === "connected"
    ? "Điện thoại đã kết nối. Có thể gửi hoặc nhận tệp."
    : status === "waiting"
      ? "Đang chờ điện thoại quét mã QR."
      : "Chưa có điện thoại kết nối.";
  elements.phoneConnectionStatus.classList.toggle("is-connected", status === "connected");
  elements.phoneHeaderStatus.hidden = status !== "connected";
  elements.phoneDisconnectButton.hidden = status !== "connected" && status !== "waiting";
  elements.phoneDisconnectButton.textContent = status === "waiting" ? "Hủy mã QR" : "Ngắt kết nối";
}

function renderDevices() {
  if (!state.hasSnapshot) return;
  if (!state.devices.length) {
    elements.deviceList.innerHTML = `
      <p class="empty-message">Chưa thấy máy tính nào. Hãy mở Relay trên máy còn lại và kiểm tra cả hai dùng chung mạng.</p>`;
    return;
  }
  const pairedIds = new Set(state.peers.map((peer) => peer.id));
  elements.deviceList.innerHTML = state.devices.map((device) => {
    const connected = pairedIds.has(device.id);
    const selected = state.selectedPeerId === device.id;
    return `
      <button class="device-row${selected ? " selected" : ""}" type="button" data-device-id="${escapeHtml(device.id)}" aria-pressed="${selected}">
        <span class="device-lamp" aria-hidden="true"></span>
        <span>
          <span class="device-name">${escapeHtml(device.name)}</span>
          <span class="device-address">${escapeHtml(device.host)}:${device.port} · ${escapeHtml(device.fingerprint.slice(0, 11))}</span>
        </span>
        <span class="device-state">${connected ? "Đã kết nối" : selected ? "Đã chọn" : "Sẵn sàng"}</span>
      </button>`;
  }).join("");
}

function renderTarget() {
  const peer = state.peers.find((candidate) => candidate.id === state.selectedPeerId);
  elements.targetStamp.textContent = peer?.name || "Chưa chọn";
  elements.transferSubtitle.textContent = peer
    ? `Các tệp sẽ được gửi trực tiếp đến ${peer.name}. Tệp đã chọn cũng hiện trên điện thoại đã kết nối để tải về.`
    : "Chọn máy tính đã kết nối để gửi. Nếu gửi cho điện thoại, tệp được chọn sẽ hiện trên điện thoại để tải về.";
  const ready = Boolean(peer && state.staged.length && !state.uploading);
  elements.sendButton.disabled = !ready;
  elements.sendButton.querySelector("span").textContent = peer ? `Gửi đến ${peer.name}` : "Chọn máy tính nhận";
}

function renderStaged() {
  const total = state.staged.reduce((sum, item) => sum + item.size, 0);
  if (state.uploading && state.uploadProgress) {
    elements.queueSummary.textContent = `Đang tải ${state.uploadProgress.name} · ${percent(state.uploadProgress.loaded, state.uploadProgress.total)}%`;
  } else {
    elements.queueSummary.textContent = state.staged.length
      ? `${state.staged.length} tệp · ${formatBytes(total)}`
      : "Chưa chọn tệp";
  }
  elements.clearStagedButton.disabled = !state.staged.length || state.uploading;
  elements.dropZone.classList.toggle("has-files", state.staged.length > 0);
  if (!state.staged.length) {
    elements.stagedList.innerHTML = `<p class="empty-message">${state.uploading ? "Đang thêm tệp…" : "Tệp đã chọn sẽ hiển thị tại đây."}</p>`;
    return;
  }
  elements.stagedList.innerHTML = state.staged.map((item) => `
    <div class="file-card">
      <span class="file-card-icon" aria-hidden="true">
        <svg viewBox="0 0 20 20"><path d="M5 2h7l3 3v13H5z"></path><path d="M12 2v4h4"></path></svg>
      </span>
      <span>
        <span class="file-card-name" title="${escapeHtml(item.relative_path)}">${escapeHtml(fileName(item.relative_path))}</span>
        <span class="file-card-size">${escapeHtml(item.relative_path)} · ${formatBytes(item.size)}</span>
      </span>
      <button class="file-card-remove" type="button" data-remove-item="${escapeHtml(item.id)}" aria-label="Bỏ ${escapeHtml(fileName(item.relative_path))}">
        <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M4 4l12 12M16 4L4 16"></path></svg>
      </button>
    </div>`).join("");
}

const lastRenderedHtml = new WeakMap();
function setHtmlIfChanged(element, html) {
  if (lastRenderedHtml.get(element) === html) return;
  element.innerHTML = html;
  lastRenderedHtml.set(element, html);
}

function transferEntries() {
  const outgoing = state.outgoing.map((transfer) => ({
    kind: "sent", id: `sent:${transfer.id}`, time: Number(transfer.created_at) || 0,
    name: transfer.batch_name, counterpart: transfer.peer_name, size: transfer.total_bytes,
    status: transfer.status, transferred: transfer.sent_bytes, speed: transfer.speed_bps,
    error: transfer.error, transferId: transfer.id, items: [],
  }));
  const incoming = state.incoming.map((transfer) => {
    const items = transfer.items || [];
    return {
      kind: "received", id: `received:${transfer.id}`, time: Number(transfer.created_at) || 0,
      name: items.length === 1 ? items[0].name : transfer.batch_name,
      counterpart: transfer.source?.name || "Máy tính khác", size: transfer.total_bytes,
      status: transfer.status, transferred: transfer.received_bytes, speed: transfer.speed_bps,
      error: transfer.error, transferId: transfer.id, items,
    };
  });
  const phone = state.phoneUploads.map((item) => ({
    kind: "received", id: `phone:${item.id}`, time: Number(item.received_at) || 0,
    name: item.name, counterpart: "Điện thoại", size: item.size,
    status: "complete", transferred: item.size, speed: 0,
    error: "", phoneItem: item, items: [],
  }));
  return [...outgoing, ...incoming, ...phone].sort((a, b) => b.time - a.time);
}

function entryStatus(entry) {
  const label = entry.status === "complete" ? "Hoàn tất"
    : entry.status === "failed" ? "Lỗi"
      : entry.status === "waiting" ? "Chờ tiếp tục"
        : entry.kind === "sent" ? "Đang gửi" : "Đang nhận";
  const recentlyCompleted = Date.now() - (state.completedAt.get(entry.id) || 0) < 1200;
  return `<span class="transfer-state ${escapeHtml(entry.status)}${recentlyCompleted ? " new-complete" : ""}">${entry.status === "complete" ? '<span class="check" aria-hidden="true">✓</span>' : ""}${label}</span>`;
}

function entryActions(entry, includeCopy = false) {
  if (entry.phoneItem) {
    const item = entry.phoneItem;
    return item.available
      ? `<button class="row-action" type="button" data-open-phone-upload="${escapeHtml(item.id)}">Mở tệp</button>${includeCopy ? `<button class="row-action" type="button" data-copy-phone-upload="${escapeHtml(item.id)}">Sao chép đường dẫn</button>` : ""}`
      : '<span class="row-meta">Tệp không còn trên máy</span>';
  }
  if (entry.kind === "sent") {
    return entry.status === "failed"
      ? `<button class="retry-button" type="button" data-retry-transfer="${escapeHtml(entry.transferId)}">Thử gửi lại</button>`
      : "";
  }
  const completed = entry.items.filter((item) => item.completed);
  return completed.map((item) => item.available
    ? `<button class="received-file-link" type="button" data-open-computer-transfer="${escapeHtml(entry.transferId)}" data-open-computer-item="${escapeHtml(item.id)}" title="Mở ${escapeHtml(item.relative_path)} bằng ứng dụng trên máy">${completed.length === 1 ? "Mở tệp" : escapeHtml(item.name)}</button>`
    : `<span class="row-meta">${escapeHtml(item.name)} · Tệp không còn trên máy</span>`).join("");
}

function entryProgress(entry) {
  if (entry.status === "complete" || entry.status === "failed") return "";
  return `<div class="transfer-track" role="progressbar" data-progress-entry="${escapeHtml(entry.id)}" aria-label="${escapeHtml(entry.name)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div class="transfer-fill" style="transform: scaleX(0)"></div></div>`;
}

function syncActiveProgress(container, entries) {
  const active = new Map(entries.filter((entry) => entry.status !== "complete" && entry.status !== "failed")
    .map((entry) => [entry.id, entry]));
  for (const track of container.querySelectorAll("[data-progress-entry]")) {
    const entry = active.get(track.dataset.progressEntry);
    if (!entry) continue;
    const progress = percent(entry.transferred, entry.size);
    track.setAttribute("aria-valuenow", String(progress));
    track.querySelector(".transfer-fill").style.transform = `scaleX(${progress / 100})`;
    const meta = container.querySelector(`[data-progress-meta="${CSS.escape(entry.id)}"]`);
    if (meta) meta.textContent = `${progress}%${entry.speed ? ` · ${formatSpeed(entry.speed)}` : ""}`;
  }
}

function entryTime(entry) {
  const date = new Date(entry.time * 1000);
  return historyGroup(entry.time) === "Hôm nay"
    ? date.toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleString("vi-VN", { day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function receiveRow(entry) {
  const actions = entryActions(entry, true);
  const detail = entry.status === "complete" ? `Từ ${escapeHtml(entry.counterpart)}`
    : `Từ ${escapeHtml(entry.counterpart)} · <span data-progress-meta="${escapeHtml(entry.id)}"></span>`;
  return `<div class="receive-row">
    <span class="row-direction received" aria-hidden="true">↓</span>
    <div class="row-main"><strong class="row-name" title="${escapeHtml(entry.name)}">${escapeHtml(entry.name)}</strong><span class="row-meta">${detail}</span></div>
    <span class="row-size">${formatBytes(entry.size)}</span><time class="row-time" datetime="${new Date(entry.time * 1000).toISOString()}">${entryTime(entry)}</time>
    <div class="row-actions">${entry.status !== "complete" ? entryStatus(entry) : ""}${actions}</div>
    ${entryProgress(entry)}${entry.error ? `<p class="transfer-error">${escapeHtml(entry.error)}</p>` : ""}
  </div>`;
}

function historyRow(entry) {
  const actions = entryActions(entry);
  const completedItems = entry.items.filter((item) => item.completed);
  const showActionsBelow = completedItems.length > 1 || completedItems.some((item) => !item.available)
    || (entry.phoneItem && !entry.phoneItem.available);
  const detail = `${entry.kind === "sent" ? "Đến" : "Từ"} ${escapeHtml(entry.counterpart)}${entry.status === "complete" ? "" : ` · <span data-progress-meta="${escapeHtml(entry.id)}"></span>`}`;
  return `<div class="history-row">
    <span class="row-direction ${entry.kind}" aria-hidden="true">${entry.kind === "sent" ? "↑" : "↓"}</span>
    <div class="row-main"><strong class="row-name" title="${escapeHtml(entry.name)}">${escapeHtml(entry.name)}</strong><span class="row-meta">${detail}</span></div>
    <span class="row-size">${formatBytes(entry.size)}</span><time class="row-time" datetime="${new Date(entry.time * 1000).toISOString()}">${entryTime(entry)}</time>
    ${entryStatus(entry)}<div class="row-actions">${showActionsBelow ? "" : actions}</div>
    ${showActionsBelow ? `<div class="row-extra">${actions}</div>` : ""}
    ${entryProgress(entry)}${entry.error ? `<p class="transfer-error">${escapeHtml(entry.error)}</p>` : ""}
  </div>`;
}

function historyGroup(time) {
  const today = new Date();
  const date = new Date(time * 1000);
  const startToday = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  if (date.getTime() >= startToday) return "Hôm nay";
  if (date.getTime() >= startToday - 86400000) return "Hôm qua";
  return "Trước đó";
}

function renderTransfers() {
  const entries = transferEntries().filter((entry) => state.historyFilter === "all"
    || (state.historyFilter === "sent" ? entry.kind === "sent" : entry.kind === "received"));
  if (!entries.length) {
    setHtmlIfChanged(elements.historyList, `<p class="empty-message">${state.historyFilter === "sent" ? "Chưa gửi tệp nào." : state.historyFilter === "received" ? "Chưa nhận tệp nào." : "Chưa có lượt truyền tệp nào."}</p>`);
    return;
  }
  let group = "";
  const html = entries.map((entry) => {
    const nextGroup = historyGroup(entry.time);
    const heading = nextGroup === group ? "" : `<div class="history-group-title">${nextGroup}</div>`;
    group = nextGroup;
    return heading + historyRow(entry);
  }).join("");
  setHtmlIfChanged(elements.historyList, html);
  syncActiveProgress(elements.historyList, entries);
}

function updateNotificationButton() {
  if (!("Notification" in window)) {
    elements.enableNotificationsButton.hidden = true;
    return;
  }
  elements.enableNotificationsButton.textContent = Notification.permission === "granted"
    ? "Thông báo đã bật" : "Bật thông báo trên máy tính";
  elements.enableNotificationsButton.disabled = Notification.permission === "granted";
}

function renderPhoneUploads() {
  const entries = transferEntries().filter((entry) => entry.kind === "received");
  elements.phoneUploadCount.textContent = `${entries.length} lượt`;
  setHtmlIfChanged(elements.phoneUploadsList, entries.length
    ? entries.map(receiveRow).join("")
    : '<p class="empty-message">Chưa nhận tệp nào. Kết nối điện thoại hoặc máy tính khác để bắt đầu.</p>');
  syncActiveProgress(elements.phoneUploadsList, entries);
}

function renderTicket() {
  if (!state.ticket) {
    elements.pairingTicket.hidden = true;
    return;
  }
  const remaining = Math.max(0, Math.ceil(state.ticket.expires_at - Date.now() / 1000));
  if (remaining <= 0) {
    state.ticket = null;
    state.ticketDialogOpen = false;
    elements.pairingTicket.hidden = true;
    return;
  }
  elements.pairingTicket.hidden = !state.ticketDialogOpen;
  elements.ticketCode.textContent = state.ticket.code;
  const minutes = Math.floor(remaining / 60);
  const seconds = String(remaining % 60).padStart(2, "0");
  elements.ticketCountdown.textContent = `Hết hạn sau ${minutes}:${seconds}`;
}

function renderAll() {
  renderStatus();
  renderPhoneConnection();
  renderDevices();
  renderTarget();
  renderStaged();
  renderTransfers();
  renderPhoneUploads();
  renderTicket();
}

let refreshPromise = null;

async function refresh() {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    const payload = await api("/api/v1/state");
    const hadSnapshot = state.hasSnapshot;
    const previousStatuses = new Map([
      ...state.outgoing.map((transfer) => [`sent:${transfer.id}`, transfer.status]),
      ...state.incoming.map((transfer) => [`received:${transfer.id}`, transfer.status]),
    ]);
    state.status = payload.status;
    state.devices = payload.devices;
    state.peers = payload.peers;
    state.staged = payload.staged;
    state.outgoing = payload.outgoing;
    state.incoming = payload.incoming;
    const previousPhoneStatus = state.phoneConnection?.status;
    state.phoneConnection = payload.phone_connection;
    const previousIds = new Set(state.phoneUploads.map((item) => item.id));
    const newUploads = (payload.phone_uploads || []).filter((item) => !previousIds.has(item.id));
    state.phoneUploads = payload.phone_uploads || [];
    for (const transfer of [...state.outgoing.map((item) => ({ ...item, key: `sent:${item.id}` })),
      ...state.incoming.map((item) => ({ ...item, key: `received:${item.id}` }))]) {
      if (hadSnapshot && transfer.status === "complete" && previousStatuses.has(transfer.key)
        && previousStatuses.get(transfer.key) !== "complete") {
        state.completedAt.set(transfer.key, Date.now());
      }
    }
    if (state.selectedPeerId && !state.peers.some((peer) => peer.id === state.selectedPeerId)
      && !state.devices.some((device) => device.id === state.selectedPeerId)) {
      state.selectedPeerId = null;
    }
    if (!state.selectedPeerId && state.peers.length) state.selectedPeerId = state.peers[0].id;
    state.hasSnapshot = true;
    renderAll();
    if (hadSnapshot && previousPhoneStatus !== "connected" && state.phoneConnection?.status === "connected") {
      showToast("Điện thoại đã kết nối với Relay.");
    }
    if (hadSnapshot && newUploads.length) {
      const message = newUploads.length === 1 ? `Đã nhận ${newUploads[0].name} từ điện thoại` : `Đã nhận ${newUploads.length} tệp từ điện thoại`;
      showToast(message);
      if ("Notification" in window && Notification.permission === "granted") {
        try { new Notification("Relay · Tệp mới từ điện thoại", { body: message }); } catch (_) { /* The in-page notice remains visible. */ }
      }
    }
  })().finally(() => {
    refreshPromise = null;
  });
  return refreshPromise;
}

async function createCode() {
  try {
    state.ticket = await api("/api/v1/pairing/code", { method: "POST" });
    state.ticketDialogOpen = true;
    elements.pairingQr.src = state.ticket.qr_data_url;
    renderTicket();
    elements.closePairingTicketButton.focus();
    setPairMessage("Quét mã này hoặc nhập mã trên máy tính còn lại.");
  } catch (error) {
    setPairMessage(error.message, true);
  }
}

async function connectPhone() {
  elements.phoneConnectButton.disabled = true;
  try {
    state.phoneInvite = await api("/api/v1/phone/invite", { method: "POST" });
    elements.phoneInviteQr.src = state.phoneInvite.qr_data_url;
    elements.phoneInviteLink.href = state.phoneInvite.url;
    elements.phoneInviteLink.textContent = state.phoneInvite.url;
    elements.phoneInvite.hidden = false;
    elements.closePhoneInviteButton.focus();
    await refresh();
    showToast("Dùng camera điện thoại quét mã QR để kết nối.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    elements.phoneConnectButton.disabled = false;
  }
}

async function disconnectPhone() {
  try {
    await api("/api/v1/phone/revoke", { method: "POST" });
    state.phoneInvite = null;
    elements.phoneInvite.hidden = true;
    elements.phoneInviteQr.removeAttribute("src");
    elements.phoneInviteLink.removeAttribute("href");
    await refresh();
    showToast("Đã ngắt kết nối điện thoại.");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function pairValues(code, endpoint = null, fingerprint = null) {
  if (code.length !== 8) {
    setPairMessage("Nhập đủ 8 ký tự của mã ghép nối.", true);
    return;
  }
  setPairMessage("Đang kiểm tra máy tính…");
  try {
    const peer = await api("/api/v1/pair", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code, endpoint, fingerprint }),
    });
    state.peers.push(peer);
    state.selectedPeerId = peer.id;
    state.ticket = null;
    state.ticketDialogOpen = false;
    elements.pairingCode.value = "";
    setPairMessage(`Đã kết nối với ${peer.name}.`);
    renderAll();
  } catch (error) {
    setPairMessage(error.message, true);
  }
}

async function pairDevice(event) {
  event.preventDefault();
  await pairValues(elements.pairingCode.value.trim().toUpperCase());
}

function parsePairingQr(value) {
  const codeMatch = value.match(/[?&]code=([^&]+)/i);
  const endpointMatch = value.match(/[?&]endpoint=([^&]+)/i);
  const fingerprintMatch = value.match(/[?&]fp=([^&]+)/i);
  if (!codeMatch) return null;
  return {
    code: decodeURIComponent(codeMatch[1]).toUpperCase(),
    endpoint: endpointMatch ? decodeURIComponent(endpointMatch[1]) : null,
    fingerprint: fingerprintMatch ? decodeURIComponent(fingerprintMatch[1]) : null,
  };
}

let scannerFrame = 0;
let scannerStream = null;

async function scanQrCode() {
  if (!navigator.mediaDevices?.getUserMedia || !("BarcodeDetector" in window)) {
    setPairMessage("Trình duyệt không quét được mã QR. Hãy nhập mã ghép nối.", true);
    return;
  }
  try {
    scannerStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
    elements.scanVideo.srcObject = scannerStream;
    elements.scanPanel.hidden = false;
    elements.scanMessage.textContent = "Hướng camera vào mã QR trên máy nhận.";
    await elements.scanVideo.play();
    const detector = new BarcodeDetector({ formats: ["qr_code"] });
    const scan = async () => {
      if (!scannerStream) return;
      const codes = await detector.detect(elements.scanVideo);
      if (codes.length) {
        const parsed = parsePairingQr(codes[0].rawValue);
        if (parsed) {
          stopScanner();
          await pairValues(parsed.code, parsed.endpoint, parsed.fingerprint);
          return;
        }
      }
      scannerFrame = requestAnimationFrame(scan);
    };
    scannerFrame = requestAnimationFrame(scan);
  } catch (error) {
    stopScanner();
    setPairMessage("Không mở được camera. Hãy nhập mã ghép nối.", true);
  }
}

function stopScanner() {
  cancelAnimationFrame(scannerFrame);
  if (scannerStream) {
    scannerStream.getTracks().forEach((track) => track.stop());
    scannerStream = null;
  }
  elements.scanVideo.srcObject = null;
  elements.scanPanel.hidden = true;
}

async function uploadFiles(files) {
  if (!files.length) return;
  const chunkSize = 8 * 1024 * 1024;
  state.uploading = true;
  state.uploadProgress = null;
  renderStaged();
  renderTarget();
  try {
    for (const file of files) {
      const relativePath = file.webkitRelativePath || file.name;
      const uploadId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
      let offset = 0;
      while (offset < file.size || (file.size === 0 && offset === 0)) {
        const chunk = file.slice(offset, Math.min(offset + chunkSize, file.size));
        const isFinal = offset + chunk.size >= file.size;
        state.uploadProgress = { name: file.name, loaded: offset, total: file.size };
        renderStaged();
        const query = new URLSearchParams({
          upload_id: uploadId,
          relative_path: relativePath,
          total_size: String(file.size),
          offset: String(offset),
          final: String(isFinal),
        });
        let result = null;
        for (let attempt = 0; attempt < 2; attempt += 1) {
          try {
            result = await api(`/api/v1/stage/chunk?${query.toString()}`, {
              method: "POST",
              body: chunk,
              timeoutMs: 120000,
            });
            break;
          } catch (error) {
            if (attempt === 1) throw error;
          }
        }
        offset = result.received;
        if (result.item) state.staged.push(result.item);
        renderStaged();
        if (result.complete) break;
      }
    }
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.uploading = false;
    state.uploadProgress = null;
    renderAll();
  }
}

async function send() {
  const peer = state.peers.find((candidate) => candidate.id === state.selectedPeerId);
  if (!peer || !state.staged.length) return;
  try {
    await api("/api/v1/transfers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        peer_id: peer.id,
        batch_name: inferBatchName(),
        item_ids: state.staged.map((item) => item.id),
      }),
    });
    showToast(`Đang gửi tệp đến ${peer.name}.`);
    await refresh();
  } catch (error) {
    showToast(error.message, true);
  }
}

elements.deviceList.addEventListener("click", (event) => {
  const row = event.target.closest("[data-device-id]");
  if (!row) return;
  const deviceId = row.dataset.deviceId;
  const paired = state.peers.find((peer) => peer.id === deviceId);
  state.selectedPeerId = deviceId;
  if (!paired) {
    setPairMessage(`Nhập mã hiển thị trên ${row.querySelector(".device-name").textContent}.`);
    elements.pairingOptions.open = true;
    elements.pairingCode.focus();
  }
  renderAll();
});

elements.viewButtons.forEach((button) => {
  button.addEventListener("click", () => showView(button.dataset.view));
});

elements.stagedList.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-remove-item]");
  if (!button) return;
  try {
    await api(`/api/v1/staged/${encodeURIComponent(button.dataset.removeItem)}`, { method: "DELETE" });
    state.staged = state.staged.filter((item) => item.id !== button.dataset.removeItem);
    renderStaged();
    renderTarget();
  } catch (error) {
    showToast(error.message, true);
  }
});

elements.historyList.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-retry-transfer]");
  if (!button) return;
  button.disabled = true;
  try {
    await api(`/api/v1/transfers/${encodeURIComponent(button.dataset.retryTransfer)}/retry`, { method: "POST" });
    await refresh();
  } catch (error) {
    showToast(error.message, true);
  }
});

elements.clearStagedButton.addEventListener("click", async () => {
  try {
    await api("/api/v1/staged", { method: "DELETE" });
    state.staged = [];
    renderAll();
  } catch (error) {
    showToast(error.message, true);
  }
});

elements.settingsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.settingsMessage.textContent = "Đang lưu…";
  elements.settingsMessage.classList.remove("error");
  try {
    const result = await api("/api/v1/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ destination: elements.destinationInput.value }),
    });
    elements.destinationInput.value = result.destination;
    elements.settingsMessage.textContent = "Đã lưu thư mục nhận tệp.";
  } catch (error) {
    elements.settingsMessage.textContent = error.message;
    elements.settingsMessage.classList.add("error");
  }
});

elements.pairForm.addEventListener("submit", pairDevice);
elements.createCodeButton.addEventListener("click", createCode);
elements.phoneConnectButton.addEventListener("click", connectPhone);
elements.phoneDisconnectButton.addEventListener("click", disconnectPhone);
function closeDialog(backdrop) {
  if (backdrop.hidden) return;
  backdrop.hidden = true;
  if (backdrop === elements.pairingTicket) {
    state.ticketDialogOpen = false;
    elements.createCodeButton.focus();
  } else {
    elements.phoneConnectButton.focus();
  }
}
elements.closePhoneInviteButton.addEventListener("click", () => closeDialog(elements.phoneInvite));
elements.closePairingTicketButton.addEventListener("click", () => closeDialog(elements.pairingTicket));
for (const backdrop of [elements.phoneInvite, elements.pairingTicket]) {
  backdrop.addEventListener("click", (event) => {
    if (event.target !== backdrop) return;
    closeDialog(backdrop);
  });
}
document.addEventListener("keydown", (event) => {
  const backdrop = !elements.phoneInvite.hidden ? elements.phoneInvite
    : !elements.pairingTicket.hidden ? elements.pairingTicket : null;
  if (!backdrop) return;
  if (event.key === "Escape") {
    closeDialog(backdrop);
  } else if (event.key === "Tab") {
    const focusable = [...backdrop.querySelectorAll("button, a[href]")];
    const first = focusable[0];
    const last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
});
elements.historyFilters.forEach((button) => button.addEventListener("click", () => {
  state.historyFilter = button.dataset.historyFilter;
  elements.historyFilters.forEach((filter) => {
    const active = filter === button;
    filter.classList.toggle("is-active", active);
    filter.setAttribute("aria-pressed", String(active));
  });
  renderTransfers();
}));
elements.enableNotificationsButton.addEventListener("click", async () => {
  if (!("Notification" in window)) return;
  const permission = await Notification.requestPermission();
  updateNotificationButton();
  showToast(permission === "granted" ? "Thông báo đã bật." : "Chưa bật thông báo. Tệp mới vẫn hiện trong Hộp thư đến.");
});

async function openReceivedFile(button) {
  const path = button.dataset.openPhoneUpload
    ? `/api/v1/received/phone/${encodeURIComponent(button.dataset.openPhoneUpload)}/open`
    : `/api/v1/received/computer/${encodeURIComponent(button.dataset.openComputerTransfer)}/${encodeURIComponent(button.dataset.openComputerItem)}/open`;
  button.disabled = true;
  try {
    await api(path, { method: "POST" });
    showToast("Đã mở tệp bằng ứng dụng mặc định trên máy.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-open-phone-upload], [data-open-computer-transfer]");
  if (button) openReceivedFile(button);
});
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy-phone-upload]");
  if (!button) return;
  const path = state.phoneUploads.find((item) => item.id === button.dataset.copyPhoneUpload)?.path;
  if (!path) return;
  try {
    await navigator.clipboard.writeText(path);
    showToast("Đã sao chép đường dẫn. Dán vào File Explorer để mở tệp.");
  } catch (_) {
    showToast("Không sao chép được. Hãy chọn đường dẫn hiển thị và sao chép thủ công.", true);
  }
});
elements.newCodeButton.addEventListener("click", createCode);
elements.scanCodeButton.addEventListener("click", scanQrCode);
elements.closeScannerButton.addEventListener("click", stopScanner);
elements.chooseFilesButton.addEventListener("click", () => elements.fileInput.click());
elements.chooseFolderButton.addEventListener("click", () => elements.folderInput.click());
elements.fileInput.addEventListener("change", (event) => uploadFiles(event.target.files));
elements.folderInput.addEventListener("change", (event) => uploadFiles(event.target.files));
elements.sendButton.addEventListener("click", send);

for (const eventName of ["dragenter", "dragover"]) {
  elements.dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    elements.dropZone.classList.add("dragging");
  });
}
for (const eventName of ["dragleave", "drop"]) {
  elements.dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    elements.dropZone.classList.remove("dragging");
  });
}
elements.dropZone.addEventListener("drop", (event) => uploadFiles(event.dataTransfer.files));

let refreshTimer = 0;

async function pollState() {
  try {
    await refresh();
  } catch (error) {
    setPairMessage(error.message, true);
  }
  window.clearTimeout(refreshTimer);
  const hasActiveTransfer = state.outgoing.some((transfer) => ["preparing", "sending"].includes(transfer.status))
    || state.incoming.some((transfer) => transfer.status === "receiving");
  const delay = hasActiveTransfer ? 500 : document.hidden ? 5000 : 2000;
  refreshTimer = window.setTimeout(pollState, delay);
}

document.addEventListener("visibilitychange", () => {
  window.clearTimeout(refreshTimer);
  pollState();
});
window.addEventListener("online", pollState);
window.setInterval(renderTicket, 1000);
updateNotificationButton();
showView("receive");
pollState();
